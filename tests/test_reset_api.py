"""演示数据一键重置（`FR-ADM-02`，2026-11-12）的接口级测试。

覆盖 `设计记录（演示数据一键重置）` 的 B4 九条，
外加三条契约级补充（跨表白名单、双击、生产环境 404）。

**四条比「接口 200」更重要的断言**：

1. **拒绝时必须一行都不删**——409 / 422 的路径里 `expected_counts` 对不上或者
   短语写错，如果代码先删后校验，测试会立刻发现业务表被清空；
2. **基线主数据必须原样**——`rooms` / `elders` / `staff_accounts` / `personas`
   在重置前后行数相等。这条断言是「不 `TRUNCATE` 全库」的唯一硬证据；
3. **审计与删除同生共死**——重置后必须存在一条 `reset.teaching`，且
   `detail.deleted` 记着每张表删了多少行（`detail` 只放行数不放数据）；
4. **生产环境 404 而不是 403**——404 连「这儿有个删库端点」都不告诉调用方。

夹具与 `tests/test_work_orders_api.py` 同构（真 PostgreSQL、每用例一事务、
结束回滚、连不上库按 `conftest` 统一策略处置）。
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

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

from app.api import admin_reset as admin_reset_api
from app.core.config import get_settings
from app.core.security import sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models.audit import AuditLog
from app.models.conversation_event import ConversationEvent
from app.models.memory import MemoryChunk
from app.models.notification import Notification
from app.models.organization import Device, Elder, Room
from app.models.staff import StaffAccount
from app.models.work_order import RiskEvent, WorkOrder, WorkOrderTransition
from app.services import reset_service
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "teaching-reset-test-secret-not-a-real-key-0123456789"

# ------------------------------------------------------------------ 固定 ID（全部虚构）

ELDER_A = uuid.UUID("ce000000-0000-4000-8000-0000000000a1")
ROOM_A = uuid.UUID("ce010000-0000-4000-8000-0000000000a1")
DEVICE_A = uuid.UUID("ce020000-0000-4000-8000-0000000000a1")

S_ADMIN = uuid.UUID("ce100000-0000-4000-8000-000000000001")
S_CARE = uuid.UUID("ce100000-0000-4000-8000-000000000002")

ORDER_A = uuid.UUID("ce200000-0000-4000-8000-000000000001")
EVENT_A = uuid.UUID("ce300000-0000-4000-8000-0000000000a1")
THREAD_A = uuid.UUID("ce400000-0000-4000-8000-0000000000a1")
NOTIFICATION_A = uuid.UUID("ce500000-0000-4000-8000-0000000000a1")
CHUNK_A = uuid.UUID("ce600000-0000-4000-8000-0000000000a1")

#: 建单基准时刻：固定过去时间，避免 `now()` 抖动。
BASE_TIME = datetime.datetime(2026, 11, 12, 2, 0, tzinfo=datetime.UTC)

_PASSWORD_PLACEHOLDER = "teaching-reset-test-row-not-a-real-hash"
_TOKEN_VERSION = 1

#: 设备种子里刻意写一个**不是**种子哈希的值，用来验证重置会把它恢复成种子值。
_STALE_DEVICE_HASH = "0" * 64


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
    application.include_router(admin_reset_api.router)

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


# ------------------------------------------------------------------ 种子数据


@dataclass(frozen=True)
class World:
    elder: uuid.UUID = ELDER_A
    room: uuid.UUID = ROOM_A
    device: uuid.UUID = DEVICE_A
    admin: uuid.UUID = S_ADMIN
    care: uuid.UUID = S_CARE
    order: uuid.UUID = ORDER_A
    event: uuid.UUID = EVENT_A
    thread: uuid.UUID = THREAD_A


async def _seed(db: AsyncConnection) -> None:
    """按外键顺序建种子。

    用 Core `insert` 而不是 ORM：这些列很多带 `server_default`，给显式值的
    只有主键与必填列，写起来短。`audit_logs` 也塞一行，用来验 `include_audit`
    开关的两条分支。
    """
    await db.execute(
        Room.__table__.insert().values({"room_id": ROOM_A, "room_no": "A-301", "status": "full"})
    )
    await db.execute(
        Elder.__table__.insert().values({"elder_id": ELDER_A, "name": "张桂芬", "status": "active"})
    )
    await db.execute(
        StaffAccount.__table__.insert().values(
            [
                {
                    "staff_id": S_ADMIN,
                    "name": "王管理",
                    "role": "admin",
                    "password_hash": _PASSWORD_PLACEHOLDER,
                    "token_version": _TOKEN_VERSION,
                    "status": "active",
                },
                {
                    "staff_id": S_CARE,
                    "name": "李护工",
                    "role": "care_worker",
                    "password_hash": _PASSWORD_PLACEHOLDER,
                    "token_version": _TOKEN_VERSION,
                    "status": "active",
                },
            ]
        )
    )
    await db.execute(
        Device.__table__.insert().values(
            {
                "device_id": DEVICE_A,
                "room_id": ROOM_A,
                "elder_id": ELDER_A,
                "token_hash": _STALE_DEVICE_HASH,
                "status": "offline",
            }
        )
    )
    await db.execute(
        RiskEvent.__table__.insert().values(
            {
                "event_id": EVENT_A,
                "elder_id": ELDER_A,
                "type": "safety_alert",
                "severity": "red",
                "source_type": "rule",
                "source_id": uuid.uuid4(),
                "status": "open",
                "created_at": BASE_TIME,
            }
        )
    )
    await db.execute(
        WorkOrder.__table__.insert().values(
            {
                "order_id": ORDER_A,
                "order_no": "WO-20261112-9001",
                "elder_id": ELDER_A,
                "event_id": EVENT_A,
                "type": "safety_alert",
                "severity": "red",
                "status": "new",
                "created_at": BASE_TIME,
            }
        )
    )
    await db.execute(
        WorkOrderTransition.__table__.insert().values(
            {
                "transition_id": uuid.uuid4(),
                "order_id": ORDER_A,
                "from_status": "new",
                "to_status": "acknowledged",
                "actor_id": None,
                "at": BASE_TIME,
                "note": "系统自动确认",
            }
        )
    )
    await db.execute(
        ConversationEvent.__table__.insert().values(
            {
                "event_id": uuid.uuid4(),
                "thread_id": THREAD_A,
                "elder_id": ELDER_A,
                "seq": 1,
                "event_type": "user_said",
                "occurred_at": BASE_TIME,
                "payload": {"text": "今天天气怎么样"},
            }
        )
    )
    await db.execute(
        Notification.__table__.insert().values(
            {
                "notification_id": NOTIFICATION_A,
                "elder_id": ELDER_A,
                "kind": "safety_alert",
                "severity": "red",
                "status": "generated",
            }
        )
    )
    await db.execute(
        MemoryChunk.__table__.insert().values(
            {
                "chunk_id": CHUNK_A,
                "elder_id": ELDER_A,
                "content": "长者所在城市是北京",
                "category": "private",
                "importance": 0.8,
                "status": "active",
            }
        )
    )
    await db.execute(
        AuditLog.__table__.insert().values(
            {
                "audit_id": uuid.uuid4(),
                "actor_type": "staff",
                "actor_id": S_ADMIN,
                "action": "seed.before_reset",
                "resource_type": "room",
                "resource_id": ROOM_A,
            }
        )
    )


@pytest.fixture
async def world(db: AsyncConnection) -> World:
    await _seed(db)
    return World()


# ------------------------------------------------------------------ 辅助


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(staff_id: uuid.UUID, role: str) -> str:
    return sign_staff_access(
        kind="staff",
        staff_id=str(staff_id),
        role=role,
        token_version=_TOKEN_VERSION,
    )


def _admin() -> dict[str, str]:
    return _bearer(_staff_token(S_ADMIN, "admin"))


def _care() -> dict[str, str]:
    return _bearer(_staff_token(S_CARE, "care_worker"))


def _assert_error(response: Response, status_code: int, code: str) -> None:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["detail"], str) and body["detail"]
    assert len(body["trace_id"]) == 32


async def _count(db: AsyncConnection, model: Any) -> int:
    result = await db.execute(select(func.count()).select_from(model.__table__))
    return int(result.scalar_one())


async def _preview(client: AsyncClient) -> dict[str, Any]:
    response = await client.get(
        "/api/admin/teaching-reset/preview",
        headers=_admin(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _body(counts: dict[str, int]) -> dict[str, Any]:
    return {
        "expected_counts": counts,
        "confirm_phrase": admin_reset_api.CONFIRM_PHRASE,
    }


# ------------------------------------------------------------------ 用例


async def test_preview_requires_admin(client: AsyncClient, world: World) -> None:
    response = await client.get("/api/admin/teaching-reset/preview", headers=_care())
    _assert_error(response, 403, "ROLE_FORBIDDEN")


async def test_preview_counts_match_actual(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """预览的键集合必须恰好是白名单（不含基线表），且行数与实际一致。"""
    plan = await _preview(client)

    assert set(plan["tables"]) == set(reset_service.RESET_TABLES)
    assert not (set(plan["tables"]) & set(reset_service.BASELINE_TABLES))

    assert plan["tables"]["work_orders"] == await _count(db, WorkOrder) == 1
    assert plan["tables"]["risk_events"] == await _count(db, RiskEvent) == 1
    assert plan["tables"]["conversation_events"] == await _count(db, ConversationEvent) == 1
    assert plan["total_rows"] == sum(plan["tables"].values())
    assert plan["device_tokens_rotated"] == 1


async def test_preview_never_includes_audit_table(client: AsyncClient, world: World) -> None:
    plan = await _preview(client)

    # 审计只增不改：`audit_logs` 恒不在重置范围内（TD §3 留痕口径）。
    assert "audit_logs" not in plan["tables"]
    # 药单是护工维护的配置，删了不会随重置恢复 → 也不在范围内。
    assert "medication_plans" not in plan["tables"]


async def test_apply_clears_business_tables(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    plan = await _preview(client)
    response = await client.post(
        "/api/admin/teaching-reset", json=_body(plan["tables"]), headers=_admin()
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["tables"]["work_orders"] == 1
    assert result["total_rows"] == plan["total_rows"]

    for model in (
        WorkOrder,
        WorkOrderTransition,
        RiskEvent,
        ConversationEvent,
        Notification,
        MemoryChunk,
    ):
        assert await _count(db, model) == 0, model.__tablename__


async def test_apply_keeps_baseline_data(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """基线主数据一行都不能少——这是「不 TRUNCATE 全库」的硬证据。"""
    before = {
        "rooms": await _count(db, Room),
        "elders": await _count(db, Elder),
        "staff_accounts": await _count(db, StaffAccount),
        "devices": await _count(db, Device),
    }
    plan = await _preview(client)
    response = await client.post(
        "/api/admin/teaching-reset", json=_body(plan["tables"]), headers=_admin()
    )
    assert response.status_code == 200, response.text

    after = {
        "rooms": await _count(db, Room),
        "elders": await _count(db, Elder),
        "staff_accounts": await _count(db, StaffAccount),
        "devices": await _count(db, Device),
    }
    assert after == before
    assert before["elders"] == 1


async def test_expected_counts_mismatch_is_409_and_deletes_nothing(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    plan = await _preview(client)
    stale = dict(plan["tables"])
    stale["work_orders"] = 99

    response = await client.post("/api/admin/teaching-reset", json=_body(stale), headers=_admin())
    _assert_error(response, 409, "CONFLICT")

    assert await _count(db, WorkOrder) == 1
    assert await _count(db, ConversationEvent) == 1


async def test_unknown_table_is_422_and_deletes_nothing(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """`expected_counts` 多传一个不在白名单的表，属于「想删别的东西」，直接拒。"""
    plan = await _preview(client)
    counts = dict(plan["tables"])
    counts["elders"] = 1

    response = await client.post("/api/admin/teaching-reset", json=_body(counts), headers=_admin())
    _assert_error(response, 422, "VALIDATION_ERROR")
    assert await _count(db, Elder) == 1
    assert await _count(db, WorkOrder) == 1


async def test_wrong_confirm_phrase_is_422_and_deletes_nothing(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    plan = await _preview(client)
    body = _body(plan["tables"])
    body["confirm_phrase"] = "重置"

    response = await client.post("/api/admin/teaching-reset", json=body, headers=_admin())
    _assert_error(response, 422, "VALIDATION_ERROR")
    assert await _count(db, WorkOrder) == 1


async def test_second_submit_after_reset_is_conflict(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """双击：第二次回传的还是删之前那份行数，必然 409，且不会把审计行再清一遍。"""
    plan = await _preview(client)
    body = _body(plan["tables"])

    first = await client.post("/api/admin/teaching-reset", json=body, headers=_admin())
    assert first.status_code == 200, first.text

    second = await client.post("/api/admin/teaching-reset", json=body, headers=_admin())
    _assert_error(second, 409, "CONFLICT")


async def test_reset_keeps_audit_rows_and_appends_one(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    plan = await _preview(client)
    # 用「前后差值」而不是绝对条数：`write_audit_isolated` 走独立会话并自己 commit，
    # 别处的用例（越权、导出、工具拒绝）会在同一个测试库里留下审计行且**不随本用例回滚**，
    # 断言绝对条数就成了「只有在单文件跑时才成立」的假绿。
    before = await _count(db, AuditLog)
    response = await client.post(
        "/api/admin/teaching-reset", json=_body(plan["tables"]), headers=_admin()
    )
    assert response.status_code == 200, response.text

    # 审计只增不改：重置只追加一条 `teaching.reset`，一条都不删。
    assert await _count(db, AuditLog) == before + 1
    # 按 action 过滤而不是「取最新一条」：同一事务里插入的审计行 `created_at` 完全相同
    # （都取 `now()`），按时间排序的「最新那条」在并列时是任意的。
    detail = (
        await db.execute(
            select(AuditLog.detail).where(AuditLog.action == reset_service.RESET_AUDIT_ACTION)
        )
    ).scalar_one_or_none()
    assert detail is not None
    assert detail["deleted"]["work_orders"] == 1


async def test_device_token_restored_to_seed_value(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    plan = await _preview(client)
    response = await client.post(
        "/api/admin/teaching-reset", json=_body(plan["tables"]), headers=_admin()
    )
    assert response.status_code == 200, response.text
    assert response.json()["device_tokens_rotated"] == 1

    row = (
        await db.execute(
            select(Device.token_hash, Device.status, Device.elder_id).where(
                Device.device_id == DEVICE_A
            )
        )
    ).one()
    expected = hashlib.sha256(reset_service.DEVICE_SEED_SECRET.encode("utf-8")).hexdigest()
    assert row[0] == expected
    assert row[1] == "online"
    # 绑定关系刻意保留：解绑会让下一次演示连登录都进不去。
    assert row[2] == ELDER_A


async def test_reset_writes_audit_with_counts(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    plan = await _preview(client)
    response = await client.post(
        "/api/admin/teaching-reset", json=_body(plan["tables"]), headers=_admin()
    )
    assert response.status_code == 200, response.text

    row = (
        await db.execute(
            select(AuditLog.actor_type, AuditLog.actor_id, AuditLog.detail).where(
                AuditLog.action == reset_service.RESET_AUDIT_ACTION
            )
        )
    ).one()
    assert row[0] == "staff"
    assert row[1] == S_ADMIN
    assert row[2]["deleted"]["work_orders"] == 1
    assert row[2]["total_rows"] == plan["total_rows"]
    assert row[2]["device_tokens_rotated"] == 1


async def test_production_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    """生产环境一律 404：不是 403，因为 404 连「这里有端点」都不说。"""
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        response = await client.get("/api/admin/teaching-reset/preview", headers=_admin())
        _assert_error(response, 404, "NOT_FOUND")
    finally:
        get_settings.cache_clear()
