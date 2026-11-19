"""危险事件的 120 秒超时升级（AC-12）。

## 为什么升「事件」而不是「通知」

AC-12 的原文是：红色事件在 2 分钟内无人确认，就把**原事件**置为 `escalated`
并通知管理员。`notification_service.escalate_unacknowledged` 升的是
`notifications.severity`——那回答的是「这条提醒有多急」；本文件升的是
`risk_events.status`——那回答的是「这起险情有没有人接手」。两者都叫升级，但
对象不同：只做前者时，照护台的事件列表里那一行会一直停在 `open`，`escalated`
这个状态从建表起没有任何一行写入，AC-12 第一条不成立。所以这里补事件侧那半段；
通知不复用、不改写原通知，而是另建一条定向给管理员的红档通知。

## 为什么幂等靠 `status` 本身

候选条件里就带 `status == 'open'`：升级过的行变成 `escalated`，下一轮查询自然
不再选中它。不再加 `escalated_at IS NULL` 这类第二道闸门——两道闸门同时存在时，
任何一处写漏都会让「到底以哪个为准」变成必须读代码才能回答的问题。而
`open -> escalated` 是状态机里唯一的一次跃迁，单靠它就够：调度器重启、手工补跑、
`--reload` 误触发、同一事务里重入，都只会升一次。

## 为什么 `escalated_at` 进 `detail` JSONB 而不加列

`risk_events` 的 DDL 已冻结（技术设计文档 §3），加一列要新迁移、要回填，还要让
所有读 `detail` 的地方知道多了一个字段。而 `escalated_at` 是**审计证据**，不是
查询条件：没有任何业务查询按它过滤（幂等判据已经交给 `status`），它只需要
「以后翻得到」。JSONB 正好承担这个角色，与 `notification_service` 把
`escalated_at` 写进 `payload` 同口径——不加列、不加迁移。

## 事务

只 `add` / `flush`，**绝不 commit**：事务边界归调用方（API 的 `get_session` 或
调度器的任务体），与 `work_order_service` 同一口径。中途抛错时整批回滚，不会留下
「事件升了、通知没建」的半截状态。

## 本文件后来多管的两件事（都只做「事务边界 + 落库」，不做判定）

1. **`acknowledge_event`（ALT-01 的写入口）**：`risk_events.acknowledged_at` 此前
   全仓只读不写——升级扫描一直在问「有没有人确认」，却没有任何路径能把答案写进去。
   补上写入点后，「已确认的红色事件不再升级」这条 AC-12 的前提才真的成立。
2. **两个「无会话链路」的旁路入口**（`:func:`record_safety_alert`` /
   `:func:`record_forbidden_tool_follow_up``）：语音直连链（`app/dialogue_runtime.py`
   与 `app/audio/reply_stub.py`）与 Agent 收口（`app/agents/companion.py`）**手上没有
   数据库会话**，而 `app/services/**` 是唯一写库层（AGENTS §2）。仓库对这种情况已有
   两个先例：`decision_log_service.log_decision` 与 `audit_service.write_audit_isolated`
   ——都是「服务自持事务、失败只记日志」。这两个函数就是同一形状的薄壳：真正建风险
   事件/工单的实现仍在 `alert_service` / `work_order_service`，这里只负责开事务、
   补时钟、把身份归一到 UUID，并在失败时**永不抛**（安全告警写失败不许吃掉长者的
   当轮回复）。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id
from app.models import RiskEvent, StaffAccount, WorkOrder
from app.services import audit_service, notification_service

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行期不 import 路由层
    from app.router.decision import RouteDecision

logger = logging.getLogger(__name__)

ESCALATION_TIMEOUT_SECONDS: Final[int] = 120
"""红色事件多久没人确认就升级（AC-12：2 分钟，PRD 硬性数值）。"""

RED_EVENT_BATCH_LIMIT: Final[int] = 50
"""单轮最多处理多少条：积压时一轮只吃一批，别让一次 tick 变成全表扫描。"""

ADMIN_NOTIFICATION_KIND: Final[str] = "safety_alert"
"""升级通知的 `kind`。

`notifications.kind` 的 CHECK 只有五个值（0012 迁移），没有 `escalation`：加第六个
值要新迁移，而本次任务明令不加迁移。`safety_alert` 是这五个里语义最贴的一个——
它本来就是「安全规则命中、必须人工介入」的那一类（`alert_service` 建单走同一值），
所以这里复用它，并在 `payload.module` 里标明来源是事件升级，读的人不会混。
"""

ACK_ACTION: Final[str] = "risk_event.ack"
"""确认红色事件的动作名（写进 `audit_logs.action`）。"""

FORBIDDEN_TOOL_ORDER_TYPE: Final[str] = "follow_up"
"""越权工具被拒后派单的类型：`follow_up`（`work_orders.type` 的 CHECK 已含此值）。"""

FORBIDDEN_TOOL_ORDER_SEVERITY: Final[str] = "yellow"
"""越权拒绝不是险情，是必须有人看一眼的异常：黄档，排进待办但不抢红单的位置。"""

FORBIDDEN_TOOL_NOTE_LIMIT: Final[int] = 60
"""工单备注里工具名的最大长度：`tool` 来自模型自报，长度不可信，截断后再落库。"""


class RiskEventServiceError(Exception):
    """危险事件服务的基类异常。"""


class RiskEventNotFoundError(RiskEventServiceError, LookupError):
    """事件不存在（API 层映射成 404 `NOT_FOUND`）。"""


class RiskEventValidationError(RiskEventServiceError, ValueError):
    """入参不可用（API 层映射成 422 `VALIDATION_ERROR`）。"""


def _moment(now: datetime.datetime | None) -> datetime.datetime:
    """把注入的时钟归一到 UTC；不注入就是此刻（naive 按 UTC 解释，与存储口径一致）。"""
    if now is None:
        return datetime.datetime.now(datetime.UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=datetime.UTC)


def _as_uuid(value: object) -> uuid.UUID | None:
    """把 UUID / UUID 文本归一成 `uuid.UUID`；`None` 或不可解析时返回 ``None``（不抛）。

    语音链路里同一个 id 有三种形态：`RuntimeContext.elder_id` 是 UUID、
    `RouteDecision.elder_id` 是文本（`app/router/wiring.py` 显式 `str()` 过）、
    设备未绑定时是 `None`。写库前统一收口到这里，免得每个调用点各写一遍 try。
    """
    if isinstance(value, uuid.UUID):
        return value
    if value is None:
        return None
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


async def _active_admin_ids(session: AsyncSession) -> list[uuid.UUID]:
    """取全部「在职管理员」：停用账号不该再被半夜叫起来，`status='disabled'` 排除。"""
    rows = (
        await session.execute(
            select(StaffAccount.staff_id)
            .where(StaffAccount.role == "admin", StaffAccount.status == "active")
            .order_by(StaffAccount.staff_id)
        )
    ).scalars()
    return list(rows.all())


async def _order_no(session: AsyncSession, *, event_id: uuid.UUID) -> str | None:
    """回查这起险情已经派出的那张单（只读，不建单）。

    为什么只读：AC-12 明确「不得再建第二个 risk_event 或工单」。管理员通知里带
    `order_no` 是为了让值班人从通知直接跳到正在处理的那张单；查不到（比如安全
    告警之外的红色事件本来就没有工单）就返回 ``None``，**不在这里补建**。
    """
    return await session.scalar(
        select(WorkOrder.order_no)
        .where(WorkOrder.event_id == event_id)
        .order_by(WorkOrder.created_at.desc())
        .limit(1)
    )


async def escalate_overdue_red_events(
    session: AsyncSession,
    *,
    now: datetime.datetime | None = None,
    timeout_seconds: int = ESCALATION_TIMEOUT_SECONDS,
) -> list[uuid.UUID]:
    """把「红色 + 无人确认 + 已过 2 分钟」的事件就地升级，返回被升级的事件 id。

    四道取数条件缺一不可，每条都对应 AC-12 里的一个词：

    - `severity == 'red'`：只升红档，黄色是在处理时限内的提醒；
    - `status == 'open'`：已 `escalated` / `closed` 的一律不动（幂等就靠它）；
    - `acknowledged_at IS NULL`：「无人确认」——有人认领过的险情不该再报一遍；
    - `created_at <= now - timeout`：「2 分钟」——边界取闭区间（恰好 120 秒即算超时）。

    升完给**每个在职管理员**各建一条红档通知。没有管理员时事件照升、只是没人收到
    通知——把「没人可叫」也变成事件被静默丢掉，比漏一条通知坏得多；但这种情况必须
    `warning` 打出来，否则排障时只能看到「事件升了、没人知道」，无法区分是「没有
    管理员账号」还是「通知代码没跑」。

    为什么是「每个管理员一条」而不是「一条通知收多人」：通知的可见范围对 staff
    靠 `assignee_staff_id` 定向（admin 虽然能看全量，但定向行在待办池里归属清楚），
    一条不带 assignee 的行会让「谁认领」变成说不清的事。幂等不靠行数：候选条件带
    `status == 'open'`，同一事件只会被选中一次，因此不会因为重跑而重复通知。
    """
    moment = _moment(now)
    effective_timeout = max(0, timeout_seconds)
    cutoff = moment - datetime.timedelta(seconds=effective_timeout)
    reason = f"unacknowledged_{effective_timeout}s"

    events = (
        (
            await session.execute(
                select(RiskEvent)
                .where(
                    RiskEvent.severity == "red",
                    RiskEvent.status == "open",
                    RiskEvent.acknowledged_at.is_(None),
                    RiskEvent.created_at <= cutoff,
                )
                .order_by(RiskEvent.created_at, RiskEvent.event_id)
                .limit(RED_EVENT_BATCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    if not events:
        return []

    admin_ids = await _active_admin_ids(session)
    if not admin_ids:
        # 事件照升（下面对每一条都写 escalated），但「没人收到」必须留痕：
        # 静默升级等于把最急的一条线丢进黑洞（PRD AC-12 的「通知管理员」不成立）。
        logger.warning(
            "risk event escalated without admin",
            extra={
                "event_count": len(events),
                "reason": "no_active_admin",
                "timeout_seconds": effective_timeout,
            },
        )

    escalated: list[uuid.UUID] = []
    for event in events:
        # JSONB 列取出来的是原对象：必须 copy 后整体赋值，原地改 flush 检测不到。
        detail = dict(event.detail or {})
        detail["escalated_at"] = moment.isoformat()
        detail["escalation_reason"] = reason
        event.detail = detail
        # `open -> escalated` 既是这次升级，也是下一轮的幂等闸门。
        event.status = "escalated"
        escalated.append(event.event_id)

        # 只读回查：把「正在处理的那张单」的编号带进通知，方便值班人一键跳转。
        order_no = await _order_no(session, event_id=event.event_id)
        for staff_id in admin_ids:
            payload: dict[str, Any] = {
                "module": "risk_event_escalation",
                "risk_event_id": str(event.event_id),
                "event_type": event.type,
                "severity": "red",
                "elder_id": str(event.elder_id),
                "escalated_at": moment.isoformat(),
                "escalation_reason": reason,
            }
            if order_no is not None:
                # 没有工单的红色事件（例如只有事件、没有派单的动作）不带这个键，
                # 让前端能区分「暂无单号」与「单号是空串」。
                payload["order_no"] = order_no
            # 通知走 `notification_service.create_notification` 这唯一入口：状态机、
            # 默认 status、枚举校验都在那一处，本模块不自己 `Notification(...)`。
            await notification_service.create_notification(
                session,
                kind=ADMIN_NOTIFICATION_KIND,
                severity="red",
                elder_id=event.elder_id,
                payload=payload,
                assignee_staff_id=staff_id,
                # FR-ALT-04：同一场红色事件对同一位管理员只该有一条升级通知。
                # 超时扫描每轮都会重算「该不该升级」，没有这个键就会每轮再插一行
                # （2026-11-13 真机库里已出现 3 条同事件 safety_alert）。
                dedupe_key=f"risk_event_escalation:{event.event_id}:{staff_id or 'unassigned'}",
            )

    await session.flush()
    logger.warning(
        "risk event escalated",
        extra={
            "escalated_count": len(escalated),
            "timeout_seconds": effective_timeout,
            "notified_admin_count": len(admin_ids),
            "event_ids": [str(item) for item in escalated],
        },
    )
    return escalated


# ------------------------------------------------------------------ 确认（ALT-01）


async def acknowledge_event(
    session: AsyncSession,
    event_id: uuid.UUID | str,
    staff_id: uuid.UUID | str,
) -> RiskEvent:
    """把一条红色事件标记为「已确认」，并写一条审计。

    为什么只写 `acknowledged_at`、**不改 `status`**：`risk_events.status` 的 CHECK
    只有 `('open','escalated','closed')`（`app/models/work_order.py` + 0012 迁移），
    没有 `acknowledged` 这个取值。要加就得新迁移，而本次修复明令不加迁移；更重要的是
    「有没有人接手」与「这起险情结没结」是两件事——确认意味着人已经到了，险情尚未
    处置完，`status` 仍应是 `open`。升级扫描的判据正是 `acknowledged_at IS NULL`，
    所以改这一列就足以让「已确认仍升级」停下，不需要第六个状态。

    幂等：`acknowledged_at` 已有值（被确认过 / 被升级逻辑以外的路径写过）时**原样返回**，
    不覆写时间戳、不重复写审计——第二次点击确认不该把「第一次确认的时刻」推后，
    那会让「多久才有人接手」这个指标失真。

    事务：只 `flush` 不 `commit`（与 `work_order_service.transition` 同口径）；
    业务写库与审计同生共死，中途抛错整批回滚。

    异常
    ----
    `RiskEventNotFoundError`：事件不存在（API 404）；
    `RiskEventValidationError`：`staff_id` 不是 UUID（API 422）。
    """
    resolved_event = _as_uuid(event_id)
    if resolved_event is None:
        # 路径参数写错（非 UUID 文本）由 FastAPI 先拦成 400；走到这里是调用方传了坏值。
        raise RiskEventNotFoundError(f"risk event {event_id!r} not found")
    resolved_staff = _as_uuid(staff_id)
    if resolved_staff is None:
        raise RiskEventValidationError("staff_id 必须是 UUID")

    event = await session.get(RiskEvent, resolved_event)
    if event is None:
        raise RiskEventNotFoundError(f"risk event {resolved_event} not found")

    if event.acknowledged_at is not None:
        logger.info(
            "risk event already acknowledged",
            extra={
                "trace_id": get_trace_id() or "-",
                "event_id": str(event.event_id),
                "staff_id": str(resolved_staff),
            },
        )
        return event

    event.acknowledged_at = datetime.datetime.now(datetime.UTC)
    await session.flush()

    # 审计与业务同事务：确认动作的可追溯性不能靠日志（日志会随轮转消失），
    # 而 audit_logs 是 AC-14 的落点。actor 一定是员工——家属/设备没有确认权（API 层已拦）。
    await audit_service.write_audit(
        session,
        actor_type=audit_service.actor_type_of("staff"),
        actor_id=resolved_staff,
        action=ACK_ACTION,
        resource_type="risk_event",
        resource_id=event.event_id,
        detail={
            "event_type": event.type,
            "severity": event.severity,
            # 状态没变要写清：读审计的人看到 acknowledged 却仍是 open，得能判断这是设计。
            "status": event.status,
        },
        trace_id=get_trace_id(),
    )

    logger.info(
        "risk event acknowledged",
        extra={
            "trace_id": get_trace_id() or "-",
            "event_id": str(event.event_id),
            "staff_id": str(resolved_staff),
            "severity": event.severity,
            "status": event.status,
        },
    )
    return event


# ------------------------------------------------------------------ 无会话链路的旁路写入


async def record_safety_alert(
    decision: RouteDecision,
    *,
    elder_id: uuid.UUID | str | None,
    thread_id: str | None,
    trace_id: str | None = None,
) -> uuid.UUID | None:
    """把一次安全命中落成「风险事件 + 红单 + 通知」，供**没有会话的语音直链**调用。

    DLG-01 的缺口：`app/dialogue_runtime.py::_route` 与 `app/audio/reply_stub.py::
    _route_and_log` 只写 `decision_logs`，`alert` 分支从未被消费——长者在真实语音里
    说「胸口疼」，库里有决策流水，照护台上却没有任何红单。REST 链的正确形态在
    `app/services/text_turn_service.py::_record_safety_alert`：补命中时刻 →
    `alert_service.handle_safety_alert` → 失败只 warning、不中断当轮回复。本函数是同一形态，
    区别只在于**自己开事务**（调用方没有会话，且 `services` 是唯一写库层）。

    与 `text_turn_service` 对齐的三点：

    - `decision` 原样传（不重建，避免丢掉 `rule_id` / `category` 之外的字段）；
    - `hit.occurred_at` 为空时在这里补 `now(UTC)`——`handle_safety_alert` 明确拒绝
      由它自己拿 `now()` 冒充命中时刻，而 L0 的 `SafetyHit` 建出来这一格就是空的；
    - 失败 / 身份缺失**只记 warning**：告警写失败不许把长者的当轮回复打断（这是本条
      修复的硬约束，比「一定落库」优先）。

    返回新建（或幂等命中）的风险事件 id；跳过或失败返回 ``None``。
    """
    hit = getattr(decision, "alert", None)
    if hit is None:
        return None

    rule_id = str(getattr(hit, "rule_id", "") or "")
    resolved_elder = _as_uuid(elder_id) or _as_uuid(getattr(decision, "elder_id", None))
    if resolved_elder is None:
        # 与 text_turn_service 的 `unbound_device` 同口径：没有身份就没有告警对象，
        # 落一条 warning 而不是编一个 UUID（AGENTS §4.9 身份只从 runtime_context 来）。
        logger.warning(
            "safety_alert_skipped",
            extra={"reason": "unbound_elder", "rule_id": rule_id, "trace_id": trace_id or "-"},
        )
        return None

    resolved_thread = thread_id or getattr(decision, "thread_id", None)
    if not resolved_thread:
        # 一直拒收等于把最急的一条线丢进黑洞；用一个稳定的占位值保证告警照落。
        resolved_thread = "-"
        logger.warning(
            "safety_alert_thread_fallback",
            extra={"rule_id": rule_id, "trace_id": trace_id or "-"},
        )

    stamped = decision
    if getattr(hit, "occurred_at", None) is None:
        stamped = dataclasses.replace(
            decision,
            alert=dataclasses.replace(hit, occurred_at=datetime.datetime.now(datetime.UTC)),
        )

    # 局部 import：`alert_service` → `work_order_service` 这条链条与本模块互不相干，
    # 放模块顶部只会让「谁依赖谁」更难读；仓库里 `companion` / `scheduler` 同口径。
    from app.db import session_factory
    from app.services import alert_service

    try:
        async with session_factory() as session:
            outcome = await alert_service.handle_safety_alert(
                session,
                decision=stamped,
                elder_id=resolved_elder,
                thread_id=str(resolved_thread),
                trace_id=trace_id,
            )
            await session.commit()
    except Exception as exc:  # 旁路写库，任何失败都只能降级成日志
        logger.warning(
            "safety_alert_failed",
            extra={
                "rule_id": rule_id,
                "trace_id": trace_id or "-",
                "error_type": type(exc).__name__,
            },
        )
        return None

    risk_event_id = getattr(outcome, "risk_event_id", None)
    logger.info(
        "safety_alert_recorded",
        extra={
            "rule_id": rule_id,
            "risk_event_id": str(risk_event_id) if risk_event_id else "-",
            "trace_id": trace_id or "-",
        },
    )
    return risk_event_id


async def record_forbidden_tool_follow_up(
    *,
    elder_id: uuid.UUID | str | None,
    tool: str,
    gate: str,
    trace_id: str | None = None,
) -> uuid.UUID | None:
    """模型请求白名单外工具被拒后，补一张 `follow_up` 工单，供 Agent 收口处调用。

    DLG-04 的缺口：`app/agents/companion.py` 对越权工具只写审计 + 播固定话术，
    照护台上看不到任何东西——「模型试图调用一个它不该有的能力」是需要人看一眼的
    异常，不该只留在日志里。

    与 `create_work_order` 的分工：建单实现只有一份（`work_order_service`），本函数
    只负责开事务 + 组备注 + 永不抛。备注**不含长者原话**（工单备注会进照护台列表，
    `api/alert_service` 的 `_work_order_note` 同一脱敏理由），只写工具名、拦截闸门与
    链路号；工具名来自模型自报，按 `FORBIDDEN_TOOL_NOTE_LIMIT` 截断。

    返回工单 id；无身份或建单失败返回 ``None``（失败只 warning，不许打断当轮回复）。
    """
    resolved_elder = _as_uuid(elder_id)
    if resolved_elder is None:
        logger.warning(
            "forbidden_tool_order_skipped",
            extra={"reason": "unbound_elder", "gate": gate, "trace_id": trace_id or "-"},
        )
        return None

    tool_name = (str(tool or "").strip() or "unknown")[:FORBIDDEN_TOOL_NOTE_LIMIT]
    gate_name = (str(gate or "").strip() or "denied")[:32]
    note_parts = [f"模型请求越权工具 {tool_name} 被拒", f"gate={gate_name}"]
    if trace_id:
        note_parts.append(f"trace_id={trace_id}")
    note = "；".join(note_parts)

    from app.db import session_factory
    from app.services import work_order_service

    try:
        async with session_factory() as session:
            order = await work_order_service.create_work_order(
                session,
                elder_id=resolved_elder,
                type=FORBIDDEN_TOOL_ORDER_TYPE,
                severity=FORBIDDEN_TOOL_ORDER_SEVERITY,
                note=note,
            )
            await session.commit()
            order_id = order.order_id
    except Exception as exc:  # 旁路建单，失败不能吃掉当轮回复
        logger.warning(
            "forbidden_tool_order_failed",
            extra={
                "gate": gate_name,
                "trace_id": trace_id or "-",
                "error_type": type(exc).__name__,
            },
        )
        return None

    logger.info(
        "forbidden_tool_order_created",
        extra={
            "order_id": str(order_id),
            "order_no": order.order_no,
            "tool": tool_name,
            "gate": gate_name,
            "trace_id": trace_id or "-",
        },
    )
    return order_id


__all__ = [
    "ACK_ACTION",
    "ADMIN_NOTIFICATION_KIND",
    "ESCALATION_TIMEOUT_SECONDS",
    "FORBIDDEN_TOOL_ORDER_SEVERITY",
    "FORBIDDEN_TOOL_ORDER_TYPE",
    "RED_EVENT_BATCH_LIMIT",
    "RiskEventNotFoundError",
    "RiskEventServiceError",
    "RiskEventValidationError",
    "acknowledge_event",
    "escalate_overdue_red_events",
    "record_forbidden_tool_follow_up",
    "record_safety_alert",
]
