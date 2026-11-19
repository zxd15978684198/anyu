"""通知升级的房态闸门（ADM-09 收口项）。

背景：`app/scheduler/notifications.py` 的升级扫描是每 60s 重问一次库的无状态轮询。
人退房（`elders.status` 变 `away`/`archived`）后，**已经入队**的通知原来仍会往家属
手机上抬档——其它四条调度链路都按 `status='active'` 过滤，只有这一条漏了。

收口口径：派发侧过滤。away/archived 长者的通知**既不升级、也不写 `escalated_at`**
（「人不在」不等于「已升级」，人回来时该升的还要能升），跳过的条数进日志
`skipped_away=`。`elder_id IS NULL` 的定向通知没有房间归属，照常升级。

用例只碰独立测试库（`ANYU_TEST_DATABASE_URL`）。前三条在事务内跑服务层，
最后一条跑真实的调度任务体，自带清理。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import Elder, Notification
from app.services import notification_service

#: 固定时间零点：所有「到点」判定都相对它算，不 sleep、不依赖真实时钟。
ANCHOR = datetime.datetime(2026, 11, 12, 7, 0, tzinfo=datetime.UTC)

#: 通知创建时刻要早于 `now - 120s`，否则不是候选。
CREATED_AT = ANCHOR - datetime.timedelta(seconds=300)


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一用例一个事务，跑完整体回滚——不 TRUNCATE，不碰开发库。"""
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


@dataclass
class Seed:
    elder_id: uuid.UUID
    notification_id: uuid.UUID


async def _seed(db: AsyncSession, *, status: str = "active", with_elder: bool = True) -> Seed:
    elder_id = uuid.uuid4()
    notification_id = uuid.uuid4()
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": "张桂芬", "status": status}],
    )
    await db.execute(
        Notification.__table__.insert(),
        [
            {
                "notification_id": notification_id,
                "elder_id": elder_id if with_elder else None,
                "kind": "safety_alert",
                "severity": "yellow",
                "status": "pending",
                "payload": {"note": "房态闸门用例"},
                "created_at": CREATED_AT,
            }
        ],
    )
    return Seed(elder_id=elder_id, notification_id=notification_id)


async def _set_elder_status(db: AsyncSession, elder_id: uuid.UUID, status: str) -> None:
    await db.execute(update(Elder).where(Elder.elder_id == elder_id).values(status=status))
    await db.flush()


async def _row(db: AsyncSession, notification_id: uuid.UUID) -> Notification:
    return (
        await db.execute(
            select(Notification)
            .where(Notification.notification_id == notification_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# --------------------------------------------------------------------- 用例


async def test_away_elder_is_skipped_and_left_unmarked(db: AsyncSession) -> None:
    """长者 away：不升级、不写 `escalated_at`、档位不变（下次回来还能正常升级）。"""
    seed = await _seed(db)
    await _set_elder_status(db, seed.elder_id, "away")

    escalated = await notification_service.escalate_unacknowledged(db, now=ANCHOR)

    assert escalated == []
    row = await _row(db, seed.notification_id)
    assert row.severity == "yellow"
    assert (row.payload or {}).get("escalated_at") is None


async def test_archived_elder_is_skipped(db: AsyncSession) -> None:
    """`archived` 与 `away` 同解：都不在场，都不升级。"""
    seed = await _seed(db)
    await _set_elder_status(db, seed.elder_id, "archived")

    assert await notification_service.escalate_unacknowledged(db, now=ANCHOR) == []


async def test_elder_back_in_house_escalates_again(db: AsyncSession) -> None:
    """房态恢复 active 后**同一条**通知照常升级——跳过不等于丢弃，也没打错标记。"""
    seed = await _seed(db)
    await _set_elder_status(db, seed.elder_id, "away")
    assert await notification_service.escalate_unacknowledged(db, now=ANCHOR) == []

    await _set_elder_status(db, seed.elder_id, "active")
    escalated = await notification_service.escalate_unacknowledged(db, now=ANCHOR)

    assert escalated == [seed.notification_id]
    row = await _row(db, seed.notification_id)
    assert row.severity == "red"  # yellow → red，只抬一档
    assert (row.payload or {})["escalated_at"] == ANCHOR.isoformat()


async def test_active_elder_still_escalates(db: AsyncSession) -> None:
    """闸门只挡「不在场」，active 长者的升级路径一字未改。"""
    seed = await _seed(db)

    assert await notification_service.escalate_unacknowledged(db, now=ANCHOR) == [
        seed.notification_id
    ]


async def test_notification_without_elder_still_escalates(db: AsyncSession) -> None:
    """`elder_id IS NULL` 的定向通知没有房间归属，不该被房态闸门误伤。"""
    seed = await _seed(db, with_elder=False)

    assert await notification_service.escalate_unacknowledged(db, now=ANCHOR) == [
        seed.notification_id
    ]


async def test_scheduler_job_skips_away_and_logs_count(caplog) -> None:
    """任务体（`run_notification_escalation`）真实跑一轮：返回空 + `skipped_away` 计数。

    这条走的是 `session_factory`（自己 commit），所以种数据要独立提交、用完自己清。
    """
    from app.db import engine as app_engine
    from app.db import session_factory
    from app.scheduler.notifications import run_notification_escalation

    # 全局 `app.db` 引擎是跨模块共享的，而 pytest-asyncio 每个用例一个 event loop：
    # 前面的模块（如 `write_audit_isolated`）可能在**已关闭的 loop** 上留下了池内
    # 连接，这里直接复用会拿到坏连接（`Event loop is closed` / `'NoneType' ... send`）。
    # 先用 `dispose()` 清空池，再开自己的连接；用完再清一次，别把脏连接留给别人。
    await app_engine.dispose()

    elder_id, notification_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with session_factory() as session:
            await session.execute(
                Elder.__table__.insert(),
                [{"elder_id": elder_id, "name": "李桂芳", "status": "away"}],
            )
            await session.execute(
                Notification.__table__.insert(),
                [
                    {
                        "notification_id": notification_id,
                        "elder_id": elder_id,
                        "kind": "safety_alert",
                        "severity": "yellow",
                        "status": "pending",
                        "payload": {"note": "调度层房态用例"},
                        "created_at": CREATED_AT,
                    }
                ],
            )
            await session.commit()

        caplog.set_level("INFO", logger="app.services.notification_service")
        escalated = await run_notification_escalation(now=ANCHOR)
        assert escalated == []
        assert any(getattr(record, "skipped_away", 0) >= 1 for record in caplog.records), [
            record.__dict__ for record in caplog.records
        ]

        async with session_factory() as session:
            severity = (
                await session.execute(
                    select(Notification.severity).where(
                        Notification.notification_id == notification_id
                    )
                )
            ).scalar_one()
        assert severity == "yellow"
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(Notification).where(Notification.notification_id == notification_id)
            )
            await session.execute(delete(Elder).where(Elder.elder_id == elder_id))
            await session.commit()
        # 这条用例是全文件唯一碰全局 `app.db` 引擎的地方；pytest-asyncio 每个用例一个
        # event loop，连接池里残留的连接绑在**已关闭的 loop** 上，后面的模块一旦也走
        # `session_factory`（如 `write_audit_isolated`）就会拿到坏连接、静默丢审计。
        # 用例结束时把池清空，别把跨 loop 的脏连接留给下一个模块。
        await app_engine.dispose()
