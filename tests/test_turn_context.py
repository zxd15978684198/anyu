"""S4 · 三 ID 贯通：日志与 span 同时带 `thread_id` / `turn_id`。

钉住四件事：

1. `turn_span()` 是轮次根 span（`span.type=turn`、`turn.id`、`conversation.thread_id`）；
2. `TurnContextProcessor` 把 contextvar 里的两个 ID 补到**所有** span 上
   （包括不经 `emit_span` 的 SDK 直开 span），显式属性优先；
3. 日志 JSON 每行带 `trace_id` / `thread_id` / `turn_id` 三个字段，缺值写 `"-"`；
4. 复位后残留为 None，不会把上一轮的 ID 带进下一轮。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import observability
from app.core.context import (
    get_thread_id,
    get_turn_id,
    reset_thread_id,
    reset_turn_id,
    set_thread_id,
    set_turn_id,
)
from app.core.logging import JsonFormatter
from app.core.observability import Tracer, TurnContextProcessor, turn_span


@pytest.fixture(autouse=True)
def _isolate_observability() -> Iterator[None]:
    observability._tracer = None
    observability._provider = None
    observability._signature = None
    yield
    if observability._provider is not None:
        observability._provider.shutdown()
    observability._tracer = None
    observability._provider = None
    observability._signature = None


def _install_memory_tracer() -> tuple[Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(TurnContextProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = Tracer(provider)
    observability._tracer = tracer
    observability._provider = provider
    return tracer, exporter


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


async def test_turn_span_carries_type_and_identity() -> None:
    _tracer, exporter = _install_memory_tracer()

    async with turn_span("turn-1", thread_id="thread-1") as span:
        span.attributes["turn.text_len"] = 6

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    finished = spans[0]
    assert finished.name == "turn"
    attrs = _attrs(finished)
    assert attrs["span.type"] == "turn"
    assert attrs["turn.id"] == "turn-1"
    assert attrs["conversation.thread_id"] == "thread-1"
    assert attrs["turn.text_len"] == 6


async def test_processor_injects_ids_into_child_spans() -> None:
    tracer, exporter = _install_memory_tracer()
    thread_token = set_thread_id("thread-9")
    turn_token = set_turn_id("turn-9")
    try:
        with tracer.span("route.decide") as child:
            child.attributes["route.layer"] = "L1"
        with tracer.span("tool.get_weather", **{"turn.id": "explicit-1"}):
            pass
    finally:
        reset_turn_id(turn_token)
        reset_thread_id(thread_token)

    by_name = {s.name: _attrs(s) for s in exporter.get_finished_spans()}
    assert by_name["route.decide"]["conversation.thread_id"] == "thread-9"
    assert by_name["route.decide"]["turn.id"] == "turn-9"
    # 显式传值优先，不被 contextvar 覆盖
    assert by_name["tool.get_weather"]["turn.id"] == "explicit-1"
    assert by_name["tool.get_weather"]["conversation.thread_id"] == "thread-9"


def test_log_line_has_three_ids() -> None:
    thread_token = set_thread_id("thread-log")
    turn_token = set_turn_id("turn-log")
    try:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
    finally:
        reset_turn_id(turn_token)
        reset_thread_id(thread_token)

    assert payload["thread_id"] == "thread-log"
    assert payload["turn_id"] == "turn-log"
    assert "trace_id" in payload


def test_ids_fall_back_to_dash_and_reset_to_none() -> None:
    assert get_thread_id() is None
    assert get_turn_id() is None

    thread_token = set_thread_id("t")
    turn_token = set_turn_id("u")
    reset_turn_id(turn_token)
    reset_thread_id(thread_token)
    assert get_thread_id() is None
    assert get_turn_id() is None

    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="bare",
        args=(),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["thread_id"] == "-"
    assert payload["turn_id"] == "-"
