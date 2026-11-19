"""迭代 6 三条验收的证据化用例（task-53）。

## 为什么有这个文件

迭代 6 的三条功能验收（`设计记录（三级路由）` 起）此前只有
「读过代码、判断成立」这一层判断，没有可重复执行的证据。本文件把三条各钉成
一组用例，并**刻意钉住可观测的调用次数**，而不是只断言布尔标志位：

| 验收条 | 本文件钉的是什么 |
| --- | --- |
| ①「我胸口疼」→ 人工分支、不继续闲聊 | `route()` 命中 L0/`safety_emergency`/`intent="alert"`/`sensitive=True`，Laya **零调用**；这一轮**不升级到 L2/Agent**（`agent.calls == 0`）；并另钉一条「云 LLM 零调用」（当前为缺陷，见下） |
| ②「停」→ 零模型 | 命中 L0/`play_control`/`intent="stop"`，Laya **零调用**；整轮 `llm.chat` 零调用（当前为缺陷）；规则层往返耗时 < 3s |
| ③含糊话 → 只澄清一次 | 首轮 L1 + `degraded="low_confidence_clarify"` 且 `turn_state["clarified"]` 被置位；**同一 turn_state** 第二轮升 L2 + `degraded="low_confidence_twice"` |

## 三条口径（写在这里，免得下一个人误读）

1. **「不继续闲聊」不能只断言 `sensitive is True`。** 标志位为真而这一轮照样进了
   `llm.chat`，长者听到的就是一句闲聊 —— 安全一票否决没有落地。所以这里断言的是
   生成路径的**调用次数**。
2. **②与 `tests/test_routing_l1.py::test_l0_hit_never_calls_decide_l1` 不是一件事。**
   那条钉「L0 命中时 `decide_l1` 不被调」（路由层的问题）；本条钉「整轮不碰云模型」
   （编排层的问题）—— 两者可以一个成立、一个不成立，实测正是如此。
3. **「3 秒内」只能用规则层耗时证明。** `test_rule_layer_round_trip_under_three_seconds`
   量的是「文本进 `route()` 到决策返回」的墙钟，全程不调云模型、不合成 TTS。
   端到端（ASR + 路由 + 生成 + TTS）的 3 秒**不在本文件证明范围**，也不该由这里伪造。

## 两个缺陷：已修复，闸门已摘

`app/services/dialogue_service.py:317-324` 的 L2 判据只认
`layer == "L2"` 或 `intent ∈ {"weather_query"}`，**曾没有 `intent == "alert"` 的短路**，
也没有任何 L0 固定意图的短路。于是：

- 「我胸口疼」这一轮会被送进 `llm.chat`（实测 1 次），闲聊模型生成的句子直接被播给长者；
- 「停」这一轮同样会进 `llm.chat`（实测 1 次），与「零模型」验收相悖。

这两条当时用 `xfail(strict=True)` 钉住：**当时**它们红（记为 xfailed，套件保持绿），
缺陷修好后它们会 XPASS 从而**变成失败**，强制下一个人回来把标记摘掉、把断言留下。

2026-11-10 N01（task-57）落地 L0 短路后，两道闸门**如设计般变红**（`[XPASS(strict)]`），
标记已按 lead 授权摘除，两条现在是硬断言（`assert llm.calls == 0`）。

## 边界

- 全部依赖用替身：不连 Laya 7001、不连库、不连 Redis、不打云模型；
- 不断言 `route_ms` 的具体毫秒数（机器相关），只断言上界 3s；
- 不 import `app.audio.*`（垫层装配是另一条路径），只走 `route()` 与 `DialogueService`。
"""

from __future__ import annotations

from time import perf_counter
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.context import DialogueIdentity
from app.router import routing
from app.router.decision import LAYER_L0, LAYER_L1, LAYER_L2
from app.router.rules import match_l0
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMReply
from app.system_one.client import FiveSignal
from app.system_one.questions import (
    DEGRADED_LOW_CONFIDENCE_CLARIFY,
    DEGRADED_LOW_CONFIDENCE_TWICE,
)

_ELDER_ID = "11111111-1111-4111-8111-111111111111"
_THREAD_ID = "thread-day06-acceptance"
_TRACE_ID = "trace-day06-acceptance"
_NOW = 1_781_000_000.0

#: 急症正例（`app/router/safety.py` 的 `_EMERGENCY_PATTERN` 里有 `胸口疼`）。
_EMERGENCY_TEXT = "我胸口疼"
#: 固定意图正例（`app/router/rules.py` 的 `play_control` 分支）。
_STOP_TEXT = "停"
#: 含糊话：必须不被 L0 命中（用例里会先断言），只可能落到 L1。
_VAGUE_TEXT = "嗯…那个"


class _LayaMustNotBeCalled:
    """Laya 替身：被调用即失败。

    L0 命中时调 Laya 说明「低层被高层覆盖」（AGENTS §6），是 P0 缺陷，
    所以这里用 AssertionError 而不是计数返回 —— 让红的第一现场就是调用点。
    """

    def __init__(self) -> None:
        self.settings = SimpleNamespace(laya_confidence_threshold=0.85)
        self.calls: list[str] = []

    async def classify(self, text: str) -> FiveSignal:
        self.calls.append(text)
        raise AssertionError("L0 命中时不允许调用 Laya（低层不得被高层覆盖）")


class _FakeLaya:
    """回放预设五信号 / 预设异常的 Laya 替身（`decide_l1` 只用到这两个口子）。"""

    def __init__(self, *, signal: FiveSignal | None = None, threshold: float = 0.85) -> None:
        self.signal = signal
        self.settings = SimpleNamespace(laya_confidence_threshold=threshold)
        self.calls: list[str] = []

    async def classify(self, text: str) -> FiveSignal:
        self.calls.append(text)
        assert self.signal is not None
        return self.signal


class _CountingLLM:
    """云 LLM 替身：只数调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, *, trace_id: str | None = None) -> LLMReply:
        self.calls += 1
        return LLMReply(text="闲聊一句", degraded=False)


class _CountingAgent:
    """L2 Agent runner 替身：只数调用次数，返回一个合法的 AgentResult 形状。

    `identity` 形参与 `AgentRunner` 契约一致（`respond` 每轮都会带）：本文件只关心
    「急症轮有没有升级到 Agent」，身份内容不参与断言，所以收下就丢。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, user_text: str, *, identity: Any = None) -> Any:
        self.calls += 1
        return SimpleNamespace(
            text="Agent 回复", degraded=False, tool_calls=0, finish_reason="stop"
        )


def _identity() -> DialogueIdentity:
    return DialogueIdentity(source="device", elder_id=_ELDER_ID, display_name="张奶奶")


async def _route(text: str, laya: Any, *, turn_state: dict[str, Any] | None = None) -> Any:
    """走真正的 `app.router.routing.route()`，只把 Laya 换成替身。"""
    return await routing.route(
        text,
        laya=laya,
        turn_state={} if turn_state is None else turn_state,
        now=_NOW,
        elder_id=_ELDER_ID,
        thread_id=_THREAD_ID,
        trace_id=_TRACE_ID,
    )


async def _respond(decision: Any, *, llm: _CountingLLM, agent: Any = None) -> Any:
    """把一条路由决策送进真正的 `DialogueService.respond()`（不连库、不连 Redis）。"""
    service = DialogueService(llm=llm, agent=agent)
    return await service.respond(
        thread_id=_THREAD_ID,
        identity=_identity(),
        user_text="（本轮转写）",
        decision=decision,
    )


# ---------------------------------------------------------------- ① 胸口疼


@pytest.mark.asyncio
async def test_emergency_short_circuits_to_l0_without_laya() -> None:
    """「我胸口疼」→ L0/safety_emergency/alert/sensitive，且 Laya 一次都不被调。"""
    laya = _LayaMustNotBeCalled()
    decision = await _route(_EMERGENCY_TEXT, laya)

    assert decision.layer == LAYER_L0
    assert decision.rule_id == "safety_emergency"
    assert decision.intent == "alert"
    assert decision.sensitive is True
    # 没经过 Laya 就没有置信度语义：写成一个具体值，防止「顺手补一个 0.0」把
    # 「其实走了 L1」这件事洗白。
    assert decision.confidence is None
    assert laya.calls == []


@pytest.mark.asyncio
async def test_emergency_turn_does_not_escalate_to_agent() -> None:
    """急症轮不升级到 L2/Agent：`agent.calls == 0`，回复不是 Agent 产物。"""
    decision = await _route(_EMERGENCY_TEXT, _LayaMustNotBeCalled())
    llm = _CountingLLM()
    agent = _CountingAgent()

    reply = await _respond(decision, llm=llm, agent=agent)

    assert decision.layer == LAYER_L0
    assert decision.layer != LAYER_L2
    assert agent.calls == 0
    assert reply.tool_calls == 0


@pytest.mark.asyncio
async def test_emergency_turn_never_calls_cloud_llm() -> None:
    """「不继续闲聊」的硬证据：急症这一轮云 LLM 调用次数必须是 0。

    曾以 `xfail(strict=True)` 钉住缺口；N01 落地短路后 XPASS（闸门按设计变红），
    2026-11-10 按 lead 授权摘标记，现在是硬断言。
    """
    decision = await _route(_EMERGENCY_TEXT, _LayaMustNotBeCalled())
    llm = _CountingLLM()

    await _respond(decision, llm=llm)

    assert llm.calls == 0


# ---------------------------------------------------------------- ② 停


@pytest.mark.asyncio
async def test_stop_short_circuits_to_l0_without_laya() -> None:
    """「停」→ L0/play_control/stop，且 Laya 一次都不被调。"""
    laya = _LayaMustNotBeCalled()
    decision = await _route(_STOP_TEXT, laya)

    assert decision.layer == LAYER_L0
    assert decision.rule_id == "play_control"
    assert decision.intent == "stop"
    assert decision.sensitive is False
    assert laya.calls == []


@pytest.mark.asyncio
async def test_stop_turn_never_calls_cloud_llm() -> None:
    """「停 → 零模型」的硬证据：整轮 `llm.chat` 与 `decide_l1` 都必须是 0 次。

    曾以 `xfail(strict=True)` 钉住缺口；N01 落地短路后 XPASS（闸门按设计变红），
    2026-11-10 按 lead 授权摘标记，现在是硬断言。
    """
    laya = _LayaMustNotBeCalled()
    decision = await _route(_STOP_TEXT, laya)
    llm = _CountingLLM()

    await _respond(decision, llm=llm)

    assert laya.calls == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_rule_layer_round_trip_under_three_seconds() -> None:
    """规则层往返 < 3s（**只证明规则层**，不证明端到端 3 秒，见模块 docstring 口径 3）。"""
    laya = _LayaMustNotBeCalled()

    started = perf_counter()
    emergency = await _route(_EMERGENCY_TEXT, laya)
    stop = await _route(_STOP_TEXT, laya)
    elapsed_ms = (perf_counter() - started) * 1000.0

    assert emergency.intent == "alert"
    assert stop.intent == "stop"
    assert laya.calls == []
    assert elapsed_ms < 3_000.0


# ---------------------------------------------------------------- ③ 含糊话


def test_vague_text_is_outside_l0() -> None:
    """前置：选的含糊话不被 L0 命中（否则后面两条被短路，成假绿）。"""
    assert match_l0(_VAGUE_TEXT) is None


@pytest.mark.asyncio
async def test_vague_text_clarifies_only_once_then_escalates() -> None:
    """首轮澄清一次（并写下 clarified 标记）；同一 turn 第二轮升 L2，不再纠缠。"""
    laya = _FakeLaya(
        signal=FiveSignal(
            intent="chitchat",
            sensitive=False,
            confidence=0.42,
            privacy=False,
            is_out_of_domain=False,
        )
    )
    turn_state: dict[str, Any] = {}

    first = await _route(_VAGUE_TEXT, laya, turn_state=turn_state)

    assert first.layer == LAYER_L1
    assert first.degraded == DEGRADED_LOW_CONFIDENCE_CLARIFY
    # 「只澄清一次」的全部依据就是这个标记：它是第二轮升层的唯一输入。
    assert turn_state["clarified"] is True

    second = await _route(_VAGUE_TEXT, laya, turn_state=turn_state)

    assert second.layer == LAYER_L2
    assert second.degraded == DEGRADED_LOW_CONFIDENCE_TWICE
