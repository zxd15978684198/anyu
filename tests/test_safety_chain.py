"""安全命中 → 三张表落库（迭代 8 N40/N41，FR-ALR-01／FR-ALR-02）。

为什么不「替身掉 alert_service 再断言它被调过」：这条链路的价值全在**同一事务里的三行
数据能不能互相指得上** —— 风险事件 ← 派单（`work_orders.event_id`）← 通知
（`payload.risk_event_id` / `payload.order_no`）。把服务层换成替身，用例可以全绿，而库里
仍然是「有事件没单」或「有单没通知」；那种半截状态正是夜里出事时值班护工看不到的那一半。

所以这里跑的是真链路：

    `app.router.rules.match_l0`（真规则表）→ 带 `SafetyHit` 的 `RouteDecision`
    → `alert_service.handle_safety_alert` → 真 SQLAlchemy 会话 → `anyu_test` 真库

`occurred_at` 由本文件补：按 N01 契约，规则层是纯函数、不取时钟，命中时刻由编排层填
（服务层缺它直接抛 `AlertContractError`，那是契约破损不是用户输入问题）。

一点边界：**不 mock 库**。每条用例在测试库上开一个真事务，读回来的是库里真实的行，
结束时整体回滚（`conftest` 的 `db` fixture 同款做法），不需要 TRUNCATE，也不会碰到开发库。

`alembic/versions/0012`（追加 `risk_events.detail` 与三处 `safety_alert` CHECK）落地前，
本文件**预期红**：红的会是 `UndefinedColumn: risk_events.detail does not exist` 或
`CheckViolation`，属于环境未就绪，不是用例写错。看到这两类错误时先 `alembic upgrade head`。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Elder, Notification, RiskEvent, WorkOrder
from app.router.rules import match_l0
from app.services.alert_service import handle_safety_alert
from conftest import require_db_reachable

#: 与 `app/router/safety.py` 的规则表同一口径；写成常量是为了断言失败时一眼看出期望值。
EMERGENCY_RULE_ID: str = "safety_emergency"

#: 与 `alert_service.SAFETY_ALERT_TYPE` 同值。刻意不 import 那个常量：用例要钉住的是
#: **库里的字面值**，import 过来就变成「实现改成什么就断言什么」，CHECK 枚举也就白加了。
SAFETY_ALERT_TYPE: str = "safety_alert"

#: 真实线程号形状（`THREAD_ID_PATTERN` 允许的字符集），不写死成简单字符串。
THREAD_ID: str = "thread:day08:safety:0001"

PHONE: str = "13800138000"


@pytest.fixture
async def db_engine():
    """每条用例一个 NullPool 引擎：测试库连接不跨用例复用，避免串状态。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    await require_db_reachable(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine):
    """一个外层事务里的会话：用例结束时整体回滚，测试库不留行。"""
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def resident(db: AsyncSession) -> uuid.UUID:
    """演示长者。只给非空且无 server_default 的列（与 memory 系列用例同口径）。"""
    elder_id = uuid.uuid4()
    db.add(Elder(elder_id=elder_id, name=f"演示长者{uuid.uuid4().hex[:6]}", status="active"))
    await db.flush()
    return elder_id


def _alert_decision(text: str, *, occurred_at: datetime, elder_id: uuid.UUID):
    """用真规则表产出一次命中，再补上编排层负责的字段（身份 + 命中时刻）。"""
    decision = match_l0(text)
    assert decision is not None, f"「{text}」没有命中 L0 规则：本用例的前提塌了"
    assert decision.intent == "alert", f"「{text}」应判为 alert，实际 {decision.intent}"
    hit = decision.alert
    assert hit is not None, "safety 命中却没带 SafetyHit：证据层缺口，先修路由再谈落库"
    return replace(
        decision,
        elder_id=str(elder_id),
        thread_id=THREAD_ID,
        alert=replace(hit, occurred_at=occurred_at),
    )


async def _count(db: AsyncSession, model, elder_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(model).where(model.elder_id == elder_id)
    )
    return result.scalar_one()


async def _only(db: AsyncSession, model, elder_id: uuid.UUID):
    result = await db.execute(select(model).where(model.elder_id == elder_id))
    rows = list(result.scalars().all())
    assert len(rows) == 1, f"{model.__tablename__} 应有 1 行，实际 {len(rows)} 行"
    return rows[0]


# --------------------------------------------------------------- ① 一次命中写三行且互相指得上


async def test_one_hit_writes_risk_event_work_order_and_notification(
    db: AsyncSession, resident: uuid.UUID
) -> None:
    """一次命中 → 风险事件 + 红单 + 通知，且三者的关联字段逐一对得上。"""
    occurred_at = datetime(2026, 11, 10, 3, 12, 41, 500_000, tzinfo=UTC)
    decision = _alert_decision("我胸口疼", occurred_at=occurred_at, elder_id=resident)

    outcome = await handle_safety_alert(
        db,
        decision=decision,
        elder_id=resident,
        thread_id=THREAD_ID,
        trace_id="trace-day08-safety-1",
    )

    assert outcome is not None, "命中 alert 的决策必须产出 AlertOutcome"
    assert outcome.created is True and outcome.duplicate is False
    assert outcome.category == "emergency"
    assert outcome.minute_bucket == occurred_at.replace(second=0, microsecond=0)

    risk = await _only(db, RiskEvent, resident)
    assert risk.type == SAFETY_ALERT_TYPE, "类型必须是稳定枚举 safety_alert（细分走 detail）"
    assert risk.severity == "red"
    assert risk.source_type == "rule"
    assert risk.status == "open"
    assert risk.source_id == outcome.idempotency_key, "幂等键要落在 source_id 上"
    assert risk.created_at == occurred_at, "created_at 取命中时刻，不是落库时刻"

    # `detail` 是可空列（0012 特意没给历史行补，编一份假证据比空更坏）。行建了而它是
    # NULL，说明写入侧没填证据 —— 那是 `alert_service` 的缺陷，不是用例的假设错。
    assert risk.detail is not None, "风险事件建出来了但 detail 是 NULL：写入侧没填命中证据"
    detail = risk.detail
    assert detail["module"] == "safety"
    assert detail["rule_id"] == EMERGENCY_RULE_ID
    assert detail["category"] == "emergency"
    assert detail["severity"] == "red"
    assert detail["thread_id"] == THREAD_ID
    assert "胸口疼" in detail["transcript_excerpt"], "detail 要带可复核的命中片段"

    order = await _only(db, WorkOrder, resident)
    assert order.type == SAFETY_ALERT_TYPE
    assert order.severity == "red"
    assert order.status == "new", "红单必须留在「待接手」队列，不许直接置成处理中"
    assert order.event_id == risk.event_id, "派单要指回那条风险事件（TD §3.4）"
    assert str(risk.event_id) in order.note, "备注里要能反查到事件，排障时不用再猜"
    assert order.order_no.startswith("WO-")

    notification = await _only(db, Notification, resident)
    assert notification.kind == SAFETY_ALERT_TYPE
    assert notification.severity == "red"
    assert notification.status == "generated"
    payload = notification.payload or {}
    assert payload["risk_event_id"] == str(risk.event_id)
    assert payload["order_no"] == order.order_no
    assert payload["rule_id"] == EMERGENCY_RULE_ID
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "胸口疼" not in serialized, "通知 payload 只放结构化事实，不放原话（另一端是家属/护工）"


# --------------------------------------------------------------- ② detail 落库前必须脱敏


async def test_real_excerpt_window_never_lands_a_whole_phone_number(
    db: AsyncSession, resident: uuid.UUID
) -> None:
    """真链路：长者把手机号说在触发词旁边，库里也不许出现完整号码。

    这里**不断言打码形态**，因为 `safety.py` 的 `transcript_excerpt` 是命中片段前后各
    8 字，11 位号码必然被窗口切碎（实测落库是 `我胸口疼，我儿子电话是1`）——「原号码
    进不了库」是窗口切片的属性，不是脱敏的功劳。本用例守的是底线：**今后谁把窗口放宽/
    改成整句，也必须先让这里变红**，再补脱敏；而不是靠「反正切碎了」蒙混过去。
    """
    occurred_at = datetime(2026, 11, 10, 9, 5, 0, tzinfo=UTC)
    decision = _alert_decision(
        f"我胸口疼，我儿子电话是{PHONE}", occurred_at=occurred_at, elder_id=resident
    )

    await handle_safety_alert(
        db, decision=decision, elder_id=resident, thread_id=THREAD_ID, trace_id=None
    )

    risk = await _only(db, RiskEvent, resident)
    assert risk.detail is not None, "风险事件建出来了但 detail 是 NULL：写入侧没填命中证据"
    serialized_detail = json.dumps(risk.detail, ensure_ascii=False)
    assert PHONE not in serialized_detail, "原号码进了库：窗口或脱敏有一处失守（AGENTS §4.2）"


async def test_redaction_masks_a_phone_that_does_reach_the_excerpt(
    db: AsyncSession, resident: uuid.UUID
) -> None:
    """防御层：上游若把整句（含完整号码）塞进 `transcript_excerpt`，写入侧必须打码。

    这条刻意**绕过规则层去构造 hit**：规则层的窗口切碎号码是「巧合」，而服务层面对
    「完整号码真的来了」时会不会打码，才是脱敏实现本身。两头都断：库里搜不到原号码，
    且出现 `mask_phone` 的打码形态（不是整段星号、也不是整句丢掉——那样人就读不出原话）。
    """
    occurred_at = datetime(2026, 11, 10, 9, 6, 0, tzinfo=UTC)
    decision = _alert_decision("我胸口疼", occurred_at=occurred_at, elder_id=resident)
    hit = decision.alert
    assert hit is not None
    decision = replace(
        decision,
        alert=replace(hit, transcript_excerpt=f"我胸口疼，我儿子电话是{PHONE}"),
    )

    await handle_safety_alert(
        db, decision=decision, elder_id=resident, thread_id=THREAD_ID, trace_id=None
    )

    risk = await _only(db, RiskEvent, resident)
    assert risk.detail is not None, "风险事件建出来了但 detail 是 NULL：写入侧没填命中证据"
    serialized_detail = json.dumps(risk.detail, ensure_ascii=False)
    assert PHONE not in serialized_detail, "手机号原样进库：脱敏没生效（AGENTS §4.2）"
    excerpt = str(risk.detail.get("transcript_excerpt", ""))
    assert "胸口疼" in excerpt, "脱敏不该把整句吞掉，人还要能读"
    assert "138****8000" in excerpt, "应走既有 mask_phone 口径，而不是整段变星号"


# --------------------------------------------------------------- ③ 同分钟重复命中不重复建单


async def test_same_minute_hit_creates_no_second_round_of_rows(
    db: AsyncSession, resident: uuid.UUID
) -> None:
    """同一分钟、同一线程、同一规则再命中一次 → 幂等命中，三张表都不新增行。"""
    occurred_at = datetime(2026, 11, 10, 3, 12, 10, tzinfo=UTC)
    decision = _alert_decision("我胸口疼", occurred_at=occurred_at, elder_id=resident)

    first = await handle_safety_alert(
        db, decision=decision, elder_id=resident, thread_id=THREAD_ID, trace_id=None
    )
    assert first is not None and first.created is True
    counts = (
        await _count(db, RiskEvent, resident),
        await _count(db, WorkOrder, resident),
        await _count(db, Notification, resident),
    )
    assert counts == (1, 1, 1)

    second = await handle_safety_alert(
        db,
        decision=decision,
        elder_id=resident,
        thread_id=THREAD_ID,
        trace_id=None,
    )

    assert second is not None
    assert second.created is False and second.duplicate is True
    assert second.risk_event_id == first.risk_event_id, "重复命中要指回既有事件，不是新造一条"
    new_counts = (
        await _count(db, RiskEvent, resident),
        await _count(db, WorkOrder, resident),
        await _count(db, Notification, resident),
    )
    assert new_counts == counts, "重复命中产生了新行：幂等键没落在 source_id 上"


# --------------------------------------------------------------- ④ 跨分钟是新的告警


async def test_next_minute_is_a_new_alert(db: AsyncSession, resident: uuid.UUID) -> None:
    """同一句话隔一分钟再喊一次是新告警：键里含分钟，不能被上一分钟的幂等键吞掉。"""
    occurred_at = datetime(2026, 11, 10, 3, 12, 55, tzinfo=UTC)
    later = occurred_at + timedelta(minutes=1)
    first_decision = _alert_decision("我胸口疼", occurred_at=occurred_at, elder_id=resident)
    later_decision = _alert_decision("我胸口疼", occurred_at=later, elder_id=resident)

    first = await handle_safety_alert(
        db, decision=first_decision, elder_id=resident, thread_id=THREAD_ID, trace_id=None
    )
    second = await handle_safety_alert(
        db, decision=later_decision, elder_id=resident, thread_id=THREAD_ID, trace_id=None
    )

    assert first is not None and second is not None
    assert first.created is True and second.created is True
    assert first.idempotency_key != second.idempotency_key
    counts = (
        await _count(db, RiskEvent, resident),
        await _count(db, WorkOrder, resident),
        await _count(db, Notification, resident),
    )
    assert counts == (2, 2, 2), "跨分钟只该各多一行，不能连锁多建"
