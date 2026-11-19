"""对话编排（迭代 4 收口 W3 · 把「听到的话」变成「该说的那句话」）。

## 这一层到底做什么

一次对话回复要串起四件事，本模块负责把它们按顺序串起来：

1. **取短期记忆** —— Redis 里按 `thread_id` 存的最近 5 轮（TD §6.3 ②）；
2. **组装 messages** —— 交给 `app.core.context.build_dialogue_messages`（纯函数，W2）；
3. **问模型** —— 交给 `app.services.llm_service.LLMService`（W1，从不抛，失败自带降级话术）；
4. **写回这一轮** —— 成功回复后把 `(user, assistant)` 追加回 Redis。

## 三条硬约束（都是有验收条款的，不是风格偏好）

**① generic 身份全程不碰 Redis。**
PRD AC-28：「仍不确定进通用模式**不写个人数据**」。所以 `identity.is_generic` 时
既不读历史也不写历史 —— 不是「读了不存」，是连读都不读（读出来的内容会进 prompt，
等于间接把上一位长者的对话带进了通用模式）。

**② `thread_id` 只由调用方传入，本模块不生成、也不向客户端透出。**
PRD AC-39：伪造/跨长者的 `thread_id` 必须被拒。生成的唯一入口是
`identity_service._new_thread_id()`（服务端按 `(elder_id, session_seq)` 开的会话），
编排层只是**消费**它。这里额外做一次格式校验，拦住「拼接进来的怪串」污染 Redis key。

**③ 存储故障不许升级成对话故障。**
Redis 挂掉时按「本轮无历史」继续，回复照常发出，只记 warn（计划 S-D9）。
理由和 TTS/VAD 一样：短期记忆是**增强**能力，让增强能力的故障反过来炸掉主链路，
是自己给自己制造 P0。

## L2 交给 Agent（计划 `adhoc-20261109-编排接线.md` §4-W2 / §8 S-W2）

L0/L1 的响应实现**保持原样**：规则层与模板已经能闭环，换实现只会换来行为回归。
只有「`decision.layer == "L2"` 且构造时注入了 `agent`」这一条判据成立时，本模块才把
长者这一句交给 `AgentRunner`（W3 用 `app.agents.companion.run_turn` 包出来的闭包），
由它做工具编排与记忆召回。Agent 抛异常或返回空白话术时**落回原路径**，并在
`agent_fallback_total{reason=...}` 上留痕 —— 与下面的第 ③ 条同理，Agent 是增强能力，
不许把主链路带崩。

L2 与 L0/L1 **共用同一份** Redis 短期记忆，读写全部留在本模块，Agent 只消费不持有：
`respond` 先把最近几轮读出来（`_load_history`），用 `_history_messages` 展开成
`[{"role": "user"}, {"role": "assistant"}, ...]` 交给 Agent，Agent 说完再
`_append_history` 写回。记忆的**所有权只有一处**，才不会出现「两个地方各有一份、
下一轮读到哪份取决于时序」的串话（S-W2）。

这条口径是 2026-11-11 纠正的：`run_turn(history=...)` 这个形参此前**没有任何生产
调用点给它传过值**，Agent 侧等于永远看不到上一轮；「Agent 自带 history」当时并不
成立，按它写的「L2 不碰 Redis」实际得到的是「L2 没有历史」。

## 与其它模块的边界

- 不碰数据库（`conversation_threads` 的读写归 `identity_service`）；
- 不发 WebSocket、不合成 TTS、不写播放轨（回程归 `app/audio/webrtc.py` 与 `app/audio/signaling.py`）；
- 不实现工具调用：工具循环在 `app/agents/companion.py`，本模块只负责「判层 + 注入 + 兜底」。

这样切的原因是：本模块的每一行都可以用「一个假 llm + 一个假 store」测完，
不需要起 PostgreSQL、不需要起 Redis、也不需要 WebSocket 客户端。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

from app.core import metrics
from app.core.context import (
    DIALOGUE_MAX_ROUNDS,
    DialogueIdentity,
    DialogueTurn,
    PersonaContext,
    build_dialogue_messages,
    get_trace_id,
)
from app.core.degrade_speech import spoken_speech
from app.core.timeouts import STAGE_LLM, with_timeout
from app.router.rule_replies import instant_reply, reply_for
from app.services.llm_service import LLMReply

logger = logging.getLogger(__name__)

#: 必须交给 Agent 作答的 L0 固定意图：天气是唯一真实 MCP Tool（AGENTS §4「一个能力
#: 一种形态」）。路由仍由 L0 短路判定（FR-DLG-02 ≤200ms 不受影响），但**作答**不能
#: 让 `llm.chat` 凭空编，否则「今天天气」这句永远取不到真数据。没有 Agent 时退回原路径。
#:
#: `health`（2026-11-12 补，FR-HLT-07 / AC-34）：规则层把「我头晕」直路由成 L0，
#: 但健康处置必须走健康决策图（`source=voice_active`），不允许模型自由作答。
#: 这里只决定「这一句交给 Agent」，Agent 内再由 `_is_health_complaint` 决定要不要
#: 先进图——两层判定同族词、职责分开：路由层判意图，Agent 层判要不要起图。
_AGENT_INTENTS: Final[frozenset[str]] = frozenset({"weather_query", "health"})

#: 复述上一轮的固定意图（FR-DLG-02）。不放进 `rule_replies.instant_reply`：
#: 那张表是纯值查找，而「重复一遍」必须读 Redis 里的上一轮真回复。
_REPEAT_INTENT: Final[str] = "repeat"

#: `thread_id` 的合法形态。真正的生成逻辑在 `identity_service._new_thread_id()`（32 位 hex），
#: 这里只做**准入校验**，放宽到常见 id 字符集，防止拼接进 Redis key 时出现空格/换行/通配符。
THREAD_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

#: Redis key 模板：`{prefix}:dialogue:{thread_id}:turns`（计划 S-D7）。
KEY_TEMPLATE: Final[str] = "{prefix}:dialogue:{thread_id}:turns"


@dataclass(frozen=True, slots=True)
class DialogueReply:
    """一轮回复的产物。

    `degraded=True` 表示这句话是**降级话术**（LLM 不可用/超时/结构异常），
    不是模型真的说了这句。前端与下游据此区分（契约 C6 的 `tts_text.degraded`）。

    `tool_calls` 是这一轮 Agent 实际调用的工具次数，只为可观测：既有调用点
    不传它（默认 0），L0/L1 与纯 chat 路径恒为 0。
    """

    text: str
    degraded: bool
    tool_calls: int = 0


class LLMChatter(Protocol):
    """本模块对 LLM 的唯一依赖面（够用就好，方便测试塞假的）。

    真实实现是 `app.services.llm_service.LLMService`；它**从不抛异常**，
    失败时返回 `LLMReply(text=<降级话术>, degraded=True)`。
    """

    async def chat(
        self, messages: Sequence[dict[str, str]], *, trace_id: str | None = None
    ) -> LLMReply: ...


class TurnStore(Protocol):
    """短期记忆存储的唯一依赖面。真实实现是本文件的 `RedisTurnStore`。"""

    async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]: ...

    async def append_turn(self, thread_id: str, *, user: str, assistant: str) -> None: ...


@runtime_checkable
class AgentResult(Protocol):
    """Agent 跑完一轮的产物（计划 §3 的交叉契约，W3 按这个形状返回）。

    本模块只消费其中三项：`text`（要说的话）、`degraded`（是不是降级话术）、
    `tool_calls`（这轮调了几次工具，用于可观测）。`finish_reason` 写进契约是给
    W3/W5 的日志与取证用的，本模块不做判定 —— 但它必须在，否则「这个对象是不是
    Agent 的产物」就失去了可读的信号。

    为什么用 Protocol + 只读属性，而不是 dataclass 或基类：W3 的 runner 是闭包，
    它可能返回 `CompanionTurnResult` 的映射结果，也可能返回一个本地轻量对象。
    属性协议让任何「长得像」的对象直接可用，既不需要继承，也不需要适配层；
    `@runtime_checkable` 只授给 `isinstance` 用的（带数据成员的协议不能用 `issubclass`）。
    """

    @property
    def text(self) -> str: ...

    @property
    def degraded(self) -> bool: ...

    @property
    def tool_calls(self) -> int: ...

    @property
    def finish_reason(self) -> str: ...


#: FR-CON-02 ① 的进度投递缝：入参是一句要说给长者听的话（如「我看看今天的天气，
#: 您稍等」）。真实投递（字幕 + 一句 TTS）归 `app/dialogue_runtime.py`；本模块只
#: 负责把编排层给的缝**原样转给 Agent**。这里自带同形别名而不 import
#: `app.agents.companion.ProgressSink`：services 不依赖 agents（AGENTS §2 分层单向），
#: 结构相同即可，形参按鸭子类型收。
ProgressSink = Callable[[str], Awaitable[None]]


class AgentRunner(Protocol):
    """Agent 跑一轮的入口：入参是**长者这一句话**，出参是上面那个形状。

    **身份随本轮传入**（`identity`），不再由 W3 的闭包自己捕获、也绝不从文本里猜：
    闭包是启动时造一次的进程级单例，它手里没有「这一轮是谁」；而长期记忆召回必须按
    `elder_id` 强制过滤（TD §6.3，AGENTS §4.9），唯一在正确时刻知道身份的就是编排层
    （`respond` 已经拿着 `identity` 读短期记忆）。所以身份走形参、由调用方显式给。

    本模块仍然**不认识** `runtime_context`：`thread_id` / `trace_id` 由服务端在别处注入，
    Agent 侧要审计/工具权限时才需要构造它（见 `app/agents/runtime_factory.py`）。
    依赖方向因此保持单向（services 不 import agents 的上下文类型）。

    `identity=None` 是给既有调用点与单测留的：没有身份时 runner 行为与接线前一致
    （不按长者召回，只有静态 `memory_recall` 或干脆不召回）。

    另外三个形参是 2026-11-11 补的，它们让「记忆/隐私/人设」这三件事在 L2 这一支
    与 L0/L1 走同一条来源：

    - `history`：编排层从 Redis 读出来的最近几轮，已展开成
      `[{"role": "user", "content": ...}, {"role": "assistant", "content": ...}]`。
      Agent **只消费不持有** —— 短期记忆的读写都在编排层，Agent 侧再存一份就是串话；
    - `privacy`：`RouteDecision.privacy` 的原值（bool 或 "true"/"false"），由 Agent 侧
      用 `is_privacy_converged` 判定。编排层不解释它，只搬运；
    - `persona`：本轮人设（`personas` 表那一份）。Agent 把它拼在协议段**之前**，
      不替换协议段；
    - `on_progress`：进度提示投递缝（2026-11-12 补，FR-CON-02 ①）。天气这类真实外呼
      超过 3 秒时，Agent 先通过它说一句「正在查询」再给结果；编排层只在**有实际投递
      能力**（`DialogueRuntime` 有信令与 TTS）时才传，纯文本入口保持不传。

    五个关键字都给了默认值：既有调用点（`eval/harness/real_baseline.py`）与
    所有单测替身不必改一行。
    """

    async def __call__(
        self,
        text: str,
        *,
        identity: DialogueIdentity | None = None,
        history: Sequence[Mapping[str, str]] | None = None,
        privacy: Any = None,
        persona: PersonaContext | None = None,
        on_progress: ProgressSink | None = None,
    ) -> AgentResult: ...


#: 计划 §3 写的是 `RunnerResult`；留个别名，W3 按计划文档 import 也能拿到同一个对象。
RunnerResult = AgentResult


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RedisTurnStore:
    """Redis 里的「最近 N 轮」存储。

    ## 数据结构

    每个 `thread_id` 一个 LIST（key 见 `KEY_TEMPLATE`），**每个元素是一轮**：

    ```
    ["{\\"user\\": \\"今天天气怎么样\\", \\"assistant\\": \\"今天晴，...\\"}", ...]
    ```

    为什么「一轮一个元素」而不是「一句一个元素」（计划 S-D15 登记的自裁）：
    `(user, assistant)` 是一次原子写入的完整逻辑单元，句级存储会在任何一次
    半途中断后留下孤儿 user，读取端配对时整段错位（把上一位的 assistant 配给
    这一位的 user）。按轮存则在同样的中断下只可能丢整轮，不会串话。

    ## 为什么用 `pipeline(transaction=True)`

    `RPUSH` + `LTRIM` 必须在同一个事务里：中间断掉会留下「永远不裁剪」的列表，
    随着对话进行无界增长。`transaction=True` 让 redis-py 用 `MULTI/EXEC` 包起来，
    一次往返既写了新轮次也裁掉了窗口外的旧轮次。
    """

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str,
        max_turns: int = DIALOGUE_MAX_ROUNDS,
    ) -> None:
        self._client = client
        self._prefix = key_prefix
        self._max_turns = max(1, int(max_turns))

    @property
    def max_turns(self) -> int:
        return self._max_turns

    def key_for(self, thread_id: str) -> str:
        """该 thread 的 Redis key；非法 `thread_id` 直接拒绝（不静默改写）。"""
        validate_thread_id(thread_id)
        return KEY_TEMPLATE.format(prefix=self._prefix, thread_id=thread_id)

    async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
        """读最近 `max_turns` 轮，按时间正序返回（最早的在前）。

        坏行（手工改过 Redis、旧版本格式、写到一半的 JSON）**跳过而不抛**：
        历史里少一轮的代价，远小于因为一条脏数据让整轮对话失败。
        """
        raw = await self._client.lrange(self.key_for(thread_id), -self._max_turns, -1)
        turns: list[DialogueTurn] = []
        for item in raw or []:
            turn = _decode_turn(item)
            if turn is not None:
                turns.append(turn)
        return turns

    async def append_turn(self, thread_id: str, *, user: str, assistant: str) -> None:
        """追加一轮并裁到窗口内（一次往返）。"""
        key = self.key_for(thread_id)
        payload = json.dumps({"user": user, "assistant": assistant}, ensure_ascii=False)
        pipe = self._client.pipeline(transaction=True)
        pipe.rpush(key, payload)
        pipe.ltrim(key, -self._max_turns, -1)
        await pipe.execute()


def validate_thread_id(thread_id: str) -> None:
    """准入校验：形态不对就不让它变成 Redis key 的一部分。"""
    if not isinstance(thread_id, str) or not THREAD_ID_PATTERN.match(thread_id):
        raise ValueError(f"invalid thread_id: {thread_id!r}")


def _decode_turn(item: Any) -> DialogueTurn | None:
    """把一条 Redis 元素解成 `DialogueTurn`；任何形态问题都返回 `None`。

    redis-py 在 `decode_responses=False` 时给的是 `bytes`（本项目就是这样配的，
    因为语音链路有二进制场景），所以两种形态都要认。
    """
    try:
        text = item.decode("utf-8") if isinstance(item, (bytes, bytearray)) else item
        if not isinstance(text, str):
            return None
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    assistant = data.get("assistant")
    if not isinstance(user, str) or not isinstance(assistant, str):
        return None
    if not user.strip() and not assistant.strip():
        return None
    return DialogueTurn(user=user, assistant=assistant)


class DialogueService:
    """编排一次对话回复。构造完即无状态（会话状态全在 `thread_id` 与 Redis 里）。"""

    def __init__(
        self,
        *,
        llm: LLMChatter,
        store: TurnStore | None = None,
        now_provider: Callable[[], datetime] = _utcnow,
        agent: AgentRunner | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._now = now_provider
        #: L2 的执行体。`None` = 没接上 Agent（工具层起不来 / 单测 / 迭代 3 前后），
        #: 此时 L2 与今天完全一致：走 `llm.chat`（静态降级，不是缺陷路径）。
        self._agent = agent

    async def respond(
        self,
        *,
        thread_id: str,
        identity: DialogueIdentity,
        user_text: str,
        persona: PersonaContext | None = None,
        decision: Any = None,
        on_progress: ProgressSink | None = None,
    ) -> DialogueReply:
        """一轮完整回复。**本方法不抛异常**（除了 `thread_id` 非法的编程错误）。

        顺序刻意是「先取历史 → 再组装 → 再问模型 → 最后写回」：
        写回放在最后，意味着模型失败时**不把这一轮存进记忆** ——
        否则长者会看到「上次问天气，数字人回了句『我稍微想一想』」被当成真答话记下来。

        `decision` 是 `app.router.decision.RouteDecision`（W1 传进来），本模块**只读
        `.layer`**。所以这里标 `Any` 而不 import 那个类型：编排层不依赖路由层的模块，
        也就不存在「router 改字段 → 编排层跟着改」的耦合；`getattr(..., None)` 顺带兜住了
        「传了个没有 layer 的对象」这种调用方错误（按非 L2 处理，行为与今天一致）。

        `on_progress` 是 FR-CON-02 ① 的进度缝：只在**真的能投递**的入口
        （`DialogueRuntime` 有信令 + TTS）才传，本方法只把它转给 Agent；不传时行为与
        接线前逐字节一致（老替身、纯文本入口都不受影响）。
        """
        validate_thread_id(thread_id)

        # ---- L0 短路 → 固定话术（迭代 8 N01）--------------------------------
        #
        # 位置：`validate_thread_id` 之后、Agent 分支之前，也就是「已经确认这一句要答、
        # 还没决定由谁来答」的那一格。
        #
        # 为什么必须在这一格：路由层只产出**标签**（`RouteDecision` 没有任何文本字段），
        # 命中急症之后唯一的文本来源就是 `llm.chat` —— 于是「命中规则层零模型 / 敏感
        # 不进大模型」这两条 TD §8.3 迭代 6 的验收永远做不到。短路需要一个「出口」，
        # 出口就是 `rule_replies` 那张话术表。
        #
        # 为什么天气不在表里：表只收「这句话本身就是完整答复」的意图。`weather_query`
        # 必须经 MCP 拿真数据，短路等于编天气；`ask_time` 要真时钟；`play_*` / `volume_*`
        # 隐含动作而本方法不执行动作。逐条理由写在 `app/router/rule_replies.py` 的模块
        # docstring 里，改表之前先读那里。
        #
        # 依赖说明：本模块原先自述「只读 `.layer`、不 import router 类型」。这里破的只是
        # 后半句，前半句仍然成立 —— `reply_for` 是纯值表查找，不 import、不假设
        # `RouteDecision` 的形状，路由层改字段不会把编排层拖进来跟着改。
        canned = reply_for(decision)
        if canned is not None:
            metrics.counter(
                "l0_short_circuit_total",
                intent=str(getattr(decision, "intent", "unknown")),
            )
            return DialogueReply(text=canned, degraded=False, tool_calls=0)

        # ---- 第二道短路：不需要模型也能答的固定意图（2026-11-12 · T6 · FR-DLG-02）
        #
        # 为什么要有第二道：上面那张表的判据是「这一句话本身就是完整答复」，于是
        # 「几点了 / 大声点 / 换一段 / 放越剧」这些**高频固定意图**一直被漏到 L2，
        # 白等一次模型（实测 2.6–3.4s，PRD 预算 200ms）。它们当初不进 `reply_for`
        # 的三条理由（要真时钟 / 要投递动作 / 要复述上一轮）在 2026-11-12 逐条被满足：
        # `instant_reply` 读真时钟；动作早在 `DialogueRuntime._push_media_control`
        # 就按同一个 `decision.intent` 下发（那一步发生在 `respond()` 之前）；`repeat`
        # 由 `_repeat_reply` 读最近一轮真回复拼。`reply_for` / `SHORT_CIRCUIT_INTENTS`
        # 原样不动，表外意图守卫继续成立。
        #
        # 位置仍在取历史之前（硬约束，见上）：除 `repeat` 外这一支一行 Redis 都不碰。
        instant = await self._instant_reply(decision, thread_id=thread_id, identity=identity)
        if instant is not None:
            metrics.counter(
                "l0_instant_total",
                intent=str(getattr(decision, "intent", "unknown")),
            )
            return DialogueReply(text=instant, degraded=False, tool_calls=0)

        # ---- L2 → 交给 Agent（计划 §4-W2）----------------------------------
        #
        # 判据：路由把这一句判成 L2，**或者**它是必须调用工具的 L0 固定意图
        # （`weather_query`，见 `_AGENT_INTENTS`），且构造时真的注入了 agent。
        # 其余 L0/L1 一律不换实现 —— 它们已由规则层/模板闭环，换实现等于制造回归。
        #
        # 短期记忆在**这一支之前**读一次，两条路径共用同一份 `history`：
        # Agent 的 `history` 形参此前从未被任何生产调用点传过值（`run_turn(history=...)`
        # 只有测试在传），所以「Agent 自带 history」当时并不成立，L2 实际是**没有历史**的。
        # 现在改成由编排层统一读写 Redis、Agent 只消费不持有 —— 记忆所有权一处，
        # 才不会出现两边各存一份、下一轮读到哪份取决于时序的串话（S-W2）。
        #
        # 读历史放在 L0 短路之后是硬约束：`reply_for` 命中时一行 Redis 都不该碰。
        history = await self._load_history(thread_id, identity=identity)

        if (
            decision is not None
            and self._agent is not None
            and (
                getattr(decision, "layer", None) == "L2"
                or getattr(decision, "intent", None) in _AGENT_INTENTS
            )
        ):
            agent = self._agent  # 局部绑定：await 之后类型收窄仍然成立
            # 只在真的有进度缝时才把 `on_progress` 放进关键字：既有单测替身只认
            # 四个关键字，无条件多传一个会把它们直接打死（TypeError → 整轮降级）。
            call_kwargs: dict[str, Any] = {
                "identity": identity,
                "history": _history_messages(history),
                "privacy": getattr(decision, "privacy", None),
                "persona": persona,
            }
            if on_progress is not None:
                call_kwargs["on_progress"] = on_progress
            try:
                result: AgentResult | None = await agent(user_text, **call_kwargs)
            except Exception as exc:
                # 同模块 docstring 的第 ③ 条：Agent 是增强能力，它挂了只许降级，不许连累这一轮回话。
                metrics.counter("agent_fallback_total", reason="exception")
                logger.warning(
                    "dialogue_agent_failed",
                    extra={"thread_id": thread_id, "error_type": type(exc).__name__},
                )
                result = None
            if result is not None and result.text.strip():
                # 写回与 L0/L1 同一条缝：Agent 说过的话同样是「这一轮的真答话」，
                # 不写回则下一轮（无论由谁作答）都看不到它，上下文就断在这里。
                await self._append_history(
                    thread_id,
                    identity=identity,
                    user=user_text,
                    assistant_text=result.text,
                )
                metrics.counter("dialogue_reply_total", degraded=str(result.degraded).lower())
                return DialogueReply(
                    text=result.text,
                    degraded=result.degraded,
                    tool_calls=result.tool_calls,
                )
            if result is not None:  # 有结果但是空白：空话不能说给长者听，落回原路径
                metrics.counter("agent_fallback_total", reason="empty")
            # 落到下面的既有路径（`llm.chat`）。

        messages = build_dialogue_messages(
            identity=identity,
            user_text=user_text,
            history=history,
            persona=persona,
            now=self._now(),
        )

        # LLM 档超时（迭代 5 §4.2「首 token 3s」）：`with_timeout` 负责计数
        # `degrade_total{stage="llm",reason="timeout"}` 并给出已话术化的 `spoken`。
        llm_result = await with_timeout(
            self._llm.chat(messages, trace_id=get_trace_id()), stage=STAGE_LLM
        )
        if not llm_result.ok or llm_result.value is None:
            logger.warning(
                "dialogue llm timeout",
                extra={"thread_id": thread_id, "timeout_ms": llm_result.timeout_ms},
            )
            metrics.counter("dialogue_reply_total", degraded="true")
            return DialogueReply(
                text=llm_result.spoken or spoken_speech("llm_timeout"),
                degraded=True,
            )
        # `llm_service` 目前是 `stream: False`，没有真「首 token」可观测：这里记的是
        # 整响应耗时，语义上是首 token 的**上界**（`llm_chat_ms` 仍是重试链路总耗时）。
        metrics.observe_ms("llm_first_token_ms", llm_result.elapsed_ms)
        reply = llm_result.value

        if reply.degraded:
            # 降级话术不是「数字人说过的话」，不入记忆（见方法 docstring）。
            metrics.counter("dialogue_reply_total", degraded="true")
        else:
            await self._append_history(
                thread_id, identity=identity, user=user_text, assistant_text=reply.text
            )
            metrics.counter("dialogue_reply_total", degraded="false")

        return DialogueReply(text=reply.text, degraded=reply.degraded)

    # ------------------------------------------------------------------ 内部
    async def _instant_reply(
        self,
        decision: Any,
        *,
        thread_id: str,
        identity: DialogueIdentity,
    ) -> str | None:
        """第二道短路的取值：固定价值表 / 真时钟 / 复述上一轮；给不出就 None。

        与 `reply_for` 同一条纪律：只读标签、不 import 路由层类型，**永不抛**
        （短路是优化路径，不是新的失败点）。
        """
        intent = getattr(decision, "intent", None)
        if not isinstance(intent, str):
            return None
        if intent == _REPEAT_INTENT:
            return await self._repeat_reply(thread_id, identity=identity)
        return instant_reply(decision, now=self._now())

    async def _repeat_reply(self, thread_id: str, *, identity: DialogueIdentity) -> str:
        """复述**上一轮真实回复**（`repeat` 的短路取值）。

        为什么不放进 `rule_replies`：那张表是纯值查找，而这里必须读 Redis。
        读失败按「无历史」处理（S-D9：存储故障不许升级成对话故障）——宁可说一句
        「刚才我没说什么」，也不能凭空编一段没说过的话糊弄长者。
        """
        history = await self._load_history(thread_id, identity=identity)
        for turn in reversed(history):
            if turn.assistant.strip():
                return f"刚才我说：{turn.assistant}"
        return "刚才我没说什么，您再问一遍？"

    async def _load_history(
        self, thread_id: str, *, identity: DialogueIdentity
    ) -> list[DialogueTurn]:
        """取短期记忆。generic 身份**直接返回空**，连 Redis 都不碰（AC-28）。"""
        if identity.is_generic or self._store is None:
            return []
        try:
            return await self._store.load_recent_turns(thread_id)
        except Exception as exc:  # 存储故障不许升级成对话故障（S-D9）
            logger.warning(
                "dialogue_history_load_failed",
                extra={"thread_id": thread_id, "error_type": type(exc).__name__},
            )
            return []

    async def _append_history(
        self,
        thread_id: str,
        *,
        identity: DialogueIdentity,
        user: str,
        assistant_text: str,
    ) -> None:
        """写回这一轮。同样：generic 不写，失败只记日志。

        入参是**文本**而不是 `LLMReply`：这一支现在有两个写入方（`llm.chat` 与 Agent），
        两者唯一的共同点就是「说了什么」。收 `LLMReply` 会让 Agent 那条路为了凑类型
        凭空造一个 LLMReply，等于把「谁说的话」和「话是谁产出的」混成一件事。
        """
        if identity.is_generic or self._store is None:
            return
        try:
            await self._store.append_turn(thread_id, user=user, assistant=assistant_text)
        except Exception as exc:  # 同上
            logger.warning(
                "dialogue_history_append_failed",
                extra={"thread_id": thread_id, "error_type": type(exc).__name__},
            )


def _history_messages(turns: Sequence[DialogueTurn]) -> list[dict[str, str]]:
    """把 `DialogueTurn` 序列展开成 Agent 认的 `[{"role", "content"}]`（正序）。

    两个要点：

    1. **一轮拆成两条**。`DialogueTurn` 是 `(user, assistant)` 的配对单元（存储侧按轮
       原子写入，见 `RedisTurnStore` 的 docstring），而模型看到的必须是逐条消息；
       在这里拆而不是在存储侧拆，是为了让「一轮一个元素」这个原子性继续成立。
    2. **空半边跳过，不补位**。`build_dialogue_messages` 对同样的脏数据是「跳过空串、
       不配平」（见 `app/core/context.py`）；这里保持同一口径，否则同一个 Redis 里
       的坏行会在两条路径上被渲染成两种形状。
    """
    messages: list[dict[str, str]] = []
    for turn in turns:
        user = turn.user.strip()
        if user:
            messages.append({"role": "user", "content": user})
        assistant = turn.assistant.strip()
        if assistant:
            messages.append({"role": "assistant", "content": assistant})
    return messages


__all__ = [
    "KEY_TEMPLATE",
    "THREAD_ID_PATTERN",
    "AgentResult",
    "AgentRunner",
    "DialogueReply",
    "DialogueService",
    "RedisTurnStore",
    "RunnerResult",
    "TurnStore",
    "validate_thread_id",
]
