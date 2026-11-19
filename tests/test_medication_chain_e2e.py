"""用药触达链路的端到端验收（迭代 10 · task-6 / PRD FR-MED-06、AC-06、AC-20）。

本文件测「回执 → 终态归因」这条**真库**链路，与既有用例的分工：

| 文件 | 管什么 | 本文件不重测 |
| --- | --- | --- |
| `tests/test_scheduler_medication.py` | 触达节拍、`decide_event_action` 边界 | 节拍换算（T0/+5/+10/+15 的间隔） |
| `tests/test_medication_scheduler_wiring.py` | 通道注入、`partial` 绑定、`escalation_attribution` 纯函数 | 纯函数取值表（那边已参数化） |
| `tests/test_medication_reminder_e2e.py` | 真提交 + 逆向清理的导入/四枪/工单/通知 | 清理手法（本文件用外层事务，主库零残留） |

三份既有用例都没断言的一层，正是本文件的验收点：

1. 四次触达的回执**逐条**落进 `medication_attempts`（含 `tts_result` 的派生口径）；
2. 升级时终态**按这些回执**写回 `medication_events`（不是拍脑袋常量）；
3. 装配根 `build_scheduler(medication_deliver=...)` 注入的通道，能一路把回执写进库；
4. 同一事件被重复驱动不会多建事件/风险事件/工单/通知（幂等）；
5. 升级产物是「风险事件 + 工单 + 通知」三件套（PRD:312 / AC-06），
   工单经 `work_orders.event_id` 挂到风险事件上——漏服升级不是只派单不留痕。

四条业务链路（逐条对应 task-6 的验收项）：

| # | 场景 | 假通道回执 | 期望终态 |
| --- | --- | --- | --- |
| 1 | 四次触达零回执 | `"none"` ×4 | `notification_failed` / `rtc_failed`（PRD:324） |
| 2 | 四次触达有回执、无人确认 | `"card_rendered"` ×4 | `missed_unconfirmed` / `no_answer`（PRD:323） |
| 3 | 整窗错过、一枪未打 | 无出站 | `notification_failed` / `rtc_failed`（空回执集） |
| 4 | 装配根注入后驱动到终态 | `"none"` ×4 | `notification_failed` / `rtc_failed` |
| 5 | 升级后重复驱动 | `"tts_finished"` ×4 | 事件/风险事件/工单/通知各仍 1 条 |

**纪律**：不 `start()` APScheduler（真起时钟就是对墙上时间的竞态），只 `build_scheduler()`
取 job 定义、直接 `await job.func(...)`；不 `sleep`，时间一律经 `now=` 注入；不
monkeypatch 被测函数——只换 `medication_jobs.db_session_factory`（隔离基建）与
`app.scheduler.get_settings`（装配根的配置入参）；不用 `tmp_path`（本沙箱建不了目录）。
"""

from __future__ import annotations

import datetime
import functools
import uuid
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

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
    Elder,
    ElderStaffAssignment,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    Notification,
    RiskEvent,
    StaffAccount,
    WorkOrder,
)
from app.scheduler import build_scheduler
from app.scheduler import medication as medication_jobs
from app.services import medication_service

SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 夹具（与 `tests/test_medication_scheduler_wiring.py` 同款隔离手法）
# ---------------------------------------------------------------------------


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
    """把调度模块的 `db_session_factory` 换进外层事务里的工厂。

    任务体自己会 `await session.commit()`；`create_savepoint` 让这个 commit 只释放
    savepoint，所以外层 rollback 依然能把整场测试抹干净。
    """
    factory = async_sessionmaker(
        bind=db.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(medication_jobs, "db_session_factory", factory)
    yield factory


# ---------------------------------------------------------------------------
# 假通道 + 种子数据
# ---------------------------------------------------------------------------


@dataclass
class _RecordingDeliver:
    """假出站通道：只记目标与固定回执，不碰网络/TTS（不伪造真实回执，值是闭集枚举）。"""

    receipt: str = "tts_finished"
    targets: list[medication_jobs.MedicationTouchTarget] = field(default_factory=list)

    async def __call__(self, target: medication_jobs.MedicationTouchTarget) -> str | None:
        self.targets.append(target)
        return self.receipt


@dataclass
class Seed:
    elder_id: uuid.UUID
    care_worker_id: uuid.UUID
    business_day: datetime.date
    plan_id: uuid.UUID


async def _seed_due_plan(db: AsyncSession, *, clock: str = "08:00") -> Seed:
    """一位长者 + 一名医生 + 一名主责护工 + 一张 `clock` 的药单（只造链路要的行）。"""
    elder_id, doctor_id, care_worker_id, plan_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    day = medication_jobs._business_date(datetime.datetime.now(datetime.UTC))

    await db.execute(
        StaffAccount.__table__.insert(),
        {"staff_id": doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        {"staff_id": care_worker_id, "name": "护工王", "role": "care_worker", "password_hash": "x"},
    )
    await db.execute(Elder.__table__.insert(), {"elder_id": elder_id, "name": "张桂芬"})
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
    )
    await db.execute(
        MedicationPlan.__table__.insert(),
        {
            "plan_id": plan_id,
            "elder_id": elder_id,
            "drug_name": "氨氯地平",
            "dose": "1 片",
            "schedule_cron": clock,
            "start_date": day,
            "end_date": None,
            "version": 1,
            "created_by": doctor_id,
            "status": "active",
        },
    )
    return Seed(
        elder_id=elder_id,
        care_worker_id=care_worker_id,
        business_day=day,
        plan_id=plan_id,
    )


def _at(seed: Seed, hour: int, minute: int) -> datetime.datetime:
    """业务自然日的 `HH:MM`（Asia/Shanghai）→ UTC 时刻。"""
    return medication_jobs.scheduled_at_for(seed.business_day, hour, minute, SHANGHAI)


# ---------------------------------------------------------------------------
# 只读断言视图：一律列级 select，不把 ORM 实例带出任务体改过的 session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventRow:
    reminder_id: uuid.UUID
    elder_id: uuid.UUID
    scheduled_at: datetime.datetime
    status: str
    terminal_state: str | None
    failure_reason: str | None
    retry_count: int


@dataclass(frozen=True)
class AttemptRow:
    attempt_no: int
    sent_at: datetime.datetime
    receipt: str | None
    tts_result: str | None
    response: str | None


@dataclass(frozen=True)
class OrderRow:
    order_no: str
    type: str
    severity: str
    status: str
    elder_id: uuid.UUID
    event_id: uuid.UUID | None
    note: str | None


@dataclass(frozen=True)
class RiskEventRow:
    event_id: uuid.UUID
    elder_id: uuid.UUID
    type: str
    severity: str
    status: str
    source_type: str | None
    source_id: uuid.UUID | None


@dataclass(frozen=True)
class NotificationRow:
    notification_id: uuid.UUID
    kind: str
    severity: str
    elder_id: uuid.UUID | None
    assignee_staff_id: uuid.UUID | None
    payload: dict[str, Any]


async def _events(db: AsyncSession, plan_id: uuid.UUID) -> list[EventRow]:
    """同一药单的全部到点事件（按 `scheduled_at` 升序）。"""
    rows = (
        await db.execute(
            select(
                MedicationEvent.reminder_id,
                MedicationEvent.elder_id,
                MedicationEvent.scheduled_at,
                MedicationEvent.status,
                MedicationEvent.terminal_state,
                MedicationEvent.failure_reason,
                MedicationEvent.retry_count,
            )
            .where(MedicationEvent.plan_id == plan_id)
            .order_by(MedicationEvent.scheduled_at)
        )
    ).all()
    return [EventRow(*row) for row in rows]


async def _attempts(db: AsyncSession, reminder_id: uuid.UUID) -> list[AttemptRow]:
    """一次提醒的全部触达记录（按 attempt_no 升序，即出站顺序）。"""
    rows = (
        await db.execute(
            select(
                MedicationAttempt.attempt_no,
                MedicationAttempt.sent_at,
                MedicationAttempt.receipt,
                MedicationAttempt.tts_result,
                MedicationAttempt.response,
            )
            .where(MedicationAttempt.event_id == reminder_id)
            .order_by(MedicationAttempt.attempt_no)
        )
    ).all()
    return [AttemptRow(*row) for row in rows]


async def _orders(db: AsyncSession, elder_id: uuid.UUID) -> list[OrderRow]:
    rows = (
        await db.execute(
            select(
                WorkOrder.order_no,
                WorkOrder.type,
                WorkOrder.severity,
                WorkOrder.status,
                WorkOrder.elder_id,
                WorkOrder.event_id,
                WorkOrder.note,
            )
            .where(WorkOrder.elder_id == elder_id)
            .order_by(WorkOrder.order_no)
        )
    ).all()
    return [OrderRow(*row) for row in rows]


async def _risk_events(db: AsyncSession, reminder_id: uuid.UUID) -> list[RiskEventRow]:
    """某次提醒派生出的风险事件（按 `created_at` 升序，正常只该有一条）。

    `source_id` 是软引用（不建外键），所以同时按 `source_type` 收窄，
    避免撞上别的链路恰好用了同一个 UUID 作 source_id。
    """
    rows = (
        await db.execute(
            select(
                RiskEvent.event_id,
                RiskEvent.elder_id,
                RiskEvent.type,
                RiskEvent.severity,
                RiskEvent.status,
                RiskEvent.source_type,
                RiskEvent.source_id,
            )
            .where(
                RiskEvent.source_type == "medication_events",
                RiskEvent.source_id == reminder_id,
            )
            .order_by(RiskEvent.created_at)
        )
    ).all()
    return [RiskEventRow(*row) for row in rows]


async def _notifications(db: AsyncSession, elder_id: uuid.UUID) -> list[NotificationRow]:
    rows = (
        await db.execute(
            select(
                Notification.notification_id,
                Notification.kind,
                Notification.severity,
                Notification.elder_id,
                Notification.assignee_staff_id,
                Notification.payload,
            )
            .where(Notification.elder_id == elder_id)
            .order_by(Notification.created_at)
        )
    ).all()
    return [NotificationRow(*row) for row in rows if row.payload is not None]


# ---------------------------------------------------------------------------
# 共用驱动：打开一次到点事件 / 打满四枪 / 断言升级后的可观测事实
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenedEvent:
    seed: Seed
    reminder_id: uuid.UUID
    t0: datetime.datetime


async def _open_event(db: AsyncSession, *, clock: str = "08:00") -> OpenedEvent:
    """造一张当天的药单，跑一轮预生成扫描，拿到本次到点事件。"""
    seed = await _seed_due_plan(db, clock=clock)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) == 1

    events = await _events(db, seed.plan_id)
    assert len(events) == 1, "预生成一轮只该建出本次到点事件"
    return OpenedEvent(seed=seed, reminder_id=events[0].reminder_id, t0=t0)


async def _drive_four_touches(deliver: _RecordingDeliver, t0: datetime.datetime) -> list[int]:
    """T0/T+5/T+10/T+15 各驱动一轮 due-scan，返回每轮 handled 数。"""
    handled: list[int] = []
    for minutes in (0, 5, 10, 15):
        handled.append(
            await medication_jobs.run_medication_due_scan(
                now=t0 + datetime.timedelta(minutes=minutes), deliver=deliver
            )
        )
    return handled


async def _assert_single_escalation_artifacts(
    db: AsyncSession,
    opened: OpenedEvent,
    *,
    terminal_state: str,
    failure_reason: str,
) -> tuple[EventRow, OrderRow, NotificationRow]:
    """升级后的可观测事实：唯一事件 / 唯一风险事件 / 唯一红单 / 唯一通知。"""
    events = await _events(db, opened.seed.plan_id)
    assert len(events) == 1, "同一 (plan, scheduled_at) 只该有一条用药事件"
    event = events[0]
    assert event.reminder_id == opened.reminder_id
    assert event.status == "escalated"
    assert event.terminal_state == terminal_state
    assert event.failure_reason == failure_reason
    assert event.scheduled_at == opened.t0

    # PRD:312 / AC-06：漏服升级必须「生成风险事件和工单」，与拒服路径同形态。
    risks = await _risk_events(db, opened.reminder_id)
    assert len(risks) == 1, "一次漏服升级只该落一条风险事件"
    risk = risks[0]
    assert risk.type == medication_service.ESCALATION_RISK_TYPE
    assert risk.severity == "red", "漏服升级是红线，不是拒服那种当班跟进（yellow）"
    assert risk.status == "open", "风险事件停在待处置，派单状态写工单那一侧"
    assert risk.elder_id == opened.seed.elder_id
    assert risk.source_type == "medication_events"
    assert risk.source_id == opened.reminder_id

    orders = await _orders(db, opened.seed.elder_id)
    assert len(orders) == 1, "一次漏服升级只该派一张工单"
    order = orders[0]
    assert order.type == medication_service.ESCALATION_REASON
    assert order.severity == "red"
    assert order.status == "new", "红线单必须停在照护台待接手，不许自造成已接手"
    assert order.elder_id == opened.seed.elder_id
    # 工单必须挂到刚建的风险事件上，否则照护台点开工单看不到「发生了什么」。
    assert order.event_id == risk.event_id
    assert order.note is not None
    assert str(opened.reminder_id) in order.note
    assert str(opened.seed.plan_id) in order.note

    notifications = await _notifications(db, opened.seed.elder_id)
    assert len(notifications) == 1, "一次漏服升级只该发一条通知"
    notification = notifications[0]
    assert notification.kind == "medication_escalation"
    assert notification.severity == "red"
    assert notification.assignee_staff_id == opened.seed.care_worker_id
    payload = notification.payload
    assert payload["module"] == "medication"
    assert payload["state"] == "escalated"
    assert payload["reminder_id"] == str(opened.reminder_id)
    assert payload["work_order_no"] == order.order_no
    assert datetime.datetime.fromisoformat(payload["scheduled_at"]) == opened.t0
    assert "四次触达" in payload["summary"]
    return event, order, notification


# ---------------------------------------------------------------------------
# 验收 1：四次触达零回执 → notification_failed / rtc_failed（PRD:324）
# ---------------------------------------------------------------------------


async def test_zero_receipt_chain_lands_notification_failed(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """假通道恒返 `"none"`：四枪全记 none，升级落「没送到」而不是「人了没应」。"""
    opened = await _open_event(db)
    deliver = _RecordingDeliver(receipt="none")

    assert await _drive_four_touches(deliver, opened.t0) == [1, 1, 1, 1]
    assert [target.attempt_no for target in deliver.targets] == [1, 2, 3, 4]
    assert {target.reminder_id for target in deliver.targets} == {opened.reminder_id}

    attempts = await _attempts(db, opened.reminder_id)
    assert [row.attempt_no for row in attempts] == [1, 2, 3, 4]
    assert [row.sent_at for row in attempts] == [
        opened.t0 + datetime.timedelta(minutes=minutes) for minutes in (0, 5, 10, 15)
    ]
    assert [row.receipt for row in attempts] == ["none"] * 4
    # 派生的 TTS 结果：零回执既不是 ok 也不是 tts_failed，只能是 NULL。
    assert [row.tts_result for row in attempts] == [None] * 4
    assert [row.response for row in attempts] == [None] * 4

    before = (await _events(db, opened.seed.plan_id))[0]
    assert before.status == "waiting_confirm"
    assert before.retry_count == 3
    assert before.terminal_state is None
    assert before.failure_reason is None

    assert (
        await medication_jobs.run_medication_due_scan(
            now=opened.t0 + datetime.timedelta(minutes=16), deliver=deliver
        )
        == 1
    )
    assert len(deliver.targets) == 4, "升级那一轮不该再出站一次"

    _, order, _ = await _assert_single_escalation_artifacts(
        db, opened, terminal_state="notification_failed", failure_reason="rtc_failed"
    )
    assert order.order_no.startswith("WO-")


# ---------------------------------------------------------------------------
# 验收 2：有回执、无人确认 → missed_unconfirmed / no_answer（PRD:323）
# ---------------------------------------------------------------------------


async def test_receipted_chain_lands_missed_unconfirmed(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """卡片渲染成功（`card_rendered`）：回执是「已送达」，终态才是「未确认」。"""
    opened = await _open_event(db)
    deliver = _RecordingDeliver(receipt="card_rendered")

    assert await _drive_four_touches(deliver, opened.t0) == [1, 1, 1, 1]

    attempts = await _attempts(db, opened.reminder_id)
    assert [row.receipt for row in attempts] == ["card_rendered"] * 4
    # PRD:305-339 的派生口径：卡片渲染成功但 TTS 没播完，记 tts_failed（不是没送到）。
    assert [row.tts_result for row in attempts] == ["tts_failed"] * 4

    assert (
        await medication_jobs.run_medication_due_scan(
            now=opened.t0 + datetime.timedelta(minutes=16), deliver=deliver
        )
        == 1
    )
    await _assert_single_escalation_artifacts(
        db, opened, terminal_state="missed_unconfirmed", failure_reason="no_answer"
    )


# ---------------------------------------------------------------------------
# 验收 3：整窗错过、一枪未打 → notification_failed / rtc_failed
# ---------------------------------------------------------------------------


async def test_window_missed_without_any_touch_lands_notification_failed(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """调度器停机错过整窗：零触达 = 零回执 = 没送到，不得落 `missed_unconfirmed`。

    PRD:323 的 `missed_unconfirmed` 前提是「至少一次获得页面回执」；一枪都没打出去
    还写「疑似漏服未确认」，等于把系统没送到记成长者没回应。
    """
    opened = await _open_event(db)
    deliver = _RecordingDeliver(receipt="card_rendered")

    assert (
        await medication_jobs.run_medication_due_scan(
            now=opened.t0 + datetime.timedelta(minutes=60), deliver=deliver
        )
        == 1
    )
    assert deliver.targets == [], "窗口已过，不该补打迟到的一枪"

    assert await _attempts(db, opened.reminder_id) == []
    _, order, _ = await _assert_single_escalation_artifacts(
        db, opened, terminal_state="notification_failed", failure_reason="rtc_failed"
    )
    assert "四次触达" in (order.note or ""), "工单文案是固定口径，不随实际触达次数改写"


# ---------------------------------------------------------------------------
# 验收 4：装配根注入的通道真把回执写进库（build_scheduler → job.func）
# ---------------------------------------------------------------------------


@dataclass
class _SettingsStub:
    """只喂 `build_scheduler()` 真正读的字段，避免测试依赖 `.env` 的实际取值。"""

    scheduler_enabled: bool
    scheduler_timezone: str = "Asia/Shanghai"
    health_checkup_start: str = "19:00"
    health_checkup_end: str = "20:00"
    daily_report_generate_hour: str = "07:00"


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    import app.scheduler as scheduler_pkg

    stub = _SettingsStub(scheduler_enabled=enabled)
    monkeypatch.setattr(scheduler_pkg, "get_settings", lambda: stub)


async def test_build_scheduler_deliver_reaches_chain_terminal_state(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从装配根 `build_scheduler(medication_deliver=...)` 一路驱动到终态。

    先钉「job 上绑的是同一个对象」，再用这个 job 跑完整条链路：库里的回执与终态
    就是这条通道的实际产物，而不是测试另起的旁路。
    """
    _patch_settings(monkeypatch, enabled=True)
    deliver = _RecordingDeliver(receipt="none")
    scheduler = build_scheduler(medication_deliver=deliver)
    try:
        job = scheduler.get_job("anyu:medication-due-scan")
        assert job is not None
        assert isinstance(job.func, functools.partial)
        assert job.func.func is medication_jobs.run_medication_due_scan
        assert job.func.keywords["deliver"] is deliver, "必须是装配根递进来的同一个对象"
        assert job.args == ()
        assert job.kwargs == {}

        opened = await _open_event(db)
        handled = [
            await job.func(now=opened.t0 + datetime.timedelta(minutes=minutes))
            for minutes in (0, 5, 10, 15)
        ]
        assert handled == [1, 1, 1, 1]
        assert await job.func(now=opened.t0 + datetime.timedelta(minutes=16)) == 1
    finally:
        if scheduler.running:  # 本用例从不 start()，这里只是兜底
            scheduler.shutdown(wait=False)

    attempts = await _attempts(db, opened.reminder_id)
    assert [row.receipt for row in attempts] == ["none"] * 4
    await _assert_single_escalation_artifacts(
        db, opened, terminal_state="notification_failed", failure_reason="rtc_failed"
    )


# ---------------------------------------------------------------------------
# 验收 5：升级后重复驱动不重复建单（幂等）
# ---------------------------------------------------------------------------


async def test_repeated_drive_after_escalation_does_not_duplicate(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """同一事件已升到终态后再驱动：handled 恒 0，风险事件/工单/通知/触达条数都不变。"""
    opened = await _open_event(db)
    deliver = _RecordingDeliver()  # 回执 tts_finished → missed_unconfirmed
    await _drive_four_touches(deliver, opened.t0)
    assert (
        await medication_jobs.run_medication_due_scan(
            now=opened.t0 + datetime.timedelta(minutes=16), deliver=deliver
        )
        == 1
    )

    _, order, notification = await _assert_single_escalation_artifacts(
        db, opened, terminal_state="missed_unconfirmed", failure_reason="no_answer"
    )
    assert len(await _attempts(db, opened.reminder_id)) == 4

    for minutes in (16, 20, 60):
        assert (
            await medication_jobs.run_medication_due_scan(
                now=opened.t0 + datetime.timedelta(minutes=minutes), deliver=deliver
            )
            == 0
        )

    assert len(await _events(db, opened.seed.plan_id)) == 1
    # 幂等的关键落点：风险事件靠 events 行的 reminder_id 认领，重复升级不会多一条。
    assert len(await _risk_events(db, opened.reminder_id)) == 1
    assert [row.order_no for row in await _orders(db, opened.seed.elder_id)] == [order.order_no]
    assert [row.notification_id for row in await _notifications(db, opened.seed.elder_id)] == [
        notification.notification_id
    ]
    assert len(await _attempts(db, opened.reminder_id)) == 4
    assert len(deliver.targets) == 4, "重复驱动不该再出站"


# ---------------------------------------------------------------------------
# 验收 6：服务层幂等——同一事件连升两次复用同一风险事件与工单
# ---------------------------------------------------------------------------


async def test_escalate_twice_reuses_same_risk_event_and_order(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`escalate_to_work_order` 重入：第二次认领已建的风险事件，返回原单不另建。

    调度器正常不会重入（事件进 escalate 终态后 due-scan 就 skip 了），但
    `notification_service.recover_after_offline` 与人工补单都可能再碰一次；
    重复建单会让照护台出现两张指向同一次漏服的红线单。
    """
    opened = await _open_event(db)
    event = await medication_service.load_event(db, opened.reminder_id)

    first = await medication_service.escalate_to_work_order(db, event=event)
    second = await medication_service.escalate_to_work_order(db, event=event)

    assert second.order_no == first.order_no
    assert second.event_id == first.event_id, "第二次必须挂回同一条风险事件"
    assert len(await _risk_events(db, opened.reminder_id)) == 1
    assert len(await _orders(db, opened.seed.elder_id)) == 1
