"""L2 上下文接线（批次 1 + 批次 2）的契约验收。

对应计划 `设计记录（长者端Agent闭环收口）` §4 批次 1/2。
本文件钉的是**编排层与 Agent 之间那三样东西**，不是 Agent 内部怎么想：

| 契约 | 断言什么 | 依据 |
| --- | --- | --- |
| history 透传 | 上一轮的 user 原句与 assistant 原话按顺序进到 Agent 手里 | TD §6.3 / 讲义 §13.3 第 1 条 |
| 跨 thread 不串 | A 会话的历史不出现在 B 会话的 history 里 | AGENTS §4.9 |
| generic 不读不写 | `source="generic"` 时 history 为空且一次都不碰 store | PRD AC-28 |
| privacy 原值透传 | 编排层不 bool 化、不解释，判定只在 `is_privacy_converged` | PRD AC-28 |
| persona 透传 | 同一个对象进到 Agent（不重造、不拷贝） | PRD FR-ADM-07 |
| 空白回复不写回 | Agent 返回空白 → 回落 `llm.chat` 且不污染短期记忆 | TD §3 |

## 与 `tests/test_dialogue_service_agent.py` 的分工

那边验「L2 走的是 Agent 而不是 llm.chat」（谁在说话）；这边验「Agent 拿到了什么上下文」
（说了什么依据）。刻意分成两个文件，是因为前者一旦红就是主链路断了，后者红只是上下文
降级——两者的处置优先级不同，混在一起看会误判严重程度。

## 为什么用假 store 而不是真 Redis

`RedisTurnStore` 的四个命令（`lrange` / `pipeline` / `rpush` / `ltrim`）已由
`tests/test_dialogue_service.py::TestRedisTurnStore` 用假 client 逐字覆盖。这里要验的是
「编排层按什么顺序、拿哪份数据去喂 Agent」，再起一遍 Redis 只是把同一段逻辑测两遍，
还会让用例在没装 Redis 的机器上变红。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.context import DialogueIdentity, DialogueTurn, PersonaContext
from app.router.decision import LAYER_L1, LAYER_L2, RouteDecision
from app.services.dialogue_service import AgentResult, DialogueService
from app.services.llm_service import LLMReply

# --------------------------------------------------------------- 固定样本

FIXED_NOW = datetime(2026, 11, 11, 6, 30, tzinfo=UTC)  # = 14:30 Asia/Shanghai

_THREAD_A = "t-l2-wiring-a"
_THREAD_B = "t-l2-wiring-b"

#: 两位长者：跨会话隔离要靠 `elder_id`，不是靠 `thread_id` 恰好不同。
_ELDER_A = "11111111-1111-4111-8111-111111111101"
_ELDER_B = "11111111-1111-4111-8111-111111111102"

LLM_TEXT = "今天晴，适合晒晒太阳。"
AGENT_TEXT = "您上回说膝盖疼，今天风小，下楼走十分钟就回来。"

PERSONA = PersonaContext(
    name="小满",
    personality="慢条斯理，爱用比喻，不说重话",
    tone_examples=("咱们慢慢来", "这个事儿不急"),
)


# --------------------------------------------------------------- 测试替身


@dataclass(frozen=True, slots=True)
class _AgentResult:
    text: str = AGENT_TEXT
    degraded: bool = False
    tool_calls: int = 0
    finish_reason: str = "speak"


class _RecorderAgent:
    """记录每一轮收到的上下文的假 Agent；签名与 `AgentRunner` 一致。"""

    def __init__(self, *, text: str = AGENT_TEXT) -> None:
        self._text = text
        self.histories: list[list[dict[str, str]] | None] = []
        self.privacies: list[Any] = []
        self.personas: list[PersonaContext | None] = []

    async def __call__(
        self,
        text: str,
        *,
        identity: DialogueIdentity | None = None,
        history: Sequence[Mapping[str, str]] | None = None,
        privacy: Any = None,
        persona: PersonaContext | None = None,
    ) -> AgentResult:
        self.histories.append(None if history is None else [dict(item) for item in history])
        self.privacies.append(privacy)
        self.personas.append(persona)
        return _AgentResult(text=self._text)

    @property
    def last_history(self) -> list[dict[str, str]]:
        payload = self.histories[-1]
        assert payload is not None, "Agent 收到的 history 是 None —— 接线没生效"
        return payload


class _RecordingStore:
    """内存版 TurnStore，读与写都留痕。"""

    def __init__(self, turns: list[DialogueTurn] | None = None) -> None:
        self.turns = list(turns or [])
        self.loads: list[str] = []
        self.appended: list[tuple[str, str, str]] = []

    async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
        self.loads.append(thread_id)
        return list(self.turns)

    async def append_turn(self, thread_id: str, *, user: str, assistant: str) -> None:
        self.appended.append((thread_id, user, assistant))


class _StubLLM:
    def __init__(self, *, text: str = LLM_TEXT) -> None:
        self._text = text
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self, messages: Sequence[dict[str, str]], *, trace_id: str | None = None
    ) -> LLMReply:
        self.calls.append(list(messages))
        return LLMReply(text=self._text, degraded=False)


# --------------------------------------------------------------- 辅助构造


def _identity(**overrides: Any) -> DialogueIdentity:
    base: dict[str, Any] = {
        "source": "voice",
        "elder_id": _ELDER_A,
        "display_name": "张桂芬",
    }
    base.update(overrides)
    return DialogueIdentity(**base)


def _decision(
    layer: str = LAYER_L2, *, elder_id: str = _ELDER_A, **overrides: Any
) -> RouteDecision:
    payload: dict[str, Any] = {
        "elder_id": elder_id,
        "thread_id": _THREAD_A,
        "layer": layer,
    }
    payload.update(overrides)
    return RouteDecision(**payload)


def _service(
    *,
    store: _RecordingStore | None = None,
    agent: _RecorderAgent | None = None,
    llm: _StubLLM | None = None,
) -> DialogueService:
    return DialogueService(
        llm=llm if llm is not None else _StubLLM(),
        store=store,
        now_provider=lambda: FIXED_NOW,
        agent=agent if agent is not None else _RecorderAgent(),
    )


# ======================================================================
# 一、history 透传
# ======================================================================


class TestHistoryReachesAgent:
    """L2 轮次必须看得见前文 —— 这是「多轮对话」的最低定义。"""

    async def test_previous_turns_are_expanded_into_messages(self) -> None:
        """一轮 `DialogueTurn`（user 一句 + assistant 一句）展开成两条消息。

        顺序是 `user` 在前 `assistant` 在后，与 `build_dialogue_messages` 同口径：
        两条消息倒过来，模型会以为「那句应答是长者说的」。
        """
        store = _RecordingStore([DialogueTurn(user="我膝盖疼", assistant="那您少走两步")])
        agent = _RecorderAgent()
        service = _service(store=store, agent=agent)

        await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(),
        )

        assert agent.last_history == [
            {"role": "user", "content": "我膝盖疼"},
            {"role": "assistant", "content": "那您少走两步"},
        ]

    async def test_second_turn_sees_the_first_turn_verbatim(self) -> None:
        """连问两轮：第二轮的 history 里必须有第一轮的原话（不是摘要、不是改写）。

        这是计划件 §4 批次 2 的验收句「同一 thread 连问两轮，第二轮能看到第一轮原文」
        的可执行版本。用真 store 语义（读 → 写 → 再读）串起两轮，而不是手工预置历史：
        预置测的是「能不能读」，这里测的是「上一轮到底写没写进去」。
        """

        class _MemoryStore(_RecordingStore):
            """读的时候按 thread 返回**已写入**的内容，模拟真实 store 的因果。"""

            async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
                self.loads.append(thread_id)
                written = [item for item in self.appended if item[0] == thread_id]
                return [
                    DialogueTurn(user=user, assistant=assistant) for _, user, assistant in written
                ]

        store = _MemoryStore()
        agent = _RecorderAgent()
        service = _service(store=store, agent=agent)

        await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="我膝盖疼",
            decision=_decision(),
        )
        await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="那今天还下楼走吗",
            decision=_decision(),
        )

        # 第二轮看到的，是第一轮真的写进去的那两句
        assert agent.last_history == [
            {"role": "user", "content": "我膝盖疼"},
            {"role": "assistant", "content": AGENT_TEXT},
        ]
        # 两轮各写一次，assistant 侧始终是 Agent 的原话
        assert [item[2] for item in store.appended] == [AGENT_TEXT, AGENT_TEXT]

    async def test_one_thread_history_never_leaks_into_another(self) -> None:
        """跨 thread 不串：A 会话的历史不出现在 B 会话的 history 里。

        用两个 thread 但**同一位长者**：这才能把「按 thread 隔离」与「按人隔离」分开 ——
        同一个人换个会话也不该把上一个会话的原文搬过来（那是串台，不是陪伴）。
        """
        per_thread: dict[str, list[DialogueTurn]] = {
            _THREAD_A: [DialogueTurn(user="A 会话里说的话", assistant="A 会话里的应答")],
            _THREAD_B: [DialogueTurn(user="B 会话里说的话", assistant="B 会话里的应答")],
        }

        class _PerThreadStore(_RecordingStore):
            async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
                self.loads.append(thread_id)
                return list(per_thread.get(thread_id, []))

        store = _PerThreadStore()
        agent = _RecorderAgent()
        service = _service(store=store, agent=agent)

        await service.respond(
            thread_id=_THREAD_B,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(thread_id=_THREAD_B),
        )

        history = agent.last_history
        assert {"role": "user", "content": "B 会话里说的话"} in history
        assert all("A 会话" not in item["content"] for item in history)
        # 读的是 B 的 thread_id，不是「随便读一份」
        assert store.loads == [_THREAD_B]

    async def test_empty_history_still_passes_an_empty_list_not_none(self) -> None:
        """没有历史时传 `[]` 而不是 `None`。

        `None` 在 `companion._build_context_messages` 里与「没接线」不可区分 —— 传空列表
        才能让下游断言「编排层确实读过、只是没有内容」，这是两类不同的故障。
        """
        store = _RecordingStore([])
        agent = _RecorderAgent()
        service = _service(store=store, agent=agent)

        await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(),
        )

        assert agent.histories == [[]]


# ======================================================================
# 二、generic 身份（AC-28）
# ======================================================================


class TestGenericIdentityNeverTouchesStore:
    """`source="generic"` = 通用模式，既没有长者身份也没有个人数据。"""

    async def test_generic_identity_passes_empty_history_and_skips_store(self) -> None:
        store = _RecordingStore([DialogueTurn(user="上一位老人的私事", assistant="我记下了")])
        agent = _RecorderAgent()
        service = _service(store=store, agent=agent)

        await service.respond(
            thread_id=_THREAD_A,
            identity=DialogueIdentity(source="generic"),
            user_text="你好",
            decision=_decision(),
        )

        # 读与写都为零：不是「读了但没用」，是一次都不许碰
        assert store.loads == []
        assert store.appended == []
        assert agent.histories == [[]]


# ======================================================================
# 三、privacy 透传
# ======================================================================


class TestPrivacyIsPassedThroughVerbatim:
    """编排层只搬运、不解释：`privacy` 真伪的判定口径只有 `is_privacy_converged`。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("true", "true", id="string-true"),
            pytest.param("false", "false", id="string-false"),
            pytest.param(True, True, id="bool-true"),
            pytest.param(None, None, id="none"),
        ],
    )
    async def test_privacy_raw_value_reaches_agent(self, raw: Any, expected: Any) -> None:
        agent = _RecorderAgent()
        service = _service(store=_RecordingStore(), agent=agent)

        await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(privacy=raw),
        )

        assert agent.privacies == [expected]
        # 身份是原值：bool 不许在半路被字符串化，反之亦然
        assert agent.privacies[0] is expected or agent.privacies[0] == expected


# ======================================================================
# 四、persona 透传
# ======================================================================


class TestPersonaReachesAgent:
    """人设由 `personas` 表加载后随本轮下传（PRD FR-ADM-07）。"""

    async def test_persona_object_is_passed_through_unchanged(self) -> None:
        agent = _RecorderAgent()
        service = _service(store=_RecordingStore(), agent=agent)

        await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="今天天气怎么样",
            persona=PERSONA,
            decision=_decision(),
        )

        # 同一个对象：中间任何一层重造人设，都会让「这一轮用的是哪版人设」失去唯一来源
        assert agent.personas[0] is PERSONA

    async def test_absent_persona_stays_none(self) -> None:
        """调用方没给人设时传 `None`，不许凭空造一个默认人设。

        `None` 在 `_agent_prefix` 里代表「逐字节返回 `AGENT_SYSTEM_PROMPT`」——
        凭空造一个空人设会让人设段多出几行空白，既改了 prompt 又拿不到人设。
        """
        agent = _RecorderAgent()
        service = _service(store=_RecordingStore(), agent=agent)

        await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(),
        )

        assert agent.personas == [None]


# ======================================================================
# 五、空白回复不写回
# ======================================================================


class TestBlankAgentReplyDoesNotPolluteMemory:
    """Agent 返回空白 → 回落 `llm.chat` 且**不写回** Agent 的那句空话。"""

    async def test_blank_agent_text_falls_back_and_writes_llm_text(self) -> None:
        store = _RecordingStore()
        llm = _StubLLM()
        service = _service(store=store, agent=_RecorderAgent(text="   "), llm=llm)

        reply = await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="今天天气怎么样",
            decision=_decision(),
        )

        assert reply.text == LLM_TEXT
        assert len(llm.calls) == 1
        # 写回的是真正说出去的那句（llm 的），不是 Agent 的空白
        assert store.appended == [(_THREAD_A, "今天天气怎么样", LLM_TEXT)]

    async def test_l0_short_circuit_touches_neither_agent_nor_store(self) -> None:
        """L0 短路仍然在最前：命中固定话术时 Redis 与 Agent 都不许碰。

        这条是回归防线 —— 批次 2 把 `_load_history` 提到了 L2 分支之前，一旦提过头
        （提到 L0 短路之前），固定话术也会白读一次 Redis。
        """
        store = _RecordingStore([DialogueTurn(user="不该被读到", assistant="也不该被写到")])
        agent = _RecorderAgent()
        service = _service(store=store, agent=agent)

        reply = await service.respond(
            thread_id=_THREAD_A,
            identity=_identity(),
            user_text="停一下",
            decision=_decision(layer=LAYER_L1, intent="stop"),
        )

        assert reply.text  # 有话术
        assert agent.histories == []
        assert store.loads == []
        assert store.appended == []
