# owner=kb-context · 迭代 10 · 天气慢查询提示与失败兜底（FR-CON-02 / CON-02）

"""天气查询三条硬要求的确定性测试（FR-CON-02，PRD:293）。

覆盖：

1. **慢查询先提示**：工具迟迟不回时，先投递「正在查询」的提示，再出结果。顺序用
   事件列表钉住，**不 sleep 3 秒** —— 把阈值注入成 0，`asyncio.wait` 的第一轮就让位；
   真实现用 3 秒，只由 `WEATHER_NOTICE_AFTER_SECONDS` 一处决定。
2. **失败回缓存并标注时间**：工具返回 `stale=True`（服务端 provider 失败后兜底出来的
   旧快照）时，播报文本必须以「这是几点查到的、可能不是最新的」开头，不许把旧数据
   说成实况。
3. **无缓存明确失败**：`transient` 失败转成固定的「查不到」话术，不编造温度/天气。
4. **没有进度缝时行为不变**：不传 `progress_sink` 也照常收口（装配层的老调用点不受影响）。

全部用假 backend + 脚本化假 LLM（`httpx.MockTransport`），不打真网、不碰数据库。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.agents import companion as companion_module
from app.agents.companion import (
    WEATHER_PENDING_NOTICE,
    WEATHER_UNAVAILABLE_SPEECH,
    RuntimeContext,
    _weather_stale_note,
    run_turn,
)
from app.agents.tool_registry import ToolRegistry, ToolResult
from app.core.config import Settings
from app.services import llm_service as llm_service_module
from app.services.llm_service import LLMService

FAKE_KEY = "test-key-not-a-real-secret"

#: 与 `mcp-servers/weather/provider.py` 的 `_snapshot` 同一形状：`observed_at` 带时区，
#: 由 `_weather_stale_note` 换算成 Asia/Shanghai 的「几点查到的」。
_SNAPSHOT: dict[str, Any] = {
    "city": "杭州",
    "temp": 28.4,
    "condition": "多云",
    "observed_at": "2026-11-10T15:00+08:00",
}


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


def _llm(script: list[str], seen: list[list[dict[str, str]]]) -> LLMService:
    """脚本化假 provider：按顺序吐回复，记下每次收到的 messages。"""
    queue = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(list(body["messages"]))
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


def _weather_ok(*, stale: bool = False) -> ToolResult:
    """MCP 工具的信封形状（`app/mcp_client` 会把整个 structuredContent 当 content）。"""
    return ToolResult(
        status="ok",
        content={
            "ok": True,
            "data": dict(_SNAPSHOT),
            "error_code": None,
            "detail": None,
            "retryable": None,
            "stale": stale,
        },
    )


class GatedBackend:
    """可挂起/可放行的工具后端：用来把「慢查询」变成确定的事件顺序。"""

    def __init__(
        self,
        result: ToolResult,
        *,
        gate: asyncio.Event | None,
        events: list[str],
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result
        self._gate = gate
        self._events = events

    async def call(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, dict(args)))
        if self._gate is not None:
            await self._gate.wait()
        self._events.append("tool")
        return self._result


class ProgressRecorder:
    """进度提示的测试替身：把每次投递记进 `notices`，同时写共享事件列表。"""

    def __init__(self, events: list[str]) -> None:
        self.notices: list[str] = []
        self._events = events

    async def __call__(self, notice: str) -> None:
        self.notices.append(notice)
        self._events.append("notice")


async def _run_turn(
    *,
    backend: GatedBackend,
    script: list[str],
    progress_sink: ProgressRecorder | None = None,
    notice_after: float = 0.0,
) -> Any:
    seen: list[list[dict[str, str]]] = []
    return await run_turn(
        "帮我看看杭州天气",
        llm=_llm(script, seen),
        tool_registry=ToolRegistry(backend=backend),
        runtime_context=RuntimeContext(
            elder_id="elder-1", thread_id="thread-1", trace_id="trace-con02"
        ),
        trace_id="trace-con02",
        progress_sink=progress_sink,
        weather_notice_after_seconds=notice_after,
    )


@pytest.fixture(autouse=True)
def _fake_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """两处真实等待（llm 重试退避、W1 路由睡眠）换成即时 no-op，测试不真等。"""

    async def _no_route_wait(_seconds: float) -> None:
        return None

    async def _no_llm_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(companion_module, "_route_decision_import_sleep", _no_route_wait)
    monkeypatch.setattr(llm_service_module, "_sleep", _no_llm_sleep)


# ------------------------------------------------------------ ① 慢查询先提示后结果


async def test_slow_weather_tool_tells_user_first_then_answers() -> None:
    """工具一直不回 → 先播「正在查询」，放行后才播结果（顺序就是验收口径）。"""
    events: list[str] = []
    gate = asyncio.Event()
    recorder = ProgressRecorder(events)
    backend = GatedBackend(_weather_ok(), gate=gate, events=events)
    task = asyncio.ensure_future(
        _run_turn(
            backend=backend,
            script=[_tool("杭州"), _speak("杭州今天多云，28 度。")],
            progress_sink=recorder,
            notice_after=0.0,
        )
    )

    # 阈值 0 时第一轮 `asyncio.wait` 必然超时；这里只让出调度，不睡眠。
    for _ in range(50):
        if recorder.notices:
            break
        await asyncio.sleep(0)

    assert recorder.notices == [WEATHER_PENDING_NOTICE]
    assert events == ["notice"]  # 结果还没出来

    gate.set()
    result = await task

    assert events == ["notice", "tool"]
    assert result.finish_reason == "speak"
    assert result.spoken_text == "杭州今天多云，28 度。"


async def test_fast_weather_tool_emits_no_pending_notice() -> None:
    """工具秒回 → 一句提示都不该播（阈值给 5 秒，慢路径根本不进）。"""
    events: list[str] = []
    recorder = ProgressRecorder(events)
    backend = GatedBackend(_weather_ok(), gate=None, events=events)

    result = await _run_turn(
        backend=backend,
        script=[_tool("杭州"), _speak("杭州今天多云，28 度。")],
        progress_sink=recorder,
        notice_after=5.0,
    )

    assert recorder.notices == []
    assert events == ["tool"]
    assert result.spoken_text == "杭州今天多云，28 度。"


async def test_slow_tool_without_progress_sink_still_answers() -> None:
    """没有进度缝（老调用点/纯文本入口）：慢查询照常收口，不抛、不投递。"""
    events: list[str] = []
    gate = asyncio.Event()
    backend = GatedBackend(_weather_ok(), gate=gate, events=events)

    async def _release() -> None:
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()

    result, _ = await asyncio.gather(
        _run_turn(
            backend=backend,
            script=[_tool("杭州"), _speak("杭州今天多云，28 度。")],
            progress_sink=None,
            notice_after=0.0,
        ),
        _release(),
    )

    assert result.spoken_text == "杭州今天多云，28 度。"


# --------------------------------------------------- ② stale 兜底必须标注「几点查到的」


async def test_stale_snapshot_note_is_pinned() -> None:
    """话术本身钉住：跨天时给「M 月 D 日 HH:MM」，并明说可能不是最新。"""
    assert (
        _weather_stale_note("2026-11-10T15:00+08:00")
        == "这是11 月 10 日 15:00 查到的天气，可能不是最新的。"
    )
    # 解析不出来时只说「之前」，不猜一个具体时间。
    assert _weather_stale_note("不是时间") == "这是之前查到的天气，可能不是最新的。"


async def test_stale_snapshot_is_prepended_to_the_answer() -> None:
    """`stale=True` → 播报文本以时间标注开头，正文仍完整保留。"""
    events: list[str] = []
    backend = GatedBackend(_weather_ok(stale=True), gate=None, events=events)

    result = await _run_turn(
        backend=backend,
        script=[_tool("杭州"), _speak("杭州温度 28 度，多云。")],
        notice_after=5.0,
    )

    note = _weather_stale_note(_SNAPSHOT["observed_at"])
    assert result.spoken_text == f"{note}杭州温度 28 度，多云。"
    assert "可能不是最新的" in result.spoken_text


# -------------------------------------------------------- ③ 无缓存：明确查不到，不编造


async def test_failed_lookup_without_cache_says_it_cannot_check() -> None:
    """`transient`（provider 挂且无兜底缓存）→ 固定「查不到」话术，不含任何读数。"""
    events: list[str] = []
    backend = GatedBackend(
        ToolResult(status="transient", content={"reason": "network"}),
        gate=None,
        events=events,
    )

    result = await _run_turn(
        backend=backend,
        script=[_tool("杭州")],  # 失败即收口，不再问模型
        notice_after=5.0,
    )

    assert result.finish_reason == "tool_transient"
    assert result.spoken_text == WEATHER_UNAVAILABLE_SPEECH
    assert "度" not in result.spoken_text
    assert "多云" not in result.spoken_text
