"""S5 观测 · 两个业务子图的 `graph.<name>` span（用药确认 / 健康采集）。

这一组用例只钉三件事：

1. **span 真的开出来、属性对**：每个节点外包的那一层是 `graph.<name>`，带
   `span.type=graph`、`graph.name=<子图名>` 与服务端注入的 `conversation.thread_id`
   （AGENTS §4.9：线程键不由模型自报，只能从状态里取）；
2. **HITL 暂停不是失败**：`interrupt()` 抛出的 `GraphInterrupt` 是「等长者回答 / 等
   复核裁决」的暂停信号，不得把 span 标成 ERROR —— 否则 trace 上每次采集与每次用药
   触达看起来都挂了，告警会立刻变成噪音；
3. **没有 exporter 时整条链路照跑**：埋点不能成为业务的前置条件（otel 关掉就是
   `NoOp`，不抛、不起线程）。

不测外发：OTLP 的真实导出由 `tests/test_observability.py` 与 `spikes/otel_smoke.py`
覆盖，这里用 `InMemorySpanExporter` 把「span 开出来且属性对」钉在本地。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import observability
from app.core.observability import Tracer
from app.graphs import health_checkup_graph as health_module
from app.graphs import medication_confirmation_graph as medication_module

ELDER = uuid.UUID("11111111-1111-1111-1111-111111111111")
PLAN = uuid.UUID("22222222-2222-2222-2222-222222222222")
DAY = datetime.date(2026, 11, 13)
START = datetime.datetime(2026, 11, 13, 8, 0, tzinfo=datetime.UTC)

HEALTH_SPAN = "graph.health_checkup"
MEDICATION_SPAN = "graph.medication_confirmation"


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
    """`Settings` 的可观测性字段子集；默认全关 = 「没起 Langfuse 容器」的那个现场。"""
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


def _install_memory_tracer() -> InMemorySpanExporter:
    """把进程单例换成「写内存」的 tracer（autouse fixture 负责用例后清理）。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observability._tracer = Tracer(provider)
    observability._provider = provider
    return exporter


async def _deliver(event: Any, text: str) -> Any:
    """假投递：只回报「卡片已渲染」，不碰 WebRTC / WS。"""
    _ = (event, text)
    return medication_module.DeliveryResult(receipt="card_rendered", tts_result="ok")


def _health_config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


async def _run_health(graph: Any, thread_id: str, replies: list[object]) -> Any:
    """跑一轮采集：按给定回答逐条 resume，返回最后一次结果。"""
    state = health_module.initial_state(ELDER, record_date=DAY, thread_id=thread_id)
    config = _health_config(thread_id)
    result = await graph.ainvoke(state, config)
    for reply in replies:
        prompt = health_module.pending_prompt(result)
        if prompt is None or prompt.get("type") != "question":
            break
        result = await graph.ainvoke(Command(resume=reply), config)
    return result


# ======================================================================
# 一、健康采集子图
# ======================================================================


async def test_health_ask_node_opens_graph_span_with_server_thread_id() -> None:
    """`ask` 节点开出一条 `graph.health_checkup`，线程键取状态里的服务端值。"""
    exporter = _install_memory_tracer()
    graph = health_module.build_health_graph(MemorySaver())
    thread_id = health_module.health_thread_id(ELDER, DAY)

    await _run_health(graph, thread_id, [])

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == [HEALTH_SPAN]
    span = spans[0]
    assert span.attributes["span.type"] == "graph"
    assert span.attributes["graph.name"] == "health_checkup"
    assert span.attributes["conversation.thread_id"] == thread_id
    assert span.end_time is not None and span.end_time >= span.start_time
    # `interrupt()` 只是暂停：第一问的 span 不是错误
    assert span.status.status_code.name != "ERROR"
    assert "error" not in span.attributes


async def test_health_green_run_spans_every_node_without_error() -> None:
    """五项全绿一轮 = 5 次 `ask` + `grade` + `finish`，7 条 span 全部成功收口。"""
    exporter = _install_memory_tracer()
    graph = health_module.build_health_graph(MemorySaver())
    thread_id = health_module.health_thread_id(ELDER, DAY)

    result = await _run_health(graph, thread_id, ["128", "78", "72", "7", 4])

    assert result["grade"] == "green"
    assert result["finished"] is True
    spans = exporter.get_finished_spans()
    # 5 个 `ask` + `grade` + `finish` 至少 7 条；`interrupt()` 之后 LangGraph 会从节点
    # 开头**重跑**被打断的那个节点，所以每条等待续跑都会多出一条同名 span（当前实现
    # 一轮全绿共 12 条）。这里钉「至少每个节点一条、且全部收在同一线程」，
    # 不去锁死重跑次数——那是 LangGraph 的实现细节，不是本仓的契约。
    assert len(spans) >= 7
    assert {span.name for span in spans} == {HEALTH_SPAN}
    for span in spans:
        assert span.status.status_code.name != "ERROR"
        assert span.attributes["conversation.thread_id"] == thread_id
        assert span.attributes["span.type"] == "graph"


# ======================================================================
# 二、用药确认子图
# ======================================================================


async def test_medication_run_opens_graph_span_per_node() -> None:
    """`remind → wait_confirm(interrupt) → terminal` 三步各一条同一线程的 graph span。"""
    exporter = _install_memory_tracer()
    graph = await medication_module.build_medication_graph(
        checkpointer=MemorySaver(),
        deliver=_deliver,
        session_factory=None,  # 纯状态机模式：不写业务表
    )
    thread_id = medication_module.medication_thread_id(
        elder_id=ELDER, plan_id=PLAN, scheduled_at=START
    )
    state = medication_module.initial_state(
        thread_id=thread_id,
        elder_id=ELDER,
        reminder_id=uuid.uuid4(),
        plan_id=PLAN,
        scheduled_at=START,
        drug_snapshot={"label": "厄贝沙坦 150mg"},
    )
    config = medication_module.state_config(thread_id)

    started = await graph.ainvoke(state, config)
    prompt = medication_module.pending_interrupt(started)
    assert prompt is not None and prompt["type"] == "medication_confirm"

    final = await graph.ainvoke(
        Command(resume={"decision": "confirmed", "channel": "button", "response": "吃了"}),
        config,
    )
    assert final["terminal_state"] == "taken_reported"
    assert final["finished"] is True

    spans = exporter.get_finished_spans()
    # `remind` / `wait_confirm` / `terminal` 各一条，至少 3 条；`wait_confirm` 在
    # resume 时被 LangGraph 从头重跑，故当前实现是 4 条。count 不锁死，防的是
    # 「埋点漏节点」，不是「runtime 重跑几次」。
    assert len(spans) >= 3
    assert {span.name for span in spans} == {MEDICATION_SPAN}
    for span in spans:
        assert span.attributes["span.type"] == "graph"
        assert span.attributes["graph.name"] == "medication_confirmation"
        assert span.attributes["conversation.thread_id"] == thread_id
        # wait_confirm 的 span 就是被 interrupt 中断的那条：不能标成 ERROR
        assert span.status.status_code.name != "ERROR"
        assert "error" not in span.attributes


# ======================================================================
# 三、暂停 vs 失败：`_node_span` 的错误口径
# ======================================================================


@pytest.mark.parametrize(
    "open_span",
    [health_module._node_span, medication_module._node_span],
    ids=["health", "medication"],
)
def test_interrupt_is_not_marked_as_error(open_span: Any) -> None:
    """`GraphInterrupt` 原样抛出（交给 LangGraph 暂停），但 span 按正常结束收口。"""
    exporter = _install_memory_tracer()

    with pytest.raises(GraphInterrupt), open_span("health:demo"):
        raise GraphInterrupt(())

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code.name != "ERROR"
    assert "error" not in span.attributes
    assert "error.type" not in span.attributes
    assert span.attributes["conversation.thread_id"] == "health:demo"


@pytest.mark.parametrize(
    "open_span",
    [health_module._node_span, medication_module._node_span],
    ids=["health", "medication"],
)
def test_real_exception_still_marks_span_error(open_span: Any) -> None:
    """真异常不能因为上面那条豁免而被吞掉错误语义。"""
    exporter = _install_memory_tracer()

    with pytest.raises(RuntimeError), open_span(None):
        raise RuntimeError("boom")

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["error"] is True
    assert span.attributes["error.type"] == "RuntimeError"
    # 没给线程键就不该出现这个属性（不填占位值）
    assert "conversation.thread_id" not in span.attributes


# ======================================================================
# 四、没有 exporter：NoOp 退化，不影响业务
# ======================================================================


async def test_graph_runs_without_exporter_and_never_raises() -> None:
    """otel 全关（没有 provider、没有 exporter）时子图照跑，埋点静默退化。"""
    tracer = observability.configure_observability(_settings())
    assert tracer.enabled is False

    graph = health_module.build_health_graph(MemorySaver())
    thread_id = health_module.health_thread_id(ELDER, DAY)
    result = await _run_health(graph, thread_id, ["128", "78", "72", "7", 4])

    assert result["finished"] is True
    assert getattr(observability, "_provider", None) is None
