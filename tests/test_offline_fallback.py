"""FR-MED-08 断网三层兜底 · 服务端段（PRD:338 第②③层 + `offline` 归因）。

PRD 原文（`docs/安语3D数字人_PRD.md:338`）：「② 服务端恢复后窗口内补呼、超窗直接生成
工单；③ 掉线与用药窗口重叠立即通知护工；`notification_failed` 增加
`failure_reason=offline` 归因（区分设备故障与拒服）」。

端侧（IndexedDB + Service Worker + `speechSynthesis`）在 `frontend/public/sw.js` 与
`frontend/src/pad/offline/`，靠浏览器手测验收，不在本文件覆盖范围。这里只盯服务端：

1. 窗口判定的**纯函数**：窗口内只补一枪、超窗建单、最后一枪 60s 未等满要 `wait`；
2. `recover_after_offline` 的窗口内分支**不建单也不写假回执**（PRD:324 的回执口径）；
3. 超窗分支落三件事：红线工单 + `terminal_state='notification_failed'` +
   `failure_reason='offline'` + 主责护工红档通知；
4. 同一次提醒重复上报（平板抖动）不重复建单、不重复建通知（幂等）；
5. `notify_offline_overlap` 只在窗口内发通知，窗口过了交给第②层归因。

隔离方式与 `tests/test_medication_plan_edit.py` 同款：真库 + `NullPool` 单连接 +
外层事务，测试结束整体回滚，不留残留（**不要**跑全量 pytest，见 AGENTS §9 第三条）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import (
    Elder,
    ElderStaffAssignment,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    Notification,
    StaffAccount,
    WorkOrder,
)
from app.services import medication_service, notification_service

NOW = datetime.datetime(2026, 11, 9, 8, 0, 0, tzinfo=datetime.UTC)
"""固定「现在」，所有窗口判定都以它为基准（UTC 存储，AGENTS §8）。"""


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    connection = await require_db_connection(db_engine)
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@dataclass
class Seed:
    elder_id: uuid.UUID
    care_worker_id: uuid.UUID
    plan_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    doctor_id, care_worker_id = uuid.uuid4(), uuid.uuid4()
    elder_id = uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": doctor_id, "name": "王医生", "role": "doctor", "password_hash": "x"},
            {
                "staff_id": care_worker_id,
                "name": "李护工",
                "role": "care_worker",
                "password_hash": "x",
            },
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": "张桂芬", "status": "active"}],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"},
            {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
        ],
    )
    plan_id = uuid.uuid4()
    await db.execute(
        MedicationPlan.__table__.insert(),
        [
            {
                "plan_id": plan_id,
                "elder_id": elder_id,
                "drug_name": "苯磺酸氨氯地平",
                "dose": "5mg",
                "schedule_cron": "08:00",
                "start_date": NOW.date(),
                "created_by": doctor_id,
            }
        ],
    )
    await db.flush()
    return Seed(elder_id=elder_id, care_worker_id=care_worker_id, plan_id=plan_id)


@dataclass
class EventSeed:
    reminder_id: uuid.UUID
    scheduled_at: datetime.datetime
    attempts: list[datetime.datetime] = field(default_factory=list)


async def _make_event(
    db: AsyncSession,
    seed: Seed,
    *,
    scheduled_at: datetime.datetime,
    attempts: list[datetime.datetime] | None = None,
) -> EventSeed:
    """建一条用药事件（复用服务层唯一写库函数），可选补若干次 `receipt='none'` 触达。"""
    event, _created = await medication_service.ensure_event(
        db,
        plan_id=seed.plan_id,
        elder_id=seed.elder_id,
        scheduled_at=scheduled_at,
        drug_snapshot={"drug_name": "苯磺酸氨氯地平", "dose": "5mg"},
        triggered_by="scheduler",
    )
    seeded = EventSeed(reminder_id=event.reminder_id, scheduled_at=scheduled_at)
    for index, sent_at in enumerate(attempts or [], start=1):
        await medication_service.record_attempt(
            db,
            event_id=event.reminder_id,
            attempt_no=index,
            sent_at=sent_at,
            receipt="none",
        )
        seeded.attempts.append(sent_at)
    await db.flush()
    return seeded


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


async def _work_orders(db: AsyncSession, elder_id: uuid.UUID) -> list[WorkOrder]:
    return list(
        (await db.execute(select(WorkOrder).where(WorkOrder.elder_id == elder_id))).scalars().all()
    )


async def _notifications(db: AsyncSession, elder_id: uuid.UUID) -> list[Notification]:
    return list(
        (await db.execute(select(Notification).where(Notification.elder_id == elder_id)))
        .scalars()
        .all()
    )


# --------------------------------------------------- ①窗口判定（纯函数，无库）


def test_window_open_allows_exactly_one_callback() -> None:
    """T0 后 3 分钟重连：窗口还开着 → 补第 2 枪（第 1 枪已发过）。"""
    plan = notification_service.decide_offline_recovery(
        scheduled_at=NOW,
        attempts_sent=1,
        now=NOW + datetime.timedelta(minutes=3),
        attempts=4,
        wait_seconds=60,
    )

    assert plan.action == "callback"
    assert plan.reason == "window_open"
    assert plan.attempt_no == 2
    assert plan.window_end == NOW + datetime.timedelta(minutes=15, seconds=60)


def test_after_window_missed_goes_straight_to_work_order() -> None:
    """窗口关闭（T+15min+60s 之后）→ 建单，不补发晚到的提醒（PRD:326）。"""
    plan = notification_service.decide_offline_recovery(
        scheduled_at=NOW,
        attempts_sent=1,
        now=NOW + datetime.timedelta(minutes=16),
        attempts=4,
        wait_seconds=60,
    )

    assert plan.action == "work_order"
    assert plan.reason == "window_missed"
    assert plan.attempt_no is None


def test_last_attempt_still_waiting_does_not_escalate_early() -> None:
    """四次都发过但最后一枪的 60s 还没等满 → `wait`，不许抢跑升级（PRD §4.5）。"""
    pending = notification_service.decide_offline_recovery(
        scheduled_at=NOW,
        attempts_sent=4,
        last_sent_at=NOW + datetime.timedelta(minutes=15),
        now=NOW + datetime.timedelta(minutes=15, seconds=10),
        attempts=4,
        wait_seconds=60,
    )
    assert pending.action == "wait"
    assert pending.reason == "last_attempt_pending"

    expired = notification_service.decide_offline_recovery(
        scheduled_at=NOW,
        attempts_sent=4,
        last_sent_at=NOW + datetime.timedelta(minutes=15),
        now=NOW + datetime.timedelta(minutes=15, seconds=61),
        attempts=4,
        wait_seconds=60,
    )
    assert expired.action == "work_order"
    assert expired.reason == "attempts_exhausted"


# --------------------------------------------------------- ②窗口内补呼（落库）


async def test_recover_inside_window_reports_callback_without_writing(
    db: AsyncSession, seeded: Seed
) -> None:
    """窗口内：只回判决，不建单、不写假回执、不动事件终态（PRD:324）。"""
    event = await _make_event(
        db,
        seeded,
        scheduled_at=NOW,
        attempts=[NOW],
    )

    outcome = await notification_service.recover_after_offline(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=2)
    )

    assert outcome.action == "callback"
    assert outcome.attempt_no == 2
    assert outcome.work_order_no is None
    assert await _work_orders(db, seeded.elder_id) == []
    assert await _notifications(db, seeded.elder_id) == []

    stored = await medication_service.load_event(db, event.reminder_id)
    assert stored.status != "escalated"
    assert stored.terminal_state is None
    assert stored.failure_reason is None


# ------------------------------------------------ ②超窗建单 + offline 归因（落库）


async def test_recover_after_window_builds_work_order_and_marks_offline(
    db: AsyncSession, seeded: Seed
) -> None:
    """超窗：红线工单 + `notification_failed` + `failure_reason='offline'` + 护工通知。"""
    event = await _make_event(db, seeded, scheduled_at=NOW)

    outcome = await notification_service.recover_after_offline(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=20)
    )

    assert outcome.action == "work_order"
    assert outcome.reason == "window_missed"
    assert outcome.work_order_no is not None

    orders = await _work_orders(db, seeded.elder_id)
    assert len(orders) == 1
    assert orders[0].order_no == outcome.work_order_no
    assert orders[0].type == "missed_medication"
    assert orders[0].severity == "red"

    stored = await medication_service.load_event(db, event.reminder_id)
    assert stored.status == "escalated"
    assert stored.terminal_state == "notification_failed"
    assert stored.failure_reason == "offline"

    notifications = await _notifications(db, seeded.elder_id)
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.notification_id == outcome.notification_id
    assert notification.kind == "medication_escalation"
    assert notification.severity == "red"
    assert notification.assignee_staff_id == seeded.care_worker_id
    assert notification.payload is not None
    assert notification.payload["failure_reason"] == "offline"
    assert notification.payload["state"] == "notification_failed"
    assert notification.payload["reminder_id"] == str(event.reminder_id)
    assert notification.payload["work_order_no"] == outcome.work_order_no


async def test_recover_after_four_attempts_marks_offline_not_refusal(
    db: AsyncSession, seeded: Seed
) -> None:
    """四次触达全 `receipt='none'`（设备没播出来）→ `offline`，不是拒服也不是知悉未确认。"""
    attempts = [NOW + datetime.timedelta(minutes=5 * index) for index in range(4)]
    event = await _make_event(db, seeded, scheduled_at=NOW, attempts=attempts)

    outcome = await notification_service.recover_after_offline(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=16)
    )

    assert outcome.action == "work_order"
    assert outcome.reason == "attempts_exhausted"

    stored = await medication_service.load_event(db, event.reminder_id)
    assert stored.terminal_state == "notification_failed"
    assert stored.failure_reason == "offline"
    assert stored.terminal_state != "missed_unconfirmed"

    attempts_left = (
        await db.execute(
            select(func.count())
            .select_from(MedicationAttempt)
            .where(MedicationAttempt.event_id == event.reminder_id)
        )
    ).scalar_one()
    assert attempts_left == 4


async def test_recover_is_idempotent_when_pad_reports_twice(db: AsyncSession, seeded: Seed) -> None:
    """平板抖动重复上报：第二次直接 `already_closed`，不多建一张工单。"""
    event = await _make_event(db, seeded, scheduled_at=NOW)

    first = await notification_service.recover_after_offline(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=20)
    )
    second = await notification_service.recover_after_offline(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=21)
    )

    assert first.action == "work_order"
    assert second.action == "already_closed"
    assert len(await _work_orders(db, seeded.elder_id)) == 1
    assert len(await _notifications(db, seeded.elder_id)) == 1


# ------------------------------------------------------------ ③掉线通知护工


async def test_offline_overlap_notifies_primary_care_once(db: AsyncSession, seeded: Seed) -> None:
    """掉线与窗口重叠 → 立刻一条红档通知，给主责护工，重复上报只建一条。"""
    event = await _make_event(db, seeded, scheduled_at=NOW)

    first = await notification_service.notify_offline_overlap(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=1)
    )
    second = await notification_service.notify_offline_overlap(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=2)
    )

    assert first is not None
    assert second is not None
    assert first.notification_id == second.notification_id
    assert first.kind == "medication_escalation"
    assert first.severity == "red"
    assert first.assignee_staff_id == seeded.care_worker_id
    assert first.payload is not None
    assert first.payload["state"] == "offline_overlap"
    assert first.payload["failure_reason"] == "offline"
    assert len(await _notifications(db, seeded.elder_id)) == 1
    # 第③层只通知，不建单（建单是第②层的超窗分支）
    assert await _work_orders(db, seeded.elder_id) == []


async def test_offline_overlap_skips_once_window_is_missed(db: AsyncSession, seeded: Seed) -> None:
    """窗口已过：不在这里发通知，交给第②层的超窗建单归因。"""
    event = await _make_event(db, seeded, scheduled_at=NOW)

    result = await notification_service.notify_offline_overlap(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=30)
    )

    assert result is None
    assert await _notifications(db, seeded.elder_id) == []


async def test_offline_overlap_skips_closed_event(db: AsyncSession, seeded: Seed) -> None:
    """事件已确认闭环：掉线通知没有意义，直接跳过。"""
    event = await _make_event(db, seeded, scheduled_at=NOW)
    await medication_service.update_event_state(
        db, reminder_id=event.reminder_id, status="confirmed", terminal_state="taken_reported"
    )

    result = await notification_service.notify_offline_overlap(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=1)
    )

    assert result is None
    assert await _notifications(db, seeded.elder_id) == []


async def test_event_row_is_the_same_row_after_recovery(db: AsyncSession, seeded: Seed) -> None:
    """不新增事件：超窗处置后 `medication_events` 仍只有原一行（无新表、无新事件态）。"""
    event = await _make_event(db, seeded, scheduled_at=NOW)

    await notification_service.recover_after_offline(
        db, reminder_id=event.reminder_id, now=NOW + datetime.timedelta(minutes=20)
    )

    total = (
        await db.execute(
            select(func.count())
            .select_from(MedicationEvent)
            .where(MedicationEvent.plan_id == seeded.plan_id)
        )
    ).scalar_one()
    assert total == 1
