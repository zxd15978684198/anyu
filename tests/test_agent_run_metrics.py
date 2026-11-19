"""`agent_run_ms` 直方图与 `agent_run_layer_total{layer}` 的埋点验收（S7-②）。

为什么直接测装饰器包住的函数而不是跑一遍 `run_turn`：本卡只关心「一轮的起止是否被计时、
异常是否也留样本、layer 标签有没有落到计数器上」。真跑 `run_turn` 要伪造 LLM 与工具注册表
（`tests/test_agents_companion.py` 已经覆盖那些业务路径），把两件事分开测，失败时才看得出
是埋点坏了还是对话逻辑坏了。

读法：`observe_ms` 同时写进程内 `_timings` 与 OTel 直方图，所以
- 「有没有记到」看 `metrics_snapshot()["timings"]`（含 count / sum_ms / max_ms / p50 / p95）；
- 「Prometheus 抓不抓得到」看 `render_metrics()` 文本里的 `agent_run_ms_bucket`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.agents import companion
from app.core import metrics as metrics_core
from app.core.metrics import render_metrics


@dataclass
class _Decision:
    layer: str


@dataclass
class _Result:
    route_decision: _Decision | None
    spoken_text: str = "说完了。"


@pytest.fixture(autouse=True)
def _clean_metrics() -> Any:
    """指标是进程级全局（含 OTel meter），每用例前后各清一次。"""
    metrics_core.reset_metrics()
    try:
        yield
    finally:
        metrics_core.reset_metrics()


def _timings() -> dict[str, dict[str, float]]:
    return metrics_core.metrics_snapshot()["timings"]  # type: ignore[return-value]


def _counters() -> dict[str, int]:
    return metrics_core.metrics_snapshot()["counters"]  # type: ignore[return-value]


@companion._observed_turn
async def _fake_turn(text: str = "随便说点什么", *, result: Any = None, **_: Any) -> Any:
    return result


async def test_agent_run_ms_recorded_on_success() -> None:
    await _fake_turn(result=_Result(_Decision("L2")))

    timings = _timings()
    assert companion.AGENT_RUN_MS in timings
    slot = timings[companion.AGENT_RUN_MS]
    assert slot["count"] == 1.0
    assert slot["sum_ms"] >= 0.0
    assert slot["max_ms"] >= 0.0
    # layer 落到计数器上（observe_ms 没有标签参数，见常量注释）
    assert _counters()[f"{companion.AGENT_RUN_LAYER_TOTAL}{{layer=L2}}"] == 1


async def test_agent_run_ms_recorded_when_turn_raises() -> None:
    """异常路径也要有样本：否则「抛异常的那几轮」会被 p95 整段忽略。"""

    @companion._observed_turn
    async def _boom(**_: Any) -> Any:
        raise RuntimeError("图崩了")

    with pytest.raises(RuntimeError, match="图崩了"):
        await _boom()

    slot = _timings()[companion.AGENT_RUN_MS]
    assert slot["count"] == 1.0
    # 没走完就没有 layer 可归：兜底成 unknown，不能凭空报一个 L2
    assert _counters()[f"{companion.AGENT_RUN_LAYER_TOTAL}{{layer=unknown}}"] == 1


async def test_layer_falls_back_to_unknown_without_route_decision() -> None:
    await _fake_turn(result=_Result(None))

    counters = _counters()
    assert counters[f"{companion.AGENT_RUN_LAYER_TOTAL}{{layer=unknown}}"] == 1
    assert f"{companion.AGENT_RUN_LAYER_TOTAL}{{layer=L2}}" not in counters
    assert _timings()[companion.AGENT_RUN_MS]["count"] == 1.0


async def test_repeated_turns_accumulate_count_and_percentiles() -> None:
    await _fake_turn(result=_Result(_Decision("L0")))
    await _fake_turn(result=_Result(_Decision("L0")))
    await _fake_turn(result=_Result(_Decision("L2")))

    slot = _timings()[companion.AGENT_RUN_MS]
    assert slot["count"] == 3.0
    assert slot["p50_ms"] is not None and slot["p95_ms"] is not None
    # 样本量 3 时分位仍有定义（最近邻法），不该是 None 或 NaN
    assert slot["p95_ms"] >= slot["p50_ms"] >= 0.0

    counters = _counters()
    assert counters[f"{companion.AGENT_RUN_LAYER_TOTAL}{{layer=L0}}"] == 2
    assert counters[f"{companion.AGENT_RUN_LAYER_TOTAL}{{layer=L2}}"] == 1


async def test_render_metrics_exposes_histogram_buckets() -> None:
    """/metrics 抓的是 OTel 侧产物：直方图以 `_bucket`/`_count`/`_sum` 三个碎片出现。"""
    await _fake_turn(result=_Result(_Decision("L2")))

    text = render_metrics()
    assert f"{companion.AGENT_RUN_MS}_bucket" in text
    assert f"{companion.AGENT_RUN_MS}_count" in text
    assert f"{companion.AGENT_RUN_MS}_sum" in text
    assert f"# TYPE {companion.AGENT_RUN_MS}" in text
    # layer 维度在同一份文本里以计数器出现
    assert companion.AGENT_RUN_LAYER_TOTAL in text
    assert 'layer="L2"' in text
