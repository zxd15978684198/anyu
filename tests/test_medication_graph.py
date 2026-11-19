"""用药确认子图 + 用药服务（PRD §4.5 / FR-MED-02..07、AC-05/06/07/20，TD §3.4 + §7.1，迭代 7 · C 卡）。

覆盖四层：

1. **纯函数**（无 I/O）：快照哈希、`next_retry_at` 节拍、S-D7-C1 终态映射、线程键、
   话术、`resolve_timeout_terminal`、`parse_decision`；
2. **写库层**（真库）：`ensure_event` / `record_attempt` 的幂等三键与 409、`update_event_state`
   的「只改非 None」与终态校验、`escalate_to_work_order`；
3. **子图**（`MemorySaver` + 真库）：正常确认 / 拒服 / 四次超时升级 / 四次零回执、
   `resume_state` 取回 `attempt_no`+`next`、重启后不重发提醒；
4. **断点恢复**（真 PostgreSQL checkpointer）：写中断态 → `close_checkpointer()` →
   重新 `get_checkpointer()` → 状态仍在。

时间零依赖：`clock` 与 `wait_seconds` 全部注入（`FakeClock`），不 sleep、不真等 60 秒。

真库用例沿用 `tests/test_health_api.py` 的隔离手法：自建 `NullPool` 引擎 → 外层事务 →
用例结束回滚，主库零残留。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.graphs import checkpointer as checkpointer_mod
from app.graphs import medication_confirmation_graph as graph_mod
from app.graphs.medication_confirmation_graph import (
    DECISION_CONFIRMED,
    DECISION_REFUSED,
    DECISION_TIMEOUT,
    RTC_CONNECTED,
    RTC_DEGRADED_WS,
    DeliveryResult,
    ReminderEvent,
    build_medication_graph,
    initial_state,
    medication_thread_id,
    parse_decision,
    pending_confirmation,
    pending_interrupt,
    reminder_text,
    reset_medication_graph,
    resolve_timeout_terminal,
    resume_confirmation,
    resume_state,
    start_or_resume,
    state_config,
)
from app.models import (
    Elder,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    RiskEvent,
    Room,
    StaffAccount,
    WorkOrder,
    WorkOrderTransition,
)
from app.services import medication_service
from app.services.medication_service import (
    TERMINAL_MAP,
    MedicationEventConflict,
    MedicationValidationError,
    confirm_event,
    confirm_from_dialog,
    drug_snapshot_hash,
    ensure_event,
    escalate_to_work_order,
    next_retry_at,
    record_attempt,
    update_event_state,
)
from conftest import require_db_connection

_PASSWORD_PLACEHOLDER = "task-day7c-test-row-not-a-real-hash"

ROOM = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbb03a")
ELDER = uuid.UUID("e0000001-0000-4000-8000-00000000c001")
DOCTOR = uuid.UUID("51000000-0000-4000-8000-0000000000c1")
PLAN = uuid.UUID("9a000001-0000-4000-8000-00000000c001")
PLAN_B = uuid.UUID("9a000002-0000-4000-8000-00000000c002")

START = datetime.datetime(2026, 11, 8, 8, 0, tzinfo=datetime.UTC)
SNAPSHOT: dict[str, Any] = {"drug_name": "氨氯地平", "dose": "5mg", "schedule_cron": "08:00"}


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
def make_session(db: AsyncConnection) -> Callable[[], AsyncSession]:
    """每次返回**新** session（同一外层事务内的 savepoint）：避免身份映射读到旧值。"""

    def _make() -> AsyncSession:
        return AsyncSession(
            bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    return _make


@pytest.fixture
async def world(db: AsyncConnection) -> dict[str, uuid.UUID]:
    """一间房、一位长者、一位医生、两张在用计划（PLAN / PLAN_B）。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM, room_no="H-301", status="available")
    )
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER, name="张桂芬", status="active")
    )
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=DOCTOR,
            name="医生老吴",
            role="doctor",
            password_hash=_PASSWORD_PLACEHOLDER,
            token_version=1,
            status="active",
        )
    )
    await db.execute(
        MedicationPlan.__table__.insert(),
        [
            {
                "plan_id": PLAN,
                "elder_id": ELDER,
                "drug_name": SNAPSHOT["drug_name"],
                "dose": SNAPSHOT["dose"],
                "schedule_cron": "08:00",
                "start_date": datetime.date(2026, 11, 1),
                "version": 1,
                "created_by": DOCTOR,
                "status": "active",
            },
            {
                "plan_id": PLAN_B,
                "elder_id": ELDER,
                "drug_name": "二甲双胍",
                "dose": "0.5g",
                "schedule_cron": "08:00",
                "start_date": datetime.date(2026, 11, 1),
                "version": 1,
                "created_by": DOCTOR,
                "status": "active",
            },
        ],
    )
    return {"room": ROOM, "elder": ELDER, "doctor": DOCTOR, "plan": PLAN, "plan_b": PLAN_B}


# ------------------------------------------------------------------ 假时钟与假投递


class FakeClock:
    """可推进的 UTC 时钟：图里所有「现在」都走它，测试不真等。"""

    def __init__(self, start: datetime.datetime) -> None:
        self.now = start

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, *, seconds: float = 0, minutes: float = 0) -> None:
        self.now = self.now + datetime.timedelta(seconds=seconds, minutes=minutes)


@dataclass
class Recorder:
    """假投递：记下每次 (event, text)，按队列返回预设结果（空队列则回「卡片已渲染」）。"""

    results: list[DeliveryResult] = field(default_factory=list)
    events: list[ReminderEvent] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    async def __call__(self, event: ReminderEvent, text: str) -> DeliveryResult:
        self.events.append(event)
        self.texts.append(text)
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(receipt="card_rendered", tts_result="ok", rtc_status=RTC_CONNECTED)


async def _build_graph(
    make_session: Callable[[], AsyncSession],
    *,
    clock: FakeClock,
    deliver: Recorder,
    wait_seconds: int = 60,
    checkpointer: Any | None = None,
) -> Any:
    return await build_medication_graph(
        checkpointer=checkpointer or MemorySaver(),
        deliver=deliver,
        session_factory=make_session,
        clock=clock,
        wait_seconds=wait_seconds,
    )


async def _seed_event(
    make_session: Callable[[], AsyncSession],
    *,
    plan_id: uuid.UUID = PLAN,
    scheduled_at: datetime.datetime = START,
) -> uuid.UUID:
    """调度器/API 先用 `ensure_event` 建事件，再把 `reminder_id` 交给图（图本身不建事件）。"""
    async with make_session() as session:
        event, created = await ensure_event(
            session,
            plan_id=plan_id,
            elder_id=ELDER,
            scheduled_at=scheduled_at,
            drug_snapshot=SNAPSHOT,
        )
        assert created is True
        await session.commit()
        return event.reminder_id


def _state(*, thread: str, reminder_id: uuid.UUID, plan_id: uuid.UUID = PLAN) -> dict[str, Any]:
    return initial_state(
        thread_id=thread,
        elder_id=str(ELDER),
        reminder_id=str(reminder_id),
        plan_id=str(plan_id),
        scheduled_at=START,
        drug_snapshot=SNAPSHOT,
    )


# ------------------------------------------------------------------ 查询辅助（每次新 session）


async def _event(
    make_session: Callable[[], AsyncSession], reminder_id: uuid.UUID
) -> MedicationEvent:
    async with make_session() as session:
        result = await session.execute(
            select(MedicationEvent).where(MedicationEvent.reminder_id == reminder_id)
        )
        return result.scalar_one()


async def _attempts(
    make_session: Callable[[], AsyncSession], reminder_id: uuid.UUID
) -> list[MedicationAttempt]:
    async with make_session() as session:
        result = await session.execute(
            select(MedicationAttempt)
            .where(MedicationAttempt.event_id == reminder_id)
            .order_by(MedicationAttempt.attempt_no)
        )
        return list(result.scalars())


async def _orders(make_session: Callable[[], AsyncSession]) -> list[WorkOrder]:
    """本用例长者名下的工单。

    **必须按 ELDER 过滤**：开发库里跑过演示数据、容器内的调度器还会真的派单，
    断言全表条数（原先无 where）会在有演示数据时假红；这条用例要证的是
    「本长者只多出一张单」，不是「全库只有一张单」。
    """
    async with make_session() as session:
        result = await session.execute(
            select(WorkOrder).where(WorkOrder.elder_id == ELDER).order_by(WorkOrder.created_at)
        )
        return list(result.scalars())


async def _risk_events(make_session: Callable[[], AsyncSession]) -> list[RiskEvent]:
    """本用例长者名下的风险事件（同样按 ELDER 过滤，理由见 `_orders`）。"""
    async with make_session() as session:
        result = await session.execute(
            select(RiskEvent).where(RiskEvent.elder_id == ELDER).order_by(RiskEvent.created_at)
        )
        return list(result.scalars())


async def _transitions(
    make_session: Callable[[], AsyncSession],
    *,
    order_id: uuid.UUID | None = None,
) -> list[WorkOrderTransition]:
    """工单状态转换记录；给了 `order_id` 就只看这一张单。

    为什么要能过滤：开发库里本来就躺着他单的转换历史（2026-11-16 实测：全表有数据），
    「本单没有写记录」这类断言在裸 `select` 上必然红 —— 那不是业务回归，是测试没隔离。
    """
    async with make_session() as session:
        stmt = select(WorkOrderTransition)
        if order_id is not None:
            stmt = stmt.where(WorkOrderTransition.order_id == order_id)
        result = await session.execute(stmt)
        return list(result.scalars())


# ------------------------------------------------------------------ 1. 纯函数


def test_drug_snapshot_hash_is_order_insensitive_and_utf8_stable() -> None:
    a = drug_snapshot_hash({"drug_name": "氨氯地平", "dose": "5mg"})
    b = drug_snapshot_hash({"dose": "5mg", "drug_name": "氨氯地平"})
    c = drug_snapshot_hash({"drug_name": "氨氯地平", "dose": "10mg"})
    assert a == b, "键序不同必须算出同一个哈希（否则幂等会误报 409）"
    assert a != c
    assert len(a) == 64 and all(ch in "0123456789abcdef" for ch in a)


def test_drug_snapshot_hash_rejects_non_mapping() -> None:
    with pytest.raises(ValueError):
        drug_snapshot_hash(["氨氯地平"])  # type: ignore[arg-type]


@pytest.mark.parametrize(("attempt_no", "expected_minutes"), [(1, 5), (2, 10), (3, 15), (4, 20)])
def test_next_retry_at_follows_five_minute_beats(attempt_no: int, expected_minutes: int) -> None:
    got = next_retry_at(START, attempt_no, 60)
    assert got == START + datetime.timedelta(minutes=expected_minutes)


def test_next_retry_at_never_undercuts_the_wait_window() -> None:
    # 等待窗 15 分钟 > T0 的 5 分钟档位 → 下一次只能在窗口结束后
    got = next_retry_at(START, 1, 900)
    assert got == START + datetime.timedelta(minutes=15)


def test_next_retry_at_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        next_retry_at(START, 0, 60)
    with pytest.raises(ValueError):
        next_retry_at(START, 1, -1)


def test_terminal_map_collapses_six_plan_states_into_four_stored_values() -> None:
    """S-D7-C1：计划 §W2 六终态 → TD §3.4 四值（cancelled/superseded 不写 terminal_state）。"""
    assert TERMINAL_MAP == {
        "confirmed": "taken_reported",
        "refused": "refused",
        "no_response": "missed_unconfirmed",
        "escalated": "missed_unconfirmed",
        "cancelled": None,
        "superseded": None,
    }
    assert medication_service.terminal_state_for("cancelled") is None
    with pytest.raises(ValueError):
        medication_service.terminal_state_for("taken")


def test_medication_thread_id_is_minute_grain_and_utc() -> None:
    shifted = START.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    a = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    b = medication_thread_id(
        elder_id=ELDER, plan_id=PLAN, scheduled_at=START + datetime.timedelta(seconds=37)
    )
    c = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=shifted)
    assert a == b == c, "同一到点时刻（含秒/毫秒与时区差异）必须落在同一条线程"
    assert a == f"medication:{ELDER}:{PLAN}:2026-11-08T08:00"


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (
            ["card_rendered", "card_rendered", "card_rendered", "card_rendered"],
            "missed_unconfirmed",
        ),
        (["none", "tts_finished", "none", "none"], "missed_unconfirmed"),
        (["none", "none", "none", "none"], "notification_failed"),
        ([], "notification_failed"),
    ],
)
def test_resolve_timeout_terminal_splits_on_receipts(flags: list[str], expected: str) -> None:
    assert resolve_timeout_terminal(flags) == expected


def test_reminder_text_uses_attempt_template_and_drug_label() -> None:
    event = ReminderEvent(
        elder_id=str(ELDER),
        reminder_id="r",
        plan_id=str(PLAN),
        attempt_no=1,
        scheduled_at=START.isoformat(),
        drug_snapshot=SNAPSHOT,
    )
    assert "氨氯地平（5mg）" in reminder_text(event)
    assert "最后一次提醒" in reminder_text(replace(event, attempt_no=4))
    # 超出模板数量时复用最后一条（不把内部上限暴露给长者）
    assert reminder_text(replace(event, attempt_no=9)) == reminder_text(
        replace(event, attempt_no=4)
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"decision": "confirmed", "channel": "button", "response": "吃了"},
            ("confirmed", "button", "吃了"),
        ),
        ("吃了", ("confirmed", None, None)),
        ({"decision": "refused"}, ("refused", None, None)),
        ("timeout", ("timeout", None, None)),
        (None, ("timeout", None, None)),
        ({"decision": "confirmed", "channel": "carrier-pigeon"}, ("confirmed", None, None)),
    ],
)
def test_parse_decision_normalizes_payloads(
    payload: object, expected: tuple[str, str | None, str | None]
) -> None:
    assert parse_decision(payload) == expected


def test_parse_decision_rejects_unknown_words() -> None:
    with pytest.raises(ValueError):
        parse_decision({"decision": "maybe"})


def test_delivery_result_validates_enums() -> None:
    assert DeliveryResult(receipt="card_rendered").receipt == "card_rendered"
    with pytest.raises(ValueError):
        DeliveryResult(receipt="done")
    with pytest.raises(ValueError):
        DeliveryResult(rtc_status="carrier-pigeon")


def test_initial_state_carries_settings_derived_limits() -> None:
    state = initial_state(
        thread_id="t",
        elder_id=ELDER,
        reminder_id=uuid.uuid4(),
        plan_id=PLAN,
        scheduled_at=START,
        drug_snapshot=SNAPSHOT,
    )
    settings = get_settings()
    assert state["max_attempts"] == settings.medication_reminder_attempts == 4
    assert settings.medication_attempt_wait_seconds == 60
    assert state["attempt_no"] == 1 and state["next_action"] == "remind"


# ------------------------------------------------------------------ 2. 写库层（真库）


async def test_ensure_event_is_idempotent_on_same_payload(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """④ 同键同内容 → 返回原事件 + False，库里只有一行。"""
    async with make_session() as session:
        first, created_first = await ensure_event(
            session,
            plan_id=PLAN,
            elder_id=ELDER,
            scheduled_at=START,
            drug_snapshot=SNAPSHOT,
        )
        await session.commit()
        second, created_second = await ensure_event(
            session,
            plan_id=PLAN,
            elder_id=ELDER,
            scheduled_at=START,
            drug_snapshot=dict(reversed(list(SNAPSHOT.items()))),  # 键序不同 = 同一份药单
        )
        await session.commit()

    assert created_first is True
    assert created_second is False
    assert first.reminder_id == second.reminder_id
    assert first.payload_hash == drug_snapshot_hash(SNAPSHOT)

    async with make_session() as session:
        count = await session.scalar(
            select(func.count()).select_from(MedicationEvent).where(MedicationEvent.plan_id == PLAN)
        )
    assert count == 1


async def test_ensure_event_raises_conflict_on_changed_payload(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """④ 同键异内容 → `MedicationEventConflict`（API 映射 409），且不落第二行。"""
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        changed = {**SNAPSHOT, "dose": "10mg"}
        with pytest.raises(MedicationEventConflict):
            await ensure_event(
                session,
                plan_id=PLAN,
                elder_id=ELDER,
                scheduled_at=START,
                drug_snapshot=changed,
            )
        await session.commit()

    async with make_session() as session:
        # 只数本用例这张药单的事件：开发库里调度器每 5 分钟会写别人的事件，
        # 断言全表计数（原先的 select(func.count()).select_from(MedicationEvent)）
        # 会被演示数据打红，而这条用例要证的只是「冲突后没多插一行」。
        count = await session.scalar(
            select(func.count()).select_from(MedicationEvent).where(MedicationEvent.plan_id == PLAN)
        )
    assert count == 1
    assert (await _event(make_session, event.reminder_id)).terminal_state is None


async def test_ensure_event_allows_same_moment_for_different_plans(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        a, created_a = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        b, created_b = await ensure_event(
            session, plan_id=PLAN_B, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
    assert created_a and created_b
    assert a.reminder_id != b.reminder_id


async def test_ensure_event_rejects_unknown_trigger(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        with pytest.raises(ValueError):
            await ensure_event(
                session,
                plan_id=PLAN,
                elder_id=ELDER,
                scheduled_at=START,
                drug_snapshot=SNAPSHOT,
                triggered_by="cron",
            )


async def test_record_attempt_is_idempotent_per_attempt_no(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """⑤ 触达幂等键 `(event_id, attempt_no)`：重复调用返回同一行。"""
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()

        first = await record_attempt(
            session,
            event_id=event.reminder_id,
            attempt_no=1,
            sent_at=START,
            receipt="card_rendered",
            tts_result="ok",
        )
        await session.commit()
        again = await record_attempt(
            session,
            event_id=event.reminder_id,
            attempt_no=1,
            sent_at=START,
            receipt="card_rendered",
        )
        await session.commit()

    assert first.attempt_id == again.attempt_id
    assert len(await _attempts(make_session, event.reminder_id)) == 1


async def test_record_attempt_backfills_response_without_overwriting(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        await record_attempt(
            session,
            event_id=event.reminder_id,
            attempt_no=2,
            sent_at=START,
            receipt="card_rendered",
        )
        await session.commit()
        await record_attempt(
            session,
            event_id=event.reminder_id,
            attempt_no=2,
            sent_at=START,
            response="我吃过了",
            response_channel="voice",
        )
        await session.commit()

    rows = await _attempts(make_session, event.reminder_id)
    assert len(rows) == 1
    assert rows[0].receipt == "card_rendered", "补 response 时不能清掉已有回执"
    assert rows[0].response == "我吃过了"
    assert rows[0].response_channel == "voice"


@pytest.mark.parametrize("attempt_no", [0, 5, -1])
async def test_record_attempt_rejects_out_of_range_attempt_no(
    world: dict[str, uuid.UUID],
    make_session: Callable[[], AsyncSession],
    attempt_no: int,
) -> None:
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        with pytest.raises(ValueError):
            await record_attempt(
                session, event_id=event.reminder_id, attempt_no=attempt_no, sent_at=START
            )


async def test_update_event_state_touches_only_given_fields(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        await update_event_state(
            session, reminder_id=event.reminder_id, status="waiting_confirm", retry_count=2
        )
        await session.commit()

    stored = await _event(make_session, event.reminder_id)
    assert stored.status == "waiting_confirm"
    assert stored.retry_count == 2
    assert stored.terminal_state is None, "没传 terminal_state 就不能写终态"
    assert stored.confirmed_at is None and stored.confirm_channel is None


async def test_update_event_state_requires_terminal_capable_status(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        with pytest.raises(ValueError):
            # 还在 calling 就写终态 = 自相矛盾行
            await update_event_state(
                session, reminder_id=event.reminder_id, terminal_state="taken_reported"
            )


async def test_update_event_state_writes_confirmation(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        await update_event_state(
            session,
            reminder_id=event.reminder_id,
            status="confirmed",
            terminal_state="taken_reported",
            confirmed_at=START + datetime.timedelta(minutes=1),
            confirm_channel="button",
            late_taken=True,
            graph_thread_id="medication:x",
        )
        await session.commit()

    stored = await _event(make_session, event.reminder_id)
    assert stored.status == "confirmed"
    assert stored.terminal_state == "taken_reported"
    assert stored.confirmed_at == START + datetime.timedelta(minutes=1)
    assert stored.confirm_channel == "button"
    assert stored.late_taken is True
    assert stored.graph_thread_id == "medication:x"


async def test_update_event_state_rejects_unknown_values(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        with pytest.raises(ValueError):
            await update_event_state(session, reminder_id=event.reminder_id, status="taken")
        with pytest.raises(ValueError):
            await update_event_state(session, reminder_id=event.reminder_id, retry_count=4)


async def test_update_event_state_missing_event_raises(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        with pytest.raises(medication_service.MedicationNotFoundError):
            await update_event_state(session, reminder_id=uuid.uuid4(), status="waiting_confirm")


# ------------------------------------------------------------------ 3. 升级工单


async def test_escalate_creates_red_work_order_and_terminalizes_event(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """③（服务级）四次触达无有效回应 → 红线工单 + 事件 escalated/missed_unconfirmed。"""
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        for attempt_no in range(1, 5):
            await record_attempt(
                session,
                event_id=event.reminder_id,
                attempt_no=attempt_no,
                sent_at=START + datetime.timedelta(minutes=5 * (attempt_no - 1)),
                receipt="card_rendered",
            )
        await session.commit()
        order = await escalate_to_work_order(session, event=event)
        await session.commit()

    assert order.type == "missed_medication"
    assert order.severity == "red"
    assert order.status == "new", "红线工单必须留在照护台「待接手」队列，不能被自造成已接手"
    assert order.elder_id == ELDER
    assert order.order_no.startswith("WO-")
    head, _, tail = order.order_no.partition("-")
    assert head == "WO" and len(tail.split("-")[0]) == 8, order.order_no
    assert tail.split("-")[-1].isdigit() and len(tail.split("-")[-1]) == 4, order.order_no
    assert str(event.reminder_id) in (order.note or ""), "备注要能回溯到具体提醒"

    stored = await _event(make_session, event.reminder_id)
    assert stored.status == "escalated"
    assert stored.terminal_state == "missed_unconfirmed"
    assert len(await _attempts(make_session, event.reminder_id)) == 4


async def test_escalation_writes_no_transition_because_schema_forbids_escalated(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """S-D7-C2（偏离登记）：任务卡要求 `new → escalated`，但该表两列都限死六态。

    这条用例是**证据**：直接在库里插 `to_status='escalated'` 必被
    `ck_work_order_transitions_to_status` 拒绝，所以服务层没有写流转；
    工单状态没变（`new`），升级事实记在 `note` + 事件终态上。
    迁移把 `'escalated'` 纳入后，此用例会红——那正是「该补 transition 了」的信号。
    """
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        order = await escalate_to_work_order(session, event=event)
        await session.commit()

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    WorkOrderTransition(
                        order_id=order.order_id,
                        from_status="new",
                        to_status="escalated",
                        note="任务卡要求的取值，schema 不允许",
                    )
                )

    assert await _transitions(make_session, order_id=order.order_id) == []


async def test_order_no_sequence_increments_within_the_day(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    numbers = []
    for plan_id in (PLAN, PLAN_B):
        async with make_session() as session:
            event, _ = await ensure_event(
                session,
                plan_id=plan_id,
                elder_id=ELDER,
                scheduled_at=START + datetime.timedelta(minutes=1),
                drug_snapshot=SNAPSHOT,
            )
            await session.commit()
            order = await escalate_to_work_order(session, event=event)
            await session.commit()
            numbers.append(order.order_no)

    assert len(set(numbers)) == 2
    assert numbers[1].endswith(f"{int(numbers[0].rsplit('-', 1)[1]) + 1:04d}")


async def test_escalate_rejects_unknown_reason(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    async with make_session() as session:
        event, _ = await ensure_event(
            session, plan_id=PLAN, elder_id=ELDER, scheduled_at=START, drug_snapshot=SNAPSHOT
        )
        await session.commit()
        with pytest.raises(ValueError):
            await escalate_to_work_order(session, event=event, reason="medication_missed")


# ------------------------------------------------------------------ 4. 子图：正常确认 / 拒服


async def test_graph_confirmed_writes_taken_reported(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """① 触达一次即确认 → `taken_reported`，且不建工单（AC-05）。"""
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder, wait_seconds=90)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    started = await graph.ainvoke(state, state_config(thread))
    prompt = pending_interrupt(started)
    assert prompt is not None
    assert prompt["type"] == "medication_confirm"
    assert prompt["attempt_no"] == 1
    assert prompt["max_attempts"] == 4
    # wait_seconds 注入生效（不是硬编码 60）
    assert datetime.datetime.fromisoformat(prompt["deadline"]) == START + datetime.timedelta(
        seconds=90
    )
    assert prompt["prompt"].startswith("您吃了吗")

    clock.advance(minutes=1)
    final = await graph.ainvoke(
        Command(resume={"decision": "confirmed", "channel": "button", "response": "吃了"}),
        state_config(thread),
    )
    assert final["decision"] == DECISION_CONFIRMED
    assert final["terminal_state"] == "taken_reported"
    assert final["status"] == "confirmed"
    assert final["finished"] is True
    assert pending_interrupt(final) is None

    stored = await _event(make_session, reminder_id)
    assert stored.status == "confirmed"
    assert stored.terminal_state == "taken_reported"
    assert stored.confirm_channel == "button"
    assert stored.confirmed_at == START + datetime.timedelta(minutes=1)
    assert stored.late_taken is False, "第一次触达就确认不算迟服"
    assert stored.graph_thread_id == thread
    assert stored.retry_count == 0
    attempts = await _attempts(make_session, reminder_id)
    assert len(attempts) == 1
    assert attempts[0].response_channel == "button"
    assert attempts[0].response == "吃了"
    assert await _orders(make_session) == []


async def test_graph_refusal_terminates_as_refused_and_stops_retrying(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """② 明确拒服 → 停止重试、终态 `refused`、建黄色风险事件 + 护工跟进工单（AC-07）。

    PRD line 321：「拒服 | `refused` | 立即停止重试，**生成风险事件+护工跟进工单**」。
    """
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await graph.ainvoke(state, state_config(thread))
    clock.advance(seconds=20)
    final = await graph.ainvoke(
        Command(resume={"decision": "refused", "channel": "voice", "response": "不想吃"}),
        state_config(thread),
    )

    assert final["decision"] == DECISION_REFUSED
    assert final["terminal_state"] == "refused"
    assert final["status"] == "escalated"
    assert len(recorder.events) == 1, "拒服后不得再触达"

    stored = await _event(make_session, reminder_id)
    assert stored.terminal_state == "refused"
    assert stored.status == "escalated"
    assert stored.failure_reason == "denied"
    assert stored.confirm_channel == "voice"
    assert stored.confirmed_at is None

    # AC-07 第二半句：风险事件 + 跟进工单，两样都得有。
    risk = await _risk_events(make_session)
    assert len(risk) == 1
    assert risk[0].type == "med_risk"
    assert risk[0].severity == "yellow"
    assert risk[0].status == "open"
    assert risk[0].source_type == "medication_events"
    assert risk[0].source_id == reminder_id

    orders = await _orders(make_session)
    assert len(orders) == 1
    assert orders[0].type == "follow_up"
    assert orders[0].severity == "yellow"
    assert orders[0].status == "new"
    assert orders[0].event_id == risk[0].event_id
    assert orders[0].elder_id == ELDER


async def test_graph_late_confirmation_marks_late_taken(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """AC-08：+5 分钟那次才确认 → `late_taken=true`，终态仍是 `taken_reported`。"""
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await graph.ainvoke(state, state_config(thread))
    clock.advance(minutes=5)
    await graph.ainvoke(Command(resume={"decision": "timeout"}), state_config(thread))
    clock.advance(minutes=1)
    final = await graph.ainvoke(
        Command(resume={"decision": "confirmed", "channel": "voice"}), state_config(thread)
    )

    assert final["attempt_no"] == 2
    assert final["terminal_state"] == "taken_reported"
    stored = await _event(make_session, reminder_id)
    assert stored.late_taken is True
    assert stored.status == "confirmed"


# ------------------------------------------------------------------ 5. 子图：四次超时升级


async def test_graph_four_timeouts_create_work_order(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """③ 四次超时 → 4 行 attempts + 红线工单 + 事件 escalated/missed_unconfirmed（AC-06）。"""
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await graph.ainvoke(state, state_config(thread))
    for expected_attempt in (2, 3, 4):
        clock.advance(minutes=5)
        suspended = await graph.ainvoke(
            Command(resume={"decision": "timeout"}), state_config(thread)
        )
        prompt = pending_interrupt(suspended)
        assert prompt is not None and prompt["attempt_no"] == expected_attempt

    clock.advance(minutes=5)
    final = await graph.ainvoke(Command(resume={"decision": "timeout"}), state_config(thread))

    assert final["decision"] == DECISION_TIMEOUT
    assert final["status"] == "escalated"
    assert final["terminal_state"] == "missed_unconfirmed"
    assert final["escalated"] is True
    assert final["order_no"], "升级节点必须回报单号"
    assert len(recorder.events) == 4, "四次触达，一次不多一次不少"

    attempts = await _attempts(make_session, reminder_id)
    assert [row.attempt_no for row in attempts] == [1, 2, 3, 4]

    stored = await _event(make_session, reminder_id)
    assert stored.status == "escalated"
    assert stored.terminal_state == "missed_unconfirmed"
    assert stored.failure_reason == "no_answer"

    orders = await _orders(make_session)
    assert len(orders) == 1
    assert orders[0].order_no == final["order_no"]
    assert orders[0].type == "missed_medication"
    assert orders[0].severity == "red"
    assert orders[0].status == "new"


async def test_graph_zero_receipts_ends_as_notification_failed(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """AC-20：四次都没有页面回执 → `notification_failed`（没送达，不是漏服）。"""
    clock = FakeClock(START)
    recorder = Recorder(
        results=[DeliveryResult(receipt="none", rtc_status=RTC_DEGRADED_WS) for _ in range(4)]
    )
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await graph.ainvoke(state, state_config(thread))
    for _ in range(3):
        clock.advance(minutes=5)
        await graph.ainvoke(Command(resume={"decision": "timeout"}), state_config(thread))
    clock.advance(minutes=5)
    final = await graph.ainvoke(Command(resume={"decision": "timeout"}), state_config(thread))

    assert final["terminal_state"] == "notification_failed"
    assert final["receipt_flags"] == ["none", "none", "none", "none"]
    stored = await _event(make_session, reminder_id)
    assert stored.terminal_state == "notification_failed"
    assert len(await _orders(make_session)) == 1, "没送达也是红线，仍要派单"


async def test_graph_escalation_is_not_duplicated_on_replay(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """升级节点幂等：事件已 escalated + 有终态时不再建第二张单（commit 成功但图没跑完的重放）。"""
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await graph.ainvoke(state, state_config(thread))
    for _ in range(3):
        clock.advance(minutes=5)
        await graph.ainvoke(Command(resume={"decision": "timeout"}), state_config(thread))
    clock.advance(minutes=5)
    await graph.ainvoke(Command(resume={"decision": "timeout"}), state_config(thread))
    assert len(await _orders(make_session)) == 1

    # 手工把状态摆回「待升级」，再跑一次 escalate 节点
    await graph.aupdate_state(
        state_config(thread),
        {"next_action": "escalate", "terminal_state": None, "decision": DECISION_TIMEOUT},
    )
    replay = await graph_mod.escalate_node(
        {"thread_id": thread, "reminder_id": str(reminder_id), "receipt_flags": ["card_rendered"]},
        runtime=graph_mod.MedicationRuntime(
            deliver=recorder, session_factory=make_session, clock=clock, wait_seconds=60
        ),
    )
    assert replay["order_no"] is None
    assert "escalate:already_escalated" in replay["warnings"]
    assert len(await _orders(make_session)) == 1, "重放不得建第二张单"


# ------------------------------------------------------------------ 6. 状态读取与重启


async def test_resume_state_exposes_attempt_no_and_next_node(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """⑥ 挂起时 `resume_state` 能取回 attempt_no / next_action / next（AC-17）。"""
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await graph.ainvoke(state, state_config(thread))
    snapshot = await resume_state(graph, thread)

    assert snapshot["attempt_no"] == 1
    assert snapshot["next_action"] == "wait_confirm"
    assert snapshot["status"] == "waiting_confirm"
    assert snapshot["next"] == ("wait_confirm",)
    assert snapshot["interrupted"] is True
    assert snapshot["thread_id"] == thread
    assert snapshot["max_attempts"] == 4


async def test_timeout_schedules_next_attempt_on_the_five_minute_beat(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await graph.ainvoke(state, state_config(thread))
    clock.advance(minutes=5)
    await graph.ainvoke(Command(resume={"decision": "timeout"}), state_config(thread))

    snapshot = await resume_state(graph, thread)
    assert snapshot["attempt_no"] == 2
    assert datetime.datetime.fromisoformat(snapshot["next_retry_at"]) == START + datetime.timedelta(
        minutes=5
    )
    assert snapshot["next"] == ("wait_confirm",)
    assert len(recorder.events) == 2
    assert len(await _attempts(make_session, reminder_id)) == 2


async def test_start_or_resume_does_not_resend_after_a_restart(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """重启后重放必须只读状态，不能把提醒再发一遍。"""
    clock = FakeClock(START)
    recorder = Recorder()
    graph = await _build_graph(make_session, clock=clock, deliver=recorder)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    await start_or_resume(graph, **state)
    assert len(recorder.events) == 1

    replayed = await start_or_resume(graph, thread_id=thread)
    assert len(recorder.events) == 1, "没有 resume 就不该重发提醒"
    assert replayed["attempt_no"] == 1
    assert replayed["next"] == ("wait_confirm",)

    finished = await start_or_resume(
        graph, thread_id=thread, resume={"decision": "confirmed", "channel": "voice"}
    )
    assert finished["terminal_state"] == "taken_reported"
    assert len(recorder.events) == 1


async def test_build_graph_raises_when_checkpointer_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用药图**不**降级 MemorySaver：取不到 checkpointer 就抛（与健康图刻意不同）。"""

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise checkpointer_mod.CheckpointerUnavailableError("langgraph schema 不可用")

    monkeypatch.setattr(checkpointer_mod, "get_checkpointer", boom)
    with pytest.raises(checkpointer_mod.CheckpointerUnavailableError):
        await build_medication_graph()


async def test_graph_recovers_after_checkpointer_restart(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """⑦ 真 PostgreSQL checkpointer：写中断态 → 关连接 → 重开 → 状态仍在且不重发。"""
    clock = FakeClock(START)
    recorder = Recorder()
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)
    reminder_id = await _seed_event(make_session)
    state = _state(thread=thread, reminder_id=reminder_id)

    reset_medication_graph()
    saver = await checkpointer_mod.get_checkpointer()
    try:
        graph = await _build_graph(make_session, clock=clock, deliver=recorder, checkpointer=saver)
        await graph.ainvoke(state, state_config(thread))
        assert len(recorder.events) == 1

        # —— 进程重启：关掉旧的 checkpointer 连接，重新取一个 ——
        await checkpointer_mod.close_checkpointer()
        await checkpointer_mod.close_checkpointer()  # 幂等：再关一次不炸
        saver2 = await checkpointer_mod.get_checkpointer()
        assert saver2 is not saver, "重启后必须是新的 saver 实例"

        graph2 = await _build_graph(
            make_session, clock=clock, deliver=recorder, checkpointer=saver2
        )
        snapshot = await resume_state(graph2, thread)
        assert snapshot["attempt_no"] == 1, "重启后必须还知道这是第几次触达"
        assert snapshot["next_action"] == "wait_confirm"
        assert snapshot["next"] == ("wait_confirm",)
        assert len(recorder.events) == 1, "恢复期间不得重发提醒"
        assert len(await _attempts(make_session, reminder_id)) == 1

        clock.advance(minutes=2)
        final = await start_or_resume(
            graph2, thread_id=thread, resume={"decision": "confirmed", "channel": "button"}
        )
        assert final["terminal_state"] == "taken_reported"
        stored = await _event(make_session, reminder_id)
        assert stored.status == "confirmed"
        assert stored.terminal_state == "taken_reported"
        assert stored.confirmed_at == START + datetime.timedelta(minutes=2)

        await saver2.adelete_thread(thread)
    finally:
        await checkpointer_mod.close_checkpointer()
        reset_medication_graph()


# ---------------------------------------------------------------------------
# 拒服派单（服务层）：AC-07 的幂等与参数校验
# ---------------------------------------------------------------------------


async def test_refuse_event_is_idempotent_and_reuses_the_same_order(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """重放（commit 成功但图没跑完）不得重复建风险事件或工单。"""
    reminder_id = await _seed_event(make_session)

    async with make_session() as session:
        event = await medication_service.load_event(session, reminder_id)
        first = await medication_service.refuse_event(session, event=event, channel="voice")
        await session.commit()
        first_no = first.order_no

    async with make_session() as session:
        event = await medication_service.load_event(session, reminder_id)
        second = await medication_service.refuse_event(session, event=event, channel="button")
        await session.commit()
        assert second.order_no == first_no

    assert len(await _risk_events(make_session)) == 1
    orders = await _orders(make_session)
    assert len(orders) == 1
    assert orders[0].severity == "yellow"
    assert orders[0].type == "follow_up"


async def test_refuse_event_rejects_unknown_channel(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """`confirm_channel` 只有 voice/button，别的值一律 `MedicationValidationError`。"""
    reminder_id = await _seed_event(make_session)

    async with make_session() as session:
        event = await medication_service.load_event(session, reminder_id)
        with pytest.raises(MedicationValidationError):
            await medication_service.refuse_event(session, event=event, channel="sms")


async def test_refuse_event_clears_confirmed_at(
    world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """拒服不是「确认服药」：即便事件上残留确认时刻也必须抹掉。"""
    reminder_id = await _seed_event(make_session)

    async with make_session() as session:
        event = await medication_service.load_event(session, reminder_id)
        event.confirmed_at = START
        await medication_service.refuse_event(session, event=event, channel="voice")
        await session.commit()

    stored = await _event(make_session, reminder_id)
    assert stored.confirmed_at is None
    assert stored.terminal_state == "refused"


# ---------------------------------------------------------------------------
# 接生产：按钮 / 对话两个入口（FR-MED-05, AC-17, AC-35）
# ---------------------------------------------------------------------------
# 两个入口做的事是同一件：先问图「这条事件还在等回应吗」，在等就 resume 让它自己
# 跑完，不在等才直写业务表。测试按同一顺序钉住三件事：停点看得见 → resume 只落
# 一次终态 → 重复调用不再写。


async def _start_until_waiting(
    make_session: Callable[[], AsyncSession],
    *,
    graph: Any,
    reminder_id: uuid.UUID,
    plan_id: uuid.UUID = PLAN,
    scheduled_at: datetime.datetime = START,
) -> str:
    """把图跑到 `wait_confirm` 的 interrupt 上，返回线程键。

    图本身不建事件：调度器/API 先用 `ensure_event` 落一条事件，再把
    `reminder_id` 交给图——所以「有停点」永远等价于「这条事件在等回应」。
    """
    thread = medication_thread_id(elder_id=ELDER, plan_id=plan_id, scheduled_at=scheduled_at)
    await graph.ainvoke(
        initial_state(
            thread_id=thread,
            elder_id=str(ELDER),
            reminder_id=str(reminder_id),
            plan_id=str(plan_id),
            scheduled_at=scheduled_at,
            drug_snapshot=SNAPSHOT,
        ),
        state_config(thread),
    )
    return thread


async def test_pending_confirmation_reads_the_hitl_stop(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """停点判定读的是 checkpoint（`aget_state().next`），不是「有没有事件行」。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())
    reminder_id = await _seed_event(make_session)
    thread = await _start_until_waiting(make_session, graph=graph, reminder_id=reminder_id)

    payload = await pending_confirmation(graph, thread)

    assert payload is not None
    assert payload["type"] == "medication_confirm"
    assert payload["reminder_id"] == str(reminder_id)
    assert payload["attempt_no"] == 1
    assert payload["prompt"], "停点载荷必须带长者话术，否则对话入口不知道该问什么"


async def test_pending_confirmation_is_none_for_unknown_thread(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """线程不存在 ≠ 出错：照护台在没起步过的事件上确认，必须落回直写而不是 500。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())

    assert await pending_confirmation(graph, "medication:no-such-thread") is None


async def test_resume_confirmation_records_dialog_source(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """AC-35：resume 载荷带的 `triggered_by` 要落到图状态里，原流程来源不许丢。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())
    reminder_id = await _seed_event(make_session)
    thread = await _start_until_waiting(make_session, graph=graph, reminder_id=reminder_id)

    state = await resume_confirmation(
        graph,
        thread_id=thread,
        decision=DECISION_CONFIRMED,
        channel="voice",
        triggered_by="dialog",
        response="我吃过了",
    )

    assert state["status"] == "confirmed"
    assert state["triggered_by"] == "dialog"
    assert state["flow_triggered_by"] == "scheduler", "覆盖前先留档，否则查不出是谁起的流程"
    assert await pending_confirmation(graph, thread) is None, "跑完的线程不该再有停点"

    stored = await _event(make_session, reminder_id)
    assert stored.status == "confirmed"
    assert stored.triggered_by == "scheduler", "事件行的 triggered_by 列仍是流程来源的权威"


async def test_confirm_from_dialog_confirms_the_pending_event(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """长者在对话里说「我吃过药了」→ 打回图 → 终态 `taken_reported`，触达行不增。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())
    reminder_id = await _seed_event(make_session)
    thread = await _start_until_waiting(make_session, graph=graph, reminder_id=reminder_id)

    async with make_session() as session:
        outcome = await confirm_from_dialog(session, elder_id=ELDER, text="我吃过药了", graph=graph)
        await session.commit()

    assert outcome.confirmed is True
    assert outcome.hitl_resumed is True
    assert outcome.status == "confirmed"
    assert outcome.terminal_state == "taken_reported"
    assert outcome.confirm_channel == "voice", "对话入口的渠道是 voice，不许写成 button"
    assert str(SNAPSHOT["drug_name"]) not in outcome.reply, "回话里不带药名"

    stored = await _event(make_session, reminder_id)
    assert stored.status == "confirmed"
    assert stored.confirm_channel == "voice"
    assert len(await _attempts(make_session, reminder_id)) == 1, "resume 不该新增触达行"
    assert (await resume_state(graph, thread))["finished"] is True


async def test_confirm_from_dialog_without_pending_event_does_not_fabricate(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """没有待确认事件时如实说「没有」，绝不能凭一句话造一条已服记录（AC-35 反向）。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())

    async with make_session() as session:
        outcome = await confirm_from_dialog(session, elder_id=ELDER, text="我吃过药了", graph=graph)

    assert outcome.confirmed is False
    assert outcome.reason == "no_pending_event"
    assert "当前没有待确认的用药提醒" in outcome.reply

    async with make_session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(MedicationEvent)
            .where(MedicationEvent.elder_id == ELDER)
        )
    assert count == 0, "没有待确认事件时一行都不许写"


async def test_confirm_from_dialog_ignores_unrecognised_words(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """听不懂就听不懂：事件停在原状态，交给主控 Agent 继续聊。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())
    reminder_id = await _seed_event(make_session)

    async with make_session() as session:
        outcome = await confirm_from_dialog(
            session, elder_id=ELDER, text="今天天气不错", graph=graph
        )

    assert outcome.confirmed is False
    assert outcome.reason == "unrecognised_text"
    stored = await _event(make_session, reminder_id)
    assert stored.status == "calling", "没听懂不能顺手把事件推进成已确认"


async def test_confirm_from_dialog_refusal_opens_one_follow_up(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """对话里明确拒服 → 停止重试、风险事件 + 护工跟进工单各一条（AC-07 同口径）。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())
    reminder_id = await _seed_event(make_session)
    await _start_until_waiting(make_session, graph=graph, reminder_id=reminder_id)

    async with make_session() as session:
        outcome = await confirm_from_dialog(session, elder_id=ELDER, text="我不吃药", graph=graph)
        await session.commit()

    assert outcome.confirmed is False, "拒服不是「已服药」"
    assert outcome.decision == "refused"
    assert outcome.status == "escalated"
    assert outcome.terminal_state == "refused"

    stored = await _event(make_session, reminder_id)
    assert stored.failure_reason == "denied"
    assert stored.confirmed_at is None
    orders = await _orders(make_session)
    assert len(orders) == 1 and orders[0].type == "follow_up"
    risks = await _risk_events(make_session)
    assert len(risks) == 1 and risks[0].type == "med_risk"

    async with make_session() as session:
        again = await confirm_from_dialog(session, elder_id=ELDER, text="我不吃药", graph=graph)
    assert again.reason == "no_pending_event"
    assert len(await _orders(make_session)) == 1, "第二次拒服不许再开单"


async def test_confirm_event_on_confirmed_event_keeps_confirmed_at(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """重复确认返回原结果：状态不变、`confirmed_at` 也不许被刷新（幂等口径）。"""
    graph = await _build_graph(make_session, clock=FakeClock(START), deliver=Recorder())
    reminder_id = await _seed_event(make_session)
    await _start_until_waiting(make_session, graph=graph, reminder_id=reminder_id)

    async with make_session() as session:
        first = await confirm_from_dialog(session, elder_id=ELDER, text="吃了", graph=graph)
        await session.commit()
    assert first.confirmed is True
    first_stored = await _event(make_session, reminder_id)

    async with make_session() as session:
        second = await confirm_from_dialog(session, elder_id=ELDER, text="吃了", graph=graph)
        await session.commit()

    assert second.confirmed is False
    assert second.reason == "no_pending_event"
    second_stored = await _event(make_session, reminder_id)
    assert second_stored.confirmed_at == first_stored.confirmed_at


async def test_confirm_falls_back_to_direct_write_when_graph_is_unavailable(
    db: Any,
    world: dict[str, uuid.UUID],
    make_session: Callable[[], AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图取不到（checkpointer 掉线）也必须能确认：业务事实优先于流程引擎。"""

    async def _boom() -> Any:
        raise checkpointer_mod.CheckpointerUnavailableError("checkpointer is down")

    monkeypatch.setattr(medication_service, "confirmation_graph", _boom)
    reminder_id = await _seed_event(make_session)

    async with make_session() as session:
        event = await medication_service.load_event(session, reminder_id)
        event, resumed = await confirm_event(session, event=event, channel="button")
        await session.commit()

    assert resumed is False
    stored = await _event(make_session, reminder_id)
    assert stored.status == "confirmed"
    assert stored.terminal_state == "taken_reported"


async def test_confirm_survives_a_checkpointer_restart(
    db: Any, world: dict[str, uuid.UUID], make_session: Callable[[], AsyncSession]
) -> None:
    """AC-17 重启恢复：丢掉运行时后用同一线程键重建，停点还在、resume 只落一次终态。

    与 `test_graph_recovers_after_checkpointer_restart` 的区别：那条测的是图自己的
    `resume_state`；这条走生产入口 `confirm_from_dialog`，即「后端重启后照护台/对话
    还能不能把那条已经开始等回应的线程接上」。
    """
    clock = FakeClock(START)
    reminder_id = await _seed_event(make_session)
    thread = medication_thread_id(elder_id=ELDER, plan_id=PLAN, scheduled_at=START)

    reset_medication_graph()
    saver = await checkpointer_mod.get_checkpointer()
    saver2: Any | None = None
    try:
        first = await _build_graph(
            make_session, clock=clock, deliver=Recorder(), checkpointer=saver
        )
        await _start_until_waiting(make_session, graph=first, reminder_id=reminder_id)
        attempts_before = len(await _attempts(make_session, reminder_id))

        # 模拟重启：关掉 checkpointer 单例（进程重启 = 连接与缓存全丢）
        await checkpointer_mod.close_checkpointer()
        saver2 = await checkpointer_mod.get_checkpointer()
        assert saver2 is not saver, "重启后必须是新的 saver，否则等于没重启"
        graph = await _build_graph(
            make_session, clock=clock, deliver=Recorder(), checkpointer=saver2
        )

        # 1) 待确认状态仍在（没有靠内存里的东西）
        pending = await pending_confirmation(graph, thread)
        assert pending is not None
        assert pending["reminder_id"] == str(reminder_id)
        assert (await _event(make_session, reminder_id)).status == "waiting_confirm"

        # 2) 恢复后确认只落一次终态，触达行数不变
        async with make_session() as session:
            outcome = await confirm_from_dialog(session, elder_id=ELDER, text="吃了", graph=graph)
            await session.commit()
        assert outcome.confirmed is True and outcome.hitl_resumed is True

        stored = await _event(make_session, reminder_id)
        assert stored.status == "confirmed"
        assert stored.terminal_state == "taken_reported"
        assert len(await _attempts(make_session, reminder_id)) == attempts_before
        assert await pending_confirmation(graph, thread) is None
    finally:
        if saver2 is not None:
            await saver2.adelete_thread(thread)
        await checkpointer_mod.close_checkpointer()
        reset_medication_graph()


def test_parse_decision_reads_free_speech() -> None:
    """对话入口拿到的是长者的整句话，结论词夹在宾语里也得认出来（AC-35）。"""
    assert parse_decision("我吃过药了")[0] == DECISION_CONFIRMED
    assert parse_decision("刚刚吃完啦")[0] == DECISION_CONFIRMED
    assert parse_decision("我不吃药")[0] == DECISION_REFUSED


def test_parse_decision_refuses_to_guess_negated_or_unclear_speech() -> None:
    """「不吃了」是拒服不是已服；「还没吃」两边都不是——认不出就抛，绝不猜。

    这条是安全用例不是文字游戏：把拒服读成已服，等于替长者撒了一次服药谎。
    """
    assert parse_decision("我不吃了")[0] == DECISION_REFUSED
    with pytest.raises(ValueError):
        parse_decision("我还没吃药")
    with pytest.raises(ValueError):
        parse_decision("今天天气不错")
