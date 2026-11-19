"""用药触达「接线」的测试（迭代 10 · task-med-2 / PRD FR-MED-06、FR-MED-08）。

本文件只测**注入与归因**，不重测触达节拍本身（那在 `tests/test_scheduler_medication.py`）：

1. `register_medication_jobs(scheduler, deliver=...)` 把通道绑进 due-scan 的
   `functools.partial`——注册时绑定一次、运行期每轮吃同一个对象；
2. `build_scheduler(medication_deliver=...)` 把通道透传给用药任务，job id 与触发时刻
   保持原样（回归既有口径）；不传时通道为 `None`（走空实现，不伪造送达）；
3. 注册后的 job 任务体真能把通道带到触达上：假 `deliver` 收到目标、库里落回执；
4. 升级归因：四次触达一次回执都没有 → `notification_failed` + `rtc_failed`（PRD:324）；
   有回执但无人确认 → `missed_unconfirmed` + `no_answer`（PRD:323）；
5. 确认图停点（task-20 · FR-MED-05 / AC-17）：到点触达后线程停在 `wait_confirm`、
   重放同一枪不重发、更早的停点不挡下一枪、进程重启后停点仍在且确认只落一次、
   图不可用时触达照发（只有 warning）。

**不起调度器时钟**：本文件只构造 `AsyncIOScheduler`、读 job 定义、直接 `await` 任务体，
没有任何 `scheduler.start()`（真起时钟会让测试变成对墙上时间的竞态）。

隔离策略沿用 `tests/test_scheduler_medication.py`：`NullPool` 引擎 → 一条真实连接 →
外层事务 → `join_transaction_mode="create_savepoint"` 的 session 工厂。
"""

from __future__ import annotations

import datetime
import functools
import logging
import uuid
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.graphs import checkpointer as checkpointer_mod
from app.graphs import medication_confirmation_graph as medication_graph
from app.models import (
    Elder,
    ElderStaffAssignment,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    RiskEvent,
    StaffAccount,
    WorkOrder,
)
from app.scheduler import build_scheduler
from app.scheduler import medication as medication_jobs
from app.services import medication_service

SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 夹具（与 `tests/test_scheduler_medication.py` 同款隔离手法）
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
    """假出站通道：只记目标与固定回执，不碰网络/TTS。"""

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
    """一位长者 + 一名医生 + 一名主责护工 + 一张 `clock` 的药单（只造接线要的行）。"""
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


async def _event_for(db: AsyncSession, plan_id: uuid.UUID) -> MedicationEvent:
    """按药单取本次到点事件（`expire_all` 因为任务体在另一个 session 里改过它）。"""
    db.expire_all()
    return (
        await db.execute(select(MedicationEvent).where(MedicationEvent.plan_id == plan_id))
    ).scalar_one()


# ---------------------------------------------------------------------------
# 验收 1：通道绑在 partial 上（注册时绑定，不是每轮重取）
# ---------------------------------------------------------------------------


def test_register_medication_jobs_binds_deliver_into_due_scan() -> None:
    """due-scan 的任务体是 `partial(run_medication_due_scan, deliver=通道)`。"""
    deliver = _RecordingDeliver()
    scheduler = AsyncIOScheduler()
    medication_jobs.register_medication_jobs(scheduler, deliver=deliver)

    job = scheduler.get_job("anyu:medication-due-scan")
    assert job is not None
    assert isinstance(job.func, functools.partial)
    assert job.func.func is medication_jobs.run_medication_due_scan
    assert job.func.keywords == {"deliver": deliver}
    assert job.func.keywords["deliver"] is deliver, "必须是同一个对象，不许每轮重造通道"
    # 通道走 partial，不用 Job.kwargs：两处都传会让任务体收到重复实参。
    assert job.args == ()
    assert job.kwargs == {}

    # 事件预生成那一支不吃通道，保持原函数（job id / 触发时刻也不许被这次改动带偏）。
    scan_job = scheduler.get_job("anyu:medication-scan")
    assert scan_job is not None
    assert scan_job.func is medication_jobs.run_medication_scan
    assert str(job.trigger) == "cron[minute='*']"
    assert str(scan_job.trigger) == "cron[minute='*/5']"


# ---------------------------------------------------------------------------
# 验收 2：build_scheduler 透传（含默认 None 与 job id 回归）
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


def test_build_scheduler_passes_deliver_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """装配根递进来的通道原样到达 due-scan；不传时是 `None`（走空实现）。"""
    _patch_settings(monkeypatch, enabled=True)
    deliver = _RecordingDeliver()

    scheduler = build_scheduler(medication_deliver=deliver)
    try:
        job = scheduler.get_job("anyu:medication-due-scan")
        assert job is not None
        assert isinstance(job.func, functools.partial)
        assert job.func.func is medication_jobs.run_medication_due_scan
        assert job.func.keywords["deliver"] is deliver
        # job id / 触发时刻 / 并发口径都不许被透传参数改动（回归）。
        assert job.id == "anyu:medication-due-scan"
        assert str(job.trigger) == "cron[minute='*']"
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 300
    finally:
        if scheduler.running:  # 本用例从不 start()，这里只是兜底
            scheduler.shutdown(wait=False)

    defaulted = build_scheduler()
    default_job = defaulted.get_job("anyu:medication-due-scan")
    assert default_job is not None
    assert isinstance(default_job.func, functools.partial)
    assert default_job.func.keywords["deliver"] is None, "未注入时退回空实现，而不是消失"


# ---------------------------------------------------------------------------
# 验收 3：注入的通道真能驱动一次触达（不 start 调度器，直接跑任务体）
# ---------------------------------------------------------------------------


async def test_injected_deliver_reaches_touch_through_registered_job(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`job.func`（注册后的 due-scan 任务体）带着通道打到触达：目标正确 + 回执落库。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    # 先取出主键再过 `expire_all()`：expired 实例的属性访问会触发同步 IO，
    # 在 async 测试里就是 MissingGreenlet（任务体在另一个 session 里改过这行）。
    reminder_id = (await _event_for(db, seed.plan_id)).reminder_id

    deliver = _RecordingDeliver()
    scheduler = AsyncIOScheduler()
    medication_jobs.register_medication_jobs(scheduler, deliver=deliver)
    job = scheduler.get_job("anyu:medication-due-scan")
    assert job is not None
    assert await job.func(now=t0) >= 1  # 任务体自己带 now，不依赖墙上时间

    mine = [target for target in deliver.targets if target.reminder_id == reminder_id]
    assert len(mine) == 1
    target = mine[0]
    assert target.attempt_no == 1
    assert target.plan_id == seed.plan_id
    assert target.elder_id == seed.elder_id
    assert target.scheduled_at == t0
    # 快照来自事件冻结药单，不是实时回查（PRD FR-MED-04）。
    assert target.drug_snapshot["drug_name"] == "氨氯地平"
    assert target.drug_snapshot["dose"] == "1 片"

    db.expire_all()
    attempt = (
        await db.execute(select(MedicationAttempt).where(MedicationAttempt.event_id == reminder_id))
    ).scalar_one()
    assert attempt.receipt == "tts_finished"
    assert attempt.tts_result == "ok"
    assert attempt.sent_at == t0

    refreshed = await _event_for(db, seed.plan_id)
    assert refreshed.status == "waiting_confirm"
    assert refreshed.retry_count == 0


# ---------------------------------------------------------------------------
# 验收 4：升级归因（两次触达口径 → 两条终态）
# ---------------------------------------------------------------------------


async def test_escalation_without_receipts_is_notification_failed(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """四次触达一次回执都没有 → PRD:324 `notification_failed` + FR-MED-06 `rtc_failed`。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    reminder_id = (await _event_for(db, seed.plan_id)).reminder_id

    deliver = _RecordingDeliver(receipt="none")
    for minutes in (0, 5, 10, 15):
        await medication_jobs.run_medication_due_scan(
            now=t0 + datetime.timedelta(minutes=minutes), deliver=deliver
        )
    await medication_jobs.run_medication_due_scan(
        now=t0 + datetime.timedelta(minutes=16), deliver=deliver
    )

    escalated = await _event_for(db, seed.plan_id)
    assert escalated.reminder_id == reminder_id
    assert escalated.status == "escalated"
    # 四处都没送到时不许落 `missed_unconfirmed`：那是「人收到了却没确认」，
    # 把「没听见」写成「没回应」等于诬告长者（PRD:324 明令不得表述为拒服/已知悉未确认）。
    assert escalated.terminal_state == "notification_failed"
    assert escalated.failure_reason == "rtc_failed"


async def test_escalation_with_receipts_stays_missed_unconfirmed(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """有回执但无人确认 → PRD:323 `missed_unconfirmed` + `no_answer`（与图同口径）。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1

    deliver = _RecordingDeliver()  # 回执 tts_finished
    for minutes in (0, 5, 10, 15):
        await medication_jobs.run_medication_due_scan(
            now=t0 + datetime.timedelta(minutes=minutes), deliver=deliver
        )
    await medication_jobs.run_medication_due_scan(
        now=t0 + datetime.timedelta(minutes=16), deliver=deliver
    )

    escalated = await _event_for(db, seed.plan_id)
    assert escalated.status == "escalated"
    assert escalated.terminal_state == "missed_unconfirmed"
    assert escalated.failure_reason == "no_answer"


@pytest.mark.parametrize(
    ("receipts", "expected"),
    [
        # 一次回执都没有（空事件 / 全 none / 全是 NULL）→ 没送到。
        ([], ("notification_failed", "rtc_failed")),
        (["none", "none", "none", "none"], ("notification_failed", "rtc_failed")),
        ([None, None], ("notification_failed", "rtc_failed")),
        # 有任一「已送达」回执 → 送到了人没回应。
        (["card_rendered"], ("missed_unconfirmed", "no_answer")),
        (["none", "tts_finished", "none"], ("missed_unconfirmed", "no_answer")),
        (["tts_finished", "card_rendered"], ("missed_unconfirmed", "no_answer")),
    ],
)
def test_escalation_attribution_maps_receipts(
    receipts: list[str | None], expected: tuple[str, str]
) -> None:
    """纯函数口径：只看回执，不看外呼结果（attempts 表没有 rtc_result 列）。"""
    assert medication_service.escalation_attribution(receipts) == expected


def test_attribution_agrees_with_confirmation_graph() -> None:
    """镜像防漂移：图里 `resolve_timeout_terminal` 的终态必须与本函数逐值一致。

    `scheduler → graphs` 也是横向 import（AGENTS §2.1），所以两份判据各自留一份；
    这里像 `test_medication_failure_reason.py` 钉 `OUTBOUND_RTC_RESULTS` 一样，
    改了任一侧就红，逼两边同改。
    """
    from app.graphs.medication_confirmation_graph import resolve_timeout_terminal

    for receipts in ((), ("none",), ("none", "none"), ("card_rendered",), ("tts_finished",)):
        terminal_state, _reason = medication_service.escalation_attribution(list(receipts))
        assert terminal_state == resolve_timeout_terminal(receipts), receipts


# ---------------------------------------------------------------------------
# 验收 5：调度器把事件挂到图的确认停点上（task-20 · FR-MED-05 / AC-17）
# ---------------------------------------------------------------------------
#
# 这一组用**真 checkpointer**（`langgraph` schema 里的 PostgreSQL saver），不用
# `MemorySaver`：本卡要证明的正是「进程重启后停点还在」，内存盘在重启语义上等于不作弊；
# 图节点写业务表时走 savepoint 工厂，所以外层 rollback 依然能把留痕抹干净。
#
# 检查点本身**不受**外层事务管辖（它在另一个 schema、由 saver 自己 commit），
# 所以每个用例结束都要 `adelete_thread`，否则 `anyu_test` 会攒垃圾。


class _ConfirmationHarness:
    """把用药确认图绑到测试连接上，并允许像「进程重启」那样整体重建。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self.saver: Any = None
        self.graph: Any = None
        self.threads: list[str] = []

    async def boot(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """装图并通过服务层的 `confirmation_graph` 接进去（生产取图点）。"""

        async def provider() -> Any:
            assert self.graph is not None, "确认图尚未装配"
            return self.graph

        monkeypatch.setattr(medication_service, "confirmation_graph", provider)
        return await self.rebuild()

    async def rebuild(self) -> Any:
        """丢掉旧的图与 saver，重新取一次 checkpointer——这就是「重启」。

        这里直接调 `build_medication_graph`（而不是 `get_medication_graph`）：后者是
        生产单例、不吃 `checkpointer` 参数，测试需要把图钉在**同一份** saver + 测试连接上。
        """
        self.saver = await checkpointer_mod.get_checkpointer()
        factory = async_sessionmaker(
            bind=self._db.bind,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.graph = await medication_graph.build_medication_graph(
            checkpointer=self.saver, session_factory=factory
        )
        return self.graph

    def remember(self, thread: str) -> str:
        self.threads.append(thread)
        return thread

    async def cleanup(self) -> None:
        for thread in self.threads:
            if self.saver is not None:
                await self.saver.adelete_thread(thread)
        medication_graph.reset_medication_graph()
        await checkpointer_mod.close_checkpointer()


async def _thread_for(seed: Seed, scheduled_at: datetime.datetime) -> str:
    return medication_graph.medication_thread_id(
        elder_id=seed.elder_id, plan_id=seed.plan_id, scheduled_at=scheduled_at
    )


async def _attempts(db: AsyncSession, reminder_id: uuid.UUID) -> list[MedicationAttempt]:
    db.expire_all()
    rows = await db.execute(
        select(MedicationAttempt)
        .where(MedicationAttempt.event_id == reminder_id)
        .order_by(MedicationAttempt.attempt_no)
    )
    return list(rows.scalars())


async def _orders_for(db: AsyncSession, elder_id: uuid.UUID) -> int:
    """这位长者的工单数（种子每次用新 UUID，所以整表计数就等于「本用例造了几张」）。"""
    db.expire_all()
    total = await db.scalar(
        select(func.count()).select_from(WorkOrder).where(WorkOrder.elder_id == elder_id)
    )
    return int(total or 0)


async def _risk_events_for(db: AsyncSession, elder_id: uuid.UUID) -> int:
    db.expire_all()
    total = await db.scalar(
        select(func.count()).select_from(RiskEvent).where(RiskEvent.elder_id == elder_id)
    )
    return int(total or 0)


async def test_due_scan_opens_a_confirmation_stop(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """到点驱动一次：图线程停在 `wait_confirm`、事件等确认、attempts 恰好一行。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    reminder_id = (await _event_for(db, seed.plan_id)).reminder_id

    harness = _ConfirmationHarness(db)
    graph = await harness.boot(monkeypatch)
    thread = harness.remember(await _thread_for(seed, t0))
    try:
        deliver = _RecordingDeliver()
        assert await medication_jobs.run_medication_due_scan(now=t0, deliver=deliver) >= 1
        assert [target.reminder_id for target in deliver.targets] == [reminder_id]

        payload = await medication_graph.pending_confirmation(graph, thread)
        assert payload is not None, "调度器发完提醒必须把线程停在确认点上"
        assert payload["type"] == "medication_confirm"
        assert str(payload["reminder_id"]) == str(reminder_id)
        assert payload["attempt_no"] == 1

        event = await _event_for(db, seed.plan_id)
        assert event.status == "waiting_confirm"
        assert len(await _attempts(db, reminder_id)) == 1

        snapshot = await graph.aget_state(medication_graph.state_config(thread))
        assert snapshot.values["triggered_by"] == "scheduler"
        assert snapshot.values["attempt_no"] == 1
        assert tuple(snapshot.next) == (medication_graph.CONFIRM_STOP_NODE,)
    finally:
        await harness.cleanup()


async def test_replaying_the_same_touch_does_not_resend_when_a_stop_waits(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一枪被重放（重启后判定重算）：停点还在，就**不再发**、attempts 不增。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    reminder_id = (await _event_for(db, seed.plan_id)).reminder_id

    harness = _ConfirmationHarness(db)
    graph = await harness.boot(monkeypatch)
    thread = harness.remember(await _thread_for(seed, t0))
    try:
        await medication_jobs.run_medication_due_scan(now=t0, deliver=_RecordingDeliver())
        assert await medication_graph.pending_confirmation(graph, thread) is not None

        replay = _RecordingDeliver()
        await medication_jobs.run_medication_touch(
            reminder_id=reminder_id, attempt_no=1, now=t0, deliver=replay
        )
        # 连整轮 due-scan 一起重放：判定可能算出「该发下一枪」，但这一枪的回应还没来。
        await medication_jobs.run_medication_due_scan(now=t0, deliver=replay)

        assert replay.targets == [], "已经有人在等这一枪的回应，再喊一次就是重复催药"
        assert len(await _attempts(db, reminder_id)) == 1
        assert (await _event_for(db, seed.plan_id)).status == "waiting_confirm"
    finally:
        await harness.cleanup()


async def test_newer_attempt_still_fires_while_an_older_stop_waits(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停点停在更早一枪上时照旧发下一枪：T+5/T+10/T+15 的节拍不许被图吞掉。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    reminder_id = (await _event_for(db, seed.plan_id)).reminder_id

    harness = _ConfirmationHarness(db)
    graph = await harness.boot(monkeypatch)
    thread = harness.remember(await _thread_for(seed, t0))
    try:
        await medication_jobs.run_medication_due_scan(now=t0, deliver=_RecordingDeliver())

        deliver = _RecordingDeliver()
        await medication_jobs.run_medication_touch(
            reminder_id=reminder_id,
            attempt_no=2,
            now=t0 + datetime.timedelta(minutes=5),
            deliver=deliver,
        )

        assert [target.attempt_no for target in deliver.targets] == [2]
        assert [row.attempt_no for row in await _attempts(db, reminder_id)] == [1, 2]
        # 确认只回答「这条提醒」，所以不叠加第二个停点：仍停在第一枪上。
        payload = await medication_graph.pending_confirmation(graph, thread)
        assert payload is not None
        assert payload["attempt_no"] == 1
    finally:
        await harness.cleanup()


async def test_confirmation_survives_a_checkpointer_restart(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """丢掉运行时重建（真重启）：停点仍在，确认一次只落一次终态、attempts 不增。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    reminder_id = (await _event_for(db, seed.plan_id)).reminder_id

    harness = _ConfirmationHarness(db)
    graph = await harness.boot(monkeypatch)
    thread = harness.remember(await _thread_for(seed, t0))
    try:
        await medication_jobs.run_medication_due_scan(now=t0, deliver=_RecordingDeliver())
        assert await medication_graph.pending_confirmation(graph, thread) is not None

        await checkpointer_mod.close_checkpointer()  # 模拟进程退出：连接与单例全丢
        rebuilt = await harness.rebuild()
        assert rebuilt is not graph, "重建后必须是新图，否则「重启」没被真模拟"
        assert await medication_graph.pending_confirmation(rebuilt, thread) is not None

        replay = _RecordingDeliver()
        await medication_jobs.run_medication_touch(
            reminder_id=reminder_id, attempt_no=1, now=t0, deliver=replay
        )
        assert replay.targets == []
        assert len(await _attempts(db, reminder_id)) == 1

        db.expire_all()
        event = await _event_for(db, seed.plan_id)
        _confirmed, resumed = await medication_service.confirm_event(
            db, event=event, channel="button", graph=rebuilt
        )
        assert resumed is True, "有停点就必须由图落终态（hitl_resumed 的语义）"

        confirmed = await _event_for(db, seed.plan_id)
        assert confirmed.status == "confirmed"
        assert confirmed.terminal_state == "taken_reported"
        assert confirmed.confirm_channel == "button"
        assert len(await _attempts(db, reminder_id)) == 1, "确认不是一次新触达"
        assert await medication_graph.pending_confirmation(rebuilt, thread) is None
    finally:
        await harness.cleanup()


async def test_touch_still_fires_when_the_confirmation_graph_is_unavailable(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图挂了只 warning：触达照发、事件照写，绝不因为流程引擎而扣下提醒。"""

    async def unavailable() -> Any:
        raise checkpointer_mod.CheckpointerUnavailableError("checkpointer is down")

    monkeypatch.setattr(medication_service, "confirmation_graph", unavailable)
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    reminder_id = (await _event_for(db, seed.plan_id)).reminder_id

    # 不用 caplog：它是会话级 handler，整套跑时别的用例与全局 logging 配置都会掺进来
    # （曾出现「单跑绿、全跑红」）。这里直接挂一个只属于本用例的 handler，只收本模块记录。
    scheduler_logger = logging.getLogger("app.scheduler.medication")
    captured: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Collect(level=logging.DEBUG)
    previous_level = scheduler_logger.level
    scheduler_logger.addHandler(handler)
    scheduler_logger.setLevel(logging.DEBUG)
    try:
        deliver = _RecordingDeliver()
        assert await medication_jobs.run_medication_due_scan(now=t0, deliver=deliver) >= 1
    finally:
        scheduler_logger.removeHandler(handler)
        scheduler_logger.setLevel(previous_level)

    assert [target.reminder_id for target in deliver.targets] == [reminder_id]
    assert len(await _attempts(db, reminder_id)) == 1
    event = await _event_for(db, seed.plan_id)
    assert event.status == "waiting_confirm"
    messages = [record.getMessage() for record in captured]
    assert any("用药确认图不可用" in message for message in messages), messages
    assert any("用药确认停点挂载失败" in message for message in messages), messages
    assert captured, "调度器这条路径必须留下日志"
    # 只断言「失败路径有 warning」：同一轮还有 INFO 汇总行（handled=N），那不是异常信号。
    assert not [r for r in captured if r.levelno >= logging.ERROR], [
        (r.levelno, r.getMessage()) for r in captured
    ]


async def test_escalation_keeps_a_single_order_while_a_stop_still_waits(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """升级链路过停点：归因与建单口径一行不改，也不许因为图多建一张单。"""
    seed = await _seed_due_plan(db)
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1

    harness = _ConfirmationHarness(db)
    graph = await harness.boot(monkeypatch)
    thread = harness.remember(await _thread_for(seed, t0))
    try:
        deliver = _RecordingDeliver(receipt="none")
        for minutes in (0, 5, 10, 15):
            await medication_jobs.run_medication_due_scan(
                now=t0 + datetime.timedelta(minutes=minutes), deliver=deliver
            )
        # 第一枪的停点一直没被回应，后面的枪照发（节拍归调度器）。
        payload = await medication_graph.pending_confirmation(graph, thread)
        assert payload is not None
        assert payload["attempt_no"] == 1

        await medication_jobs.run_medication_due_scan(
            now=t0 + datetime.timedelta(minutes=16), deliver=deliver
        )
        escalated = await _event_for(db, seed.plan_id)
        assert escalated.status == "escalated"
        assert escalated.terminal_state == "notification_failed"
        assert escalated.failure_reason == "rtc_failed"
        assert await _orders_for(db, seed.elder_id) == 1
        assert await _risk_events_for(db, seed.elder_id) == 1

        # 再驱动一轮：闭环事件不许重开一张单（幂等），停点也不该被当成新证据。
        await medication_jobs.run_medication_due_scan(
            now=t0 + datetime.timedelta(minutes=17), deliver=deliver
        )
        assert await _orders_for(db, seed.elder_id) == 1
        assert await _risk_events_for(db, seed.elder_id) == 1
    finally:
        await harness.cleanup()
