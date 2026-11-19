# owner=lead · 迭代 9 · L2 攻防定向用例（红绿成对）

"""迭代 9 L2 定向用例：把 L1 攻击面表里「能攻破 / 说不清」的条目钉成可执行断言。

依据 `设计记录（攻防检验设计）` v1.1 §3 / §4.1 / §4.3。
写法是「红先于绿」：

- 每条**红用例**断言的是「修好之后应当成立」的行为，今天必然失败 →
  `@pytest.mark.xfail(strict=True)`。strict 的含义是：一旦有人修好，套件会立刻
  报错，逼着这个标记被摘掉 —— 红用例升级成普通用例，不允许静默通过。
- 每条红用例都配一条**绿用例**（同一板块的正常路径），证明防线不是靠
  「全部拒绝」实现的（设计 §4.3）。
- 断言落在副作用上（设计 §4.1-2）：不看有没有抛错，看有没有拒绝执行、
  看长者听到的是不是人话。

当前登记的红用例（修好后摘标记）：

| 编号 | 缺口 | 用例 | 状态 |
| --- | --- | --- | --- |
| B3-04 | 熔断「1 分钟内」跨轮不成立 | `test_breaker_window_survives_across_turns` | 已修，转绿 |
| B4-02 | 自由文本医疗诊断口径 | `test_output_guard_blocks_diagnosis_assertion` | 已修，转绿 |
| B4-03 | 姓名类不在脱敏规则表 | `test_log_redaction_masks_elder_honorific_names` | 已修，转绿 |

三条都有对应的绿用例证明不是"一刀切"：
`test_breaker_still_trips_inside_a_single_turn`、
`test_output_guard_allows_health_advice_without_diagnosis`、
`test_log_redaction_leaves_ordinary_text_alone`。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agents import companion as companion_module
from app.agents.companion import RuntimeContext, run_turn
from app.agents.tool_registry import ToolRegistry, ToolResult
from app.core.config import Settings
from app.core.injection import LAYER_OUTPUT, check_output
from app.core.redact import redact_text
from app.services import llm_service as llm_service_module
from app.services.llm_service import LLMService

FAKE_KEY = "test-key-not-a-real-secret"


class FakeBackend:
    """本地只读 Protocol 的测试替身：记录真实触达后端的调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, dict(args)))
        return ToolResult(status="ok", content={"text": "默认天气：晴。"})


def _settings(**overrides: Any) -> Settings:
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


def _llm(script: list[str]) -> LLMService:
    """脚本化假 provider（MockTransport，不打真网）；脚本按调用顺序消费。"""
    queue = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        text = queue.pop(0) if queue else '{"next": "speak", "speech": "默认兜底话术。"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMService(_settings(), client=client)


def _tool(city: str) -> str:
    return json.dumps(
        {"next": "tool", "tool": "get_weather", "args": {"city": city}}, ensure_ascii=False
    )


def _speak(text: str) -> str:
    return json.dumps({"next": "speak", "speech": text}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """把两处真实等待（llm 重试退避与 W1 60s×3）换成即时 no-op，并清空会话级护栏缓存。

    护栏缓存是**会话级**的（B3-04 的修法），同一个 `thread_id` 在进程内会被复用；
    每个用例开始时清空，避免上一个用例的熔断状态漏进下一个。
    """
    companion_module.reset_guardrails()

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(companion_module, "_route_decision_import_sleep", _noop)
    monkeypatch.setattr(llm_service_module, "_sleep", _noop)


async def _one_turn(
    llm: LLMService,
    registry: ToolRegistry,
    *,
    thread_id: str = "thread-l2",
    trace_id: str = "trace-l2",
) -> Any:
    """不传 `guardrail=`：这就是**生产装配**的行为（runtime_factory 也不传）。"""
    return await run_turn(
        "帮我看看杭州天气",
        llm=llm,
        tool_registry=registry,
        runtime_context=RuntimeContext(elder_id="elder-l2", thread_id=thread_id, trace_id=trace_id),
        trace_id=trace_id,
    )


# ---------------------------------------------------------------- B3-04 熔断跨轮


async def test_breaker_window_survives_across_turns() -> None:
    """同一会话先后两轮、同一工具累计第 3 次（60s 内）→ 必须熔断。

    修复前：第二轮拿到新的 `Guardrail()`，窗口是空的，第 3 次照样放行。
    修复后：`run_turn(guardrail=None)` 按 (elder_id, thread_id) 复用实例。
    """
    backend = FakeBackend()
    registry = ToolRegistry(backend=backend)
    llm = _llm([_tool("杭州"), _tool("上海"), _speak("好的。"), _tool("北京"), _speak("好的。")])

    await _one_turn(llm, registry)
    second = await _one_turn(llm, registry)

    assert second.finish_reason == "tool_breaker"
    # 副作用断言：第 3 次不得触达后端
    assert len(backend.calls) == 2


async def test_breaker_still_trips_inside_a_single_turn() -> None:
    """绿用例：**同一轮内**第 3 次同工具调用必须熔断，且不触达后端。"""
    backend = FakeBackend()
    registry = ToolRegistry(backend=backend)
    llm = _llm([_tool("杭州"), _tool("上海"), _tool("北京"), _speak("好的。")])

    result = await _one_turn(llm, registry)

    assert result.finish_reason == "tool_breaker"
    assert len(backend.calls) == 2


# ------------------------------------------------- B4-02 医疗诊断口径（已修，转绿）


def test_output_guard_blocks_diagnosis_assertion() -> None:
    """红线 4：不得给出诊断结论（AGENTS §4.4 / PRD 业务边界）。"""
    result = check_output("您这是高血压，得赶紧吃降压药。")

    assert result.blocked is True
    assert result.layer == LAYER_OUTPUT
    assert result.pattern_id == "output_diagnosis"


def test_output_guard_blocks_confirmed_diagnosis_phrasing() -> None:
    """「确诊为 X」同样属于下结论，不是「建议问医生」。"""
    assert check_output("医院确诊为糖尿病。").blocked is True
    assert check_output("您得了中风，要马上住院。").blocked is True


def test_output_guard_allows_health_advice_without_diagnosis() -> None:
    """绿用例：异常值只给「建议问医生」，必须放行 —— 证明不是一刀切拒答。"""
    result = check_output("血压 180/110 有点高，先坐下歇会儿，我建议尽快问问医生。")

    assert result.blocked is False
    assert result.pattern_id is None


# -------------------------------------------------------- B4-03 姓名脱敏（红）


def test_log_redaction_masks_elder_honorific_names() -> None:
    """日志侧：长者称呼不得明文落盘（与 `check_output` 的姓名闸同一口径）。"""
    assert "王奶奶" not in redact_text("王奶奶今天心情不错，吃了两碗饭")
    assert "张三" not in redact_text("张三的儿子来看过他")
    # 上下文里的真名也要打掉，但前缀「患者叫」必须留着，否则日志读不通
    masked = redact_text("患者叫王秀兰，今天情绪很好")
    assert "王秀兰" not in masked
    assert masked.startswith("患者叫王")


def test_log_redaction_leaves_ordinary_text_alone() -> None:
    """绿用例：普通叙述不得被改花 —— 证明姓名规则不是"见到汉字就打码"。"""
    assert redact_text("今天天气不错，吃了两碗饭") == "今天天气不错，吃了两碗饭"
    assert redact_text("他昨天来了，坐了一会儿") == "他昨天来了，坐了一会儿"
    assert redact_text("血压 180/110 偏高") == "血压 180/110 偏高"
