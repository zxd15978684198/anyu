"""task-12 / FR-ALT-01：红色事件通知监护人 + 120 秒无人确认升级通知管理员。

覆盖六件事：

1. 一次红色命中落**两条**通知：护工那条（定向派单）+ 家属那条（`payload.recipients`）；
2. 家属那条的 payload 只放结构化事实（受众标记、事件 id、类别、严重度、时间、单号），
   不夹带长者原话——隐私边界在 PRD 177 的家属白名单里；
3. 长者没有在职家属时不建家属行（`guardian_notification_id` 为 `None`），
   而不是建一条家属永远看不见的空通知；
4. 幂等：同一分钟同一规则重复命中只会有一套（家属行也只有一条），
   直接重调 `notify_safety_alert_guardians` 也返回同一条；
5. 升级通知带 `order_no`（只读回查，不建第二张单——AC-12）；
6. 「没有在职管理员」时事件照升、通知不建，但必须留下 warning。

夹具沿用 `tests/test_alert_service.py` 的「单连接 + 外层事务回滚」口径：
`alert_service` / `risk_event_service` 都只 flush 不 commit，用例里的写入
不会落进开发库。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

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
from app.core.runtime_context import Identity
from app.core.security import KIND_GUARDIAN
from app.models import (
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    Notification,
    RiskEvent,
    StaffAccount,
    WorkOrder,
)
from app.router.decision import RouteDecision, SafetyHit
from app.services import alert_service, notification_service, risk_event_service

UTC = datetime.UTC
#: 固定锚点：夜里 03:12（用 UTC 直接写，避免用例跟着机器时区漂）。
OCCURRED_AT = datetime.datetime(2026, 11, 12, 3, 12, 0, tzinfo=UTC)
THREAD_ID = "thread-alt-1"
TRACE_ID = "trace-alt-1"


@pytest.fixture()
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture()
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


@dataclass(frozen=True)
class Seed:
    care_worker_id: uuid.UUID
    admin_id: uuid.UUID
    admin_b_id: uuid.UUID
    elder_id: uuid.UUID
    guardian_ids: tuple[uuid.UUID, ...]


def _staff_row(staff_id: uuid.UUID, name: str, role: str) -> dict[str, object]:
    return {"staff_id": staff_id, "name": name, "role": role, "password_hash": "x"}


@pytest.fixture()
async def seeded(db: AsyncSession) -> Seed:
    """一个长者 + 主责护工 + 两位在职管理员 + 两位在职家属。"""
    care_worker_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    admin_b_id = uuid.uuid4()
    elder_id = uuid.uuid4()
    guardian_ids = (uuid.uuid4(), uuid.uuid4())
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            _staff_row(care_worker_id, "李护工", "care_worker"),
            _staff_row(admin_id, "王管理", "admin"),
            _staff_row(admin_b_id, "周管理", "admin"),
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": f"张桂芬{uuid.uuid4().hex[:6]}", "status": "active"}],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [{"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"}],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [
            {
                "guardian_id": guardian_ids[0],
                "name": "家属甲",
                "password_hash": "x",
                "status": "active",
            },
            {
                "guardian_id": guardian_ids[1],
                "name": "家属乙",
                "password_hash": "x",
                "status": "active",
            },
        ],
    )
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "guardian_id": guardian_ids[0],
                "relation": "daughter",
                "status": "active",
            },
            {
                "elder_id": elder_id,
                "guardian_id": guardian_ids[1],
                "relation": "son",
                "status": "active",
            },
        ],
    )
    await db.flush()
    return Seed(
        care_worker_id=care_worker_id,
        admin_id=admin_id,
        admin_b_id=admin_b_id,
        elder_id=elder_id,
        guardian_ids=guardian_ids,
    )


@pytest.fixture()
async def seeded_lonely(db: AsyncSession) -> Seed:
    """同一套人，但长者没有任何在职家属（独居/家属未注册是合法状态）。"""
    care_worker_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    elder_id = uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            _staff_row(care_worker_id, "李护工", "care_worker"),
            _staff_row(admin_id, "王管理", "admin"),
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": f"李振华{uuid.uuid4().hex[:6]}", "status": "active"}],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [{"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"}],
    )
    await db.flush()
    return Seed(
        care_worker_id=care_worker_id,
        admin_id=admin_id,
        admin_b_id=uuid.uuid4(),
        elder_id=elder_id,
        guardian_ids=(),
    )


@pytest.fixture()
async def seeded_no_admin(db: AsyncSession) -> Seed:
    """有人有家属，但一个在职管理员都没有：升级只能留 warning，不能伪造收件人。"""
    care_worker_id = uuid.uuid4()
    elder_id = uuid.uuid4()
    guardian_id = uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [_staff_row(care_worker_id, "李护工", "care_worker")],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": f"王秀兰{uuid.uuid4().hex[:6]}", "status": "active"}],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [{"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"}],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [{"guardian_id": guardian_id, "name": "家属丙", "password_hash": "x", "status": "active"}],
    )
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "guardian_id": guardian_id,
                "relation": "daughter",
                "status": "active",
            }
        ],
    )
    await db.flush()
    return Seed(
        care_worker_id=care_worker_id,
        admin_id=uuid.uuid4(),
        admin_b_id=uuid.uuid4(),
        elder_id=elder_id,
        guardian_ids=(guardian_id,),
    )


def _hit(**overrides: object) -> SafetyHit:
    values: dict[str, object] = {
        "rule_id": "safety_emergency",
        "category": "emergency",
        "severity": "red",
        "matched": "胸口疼",
        "transcript_excerpt": "我胸口疼得厉害，快叫医生",
        "occurred_at": OCCURRED_AT,
    }
    values.update(overrides)
    return SafetyHit(**values)  # type: ignore[arg-type]


def _decision(hit: SafetyHit | None) -> RouteDecision:
    return RouteDecision(
        elder_id=None,
        thread_id=None,
        layer="L0",
        rule_id="safety_emergency",
        intent="alert",
        sensitive=True,
        alert=hit,
    )


async def _handle(
    db: AsyncSession, seed: Seed, hit: SafetyHit | None = None
) -> alert_service.AlertOutcome | None:
    return await alert_service.handle_safety_alert(
        db,
        decision=_decision(_hit() if hit is None else hit),
        elder_id=seed.elder_id,
        thread_id=THREAD_ID,
        trace_id=TRACE_ID,
    )


async def _notifications(db: AsyncSession) -> list[Notification]:
    return list((await db.execute(select(Notification))).scalars().all())


async def _guardian_rows(db: AsyncSession) -> list[Notification]:
    """家属告警行：靠 `payload.audience == 'guardian'` 认，不靠 kind（护工那条同 kind）。"""
    rows = await _notifications(db)
    return [
        row
        for row in rows
        if isinstance(row.payload, dict) and row.payload.get("audience") == "guardian"
    ]


async def _count(db: AsyncSession, model: object) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# 1~2. 红色命中 → 护工一条 + 家属一条；家属 payload 只放结构化事实
# ---------------------------------------------------------------------------


async def test_red_alert_notifies_caregiver_and_guardians(db: AsyncSession, seeded: Seed) -> None:
    outcome = await _handle(db, seeded)
    assert outcome is not None and outcome.created is True

    rows = await _notifications(db)
    assert len(rows) == 2, "一次红色命中应当只有护工 + 家属两条通知"

    caregiver = next(row for row in rows if row.assignee_staff_id is not None)
    assert caregiver.assignee_staff_id == seeded.care_worker_id
    assert caregiver.kind == alert_service.SAFETY_ALERT_TYPE

    guardians = await _guardian_rows(db)
    assert len(guardians) == 1, "一条通知收多人：不得按家属逐个复制"
    guardian_row = guardians[0]
    assert guardian_row.kind == "safety_alert"
    assert guardian_row.severity == "red"
    assert guardian_row.status == "generated"
    assert guardian_row.elder_id == seeded.elder_id
    # `assignee_staff_id` 的外键只能指员工：家属一律走 payload.recipients。
    assert guardian_row.assignee_staff_id is None
    assert guardian_row.payload is not None
    assert guardian_row.payload["audience"] == "guardian"
    assert guardian_row.payload["risk_event_id"] == str(outcome.risk_event_id)
    assert guardian_row.payload["category"] == "emergency"
    assert guardian_row.payload["severity"] == "red"
    assert guardian_row.payload["elder_id"] == str(seeded.elder_id)
    assert guardian_row.payload["occurred_at"] == OCCURRED_AT.isoformat()
    assert guardian_row.payload["order_no"] == outcome.order_no
    # 收件人是全部在职家属，按 id 排序（可复现），且是字符串形态的 UUID。
    assert guardian_row.payload["recipients"] == sorted(str(gid) for gid in seeded.guardian_ids)
    # 家属侧只给「需关注事件摘要」：不得夹带长者原话或命中片段。
    rendered = str(guardian_row.payload)
    assert "胸口疼" not in rendered
    assert "快叫医生" not in rendered
    # outcome 要把两条通知分别指出来，调用方才能分流。
    assert outcome.guardian_notification_id == guardian_row.notification_id
    assert outcome.notification_id == caregiver.notification_id


async def test_alert_without_guardian_creates_only_caregiver_row(
    db: AsyncSession, seeded_lonely: Seed
) -> None:
    outcome = await _handle(db, seeded_lonely)
    assert outcome is not None and outcome.created is True

    rows = await _notifications(db)
    assert len(rows) == 1, "没有在职家属时不建家属行"
    assert rows[0].assignee_staff_id == seeded_lonely.care_worker_id
    assert outcome.guardian_notification_id is None


async def test_yellow_hit_is_not_pushed_to_family(db: AsyncSession, seeded: Seed) -> None:
    """FR-ALT-01 只管红色：黄色是内部提醒，推给家属只会造成非紧急骚扰。"""
    outcome = await _handle(db, seeded, _hit(severity="yellow"))
    assert outcome is not None and outcome.created is True
    assert outcome.guardian_notification_id is None
    assert await _guardian_rows(db) == []


# ---------------------------------------------------------------------------
# 3. 幂等：(elder_id, risk_event_id) 只建一次
# ---------------------------------------------------------------------------


async def test_same_minute_hit_keeps_single_guardian_row(db: AsyncSession, seeded: Seed) -> None:
    first = await _handle(db, seeded)
    second = await _handle(db, seeded)
    assert first is not None and first.created is True
    assert second is not None and second.duplicate is True
    # 重复命中不会再通知家属（否则家属端一分钟能刷出好几条同样的告警）。
    assert second.guardian_notification_id is None

    assert await _count(db, RiskEvent) == 1
    assert await _count(db, WorkOrder) == 1
    assert len(await _notifications(db)) == 2
    assert len(await _guardian_rows(db)) == 1


async def test_notify_safety_alert_guardians_is_idempotent(db: AsyncSession, seeded: Seed) -> None:
    event_id = uuid.uuid4()
    first = await notification_service.notify_safety_alert_guardians(
        db,
        elder_id=seeded.elder_id,
        risk_event_id=event_id,
        category="emergency",
        severity="red",
        occurred_at=OCCURRED_AT,
    )
    second = await notification_service.notify_safety_alert_guardians(
        db,
        elder_id=seeded.elder_id,
        risk_event_id=event_id,
        category="emergency",
        severity="red",
        occurred_at=OCCURRED_AT,
    )
    assert first is not None and second is not None
    assert first.notification_id == second.notification_id
    assert len(await _guardian_rows(db)) == 1


# ---------------------------------------------------------------------------
# 4. 家属按 elde_id 可见：能看到这条家属行
# ---------------------------------------------------------------------------


async def test_guardian_can_read_the_guardian_row(db: AsyncSession, seeded: Seed) -> None:
    await _handle(db, seeded)
    identity = Identity(kind=KIND_GUARDIAN, guardian_id=str(seeded.guardian_ids[0]))
    page = await notification_service.list_for_staff(db, identity, page=1, page_size=20)

    # 家属可见范围按 `elder_id` 放行（护工那条同属这个长者，因此也在列表里）；
    # 断言的重点是「家属那条确实可见且只有一条」，而不是列表总条数。
    visible_guardian_rows = [
        item for item in page.items if (item.payload or {}).get("audience") == "guardian"
    ]
    assert len(visible_guardian_rows) == 1
    assert visible_guardian_rows[0].kind == "safety_alert"


# ---------------------------------------------------------------------------
# 5~6. 升级通知：带单号、每管理员一条、不建第二个事件/工单
# ---------------------------------------------------------------------------


async def test_escalation_notifies_admins_with_order_no(db: AsyncSession, seeded: Seed) -> None:
    outcome = await _handle(db, seeded)
    assert outcome is not None and outcome.order_no is not None

    escalated = await risk_event_service.escalate_overdue_red_events(
        db,
        now=OCCURRED_AT
        + datetime.timedelta(seconds=risk_event_service.ESCALATION_TIMEOUT_SECONDS + 1),
    )
    assert escalated == [outcome.risk_event_id]

    admin_rows = [
        row
        for row in await _notifications(db)
        if row.payload and row.payload.get("module") == "risk_event_escalation"
    ]
    assert len(admin_rows) == 2, "每个在职管理员各一条（定向可见性）"
    assert {row.assignee_staff_id for row in admin_rows} == {seeded.admin_id, seeded.admin_b_id}
    for row in admin_rows:
        assert row.kind == "safety_alert"
        assert row.severity == "red"
        assert row.elder_id == seeded.elder_id
        assert row.payload is not None
        assert row.payload["risk_event_id"] == str(outcome.risk_event_id)
        # 只读回查既有工单的单号：不建第二张单（AC-12）。
        assert row.payload["order_no"] == outcome.order_no
        assert row.payload["elder_id"] == str(seeded.elder_id)
        assert row.payload["escalated_at"] is not None
        assert row.payload["escalation_reason"] == (
            f"unacknowledged_{risk_event_service.ESCALATION_TIMEOUT_SECONDS}s"
        )

    # AC-12：升级不得造出第二个风险事件或第二张工单。
    assert await _count(db, RiskEvent) == 1
    assert await _count(db, WorkOrder) == 1
    event = await db.get(RiskEvent, outcome.risk_event_id)
    assert event is not None and event.status == "escalated"


async def test_second_escalation_round_does_not_notify_again(
    db: AsyncSession, seeded: Seed
) -> None:
    await _handle(db, seeded)
    later = OCCURRED_AT + datetime.timedelta(minutes=10)
    await risk_event_service.escalate_overdue_red_events(db, now=later)
    before = len(await _notifications(db))
    # 再跑一轮：候选条件带 `status == 'open'`，已 escalated 的事件不会再被选中。
    assert await risk_event_service.escalate_overdue_red_events(db, now=later) == []
    assert len(await _notifications(db)) == before


async def test_escalation_without_admin_warns_and_creates_no_admin_row(
    db: AsyncSession, seeded_no_admin: Seed, caplog: pytest.LogCaptureFixture
) -> None:
    outcome = await _handle(db, seeded_no_admin)
    assert outcome is not None

    with caplog.at_level(logging.WARNING, logger="app.services.risk_event_service"):
        escalated = await risk_event_service.escalate_overdue_red_events(
            db, now=OCCURRED_AT + datetime.timedelta(minutes=3)
        )

    assert escalated == [outcome.risk_event_id], "没人可叫也要把事件升上去"
    messages = [record.getMessage() for record in caplog.records]
    assert "risk event escalated without admin" in messages, "没有管理员必须留 warning，不能静默"
    # 不伪造收件人：一条定向给管理员的通知都不该有。
    admin_rows = [
        row
        for row in await _notifications(db)
        if row.payload and row.payload.get("module") == "risk_event_escalation"
    ]
    assert admin_rows == []
