"""调度层用药四次触达 + 维护任务的测试（迭代 7 · D 卡验收）。

覆盖六条验收（本文件与任务卡一一对应）：

1. `register_medication_jobs` 后 job 列表含预期 id（含维护任务）；
2. 到点计划生成事件、非到点不生成（真库 + 独立事务）；
3. `scheduler_enabled=False` 时 `build_scheduler()` 不含任何任务（回归既有口径）；
4. 四次触达用尽后触发升级：红线工单 + `medication_escalation` 通知（假 `deliver` +
   注入 `now`，不真等 60 秒）；
5. 72h 音频清理空转也打点（`runs_total=1` / `files_total=0`）；
6. `schedule_cron` 非法形态跳过并 warning（数据库列没有 CHECK，坏值必须被容忍）。

隔离策略沿用 `tests/test_reports_api.py`：`NullPool` 引擎 → 一条真实连接 → 外层事务
→ `join_transaction_mode="create_savepoint"` 的 session 工厂（任务体自己 commit 只释放
savepoint，最外层 rollback 保证主库零残留）。
"""

from __future__ import annotations

import datetime
import logging
import os
import uuid
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

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

import app.scheduler as scheduler_pkg
from app.core.config import get_settings
from app.core.metrics import metrics_snapshot, reset_metrics
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
from app.scheduler import build_scheduler
from app.scheduler import maintenance as maintenance_jobs
from app.scheduler import medication as medication_jobs

SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 夹具
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
    monkeypatch.setattr(maintenance_jobs, "db_session_factory", factory)
    yield factory


# ---------------------------------------------------------------------------
# 种子数据
# ---------------------------------------------------------------------------


@dataclass
class Seed:
    elder_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    business_day: datetime.date
    plans: dict[str, uuid.UUID] = field(default_factory=dict)


async def _seed(
    db: AsyncSession,
    *,
    clocks: dict[str, str],
    plan_ids: dict[str, uuid.UUID] | None = None,
) -> Seed:
    """一位长者 + 一名医生 + 一名主责护工 + 若干药单（`clocks` 是「别名 → HH:MM」）。

    `plan_ids` 可选：扫描按 `plan_id` 排序，要断言「前一条失败不影响后一条」时，
    必须把顺序钉死，否则 `uuid4()` 会让用例在两条计划之间随机翻转。
    """
    elder_id, doctor_id, care_worker_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    day = medication_jobs._business_date(datetime.datetime.now(datetime.UTC))

    await db.execute(
        StaffAccount.__table__.insert(),
        {"staff_id": doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        {"staff_id": care_worker_id, "name": "护工王", "role": "care_worker", "password_hash": "x"},
    )
    await db.execute(
        Elder.__table__.insert(),
        {"elder_id": elder_id, "name": "张桂芬", "status": "active"},
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
    )

    seed = Seed(
        elder_id=elder_id,
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        business_day=day,
    )
    for alias, clock in clocks.items():
        plan_id = (plan_ids or {}).get(alias) or uuid.uuid4()
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
        seed.plans[alias] = plan_id
    return seed


def _at(seed: Seed, hour: int, minute: int) -> datetime.datetime:
    """业务自然日的 `HH:MM`（Asia/Shanghai）→ UTC 时刻。"""
    return medication_jobs.scheduled_at_for(seed.business_day, hour, minute, SHANGHAI)


async def _load_event(db: AsyncSession, reminder_id: uuid.UUID) -> MedicationEvent:
    """重查事件并刷新属性。

    `expire_all()` 是必需的：任务体在**另一个 session** 里改了同一行，本 session 的
    identity map 里那份对象还是旧值，直接 `select` 会拿到陈旧属性（SQLAlchemy 默认不
    覆写已填充字段），断言就会对着过期快照跑。
    """
    db.expire_all()
    return (
        await db.execute(select(MedicationEvent).where(MedicationEvent.reminder_id == reminder_id))
    ).scalar_one()


# ---------------------------------------------------------------------------
# 验收 3：scheduler_enabled=False 时不含任何任务（回归既有口径）
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
    stub = _SettingsStub(scheduler_enabled=enabled)
    monkeypatch.setattr(scheduler_pkg, "get_settings", lambda: stub)


def test_build_scheduler_disabled_has_no_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`scheduler_enabled=False` → 一个任务都不注册（reload 场景不双触发）。"""
    _patch_settings(monkeypatch, enabled=False)
    scheduler = build_scheduler()
    try:
        assert scheduler.get_jobs() == []
    finally:
        if scheduler.running:  # 未 start() 的调度器 shutdown 会抛 SchedulerNotRunningError
            scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 验收 1：注册后的 job id 与触发时刻
# ---------------------------------------------------------------------------


def test_register_medication_jobs_registers_expected_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """用药与维护任务都挂在 `anyu:` 前缀下，且与原三个日任务共存。"""
    _patch_settings(monkeypatch, enabled=True)
    scheduler = build_scheduler()
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}
    finally:
        if scheduler.running:  # 未 start() 的调度器 shutdown 会抛 SchedulerNotRunningError
            scheduler.shutdown(wait=False)

    expected = {
        "anyu:medication-scan",
        "anyu:medication-due-scan",
        "anyu:audio-cleanup",
        "anyu:memory-decay",
        "anyu:memory-capacity",
    }
    assert expected <= set(jobs)
    # 既有三个日任务不许被这次追加改掉（回归）。
    assert {
        "anyu:health-checkup-due",
        "anyu:health-missed-scan",
        "anyu:daily-report-generate",
    } <= set(jobs)

    triggers = {job_id: str(jobs[job_id].trigger) for job_id in expected}
    assert triggers["anyu:medication-scan"] == "cron[minute='*/5']"
    assert triggers["anyu:medication-due-scan"] == "cron[minute='*']"
    assert triggers["anyu:audio-cleanup"] == "cron[minute='0']"
    assert triggers["anyu:memory-decay"] == "cron[hour='3', minute='0']"
    assert triggers["anyu:memory-capacity"] == "cron[hour='3', minute='10']"
    for job_id in expected:
        assert jobs[job_id].coalesce is True
        assert jobs[job_id].max_instances == 1
        assert jobs[job_id].misfire_grace_time == 300


# ---------------------------------------------------------------------------
# 验收 2：到点生成 / 非到点不生成 / 幂等
# ---------------------------------------------------------------------------


async def test_scan_creates_due_event_only(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """08:00 的计划在 09:00 生成事件；23:00 的计划不生成；重跑不重复建。"""
    seed = await _seed(db, clocks={"due": "08:00", "later": "23:00"})
    now = _at(seed, 9, 0)

    created = await medication_jobs.run_medication_scan(now=now, session_factory=isolated_factory)
    assert created >= 1

    due_events = (
        (
            await db.execute(
                select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["due"])
            )
        )
        .scalars()
        .all()
    )
    assert len(due_events) == 1
    event = due_events[0]
    assert event.scheduled_at == _at(seed, 8, 0)
    assert event.elder_id == seed.elder_id
    assert event.status == "calling"
    assert event.triggered_by == "scheduler"
    assert event.drug_snapshot["drug_name"] == "氨氯地平"

    later_events = (
        (
            await db.execute(
                select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["later"])
            )
        )
        .scalars()
        .all()
    )
    assert later_events == [], "23:00 的药单在 09:00 不该生成事件"

    # 幂等：同键同内容重跑不新建（PRD FR-MED-04）。
    assert await medication_jobs.run_medication_scan(now=now) >= 0
    again = (
        (
            await db.execute(
                select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["due"])
            )
        )
        .scalars()
        .all()
    )
    assert len(again) == 1


# ---------------------------------------------------------------------------
# 验收 6：schedule_cron 非法 → 跳过 + warning
# ---------------------------------------------------------------------------


async def test_scan_skips_invalid_schedule_cron(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """数据库列没有 CHECK，坏的 `schedule_cron` 必须「跳过 + warning」而不是炸整轮。"""
    seed = await _seed(db, clocks={"broken": "every 8 hours"})

    with caplog.at_level(logging.WARNING, logger="app.scheduler.medication"):
        await medication_jobs.run_medication_scan(now=_at(seed, 9, 0))

    broken_events = (
        (
            await db.execute(
                select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["broken"])
            )
        )
        .scalars()
        .all()
    )
    assert broken_events == []
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(
        "schedule_cron" in record.getMessage() and str(seed.plans["broken"]) in record.getMessage()
        for record in warnings
    ), [record.getMessage() for record in warnings]


async def test_scan_conflict_on_one_plan_does_not_abort_round(
    db: AsyncSession,
    isolated_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """回归 2026-11-16：一条计划「同键异内容」只该跳过它自己，不能拖垮整轮。

    缺陷原形：冲突分支的 `session.rollback()` 把**整个 session** 已加载的 ORM 实例
    标记为 expired（`expire_on_commit=False` 只管 commit，管不住 rollback）；轮到下一条
    plan 读 `schedule_cron` 时触发懒加载，属性访问在同步上下文、懒加载要发 SQL，于是
    抛 `MissingGreenlet`，异常冒到外层「整轮失败」——同一轮里其他长者的提醒一条都不生成。

    修复把事务粒度收到单条计划，本用例断言的就是「前一条冲突后，后一条照样落库」。
    """
    from app.services import medication_service

    # 顺序必须钉死：扫描按 plan_id 排序，conflict 要先于 healthy 被处理，
    # 否则用例在两条计划之间随机翻转，测不到「前面失败影响后面」。
    seed = await _seed(
        db,
        clocks={"conflict": "08:00", "healthy": "09:00"},
        plan_ids={
            "conflict": uuid.UUID("00000000-0000-4000-8000-0000000000a1"),
            "healthy": uuid.UUID("00000000-0000-4000-8000-0000000000a2"),
        },
    )

    # 给 conflict 计划预置一条「同键不同内容」的旧事件：药名被改过，快照对不上。
    stale_snapshot = {"drug_name": "硝苯地平", "dose": "1 片"}
    await db.execute(
        MedicationEvent.__table__.insert(),
        {
            "reminder_id": uuid.uuid4(),
            "plan_id": seed.plans["conflict"],
            "elder_id": seed.elder_id,
            "scheduled_at": _at(seed, 8, 0),
            "drug_snapshot": stale_snapshot,
            "payload_hash": medication_service.drug_snapshot_hash(stale_snapshot),
            "triggered_by": "scheduler",
            "status": "calling",
            "terminal_state": None,
            "late_taken": False,
            "retry_count": 0,
        },
    )
    await db.flush()

    with caplog.at_level(logging.WARNING, logger="app.scheduler.medication"):
        created = await medication_jobs.run_medication_scan(
            now=_at(seed, 9, 0), session_factory=isolated_factory
        )

    db.expire_all()
    healthy_events = (
        (
            await db.execute(
                select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["healthy"])
            )
        )
        .scalars()
        .all()
    )
    assert len(healthy_events) == 1, "冲突之后的那条计划仍必须生成事件（本轮修复的回归面）"
    # 不写 `created == 1`：`run_medication_scan` 查的是全库 active 计划，开发库里另有三条
    # 演示药单也会被扫到（其中两条本身就是同键异内容）。本用例只认「本计划被建出来」。
    assert created >= 1

    db.expire_all()
    conflict_events = (
        (
            await db.execute(
                select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["conflict"])
            )
        )
        .scalars()
        .all()
    )
    assert len(conflict_events) == 1, "冲突时保留原事件，不新增也不改写"
    assert conflict_events[0].drug_snapshot["drug_name"] == "硝苯地平"

    messages = [record.getMessage() for record in caplog.records]
    assert any("同键异内容" in message for message in messages), messages
    assert not any("整轮失败" in message for message in messages), messages


# ---------------------------------------------------------------------------
# 验收 4：四次触达 → 升级（假 deliver + 注入 now，不真等）
# ---------------------------------------------------------------------------


@dataclass
class _RecordingDeliver:
    """假出站通道：只记目标，不碰网络/TTS。"""

    receipt: str = "tts_finished"
    targets: list[medication_jobs.MedicationTouchTarget] = field(default_factory=list)

    async def __call__(
        self, target: medication_jobs.MedicationTouchTarget
    ) -> str | None:  # pragma: no cover - 调用即记录
        self.targets.append(target)
        return self.receipt


async def test_four_touches_then_escalate(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """T0/+5/+10/+15 四枪 → 最后一枪 60s 窗关闭后升级：红线工单 + 派护工通知。"""
    seed = await _seed(db, clocks={"due": "08:00"})
    t0 = _at(seed, 8, 0)

    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    event = (
        await db.execute(
            select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["due"])
        )
    ).scalar_one()
    reminder_id = event.reminder_id

    deliver = _RecordingDeliver()
    for minutes in (0, 5, 10, 15):
        await medication_jobs.run_medication_due_scan(
            now=t0 + datetime.timedelta(minutes=minutes), deliver=deliver
        )

    def _mine() -> list[medication_jobs.MedicationTouchTarget]:
        """只数本用例那条提醒的触达（共享开发库里可能有别人造的到点事件）。"""
        return [target for target in deliver.targets if target.reminder_id == reminder_id]

    attempts = (
        (
            await db.execute(
                select(MedicationAttempt)
                .where(MedicationAttempt.event_id == reminder_id)
                .order_by(MedicationAttempt.attempt_no)
            )
        )
        .scalars()
        .all()
    )
    assert [attempt.attempt_no for attempt in attempts] == [1, 2, 3, 4]
    assert [attempt.sent_at for attempt in attempts] == [
        t0 + datetime.timedelta(minutes=minutes) for minutes in (0, 5, 10, 15)
    ]
    assert all(attempt.receipt == "tts_finished" for attempt in attempts)
    assert all(attempt.tts_result == "ok" for attempt in attempts)
    assert len(_mine()) == 4

    # 窗口还没关（最后一枪只过了 30 秒）：既不再触达，也不升级。
    midway = t0 + datetime.timedelta(minutes=15, seconds=30)
    await medication_jobs.run_medication_due_scan(now=midway, deliver=deliver)
    refreshed = await _load_event(db, reminder_id)
    assert refreshed.status == "waiting_confirm"
    assert refreshed.retry_count == 3
    assert len(_mine()) == 4

    # 60 秒窗关闭仍无确认 → 升级。
    await medication_jobs.run_medication_due_scan(
        now=t0 + datetime.timedelta(minutes=16), deliver=deliver
    )
    escalated = await _load_event(db, reminder_id)
    assert escalated.status == "escalated"
    assert escalated.terminal_state == "missed_unconfirmed"
    assert len(_mine()) == 4, "升级不许再补发第五枪"

    orders = (
        (
            await db.execute(
                select(WorkOrder).where(
                    WorkOrder.elder_id == seed.elder_id,
                    WorkOrder.type == "missed_medication",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(orders) == 1
    assert orders[0].severity == "red"
    assert orders[0].status == "new"
    assert str(reminder_id) in (orders[0].note or "")

    notifications = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.kind == "medication_escalation",
                    Notification.elder_id == seed.elder_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.severity == "red"
    assert notification.assignee_staff_id == seed.care_worker_id
    assert notification.payload["reminder_id"] == str(reminder_id)
    assert notification.payload["work_order_no"] == orders[0].order_no


async def test_stale_window_escalates_without_late_touch(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """窗口整体错过（调度器停机 1 小时）→ 直接升级，不补发晚到的提醒（FR-MED-08）。"""
    seed = await _seed(db, clocks={"due": "08:00"})
    t0 = _at(seed, 8, 0)
    assert await medication_jobs.run_medication_scan(now=t0) >= 1
    event = (
        await db.execute(
            select(MedicationEvent).where(MedicationEvent.plan_id == seed.plans["due"])
        )
    ).scalar_one()

    deliver = _RecordingDeliver()
    await medication_jobs.run_medication_due_scan(
        now=t0 + datetime.timedelta(hours=1), deliver=deliver
    )

    touched = [t for t in deliver.targets if t.reminder_id == event.reminder_id]
    assert touched == [], "超窗事件不许补发触达"
    escalated = await _load_event(db, event.reminder_id)
    assert escalated.status == "escalated"


# ---------------------------------------------------------------------------
# 维护任务对齐：记忆衰减 / 容量淘汰（验证 memory_service 的 keyword-only 签名）
# ---------------------------------------------------------------------------


async def test_maintenance_memory_jobs_run_clean(
    isolated_factory: async_sessionmaker[AsyncSession],
) -> None:
    """两个记忆维护任务在真库上跑通、返回整数且不抛（服务层已 commit，事务可回滚）。"""
    decayed = await maintenance_jobs.run_memory_decay()
    assert isinstance(decayed, int)
    assert decayed >= 0

    evicted = await maintenance_jobs.run_memory_capacity_eviction()
    assert isinstance(evicted, int)
    assert evicted >= 0


# ---------------------------------------------------------------------------
# 验收 5：72h 音频清理（空转也打点）+ 过期文件真删
# ---------------------------------------------------------------------------


def _metrics_enabled() -> bool:
    return bool(get_settings().metrics_enabled)


@pytest.mark.skipif(not _metrics_enabled(), reason="metrics_enabled=False 时打点是 no-op")
async def test_audio_cleanup_empty_spin_counts_zero(tmp_path: Path) -> None:
    """没有落盘音频时任务跑空转：runs+1、files=0（区分「跑过没文件」与「没跑」）。"""
    reset_metrics()
    removed = await maintenance_jobs.run_audio_cleanup(root=tmp_path)
    counters = metrics_snapshot()["counters"]
    assert removed == 0
    assert counters["audio_cleanup_runs_total"] == 1
    assert counters["audio_cleanup_files_total"] == 0


@pytest.mark.skipif(not _metrics_enabled(), reason="metrics_enabled=False 时打点是 no-op")
async def test_audio_cleanup_removes_only_expired_audio(tmp_path: Path) -> None:
    """超过 72h 的音频删掉，新鲜文件与非音频文件原样保留。"""
    stale = tmp_path / "old.wav"
    stale.write_bytes(b"RIFF....")
    fresh = tmp_path / "new.webm"
    fresh.write_bytes(b"webm")
    keep = tmp_path / "notes.txt"
    keep.write_text("not audio", encoding="utf-8")

    now = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.UTC)
    old_ts = (now - datetime.timedelta(hours=73)).timestamp()
    os.utime(stale, (old_ts, old_ts))

    reset_metrics()
    removed = await maintenance_jobs.run_audio_cleanup(now=now, root=tmp_path)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert keep.exists()
    counters = metrics_snapshot()["counters"]
    assert counters["audio_cleanup_runs_total"] == 1
    assert counters["audio_cleanup_files_total"] == 1
