"""迭代 7 用药链路端到端验收：导入 → 当日事件 → 四轮触达 → 漏服工单（task-52）。

跑法（**必须**指向独立测试库，见下方「为什么不能事务回滚」）::

    $env:ANYU_TEST_DATABASE_URL = "postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test"  # pragma: allowlist secret
    uv run pytest tests/test_medication_reminder_e2e.py -q

覆盖 TD:1114 / TD:1115 的 迭代 7 验收第 1、2、6 条：
1. 一次导入 5 条药单 → 当天生成 5 条 `medication_events`，时刻与药单一致；
2. 到点 → 第 1 枪；60 秒不答 → 第 2/3/4 枪按 +5/+10/+15 分钟递增（`attempt_no` 1..4）；
3. 四枪打完 + 最后一窗 60 秒仍无确认 → `missed_medication` 红线工单 + 派护工通知。

## 时间推进口径（报告里逐条对照，不许混写）

本文件**不 sleep**，也不依赖 APScheduler 的调度器实例。所有「时间过去」都是把
`now=` 注入**真实入口**，由入口内部的判定与写库逻辑自己推进：

| 步骤 | 调用的入口 | 时间从哪来 |
| --- | --- | --- |
| 生成当日事件（5 次扫描） | `run_medication_scan(now=…)` | 注入：每个服药时刻前 5 分钟 |
| 第 1–4 轮触达 | `run_medication_due_scan(now=…)` → `run_medication_touch` | 注入：T0、T0+5、T0+10、T0+15 分钟 |
| 「60 秒窗未关」这一轮 | `run_medication_due_scan(now=T0+60s)` | 注入；预期不产生第 2 枪 |
| 升级工单 | `run_medication_due_scan(now=T0+16min)` → `_escalate_event` | 注入 |
| 判定边界（不写库） | `decide_event_action(…)` | **直接调纯函数**（用例名带 `_decision_` 前缀） |

**没有任何一步是「真定时器到点」**：`now=None` 的真实取时路径、APScheduler 的 job
注册与 5 分钟扫描间隔都没有被执行。能证明的是「给定 `now` 时链路做出正确推进」，
不能证明「墙钟到点会触发」——后者要靠调度器装配的集成测试，不在本卡范围。

## 归属验收口径

本仓库是**单机构部署**：schema 里没有租户/社区列，PRD 与 ER 也没有（不是漏表）。
因此「归属正确」在本用例里只落到两处——`work_orders.elder_id` 指向被打断的那位长者、
通知的 `assignee_staff_id` 等于该长者的 `primary_care` 护工；**不按租户维度断言**。

## 为什么不能用事务回滚夹具

`app/scheduler/medication.py` 的 `run_medication_touch` / `_escalate_event` /
`run_medication_scan` 都在**自己的会话**里 `commit()`（`db_session_factory`），
外层事务回滚夹具接不住这些提交。因此本文件：

- 硬性要求 `ANYU_TEST_DATABASE_URL`，未设直接 fail（不 skip：跳过等于假装绿，
  而未设变量时默认库是开发库 `anyu`，写进去就是污染演示数据）；
- 把调度器模块级 `db_session_factory` monkeypatch 成本用例自建的工厂，
  保证即便进程里还有别的库配置，也不会打到开发库；
- 自己在 teardown 里按 `elder_id` 逆向清理（notification → work_order → attempt →
  event → plan → assignment → elder → staff）。
"""

from __future__ import annotations

import datetime
import os
import uuid
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

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
from app.scheduler import medication as medication_scheduler
from app.services import medication_service

TEST_DB_ENV = "ANYU_TEST_DATABASE_URL"
SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")

#: 5 条药单的时刻：覆盖早/午/晚三个时段，且两两相邻 30 分钟，便于断言「同日 5 条事件」。
SLOTS: tuple[tuple[int, int], ...] = ((8, 0), (8, 30), (12, 0), (12, 30), (20, 0))
DRUGS: tuple[tuple[str, str], ...] = (
    ("二甲双胍", "0.5g 1 片"),
    ("阿卡波糖", "50mg 1 片"),
    ("氨氯地平", "5mg 1 片"),
    ("阿托伐他汀", "20mg 1 片"),
    ("碳酸钙D3", "600mg 1 片"),
)


def _test_database_url() -> str:
    """取独立测试库地址；没设就 fail（理由见模块 docstring）。"""
    url = os.environ.get(TEST_DB_ENV, "").strip()
    if not url:
        pytest.fail(
            f"{TEST_DB_ENV} 未设置：本用例会对用药事件与工单真实 commit，"
            "不设变量时默认库是开发库 anyu。请指向独立测试库后重跑。",
            pytrace=False,
        )
    return url


def _as_utc(day: datetime.date, hour: int, minute: int, *, seconds: int = 0) -> datetime.datetime:
    """业务自然日 + 上海时刻 → UTC（与 `app/scheduler/medication.scheduled_at_for` 同口径）。"""
    local = datetime.datetime.combine(day, datetime.time(hour, minute, seconds), tzinfo=SHANGHAI)
    return local.astimezone(datetime.UTC)


def _at(base: datetime.datetime, *, minutes: int = 0, seconds: int = 0) -> datetime.datetime:
    return base + datetime.timedelta(minutes=minutes, seconds=seconds)


@dataclass(frozen=True, slots=True)
class Seeded:
    """一条用例自己的种子：一个医生、一位长者、一条主责护工关系。"""

    doctor_id: uuid.UUID
    elder_id: uuid.UUID
    elder_name: str


@dataclass(slots=True)
class Database:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Database]:
    """自建测试库引擎，并把调度器的模块级会话工厂换过来。"""
    engine = create_async_engine(_test_database_url(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # 调度器/服务在模块级 `from app.db import session_factory as db_session_factory`；
    # 那条引用指向进程启动时的默认库，必须换成本测试库的工厂。
    monkeypatch.setattr(medication_scheduler, "db_session_factory", factory)
    try:
        yield Database(engine=engine, factory=factory)
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded(db: Database) -> AsyncIterator[Seeded]:
    """插医生 + 长者 + 主责护工关系；用例结束后按 elder_id 逆向清理。"""
    doctor_id = uuid.uuid4()
    elder_id = uuid.uuid4()
    # 演示库里可能已有同名长者；姓名带随机后缀，避免导入时姓名撞成「对应多位长者」。
    elder_name = f"端到端长者{uuid.uuid4().hex[:6]}"

    async with db.factory() as session:
        session.add_all(
            [
                StaffAccount(
                    staff_id=doctor_id,
                    name="端到端医生",
                    role="doctor",
                    password_hash="x",  # pragma: allowlist secret - 测试占位，不参与登录
                ),
                Elder(elder_id=elder_id, name=elder_name, status="active"),
            ]
        )
        # 先 flush 出两张父表行，再插关系：ORM 没有 relationship 声明，
        # 同一批 add 时 SQLAlchemy 排不出 elder_staff_assignments 的外键顺序。
        await session.flush()
        session.add(
            ElderStaffAssignment(elder_id=elder_id, staff_id=doctor_id, relation="primary_care")
        )
        await session.commit()

    try:
        yield Seeded(doctor_id=doctor_id, elder_id=elder_id, elder_name=elder_name)
    finally:
        # 逆向清理：外键顺序 notification → work_order → attempt → event → plan →
        # assignment → elder → staff。只删本用例的 elder_id/doctor_id，不碰别的数据。
        async with db.factory() as session:
            event_ids = (
                (
                    await session.execute(
                        select(MedicationEvent.reminder_id).where(
                            MedicationEvent.elder_id == elder_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(delete(Notification).where(Notification.elder_id == elder_id))
            await session.execute(delete(WorkOrder).where(WorkOrder.elder_id == elder_id))
            if event_ids:
                await session.execute(
                    delete(MedicationAttempt).where(MedicationAttempt.event_id.in_(list(event_ids)))
                )
            await session.execute(
                delete(MedicationEvent).where(MedicationEvent.elder_id == elder_id)
            )
            await session.execute(delete(MedicationPlan).where(MedicationPlan.elder_id == elder_id))
            await session.execute(
                delete(ElderStaffAssignment).where(ElderStaffAssignment.elder_id == elder_id)
            )
            await session.execute(delete(Elder).where(Elder.elder_id == elder_id))
            await session.execute(delete(StaffAccount).where(StaffAccount.staff_id == doctor_id))
            await session.commit()


def _csv_payload(seeded: Seeded, day: datetime.date) -> bytes:
    """5 行 CSV：用 elder_id 列定位长者，避免姓名匹配的歧义分支。"""
    lines = ["elder_id,drug_name,dose,schedule_cron,start_date,end_date"]
    for (hour, minute), (drug, dose) in zip(SLOTS, DRUGS, strict=True):
        lines.append(f"{seeded.elder_id},{drug},{dose},{hour:02d}:{minute:02d},{day.isoformat()},")
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _import_plans(db: Database, seeded: Seeded, day: datetime.date) -> list[uuid.UUID]:
    """走真实的「预检 + 落库」导入路径建 5 条药单，返回 plan_id 列表。"""
    async with db.factory() as session:
        preview = await medication_service.preview_import(
            session, payload=_csv_payload(seeded, day)
        )
        assert preview.accepted, [issue.message for issue in preview.issues]
        result = await medication_service.apply_import(
            session, preview=preview, created_by=seeded.doctor_id
        )
        await session.commit()
        assert result.accepted
        assert len(result.created) == len(SLOTS)
        return [item.plan.plan_id for item in result.created]


async def _events_of(db: Database, *, elder_id: uuid.UUID) -> list[MedicationEvent]:
    async with db.factory() as session:
        return list(
            (
                await session.execute(
                    select(MedicationEvent)
                    .where(MedicationEvent.elder_id == elder_id)
                    .order_by(MedicationEvent.scheduled_at)
                )
            )
            .scalars()
            .all()
        )


async def _attempts_of(db: Database, *, reminder_id: uuid.UUID) -> list[MedicationAttempt]:
    async with db.factory() as session:
        return list(
            (
                await session.execute(
                    select(MedicationAttempt)
                    .where(MedicationAttempt.event_id == reminder_id)
                    .order_by(MedicationAttempt.attempt_no)
                )
            )
            .scalars()
            .all()
        )


async def _work_orders_of(db: Database, *, elder_id: uuid.UUID) -> list[WorkOrder]:
    async with db.factory() as session:
        return list(
            (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == elder_id)))
            .scalars()
            .all()
        )


async def _notifications_of(db: Database, *, elder_id: uuid.UUID) -> list[Notification]:
    async with db.factory() as session:
        return list(
            (await session.execute(select(Notification).where(Notification.elder_id == elder_id)))
            .scalars()
            .all()
        )


async def _touch_round(
    db: Database,
    *,
    now: datetime.datetime,
    delivered: list[tuple[uuid.UUID, int]],
) -> int:
    """跑到点扫描一轮；`deliver` 只记录本次出站，不伪造送达通道之外的事情。"""

    async def deliver(target: medication_scheduler.MedicationTouchTarget) -> str:
        delivered.append((target.reminder_id, target.attempt_no))
        return "tts_finished"

    return await medication_scheduler.run_medication_due_scan(now=now, deliver=deliver)


# ---------------------------------------------------------------------------
# 1. 导入 → 当天 5 条事件（验收第 1 条）
# ---------------------------------------------------------------------------


async def test_import_then_scan_creates_todays_five_events(db: Database, seeded: Seeded) -> None:
    """导入 5 条药单，按 5 个服药时刻各扫一次 → 当天恰好 5 条事件，时刻一一对应。"""
    day = datetime.datetime.now(SHANGHAI).date()
    plan_ids = await _import_plans(db, seeded, day)
    assert len(plan_ids) == 5

    # 每次扫描的 now 是「该时刻前 5 分钟」：scan 的窗口是 [now, now+5min]。
    created_total = 0
    for hour, minute in SLOTS:
        slot = _as_utc(day, hour, minute)
        created = await medication_scheduler.run_medication_scan(
            now=_at(slot, minutes=-5), session_factory=db.factory
        )
        assert created == 1, f"{hour:02d}:{minute:02d} 这一轮应新建 1 条事件"
        created_total += created

    assert created_total == len(SLOTS)

    events = await _events_of(db, elder_id=seeded.elder_id)
    assert len(events) == len(SLOTS)
    assert {event.plan_id for event in events} == set(plan_ids)
    assert [event.scheduled_at for event in events] == [
        _as_utc(day, hour, minute) for hour, minute in SLOTS
    ]
    assert all(event.status == "calling" for event in events)
    assert all(event.triggered_by == "scheduler" for event in events)

    # 再扫一遍同一时刻：幂等，不再新增（ensure_event 的同键复用）。
    again = await medication_scheduler.run_medication_scan(
        now=_at(_as_utc(day, 8, 0), minutes=-5), session_factory=db.factory
    )
    assert again == 0


# ---------------------------------------------------------------------------
# 2. 四轮触达 → 漏服工单（验收第 2、6 条）
# ---------------------------------------------------------------------------


async def test_four_rounds_then_work_order(db: Database, seeded: Seeded) -> None:
    """T0 起四枪递增，T0+16min 升级为红线工单并派给主责护工。"""
    day = datetime.datetime.now(SHANGHAI).date()
    await _import_plans(db, seeded, day)
    for hour, minute in SLOTS:
        await medication_scheduler.run_medication_scan(
            now=_at(_as_utc(day, hour, minute), minutes=-5), session_factory=db.factory
        )

    events = await _events_of(db, elder_id=seeded.elder_id)
    target = next(event for event in events if event.scheduled_at == _as_utc(day, 8, 0))
    reminder_id = target.reminder_id
    s0 = _as_utc(day, 8, 0)

    delivered: list[tuple[uuid.UUID, int]] = []

    # 第 1 枪：T0 到点。
    handled = await _touch_round(db, now=s0, delivered=delivered)
    assert handled == 1
    assert delivered == [(reminder_id, 1)]

    # 60 秒窗关闭，但下一枪的节拍档位是 +5 分钟：这一轮只应 wait，不产生第 2 枪。
    handled = await _touch_round(db, now=_at(s0, seconds=60), delivered=delivered)
    assert handled == 0, "60 秒窗不等于下一枪的到点时刻：此轮必须只 wait"
    assert delivered == [(reminder_id, 1)]

    # 第 2/3/4 枪：+5 / +10 / +15 分钟。
    for minutes, expected_attempt in ((5, 2), (10, 3), (15, 4)):
        handled = await _touch_round(db, now=_at(s0, minutes=minutes), delivered=delivered)
        assert handled == 1, f"T0+{minutes}min 应推进一条事件"
        assert delivered[-1] == (reminder_id, expected_attempt)

    attempts = await _attempts_of(db, reminder_id=reminder_id)
    assert [attempt.attempt_no for attempt in attempts] == [1, 2, 3, 4]
    assert [attempt.sent_at for attempt in attempts] == [
        s0,
        _at(s0, minutes=5),
        _at(s0, minutes=10),
        _at(s0, minutes=15),
    ]
    assert all(attempt.receipt == "tts_finished" for attempt in attempts)

    # 最后一枪的 60 秒窗关闭（T0+15min+60s = T0+16min）：升级，不再补发第 5 枪。
    handled = await _touch_round(db, now=_at(s0, minutes=16), delivered=delivered)
    assert handled == 1
    assert len(delivered) == 4, "升级轮不应再出站一次触达"

    events = await _events_of(db, elder_id=seeded.elder_id)
    escalated = next(event for event in events if event.reminder_id == reminder_id)
    assert escalated.status == "escalated"
    assert escalated.terminal_state == "missed_unconfirmed"
    assert escalated.retry_count == 3

    orders = await _work_orders_of(db, elder_id=seeded.elder_id)
    assert len(orders) == 1
    order = orders[0]
    assert order.type == medication_service.ESCALATION_REASON == "missed_medication"
    assert order.severity == "red"
    assert order.status == "new"
    assert order.elder_id == seeded.elder_id
    assert order.note is not None
    assert str(reminder_id) in order.note
    assert str(target.plan_id) in order.note

    # 归属只按 elder_id + 主责护工断言：本仓库无租户/社区维度（PRD/ER 亦无）。
    notifications = await _notifications_of(db, elder_id=seeded.elder_id)
    assert len(notifications) == 1
    note = notifications[0]
    assert note.kind == "medication_escalation"
    assert note.severity == "red"
    assert note.assignee_staff_id == seeded.doctor_id
    assert note.payload is not None
    assert note.payload["reminder_id"] == str(reminder_id)
    assert note.payload["work_order_no"] == order.order_no


async def test_without_escalation_branch_no_work_order(
    db: Database, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向护栏：把「四枪打完 → 升级」这一步短路掉，工单就不该出现。

    这不是替代真实变异（真实变异见 `.tmp` 里的临时 patch 与报告），而是留在仓库里的
    永久回归护栏：只要有人把升级分支删掉/条件写反，这条用例会先红。
    """
    day = datetime.datetime.now(SHANGHAI).date()
    await _import_plans(db, seeded, day)
    for hour, minute in SLOTS:
        await medication_scheduler.run_medication_scan(
            now=_at(_as_utc(day, hour, minute), minutes=-5), session_factory=db.factory
        )

    async def _no_escalation(*, reminder_id: uuid.UUID, now: datetime.datetime) -> None:
        return None

    monkeypatch.setattr(medication_scheduler, "_escalate_event", _no_escalation)

    events = await _events_of(db, elder_id=seeded.elder_id)
    reminder_id = next(
        event.reminder_id for event in events if event.scheduled_at == _as_utc(day, 8, 0)
    )
    s0 = _as_utc(day, 8, 0)
    delivered: list[tuple[uuid.UUID, int]] = []
    for offset in (0, 5, 10, 15, 16):
        await _touch_round(db, now=_at(s0, minutes=offset), delivered=delivered)

    assert len(delivered) == 4, "四枪照旧发出，只有升级分支被短路"
    assert await _work_orders_of(db, elder_id=seeded.elder_id) == []
    events = await _events_of(db, elder_id=seeded.elder_id)
    current = next(event for event in events if event.reminder_id == reminder_id)
    assert current.status == "waiting_confirm", "没有升级就不该进 escalated 终态"


# ---------------------------------------------------------------------------
# 3. 判定边界：直接调纯函数（不写库，报告里要标明这一组不是入口驱动）
# ---------------------------------------------------------------------------


def test_decision_boundaries_of_four_attempts() -> None:
    """`decide_event_action` 的四个边界：到点、四枪打完、窗口错过、计划停用。

    **这一组是直接调判定函数，不是调度入口**：用来钉住「为什么 +15 分钟还能出第 4 枪、
    +16 分钟变成升级」的口径，避免只靠端到端用例的间接覆盖。
    """
    s0 = _as_utc(datetime.date(2026, 11, 10), 8, 0)
    kwargs = dict(
        scheduled_at=s0,
        attempts=4,
        wait_seconds=60,
        interval_minutes=5,
        plan_status="active",
    )

    first = medication_scheduler.decide_event_action(
        attempts_sent=0, last_sent_at=None, now=s0, **kwargs
    )
    assert (first.action, first.attempt_no, first.reason) == ("touch", 1, "slot_due")

    fourth = medication_scheduler.decide_event_action(
        attempts_sent=3, last_sent_at=_at(s0, minutes=10), now=_at(s0, minutes=15), **kwargs
    )
    assert (fourth.action, fourth.attempt_no, fourth.reason) == ("touch", 4, "slot_due")

    exhausted = medication_scheduler.decide_event_action(
        attempts_sent=4, last_sent_at=_at(s0, minutes=15), now=_at(s0, minutes=16), **kwargs
    )
    assert (exhausted.action, exhausted.reason) == ("escalate", "attempts_exhausted")

    missed = medication_scheduler.decide_event_action(
        attempts_sent=0, last_sent_at=None, now=_at(s0, minutes=17), **kwargs
    )
    assert (missed.action, missed.reason) == ("escalate", "window_missed")

    skipped = medication_scheduler.decide_event_action(
        attempts_sent=0, last_sent_at=None, now=s0, **{**kwargs, "plan_status": "stopped"}
    )
    assert (skipped.action, skipped.reason) == ("skip", "plan_stopped")
