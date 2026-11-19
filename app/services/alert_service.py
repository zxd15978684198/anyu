"""安全一票否决的告警落库链路（迭代 8 N10，FR-ALR-01／FR-ALR-02）。

为什么需要这一层
----------------
`app/router/safety.py` 只回一个**纯判定**：命中哪条规则（`SafetyHit`）。真正要落库的
三件事——风险事件、派单、通知——必须由服务层在一个事务里写完，否则会出现
「有事件没单」「有单没通知」的半截状态，值班护工看到的与库里记的不是一回事。

一次命中写三张表（同事务，任一失败整批回滚）
--------------------------------------------
1. ``risk_events``：`type='safety_alert'`、`severity='red'`、`source_type='rule'`、
   `source_id=幂等键`，`status='open'`，`created_at` 取**命中时刻**而不是落库时刻
   （事后按时间统计才不会把夜里 03:12 的呼救记成 09:00 的批量作业）。
2. ``work_orders``：`type='safety_alert'`、`severity='red'`、`status='new'`，
   `event_id` 指向上面那条风险事件（TD §3.4：这一列只指 `risk_events`），
   `assignee_id` 填该长者的主责护工（`elder_staff_assignments.relation='primary_care'`，
   没有就不派、留空由值班台接）。
3. ``notifications``：一条 `kind='safety_alert'`、`severity='red'` 的红色通知，
   `assignee_staff_id` 同上；payload 只放**结构化事实**（rule_id / category /
   risk_event_id / order_no / trace_id），**不放原话**。
4. ``notifications``（FR-ALT-01，task-12 追加）：若这位长者有在职家属，再建**一条**
   家属通知，`payload.audience='guardian'` + `payload.recipients=[guardian_id…]`，
   幂等键 `(elder_id, risk_event_id)`。为什么是第二条而不是把家属塞进第 3 条：
   第 3 条是派单凭据（`assignee_staff_id` 只能指员工），家属的收件人、可见性与
   「谁已读」都不同；两条同行会让「派给谁」和「告知谁」在一行里说不清。
   家属侧只给标题 + 事件类型 + 时间，**不夹带原话**（PRD 177 白名单）。

三类命中只差 `detail.category`
-----------------------------
`emergency` / `harm` / `request_human` 三类的处置动作并不相同（急症与自伤都是红单，
但话术与升级路径不同），而「一条告警是什么」必须能事后按类型统计。所以**类型字段
一律是稳定枚举 `safety_alert`**，细分一律落在 `detail.category`——把三类塞进
`red_emergency` / `sos` / `follow_up` 会让按 type 的统计全部失真，也让「安全命中」
这条链路在数据里消失。（`safety_alert` 三处 CHECK 由迁移 0012 追加，见 N02。）

幂等
----
键 = `(elder_id, thread_id, rule_id, occurred_at 取分钟)`，落成
``source_id = uuid5(NAMESPACE_URL, "anyu:rule:{rule_id}:{thread_id}:{minute}")``。
选 uuid5 而不是「按 created_at 分钟窗查重」的理由：后者依赖落库与应用服务器时钟
一致，一有延迟就漏判（同一句呼救建两张单）或误判（隔一分钟的两句只建一张）。
uuid5 让「是不是同一次告警」变成一次等值查询，且 `source_id` 列本来就要求 UUID——
**rule_id 是文本，塞不进这一列**，这是 N01 契约里唯一一处无法照抄的地方。

脱敏
----
`SafetyHit.transcript_excerpt` 是长者原话切片，写库前**必须**过
`app/core/redact.py`（本模块不自己写正则，AGENTS §4.2）。落库的是脱敏后的文本，
原话只在内存里活过一次函数调用。

调用边界
--------
本模块属于 `app/services/`（唯一写库层）：只 flush、不 commit，事务边界由调用方
（对话编排 / 后续的 `dialogue_service` 接线）持有。`decision.alert is None` 时：
`safety.py` 判定为安全命中却丢了 hit → 抛 `AlertContractError`（契约破损，宁可炸在
测试里也不要静默放过一次呼救）；普通决策（`intent != 'alert'`）返回 ``None``，
因为那不是本函数该处理的东西。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redact import redact_mapping, redact_text
from app.models import ElderStaffAssignment, RiskEvent, WorkOrder
from app.router.decision import RouteDecision
from app.services import notification_service, work_order_service

logger = logging.getLogger(__name__)

#: 安全命中的告警意图值（与 `app/router/safety.py` 的 `ALERT_INTENT` 同一口径）。
ALERT_INTENT = "alert"

#: 三张表统一的类型值；细分走 `detail.category`（迁移 0012 追加 CHECK 枚举）。
SAFETY_ALERT_TYPE: str = "safety_alert"

#: 告警来源：规则层命中（不是按钮、不是传感器）。
SAFETY_SOURCE_TYPE: str = "rule"

#: 「风险事件 + 工单」两张表都合法、且语义一致的类型值。
#:
#: 为什么要有这个闭集：`handle_safety_alert` 会把同一个值同时写进 `risk_events.type`
#: 与 `work_orders.type`，而两张表的 CHECK 并不相同（工单多 `drug_question` 等，
#: 风险事件多 `med_risk`）。若调用方传了一个只在工单表合法的值（如 `follow_up`），
#: 失败会发生在 flush 时、报成 IntegrityError——那时候已经分不清是代码写错了还是
#: 数据坏了。这里提前在纯 Python 层拒掉，错误信息直接点出「两张表都要合法」。
ALERT_TYPE_VALUES: tuple[str, ...] = (
    "safety_alert",
    "sos",
    "health_alert",
)

#: 三类命中（与 `SafetyHit.category` 的闭集逐字一致）。
CATEGORY_EMERGENCY: str = "emergency"
CATEGORY_HARM: str = "harm"
CATEGORY_REQUEST_HUMAN: str = "request_human"
SAFETY_CATEGORIES: tuple[str, ...] = (
    CATEGORY_EMERGENCY,
    CATEGORY_HARM,
    CATEGORY_REQUEST_HUMAN,
)

#: 通知 payload 里的模块标记（家属端/照护台按它分组，不必解析 kind 之外的字段）。
PAYLOAD_MODULE: str = "safety"

#: 主责护工的绑定关系值（`elder_staff_assignments.relation`）。
PRIMARY_CARE_RELATION: str = "primary_care"

#: 幂等键命名空间前缀；只影响 uuid5 的输入串，改它等于让全部历史告警「看起来不同」。
IDEMPOTENCY_NAMESPACE: str = "anyu:rule"

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


class AlertServiceError(Exception):
    """告警服务的基类异常。"""


class AlertContractError(AlertServiceError, TypeError):
    """上游契约破损：该带 `SafetyHit` 的决策没带，或字段越界。

    这类错误**不是**「用户输入不合法」，而是编排层少填了东西；调用方应当让它
    冒泡到测试/日志，而不是降级成话术继续走。
    """


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    """一次 `handle_safety_alert` 的结果。

    `created=False` 表示命中同一分钟内的重复告警（幂等键已存在），此时三张表都
    不会再写一行；`risk_event_id` 仍是既有那条，`order_no` 会按该事件回查出既有单号
    （一键求助要把「正在处理」的凭据回给前端），而 `work_order_id`/`notification_id`
    只有新建时才保证非空（重复路径不反查这两个 ID，调用方按 `created` 分流）。
    """

    created: bool
    duplicate: bool
    idempotency_key: uuid.UUID
    category: str
    rule_id: str
    minute_bucket: datetime.datetime
    risk_event_id: uuid.UUID
    work_order_id: uuid.UUID | None = None
    notification_id: uuid.UUID | None = None
    order_no: str | None = None
    #: FR-ALT-01 家属那条通知的 id；长者没有在职家属时为 None（合法分支，不是失败）。
    #: 单独一个字段而不是复用 `notification_id`：护工那条与家属那条是两条行，
    #: 调用方/测试都要能分辨「派给谁的通知」与「告知家属的通知」。
    guardian_notification_id: uuid.UUID | None = None


def _as_utc(value: datetime.datetime, *, field: str) -> datetime.datetime:
    """统一成带时区的 UTC；naive 值按 UTC 解释（库里全是 timestamptz）。"""
    if not isinstance(value, datetime.datetime):
        raise AlertContractError(f"{field} 必须是 datetime，收到 {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def minute_bucket(occurred_at: datetime.datetime) -> datetime.datetime:
    """把命中时刻压到分钟（UTC），幂等键与 `source_id` 都用它。"""
    utc = _as_utc(occurred_at, field="occurred_at")
    return utc.replace(second=0, microsecond=0)


def idempotency_key(*, rule_id: str, thread_id: str, occurred_at: datetime.datetime) -> uuid.UUID:
    """`uuid5(NAMESPACE_URL, "anyu:rule:{rule}|{thread}|{minute}")`。

    纯函数，便于单测直接钉住「同分钟同线程同规则 → 同键；跨分钟 → 不同键」。
    """
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise AlertContractError(f"rule_id 必须是非空字符串，收到 {rule_id!r}")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise AlertContractError(f"thread_id 必须是非空字符串，收到 {thread_id!r}")
    bucket = minute_bucket(occurred_at)
    seed = f"{IDEMPOTENCY_NAMESPACE}:{rule_id.strip()}:{thread_id.strip()}:{bucket.isoformat()}"
    return uuid.uuid5(uuid.NAMESPACE_URL, seed)


def _resolve_category(hit: Any) -> str:
    """校验 `SafetyHit.category` 落在三值闭集里。"""
    category = getattr(hit, "category", None)
    if category not in SAFETY_CATEGORIES:
        raise AlertContractError(
            f"SafetyHit.category 只能是 {SAFETY_CATEGORIES}，收到 {category!r}"
            "（分类由 safety.py 按规则映射，服务层不猜）"
        )
    return str(category)


def _resolve_severity(hit: Any) -> str:
    """迭代 8 的红色事件只有一档；`yellow` 预留给以后的分级，现在只接受 red。"""
    severity = getattr(hit, "severity", None) or "red"
    if severity not in ("red", "yellow"):
        raise AlertContractError(f"SafetyHit.severity 只能是 red / yellow，收到 {severity!r}")
    return str(severity)


def _redacted_excerpt(hit: Any) -> str:
    """原话切片过既有脱敏工具；不自己写正则（AGENTS §4.2）。"""
    excerpt = getattr(hit, "transcript_excerpt", "") or ""
    if not isinstance(excerpt, str):
        raise AlertContractError(
            f"SafetyHit.transcript_excerpt 必须是字符串，收到 {type(excerpt).__name__}"
        )
    return redact_text(excerpt) if excerpt else ""


def _redact_free_text(value: Any) -> Any:
    """对结构里的**每一个字符串值**跑 `redact_text`，其余类型原样下钻。

    为什么不能只调 `redact_mapping`（这层包装不许「优化」掉）
    -------------------------------------------------------
    `redact_mapping` 是**按键名**脱敏的：它拿 `SENSITIVE_KEY_RULES` 做键名子串匹配
    （如 `phone` / `id_card` / `token`），命中才把值交给对应 mask；键名不命中就走
    `_redact_value` 原样返回（只递归下钻，不做内容扫描）。

    也就是说下面这条**会原样返回**，手机号一个字符都不遮：

        redact_mapping({"transcript_excerpt": "我胸口疼，电话 13800138000"})
        # → {'transcript_excerpt': '我胸口疼，电话 13800138000'}

    而安全告警要落库的原话切片（`transcript_excerpt` / `matched`）恰恰就是这种形态：
    键名是业务字段名、敏感内容在**值**里。若只套 `redact_mapping`，「脱敏」在自由文本
    这条路径上等于完全失效，且失效得无声无息——函数调了、返回值没变。

    所以这里两层叠加，各管一件事、互不替代：
    1. 本函数按**内容**逐值 `redact_text()`（手机号/身份证/`sk-` 类密钥的正则规则）；
    2. 外层 `_redact_payload` 再调 `redact_mapping` 按**键名**兜结构化字段
       （将来 payload 里若出现 `phone` / `secret` 这类键，仍会被覆盖）。

    改这段前先想清楚：把 `return redact_text(value)` 换成 `return value`，用例
    `test_excerpt_is_redacted_before_it_reaches_the_database` 会红，而
    `test_redaction_call_is_load_bearing` 会告诉你「红线就是这一行」。
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_free_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_free_text(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_free_text(item) for item in value)
    return value


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """自由文本逐值脱敏 + 结构化键名兜底，返回新 dict。"""
    return redact_mapping(_redact_free_text(payload))


def _detail_payload(
    *,
    hit: Any,
    category: str,
    severity: str,
    thread_id: str,
    trace_id: str | None,
    occurred_at: datetime.datetime,
) -> dict[str, Any]:
    """`risk_events.detail` 的内容：结构化事实 + 脱敏后的原话切片。"""
    payload: dict[str, Any] = {
        "module": PAYLOAD_MODULE,
        "rule_id": str(getattr(hit, "rule_id", "")),
        "category": category,
        "severity": severity,
        "matched": redact_text(str(getattr(hit, "matched", "") or "")),
        "thread_id": thread_id,
        "occurred_at": occurred_at.isoformat(),
    }
    if trace_id:
        payload["trace_id"] = trace_id
    excerpt = _redacted_excerpt(hit)
    if excerpt:
        payload["transcript_excerpt"] = excerpt
    # 兜底再扫一遍：结构里若混进了别的敏感值（如长者口述的手机号），一并盖掉。
    return _redact_payload(payload)


async def _primary_care_staff(session: AsyncSession, *, elder_id: uuid.UUID) -> uuid.UUID | None:
    """该长者的主责护工；没有绑定就返回 None（值班台按队列接，不伪造受理人）。"""
    result = await session.execute(
        select(ElderStaffAssignment.staff_id)
        .where(
            ElderStaffAssignment.elder_id == elder_id,
            ElderStaffAssignment.relation == PRIMARY_CARE_RELATION,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _insert_work_order(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    event_id: uuid.UUID,
    severity: str,
    note: str,
    assignee_id: uuid.UUID | None,
    order_type: str = SAFETY_ALERT_TYPE,
) -> WorkOrder:
    """建一张红单——单号生成与撞键重试统一交给 `work_order_service`。

    单号格式（`WO-YYYYMMDD-NNNN`）、`status='new'` / `false_alarm=False`、撞唯一键重算
    一次，这三件事都是 `work_order_service.create_work_order` 的事实，本函数不再各写
    一份（两份实现早晚会漂移，届时单号规则或状态口径只改一处就会分裂）。这里只负责
    把「安全告警」的字段口径喂过去：`type` 默认 `safety_alert`（语音安全命中），
    一键求助（SOS）传 `sos`，好让护工在工单列表里一眼区分「按了按钮」与「嘴上说急症」；
    `event_id` 指上面那条风险事件、主责护工可能不存在（留空由值班台接）。
    """
    return await work_order_service.create_work_order(
        session,
        elder_id=elder_id,
        type=order_type,
        severity=severity,
        note=note,
        event_id=event_id,
        assignee_staff_id=assignee_id,
    )


async def _find_existing(
    session: AsyncSession, *, elder_id: uuid.UUID, key: uuid.UUID
) -> RiskEvent | None:
    """按幂等键反查既有风险事件（`source_id` 上有唯一语义，查询是等值命中）。"""
    result = await session.execute(
        select(RiskEvent).where(
            RiskEvent.elder_id == elder_id,
            RiskEvent.source_type == SAFETY_SOURCE_TYPE,
            RiskEvent.source_id == key,
        )
    )
    return result.scalar_one_or_none()


async def _find_order_no(session: AsyncSession, *, event_id: uuid.UUID) -> str | None:
    """按风险事件反查工单号。

    幂等重复路径原本只回 `risk_event_id`（反正没建新单），但一键求助的调用方要把
    「原来那张单」再念给长者听（「护工已经在路上了」），没有单号就只能干说一句话。
    这里取最新一条：一张风险事件正常对应一张工单，历史上若出现过补单，最新那张才是
    现在有效的那张。
    """
    result = await session.execute(
        select(WorkOrder.order_no)
        .where(WorkOrder.event_id == event_id)
        .order_by(WorkOrder.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _work_order_note(
    *,
    rule_id: str,
    category: str,
    risk_event_id: uuid.UUID,
    thread_id: str,
    trace_id: str | None,
) -> str:
    """工单备注：只放可追溯的结构化标识，**不放原话**（备注会进照护台列表）。"""
    parts = [
        f"安全告警 rule_id={rule_id}",
        f"category={category}",
        f"risk_event_id={risk_event_id}",
        f"thread_id={thread_id}",
    ]
    if trace_id:
        parts.append(f"trace_id={trace_id}")
    return "；".join(parts)


async def handle_safety_alert(
    session: AsyncSession,
    *,
    decision: RouteDecision,
    elder_id: uuid.UUID,
    thread_id: str,
    trace_id: str | None = None,
    work_order_type: str = SAFETY_ALERT_TYPE,
) -> AlertOutcome | None:
    """把一次安全命中落成「风险事件 + 红单 + 通知」，同事务完成。

    参数
    ----
    session
        调用方持有的会话；本函数只 flush，**不 commit**。
    decision
        路由决策；安全命中必须在 `alert` 上带 `SafetyHit`（`app/router/decision.py`）。
    elder_id
        服务端 `runtime_context` 注入的长者 ID（AGENTS §4.9：不从文本里取）。
    thread_id
        本次对话线程；与分钟一起构成幂等键的一部分。
    trace_id
        链路 ID，只写进 detail / 通知 payload / 日志，便于按一次呼救串起三张表。
    work_order_type
        `risk_events.type` 与 `work_orders.type` 共用的类型值，默认 `safety_alert`
        （语音安全命中）。一键求助传 `sos`，护工在列表里就不必靠备注猜「是按键还是
        说话」。刻意做成 keyword-only 带默认值：既有调用点一个都不用改，行为零变化。

    返回
    ----
    `AlertOutcome`（`created` 或 `duplicate`）；非告警决策返回 ``None``。

    异常
    ----
    `AlertContractError`：`intent='alert'` 但没带 hit、category/severity 越界、
    缺 `occurred_at`（命中时刻必须由编排层填，服务层不拿 `now()` 冒充）。
    """
    if not isinstance(elder_id, uuid.UUID):
        raise AlertContractError(f"elder_id 必须是 UUID，收到 {type(elder_id).__name__}")

    intent = getattr(decision, "intent", None)
    hit = getattr(decision, "alert", None)
    if hit is None:
        if intent == ALERT_INTENT:
            raise AlertContractError(
                "intent='alert' 的决策必须带 SafetyHit（app/router/decision.py）；"
                "拿到 None 说明 safety.py 的接线漏了，拒绝静默放过一次安全命中"
            )
        # 普通决策不该走到这里；返回 None 而不是抛错，避免把每一轮闲聊都变成告警。
        return None

    category = _resolve_category(hit)
    severity = _resolve_severity(hit)
    rule_id = str(getattr(hit, "rule_id", "") or "").strip()
    if not rule_id:
        raise AlertContractError("SafetyHit.rule_id 不能为空（告警必须能追溯到规则）")

    # 类型先在这里验：一个只在工单表合法的值会让 risk_events 的 flush 报 IntegrityError，
    # 到那时已经分不清是调用方传错还是数据坏了（见 ALERT_TYPE_VALUES 的注释）。
    if work_order_type not in ALERT_TYPE_VALUES:
        raise AlertContractError(
            f"work_order_type 只能是 {ALERT_TYPE_VALUES}，收到 {work_order_type!r}"
            "（同一个值会写进 risk_events 与 work_orders，两张表的 CHECK 都要满足）"
        )

    occurred_at = getattr(hit, "occurred_at", None)
    if occurred_at is None:
        raise AlertContractError(
            "SafetyHit.occurred_at 必须由编排层填写：服务层不取时钟，"
            "否则同一句呼救在不同机器上会算出不同的幂等键"
        )
    occurred_utc = _as_utc(occurred_at, field="occurred_at")
    bucket = minute_bucket(occurred_utc)
    key = idempotency_key(rule_id=rule_id, thread_id=thread_id, occurred_at=occurred_utc)

    existing = await _find_existing(session, elder_id=elder_id, key=key)
    if existing is not None:
        logger.info(
            "safety alert 重复命中（同一分钟同一线程同一规则）rule_id=%s elder_id=%s key=%s",
            rule_id,
            elder_id,
            key,
        )
        return AlertOutcome(
            created=False,
            duplicate=True,
            idempotency_key=key,
            category=category,
            rule_id=rule_id,
            minute_bucket=bucket,
            risk_event_id=existing.event_id,
            # 既有那张单的单号：一键求助要把「正在处理」的凭据回给前端，
            # 不能让长者连点两下就看到两个不同的结果。
            order_no=await _find_order_no(session, event_id=existing.event_id),
        )

    assignee_id = await _primary_care_staff(session, elder_id=elder_id)
    detail = _detail_payload(
        hit=hit,
        category=category,
        severity=severity,
        thread_id=thread_id,
        trace_id=trace_id,
        occurred_at=occurred_utc,
    )

    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=elder_id,
        type=work_order_type,
        severity=severity,
        source_type=SAFETY_SOURCE_TYPE,
        source_id=key,
        # 命中证据直传：`detail` 由 0012 追加（jsonb / nullable）。这里不写
        # `hasattr` 兜底——列真没了就该当场炸，静默丢证据比报错更坏。
        detail=detail,
        status="open",
        created_at=occurred_utc,
    )
    session.add(event)
    await session.flush()

    order = await _insert_work_order(
        session,
        elder_id=elder_id,
        event_id=event.event_id,
        severity=severity,
        note=_work_order_note(
            rule_id=rule_id,
            category=category,
            risk_event_id=event.event_id,
            thread_id=thread_id,
            trace_id=trace_id,
        ),
        assignee_id=assignee_id,
        order_type=work_order_type,
    )

    payload: dict[str, Any] = {
        "module": PAYLOAD_MODULE,
        "rule_id": rule_id,
        "category": category,
        "severity": severity,
        "risk_event_id": str(event.event_id),
        "order_no": order.order_no,
        "thread_id": thread_id,
        "occurred_at": occurred_utc.isoformat(),
    }
    if trace_id:
        payload["trace_id"] = trace_id
    # 通知走 `notification_service.create_notification` 这唯一入口：状态机、
    # 默认 status、payload 脱敏口径都在那一处，本模块不再自己 `Notification(...)`，
    # 否则就是 AGENTS §4-8 说的「同一个能力造第二套写入口」。
    notification = await notification_service.create_notification(
        session,
        kind=SAFETY_ALERT_TYPE,
        severity=severity,
        elder_id=elder_id,
        payload=_redact_payload(payload),
        assignee_staff_id=assignee_id,
        # FR-ALT-04：一场安全事件只通知一次。重试路径上即便走到「新建事件」分支，
        # 同一 event_id 也不会多插一条（事件本身按分钟后缀去重，这里是通知层兜底）。
        dedupe_key=f"safety_alert:risk_event:{event.event_id}",
    )
    # FR-ALT-01：同一场红色事件还要让家属知道（护工那条是派单凭据，家属这条是告知）。
    # 两条各自幂等、互不替代：家属读通知只按 elder_id 放行，两条其实都在家属列表里，
    # 靠 `payload.audience` 区分「派给护工的」与「告知家属的」——但只有家属那条的
    # 收件人清单是真的（`payload.recipients`），投递与已读才能按家属算。
    # FR-ALT-01 只管红色事件：黄色是内部提醒（`_resolve_severity` 现在只放行 red，
    # 这一层判定是给以后真的开黄档时留的分岔），推给家属只会让家属端被非紧急状况刷屏。
    # 无在职家属时该函数返回 None 且不建行，所以这里的 `None` 是正常分支而不是失败。
    guardian_notification = (
        await notification_service.notify_safety_alert_guardians(
            session,
            elder_id=elder_id,
            risk_event_id=event.event_id,
            category=category,
            severity=severity,
            occurred_at=occurred_utc,
            order_no=order.order_no,
            trace_id=trace_id,
        )
        if severity == "red"
        else None
    )
    guardian_notification_id = (
        guardian_notification.notification_id if guardian_notification is not None else None
    )

    logger.info(
        "safety alert 落库 rule_id=%s category=%s elder_id=%s risk_event_id=%s "
        "order_no=%s notification_id=%s guardian_notification_id=%s",
        rule_id,
        category,
        elder_id,
        event.event_id,
        order.order_no,
        notification.notification_id,
        guardian_notification_id,
    )

    return AlertOutcome(
        created=True,
        duplicate=False,
        idempotency_key=key,
        category=category,
        rule_id=rule_id,
        minute_bucket=bucket,
        risk_event_id=event.event_id,
        work_order_id=order.order_id,
        notification_id=notification.notification_id,
        order_no=order.order_no,
        guardian_notification_id=guardian_notification_id,
    )


__all__ = [
    "ALERT_INTENT",
    "ALERT_TYPE_VALUES",
    "CATEGORY_EMERGENCY",
    "CATEGORY_HARM",
    "CATEGORY_REQUEST_HUMAN",
    "PAYLOAD_MODULE",
    "PRIMARY_CARE_RELATION",
    "SAFETY_ALERT_TYPE",
    "SAFETY_CATEGORIES",
    "SAFETY_SOURCE_TYPE",
    "AlertContractError",
    "AlertOutcome",
    "AlertServiceError",
    "handle_safety_alert",
    "idempotency_key",
    "minute_bucket",
]
