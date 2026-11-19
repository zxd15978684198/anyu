"""`app/mcp_client/client.py` 的工具调用指标（迭代 5 S7 补洞 #1）。

钉住两件事：

1. **每次 `call_tool` 终态记一笔** `mcp_call_ms` 与 `mcp_calls_total{result=ok|error}`；
   transient 重试 ≤2 次仍只算一次调用（不按 attempt 补记）。
2. **标签里没有工具名**：四分类一律折成 `ok` / `error` 二值，避免高基数。

打点走 `app.core.metrics` 的公开门面（`counter` / `observe_ms`），故用 `reset_metrics()`
+ `metrics_snapshot()` 断言「真的记到了」，不 monkeypatch 打点函数本身。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import StdioServerParameters

from app.core import metrics as metrics_core
from app.mcp_client.client import McpClient

_CALL_DELAY_S = 0.003  # 保证 elapsed_ms 取整后 ≥1，指标与返回值才能精确对上


@pytest.fixture(autouse=True)
def _metrics_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每用例强制开指标并清空，结束后再清一次（进程级全局 dict，不清会漏进下个用例）。"""
    monkeypatch.setattr(metrics_core, "get_settings", lambda: SimpleNamespace(metrics_enabled=True))
    metrics_core.reset_metrics()
    yield
    metrics_core.reset_metrics()


class _FakeSdk:
    """SDK `Client` 的替身：只实现 `call_tool`，其余由 `McpClient` 的注入旁路。"""

    def __init__(self, *, raw: Any = None, exc: BaseException | None = None) -> None:
        self._raw = raw
        self._exc = exc
        self.calls = 0

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        read_timeout_seconds: float | None = None,
    ) -> Any:
        self.calls += 1
        await asyncio.sleep(_CALL_DELAY_S)
        if self._exc is not None:
            raise self._exc
        return self._raw


class _FakeRegistry:
    """`registry.allows/validate` 的替身：用于覆盖建连前就终结的两类（forbidden/param_error）。"""

    def __init__(self, *, allows: bool = True, error: str | None = None) -> None:
        self._allows = allows
        self._error = error

    def allows(self, name: str) -> bool:
        return self._allows

    def validate(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        return dict(args), self._error


class _BoomRegistry:
    """校验层抛真异常：验证收口点对「非四分类」的逃逸异常也记一笔 error。"""

    def allows(self, name: str) -> bool:
        return True

    def validate(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        raise RuntimeError("boom")


def _ok_raw(temp_c: int = 28) -> Any:
    payload = {"temp_c": temp_c}
    return SimpleNamespace(
        is_error=False,
        content=[SimpleNamespace(type="text", text=f'{{"temp_c": {temp_c}}}')],
        structured_content=payload,
    )


def _client(sdk: _FakeSdk | None = None, registry: Any | None = None) -> McpClient:
    client = McpClient(
        name="fake-weather",
        stdio=StdioServerParameters(command=sys.executable, args=["-c", "pass"]),
        timeout_seconds=1.0,
        registry=registry,
        backoff_base=0.0,
        max_transient_retries=0,  # 失败只打一格，不需要真退避
    )
    if sdk is not None:

        async def _fake_connect() -> _FakeSdk:
            return sdk

        client._ensure_connected = _fake_connect  # type: ignore[method-assign]
    return client


async def test_ok_call_records_one_latency_and_ok_counter() -> None:
    sdk = _FakeSdk(raw=_ok_raw())
    client = _client(sdk)

    result = await client.call_tool("get_weather", {"city": "H"})

    assert result.status == "ok"
    snapshot = metrics_core.metrics_snapshot()
    assert snapshot["counters"] == {"mcp_calls_total{result=ok}": 1}
    timings = snapshot["timings"]
    assert set(timings) == {"mcp_call_ms"}
    slot = timings["mcp_call_ms"]
    assert slot["count"] == 1
    assert slot["sum_ms"] > 0
    # 与返回值同源（一次调用只算一处耗时）；极快调用 elapsed_ms 会被取整成 0，
    # 那时指标退回装饰器实测量（>0 已断言），不再逐字比较。
    if result.elapsed_ms > 0:
        assert slot["sum_ms"] == pytest.approx(result.elapsed_ms)
        assert slot["max_ms"] == pytest.approx(result.elapsed_ms)


async def test_transient_failure_records_error_and_one_sample_per_call() -> None:
    """超时（transient，重试耗尽）→ error 一笔、耗时一笔，且不按 attempt 放大。"""
    sdk = _FakeSdk(exc=TimeoutError())
    client = _client(sdk)

    result = await client.call_tool("get_weather", {"city": "H"})

    assert result.status == "transient"
    assert sdk.calls == 1  # max_transient_retries=0 → 只发一次
    snapshot = metrics_core.metrics_snapshot()
    assert snapshot["counters"] == {"mcp_calls_total{result=error}": 1}
    assert snapshot["timings"]["mcp_call_ms"]["count"] == 1


async def test_pre_dispatch_rejections_also_count_as_error() -> None:
    """forbidden / param_error 在建连前终结，但同样是一次失败的调用收口。"""
    forbidden = await _client(registry=_FakeRegistry(allows=False)).call_tool("boom_tool", {})
    bad_args = await _client(registry=_FakeRegistry(error="missing_required:city")).call_tool(
        "get_weather", {}
    )

    assert forbidden.status == "forbidden"
    assert bad_args.status == "param_error"
    snapshot = metrics_core.metrics_snapshot()
    # 两次调用、两种失败原因 → 同一个 error 桶；标签里不出现工具名。
    assert snapshot["counters"] == {"mcp_calls_total{result=error}": 2}
    assert snapshot["timings"]["mcp_call_ms"]["count"] == 2


async def test_exception_escaping_call_tool_still_records_error() -> None:
    """`call_tool` 抛真异常时（registry 校验炸了这种非四分类路径），收口点仍要落 error。"""
    client = _client(registry=_BoomRegistry())

    with pytest.raises(RuntimeError, match="boom"):
        await client.call_tool("get_weather", {"city": "H"})

    snapshot = metrics_core.metrics_snapshot()
    assert snapshot["counters"] == {"mcp_calls_total{result=error}": 1}
    assert snapshot["timings"]["mcp_call_ms"]["count"] == 1
