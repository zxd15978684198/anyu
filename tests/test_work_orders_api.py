"""工单看板（E15 / E16 / E17 / E18，2026-11-12）的接口级测试。

覆盖 `设计记录（工单看板补齐）` 的批次 B2：

- **E15 `GET /api/work-orders`** —— 看板列表，默认只返回未处理四态，红档置顶；
- **E16 `GET /api/work-orders/{id}`** —— 详情 + 完整状态变更历史（FR-ALT-02 第一条验收）；
- **E17 `POST /api/work-orders/{id}/transitions`** —— 状态流转，一次调用写状态 + 留痕 + 审计；
- **E18 `POST /api/work-orders/{id}/close-event`** —— 工单关闭后闭环风险事件。

四条契约级断言（比「接口 200」更重要）：

1. **状态流转必须留痕**：每次 transition 落一行 `work_order_transitions`，含 from/to/actor/at，
   否则 FR-ALT-02「任一工单可查到完整状态变更历史」不成立；
2. **非法迁移是 409 不是 422**：`new → resolved` 这类跳步是「当前状态不允许」而不是
   「请求写错了」，两个级别的错误不能混；
3. **关闭五条约束零例外**：误报必填原因、非误报必填处置结果、红档必填实际处置时间、
   只有 admin 能重开、重开必填原因——每一条都有独立用例；
4. **范围过滤两条路径同一答案**：列表用 SQL 过滤、详情用判定，不能让护工
   「列表里看不见、但知道 order_id 就能查详情」。

夹具与 `tests/test_devices_api.py` 同构（真 PostgreSQL、每用例一事务、结束回滚、无库即 fail），
理由见该文件模块 docstring。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import work_orders as work_orders_api
from app.core.config import get_settings
from app.core.security import sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Elder
from app.models.audit import AuditLog
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.models.work_order import RiskEvent, WorkOrder, WorkOrderTransition
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "work-orders-test-secret-not-a-real-key-0123456789"

# ------------------------------------------------------------------ 固定 ID（全部虚构）

ELDER_A = uuid.UUID("cf000000-0000-4000-8000-0000000000a1")
ELDER_B = uuid.UUID("cf000000-0000-4000-8000-0000000000b1")
ELDER_C = uuid.UUID("cf000000-0000-4000-8000-0000000000c1")

S_ADMIN = uuid.UUID("cf100000-0000-4000-8000-000000000001")
S_CARE = uuid.UUID("cf100000-0000-4000-8000-000000000002")
S_CARE_OTHER = uuid.UUID("cf100000-0000-4000-8000-000000000003")
S_DOCTOR = uuid.UUID("cf100000-0000-4000-8000-000000000004")

ORDER_NEW_RED = uuid.UUID("cf200000-0000-4000-8000-000000000001")
ORDER_NEW_YELLOW = uuid.UUID("cf200000-0000-4000-8000-000000000002")
ORDER_ACK_RED = uuid.UUID("cf200000-0000-4000-8000-000000000003")
ORDER_CLOSED = uuid.UUID("cf200000-0000-4000-8000-000000000004")
ORDER_MISSING = uuid.UUID("cf200000-0000-4000-8000-0000000009ff")

EVENT_A = uuid.UUID("cf300000-0000-4000-8000-0000000000a1")

#: `risk_events.source_id` 是 UUID 列（不是自由文本），种子值必须是合法 UUID。
SOURCE_RULE = uuid.UUID("cf310000-0000-4000-8000-00000000000f")

#: 建单基准时刻：固定过去时间，让 `created_at ASC` 的排序断言稳定。
BASE_TIME = datetime.datetime(2026, 11, 12, 1, 0, tzinfo=datetime.UTC)

# `password_hash` 在鉴权链路里只做 NOT NULL 占位，不跑 bcrypt。
_PASSWORD_PLACEHOLDER = "work-orders-test-row-not-a-real-hash"

#: 员工种子里的 `token_version`：必须与 `_staff_token()` 签发时用的一致，否则一律 401。
_TOKEN_VERSION = 1


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """注入测试密钥；结束后清 Settings 缓存，避免把密钥泄漏给同进程后续文件。"""
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
    """每用例一个事务、结束回滚；连不上库按 conftest 的统一策略处置（默认 fail）。"""
    conn = await require_db_connection(db_engine)

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def client(db: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    """临时装配 work_orders router；请求与测试事务共用同一事件循环与同一连接。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(work_orders_api.router)

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
    """三位长者、四名员工；护工 S_CARE 管 A，S_CARE_OTHER 管 B，医生管 C。"""

    admin: uuid.UUID = S_ADMIN
    care: uuid.UUID = S_CARE
    care_other: uuid.UUID = S_CARE_OTHER
    doctor: uuid.UUID = S_DOCTOR
    order_new_red: uuid.UUID = ORDER_NEW_RED
    order_new_yellow: uuid.UUID = ORDER_NEW_YELLOW
    order_ack_red: uuid.UUID = ORDER_ACK_RED
    order_closed: uuid.UUID = ORDER_CLOSED


def _staff_rows() -> list[dict[str, object]]:
    return [
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
        {
            "staff_id": S_CARE_OTHER,
            "name": "赵护工",
            "role": "care_worker",
            "password_hash": _PASSWORD_PLACEHOLDER,
            "token_version": _TOKEN_VERSION,
            "status": "active",
        },
        {
            "staff_id": S_DOCTOR,
            "name": "王医生",
            "role": "doctor",
            "password_hash": _PASSWORD_PLACEHOLDER,
            "token_version": _TOKEN_VERSION,
            "status": "active",
        },
    ]


def _assignment_rows() -> list[dict[str, object]]:
    return [
        {
            "id": uuid.uuid4(),
            "elder_id": ELDER_A,
            "staff_id": S_CARE,
            "relation": "primary_care",
        },
        {
            "id": uuid.uuid4(),
            "elder_id": ELDER_B,
            "staff_id": S_CARE_OTHER,
            "relation": "primary_care",
        },
        {
            "id": uuid.uuid4(),
            "elder_id": ELDER_C,
            "staff_id": S_DOCTOR,
            "relation": "doctor",
        },
    ]


def _work_order_rows() -> list[dict[str, object]]:
    return [
        {
            "order_id": ORDER_NEW_RED,
            "order_no": "WO-20261112-0001",
            "elder_id": ELDER_A,
            "event_id": EVENT_A,
            "type": "safety_alert",
            "severity": "red",
            "status": "new",
            "created_at": BASE_TIME,
        },
        {
            "order_id": ORDER_NEW_YELLOW,
            "order_no": "WO-20261112-0002",
            "elder_id": ELDER_A,
            "event_id": None,
            "type": "missed_medication",
            "severity": "yellow",
            "status": "new",
            "created_at": BASE_TIME + datetime.timedelta(minutes=1),
        },
        {
            "order_id": ORDER_ACK_RED,
            "order_no": "WO-20261112-0003",
            "elder_id": ELDER_C,
            "event_id": None,
            "type": "health_alert",
            "severity": "red",
            "status": "acknowledged",
            "created_at": BASE_TIME + datetime.timedelta(minutes=2),
        },
        {
            "order_id": ORDER_CLOSED,
            "order_no": "WO-20261112-0004",
            "elder_id": ELDER_A,
            "event_id": None,
            "type": "follow_up",
            "severity": "yellow",
            "status": "closed",
            "created_at": BASE_TIME + datetime.timedelta(minutes=3),
            "closed_at": BASE_TIME + datetime.timedelta(minutes=30),
            "note": "已电话回访",
        },
    ]


@pytest.fixture
async def world(db: AsyncConnection) -> World:
    # 员工行必须先插：`elder_staff_assignments.staff_id` 外键到 staff_accounts，
    # 缺行会直接 ForeignKeyViolation。
    await db.execute(StaffAccount.__table__.insert(), _staff_rows())
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": ELDER_A, "name": "张桂芬", "status": "active"},
            {"elder_id": ELDER_B, "name": "李建国", "status": "active"},
            {"elder_id": ELDER_C, "name": "王秀兰", "status": "active"},
        ],
    )
    await db.execute(ElderStaffAssignment.__table__.insert(), _assignment_rows())
    await db.execute(
        RiskEvent.__table__.insert(),
        [
            {
                "event_id": EVENT_A,
                "elder_id": ELDER_A,
                "type": "safety_alert",
                "severity": "red",
                "source_type": "safety",
                "source_id": SOURCE_RULE,
                "status": "open",
                "created_at": BASE_TIME,
            },
        ],
    )
    await db.execute(WorkOrder.__table__.insert(), _work_order_rows())
    return World()


# ------------------------------------------------------------------ 辅助


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(staff_id: uuid.UUID, role: str) -> str:
    return sign_staff_access(
        kind="staff", staff_id=staff_id, role=role, token_version=_TOKEN_VERSION
    )


def _admin(world: World) -> dict[str, str]:
    return _bearer(_staff_token(world.admin, "admin"))


def _care(world: World) -> dict[str, str]:
    return _bearer(_staff_token(world.care, "care_worker"))


def _care_other(world: World) -> dict[str, str]:
    return _bearer(_staff_token(world.care_other, "care_worker"))


def _doctor(world: World) -> dict[str, str]:
    return _bearer(_staff_token(world.doctor, "doctor"))


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    """断言契约错误体：状态码 + code + detail + 非空 32 位 trace_id。"""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


async def _order_row(db: AsyncConnection, order_id: uuid.UUID) -> object:
    return (
        await db.execute(
            select(
                WorkOrder.status,
                WorkOrder.note,
                WorkOrder.closed_at,
                WorkOrder.false_alarm,
                WorkOrder.false_alarm_reason,
                WorkOrder.actual_handled_at,
            ).where(WorkOrder.order_id == order_id)
        )
    ).one()


async def _transitions(db: AsyncConnection, order_id: uuid.UUID) -> list[object]:
    result = await db.execute(
        select(
            WorkOrderTransition.from_status,
            WorkOrderTransition.to_status,
            WorkOrderTransition.actor_id,
            WorkOrderTransition.note,
        )
        .where(WorkOrderTransition.order_id == order_id)
        .order_by(WorkOrderTransition.at.asc())
    )
    return list(result.all())


async def _event_status(db: AsyncConnection, event_id: uuid.UUID) -> str:
    return (
        await db.execute(select(RiskEvent.status).where(RiskEvent.event_id == event_id))
    ).scalar_one()


async def _audit_rows(db: AsyncConnection, action: str) -> list[Mapping[str, Any]]:
    """查 `audit_logs` 表。审计落在库里（`write_audit` 与业务写库同生共死），不是 logger。

    注意：这里拿到的是 Core 连接，不是 ORM session，所以 `select(AuditLog)` 执行后
    每行是 8 个原始列值（`.scalars()` 只会取第一列 audit_id）。用 `.mappings()`
    读成字典最稳，不依赖 Session 的实体处理。
    """
    result = await db.execute(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at.asc())
    )
    return list(result.mappings().all())


# ------------------------------------------------------------------ E15 · 看板列表


async def test_list_defaults_to_active_orders_with_red_first(
    client: AsyncClient, world: World
) -> None:
    """E15：默认只返回未处理四态，**红档排在黄档之前**（看板的核心语义）。

    用 admin 看全量：护工 S_CARE 只管长者 A，看不到长者 C 那张红档（另有用例覆盖）。
    """
    response = await client.get("/api/work-orders", headers=_admin(world))

    assert response.status_code == 200, response.text
    body = response.json()
    # 默认排除 closed：交接清单不该塞满已经处理完的单子。
    assert body["total"] == 3
    statuses = [item["status"] for item in body["items"]]
    assert "closed" not in statuses
    severities = [item["severity"] for item in body["items"]]
    # 红档置顶：前两条红，最后一条黄。
    assert severities == ["red", "red", "yellow"]
    # 同严重度内按建单时刻升序（老单先处理）。
    assert [item["order_no"] for item in body["items"]] == [
        "WO-20261112-0001",
        "WO-20261112-0003",
        "WO-20261112-0002",
    ]


async def test_list_red_first_survives_pagination(client: AsyncClient, world: World) -> None:
    """E15：分页时红档依然置顶（排序在 SQL 里，不是前端排当前页）。"""
    response = await client.get("/api/work-orders?page=1&page_size=2", headers=_admin(world))

    body = response.json()
    assert [item["severity"] for item in body["items"]] == ["red", "red"]

    second = await client.get("/api/work-orders?page=2&page_size=2", headers=_admin(world))
    assert [item["severity"] for item in second.json()["items"]] == ["yellow"]


async def test_list_filters_by_status_and_severity(client: AsyncClient, world: World) -> None:
    """E15：显式筛选生效；`status=closed` 能查到已关闭的（看历史不留死角）。"""
    closed = await client.get("/api/work-orders?status=closed", headers=_admin(world))
    assert closed.status_code == 200
    assert [item["order_no"] for item in closed.json()["items"]] == ["WO-20261112-0004"]

    yellow = await client.get("/api/work-orders?severity=yellow", headers=_admin(world))
    assert {item["severity"] for item in yellow.json()["items"]} == {"yellow"}


async def test_list_rejects_unknown_status(client: AsyncClient, world: World) -> None:
    """E15：非法状态名是 422（写错了），不是 409（状态不允许）。"""
    response = await client.get("/api/work-orders?status=archived", headers=_admin(world))
    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_care_worker_only_sees_assigned_elders(client: AsyncClient, world: World) -> None:
    """范围过滤：S_CARE 只管长者 A，看不到长者 C 的单子（FR-ADM-06 三道闸门）。"""
    response = await client.get("/api/work-orders", headers=_care(world))

    body = response.json()
    # 长者 A 有两张未处理单（一红一黄），长者 C 那张红档不在范围内。
    assert body["total"] == 2
    assert {item["elder_id"] for item in body["items"]} == {str(ELDER_A)}


async def test_care_worker_does_not_see_other_worker_elder(
    client: AsyncClient, world: World
) -> None:
    """另一位护工只看得到长者 B（本用例里没有单子），证明过滤是真按 assignment 走的。"""
    response = await client.get("/api/work-orders", headers=_care_other(world))

    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_doctor_sees_only_doctor_relation_elders(client: AsyncClient, world: World) -> None:
    """医生按 `relation='doctor'` 收敛：只有长者 C 的那张单子。"""
    response = await client.get("/api/work-orders", headers=_doctor(world))

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["order_no"] == "WO-20261112-0003"


async def test_list_elder_name_is_masked(client: AsyncClient, world: World) -> None:
    """看板列表里的长者姓名必须脱敏（`mask_name`：姓 + *）。"""
    response = await client.get("/api/work-orders", headers=_admin(world))

    names = {item["elder_name"] for item in response.json()["items"]}
    assert "张桂芬" not in names
    assert "张**" in names


async def test_anonymous_gets_401(client: AsyncClient, world: World) -> None:
    """无凭证 → 401（不是 403：拿错凭证体系与权限不足是两件事）。"""
    response = await client.get("/api/work-orders")
    assert response.status_code == 401


# ------------------------------------------------------------------ E16 · 详情与历史


async def test_detail_returns_full_transition_history(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E16：详情带完整流转历史（FR-ALT-02 第一条验收）。"""
    await db.execute(
        WorkOrderTransition.__table__.insert(),
        [
            {
                "transition_id": uuid.uuid4(),
                "order_id": ORDER_ACK_RED,
                "from_status": "new",
                "to_status": "acknowledged",
                "actor_id": S_DOCTOR,
                "at": BASE_TIME + datetime.timedelta(minutes=5),
                "note": "已确认",
            },
        ],
    )

    response = await client.get(f"/api/work-orders/{ORDER_ACK_RED}", headers=_admin(world))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_no"] == "WO-20261112-0003"
    assert len(body["transitions"]) == 1
    row = body["transitions"][0]
    assert (row["from_status"], row["to_status"]) == ("new", "acknowledged")
    assert row["actor_id"] == str(S_DOCTOR)
    assert row["note"] == "已确认"


async def test_detail_of_unknown_order_is_404(client: AsyncClient, world: World) -> None:
    """E16：不存在的工单 → 404 `NOT_FOUND`（不是 500）。"""
    response = await client.get(f"/api/work-orders/{ORDER_MISSING}", headers=_admin(world))
    _assert_error(response, 404, "NOT_FOUND")


async def test_detail_out_of_scope_is_403(client: AsyncClient, world: World) -> None:
    """知道 order_id 也不能越过范围：护工 S_CARE 查不到长者 C 的单子。"""
    response = await client.get(f"/api/work-orders/{ORDER_ACK_RED}", headers=_care(world))
    _assert_error(response, 403, "SCOPE_FORBIDDEN")


# ------------------------------------------------------------------ E17 · 状态流转


async def test_transition_writes_status_row_and_audit(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E17：一次流转写三件事——状态、transitions 行、审计日志。

    审计走 `audit_service.write_audit`（`audit_logs` 表，与业务写库同生共死），
    不是 logger——所以断言查表，不查 caplog。
    """
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "acknowledged", "note": "马上到"},
        headers=_care(world),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "acknowledged"
    # 响应自带刚写入的流转历史，前端不必再发 GET。
    assert len(body["transitions"]) == 1
    assert body["transitions"][0]["to_status"] == "acknowledged"
    assert body["transitions"][0]["note"] == "马上到"

    row = await _order_row(db, ORDER_NEW_RED)
    assert row.status == "acknowledged"

    history = await _transitions(db, ORDER_NEW_RED)
    assert [(item.from_status, item.to_status) for item in history] == [("new", "acknowledged")]
    assert history[0].actor_id == S_CARE

    audits = await _audit_rows(db, "work_order.transition")
    assert len(audits) == 1
    assert audits[0]["resource_id"] == ORDER_NEW_RED
    assert audits[0]["detail"]["from_status"] == "new"
    assert audits[0]["detail"]["to_status"] == "acknowledged"


async def test_illegal_transition_is_409(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E17：`new → resolved` 跳步是 409（当前状态不允许），且不写任何行。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "resolved"},
        headers=_care(world),
    )

    _assert_error(response, 409, "CONFLICT")
    row = await _order_row(db, ORDER_NEW_RED)
    assert row.status == "new"
    assert await _transitions(db, ORDER_NEW_RED) == []


async def test_unknown_status_name_is_422(client: AsyncClient, world: World) -> None:
    """E17：不在六态里的状态名是 422（请求写错了），不是 409。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "archived"},
        headers=_care(world),
    )
    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_transition_forbids_extra_fields(client: AsyncClient, world: World) -> None:
    """E17：夹带未定义字段 → 422（`extra="forbid"`，不静默忽略）。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "acknowledged", "actor_id": str(S_ADMIN)},
        headers=_care(world),
    )
    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_close_requires_disposition_note(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """关闭约束：非误报关单必须填写处置结果（PRD:392）。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_YELLOW}/transitions",
        json={"to_status": "closed"},
        headers=_care(world),
    )

    _assert_error(response, 422, "VALIDATION_ERROR")
    row = await _order_row(db, ORDER_NEW_YELLOW)
    assert row.status == "new"


async def test_close_red_requires_actual_handled_at(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """关闭约束：红档关单必须填实际处置时间（用于响应时长统计）。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "closed", "note": "已送医"},
        headers=_care(world),
    )

    _assert_error(response, 422, "VALIDATION_ERROR")
    row = await _order_row(db, ORDER_NEW_RED)
    assert row.status == "new"


async def test_close_red_with_handled_at_succeeds(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """红档关单：note + actual_handled_at 齐全 → 成功，且三列都落库。"""
    handled = datetime.datetime(2026, 11, 12, 1, 20, tzinfo=datetime.UTC)

    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={
            "to_status": "closed",
            "note": "已送医，家属已通知",
            "actual_handled_at": handled.isoformat(),
        },
        headers=_care(world),
    )

    assert response.status_code == 200, response.text
    row = await _order_row(db, ORDER_NEW_RED)
    assert row.status == "closed"
    assert row.note == "已送医，家属已通知"
    assert row.closed_at is not None
    assert row.actual_handled_at == handled
    assert row.false_alarm is False


async def test_false_alarm_requires_reason(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """关闭约束：标误报必须填误报原因，且原单不会被删（误报只打标记）。"""
    rejected = await client.post(
        f"/api/work-orders/{ORDER_NEW_YELLOW}/transitions",
        json={"to_status": "closed", "false_alarm": True},
        headers=_care(world),
    )
    _assert_error(rejected, 422, "VALIDATION_ERROR")

    accepted = await client.post(
        f"/api/work-orders/{ORDER_NEW_YELLOW}/transitions",
        json={"to_status": "closed", "false_alarm": True, "false_alarm_reason": "误触按钮"},
        headers=_care(world),
    )
    assert accepted.status_code == 200, accepted.text

    row = await _order_row(db, ORDER_NEW_YELLOW)
    assert row.status == "closed"
    assert row.false_alarm is True
    assert row.false_alarm_reason == "误触按钮"


async def test_reopen_is_admin_only(client: AsyncClient, db: AsyncConnection, world: World) -> None:
    """重开只有 admin 能做（PRD:133 把「关工单」列为高风险写操作）。"""
    denied = await client.post(
        f"/api/work-orders/{ORDER_CLOSED}/transitions",
        json={"to_status": "reopened", "note": "家属要求复查"},
        headers=_care(world),
    )
    _assert_error(denied, 403, "ROLE_FORBIDDEN")
    assert (
        await db.execute(select(WorkOrder.status).where(WorkOrder.order_id == ORDER_CLOSED))
    ).scalar_one() == "closed"

    allowed = await client.post(
        f"/api/work-orders/{ORDER_CLOSED}/transitions",
        json={"to_status": "reopened", "note": "家属要求复查"},
        headers=_admin(world),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "reopened"


async def test_reopen_requires_note(client: AsyncClient, world: World) -> None:
    """重开必须填写原因（PRD:392「管理员可重开并保留原因」）。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_CLOSED}/transitions",
        json={"to_status": "reopened"},
        headers=_admin(world),
    )
    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_transition_out_of_scope_is_403(client: AsyncClient, world: World) -> None:
    """范围外的工单不能流转（与详情同一条判定）。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_ACK_RED}/transitions",
        json={"to_status": "in_progress"},
        headers=_care(world),
    )
    _assert_error(response, 403, "SCOPE_FORBIDDEN")


# ------------------------------------------------------------------ E18 · 关闭风险事件


async def test_close_event_requires_closed_order(client: AsyncClient, world: World) -> None:
    """E18：工单未关闭就关事件 → 409（PRD:392 是两步，不是一步）。"""
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/close-event", headers=_care(world)
    )
    _assert_error(response, 409, "CONFLICT")


async def test_close_event_after_closing_order(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E18：关单之后关事件成功，事件状态变 closed。"""
    handled = datetime.datetime(2026, 11, 12, 1, 20, tzinfo=datetime.UTC)
    closed = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "closed", "note": "已送医", "actual_handled_at": handled.isoformat()},
        headers=_care(world),
    )
    assert closed.status_code == 200, closed.text

    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/close-event", headers=_care(world)
    )

    assert response.status_code == 200, response.text
    assert response.json()["event_status"] == "closed"
    assert await _event_status(db, EVENT_A) == "closed"


async def test_close_event_is_idempotent(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E18：事件已关闭时重复调用不报错（幂等，便于前端重试）。"""
    handled = datetime.datetime(2026, 11, 12, 1, 20, tzinfo=datetime.UTC)
    await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "closed", "note": "已送医", "actual_handled_at": handled.isoformat()},
        headers=_care(world),
    )
    first = await client.post(f"/api/work-orders/{ORDER_NEW_RED}/close-event", headers=_care(world))
    second = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/close-event", headers=_care(world)
    )

    assert first.status_code == 200 and second.status_code == 200
    assert await _event_status(db, EVENT_A) == "closed"


# ------------------------------------------------------------------ 第五轮 W4 · FR-ALT-02 复合关闭


async def test_close_order_with_event_closes_both_in_one_transaction(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """关单时带 `event_id`：工单与关联风险事件在同一事务里一起关闭。

    这是 FR-ALT-02 的修法（「同一事务复合关闭」，不是前端拆两步）：一次请求后
    工单 `closed`、事件 `closed` 且 `closed_at` 非空、审计里能看到 operator 与事件号。
    """
    handled = datetime.datetime(2026, 11, 12, 1, 20, tzinfo=datetime.UTC)
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={
            "to_status": "closed",
            "note": "已送医",
            "actual_handled_at": handled.isoformat(),
            "event_id": str(EVENT_A),
        },
        headers=_care(world),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "closed"
    assert body["event_id"] == str(EVENT_A)
    assert body["event_status"] == "closed"

    row = await _order_row(db, ORDER_NEW_RED)
    assert row.status == "closed" and row.closed_at is not None

    event = (
        await db.execute(
            select(RiskEvent.status, RiskEvent.closed_at).where(RiskEvent.event_id == EVENT_A)
        )
    ).one()
    assert event.status == "closed" and event.closed_at is not None

    # 状态变更历史里只有这一条 closed（复合关闭不额外写空流转）。
    transitions = await _transitions(db, ORDER_NEW_RED)
    assert [item.to_status for item in transitions] == ["closed"]

    audits = await _audit_rows(db, "risk_event.close")
    assert any(str(item["resource_id"]) == str(EVENT_A) for item in audits)


async def test_close_order_with_foreign_event_leaves_order_and_events_untouched(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """`event_id` 不属于本单 → 409，且**工单与两条事件都保持原样**（重点断言后者）。

    这是「不许出现『单关了、风险还开着』」的反面证明：复合关闭任一步校验不过就
    整笔回滚，不会留下半成品。
    """
    event_b = uuid.UUID("cf300000-0000-4000-8000-0000000000b2")
    await db.execute(
        RiskEvent.__table__.insert(),
        [
            {
                "event_id": event_b,
                "elder_id": ELDER_B,
                "type": "safety_alert",
                "severity": "red",
                "source_type": "safety",
                "source_id": SOURCE_RULE,
                "status": "open",
                "created_at": BASE_TIME,
            }
        ],
    )
    handled = datetime.datetime(2026, 11, 12, 1, 20, tzinfo=datetime.UTC)
    foreign = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={
            "to_status": "closed",
            "note": "已送医",
            "actual_handled_at": handled.isoformat(),
            "event_id": str(event_b),
        },
        headers=_care(world),
    )
    _assert_error(foreign, 409, "CONFLICT")

    missing = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={
            "to_status": "closed",
            "note": "已送医",
            "actual_handled_at": handled.isoformat(),
            "event_id": "cf300000-0000-4000-8000-00000000dead",
        },
        headers=_care(world),
    )
    _assert_error(missing, 409, "CONFLICT")

    row = await _order_row(db, ORDER_NEW_RED)
    assert row.status == "new" and row.closed_at is None
    assert await _event_status(db, EVENT_A) == "open"
    assert await _event_status(db, event_b) == "open"
    assert await _transitions(db, ORDER_NEW_RED) == []


async def test_close_order_with_already_closed_event_is_rejected(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """事件已被前一单关掉、本单还开着 → 409，且工单与事件行一个字段都不许动。

    这是真会撞的路径（上一场提醒关过事件、这一场工单仍 pending）：服务层的
    「已关闭」分支必须挡在改字段之前，否则就成了「单关了、事件又被人重开」。
    """
    previous_closed_at = datetime.datetime(2026, 11, 12, 0, 30, tzinfo=datetime.UTC)
    await db.execute(
        RiskEvent.__table__.update()
        .where(RiskEvent.event_id == EVENT_A)
        .values(status="closed", closed_at=previous_closed_at)
    )
    event_columns = select(
        RiskEvent.event_id,
        RiskEvent.elder_id,
        RiskEvent.type,
        RiskEvent.severity,
        RiskEvent.source_type,
        RiskEvent.source_id,
        RiskEvent.status,
        RiskEvent.closed_at,
    ).where(RiskEvent.event_id == EVENT_A)
    before = (await db.execute(event_columns)).one()
    assert before.status == "closed" and before.closed_at == previous_closed_at

    handled = datetime.datetime(2026, 11, 12, 1, 20, tzinfo=datetime.UTC)
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={
            "to_status": "closed",
            "note": "已送医",
            "actual_handled_at": handled.isoformat(),
            "event_id": str(EVENT_A),
        },
        headers=_care(world),
    )
    _assert_error(response, 409, "CONFLICT")

    row = await _order_row(db, ORDER_NEW_RED)
    assert row.status == "new" and row.closed_at is None
    after = (await db.execute(event_columns)).one()
    assert after == before
    assert await _transitions(db, ORDER_NEW_RED) == []
    assert await _audit_rows(db, "risk_event.close") == []


async def test_transition_notifies_family_with_work_order_kind(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """C1：接单后给该长者发一条 `kind="work_order"` 的家属可见通知。

    去重键 `work_order:<id>:<status>`：同一次流转重复上报不会刷出第二条。
    """
    response = await client.post(
        f"/api/work-orders/{ORDER_NEW_RED}/transitions",
        json={"to_status": "acknowledged", "note": "马上到"},
        headers=_care(world),
    )
    assert response.status_code == 200, response.text

    rows = (
        (
            await db.execute(
                text(
                    "SELECT kind, elder_id, payload, status FROM notifications "
                    "WHERE kind = 'work_order'"
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1, rows
    assert str(rows[0]["elder_id"]) == str(ELDER_A)
    assert rows[0]["payload"]["to_status"] == "acknowledged"
    assert rows[0]["payload"]["order_no"].startswith("WO-")
    assert rows[0]["status"] == "generated"
