"""可观测层门面：OTel SDK 装配 + 业务打点助手（迭代 10 · W4 · S2）。

## 为什么还留一层门面

门面只回答两件事：**怎么开、怎么记**。span 的父子关系、采样、批处理、导出重试全部交给
`opentelemetry-sdk`——这些正是自研实现最容易做错的部分（S1 之前它是「所有 span 都是平铺
的」）。调用方（`app/agents/companion.py`、`app/mcp_client/client.py`、`app/core/_observe.py`）
只认 `agent_span()` / `tool_span()` / `get_tracer().record()` 三种写法，换实现不动业务代码。

## 数据流

```
调用方 --span()/record()--> OTel SDK span
                              ├─ SpanProcessor: TraceIdBridgeProcessor（补 app.trace_id）
                              └─ BatchSpanProcessor --> OTLP/HTTP
                                   ├─ Grafana Cloud  {OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces
                                   └─ Langfuse       {LANGFUSE_HOST}/api/public/otel/v1/traces
```

导出失败由 SDK 自己重试并丢批；本模块只在配置阶段打日志，不在热路径上做任何 I/O。

## 两个开关（D8）

- `otel_enabled` **只控 trace**：关掉时根本不建 `TracerProvider`，门面落到 `NoOp` tracer，
  零线程、零队列、零 socket（S2 的硬验收）。
- `metrics_enabled` 只控指标（S3 起与指标门面配合）。关 trace 不影响 `/metrics`。

## trace_id 桥接（D8 / §8 T1）

`app/core/trace.py` 在请求入口决定两类情况：

1. 客户端没传或传了非法值 → trace_id 取 span 的 `traceId`（32 hex），日志与 trace 一致；
2. 客户端传了合法 `X-Trace-Id` → trace_id **保留客户端原值**（响应头照样回显），同时由
   `TraceIdBridgeProcessor` 把它落到 span 属性 `app.trace_id`，日志与 trace 之间仍可互查。

## 隐私

- 异常由 SDK 记成 span event（只含异常类型名，本模块不写异常正文）；
- 输入/输出只经 `summarize()` 进属性，长度上限 `SUMMARY_LIMIT`；
- PII 的最终闸口在 `SpanProcessor` 层（P4），本层不重复实现脱敏规则。
"""

from __future__ import annotations

import base64
import contextlib
import logging
import random
import time
from collections.abc import Iterator, MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import unquote

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased
from opentelemetry.trace import NoOpTracerProvider

from app.core.config import get_settings
from app.core.context import get_thread_id, get_turn_id
from app.core.redact import redact_text

logger = logging.getLogger("app.observability")

#: service.name 的兜底值（`Settings.otel_service_name` 缺省也是这个）。
DEFAULT_SERVICE_NAME: Final = "anyu-backend"

#: 单次导出的超时（毫秒）。2s 是「比一次端到端预算还短」的量级，宁可丢也不排队。
DEFAULT_TIMEOUT_MS: Final = 2000

#: 内部 SpanProcessor 的个数（trace_id 桥接 + 三 ID 注入）。签名只关心其后的导出通道。
_INTERNAL_PROCESSOR_COUNT: Final = 2

#: SDK tracer 的 scope 名（``service.name`` 之外的 instrumentation scope）。
_SCOPE_NAME: Final = "anyu.observability"

#: Langfuse 的 OTLP 入口前缀（标尾 `/v1/traces` 由装配补齐）。
_LANGFUSE_OTLP_PATH: Final = "/api/public/otel"

#: OTLP/HTTP 的 trace 信号路径；`OTEL_EXPORTER_OTLP_ENDPOINT` 给的是 base。
_TRACES_PATH: Final = "/v1/traces"

_FLUSH_TIMEOUT_MS: Final = 3000
_SHUTDOWN_TIMEOUT_MS: Final = 5000

#: 主控 Agent 一次执行的根 span 名。PRD FR-TC-04：「每次主控 Agent 执行对应一个 Trace」，
#: 所以根 span 必须由**执行入口**建（`app/agents/companion.py` 的 ReAct Loop），
#: 而不是由 router 或 api 建——后两者一次请求里可能一个 Agent 都不跑。
AGENT_SPAN_NAME: Final = "agent.companion"

#: Tool span 的名字前缀：`tool.<工具名>`，一次 ReAct 循环里按调用顺序并列在根 span 下。
TOOL_SPAN_PREFIX: Final = "tool."

#: 主控 Agent 的循环语义版本（不是模型版本）。模型版本走 `gen_ai.request.model`，
#: 这里单独记一笔是为了「换了循环实现」与「换了模型」在 trace 上能分开看。
AGENT_LOOP_ATTRIBUTE: Final = "agent.loop"

#: 输入/输出摘要的长度上限（字符）。FR-TC-04 要「摘要」不要全文：把长者原话整段塞进
#: trace 既超长也违反脱敏口径，截断由 `summarize()` 统一做，调用方不要各写一套。
SUMMARY_LIMIT: Final = 200

#: 当前请求里「客户端传入但非 32-hex」的 trace id；由 `TraceIdBridgeProcessor` 写到
#: span 属性 `app.trace_id` 上。32-hex 的客户端 id 直接当 trace id 用，不落这条属性。
_client_trace_id: ContextVar[str | None] = ContextVar("observability_client_trace_id", default=None)

_HEX_DIGITS: Final = frozenset("0123456789abcdef")


# ======================================================================
# 一、数据模型与属性
# ======================================================================


@dataclass(slots=True)
class Span:
    """`record()` 的返回值：一次已结束 span 的**快照**（不是 SDK span 的替代品）。

    保留这个轻量类型的唯一理由：`app/core/_observe.py` 的 `emit_span()` 把它当返回值，
    测试也拿它断言。真正的生命周期仍归 SDK。
    """

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    start_ns: int = 0
    end_ns: int | None = None

    @property
    def duration_ms(self) -> float | None:
        """耗时（毫秒，保留 3 位）；span 还没结束返回 None（不是 0）。"""
        if self.end_ns is None:
            return None
        return round((self.end_ns - self.start_ns) / 1_000_000, 3)


def format_trace_id(trace_id: int) -> str:
    """OTel 的 int trace id → 32 位小写十六进制（日志/响应头用这一份）。"""
    return format(trace_id, "032x")


def format_span_id(span_id: int) -> str:
    """OTel 的 int span id → 16 位小写十六进制。"""
    return format(span_id, "016x")


def is_hex_trace_id(value: str) -> bool:
    """是否是 OTel 能直接接受的 32 位十六进制 trace id（客户端传入时用它分流）。"""
    candidate = value.strip().lower()
    return len(candidate) == 32 and all(char in _HEX_DIGITS for char in candidate)


def new_span_id() -> int:
    """随机 span id（64 bit，非 0）：客户端 trace id 透传时用来造远程父上下文。"""
    return random.getrandbits(64) or 1


#: 绝不进 trace 的原始载荷字段（D4 闸②）：要摘要就走 `*.input_summary` /
#: `*.output_summary`，原始 prompt / 工具入参 / 模型补全一律丢弃。
_PII_BLOCKED_KEYS: Final = frozenset(
    {
        "input.value",
        "output.value",
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.prompt.raw",
        "tool.arguments",
        "tool.result",
        "agent.input",
        "agent.output",
    }
)


def _attribute_value(value: Any) -> Any:
    """把业务值收窄成 OTel 属性类型；字符串一律过脱敏，绝不让 SDK 抛 ValueError。

    **脱敏在写入端做**（D4 闸②）：`on_end` 阶段 span 属性已只读改不动，与其等导出前
    再扫，不如让「带手机号/身份证/姓名的字符串」一开始就写不进去。代价是每个字符串
    属性多一次正则（属性数是个位数，可忽略）。
    """
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list | tuple):
        items = [_attribute_value(item) for item in value]
        if all(isinstance(item, bool | int | float | str) for item in items):
            return items
        return str(value)
    return str(value)


def _clean_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """去掉 `None`（SDK 不接受 None）并统一类型。"""
    if not attributes:
        return {}
    return {
        str(key): _attribute_value(value)
        for key, value in attributes.items()
        if value is not None and str(key) not in _PII_BLOCKED_KEYS
    }


class _SpanAttributes(MutableMapping[str, Any]):
    """把 `span.attributes["k"] = v` 映射到 `span.set_attribute()`。

    业务代码（`companion.py` / `mcp_client/client.py`）用的是 dict 写法，SDK span 没有
    `.attributes` 可写属性；用这层代理而不是改十几个调用点。
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def __getitem__(self, key: str) -> Any:
        return getattr(self._span, "attributes", {})[key]

    def __setitem__(self, key: str, value: Any) -> None:
        name = str(key)
        if name in _PII_BLOCKED_KEYS:  # D4 闸②：原始载荷字段直接丢弃
            return
        self._span.set_attribute(name, _attribute_value(value))

    def __delitem__(self, key: str) -> None:
        raise NotImplementedError("span 属性不支持删除")

    def __iter__(self) -> Iterator[str]:
        return iter(getattr(self._span, "attributes", {}))

    def __len__(self) -> int:
        return len(getattr(self._span, "attributes", {}))


class _SpanHandle:
    """`with` 块里拿到的东西：只暴露 `.attributes` 与 `.mark_error()`。"""

    __slots__ = ("_span", "attributes")

    def __init__(self, span: Any) -> None:
        self._span = span
        self.attributes = _SpanAttributes(span)

    def mark_error(self, error_type: str) -> None:
        """标异常：只记**类型名**，正文（常含用户数据）不进 trace。"""
        self._span.set_attribute("error", True)
        self._span.set_attribute("error.type", str(error_type))
        self._span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(error_type)))


class _SpanScope:
    """手工管理「进上下文 + 结束」：同步/异步 `with` 都成立。

    不用 `start_as_current_span()` 返回的 `_AgnosticContextManager`：那个对象只实现了
    同步 `__enter__`/`__exit__`（OTel 1.45），在 `async with` 下会 `AttributeError`。
    这里改用 `start_span()` + `use_span(end_on_exit=True)`，同步进入、同步退出——
    ContextVar 本身是任务局部的，异步场景里同样成立，且退出时一定复位。
    """

    __slots__ = ("_cm", "_handle", "_span")

    def __init__(self, span: Any) -> None:
        self._span = span
        self._cm: Any = None
        self._handle: _SpanHandle | None = None

    def __enter__(self) -> _SpanHandle:
        self._cm = otel_trace.use_span(
            self._span,
            end_on_exit=True,
            record_exception=False,
            set_status_on_exception=False,
        )
        self._handle = _SpanHandle(self._cm.__enter__())
        return self._handle

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        if exc_type is not None and self._handle is not None:
            self._handle.mark_error(exc_type.__name__)
        return self._cm.__exit__(exc_type, exc, traceback) if self._cm is not None else None

    async def __aenter__(self) -> _SpanHandle:
        self._cm = otel_trace.use_span(
            self._span,
            end_on_exit=True,
            record_exception=False,
            set_status_on_exception=False,
        )
        self._handle = _SpanHandle(self._cm.__enter__())
        return self._handle

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        if exc_type is not None and self._handle is not None:
            self._handle.mark_error(exc_type.__name__)
        return self._cm.__exit__(exc_type, exc, traceback) if self._cm is not None else None


class _NullSpanScope:
    """观测不可用时的空 span：`with` / `async with` 都成立，什么都不记、什么都不抛。"""

    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


# ======================================================================
# 二、SDK 装配
# ======================================================================


class TraceIdBridgeProcessor(SpanProcessor):
    """把客户端传入的 `X-Trace-Id` 落到 span 属性 `app.trace_id`（D8 / §8 T1）。

    只在「客户端传了值、但不是 32-hex」时命中：32-hex 的情况直接当父上下文用，traceId
    本身就是客户端值，不需要额外属性。`on_end` 什么都不做——桥接只发生一次。
    """

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        client_trace_id = _client_trace_id.get()
        if client_trace_id:
            span.set_attribute("app.trace_id", client_trace_id)

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int | None = None) -> bool:
        return True


class TurnContextProcessor(SpanProcessor):
    """把服务端的三 ID 补到**每一个** span 上（S4 / §8 T13）。

    为什么不能只在 `emit_span` 里注入：SDK 下父 span（turn/agent/graph）由
    `start_span` 建，**不经过** `app/core/_observe.py` 的封装；只在 `emit_span` 补会让
    `thread_id`/`turn_id` 只覆盖一半 span。

    已有同名属性时不覆盖（显式传参优先于 contextvar）。
    """

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        existing = getattr(span, "attributes", None)
        known = existing or {}
        thread_id = get_thread_id()
        turn_id = get_turn_id()
        if thread_id and "conversation.thread_id" not in known:
            span.set_attribute("conversation.thread_id", thread_id)
        if turn_id and "turn.id" not in known:
            span.set_attribute("turn.id", turn_id)

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int | None = None) -> bool:
        return True


def set_client_trace_id(value: str | None) -> Any:
    """请求入口标记「客户端 trace id」；返回 token 供 `reset_client_trace_id()` 复位。"""
    return _client_trace_id.set(value)


def reset_client_trace_id(token: Any) -> None:
    """复位客户端 trace id（**必须**放在请求的 finally 里，避免协程复用串号）。"""
    with contextlib.suppress(ValueError):
        _client_trace_id.reset(token)


def _flag(settings: object, name: str) -> bool:
    return bool(getattr(settings, name, False))


def _traces_url(endpoint: str) -> str:
    """`OTEL_EXPORTER_OTLP_ENDPOINT`（base）→ 具体信号的 URL；已带路径就原样用。"""
    base = (endpoint or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith(_TRACES_PATH):
        return base
    return f"{base}{_TRACES_PATH}"


def _parse_headers(raw: str) -> dict[str, str]:
    """解析 `key=value,key2=value2`；按 OTel 规范做 percent-decode（Grafana 的 `Basic%20`）。"""
    headers: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key, value = unquote(key.strip()), unquote(value.strip())
        if key:
            headers[key] = value
    return headers


def _langfuse_headers(public_key: str, secret_key: str) -> dict[str, str]:
    """Langfuse 云实例的 OTLP 端点用 Basic Auth（public:secret）。"""
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def _timeout_seconds() -> float:
    return DEFAULT_TIMEOUT_MS / 1000


def _build_processors(settings: object) -> list[SpanProcessor]:
    """按配置装配 span 处理链；**key 只从 Settings 读**，缺一个就不启用该通道。"""
    processors: list[SpanProcessor] = [TraceIdBridgeProcessor(), TurnContextProcessor()]
    endpoint = str(getattr(settings, "otel_exporter_otlp_endpoint", "") or "")
    if _flag(settings, "otel_enabled") and _traces_url(endpoint):
        headers = _parse_headers(str(getattr(settings, "otel_exporter_otlp_headers", "") or ""))
        processors.append(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=_traces_url(endpoint),
                    headers=headers or None,
                    timeout=_timeout_seconds(),
                )
            )
        )
    if _flag(settings, "langfuse_enabled"):
        host = str(getattr(settings, "langfuse_host", "") or "")
        public_key = str(getattr(settings, "langfuse_public_key", "") or "")
        secret_key = str(getattr(settings, "langfuse_secret_key", "") or "")
        if host and public_key and secret_key:
            processors.append(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=f"{host.rstrip('/')}{_LANGFUSE_OTLP_PATH}{_TRACES_PATH}",
                        headers=_langfuse_headers(public_key, secret_key),
                        timeout=_timeout_seconds(),
                    )
                )
            )
        else:
            logger.warning("langfuse_enabled but host/keys incomplete; langfuse channel stays off")
    return processors


def _resource(settings: object) -> Resource:
    attributes: dict[str, str] = {
        "service.name": str(getattr(settings, "otel_service_name", "") or DEFAULT_SERVICE_NAME)
    }
    environment = str(getattr(settings, "app_env", "") or "").strip()
    if environment:
        attributes["deployment.environment"] = environment
    return Resource.create(attributes)


def _sampler(settings: object) -> Any:
    """按 trace_id 哈希做 head 采样；`ParentBased` 保证同一 trace 的采样结果恒定。"""
    rate = float(getattr(settings, "otel_sample_rate", 1.0) or 0.0)
    if rate >= 1.0:
        root: Any = ALWAYS_ON
    elif rate <= 0.0:
        root = ALWAYS_OFF
    else:
        root = TraceIdRatioBased(rate)
    return ParentBased(root=root)


def _keys_fingerprint(settings: object) -> str:
    """key 的指纹（不落原文）：改 key 要能触发重新配置，又不能把密钥写进日志。"""
    import hashlib

    raw = f"{getattr(settings, 'langfuse_public_key', '')}:{getattr(settings, 'langfuse_secret_key', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _signature_of(settings: object, processors: list[SpanProcessor]) -> tuple[Any, ...]:
    channels = tuple(
        type(processor).__name__ for processor in processors[_INTERNAL_PROCESSOR_COUNT:]
    )
    return (
        _flag(settings, "otel_enabled"),
        str(getattr(settings, "otel_exporter_otlp_endpoint", "") or ""),
        str(getattr(settings, "otel_service_name", "") or ""),
        _flag(settings, "langfuse_enabled"),
        str(getattr(settings, "langfuse_host", "") or ""),
        _keys_fingerprint(settings),
        float(getattr(settings, "otel_sample_rate", 1.0) or 0.0),
        channels,
    )


# ======================================================================
# 三、Tracer 门面
# ======================================================================


class Tracer:
    """SDK 的薄门面。`provider=None` 表示「trace 关闭」：落到 NoOp，零开销纯 no-op。"""

    __slots__ = ("_provider", "_tracer")

    def __init__(self, provider: TracerProvider | None = None) -> None:
        self._provider = provider
        self._tracer = (
            provider.get_tracer(_SCOPE_NAME)
            if provider is not None
            else NoOpTracerProvider().get_tracer(_SCOPE_NAME)
        )

    @property
    def provider(self) -> TracerProvider | None:
        return self._provider

    @property
    def otel_tracer(self) -> Any:
        """HTTP 中间件等需要自己 `start_span` 的调用点用这一份。"""
        return self._tracer

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    def span(self, name: str, **attributes: Any) -> _SpanScope:
        """开一个进上下文的 span；`with` / `async with` 均可，退出自动结束。

        异常**不由 SDK 记正文**（`record_exception=False` / `set_status_on_exception=False`）：
        OTel 默认会把异常 `str` 写进 status description，而转写文本、手机号常混在异常消息里。
        这里只由 `_SpanScope.__exit__` 记**类型名**。
        """
        return _SpanScope(self._tracer.start_span(name, attributes=_clean_attributes(attributes)))

    def record(
        self,
        name: str,
        *,
        duration_ms: float | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        """记一个**已结束**的 span（时间窗按 `duration_ms` 回填到过去）。

        用于「只有日志、没有计时上下文」的调用点（音频域、路由决策、LLM 用量）。
        """
        millis = max(float(duration_ms or 0.0), 0.0)
        start_ns = time.time_ns() - int(millis * 1_000_000)
        span = self._tracer.start_span(name, start_time=start_ns)
        for key, value in _clean_attributes(attributes).items():
            span.set_attribute(key, value)
        span.end()
        context = span.get_span_context()
        return Span(
            name=name,
            trace_id=format_trace_id(context.trace_id) if context.is_valid else "",
            span_id=format_span_id(context.span_id) if context.is_valid else "",
            attributes=dict(attributes or {}),
            start_ns=start_ns,
            end_ns=time.time_ns(),
        )

    def flush(self, *, timeout_ms: int = _FLUSH_TIMEOUT_MS) -> bool:
        if self._provider is None:
            return True
        try:
            return bool(self._provider.force_flush(timeout_millis=timeout_ms))
        except Exception as exc:  # pragma: no cover - SDK 内部错误只降级不抛
            logger.warning("observability flush failed", extra={"error_type": type(exc).__name__})
            return False

    def shutdown(self) -> None:
        if self._provider is None:
            return
        with contextlib.suppress(Exception):
            self._provider.shutdown()


_provider: TracerProvider | None = None
_tracer: Tracer | None = None
_signature: tuple[Any, ...] | None = None


def configure_observability(settings: object | None = None) -> Tracer:
    """按配置初始化（或复用）全局 tracer；**同一份配置重复调用返回同一个对象**。

    由 `app/main.py` 的 lifespan 在 `yield` 前调用；关掉导出时这里只装一个 NoOp tracer，
    开销是「一个函数调用」——没有线程、没有队列、没有 socket。
    """
    global _provider, _signature, _tracer
    resolved = settings if settings is not None else get_settings()
    processors = _build_processors(resolved)
    signature = _signature_of(resolved, processors)
    current = _tracer
    if current is not None and signature == _signature:
        return current

    # 内部处理器（trace_id 桥接 + 三 ID 注入）总是挂上；只有真实导出通道存在时才建
    # provider——`otel_enabled=False` 且 Langfuse 也没配时 `mine` 为空，落到 NoOp，
    # 保证「关掉 = 零开销」。
    exporting = [p for p in processors if isinstance(p, BatchSpanProcessor)]
    provider: TracerProvider | None = None
    if exporting:
        provider = TracerProvider(resource=_resource(resolved), sampler=_sampler(resolved))
        for processor in processors:
            provider.add_span_processor(processor)

    old_provider = _provider
    _provider = provider
    _tracer = Tracer(provider)
    _signature = signature
    if old_provider is not None:
        with contextlib.suppress(Exception):
            old_provider.shutdown()

    if provider is not None:
        logger.info(
            "observability configured",
            extra={"channels": [type(p).__name__ for p in exporting]},
        )
    else:
        logger.info("observability configured without exporter; spans stay local (no-op)")
    return _tracer


def get_tracer() -> Tracer:
    """取全局 tracer；首次调用时按 `get_settings()` 装配。"""
    tracer = _tracer
    if tracer is not None:
        return tracer
    return configure_observability()


async def flush(*, timeout_ms: int = _FLUSH_TIMEOUT_MS) -> bool:
    """排空导出队列（生命周期收尾用）；无 tracer 时直接 True，**永不抛**。"""
    tracer = _tracer
    if tracer is None:
        return True
    return tracer.flush(timeout_ms=timeout_ms)


async def shutdown(*, timeout_ms: int = _SHUTDOWN_TIMEOUT_MS) -> None:
    """关闭全局 tracer 并复位单例（下次 `get_tracer()` 会得到干净的 tracer）。"""
    global _provider, _signature, _tracer
    del timeout_ms  # 语义保留：SDK 的 shutdown 自己控制超时
    tracer, _tracer = _tracer, None
    _provider = None
    _signature = None
    if tracer is not None:
        tracer.shutdown()


# ======================================================================
# 四、业务打点助手（FR-TC-04）
# ======================================================================


def summarize(value: object, *, limit: int = SUMMARY_LIMIT) -> str:
    """把输入/输出压成单行摘要：折叠空白 + 截断到 `limit` 字符。

    空值返回空串（**不返回 `"None"`**）：trace 里出现字面量 `None` 会让人以为模型真的
    说了 "None"。超长只截不报错——摘要是给人看的，不是审计证据，审计走 `audit_logs`。
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def _scope(name: str, attributes: dict[str, Any]) -> _SpanScope | _NullSpanScope:
    """建 span；**任何异常都退化成 `_NullSpanScope`**，绝不把埋点问题抛给业务。"""
    try:
        return get_tracer().span(name, **attributes)
    except Exception as exc:
        logger.warning(
            "observability span unavailable",
            extra={"span_name": name, "error_type": type(exc).__name__},
        )
        return _NullSpanScope()


def agent_span(
    name: str = AGENT_SPAN_NAME,
    *,
    model: str | None = None,
    thread_id: str | None = None,
    layer: str | None = None,
    elder_id: str | None = None,
    **attributes: Any,
) -> _SpanScope | _NullSpanScope:
    """主控 Agent 一次执行的**根 span**（FR-TC-04 的 Trace 边界）。

    用法（在 `app/agents/companion.py` 的 ReAct Loop 入口，`async with` 直接包住整轮）：

    ```python
    async with agent_span(model=settings.llm_model, thread_id=thread_id) as span:
        span.attributes["agent.input_summary"] = summarize(user_text)
        ...   # 路由/分类器/工具/TTS 的 span 都挂在这条 trace 下
        span.attributes["agent.output_summary"] = summarize(reply)
    ```

    属性口径（与 `app/core/_observe.py` 的点号命名一致，别自创）：

    | 属性 | 含义 |
    | --- | --- |
    | `span.type` | 固定 `agent`，导出后按类型筛 span 就靠它 |
    | `agent.name` | 根 span 名（默认 `agent.companion`） |
    | `agent.loop` | ReAct 循环语义版本（见 `AGENT_LOOP_ATTRIBUTE`） |
    | `gen_ai.request.model` | **模型版本**（OTel GenAI 约定） |
    | `conversation.thread_id` | LangGraph 线程（服务端注入，不是模型自报） |
    | `route.layer` | 决定这一轮由哪一层回答（L0/L1/L2） |
    | `elder.id` | 长者在**本项目内的 UUID**（不是姓名、不是床号） |

    异常不需要调用方处理：SDK 会把异常记成 span event 并把 status 置 ERROR。
    """
    attrs: dict[str, Any] = {"span.type": "agent", "agent.name": name}
    if model:
        attrs["gen_ai.request.model"] = model
    if thread_id:
        attrs["conversation.thread_id"] = thread_id
    if layer:
        attrs["route.layer"] = layer
    if elder_id:
        attrs["elder.id"] = elder_id
    attrs.update(attributes)
    return _scope(name, attrs)


def tool_span(
    tool_name: str,
    *,
    call_index: int | None = None,
    server: str | None = None,
    **attributes: Any,
) -> _SpanScope | _NullSpanScope:
    """一次 Tool 调用的 span（FR-TC-04：工具调用是 Span），名字为 `tool.<工具名>`。

    用法（`app/mcp_client` 的调用点，包住「发现 → 校验 → 调用 → 超时 → 审计」整段）：

    ```python
    async with tool_span("get_weather", call_index=n, server="weather") as span:
        span.attributes["tool.arguments_summary"] = summarize(arguments)
        result = await call(...)
        span.attributes["tool.result_summary"] = summarize(result)
    ```
    """
    attrs: dict[str, Any] = {"span.type": "tool", "tool.name": tool_name}
    if call_index is not None:
        attrs["tool.call_index"] = int(call_index)
    if server:
        attrs["tool.server"] = server
    attrs.update(attributes)
    return _scope(f"{TOOL_SPAN_PREFIX}{tool_name}", attrs)


def graph_span(
    name: str,
    *,
    thread_id: str | None = None,
    **attributes: Any,
) -> _SpanScope | _NullSpanScope:
    """业务子图一次执行的 span（S5）：名字 `graph.<name>`，属性带 `thread_id`。

    只包**子图入口**（LangGraph 的节点函数之前那一层），不复刻节点内部已有的 span。
    """
    attrs: dict[str, Any] = {"span.type": "graph", "graph.name": name}
    if thread_id:
        attrs["conversation.thread_id"] = thread_id
    attrs.update(attributes)
    return _scope(f"graph.{name}", attrs)


def turn_span(
    turn_id: str,
    *,
    thread_id: str | None = None,
    **attributes: Any,
) -> _SpanScope | _NullSpanScope:
    """一轮对话的根 span（S6）：`turn → route / agent / tts` 的父子链由它定。

    只在**真正有一次执行**的入口开：语音 `on_asr_final` 与文本轮 `run_text_turn`。
    开场白/主动播报不是「模型执行一轮」，不开 turn span（计划 §7）。
    """
    attrs: dict[str, Any] = {"span.type": "turn", "turn.id": turn_id}
    if thread_id:
        attrs["conversation.thread_id"] = thread_id
    attrs.update(attributes)
    return _scope("turn", attrs)


def task_span(
    task_name: str,
    *,
    trace_id: str | None = None,
    **attributes: Any,
) -> _SpanScope | _NullSpanScope:
    """一次 Celery 任务执行的 span（计划 §6-4 A′）：名字 `task.<短名>`。

    与 `agent_span` 不同，worker 侧没有「一轮对话」，一次任务就是一条最小 trace 根；
    `task.name` 记完整点分名（`app.tasks.jobs.reports.daily_report_delivery`），
    看板上按模块聚合时不必再切字符串。`trace_id` 由任务入参或 `new_trace_id()` 传入，
    与日志里的 `trace_id` 是同一个值（bridge 会写进 `app.trace_id`）。
    """
    attrs: dict[str, Any] = {"span.type": "task", "task.name": task_name}
    if trace_id:
        attrs["app.trace_id"] = trace_id
    attrs.update(attributes)
    return _scope(f"task.{task_name.rsplit('.', 1)[-1]}", attrs)


__all__ = [
    "AGENT_LOOP_ATTRIBUTE",
    "AGENT_SPAN_NAME",
    "DEFAULT_SERVICE_NAME",
    "DEFAULT_TIMEOUT_MS",
    "SUMMARY_LIMIT",
    "TOOL_SPAN_PREFIX",
    "Span",
    "TraceIdBridgeProcessor",
    "Tracer",
    "TurnContextProcessor",
    "agent_span",
    "configure_observability",
    "flush",
    "format_span_id",
    "format_trace_id",
    "get_tracer",
    "graph_span",
    "is_hex_trace_id",
    "new_span_id",
    "reset_client_trace_id",
    "set_client_trace_id",
    "shutdown",
    "summarize",
    "tool_span",
    "turn_span",
]
