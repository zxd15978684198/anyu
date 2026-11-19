"""通知 / 工单幂等去重键的测试（迭代 10 第五轮 · W1 / PRD FR-ALT-04）。

只测**去重键这一层**：同键连调两次只留一行且第二次带 `deduplicated=True`、
并发五路仍只留一行、不传键时行为与改造前完全一致（不去重）。

写作用域与口径：

- 服务层（`notification_service.create_notification` /
  `work_order_service.create_work_order`）是唯一入口，这里只打这两个入口；
- 「同键两行」的并发路径靠**真实多连接**复现（`asyncio.gather` + 各自 session），
  不用 mock——mock 掉 `IntegrityError` 只能证明「我们写了 except 分支」，证明不了
  「唯一索引真的会挡」；
- 并发那两条用例会真提交（要跨事务撞唯一索引），所以结束时按随机 `elder_id` 清干净，
  不依赖外层事务回滚（其余用例仍用 `tests/test_scheduler_devices.py` 的隔离手法）。
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import Elder, Notification, StaffAccount, WorkOrder
from app.services import notification_service, work_order_service

#: 钉死的「现在」：UTC 12:00 = 上海 20:00（业务日 2026-11-12）。
NOW = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.UTC)


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
    """外层事务里的 session：服务层的 flush 落到事务里，用例结束整体回滚。"""
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


@dataclass(frozen=True)
class Seed:
    elder_id: uuid.UUID
    staff_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    elder_id = uuid.uuid4()
    staff_id = uuid.uuid4()
    await db.execute(
        Elder.__table__.insert().values(
            elder_id=elder_id, name=f"幂等测试长者{uuid.uuid4().hex[:4]}", status="active"
        )
    )
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=staff_id,
            name="幂等测试护工",
            role="care_worker",
            password_hash="x",
            status="active",
        )
    )
    await db.flush()
    return Seed(elder_id=elder_id, staff_id=staff_id)


async def _notification_rows(db: AsyncSession, *, elder_id: uuid.UUID) -> list[Notification]:
    return list(
        (
            await db.execute(
                select(Notification)
                .where(Notification.elder_id == elder_id)
                .order_by(Notification.created_at, Notification.notification_id)
            )
        )
        .scalars()
        .all()
    )


async def _work_order_rows(db: AsyncSession, *, elder_id: uuid.UUID) -> list[WorkOrder]:
    return list(
        (
            await db.execute(
                select(WorkOrder)
                .where(WorkOrder.elder_id == elder_id)
                .order_by(WorkOrder.created_at, WorkOrder.order_id)
            )
        )
        .scalars()
        .all()
    )


async def _cleanup(engine: AsyncEngine, *, elder_ids: list[uuid.UUID]) -> None:
    """并发用例真提交过，按随机长者 id 把自己造的行删干净（不碰别人的数据）。"""
    async with engine.begin() as conn:
        await conn.execute(delete(Notification).where(Notification.elder_id.in_(elder_ids)))
        await conn.execute(delete(WorkOrder).where(WorkOrder.elder_id.in_(elder_ids)))
        await conn.execute(delete(Elder).where(Elder.elder_id.in_(elder_ids)))


# ------------------------------------------------------------------ 串行去重


async def test_notification_same_key_returns_existing_row(db: AsyncSession) -> None:
    seed = await _seed(db)
    key = "medication_reminder:test-plan:2026-11-12T08:00:00+00:00"

    first = await notification_service.create_notification(
        db,
        kind="medication_escalation",
        severity="red",
        elder_id=seed.elder_id,
        payload={"module": "medication"},
        dedupe_key=key,
    )
    # 先断言再发第二次：命中缓存时 SQLAlchemy 返回的是**同一个** ORM 实例，
    # 第二次调用会把它标成 deduplicated=True，晚断言就会看着像是第一次也被去重了。
    assert first.deduplicated is False
    first_id = first.notification_id

    second = await notification_service.create_notification(
        db,
        kind="medication_escalation",
        severity="red",
        elder_id=seed.elder_id,
        payload={"module": "medication"},
        dedupe_key=key,
    )

    assert second.deduplicated is True
    assert second.notification_id == first_id
    rows = await _notification_rows(db, elder_id=seed.elder_id)
    assert len(rows) == 1
    assert rows[0].dedupe_key == key


async def test_notification_key_is_scoped_by_kind(db: AsyncSession) -> None:
    """同 elder 同键、**不同 kind** 是两条（索引是 `(elder_id, kind, dedupe_key)`）。"""
    seed = await _seed(db)
    key = "shared:2026-11-12"

    a = await notification_service.create_notification(
        db, kind="safety_alert", severity="yellow", elder_id=seed.elder_id, dedupe_key=key
    )
    b = await notification_service.create_notification(
        db, kind="attention_signal", severity="yellow", elder_id=seed.elder_id, dedupe_key=key
    )

    assert a.notification_id != b.notification_id
    assert b.deduplicated is False
    assert len(await _notification_rows(db, elder_id=seed.elder_id)) == 2


async def test_notification_without_key_keeps_old_behaviour(db: AsyncSession) -> None:
    """不传键 = 老行为：重复调用就多一行（人工重发 / 无长者维度的系统通知靠这条）。"""
    seed = await _seed(db)
    for _ in range(2):
        created = await notification_service.create_notification(
            db, kind="safety_alert", severity="green", elder_id=seed.elder_id
        )
        assert created.deduplicated is False

    rows = await _notification_rows(db, elder_id=seed.elder_id)
    assert len(rows) == 2
    assert all(row.dedupe_key is None for row in rows)


async def test_notification_system_level_is_not_deduped(db: AsyncSession) -> None:
    """`elder_id IS NULL` 的站内通知没有长者维度，不参与去重（否则会互相吃掉）。"""
    for _ in range(2):
        await notification_service.create_notification(
            db, kind="family_report", severity="green", elder_id=None, dedupe_key="sys:1"
        )
    total = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.elder_id.is_(None), Notification.dedupe_key == "sys:1")
        )
    ).scalar_one()
    assert total == 2


async def test_work_order_same_key_returns_existing_row(db: AsyncSession) -> None:
    seed = await _seed(db)
    key = f"device_offline:{uuid.uuid4()}:2026-11-12:1"

    first = await work_order_service.create_work_order(
        db,
        elder_id=seed.elder_id,
        type="device_fault",
        severity="yellow",
        note="设备心跳停止",
        dedupe_key=key,
    )
    assert first.deduplicated is False
    first_id = first.order_id

    second = await work_order_service.create_work_order(
        db,
        elder_id=seed.elder_id,
        type="device_fault",
        severity="yellow",
        note="设备心跳停止",
        dedupe_key=key,
    )

    assert second.deduplicated is True
    assert second.order_id == first_id
    rows = await _work_order_rows(db, elder_id=seed.elder_id)
    assert len(rows) == 1


async def test_oversized_dedupe_key_is_rejected(db: AsyncSession) -> None:
    seed = await _seed(db)
    with pytest.raises(notification_service.NotificationValidationError):
        await notification_service.create_notification(
            db,
            kind="safety_alert",
            severity="yellow",
            elder_id=seed.elder_id,
            dedupe_key="k" * 121,
        )
    with pytest.raises(work_order_service.WorkOrderValidationError):
        await work_order_service.create_work_order(
            db,
            elder_id=seed.elder_id,
            type="device_fault",
            severity="yellow",
            note="x",
            dedupe_key="k" * 121,
        )


async def test_partial_unique_index_rejects_duplicate_rows(db: AsyncSession) -> None:
    """绕开服务层的预查，直接插两行同键：唯一索引必须挡第二行。

    这条用例证明的是**库约束真的在**——服务层的「先查后插」只能挡住串行重放，
    挡住并发的是索引（上面前两条并发用例测的正是这条路径）。
    """
    seed = await _seed(db)
    key = f"raw:{uuid.uuid4()}"
    values = {
        "elder_id": seed.elder_id,
        "kind": "safety_alert",
        "severity": "yellow",
        "status": "generated",
        "payload": {},
        "dedupe_key": key,
    }
    await db.execute(Notification.__table__.insert().values(**values))
    with pytest.raises(IntegrityError):
        await db.execute(Notification.__table__.insert().values(**values))


# -------------------------------------------------------------------- 并发去重


async def test_concurrent_notifications_collapse_to_one_row(db_engine: AsyncEngine) -> None:
    """五路同时建同键通知：库级唯一索引兜底，最终只留一行。

    这条用例**真提交**（要跨连接撞索引），结束按随机 elder_id 清干净。
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    elder_id = uuid.uuid4()
    key = f"concurrent:{uuid.uuid4()}"

    async with factory() as session:
        await session.execute(
            Elder.__table__.insert().values(elder_id=elder_id, name="并发测试长者", status="active")
        )
        await session.commit()

    async def _one() -> uuid.UUID:
        async with factory() as session:
            created = await notification_service.create_notification(
                session,
                kind="safety_alert",
                severity="red",
                elder_id=elder_id,
                payload={"module": "devices"},
                dedupe_key=key,
            )
            await session.commit()
            return created.notification_id

    try:
        ids = await asyncio.gather(*[_one() for _ in range(5)])
        async with factory() as session:
            rows = await _notification_rows(session, elder_id=elder_id)
        assert len(rows) == 1
        assert len(set(ids)) == 1
    finally:
        await _cleanup(db_engine, elder_ids=[elder_id])


async def test_concurrent_work_orders_collapse_to_one_row(db_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    elder_id = uuid.uuid4()
    key = f"concurrent-order:{uuid.uuid4()}"

    async with factory() as session:
        await session.execute(
            Elder.__table__.insert().values(elder_id=elder_id, name="并发测试长者", status="active")
        )
        await session.commit()

    async def _one() -> uuid.UUID:
        async with factory() as session:
            order = await work_order_service.create_work_order(
                session,
                elder_id=elder_id,
                type="device_fault",
                severity="yellow",
                note="并发掉线",
                dedupe_key=key,
            )
            await session.commit()
            return order.order_id

    try:
        ids = await asyncio.gather(*[_one() for _ in range(5)])
        async with factory() as session:
            rows = await _work_order_rows(session, elder_id=elder_id)
        assert len(rows) == 1
        assert len(set(ids)) == 1
    finally:
        await _cleanup(db_engine, elder_ids=[elder_id])
