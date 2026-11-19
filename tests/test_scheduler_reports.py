"""日报推送重试的**调度接线**（FR-RPT-03 / AC-25，迭代 10）。

服务层的 1/5/15 重试与失败落账在 `tests/test_notification_service.py` 里已经钉住；
本文件只回答两个上层问题：

1. `build_scheduler` 真的把「到点扫描」注册成了一条 interval 任务——没有这条 job，
   重试簿记只是躺在 `payload.delivery` 里，生产上永远不会推进；
2. `run_daily_report_delivery` 真能开 session → 调服务 → commit，且通道/库异常只记
   日志、不外抛（一次 tick 失败不能把后续 tick 全停）。

**不起调度器时钟**：只构造 `AsyncIOScheduler`、读 job 定义、直接 `await` 任务体。
隔离沿用 `tests/test_notification_service.py`：NullPool 引擎 → 一条真实连接 →
外层事务 → savepoint 的 session 工厂，所有写入随 rollback 消失。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import Elder, ElderGuardian, GuardianAccount, Notification
from app.scheduler import JOB_PREFIX, build_scheduler
from app.scheduler import reports as report_jobs
from app.services import notification_service

#: 固定时间零点：重试推进都相对它算。
ANCHOR = datetime.datetime(2026, 11, 12, 7, 0, tzinfo=datetime.UTC)

#: 首枪偏移（与 `notification_service.DAILY_REPORT_RETRY_MINUTES[0]` 同源）。
FIRST_RETRY = datetime.timedelta(minutes=notification_service.DAILY_REPORT_RETRY_MINUTES[0])


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
    monkeypatch.setattr(report_jobs, "session_factory", factory)
    return factory


@dataclass
class Seed:
    elder_id: uuid.UUID
    guardian_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    elder_id, guardian_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": "张桂芬", "status": "active"}],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [{"guardian_id": guardian_id, "name": "家属张", "password_hash": "x", "status": "active"}],
    )
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "guardian_id": guardian_id,
                "relation": "daughter",
                "status": "active",
            }
        ],
    )
    await db.flush()
    return Seed(elder_id=elder_id, guardian_id=guardian_id)


async def _due_notification(db: AsyncSession, seed: Seed) -> Notification:
    """建一条已排到首枪的日报通知，返回它（`next_retry_at = ANCHOR + 1min`）。"""
    notification = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=uuid.uuid4(),
        report_date=datetime.date(2026, 11, 12),
        version=1,
        now=ANCHOR,
    )
    assert notification is not None
    await db.flush()
    return notification


# --------------------------------------------------------------- 注册与任务体


async def test_build_scheduler_registers_daily_report_delivery_job() -> None:
    """装配根必须把扫描任务挂进去：没有 job 就没有自动重试。"""
    scheduler = build_scheduler()
    job = scheduler.get_job(f"{JOB_PREFIX}:{report_jobs.DELIVERY_JOB_NAME}")
    assert job is not None, "build_scheduler 必须注册日报推送到点扫描"
    assert job.func is report_jobs.run_daily_report_delivery
    assert job.trigger.interval.total_seconds() == report_jobs.DELIVERY_INTERVAL_SECONDS
    # 与既有轮询任务同口径：错过多次只补跑一次、上一轮没跑完不叠加。
    assert job.coalesce is True
    assert job.max_instances == 1


async def test_run_daily_report_delivery_commits_delivered(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """到点后任务体把通知推到 delivered，并自己 commit（不是等调用方提交）。"""
    seed = await _seed(db)
    notification = await _due_notification(db, seed)

    processed = await report_jobs.run_daily_report_delivery(now=ANCHOR + FIRST_RETRY)

    assert processed >= 1
    await db.refresh(notification)
    assert notification.status == "delivered"
    assert notification.payload["delivery"]["next_retry_at"] is None


async def test_run_daily_report_delivery_before_due_does_nothing(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """没到点不推进：扫描是「到点才动」，不能提前把首枪打掉。"""
    seed = await _seed(db)
    notification = await _due_notification(db, seed)

    processed = await report_jobs.run_daily_report_delivery(now=ANCHOR)

    assert processed == 0
    await db.refresh(notification)
    assert notification.status == "generated"


async def test_run_daily_report_delivery_swallows_errors(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务层炸掉只记日志：调度器的一次 tick 失败不该让后续 tick 全停。"""

    async def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("database down")

    monkeypatch.setattr(
        notification_service,
        "deliver_due_daily_reports",
        _boom,
    )

    assert await report_jobs.run_daily_report_delivery(now=ANCHOR + FIRST_RETRY) == 0
