"""迭代 8 · N40 用例先行：L0 命中后短路、零模型。

## 这张卡为什么是用例先行

短路点还没落（Lead 正在插 `respond()`），契约已经定了，所以本文件现在**预期是红的**：
红的是「命中 L0 之后仍然调了一次模型」，这恰好就是 N01 要消灭的行为。真实红信息随
任务报告发给 Lead，他落完 N01 后复跑。

## 契约（Lead 已落地，用例按此写，不改）

- `app/router/decision.py`：`SafetyHit(rule_id, category, severity, matched,
  transcript_excerpt, occurred_at)`；`RouteDecision.alert: SafetyHit | None = None`。
- `app/router/rule_replies.py`：`reply_for(decision) -> str | None`；短路意图闭集
  `SHORT_CIRCUIT_INTENTS = {"alert", "stop", "farewell"}`；表外意图一律返回 `None`。
- 短路点在 `app/services/dialogue_service.py` 的 `respond()`：`validate_thread_id`
  之后、Agent 分支之前。

## 断言口径（三条都被 Lead 点过名，别在这里「优化」）

1. **零模型用调用次数证明，不用文本像不像**：文本可能被别处改掉，`llm.calls` 的长度
   才是硬证据。文本只作为附带断言（同一句固定话术）。
2. **天气句必须 ≥1 次**：这是防「过度短路」的反向用例。如果哪天有人把 `weather_query`
   也塞进短路表，MCP 工具路径就断了，这条必须红——**红了报 Lead，不许改断言迁就实现**。
3. **不碰 Redis 短期记忆**：短路点在取历史之前（契约位置决定），固定话术既不是模型
   说的、也不该被记成「数字人说过的话」（与 L2 分支同一口径）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.context import DialogueIdentity, DialogueTurn
from app.router.decision import LAYER_L0, RouteDecision
from app.router.rule_replies import (
    DIRECT_REPLIES,
    SAFETY_FALLBACK,
    SAFETY_REPLIES,
    SHORT_CIRCUIT_INTENTS,
    reply_for,
)
from app.router.rules import match_l0
from app.services.dialogue_service import DialogueReply, DialogueService
from app.services.llm_service import LLMReply

FIXED_NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)  # 14:00 Asia/Shanghai
THREAD_ID = "t-l0-short-circuit-1"

#: 假模型的话术：与任何固定话术**刻意不同**，短路没生效时一眼能看出来。
LLM_TEXT = "这是模型说的话，不该出现在短路用例里。"


# --------------------------------------------------------------- 替身


class FakeLLM:
    """只做一件事：数自己被调用了几次，并把收到的 messages 留痕。"""

    def __init__(self, *, text: str = LLM_TEXT) -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self, messages: Sequence[dict[str, str]], *, trace_id: str | None = None
    ) -> LLMReply:
        self.calls.append([dict(message) for message in messages])
        return LLMReply(text=self.text, degraded=False)

    @property
    def count(self) -> int:
        return len(self.calls)


class FakeStore:
    """内存版 TurnStore；短路用例里它应当一次都不被碰。"""

    def __init__(self, turns: list[DialogueTurn] | None = None) -> None:
        self.turns = list(turns or [])
        self.loads = 0
        self.appended: list[tuple[str, str, str]] = []

    async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
        self.loads += 1
        return list(self.turns)

    async def append_turn(self, thread_id: str, *, user: str, assistant: str) -> None:
        self.appended.append((thread_id, user, assistant))


@dataclass(frozen=True, slots=True)
class FakeAgentResult:
    text: str = "Agent 说的话"
    degraded: bool = False
    tool_calls: int = 0
    finish_reason: str = "speak"


def _identity(**overrides: Any) -> DialogueIdentity:
    base: dict[str, Any] = {
        "source": "voice",
        "elder_id": "11111111-1111-4111-8111-111111111102",
        "display_name": "张桂芬",
    }
    base.update(overrides)
    return DialogueIdentity(**base)


def _service(
    *,
    llm: FakeLLM | None = None,
    store: FakeStore | None = None,
) -> DialogueService:
    return DialogueService(
        llm=llm if llm is not None else FakeLLM(),
        store=store,
        now_provider=lambda: FIXED_NOW,
    )


async def _respond(
    text: str,
    *,
    llm: FakeLLM,
    store: FakeStore | None = None,
) -> tuple[DialogueReply, RouteDecision]:
    """用**真实 L0 匹配**产出 decision，再交给 respond —— 不手搓 decision 标签。"""
    decision = match_l0(text)
    assert decision is not None, f"「{text}」没命中 L0，用例输入的假设就不成立"
    reply = await _service(llm=llm, store=store).respond(
        thread_id=THREAD_ID,
        identity=_identity(),
        user_text=text,
        decision=decision,
    )
    return reply, decision


# --------------------------------------------------------------- ① 急症：零模型


async def test_emergency_text_never_reaches_the_model() -> None:
    """「我胸口疼」命中 `safety_emergency`：模型一次都不能调，说的是固定话术。"""
    llm = FakeLLM()
    reply, decision = await _respond("我胸口疼", llm=llm)

    assert decision.rule_id == "safety_emergency"
    assert decision.intent == "alert"
    # 硬证据：调用次数。文本只在下面作为附带断言。
    assert llm.count == 0, f"命中急症仍然调用了模型 {llm.count} 次（零模型是硬要求）"
    assert reply.text == SAFETY_REPLIES["safety_emergency"]
    assert reply.degraded is False, "固定话术不是降级话术，不该标 degraded"
    assert reply.tool_calls == 0


# --------------------------------------------------------------- ②③ 停 / 再见：零模型


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("停", DIRECT_REPLIES["stop"]),
        ("再见", DIRECT_REPLIES["farewell"]),
    ],
)
async def test_direct_intents_short_circuit_without_the_model(
    utterance: str, expected: str
) -> None:
    llm = FakeLLM()
    reply, decision = await _respond(utterance, llm=llm)

    assert decision.intent in SHORT_CIRCUIT_INTENTS
    assert llm.count == 0, f"「{utterance}」仍然调用了模型 {llm.count} 次"
    assert reply.text == expected
    assert reply.degraded is False


# --------------------------------------------------------------- ④ 天气：防过度短路


async def test_weather_query_still_reaches_the_model() -> None:
    """短路不得吞掉天气句：它必须落到 MCP/模型路径上（本用例不注入 agent，走 `llm.chat`）。"""
    llm = FakeLLM(text="今天晴，十八到二十四度。")
    reply, decision = await _respond("今天天气怎么样", llm=llm)

    assert decision.intent == "weather_query"
    assert llm.count >= 1, (
        "天气句被短路吞掉了：MCP 工具路径断掉（迭代 6 验收第 3 条）。"
        "这条红了报 Lead，不许改断言迁就实现。"
    )
    assert reply.text == "今天晴，十八到二十四度。"


# --------------------------------------------------------------- ⑤ 表外意图守卫


@pytest.mark.parametrize(
    ("utterance", "intent"),
    [
        ("现在几点", "ask_time"),
        ("再说一遍", "repeat"),
        ("放越剧", "play_yueju"),
    ],
)
def test_reply_for_returns_none_for_out_of_table_intents(utterance: str, intent: str) -> None:
    decision = match_l0(utterance)
    assert decision is not None
    assert decision.intent == intent
    assert intent not in SHORT_CIRCUIT_INTENTS
    assert reply_for(decision) is None, f"「{intent}」不该进短路表（见表外意图守卫的理由）"


# --------------------------------------------------------------- ⑥ 未知安全 rule_id


@pytest.mark.parametrize("rule_id", ["safety_brand_new_rule", None])
def test_unknown_safety_rule_id_falls_back_to_safety_fallback(rule_id: str | None) -> None:
    """未知（或缺失）安全 `rule_id` 也必须「喊人」：落到兜底话术，不是空话。"""
    decision = RouteDecision(
        elder_id=None,
        thread_id=None,
        layer=LAYER_L0,
        rule_id=rule_id,
        intent="alert",
        sensitive=True,
    )

    text = reply_for(decision)

    assert text == SAFETY_FALLBACK
    assert text is not None and text.strip() != ""


# --------------------------------------------------------------- ⑦ SafetyHit：命中证据


def test_safety_hit_carries_review_evidence() -> None:
    """`match_safety` 命中时同时填 `RouteDecision.alert`（Lead 2026-11-10 消息明确的契约）。

    没有这层证据，事后复核只看到一个 `rule_id`，「为什么判成急症」无从查起
    （误报只能靠手工打标）。`occurred_at` 由编排层填，纯文本匹配阶段允许为 None，故不在此断言。
    """
    decision = match_l0("我胸口疼")
    assert decision is not None and decision.intent == "alert"

    hit = decision.alert
    assert hit is not None, "命中安全规则但 RouteDecision.alert 为空：证据层没填"
    assert hit.rule_id == "safety_emergency"
    assert hit.category == "emergency", "category 应是三值 emergency/harm/request_human"
    assert hit.severity == "red", "迭代 8 的红色事件只有一档 red"
    assert hit.matched, "matched 应是命中的原文片段"
    assert hit.matched in hit.transcript_excerpt, "transcript_excerpt 必须包含命中片段"


# --------------------------------------------------------------- ⑧ 契约推论：不碰短期记忆


async def test_short_circuit_does_not_touch_redis_history() -> None:
    """固定话术不是「数字人说过的话」，不该被写进 Redis 短期记忆（与 L2 分支同一口径）。

    这是契约位置（`validate_thread_id` 之后、取历史之前）的推论；红了说明短路点被
    放到了取历史之后，属于要报 Lead 的偏差。
    """
    store = FakeStore()
    llm = FakeLLM()
    reply, _ = await _respond("我胸口疼", llm=llm, store=store)

    assert reply.text == SAFETY_REPLIES["safety_emergency"]
    assert store.loads == 0, "短路路径读了 Redis 历史：短路点位置偏到了取历史之后"
    assert store.appended == [], "短路路径把固定话术写进了 Redis 历史"
