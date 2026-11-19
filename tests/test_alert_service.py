"""`app/services/alert_service.py` 的定向用例（迭代 8 N10）。

覆盖：
1. 一次安全命中同事务写三张表，且三张表能按 `rule_id` / `risk_event_id` 串起来；
2. 同一分钟同一线程同一规则重复命中 → 只建一套（幂等靠 `source_id = uuid5(...)`）；
3. 跨分钟再喊一次 → 这是第二次真实告警，必须建第二套；
4. 原话切片写库前过脱敏：库里搜不到原话全文（断言数据，不断言「函数被调用过」）；
5. 三类 category 都能落库，且类型字段一律稳定枚举 `safety_alert`、细分在 detail；
6. 契约破损时抛 `AlertContractError` 而不是静默放过（intent=alert 但没 hit、缺 occurred_at、
   category 越界）。

夹具口径：`alert_service` 只 flush 不 commit，所以沿用仓库「单连接 + 外层事务回滚」的
夹具（`tests/test_medication_plan_edit.py` 同款）；用例里的写入不会落进开发库。
"""

from __future__ import annotations

import datetime
import re
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
from app.services import alert_service

UTC = datetime.UTC
#: 固定锚点：夜里 03:12（用 UTC 直接写，避免用例跟着机器时区漂）。
OCCURRED_AT = datetime.datetime(2026, 11, 10, 3, 12, 0, tzinfo=UTC)
THREAD_ID = "thread-alert-1"
TRACE_ID = "trace-alert-1"


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
    doctor_id: uuid.UUID
    elder_id: uuid.UUID


@pytest.fixture()
async def seeded(db: AsyncSession) -> Seed:
    """一个长者 + 一位主责护工 + 一位医生（医生不参与派单，用来验证不会乱派）。"""
    care_worker_id = uuid.uuid4()
    doctor_id = uuid.uuid4()
    elder_id = uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": care_worker_id,
                "name": "李护工",
                "role": "care_worker",
                "password_hash": "x",
            },
            {"staff_id": doctor_id, "name": "王医生", "role": "doctor", "password_hash": "x"},
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": f"张桂芬{uuid.uuid4().hex[:6]}", "status": "active"}],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "staff_id": care_worker_id,
                "relation": "primary_care",
            }
        ],
    )
    await db.flush()
    return Seed(care_worker_id=care_worker_id, doctor_id=doctor_id, elder_id=elder_id)


def _hit(
    *,
    rule_id: str = "safety_emergency",
    category: str = "emergency",
    severity: str = "red",
    excerpt: str = "我胸口疼得厉害，快叫医生",
    occurred_at: datetime.datetime | None = OCCURRED_AT,
) -> SafetyHit:
    return SafetyHit(
        rule_id=rule_id,
        category=category,
        severity=severity,
        matched="胸口疼",
        transcript_excerpt=excerpt,
        occurred_at=occurred_at,
    )


def _decision(hit: SafetyHit | None, *, intent: str = "alert") -> RouteDecision:
    return RouteDecision(
        elder_id=None,
        thread_id=None,
        layer="L0",
        rule_id="safety_emergency",
        intent=intent,
        sensitive=True,
        alert=hit,
    )


async def _count(db: AsyncSession, model: object, *where: object) -> int:
    result = await db.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


async def _handle(db: AsyncSession, seed: Seed, hit: SafetyHit | None) -> object:
    return await alert_service.handle_safety_alert(
        db,
        decision=_decision(hit),
        elder_id=seed.elder_id,
        thread_id=THREAD_ID,
        trace_id=TRACE_ID,
    )


# ---------------------------------------------------------------------------
# 1. 三张表一次落地，且彼此可追溯
# ---------------------------------------------------------------------------


async def test_alert_writes_risk_event_work_order_and_notification(
    db: AsyncSession, seeded: Seed
) -> None:
    outcome = await _handle(db, seeded, _hit())
    assert outcome is not None
    assert outcome.created is True
    assert outcome.duplicate is False
    assert outcome.category == "emergency"
    assert outcome.rule_id == "safety_emergency"

    events = (await db.execute(select(RiskEvent))).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.type == alert_service.SAFETY_ALERT_TYPE == "safety_alert"
    assert event.severity == "red"
    assert event.source_type == "rule"
    assert event.source_id == outcome.idempotency_key
    assert event.status == "open"
    # 事件时刻取命中时刻，不是落库时刻（夜里呼救不能被记成白天）。
    assert event.created_at == OCCURRED_AT
    assert event.elder_id == seeded.elder_id

    orders = (await db.execute(select(WorkOrder))).scalars().all()
    assert len(orders) == 1
    order = orders[0]
    assert order.type == "safety_alert"
    assert order.severity == "red"
    assert order.status == "new"
    assert order.event_id == event.event_id  # 这一列只指 risk_events（TD §3.4）
    assert order.assignee_id == seeded.care_worker_id
    assert re.fullmatch(r"WO-\d{8}-\d{4}", order.order_no)
    assert order.note is not None
    assert "rule_id=safety_emergency" in order.note
    assert f"risk_event_id={event.event_id}" in order.note
    assert f"trace_id={TRACE_ID}" in order.note

    notifications = (await db.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
    note = notifications[0]
    assert note.kind == "safety_alert"
    assert note.severity == "red"
    assert note.elder_id == seeded.elder_id
    assert note.assignee_staff_id == seeded.care_worker_id
    assert note.payload is not None
    assert note.payload["module"] == "safety"
    assert note.payload["rule_id"] == "safety_emergency"
    assert note.payload["category"] == "emergency"
    assert note.payload["risk_event_id"] == str(event.event_id)
    assert note.payload["order_no"] == order.order_no
    assert note.payload["trace_id"] == TRACE_ID


# ---------------------------------------------------------------------------
# 2/3. 幂等：同分钟只建一套，跨分钟是新的一次告警
# ---------------------------------------------------------------------------


async def test_same_minute_hit_creates_only_one_set(db: AsyncSession, seeded: Seed) -> None:
    first = await _handle(db, seeded, _hit())
    second = await _handle(
        db,
        seeded,
        _hit(occurred_at=OCCURRED_AT + datetime.timedelta(seconds=41)),
    )

    assert first is not None and second is not None
    assert first.created is True
    assert second.created is False
    assert second.duplicate is True
    # 重复路径复用既有风险事件的行号，调用方据此判断「这次没有再建单」。
    assert second.risk_event_id == first.risk_event_id
    assert second.idempotency_key == first.idempotency_key

    assert await _count(db, RiskEvent) == 1
    assert await _count(db, WorkOrder) == 1
    assert await _count(db, Notification) == 1


async def test_next_minute_hit_is_a_second_alert(db: AsyncSession, seeded: Seed) -> None:
    first = await _handle(db, seeded, _hit())
    later = await _handle(
        db,
        seeded,
        _hit(occurred_at=OCCURRED_AT + datetime.timedelta(minutes=1)),
    )

    assert first is not None and later is not None
    assert later.created is True
    assert later.idempotency_key != first.idempotency_key
    # 跨分钟意味着「又喊了一次」，第二张单必须建出来。
    assert await _count(db, RiskEvent) == 2
    assert await _count(db, WorkOrder) == 2
    assert await _count(db, Notification) == 2


def test_idempotency_key_is_minute_scoped() -> None:
    base = alert_service.idempotency_key(
        rule_id="safety_emergency", thread_id=THREAD_ID, occurred_at=OCCURRED_AT
    )
    same_minute = alert_service.idempotency_key(
        rule_id="safety_emergency",
        thread_id=THREAD_ID,
        occurred_at=OCCURRED_AT + datetime.timedelta(seconds=59),
    )
    next_minute = alert_service.idempotency_key(
        rule_id="safety_emergency",
        thread_id=THREAD_ID,
        occurred_at=OCCURRED_AT + datetime.timedelta(minutes=1),
    )
    other_rule = alert_service.idempotency_key(
        rule_id="safety_harm", thread_id=THREAD_ID, occurred_at=OCCURRED_AT
    )
    other_thread = alert_service.idempotency_key(
        rule_id="safety_emergency", thread_id="thread-2", occurred_at=OCCURRED_AT
    )

    assert base == same_minute
    assert base != next_minute
    assert base != other_rule
    assert base != other_thread
    assert alert_service.minute_bucket(OCCURRED_AT) == OCCURRED_AT


# ---------------------------------------------------------------------------
# 4. 脱敏：断言库里搜不到原话，而不是断言函数被调用过
# ---------------------------------------------------------------------------


async def test_excerpt_is_redacted_before_it_reaches_the_database(
    db: AsyncSession, seeded: Seed
) -> None:
    raw = "我胸口疼，电话 13800138000，身份证 310101194001011234"
    await _handle(db, seeded, _hit(excerpt=raw))

    event = (await db.execute(select(RiskEvent))).scalars().one()
    detail = event.detail  # 0012 之后是模型上的真实字段，缺了就该 AttributeError
    stored = str(detail)
    assert "13800138000" not in stored
    assert "310101194001011234" not in stored
    assert "138****8000" in stored or "138" in stored  # 掩码形态，不是原文

    # 三张表里任何文本列都不许出现原话全文。
    for model in (RiskEvent, WorkOrder, Notification):
        rows = (await db.execute(select(model))).scalars().all()
        for row in rows:
            text = str(
                {column.key: getattr(row, column.key, None) for column in model.__table__.columns}
            )
            assert "13800138000" not in text
            assert "310101194001011234" not in text


async def test_redaction_call_is_load_bearing(
    db: AsyncSession, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把 `redact_text` 换成打标函数，标记必须穿过 `_redact_free_text` 落进 detail。

    上一条验的是「现在脱了」，这一条验的是「脱敏这件事真的由那行代码负责」：给
    `redact_text` 加上 ``[R]`` 前缀后，**只有**经过 `_redact_free_text` 的字符串才会带标记。
    所以断言 `detail["thread_id"].startswith("[R]")`——`thread_id` 不是敏感键、也不在
    `_detail_payload` 里被单独处理，它是「逐值脱敏」这条路径唯一的探针；一旦有人把
    `return redact_text(value)` 改回 `return value`，这里立刻红，而不是等出事故才发现。
    """

    def _marker(text: str) -> str:
        return f"[R]{text}"

    monkeypatch.setattr(alert_service, "redact_text", _marker)
    await _handle(db, seeded, _hit())

    event = (await db.execute(select(RiskEvent))).scalars().one()
    assert event.detail is not None
    assert str(event.detail["thread_id"]).startswith("[R]")
    assert "[R]" in str(event.detail["transcript_excerpt"])


# ---------------------------------------------------------------------------
# 5. 三类 category：类型字段稳定，细分进 detail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "category"),
    [
        ("safety_emergency", "emergency"),
        ("safety_harm", "harm"),
        ("safety_request_human", "request_human"),
    ],
)
async def test_category_is_carried_in_detail_not_in_type(
    db: AsyncSession, seeded: Seed, rule_id: str, category: str
) -> None:
    outcome = await _handle(db, seeded, _hit(rule_id=rule_id, category=category))
    assert outcome is not None
    assert outcome.category == category

    event = (await db.execute(select(RiskEvent))).scalars().one()
    assert event.type == "safety_alert"  # 三张表的 type 一律稳定枚举
    assert event.severity == "red"
    detail = event.detail  # 0012 之后是模型上的真实字段，缺了就该 AttributeError
    assert detail["category"] == category
    assert detail["rule_id"] == rule_id
    assert detail["matched"]

    order = (await db.execute(select(WorkOrder))).scalars().one()
    assert order.type == "safety_alert"
    assert f"category={category}" in (order.note or "")

    notification = (await db.execute(select(Notification))).scalars().one()
    assert notification.kind == "safety_alert"
    assert notification.payload is not None
    assert notification.payload["category"] == category


# ---------------------------------------------------------------------------
# 6. 契约破损要炸，不要静默放过
# ---------------------------------------------------------------------------


async def test_alert_intent_without_hit_raises_contract_error(
    db: AsyncSession, seeded: Seed
) -> None:
    with pytest.raises(alert_service.AlertContractError, match="SafetyHit"):
        await alert_service.handle_safety_alert(
            db,
            decision=_decision(None),
            elder_id=seeded.elder_id,
            thread_id=THREAD_ID,
            trace_id=TRACE_ID,
        )
    assert await _count(db, RiskEvent) == 0
    assert await _count(db, WorkOrder) == 0
    assert await _count(db, Notification) == 0


async def test_non_alert_decision_is_ignored(db: AsyncSession, seeded: Seed) -> None:
    """普通决策（intent 不是 alert）不该被写成告警；返回 None 表示「不归我管」。"""
    outcome = await alert_service.handle_safety_alert(
        db,
        decision=_decision(None, intent="chat"),
        elder_id=seeded.elder_id,
        thread_id=THREAD_ID,
        trace_id=TRACE_ID,
    )
    assert outcome is None
    assert await _count(db, RiskEvent) == 0


async def test_missing_occurred_at_raises(db: AsyncSession, seeded: Seed) -> None:
    """命中时刻必须由编排层填：服务层取 now() 会让同一句呼救产生不同幂等键。"""
    with pytest.raises(alert_service.AlertContractError, match="occurred_at"):
        await _handle(db, seeded, _hit(occurred_at=None))
    assert await _count(db, RiskEvent) == 0


async def test_unknown_category_raises(db: AsyncSession, seeded: Seed) -> None:
    with pytest.raises(alert_service.AlertContractError, match="category"):
        await _handle(db, seeded, _hit(category="fire"))
    assert await _count(db, RiskEvent) == 0
    assert await _count(db, WorkOrder) == 0


async def test_without_primary_care_still_files_alert(db: AsyncSession, seeded: Seed) -> None:
    """没绑主责护工也要落库（值班台按红单队列接），只是不指定受理人。"""
    await db.execute(
        ElderStaffAssignment.__table__.delete().where(
            ElderStaffAssignment.elder_id == seeded.elder_id
        )
    )
    outcome = await _handle(db, seeded, _hit())
    assert outcome is not None
    order = (await db.execute(select(WorkOrder))).scalars().one()
    assert order.assignee_id is None
    notification = (await db.execute(select(Notification))).scalars().one()
    assert notification.assignee_staff_id is None


# ---------------------------------------------------------------------------
# 7. FR-ALT-01：同一场命中还要给家属建一条（收件人写进 payload.recipients）
# ---------------------------------------------------------------------------


async def test_alert_with_guardian_files_a_second_notification_for_family(
    db: AsyncSession, seeded: Seed
) -> None:
    """护工那条是派单凭据，家属那条是告知：两条行、各自幂等、不能相互替代。"""
    guardian_id = uuid.uuid4()
    await db.execute(
        GuardianAccount.__table__.insert(),
        [
            {
                "guardian_id": guardian_id,
                "name": "家属张",
                "password_hash": "x",
                "status": "active",
            }
        ],
    )
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": seeded.elder_id,
                "guardian_id": guardian_id,
                "relation": "daughter",
                "status": "active",
            }
        ],
    )
    await db.flush()

    outcome = await _handle(db, seeded, _hit())
    assert outcome is not None and outcome.created is True

    assert await _count(db, Notification) == 2
    guardian_row = (
        await db.execute(
            select(Notification).where(
                Notification.payload["audience"].astext == "guardian",
            )
        )
    ).scalar_one()
    assert guardian_row.kind == "safety_alert"
    assert guardian_row.assignee_staff_id is None
    assert guardian_row.payload is not None
    assert guardian_row.payload["recipients"] == [str(guardian_id)]
    assert outcome.guardian_notification_id == guardian_row.notification_id
    # 两条通知指向同一起险情，工单仍是那一张（不因为多通知一条就多派单）。
    assert guardian_row.payload["risk_event_id"] == str(outcome.risk_event_id)
    assert await _count(db, WorkOrder) == 1
