"""一键求助端点 `POST /api/sos` 的接口级测试（FR-ALT-06 / FR-UI-08，task-77）。

## 这一层测什么、不测什么

- **测**：HTTP 契约本身——设备令牌鉴权、`elder_id` 只从设备绑定取（请求体带来的
  elder_id 必须被忽略）、一次求助落成「风险事件 + 红单 + 通知」、同一自然分钟内重复按
  不重复建单（且回原单号）、未绑长者 409、无令牌 401、`note` 超长 422。
- **不测**：`alert_service` 内部的脱敏、严重度映射、幂等键算法（`tests/test_alert_service.py`
  已覆盖）。这里只验证 SOS 端点把「按键」翻译成一张什么样的单据、以及它有没有绕开服务层。

## 两个刻意的决定

1. **时钟冻结**：`app.api.sos` 里 `SafetyHit.occurred_at = datetime.now(UTC)` 是 PRD 允许的
   唯一「服务端填 now」场景，但测试直接跑真时钟会在分钟边界上偶发失败（08:00:59 与
   08:01:01 是两个幂等桶）。这里把 `sos.datetime` 换成一个只实现 `now()` 的替身，
   让「同一分钟」这件事变成确定条件。
2. **不用 mock 服务层**：本文件跑真 `alert_service` + 真数据库事务（外层事务最后 rollback），
   因为要验的正是「三张表都写了、且 type 是 sos」，这是端点的核心产出，mock 掉就等于没测。

无 PostgreSQL 时由 conftest 的 `require_db_connection()` 直接 fail
（`ANYU_ALLOW_DB_SKIP=1` 才 skip），与既有 API 用例策略一致。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import sos as sos_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session
from app.models import (
    Bed,
    Device,
    Elder,
    ElderStaffAssignment,
    Notification,
    RiskEvent,
    Room,
    StaffAccount,
    WorkOrder,
)
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "sos-rest-test-secret-not-a-real-key-0123456789abcdef"

# ---------------------------------------------------------------- 固定 ID（全部虚构）
ROOM_ID = uuid.UUID("b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1")
BED_ID = uuid.UUID("b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2")
ELDER_ID = uuid.UUID("b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3")
ELDER_OTHER = uuid.UUID("b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4")
DEVICE_BOUND = uuid.UUID("b5b5b5b5-b5b5-4b5b-8b5b-b5b5b5b5b5b5")
DEVICE_FREE = uuid.UUID("b6b6b6b6-b6b6-4b6b-8b6b-b6b6b6b6b6b6")
CARE_WORKER_ID = uuid.UUID("b7b7b7b7-b7b7-4b7b-8b7b-b7b7b7b7b7b7")

SECRET_BOUND = "device-secret-sos-bound-not-a-real-value"
SECRET_FREE = "device-secret-sos-free-not-a-real-value"

#: 冻结时刻：2026-11-11 10:30（Asia/Shanghai）。写死时间让「同一分钟」可复现。
FROZEN_NOW = datetime(2026, 11, 11, 2, 30, tzinfo=UTC)


# ---------------------------------------------------------------- 夹具


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """把端点里的 `datetime.now(UTC)` 冻在 `FROZEN_NOW`，让幂等桶确定。"""

    class _FrozenDatetime:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(sos_api, "datetime", _FrozenDatetime)
    return FROZEN_NOW


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def client(db: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    """用真实 `create_app()` 装配（含 401/422 全局处理器），只把 get_session 指向测试事务。"""
    from app.main import create_app

    application = create_app(get_settings())

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    application.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """一位长者 + 主责护工 + 两台设备（一台绑定、一台未绑定）。

    主责护工是必需的：`alert_service` 按 `primary_care` 关系派单并发通知，
    没有它就只能验证「单建出来了」，验不了 `notified`。
    """
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="C-301", status="full")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {
                "elder_id": ELDER_ID,
                "name": "李桂芳",
                "birth_date": date(1944, 3, 2),
                "gender": "female",
                "status": "active",
            },
            {
                "elder_id": ELDER_OTHER,
                "name": "赵德海",
                "birth_date": date(1942, 9, 18),
                "gender": "male",
                "status": "active",
            },
        ],
    )
    await db.execute(
        Bed.__table__.insert().values(
            bed_id=BED_ID,
            room_id=ROOM_ID,
            bed_no="1 号床",
            status="occupied",
            elder_id=ELDER_ID,
        )
    )
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=CARE_WORKER_ID,
            name="周护工",
            role="care_worker",
            password_hash="x",
        )
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert().values(
            elder_id=ELDER_ID,
            staff_id=CARE_WORKER_ID,
            relation="primary_care",
        )
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": DEVICE_BOUND,
                "room_id": ROOM_ID,
                "elder_id": ELDER_ID,
                "token_hash": sha256_hex(SECRET_BOUND),
                "status": "online",
            },
            {
                "device_id": DEVICE_FREE,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_FREE),
                "status": "offline",
            },
        ],
    )


# ---------------------------------------------------------------- 工具


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_token(device_id: uuid.UUID, secret: str) -> str:
    return sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])


async def _count(db: AsyncConnection, model: object, *where: object) -> int:
    result = await db.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


# ---------------------------------------------------------------- 正常路径


async def test_sos_creates_risk_event_and_red_work_order(
    client: AsyncClient, db: AsyncConnection, seeded: None, frozen_now: datetime
) -> None:
    """一次按键 = 一条 `sos` 风险事件 + 一张 `sos` 红单 + 一条通知；字段全部对齐前端。"""
    response = await client.post(
        "/api/sos", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # 字段名是前端 `frontend/src/pad/apiSos.ts` 的契约，改名会让按钮静默失败。
    assert set(body) == {"sos_id", "created", "work_order_no", "notified", "trace_id"}, body
    assert body["created"] is True
    assert body["notified"] is True, "主责护工在，通知必须发出去"
    assert isinstance(body["work_order_no"], str) and body["work_order_no"].startswith("WO-")
    assert uuid.UUID(body["sos_id"])  # 必须是合法 UUID，前端要拿它显示「已记录」

    # 风险事件：type=sos，长者来自设备绑定。
    # 注意：这里在连接（AsyncConnection）上执行，ORM 实体不会被组装，
    # 所以只取需要的列而不是 `select(RiskEvent)`（那会拿到一串裸列）。
    event = (
        await db.execute(
            select(
                RiskEvent.event_id,
                RiskEvent.type,
                RiskEvent.severity,
                RiskEvent.elder_id,
                RiskEvent.detail,
            ).where(RiskEvent.event_id == uuid.UUID(body["sos_id"]))
        )
    ).one()
    assert event.type == "sos"
    assert event.severity == "red"
    assert event.elder_id == ELDER_ID

    # 工单：type=sos（不是笼统的 safety_alert，护理列表要能一眼看出是按键）、
    # 严重度红、挂回同一事件（护工从单子能追到事件与通知）。
    order = (
        await db.execute(
            select(
                WorkOrder.order_no,
                WorkOrder.type,
                WorkOrder.severity,
                WorkOrder.event_id,
                WorkOrder.elder_id,
                WorkOrder.assignee_id,
            ).where(WorkOrder.order_no == body["work_order_no"])
        )
    ).one()
    assert order.type == "sos"
    assert order.severity == "red"
    assert order.event_id == event.event_id
    assert order.elder_id == ELDER_ID
    assert order.assignee_id == CARE_WORKER_ID

    # 通知：kind 只能走 safety_alert（notifications.kind 的 CHECK 里没有 sos）。
    assert await _count(db, Notification, Notification.elder_id == ELDER_ID) == 1
    kind = (
        await db.execute(select(Notification.kind).where(Notification.elder_id == ELDER_ID))
    ).scalar_one()
    assert kind == "safety_alert"


async def test_sos_is_idempotent_within_same_minute(
    client: AsyncClient, db: AsyncConnection, seeded: None, frozen_now: datetime
) -> None:
    """同一分钟连按两次：只建一张单，第二次 created=false 且回原来的 sos_id / 单号。

    长者看到的是「已经在处理」而不是两个不同的结果——这正是幂等的产品意义。
    """
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))

    first = await client.post("/api/sos", headers=headers)
    second = await client.post("/api/sos", headers=headers)

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    first_body, second_body = first.json(), second.json()
    assert first_body["created"] is True
    assert second_body["created"] is False, "同一分钟内第二按不该再建单"
    assert second_body["sos_id"] == first_body["sos_id"], "回给长者的是同一件事件"
    assert second_body["work_order_no"] == first_body["work_order_no"], "单号必须回原来的那张"
    assert second_body["trace_id"] != first_body["trace_id"], "两次请求的 trace_id 应各自独立"

    # 三张表都只有一行——重复按没有副作用。
    assert await _count(db, RiskEvent, RiskEvent.elder_id == ELDER_ID) == 1
    assert await _count(db, WorkOrder, WorkOrder.elder_id == ELDER_ID) == 1
    assert await _count(db, Notification, Notification.elder_id == ELDER_ID) == 1


# ---------------------------------------------------------------- 鉴权与校验


async def test_sos_without_token_is_401(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """没令牌 → 401（鉴权在端点之前拦下，一张单都不该建）。"""
    response = await client.post("/api/sos")

    assert response.status_code == 401, response.text
    assert await _count(db, RiskEvent) == 0
    assert await _count(db, WorkOrder) == 0


async def test_sos_on_unbound_device_is_409(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """未绑定长者的设备：409 `device_unbound`，且三张表都不写。

    设备可能被解绑（换床、退住、维护中），此时按铃不能凭空造事件：
    不知道该救谁就不该建单，否则护理队列里会出现一条没有归属的红单。
    """
    response = await client.post(
        "/api/sos", headers=_bearer(_device_token(DEVICE_FREE, SECRET_FREE))
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "device_unbound", body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and body["trace_id"]

    assert await _count(db, RiskEvent) == 0, "未绑长者却写了 risk_events：按铃凭空造事件"
    assert await _count(db, WorkOrder) == 0
    assert await _count(db, Notification) == 0


async def test_sos_note_over_100_chars_is_422(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """`note` 超 100 字 → 422，且不写库：校验失败不该留下半张单。"""
    response = await client.post(
        "/api/sos",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        json={"note": "头" * 101},
    )

    assert response.status_code == 422, response.text
    assert await _count(db, RiskEvent) == 0
    assert await _count(db, WorkOrder) == 0


async def test_sos_ignores_elder_id_in_body(
    client: AsyncClient, db: AsyncConnection, seeded: None, frozen_now: datetime
) -> None:
    """请求体带来的 elder_id 一律丢弃（AGENTS §4-9）：落库只能是设备绑定那位。

    这是本轮最该有的用例——「我替别人按了个求助」若能成立，等于给了长者一个
    跨房间签发的口子。
    """
    response = await client.post(
        "/api/sos",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        json={"elder_id": str(ELDER_OTHER), "note": "我头晕"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    event = (
        await db.execute(
            select(RiskEvent.elder_id, RiskEvent.detail).where(
                RiskEvent.event_id == uuid.UUID(body["sos_id"])
            )
        )
    ).one()
    assert event.elder_id == ELDER_ID, "落库必须是设备绑定的长者，不能是 body 里的"
    assert await _count(db, RiskEvent, RiskEvent.elder_id == ELDER_OTHER) == 0
    assert await _count(db, WorkOrder, WorkOrder.elder_id == ELDER_OTHER) == 0

    # note 进 detail，供护工复核（脱敏由服务层负责，这里只确认没丢）。
    assert "我头晕" in str(event.detail)
