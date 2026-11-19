"""trace_id 中间件：进出请求各打一行日志，贯穿整个调用链。

为什么是纯 ASGI 而不是 `BaseHTTPMiddleware`：后者在 Starlette 里以任务组包装下游应用，
对 SSE / WebRTC 信令这类长连接与流式响应有已知副作用（迭代 5 起就要用）。纯 ASGI 只多
十几行，没有这层风险。

行为约定（S2 起与 OTel SDK 对齐，见计划 D8）：

1. 请求带合法 `X-Trace-Id` 且是 32-hex 时，直接当 OTel 的 trace id 用（远程父上下文），
   日志、响应头、span traceId 三者相等；
2. 带合法但非 32-hex 的 `X-Trace-Id` 时，**保留客户端原值**做日志与响应头，同时由
   `TraceIdBridgeProcessor` 把它落到 span 属性 `app.trace_id`（日志与 trace 仍可互查）；
3. 没带或非法时，trace_id 取 span 的 traceId，保证「点开日志能跳到 trace」；
4. 响应头一定回写 `X-Trace-Id`，前端可据此上报问题；
5. 无论成功失败都 reset contextvar，避免协程复用时串号。
"""

from __future__ import annotations

import logging
import time
from typing import cast

from opentelemetry import trace as otel_trace
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    TraceFlags,
    set_span_in_context,
)
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import (
    TRACE_HEADER,
    is_valid_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
)
from app.core.metrics import record_http_request
from app.core.observability import (
    format_trace_id,
    get_tracer,
    is_hex_trace_id,
    new_span_id,
    reset_client_trace_id,
    set_client_trace_id,
)

logger = logging.getLogger("app.http")


class TraceIdMiddleware:
    """纯 ASGI 中间件：注入 trace_id + 请求 span（`http.request`）+ 访问日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming_raw = headers.get(TRACE_HEADER)
        # incoming 判空要放在 is_valid_trace_id 之前：Headers.get 返回 str | None，
        # mypy 只认显式判空收窄，否则下面的 .strip() 报 union-attr
        incoming = (
            incoming_raw.strip() if incoming_raw and is_valid_trace_id(incoming_raw) else None
        )

        # 32-hex 直接当父上下文（远程父 span）；否则起一条新 trace，客户端原值走属性桥接。
        parent_context = None
        if incoming and is_hex_trace_id(incoming):
            parent_context = set_span_in_context(
                NonRecordingSpan(
                    SpanContext(
                        trace_id=int(incoming, 16),
                        span_id=new_span_id(),
                        is_remote=True,
                        trace_flags=cast(TraceFlags, TraceFlags.SAMPLED),
                    )
                )
            )

        client_token = set_client_trace_id(
            incoming if incoming and not is_hex_trace_id(incoming) else None
        )
        tracer = get_tracer()
        span = tracer.otel_tracer.start_span(
            "http.request",
            context=parent_context,
            kind=SpanKind.SERVER,
            attributes={
                "http.request.method": str(scope.get("method", "?")),
                "url.path": str(scope.get("path", "")),
            },
        )
        if incoming and not is_hex_trace_id(incoming):
            trace_id = incoming
        else:
            context = span.get_span_context()
            trace_id = format_trace_id(context.trace_id) if context.is_valid else new_trace_id()

        token = set_trace_id(trace_id)
        scope.setdefault("state", {})["trace_id"] = trace_id
        started = time.perf_counter()
        status_code = 500

        async def send_with_trace(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[TRACE_HEADER] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        except BaseException:
            span.set_attribute("http.response.status_code", status_code)
            span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR))
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            span.set_attribute("http.response.status_code", status_code)
            logger.info(
                "request finished",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "elapsed_ms": elapsed_ms,
                    "trace_id_from_client": bool(incoming),
                },
            )
            # PRD:457 FR-TC-08 的「请求量」：路由已匹配完，这里才拿得到模板路径。
            # `/metrics` 自己不计入 —— 监控抓取 15s 一次，计进来会把业务请求量稀释成看板曲线。
            if scope.get("path") != "/metrics":
                route = scope.get("route")
                template = getattr(route, "path", None)
                record_http_request(
                    method=str(scope.get("method", "?")),
                    path=template if isinstance(template, str) and template else "unmatched",
                    status=status_code,
                    elapsed_ms=elapsed_ms,
                )
            span.end()
            reset_trace_id(token)
            reset_client_trace_id(client_token)
