"""迭代 6 W4-half：app/mcp_client 四分类 / 白名单 / 手写 schema 校验 / 发现缓存 测试。

测试策略（固定口径）
- **协议路用官方 mcp 2.3.0 SDK**（与 client.py 同一选型）；测试服务器是本文件内自建的
  minimal MCP server：stdio 侧为「换行分隔 JSON-RPC」子进程脚本（stdlib 手写协议对端，
  与 SDK 同一 wire 语义），http 侧为 stdlib ThreadingHTTPServer 起在本机 127.0.0.1 上的
  streamable-http 假服务器。**不 import、不依赖同波 W5 的 mcp-servers/weather 实现**。
- 全程局域：127.0.0.1 + 本地子进程，零外呼网络。
- 分类判定顺序（AGENTS §8）用「从不建连」等旁证钉住（`client._entered is False`）。
- 临时错误重试：`backoff_base=0` 注入（退避基数清零，只验证重试次数语义不验证睡眠时长）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types import HEADER_MISMATCH, INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND

from app.core.runtime_context import Identity, reset_identity, set_identity
from app.mcp_client.client import ELDER_HEADER, TRACE_HEADER, McpClient, _classify_mcp_error
from app.mcp_client.registry import DEFAULT_TOOL_WHITELIST, McpRegistry, ToolDescriptor

# ---------------------------------------------------------------------------
# 共享 fixture 数据：fake tool 清单与天气 schema
# ---------------------------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_weather",
        "description": "mock weather lookup",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
    {
        "name": "slow_weather",
        "description": "always sleeps far beyond any test timeout",
        "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
    {
        "name": "boom_tool",
        "description": "returns isError=true (business failure)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "strict_tool",
        "description": "mock validator: requires 'password', else JSON-RPC -32602",
        "inputSchema": {
            "type": "object",
            "properties": {"password": {"type": "string"}},
            "required": ["password"],
        },
    },
]

_WEATHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "enum": ["杭州", "上海"]},
        "days": {"type": "integer", "default": 1},
        "unit": {"type": "string", "default": "celsius"},
    },
    "required": ["city"],
    "additionalProperties": False,
}


def _weather_payload(args: dict[str, Any]) -> dict[str, Any]:
    """get_weather 的.mock 结构化返回（服务端 self.isError=False 的正常结果）。"""
    return {"city": args.get("city") or "H", "temp_c": 28}


# ---------------------------------------------------------------------------
# stdio 侧：换行分隔 JSON-RPC 的 minimal MCP 服务器（子进程脚本，纯 stdlib）
# ---------------------------------------------------------------------------

_FAKE_SERVER_SCRIPT = rf"""
import json
import sys
import time

TOOLS = {_TOOLS!r}


def _resp(req_id, result):
    return json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": result}}, ensure_ascii=True)


def _err(req_id, code, message):
    return json.dumps(
        {{"jsonrpc": "2.0", "id": req_id, "error": {{"code": code, "message": message}}}},
        ensure_ascii=True,
    )


def _weather_result(args):
    payload = {{"city": args.get("city") or "H", "temp_c": 28}}
    return {{
        "content": [{{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}}],
        "structuredContent": payload,
        "isError": False,
    }}


def _handle(req):
    method = req.get("method")
    req_id = req.get("id")
    if req_id is None:
        return None  # notification：无响应（notifications/initialized 等）
    params = req.get("params") or {{}}
    if method == "initialize":
        requested = params.get("protocolVersion") or "2024-11-05"
        return _resp(
            req_id,
            {{
                "protocolVersion": requested,
                "capabilities": {{"tools": {{"listChanged": False}}}},
                "serverInfo": {{"name": "fake-w4-server", "version": "0.1.0"}},
            }},
        )
    if method == "ping":
        return _resp(req_id, {{}})
    if method == "tools/list":
        return _resp(req_id, {{"tools": TOOLS}})
    if method == "tools/call":
        tool = params.get("name")
        args = params.get("arguments") or {{}}
        if tool == "get_weather":
            return _resp(req_id, _weather_result(args))
        if tool == "slow_weather":
            time.sleep(30)
            return _resp(req_id, _weather_result(args))
        if tool == "boom_tool":
            return _resp(
                req_id,
                {{"content": [{{"type": "text", "text": "mock internal failure"}}], "isError": True}},
            )
        if tool == "strict_tool":
            if "password" not in args:
                return _err(req_id, -32602, "Mock strict_tool: missing required argument 'password'")
            return _resp(req_id, _weather_result(args))
        return _err(req_id, -32601, "Mock server: tool not found: %s" % tool)
    return _err(req_id, -32601, "Mock server: method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue  # 协议外脏数据：静默吞掉，绝不写 stdout
        try:
            out = _handle(req)
        except Exception:
            continue
        if out is not None:
            sys.stdout.buffer.write((out + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()


main()
"""

_STDIO_CALL_TIMEOUT_MS = 4000  # 覆盖子进程 spawn + initialize 握手 + 一次工具调用的整窗


@pytest.fixture()
def stdio_params(tmp_path) -> StdioServerParameters:
    """把 minimal stdio MCP server 脚本落盘并回传 StdioServerParameters（子进程由 SDK 启）。"""
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_SERVER_SCRIPT, encoding="utf-8")
    return StdioServerParameters(command=sys.executable, args=[str(script)])


# ---------------------------------------------------------------------------
# http 侧：stdlib ThreadingHTTPServer 上的 streamable-http 假服务器（127.0.0.1）
# ---------------------------------------------------------------------------


def _rpc_error_body(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


#: 假服务器收到的每个 POST 的 (method, 小写 header, params)：用来钉「客户端真的发了身份头」。
_SEEN_REQUESTS: list[tuple[str | None, dict[str, str], dict[str, Any]]] = []


class _StreamableHttpHandler(BaseHTTPRequestHandler):
    """streamable-http 最小实现：POST JSON-RPC 单响应；GET SSE 拉流口回 405（客户端静默放弃）。"""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send_json(405, {})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            self._send_json(400, _rpc_error_body(None, -32700, "Parse error"))
            return
        req_id = req.get("id")
        _SEEN_REQUESTS.append(
            (
                req.get("method"),
                {k.lower(): v for k, v in self.headers.items()},
                req.get("params") or {},
            )
        )
        if req_id is None:
            # notification：按 streamable-http 规范回 202 Accepted。
            self._send_json(202, {})
            return
        params = req.get("params") or {}
        method = req.get("method")
        if method == "initialize":
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake-w4-http", "version": "0.1.0"},
                    },
                },
            )
        elif method == "ping":
            self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS}})
        elif method == "tools/call":
            tool = params.get("name")
            args = params.get("arguments") or {}
            if tool == "get_weather":
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(_weather_payload(args), ensure_ascii=True),
                                }
                            ],
                            "structuredContent": _weather_payload(args),
                            "isError": False,
                        },
                    },
                )
            elif tool == "slow_weather":
                time.sleep(30)  # 远超任何测试窗口，必然触发客户端超时
                self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})
            else:
                self._send_json(200, _rpc_error_body(req_id, -32601, f"tool not found: {tool}"))
        else:
            self._send_json(200, _rpc_error_body(req_id, -32601, f"method not found: {method}"))


@pytest.fixture()
def http_base() -> str:
    """本机起一个 streamable-http 假服务器，返回 Endpooint URL。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamableHttpHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# McpRegistry：手写最小 JSON-Schema 校验 5 例 + 白名单/缓存
# ---------------------------------------------------------------------------


def _weather_registry() -> McpRegistry:
    return McpRegistry(whitelist={"get_weather": ()}, schemas={"get_weather": _WEATHER_SCHEMA})


def test_schema_missing_required() -> None:
    canonical, err = _weather_registry().validate("get_weather", {"unit": "f"})
    assert canonical == {}
    assert err == "missing_required:city"


def test_schema_type_mismatch() -> None:
    canonical, err = _weather_registry().validate("get_weather", {"city": 42})
    assert canonical == {}
    assert err == "type_mismatch:city"


def test_schema_enum_mismatch() -> None:
    canonical, err = _weather_registry().validate("get_weather", {"city": "北京"})
    assert canonical == {}
    assert err == "enum_mismatch:city"


def test_schema_unexpected_property() -> None:
    # additionalProperties=False：模型自报的 elder_id 必须被丢弃/拒绝（AC-29 的参数层闸门）。
    canonical, err = _weather_registry().validate(
        "get_weather", {"city": "杭州", "elder_id": "m001"}
    )
    assert canonical == {}
    assert err == "unexpected_property:elder_id"


def test_schema_valid_fills_defaults() -> None:
    canonical, err = _weather_registry().validate("get_weather", {"city": "上海"})
    assert err is None
    assert canonical == {"city": "上海", "days": 1, "unit": "celsius"}


def test_schema_not_whitelisted_rejected() -> None:
    canonical, err = _weather_registry().validate("boom_tool", {})
    assert canonical == {}
    assert err == "not_whitelisted:boom_tool"


class _FakeBackend:
    """registry.discover 的替身：计数 list_tools 调用次数（缓存语义测试用）。"""

    name = "fake-weather"

    def __init__(self, tools: list[Any]) -> None:
        self._tools = tools
        self.calls = 0

    async def list_tools(self) -> list[Any]:
        self.calls += 1
        return list(self._tools)


def _fake_tool(name: str, schema: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(name=name, description=f"mock {name}", input_schema=schema or {})


async def test_registry_discover_cache_and_invalidate() -> None:
    backend = _FakeBackend([_fake_tool("get_weather", {"type": "object"})])
    registry = McpRegistry(whitelist={"get_weather": ()})

    first = await registry.discover(backend)
    assert backend.calls == 1
    assert [d.name for d in first] == ["get_weather"]

    second = await registry.discover(backend)  # 缓存命中：不再触发 list_tools
    assert backend.calls == 1
    assert second == first

    registry.invalidate()  # 唯一失效手段（显式，无 TTL）
    await registry.discover(backend)
    assert backend.calls == 2


async def test_registry_discover_fail_closed_outside_whitelist() -> None:
    backend = _FakeBackend(
        [_fake_tool("get_weather", {}), _fake_tool("boom_tool", {}), _fake_tool("zero_tool", {})]
    )
    registry = McpRegistry()  # 缺省白名单：get_weather 唯一真实外部接口（齐平 W3）
    descriptors = await registry.discover(backend)
    assert [d.name for d in descriptors] == ["get_weather"]
    assert DEFAULT_TOOL_WHITELIST.keys() == {"get_weather"}


def test_whitelist_roles_matrix() -> None:
    registry = McpRegistry(whitelist={"get_weather": ("family",), "boom_tool": ()})
    assert registry.allows("boom_tool")  # 空 role 集＝不限角色
    assert registry.allows("get_weather", roles=["family"])
    assert not registry.allows("get_weather", roles=["caretaker"])
    assert not registry.allows("zero_tool")
    assert registry.roles_for("zero_tool") == ()
    # ToolDescriptor 形状（discover 产物契约）
    descriptor = ToolDescriptor(name="get_weather", input_schema={"type": "object"})
    assert descriptor.allowed_roles == ()
    assert descriptor.description is None


def test_classify_mcp_error_mapping() -> None:
    # JSON-RPC 错误码 → 四分类映射钉住（-32602 参数 / -32601 与 -32020 不存在 / 其余临时）。
    assert _classify_mcp_error(MCPError(INVALID_PARAMS, "bad args")) == "param_error"
    assert _classify_mcp_error(MCPError(METHOD_NOT_FOUND, "no such tool")) == "not_found"
    assert _classify_mcp_error(MCPError(HEADER_MISMATCH, "stale session")) == "not_found"
    assert _classify_mcp_error(MCPError(INTERNAL_ERROR, "boom")) == "transient"


# ---------------------------------------------------------------------------
# McpClient：三选一构造与 SSE plumbing（不建连的静态分支）
# ---------------------------------------------------------------------------


def test_client_requires_exactly_one_transport() -> None:
    with pytest.raises(ValueError):
        McpClient(name="x")
    ssp = StdioServerParameters(command=sys.executable, args=["-c", "pass"])
    with pytest.raises(ValueError):
        McpClient(name="x", stdio=ssp, url="http://127.0.0.1:9/mcp")
    with pytest.raises(ValueError):
        McpClient(name="x", url="http://127.0.0.1:9/mcp", sse="http://127.0.0.1:9/sse")


def test_sse_transport_branch_plumbing() -> None:
    """SSE：将 `sse_client(url)` 的 async CM 直接交给 Client 的分支（不触网，只验分派）。"""
    client = McpClient(name="sse-fake", sse="http://127.0.0.1:9/sse", timeout_seconds=1.0)
    sdk = client._new_sdk_client()
    server = getattr(sdk, "server", None)
    assert server is not None
    assert not isinstance(server, (str, StdioServerParameters))  # 既非 streamable-http 也非 stdio


# ---------------------------------------------------------------------------
# stdio 端到端：ok / 业务 isError / 协议 -32602 / -32601 / 注册台发现+白名单过滤
# ---------------------------------------------------------------------------


def _stdio_client(
    params: StdioServerParameters,
    registry: Any | None = None,
) -> McpClient:
    return McpClient(
        name="fake-stdio-weather",
        stdio=params,
        timeout_seconds=8.0,
        registry=registry,
        connect_timeout_seconds=8.0,
    )


async def test_stdio_list_tools_and_call_ok(stdio_params: StdioServerParameters) -> None:
    client = _stdio_client(stdio_params)
    try:
        await client.connect()
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert {"get_weather", "slow_weather", "boom_tool", "strict_tool"} <= names

        result = await client.call_tool(
            "get_weather", {"city": "H"}, timeout_ms=_STDIO_CALL_TIMEOUT_MS
        )
        assert result.status == "ok"
        assert result.is_error is False
        assert result.attempts == 1
        assert result.content == {"city": "H", "temp_c": 28}  # structuredContent dict 化
        assert result.text and "temp_c" in result.text
    finally:
        await client.close()


async def test_stdio_business_is_error_is_passthrough(stdio_params: StdioServerParameters) -> None:
    """isError=true 是业务层失败（如天气 key 缺失）：协议 ok、is_error 透传、不计四分类。"""
    client = _stdio_client(stdio_params)
    try:
        result = await client.call_tool("boom_tool", {}, timeout_ms=_STDIO_CALL_TIMEOUT_MS)
        assert result.status == "ok"
        assert result.is_error is True
        assert result.attempts == 1
    finally:
        await client.close()


async def test_stdio_protocol_param_error_minus_32602(stdio_params: StdioServerParameters) -> None:
    """strict_tool 缺 required → 服务器回 -32602 → SDK 抛 MCPError → param_error（一击即中不重试）。"""
    client = _stdio_client(stdio_params)
    try:
        result = await client.call_tool("strict_tool", {}, timeout_ms=_STDIO_CALL_TIMEOUT_MS)
        assert result.status == "param_error"
        assert result.attempts == 1
        assert result.message  # 安全话术非空
        assert "Traceback" not in result.message  # 不回显内部栈
    finally:
        await client.close()


async def test_stdio_not_found_minus_32601(stdio_params: StdioServerParameters) -> None:
    client = _stdio_client(stdio_params)
    try:
        result = await client.call_tool("zero_tool", {}, timeout_ms=_STDIO_CALL_TIMEOUT_MS)
        assert result.status == "not_found"
        assert result.attempts == 1
    finally:
        await client.close()


async def test_stdio_registry_discover_and_validate(stdio_params: StdioServerParameters) -> None:
    """注册台经 stdio 后端发现：白名单 fail-closed + inputSchema 从缓存回填 validate。"""
    registry = McpRegistry(whitelist={"get_weather": ()})
    client = _stdio_client(stdio_params, registry=registry)
    try:
        await client.connect()
        descriptors = await registry.discover(client)
        assert [d.name for d in descriptors] == ["get_weather"]
        assert descriptors[0].input_schema  # 服务端 inputSchema 带回
        assert descriptors[0].allowed_roles == ()

        # validate 由发现缓存中的 schema 驱动：合法通过、白名单外拒绝。
        canonical, err = registry.validate("get_weather", {"city": "H"})
        assert err is None
        assert canonical == {"city": "H"}
        assert registry.validate("boom_tool", {})[1] == "not_whitelisted:boom_tool"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# http(streamable-http) 端到端：ok / 超时 transient 重试 / 拒连 transient / 越权闸门
# ---------------------------------------------------------------------------


async def test_http_streamable_call_ok(http_base: str) -> None:
    registry = McpRegistry(whitelist={"get_weather": ()})  # 无 schema：透传不瞎校验
    client = McpClient(
        name="fake-http-weather", url=http_base, timeout_seconds=5.0, registry=registry
    )
    try:
        result = await client.call_tool("get_weather", {"city": "H"}, timeout_ms=3000)
        assert result.status == "ok"
        assert result.is_error is False
        assert result.attempts == 1
        assert result.content == {"city": "H", "temp_c": 28}
        assert result.text and "temp_c" in result.text
    finally:
        await client.close()


async def test_call_tool_sends_runtime_elder_header(http_base: str) -> None:
    """FR-TC-03 第一道：streamable-http 派发时把 runtime 身份写进 `X-Anyu-Elder`。

    同时钉两件容易退化的细节：
    - body 里仍保留同一个 elder_id（§4.9 的「丢弃 + 覆盖」没被 header 改动顺带删掉），
      于是服务端第二道看到的 body 与 header 一致，不会误判越权；
    - 换成没有 elder 的身份后，**同一个复用连接**上那个头必须消失 —— 否则会串到上一位长者。
    """
    elder = str(uuid.uuid4())
    trace = f"tc03{uuid.uuid4().hex[:24]}"
    client = McpClient(name="mock-operations", url=http_base, timeout_seconds=5.0)
    token = set_identity(Identity(kind="device", device_id=str(uuid.uuid4()), elder_id=elder))
    try:
        _SEEN_REQUESTS.clear()
        await client.call_tool(
            "create_work_order", {"type": "sos", "severity": "red"}, trace_id=trace
        )

        headers, body = _tool_call("create_work_order")
        assert headers[ELDER_HEADER.lower()] == elder
        assert headers[TRACE_HEADER.lower()] == trace
        assert body["elder_id"] == elder

        staff = Identity(kind="staff", staff_id=str(uuid.uuid4()), role="admin")
        reset_identity(token)
        token = set_identity(staff)
        _SEEN_REQUESTS.clear()
        await client.call_tool(
            "send_family_notification", {"kind": "family_report", "message": "好"}
        )

        headers, body = _tool_call("send_family_notification")
        assert ELDER_HEADER.lower() not in headers
        assert "elder_id" not in body
    finally:
        reset_identity(token)
        await client.close()


def _tool_call(tool: str) -> tuple[dict[str, str], dict[str, Any]]:
    """取假服务器收到的某次 `tools/call` 的 (header, arguments)。"""
    for method, headers, params in _SEEN_REQUESTS:
        if method == "tools/call" and params.get("name") == tool:
            return headers, dict(params.get("arguments") or {})
    raise AssertionError(f"没有捕获到 {tool} 的 tools/call 请求")


async def test_http_not_found(http_base: str) -> None:
    client = McpClient(name="fake-http-weather", url=http_base, timeout_seconds=5.0)
    try:
        result = await client.call_tool("zero_tool", {}, timeout_ms=3000)
        assert result.status == "not_found"
        assert result.attempts == 1
    finally:
        await client.close()


async def test_http_transient_timeout_exhausts_backoff_retries(http_base: str) -> None:
    """slow_weather 永远不回 → 每次超时 → 退避重试 ≤2 次（1+2 全部失败）→ transient。"""
    client = McpClient(
        name="fake-http-weather-slow",
        url=http_base,
        timeout_seconds=5.0,
        backoff_base=0.0,  # 注 0：验证重试次数语义，不消耗真实退避时长
        max_transient_retries=2,
    )
    try:
        result = await client.call_tool("slow_weather", {"city": "H"}, timeout_ms=400)
        assert result.status == "transient"
        assert result.attempts == 3  # 初始 1 次 + 重试 2 次
        assert result.message  # 降级安全话术
        assert result.elapsed_ms > 0
    finally:
        await client.close()


async def test_http_transient_connection_error(http_base: str) -> None:
    """端口 1 拒连（本机回环连接错）→ 连接层炸也归 transient，重试耗尽降级。"""
    client = McpClient(
        name="fake-http-unreachable",
        url="http://127.0.0.1:1/mcp",
        timeout_seconds=5.0,
        backoff_base=0.0,
        max_transient_retries=2,
    )
    try:
        result = await client.call_tool("get_weather", {"city": "H"}, timeout_ms=3000)
        assert result.status == "transient"
        assert result.attempts == 3
    finally:
        await client.close()


async def test_whitelist_outside_tool_is_forbidden_without_connect(http_base: str) -> None:
    """越权：白名单外 → forbidden；不建任何连接（AGENTS §8：拒绝 + 审计 + trace_id）。"""
    client = McpClient(
        name="fake-http-weather-gate",
        url=http_base,  # 可达 URL，但永远不应被打开
        timeout_seconds=5.0,
        registry=McpRegistry(whitelist={"get_weather": ()}),
    )
    try:
        result = await client.call_tool("boom_tool", {}, timeout_ms=1000, trace_id="t-403")
        assert result.status == "forbidden"
        assert result.attempts == 1
        assert result.trace_id == "t-403"
        assert client._entered is False  # 从未建连的旁证
    finally:
        await client.close()


async def test_schema_gate_param_error_before_connect(http_base: str) -> None:
    """参数闸门：schema 校验失败 → param_error（安全 token 可回传模型重试），且不建连。"""
    registry = McpRegistry(whitelist={"get_weather": ()}, schemas={"get_weather": _WEATHER_SCHEMA})
    client = McpClient(
        name="fake-http-weather-schema",
        url=http_base,
        timeout_seconds=5.0,
        registry=registry,
    )
    try:
        result = await client.call_tool("get_weather", {}, timeout_ms=1000)
        assert result.status == "param_error"
        assert result.attempts == 1
        assert "missing_required:city" in result.message
        assert client._entered is False
    finally:
        await client.close()


async def test_schema_gate_allows_valid_args_through(http_base: str) -> None:
    """schema 校验合法时补 defaults 并放行到协议层（canonical 补 days/unit 后仍得到正常结果）。"""
    registry = McpRegistry(whitelist={"get_weather": ()}, schemas={"get_weather": _WEATHER_SCHEMA})
    client = McpClient(
        name="fake-http-weather-schema-ok",
        url=http_base,
        timeout_seconds=5.0,
        registry=registry,
    )
    try:
        result = await client.call_tool("get_weather", {"city": "杭州"}, timeout_ms=3000)
        assert result.status == "ok"
        assert result.content == {"city": "杭州", "temp_c": 28}
    finally:
        await client.close()


def test_stdio_server_script_runs_standalone_and_exits_on_eof(
    stdio_params: StdioServerParameters,
) -> None:
    """冒烟：minimal stdio 服务器脚本可独立运行、EOF 干净退出 0（不依赖 W5 服务）。"""
    proc = subprocess.Popen(  # noqa: S603 - 本文件写入脚本 + sys.executable，受信输入
        [sys.executable, str(stdio_params.args[0])],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.stdin is None  # stdin=DEVNULL：直接 EOF 退出（服务器脚本容忍空读）
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


async def test_stdio_server_supports_probe_fallback(stdio_params: StdioServerParameters) -> None:
    """mode=auto：`server/discover` 探测回 -32601 → SDK 回退 legacy initialize → 闭环可用。"""
    client = _stdio_client(stdio_params)
    try:
        await client.connect()  # probe 失败但回退成功即视为建连完成
        result = await client.call_tool(
            "get_weather", {"city": "H"}, timeout_ms=_STDIO_CALL_TIMEOUT_MS
        )
        assert result.status == "ok"
        assert result.content == {"city": "H", "temp_c": 28}
    finally:
        await client.close()
