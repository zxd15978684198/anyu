"""PII 金丝雀（S10 · D4 闸②）：看不见的泄漏比看得见的更危险。

判定口径只有一条：**任何 span 属性里都不许出现原文**。脱敏发生在写入端
（`app/core/observability.py::_attribute_value`），所以这里断言的是「写不进去」，
不是「导出时被滤掉」——导出端过滤要求属性可写，而 `on_end` 阶段 span 属性已只读。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import observability
from app.core.observability import (
    _PII_BLOCKED_KEYS,
    Tracer,
    agent_span,
    summarize,
    tool_span,
)

PHONE = "13800138000"
ID_CARD = "110105194912310021"
NAME = "王奶奶"


@pytest.fixture()
def memory_exporter() -> Iterator[InMemorySpanExporter]:
    """把进程单例换成写内存的 tracer，用例结束复位（与 agent spans 测试同款）。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous_tracer = observability._tracer
    previous_provider = observability._provider
    observability._tracer = Tracer(provider)
    observability._provider = provider
    try:
        yield exporter
    finally:
        observability._tracer = previous_tracer
        observability._provider = previous_provider


def _attribute_text(exporter: InMemorySpanExporter) -> str:
    lines: list[str] = []
    for span in exporter.get_finished_spans():
        for key, value in (span.attributes or {}).items():
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def test_phone_number_never_reaches_the_span(memory_exporter: InMemorySpanExporter) -> None:
    with agent_span(thread_id="thread-1") as handle:
        assert handle is not None
        handle.attributes["turn.input_summary"] = f"长者说电话是 {PHONE}"

    text = _attribute_text(memory_exporter)
    assert f"turn.input_summary=长者说电话是 {PHONE}" not in text
    assert PHONE not in text
    assert "****" in text  # 被掩码而不是整段丢弃：排查仍要看得见「这里原来是手机号」


def test_id_card_never_reaches_the_span(memory_exporter: InMemorySpanExporter) -> None:
    with tool_span("get_weather", call_index=0, server="weather") as handle:
        assert handle is not None
        handle.attributes["tool.result_summary"] = f"身份证 {ID_CARD} 已核对"

    text = _attribute_text(memory_exporter)
    assert ID_CARD not in text
    assert "****" in text


def test_elder_name_is_masked(memory_exporter: InMemorySpanExporter) -> None:
    with agent_span(thread_id="thread-1") as handle:
        assert handle is not None
        handle.attributes["turn.output_summary"] = f"{NAME}，该吃药了"

    text = _attribute_text(memory_exporter)
    assert NAME not in text  # 只留姓氏，名字打星


def test_blocked_payload_keys_are_dropped_on_write(
    memory_exporter: InMemorySpanExporter,
) -> None:
    with agent_span(thread_id="thread-1") as handle:
        assert handle is not None
        for key in _PII_BLOCKED_KEYS:
            handle.attributes[key] = "原始载荷不该进 trace"

    recorded = memory_exporter.get_finished_spans()[0]
    for key in _PII_BLOCKED_KEYS:
        assert key not in (recorded.attributes or {})


def test_blocked_payload_keys_are_dropped_at_span_creation(
    memory_exporter: InMemorySpanExporter,
) -> None:
    with agent_span(**{"input.value": "raw prompt", "turn.input_summary": "ok"}):
        pass

    recorded = memory_exporter.get_finished_spans()[0]
    assert "input.value" not in (recorded.attributes or {})
    assert recorded.attributes["turn.input_summary"] == "ok"


def test_summary_of_dirty_text_is_sanitized(memory_exporter: InMemorySpanExporter) -> None:
    with agent_span(thread_id="thread-1") as handle:
        assert handle is not None
        handle.attributes["turn.input_summary"] = summarize(f"打 {PHONE}，{NAME}家")

    text = _attribute_text(memory_exporter)
    assert PHONE not in text
    assert NAME not in text
    assert len(text) < 400  # summarize 的 200 字上限 + 属性名


def test_no_raw_pii_in_a_full_turn_like_span_tree(
    memory_exporter: InMemorySpanExporter,
) -> None:
    """一轮内的根 span + 工具子 span 全部属性拼起来也不许有原文。"""
    with agent_span(model="deepseek-chat", thread_id="thread-1", elder_id="elder-1") as handle:
        assert handle is not None
        handle.attributes["turn.input_summary"] = summarize(f"{NAME} 说 {PHONE}")
        with tool_span("create_work_order", call_index=1, server="mock-operations") as child:
            assert child is not None
            child.attributes["tool.arguments_summary"] = summarize({"note": f"联系 {PHONE}"})

    text = _attribute_text(memory_exporter)
    for raw in (PHONE, ID_CARD, NAME):
        assert raw not in text
    assert len(memory_exporter.get_finished_spans()) == 2
