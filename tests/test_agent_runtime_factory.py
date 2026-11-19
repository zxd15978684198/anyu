"""W3 接线：`app/agents/runtime_factory.py` 的装配、降级与结果映射（迭代 10 · W3）。

口径（计划 §4-W3）
- 天气 MCP 不可达 → 工具层**仍装配成功**：MCP 侧调用回 `transient`，本地知识后端照常
  （旧口径是整体返回 `None`，已按「一个依赖挂了不连坐无关能力」改掉）；
- 没有工具层 → `build_agent_runner` 返回 `None`（不假装有 Agent）；
- runner 只做一件事：调 `run_turn` 一次，把 `CompanionTurnResult` 映射成四字段结果，
  `degraded` 由 `degrade_reason` 决定（真降级 ≠ 没触发工具）。

网络面：`McpClient.connect` / `list_tools` 全部替掉 —— 本机 7101 上可能真有 weather-mcp
在跑（实测 2026-11-09 有），用例不能依赖「恰好有一个真服务」或「恰好没有」。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents import runtime_factory
from app.agents.companion import CompanionTurnResult, MemoryRecaller
from app.agents.runtime_factory import (
    build_agent_runner,
    build_tool_registry,
    close_tool_registry,
    tool_client_of,
)
from app.agents.tool_registry import TOOL_STATUS_TRANSIENT, ToolRegistry
from app.core.config import Settings
from app.core.context import DialogueIdentity, PersonaContext, reset_trace_id, set_trace_id
from app.mcp_client.client import McpClient
from app.services.llm_service import LLMService

_TRACE_ID = "0123456789abcdef0123456789abcdef"

#: 本文件只断言「身份有没有传到 run_turn」，身份内容本身不重要；用一个合法 UUID。
_ELDER_ID = "11111111-1111-4111-8111-111111111102"


def _settings(**overrides: Any) -> Settings:
    """测试专用配置：`_env_file=None` 断开本机 .env（不读也不写真实配置）。"""
    base: dict[str, Any] = {
        "app_env": "test",
        "mcp_weather_server_url": "http://127.0.0.1:7101/mcp",
        # N21：mock-operations 在本文件里默认关掉。探活是真网络调用，本机 7102 可能真有
        # server 在跑（实测有），用例不能依赖「恰好连得上」或「恰好连不上」；需要它的
        # 用例用 `mcp_operations_server_url=...` 单独开，并自己 monkeypatch 网络面。
        "mcp_operations_server_url": "",
        "mcp_call_timeout_seconds": 1,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _llm() -> LLMService:
    """run_turn 被替身时，llm 只需是一个可传的对象（本文件不发真实模型请求）。"""
    return cast(LLMService, object())


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """替掉 `McpClient` 的网络面：记下「探活连过谁」，`list_tools` 回一个 get_weather。"""
    connected: list[str] = []

    async def _connect(self: McpClient) -> None:
        connected.append(self.name)

    async def _list_tools(self: McpClient, *, timeout_ms: int | None = None) -> list[Any]:
        return [
            SimpleNamespace(
                name="get_weather",
                description="按城市查天气",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            )
        ]

    monkeypatch.setattr(McpClient, "connect", _connect)
    monkeypatch.setattr(McpClient, "list_tools", _list_tools)
    return connected


@pytest.fixture
def turn_result() -> CompanionTurnResult:
    return CompanionTurnResult(
        spoken_text="今儿多云，出门记得加件外套。",
        route_decision=None,
        finish_reason="speak",
        degrade_reason=None,
        falsified_identity=[],
        tool_calls=[{"tool": "get_weather", "allowed": True}],
        steps=[{"step": 1, "next": "finish"}],
    )


# ---------------------------------------------------------------- 工具层装配


async def test_build_tool_registry_wires_whitelist(fake_mcp: list[str]) -> None:
    registry = await build_tool_registry(_settings())

    assert isinstance(registry, ToolRegistry)
    # `ToolRegistry` 只有私有 `_whitelist`（:149），没有公开属性/构造回读口；
    # 断言落在实际属性上，另用公开的 `is_whitelisted()` 做第二道确认。
    # N22 追加机构知识检索（本地后端），N21 追加 mock-operations 两个工具 —— 白名单是
    # 四项精确集合，不是「包含」：多放行一个工具照样红。
    assert registry._whitelist == frozenset(
        {
            "get_weather",
            "search_institution_knowledge",
            "create_work_order",
            "send_family_notification",
        }
    )
    assert registry.is_whitelisted("get_weather")
    assert registry.is_whitelisted("search_institution_knowledge")
    assert registry.is_whitelisted("create_work_order")
    assert registry.is_whitelisted("send_family_notification")
    # 白名单放行 ≠ 有人接了后端：这两个必须挂到 ops 后端上（否则调用会落到天气 client）。
    assert {"create_work_order", "send_family_notification"} <= set(registry._local_backends)

    client = tool_client_of(registry)
    assert isinstance(client, McpClient)
    assert client.timeout_seconds == 1.0  # mcp_call_timeout_seconds 透传到 client
    assert fake_mcp == ["weather"]  # 探活确实连了一次，且用 server 名 "weather"


async def test_unreachable_weather_still_wires_knowledge_tool() -> None:
    """天气不可达 → 工具层**照样装配成功**：天气回 transient，本地知识不连坐。

    这是回归用例。旧行为是 `build_tool_registry` 直接 `return None`，而
    `build_agent_runner` 见 `None` 就整体不建 —— 一个外部 HTTP 服务抖动会把
    本地机构知识检索一起拖死（两者唯一共同点是「都叫工具」）。
    """
    registry = await build_tool_registry(_settings(mcp_weather_server_url="http://127.0.0.1:9"))

    assert registry is not None, "天气不可达不该让整个工具层消失"
    assert tool_client_of(registry) is None, "占位后端没有真实 MCP 客户端可关"

    # 本地后端仍在：知识检索就是靠它，不需要天气服务在线。
    assert "search_institution_knowledge" in registry._local_backends

    # MCP 侧：真实连接失败 → transient（临时错误），不是 not_found（不能骗长者「没有」）。
    weather = await registry.call("get_weather", {"city": "杭州"})
    assert weather.result.status == TOOL_STATUS_TRANSIENT
    assert weather.result.content["reason"] == "mcp_unavailable"


async def test_close_tool_registry_is_idempotent_and_quiet(fake_mcp: list[str]) -> None:
    registry = await build_tool_registry(_settings())

    await close_tool_registry(registry)
    await close_tool_registry(registry)  # 幂等：第二次仍然是空连接
    await close_tool_registry(None)


# ---------------------------------------------------------------- Agent runner


async def test_build_agent_runner_without_tool_registry_is_none() -> None:
    runner = build_agent_runner(settings=_settings(), llm=_llm(), tool_registry=None)

    assert runner is None


async def test_runner_calls_run_turn_once_and_maps_result(
    fake_mcp: list[str],
    turn_result: CompanionTurnResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = await build_tool_registry(_settings())
    assert registry is not None
    calls: list[dict[str, Any]] = []
    llm = _llm()

    async def _fake_run_turn(text: str, **kwargs: Any) -> CompanionTurnResult:
        calls.append({"text": text, **kwargs})
        return turn_result

    monkeypatch.setattr(runtime_factory, "run_turn", _fake_run_turn)

    token = set_trace_id(_TRACE_ID)
    try:
        runner = build_agent_runner(settings=_settings(), llm=llm, tool_registry=registry)
        assert runner is not None
        result = await runner("今天天气怎么样")
    finally:
        reset_trace_id(token)

    # 确实走到 run_turn，且只走一次
    assert len(calls) == 1
    assert calls[0]["text"] == "今天天气怎么样"
    assert calls[0]["llm"] is llm
    assert calls[0]["tool_registry"] is registry
    assert calls[0]["runtime_context"] is None  # 身份只由服务端注入，这里必须是空
    assert calls[0]["memory_recall"] is None
    assert calls[0]["trace_id"] == _TRACE_ID  # 透传当前请求的 trace_id

    # 四字段映射
    assert result.text == turn_result.spoken_text
    assert result.degraded is False  # degrade_reason 为空 → 不算降级
    assert result.tool_calls == 1  # len(tool_calls)
    assert result.finish_reason == "speak"


async def test_runner_uses_recall_factory_when_identity_present(
    fake_mcp: list[str],
    turn_result: CompanionTurnResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本轮有身份时，`memory_recall` 必须是 `recall_factory(identity)` 现造的那个。

    这是「长期记忆召回按人过滤」的接缝：静态 `memory_recall` 是启动期造的、手里没有身份，
    一旦这一轮带来了 `identity` 就必须让位给按 `elder_id` 造出来的召回器 —— 否则读的是
    上一位长者的记忆。`runtime_context` 由 `identity` 派生（B3-07：审计要能定位主体与长者）。
    """
    registry = await build_tool_registry(_settings())
    assert registry is not None
    calls: list[dict[str, Any]] = []
    identity = DialogueIdentity(source="device", elder_id=_ELDER_ID)

    async def _static_recall(text: str) -> list[str]:
        return ["启动期造的那份记忆"]

    async def _per_elder_recall(text: str) -> list[str]:
        return ["这位长者自己的记忆"]

    made: list[DialogueIdentity] = []

    def _factory(who: DialogueIdentity) -> MemoryRecaller | None:
        made.append(who)
        return _per_elder_recall

    async def _fake_run_turn(text: str, **kwargs: Any) -> CompanionTurnResult:
        calls.append({"text": text, **kwargs})
        return turn_result

    monkeypatch.setattr(runtime_factory, "run_turn", _fake_run_turn)

    runner = build_agent_runner(
        settings=_settings(),
        llm=_llm(),
        tool_registry=registry,
        memory_recall=_static_recall,
        recall_factory=_factory,
    )
    assert runner is not None

    await runner("今天天气怎么样", identity=identity)

    # 工厂收到的是这一轮的身份**本身**（不重造、不拷贝）：重造一层就再也说不清是谁的记忆
    assert made == [identity]
    assert made[0] is identity
    # run_turn 收到的是工厂造出来的召回器 —— 静态值被覆盖，不是「两个都传」
    assert calls[0]["memory_recall"] is _per_elder_recall
    context = calls[0]["runtime_context"]
    assert context is not None
    assert context.elder_id == str(identity.elder_id)


async def test_runner_degrades_to_static_recall_when_factory_raises(
    fake_mcp: list[str],
    turn_result: CompanionTurnResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工厂构造期抛异常 → 只降级到静态召回，**不许**把这一轮打成整轮失败。

    记忆是增强依赖，与 `_probe_embedding`、`MemoryTurnHook` 同一口径：
    `companion._recall_memories` 已把「**执行期**」异常收敛成 `[]`，「**构造期**」
    （`recall_factory(identity)` 抛）是同一类失败的另一半。若让它冒出去，`respond` 的
    `except` 会把整轮 L2 打回 `llm.chat`，连工具能力一起丢掉 —— 爆炸半径明显错配。
    """
    registry = await build_tool_registry(_settings())
    assert registry is not None
    calls: list[dict[str, Any]] = []
    identity = DialogueIdentity(source="device", elder_id=_ELDER_ID)

    async def _static_recall(text: str) -> list[str]:
        return ["启动期造的那份记忆"]

    made: list[DialogueIdentity] = []

    def _factory(who: DialogueIdentity) -> MemoryRecaller | None:
        made.append(who)
        raise RuntimeError("embedding provider unavailable")

    async def _fake_run_turn(text: str, **kwargs: Any) -> CompanionTurnResult:
        calls.append({"text": text, **kwargs})
        return turn_result

    monkeypatch.setattr(runtime_factory, "run_turn", _fake_run_turn)

    runner = build_agent_runner(
        settings=_settings(),
        llm=_llm(),
        tool_registry=registry,
        memory_recall=_static_recall,
        recall_factory=_factory,
    )
    assert runner is not None

    result = await runner("今天天气怎么样", identity=identity)  # 不抛

    # 工厂确实被调用过（拿到的身份是对的），只是结果没被采纳
    assert made == [identity]
    # run_turn 照跑，拿到的是静态召回器 —— 不是 None，也不是工厂那半截返回
    assert len(calls) == 1
    assert calls[0]["memory_recall"] is _static_recall
    context = calls[0]["runtime_context"]
    assert context is not None
    assert context.elder_id == str(identity.elder_id)
    # 整轮结果照常映射：降级只到「本轮不按人召回」，没上升到整轮失败
    assert result.text == turn_result.spoken_text


async def test_runner_keeps_static_recall_when_identity_is_absent(
    fake_mcp: list[str],
    turn_result: CompanionTurnResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有身份时工厂**不被调用**，静态 `memory_recall` 原样生效。

    既有调用点（以及 `eval/` 里那条基线）就是这样调的：少了这一条，「无身份也不许
    凭空的召回」会被一个「反正工厂总会跑」的实现悄悄破坏。
    """
    registry = await build_tool_registry(_settings())
    assert registry is not None
    calls: list[dict[str, Any]] = []

    async def _static_recall(text: str) -> list[str]:
        return ["启动期造的那份记忆"]

    made: list[DialogueIdentity] = []

    def _factory(who: DialogueIdentity) -> MemoryRecaller | None:
        made.append(who)
        return None

    async def _fake_run_turn(text: str, **kwargs: Any) -> CompanionTurnResult:
        calls.append({"text": text, **kwargs})
        return turn_result

    monkeypatch.setattr(runtime_factory, "run_turn", _fake_run_turn)

    runner = build_agent_runner(
        settings=_settings(),
        llm=_llm(),
        tool_registry=registry,
        memory_recall=_static_recall,
        recall_factory=_factory,
    )
    assert runner is not None

    await runner("今天天气怎么样")  # 不传 identity

    assert made == []
    assert calls[0]["memory_recall"] is _static_recall


async def test_runner_forwards_history_privacy_persona_to_run_turn(
    fake_mcp: list[str],
    turn_result: CompanionTurnResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本轮上下文的三个形参**原样**转发给 `run_turn`，本模块不解释、不加工。

    `runtime_factory` 是「装配层」不是「策略层」：它一旦对 history 做裁剪、对 privacy
    做 bool 化、对 persona 做重造，就出现了第二处口径 —— 与 `dialogue_service` 各算一遍，
    两处迟早对不上（AGENTS §4.8 要堵的正是同一能力两套实现）。

    `runtime_context` 从 `identity` 派生（B3-07：审计要能定位主体与长者），绝不在这里
    构造 RuntimeContext（那会把审计/工具语义拖进一次纯记忆查询，且诱使下游从文本猜人）。
    """
    registry = await build_tool_registry(_settings())
    assert registry is not None
    calls: list[dict[str, Any]] = []

    async def _fake_run_turn(text: str, **kwargs: Any) -> CompanionTurnResult:
        calls.append({"text": text, **kwargs})
        return turn_result

    monkeypatch.setattr(runtime_factory, "run_turn", _fake_run_turn)

    history: list[dict[str, str]] = [
        {"role": "user", "content": "我膝盖疼"},
        {"role": "assistant", "content": "那您少走两步"},
    ]
    persona = PersonaContext(
        name="小满",
        personality="慢条斯理，爱用比喻，不说重话",
        tone_examples=("咱们慢慢来",),
    )

    runner = build_agent_runner(settings=_settings(), llm=_llm(), tool_registry=registry)
    assert runner is not None

    await runner(
        "今天天气怎么样",
        identity=DialogueIdentity(source="device", elder_id=_ELDER_ID),
        history=history,
        privacy="true",
        persona=persona,
    )

    assert len(calls) == 1
    # 同一个列表对象、同一个 persona 对象：装配层不许复制或重建
    assert calls[0]["history"] is history
    assert calls[0]["persona"] is persona
    # privacy 保持原值字符串，没有在途中被 bool 化
    assert calls[0]["privacy"] == "true"
    context = calls[0]["runtime_context"]
    assert context is not None
    assert context.elder_id == str(_ELDER_ID)


async def test_runner_defaults_context_kwargs_to_none(
    fake_mcp: list[str],
    turn_result: CompanionTurnResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传上下文时三个形参都是 `None` —— 既有调用点（含 `eval/` 基线）零改动。

    `None` 与「传了空列表」在 `companion` 里语义不同：`history=None` 表示「调用方没有
    上下文消息」，此时 `_build_context_messages` 走缺省分支；空列表则要求它按「读到了
    但没有内容」处理。这里钉住默认值，防止有人把默认值改成 `[]` 改变既有行为。
    """
    registry = await build_tool_registry(_settings())
    assert registry is not None
    calls: list[dict[str, Any]] = []

    async def _fake_run_turn(text: str, **kwargs: Any) -> CompanionTurnResult:
        calls.append({"text": text, **kwargs})
        return turn_result

    monkeypatch.setattr(runtime_factory, "run_turn", _fake_run_turn)

    runner = build_agent_runner(settings=_settings(), llm=_llm(), tool_registry=registry)
    assert runner is not None

    await runner("今天天气怎么样")

    assert calls[0]["history"] is None
    assert calls[0]["privacy"] is None
    assert calls[0]["persona"] is None


async def test_runner_marks_degraded_from_degrade_reason(
    fake_mcp: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = await build_tool_registry(_settings())
    assert registry is not None

    async def _fake_run_turn(text: str, **kwargs: Any) -> CompanionTurnResult:
        return CompanionTurnResult(
            spoken_text="我先陪您说说话。",
            route_decision=None,
            finish_reason="llm_degraded",
            degrade_reason="llm_timeout",
            falsified_identity=[],
            tool_calls=[],
            steps=[],
        )

    monkeypatch.setattr(runtime_factory, "run_turn", _fake_run_turn)
    runner = build_agent_runner(settings=_settings(), llm=_llm(), tool_registry=registry)
    assert runner is not None

    result = await runner("今天天气怎么样")

    assert result.degraded is True  # 有 degrade_reason → 降级
    assert result.text == "我先陪您说说话。"
    assert result.tool_calls == 0
    assert result.finish_reason == "llm_degraded"
