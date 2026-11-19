# owner=lead · 迭代 10 · task-22 主动主诉与对话内用药确认的前置路由

"""FR-HLT-07 / AC-34、AC-35 的前置路由测试（Lead 亲手补齐，task-22）。

验的是「进了 Agent 之后第一步做什么」，不是图本身（图的用例在
`tests/test_health_checkup_graph.py` / `tests/test_medication_graph.py`）：

- 主诉句命中 → **先调健康入口、绝不调 LLM**（不允许模型自由作答）；
- 没有服务端身份（`elder_id` 为空）→ 不路由：采集按长者切片，身份不能猜（AGENTS §4.9）；
- 入口抛异常 → 固定兜底话术，不炸这一轮、不伪造「已记录」；
- 用药确认句命中 → 调用药入口，播报逐字用服务层 `reply`；
- 「我吃了两碗饭」这类句子**不得**被认成服药确认（记错药是最不能容忍的一类错）。

LLM 走 `app.services.llm_service` + `httpx.MockTransport`（与
`tests/test_agents_companion.py` 同一套注入缝），断言的是「请求次数」——
次数为 0 才叫「没经过模型」。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agents import companion as companion_module
from app.agents.companion import (
    HEALTH_CHECKUP_DONE_SPEECH,
    HEALTH_TURN_FALLBACK_SPEECH,
    MEDICATION_NO_PENDING_SPEECH,
    run_turn,
)
from app.agents.tool_registry import ToolRegistry, ToolResult
from app.core.config import Settings
from app.services import llm_service as llm_service_module
from app.services.dialogue_service import _AGENT_INTENTS
from app.services.llm_service import LLMService

FAKE_KEY = "test-key-not-a-real-secret"
ELDER_ID = "6f1c2f8e-7a1d-4f3a-9d2e-1b3c5a7e9f01"


class FakeBackend:
    """工具后端替身：这两条前置路由不该碰工具，调用即失败。"""

    async def call(self, name: str, args: dict) -> ToolResult:  # pragma: no cover - 不该被调
        raise AssertionError(f"前置路由不应调用工具：{name} {args}")


class CountingLLM:
    """包装真 `LLMService`，只少数它被调了几次。"""

    def __init__(self, service: LLMService) -> None:
        self._service = service
        self.calls = 0

    async def chat(self, messages: list[dict[str, str]], *, trace_id: str | None = None):
        self.calls += 1
        return await self._service.chat(messages, trace_id=trace_id)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="https://llm.invalid/v1",
        llm_api_key=FAKE_KEY,
        llm_model="test-model",
        llm_timeout_seconds=5,
        llm_max_retries=0,
        llm_temperature=0.3,
    )


def _llm(speech: str = "模型自由发挥。") -> CountingLLM:
    """脚本化假 provider：永远回一句 speak，方便断言「模型有没有被调用」。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"next": "speak", "speech": speech}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CountingLLM(LLMService(_settings(), client=client))


class RecordingCall:
    """记录调用参数的入口替身。"""

    def __init__(self, outcome: Any = None, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._outcome = outcome
        self._error = error

    async def __call__(self, text: str, elder_id: str) -> Any:
        self.calls.append((text, elder_id))
        if self._error is not None:
            raise self._error
        return self._outcome


class _Prompt:
    def __init__(self, text: str) -> None:
        self.prompt = {"prompt": text}


class _MedicationOutcome:
    def __init__(self, reply: str) -> None:
        self.reply = reply


@pytest.fixture(autouse=True)
def _no_real_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """等待点全换 immediate no-op：用例不该真的睡 60 秒。"""

    async def _noop(seconds: float) -> None:
        return None

    monkeypatch.setattr(companion_module, "_route_decision_import_sleep", _noop)
    monkeypatch.setattr(llm_service_module, "_sleep", _noop)


def _registry() -> ToolRegistry:
    return ToolRegistry(backend=FakeBackend())


async def test_complaint_routes_to_health_graph_without_llm() -> None:
    health = RecordingCall(_Prompt("您头晕多久了？"))
    llm = _llm()

    result = await run_turn(
        "我头晕",
        llm=llm,  # type: ignore[arg-type]
        tool_registry=_registry(),
        elder_id=ELDER_ID,
        health_checkup=health,
    )

    assert health.calls == [("我头晕", ELDER_ID)]
    assert result.spoken_text == "您头晕多久了？"
    assert result.finish_reason == "health_checkup"
    assert llm.calls == 0, "主诉必须走图，不允许经过模型自由作答"


@pytest.mark.parametrize(
    "text",
    [
        "我心口闷得厉害",
        "胸口有点疼",
        "肚子疼了一早上",
        "头有点痛",
        "浑身不舒服",
    ],
)
async def test_non_dizziness_complaints_also_route_to_graph(text: str) -> None:
    """胸部 / 腹部 / 全身主诉同样必须进图，不许漏给模型自由发挥。

    2026-11-12 服务级实测：「我心口闷得厉害」没命中主诉正则，直接落到 L2，
    模型去调了两个 MCP 写工具、最后回一句「没登记成功」——主诉没进图就等于
    既没采集、也没工单，只有一句没着落的话。
    """
    health = RecordingCall(_Prompt("您先坐下歇着，我记一下"))
    llm = _llm()

    result = await run_turn(
        text,
        llm=llm,  # type: ignore[arg-type]
        tool_registry=_registry(),
        elder_id=ELDER_ID,
        health_checkup=health,
    )

    assert health.calls == [(text, ELDER_ID)]
    assert result.finish_reason == "health_checkup"
    assert llm.calls == 0, "主诉必须走图，不允许经过模型自由作答"


async def test_complaint_without_identity_is_not_routed() -> None:
    health = RecordingCall(_Prompt("不该出现"))
    llm = _llm("模型话术")

    result = await run_turn(
        "我头晕",
        llm=llm,  # type: ignore[arg-type]
        tool_registry=_registry(),
        health_checkup=health,
    )

    assert health.calls == []
    assert llm.calls == 1
    assert result.spoken_text == "模型话术"


async def test_health_failure_falls_back_to_fixed_speech() -> None:
    health = RecordingCall(error=RuntimeError("checkpointer down"))
    llm = _llm()

    result = await run_turn(
        "我有点眩晕",
        llm=llm,  # type: ignore[arg-type]
        tool_registry=_registry(),
        elder_id=ELDER_ID,
        health_checkup=health,
    )

    assert len(health.calls) == 1
    assert result.spoken_text == HEALTH_TURN_FALLBACK_SPEECH
    assert llm.calls == 0


async def test_finished_checkup_uses_closing_speech() -> None:
    health = RecordingCall(None)

    result = await run_turn(
        "头有点昏",
        llm=_llm(),  # type: ignore[arg-type]
        tool_registry=_registry(),
        elder_id=ELDER_ID,
        health_checkup=health,
    )

    assert result.spoken_text == HEALTH_CHECKUP_DONE_SPEECH


async def test_medication_confirmation_routes_to_same_graph() -> None:
    confirm = RecordingCall(_MedicationOutcome("好，我记下来了，护工那边也看得到。"))
    llm = _llm()

    result = await run_turn(
        "我吃过药了",
        llm=llm,  # type: ignore[arg-type]
        tool_registry=_registry(),
        elder_id=ELDER_ID,
        medication_confirm=confirm,
    )

    assert confirm.calls == [("我吃过药了", ELDER_ID)]
    assert result.spoken_text == "好，我记下来了，护工那边也看得到。"
    assert result.finish_reason == "medication_confirm"
    assert llm.calls == 0, "确认动作必须进图，不能让模型自己说「已记录」"


async def test_medication_failure_does_not_claim_recorded() -> None:
    confirm = RecordingCall(error=RuntimeError("graph down"))

    result = await run_turn(
        "我还没吃药",
        llm=_llm(),  # type: ignore[arg-type]
        tool_registry=_registry(),
        elder_id=ELDER_ID,
        medication_confirm=confirm,
    )

    assert result.spoken_text == MEDICATION_NO_PENDING_SPEECH
    assert "已经记录" not in result.spoken_text


async def test_eating_rice_is_not_medication_confirmation() -> None:
    confirm = RecordingCall(_MedicationOutcome("不该出现"))
    llm = _llm("模型话术")

    result = await run_turn(
        "我吃了两碗饭",
        llm=llm,  # type: ignore[arg-type]
        tool_registry=_registry(),
        elder_id=ELDER_ID,
        medication_confirm=confirm,
    )

    assert confirm.calls == []
    assert llm.calls == 1
    assert result.spoken_text == "模型话术"


def test_health_intent_reaches_agent_path() -> None:
    """意图白名单是 AC-34 的入口条件：漏了它，规则层判出 health 也进不了 Agent。"""

    assert "health" in _AGENT_INTENTS
