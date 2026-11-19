"""`app/core/llm_telemetry.py` 的三条硬性质（plan §4.4 D4、§6 S9）。

这里不测 openinference 本身（那是 `spikes/otel_smoke.py` 的 C1/C2 与生产 exporter 的事），
只锁本项目的**开关契约**：

1. `otel_enabled=False` → 返回 `False`，且不把 `openinference*` 放进 `sys.modules`；
2. 没有 `TracerProvider`（trace 前门是 NoOp）→ 返回 `False`，不崩、不装；
3. 重复调用 → 仍返回 `True`，但 `instrument()` 只被调一次。

假库用 `sys.modules` 注入，不依赖真实 `openinference` 存在（它只在 otel 开启的运行期装）。
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

from app.core import llm_telemetry, observability


@dataclass
class _Settings:
    """只带 `instrument_llm` 会读的那一个字段；用真 `Settings` 会牵扯 .env。"""

    otel_enabled: bool


@dataclass
class _Tracer:
    """`observability.Tracer` 的最小替身：本模块只读 `.provider`。"""

    provider: object | None


def _install_fake_openinference(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    """把 `openinference.instrumentation.langchain` 换成记录调用次数的假包。

    父包也要塞进 `sys.modules`：`from a.b.c import X` 会先找 `a`、`a.b`，
    缺了它们 CPython 会去真实文件系统上找，测试就不再是隔离的。
    """

    module = types.ModuleType("openinference.instrumentation.langchain")

    class LangChainInstrumentor:
        def instrument(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    module.LangChainInstrumentor = LangChainInstrumentor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openinference", types.ModuleType("openinference"))
    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation",
        types.ModuleType("openinference.instrumentation"),
    )
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.langchain", module)


def _openinference_modules() -> set[str]:
    return {name for name in sys.modules if name.split(".")[0] == "openinference"}


def test_disabled_returns_false_without_importing_openinference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _openinference_modules()
    monkeypatch.setattr(llm_telemetry, "_INSTRUMENTED", False)

    assert llm_telemetry.instrument_llm(_Settings(otel_enabled=False)) is False

    # 不是「没装成功」，而是「连 import 都没发生」：D4 的「关掉即不加载」。
    assert _openinference_modules() == before


def test_missing_provider_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_openinference(monkeypatch, calls)
    monkeypatch.setattr(llm_telemetry, "_INSTRUMENTED", False)
    monkeypatch.setattr(observability, "get_tracer", lambda: _Tracer(provider=None))

    assert llm_telemetry.instrument_llm(_Settings(otel_enabled=True)) is False
    assert calls == []
    assert llm_telemetry._INSTRUMENTED is False


def test_repeated_calls_instrument_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_openinference(monkeypatch, calls)
    provider = object()
    monkeypatch.setattr(llm_telemetry, "_INSTRUMENTED", False)
    monkeypatch.setattr(observability, "get_tracer", lambda: _Tracer(provider=provider))

    settings = _Settings(otel_enabled=True)
    assert llm_telemetry.instrument_llm(settings) is True
    assert llm_telemetry.instrument_llm(settings) is True

    assert len(calls) == 1
    assert calls[0]["tracer_provider"] is provider
