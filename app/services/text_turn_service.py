"""文字轮次的单一实现：路由 → 安全告警落库 → 服务层回话。

为什么单独成服务（2026-11-11）：`POST /api/dialogue/text`（打字）与
`POST /api/audio/utterance`（按住说话，ASR 之后拿到的是同一份文本）**必须走同一条
对话链**——AGENTS §4-8「一个能力只有一种形态」。此前两处各写一套，按住说话那条
只回了「您说的是『…』，我记下了」，既不进 L0 安全规则，也读不到记忆。

分层（AGENTS §2）：本模块属 services 层，只编排既有服务，不碰 HTTP、不碰路由细节：

    text ──► wire_route（L0 规则层 → L1 Laya）
              ├─ 命中安全规则：alert_service 落 risk_events + 红单 + 通知
              └─► DialogueService.respond（L0 话术短路 / Agent / LLM）

按住说话这条路的「这一轮是谁在说」也收在这里（`identify_speaker`）：它只产出给前端
看的旁路结论，**不参与授权**（AGENTS §4-9 身份只认设备令牌）。

失败口径：**打了字/说了话必须有回音**。路由抛错 → 退化成「无 decision」直接进 LLM；
服务层抛错 → 降级话术 + `degraded=True`。绝不向外抛（调用方是面向长者的端点）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid as uuid_lib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.context import (
    DialogueIdentity,
    reset_thread_id,
    reset_turn_id,
    set_thread_id,
    set_turn_id,
)
from app.core.degrade_speech import rest_speech
from app.core.observability import turn_span
from app.services import identity_service, voiceprint_service
from app.services.alert_service import AlertOutcome, handle_safety_alert
from app.services.decision_log_service import log_decision
from app.services.dialogue_service import DialogueReply, DialogueService

if TYPE_CHECKING:  # pragma: no cover - 仅为类型检查与阅读
    from app.services.conversation_event_recorder import ConversationEventRecorder
    from app.services.memory_turn_hook import MemoryTurnHook

logger = logging.getLogger(__name__)

#: 路由函数形状：`wire_route(text, *, elder_id, thread_id, trace_id) -> RouteDecision`。
#: 只约束用到的三个关键字参数，不 import 路由层具体类型（services → router 不做模块级依赖）。
RouteText = Callable[..., Awaitable[Any]]

#: 没有身份级联上下文时的线程号：一台设备一条线程，满足
#: `validate_thread_id` 的 `^[A-Za-z0-9._:-]{1,128}$` 口径。
THREAD_TEMPLATE: Final[str] = "device:{device_id}"


@dataclass(frozen=True, slots=True)
class TextTurnResult:
    """一轮对话的结果（调用方只管要不要念出来 / 要不要 TTS）。"""

    reply: DialogueReply
    decision: Any | None = None
    layer: str | None = None
    safety_alert: bool = False
    #: 旁路信息：告警是否真的落库成功（落库失败也要把话答出去，故不抛）
    alert_recorded: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def thread_id_for_device(device_id: str) -> str:
    """设备维度的线程号（打字与降级语音共用一条短期历史）。"""
    return THREAD_TEMPLATE.format(device_id=device_id or "-")


#: 把 `THREAD_TEMPLATE` 那个**给人看**的字符串线程号，映射成**给库用**的 UUID 的命名空间。
#:
#: 为什么必须映射：`conversation_events.thread_id` 是 `uuid.UUID NOT NULL`
#: （`app/models/conversation_event.py`），而打字链路的线程号是 `device:<uuid>` 字符串。
#: 直接塞字符串，`conversation_event_service._as_uuid` 会 `ValueError`——每打一次字就报一次错。
#: 用 `uuid5`（确定性派生）而不是 `uuid4`：同一台设备的每次会话必须落在同一条事件流上，
#: 否则回放会把一位长者的对话拆成互不相干的碎片。写法对齐
#: `app/services/alert_service.py` 的幂等键派生先例。
THREAD_NS_PREFIX: Final[str] = "anyu:device-thread:"

#: 记忆写回任务的强引用池。
#:
#: `asyncio.create_task` 只持**弱引用**（官方文档原话："Save a reference to the result of
#: this function"），事件循环里没有别的东西指着它时任务会被 GC 回收，中途消失且不留痕迹。
#: 语音链路用实例属性 `DialogueRuntime._memory_tasks` 挡这一层；这里是模块级函数，没有
#: 实例可挂，故用模块级 set + done 回调自清（与 `dialogue_runtime` 同口径）。
_MEMORY_TASKS: set[asyncio.Task[Any]] = set()


def db_thread_id_for_device(device_id: str) -> uuid_lib.UUID:
    """同一台设备在**事件流表**里的线程 UUID（`thread_id_for_device` 的镜像）。

    一个给人看、一个给库看；两者一一对应且都是确定性的，改其中一个必须同改另一个。
    """
    return uuid_lib.uuid5(uuid_lib.NAMESPACE_URL, f"{THREAD_NS_PREFIX}{device_id or '-'}")


def thread_id_from_text(thread_id: str) -> uuid_lib.UUID | None:
    """把一串已有的线程号转成事件流能用的 UUID；转不了返回 None（不猜）。

    三种情况：本身就是 UUID（话音链路 `conversation_threads` 给的那种）→ 直接用；
    `device:<uuid>` 形态 → 走 `uuid5` 派生；其余（含空串）→ None，宁可不记也不写错主人。
    """
    raw = (thread_id or "").strip()
    if not raw:
        return None
    try:
        return uuid_lib.UUID(raw)
    except (TypeError, ValueError, AttributeError):
        pass
    if raw.startswith("device:"):
        return db_thread_id_for_device(raw[len("device:") :])
    return None


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """「这一轮是谁在说」的旁路结论——**只给前端提示，不参与任何授权判定**。"""

    #: voice = 声纹命中；device = 按设备绑定长者继续；generic = 设备未绑长者。
    source: str
    matched: bool
    #: 为什么没认出（`low_confidence` / `vp_unavailable` / `no_samples` / ...），认出时为空。
    reason: str | None = None
    confidence: float | None = None
    elder_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """响应体形状（JSON 可序列化；`reason` 只透传字符串，不泄露内部对象）。"""
        return {
            "source": self.source,
            "matched": self.matched,
            "reason": self.reason,
            "confidence": self.confidence,
            "elder_id": self.elder_id,
        }


async def identify_speaker(
    session: AsyncSession,
    *,
    device_id: str | None,
    audio_bytes: bytes | None,
    trace_id: str = "-",
    log_name: str = "utterance",
) -> TurnIdentity | None:
    """按住说话这一轮的说话人旁路判定。**永不抛，也绝不阻断对话**。

    为什么要有它：按住说话的整段音频在 ASR 之前已经在内存里，顺手做一次声纹比对，
    前端就能提示「这次没认出是您，要不要重试」——但**只能提示**。真正的身份仍是设备
    令牌背后的服务端会话（AGENTS §4-9），所以这里低置信 / 无样本 / 容器不可用都只是
    往响应里塞一个 reason，照常用设备绑定的长者继续对话。

    口径（与 `identity_service.verify_session_identity` 的三态一一对应）：
    1. 没拿到设备号或音频 → 不判（返回 None）；
    2. 设备未绑长者 → `source="generic", reason="elder_unbound"`，不打推理容器；
    3. 该长者**没有 active 声纹样本** → `source="device", reason="no_samples"`，同样
       不打容器。这一闸是必需的：`cosine_compare_against_elder` 是**先**调容器抽
       embedding、**后**查样本的（`voiceprint_service.py:368`），没有样本时白等一次
       容器超时，等于每按一次说话都慢一截；
    4. 有样本才真比对：命中 → `source="voice"`；低置信 / 容器 503 → 仍按设备绑定
       继续（`reason` 透传），**不弹确认、不重试、不阻断**。

    实现注意：`verify_session_identity` 命中时会顺手开一条 `conversation_threads`
    （`_open_thread` 里自己 commit）。这条 REST 链路不用它——线程号由
    `thread_id_for_device` 固定成设备维度——所以判定完立刻 `end_open_thread` 收口，
    否则每按一次说话就堆一条永不结束的 open thread。收口动作本身也会 commit，
    因此**本函数必须在一轮对话的写库动作之前调用**（`run_text_turn` 之前）。
    """
    if not device_id or not audio_bytes:
        return None
    device_uuid = as_uuid(device_id)
    if device_uuid is None:
        return None

    try:
        elder_id = await identity_service.resolve_device_elder(session, device_uuid)
    except Exception as exc:  # 旁路：认不出就按 device 继续
        logger.warning(
            f"{log_name}_identity_lookup_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        return None
    if elder_id is None:
        return TurnIdentity(source="generic", matched=False, reason="elder_unbound")

    try:
        status = await voiceprint_service.get_status(session, elder_id)
    except Exception as exc:
        logger.warning(
            f"{log_name}_identity_status_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        return TurnIdentity(
            source="device", matched=False, reason="status_unavailable", elder_id=str(elder_id)
        )
    if status.status == "none":
        # 没样本就别打容器（见 docstring 第 3 条）。
        return TurnIdentity(
            source="device", matched=False, reason="no_samples", elder_id=str(elder_id)
        )

    try:
        decision = await identity_service.verify_session_identity(
            session, device_id=device_uuid, audio_bytes=audio_bytes
        )
    except Exception as exc:
        logger.warning(
            f"{log_name}_identity_verify_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        return TurnIdentity(
            source="device", matched=False, reason="verify_failed", elder_id=str(elder_id)
        )

    await _close_ephemeral_thread(session, decision=decision, trace_id=trace_id, log_name=log_name)

    matched = decision.source == "voice"
    return TurnIdentity(
        source="voice" if matched else "device",
        matched=matched,
        reason=decision.reason,
        confidence=decision.confidence,
        elder_id=str(elder_id),
    )


async def _close_ephemeral_thread(
    session: AsyncSession,
    *,
    decision: Any,
    trace_id: str,
    log_name: str,
) -> None:
    """收口 `verify_session_identity` 顺手开的对话线程（REST 链路不用它）。**永不抛**。"""
    thread_id = getattr(decision, "thread_id", None)
    if thread_id is None:
        return
    try:
        await identity_service.end_open_thread(session, thread_id=thread_id)
    except Exception as exc:
        logger.warning(
            f"{log_name}_identity_thread_close_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
            exc_info=True,
        )


async def run_text_turn(
    session: AsyncSession,
    *,
    text: str,
    elder_text: str | None,
    thread_id: str,
    trace_id: str,
    service: DialogueService,
    route_text: RouteText,
    log_name: str = "text_turn",
    recorder: ConversationEventRecorder | None = None,
    memory_hook: MemoryTurnHook | None = None,
    response_id: str | None = None,
) -> TextTurnResult:
    """文本轮的**边界**：开 turn 根 span、落三 ID、记轮级耗时，再交给编排体（S4 + S6）。

    `turn_id` 服务端按轮生成（与语音侧 `webrtc` 的轮次计数同语义，但两侧不共享编号）：
    文本轮没有设备级轮次计数器，用 `uuid4().hex` 保证唯一即可。
    """
    turn_id = uuid_lib.uuid4().hex
    started = time.perf_counter()
    thread_token = set_thread_id(thread_id)
    turn_token = set_turn_id(turn_id)
    try:
        async with turn_span(turn_id, thread_id=thread_id) as span:
            # 观测关闭时 turn_span 返回 _NullSpanScope，其 __aenter__ 给 None；
            # 直接写 span.attributes 会在无观测环境里 AttributeError，这里按空实现跳过。
            if span is not None:
                span.attributes["turn.text_len"] = len(text)
            result = await _run_text_turn_body(
                session,
                text=text,
                elder_text=elder_text,
                thread_id=thread_id,
                trace_id=trace_id,
                service=service,
                route_text=route_text,
                log_name=log_name,
                recorder=recorder,
                memory_hook=memory_hook,
                response_id=response_id,
            )
            if span is not None:
                span.attributes["turn.status"] = "ok"
            return result
    finally:
        reset_turn_id(turn_token)
        reset_thread_id(thread_token)
        metrics.observe_ms("turn_total_ms", (time.perf_counter() - started) * 1000)


async def _run_text_turn_body(
    session: AsyncSession,
    *,
    text: str,
    elder_text: str | None,
    thread_id: str,
    trace_id: str,
    service: DialogueService,
    route_text: RouteText,
    log_name: str = "text_turn",
    recorder: ConversationEventRecorder | None = None,
    memory_hook: MemoryTurnHook | None = None,
    response_id: str | None = None,
) -> TextTurnResult:
    """跑完一轮：路由 → 安全落库 → 回话（+ 旁路：事件流、长期记忆）。**永不抛**。

    `recorder` / `memory_hook` 缺省 None：既有调用点（含 `app/api/audio.py` 的按住说话）
    签名不变、行为逐字不变。装上之后这一轮才有「发生过」的证据与跨会话记忆。

    为什么两件都只是**旁路**：证据与记忆都不许把对话打挂。任何写入失败只记 warning，
    回复照常返回——长者要的是回音，不是把一次数据库抖动变成一次说不出话。
    """
    decision = await _route_once(
        route_text,
        text=text,
        elder_text=elder_text,
        thread_id=thread_id,
        trace_id=trace_id,
    )

    safety_alert = decision is not None and getattr(decision, "alert", None) is not None
    alert_recorded = False
    if decision is not None:
        if safety_alert:
            # 规则层是纯函数、不取时钟；告警服务要求 occurred_at 必填（缺了会抛，
            # 拒绝静默放过一次安全命中），由编排层补戳。
            decision = _stamp_hit_clock(decision)
            # 只有真的拿到 outcome 才算「已通知」；路由命中 ≠ 落库成功。
            alert_recorded = (
                await _record_safety_alert(
                    session,
                    decision=decision,
                    elder_id=as_uuid(elder_text),
                    thread_id=thread_id,
                    trace_id=trace_id,
                    log_name=log_name,
                )
                is not None
            )
        # 决策日志是旁路（永不抛）：话音链路在 `DialogueRuntime._route` 里也落这一笔。
        await log_decision(decision)

    db_thread = thread_id_from_text(thread_id)
    db_elder = as_uuid(elder_text)
    # `user_said` 必须在回话**之前**落库：先有「长者说了什么」再有「我怎么答」，
    # 顺序反了就变成用结果反推输入。话音链路同口径（`dialogue_runtime` 在调 LLM 前写）。
    await _record_event(
        recorder,
        thread_id=db_thread,
        elder_id=db_elder,
        event_type="user_said",
        payload={"text": text, "chars": len(text)},
        trace_id=trace_id,
        log_name=log_name,
    )

    reply = await _respond(
        service,
        thread_id=thread_id,
        elder_text=elder_text,
        text=text,
        decision=decision,
        log_name=log_name,
    )

    await _record_event(
        recorder,
        thread_id=db_thread,
        elder_id=db_elder,
        event_type="assistant_said",
        payload={
            "text": reply.text,
            "degraded": reply.degraded,
            "route": getattr(decision, "layer", None) if decision is not None else None,
            # 弱引用：把「说了」与「合成/播报」串起来查，不建外键。
            "response_id": response_id or uuid_lib.uuid4().hex,
        },
        trace_id=trace_id,
        log_name=log_name,
    )

    _schedule_memory_writeback(
        memory_hook,
        elder_id=elder_text,
        thread_id=thread_id,
        user_text=text,
        assistant_text=reply.text,
        log_name=log_name,
    )

    return TextTurnResult(
        reply=reply,
        decision=decision,
        layer=getattr(decision, "layer", None) if decision is not None else None,
        safety_alert=safety_alert,
        alert_recorded=alert_recorded,
    )


async def _route_once(
    route_text: RouteText,
    *,
    text: str,
    elder_text: str | None,
    thread_id: str,
    trace_id: str,
) -> Any:
    """问一次路由。**永不抛**：路由挂了退化成「没有 decision」，直接进 LLM 支。

    L0 规则命中（急症/敏感/固定意图）必须在模型之前——这里失败也不许把安全判断
    跳过后的整轮对话卡死；宁可这一轮没有路由层增益，也不能没有回音。
    """
    try:
        return await route_text(
            text,
            elder_id=elder_text,
            thread_id=thread_id,
            trace_id=trace_id,
        )
    except Exception as exc:
        logger.warning(
            "dialogue_route_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        return None


def _stamp_hit_clock(decision: Any) -> Any:
    """补齐 `SafetyHit.occurred_at`（规则层不取时钟，编排层补）。

    `RouteDecision` / `SafetyHit` 都是 frozen dataclass，只能整体替换。
    """
    hit = getattr(decision, "alert", None)
    if hit is None or getattr(hit, "occurred_at", None) is not None:
        return decision
    return replace(decision, alert=replace(hit, occurred_at=datetime.now(UTC)))


async def _record_safety_alert(
    session: AsyncSession,
    *,
    decision: Any,
    elder_id: uuid_lib.UUID | None,
    thread_id: str,
    trace_id: str,
    log_name: str,
) -> AlertOutcome | None:
    """把安全命中落成 risk_events + 红单 + 通知（`alert_service` 单一实现，只 flush）。

    **永不抛**：先把那句呼救话答出去，比把一次告警落库更重要；失败只记错误日志，
    由值班从日志里追（含 trace_id，可回查）。设备未绑长者时没有 elder_id 可写，
    只记一条 warning——不落一条「无主」的安全事件，避免告警错配到别的长者名下。

    返回 `AlertOutcome` 表示**确实落库**（调用方据此回填 `safety_alert`，
    不能拿「路由说命中」冒充「已经通知到人」）；返回 None 表示跳过或失败。
    """
    if elder_id is None:
        logger.warning(
            f"{log_name}_safety_alert_skipped",
            extra={"trace_id": trace_id, "thread_id": thread_id, "reason": "unbound_device"},
        )
        return None
    try:
        return await handle_safety_alert(
            session,
            decision=decision,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )
    except Exception as exc:
        logger.warning(
            f"{log_name}_safety_alert_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        await session.rollback()
        return None


async def _respond(
    service: DialogueService,
    *,
    thread_id: str,
    elder_text: str | None,
    text: str,
    decision: Any,
    log_name: str,
) -> DialogueReply:
    """调服务拿回复；服务层任何异常都转成「降级话术 + degraded=True」。

    身份口径：设备绑定了长者 → `source="device"`（`DialogueIdentity` 会校验
    elder_id 必填）；未绑定 → `source="generic"`（generic 强制 elder_id=None），
    否则构造就抛 ValueError，一轮对话变成 500。
    """
    identity = (
        DialogueIdentity(source="device", elder_id=elder_text)
        if elder_text
        else DialogueIdentity(source="generic")
    )
    try:
        return await service.respond(
            thread_id=thread_id,
            identity=identity,
            user_text=text,
            decision=decision,
        )
    except Exception as exc:
        logger.warning(
            f"{log_name}_reply_failed",
            extra={"thread_id": thread_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        return DialogueReply(text=rest_speech("internal"), degraded=True)


async def _record_event(
    recorder: ConversationEventRecorder | None,
    *,
    thread_id: uuid_lib.UUID | None,
    elder_id: uuid_lib.UUID | None,
    event_type: str,
    payload: dict[str, Any],
    trace_id: str,
    log_name: str,
) -> None:
    """写一条会话事件。**永不抛**。

    三处前置检查，缺一条就不写：
    1. 没装 recorder（装配失败 / 测试没传）→ 静默跳过，不是错误；
    2. `thread_id` 转不成 UUID → 不写。事件表 `thread_id` 是 NOT NULL UUID，
       塞字符串会让整条链路每次打字都报一次 `ValueError`；
    3. 没有 `elder_id`（未绑长者的 generic 设备）→ 不写。事件表的 `elder_id`
       有指向 `elders` 的外键，写不存在的长者会炸；而「无主事件」也没有回放价值。

    为什么不并进调用方的 `session`：`conversation_event_service._append` 用
    `pg_advisory_xact_lock` + `begin_nested()` 分配 `seq`，那是**事务级**锁，
    离开事务即释放。攒着跟主事务一起提交，`UNIQUE(thread_id, seq)` 的唯一性就没了。
    recorder 自带「一次调用一个 session 一个 commit」，正是为此。
    """
    if recorder is None or thread_id is None or elder_id is None:
        return
    try:
        await recorder.record(
            thread_id=thread_id,
            elder_id=elder_id,
            event_type=event_type,
            payload=payload,
            trace_id=trace_id,
        )
    except Exception as exc:
        logger.warning(
            f"{log_name}_event_write_failed",
            extra={
                "trace_id": trace_id,
                "thread_id": str(thread_id),
                "event_type": event_type,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )


def _schedule_memory_writeback(
    memory_hook: MemoryTurnHook | None,
    *,
    elder_id: str | None,
    thread_id: str,
    user_text: str,
    assistant_text: str,
    log_name: str,
) -> None:
    """把长期记忆写回丢到后台。**同步函数、永不抛、不 await**。

    为什么不在本轮等它：提取要调一次 LLM（实测 2–4 秒），而端到端预算只有 1.5 秒
    （PRD §5.3）。等它等于为了「以后能记住」牺牲「现在能回话」。话音链路同口径，
    见 `app/dialogue_runtime.py` 的 `_schedule_memory`。

    为什么必须留强引用：`asyncio.create_task` 只持弱引用，不留就被 GC 静默回收
    （`_MEMORY_TASKS` 上方有解释）。hook 自己契约是「从不抛」，所以这里不用再兜异常。
    """
    if memory_hook is None or not elder_id:
        # 未绑长者的设备没有可归属的记忆主体；generic 身份连会话都不该开。
        return
    try:
        task = asyncio.create_task(
            memory_hook(
                elder_id=elder_id,
                thread_id=thread_id,
                user_text=user_text,
                assistant_text=assistant_text,
            ),
            name=f"memory-writeback:{thread_id}",
        )
    except RuntimeError as exc:
        # 没有运行中的事件循环（同步测试/脚本直调）。记忆是旁路，丢了不影响回话。
        logger.warning(
            f"{log_name}_memory_schedule_failed",
            extra={"thread_id": thread_id, "error_type": type(exc).__name__},
        )
        return
    _MEMORY_TASKS.add(task)
    task.add_done_callback(_MEMORY_TASKS.discard)


def as_uuid(value: str | None) -> uuid_lib.UUID | None:
    """身份里的 UUID 文本 → UUID；缺失/非法按「没有身份」处理（边界不外抛）。"""
    if not value:
        return None
    try:
        return uuid_lib.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None


__all__ = [
    "THREAD_NS_PREFIX",
    "THREAD_TEMPLATE",
    "RouteText",
    "TextTurnResult",
    "TurnIdentity",
    "as_uuid",
    "db_thread_id_for_device",
    "identify_speaker",
    "run_text_turn",
    "thread_id_for_device",
    "thread_id_from_text",
]
