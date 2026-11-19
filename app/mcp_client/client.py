"""MCP 客户端：stdio / http(SSE+streamable) 双传输，统一 `call_tool` 四分类（迭代 6 W4-half）。

口径出处
- 协议路：**官方 mcp SDK**（pyproject 已声明 `mcp>=1.1`，实测 .venv 装 2.3.0）。用 2.3.0 的
  高层 `mcp.Client`：stdio（`StdioServerParameters`）、http（URL 字符串 → streamable-http、
  `sse_client(url)` CM → SSE）三种 transport 一个封装搞定；AGENTS §4.8 禁止第二套同类协议
  实现，手写 JSON-RPC 会成为与 SDK/天气 server 对不上的第二形态 —— 故不做。
- 失败四分类（AGENTS §8，§5 高风险验收点）：参数错→校验拦截并回传模型重试；目标不存在→
  如实告知；临时错误→退避重试 ≤2 次仍败则降级；越权→拒绝（不建连接）。
  `call_tool` **永不抛这四类**，一律返回 `ToolResult.status`，由调用方（companion/闸门层）
  决定预算、话术与审计；`McpToolError` 四子类保留为内部分类载体与显式 API。
- 超时：**交给 SDK `read_timeout_seconds`**（每个请求一个窗口，anyio scope 在请求帧内部
  自管、自进自出）。给协议调用外层再套 `anyio.fail_after` 会让 SDK 的内部 TaskGroup 在
  scope 退出后才被拆（async CM 进出一个外的 cancel scope，违反 anyio 记账规则，实测
  报「Attempted to exit a cancel scope that isn't the current tasks's current cancel
  scope」并污染后续请求）——因此 connect/close/协议调用**都不再套外层窗口**；SDK 超时
  异常（TimeoutError / httpx2.ReadTimeout 等）统一归类 transient。
- transient 分类：SDK 超时异常（TimeoutError / httpx2.ReadTimeout）、MCPError 非
  INVALID_PARAMS/-32601/-32020 的其它 code、网络层异常（httpx2/OSError/流断/子进程炸等
  `except Exception` 兜底，绝不当永久错）。超时→连接视为已污染（流里可能挂着半发请求）→
  下个 attempt 前断开重建。
- 越界：白名单校验委托 `registry.allows()`（缺省注册台=`get_weather` 唯一真实外部接口，
  齐平 W3，见 registry.py）；有 registry 时 ensure 白名单 fail-closed 且**在建连之前**拦截。
- 身份/审计：`trace_id` 由调用方显式传入（禁读环境、不散读 os.environ）；audit_logs 落库
  归 W3/W6 的 tool_registry 与 services 层，本模块只打结构化日志。
- 身份注入：`_ELDER_SCOPED_TOOLS` 里的工具，入参 `elder_id` 在**派发处**统一丢弃并
  换成 `runtime_context` 的身份（AGENTS §4.9「客户端与模型传入的值一律丢弃」）；
  取不到身份时不补值，交给 schema 校验变成 param_error。
- 身份**两道路径**（FR-TC-03）：除 body 覆盖外，streamable-http 还会把同一个 elder_id
  写进 header `X-Anyu-Elder`（连同 `X-Trace-Id`）。服务端据此复核 body 有没有被
  伪造 —— 两者不一致即 `forbidden/elder_mismatch` 并写审计。直连服务端时第二道单独成立。
- 业务错误：服务器按 MCP 规范在结果里带 `isError=true` 属于**工具自身契约内失败**（如
  天气 key 缺失），协议层面调用成功 —— 状态保持 `ok`、`is_error=True` 透传，不计入四分类
  （AGENTS 四分类只覆盖协议/权限/参数/网络层）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any, Literal

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # type: ignore[attr-defined]
from mcp.shared.exceptions import MCPError
from mcp_types import HEADER_MISMATCH, INVALID_PARAMS, METHOD_NOT_FOUND

from app.core.config import get_settings
from app.core.metrics import counter, observe_ms
from app.core.observability import summarize, tool_span
from app.core.runtime_context import get_identity

_LOG = logging.getLogger(__name__)

#: 身份与 trace 的传输层头名（与 `mcp-servers/mock_operations/ops.py` 同名契约）。
#: 身份所以走 header 而不是只走 body：body 是模型可写的入参，header 由本模块在派发处
#: 写入，服务端因此能复核「body 里的 elder_id 是不是被伪造过」（FR-TC-03 第二道）。
ELDER_HEADER: str = "X-Anyu-Elder"
TRACE_HEADER: str = "X-Trace-Id"

#: 需要服务端身份注入的工具（AGENTS §4.9：`elder_id`/`thread_id` 只能从服务端
#: runtime_context 注入，客户端与模型传入的值一律丢弃）。放在 `mcp_client` 的调用
#: 派发处、而不是各 server 的 `ops.py` 里：入口只有一个，漏一个工具就漏一条越权路径。
_ELDER_SCOPED_TOOLS: frozenset[str] = frozenset({"create_work_order", "send_family_notification"})

#: 四分类状态闭集（AGENTS §8）。新增状态属契约变更，需同步 tests/test_mcp_client.py。
ToolStatus = Literal["ok", "param_error", "not_found", "transient", "forbidden"]

#: 对外安全话术（给 companion/模型的通用口径，不含内部栈、不含第三方 URL/密钥细节）。
_SAFE_MESSAGES: dict[str, str] = {
    "param_error": "参数不合规：请按工具 schema 修正后重试。",
    "not_found": "该工具不存在或未注册，请如实告知，不要编造结果。",
    "transient": "服务暂时不可用（超时或网络故障），已按重试上限降级。",
    "forbidden": "该工具不在当前会话的许可清单中，调用已拒绝并应记审计。",
}


class McpToolError(Exception):
    """四分类基类：`status` 告诉上层怎么处置，`message` 只含安全话术（不回显栈/URL）。"""

    status: ToolStatus

    def __init__(self, status: ToolStatus, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class ToolParamError(McpToolError):
    """入参不符 schema（AGENTS §8：回传模型重试并计入预算）。"""

    status = "param_error"

    def __init__(self, detail: str = "") -> None:
        text = _SAFE_MESSAGES["param_error"]
        if detail:
            text = f"{text}（{detail}）"
        super().__init__("param_error", text)
        self.detail = detail


class ToolNotFound(McpToolError):
    """目标工具不存在（AGENTS §8：如实告知，不编造）。"""

    status = "not_found"

    def __init__(self) -> None:
        super().__init__("not_found", _SAFE_MESSAGES["not_found"])


class ToolTransient(McpToolError):
    """临时错误：超时 / 5xx / 连接错；内建退避重试 ≤2 次后降级（AGENTS §8）。"""

    status = "transient"

    def __init__(self, reason: str = "unknown") -> None:
        super().__init__("transient", _SAFE_MESSAGES["transient"])
        self.reason = reason


class ToolForbidden(McpToolError):
    """registry 判白名单外或无权限（AGENTS §8：拒绝 + 审计 + trace_id）。"""

    status = "forbidden"

    def __init__(self) -> None:
        super().__init__("forbidden", _SAFE_MESSAGES["forbidden"])


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具调用的终态。`status="ok"` 且服务端 `isError=true` 时 `is_error=True` 透传。"""

    status: ToolStatus
    name: str
    content: dict[str, Any] | None = (
        None  # 结构化返回：structuredContent 优先，纯文本退化为 {"text": ...}
    )
    text: str | None = None
    message: str = ""
    is_error: bool = False
    attempts: int = 1
    elapsed_ms: int = 0
    trace_id: str | None = None


# ---------------------------------------------------------------- FR-TC-04 埋点


def _record_tool_span(span: Any, tool_args: Any, result: Any) -> None:
    """把入参摘要与四分类结果挂到 tool span 上；**写失败只 warning**，不影响调用结果。"""
    if span is None:  # 观测不可用（无 tracer / 未配置）→ 空 scope 返回 None
        return
    try:
        span.attributes["tool.arguments_summary"] = summarize(tool_args)
        status = str(getattr(result, "status", "") or "")
        if status and status != "ok":
            # 四分类闭集（AGENTS §8）：param_error / not_found / transient / forbidden。
            # 成功不写这个键——trace 上「有 error_class」即失败，不用二次比对取值。
            span.attributes["tool.error_class"] = status
        span.attributes["tool.result_summary"] = summarize(getattr(result, "message", ""))
    except Exception:  # 埋点坏了不许影响工具返回
        _LOG.warning("tool span attributes failed", exc_info=True)


def _record_tool_metrics(result: ToolResult | None, elapsed_ms: float) -> None:
    """一次 `call_tool` 终态记两笔指标（迭代 5 S7 补洞 #1）：耗时 + 成功/失败。

    口径
    - **每次调用一笔，不按 attempt 补记**：transient 退避重试 ≤2 次仍属同一次工具调用，
      按 attempt 记会把一次调用放大成 3 个样本，成功率与 P95 都会失真。
    - 标签只放 `result=ok|error`，**不放工具名**：工具名随注册表增长，属高基数标签；
      「哪个工具慢」由 tool span（`_record_tool_span`）回答，指标只负责聚合速率与分位。
    - 四分类 → 二值：`ok` 记 ok；`param_error` / `not_found` / `transient` / `forbidden`
      一律 error（含 registry 在建连前就拒绝的两类，它们同样是本次调用的失败收口）。
    - `elapsed_ms` 与 `ToolResult.elapsed_ms` 同源，避免同一笔耗时在两处各算一次而悄悄错开。
    - 观测自身坏了不许影响工具返回：`counter` / `observe_ms` 已各自兜底，这里再兜一层。
    """
    outcome = "ok" if getattr(result, "status", "") == "ok" else "error"
    try:
        counter("mcp_calls_total", result=outcome)
        observe_ms("mcp_call_ms", elapsed_ms)
    except Exception:  # 埋点坏了不许影响工具返回（与 `_record_tool_span` 同口径）
        _LOG.warning("tool metrics failed", exc_info=True)


def _observed_tool_call(
    fn: Callable[..., Awaitable[ToolResult]],
) -> Callable[..., Awaitable[ToolResult]]:
    """给每次 `McpClient.call_tool` 开一个 `tool.<工具名>` span（FR-TC-04：工具调用是 Span）。

    同时是本模块**唯一的调用收口点**：`call_tool` 体里有多处 `return`（forbidden /
    param_error / ok / param_error-协议 / transient），指标若散在那些 return 前面，
    新加一个分支就会漏记一笔；包在这里则是「入口起、出口止」，异常/取消也从同一个
    except 汇合，失败样本不会掉。包裹是**纯观察**：不改控制流、不改异常语义
    （块内异常经 `_SpanScope.__aexit__` 原样上抛，它返回 None，不吞异常）。

    为什么用装饰器而不是在 `call_tool` 体里写 `async with`：函数体 200 余行，整体缩进
    会把 diff 变成「整段重排」、盖住真实改动。

    观测自身永远不抛：`tool_span()` 走 `_scope()` 兜底，取不到 tracer 时返回空 scope。
    `call_index` 由调用方（companion 的 ReAct 循环）决定，这里不知道本轮第几次，
    故不传——补一个假的序号比不记更糟。
    """

    @wraps(fn)
    async def observed(self: McpClient, name: str, *args: Any, **kwargs: Any) -> ToolResult:
        started = time.perf_counter()
        async with tool_span(name, server=getattr(self, "name", None)) as span:
            try:
                result = await fn(self, name, *args, **kwargs)
            except BaseException:
                # 真异常/取消（四分类不会走到这里，`call_tool` 一律返回结果）：
                # 仍要落一笔 error，否则失败样本只存在于日志里。
                _record_tool_metrics(None, (time.perf_counter() - started) * 1000.0)
                raise
            elapsed_ms = float(result.elapsed_ms) or (time.perf_counter() - started) * 1000.0
            _record_tool_metrics(result, elapsed_ms)
            _record_tool_span(span, args[0] if args else kwargs.get("args"), result)
        return result

    return observed


class McpClient:
    """一个 MCP server 的接入客户端（stdio/http 二选一），负责协议与四分类。

    生命周期推荐显式 `await client.connect()` ... `await client.close()`；
    忘记 connect 时 `call_tool`/`list_tools` 会惰性连。此对象不支持并发调用
    （companion 主循环本就串行发工具调用；并发语义待 迭代 9 接线再补锁）。
    """

    def __init__(
        self,
        *,
        name: str,
        stdio: StdioServerParameters | None = None,
        url: str | None = None,
        sse: str | None = None,
        timeout_seconds: float | None = None,
        registry: Any | None = None,
        backoff_base: float = 0.1,
        max_transient_retries: int = 2,
        connect_timeout_seconds: float = 5.0,
    ) -> None:
        """stdio/url/sse 三选一；`registry` 缺省 None=裸协议客户端（不做白名单/参数校验）。

        `timeout_seconds` 缺省从 `get_settings().mcp_call_timeout_seconds`（默认 5s）取，
        构造参数可覆盖（测试与 迭代 9 接线注入）。
        """
        targets = [value for value in (stdio, url, sse) if value is not None]
        if len(targets) != 1:
            raise ValueError("McpClient 需要 stdio / url / sse 三选一，且只能一个")
        self.name = name
        self._stdio = stdio
        self._url = url
        self._sse = sse
        self._registry = registry
        self.timeout_seconds = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(get_settings().mcp_call_timeout_seconds)
        )
        self.backoff_base = float(backoff_base)
        self.max_transient_retries = max(0, int(max_transient_retries))
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self._client: Client | None = None
        self._entered = False
        self._dirty = False  # 连接被超时/断流污染过，下一次使用前必须重建
        # streamable-http 专用：header 要在同一个 httpx 客户端上按调用改，故自己持有。
        self._http_client: Any | None = None

    # ------------------------------------------------------------- 生命周期

    async def connect(self) -> None:
        """显式建连（call_tool/list_tools 均会惰性补建）。"""
        await self._ensure_connected()

    async def close(self) -> None:
        """显式关停（幂等）。"""
        await self._discard_client()

    # ------------------------------------------------------------- 发现

    async def list_tools(self, *, timeout_ms: int | None = None) -> list[Any]:
        """列出服务器提供的工具（SDK `Tool` 原始模型），白名单过滤交给 registry。

        `cache_mode="bypass"`：进程内缓存/TTL 语义归 McpRegistry 管（显式 invalidate），
        不用 SDK 自己的响应缓存，避免两层缓存只见其一失效。
        `timeout_ms` 仅记录缺省语义（发现超时由 SDK 构造级 read_timeout_seconds 收口，
        `list_tools` 本身没有 per-request 参数），不再做外层窗口。
        """
        client = await self._ensure_connected()
        result = await client.list_tools(cache_mode="bypass")
        return list(result.tools)

    # ------------------------------------------------------------- 调用

    @_observed_tool_call
    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
        trace_id: str | None = None,
    ) -> ToolResult:
        """调工具，把四分类/超时/重试全部收口成 `ToolResult`（永不抛四分类）。

        判定顺序（AGENTS §8、测试钉住）：
        ① registry 白名单 → forbidden（不建连、不重试）；
        ② registry schema 校验 → param_error（不建连、不重试，详情 token 可回传模型）；
        ③ 协议调用：transient 才退避重试 ≤2 次（基数可注 0 测速），其余一击到位；
        ④ 重试耗尽仍 transient → `status="transient"` 结果（调用方走降级话术）。
        """
        started = time.perf_counter()
        timeout_ms = int(timeout_ms) if timeout_ms is not None else int(self.timeout_seconds * 1000)
        window = _window_seconds(timeout_ms, self.timeout_seconds)
        wire_args: dict[str, Any] = dict(args or {})
        # 身份注入必须在 schema 校验之前：模型常常不传 elder_id（它也不该知道），
        # 注入后才是完整入参；校验放在注入后，服务端 schema 的 required 要求就能成立。
        wire_args = self._inject_identity_elder(name, wire_args, trace_id=trace_id)

        if self._registry is not None:
            if not self._registry.allows(name):
                _LOG.warning(
                    "mcp_call_forbidden trace_id=%s tool=%s registry=%s", trace_id, name, self.name
                )
                return _elapsed(
                    ToolResult(
                        status="forbidden",
                        name=name,
                        message=_SAFE_MESSAGES["forbidden"],
                        attempts=1,
                        trace_id=trace_id,
                    ),
                    started,
                )
            canonical, error = self._registry.validate(name, wire_args)
            if error is not None:
                _LOG.warning(
                    "mcp_call_param_error trace_id=%s tool=%s detail=%s", trace_id, name, error
                )
                # detail 是 schema 层 token（如 missing_required:city），的安全可回传模型重试。
                return _elapsed(
                    ToolResult(
                        status="param_error",
                        name=name,
                        message=ToolParamError(error).message,
                        attempts=1,
                        trace_id=trace_id,
                    ),
                    started,
                )
            wire_args = canonical

        total_attempts = self.max_transient_retries + 1  # 初始 1 次 + 退避重试 ≤2 次
        transient_reason = "unknown"
        for attempt in range(1, total_attempts + 1):
            try:
                # 重建/建连也放进 try：连接层失败同样归类 transient（除 forbidden 不建连外）。
                if self._dirty:
                    await self._discard_client()
                client = await self._ensure_connected()
                self._apply_identity_headers(name, trace_id=trace_id)
                raw = await client.call_tool(name, wire_args, read_timeout_seconds=window)
                result = self._raw_to_ok_result(name, raw, attempts=attempt, trace_id=trace_id)
                return _elapsed(result, started)
            except TimeoutError:
                self._dirty = True
                transient_reason = "timeout"
            except MCPError as exc:
                classified = _classify_mcp_error(exc)
                if classified in ("param_error", "not_found"):
                    # 这两类不会靠重试好转：立刻终结，不退避。
                    return _elapsed(
                        ToolResult(
                            status=classified,
                            name=name,
                            message=_SAFE_MESSAGES[classified],
                            attempts=attempt,
                            trace_id=trace_id,
                        ),
                        started,
                    )
                self._dirty = True
                transient_reason = f"mcp_error_{exc.code}"
            except Exception as exc:
                self._dirty = True
                transient_reason = f"connection_error:{type(exc).__name__}"
                _LOG.warning(
                    "mcp_call_transport_error trace_id=%s tool=%s kind=%s",
                    trace_id,
                    name,
                    type(exc).__name__,
                )
            if attempt < total_attempts:
                await anyio.sleep(self.backoff_base * (2 ** (attempt - 1)))
        _LOG.warning(
            "mcp_call_transient_exhausted trace_id=%s tool=%s attempts=%s reason=%s",
            trace_id,
            name,
            total_attempts,
            transient_reason,
        )
        return _elapsed(
            ToolResult(
                status="transient",
                name=name,
                message=_SAFE_MESSAGES["transient"],
                attempts=total_attempts,
                trace_id=trace_id,
            ),
            started,
        )

    # ------------------------------------------------------------- 身份注入

    def _inject_identity_elder(
        self, name: str, wire_args: dict[str, Any], *, trace_id: str | None
    ) -> dict[str, Any]:
        """用 runtime_context 的身份覆盖入参里的 `elder_id`（AGENTS §4.9）。

        口径是「丢弃 + 覆盖」而不是「发现就报错」：模型传了别人的 elder_id，说明它在
        猜身份，不该让这一轮对话失败，而该让这次写库落到**实际发起请求的长者**名下；
        没传也照样注入 —— 模型本来就不该知道 elder_id。

        取不到可注入的 elder_id 时（没有 identity，或身份类型是 staff/guardian ——
        照护台与家属端选哪个长者是请求参数，不是身份），丢弃模型值且**不补**：
        随后的 schema 校验会把它变成 `missing_required:elder_id` 的 param_error，
        这比替调用方猜一个长者安全。
        """
        if name not in _ELDER_SCOPED_TOOLS:
            return wire_args
        identity = get_identity()
        elder_id = identity.elder_id if identity is not None else None
        supplied = "elder_id" in wire_args
        wire_args.pop("elder_id", None)
        if elder_id:
            wire_args["elder_id"] = elder_id
        if supplied or not elder_id:
            # 只记「有没有传」，不记传了什么：别人的 elder_id 本身是敏感标识。
            _LOG.warning(
                "tool elder_id overridden",
                extra={
                    "tool": name,
                    "server": self.name,
                    "supplied": supplied,
                    "injected": bool(elder_id),
                    "trace_id": trace_id,
                },
            )
        return wire_args

    def _apply_identity_headers(self, name: str, *, trace_id: str | None) -> None:
        """把当前 runtime_context 身份写进传输层 header（FR-TC-03 第一道路径）。

        只在 streamable-http（有 `_http_client`）时生效：stdio/sse 由 SDK 自己建连接，
        没有可复用的 header 容器 —— 那两种形态下 `elder_id` 仍只走 body，服务端在缺
        header 时按 `elder_id_missing` 拒绝，不会误放行。

        每次调用都重写/删除这两个头：连接是复用的，上一位长者的值必须擦掉，
        否则同一进程里串号比没有 header 更危险。
        """
        http_client = self._http_client
        if http_client is None:
            return
        identity = get_identity()
        elder_id = identity.elder_id if identity is not None else None
        if name not in _ELDER_SCOPED_TOOLS:
            elder_id = None
        headers = http_client.headers
        if elder_id:
            headers[ELDER_HEADER] = elder_id
        elif ELDER_HEADER in headers:
            del headers[ELDER_HEADER]
        if trace_id:
            headers[TRACE_HEADER] = trace_id
        elif TRACE_HEADER in headers:
            del headers[TRACE_HEADER]

    # ------------------------------------------------------------- 内部

    def _new_sdk_client(self) -> Client:
        """按构造参数挑 transport：StdioServerParameters→stdio；URL→streamable-http；CM→SSE。"""
        if self._stdio is not None:
            server: Any = self._stdio
        elif self._url is not None:
            # 自己建 httpx 客户端（而不是让 SDK 按 str 内部建）：只有自己持有，才能在
            # 每次调用前改 header，并在拆除时显式关掉连接池。
            self._http_client = create_mcp_http_client(headers={})
            server = streamable_http_client(self._url, http_client=self._http_client)
        else:
            # 安全校验同源由 sse_client 内部（urljoin + same-origin）保证。
            server = sse_client(self._sse)  # type: ignore[arg-type]
        # `server` 已在上面三个分支里统一成 `Any`（首个分支显式标注），故此处不需要
        # `# type: ignore[arg-type]`：加上反而会被 `warn_unused_ignores` 判成死忽略。
        return Client(
            server=server,
            mode="auto",  # 先 probe `server/discover`，legacy 服务器自动回退标准 initialize
            read_timeout_seconds=self.timeout_seconds,
        )

    async def _ensure_connected(self) -> Client:
        """惰性建连；已建且未脏则复用。

        建连**不套外层窗口**（见模块头：anyio 记账规则——SDK CM 若被包进会中途退出的
        cancel scope，后续 teardown 会报 scope 错）。握手超时由 SDK `read_timeout_seconds`
        收口；`connect_timeout_seconds` 保留为配置面参数，暂无外层闸。
        """
        if self._client is not None and self._entered and not self._dirty:
            return self._client
        if self._client is not None:  # 防御：脏连接理论上应先被上层丢弃，这里兜第二次
            await self._discard_client()
        client = self._new_sdk_client()
        try:
            await client.__aenter__()
        except BaseException:
            # 半开防线：无论异常/取消，都要把 ExitStack 收干净后再抛。
            await self._abandon(client)
            raise
        self._client = client
        self._entered = True
        self._dirty = False
        _LOG.info("mcp_connected name=%s trace_id=-", self.name)
        return client

    async def _discard_client(self) -> None:
        """收掉当前连接（幂等）。拆弃必须裸 `__aexit__`：shield/fail_after 包装实测会让
        本任务残留未消化的取消计数，下一次建连在 TaskGroup.start 处收到 CancelledError
        （纯 SDK probe 复现：裸拆后同场景干净）。SDK 拆链路自带超时（stdio 进程树最多等
        2s 杀掉；httpx2 close 有内建超时），不需要我们兜秒表。"""
        client, self._client = self._client, None
        http_client, self._http_client = self._http_client, None
        self._entered = False
        self._dirty = False
        if client is not None:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
        # 传输段自己建的 httpx 客户端由自己关：SDK 只负责它内部建的那个。
        if http_client is not None:
            with contextlib.suppress(Exception):
                await http_client.aclose()

    async def _abandon(self, client: Client) -> None:
        """建连中途失败/取消后的半开拆除，不向上传播二次异常。"""
        with contextlib.suppress(Exception):
            await client.__aexit__(None, None, None)

    def _raw_to_ok_result(
        self, name: str, raw: Any, *, attempts: int, trace_id: str | None
    ) -> ToolResult:
        """把 SDK `CallToolResult` 拍平成 `ToolResult`（结构化 content dict + isError 透传）。"""
        is_error = bool(getattr(raw, "is_error", False))
        blocks = getattr(raw, "content", None) or []
        texts: list[str] = []
        other_types: list[str] = []
        for block in blocks:
            if getattr(block, "type", "") == "text":
                texts.append(str(getattr(block, "text", "")))
            else:  # image/audio/resource 等块：只记类型，不猜内容
                other_types.append(str(getattr(block, "type", "unknown")))
        text = "\n".join(piece for piece in texts if piece) or None
        structured = getattr(raw, "structured_content", None)
        if structured is not None:
            content: dict[str, Any] = (
                dict(structured) if isinstance(structured, Mapping) else {"value": structured}
            )
        elif blocks:
            content = {"text": text or ""}
            if other_types:
                content["non_text_blocks"] = other_types
        else:
            content = {}
        return ToolResult(
            status="ok",
            name=name,
            content=content,
            text=text,
            message=("工具返回了业务失败" if is_error else ""),
            is_error=is_error,
            attempts=attempts,
            trace_id=trace_id,
        )


def _window_seconds(timeout_ms: int | None, fallback_seconds: float) -> float:
    """统一换算单次超时窗：timeout_ms 优先，缺省用 client 配置（settings 兜底）。"""
    ms = int(timeout_ms) if timeout_ms is not None else int(fallback_seconds * 1000)
    return max(0.001, ms / 1000.0)


def _classify_mcp_error(exc: MCPError) -> Literal["param_error", "not_found", "transient"]:
    """JSON-RPC 错误码 → 四分类映射（AGENTS §8）。其余一律 transient（可重试可降级）。"""
    if exc.code == INVALID_PARAMS:
        return "param_error"  # -32602：入参问题，靠重试无法自愈
    if exc.code in (METHOD_NOT_FOUND, HEADER_MISMATCH):
        return "not_found"  # -32601 / -32020：工具不存在或 schema 过期（SDK 已内建重列一次）
    return "transient"  # -32700/-32600/-32603/-32000/-32022 等临时故障


def _elapsed(result: ToolResult, started: float) -> ToolResult:
    """统一在结果 finalize 时补 elapsed_ms（ToolResult 是 frozen dataclass，用 replace 重建）。"""
    return dataclasses.replace(result, elapsed_ms=int((time.perf_counter() - started) * 1000))
