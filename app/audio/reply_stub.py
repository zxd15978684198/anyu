"""最小回复垫层（迭代 5 B1 · plan `设计记录（延迟护栏与口型）` §1 B2）。

链路（一句固定话术把「听到」接到「出声」，迭代 5 v1.3 §4.2b 起逐句下发）：

    asr_final ──► REPLY_TEXT ──► synthesize_by_sentence(逐句) ──► session.enqueue_tts_pcm()

**打点口径变更（迭代 5 v1.3 §4.2b）**：TTS 从整段合成改为「逐句合成、逐句入队」，
首包 = **第一句** PCM 完成时刻。轮级打点 `tts_first_packet_ms` / `tts_total_ms` 因此
从 `tts.py::_record`（句级调用点，每句一次，count 会=句数、P50/P95 语义被破坏）
搬到本回复管线：first packet 在第一句入队前、total 在逐句循环自然走完后（按轮打点）。
`_record` 只保留结构化日志；REST 直调链没有轮级打点，是已知取舍。

为什么需要它：本分支**没有回复编排**（`app/agents` / `app/router` / `app/mcp_client` /
`app/graphs` / `app/system_one` 全是空壳），WS 主链走到
`signaling.deliver_asr_event` 就终止，服务端不回灌任何 PCM —— 前端永远等不到「开口」，
PRD line 572 的「20 次里 ≥19 次在 1.5s 内开口」无从产生（plan §1 B2 / §3.2 ④）。

迭代 6 W4 接线（plan `设计记录（三级路由）` §4-W4）：构造器新增
`router` / `identity` / `trace_id` 三个可选参。不注入（缺省 None）时保持 迭代 5
占位行为——打点名、标签 `not_implemented`、既有断言全部不动；注入后在同一点位
走真实路由：`identity()` 解析设备身份 → `router(asr_text, ...)` 得 `RouteDecision`
→ `observe_ms("route_ms", 实测)` + `counter("route_placeholder_total",
source="wired_day06")` → `decision_log_service.log_decision` 落 `decision_logs`。
整块兜底 try/except：路由失败绝不影响「听到 → 出声」主链（下面第 3 条），
失败轮标签 `wired_day06_error`。

四条约束（动手前先读 plan，别自己加第五种语义）：

1. **不新增抽象层**：逐句消费 `app/audio/tts_sentences.synthesize_by_sentence(text,
   provider=...)`（F1a 现成助手，内部仍逐句调冻结契约 `TTSProvider.synthesize(text: str) -> bytes`），
   下行仍是既有的 `MediaSession.enqueue_tts_pcm(turn_id: str, pcm: bytes)`。
2. **每 turn 只回一次**：provider 重发 FINAL、前端重发 `asr_end` 都不能让同一 turn 的
   声音重来一遍（重复回灌会污染 `llm_first_token_ms` 基线）。
3. **失败不向上抛**：第一句就失败（尚未入队任何句）时，退回
   `app/core/degrade_speech.spoken_speech("tts_unavailable")` 的**既有**文字下行；
   已入队部分句后逐句合成抛错则截断续播、保留已出声句（局部好于无声）。
   调用方原本的 `deliver_asr_event` 行为不受任何影响。
4. **打点名字与 plan §3.2 一致**（迭代 6 接真 LLM 时只换实现、不改 key）：
   `route_ms` + `route_placeholder_total{source=not_implemented}`（③ 路由占位）、
   `llm_first_token_ms` + `reply_source_total{provider=mock}`（④ LLM 首 token 档位）。
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final

# `synthesize_by_sentence` 用「直接绑定」而非 `from app.audio import tts_sentences` 后
# 再前缀调用：测试只需 monkeypatch `reply_stub.synthesize_by_sentence` 这一个名字即可
# 拦到本模块全部调用点，不波及 tts_sentences 本体（app/dialogue_runtime.py:220 还依赖真实现）。
from uuid import UUID

from app.audio._observe import observe_latency_ms
from app.audio.tts import build_tts
from app.audio.tts_sentences import synthesize_by_sentence
from app.core.degrade_speech import spoken_speech
from app.core.injection import FIXED_REFUSAL, InjectionResult, check_intent, guard
from app.core.metrics import counter, observe_ms
from app.router.decision import is_privacy_converged
from app.services import risk_event_service
from app.services.decision_log_service import log_decision

if TYPE_CHECKING:
    # 仅类型用：webrtc 反过来 import 本模块，运行期不建立循环依赖。
    from app.audio.asr import TTSProvider
    from app.audio.webrtc import MediaSession
    from app.router.decision import RouteDecision

    #: W4 注入的路由薄壳（`app/router/wiring.wire_route` 的形态）。
    RouteRouter = Callable[..., Awaitable["RouteDecision"]]
    #: 设备身份解析（`wiring.resolve_identity` 绑定 device_id 后的形态）。
    IdentityResolver = Callable[[], Awaitable[tuple["UUID | None", "str | None"]]]

logger = logging.getLogger("app.audio.reply_stub")

#: 垫层期唯一的一句回复话术。迭代 6 接真 LLM 后由编排产出，本常量降为兜底话术。
REPLY_TEXT: Final[str] = "我听到了，您慢慢说。"

#: `reply_source_total` 的 provider 标签：垫层不是真 LLM，如实标 `mock`（plan §1 B2 / §6.2）。
REPLY_SOURCE: Final[str] = "mock"

#: 去重窗口：只记最近 64 个 turn。长连接若把每个 turn_id 都攒着，内存会随会话时长线性涨。
REPLIED_TURNS_MAX: Final[int] = 64

#: 双人间隐私收敛话术（迭代 4 W3 / S-D4-3，PRD line 571「设备绑定 1v1 声纹校验与隐私收敛」）。
#: 命中 `RouteDecision.privacy` 为真时**前置**在本轮话术最前面：先短句、后正文。
#: 为什么只加一句而不改正文：本轮语义是 Laya/L2 产出的，垫层没法安全地「改写」它
#: （改语义可能把用药点播说错），而一声提醒已经足以让长者意识到「这儿还有别人」。
PRIVACY_NOTICE_TEXT: Final[str] = "这儿可能还有别人，咱们轻轻说。"

#: 隐私收敛审计的 `action` / `resource_type`（`audit_logs` 台账口径，与
#: `injection_blocked` 同一个 `dialogue_turn` 资源族）。
PRIVACY_AUDIT_ACTION: Final[str] = "privacy_converged"
PRIVACY_AUDIT_RESOURCE_TYPE: Final[str] = "dialogue_turn"


#: privacy 真伪判定上收到 `app/router/decision.py::is_privacy_converged`（2026-11-09 W4）。
#: 本模块曾有一份与 `app/agents/companion.py` 逐字相同的镜像 `_privacy_is_true`，因为
#: `audio` 与 `agents` 的横向 import 被 AGENTS §2.2 禁止，只能靠注释约定「改一处必须
#: 同改另一处」；现在两处调用点 import 同一个函数，判定口径不可能再分叉。


class ReplyOutcome(StrEnum):
    """一次 `asr_final` 的处置结果；调用方只拿来打日志，不参与任何业务判断。"""

    ENQUEUED = "enqueued"  # 已合成并写入下行队列（真正的「开口」时刻）
    DEGRADED = "degraded"  # TTS 不可用/空音频 → 已下发文字降级话术
    DUPLICATE = "duplicate"  # 该 turn 已经回过一次，本次跳过
    SKIPPED = "skipped"  # 下行被拒（turn 已过期 / 写轨已关 / 背压），什么都不下发


class ReplyStub:
    """把一次 `asr_final` 换成一段固定话术的 PCM，并写入会话播放队列。

    生产上只在 `app/audio/webrtc.py::_asr_callback` 里实例化（每连接一个，
    turn 去重状态跟着连接走）；测试可直接注入 fake `tts` / `session` / `deliver`。
    """

    def __init__(
        self,
        *,
        tts: TTSProvider | None = None,
        deliver: Callable[[Mapping[str, Any]], bool] | None = None,
        router: RouteRouter | None = None,
        identity: IdentityResolver | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._tts = tts
        # 文字降级下行：生产传 `lambda msg: _deliver(device_id, msg)`，测试传记录器。
        self._deliver = deliver
        # 迭代 6 W4 路由三件套：生产由 webrtc._asr_callback 注入
        # （router=wire_route、identity=lambda: resolve_identity(device_id)）；
        # 缺省 None = 迭代 5 占位行为（基线断言零改动）。
        self._router = router
        self._identity = identity
        self._trace_id = trace_id
        self._replied: deque[str] = deque(maxlen=REPLIED_TURNS_MAX)
        self._replied_set: set[str] = set()

    @property
    def tts(self) -> TTSProvider:
        """惰性构建 TTS：连接建立时不一定真要说话，没人开口就不该去读 TTS 配置。"""
        if self._tts is None:
            self._tts = build_tts()
        return self._tts

    async def _route_and_log(self, turn_id: str, text: str) -> RouteDecision | None:
        """W4 实测路由分支：身份解析 → 路由 → 打点 + 落库。**永不抛**（docstring 第 3 条）。

        任何一步失败都只记 warning + `wired_day06_error` 标签——评测留痕不值得
        打断「听到 → 出声」主链。`text` 只取 `text_len` 进日志，内容不落（隐私）。

        **回传整条 `RouteDecision`（迭代 4 W3 起，原来是只回 `intent`）**，理由三条：
        ① 调用方现在要两样东西——`intent`（迭代 9 W3 意图层校验）与 `privacy`
           （W3 隐私收敛话术 + 审计），回 decision 后两者同源、不需要为 `privacy`
           再补第二个来源；
        ② 隐私审计的 `detail` 要 `rule_id`（「哪条规则判出双人间」是复盘唯一线索），
           `tuple[intent, bool]` 会把它丢掉，回 decision 才拿得到；
        ③ 本方法是私有方法、全仓只有 `reply()` 一个调用点（构造函数签名未动），
           不存在需要兼容的外部契约——多加一个字段反而比改返回类型更容易漂。
        任何失败返回 `None`，调用方据此跳过意图层校验与隐私收敛（不拿空串当命中）。
        """
        try:
            router = self._router
            if router is None:
                # 类型收窄用（调用点 `reply()` 已判过 `self._router is None` 走占位分支）：
                # 兜底保持本方法 docstring 的「永不抛、无路由即返回 None」口径。
                return None
            elder_id: UUID | None = None
            thread_id: str | None = None
            if self._identity is not None:
                elder_id, thread_id = await self._identity()
            decision = await router(
                text,
                elder_id=elder_id,
                thread_id=thread_id,
                trace_id=self._trace_id,
            )
            # route_ms 的实测值由 route() 内部 perf_counter 观测（含 L0/L1 分派），
            # 这里只搬运——打点口径与 迭代 5 基线表同 key 对齐（plan §3.2）。
            observe_ms("route_ms", float(decision.route_ms))
            counter("route_placeholder_total", source="wired_day06")
            await log_decision(decision)
            # DLG-01：垫层链同样是真实语音主链，`decision.alert` 不能只进决策流水。
            # 与 REST 链（`text_turn_service._record_safety_alert`）同口径补落库，位置在
            # `log_decision` **之后**（流水不因告警失败而丢）。`record_safety_alert`
            # 自身永不抛，因此不会把整块 try 打到 `route_wire_failed` 分支上去。
            await risk_event_service.record_safety_alert(
                decision,
                elder_id=elder_id,
                thread_id=thread_id,
                trace_id=self._trace_id,
            )
            return decision
        except Exception:
            logger.warning(
                "route_wire_failed",
                extra={"turn_id": turn_id, "text_len": len(text)},
                exc_info=True,
            )
            counter("route_placeholder_total", source="wired_day06_error")
            return None

    async def _guard_injection(self, turn_id: str, text: str) -> InjectionResult | None:
        """输入层注入拦截（迭代 9 W3 第 1 个调用点）。**永不抛**，命中则落审计。

        `guard()` 是纯函数（零 I/O、零模型调用）；审计走
        `write_audit_isolated`（自开 session + commit + 绝不抛），`detail` 里只留
        `pattern_id`/`layer`/`reason`/`trace_id`，**不存原文**（隐私 AGENTS §8）。
        """
        try:
            verdict = guard(text)
        except Exception:  # pragma: no cover - 纯函数不该抛，兜一层防拖垮主链
            logger.warning("injection_guard_failed", extra={"turn_id": turn_id})
            return None
        if not verdict.blocked:
            return None
        logger.warning(
            "injection_blocked",
            extra={
                "turn_id": turn_id,
                "layer": verdict.layer,
                "pattern_id": verdict.pattern_id,
            },
        )
        counter("injection_blocked_total", layer=verdict.layer)
        try:
            from app.services.audit_service import NIL_RESOURCE_ID, write_audit_isolated

            elder_id: UUID | None = None
            if self._identity is not None:
                try:
                    elder_id, _ = await self._identity()
                except Exception:
                    elder_id = None
            resource_id = elder_id if isinstance(elder_id, UUID) else NIL_RESOURCE_ID
            await write_audit_isolated(
                actor_type="system",
                actor_id=resource_id,
                action="injection_blocked",
                resource_type="dialogue_turn",
                resource_id=resource_id,
                detail={
                    "pattern_id": verdict.pattern_id,
                    "layer": verdict.layer,
                    "reason": verdict.reason,
                    "trace_id": self._trace_id,
                },
                trace_id=self._trace_id,
            )
        except Exception:  # pragma: no cover - 审计失败绝不打断拦截动作本身
            logger.warning("injection_audit_failed", extra={"turn_id": turn_id})
        return verdict

    async def _audit_privacy_converged(self, turn_id: str, decision: RouteDecision) -> None:
        """双人间隐私收敛留痕（迭代 4 W3 / S-D4-3）。**永不抛**，命中一轮写一行。

        `detail` 只留 `trace_id` / `rule_id` / `intent`——都是「怎么判出来的」，
        **不存转写原文、不存回复正文**（转写可能含长者隐私，AGENTS §8）。
        审计走 `write_audit_isolated`（自开 session + commit + 本身绝不抛）；这里再包
        一层 try 是因为调用点在合成之前——为一条旁路台账赔上整轮哑掉不值。

        `actor_id` / `resource_id` 取 `decision.elder_id`，**不再调一次 `self._identity()`**：
        决策里的身份与它同源（`wiring.resolve_identity` → `wire_route` 转成规范文本），
        而 `self._identity()` 是两次库查询（设备绑定 + 未结束会话）；双人间可能句句命中，
        不值得为同一份身份每句多跑一趟库。反解不成 UUID 就落全零占位——
        `audit_logs.actor_id` 是 UUID 列，编一个「看起来像真的」ID 比写全零更糟（AGENTS §4.9）。
        """
        try:
            from app.services.audit_service import NIL_RESOURCE_ID, write_audit_isolated

            resource_id = NIL_RESOURCE_ID
            raw_elder_id = getattr(decision, "elder_id", None)
            if isinstance(raw_elder_id, uuid.UUID):
                resource_id = raw_elder_id
            elif isinstance(raw_elder_id, str) and raw_elder_id:
                try:
                    resource_id = uuid.UUID(raw_elder_id)
                except (AttributeError, TypeError, ValueError):
                    resource_id = NIL_RESOURCE_ID
            written = await write_audit_isolated(
                actor_type="system",
                actor_id=resource_id,
                action=PRIVACY_AUDIT_ACTION,
                resource_type=PRIVACY_AUDIT_RESOURCE_TYPE,
                resource_id=resource_id,
                detail={
                    "trace_id": self._trace_id,
                    "rule_id": decision.rule_id,
                    "intent": decision.intent,
                },
                trace_id=self._trace_id,
            )
            logger.info(
                "privacy_converged",
                extra={
                    "turn_id": turn_id,
                    "rule_id": decision.rule_id,
                    "audit_written": written,
                },
            )
        except Exception:  # pragma: no cover - 旁路审计，绝不打断回复
            logger.warning("privacy_audit_failed", extra={"turn_id": turn_id})

    async def reply(
        self,
        *,
        turn_id: str,
        session: MediaSession,
        asr_text: str = "",
    ) -> ReplyOutcome:
        """对一次 `asr_final` 逐句合成、逐句下发固定话术，**从不抛异常**（见模块 docstring 第 3 条）。

        打点口径（迭代 5 v1.3 §4.2b，见模块 docstring）：`tts_first_packet_ms` /
        `llm_first_token_ms` 在第一句入队前各记一次（一轮恰一次）；`tts_total_ms`
        仅在逐句循环自然走完时记一次；被拒/截断的轮次不记总时长。

        `asr_text` 的用途（迭代 6 W4 起分叉）：未注入 `router` 时仍是 迭代 5 的
        「只触发、不参与合成」，仅用于日志里的 `text_len`；注入后它会进路由
        （`wire_route`）并落 `decision_logs`。**转写文本本身依旧不落日志**
        （可能含长者隐私，AGENTS §8）；空白转写不进路由（无可路由，保持占位标签）。
        """
        if not self._claim(turn_id):
            return ReplyOutcome.DUPLICATE

        # ③ 路由决策点位（plan §3.2 冻结 key，迭代 6 W4 换实现不改名）：
        #    - 迭代 9 W3 新增最前置的注入拦截：`guard()` 命中 → 不路由、不出占位标签
        #      以外的任何东西（`route_ms` 记 0ms、标签 injection_blocked），改播固定拒答话术；
        #    - 未注入 router（迭代 5 缺省 / 空白转写无可路由）→ 占位 0ms，
        #      标签 not_implemented，基线断言（test_metrics_keys_match_plan）零改动；
        #    - 注入 router → 实测：见 `_route_and_log`（身份 → 路由 → 打点 → 落库），
        #      随后再跑意图层校验（第 2 个调用点），命中同样改播固定拒答话术。
        spoken_text: str = REPLY_TEXT
        # W3 隐私收敛的两个前置条件：本轮没被拒（注入拦截优先级更高）+ 真拿到了决策。
        decision: RouteDecision | None = None
        refused = False
        if asr_text.strip():
            blocked = await self._guard_injection(turn_id, asr_text)
            if blocked is not None:
                spoken_text = FIXED_REFUSAL
                refused = True
                observe_ms("route_ms", 0.0)
                counter("route_placeholder_total", source="injection_blocked")
            elif self._router is None:
                observe_ms("route_ms", 0.0)
                counter("route_placeholder_total", source="not_implemented")
            else:
                decision = await self._route_and_log(turn_id, asr_text)
                intent = None if decision is None else decision.intent
                if intent is not None:
                    verdict = check_intent(asr_text, intent)
                    if verdict.blocked:
                        spoken_text = FIXED_REFUSAL
                        refused = True
                        counter("injection_blocked_total", layer=verdict.layer)
                        logger.warning(
                            "injection_blocked",
                            extra={
                                "turn_id": turn_id,
                                "layer": verdict.layer,
                                "pattern_id": verdict.pattern_id,
                            },
                        )
        else:
            observe_ms("route_ms", 0.0)
            counter("route_placeholder_total", source="not_implemented")

        # 迭代 4 W3 / S-D4-3：双人间隐私收敛。命中时**前置**一句提醒（先短句、后正文，
        # 不插在中间），并写一条 `privacy_converged` 审计。三条边界都在这里：
        #  - 注入拦截优先（`refused`）：被拒的轮次只出拒绝话术，不叠加提醒——
        #    对攻击者叮嘱「咱们轻轻说」没有意义，还会把拒答话术污染成两句；
        #  - privacy 的真假沿用 decision_logs 的收敛口径（见 `is_privacy_converged`，
        #    实现已上收到 `app/router/decision.py`），不另立第二套判定，免得
        #    「库里有 true、话术却没加」对不上账；
        #  - 审计是旁路（`_audit_privacy_converged` 永不抛），台账写失败不影响开口。
        if not refused and decision is not None and is_privacy_converged(decision.privacy):
            spoken_text = f"{PRIVACY_NOTICE_TEXT}{spoken_text}"
            await self._audit_privacy_converged(turn_id, decision)

        # 迭代 5 v1.3 §4.2b：逐句合成、逐句入队。轮级起表在进入逐句循环**之前**。
        # 计时用 `perf_counter` 而不是 `monotonic`：Windows 上 `monotonic` 走
        # `GetTickCount64`，粒度约 15.6ms —— 实测 6ms 的合成耗时会一半记成 0、
        # 一半记成 16，P50 直接失真（迭代 5 E1 实测发现）。`perf_counter` 是 QPC，亚微秒级。
        t0 = perf_counter()
        enqueued_sentences = 0
        enqueued_bytes = 0
        first_packet_observed = False
        enqueued_rejected = False  # 入队被拒 → 停止续句（旧代际不续播）
        try:
            async for pcm in synthesize_by_sentence(spoken_text, provider=self.tts):
                if not pcm:
                    # 与既有口径一致（旧实现「synthesize 返回空音频」即降级）：空 PCM 不入队
                    # —— A3 的写轨拿到空 buffer 会把轨写坏（既有用例钉住该护栏）。它既不是
                    # 「可听首包」也不计句：不打点、不入队、继续等下一句；全空 → 自然落进
                    # 「无句可下」的降级出口（音轨零写入）。
                    continue
                if not first_packet_observed:
                    first_packet_observed = True
                    elapsed = (perf_counter() - t0) * 1000.0
                    # 首包口径（§4.2b）：「首包」= 第一句完成时刻，**入队前**打点，
                    # 写轨耗时不算进首包。
                    observe_latency_ms("tts_first_packet_ms", elapsed)
                    # ④ LLM 首 token 档位（口径保持原样：一轮恰一次，迭代 6 换实现只改值
                    #    不改 key，plan §3.2 ④）：垫层下「首 token」就是「第一句变成可播
                    #    音频」的时刻，逐句形态下与首包是同一瞬间，每轮只记一次。
                    observe_ms("llm_first_token_ms", elapsed)
                    counter("reply_source_total", provider=REPLY_SOURCE)
                try:
                    await session.enqueue_tts_pcm(turn_id, pcm)
                except Exception as exc:
                    # 过期 turn / 写轨已关 / 背压：这段音频对长者已经没有意义（多半刚被打断）。
                    # 与现状同口径记 SKIPPED，并 break 停止续句 —— PLAN R9：barge_in 期间
                    # 旧句不得继续合成入队；也不补降级话术（打断后再播一句就是噪音）。
                    logger.info(
                        "reply stub pcm rejected",
                        extra={"turn_id": turn_id, "error_type": type(exc).__name__},
                    )
                    enqueued_rejected = True
                    break
                enqueued_sentences += 1
                enqueued_bytes += len(pcm)
        except Exception as exc:  # 逐句合成的任何失败都只在本层收口，绝不外抛
            # provider.synthesize 的异常经 synthesize_by_sentence 逐句透传到这里。二分：
            #  - 一句都未入队 → 维持整体降级口径（与旧实现「synthesize 失败」同路）；
            #  - 已入队部分句 → 局部好于无声：保留已入队句、停止续句（同 R9「旧代际不续播」），
            #    不补降级话术 —— 长者已经听到半句话，再来一条「说不利索」反而是噪音。
            if enqueued_sentences == 0:
                logger.warning(
                    "reply stub tts failed",
                    extra={"turn_id": turn_id, "error_type": type(exc).__name__},
                )
                return self._degrade(turn_id)
            logger.warning(
                "reply stub tts failed after partial enqueue",
                extra={
                    "turn_id": turn_id,
                    "error_type": type(exc).__name__,
                    "sentences_enqueued": enqueued_sentences,
                },
            )
            # 本轮确实出声了（哪怕被截断），outcome 如实记 enqueued；截断事实靠 warning 留痕。
            return ReplyOutcome.ENQUEUED

        if enqueued_rejected:
            return ReplyOutcome.SKIPPED

        if not first_packet_observed:
            # 一句可播都没有（空文本 / 全标点被切句器丢弃 / provider 句句返回空 PCM）：
            # 保持既有空路径行为 —— 降级、不观测任何轮级打点（没发生的耗时不能记 0）。
            # 旧形态的「synthesize 返回空 PCM」分支由上面 142-147 的逐句空 PCM 跳过吸收到这里。
            logger.warning(
                "reply stub produced no sentence",
                extra={"turn_id": turn_id, "text_len": len(asr_text)},
            )
            return self._degrade(turn_id)

        # 循环自然走完（所有句已入队）才打总时长：被拒/截断的轮次不打，
        # 否则「总时长」的 P95 会混入被 barge_in 掐掉的轮次。
        observe_latency_ms("tts_total_ms", (perf_counter() - t0) * 1000.0)
        logger.info(
            "reply stub enqueued",
            extra={
                "turn_id": turn_id,
                "sentences": enqueued_sentences,
                "pcm_bytes": enqueued_bytes,
                "text_len": len(asr_text),
            },
        )
        return ReplyOutcome.ENQUEUED

    def _degrade(self, turn_id: str) -> ReplyOutcome:
        """TTS 走不通 → 用既有 `tts_text` 下行把降级短句当大字幕给长者看。

        为什么是 `tts_text` 而不是 `error`：`error` 帧的定位是「本轮没播出来、只补一句降级
        话术」，而这一轮（上传降级轮）的**正文本身就是这句降级话术**，本帧已经承载了它；
        再补一个 `error` 等于同一句话报两遍。话音轮不同——那边正文另有 `tts_segments`
        承载，所以首包超时用 `error{code:"tts_unavailable"}`（C2 追加枚举，2026-11-11 补登记，
        见 `契约记录`）。改本帧型属行为变更，需另立裁决。
        """
        text = spoken_speech("tts_unavailable")
        if self._deliver is None:
            return ReplyOutcome.DEGRADED
        delivered = self._deliver({"type": "tts_text", "text": text})
        logger.info(
            "reply stub degraded",
            extra={"turn_id": turn_id, "delivered": delivered},
        )
        return ReplyOutcome.DEGRADED

    def _claim(self, turn_id: str) -> bool:
        """占用一个 turn 的回灌名额；同一 turn 第二次调用返回 False（防重复回灌）。"""
        if turn_id in self._replied_set:
            return False
        if len(self._replied) == REPLIED_TURNS_MAX:
            # deque 到上限时 append 会自动挤掉最旧一个，集合必须同步摘除，否则会「记得」
            # 一个早已滑出窗口的 turn，未来同 id 就再也回不了（极小概率但很难查）。
            self._replied_set.discard(self._replied[0])
        self._replied.append(turn_id)
        self._replied_set.add(turn_id)
        return True


__all__ = [
    "PRIVACY_AUDIT_ACTION",
    "PRIVACY_AUDIT_RESOURCE_TYPE",
    "PRIVACY_NOTICE_TEXT",
    "REPLIED_TURNS_MAX",
    "REPLY_SOURCE",
    "REPLY_TEXT",
    "ReplyOutcome",
    "ReplyStub",
]
