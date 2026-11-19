"""S2 · 可观测性门面换 OTel SDK 后的行为契约（`app/core/observability.py`）。

## 这组用例防什么

换 SDK 最怕两件事：**门面被悄悄改了口径**（调用点还在用 `span.attributes[...] = ...`）
和**关掉时仍然起线程/建连接**（AGENTS §7 的「关闭零开销」）。所以这里钉四层：

1. **no-op 层**：`Tracer()`（`provider=None`）必须完全不建 provider、不起线程，
   `span()` / `record()` / `flush()` 照常返回，不抛；
2. **门面层**：`agent_span` / `tool_span` 的父子关系与属性仍按 FR-TC-04 的口径落到 SDK span；
3. **装配层**：`_build_processors` 的通道选择、`_parse_headers` 的 `Basic%20` 解码、
   `_traces_url` 的 `/v1/traces` 追加；
4. **隐私层**：异常只记类型名——OTel 默认会把异常正文写进 status description，
   转写文本/手机号常混在异常消息里，这条必须有断言守着。

## 故意不测的

真实出网（Langfuse / Grafana）：`spikes/otel_smoke.py` 的 C1/C2 已覆盖，这里只验本地语义。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import observability
from app.core.observability import (
    AGENT_SPAN_NAME,
    TOOL_SPAN_PREFIX,
    TraceIdBridgeProcessor,
    Tracer,
    agent_span,
    configure_observability,
    format_span_id,
    format_trace_id,
    is_hex_trace_id,
    new_span_id,
    reset_client_trace_id,
    set_client_trace_id,
    tool_span,
)


@pytest.fixture(autouse=True)
def _isolate_observability() -> Iterator[None]:
    """每用例从「没有 tracer / provider」开始，结束后也丢掉单例。"""
    observability._tracer = None
    observability._provider = None
    observability._signature = None
    yield
    if observability._provider is not None:
        observability._provider.shutdown()
    observability._tracer = None
    observability._provider = None
    observability._signature = None


def _settings(**overrides: Any) -> SimpleNamespace:
    """`Settings` 的可观测性字段子集；默认全关。"""
    base: dict[str, Any] = {
        "otel_enabled": False,
        "otel_exporter_otlp_endpoint": "",
        "otel_exporter_otlp_protocol": "http/protobuf",
        "otel_exporter_otlp_headers": "",
        "otel_service_name": "anyu-backend",
        "langfuse_enabled": False,
        "langfuse_host": "https://cloud.langfuse.com",
        "langfuse_public_key": "",
        "langfuse_secret_key": "",
        "langfuse_sample_rate": 1.0,
        "otel_sample_rate": 1.0,
        "app_env": "test",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _memory_tracer() -> tuple[Tracer, InMemorySpanExporter]:
    """把进程单例换成「写内存」的 tracer；autouse fixture 负责用例后清理。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = Tracer(provider)
    observability._tracer = tracer
    observability._provider = provider
    return tracer, exporter


# ======================================================================
# 一、no-op 层：provider=None 时零开销
# ======================================================================


def test_tracer_without_provider_is_noop_and_has_no_provider() -> None:
    tracer = Tracer()
    assert tracer.enabled is False
    assert tracer.provider is None

    with agent_span(model="deepseek-chat") as span:
        assert span is not None
        span.attributes["agent.input_summary"] = "你好"  # 写属性不抛，也不落任何地方

    recorded = tracer.record("route.decide", duration_ms=12.5, attributes={"route.layer": "L0"})
    assert recorded.trace_id == ""  # NoOp 没有有效上下文，不伪造 id
    assert recorded.span_id == ""
    assert recorded.duration_ms is not None

    assert tracer.flush() is True
    tracer.shutdown()  # 不抛


async def test_module_flush_without_tracer_returns_true() -> None:
    assert await observability.flush() is True
    await observability.shutdown()


# ======================================================================
# 二、门面层：父子关系与属性
# ======================================================================


def test_agent_span_is_root_and_tool_span_nests_under_it() -> None:
    _, exporter = _memory_tracer()

    with agent_span(
        model="deepseek-chat", thread_id="thread-1", layer="L2", elder_id="elder-1"
    ) as root_handle:
        assert root_handle is not None
        with tool_span("get_weather", call_index=0, server="weather") as tool_handle:
            assert tool_handle is not None
            tool_handle.attributes["tool.arguments_summary"] = "{'city': '杭州'}"

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans[AGENT_SPAN_NAME]
    tool = spans["tool.get_weather"]
    assert root.name == AGENT_SPAN_NAME == "agent.companion"
    assert tool.name == f"{TOOL_SPAN_PREFIX}get_weather" == "tool.get_weather"

    # FR-TC-04：一次主控 Agent 执行 = 一条 Trace，工具 span 同 trace 且父指向根
    assert tool.context.trace_id == root.context.trace_id
    assert tool.parent is not None and tool.parent.span_id == root.context.span_id
    # 根 span 没有父
    assert root.parent is None or not root.parent.is_valid

    assert root.attributes["span.type"] == "agent"
    assert root.attributes["agent.name"] == AGENT_SPAN_NAME
    assert root.attributes["gen_ai.request.model"] == "deepseek-chat"
    assert root.attributes["conversation.thread_id"] == "thread-1"
    assert root.attributes["route.layer"] == "L2"
    assert root.attributes["elder.id"] == "elder-1"
    assert tool.attributes["span.type"] == "tool"
    assert tool.attributes["tool.call_index"] == 0
    assert tool.attributes["tool.server"] == "weather"
    assert tool.attributes["tool.arguments_summary"] == "{'city': '杭州'}"


def test_record_backfills_duration_and_exports_attributes() -> None:
    _, exporter = _memory_tracer()
    recorded = observability.get_tracer().record(
        "route.decide", duration_ms=12.5, attributes={"route.layer": "L0", "route.decision": "L0"}
    )

    assert recorded.name == "route.decide"
    assert len(recorded.trace_id) == 32
    assert len(recorded.span_id) == 16
    assert recorded.duration_ms is not None and recorded.duration_ms >= 0

    span = exporter.get_finished_spans()[0]
    assert span.name == "route.decide"
    assert span.attributes["route.layer"] == "L0"
    duration_ms = (span.end_time - span.start_time) / 1_000_000
    assert 8 <= duration_ms <= 200  # 回填到过去 12.5ms，允许调度抖动


def test_span_records_only_error_type_never_exception_text() -> None:
    _, exporter = _memory_tracer()

    with pytest.raises(RuntimeError), agent_span() as span:
        assert span is not None
        raise RuntimeError("boom: 13800000000")  # pragma: allowlist secret

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == "RuntimeError"  # 只有类型名
    assert "13800000000" not in str(span.attributes)  # pragma: allowlist secret
    assert "13800000000" not in str(span.status.description)  # pragma: allowlist secret


# ======================================================================
# 三、装配层：通道、header、URL
# ======================================================================


def test_configure_with_all_channels_off_builds_nothing() -> None:
    first = configure_observability(_settings())
    second = configure_observability(_settings())
    assert first is second  # 同一份配置复用同一对象
    assert first.enabled is False
    assert observability._provider is None


def test_build_processors_adds_bridge_plus_two_batch_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_exporter(**kwargs: Any) -> InMemorySpanExporter:
        calls.append(kwargs)
        return InMemorySpanExporter()

    monkeypatch.setattr(observability, "OTLPSpanExporter", _fake_exporter)
    settings = _settings(
        otel_enabled=True,
        otel_exporter_otlp_endpoint="https://otlp-gateway-prod-ap-northeast-0.grafana.net/otlp",
        otel_exporter_otlp_headers="Authorization=Basic%20abc",
        langfuse_enabled=True,
        langfuse_public_key="pk-lf-1",
        langfuse_secret_key="sk-lf-1",  # pragma: allowlist secret
    )

    processors = observability._build_processors(settings)

    assert isinstance(processors[0], TraceIdBridgeProcessor)
    assert isinstance(processors[1], observability.TurnContextProcessor)
    # 内部两个处理器之后才是导出通道（Grafana、Langfuse 各一条 Batch）
    assert [type(p).__name__ for p in processors[2:]] == [
        "BatchSpanProcessor",
        "BatchSpanProcessor",
    ]
    assert calls[0]["endpoint"].endswith("/v1/traces")
    assert calls[0]["headers"] == {"Authorization": "Basic abc"}  # %20 已解码
    assert calls[1]["endpoint"] == "https://cloud.langfuse.com/api/public/otel/v1/traces"


def test_langfuse_channel_needs_all_three_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "OTLPSpanExporter", lambda **_: InMemorySpanExporter())
    settings = _settings(langfuse_enabled=True, langfuse_public_key="pk", langfuse_secret_key="")

    processors = observability._build_processors(settings)

    assert len(processors) == 2  # 只有两个内部处理器（桥接 + 三 ID 注入），无导出通道


def test_parse_headers_handles_percent_encoded_basic_and_multiple_pairs() -> None:
    parsed = observability._parse_headers("Authorization=Basic%20dXNlcjpwYXNz, X-Scope-OrgID=42")
    assert parsed == {"Authorization": "Basic dXNlcjpwYXNz", "X-Scope-OrgID": "42"}


def test_traces_url_appends_signal_path_once() -> None:
    assert observability._traces_url("https://h/otlp") == "https://h/otlp/v1/traces"
    assert observability._traces_url("https://h/otlp/v1/traces") == "https://h/otlp/v1/traces"
    assert observability._traces_url("") == ""


def test_format_helpers_roundtrip_otel_ids() -> None:
    assert len(format_span_id(new_span_id())) == 16
    assert format_trace_id(1) == "0" * 31 + "1"
    assert is_hex_trace_id("0" * 31 + "a") is True
    assert is_hex_trace_id("abcd1234efgh5678") is False  # 非 hex：日志/响应头保原值
    assert is_hex_trace_id("G" * 32) is False


# ======================================================================
# 四、D8 trace_id 桥接：客户端原值落 `app.trace_id`
# ======================================================================


def test_bridge_processor_writes_client_trace_id_and_resets() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(TraceIdBridgeProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observability._tracer = Tracer(provider)
    observability._provider = provider

    token = set_client_trace_id("abcd1234efgh5678")
    try:
        with agent_span():
            pass
    finally:
        reset_client_trace_id(token)

    with agent_span():
        pass

    first, second = exporter.get_finished_spans()
    assert first.attributes["app.trace_id"] == "abcd1234efgh5678"
    assert "app.trace_id" not in second.attributes  # 复位后不再串号
