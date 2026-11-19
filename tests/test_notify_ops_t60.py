"""task-60 定点用例：通知升级（N30）/ 重发（N31）/ 10 秒撤销（N32）/ 主诉进健康采集（N33）。

## 为什么单独一个文件

`tests/test_notification_ops.py` 归 gate-pytest（它要覆盖通知状态机的既有口径），
本文件只钉 task-60 这四件事，**名字刻意不同**，避免两条线同时改一个文件。

## 时钟一律注入

四件事的判据都是「墙上时间过了多久」：120s 未确认、10s 内可撤、19:00–20:00 之外。
`sleep` 会让用例慢且不稳，所以服务层与图入口都开了 `now` 形参，用例直接把时间戳
递进去；`escalate_unacknowledged` 的 `created_at` 也由用例自己写（服务端默认值是
`now()`，同一事务里多次插入会拿到同一个时刻，没法测先后）。

## 真库口径

全部走 `tests/conftest.py` 的 `db_conn`（每用例一个事务、结束回滚），不建独立测试库；
`_release_pool` 处置「每用例新事件循环 + 模块级连接池」的 P-03 坑（与
`tests/test_memory_weight.py` 同一条口径）。没有库时 `require_db_connection` 直接 fail。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.auth import RoleForbiddenError
from app.core.runtime_context import Identity
from app.db import engine as app_engine
from app.graphs import health_checkup_graph as graph_mod
from app.models import Elder, Notification
from app.models.audit import AuditLog
from app.router.rules import match_l0
from app.services import notification_service as svc
from app.services.notification_service import (
    ESCALATION_DELAY_SECONDS,
    REVOKE_WINDOW_SECONDS,
    InvalidTransitionError,
    NotificationValidationError,
    RevokeWindowExpiredError,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

# ============================================================
# 夹具与小工具
# ============================================================


@pytest.fixture(autouse=True)
async def _release_pool() -> AsyncIterator[None]:
    """每个用例结束把 `app.db.engine` 池里的连接还掉（仓库已登记的 P-03 坑）。

    `pytest-asyncio` 每用例一个新事件循环，而 `app.db.engine` 是模块级连接池：池里留着
    上一个循环里建的 asyncpg 连接，下一个用例取到它就会报「Event loop is closed」。
    """
    yield
    await app_engine.dispose()


def _session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码；残留由夹具回滚。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


async def _seed_elder(conn: AsyncConnection) -> uuid.UUID:
    """`notifications.elder_id` 的外键目标；占位姓名，不写真实长者数据（AGENTS §4.3）。"""
    elder_id = uuid.uuid4()
    await conn.execute(Elder.__table__.insert().values(elder_id=elder_id, name="测试长者"))
    return elder_id


def _admin() -> Identity:
    """admin 的可见范围是「不过滤」（`_scope_relation` 返回 None），不需要分配关系。"""
    return Identity(kind="staff", staff_id=str(uuid.uuid4()), role="admin")


def _care_worker() -> Identity:
    return Identity(kind="staff", staff_id=str(uuid.uuid4()), role="care_worker")


def _doctor() -> Identity:
    return Identity(kind="staff", staff_id=str(uuid.uuid4()), role="doctor")


def _device(elder_id: uuid.UUID) -> Identity:
    """端侧回执只认设备会话，且设备必须绑着同一位长者（FR-ALT-03）。"""
    return Identity(kind="device", device_id=str(uuid.uuid4()), elder_id=str(elder_id))


async def _push_to_delivered(
    session: AsyncSession, notification: Notification, elder_id: uuid.UUID
) -> None:
    """推送 + 端侧回执：服务端只能「尝试推送」，`delivered` 必须由回执写下。"""
    await svc.deliver(session, _admin(), notification.notification_id)
    await svc.receipt(session, _device(elder_id), notification.notification_id)
    await session.flush()


async def _create(
    session: AsyncSession,
    elder_id: uuid.UUID,
    *,
    severity: str = "green",
    created_at: datetime.datetime | None = None,
) -> Notification:
    """建一条 `generated` 通知，并可选地把 `created_at` 拨到指定时刻。"""
    notification = await svc.create_notification(
        session, kind="health_review", severity=severity, elder_id=elder_id
    )
    if created_at is not None:
        notification.created_at = created_at
        await session.flush()
    return notification


async def _count(session: AsyncSession, elder_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(Notification).where(Notification.elder_id == elder_id)
    return int((await session.execute(stmt)).scalar_one())


async def _delivered_since(
    session: AsyncSession,
    elder_id: uuid.UUID,
    *,
    severity: str,
    created_at: datetime.datetime,
) -> Notification:
    """一条**已投递**、创建时刻可回拨的通知：升级的判据是「发出后没人确认」。

    为什么不直接建 `generated` 的行：`generated` 是「还没发出去」，没人收到自然谈不上
    「没人确认」；升级只处理 `pending` / `delivered`（实现里的
    `_ESCALATION_CANDIDATE_STATUSES`），所以用例必须先真的投递一次。
    """
    notification = await _create(session, elder_id, severity=severity)
    await _push_to_delivered(session, notification, elder_id)
    notification.created_at = created_at
    await session.flush()
    return notification


# ============================================================
# 0. 两个硬数值：120s 与 10s（PRD §4.7 / FR-NTF 口径）
# ============================================================


def test_escalation_and_revocation_windows_are_frozen() -> None:
    """`120` 与 `10` 是验收口径，不是实现细节。

    为什么要单独钉：下面那些用例是拿常量自己算偏移量的（`REVOKE_WINDOW_SECONDS + 1`），
    把 10 改成 3600 时它们仍然自洽地变绿——**常量本身被改**这件事只有这一条能抓住。
    """
    assert ESCALATION_DELAY_SECONDS == 120
    assert REVOKE_WINDOW_SECONDS == 10


# ============================================================
# A. N30：120s 未确认自动升级
# ============================================================


async def test_escalation_promotes_green_to_yellow_after_delay(db_conn: AsyncConnection) -> None:
    """绿色通知过了 120s 没人确认 → 就地抬成黄色，并写下幂等标记。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _delivered_since(
        session,
        elder_id,
        severity="green",
        created_at=now - datetime.timedelta(seconds=ESCALATION_DELAY_SECONDS + 1),
    )

    escalated = await svc.escalate_unacknowledged(session, now=now)

    assert escalated == [notification.notification_id]
    await session.refresh(notification)
    assert notification.severity == "yellow"
    assert notification.payload["escalated_at"] == now.isoformat()
    assert notification.payload["escalated_from"] == "green"
    assert notification.payload["escalation_count"] == 1
    assert notification.payload["escalation_reason"] == "unacknowledged_120s"


async def test_escalation_is_idempotent(db_conn: AsyncConnection) -> None:
    """再跑一轮不许二次抬档：`payload.escalated_at` 就是闸门，悬空 1 秒也不行。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _delivered_since(
        session,
        elder_id,
        severity="green",
        created_at=now - datetime.timedelta(seconds=500),
    )

    assert await svc.escalate_unacknowledged(session, now=now) == [notification.notification_id]
    later = now + datetime.timedelta(seconds=600)
    assert await svc.escalate_unacknowledged(session, now=later) == []

    await session.refresh(notification)
    assert notification.severity == "yellow", "第二轮不许把黄色再抬成红色"
    assert notification.payload["escalation_count"] == 1


async def test_escalation_skips_fresh_and_terminal_rows(db_conn: AsyncConnection) -> None:
    """只处理「还开着」且「确实超时」的行：新建的、已确认的都不许动。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)

    fresh = await _delivered_since(session, elder_id, severity="green", created_at=now)
    acknowledged = await _delivered_since(
        session,
        elder_id,
        severity="red",
        created_at=now - datetime.timedelta(minutes=10),
    )
    await svc.acknowledge(session, _admin(), acknowledged.notification_id)

    assert await svc.escalate_unacknowledged(session, now=now) == []

    await session.refresh(fresh)
    await session.refresh(acknowledged)
    assert fresh.severity == "green"
    assert "escalated_at" not in (fresh.payload or {})
    assert acknowledged.severity == "red"
    assert "escalated_at" not in (acknowledged.payload or {})


async def test_escalation_leaves_red_on_top_but_still_records_it(
    db_conn: AsyncConnection,
) -> None:
    """红色已在顶上：不换档位，但必须留痕（否则「红色也没人管」不可见）。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _delivered_since(
        session,
        elder_id,
        severity="red",
        created_at=now - datetime.timedelta(seconds=ESCALATION_DELAY_SECONDS + 1),
    )

    assert await svc.escalate_unacknowledged(session, now=now) == [notification.notification_id]

    await session.refresh(notification)
    assert notification.severity == "red"
    assert notification.payload["escalated_from"] == "red"
    assert notification.payload["escalation_count"] == 1


async def test_escalation_queue_takes_red_first_when_limit_is_small(
    db_conn: AsyncConnection,
) -> None:
    """派单队列的取数顺序：红档先、同档新的先（三处 `order_by` 统一的第二处）。

    为什么单独钉：`limit` 是「每轮处理多少条」，顺序若退回纯 `created_at` 升序，
    最老的那批绿档会先占掉这一轮名额、红色反而排在后面——列表看着正常，队列行为全错。
    """
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    overdue = now - datetime.timedelta(seconds=ESCALATION_DELAY_SECONDS + 60)

    # 故意让「时间」与「档位」唱反调：绿档最新、红档最旧。这样纯 `created_at DESC`
    # 会先取到绿/黄（本用例必红），只有「档位优先」才取到两条红。
    green_new = await _delivered_since(
        session,
        elder_id,
        severity="green",
        created_at=overdue,
    )
    yellow_mid = await _delivered_since(
        session,
        elder_id,
        severity="yellow",
        created_at=overdue - datetime.timedelta(minutes=10),
    )
    red_new = await _delivered_since(
        session,
        elder_id,
        severity="red",
        created_at=overdue - datetime.timedelta(minutes=20),
    )
    red_old = await _delivered_since(
        session,
        elder_id,
        severity="red",
        created_at=overdue - datetime.timedelta(minutes=30),
    )

    escalated = await svc.escalate_unacknowledged(session, now=now, limit=2)

    # 红档先；两条红里新的先（`created_at DESC`），所以不是「按 id」也不是「按插入序」。
    assert escalated == [red_new.notification_id, red_old.notification_id]

    await session.refresh(green_new)
    await session.refresh(yellow_mid)
    assert "escalated_at" not in (green_new.payload or {})
    assert "escalated_at" not in (yellow_mid.payload or {})


async def test_offline_reuse_prefers_red_then_the_newest(db_conn: AsyncConnection) -> None:
    """断网通知复用查询的排序（三处 `order_by` 统一的第三处）。

    `_find_offline_notification` 用 `limit(1)` 挑一条来复用：顺序若退回纯
    `created_at.desc()`，同一提醒抖动期间先建过红档、后来一条绿档就把红档盖住了，
    复用判定会随抖动次数横跳，最终落到哪条通知上不可解释。
    """
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    state = svc.OFFLINE_NOTIFICATION_STATES[0]

    async def _offline(
        reminder_id: uuid.UUID,
        severity: str,
        created_at: datetime.datetime,
    ) -> Notification:
        notification = await svc.create_notification(
            session,
            kind="medication_escalation",
            severity=severity,
            elder_id=elder_id,
            payload={
                "failure_reason": svc.OFFLINE_FAILURE_REASON,
                "reminder_id": str(reminder_id),
                "state": state,
            },
        )
        notification.created_at = created_at
        await session.flush()
        return notification

    # 第一组：档位不同 → 红的胜出（哪怕它更旧）。
    reminder_a = uuid.uuid4()
    red_old = await _offline(reminder_a, "red", now - datetime.timedelta(minutes=60))
    await _offline(reminder_a, "yellow", now - datetime.timedelta(minutes=30))
    picked = await svc._find_offline_notification(session, reminder_id=reminder_a, state=state)
    assert picked is not None
    assert picked.notification_id == red_old.notification_id

    # 第二组：档位相同 → 新的胜出（这条钉的是 `created_at.desc()` 这个 tiebreak）。
    reminder_b = uuid.uuid4()
    await _offline(reminder_b, "red", now - datetime.timedelta(minutes=60))
    red_new = await _offline(reminder_b, "red", now - datetime.timedelta(minutes=1))
    picked_new = await svc._find_offline_notification(session, reminder_id=reminder_b, state=state)
    assert picked_new is not None
    assert picked_new.notification_id == red_new.notification_id


# ============================================================
# B. N31：重发不新建行
# ============================================================


async def test_resend_reuses_the_same_row(db_conn: AsyncConnection) -> None:
    """重发 = 同一行再投一次：行数不变、`delivered_at` 推到现在、`resend_count` 加一。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    admin = _admin()
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _create(session, elder_id, severity="yellow")
    before = await _count(session, elder_id)

    item = await svc.resend(session, admin, notification.notification_id, now=now)

    assert item.notification_id == notification.notification_id
    assert item.status == "delivered"
    assert item.delivered_at == now
    assert item.payload["resend_count"] == 1
    assert item.payload["last_resent_at"] == now.isoformat()
    assert await _count(session, elder_id) == before, "重发不许新增 notification 行"


async def test_resend_refreshes_delivery_time_without_changing_status(
    db_conn: AsyncConnection,
) -> None:
    """已经 delivered 的再重发：状态仍是 delivered，只把投递时间往后推。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    admin = _admin()
    first = datetime.datetime(2026, 11, 10, 8, 0, tzinfo=datetime.UTC)
    second = first + datetime.timedelta(minutes=5)
    notification = await _create(session, elder_id, severity="green")

    await svc.resend(session, admin, notification.notification_id, now=first)
    item = await svc.resend(session, admin, notification.notification_id, now=second)

    assert item.status == "delivered"
    assert item.delivered_at == second
    assert item.payload["resend_count"] == 2


async def test_resend_rejects_doctor(db_conn: AsyncConnection) -> None:
    """重发是照护排班侧的运营动作：医生能看能确认，但不能重发。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    notification = await _create(session, elder_id, severity="green")

    with pytest.raises(RoleForbiddenError, match="limited to admin and care_worker"):
        await svc.resend(session, _doctor(), notification.notification_id)


async def test_resend_rejects_terminal_status(db_conn: AsyncConnection) -> None:
    """终态不许重发：已确认的事再发一次，等于把闭环重新打开。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    admin = _admin()
    notification = await _create(session, elder_id, severity="yellow")
    await _push_to_delivered(session, notification, elder_id)
    await svc.acknowledge(session, admin, notification.notification_id)

    with pytest.raises(InvalidTransitionError):
        await svc.resend(session, admin, notification.notification_id)


# ============================================================
# C. N32：10 秒内撤销 + 审计
# ============================================================


async def _delivered(
    session: AsyncSession,
    elder_id: uuid.UUID,
    *,
    sent_at: datetime.datetime,
) -> Notification:
    """投递一条通知并把 `delivered_at` 拨到指定时刻（`deliver()` 没开 `now` 形参）。"""
    notification = await _create(session, elder_id, severity="red")
    await _push_to_delivered(session, notification, elder_id)
    notification.delivered_at = sent_at
    await session.flush()
    return notification


async def test_revoke_within_window_marks_failed_and_writes_audit(
    db_conn: AsyncConnection,
) -> None:
    """窗口内撤销：状态转 failed、payload 留痕、审计与业务改动在**同一个事务**里。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    admin = _admin()
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _delivered(
        session, elder_id, sent_at=now - datetime.timedelta(seconds=REVOKE_WINDOW_SECONDS - 7)
    )

    item = await svc.revoke(session, admin, notification.notification_id, reason="误发", now=now)

    assert item.status == "failed"
    assert item.payload["revoked"] is True
    assert item.payload["revoked_by"] == admin.staff_id
    assert item.payload["revoke_reason"] == "误发"
    assert item.payload["revoked_at"] == now.isoformat()

    audit = (
        await session.execute(
            select(AuditLog).where(AuditLog.resource_id == notification.notification_id)
        )
    ).scalar_one()
    assert audit.action == "notification.revoke"
    assert audit.resource_type == "notifications"
    assert audit.actor_type == "staff"
    assert audit.detail is not None
    assert audit.detail["to_status"] == "failed"
    assert audit.detail["reason"] == "误发"


async def test_revoke_after_window_is_rejected(db_conn: AsyncConnection) -> None:
    """过了 10 秒就不是「手滑」，是「已经发生」：拒绝，且状态不动。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    admin = _admin()
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _delivered(
        session, elder_id, sent_at=now - datetime.timedelta(seconds=REVOKE_WINDOW_SECONDS + 1)
    )

    with pytest.raises(RevokeWindowExpiredError) as excinfo:
        await svc.revoke(session, admin, notification.notification_id, now=now)

    assert excinfo.value.window_seconds == REVOKE_WINDOW_SECONDS
    await session.refresh(notification)
    assert notification.status == "delivered"


async def test_revoke_rejects_acknowledged(db_conn: AsyncConnection) -> None:
    """已确认的通知不能撤：确认是人的签字，签字之后只能走作废流程。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    admin = _admin()
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _delivered(session, elder_id, sent_at=now)
    await svc.acknowledge(session, admin, notification.notification_id)

    with pytest.raises(InvalidTransitionError):
        await svc.revoke(session, admin, notification.notification_id, now=now)


async def test_revoke_reason_is_capped(db_conn: AsyncConnection) -> None:
    """撤销原因要进审计，超长直接拒（与 API 的 `max_length=100` 同一口径）。"""
    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    admin = _admin()
    now = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
    notification = await _delivered(session, elder_id, sent_at=now - datetime.timedelta(seconds=3))

    with pytest.raises(NotificationValidationError, match="at most 100 characters"):
        await svc.revoke(session, admin, notification.notification_id, reason="x" * 101, now=now)


# ============================================================
# D. N33：非采集时段「我头晕」进健康采集
# ============================================================


class _FakeGraph:
    """记录 `ainvoke` 的入参，返回「没有待答问题」的最小结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((dict(state), dict(config)))
        return {}


class _ExplodingGraph:
    """窗口内不该起图：被调用即失败（比「没调用」更强的断言）。"""

    async def ainvoke(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("采集窗口内不许另起一条健康采集")


@pytest.mark.parametrize(
    ("clock", "inside"),
    [
        ("18:59", False),
        ("19:00", True),
        ("19:59", True),
        ("20:00", False),
    ],
)
def test_collection_window_boundaries(clock: str, inside: bool) -> None:
    """窗口是左闭右开：19:00 起、20:00 整点已结束（20:00 起由漏采扫描接管）。"""
    hour, minute = (int(part) for part in clock.split(":"))
    moment = datetime.datetime(2026, 11, 10, hour, minute, tzinfo=SHANGHAI)
    assert graph_mod.inside_collection_window(moment) is inside


async def test_complaint_checkup_starts_graph_outside_the_window() -> None:
    """白天一声「我头晕」：起一条 `voice_active` 采集，主诉原文进 state，话术不提诊断。"""
    elder_id = uuid.uuid4()
    day = datetime.date(2026, 11, 10)
    now = datetime.datetime(2026, 11, 10, 15, 0, tzinfo=SHANGHAI)
    graph = _FakeGraph()

    outcome = await graph_mod.start_complaint_checkup(
        elder_id=elder_id, complaint="我头晕", now=now, record_date=day, graph=graph
    )

    assert outcome.action == "started"
    assert outcome.thread_id == graph_mod.health_thread_id(elder_id, day)
    assert outcome.complaint == "我头晕"
    assert "建议问医生" in outcome.notice
    assert len(graph.calls) == 1
    state, config = graph.calls[0]
    assert state["complaint"] == "我头晕"
    assert state["source"] == "voice_active"
    assert config["configurable"]["thread_id"] == outcome.thread_id


async def test_complaint_checkup_defers_to_rest_inside_the_window() -> None:
    """采集窗口内不另起图：返回 `in_window_use_rest`，调用方走既有 REST 入口。"""
    outcome = await graph_mod.start_complaint_checkup(
        elder_id=uuid.uuid4(),
        complaint="我头晕",
        now=datetime.datetime(2026, 11, 10, 19, 30, tzinfo=SHANGHAI),
        record_date=datetime.date(2026, 11, 10),
        graph=_ExplodingGraph(),
    )

    assert outcome.action == "in_window_use_rest"
    assert outcome.prompt is None


async def test_complaint_checkup_rejects_blank_complaint() -> None:
    with pytest.raises(ValueError):
        await graph_mod.start_complaint_checkup(
            elder_id=uuid.uuid4(),
            complaint="   ",
            record_date=datetime.date(2026, 11, 10),
            graph=_FakeGraph(),
        )


def test_rules_route_dizziness_into_safety_alert() -> None:
    """task-43 起「头晕」不再由 rules.py 直出 `health`，而是安全一票否决短路。

    改动理由（PRD FR-HLT-03）：判成普通健康意图时，当天采集若已结束，pad 只回一句
    「好，这次就问到这儿」，既无「建议问医生」也没有人工告警。头晕/眩晕是跌倒与晕厥
    前兆，必须走 `safety.py` 的告警链路（落 risk_events + 红/黄复核单）。
    """
    decision = match_l0("我头晕")
    assert decision is not None
    assert decision.rule_id == "safety_emergency"
    assert decision.intent == "alert"
    assert decision.sensitive is True

    # 模糊修饰不降级成普通意图：仍是告警（严重度由语境判黄），只是不判红。
    vague = match_l0("我有点头晕")
    assert vague is not None
    assert (vague.rule_id, vague.intent) == ("safety_emergency", "alert")

    # 否定句照旧不吞：词族命中但语境判否定 → 不告警、交 L1/L2。
    assert match_l0("我不晕") is None
    weather = match_l0("今天天气怎么样")
    assert weather is not None
    assert weather.intent == "weather_query"
