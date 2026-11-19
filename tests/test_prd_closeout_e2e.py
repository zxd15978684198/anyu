"""PRD 收口端到端：AC-34（主动主诉）与 AC-35（对话内用药确认）。

这两条是本轮最后两条**只能整链验**的 P0：
- AC-34：长者说「我头晕」→ 主控 Agent 调健康决策图（`source=voice_active`），
  大模型不许绕过；判据是「图真的被推进 + 会话登记为 voice_active」，
  不是「话术里出现了某个词」。
- AC-35：长者说「我吃过药了」→ 走同一张用药确认图（`triggered_by=dialog`），
  判据是业务表终态 + `hitl_resumed` 语义，不是模型自述。

隔离口径与 `tests/test_medication_graph.py` 一致：NullPool 引擎 → 一条真实连接 →
外层事务 → savepoint 的 session 工厂，所有写入随 rollback 消失。
**图用 `MemorySaver`**：本文件验的是「装配层真的把这一轮交给图了」，不验生产
checkpointer 的持久化（那条在 `test_medication_graph.py::test_confirm_survives_a_checkpointer_restart`
里已经钉住），用 MemorySaver 可以避免往测试库的 `langgraph` schema 里反复写线程。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app import turn_wiring
from app.core.config import get_settings
from app.graphs import medication_confirmation_graph as medication_graph
from app.graphs.health_checkup_graph import build_health_graph
from app.models import Elder, MedicationPlan, StaffAccount
from app.models.health import HealthCheckupSession
from app.services import medication_service

ROOM = uuid.uuid4()
ELDER = uuid.uuid4()
DOCTOR = uuid.uuid4()
PLAN = uuid.uuid4()

_PASSWORD_PLACEHOLDER = "x"  # pragma: allowlist secret

SNAPSHOT: dict[str, object] = {"drug_name": "氨氯地平", "dose": "5mg", "schedule_cron": "08:00"}

START = datetime.datetime(2026, 11, 12, 0, 0, tzinfo=datetime.UTC)


# --------------------------------------------------------------------- fixtures


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
    def _make() -> AsyncSession:
        return AsyncSession(
            bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    return _make


@pytest.fixture
def session_factory(db: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """装配层拿到的工厂：与 `make_session` 同一个连接、同一外层事务。"""
    return async_sessionmaker(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def world(db: AsyncConnection) -> dict[str, uuid.UUID]:
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
        MedicationPlan.__table__.insert().values(
            plan_id=PLAN,
            elder_id=ELDER,
            drug_name=SNAPSHOT["drug_name"],
            dose=SNAPSHOT["dose"],
            schedule_cron=str(SNAPSHOT["schedule_cron"]),
            start_date=datetime.date(2026, 11, 1),
            version=1,
            created_by=DOCTOR,
            status="active",
        )
    )
    return {"elder_id": ELDER, "plan_id": PLAN}


@pytest.fixture
def memory_health_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """把健康图工厂换成 MemorySaver 版本，避免往测试库 `langgraph` schema 写线程。"""
    graph = build_health_graph(MemorySaver())

    async def _fake_get_health_graph() -> object:
        return graph

    monkeypatch.setattr(turn_wiring.health_graph, "get_health_graph", _fake_get_health_graph)


# ------------------------------------------------------------------- AC-34


async def test_complaint_opens_active_checkup_and_persists_source(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    make_session: Callable[[], AsyncSession],
    memory_health_graph: None,
) -> None:
    """AC-34：主诉一句话 → 图跑到第一个问句，会话登记 `voice_active`。"""
    turn = turn_wiring.build_health_checkup_turn(session_factory)

    outcome = await turn("我有点头晕", str(world["elder_id"]))

    assert outcome is not None, "主诉必须把健康图推进到第一个问句"
    assert outcome.prompt is not None, "第一问要能说给长者听"
    async with make_session() as session:
        row = await session.scalar(
            select(HealthCheckupSession).where(HealthCheckupSession.elder_id == world["elder_id"])
        )
    assert row is not None, "起图必须登记采集会话（否则 20:00 漏采、60 秒超时都无从谈起）"
    assert row.status == "asking"
    assert row.source == "voice_active", "AC-34 要求 source=voice_active"
    assert row.expires_at is not None, "每问必须有 60 秒截止时间"


async def test_follow_up_complaint_does_not_open_a_second_session(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    make_session: Callable[[], AsyncSession],
    memory_health_graph: None,
) -> None:
    """同一天第二次主诉不另起一场：沿用当天那一场（AC-11 的「一次采集」口径）。"""
    turn = turn_wiring.build_health_checkup_turn(session_factory)

    await turn("我有点头晕", str(world["elder_id"]))
    await turn("还有点恶心", str(world["elder_id"]))

    async with make_session() as session:
        rows = (await session.scalars(select(HealthCheckupSession))).all()
    assert len(rows) == 1, "一天只能有一场采集（uq_health_checkup_day）"


async def test_bad_elder_id_is_not_guessed(
    session_factory: async_sessionmaker[AsyncSession], memory_health_graph: None
) -> None:
    """身份解析不出 UUID 就返回 None，不按文本猜（AGENTS §4.9）。"""
    turn = turn_wiring.build_health_checkup_turn(session_factory)
    assert await turn("我头晕", "not-a-uuid") is None


# ------------------------------------------------------------------- AC-35


async def test_dialog_confirmation_lands_on_business_table(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    make_session: Callable[[], AsyncSession],
) -> None:
    """AC-35：对话说「我吃过药了」→ 同一张图的确认路径落业务表终态。"""
    async with make_session() as session:
        event, created = await medication_service.ensure_event(
            session,
            plan_id=world["plan_id"],
            elder_id=world["elder_id"],
            scheduled_at=START,
            drug_snapshot=SNAPSHOT,
        )
        await session.commit()
    assert created is True

    turn = turn_wiring.build_medication_confirm_turn(session_factory)
    outcome = await turn("我吃过药了", str(world["elder_id"]))

    assert outcome is not None
    assert outcome.confirmed is True
    assert outcome.decision == medication_graph.DECISION_CONFIRMED
    assert outcome.status == medication_service.CONFIRMED_STATUS
    assert outcome.terminal_state == medication_service.CONFIRMED_TERMINAL_STATE
    assert outcome.confirm_channel == medication_service.DIALOG_CHANNEL

    async with make_session() as session:
        refreshed = await session.get(type(event), event.reminder_id)
    assert refreshed is not None
    assert refreshed.status == medication_service.CONFIRMED_STATUS
    assert refreshed.confirm_channel == medication_service.DIALOG_CHANNEL


async def test_dialog_confirmation_without_pending_event_is_honest(
    world: dict[str, uuid.UUID], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """没有待确认提醒时如实说没记，不伪造一次确认。"""
    turn = turn_wiring.build_medication_confirm_turn(session_factory)
    outcome = await turn("我吃过药了", str(world["elder_id"]))
    assert outcome is not None
    assert outcome.confirmed is False
    assert outcome.reason == "no_pending_event"
    assert outcome.reply == medication_service.DIALOG_NO_PENDING_REPLY
