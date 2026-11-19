# owner=W5 · 迭代 4 · Agent 上下文裁剪 + 隐私场景跳过记忆（PRD line 571）

"""W5 验收：`trim_history` 的规则式裁剪 + `RouteDecision.privacy` 为真时跳过记忆召回。

覆盖口径（设计记录（身份闭环缺口补齐） §3-W5）：
- 长历史裁剪：persona 永远在首位、只保留最近 `KEEP_RECENT_MESSAGES` 条、中间压成
  一条「（已省略 N 条更早的对话）」且 N 精确、总条数 ≤ `MAX_HISTORY_MESSAGES`；
- 被裁段里含 `role="tool"` 结果时摘要带上**最后一条**（且只带一条），超过
  `MAX_TOOL_SUMMARY_CHARS` 截断加 `…`——规则式摘要，全程不碰 LLM；
- privacy 为真（bool 或 "true"/"false"/标签字符串）：记忆召回**一次都不调**，
  上下文里出现 `PRIVACY_NOTICE_TEXT`；为假时记忆照常召回并注入（回归）；
- 裁剪确实接进了图：`run_turn(history=...)` 送给 LLM 的 messages 就是裁剪后的形状，
  且隐私说明这种「本轮控制位」不会被长历史挤出去。

假 LLM / 假工具后端 / 假记忆召回器的构造方式沿用 tests/test_agents_companion.py
（`Settings(_env_file=None)` + `httpx.MockTransport`），不新建 fixture 体系；
本文件只测纯函数与 run_turn 的消息形状，**不碰数据库**。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agents import companion as companion_module
from app.agents.companion import (
    AGENT_SYSTEM_PROMPT,
    KEEP_RECENT_MESSAGES,
    MAX_HISTORY_MESSAGES,
    MAX_TOOL_SUMMARY_CHARS,
    PRIVACY_NOTICE_TEXT,
    RuntimeContext,
    is_privacy_converged,
    run_turn,
    trim_history,
)
from app.agents.tool_registry import ToolRegistry, ToolResult
from app.core.config import Settings
from app.core.context import PersonaContext
from app.services import llm_service as llm_service_module
from app.services.llm_service import LLMService

FAKE_KEY = "test-key-not-a-real-secret"
PERSONA: dict[str, Any] = {"role": "system", "content": AGENT_SYSTEM_PROMPT}
#: 「被省略 N 条」的固定句式片段（断言只用这个前缀，不复述整句）。
OMITTED = "（已省略 "


class FakeBackend:
    """工具后端替身：本文件不验证工具行为，只保证 registry 可构造、不会被误触。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, dict(args)))
        return ToolResult(status="ok", content={"text": "默认天气：晴。"})


class FakeRecaller:
    """记忆召回注入缝的替身：记录每次调用的原句，回固定几条记忆文本。"""

    def __init__(self, memories: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self.memories = list(memories or ["长者喜欢越剧《梁山伯与祝英台》。"])

    async def __call__(self, query: str) -> list[str]:
        self.calls.append(query)
        return list(self.memories)


def _settings(**overrides: Any) -> Settings:
    """测试专用 Settings：断开本机 .env（_env_file=None），其余看 llm_service 口径。"""
    base: dict[str, Any] = {
        "llm_base_url": "https://llm.invalid/v1",
        "llm_api_key": FAKE_KEY,
        "llm_model": "test-model",
        "llm_timeout_seconds": 5,
        "llm_max_retries": 2,
        "llm_temperature": 0.3,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _llm(script: list[str], seen: list[list[dict[str, str]]]) -> LLMService:
    """把 llm_service 换成脚本化假 provider（MockTransport，不打真网），并记录入参。"""
    queue = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(list(body["messages"]))
        text = queue.pop(0) if queue else '{"next": "speak", "speech": "默认兜底话术。"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMService(_settings(), client=client)


def _speak(text: str) -> str:
    return json.dumps({"next": "speak", "speech": text}, ensure_ascii=False)


def _history(count: int) -> list[dict[str, Any]]:
    """造 `count` 条普通历史（user/assistant 交替，内容带序号便于断言顺序）。"""
    turns: list[dict[str, Any]] = []
    for index in range(count):
        turns.append(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"第 {index} 条历史",
            }
        )
    return turns


def _runtime() -> RuntimeContext:
    return RuntimeContext(elder_id="elder-1", thread_id="thread-1", trace_id="trace-w5")


@pytest.fixture(autouse=True)
def _fake_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """把两处真实等待（llm 重试退避与 W1 60s×3）换成即时 no-op，和 W3 用例同款。"""

    async def _no_route_wait(_seconds: float) -> None:
        return None

    async def _no_llm_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(companion_module, "_route_decision_import_sleep", _no_route_wait)
    monkeypatch.setattr(llm_service_module, "_sleep", _no_llm_sleep)


# ------------------------------------------------------------------ trim_history


def test_trim_history_bounds_long_history() -> None:
    """30 条（1 persona + 29 条历史）→ persona + 摘要 + 最近 8 条，N 精确。"""
    messages = [PERSONA, *_history(29)]
    assert len(messages) == 30

    trimmed = trim_history(messages)

    assert len(trimmed) == 10
    assert len(trimmed) <= MAX_HISTORY_MESSAGES
    # 首条仍是 persona（prompt cache 的固定前缀，绝不能被裁）。
    assert trimmed[0]["role"] == "system"
    assert trimmed[0]["content"] == AGENT_SYSTEM_PROMPT
    # 中间一条摘要，N = 被裁掉的 29 - 8 = 21。
    assert trimmed[1]["role"] == "system"
    assert trimmed[1]["content"].startswith(f"{OMITTED}21 条更早的对话）")
    # 其余是最近 keep_recent 条，顺序不变。
    assert [m["content"] for m in trimmed[2:]] == [f"第 {i} 条历史" for i in range(21, 29)]
    assert [m["role"] for m in trimmed[2:]] == ["assistant", "user"] * 4
    # 输入列表不被就地改写。
    assert len(messages) == 30


def test_trim_history_keeps_everything_within_budget() -> None:
    """条数没超预算时原样返回：不裁、也不塞一条空摘要。"""
    messages = [PERSONA, *_history(MAX_HISTORY_MESSAGES - 1)]
    assert len(messages) == MAX_HISTORY_MESSAGES

    trimmed = trim_history(messages)

    assert trimmed == messages
    assert not any(OMITTED in str(message["content"]) for message in trimmed)


def test_trim_history_without_persona_still_caps() -> None:
    """没有首条 system 时也要封住条数（摘要占一条，保留 keep_recent 条）。"""
    trimmed = trim_history(_history(30))

    assert len(trimmed) == KEEP_RECENT_MESSAGES + 1
    assert trimmed[0]["role"] == "system"
    assert trimmed[0]["content"].startswith(f"{OMITTED}22 条更早的对话）")
    assert trim_history([]) == []


def test_trim_summary_carries_last_tool_result() -> None:
    """被裁段里的工具结果要带上，且只带最后一条。"""
    messages: list[dict[str, Any]] = [
        PERSONA,
        {"role": "user", "content": "帮我查杭州天气"},
        {"role": "tool", "content": "工具输出 A：杭州小雨。"},
        {"role": "tool", "content": "工具输出 B：杭州晴，26 度。"},
        *_history(20),
    ]

    trimmed = trim_history(messages)
    summary = trimmed[1]["content"]

    assert summary.startswith(f"{OMITTED}15 条更早的对话）")
    assert summary.endswith("最后一条工具结果：工具输出 B：杭州晴，26 度。")
    assert summary.count("最后一条工具结果：") == 1
    assert all(message["role"] != "tool" for message in trimmed[2:])


def test_trim_summary_truncates_long_tool_result() -> None:
    """工具结果超长时截到 MAX_TOOL_SUMMARY_CHARS 并加 `…`（摘要自己不撑爆窗口）。"""
    long_text = "晴" * 400
    messages: list[dict[str, Any]] = [
        PERSONA,
        {"role": "tool", "content": long_text},
        *_history(20),
    ]

    trimmed = trim_history(messages)
    summary = trimmed[1]["content"]
    marker = "最后一条工具结果："
    assert marker in summary

    tail = summary.split(marker, 1)[1]
    assert len(tail) == MAX_TOOL_SUMMARY_CHARS + 1
    assert tail == f"{long_text[:MAX_TOOL_SUMMARY_CHARS]}…"


# ------------------------------------------------------------------ 隐私判定口径


@pytest.mark.parametrize(
    ("privacy", "expected"),
    [
        (True, True),
        ("true", True),
        ("couple", True),
        ("双人间", True),
        (False, False),
        ("false", False),
        ("  FALSE ", False),
        ("0", False),
        ("", False),
        (None, False),
    ],
)
def test_is_privacy_converged_accepts_bool_and_string(privacy: Any, expected: bool) -> None:
    """bool（Laya 五信号）与 str（落库形态/标签）都要接；只判「是否明确为假」。"""
    assert is_privacy_converged(privacy) is expected


# ------------------------------------------------------------------ 图的上下文入口


async def test_privacy_turn_skips_memory_recall() -> None:
    """privacy=true：召回一次都不调，上下文里出现隐私说明句。"""
    recaller = FakeRecaller()
    seen: list[list[dict[str, str]]] = []

    result = await run_turn(
        "隔壁张阿姨刚才说什么了",
        llm=_llm([_speak("我不打听别人家的事，咱们聊点您自己的。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-w5",
        privacy=True,
        memory_recall=recaller,
    )

    assert recaller.calls == []  # 关键断言：整段跳过，0 次调用
    assert result.finish_reason == "speak"
    messages = seen[0]
    assert messages[0]["content"] == AGENT_SYSTEM_PROMPT
    assert messages[1]["content"] == PRIVACY_NOTICE_TEXT
    assert not any("梁山伯" in message["content"] for message in messages)


@pytest.mark.parametrize("privacy", [False, "false", None])
async def test_plain_turn_still_recalls_memory(privacy: Any) -> None:
    """回归：privacy 为假时记忆照常召回并注入，且没有隐私说明句。"""
    recaller = FakeRecaller()
    seen: list[list[dict[str, str]]] = []

    await run_turn(
        "今天想听点什么？",
        llm=_llm([_speak("我给您放一段越剧吧。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-w5",
        privacy=privacy,
        memory_recall=recaller,
    )

    assert recaller.calls == ["今天想听点什么？"]  # 入参是本轮原句
    messages = seen[0]
    assert any("梁山伯" in message["content"] for message in messages)
    assert not any(message["content"] == PRIVACY_NOTICE_TEXT for message in messages)


async def test_run_turn_trims_long_history_before_graph() -> None:
    """裁剪后的列表真的进了图：LLM 收到的是 persona + 摘要 + 最近 8 条 + 本轮用户句。"""
    seen: list[list[dict[str, str]]] = []

    result = await run_turn(
        "我们刚才聊到哪儿了",
        llm=_llm([_speak("您刚才说起想听越剧。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-w5",
        history=_history(29),
    )

    assert result.finish_reason == "speak"
    messages = seen[0]
    assert len(messages) == 11  # 1 persona + 1 摘要 + 8 保留 + 1 本轮用户句
    assert len(messages) <= MAX_HISTORY_MESSAGES + 1
    assert sum(1 for m in messages if m["content"] == AGENT_SYSTEM_PROMPT) == 1
    assert messages[1]["content"].startswith(f"{OMITTED}21 条更早的对话）")
    assert messages[-1]["content"] == "用户说：我们刚才聊到哪儿了"


async def test_privacy_notice_survives_long_history() -> None:
    """长会话 + 隐私收敛：说明句不会被裁出去（模型看不见就不会守）。"""
    recaller = FakeRecaller()
    seen: list[list[dict[str, str]]] = []

    await run_turn(
        "刚才隔壁那家在说什么",
        llm=_llm([_speak("咱们不说别人的事。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-w5",
        history=_history(29),
        privacy="true",
        memory_recall=recaller,
    )

    messages = seen[0]
    assert recaller.calls == []
    assert PRIVACY_NOTICE_TEXT in [message["content"] for message in messages]
    assert len(messages) <= MAX_HISTORY_MESSAGES


# ---------------------------------------------------------------- 人设注入（批次 1）


def _persona() -> PersonaContext:
    return PersonaContext(
        name="小满",
        personality="慢条斯理，爱用比喻，不说重话",
        tone_examples=("咱们慢慢来", "这个事儿不急"),
    )


async def test_persona_none_keeps_prompt_byte_identical() -> None:
    """没有 persona 时首条必须**逐字节**等于 `AGENT_SYSTEM_PROMPT`。

    这条是 prompt cache 的防线：前缀只要多一个空格，供应商的 prefix cache 就整段失效，
    而且 `tests/test_context_build.py` 那几条拿字符串做等值断言的用例也会一起红。
    与 `_agent_prefix(None)` 的「逐字节返回」是同一条契约的两个观测点。
    """
    seen: list[list[dict[str, str]]] = []

    await run_turn(
        "今天天气怎么样",
        llm=_llm([_speak("今天晴。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-persona",
    )

    assert seen[0][0]["content"] == AGENT_SYSTEM_PROMPT


async def test_persona_prefix_carries_persona_and_keeps_protocol() -> None:
    """有 persona 时：人设段进去了，**协议段原样保留**。

    协议段（「每一步你都必须只返回一个 JSON 对象」）是 ReAct 的解析契约 ——
    它一旦被人设段顶掉，Agent 立刻不会调工具（返回自由文本 → 解析失败 → 整轮降级）。
    所以这里断言的是「叠加」而不是「替换」：三样东西都要在。
    """
    seen: list[list[dict[str, str]]] = []
    persona = _persona()

    await run_turn(
        "今天天气怎么样",
        llm=_llm([_speak("今天晴。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-persona",
        persona=persona,
    )

    prefix = seen[0][0]["content"]
    assert "小满" in prefix  # 名字
    assert "慢条斯理" in prefix  # 性格
    assert "咱们慢慢来" in prefix  # 说话示例
    assert prefix.endswith(AGENT_SYSTEM_PROMPT)  # 协议段完整保留且在末尾
    # 协议段只出现一次：重复拼接会让模型收到两份规则，行为不可预期
    assert prefix.count(AGENT_SYSTEM_PROMPT) == 1


async def test_persona_prefix_is_byte_stable_across_turns() -> None:
    """同一 persona 连续组装两次，前缀字节完全相同（prompt cache 稳定性）。

    `PersonaContext.tone_examples` 保序不去重（见 `app/core/context.py` 的 docstring），
    就是为了这条：任何「为了好看」的重排都会让第二个前缀 ≠ 第一个，缓存全废。
    """
    persona = _persona()
    seen: list[list[dict[str, str]]] = []

    for _ in range(2):
        await run_turn(
            "今天天气怎么样",
            llm=_llm([_speak("今天晴。")], seen),
            tool_registry=ToolRegistry(backend=FakeBackend()),
            runtime_context=_runtime(),
            trace_id="trace-persona",
            persona=persona,
        )

    assert len(seen) == 2
    assert seen[0][0]["content"] == seen[1][0]["content"]


async def test_privacy_notice_sits_after_persona_prefix() -> None:
    """隐私说明必须插在**前缀之后**（index 1），不能盖在人设前面。

    `build_agent_context` 靠 `messages[0]["content"] == prefix` 定位前缀；`_build_context_messages`
    若给 `raw` 首条与 `prefix=` 传了不同的字符串，控制位就会被插到前缀之前 ——
    模型先读到「本轮为隐私收敛模式」再读到人设与协议段，指令优先级反了。
    """
    seen: list[list[dict[str, str]]] = []
    persona = _persona()

    await run_turn(
        "隔壁张阿姨刚才说什么了",
        llm=_llm([_speak("咱们不说别人的事。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-persona",
        privacy=True,
        persona=persona,
    )

    messages = seen[0]
    assert messages[0]["content"].endswith(AGENT_SYSTEM_PROMPT)
    assert messages[1]["content"] == PRIVACY_NOTICE_TEXT
