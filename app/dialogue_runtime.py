"""对话链路的进程级装配（迭代 4 收口 W4）。

## 为什么单独放一个模块，而不是全塞进 `app/main.py`

`app/main.py` 的职责是「FastAPI 装配」（AGENTS §2：只做装配、不写业务逻辑）。
对话编排的装配除了路由之外还要：建 Redis 连接、建 LLM 客户端、拿 TTS provider、
注册/注销终稿回调、维护每连接的 `thread_id` 与身份缓存。这些是**运行时状态**，
塞进 `main.py` 会让那个文件变成第二个业务层，也让「不跑 lifespan 的测试」
必须整段 import 才测得了。

## 一次回复的完整路径

```
asr_final
  └─ signaling.deliver_asr_event(turn_id=…)       # app/audio/signaling.py
       └─ _schedule_dialogue_invoke(…)             # 不 await，挂到事件循环
            └─ on_asr_final(device_id, turn_id, trace_id, text)   ← 本模块
                 ├─ 取身份与 thread_id（media_bridge 的级联结果）
                 ├─ DialogueService.respond(…)      # Redis 5 轮 + 组装 + LLM
                 ├─ signaling.deliver(tts_text)     # 字幕先到屏幕
                 └─ webrtc.enqueue_tts_for_device(…) # 音频随后
```

## 三条必须遵守的判断

**① `thread_id` 只从服务端取。**
`media_bridge._apply_terminal` 已经把 `decision.thread_id` 记在级联对象上
（`app/audio/media_bridge.py:446`）。这里只读它，不自己生成 —— PRD AC-39 要求
伪造/跨长者的 `thread_id` 被拒，生成入口必须唯一。

**② 级联还没结束（身份未定）时不编排。**
`active_cascade(device_id).resolved is False` 意味着这一轮的音还没有派上用场（
声纹正在比对、或正在等用户确认）。此时按 generic 走会把「上一位长者的记忆」
错误地卷进来（AC-28）。**什么都不做**比猜一个身份更安全。

**③ 音频送不出去不影响字幕。**
`enqueue_tts_for_device` 返回 `False` 时只记日志：字幕已经发出去了，
长者至少能看到字；为了一段没送出去的音频去补发降级话术，反而会出现
「同一句话先说一次没有声音的，再来说一次有声音的」。

## 关停顺序

`shutdown()` 先注销回调再 `aclose()`：注销之后到达的 `asr_final` 只会发字幕，
不会在 Redis/LLM 客户端已经关掉的半截状态上去用它们。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from app.audio import media_bridge, signaling
from app.audio.reply_stub import (
    PRIVACY_AUDIT_ACTION,
    PRIVACY_AUDIT_RESOURCE_TYPE,
    PRIVACY_NOTICE_TEXT,
)
from app.core import metrics
from app.core.context import (
    DialogueIdentity,
    PersonaContext,
    reset_thread_id,
    reset_turn_id,
    set_thread_id,
    set_turn_id,
)
from app.core.degrade_speech import spoken_speech
from app.core.observability import turn_span
from app.core.state_store import StateKind, StateStore, StateVersionConflict, make_key
from app.core.timeouts import STAGE_TTS, with_timeout
from app.router.decision import is_privacy_converged
from app.services import media_service, risk_event_service
from app.services.decision_log_service import log_decision

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.router.decision import RouteDecision
    from app.services.conversation_event_recorder import ConversationEventRecorder
    from app.services.dialogue_service import DialogueService
    from app.services.memory_turn_hook import MemoryTurnHook

    PersonaContextLoader = Callable[[str], Awaitable[PersonaContext | None]]
    #: `app/router/wiring.wire_route` 的形态（W1 注入；缺省 None 时本模块不路由）。
    RouteRouter = Callable[..., Awaitable[RouteDecision]]

logger: Final[logging.Logger] = logging.getLogger("app.dialogue_runtime")

#: 身份来源 → 编排层的身份口径。`media_bridge` 的 `source` 只有这四值。
_KNOWN_SOURCES: Final[frozenset[str]] = frozenset({"device", "voice", "confirm", "generic"})

#: 长期记忆写回的后台时限（N27b）。给得比一轮回复宽：它跑在回复之后，不占长者的等待
#: 时间（PRD §5.3 的 1.5s 预算里没有它）；但也不能无限挂着——提取要调一次 LLM
#: （演示机上实测 2–4s），写回是一次小事务，15s 足够跑完两段还留出抖动余量。
MEMORY_TURN_TIMEOUT_S: Final[float] = 15.0


class DialogueRuntime:
    """把「一轮对话」接到 WS 链路上；持有进程级的编排依赖。

    生产只在 `app/main.py` 的 lifespan 里构造一次；测试可直接构造并把
    `service` 换成假的（这样不需要 Redis / LLM / TTS 就能验证接线语义）。
    """

    def __init__(
        self,
        *,
        service: DialogueService,
        persona_loader: PersonaContextLoader | None = None,
        tts: Any = None,
        tts_factory: Callable[[str | None], Any] | None = None,
        router: RouteRouter | None = None,
        recorder: ConversationEventRecorder | None = None,
        memory_hook: MemoryTurnHook | None = None,
        state: StateStore | None = None,
    ) -> None:
        self._service = service
        self._persona_loader = persona_loader
        self._tts = tts
        # 按人设音色取 TTS。单实例 `tts` 是旧接口（测试与不支持音色的链路仍用）；
        # 两者都缺省时不做 TTS。缓存按音色分桶，避免每句都新建 provider。
        self._tts_factory = tts_factory
        self._tts_by_voice: dict[str | None, Any] = {}
        # 会话事件流（W1 · N40 接线）：缺省 None = 不记事件（行为与前一日完全一致）。
        # 生产由 `app/main.py` 注入；事件流是旁路，写不进去只记日志，不影响这一轮对话。
        self._recorder = recorder
        # 三级路由（W1 接线）：缺省 None = 不路由（迭代 5 行为，基线零改动）。
        # 生产由 `app/main.py` 注入 `app/router/wiring.wire_route`；本模块只决定
        # 「何时调、拿到 decision 后怎么用」，不在装配层发明路由策略。
        self._router = router
        # 长期记忆写回（N27b 接线）：缺省 None = 不记记忆（行为与接线前一致）。
        # 它跑在**回复之后**的后台任务里，理由是提取要调一次 LLM，不该让长者等；
        # 因此这里持有的不是结果而是任务句柄，`shutdown()` 负责收尾。
        self._memory_hook = memory_hook
        self._memory_tasks: set[asyncio.Task[None]] = set()
        # 统一状态中心（N43 接线）：缺省 None = 不落状态（行为与接线前一致）。
        # 只写 turn 段（`WRITE_PERMISSIONS` 里 TURN → dialogue_runtime），
        # cascade/media 两段归 media_bridge —— 写权限是硬编码的，越段写会直接抛。
        self._state = state
        self._registered = False
        # 媒体控制下行的序号（按设备自增）。前端用 `seq` 去重/丢弃迟到的旧指令：
        # 「换一段」连说两遍时，两条指令可能跨两轮到达，没有序号就只能靠到达顺序猜。
        # 进程内字典够用——下行本来就只在一个事件循环、一个进程里发生。
        self._media_seq: dict[str, int] = {}

    # ---------------------------------------------------------------- 生命周期
    def register(self) -> None:
        """注册终稿回调。重复调用无副作用（lifespan 重入不会叠加两份）。"""
        if self._registered:
            return
        signaling.set_dialogue_final_handler(self.on_asr_final)
        self._registered = True
        logger.info("dialogue runtime registered")

    async def shutdown(self) -> None:
        """注销回调。**先注销再关依赖**（见模块 docstring 关停顺序）。"""
        # 先把在跑的记忆写回收掉再注销：这些任务持有数据库会话，若在
        # `session_factory` 的引擎关掉之后再跑，会得到「引擎已释放」的报错，
        # 而那正是关停时最常见、也最容易被当成「记忆功能坏了」的一条噪声。
        await self._drain_memory_tasks()
        if not self._registered:
            return
        signaling.set_dialogue_final_handler(None)
        self._registered = False
        logger.info("dialogue runtime unregistered")

    # ---------------------------------------------------------------- 主链路
    async def on_asr_final(self, device_id: str, turn_id: str, trace_id: str, text: str) -> None:
        """一轮对话的**边界**：开 turn 根 span、把三 ID 落进上下文，再交给编排体。

        这一层只做三件事（S4 + S6）：`thread_id`/`turn_id` 的 ContextVar、`turn` 根 span、
        轮级耗时 `turn_total_ms`。编排细节全在 `_run_asr_final_turn`，它自己保证「从不抛」，
        所以这里不需要再套异常处理；`finally` 只负责复位 contextvar 与收尾打点。

        身份没定或转写为空时**不算一次执行**：不开 span、不记轮级耗时（计划 §7）。
        """
        started = time.perf_counter()
        resolved = self._resolve(device_id)
        if resolved is None or not text.strip():
            await self._run_asr_final_turn(device_id, turn_id, trace_id, text)
            return

        _identity, thread_id = resolved
        thread_token = set_thread_id(thread_id)
        turn_token = set_turn_id(turn_id)
        try:
            async with turn_span(turn_id, thread_id=thread_id) as span:
                if span is not None:
                    span.attributes["turn.text_len"] = len(text)
                await self._run_asr_final_turn(device_id, turn_id, trace_id, text)
                if span is not None:
                    span.attributes["turn.status"] = "ok"
        finally:
            reset_turn_id(turn_token)
            reset_thread_id(thread_token)
            metrics.observe_ms("turn_total_ms", (time.perf_counter() - started) * 1000)

    async def _run_asr_final_turn(
        self, device_id: str, turn_id: str, trace_id: str, text: str
    ) -> None:
        """一次终稿 → 一句话 → 一段音频。**从不抛**（调用方已在 `signaling` 兜一层）。"""
        if not text.strip():
            # 空转写（噪音被识别成空串 / 模型只返回标点）不成一轮对话。
            logger.info("dialogue skipped empty text", extra=_extra(trace_id, device_id, turn_id))
            return

        resolved = self._resolve(device_id)
        if resolved is None:
            # 身份还没定：这一轮先不编排（见模块 docstring ②）。
            logger.info("dialogue skipped unresolved", extra=_extra(trace_id, device_id, turn_id))
            return

        identity, thread_id = resolved
        # `generic` 身份（设备没认出来、声纹也没匹配）时 `elder_id` 是 None：
        # 这种轮次**可以回答**，但**不许落事件流与长期记忆** —— 两张表都要求
        # `elder_id` 非空，硬塞一个占位值等于把身份不明的内容挂到别人名下。
        elder_id = identity.elder_id
        # 会话事件流（N40）：**先记用户说了什么**，再去做任何可能失败的事。
        # 顺序不能反——路由或生成抛错时，事件流里必须留下「这一轮听到的是什么」，
        # 否则事后回放只能看到一串 assistant_said 缺口的悬空会话。
        if elder_id is not None:
            await self._record(
                thread_id=thread_id,
                elder_id=elder_id,
                event_type="user_said",
                payload={"text": text, "chars": len(text)},
                trace_id=trace_id,
            )
        persona = await self._load_persona(device_id)
        # 三级路由（W1）：在装配层问一句「这轮该谁答」，把结果既交给编排层
        # （`respond(decision=…)`），也落 `decision_logs` 留痕。路由挂了不连累回话。
        decision = await self._route(
            text,
            identity,
            thread_id=thread_id,
            trace_id=trace_id,
            device_id=device_id,
            turn_id=turn_id,
        )

        # 媒体控制（FR-CON-03）：规则层判定完之后**立刻**下发，不排在回复之后。
        # 理由是体感——长者说「换一段」，先要让正在响的那段停下来，再听数字人回话；
        # 若排在 TTS 之后，他会一边听着旧唱段一边等回复。回复文案与下行互不依赖。
        self._push_media_control(device_id, decision, trace_id=trace_id, turn_id=turn_id)

        # 这一轮回复的身份（N41）：`assistant_said` 与每一条 `response_segment_delivered`
        # 都带同一个 `response_id`，回放时才能回答「这句话送到耳朵里了没有、送到第几段」。
        # 用 uuid4 而不是复用 turn_id：一轮里重连、重试都可能产出多条回复。
        #
        # 提前到 `respond(...)` 之前分配：进度提示（FR-CON-02 ①）要在结果**之前**播出，
        # 提示段自己也要一个 id；提示段与正文各用一个，回放时不会把两段的
        # `segment_index` 串成同一条回复。
        response_id = uuid.uuid4().hex

        async def _on_progress(notice: str) -> None:
            """把「正在查询」这类进度提示先投递出去（字幕 + 一句 TTS）。

            大多数轮次根本不会调用它——`companion` 只在天气慢查询超过 3 秒时才用；
            这里不再做去重/限流：一轮播几次由 Agent 侧决定（一轮一次），装配层
            再加一层，会让「谁决定播报」出现两个答案。
            """
            if not signaling.deliver(
                device_id,
                {"type": "tts_text", "text": notice, "degraded": False},
            ):
                # 设备已掉线：不花钱合成一段没人听的音频（与正文同一口径）。
                logger.info(
                    "progress notice dropped",
                    extra=_extra(trace_id, device_id, turn_id),
                )
                return
            await self._deliver_audio(
                device_id,
                turn_id,
                trace_id,
                notice,
                False,
                thread_id=thread_id,
                elder_id=elder_id,
                response_id=uuid.uuid4().hex,
                voice_id=persona.voice_id if persona is not None else None,
            )

        reply = await self._service.respond(
            thread_id=thread_id,
            identity=identity,
            user_text=text,
            persona=persona,
            decision=decision,
            on_progress=_on_progress,
        )

        reply_text = reply.text
        if decision is not None and is_privacy_converged(decision.privacy):
            # 双人间隐私收敛：先一句短提醒、再正文（口径见 `reply_stub.PRIVACY_NOTICE_TEXT`）。
            # 只前置提醒、不改正文——正文语义是路由/生成产出的，装配层没有资格改写它。
            reply_text = f"{PRIVACY_NOTICE_TEXT}{reply_text}"
            await _audit_privacy_converged(
                identity,
                decision,
                trace_id=trace_id,
                device_id=device_id,
                turn_id=turn_id,
            )

        # 记「这一轮说了什么」：放在投递之前，因为投递失败（设备掉线）时
        # 这句话仍然**已经生成过**——回放要能看出「答了但没送到」，而不是当它没发生。
        # 「送到了没有」是另一类事件（N41 分段送达），不要混进这一行。
        if elder_id is not None:
            await self._record(
                thread_id=thread_id,
                elder_id=elder_id,
                event_type="assistant_said",
                payload={
                    "text": reply_text,
                    "degraded": reply.degraded,
                    "route": decision.layer if decision is not None else None,
                    "response_id": response_id,
                },
                trace_id=trace_id,
            )

        # 长期记忆写回（N27b）：挂成后台任务，**不 await**。
        # 放在这里而不是投递之后，是因为「长者说了一句话」这件事已经成立，
        # 哪怕设备随后掉线（下面 `delivered is False` 直接 return），
        # 这句「我对青霉素过敏」也应当进长期记忆 —— 记忆的来源是这次对话，不是这次送达。
        if elder_id is not None:
            self._schedule_memory(
                thread_id=thread_id,
                elder_id=elder_id,
                user_text=text,
                assistant_text=reply_text,
            )

        # 字幕先到屏幕：即使后面的音频失败，长者也不是「什么都没发生」。
        delivered = signaling.deliver(
            device_id,
            {"type": "tts_text", "text": reply_text, "degraded": reply.degraded},
        )

        # 统一状态中心（N43）：这一轮**已经发生**了 —— 状态描述的是发生过的一轮，
        # 不是「送达到了」的一轮。所以写在投递判定之前：设备掉线也好、字幕被丢也好，
        # 「上一轮问了什么、答了什么」都必须留得下，否则下一轮组装上下文会以为没聊过。
        await self._commit_turn_state(
            thread_id=thread_id,
            user_text=text,
            reply_text=reply_text,
            route=decision.layer if decision is not None else None,
            trace_id=trace_id,
        )

        if not delivered:
            # 设备已经掉线：不再花 TTS 的钱去合成一段没人听的音频。
            logger.info("dialogue text dropped", extra=_extra(trace_id, device_id, turn_id))
            return

        await self._deliver_audio(
            device_id,
            turn_id,
            trace_id,
            reply_text,
            reply.degraded,
            thread_id=thread_id,
            elder_id=elder_id,
            response_id=response_id,
            voice_id=persona.voice_id if persona is not None else None,
        )

    async def _commit_turn_state(
        self,
        *,
        thread_id: str,
        user_text: str,
        reply_text: str,
        route: str | None,
        trace_id: str,
    ) -> None:
        """把本轮摘要写进 `turn` 状态（CAS 版本号）。不抛、不重试超过一次。

        为什么用 CAS 而不是直接 HSET：两条 ASR 终稿在极端时序下会并发（重连后
        旧连接的最后一句与新连接的第一句），直接写会丢更新且**丢了也看不出来**；
        CAS 至少把「有人抢先写过」变成一个可重读的事实。

        为什么只重试一次：第二次再冲突说明这一轮之间又插进了新的一轮，
        再重试就是在覆盖更新的一轮 —— 那一轮比这一轮更接近「当前状态」。
        """
        if self._state is None:
            return
        key = make_key(StateKind.TURN, thread_id)
        value = {
            "user_text": user_text[:200],
            "reply_text": reply_text[:200],
            "route": route,
            "trace_id": trace_id,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        for attempt in range(2):
            try:
                current = await self._state.read(key)
                expected = current.version if current is not None else 0
                await self._state.write(key, expected, value)
                return
            except StateVersionConflict:
                if attempt == 0:
                    continue  # 重读一次再试
                logger.warning(
                    "turn_state_conflict",
                    extra={"trace_id": trace_id, "thread_id": thread_id},
                )
                return
            except Exception as exc:  # 旁路兜底：状态坏了不许连累这一轮
                logger.warning(
                    "turn_state_write_failed",
                    extra={"trace_id": trace_id, "error_type": type(exc).__name__},
                )
                return

    async def _record(
        self,
        *,
        thread_id: str,
        elder_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        trace_id: str,
    ) -> None:
        """记一条会话事件。缺省不记、失败不抛（事件流是旁路）。

        `ConversationEventRecorder` 自身已经把异常收敛成 `False`，这里再兜一层是给
        **替身/未来别的 recorder 实现**兜底：记录器契约是「永不抛」，但主链路不该
        因为一个旁路组件的违约就整轮不成立。
        """
        if self._recorder is None:
            return
        try:
            await self._recorder.record(
                thread_id=thread_id,
                elder_id=elder_id,
                event_type=event_type,
                payload=payload,
                trace_id=trace_id,
            )
        except Exception as exc:  # 旁路兜底：契约违约也不许连累主链路
            logger.warning(
                "conversation_event_record_failed",
                extra={
                    "event_type": event_type,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            )

    # ---------------------------------------------------------------- 媒体控制下行
    def _push_media_control(
        self,
        device_id: str,
        decision: RouteDecision | None,
        *,
        trace_id: str,
        turn_id: str,
    ) -> None:
        """把规则层的点播动作翻成一条 `media_control` 下行（FR-CON-03）。

        为什么在这里做、不在 `media_service` 里做：动作**已经由路由层判定完毕**
        （`decision.intent` 是 `play_next` / `stop` / `volume_up` / `volume_down` /
        `play_yueju` / `play_jingju`），这里只做「翻译 + 投递」。若放在服务层，
        服务层就得知道「什么时候该发 WS」，那是编排层的职责，会让服务层反向依赖音频层。

        为什么只发动作、不在这里挑素材：`next` 的选段与记账（打断 0.0、排除最近 N 条）
        是 `media_service.switch_next` 的既有契约，跑在 HTTP（`POST /api/media/switch-next`）
        里；前端收到 `next` 直接调那个端点，两端只有一条挑选路径。若这里也挑一次，
        就会出现「服务端挑了一段、前端又挑一段」的两套口径。

        投递失败不抛：设备掉线时这条指令没人收，不该把整轮对话打回失败。
        """
        if decision is None:
            return
        intent = getattr(decision, "intent", None)
        action = media_service.control_intent_from_rule(intent) if isinstance(intent, str) else None
        if action is None:
            return

        # `opera:yueju` / `opera:jingju` → 打开面板并指定剧种（挑段仍走 HTTP）。
        # 剧种写中文而不是 slug：`media_library.genre` 存的是「越剧」「京剧」，
        # 前端拿到就直接传给 `POST /api/media/pick`，中间不再做一层翻译。
        _OPERA_GENRES: Final[dict[str, str]] = {"opera:yueju": "越剧", "opera:jingju": "京剧"}
        if action.startswith("opera:"):
            payload: dict[str, Any] = {
                "action": "play",
                "kind": "opera",
                "genre": _OPERA_GENRES.get(action, ""),
            }
        else:
            payload = {"action": action}

        seq = self._media_seq.get(device_id, 0) + 1
        self._media_seq[device_id] = seq
        delivered = signaling.deliver(
            device_id,
            {"type": "media_control", "seq": seq, **payload},
        )
        logger.info(
            "media control pushed",
            extra={
                **_extra(trace_id, device_id, turn_id),
                "action": payload["action"],
                "seq": seq,
                "intent": intent,
                "route": getattr(decision, "layer", None),
                "delivered": delivered,
            },
        )

    # ---------------------------------------------------------------- 记忆写回
    def _schedule_memory(
        self,
        *,
        thread_id: str,
        elder_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """把一轮对话挂成后台记忆写回任务。缺省不挂、挂失败不抛。

        三个刻意的选择：

        1. **不 await**。提取要调一次 LLM（演示机 2–4s），而 PRD §5.3 的 1.5s
           端到端预算里没有「写记忆」这一项。让长者多等 3 秒去记一句「我喜欢越剧」，
           是把工程上的方便换成了体验上的代价。
        2. **任务句柄要留住**。`asyncio.create_task` 只持有弱引用，不自己存一份的话
           任务可能在中途被 GC 掉 —— 那是极难复现的「偶尔漏记一条」。
        3. **不收集异常**。`_run_memory` 自己已经把异常收敛成日志；这里再 `await`
           或 `.result()` 只会把「旁路失败」重新接回主链路。
        """
        if self._memory_hook is None:
            return
        task: asyncio.Task[None] = asyncio.create_task(
            self._run_memory(
                thread_id=thread_id,
                elder_id=elder_id,
                user_text=user_text,
                assistant_text=assistant_text,
            ),
            name=f"memory-writeback:{thread_id}",
        )
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    async def _run_memory(
        self,
        *,
        thread_id: str,
        elder_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """后台任务的实体：带时限地跑一次写回，**从不抛**。"""
        if self._memory_hook is None:
            return
        try:
            await asyncio.wait_for(
                self._memory_hook(
                    elder_id=elder_id,
                    thread_id=thread_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                ),
                timeout=MEMORY_TURN_TIMEOUT_S,
            )
        except TimeoutError:
            # 超时不是错误：提取模型慢了一拍，这一轮就当没记住。
            # 记 warning 而不是 info，因为「记忆功能整体变慢」值得被看见。
            logger.warning(
                "memory writeback timeout",
                extra={"thread_id": thread_id, "elder_id": elder_id},
            )
        except Exception as exc:  # 旁路兜底：钩子违约也不许连累主链路
            logger.warning(
                "memory hook crashed",
                extra={
                    "thread_id": thread_id,
                    "elder_id": elder_id,
                    "error_type": type(exc).__name__,
                },
            )

    async def _drain_memory_tasks(self) -> None:
        """关停时取消并等待在跑的记忆任务（**只在这里 await 它们**）。

        `return_exceptions=True` 是必须的：取消本身会让任务抛 `CancelledError`，
        不吞掉的话关停流程会被自己的收尾动作打断。
        """
        if not self._memory_tasks:
            return
        pending = list(self._memory_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._memory_tasks.clear()
        logger.info("memory tasks drained", extra={"count": len(pending)})

    # ---------------------------------------------------------------- 内部
    async def _route(
        self,
        text: str,
        identity: DialogueIdentity,
        *,
        thread_id: str,
        trace_id: str,
        device_id: str,
        turn_id: str,
    ) -> RouteDecision | None:
        """问一次三级路由并落 `decision_logs`。**永不抛**（路由挂了不能连累回话）。

        `elder_id` 直接取 `identity.elder_id`：那是 `_resolve` 从服务端身份级联拿到
        的值（str 形态，generic 轮为 None）。装配层**不发明也不改写**身份（AGENTS
        §4.9）——`wire_route` 的边界自己决定怎么用这个值；传 None 时
        `decision_log_service` 按「无身份」跳过落库。
        """
        if self._router is None:
            return None
        try:
            decision = await self._router(
                text,
                elder_id=identity.elder_id,
                thread_id=thread_id,
                trace_id=trace_id,
            )
        except Exception as exc:  # 路由失败只降级，不打断「听到 → 出声」
            logger.warning(
                "route failed",
                extra={
                    **_extra(trace_id, device_id, turn_id),
                    "error_type": type(exc).__name__,
                },
            )
            return None
        if decision is not None:
            # `log_decision` 自己永不抛（无身份跳过、写库失败只 warning），不重复兜。
            await log_decision(decision)
            # DLG-01：`decision.alert` 此前在语音主链上无人消费——长者在真实语音里说
            # 「胸口疼」，库里只有一行决策流水，照护台上没有红单。与 REST 链
            # （`text_turn_service._record_safety_alert`）同口径补落库，位置在
            # `log_decision` **之后**：决策流水是复盘基线，不能因为告警写失败而丢。
            # `record_safety_alert` 自身永不抛（失败只 warning），不打断当轮回复。
            await risk_event_service.record_safety_alert(
                decision,
                elder_id=_alert_elder_id(identity, device_id),
                thread_id=thread_id,
                trace_id=trace_id,
            )
            # `route_ms` 由 `app/router/routing.py` 实测（W1 归编排侧计时），这里只负责
            # 把它放进 metrics 环形缓冲：`record_decision` 只落层命中计数与 span，
            # 不写时延，不补这一行 /metrics 里就看不到路由耗时。
            route_ms = getattr(decision, "route_ms", None)
            if isinstance(route_ms, int | float) and route_ms >= 0:
                metrics.observe_ms("route_ms", float(route_ms))
        return decision

    def _resolve(self, device_id: str) -> tuple[DialogueIdentity, str] | None:
        """从身份级联取 `(identity, thread_id)`；未定或留空时返回 `None`。"""
        cascade = media_bridge.active_cascade(device_id)
        if cascade is None or not cascade.resolved:
            return None
        thread_id = (cascade.thread_id or "").strip()
        if not thread_id:
            return None
        source = cascade.source or ""
        if source not in _KNOWN_SOURCES:
            return None
        if source == "generic":
            return DialogueIdentity(source="generic"), thread_id
        elder_id = (cascade.candidate_elder_id or "").strip()
        if not elder_id:
            # 非 generic 却没 elder_id：数据不一致，按「身份未定」处理（不猜）。
            return None
        display_name = _display_name(cascade)
        return (
            DialogueIdentity(source=source, elder_id=elder_id, display_name=display_name),
            thread_id,
        )

    async def _load_persona(self, device_id: str) -> PersonaContext | None:
        """人设：`persona_loader` 没配或读失败时返回 `None`（组装层会退到默认名）。"""
        if self._persona_loader is None:
            return None
        cascade = media_bridge.active_cascade(device_id)
        elder_id = (cascade.candidate_elder_id if cascade is not None else "") or ""
        if not elder_id.strip():
            return None
        try:
            return await self._persona_loader(elder_id)
        except Exception as exc:  # 人设读失败只降级，不阻断回复
            logger.warning(
                "dialogue persona load failed",
                extra={"elder_id": elder_id, "error_type": type(exc).__name__},
            )
            return None

    def _tts_for_voice(self, voice_id: str | None) -> Any:
        """按人设音色取 provider；没有工厂时沿用单实例。"""
        if self._tts_factory is None:
            return self._tts
        key = (voice_id or "").strip() or None
        if key not in self._tts_by_voice:
            self._tts_by_voice[key] = self._tts_factory(key)
        return self._tts_by_voice[key]

    def _notify_tts_unavailable(
        self,
        device_id: str,
        *,
        trace_id: str,
        turn_id: str,
        reason: str,
    ) -> None:
        """语音一句都没送出去时，给长者端一条可见降级（字幕照常保留）。

        与首包超时同用 `error{code:"tts_unavailable"}`：前端已有 `case "error"`
        渲染 notice，不新增帧型；`message` 只取脱敏口语话术，不带 provider/堆栈。
        """
        signaling.deliver(
            device_id,
            {
                "type": "error",
                "code": "tts_unavailable",
                "message": spoken_speech("tts_unavailable"),
            },
        )
        logger.warning(
            "dialogue tts unavailable",
            extra={**_extra(trace_id, device_id, turn_id), "reason": reason},
        )

    async def _deliver_audio(
        self,
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        degraded: bool,
        *,
        thread_id: str,
        elder_id: str | None,
        response_id: str,
        voice_id: str | None = None,
    ) -> None:
        """TTS → 播放轨，**按句逐块写**（迭代 5 F1）。返回 `False` 只记日志（见 docstring ③）。

        ## 为什么是逐句而不是整段

        整段合成下「第一声什么时候响」= 整段合成耗时。实测一句
        「您好呀，我是小满，今天感觉怎么样？」（`zh-CN-XiaoxiaoNeural`）要 **3056 ms**，
        而 PRD line 521 / line 543-546 要求端到端 ≤1.5 s。回复越长越慢，是线性增长。

        按句切之后：第一句合成完立刻写轨，后面每句在**上一句还在播**的时候合成完毕。
        首包时间从此只取决于**第一句的长度**。

        ## 首包打点口径变了

        `tts_first_packet_ms` 现在是「第一句 PCM 到手」而不是「整段到手」。这正好是
        迭代 5 W1 要的那条曲线；旧口径见 `app/audio/tts.py::_record` 的注释（那里仍是
        整段，因为 `synthesize` 本身没变）。

        ## 打断

        每写一句都要过 `enqueue_tts_pcm_chunk` 的 turn 校验：用户一开口
        `barge_in()` 就把 `_playback_turn_id` 置 `None`，后续每句自动被拒，循环
        在下一句的 `continue` 里安静结束 —— 不需要另设取消标志。
        """
        provider = self._tts_for_voice(voice_id)
        if provider is None:
            self._notify_tts_unavailable(
                device_id,
                trace_id=trace_id,
                turn_id=turn_id,
                reason="provider_missing",
            )
            return
        from time import perf_counter

        from app.audio.tts_sentences import (
            TtsChunk,
            char_offsets_ms,
            split_sentences,
            synthesize_sentences_stream,
        )
        from app.audio.webrtc import (
            DOWNLINK_FRAME_MS,
            enqueue_tts_chunk_placed_for_device,
            flush_tts_for_device,
        )

        started = perf_counter()
        first_packet_ms: float | None = None
        chunks_written = 0
        dropped = 0
        # 分段送达（N41）：每写进播放轨一句就是「这一段真的送到了耳朵里」。
        # 只记**写进队列成功**的段 —— `dropped` 的段是没送到的，不能混进来充数。
        segments: list[dict[str, Any]] = []

        # 逐句字幕：句子与合成器用**同一份切句结果**，保证「第 k 段音频」与「第 k 句
        # 字幕」一一对应。
        #
        # `start_ms` 按**播放游标**算，不是按已入队帧数累加（2026-11-11 修正）：
        # `PlaybackAudioTrack.recv()` 队列空时会补静音帧且序号照增，而逐句合成是串行的
        # —— 下一句合成慢于上一句播放时，真实音频里就多出一段「入队帧数时间轴」上不存在
        # 的静音，字幕因此整体领先，且**领先量随句数累加**（用户报的「越说越不同步」）。
        # 播放游标把静音一并计入，两端零点仍取「首段真的出声」，偏差就此归零。
        sentences = split_sentences(text)
        caption: list[dict[str, Any]] = []
        caption_by_index: dict[int, dict[str, Any]] = {}
        pcm_by_sentence: dict[int, bytearray] = {}
        frames_by_sentence: dict[int, int] = {}
        first_content_frame: int | None = None

        def _push_caption() -> None:
            # 每多一句/每多一块就整份重发一次：句数很少（一般 1–5 句），换来的是
            # 「第一句字幕立刻能按句推进」而不是等整段合成完。失败不抛（旁路）。
            if not caption:
                return
            signaling.deliver(device_id, {"type": "tts_segments", "segments": list(caption)})

        def _note_chunk(index: int, *, frames: int, start_frame: int, pcm: bytes) -> None:
            """一块 PCM 写进播放轨后更新该句字幕：首块建条目，后续块累加时长/重算字速。"""
            nonlocal first_content_frame
            if index >= len(sentences):
                return
            if first_content_frame is None:
                first_content_frame = start_frame
            accumulated = pcm_by_sentence.setdefault(index, bytearray())
            accumulated.extend(pcm)
            if frames:
                frames_by_sentence[index] = frames_by_sentence.get(index, 0) + frames
                entry = caption_by_index.get(index)
                if entry is None:
                    entry = {
                        "text": sentences[index],
                        "start_ms": (start_frame - first_content_frame) * DOWNLINK_FRAME_MS,
                        "duration_ms": 0,
                    }
                    caption_by_index[index] = entry
                    caption.append(entry)
            else:
                entry = caption_by_index.get(index)
            if entry is None:
                return
            entry["duration_ms"] = frames_by_sentence.get(index, 0) * DOWNLINK_FRAME_MS
            # 句内节奏：按真实能量分配字符，停顿不占字（缺省时前端退回匀速比例）。
            offsets = char_offsets_ms(
                bytes(accumulated), sentences[index], duration_ms=entry["duration_ms"]
            )
            if offsets:
                entry["char_ms"] = offsets
            _push_caption()

        agen = synthesize_sentences_stream(text, provider=provider)
        try:
            try:
                # 只给**第一块**上超时（迭代 5 §4.2「TTS 首包 1s」）。后续块是在上一块
                # 还在播的时候流出来的，再套超时只会把已经正常的音频砍断。
                first = await with_timeout(agen.__anext__(), stage=STAGE_TTS)
            except StopAsyncIteration:  # 切句后没有可合成内容（纯标点 / 空白）
                first = None

            if first is not None and not first.ok:
                metrics.counter("reply_source_total", provider="llm", degraded="true")
                logger.warning(
                    "dialogue tts first packet timeout",
                    extra={
                        **_extra(trace_id, device_id, turn_id),
                        "timeout_ms": first.timeout_ms,
                    },
                )
                # 文字已经先发到屏幕（`on_asr_final` 里 `tts_text` 在前）：这里只补一条
                # 可见提示，**不往播放轨塞一句话**——合成器已经错过这一轮，再排队只会
                # 让长者听完一轮静默之后再听到第二句道歉。
                signaling.deliver(
                    device_id,
                    {
                        "type": "error",
                        "code": "tts_unavailable",
                        "message": first.spoken or spoken_speech("tts_unavailable"),
                    },
                )
                await flush_tts_for_device(device_id, turn_id)
                return
            if first is None:
                await flush_tts_for_device(device_id, turn_id)
                return
            first_chunk = first.value
            if first_chunk is None:
                await flush_tts_for_device(device_id, turn_id)
                return

            async def _chained(head: TtsChunk) -> AsyncIterator[TtsChunk]:
                yield head
                async for item in agen:
                    yield item

            async for chunk in _chained(first_chunk):
                if not chunk.pcm:
                    # 空 PCM 不入队：provider 返回空字节是「这句没合成出东西」，
                    # 往队列塞空块只会让 PlaybackAudioTrack 多播一帧静音。
                    if chunk.final:
                        dropped += 1
                    continue
                if first_packet_ms is None:
                    first_packet_ms = (perf_counter() - started) * 1000.0
                placed = await enqueue_tts_chunk_placed_for_device(device_id, turn_id, chunk.pcm)
                if placed.frames:
                    chunks_written += 1
                    _note_chunk(
                        chunk.index,
                        frames=placed.frames,
                        start_frame=placed.start_frame,
                        pcm=chunk.pcm,
                    )
                if chunk.final:
                    frames = frames_by_sentence.get(chunk.index, 0)
                    if frames:
                        segments.append(
                            {
                                "segment_index": len(segments),
                                "delivered_ms": round((perf_counter() - started) * 1000.0, 1),
                                "frames": frames,
                            }
                        )
        except Exception as exc:
            # TTS 走不通：字幕已经发出去了，不补第二句 tts_text（会变成两句字幕），
            # 但要在「一句都没出声」时给一条 error 帧，让长者端有可见降级。
            logger.warning(
                "dialogue tts failed",
                extra={
                    **_extra(trace_id, device_id, turn_id),
                    "error_type": type(exc).__name__,
                    "chunks_written": chunks_written,
                },
            )
            tail_frames = await flush_tts_for_device(device_id, turn_id)
            if not chunks_written and not tail_frames:
                # 一句都没写轨：subtitle 已在，但长者听不到任何声音，必须给可见降级；
                # 已经听到前半句的轮次不补提示，避免「半句话 + 一句道歉」的噪音。
                self._notify_tts_unavailable(
                    device_id,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    reason=type(exc).__name__,
                )
            # 已经送到的段仍是事实（长者确实听到了前两句），失败不能把它们抹掉。
            await self._record_segments(
                thread_id=thread_id,
                elder_id=elder_id,
                response_id=response_id,
                segments=segments,
                trace_id=trace_id,
            )
            return
        finally:
            await agen.aclose()

        # 收尾：把最后一句不足一帧的残样补齐，否则最后一个字会被吞掉。
        flushed = await flush_tts_for_device(device_id, turn_id)
        if flushed and caption:
            # 尾帧属于**最后一句**：不补进去，最后一句的时长会系统性少算 ≤20ms，
            # 末字永远差一口气打不完。补完再整份重发一次。
            tail_ms = flushed * DOWNLINK_FRAME_MS
            last = caption[-1]
            last["duration_ms"] = int(last["duration_ms"]) + tail_ms
            if last.get("char_ms"):
                last["char_ms"][-1] = last["duration_ms"]
            _push_caption()

        if first_packet_ms is not None:
            # 口径：第一句 PCM 到手（不是「整段到手」，见本方法 docstring）。
            metrics.observe_ms("tts_first_packet_ms", first_packet_ms)
        metrics.counter("reply_source_total", provider="llm", degraded=str(degraded).lower())

        if not chunks_written and not flushed:
            # 一句都没写进去：要么被打断，要么 PCM 全空。后者值得记一条。
            logger.info(
                "dialogue audio dropped",
                extra={
                    **_extra(trace_id, device_id, turn_id),
                    "reason": "no_frames",
                    "dropped_chunks": dropped,
                },
            )

        await self._record_segments(
            thread_id=thread_id,
            elder_id=elder_id,
            response_id=response_id,
            segments=segments,
            trace_id=trace_id,
        )

    async def _record_segments(
        self,
        *,
        thread_id: str,
        elder_id: str | None,
        response_id: str,
        segments: list[dict[str, Any]],
        trace_id: str,
    ) -> None:
        """把「哪些段真的送到了」写成事件（N41）。缺省不记、失败不抛。

        为什么批量而不是每句一条 `record()`：TTS 循环里每写一句就等一次数据库往返，
        会把「下一句合成完立刻写轨」的流水线打断 —— 那是首包 ≤1.5s 的实现基础。
        所以先攒在内存里，一轮结束一次 `record_many`（一个事务）。

        为什么段号是连续重排的 `len(segments)` 而不是 TTS 循环里的原始序号：
        `dropped` 的段没送到，若沿用原始序号，「第 3 段缺失」和「一共只有 2 段」
        在回放时无法区分。送达序列本身就是完整证据。
        """
        if not segments:
            return
        if self._recorder is None:
            return
        if elder_id is None:
            # `generic` 身份没有长者 id：分段送达表要求非空，硬塞占位值是伪造身份。
            return
        from app.services.conversation_event_service import NewEvent

        events = [
            NewEvent(
                event_type="response_segment_delivered",
                payload={"response_id": response_id, **seg},
                trace_id=trace_id,
            )
            for seg in segments
        ]
        try:
            await self._recorder.record_many(
                thread_id=thread_id,
                elder_id=elder_id,
                events=events,
                trace_id=trace_id,
            )
        except Exception as exc:  # 旁路兜底：契约违约也不许连累主链路
            logger.warning(
                "response_segment_record_failed",
                extra={
                    "trace_id": trace_id,
                    "segments": len(segments),
                    "error_type": type(exc).__name__,
                },
            )


def _audit_resource_id(elder_id: str | None, *, nil: uuid.UUID) -> uuid.UUID:
    """`DialogueIdentity.elder_id`（str）→ 审计台账的 UUID；解析不出退回 `NIL`。

    `audit_logs.actor_id` / `resource_id` 是 UUID 列，而身份级联给的是字符串
    （`elder-1` 这类演示值、甚至 generic 轮的 None）。解析失败**不发明身份**，
    退回 `NIL_RESOURCE_ID`（与 `reply_stub._audit_privacy_converged` 同口径）。
    """
    if elder_id:
        try:
            return uuid.UUID(elder_id)
        except (TypeError, ValueError, AttributeError):
            pass
    return nil


async def _audit_privacy_converged(
    identity: DialogueIdentity,
    decision: RouteDecision,
    *,
    trace_id: str,
    device_id: str,
    turn_id: str,
) -> None:
    """双人间隐私收敛落一条 `audit_logs`（**永不抛**，失败只 warning）。

    `detail` 只放复盘需要的三样：`trace_id`（串链路）、`rule_id`（哪条规则判出的
    双人间）、`intent`（本轮意图）。**不放话术原文**——正文可能含长者健康/用药信息。
    """
    from app.services.audit_service import NIL_RESOURCE_ID, write_audit_isolated

    resource_id = _audit_resource_id(identity.elder_id, nil=NIL_RESOURCE_ID)
    try:
        await write_audit_isolated(
            actor_type="system",
            actor_id=resource_id,
            action=PRIVACY_AUDIT_ACTION,
            resource_type=PRIVACY_AUDIT_RESOURCE_TYPE,
            resource_id=resource_id,
            detail={
                "trace_id": trace_id,
                "rule_id": decision.rule_id,
                "intent": decision.intent,
            },
            trace_id=trace_id,
        )
    except Exception as exc:  # 审计失败只留 warning，不再抛回主链
        logger.warning(
            "privacy audit failed",
            extra={**_extra(trace_id, device_id, turn_id), "error_type": type(exc).__name__},
        )


def _alert_elder_id(identity: DialogueIdentity, device_id: str) -> str | None:
    """告警归属：通用轮次借用设备当前绑定长者，仅用于落红单。

    generic 轮次的 `identity.elder_id` 恒为 None。若原样传给 `record_safety_alert`，
    长者说「胸口疼」时只会回一句安慰话，照护台收不到任何红单（PRD FR-VPR-03
    只禁止读记忆/写健康/确认用药，没有禁止告警）。这里只借
    `cascade.candidate_elder_id`（服务端从设备绑定解析出的值）作为告警对象，
    不把它写进对话事件、健康记录或记忆；身份仍由服务端注入，模型无从参与。
    """
    if identity.elder_id:
        return identity.elder_id
    cascade = media_bridge.active_cascade(device_id)
    if cascade is None:
        return None
    return (cascade.candidate_elder_id or "").strip() or None


def _display_name(cascade: Any) -> str | None:
    """从级联的候选集里取称呼（C5 的 `candidates` 恒 1 位）。"""
    candidates = getattr(cascade, "candidates", None) or ()
    for item in candidates:
        if isinstance(item, dict):
            name = item.get("display_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _extra(trace_id: str, device_id: str, turn_id: str) -> dict[str, object]:
    """结构化日志公共字段：每一行都带 trace_id（AGENTS §8）。"""
    return {"trace_id": trace_id, "device_id": device_id, "turn_id": turn_id}


__all__ = ["DialogueRuntime"]
