"""W2 的验收集：L2 真的走 Agent（`app/services/dialogue_service.py`）。

对应计划 `设计记录（编排接线）` §4-W2 与 §8 的 S-W1 / S-W2。
在 W2 之前，`respond` 只有一条直线（取 Redis 历史 → 组装 → `llm.chat` → 写回），
`app/agents/companion.py::run_turn` 全仓没有一个生产调用点；本文件钉住的就是
「路由判成 L2 之后，说话的是 Agent，不是 `llm.chat`」，以及「除 L2 以外一行行为都没变」。

## 覆盖的口径

| 分组 | 用例 | 依据 |
| --- | --- | --- |
| L2 走 Agent | agent 调 1 次、`llm.chat` **0** 次、text/`tool_calls` 来自 agent | §4-W2.2 |
| L2 走 Agent | 本轮 `identity` **原样**传给 agent（长期记忆召回的过滤键） | §4-W2.2 / AGENTS §4.9 |
| L2 走 Agent | `degraded` 透传（降级话术要能被下游看见） | §3 契约 |
| L2 走 Agent | Redis 短期记忆**读一次、写回一轮**，与 L0/L1 同一份（所有权在编排层） | §8 S-W2（2026-11-11 翻转） |
| 非 L2 | L0 / L1 / `decision=None` / 无 `layer` 的对象 → agent **0** 次、`llm.chat` 1 次 | §8 S-W1 |
| 非 L2 | L1 仍读历史、仍写回（逐字不动） | §4-W2.3 |
| 兜底 | agent 抛异常 → 回落 `llm.chat` + `agent_fallback_total{reason=exception}` | §3 / S-W3 |
| 兜底 | agent 返回空白 → 回落 + `agent_fallback_total{reason=empty}` | §4-W2.4 |
| 契约 | `AgentResult` 是**结构化**协议（任意同形对象都算），`tool_calls` 默认 0 | §3 / §3.3 |

## 两个刻意的取舍

1. **不真起 Redis、不真起 Agent**：本文件的全部断言都能用一个假 llm + 一个假 agent +
   一个假 store 表达，真起外部服务只会让用例在没装 Redis 的机器上变红。
2. **agent 返回的 `text` 原样透传**（`strip()` 只用来判空，不拿来改写）：把要不要
   去首尾空格的决定留给 Agent —— 编排层再 trim 一次，等于同一个规则两处实现。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core import metrics
from app.core.context import DialogueIdentity, DialogueTurn, PersonaContext
from app.router.decision import LAYER_L0, LAYER_L1, LAYER_L2, RouteDecision
from app.services.dialogue_service import (
    AgentResult,
    AgentRunner,
    DialogueReply,
    DialogueService,
    ProgressSink,
)
from app.services.llm_service import LLMReply

# --------------------------------------------------------------- 测试替身

FIXED_NOW = datetime(2026, 11, 7, 4, 30, tzinfo=UTC)  # = 12:30 Asia/Shanghai
THREAD_ID = "t-l2-1"

#: 假 llm 的话术，与假 agent 的话术**刻意不同**：断言「谁说的话」时只看文本就够。
LLM_TEXT = "今天晴，适合晒晒太阳。"
AGENT_TEXT = "今天多云转晴，最高二十度，下午风小，适合下楼走走。"


@dataclass(frozen=True, slots=True)
class FakeAgentResult:
    """长得像 `AgentResult` 的最小对象 —— W3 的 runner 闭包会返回同类物。"""

    text: str = AGENT_TEXT
    degraded: bool = False
    tool_calls: int = 2
    finish_reason: str = "speak"


class FakeLLM:
    """按脚本返回的假 LLM；记录每次收到的 messages 便于断言「没被调用」。"""

    def __init__(self, *, text: str = LLM_TEXT, degraded: bool = False) -> None:
        self.text = text
        self.degraded = degraded
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self, messages: Sequence[dict[str, str]], *, trace_id: str | None = None
    ) -> LLMReply:
        self.calls.append(list(messages))
        return LLMReply(text=self.text, degraded=self.degraded)


class FakeAgent:
    """假 Agent：记录收到的那句话、这一轮的身份、以及编排层搬来的上下文；可按脚本抛异常。

    `__call__` 是 async 且签名与 `AgentRunner` 一致（`text` + 四个只读关键字），
    因此它天然满足那个 Protocol —— 不需要继承，也不需要适配层。

    2026-11-11 起 `AgentRunner` 多了 `history` / `privacy` / `persona` 三个关键字：
    这里**必须**跟着扩签名，否则真实现传进来的关键字会 TypeError，而那个 TypeError 会被
    `respond` 的 `except Exception` 吞成「agent 挂了」—— 表现是「agent 一次都没被调到」，
    看起来像接线断了，实际是替身没跟上契约（本文件第一次跑就是 7 红）。
    """

    def __init__(
        self,
        *,
        result: FakeAgentResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.result = result if result is not None else FakeAgentResult()
        self.exc = exc
        self.calls: list[str] = []
        #: 每一轮收到的 `identity`（按调用顺序）。召回是否按长者过滤全靠它。
        self.identities: list[DialogueIdentity | None] = []
        #: 每一轮收到的短期记忆（已展开成 user/assistant 逐条消息）。
        self.histories: list[list[dict[str, str]] | None] = []
        #: 每一轮收到的 `privacy` 原值（编排层不解释、只搬运）。
        self.privacies: list[Any] = []
        #: 每一轮收到的人设对象。
        self.personas: list[PersonaContext | None] = []
        #: 每一轮收到的进度投递缝（FR-CON-02 ①）；没传时是 None。
        self.progress_sinks: list[ProgressSink | None] = []

    async def __call__(
        self,
        text: str,
        *,
        identity: DialogueIdentity | None = None,
        history: Sequence[Mapping[str, str]] | None = None,
        privacy: Any = None,
        persona: PersonaContext | None = None,
        on_progress: ProgressSink | None = None,
    ) -> FakeAgentResult:
        self.calls.append(text)
        self.identities.append(identity)
        self.histories.append(None if history is None else [dict(item) for item in history])
        self.privacies.append(privacy)
        self.personas.append(persona)
        self.progress_sinks.append(on_progress)
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeStore:
    """内存版 TurnStore；`loads` 与 `appended` 都留痕。

    L0/L1/L2 三条路径**共用这一份**短期记忆（所有权只在编排层），所以这里既能断言
    「L2 一读一写」，也能断言「L0 短路时一行都不碰」—— 同一个替身两种用法，不必分叉。
    """

    def __init__(self, turns: list[DialogueTurn] | None = None) -> None:
        self.turns = list(turns or [])
        self.loads = 0
        self.appended: list[tuple[str, str, str]] = []

    async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
        self.loads += 1
        return list(self.turns)

    async def append_turn(self, thread_id: str, *, user: str, assistant: str) -> None:
        self.appended.append((thread_id, user, assistant))

    @property
    def calls(self) -> int:
        """这一轮总共碰了几次存储（读 + 写）。L2 是 2（一读一写），L0 是 0。"""
        return self.loads + len(self.appended)


def _identity(**overrides: Any) -> DialogueIdentity:
    base: dict[str, Any] = {
        "source": "voice",
        "elder_id": "11111111-1111-4111-8111-111111111102",
        "display_name": "张桂芬",
    }
    base.update(overrides)
    return DialogueIdentity(**base)


def _decision(layer: str) -> RouteDecision:
    """真实的 `RouteDecision`（frozen + slots）—— 与 W1 传进来的那个是同一个类型。"""
    return RouteDecision(
        elder_id="11111111-1111-4111-8111-111111111102",
        thread_id=THREAD_ID,
        layer=layer,
        intent="chitchat",
        confidence=0.42,
    )


def _service(
    *,
    llm: FakeLLM | None = None,
    store: FakeStore | None = None,
    agent: AgentRunner | None = None,
) -> DialogueService:
    return DialogueService(
        llm=llm if llm is not None else FakeLLM(),
        store=store,
        now_provider=lambda: FIXED_NOW,
        agent=agent,
    )


def _counters() -> dict[str, int]:
    return cast(dict[str, int], metrics.metrics_snapshot()["counters"])


@pytest.fixture(autouse=True)
def _metrics_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把打点开关钉成 True，并在每个用例前后清空计数。

    `metrics.counter` 在 `settings.metrics_enabled=False` 时是 no-op（还**不建 key**），
    出厂的 `.env` 一变，本文件里所有「计数值为 1」的断言就会静默变成断言 0 —— 显式钉住
    比依赖环境更可靠。前置与后置各清一次，是为了不让上一个用例的计数漏进下一个。
    """
    monkeypatch.setattr(metrics, "get_settings", lambda: SimpleNamespace(metrics_enabled=True))
    metrics.reset_metrics()
    yield
    metrics.reset_metrics()


# ======================================================================
# 一、L2 → 交给 Agent
# ======================================================================


class TestL2GoesThroughAgent:
    """L2 的唯一判据成立时，说话的是 Agent。"""

    async def test_l2_uses_agent_and_never_calls_llm(self) -> None:
        llm = FakeLLM()
        store = FakeStore([DialogueTurn(user="上一问", assistant="上一答")])
        agent = FakeAgent(result=FakeAgentResult(text=AGENT_TEXT, tool_calls=2))
        service = _service(llm=llm, store=store, agent=agent)

        reply = await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        # ① agent 被调 1 次，且拿到的是长者这一句话本身（不是组装后的 messages）
        assert agent.calls == ["今天天气怎么样"]
        # ② llm.chat 一次都没被调用 —— 这正是 W2 要兑现的「L2 真的走 Agent」
        assert llm.calls == []
        # ③ 话术与工具计数都来自 agent
        assert reply.text == AGENT_TEXT
        assert reply.degraded is False
        assert reply.tool_calls == 2

    async def test_progress_sink_is_forwarded_only_when_given(self) -> None:
        """FR-CON-02 ①：编排层把进度缝原样交给 Agent；没给时替身收到的是 None。

        这两条一起钉住「不传就与接线前一致」——`respond` 用条件关键字传参，
        老替身（只认四个关键字）才不会被多出来的实参打死。
        """
        agent = FakeAgent()
        service = _service(agent=agent)
        notices: list[str] = []

        async def sink(notice: str) -> None:
            notices.append(notice)

        await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
            on_progress=sink,
        )
        assert agent.progress_sinks == [sink]
        assert notices == []  # 编排层只转发，不替 Agent 决定什么时候播

        await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )
        assert agent.progress_sinks[-1] is None

    async def test_l2_turn_reads_and_writes_short_term_memory(self) -> None:
        """S-W2（2026-11-11 翻转）：L2 与 L0/L1 **共用同一份** Redis 短期记忆。

        旧口径钉的是「`load` 与 `append` 都是 0」（理由是「Agent 自带 history，双写会串」），
        但 `run_turn(history=...)` 当时**没有任何生产调用点传过值** —— 于是那条口径的实际
        效果是「L2 没有历史」。现在改成编排层统一读写、Agent 只消费不持有：

        ① 读：这一轮的历史真的被读出来并交给了 Agent；
        ② 写：Agent 说的那句话作为这一轮的 assistant 原文写回（不是 llm 的话术）；
        ③ 所有权：`store` 只被碰这两次，没有第二个写入方。

        断言 `appended` 的 assistant 文本 `== AGENT_TEXT` 是刻意的：Agent 的话若被换成
        `LLM_TEXT`，下一轮读到的历史就是「用户没听过的那句话」，串话从记忆层开始。
        """
        store = FakeStore([DialogueTurn(user="上一问", assistant="上一答")])
        agent = FakeAgent()
        service = _service(store=store, agent=agent)

        reply = await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        # ① 读一次：历史展开成 user/assistant 逐条交给 Agent
        assert store.loads == 1
        assert agent.histories == [
            [
                {"role": "user", "content": "上一问"},
                {"role": "assistant", "content": "上一答"},
            ]
        ]
        # ② 写一轮：assistant 原文来自 AgentResult，不是 llm 的话术
        assert store.appended == [(THREAD_ID, "今天天气怎么样", AGENT_TEXT)]
        assert reply.text == AGENT_TEXT
        # ③ 总量正好一读一写：没有第二个写入方，也没有重复写
        assert store.calls == 2

    async def test_l2_degraded_flag_is_passed_through(self) -> None:
        """Agent 降级时下游必须看得出来（`tts_text.degraded` 靠这个字段切换话术）。"""
        agent = FakeAgent(
            result=FakeAgentResult(text="我稍微想一想，请等我一下下。", degraded=True)
        )
        service = _service(agent=agent)

        reply = await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        assert reply.degraded is True
        assert reply.text == "我稍微想一想，请等我一下下。"
        assert _counters()["dialogue_reply_total{degraded=true}"] == 1
        assert "dialogue_reply_total{degraded=false}" not in _counters()

    async def test_l2_records_the_same_reply_counter_as_chat_path(self) -> None:
        """成功回复打 `dialogue_reply_total{degraded=false}` —— 与 `llm.chat` 路径同名同标签。

        不换名字是刻意的：`/metrics` 的消费方与既有看板按这个名字算「回了多少句」，
        L2 换一条实现就换一个指标名，等于让监控凭空掉一块。
        """
        service = _service(agent=FakeAgent())

        await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        assert _counters()["dialogue_reply_total{degraded=false}"] == 1
        assert not any(key.startswith("agent_fallback_total") for key in _counters())

    async def test_duck_typed_decision_also_selects_agent(self) -> None:
        """`respond` 只 `getattr(decision, "layer", None)` —— 任何有 layer 的对象都算。

        这样编排层不必 import 路由层（见 `decision: Any` 的注释），W1 传真实
        `RouteDecision`、单测传轻量对象，走的是同一条判断。
        """
        agent = FakeAgent()
        service = _service(agent=agent)

        await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=SimpleNamespace(layer=LAYER_L2),
        )

        assert len(agent.calls) == 1

    async def test_identity_is_passed_through_to_agent(self) -> None:
        """本轮身份必须原样交到 Agent 手里（长期记忆召回的过滤键）。

        身份若在这里丢掉，runner 只能拿闭包启动时捕获的那个（空的）身份去召回：
        要么召不回来、要么按错的人召 —— 两者都是「把别人的记忆说给你听」这一类缺陷。
        所以断言到**同一个对象**（`is`），而不只是字段相等：中间任何一层重造身份，
        都会让「谁在读谁的记忆」失去唯一可信来源（AGENTS §4.9）。
        """
        agent = FakeAgent()
        service = _service(agent=agent)
        identity = _identity()

        await service.respond(
            thread_id=THREAD_ID,
            identity=identity,
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        assert agent.identities == [identity]
        assert agent.identities[0] is identity


# ======================================================================
# 二、非 L2 → 一行都不许变
# ======================================================================


class TestNonL2NeverUsesAgent:
    """L0 / L1 / 没有 decision：既有实现原样执行（S-W1）。"""

    @pytest.mark.parametrize(
        "decision",
        [
            pytest.param(_decision(LAYER_L0), id="L0"),
            pytest.param(_decision(LAYER_L1), id="L1"),
            pytest.param(None, id="no-decision"),
            pytest.param(SimpleNamespace(rule_id="safety_emergency"), id="object-without-layer"),
        ],
    )
    async def test_non_l2_skips_agent_and_calls_llm_once(self, decision: Any) -> None:
        llm = FakeLLM()
        agent = FakeAgent()
        service = _service(llm=llm, agent=agent)

        reply = await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=decision,
        )

        assert agent.calls == []
        assert len(llm.calls) == 1
        assert reply.text == LLM_TEXT
        # 非 L2 的回复恒为 0 次工具调用（它压根没经过工具层）
        assert reply.tool_calls == 0

    async def test_l1_still_reads_and_writes_short_term_memory(self) -> None:
        """L1 的记忆行为逐字不动：读一次历史、写回一轮。"""
        store = FakeStore([DialogueTurn(user="上一问", assistant="上一答")])
        llm = FakeLLM()
        service = _service(llm=llm, store=store, agent=FakeAgent())

        await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L1),
        )

        assert store.loads == 1
        assert store.appended == [(THREAD_ID, "今天天气怎么样", LLM_TEXT)]
        # 历史真的进了 prompt（不是「读了但没用」）
        blob = "\n".join(message["content"] for message in llm.calls[0])
        assert "上一问" in blob

    async def test_l2_without_agent_falls_back_to_chat(self) -> None:
        """没注入 agent（W3 起不来 / 单测 / 迭代 3 之前）时，L2 与今天完全一致。"""
        llm = FakeLLM()
        service = _service(llm=llm, agent=None)

        reply = await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        assert len(llm.calls) == 1
        assert reply.text == LLM_TEXT


# ======================================================================
# 三、Agent 挂了 → 落回既有路径
# ======================================================================


class TestAgentFailureFallsBack:
    """Agent 是增强能力：它出任何问题都只许降级，不许连累这一轮回话。"""

    async def test_agent_exception_falls_back_to_llm_and_counts(self) -> None:
        llm = FakeLLM()
        store = FakeStore()
        agent = FakeAgent(exc=RuntimeError("tool registry down"))
        service = _service(llm=llm, store=store, agent=agent)

        reply = await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        # ① 异常没有抛给调用方，回复照常
        assert reply.text == LLM_TEXT
        assert reply.degraded is False
        assert reply.tool_calls == 0
        # ② 只调了一次 llm.chat（回落路径就是原路径，不会再问第二遍）
        assert len(llm.calls) == 1
        # ③ 打点留痕，reason 区分「抛异常」与「空话术」
        assert _counters()["agent_fallback_total{reason=exception}"] == 1
        assert "agent_fallback_total{reason=empty}" not in _counters()

    async def test_agent_empty_text_falls_back_and_counts_empty(self) -> None:
        llm = FakeLLM()
        agent = FakeAgent(result=FakeAgentResult(text="   "))
        service = _service(llm=llm, agent=agent)

        reply = await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        # agent 确实被问过（不是「没注入所以没调」）
        assert len(agent.calls) == 1
        assert len(llm.calls) == 1
        assert reply.text == LLM_TEXT
        assert _counters()["agent_fallback_total{reason=empty}"] == 1
        assert "agent_fallback_total{reason=exception}" not in _counters()

    async def test_fallback_resumes_the_original_path_including_memory(self) -> None:
        """回落 ≠ 特殊路径：落回去之后 Redis 读完照写，与 `decision=None` 时一致。"""
        store = FakeStore([DialogueTurn(user="上一问", assistant="上一答")])
        llm = FakeLLM()
        service = _service(llm=llm, store=store, agent=FakeAgent(exc=TimeoutError("agent slow")))

        await service.respond(
            thread_id=THREAD_ID,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(LAYER_L2),
        )

        assert store.loads == 1
        assert store.appended == [(THREAD_ID, "今天天气怎么样", LLM_TEXT)]

    async def test_generic_identity_still_never_touches_store_on_fallback(self) -> None:
        """回落到原路径时 AC-28 仍然成立：generic 连读都不读。"""
        store = FakeStore([DialogueTurn(user="上一位老人的私事", assistant="我记下了")])
        llm = FakeLLM()
        service = _service(llm=llm, store=store, agent=FakeAgent(exc=RuntimeError("boom")))

        await service.respond(
            thread_id=THREAD_ID,
            identity=DialogueIdentity(source="generic"),
            user_text="你好",
            decision=_decision(LAYER_L2),
        )

        assert store.calls == 0
        blob = "\n".join(message["content"] for message in llm.calls[0])
        assert "上一位老人的私事" not in blob


# ======================================================================
# 四、契约本身（W3 依赖的形状）
# ======================================================================


def test_agent_result_protocol_is_structural() -> None:
    """`AgentResult` 是 `@runtime_checkable` 的**属性**协议：同形对象即通过。

    这条用例保护的是 W3 的自由度：它的 runner 闭包不必继承任何基类，
    只要返回的对象有这四个属性就能交进 `respond`。
    """
    assert isinstance(FakeAgentResult(), AgentResult)
    assert isinstance(
        SimpleNamespace(
            **{"text": "x", "degraded": False, "tool_calls": 0, "finish_reason": "speak"}
        ),
        AgentResult,
    )
    # 缺字段就不算（不是「只要能 print 就算」）
    assert not isinstance(SimpleNamespace(text="x"), AgentResult)


def test_dialogue_reply_tool_calls_defaults_to_zero() -> None:
    """既有调用点（`DialogueReply(text=..., degraded=...)`）一行都不用改。"""
    reply = DialogueReply(text="你好", degraded=False)
    assert reply.tool_calls == 0
    # 位置上仍然可以只用前两个字段构造（老代码有位置传参的可能）
    assert DialogueReply("你好", True).tool_calls == 0
