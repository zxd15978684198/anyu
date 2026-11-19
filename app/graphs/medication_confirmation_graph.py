"""用药确认子图（PRD §4.5 / FR-MED-02..07、AC-05/06/07/20，技术设计文档 §7.1，迭代 7 · C 卡）。

## 一张图，两个入口

`triggered_by` 取 `scheduler`（定时扫描到点）或 `dialog`（长者说「我要吃药」）时走的是
**同一张图**（TD §7.1）：图的形状只由「第几次触达」决定，与谁触发无关。这样两个入口
不会各自长出一套重试/升级逻辑——新增入口只是多一个建事件的地方。

## 图

```
START → remind ──→ wait_confirm ─┬─ confirmed ──────────────────────→ terminal → END
                                 ├─ refused ────────────────────────→ terminal → END
                                 ├─ 超时 & attempt_no < 4 → remind（attempt_no + 1）
                                 └─ 超时 & attempt_no == 4 ──→ escalate → terminal → END
```

四个节点是 `remind` / `wait_confirm` / `escalate` / `terminal`；`wait_confirm` 里的
`interrupt()` 就是 HITL 停点：长者回应、护工代确认、超时判定，统一以
`Command(resume=...)` 打回来（`resume={"decision": "confirmed" | "refused" | "timeout", "channel": "voice" | "button"}`），
**不新增旁路写库**（计划 §W2）。载荷还可带两个可选键：`response`（长者原话，落
`medication_attempts.response`）与 `triggered_by`（`dialog` 入口用它把「这次确认从哪来」
写回状态，见 AC-35；事件行的 `triggered_by` 列仍是流程来源的权威）。超时不是靠
`sleep(60)` 实现的——60 秒由外部（调度器 `next_retry_at` / 前端计时器 / 测试里的时钟）
推动，图本身不阻塞，因此这里对真实时间零依赖（`wait_seconds` 与 `clock` 都可注入）。

## 每次触达写哪张表

| 节点 | 业务表写入 | 说明 |
| --- | --- | --- |
| `remind` | `medication_attempts`（`(event_id, attempt_no)` 幂等）+ `medication_events.status='waiting_confirm'`、`retry_count`、`graph_thread_id` | 发出即留痕，重试不产生重复行 |
| `wait_confirm` | `medication_attempts` 补 `response` / `response_channel` + `medication_events.status` / `confirmed_at` / `confirm_channel` / `failure_reason` | resume 后写，故重放不会重复写 |
| `escalate` | `work_orders`（`missed_medication` / `red` / `new`）+ `medication_events.status='escalated'`、`terminal_state` | 安全网：失败**向上抛**，不允许静默丢单 |
| `terminal` | `medication_events.terminal_state` / `status` / `failure_reason` / `late_taken` | 终态四值，映射见 `medication_service.TERMINAL_MAP` |

TD §7.1 原文：「图状态只做流程编排，业务事实在业务表」。所以上述写入全部经
`medication_service`（唯一写库层，AGENTS §2.1），节点内不碰 ORM；写库与图状态是
**两条独立事实线**：穿库失败只降级成 `warnings` + 日志（不能因为记不上账就不提醒
长者），唯独 `escalate` 例外——四次都没响应还不派单，等于这条安全网根本没挂上。

## 与健康子图的三处刻意不同

1. **checkpointer 取不到就抛**：`health_checkup_graph.get_health_graph()` 取不到
   checkpointer 时降级 `MemorySaver`（采集中断只丢一次采集）；用药**不允许**丢状态——
   丢了就不知道该第几次触达、有没有升级，直接变成漏服。故 `build_medication_graph()`
   缺省走 `get_checkpointer()`，失败**原样抛出**（`CheckpointerUnavailableError`），
   由调用方决定是告警还是重试（`app/graphs/checkpointer.py` 顶部同款口径）。
2. **节点是 async 且自带写库**：健康图节点是同步纯函数（采集答案与打分不落业务表），
   用药图每个节点都要写业务表，而项目 I/O 一律 async（AGENTS §8）。
3. **不在图里睡 60 秒**：等待由外部时钟推进，图只记 `next_retry_at` 与 `deadline`。

## 线程键

`medication:{elder_id}:{plan_id}:{YYYY-MM-DDTHH:MM}`（UTC，分钟粒度）。同一计划同一到点
时刻只有一条线程，与事件幂等键 `(plan_id, scheduled_at)` 对齐；`elder_id` 进线程键是为了
排查时能直接看出是谁的药（与 `uq_medication_events_plan_id_scheduled_at` 不冲突）。
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.observability import graph_span
from app.services import medication_service

logger = logging.getLogger(__name__)

RTC_CONNECTED = "rtc_connected"
RTC_FAILED = "rtc_failed"
RTC_DEGRADED_WS = "degraded_ws"
RTC_STATUSES = (RTC_CONNECTED, RTC_FAILED, RTC_DEGRADED_WS)
"""`rtc_status` 三值（TD §7.1 图状态字段；`degraded_ws` = 外呼失败但 WS 文本送达）。"""

DECISION_CONFIRMED = "confirmed"
DECISION_REFUSED = "refused"
DECISION_TIMEOUT = "timeout"
DECISIONS = (DECISION_CONFIRMED, DECISION_REFUSED, DECISION_TIMEOUT)

_RECEIVED_RECEIPTS = frozenset({"card_rendered", "tts_finished"})
"""算「页面确实收到过」的回执；`none` 不算，用于 AC-20 的终态分流。"""

_CONFIRM_ALIASES = {
    "confirmed": DECISION_CONFIRMED,
    "taken": DECISION_CONFIRMED,
    "taken_reported": DECISION_CONFIRMED,
    "yes": DECISION_CONFIRMED,
    "吃了": DECISION_CONFIRMED,
    "已服用": DECISION_CONFIRMED,
}
_REFUSE_ALIASES = {
    "refused": DECISION_REFUSED,
    "refuse": DECISION_REFUSED,
    "denied": DECISION_REFUSED,
    "no": DECISION_REFUSED,
    "不吃": DECISION_REFUSED,
    "拒服": DECISION_REFUSED,
}
_TIMEOUT_ALIASES = {
    "timeout": DECISION_TIMEOUT,
    "no_response": DECISION_TIMEOUT,
    "expired": DECISION_TIMEOUT,
    "超时": DECISION_TIMEOUT,
}

_REFUSE_PATTERN = re.compile(r"(不|别|拒绝)(想?吃|服|要)|拒服")
_NOT_YET_PATTERN = re.compile(r"(没|未|还没|还没有|尚未)(吃|服)")
_CONFIRM_PATTERN = re.compile(r"吃(过|完|好|啦|下去)|吃了|已服用|服过|服用了")
"""自由口语里的结论词：对话入口拿到的是长者的整句话，不是干净的一个词。

`_CONFIRM_ALIASES` 那套是**整串相等**（给按钮/前端传枚举用），这里做的是「这句话里
说了什么」，两套语义不同，不是重复实现。用正则而不是 `in`：长者说的是「我吃过药了」，
「吃过」与「了」之间还夹着宾语，单纯包含匹配会漏。
"""


def _free_text_decision(text: str) -> str | None:
    """从一句口语里认出结论；认不出返回 `None`，由调用方如实说「没听清」。

    判定顺序是三个否定优先的先后问题，不是随手排的：

    1. 先说「不吃 / 拒服」→ 拒服。必须排在最前：「我不吃了」同时含「吃了」，
       若先认肯定词，长者一句拒服会被记成已服药——这是用药安全上的反向错误。
    2. 再说「没吃 / 未服」→ 认不出。这不是拒服也不是已服，只能当没结论，
       交给主控 Agent 继续问；猜成任何一侧都会落错事实。
    3. 最后才认「吃过 / 吃完 / 吃了 / 已服用」。
    """
    if _REFUSE_PATTERN.search(text):
        return DECISION_REFUSED
    if _NOT_YET_PATTERN.search(text):
        return None
    if _CONFIRM_PATTERN.search(text):
        return DECISION_CONFIRMED
    return None


CONFIRM_PROMPT = "您吃了吗？吃完跟我说一声「吃了」，或者点一下屏幕上的「已服用」。"
"""`wait_confirm` 挂起时给前端的提示语（长者端大字 + 语音）。"""

REMINDER_TEMPLATES = (
    "该吃药啦：{drug}。吃完了跟我说一声「吃了」，或者点一下屏幕上的「已服用」。",
    "提醒您一下，{drug} 还没确认呢。吃完记得跟我说一声。",
    "已经是第 {attempt} 次提醒了，{drug} 记得吃呀。",
    "最后一次提醒：{drug} 请尽快服用。如果您已经吃过了，请告诉我。",
)
"""四次触达的话术（T0/+5/+10/+15）：越到后面越强调，但**不吓人、不诊断**（AGENTS §4.4）。"""

DEFAULT_WAIT_SECONDS = 60

UNSET = object()
"""哨兵：区分「没传参数」与「显式传 None」。"""

UNSET_SESSION_FACTORY = UNSET
"""哨兵：区分「没传 session_factory（用生产默认）」与「显式传 None（不写业务表）」。"""


# ---------------------------------------------------------------------------
# 投递面（注入）：图不碰 WebRTC / WS
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReminderEvent:
    """一次触达要「对谁说、说什么药、第几次」——投递实现的入参，不含传输细节。"""

    elder_id: str
    reminder_id: str
    plan_id: str
    attempt_no: int
    scheduled_at: str
    drug_snapshot: Mapping[str, Any] = field(default_factory=dict)
    thread_id: str | None = None

    @property
    def drug_label(self) -> str:
        name = str(self.drug_snapshot.get("drug_name") or "您的药")
        dose = str(self.drug_snapshot.get("dose") or "").strip()
        return f"{name}（{dose}）" if dose else name


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """一次投递的结果；三列都直接落 `medication_attempts` / 图状态。"""

    receipt: str = "none"
    tts_result: str | None = None
    rtc_status: str = RTC_CONNECTED
    detail: str = ""

    def __post_init__(self) -> None:
        if self.receipt not in medication_service.ATTEMPT_RECEIPTS:
            raise ValueError(
                f"receipt 只能是 {medication_service.ATTEMPT_RECEIPTS}，收到 {self.receipt!r}"
            )
        if self.tts_result is not None and self.tts_result not in medication_service.TTS_RESULTS:
            raise ValueError(
                f"tts_result 只能是 {medication_service.TTS_RESULTS} 或 None，收到 {self.tts_result!r}"
            )
        if self.rtc_status not in RTC_STATUSES:
            raise ValueError(f"rtc_status 只能是 {RTC_STATUSES}，收到 {self.rtc_status!r}")


DeliverFn = Callable[[ReminderEvent, str], Awaitable[DeliveryResult]]
"""投递回调：`await deliver(event, text) -> DeliveryResult`。

生产实现由调度器/API 传入（外呼链路 `app/audio/outbound.call_and_speak` 或 WS 推送），
图本身**不 import 任何传输层**：换 WebRTC、换降级 WS、换测试假实现都不动这张图。
"""

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Clock = Callable[[], datetime.datetime]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True, slots=True)
class MedicationRuntime:
    """一张图跑一次运行期需要的全部外部依赖（可注入 = 可测、可换实现）。"""

    deliver: DeliverFn
    session_factory: SessionFactory | None = None
    clock: Clock = _utcnow
    wait_seconds: int = DEFAULT_WAIT_SECONDS
    max_attempts: int = 4

    def now(self) -> datetime.datetime:
        return self.clock()


# ---------------------------------------------------------------------------
# 图状态
# ---------------------------------------------------------------------------


class MedicationConfirmationState(TypedDict, total=False):
    """一次用药确认流程的图状态（TD §7.1 字段集，命名与库列对齐）。

    **只做流程编排**：它是「走到第几步」的游标，不是用药事实的权威存储——
    权威在 `medication_events` / `medication_attempts` / `work_orders`。
    """

    thread_id: str
    elder_id: str
    plan_id: str
    reminder_id: str
    drug_snapshot: dict[str, Any]
    scheduled_at: str
    triggered_by: str
    flow_triggered_by: str
    profile: str
    attempt_no: int
    max_attempts: int
    retry_count: int
    status: str
    next_action: str
    decision: str | None
    last_response: str | None
    failure_reason: str | None
    terminal_state: str | None
    confirmed_at: str | None
    confirm_channel: str | None
    late_taken: bool
    receipt_flags: list[str]
    rtc_status: list[str]
    prompts: list[str]
    delivered_at: str | None
    deadline: str | None
    next_retry_at: str | None
    order_no: str | None
    escalated: bool
    finished: bool
    warnings: list[str]


# ---------------------------------------------------------------------------
# 纯函数：状态换算（无 I/O，测试直接用）
# ---------------------------------------------------------------------------


def medication_thread_id(
    *,
    elder_id: str | uuid.UUID,
    plan_id: str | uuid.UUID,
    scheduled_at: datetime.datetime,
) -> str:
    """线程键 `medication:{elder_id}:{plan_id}:{YYYY-MM-DDTHH:MM}`（UTC，分钟粒度）。

    秒/微秒落在键外：调度器重算一次 `scheduled_at` 不该因此找不到原线程。
    """
    moment = scheduled_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    moment = moment.astimezone(datetime.UTC)
    return f"medication:{elder_id}:{plan_id}:{moment:%Y-%m-%dT%H:%M}"


def state_config(thread_id: str) -> dict[str, dict[str, str]]:
    """LangGraph 调用配置（`{"configurable": {"thread_id": ...}}`）。"""
    if not thread_id:
        raise ValueError("thread_id 不能为空：用药线程键必须由服务端生成（AGENTS §4.9）")
    return {"configurable": {"thread_id": thread_id}}


def reminder_text(event: ReminderEvent, *, max_attempts: int = 4) -> str:
    """第 `attempt_no` 次触达的话术；超出模板数就复用最后一条（不显示内部次数上限）。"""
    index = max(1, min(int(event.attempt_no), len(REMINDER_TEMPLATES))) - 1
    return REMINDER_TEMPLATES[index].format(drug=event.drug_label, attempt=event.attempt_no)


def resolve_timeout_terminal(receipt_flags: Sequence[str]) -> str:
    """四次触达后的终态分流（PRD AC-20 / FR-MED-02）。

    - 至少一次拿到了页面回执（卡片渲染 / 语音播完）→ `missed_unconfirmed`：人是被提醒到
      但没确认，属于「漏服待核实」，要人工回访（AC-06）；
    - 一次回执都没有 → `notification_failed`：**根本没送达**，先查设备/网络，别去质问长者。
    """
    if any(flag in _RECEIVED_RECEIPTS for flag in receipt_flags):
        return "missed_unconfirmed"
    return "notification_failed"


def parse_decision(payload: object) -> tuple[str, str | None, str | None]:
    """把 resume 载荷解析成 `(decision, confirm_channel, response_text)`。

    接受三种写法：`{"decision": "...", "channel": "voice", "response": "..."}`、
    纯字符串（`"吃了"` / `"refused"` / `"timeout"`）、`None`（等价超时）。
    `channel` 只允许 `voice` / `button`（库里 CHECK 同款），其它值按 `None` 落。
    """
    if payload is None:
        return DECISION_TIMEOUT, None, None
    raw: str | None
    channel: str | None = None
    response: str | None = None
    if isinstance(payload, str):
        raw = payload
    elif isinstance(payload, Mapping):
        # `Mapping.get()` 的返回是 `Any | None`：显式声明成 `str | None` 收口，
        # 取值口径不变（非字符串仍由下面的 `str(...)` / `isinstance` 兜住）。
        raw = payload.get("decision")
        channel = payload.get("channel")
        response = payload.get("response")
    else:
        raise ValueError(f"无法解析的 resume 载荷类型：{type(payload).__name__}")

    text = str(raw or "").strip()
    key = text.lower()
    decision: str
    if key in _CONFIRM_ALIASES:
        decision = DECISION_CONFIRMED
    elif key in _REFUSE_ALIASES:
        decision = DECISION_REFUSED
    elif key in _TIMEOUT_ALIASES:
        decision = DECISION_TIMEOUT
    else:
        # 整串对不上再退回「这句话里说了什么」：对话入口拿到的是长者的原话。
        free = _free_text_decision(text)
        if free is None:
            raise ValueError(f"未知的确认结论：{text!r}（只接受 confirmed / refused / timeout）")
        decision = free

    confirm_channel = channel if channel in medication_service.CONFIRM_CHANNELS else None
    response_text = str(response) if isinstance(response, str) and response.strip() else None
    return decision, confirm_channel, response_text


def pending_interrupt(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """从 `ainvoke` 的返回值里取挂起的 HITL 提示（没有挂起就返回 `None`）。"""
    raw = result.get("__interrupt__")
    if not raw:
        return None
    first = raw[0]
    value = getattr(first, "value", first)
    return value if isinstance(value, Mapping) else {"value": value}


def initial_state(
    *,
    thread_id: str,
    elder_id: str | uuid.UUID,
    reminder_id: str | uuid.UUID,
    plan_id: str | uuid.UUID,
    scheduled_at: datetime.datetime | str,
    drug_snapshot: Mapping[str, Any] | None = None,
    triggered_by: str = "scheduler",
    attempt_no: int = 1,
    profile: str | None = None,
) -> MedicationConfirmationState:
    """给调度器/API 用的起步状态（字段与图内节点约定一一对应）。"""
    moment = scheduled_at if isinstance(scheduled_at, str) else scheduled_at.isoformat()
    return MedicationConfirmationState(
        thread_id=thread_id,
        elder_id=str(elder_id),
        plan_id=str(plan_id),
        reminder_id=str(reminder_id),
        drug_snapshot=dict(drug_snapshot or {}),
        scheduled_at=moment,
        triggered_by=triggered_by,
        profile=profile or "",
        attempt_no=attempt_no,
        max_attempts=medication_service.max_attempts(),
        retry_count=max(0, attempt_no - 1),
        status="calling",
        next_action="remind",
        decision=None,
        last_response=None,
        failure_reason=None,
        terminal_state=None,
        confirmed_at=None,
        confirm_channel=None,
        late_taken=False,
        receipt_flags=[],
        rtc_status=[],
        prompts=[],
        delivered_at=None,
        deadline=None,
        next_retry_at=None,
        order_no=None,
        escalated=False,
        finished=False,
        warnings=[],
    )


# ---------------------------------------------------------------------------
# 节点：remind
# ---------------------------------------------------------------------------


async def remind_node(
    state: MedicationConfirmationState, *, runtime: MedicationRuntime
) -> dict[str, Any]:
    """第 `attempt_no` 次触达：调注入的 `deliver` 发提醒 → 写 attempts / events。

    节点内**不碰 WebRTC / WS**（投递面是 `deliver`）；先发后记：先让长者听到提醒，
    再补账——反过来会出现「账记了、提醒没发出去」的漏服。
    """
    attempt_no = int(state.get("attempt_no") or 1)
    if not 1 <= attempt_no <= runtime.max_attempts:
        raise ValueError(
            f"attempt_no 越界：{attempt_no}（上限 {runtime.max_attempts}，"
            "应走 escalate 而不是再提醒）"
        )
    warnings = _warnings(state)
    event = _reminder_event(state, attempt_no)
    text = reminder_text(event, max_attempts=runtime.max_attempts)

    result = await runtime.deliver(event, text)
    now = runtime.now()

    reminder_id = _uuid_or_none(state.get("reminder_id"))
    await _record_attempt(
        runtime,
        warnings=warnings,
        reminder_id=reminder_id,
        attempt_no=attempt_no,
        sent_at=now,
        receipt=result.receipt,
        tts_result=result.tts_result,
    )
    await _update_event(
        runtime,
        warnings=warnings,
        reminder_id=reminder_id,
        status="waiting_confirm",
        retry_count=attempt_no - 1,
        graph_thread_id=_thread_id(state),
    )

    logger.info(
        "medication remind sent thread_id=%s attempt_no=%s receipt=%s rtc_status=%s",
        _thread_id(state),
        attempt_no,
        result.receipt,
        result.rtc_status,
    )
    return {
        "attempt_no": attempt_no,
        "retry_count": attempt_no - 1,
        "status": "waiting_confirm",
        "next_action": "wait_confirm",
        "decision": None,
        "last_response": None,
        "receipt_flags": [*(state.get("receipt_flags") or ()), result.receipt],
        "rtc_status": [*(state.get("rtc_status") or ()), result.rtc_status],
        "prompts": [*(state.get("prompts") or ()), text],
        "delivered_at": now.isoformat(),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 节点：wait_confirm（HITL 停点）
# ---------------------------------------------------------------------------


async def wait_confirm_node(
    state: MedicationConfirmationState, *, runtime: MedicationRuntime
) -> dict[str, Any]:
    """挂起等回应；resume 后按结论分流（确认 / 拒服 / 超时）。

    `interrupt()` 之前的计算在 suspend 时被丢弃、resume 时重放——所以**写库放在
    interrupt 之后**，保证「一次触达只落一次回应」。
    """
    attempt_no = int(state.get("attempt_no") or 1)
    warnings = _warnings(state)
    deadline = runtime.now() + datetime.timedelta(seconds=runtime.wait_seconds)

    payload = interrupt(
        {
            "type": "medication_confirm",
            "thread_id": _thread_id(state),
            "elder_id": state.get("elder_id"),
            "reminder_id": state.get("reminder_id"),
            "plan_id": state.get("plan_id"),
            "attempt_no": attempt_no,
            "max_attempts": runtime.max_attempts,
            "wait_seconds": runtime.wait_seconds,
            "deadline": deadline.isoformat(),
            "prompt": CONFIRM_PROMPT,
        }
    )
    decision, channel, response = parse_decision(payload)
    now = runtime.now()
    reminder_id = _uuid_or_none(state.get("reminder_id"))

    # AC-35：对话入口的确认要能回查「这次确认从哪来」。来源随 resume 载荷带进来，
    # 由节点写回状态——不在外面 `aupdate_state`，那会和 interrupt 的重放顺序纠缠
    # （重放时状态已被外部改过一次，很难说清哪一份才是节点看到的）。
    source = payload.get("triggered_by") if isinstance(payload, Mapping) else None
    source_update: dict[str, Any] = {}
    if source in medication_service.TRIGGERED_BY:
        # 覆盖 `triggered_by`，但把原值留在 `flow_triggered_by`：一次到点流程由谁发起
        # （scheduler）和这次确认从哪来（dialog）是两个问题，后者才是 AC-35 要的。
        source_update = {
            "triggered_by": source,
            "flow_triggered_by": str(state.get("triggered_by") or ""),
        }

    await _record_attempt(
        runtime,
        warnings=warnings,
        reminder_id=reminder_id,
        attempt_no=attempt_no,
        sent_at=now,
        response=response,
        response_channel=channel or ("none" if decision != DECISION_TIMEOUT else None),
    )

    if decision == DECISION_CONFIRMED:
        # AC-08：+5/+10/+15 才确认 = 迟服，标 late_taken 但终态仍是 taken_reported。
        late = attempt_no > 1
        await _update_event(
            runtime,
            warnings=warnings,
            reminder_id=reminder_id,
            status="confirmed",
            confirmed_at=now,
            confirm_channel=channel,
            graph_thread_id=_thread_id(state),
            late_taken=late or None,
        )
        return {
            "decision": DECISION_CONFIRMED,
            "status": "confirmed",
            "next_action": "terminal",
            "last_response": response,
            "confirmed_at": now.isoformat(),
            "confirm_channel": channel,
            "failure_reason": None,
            "late_taken": late,
            "warnings": warnings,
            **source_update,
        }

    if decision == DECISION_REFUSED:
        # AC-07：明确拒服 → 停止重试（不再回到 remind），事件进 escalated 走人工跟进。
        await _update_event(
            runtime,
            warnings=warnings,
            reminder_id=reminder_id,
            status="escalated",
            failure_reason="denied",
            graph_thread_id=_thread_id(state),
        )
        # AC-07 第二半句：只置终态不够，必须建「风险事件 + 护工跟进工单」。
        await _refuse_follow_up(
            runtime,
            warnings=warnings,
            reminder_id=reminder_id,
            channel=channel,
        )
        return {
            "decision": DECISION_REFUSED,
            "status": "escalated",
            "next_action": "terminal",
            "last_response": response,
            "failure_reason": "denied",
            "warnings": warnings,
            **source_update,
        }

    # 超时：还有次数就下一轮，用尽就升级
    await _update_event(
        runtime,
        warnings=warnings,
        reminder_id=reminder_id,
        status="waiting_confirm",
        failure_reason="no_answer",
        graph_thread_id=_thread_id(state),
    )
    if attempt_no >= runtime.max_attempts:
        return {
            "decision": DECISION_TIMEOUT,
            "status": "waiting_confirm",
            "next_action": "escalate",
            "last_response": None,
            "failure_reason": "no_answer",
            "warnings": warnings,
        }

    next_at = medication_service.next_retry_at(
        _scheduled_at(state, fallback=now), attempt_no, runtime.wait_seconds
    )
    return {
        "decision": DECISION_TIMEOUT,
        "status": "waiting_confirm",
        "next_action": "remind",
        "attempt_no": attempt_no + 1,
        "retry_count": attempt_no,
        "last_response": None,
        "failure_reason": "no_answer",
        "next_retry_at": next_at.isoformat(),
        "warnings": warnings,
    }


def route_after_wait(state: MedicationConfirmationState) -> str:
    """`wait_confirm` 之后去哪：`terminal` / `remind` / `escalate`（由节点写好 `next_action`）。"""
    action = str(state.get("next_action") or "")
    return action if action in {"remind", "escalate", "terminal"} else "terminal"


# ---------------------------------------------------------------------------
# 节点：escalate（安全网）
# ---------------------------------------------------------------------------


async def escalate_node(
    state: MedicationConfirmationState, *, runtime: MedicationRuntime
) -> dict[str, Any]:
    """四次触达都没拿到有效回应 → 建红线工单并让事件进升级终态（AC-06）。

    与其它节点**故意不同**：写库失败不吞。提醒发不出去最多少一次触达，可重试；
    四次都没响应还不派单，等于整条安全网没挂上——宁可让调度器看到异常去告警。

    幂等：事件已经是 `escalated` 且有终态就不再重复建单（commit 成功但图没跑完的重放场景）。
    """
    warnings = _warnings(state)
    terminal_state = resolve_timeout_terminal(tuple(state.get("receipt_flags") or ()))
    reminder_id = _uuid_or_none(state.get("reminder_id"))
    order_no: str | None = None

    if runtime.session_factory is None:
        warnings.append("persist_skipped:escalate:no_session_factory")
        logger.error("medication escalate without session_factory thread_id=%s", _thread_id(state))
    else:
        if reminder_id is None:
            raise ValueError("escalate 需要 reminder_id：没有事件无法派单")
        async with runtime.session_factory() as session:
            event = await medication_service.load_event(session, reminder_id)
            if event.status == "escalated" and event.terminal_state is not None:
                warnings.append("escalate:already_escalated")
                logger.info(
                    "medication escalate skipped (already escalated) reminder_id=%s", reminder_id
                )
            else:
                order = await medication_service.escalate_to_work_order(session, event=event)
                order_no = order.order_no
                await medication_service.update_event_state(
                    session,
                    reminder_id=reminder_id,
                    status="escalated",
                    terminal_state=terminal_state,
                    failure_reason="no_answer",
                    graph_thread_id=_thread_id(state),
                )
            await _commit(session)
        logger.warning(
            "medication escalated thread_id=%s order_no=%s order_no_created=%s",
            _thread_id(state),
            order_no,
            order_no is not None,
        )

    return {
        "status": "escalated",
        "terminal_state": terminal_state,
        "next_action": "terminal",
        "escalated": True,
        "order_no": order_no,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 节点：terminal
# ---------------------------------------------------------------------------


async def terminal_node(
    state: MedicationConfirmationState, *, runtime: MedicationRuntime
) -> dict[str, Any]:
    """收口：把终态四值写进事件（映射见 `medication_service.TERMINAL_MAP`）。

    重复执行安全：同样的终态再写一次结果相同（幂等），因此节点级重放不会污染事实。
    """
    warnings = _warnings(state)
    decision = str(state.get("decision") or DECISION_TIMEOUT)
    status = "confirmed" if decision == DECISION_CONFIRMED else "escalated"
    if decision == DECISION_CONFIRMED:
        terminal_state = medication_service.terminal_state_for(DECISION_CONFIRMED)
    elif decision == DECISION_REFUSED:
        terminal_state = medication_service.terminal_state_for(DECISION_REFUSED)
    else:
        terminal_state = str(
            state.get("terminal_state") or medication_service.terminal_state_for("no_response")
        )

    reminder_id = _uuid_or_none(state.get("reminder_id"))
    confirmed_at = _parse_dt(state.get("confirmed_at"))
    await _update_event(
        runtime,
        warnings=warnings,
        reminder_id=reminder_id,
        status=status,
        terminal_state=terminal_state,
        failure_reason=_failure_reason(state, decision),
        confirmed_at=confirmed_at,
        confirm_channel=state.get("confirm_channel") if decision == DECISION_CONFIRMED else None,
        graph_thread_id=_thread_id(state),
        late_taken=bool(state.get("late_taken")) if decision == DECISION_CONFIRMED else None,
    )
    logger.info(
        "medication terminal thread_id=%s decision=%s terminal_state=%s",
        _thread_id(state),
        decision,
        terminal_state,
    )
    return {
        "status": status,
        "next_action": "end",
        "decision": decision,
        "terminal_state": terminal_state,
        "finished": True,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------


def _default_deliver() -> DeliverFn:
    """未注入投递实现时的兜底：**不假装送达**，只记一条 error 并回报 `receipt='none'`。

    生产必须注入（调度器传外呼/WS 推送）。兜底取 `receipt='none'` 而不是
    `card_rendered`：四次都走兜底时终态会算成 `notification_failed`（AC-20「根本没送达」），
    而不是骗人的 `missed_unconfirmed`。
    """

    async def deliver(event: ReminderEvent, text: str) -> DeliveryResult:
        logger.error(
            "medication deliver not injected: thread_id=%s attempt_no=%s text_len=%s",
            event.thread_id,
            event.attempt_no,
            len(text),
        )
        return DeliveryResult(receipt="none", rtc_status=RTC_FAILED, detail="deliver 未注入")

    return deliver


@contextmanager
def _node_span(thread_id: str | None) -> Iterator[None]:
    """节点的 `graph.medication_confirmation` span（S5）。

    `interrupt()` 抛的 `GraphInterrupt` 是 HITL 的**暂停信号**（等走到点的长者确认），
    属正常流程；若让它穿过 `with`，`_SpanScope` 会把 span 标成 ERROR，trace 上看起来
    每次触达都失败了。这里接住它、先按「正常结束」收口 span 再原样抛出，交给 LangGraph
    的循环去暂停；真正的异常仍照常标 ERROR。
    """
    scope = graph_span("medication_confirmation", thread_id=thread_id)
    scope.__enter__()
    try:
        yield
    except GraphInterrupt:
        scope.__exit__(None, None, None)
        raise
    except BaseException as exc:
        scope.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        scope.__exit__(None, None, None)


async def build_medication_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    deliver: DeliverFn | None = None,
    session_factory: SessionFactory | object | None = UNSET_SESSION_FACTORY,
    clock: Clock | None = None,
    wait_seconds: int | None = None,
    max_attempts: int | None = None,
) -> Any:
    """编译用药确认子图。

    - `checkpointer` 缺省 → `await get_checkpointer()`；**失败原样抛出**
      （`CheckpointerUnavailableError`）。与 `get_health_graph()` 的 MemorySaver 降级
      **刻意不同**：健康采集丢一次可以重采，用药丢状态就不知道第几次触达、有没有升级，
      直接等于漏服（见模块 docstring「与健康子图的三处刻意不同」）。
    - `deliver` 缺省 → `_default_deliver()`（只记 error、回报 `receipt='none'`）。
    - `session_factory` 缺省 → `app.db.session_factory`；显式传 `None` = 纯状态机模式
      （不写业务表，仅供单测/演示）。
    - `wait_seconds` / `max_attempts` 缺省读配置 `medication_attempt_wait_seconds` /
      `medication_reminder_attempts`（不硬编码，AGENTS §7）。
    """
    if checkpointer is None:
        from app.graphs.checkpointer import get_checkpointer

        checkpointer = await get_checkpointer()

    factory: SessionFactory | None
    if session_factory is UNSET_SESSION_FACTORY:
        from app.db import session_factory as db_session_factory

        factory = db_session_factory
    else:
        factory = session_factory  # type: ignore[assignment]

    settings = get_settings()
    runtime = MedicationRuntime(
        deliver=deliver or _default_deliver(),
        session_factory=factory,
        clock=clock or _utcnow,
        wait_seconds=int(
            wait_seconds if wait_seconds is not None else settings.medication_attempt_wait_seconds
        ),
        max_attempts=int(
            max_attempts if max_attempts is not None else settings.medication_reminder_attempts
        ),
    )

    # 每个节点外包一层 `graph.medication_confirmation` span（S5）：节点内部已有业务
    # span 的不再复刻，这里只补「走到哪一步」这一层；thread_id 取状态里的服务端线程键
    # （AGENTS §4.9），缺省时 span 只少一个属性、不影响执行。
    async def _remind(state: MedicationConfirmationState) -> dict[str, Any]:
        with _node_span(_thread_id(state)):
            return await remind_node(state, runtime=runtime)

    async def _wait_confirm(state: MedicationConfirmationState) -> dict[str, Any]:
        with _node_span(_thread_id(state)):
            return await wait_confirm_node(state, runtime=runtime)

    async def _escalate(state: MedicationConfirmationState) -> dict[str, Any]:
        with _node_span(_thread_id(state)):
            return await escalate_node(state, runtime=runtime)

    async def _terminal(state: MedicationConfirmationState) -> dict[str, Any]:
        with _node_span(_thread_id(state)):
            return await terminal_node(state, runtime=runtime)

    # `StateGraph` 是 4 个泛型参数的类（StateT / ContextT / InputT / OutputT），
    # 后三个在 langgraph 侧已声明默认值（= StateT / None），故只需填状态类型；
    # 返回值仍用 `Any`，理由同 `health_checkup_graph.build_health_graph()`。
    builder: StateGraph[MedicationConfirmationState] = StateGraph(MedicationConfirmationState)
    builder.add_node("remind", _remind)
    builder.add_node("wait_confirm", _wait_confirm)
    builder.add_node("escalate", _escalate)
    builder.add_node("terminal", _terminal)

    builder.add_edge(START, "remind")
    builder.add_edge("remind", "wait_confirm")
    builder.add_conditional_edges(
        "wait_confirm",
        route_after_wait,
        {"remind": "remind", "escalate": "escalate", "terminal": "terminal"},
    )
    builder.add_edge("escalate", "terminal")
    builder.add_edge("terminal", END)

    return builder.compile(checkpointer=checkpointer)


_GRAPH: Any | None = None


async def get_medication_graph(
    *,
    deliver: DeliverFn | None = None,
    session_factory: SessionFactory | object | None = UNSET_SESSION_FACTORY,
    clock: Clock | None = None,
    wait_seconds: int | None = None,
    max_attempts: int | None = None,
) -> Any:
    """进程内单例（与 `get_health_graph()` 同款用法）；API / 调度器共用一份编译结果。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = await build_medication_graph(
            deliver=deliver,
            session_factory=session_factory,
            clock=clock,
            wait_seconds=wait_seconds,
            max_attempts=max_attempts,
        )
    return _GRAPH


def reset_medication_graph() -> None:
    """丢掉单例（测试与热重载用）；不动 checkpointer 本身。"""
    global _GRAPH
    _GRAPH = None


# ---------------------------------------------------------------------------
# 调用面：启动 / 接续 / 读状态
# ---------------------------------------------------------------------------


async def start_or_resume(graph: Any, *, thread_id: str, **inputs: Any) -> dict[str, Any]:
    """启动或接续一条用药线程，返回最新状态。

    - 没有 checkpoint → 用 `inputs` 起步（至少给 `reminder_id` / `elder_id` / `plan_id`）；
    - 有 checkpoint 且给了 `resume=` → `Command(resume=...)` 打回 HITL 停点
      （`{"decision": ...}` / `"吃了"` / `"timeout"`）；
    - 有 checkpoint 但没给 `resume` → **什么都不做**，只返回当前状态。重启后重放必须
      是这个行为：不能一重启就把提醒再发一遍（长者会被重复催药）。
    """
    config = state_config(thread_id)
    resume = inputs.pop("resume", UNSET)
    snapshot = await graph.aget_state(config)
    values = dict(getattr(snapshot, "values", None) or {})

    if not values:
        seed: dict[str, Any] = _seed_defaults(thread_id=thread_id)
        seed.update(inputs)
        seed["thread_id"] = thread_id
        return dict(await graph.ainvoke(seed, config))

    if resume is not UNSET:
        return dict(await graph.ainvoke(Command(resume=resume), config))

    return await resume_state(graph, thread_id)


async def resume_state(graph: Any, thread_id: str) -> dict[str, Any]:
    """读回线程的图状态（重启恢复 / 照护台查询用）。

    返回值在状态字段之外多三项：`next`（下一个待执行节点，空 = 已跑完）、
    `interrupted`（是否正挂在 HITL 停点）、`thread_id`。`next` 里有 `wait_confirm`
    就说明「提醒已发出，正在等回应」——这是 AC-17/30 断点恢复的判据。
    """
    snapshot = await graph.aget_state(state_config(thread_id))
    values: dict[str, Any] = dict(getattr(snapshot, "values", None) or {})
    pending = tuple(getattr(snapshot, "next", None) or ())
    values["thread_id"] = values.get("thread_id") or thread_id
    values["next"] = pending
    values["interrupted"] = bool(pending)
    return values


CONFIRM_STOP_NODE = "wait_confirm"
"""HITL 停点所在的节点名。`aget_state().next` 含它 = 提醒已发出、正等长者回应。"""


async def pending_confirmation(graph: Any, thread_id: str) -> Mapping[str, Any] | None:
    """该线程是否正停在用药确认的 interrupt 上；是则返回 interrupt 载荷，否则 `None`。

    为什么不复用 `pending_interrupt()`：那个函数读的是 `ainvoke` 返回值里的
    `__interrupt__`，而按钮/对话确认手里只有一个 `thread_id`（甚至可能是别的进程
    重启后拿到的），只能从 checkpoint 里把停点读出来。
    """
    snapshot = await graph.aget_state(state_config(thread_id))
    pending = tuple(getattr(snapshot, "next", None) or ())
    if CONFIRM_STOP_NODE not in pending:
        return None
    for task in getattr(snapshot, "tasks", None) or ():
        for item in getattr(task, "interrupts", None) or ():
            value = getattr(item, "value", None)
            if isinstance(value, Mapping):
                return dict(value)
    # 停在 `wait_confirm` 却取不到载荷：仍算「有待确认」，只是没有细节可带出去。
    return {}


async def resume_confirmation(
    graph: Any,
    *,
    thread_id: str,
    decision: str,
    channel: str,
    triggered_by: str | None = None,
    response: str | None = None,
) -> dict[str, Any]:
    """把一条回应打回 HITL 停点（照护台按钮与对话入口共用同一条路径）。

    载荷里的 `triggered_by` 只用于 AC-35 溯源：节点会把它写回状态，让
    `get_state()` 能回答「这次确认是从对话来的，还是照护台点的按钮」。
    """
    payload: dict[str, Any] = {"decision": decision, "channel": channel}
    if response is not None:
        payload["response"] = response
    if triggered_by is not None:
        payload["triggered_by"] = triggered_by
    return await start_or_resume(graph, thread_id=thread_id, resume=payload)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _seed_defaults(*, thread_id: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "attempt_no": 1,
        "retry_count": 0,
        "status": "calling",
        "next_action": "remind",
        "decision": None,
        "last_response": None,
        "failure_reason": None,
        "terminal_state": None,
        "confirmed_at": None,
        "confirm_channel": None,
        "late_taken": False,
        "receipt_flags": [],
        "rtc_status": [],
        "prompts": [],
        "delivered_at": None,
        "deadline": None,
        "next_retry_at": None,
        "order_no": None,
        "escalated": False,
        "finished": False,
        "warnings": [],
    }


def _warnings(state: MedicationConfirmationState) -> list[str]:
    """`list` 通道不累加（无 reducer），每个节点自己带上历史再返回。"""
    return list(state.get("warnings") or ())


def _thread_id(state: MedicationConfirmationState) -> str | None:
    value = state.get("thread_id")
    return str(value) if value else None


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _parse_dt(value: object) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _scheduled_at(
    state: MedicationConfirmationState, *, fallback: datetime.datetime
) -> datetime.datetime:
    return _parse_dt(state.get("scheduled_at")) or fallback


def _reminder_event(state: MedicationConfirmationState, attempt_no: int) -> ReminderEvent:
    return ReminderEvent(
        elder_id=str(state.get("elder_id") or ""),
        reminder_id=str(state.get("reminder_id") or ""),
        plan_id=str(state.get("plan_id") or ""),
        attempt_no=attempt_no,
        scheduled_at=str(state.get("scheduled_at") or ""),
        drug_snapshot=dict(state.get("drug_snapshot") or {}),
        thread_id=_thread_id(state),
    )


def _failure_reason(state: MedicationConfirmationState, decision: str) -> str | None:
    if decision == DECISION_CONFIRMED:
        return None
    if decision == DECISION_REFUSED:
        return "denied"
    return str(state.get("failure_reason") or "no_answer")


async def _record_attempt(
    runtime: MedicationRuntime,
    *,
    warnings: list[str],
    reminder_id: uuid.UUID | None,
    attempt_no: int,
    sent_at: datetime.datetime,
    receipt: str | None = None,
    tts_result: str | None = None,
    response: str | None = None,
    response_channel: str | None = None,
) -> None:
    """写 `medication_attempts`（幂等键 `(event_id, attempt_no)`）；失败只降级告警。"""
    if runtime.session_factory is None or reminder_id is None:
        return
    try:
        async with runtime.session_factory() as session:
            await medication_service.record_attempt(
                session,
                event_id=reminder_id,
                attempt_no=attempt_no,
                sent_at=sent_at,
                receipt=receipt,
                tts_result=tts_result,
                response=response,
                response_channel=response_channel,
            )
            await _commit(session)
    except Exception as exc:
        warnings.append(f"persist_failed:attempt:{type(exc).__name__}")
        logger.warning(
            "medication attempt persist failed event_id=%s attempt_no=%s error=%s",
            reminder_id,
            attempt_no,
            exc,
        )


async def _update_event(
    runtime: MedicationRuntime,
    *,
    warnings: list[str],
    reminder_id: uuid.UUID | None,
    **fields: Any,
) -> None:
    """写 `medication_events` 的增量字段；失败只降级告警（终态见 `escalate_node`）。"""
    if runtime.session_factory is None or reminder_id is None:
        return
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        return
    try:
        async with runtime.session_factory() as session:
            await medication_service.update_event_state(session, reminder_id=reminder_id, **payload)
            await _commit(session)
    except Exception as exc:
        warnings.append(f"persist_failed:event:{type(exc).__name__}")
        logger.warning(
            "medication event persist failed event_id=%s fields=%s error=%s",
            reminder_id,
            sorted(payload),
            exc,
        )


async def _refuse_follow_up(
    runtime: MedicationRuntime,
    *,
    warnings: list[str],
    reminder_id: uuid.UUID | None,
    channel: str | None,
) -> None:
    """拒服后补建「风险事件 + 护工跟进工单」（AC-07 / PRD line 321）。

    降级口径与 `_update_event` 一致：终态此时已经写进去了，这里失败**不掀翻图**，
    但必须留 ERROR 日志——派单漏了是靠日志和 `/metrics` 发现的，不能静默。
    幂等由 `medication_service.refuse_event` 保证（重放返回同一张单）。
    """
    if runtime.session_factory is None or reminder_id is None:
        warnings.append("persist_skipped:refusal_follow_up")
        logger.error(
            "medication refusal follow-up skipped has_factory=%s has_reminder=%s",
            runtime.session_factory is not None,
            reminder_id is not None,
        )
        return
    try:
        async with runtime.session_factory() as session:
            event = await medication_service.load_event(session, reminder_id)
            order = await medication_service.refuse_event(session, event=event, channel=channel)
            await _commit(session)
    except Exception as exc:
        warnings.append(f"persist_failed:refusal_follow_up:{type(exc).__name__}")
        logger.error(
            "medication refusal follow-up failed reminder_id=%s channel=%s error=%s",
            reminder_id,
            channel,
            exc,
        )
        return
    logger.warning(
        "medication refusal follow-up created reminder_id=%s order_no=%s",
        reminder_id,
        order.order_no,
    )


async def _commit(session: AsyncSession) -> None:
    """图节点自己持有 session，故由节点提交（服务层只 flush）。"""
    await session.commit()
