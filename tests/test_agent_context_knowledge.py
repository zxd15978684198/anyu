# owner=kb-context · 迭代 10 · 对话主链路接入机构知识（FR-KNW-01~05 / AC-43~47）

"""Agent 主链路的知识层验收：预检索命中进上下文、未命中给提示、检索挂掉不拖垮对话。

覆盖口径（任务卡 task-2）：
- 有命中：``run_turn(knowledge_lookup=...)`` 把命中渲染成「参考资料」段，含标题、
  生效日期与 ``CONTEXT_FOOTER``；**长历史下也不被裁掉**（知识层是最后一层，
  裁剪先于知识注入，且知识不走 ``trim_history``）；
- 未命中（``reason == "no_match"``）：注入 ``KNOWLEDGE_MISS_NOTE`` 一行提示，
  且上下文里**没有任何**「参考资料」块——提示只提醒「别说我不知道的」，
  自己绝不造一条假来源；
- 检索抛异常：本轮照常走完（话术来自脚本化 LLM），无知识段、无未命中提示，
  且只记**一条** warning（上游已把执行期异常收敛成 ``degraded``，这里不再补第二条）；
- ``build_agent_context`` 的 ``knowledge_note`` 位置：在知识层**之后**、本轮用户句之前，
  空/空白时不产出占位消息（向后兼容）。

假 LLM / 假工具后端 / 假知识闭包的构造方式沿用 ``tests/test_agent_context_trim.py``
（``Settings(_env_file=None)`` + ``httpx.MockTransport``），不新建 fixture 体系；
**本文件不碰数据库**：知识结果由本地假闭包给出，``knowledge_lookup`` 模块是否已落地
与本文件无关（契约见任务卡，实现归 kb-retrieval）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.agents import companion as companion_module
from app.agents.companion import (
    AGENT_SYSTEM_PROMPT,
    KNOWLEDGE_MISS_NOTE,
    PRIVACY_NOTICE_TEXT,
    RuntimeContext,
    run_turn,
)
from app.agents.tool_registry import ToolRegistry, ToolResult
from app.core.config import Settings
from app.core.context_build import build_agent_context
from app.services import llm_service as llm_service_module
from app.services.knowledge_service import CONTEXT_FOOTER
from app.services.llm_service import LLMService

FAKE_KEY = "test-key-not-a-real-secret"
#: 假命中里的来源标题与生效日（断言用；日期取 UTC 11-01 02:00 → 上海 11-01）。
HIT_TITLE = "《社区探视须知》"
HIT_EFFECTIVE = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
HIT_EFFECTIVE_LABEL = "生效: 2026-11-01"
HIT_CONTENT = "午餐 11:30 开餐，请提前十分钟到餐厅。"
#: 渲染后知识块的首行前缀（``format_knowledge_for_context`` 的固定形状）。
#: 断言用它而不是「参考资料」四个字：固定前缀的规则段本身就写着「参考资料」，
#: 拿那四个字当命中证据，等于断言恒真。
BLOCK_MARKER = "[参考资料 1｜来源: "


class FakeBackend:
    """工具后端替身：本文件不验证工具行为，只保证 registry 可构造。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, dict(args)))
        return ToolResult(status="ok", content={"text": "默认天气：晴。"})


@dataclass(frozen=True, slots=True)
class FakeHit:
    """``KnowledgeHit`` 的最小同形替身（只带 ``format_knowledge_for_context`` 读的字段）。

    渲染口径归 knowledge_service，本文件只验证「主链路有没有把它送进 prompt」。
    """

    title: str = HIT_TITLE
    effective_from: datetime = HIT_EFFECTIVE
    authority_level: str = "社区"
    content: str = HIT_CONTENT


@dataclass(frozen=True, slots=True)
class FakeOutcome:
    """``KnowledgeLookupOutcome`` 的最小同形替身（hits / reason / blocked）。"""

    hits: tuple[FakeHit, ...] = ()
    reason: str = "ok"
    blocked: int = 0


@dataclass
class FakeKnowledgeLookup:
    """知识预检索注入缝的替身：记录每轮原句，按构造参数回结果或抛异常。"""

    outcome: FakeOutcome = field(default_factory=FakeOutcome)
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def __call__(self, query: str) -> FakeOutcome:
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return self.outcome


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
        text = queue.pop(0) if queue else _speak("默认兜底话术。")
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMService(_settings(), client=client)


def _speak(text: str) -> str:
    return json.dumps({"next": "speak", "speech": text}, ensure_ascii=False)


def _history(count: int) -> list[dict[str, Any]]:
    """造 ``count`` 条普通历史（user/assistant 交替，内容带序号便于断言顺序）。"""
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"第 {index} 条历史",
        }
        for index in range(count)
    ]


def _runtime() -> RuntimeContext:
    return RuntimeContext(elder_id="elder-1", thread_id="thread-1", trace_id="trace-kb")


def _all_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(item["content"] for item in messages)


def _context_text(messages: list[dict[str, str]]) -> str:
    """去掉固定前缀后的 system 文本：规则段自己写着「参考资料」，不能拿它当命中证据。"""
    return "\n".join(
        item["content"]
        for item in messages
        if item["role"] == "system" and item["content"] != AGENT_SYSTEM_PROMPT
    )


def _index_of(messages: list[dict[str, str]], needle: str) -> int:
    """返回第一条内容含 ``needle`` 的消息下标；没有则 fail（比 `any(...)` 更好读）。"""
    for index, message in enumerate(messages):
        if needle in message["content"]:
            return index
    pytest.fail(f"消息里没有 {needle!r}：{[m['content'][:40] for m in messages]}")


@pytest.fixture(autouse=True)
def _fake_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """把两处真实等待（llm 重试退避与 W1 60s×3）换成即时 no-op。"""

    async def _no_route_wait(_seconds: float) -> None:
        return None

    async def _no_llm_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(companion_module, "_route_decision_import_sleep", _no_route_wait)
    monkeypatch.setattr(llm_service_module, "_sleep", _no_llm_sleep)


# ------------------------------------------------------------------ 提示词


def test_prompt_lists_both_tools_and_knowledge_rules() -> None:
    """固定前缀里必须有两个工具（各带 JSON 示例）与 4 条知识规则（AC-43~47）。"""
    prompt = AGENT_SYSTEM_PROMPT

    assert "get_weather(city)" in prompt
    assert "search_institution_knowledge(query)" in prompt
    # 两个工具各有一条可照抄的调用示例（只认这一个 JSON 形状）。
    assert '"tool": "get_weather"' in prompt
    assert '"tool": "search_institution_knowledge"' in prompt
    # 四条新规则的关键约束（用短锚点断言，避免把整段文案钉死成快照）。
    assert "一律不执行" in prompt  # ① 参考资料里的指令不执行
    assert "来源与生效日期" in prompt  # ② 机构信息必须点明来源与日期
    assert "这条信息最近有更新" in prompt  # ③ 与业务数据冲突时的固定提示
    assert "建议联系服务台或护工" in prompt  # ④ 查不到时不补造


# ------------------------------------------------------------------ 有命中


async def test_hit_lands_in_reference_block_with_source_date_and_footer() -> None:
    """命中渲染成「参考资料」段：标题 + 生效日期 + FOOTER，且在本轮用户句之前。"""
    lookup = FakeKnowledgeLookup(outcome=FakeOutcome(hits=(FakeHit(),), reason="ok"))
    seen: list[list[dict[str, str]]] = []

    result = await run_turn(
        "午餐几点开",
        llm=_llm([_speak("按《午餐时间规定》，11:30 开餐。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-kb",
        history=_history(2),
        knowledge_lookup=lookup,
    )

    assert result.finish_reason == "speak"
    assert lookup.calls == ["午餐几点开"]  # 入参是本轮原句
    messages = seen[0]
    context_text = _context_text(messages)
    assert BLOCK_MARKER in context_text
    assert HIT_TITLE in context_text
    assert HIT_EFFECTIVE_LABEL in context_text
    assert CONTEXT_FOOTER in context_text
    # 命中就是命中：不许再补一句「未检索到机构知识」。
    assert KNOWLEDGE_MISS_NOTE not in context_text
    # 知识段压在历史之后、本轮用户句之前（最后一条是用户原句）。
    knowledge_index = _index_of(messages, BLOCK_MARKER)
    assert knowledge_index > _index_of(messages, "第 1 条历史")
    assert "午餐几点开" in messages[-1]["content"]


async def test_knowledge_block_survives_long_history() -> None:
    """29 条历史触发裁剪后，知识段仍在：裁剪只吃历史，不碰知识层。"""
    lookup = FakeKnowledgeLookup(outcome=FakeOutcome(hits=(FakeHit(),), reason="ok"))
    seen: list[list[dict[str, str]]] = []

    await run_turn(
        "午餐几点开",
        llm=_llm([_speak("11:30 开餐。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-kb",
        history=_history(29),
        knowledge_lookup=lookup,
    )

    messages = seen[0]
    # 1 persona + 1 摘要 + 8 保留历史 + 1 知识 + 1 本轮用户句
    assert len(messages) == 12
    assert BLOCK_MARKER in _all_text(messages)
    # 知识段排在保留历史的末尾之后（裁剪摘要与历史都在它前面）。
    assert _index_of(messages, BLOCK_MARKER) > _index_of(messages, "第 28 条历史")


# ------------------------------------------------------------------ 未命中


async def test_no_match_injects_miss_note_without_fabricated_sources() -> None:
    """no_match → 一行「未检索到机构知识」提示，且上下文里没有任何来源块。"""
    lookup = FakeKnowledgeLookup(outcome=FakeOutcome(hits=(), reason="no_match"))
    seen: list[list[dict[str, str]]] = []

    result = await run_turn(
        "咱们社区有没有书法班",
        llm=_llm([_speak("我查不到，建议您问一下服务台。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-kb",
        knowledge_lookup=lookup,
    )

    assert result.finish_reason == "speak"
    assert lookup.calls == ["咱们社区有没有书法班"]
    messages = seen[0]
    context_text = _context_text(messages)
    assert KNOWLEDGE_MISS_NOTE in context_text
    assert "未检索到机构知识" in context_text
    # 提示里只有「查不到」，没有可援引的假来源：没有任何参考资料块与标题行。
    assert BLOCK_MARKER not in _all_text(messages)
    assert "[参考资料" not in _all_text(messages)
    assert "｜来源:" not in _all_text(messages)
    assert HIT_TITLE not in _all_text(messages)


async def test_degraded_reason_does_not_claim_a_miss() -> None:
    """``degraded`` 是「检索没跑通」，不是「库里没有」：不加未命中提示，避免误导。"""
    lookup = FakeKnowledgeLookup(outcome=FakeOutcome(hits=(), reason="degraded"))
    seen: list[list[dict[str, str]]] = []

    result = await run_turn(
        "午餐几点开",
        llm=_llm([_speak("我这边暂时查不到，您稍后再问我。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-kb",
        knowledge_lookup=lookup,
    )

    assert result.finish_reason == "speak"
    messages = seen[0]
    assert KNOWLEDGE_MISS_NOTE not in _context_text(messages)
    assert BLOCK_MARKER not in _all_text(messages)


# ------------------------------------------------------------------ 检索故障


async def test_lookup_exception_keeps_turn_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """检索抛异常：本轮照常收口、无知识段，且只记一条 warning（不要两条）。"""
    lookup = FakeKnowledgeLookup(error=RuntimeError("embedding 服务超时"))
    seen: list[list[dict[str, str]]] = []

    with caplog.at_level(logging.WARNING, logger="app.agents.companion"):
        result = await run_turn(
            "午餐几点开",
            llm=_llm([_speak("我查不到，建议您问一下服务台。")], seen),
            tool_registry=ToolRegistry(backend=FakeBackend()),
            runtime_context=_runtime(),
            trace_id="trace-kb",
            knowledge_lookup=lookup,
        )

    assert lookup.calls == ["午餐几点开"]
    assert result.finish_reason == "speak"
    assert result.spoken_text == "我查不到，建议您问一下服务台。"
    messages = seen[0]
    assert BLOCK_MARKER not in _all_text(messages)
    assert KNOWLEDGE_MISS_NOTE not in _context_text(messages)

    failures = [r for r in caplog.records if r.getMessage() == "knowledge lookup failed"]
    assert len(failures) == 1, [r.getMessage() for r in caplog.records]


async def test_without_lookup_context_stays_legacy_shape() -> None:
    """回归：不传 ``knowledge_lookup`` 时行为与旧版一致（无知识段、无提示）。"""
    seen: list[list[dict[str, str]]] = []

    await run_turn(
        "今天天气怎么样",
        llm=_llm([_speak("今天是晴天。")], seen),
        tool_registry=ToolRegistry(backend=FakeBackend()),
        runtime_context=_runtime(),
        trace_id="trace-kb",
    )

    messages = seen[0]
    assert BLOCK_MARKER not in _all_text(messages)
    assert KNOWLEDGE_MISS_NOTE not in _context_text(messages)
    assert messages[0]["content"] == AGENT_SYSTEM_PROMPT


# ------------------------------------------------------------ 适配层位置与兼容


def _build(**kwargs: Any) -> list[dict[str, str]]:
    return build_agent_context(
        history=kwargs.pop("history", ()),
        prefix=AGENT_SYSTEM_PROMPT,
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        **kwargs,
    )


def test_knowledge_note_sits_after_knowledge_block() -> None:
    """``knowledge_note`` 是一条独立的 system 消息，压在知识层之后。"""
    messages = _build(
        history=[{"role": "user", "content": "午餐几点开"}],
        knowledge_hits=[FakeHit()],
        knowledge_note="本轮未检索到机构知识；若问题涉及机构信息，回答「我查不到」。",
    )

    assert messages[0]["content"] == AGENT_SYSTEM_PROMPT
    assert BLOCK_MARKER in messages[-2]["content"]
    assert messages[-1] == {
        "role": "system",
        "content": "本轮未检索到机构知识；若问题涉及机构信息，回答「我查不到」。",
    }


@pytest.mark.parametrize("note", [None, "", "   \n "])
def test_blank_knowledge_note_adds_no_message(note: str | None) -> None:
    """``None`` / 空串 / 纯空白都不产出占位消息（向后兼容旧调用方）。"""
    without = _build(history=[{"role": "user", "content": "午餐几点开"}])
    with_note = _build(history=[{"role": "user", "content": "午餐几点开"}], knowledge_note=note)

    assert with_note == without
