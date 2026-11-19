"""健康采集代填 / 趋势 / 补录 / 复核（FR-HLT-04/06，契约 C4 错误体）的接口级测试。

跑在真实 PostgreSQL 上，每用例一个外层事务、结束回滚，主库零残留；被测应用在文件内
临时装配（不改 `app/main.py`），只 include `app/api/health.py` 一个 router —— 顺便验证
`/health` 探针与新增业务端点可以共存于同一模块。

为什么自建 NullPool 引擎：模块级连接池会把上一个事件循环的 asyncpg 连接留给下一个
用例（任务板 P-03），而本文件需要测试事务与 ASGI 请求在同一事件循环里。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import health as health_api
from app.core.config import get_settings
from app.core.security import sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    Elder,
    ElderStaffAssignment,
    GuardianAccount,
    HealthRecord,
    Room,
    StaffAccount,
)
from conftest import require_db_connection

TEST_JWT_SECRET = "task32-test-secret-not-a-real-key-0123456789abcdef"

_PASSWORD_PLACEHOLDER = "task32-test-row-not-a-real-hash"

ROOM = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbb02a")
E1 = uuid.UUID("e0000001-0000-4000-8000-00000000a001")
E2 = uuid.UUID("e0000002-0000-4000-8000-00000000a002")
E_MISSING = uuid.UUID("e0000999-0000-4000-8000-00000000a999")

S_ADMIN = uuid.UUID("51000000-0000-4000-8000-0000000000a1")
S_CARE1 = uuid.UUID("51000000-0000-4000-8000-0000000000a2")
S_DOCTOR1 = uuid.UUID("51000000-0000-4000-8000-0000000000a3")
S_CARE_NONE = uuid.UUID("51000000-0000-4000-8000-0000000000a4")
G_ONE = uuid.UUID("62000000-0000-4000-8000-0000000000a1")

DAY = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=8))).date()


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(health_api.router)

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


@dataclass(frozen=True)
class World:
    room: uuid.UUID = ROOM
    elder1: uuid.UUID = E1
    elder2: uuid.UUID = E2
    admin: uuid.UUID = S_ADMIN
    care1: uuid.UUID = S_CARE1
    doctor1: uuid.UUID = S_DOCTOR1
    guardian: uuid.UUID = G_ONE


@pytest.fixture
async def world(db: AsyncConnection) -> World:
    """一间房、两位长者；care1 负责 elder1（primary_care），doctor1 负责 elder1（doctor）。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM, room_no="H-201", status="available")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": E1, "name": "张桂芬", "status": "active"},
            {"elder_id": E2, "name": "李建国", "status": "active"},
        ],
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": S_ADMIN,
                "name": "管理员",
                "role": "admin",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
                "status": "active",
            },
            {
                "staff_id": S_CARE1,
                "name": "护工小周",
                "role": "care_worker",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
                "status": "active",
            },
            {
                "staff_id": S_DOCTOR1,
                "name": "医生老吴",
                "role": "doctor",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
                "status": "active",
            },
            {
                "staff_id": S_CARE_NONE,
                "name": "护工老陈",
                "role": "care_worker",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
                "status": "active",
            },
        ],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {"elder_id": E1, "staff_id": S_CARE1, "relation": "primary_care"},
            {"elder_id": E1, "staff_id": S_DOCTOR1, "relation": "doctor"},
        ],
    )
    await db.execute(
        GuardianAccount.__table__.insert().values(
            guardian_id=G_ONE,
            name="家属小张",
            password_hash=_PASSWORD_PLACEHOLDER,
            token_version=1,
            status="active",
        )
    )
    return World()


# ------------------------------------------------------------------ 请求辅助


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(staff_id: uuid.UUID, role: str, token_version: int = 1) -> str:
    return sign_staff_access(
        kind="staff", staff_id=staff_id, role=role, token_version=token_version
    )


def _guardian_token(guardian_id: uuid.UUID, token_version: int = 1) -> str:
    return sign_staff_access(
        kind="guardian", staff_id=guardian_id, role="", token_version=token_version
    )


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


async def _fill(
    client: AsyncClient,
    token: str,
    *,
    elder_id: uuid.UUID = E1,
    source: str = "staff_active",
    record_date: datetime.date | None = None,
    **values: object,
) -> Response:
    payload: dict[str, object] = {"elder_id": str(elder_id), "source": source, **values}
    if record_date is not None:
        payload["record_date"] = record_date.isoformat()
    return await client.post("/api/health/records", json=payload, headers=_bearer(token))


async def _records(db: AsyncConnection, elder_id: uuid.UUID) -> list[HealthRecord]:
    """回查某长者的版本链（按 revision 升序）。

    `select(Model)` 在 `AsyncConnection` 上只返回列元组，故借一个只读 Session 取 ORM 行。
    """
    session = AsyncSession(bind=db, expire_on_commit=False)
    try:
        rows = await session.execute(
            select(HealthRecord)
            .where(HealthRecord.elder_id == elder_id)
            .order_by(HealthRecord.revision)
        )
        return list(rows.scalars())
    finally:
        await session.close()


async def _count_reviews(db: AsyncConnection) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(HealthRecord).where(HealthRecord.grade != "green")
            )
        ).scalar_one()
    )


# ------------------------------------------------------------------ 探针与鉴权


async def test_health_probe_is_untouched(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_fill_requires_authentication(client: AsyncClient, world: World) -> None:
    response = await client.post("/api/health/records", json={"elder_id": str(E1), "systolic": 128})
    _assert_error(response, 401, "TOKEN_INVALID")


async def test_guardian_is_forbidden(client: AsyncClient, world: World) -> None:
    response = await _fill(client, _guardian_token(G_ONE), systolic=128)
    _assert_error(response, 403, "ROLE_FORBIDDEN")


# ------------------------------------------------------------------ 代填


async def test_fill_green_returns_201_and_persists(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    response = await _fill(
        client,
        _staff_token(S_CARE1, "care_worker"),
        systolic=128,
        diastolic=78,
        heart_rate=72,
        sleep_hours=7,
        mood_score=4,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["grade"] == "green"
    assert body["revision"] == 1
    assert body["source"] == "staff_active"
    assert body["notification_id"] is None
    assert body["sleep_hours"] == 7.0  # Decimal 序列化成数字，不是字符串
    assert body["elder_id"] == str(E1)

    rows = await _records(db, E1)
    assert len(rows) == 1
    assert rows[0].grade == "green"
    assert rows[0].record_date == DAY
    assert rows[0].superseded_by is None
    assert rows[0].recorded_by_staff_id == S_CARE1


async def test_fill_yellow_creates_review_task(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    response = await _fill(client, _staff_token(S_CARE1, "care_worker"), systolic=150, diastolic=95)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["grade"] == "yellow"
    assert body["notification_id"]

    review = await client.get(
        f"/api/health/review/{body['notification_id']}",
        headers=_bearer(_staff_token(S_CARE1, "care_worker")),
    )
    assert review.status_code == 200, review.text
    task = review.json()
    assert task["severity"] == "yellow"
    assert task["status"] == "generated"
    assert task["payload"]["grade"] == "yellow"
    assert {"systolic_high", "diastolic_high"} <= set(task["payload"]["abnormal_reasons"])
    # 黄档先由护工处理；只有红档才强制医生复核（payload 标记）
    assert task["payload"]["requires_doctor_review"] is False


async def test_fill_red_single_component(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    response = await _fill(client, _staff_token(S_CARE1, "care_worker"), systolic=190)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["grade"] == "red"
    assert body["abnormal_reasons"][0] == "systolic_red"
    assert body["notification_id"]

    rows = await _records(db, E1)
    assert len(rows) == 1
    assert rows[0].grade == "red"
    assert rows[0].abnormal_reasons[0] == "systolic_red"

    review = await client.get(
        f"/api/health/review/{body['notification_id']}",
        headers=_bearer(_staff_token(S_CARE1, "care_worker")),
    )
    assert review.status_code == 200, review.text
    assert review.json()["payload"]["requires_doctor_review"] is True


async def test_fill_out_of_scope_is_403_and_writes_nothing(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    response = await _fill(client, _staff_token(S_CARE1, "care_worker"), elder_id=E2, systolic=128)

    _assert_error(response, 403, "SCOPE_FORBIDDEN")
    assert await _records(db, E2) == []


async def test_fill_invalid_payload_is_422(client: AsyncClient, world: World) -> None:
    care = _staff_token(S_CARE1, "care_worker")

    inverted = await _fill(client, care, systolic=120, diastolic=130)
    _assert_error(inverted, 422, "VALIDATION_ERROR")

    out_of_band = await _fill(client, care, mood_score=9)
    _assert_error(out_of_band, 422, "VALIDATION_ERROR")

    bad_source = await _fill(client, care, source="voice_active", systolic=128)
    _assert_error(bad_source, 422, "VALIDATION_ERROR")


async def test_fill_unknown_elder_is_404(client: AsyncClient, world: World) -> None:
    response = await _fill(client, _staff_token(S_ADMIN, "admin"), elder_id=E_MISSING, systolic=128)
    _assert_error(response, 404, "NOT_FOUND")


# ------------------------------------------------------------------ 范围


async def test_doctor_scope_uses_doctor_relation(client: AsyncClient, world: World) -> None:
    doctor = _staff_token(S_DOCTOR1, "doctor")

    allowed = await _fill(client, doctor, systolic=128, diastolic=78)
    assert allowed.status_code == 201, allowed.text

    denied = await _fill(client, doctor, elder_id=E2, systolic=128)
    _assert_error(denied, 403, "SCOPE_FORBIDDEN")


async def test_admin_can_fill_any_elder(client: AsyncClient, world: World) -> None:
    response = await _fill(client, _staff_token(S_ADMIN, "admin"), elder_id=E2, systolic=128)
    assert response.status_code == 201, response.text
    assert response.json()["grade"] == "yellow"  # 只有收缩压，缺舒张压 → 黄


# ------------------------------------------------------------------ 趋势


async def test_trend_lists_current_versions_ascending(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    care = _staff_token(S_CARE1, "care_worker")
    await _fill(
        client, care, record_date=DAY - datetime.timedelta(days=2), systolic=128, diastolic=78
    )
    await _fill(
        client, care, record_date=DAY - datetime.timedelta(days=1), systolic=132, diastolic=80
    )

    response = await client.get(f"/api/health/records/{E1}?days=3", headers=_bearer(care))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["days"] == 3
    assert [item["record_date"] for item in body["records"]] == [
        (DAY - datetime.timedelta(days=2)).isoformat(),
        (DAY - datetime.timedelta(days=1)).isoformat(),
    ]
    assert body["records"][0]["systolic"] == 128
    assert isinstance(body["records"][0]["sleep_hours"], (int, float)) or (
        body["records"][0]["sleep_hours"] is None
    )


# ------------------------------------------------------------------ 补录（版本链）


async def test_supersede_appends_version_and_keeps_original(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    care = _staff_token(S_CARE1, "care_worker")
    created = await _fill(client, care, systolic=128, diastolic=78, sleep_hours=7)
    original = created.json()

    response = await client.post(
        f"/api/health/records/{original['record_id']}/supersede",
        json={"systolic": 150, "diastolic": 95, "sleep_hours": 5},
        headers=_bearer(care),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["record"]["revision"] == 2
    assert body["record"]["grade"] == "yellow"
    assert body["record"]["notification_id"]
    assert body["previous"]["revision"] == 1
    assert body["previous"]["systolic"] == 128  # 原值一个不动
    assert body["previous"]["superseded_by"] == body["record"]["record_id"]

    rows = await _records(db, E1)
    assert len(rows) == 2  # 追加版本，不覆盖
    assert rows[0].systolic_mmhg == 128
    assert rows[0].superseded_by == rows[1].record_id

    # 折线只看当前版本：补录前的旧值留在审计链里
    trend = await client.get(f"/api/health/records/{E1}?days=1", headers=_bearer(care))
    assert [item["revision"] for item in trend.json()["records"]] == [2]


async def test_supersede_twice_returns_409(client: AsyncClient, world: World) -> None:
    care = _staff_token(S_CARE1, "care_worker")
    original = (await _fill(client, care, systolic=128, diastolic=78)).json()

    first = await client.post(
        f"/api/health/records/{original['record_id']}/supersede",
        json={"systolic": 130},
        headers=_bearer(care),
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/health/records/{original['record_id']}/supersede",
        json={"systolic": 135},
        headers=_bearer(care),
    )
    _assert_error(second, 409, "CONFLICT")


async def test_supersede_missing_record_is_404(client: AsyncClient, world: World) -> None:
    response = await client.post(
        f"/api/health/records/{uuid.uuid4()}/supersede",
        json={"systolic": 128},
        headers=_bearer(_staff_token(S_CARE1, "care_worker")),
    )
    _assert_error(response, 404, "NOT_FOUND")


# ------------------------------------------------------------------ 复核（HITL）


async def test_resolve_review_marks_acknowledged_and_is_idempotent(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    care = _staff_token(S_CARE1, "care_worker")
    created = (await _fill(client, care, systolic=150, diastolic=95)).json()
    notification_id = created["notification_id"]

    response = await client.post(
        f"/api/health/review/{notification_id}/resolve",
        json={"decision": "escalated", "note": "已联系家属与医生"},
        headers=_bearer(care),
    )

    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "acknowledged"
    assert task["acknowledged_at"]
    assert task["payload"]["review_decision"] == "escalated"
    assert task["payload"]["reviewed_by_staff_id"] == str(S_CARE1)
    assert task["payload"]["review_note"] == "已联系家属与医生"

    again = await client.post(
        f"/api/health/review/{notification_id}/resolve",
        json={"decision": "acknowledged"},
        headers=_bearer(care),
    )
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "acknowledged"
    assert again.json()["acknowledged_at"] == task["acknowledged_at"]


async def test_list_records_filters_and_paginates(client: AsyncClient, world: World) -> None:
    care = _staff_token(S_CARE1, "care_worker")
    await _fill(
        client, care, record_date=DAY - datetime.timedelta(days=1), systolic=128, diastolic=78
    )
    await _fill(client, care, record_date=DAY, systolic=132, diastolic=80)

    listed = await client.get(f"/api/health/records?elder_id={E1}", headers=_bearer(care))
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 20
    # 最近记录在最上：record_date DESC
    assert [item["record_date"] for item in body["items"]] == [
        DAY.isoformat(),
        (DAY - datetime.timedelta(days=1)).isoformat(),
    ]

    single_day = await client.get(
        f"/api/health/records?elder_id={E1}&date={(DAY - datetime.timedelta(days=1)).isoformat()}",
        headers=_bearer(care),
    )
    assert single_day.status_code == 200, single_day.text
    assert single_day.json()["total"] == 1
    assert (
        single_day.json()["items"][0]["record_date"]
        == (DAY - datetime.timedelta(days=1)).isoformat()
    )

    paged = await client.get(
        f"/api/health/records?elder_id={E1}&page=2&page_size=1", headers=_bearer(care)
    )
    assert paged.status_code == 200, paged.text
    assert paged.json()["total"] == 2
    assert len(paged.json()["items"]) == 1
    assert paged.json()["items"][0]["record_date"] == (DAY - datetime.timedelta(days=1)).isoformat()


async def test_list_records_out_of_scope_is_403(client: AsyncClient, world: World) -> None:
    care = _staff_token(S_CARE1, "care_worker")
    await _fill(client, care, systolic=128, diastolic=78)

    response = await client.get(f"/api/health/records?elder_id={E2}", headers=_bearer(care))
    _assert_error(response, 403, "SCOPE_FORBIDDEN")

    # 不传 elder_id 时按负责关系收敛：care1 只看得到 elder1 的记录
    scoped = await client.get("/api/health/records", headers=_bearer(care))
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["total"] == 1
    assert scoped.json()["items"][0]["elder_id"] == str(E1)


async def test_review_out_of_scope_is_403(client: AsyncClient, world: World) -> None:
    care = _staff_token(S_CARE1, "care_worker")
    created = (await _fill(client, care, systolic=150, diastolic=95)).json()

    # care_none 与 elder1 没有任何关系 → 看不到这条复核任务
    response = await client.get(
        f"/api/health/review/{created['notification_id']}",
        headers=_bearer(_staff_token(S_CARE_NONE, "care_worker")),
    )
    _assert_error(response, 403, "SCOPE_FORBIDDEN")


# ------------------------------------------------- 采集会话（FR-HLT-01 的接口面）


@pytest.fixture
def memory_graph(monkeypatch: pytest.MonkeyPatch) -> object:
    """接口测试不让 `get_health_graph()` 去连 checkpoint 库：换成进程内 MemorySaver。"""
    from langgraph.checkpoint.memory import MemorySaver

    from app.graphs import health_checkup_graph as health_checkup

    graph = health_checkup.build_health_graph(MemorySaver())

    async def _graph() -> object:
        return graph

    monkeypatch.setattr(health_checkup, "get_health_graph", _graph)
    return graph


async def _start_checkup(db: AsyncConnection, elder_id: uuid.UUID = E1) -> None:
    """直接调服务层起一场采集（等价于 19:00 任务做的事），让接口有东西可拉。"""
    from app.graphs import health_checkup_graph as health_checkup

    session = AsyncSession(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        await health_checkup.start_checkup(
            session,
            elder_id=elder_id,
            record_date=DAY,
            now=datetime.datetime.now(datetime.UTC),
        )
        await session.commit()
    finally:
        await session.close()


async def test_checkup_session_exposes_current_question(
    client: AsyncClient, db: AsyncConnection, world: World, memory_graph: object
) -> None:
    """pad 拉这一条就能播报第一问：题面、第几次、截止时间都在（这就是「投递」）。"""
    await _start_checkup(db)
    care = _staff_token(S_CARE1, "care_worker")

    response = await client.get(f"/api/health/checkup/{E1}", headers=_bearer(care))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "asking"
    assert body["source"] == "voice_scheduled"  # 19:00 定时采集的口径
    assert body["active"] is True
    assert body["current_key"] == "systolic"
    assert body["prompt"]["key"] == "systolic"
    assert body["prompt"]["attempt"] == 1
    assert body["expires_at"] is not None
    # 线程键与图口径一致：pad 侧不需要它，但复核 resume 与超时推进都靠它接回同一场会话。
    from app.graphs.health_checkup_graph import health_thread_id

    assert body["thread_id"] == health_thread_id(E1, DAY)


async def test_checkup_session_without_open_session_is_404(
    client: AsyncClient, world: World, memory_graph: object
) -> None:
    """当天没起采集就是没有：404，别编一个空会话骗 pad。"""
    care = _staff_token(S_CARE1, "care_worker")
    response = await client.get(f"/api/health/checkup/{E1}", headers=_bearer(care))
    _assert_error(response, 404, "NOT_FOUND")


async def test_checkup_session_out_of_scope_is_403(
    client: AsyncClient, db: AsyncConnection, world: World, memory_graph: object
) -> None:
    """范围外先 403（不是 404）：否则「有没有这场采集」会变成越权情报。"""
    await _start_checkup(db)
    response = await client.get(
        f"/api/health/checkup/{E1}", headers=_bearer(_staff_token(S_CARE_NONE, "care_worker"))
    )
    _assert_error(response, 403, "SCOPE_FORBIDDEN")


async def test_checkup_answer_moves_to_the_next_question(
    client: AsyncClient, db: AsyncConnection, world: World, memory_graph: object
) -> None:
    """答一句收缩压 → 下一题变舒张压，已答项留在会话快照里。"""
    await _start_checkup(db)
    care = _staff_token(S_CARE1, "care_worker")

    response = await client.post(
        f"/api/health/checkup/{E1}/answer", json={"text": "128"}, headers=_bearer(care)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["record"] is None  # 还没走完，不该提前落库
    session_body = body["session"]
    assert session_body["status"] == "asking"
    assert session_body["current_key"] == "diastolic"
    assert session_body["answers"] == {"systolic": 128}
    assert session_body["expires_at"] is not None


async def test_checkup_answer_end_reply_closes_and_persists(
    client: AsyncClient, db: AsyncConnection, world: World, memory_graph: object
) -> None:
    """长者说「不测了」：会话 ended，已答的收缩压照样落库（不丢已采集数据）。"""
    await _start_checkup(db)
    care = _staff_token(S_CARE1, "care_worker")
    await client.post(
        f"/api/health/checkup/{E1}/answer", json={"text": "128"}, headers=_bearer(care)
    )

    response = await client.post(
        f"/api/health/checkup/{E1}/answer",
        json={"text": "不测了，我不想测了"},
        headers=_bearer(care),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["status"] == "ended"
    assert body["session"]["prompt"] is None
    assert body["record"] is not None
    assert body["record"]["systolic"] == 128
    assert body["record"]["diastolic"] is None  # 没答的项就是没有，不补 0
    assert body["record"]["grade"] == "yellow"  # 其余项缺失判黄，不伪造正常值
    assert body["record"]["source"] == "voice_scheduled"
    assert await _records(db, E1) != []


async def test_checkup_finish_marks_missing_in_one_call(
    client: AsyncClient, db: AsyncConnection, world: World, memory_graph: object
) -> None:
    """「结束采集」一次收口：缺的题由服务端补成 missing，不再逐题往返。

    pad 上这一下以前是 1 次「标缺失」× 未答题数，最多 6 次串行请求，按钮要转 2–5 秒；
    现在一次 `POST /finish`：服务端按图里的 state 把没答的题补进 missing 再落终态，
    已答的收缩压照常落库。零答案仍不落库（那是漏采，不是采集）。
    """
    from app.graphs.health_checkup_graph import QUESTION_ORDER

    await _start_checkup(db)
    care = _staff_token(S_CARE1, "care_worker")
    await client.post(
        f"/api/health/checkup/{E1}/answer", json={"text": "128"}, headers=_bearer(care)
    )

    response = await client.post(f"/api/health/checkup/{E1}/finish", headers=_bearer(care))

    assert response.status_code == 200, response.text
    body = response.json()
    session_body = body["session"]
    assert session_body["status"] != "asking"
    assert session_body["prompt"] is None
    assert session_body["answers"] == {"systolic": 128}
    # 没答的四项一次补齐（顺序按 QUESTION_ORDER，便于端侧按固定顺序显示）
    assert session_body["missing"] == [k for k in QUESTION_ORDER if k != "systolic"]
    assert body["record"] is not None
    assert body["record"]["systolic"] == 128
    assert body["record"]["diastolic"] is None  # 不补 0，缺失就是缺失
    assert await _records(db, E1) != []

    # 再点一次：幂等，不重复落库、不新开一场
    records_before = len(await _records(db, E1))
    again = await client.post(f"/api/health/checkup/{E1}/finish", headers=_bearer(care))
    assert again.status_code == 200, again.text
    assert again.json()["session"]["status"] == session_body["status"]
    assert len(await _records(db, E1)) == records_before


async def test_checkup_session_guardian_can_read_when_bound(
    client: AsyncClient, db: AsyncConnection, world: World, memory_graph: object
) -> None:
    """绑定家属也看得到采集进度；没绑定就 403。"""
    from app.models import ElderGuardian

    await db.execute(
        ElderGuardian.__table__.insert().values(
            elder_id=E1, guardian_id=G_ONE, relation="daughter", status="active"
        )
    )
    await _start_checkup(db)

    bound = await client.get(f"/api/health/checkup/{E1}", headers=_bearer(_guardian_token(G_ONE)))
    assert bound.status_code == 200, bound.text
    assert bound.json()["current_key"] == "systolic"

    unbound = await client.get(f"/api/health/checkup/{E2}", headers=_bearer(_guardian_token(G_ONE)))
    _assert_error(unbound, 403, "SCOPE_FORBIDDEN")
