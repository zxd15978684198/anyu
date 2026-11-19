"""红色事件 120 秒超时升级（AC-12）的服务层与调度层测试。

契约级断言（比「函数返回了 id」更重要）：

1. **只有红档 + 未确认 + 满 120 秒才升**：黄档、已确认、119 秒三条都用例锁住，
   否则改一行条件就能让 AC-12 变成「把所有事件都升级」；
2. **边界取闭区间**：恰好 120 秒要升（`<=` 不是 `<`）；
3. **幂等靠 `status`**：第二次调用返回空表，且不会为同一事件建第二串通知；
4. **管理员定向通知**：每个在职 admin 各一条，停用账号与非 admin 账号不建；
5. **任务体自己 commit，异常不外抛**：调度器里一次失败不能让后续 tick 全停。

夹具与 `tests/test_scheduler_medication.py` 同构（真 PostgreSQL、每用例一外层事务、
结束回滚、连不上库即 fail），任务体那条 commit 用 `create_savepoint` 关进外层事务。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

import app.scheduler as scheduler_pkg
from app.core.config import get_settings
from app.models import Elder, Notification, RiskEvent, StaffAccount
from app.scheduler import build_scheduler
from app.scheduler import risks as risk_jobs
from app.services import risk_event_service

# ------------------------------------------------------------------ 固定 ID（全部虚构）

ELDER_A = uuid.UUID("cf400000-0000-4000-8000-0000000000a1")

S_ADMIN = uuid.UUID("cf410000-0000-4000-8000-000000000001")
S_ADMIN_B = uuid.UUID("cf410000-0000-4000-8000-000000000002")
S_ADMIN_DISABLED = uuid.UUID("cf410000-0000-4000-8000-000000000003")
S_CARE = uuid.UUID("cf410000-0000-4000-8000-000000000004")

EVENT_RED_OVERDUE = uuid.UUID("cf420000-0000-4000-8000-000000000001")
EVENT_RED_FRESH = uuid.UUID("cf420000-0000-4000-8000-000000000002")
EVENT_RED_ACKED = uuid.UUID("cf420000-0000-4000-8000-000000000003")
EVENT_YELLOW_OVERDUE = uuid.UUID("cf420000-0000-4000-8000-000000000004")
EVENT_RED_EDGE = uuid.UUID("cf420000-0000-4000-8000-000000000005")

#: `risk_events.source_id` 是 UUID 列（不是自由文本），种子值必须是合法 UUID。
SOURCE_RULE = uuid.UUID("cf430000-0000-4000-8000-00000000000f")

#: 判定时刻固定成整分，119 / 120 / 121 秒三条边界才不会被微秒抖动打乱。
NOW = datetime.datetime(2026, 11, 12, 1, 10, tzinfo=datetime.UTC)

#: `password_hash` 在鉴权链路里只做 NOT NULL 占位，不跑 bcrypt。
_PASSWORD_PLACEHOLDER = "risk-escalation-test-row-not-a-real-hash"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """独立 NullPool 引擎：用完即断，不污染共享连接池。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一条真实连接 + 外层事务：所有写入最后随 rollback 消失（主库零残留）。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def isolated_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """把调度模块的 `session_factory` 换进外层事务里的工厂。

    任务体自己会 `await session.commit()`；`create_savepoint` 让这个 commit 只释放
    savepoint，所以外层 rollback 依然能把整场测试抹干净。
    """
    factory = async_sessionmaker(
        bind=db.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(risk_jobs, "session_factory", factory)
    yield factory


# ------------------------------------------------------------------ 种子数据


@dataclass(frozen=True)
class World:
    """两位在职管理员、一位停用管理员、一位护工；四个事件覆盖四条取数条件。"""

    admin: uuid.UUID = S_ADMIN
    admin_b: uuid.UUID = S_ADMIN_B


def _staff_rows() -> list[dict[str, object]]:
    base = {"password_hash": _PASSWORD_PLACEHOLDER, "status": "active"}
    return [
        {"staff_id": S_ADMIN, "name": "王管理", "role": "admin", **base},
        {"staff_id": S_ADMIN_B, "name": "周管理", "role": "admin", **base},
        {
            "staff_id": S_ADMIN_DISABLED,
            "name": "停用管理",
            "role": "admin",
            "password_hash": _PASSWORD_PLACEHOLDER,
            "status": "disabled",
        },
        {"staff_id": S_CARE, "name": "李护工", "role": "care_worker", **base},
    ]


def _risk_event_row(
    event_id: uuid.UUID,
    *,
    severity: str = "red",
    created_at: datetime.datetime,
    acknowledged_at: datetime.datetime | None = None,
    status: str = "open",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "elder_id": ELDER_A,
        "type": "safety_alert",
        "severity": severity,
        "source_type": "safety",
        "source_id": SOURCE_RULE,
        "status": status,
        "created_at": created_at,
        "acknowledged_at": acknowledged_at,
    }


@pytest.fixture
async def seeded(db: AsyncSession) -> World:
    """员工行必须先插：`risk_events.elder_id` 外键到 elders。"""
    await db.execute(StaffAccount.__table__.insert(), _staff_rows())
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": ELDER_A, "name": "张桂芬", "status": "active"}],
    )
    await db.execute(
        RiskEvent.__table__.insert(),
        [
            # 121 秒前建的红色事件、无人确认 → 唯一该升的一条。
            _risk_event_row(EVENT_RED_OVERDUE, created_at=NOW - datetime.timedelta(seconds=121)),
            # 119 秒前 → 还没到 2 分钟，不许抢跑。
            _risk_event_row(EVENT_RED_FRESH, created_at=NOW - datetime.timedelta(seconds=119)),
            # 红色但已被认领 → 不再打扰。
            _risk_event_row(
                EVENT_RED_ACKED,
                created_at=NOW - datetime.timedelta(seconds=600),
                acknowledged_at=NOW - datetime.timedelta(seconds=540),
            ),
            # 黄色即使放了一夜也不归这条规则管。
            _risk_event_row(
                EVENT_YELLOW_OVERDUE,
                severity="yellow",
                created_at=NOW - datetime.timedelta(seconds=3600),
            ),
        ],
    )
    return World()


# ------------------------------------------------------------------ 辅助


async def _status(db: AsyncSession, event_id: uuid.UUID) -> str:
    return (
        await db.execute(select(RiskEvent.status).where(RiskEvent.event_id == event_id))
    ).scalar_one()


async def _detail(db: AsyncSession, event_id: uuid.UUID) -> dict[str, object]:
    return (
        await db.execute(select(RiskEvent.detail).where(RiskEvent.event_id == event_id))
    ).scalar_one()


async def _notifications_for(db: AsyncSession, event_id: uuid.UUID) -> list[Notification]:
    rows = (
        await db.execute(
            select(Notification).where(
                Notification.payload["risk_event_id"].astext == str(event_id)
            )
        )
    ).scalars()
    return list(rows.all())


# ------------------------------------------------------------------ 服务层：四条取数条件


async def test_escalates_overdue_unacknowledged_red_event(db: AsyncSession, seeded: World) -> None:
    """红色 + 无人确认 + 满 120 秒 → 就地升 `escalated` 并写 `escalated_at`。"""
    escalated = await risk_event_service.escalate_overdue_red_events(db, now=NOW)

    assert escalated == [EVENT_RED_OVERDUE]
    assert await _status(db, EVENT_RED_OVERDUE) == "escalated"
    detail = await _detail(db, EVENT_RED_OVERDUE)
    assert detail["escalated_at"] == NOW.isoformat()
    assert detail["escalation_reason"] == "unacknowledged_120s"


async def test_yellow_event_is_left_alone(db: AsyncSession, seeded: World) -> None:
    """黄色事件放了一夜也不归这条规则管：只有红色进候选。"""
    escalated = await risk_event_service.escalate_overdue_red_events(db, now=NOW)

    assert EVENT_YELLOW_OVERDUE not in escalated
    assert await _status(db, EVENT_YELLOW_OVERDUE) == "open"


async def test_acknowledged_event_is_left_alone(db: AsyncSession, seeded: World) -> None:
    """已确认（`acknowledged_at` 非空）的红色事件不再升——有人接手了。"""
    escalated = await risk_event_service.escalate_overdue_red_events(db, now=NOW)

    assert EVENT_RED_ACKED not in escalated
    assert await _status(db, EVENT_RED_ACKED) == "open"


async def test_event_within_timeout_is_left_alone(db: AsyncSession, seeded: World) -> None:
    """119 秒还没到 2 分钟：不许抢跑。"""
    escalated = await risk_event_service.escalate_overdue_red_events(db, now=NOW)

    assert EVENT_RED_FRESH not in escalated
    assert await _status(db, EVENT_RED_FRESH) == "open"


async def test_boundary_at_exactly_timeout_escalates(db: AsyncSession, seeded: World) -> None:
    """恰好 120 秒算超时（`created_at <= cutoff`，闭区间）。"""
    await db.execute(
        RiskEvent.__table__.insert(),
        _risk_event_row(EVENT_RED_EDGE, created_at=NOW - datetime.timedelta(seconds=120)),
    )

    escalated = await risk_event_service.escalate_overdue_red_events(db, now=NOW)

    assert EVENT_RED_EDGE in escalated
    assert await _status(db, EVENT_RED_EDGE) == "escalated"


# ------------------------------------------------------------------ 幂等与通知


async def test_second_call_returns_empty_and_does_not_duplicate_notifications(
    db: AsyncSession, seeded: World
) -> None:
    """幂等靠 `status`：第二次调用没有候选，也不会再建一串通知。"""
    first = await risk_event_service.escalate_overdue_red_events(db, now=NOW)
    second = await risk_event_service.escalate_overdue_red_events(db, now=NOW)

    assert first == [EVENT_RED_OVERDUE]
    assert second == []
    count = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.payload["risk_event_id"].astext == str(EVENT_RED_OVERDUE))
        )
    ).scalar_one()
    assert count == 2  # 只有两个在职管理员


async def test_escalation_notifies_every_active_admin(db: AsyncSession, seeded: World) -> None:
    """升级时给每个在职 admin 各建一条红档通知；停用 admin 与护工不建。"""
    await risk_event_service.escalate_overdue_red_events(db, now=NOW)

    rows = await _notifications_for(db, EVENT_RED_OVERDUE)
    assert len(rows) == 2
    assert {row.assignee_staff_id for row in rows} == {S_ADMIN, S_ADMIN_B}
    assert all(row.kind == "safety_alert" for row in rows)
    assert all(row.severity == "red" for row in rows)
    assert all(row.elder_id == ELDER_A for row in rows)
    assert all(row.status == "generated" for row in rows)

    payload = rows[0].payload or {}
    assert payload["module"] == "risk_event_escalation"
    assert payload["risk_event_id"] == str(EVENT_RED_OVERDUE)
    assert payload["event_type"] == "safety_alert"
    assert payload["escalation_reason"] == "unacknowledged_120s"
    assert payload["escalated_at"] == NOW.isoformat()


# ------------------------------------------------------------------ 调度层


async def test_job_commits_and_reports_escalated_ids(
    db: AsyncSession,
    seeded: World,
    isolated_factory: async_sessionmaker[AsyncSession],
) -> None:
    """任务体自己 commit（关进 savepoint）；返回文本 id，重跑返回空表。"""
    escalated = await risk_jobs.run_red_event_escalation(now=NOW)

    assert escalated == [str(EVENT_RED_OVERDUE)]
    assert await _status(db, EVENT_RED_OVERDUE) == "escalated"
    assert await risk_jobs.run_red_event_escalation(now=NOW) == []


async def test_job_swallows_service_errors(
    db: AsyncSession,
    seeded: World,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务层抛错只记日志：任务体不往外抛，返回已处理的部分（空表）。"""

    async def _boom(*args: object, **kwargs: object) -> list[uuid.UUID]:
        raise RuntimeError("boom")

    monkeypatch.setattr(risk_event_service, "escalate_overdue_red_events", _boom)

    assert await risk_jobs.run_red_event_escalation(now=NOW) == []
    assert await _status(db, EVENT_RED_OVERDUE) == "open"


@dataclass
class _SettingsStub:
    """只喂 `build_scheduler()` 真正读的字段，避免测试依赖 `.env` 的实际取值。"""

    scheduler_enabled: bool
    scheduler_timezone: str = "Asia/Shanghai"
    health_checkup_start: str = "19:00"
    health_checkup_end: str = "20:00"
    daily_report_generate_hour: str = "07:00"


def test_scheduler_registers_risk_event_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """新增的升级任务与既有任务同口径，且既有 job 一个都不许被动过。"""
    stub = _SettingsStub(scheduler_enabled=True)
    monkeypatch.setattr(scheduler_pkg, "get_settings", lambda: stub)
    scheduler = build_scheduler()
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}
    finally:
        if scheduler.running:  # 未 start() 的调度器 shutdown 会抛 SchedulerNotRunningError
            scheduler.shutdown(wait=False)

    job = jobs["anyu:risk-event-escalation"]
    assert str(job.trigger) == "interval[0:01:00]"
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time == 300

    # 既有任务（三个日任务 + 用药/维护/通知升级）必须在，这次只许加一个。
    assert {
        "anyu:health-checkup-due",
        "anyu:health-missed-scan",
        "anyu:daily-report-generate",
        "anyu:medication-scan",
        "anyu:medication-due-scan",
        "anyu:audio-cleanup",
        "anyu:memory-decay",
        "anyu:memory-capacity",
        "anyu:notification-escalation",
    } <= set(jobs)
