"""设备掉线派单的测试（迭代 10 · W6 / PRD FR-ADM-10 后半）。

只测**判据、派单、通知、幂等**四件事，不测调度时钟（不起 `scheduler.start()`，
那会把用例变成对墙上时间的竞态）；`now` 由用例注入，阈值 5 分钟是业务口径，
不需要 `sleep(300)`。

隔离手法沿用 `tests/test_scheduler_health.py`：`NullPool` 引擎 → 一条真实连接 →
外层事务 → `join_transaction_mode="create_savepoint"` 的 session 工厂，任务体里的
`commit()` 只落到 savepoint，用例结束随 rollback 消失（主库零残留）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
    Device,
    Elder,
    ElderStaffAssignment,
    Notification,
    Room,
    StaffAccount,
    WorkOrder,
)
from app.scheduler import JOB_PREFIX, build_scheduler
from app.scheduler import devices as device_jobs

NOW = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.UTC)
#: 10 分钟没心跳：稳稳越过 5 分钟阈值。
STALE = NOW - datetime.timedelta(minutes=10)
#: 30 秒前刚心跳：稳稳在阈值内。
FRESH = NOW - datetime.timedelta(seconds=30)


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
    monkeypatch.setattr(device_jobs, "session_factory", factory)
    return factory


@dataclass(frozen=True)
class Seed:
    """一套最小可派单现场：房间 + 长者 + 主责护工 + 管理员 + 已绑定设备。"""

    room_id: uuid.UUID
    elder_id: uuid.UUID
    device_id: uuid.UUID
    care_worker_id: uuid.UUID
    admin_id: uuid.UUID


async def _seed(
    db: AsyncSession,
    *,
    elder_status: str = "active",
    device_status: str = "online",
    last_heartbeat: datetime.datetime | None = STALE,
    bound: bool = True,
    with_care_worker: bool = True,
) -> Seed:
    room_id = uuid.uuid4()
    elder_id = uuid.uuid4()
    device_id = uuid.uuid4()
    care_worker_id = uuid.uuid4()
    admin_id = uuid.uuid4()

    await db.execute(
        Room.__table__.insert().values(room_id=room_id, room_no=f"DV-{room_id.hex[:6]}")
    )
    await db.execute(
        Elder.__table__.insert().values(
            elder_id=elder_id, name=f"掉线测试长者{uuid.uuid4().hex[:4]}", status=elder_status
        )
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": care_worker_id,
                "name": "李护工",
                "role": "care_worker",
                "password_hash": "x",
            },
            {"staff_id": admin_id, "name": "王管理", "role": "admin", "password_hash": "x"},
        ],
    )
    if with_care_worker:
        await db.execute(
            ElderStaffAssignment.__table__.insert().values(
                elder_id=elder_id, staff_id=care_worker_id, relation="primary_care"
            )
        )
    await db.execute(
        Device.__table__.insert().values(
            device_id=device_id,
            room_id=room_id,
            elder_id=elder_id if bound else None,
            token_hash=f"hash-{device_id.hex}",
            status=device_status,
            last_heartbeat=last_heartbeat,
        )
    )
    await db.flush()
    return Seed(
        room_id=room_id,
        elder_id=elder_id,
        device_id=device_id,
        care_worker_id=care_worker_id,
        admin_id=admin_id,
    )


async def _orders(db: AsyncSession, elder_id: uuid.UUID) -> list[WorkOrder]:
    db.expire_all()
    rows = await db.execute(
        select(WorkOrder)
        .where(WorkOrder.elder_id == elder_id, WorkOrder.type == device_jobs.OFFLINE_ORDER_TYPE)
        .order_by(WorkOrder.created_at, WorkOrder.order_no)
    )
    return list(rows.scalars().all())


async def _notifications(db: AsyncSession, elder_id: uuid.UUID) -> list[Notification]:
    db.expire_all()
    rows = await db.execute(
        select(Notification)
        .where(
            Notification.elder_id == elder_id,
            Notification.kind == device_jobs.OFFLINE_NOTIFICATION_KIND,
        )
        .order_by(Notification.created_at)
    )
    return list(rows.scalars().all())


# ----------------------------------------------------------------- 注册与任务体


async def test_build_scheduler_registers_device_offline_job() -> None:
    """装配根必须挂上掉线扫描：没有这条 job，「心跳超时判掉线」在生产上不会发生。"""
    scheduler = build_scheduler()
    job = scheduler.get_job(f"{JOB_PREFIX}:{device_jobs.OFFLINE_JOB_NAME}")
    assert job is not None, "build_scheduler 必须注册设备掉线扫描"
    assert job.func is device_jobs.scan_offline_devices
    assert job.trigger.interval.total_seconds() == device_jobs.OFFLINE_SCAN_INTERVAL_SECONDS
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time == 300


async def test_register_device_jobs_uses_the_shared_prefix() -> None:
    """单独的注册函数也要走统一 job id 前缀与 interval（装配根之外的入口同样受管）。"""
    scheduler = AsyncIOScheduler()
    device_jobs.register_device_jobs(scheduler, interval_seconds=30)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == f"{JOB_PREFIX}:{device_jobs.OFFLINE_JOB_NAME}"
    assert jobs[0].trigger.interval.total_seconds() == 30


async def test_scan_dispatches_device_fault_and_notifies_worker_and_admin(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """心跳过期 + 在住 + 未派单 → 一张 `device_fault` 黄单，护工与管理员各一条通知。"""
    seed = await _seed(db)

    created = await device_jobs.scan_offline_devices(now=NOW)

    assert created == 1
    orders = await _orders(db, seed.elder_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.type == "device_fault"
    assert order.severity == "yellow"
    assert order.status == "new"
    assert order.assignee_id == seed.care_worker_id
    assert str(seed.device_id) in order.note
    assert "10 分钟" in order.note
    order_no = order.order_no

    notifications = await _notifications(db, seed.elder_id)
    # 护工一条 + 管理员一条：PRD 的「通知管理员与护工」是两类待办，不能只发一边。
    assert {item.assignee_staff_id for item in notifications} == {
        seed.care_worker_id,
        seed.admin_id,
    }
    for item in notifications:
        assert item.severity == "yellow"
        assert item.payload["module"] == "devices"
        assert item.payload["state"] == "offline"
        assert item.payload["device_id"] == str(seed.device_id)
        assert item.payload["room_id"] == str(seed.room_id)
        assert item.payload["order_no"] == order_no
        assert item.payload["offline_minutes"] == 10


async def test_scan_is_idempotent_while_the_order_is_open(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """同一场掉线连扫两轮：第二轮回 0，不刷第二张单、也不刷通知。"""
    seed = await _seed(db)

    assert await device_jobs.scan_offline_devices(now=NOW) == 1
    assert await device_jobs.scan_offline_devices(now=NOW + datetime.timedelta(minutes=1)) == 0

    assert len(await _orders(db, seed.elder_id)) == 1
    assert len(await _notifications(db, seed.elder_id)) == 2


async def test_scan_dispatches_again_after_the_order_is_closed(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """关单后设备仍无心跳 = 新的一次掉线，必须重新派单（去重键是「未关单」而非「派过」）。"""
    seed = await _seed(db)
    assert await device_jobs.scan_offline_devices(now=NOW) == 1
    orders = await _orders(db, seed.elder_id)
    orders[0].status = "closed"
    await db.commit()

    assert await device_jobs.scan_offline_devices(now=NOW + datetime.timedelta(minutes=1)) == 1
    assert len(await _orders(db, seed.elder_id)) == 2


async def test_scan_skips_elder_away(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """长者 away（外出/转区）：人不在这个房间，设备掉线不派单、也不留半截通知。"""
    seed = await _seed(db, elder_status="away")

    assert await device_jobs.scan_offline_devices(now=NOW) == 0
    assert await _orders(db, seed.elder_id) == []
    assert await _notifications(db, seed.elder_id) == []


async def test_scan_skips_locked_fresh_and_unbound_devices(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """三种不该派单的设备各一台：主动锁定、心跳还新鲜、未绑定长者（建不出单）。"""
    locked = await _seed(db, device_status="locked")
    fresh = await _seed(db, last_heartbeat=FRESH)
    unbound = await _seed(db, bound=False)
    never = await _seed(db, last_heartbeat=None)

    assert await device_jobs.scan_offline_devices(now=NOW) == 0
    for seed in (locked, fresh, unbound, never):
        assert await _orders(db, seed.elder_id) == []


async def test_scan_without_primary_care_still_dispatches_to_the_admin(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """没有主责护工时单照建（留在未指派队列），通知只剩管理员那条——不伪造指派。"""
    seed = await _seed(db, with_care_worker=False)

    assert await device_jobs.scan_offline_devices(now=NOW) == 1
    orders = await _orders(db, seed.elder_id)
    assert len(orders) == 1
    assert orders[0].assignee_id is None
    notifications = await _notifications(db, seed.elder_id)
    assert [item.assignee_staff_id for item in notifications] == [seed.admin_id]
