"""四信号「建议关注」调度、落库、派单的测试（迭代 10 第四轮 · W4 / PRD FR-HLT-10）。

只测**命中判定、落通知、派单、幂等、文案红线、长者状态过滤**六件事，不测调度时钟
（不起 `scheduler.start()`，那会把用例变成对墙上时间的竞态）；`now` 由用例注入。

隔离手法沿用 `tests/test_scheduler_devices.py`：`NullPool` 引擎 → 一条真实连接 →
外层事务 → `join_transaction_mode="create_savepoint"` 的 session 工厂，任务体里的
`commit()` 只落到 savepoint，用例结束随 rollback 消失（主库零残留）。

造信号的手法：深夜交互是四条里**最好钉死**的一条——只要在本地 23:00–05:00 之间塞
三条 `decision_logs`，就稳稳命中 `night_frequent_interaction`；其余三条刻意留空
（没有 `health_records`、最新交互不足 48h、意图各不相同），保证用例断言的是
「一条信号」而不是「恰好命中几条里的第几条」。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import (
    DecisionLog,
    Elder,
    ElderStaffAssignment,
    HealthRecord,
    Notification,
    StaffAccount,
    WorkOrder,
)
from app.scheduler import JOB_PREFIX, build_scheduler
from app.scheduler import attention as attention_jobs
from app.services import alert_aggregation

#: 钉死的「现在」：UTC 12:00 = 上海 20:00（业务日 2026-11-12）。
NOW = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.UTC)
#: 上海 23:30 / 00:00 / 23:00 三个深夜时刻（换算成 UTC 存库）。
NIGHT_STAMPS = (
    datetime.datetime(2026, 11, 11, 15, 30, tzinfo=datetime.UTC),
    datetime.datetime(2026, 11, 10, 16, 0, tzinfo=datetime.UTC),
    datetime.datetime(2026, 11, 9, 15, 0, tzinfo=datetime.UTC),
)
NIGHT_CODE = alert_aggregation.SIGNAL_NIGHT_INTERACTION


# --------------------------------------------------------------------- fixtures


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
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


@pytest.fixture
def isolated_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把调度模块的 `session_factory` 换进外层事务：任务体的 commit 只放 savepoint。"""
    factory = async_sessionmaker(
        bind=db.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(attention_jobs, "session_factory", factory)
    return factory


# --------------------------------------------------------------------- 造数据


@dataclass(frozen=True)
class Seed:
    """一套最小现场：长者 + 主责护工（可选）。"""

    elder_id: uuid.UUID
    care_worker_id: uuid.UUID


async def _seed(
    db: AsyncSession,
    *,
    elder_status: str = "active",
    with_care_worker: bool = True,
    night_stamps: tuple[datetime.datetime, ...] = NIGHT_STAMPS,
) -> Seed:
    elder_id = uuid.uuid4()
    care_worker_id = uuid.uuid4()

    await db.execute(
        Elder.__table__.insert().values(
            elder_id=elder_id, name=f"关注测试长者{uuid.uuid4().hex[:4]}", status=elder_status
        )
    )
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=care_worker_id,
            name="李护工",
            role="care_worker",
            password_hash="x",
        )
    )
    if with_care_worker:
        await db.execute(
            ElderStaffAssignment.__table__.insert().values(
                elder_id=elder_id, staff_id=care_worker_id, relation="primary_care"
            )
        )
    for index, stamp in enumerate(night_stamps):
        await db.execute(
            DecisionLog.__table__.insert().values(
                elder_id=str(elder_id),
                layer="L0",
                intent=("weather", "chitchat", "opera")[index % 3],
                sensitive=False,
                is_out_of_domain=False,
                route_ms=5,
                created_at=stamp,
            )
        )
    await db.flush()
    return Seed(elder_id=elder_id, care_worker_id=care_worker_id)


async def _notifications(db: AsyncSession, elder_id: uuid.UUID) -> list[Notification]:
    db.expire_all()
    rows = await db.execute(
        select(Notification)
        .where(
            Notification.elder_id == elder_id,
            Notification.kind == attention_jobs.ATTENTION_NOTIFICATION_KIND,
        )
        .order_by(Notification.created_at, Notification.notification_id)
    )
    return list(rows.scalars().all())


async def _orders(db: AsyncSession, elder_id: uuid.UUID) -> list[WorkOrder]:
    db.expire_all()
    rows = await db.execute(
        select(WorkOrder)
        .where(
            WorkOrder.elder_id == elder_id, WorkOrder.type == attention_jobs.ATTENTION_ORDER_TYPE
        )
        .order_by(WorkOrder.created_at, WorkOrder.order_no)
    )
    return list(rows.scalars().all())


# ------------------------------------------------------------------ 注册与任务体


async def test_scan_creates_notification_and_work_order(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """命中一条信号：建 1 条通知 + 派 1 张黄档 follow_up 单给主责护工；job 必须已注册。"""
    scheduler = build_scheduler()
    job = scheduler.get_job(f"{JOB_PREFIX}:{attention_jobs.ATTENTION_JOB_NAME}")
    assert job is not None, "build_scheduler 必须注册关注扫描"
    assert job.func is attention_jobs.scan_attention_signals
    # 每日一次、09:00：直接读 CronTrigger 的字符串形态，避免依赖 APScheduler 内部字段对象。
    assert f"hour='{attention_jobs.ATTENTION_SCAN_HOUR}'" in str(job.trigger)
    assert "minute='0'" in str(job.trigger)

    seed = await _seed(db)
    created = await attention_jobs.scan_attention_signals(now=NOW)
    assert created == 1

    notifications = await _notifications(db, seed.elder_id)
    assert len(notifications) == 1
    notification = notifications[0]
    # 先抓成普通值：下面 `_orders()` 会 `expire_all()`，再回头读 ORM 属性会撞 MissingGreenlet。
    notification_payload = dict(notification.payload)
    assert notification.severity == "yellow"
    assert notification.assignee_staff_id == seed.care_worker_id
    assert notification_payload["code"] == NIGHT_CODE
    assert notification_payload["record_date"] == "2026-11-12"
    assert notification_payload["level"] == alert_aggregation.LEVEL_ATTENTION

    orders = await _orders(db, seed.elder_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "new"
    assert order.severity == "yellow"
    assert order.assignee_id == seed.care_worker_id
    assert order.note is not None and "建议" in order.note
    # 备注里带着单号回指，护工能在通知里找到对应的单。
    assert notification_payload["order_no"] == order.order_no


async def test_signal_without_primary_care_only_notifies_and_warns(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """没有主责护工：通知照建（待办池还能看到），但不派单，并留一条 warning。"""
    seed = await _seed(db, with_care_worker=False)

    with caplog.at_level("WARNING"):
        created = await attention_jobs.scan_attention_signals(now=NOW)

    assert created == 1
    notifications = await _notifications(db, seed.elder_id)
    assert len(notifications) == 1
    assert notifications[0].assignee_staff_id is None
    assert notifications[0].payload["order_no"] is None
    assert await _orders(db, seed.elder_id) == []
    assert "attention_signal_without_primary_care" in caplog.text


@pytest.mark.parametrize("elder_status", ["away", "archived"])
async def test_out_of_house_elder_is_skipped(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    elder_status: str,
) -> None:
    """不在住的长者（外出/归档）：不评估、不建行（FR-ADM-09 同口径）。"""
    seed = await _seed(db, elder_status=elder_status)

    created = await attention_jobs.scan_attention_signals(now=NOW)

    assert created == 0
    assert await _notifications(db, seed.elder_id) == []
    assert await _orders(db, seed.elder_id) == []


async def test_same_business_day_is_idempotent(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """同长者 + 同信号码 + 同业务日只一条：重跑 created=0，库里不增行。"""
    seed = await _seed(db)

    assert await attention_jobs.scan_attention_signals(now=NOW) == 1
    assert await attention_jobs.scan_attention_signals(now=NOW) == 0

    assert len(await _notifications(db, seed.elder_id)) == 1
    assert len(await _orders(db, seed.elder_id)) == 1


async def test_wording_gate_rejects_diagnostic_text(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文案红线：合成一条带诊断口径的信号，建单前必须被闸门拦下，且一行都不落库。"""
    seed = await _seed(db)

    async def _fake_evaluate(
        session: AsyncSession, *, elder_id: uuid.UUID, now: datetime.datetime | None = None
    ) -> alert_aggregation.AttentionReport:
        return alert_aggregation.AttentionReport(
            elder_id=elder_id,
            generated_at=NOW,
            window_days=alert_aggregation.ATTENTION_WINDOW_DAYS,
            rule_version=alert_aggregation.RULE_VERSION,
            signals=(
                alert_aggregation.AttentionSignal(
                    code="synthetic",
                    title="您这是高血压",
                    detail="（测试用，绕过 _signal 直接构造）",
                    suggestion="建议吃药",
                ),
            ),
        )

    monkeypatch.setattr(alert_aggregation, "evaluate_attention_signals", _fake_evaluate)

    with pytest.raises(alert_aggregation.AttentionWordingError):
        await attention_jobs.scan_attention_signals(now=NOW)

    assert await _notifications(db, seed.elder_id) == []
    assert await _orders(db, seed.elder_id) == []


async def test_no_signal_creates_nothing(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """空信号不建单：没有交互记录的长者本轮 created=0，库里零新增。"""
    seed = await _seed(db, night_stamps=())

    created = await attention_jobs.scan_attention_signals(now=NOW)

    assert created == 0
    assert await _notifications(db, seed.elder_id) == []
    assert await _orders(db, seed.elder_id) == []


async def test_rerun_does_not_duplicate_work_order(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """重跑不重复派单：第二次跑完，单数仍是 1，单号不变（幂等键落在业务日，不靠计数）。"""
    seed = await _seed(db)

    await attention_jobs.scan_attention_signals(now=NOW)
    first_orders = await _orders(db, seed.elder_id)
    assert len(first_orders) == 1
    first_order_no = first_orders[0].order_no

    assert await attention_jobs.scan_attention_signals(now=NOW) == 0
    second_orders = await _orders(db, seed.elder_id)
    assert len(second_orders) == 1
    assert second_orders[0].order_no == first_order_no


async def test_no_health_record_means_no_mood_signal(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """四信号互不串味：没有 `health_records` 时情绪信号不出现（只用深夜信号断言）。"""
    seed = await _seed(db)
    assert (
        await db.execute(
            select(HealthRecord.record_id).where(HealthRecord.elder_id == seed.elder_id)
        )
    ).all() == []

    await attention_jobs.scan_attention_signals(now=NOW)

    notifications = await _notifications(db, seed.elder_id)
    assert [n.payload["code"] for n in notifications] == [NIGHT_CODE]
