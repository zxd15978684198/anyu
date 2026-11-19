"""迭代 9 · W4c：运维统计端点（`/api/stats/overview` 与 `/api/stats/security`）。

覆盖六件事（对应验收要求）：

1. **仅管理员**：医生 / 护工 / 家属访问一律 403 `ROLE_FORBIDDEN`，且 `audit_logs`
   各多一条越权行（`actor_id` = 该主体，`detail.reason` 区分 `non_admin` 与
   `non_staff_channel`）；
2. **数字与夹具一致**：每张统计表的夹具增量都逐项对上，且工单的
   `red + yellow == open`（已结的红单不再计入待办）；
3. **全零不是 null**：事务内清空七张表后，所有叶子字段都是 `int` 且等于 0；
4. **`generated_at` 合法**：能 `fromisoformat` 解析、带时区、偏移为 0；
5. **「今天」按 Asia/Shanghai 取日界**：昨天的事件不进 `events_today`；
6. **`denied_requests` 的三种历史形态都被数到**：`action='tool.denied'`（工具闸门）、
   `detail.result='forbidden'`、`detail.reason` 命中拒绝取值；`reason='checkout'`
   与 `result='success'` 的正常审计不误算。

## 为什么不写死绝对数字

统计端点是**全局**口径，没有 `elder_id` 之类可以按主体过滤的参数；而 `audit_logs`
是只增表、开发库可能残留上一轮的数据（`tests/test_memory_api.py` 的 `_audit_rows`
注释里记过这个坑）。所以计数一律用「调用前的基线 + 夹具增量」断言：
`after[x] == before[x] + 夹具条数`。这样断言仍然精确到个位，但在有历史行的库上不会假红。

## 测试怎么不污染库

同 `tests/test_memory_api.py`：业务读走 `db` fixture 的外层事务（请求内 `commit()`
只释放保存点，结束整体回滚）；越权审计走 `write_audit_isolated`，用
`isolated_session_factory` 把它「自己开的那条会话」钉到同一条测试连接上，否则查不到。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import stats as stats_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    AuditLog,
    Device,
    Elder,
    MedicationEvent,
    MedicationPlan,
    MemoryChunk,
    Room,
    StaffAccount,
    WorkOrder,
)
from app.services import audit_service

#: 测试侧独立实现一份「上海日界」，不 import 端点的 `_today_window_utc`：
#: 用被测代码算期望值等于自己证明自己，日界算错时两边会一起错。
_SHANGHAI = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")


def _today_start_utc() -> datetime.datetime:
    """今日 `00:00:00+08:00` 对应的 UTC 时刻（aware）。"""
    now = datetime.datetime.now(_SHANGHAI)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.UTC)


# --------------------------------------------------------------------- fixtures


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """库连接用 NullPool：pytest-asyncio 每个用例一个新事件循环，
    连接池里的连接跨循环复用会报 `Event loop is closed`（`tests/test_memory_medication_models.py` 同款）。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """外层事务：测试里所有写入最后整体回滚。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await trans.rollback()
        await session.close()
        await conn.close()


@pytest.fixture(autouse=True)
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把越权审计的「独立会话」钉在同一条测试连接上，否则回滚后查不到那行。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(audit_service, "session_factory", factory)
    return factory


@dataclass
class Bundle:
    """一个只挂载 stats 路由的最小应用 + 可切换的身份槽。"""

    client: AsyncClient
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(stats_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db.bind,  # type: ignore[arg-type]
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

    holder: list[Identity] = []

    def override_principal() -> Identity:
        return holder[0]

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_principal] = override_principal

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        yield Bundle(client=ac, holder=holder)


@dataclass
class Seed:
    """一组固定 UUID 的主体；生成在插入之前，好在「基线 → 插夹具 → 再调」之间复用。"""

    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    guardian_id: uuid.UUID
    room_id: uuid.UUID
    elder_id: uuid.UUID
    archived_elder_id: uuid.UUID
    plan_id: uuid.UUID
    second_plan_id: uuid.UUID
    stopped_plan_id: uuid.UUID


@pytest.fixture
def seed() -> Seed:
    ids = [uuid.uuid4() for _ in range(10)]
    return Seed(*ids)


def _as_admin(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.admin_id), role="admin")


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


# --------------------------------------------------------------------- 夹具数据


async def _seed_core(db: AsyncSession, seed: Seed) -> None:
    """一次插满概览要数的五张表，条数是刻意算过的：

    - 长者 2 条（1 active + 1 archived）→ `total=2`、`active=1`
    - 设备 3 条（2 online + 1 offline）→ `total=3`、`online=2`
    - 工单 5 条（3 未结 + resolved/closed 各 1）→ `open=3`、`red=2`、`yellow=1`
    - 药单 3 条（2 active + 1 stopped）→ `plans_active=2`
    - 用药事件 4 条（今日 3 + 昨日 1）→ `events_today=3`、`confirmed_today=2`
    - 记忆切片 4 条（2 active + 1 superseded + 1 deleted）→ `chunks_active=2`
    """
    tag = seed.elder_id.hex[:6]
    today = _today_start_utc()

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": seed.admin_id, "name": "管理员", "role": "admin", "password_hash": "x"},
            {"staff_id": seed.doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
            {
                "staff_id": seed.care_worker_id,
                "name": "护工王",
                "role": "care_worker",
                "password_hash": "x",
            },
        ],
    )
    await db.execute(Room.__table__.insert(), [{"room_id": seed.room_id, "room_no": f"A-{tag}"}])
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": seed.elder_id, "name": "张桂芬", "status": "active"},
            {"elder_id": seed.archived_elder_id, "name": "李振华", "status": "archived"},
        ],
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": uuid.uuid4(),
                "room_id": seed.room_id,
                "elder_id": seed.elder_id,
                "token_hash": f"{tag}-t1",
                "status": "online",
            },
            {
                "device_id": uuid.uuid4(),
                "room_id": seed.room_id,
                "elder_id": None,
                "token_hash": f"{tag}-t2",
                "status": "online",
            },
            {
                "device_id": uuid.uuid4(),
                "room_id": seed.room_id,
                "elder_id": None,
                "token_hash": f"{tag}-t3",
                "status": "offline",
            },
        ],
    )
    await db.execute(
        WorkOrder.__table__.insert(),
        [
            {
                "order_id": uuid.uuid4(),
                "order_no": f"WO-{tag}-1",
                "elder_id": seed.elder_id,
                "type": "sos",
                "severity": "red",
                "status": "new",
            },
            {
                "order_id": uuid.uuid4(),
                "order_no": f"WO-{tag}-2",
                "elder_id": seed.elder_id,
                "type": "health_alert",
                "severity": "yellow",
                "status": "acknowledged",
            },
            {
                "order_id": uuid.uuid4(),
                "order_no": f"WO-{tag}-3",
                "elder_id": seed.elder_id,
                "type": "missed_medication",
                "severity": "red",
                "status": "in_progress",
            },
            # 已结单：红包也不该再算进「红单待办」
            {
                "order_id": uuid.uuid4(),
                "order_no": f"WO-{tag}-4",
                "elder_id": seed.elder_id,
                "type": "device_fault",
                "severity": "red",
                "status": "resolved",
            },
            {
                "order_id": uuid.uuid4(),
                "order_no": f"WO-{tag}-5",
                "elder_id": seed.elder_id,
                "type": "follow_up",
                "severity": "yellow",
                "status": "closed",
            },
        ],
    )
    await db.execute(
        MedicationPlan.__table__.insert(),
        [
            {
                "plan_id": seed.plan_id,
                "elder_id": seed.elder_id,
                "drug_name": "氨氯地平",
                "dose": "5mg",
                "schedule_cron": "08:00",
                "start_date": datetime.date(2026, 1, 1),
                "created_by": seed.doctor_id,
                "status": "active",
            },
            {
                "plan_id": seed.stopped_plan_id,
                "elder_id": seed.elder_id,
                "drug_name": "二甲双胍",
                "dose": "1 片",
                "schedule_cron": "12:00",
                "start_date": datetime.date(2026, 1, 1),
                "end_date": datetime.date(2026, 3, 1),
                "created_by": seed.doctor_id,
                "status": "stopped",
            },
            {
                "plan_id": seed.second_plan_id,
                "elder_id": seed.archived_elder_id,
                "drug_name": "阿司匹林",
                "dose": "100mg",
                "schedule_cron": "20:00",
                "start_date": datetime.date(2026, 2, 1),
                "created_by": seed.doctor_id,
                "status": "active",
            },
        ],
    )
    await db.execute(
        MedicationEvent.__table__.insert(),
        [
            {
                "reminder_id": uuid.uuid4(),
                "plan_id": seed.plan_id,
                "elder_id": seed.elder_id,
                "scheduled_at": today + datetime.timedelta(hours=8),
                "drug_snapshot": {"drug_name": "氨氯地平", "dose": "5mg"},
                "payload_hash": f"{tag}-h1",
                "triggered_by": "scheduler",
                "status": "confirmed",
            },
            {
                "reminder_id": uuid.uuid4(),
                "plan_id": seed.plan_id,
                "elder_id": seed.elder_id,
                "scheduled_at": today + datetime.timedelta(hours=12),
                "drug_snapshot": {"drug_name": "氨氯地平", "dose": "5mg"},
                "payload_hash": f"{tag}-h2",
                "triggered_by": "scheduler",
                "status": "confirmed",
            },
            {
                "reminder_id": uuid.uuid4(),
                "plan_id": seed.plan_id,
                "elder_id": seed.elder_id,
                "scheduled_at": today + datetime.timedelta(hours=20),
                "drug_snapshot": {"drug_name": "氨氯地平", "dose": "5mg"},
                "payload_hash": f"{tag}-h3",
                "triggered_by": "dialog",
                "status": "calling",
            },
            # 昨天：跨日界，两个今日字段都不该数到它
            {
                "reminder_id": uuid.uuid4(),
                "plan_id": seed.plan_id,
                "elder_id": seed.elder_id,
                "scheduled_at": today - datetime.timedelta(hours=2),
                "drug_snapshot": {"drug_name": "氨氯地平", "dose": "5mg"},
                "payload_hash": f"{tag}-h4",
                "triggered_by": "scheduler",
                "status": "confirmed",
            },
        ],
    )
    await db.execute(
        MemoryChunk.__table__.insert(),
        [
            {
                "chunk_id": uuid.uuid4(),
                "elder_id": seed.elder_id,
                "content": "女儿上周日来看过我。",
                "status": "active",
            },
            {
                "chunk_id": uuid.uuid4(),
                "elder_id": seed.elder_id,
                "content": "我年轻时在杭州当过小学教员。",
                "status": "active",
            },
            {
                "chunk_id": uuid.uuid4(),
                "elder_id": seed.elder_id,
                "content": "被新摘要取代的旧记忆。",
                "status": "superseded",
            },
            {
                "chunk_id": uuid.uuid4(),
                "elder_id": seed.elder_id,
                "content": "已软删的记忆。",
                "status": "deleted",
            },
        ],
    )
    await db.flush()


async def _add_audit(
    db: AsyncSession,
    *,
    action: str,
    actor_type: str = "guardian",
    actor_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
    created_at: datetime.datetime | None = None,
) -> None:
    """直接插一行审计（端点自己写库是禁止的，测试夹具可以）。"""
    values: dict[str, Any] = {
        "audit_id": uuid.uuid4(),
        "actor_type": actor_type,
        "actor_id": actor_id or uuid.uuid4(),
        "action": action,
        "resource_type": "test",
        "resource_id": uuid.uuid4(),
        "detail": detail,
    }
    if created_at is not None:
        values["created_at"] = created_at
    await db.execute(AuditLog.__table__.insert(), [values])
    await db.flush()


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按 action + 主体过滤；`audit_logs` 只增，必须带 actor 才不会数到历史行。"""
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    return list(await db.scalars(stmt))


async def _clear_stats_tables(db: AsyncSession) -> None:
    """事务内清空七张统计表（结束回滚），让「全零」可判定。子表先删。"""
    for model in (
        WorkOrder,
        MedicationEvent,
        MedicationPlan,
        MemoryChunk,
        Device,
        Elder,
        AuditLog,
    ):
        await db.execute(delete(model))
    await db.flush()


# ------------------------------------------------------------- 概览：数字与结构


async def test_overview_admin_counts_match_fixtures(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """admin 拿到 200：五组计数与夹具增量逐项相等，且形状/类型/红黄子集都对。"""
    client.holder[:] = [_as_admin(seed)]
    before = (await client.client.get("/api/stats/overview")).json()

    await _seed_core(db, seed)
    resp = await client.client.get("/api/stats/overview")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "elders",
        "devices",
        "work_orders",
        "health",
        "medication",
        "memory",
        "generated_at",
    }
    assert body["elders"]["total"] == before["elders"]["total"] + 2
    assert body["elders"]["active"] == before["elders"]["active"] + 1
    assert body["devices"]["total"] == before["devices"]["total"] + 3
    assert body["devices"]["online"] == before["devices"]["online"] + 2
    assert body["work_orders"]["open"] == before["work_orders"]["open"] + 3
    assert body["work_orders"]["red"] == before["work_orders"]["red"] + 2
    assert body["work_orders"]["yellow"] == before["work_orders"]["yellow"] + 1
    assert body["medication"]["plans_active"] == before["medication"]["plans_active"] + 2
    assert body["medication"]["events_today"] == before["medication"]["events_today"] + 3
    assert body["medication"]["confirmed_today"] == before["medication"]["confirmed_today"] + 2
    assert body["memory"]["chunks_active"] == before["memory"]["chunks_active"] + 2

    # 红黄只在未结集合内拆分：已结的红单不再进 red
    orders = body["work_orders"]
    assert orders["red"] + orders["yellow"] <= orders["open"]

    # 形状与类型：嵌套键齐全，叶子一律是非负 int（不是 null、不是字符串）
    assert set(body["elders"]) == {"total", "active"}
    assert set(body["devices"]) == {"total", "online"}
    assert set(body["work_orders"]) == {"open", "red", "yellow"}
    assert set(body["medication"]) == {"plans_active", "events_today", "confirmed_today"}
    assert set(body["memory"]) == {"chunks_active"}
    for group in ("elders", "devices", "work_orders", "medication", "memory"):
        for name, value in body[group].items():
            assert isinstance(value, int), f"{group}.{name} 不是 int：{value!r}"
            assert value >= 0, f"{group}.{name} 不应为负：{value!r}"


async def test_overview_generated_at_is_iso8601_utc(client: Bundle, seed: Seed) -> None:
    """`generated_at` 可被 `fromisoformat` 解析，带时区且偏移为 0（UTC）。"""
    client.holder[:] = [_as_admin(seed)]
    before = datetime.datetime.now(datetime.UTC)

    resp = await client.client.get("/api/stats/overview")

    assert resp.status_code == 200, resp.text
    raw = resp.json()["generated_at"]
    assert isinstance(raw, str)
    parsed = datetime.datetime.fromisoformat(raw)
    assert parsed.tzinfo is not None, f"generated_at 缺时区：{raw!r}"
    assert parsed.utcoffset() == datetime.timedelta(0), f"generated_at 不是 UTC：{raw!r}"
    assert before <= parsed <= datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=5)


async def test_overview_medication_excludes_yesterday(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """跨日界：只插昨天的事件，今日两个字段都不动。"""
    client.holder[:] = [_as_admin(seed)]
    before = (await client.client.get("/api/stats/overview")).json()

    await _seed_core(db, seed)
    await db.execute(
        MedicationEvent.__table__.insert(),
        [
            {
                "reminder_id": uuid.uuid4(),
                "plan_id": seed.plan_id,
                "elder_id": seed.elder_id,
                "scheduled_at": _today_start_utc() - datetime.timedelta(days=1),
                "drug_snapshot": {"drug_name": "氨氯地平", "dose": "5mg"},
                "payload_hash": f"{seed.elder_id.hex[:6]}-y1",
                "triggered_by": "scheduler",
                "status": "confirmed",
            }
        ],
    )
    await db.flush()

    body = (await client.client.get("/api/stats/overview")).json()

    # 夹具里有 3 条今日事件（2 confirmed），昨天的两条（夹具 1 + 本次 1）都不计入
    assert body["medication"]["events_today"] == before["medication"]["events_today"] + 3
    assert body["medication"]["confirmed_today"] == before["medication"]["confirmed_today"] + 2


async def test_overview_zero_not_null(client: Bundle, db: AsyncSession, seed: Seed) -> None:
    """七张表全空时两个端点都返回 0，而不是 null。"""
    client.holder[:] = [_as_admin(seed)]
    await _clear_stats_tables(db)

    resp = await client.client.get("/api/stats/overview")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elders"] == {"total": 0, "active": 0}
    assert body["devices"] == {"total": 0, "online": 0}
    assert body["work_orders"] == {"open": 0, "red": 0, "yellow": 0}
    assert body["medication"] == {"plans_active": 0, "events_today": 0, "confirmed_today": 0}
    assert body["memory"] == {"chunks_active": 0}
    for group in ("elders", "devices", "work_orders", "medication", "memory"):
        for name, value in body[group].items():
            assert value is not None, f"{group}.{name} 是 null"
            assert value == 0

    security = (await client.client.get("/api/stats/security")).json()
    assert security["audit_today"] == 0
    assert security["denied_requests"] == 0
    assert security["injection_blocked"] == 0
    assert security["exports_today"] == 0


# ------------------------------------------------------------- 安全态势


async def test_security_admin_counts_match_fixtures(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """四个指标各自对上夹具；两天前的导出不进 `exports_today`（旧行不算）。"""
    client.holder[:] = [_as_admin(seed)]
    before = (await client.client.get("/api/stats/security")).json()
    now = datetime.datetime.now(datetime.UTC)
    old = now - datetime.timedelta(days=2)

    await _add_audit(
        db,
        action="injection_blocked",
        detail={"pattern_id": "p-01", "layer": "input", "trace_id": "t-inj"},
        created_at=now,
    )
    await _add_audit(
        db,
        action=audit_service.EXPORT_ACTION,
        actor_type="staff",
        actor_id=seed.admin_id,
        detail={"row_count": 3, "trace_id": "t-exp"},
        created_at=now,
    )
    # 两条历史导出：动作对得上，日子对不上
    await _add_audit(db, action=audit_service.EXPORT_ACTION, detail={}, created_at=old)
    await _add_audit(db, action=stats_api.MEMORY_EXPORT_ACTION, detail={}, created_at=old)

    resp = await client.client.get("/api/stats/security")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "audit_today",
        "denied_requests",
        "injection_blocked",
        "exports_today",
        "generated_at",
    }
    assert body["audit_today"] == before["audit_today"] + 2
    assert body["injection_blocked"] == before["injection_blocked"] + 1
    assert body["exports_today"] == before["exports_today"] + 1
    assert body["denied_requests"] == before["denied_requests"]


async def test_security_denied_counts_all_denial_shapes(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """三种拒绝形态都数到，正常审计（reason='checkout'）与成功审计不误算。"""
    client.holder[:] = [_as_admin(seed)]
    before = (await client.client.get("/api/stats/security")).json()

    # ① 动作即结论：MCP 工具闸门（白名单/预算/熔断/schema）
    await _add_audit(
        db,
        action=stats_api.TOOL_DENIED_ACTION,
        actor_type="system",
        detail={"gate": "whitelist", "tool": "create_work_order", "trace_id": "trace-w9"},
    )
    # ② 端点层形态：memory 那套 detail={"reason": "non_admin", ...}
    await _add_audit(db, action="memory.read", detail={"reason": "non_admin", "role": "doctor"})
    # ③ 服务层形态：_audit(..., result="forbidden", reason="scope")
    await _add_audit(db, action="elder.read", detail={"result": "forbidden", "reason": "scope"})
    # 正常审计不该被算成越权：退房删记忆、成功绑定设备
    await _add_audit(db, action="delete_memory", detail={"reason": "checkout"})
    await _add_audit(db, action="device.bind", detail={"result": "success"})

    body = (await client.client.get("/api/stats/security")).json()

    assert body["denied_requests"] == before["denied_requests"] + 3


async def test_security_zero_not_null(client: Bundle, db: AsyncSession, seed: Seed) -> None:
    """审计表为空时四个计数都是 0。"""
    client.holder[:] = [_as_admin(seed)]
    await _clear_stats_tables(db)

    resp = await client.client.get("/api/stats/security")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_today"] == 0
    assert body["denied_requests"] == 0
    assert body["injection_blocked"] == 0
    assert body["exports_today"] == 0


# ------------------------------------------------------------- 越权：403 + 留痕


async def test_doctor_overview_forbidden_and_audited(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """医生（有效员工、非 admin）→ 403 `ROLE_FORBIDDEN`，并落一条越权审计。"""
    client.holder[:] = [_as_doctor(seed)]

    resp = await client.client.get("/api/stats/overview")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, stats_api.ACTION_OVERVIEW, actor_id=seed.doctor_id)
    assert len(rows) == 1
    assert rows[0].actor_type == "staff"
    assert rows[0].resource_type == stats_api.STATS_RESOURCE_TYPE
    assert rows[0].detail["reason"] == "non_admin"
    assert rows[0].detail["role"] == "doctor"
    assert rows[0].detail["kind"] == "staff"
    assert rows[0].detail["trace_id"] == resp.headers["x-trace-id"]


async def test_care_worker_overview_forbidden_and_audited(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """护工 → 403，审计只多一条（两次请求就两条，不会漏记或重记）。"""
    client.holder[:] = [_as_care_worker(seed)]

    first = await client.client.get("/api/stats/overview")
    second = await client.client.get("/api/stats/overview")

    assert first.status_code == 403, first.text
    assert second.status_code == 403, second.text
    rows = await _audit_rows(db, stats_api.ACTION_OVERVIEW, actor_id=seed.care_worker_id)
    assert len(rows) == 2
    assert {row.detail["reason"] for row in rows} == {"non_admin"}


async def test_guardian_overview_forbidden_with_channel_reason(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """家属（非员工主体）同样是 403 `ROLE_FORBIDDEN`，reason 记 `non_staff_channel`。"""
    client.holder[:] = [_as_guardian(seed)]

    resp = await client.client.get("/api/stats/overview")

    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, stats_api.ACTION_OVERVIEW, actor_id=seed.guardian_id)
    assert len(rows) == 1
    assert rows[0].actor_type == "guardian"
    assert rows[0].detail["reason"] == "non_staff_channel"


async def test_doctor_security_forbidden_and_audited(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """安全态势端点单独留痕（action 与概览不同，排障时能分清是哪个端点被拒）。"""
    client.holder[:] = [_as_doctor(seed)]

    resp = await client.client.get("/api/stats/security")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    assert await _audit_rows(db, stats_api.ACTION_OVERVIEW, actor_id=seed.doctor_id) == []
    rows = await _audit_rows(db, stats_api.ACTION_SECURITY, actor_id=seed.doctor_id)
    assert len(rows) == 1


async def test_forbidden_body_shape_and_trace_id(client: Bundle, seed: Seed) -> None:
    """错误体是契约 §7 的三字段形状，且 `trace_id` 与响应头一致。"""
    client.holder[:] = [_as_guardian(seed)]

    resp = await client.client.get("/api/stats/overview")

    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert set(body) == {"detail", "code", "trace_id"}
    assert body["code"] == "ROLE_FORBIDDEN"
    assert body["detail"]
    assert body["trace_id"] == resp.headers["x-trace-id"]


async def test_denied_access_counted_in_security(
    client: Bundle, db: AsyncSession, seed: Seed
) -> None:
    """越权被拒这件事本身要被安全看板数到：一次 403 → `denied_requests` +1。"""
    client.holder[:] = [_as_admin(seed)]
    before = (await client.client.get("/api/stats/security")).json()

    client.holder[:] = [_as_guardian(seed)]
    denied = await client.client.get("/api/stats/overview")
    assert denied.status_code == 403

    client.holder[:] = [_as_admin(seed)]
    body = (await client.client.get("/api/stats/security")).json()

    assert body["denied_requests"] == before["denied_requests"] + 1
    assert body["audit_today"] == before["audit_today"] + 1


async def test_router_paths() -> None:
    """路由前缀照项目惯例写在模块里（`/api`，不是 `/api/v1`），变量名固定 `router`。"""
    paths = {route.path for route in stats_api.router.routes}
    assert paths == {"/api/stats/overview", "/api/stats/security"}
