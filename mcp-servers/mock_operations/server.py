"""Mock Operations MCP 服务（迭代 8 · N20，mcp-servers/mock_operations）。

工具面两个（PRD FR-TC-03 / 技术设计文档 §2.2）：

- ``create_work_order(type, severity, elder_id?, note?)`` —— 建一张 `new` 工单；
- ``send_family_notification(kind, message, elder_id?)`` —— 生成一条家属通知。

`elder_id` 不在必填列表里：它由客户端 `app/mcp_client/client.py` 在派发处用
runtime_context 的身份**丢弃并覆盖**（AGENTS §4.9）。这里保留可选参数是为了
「绕过客户端直连」时仍能给出结构化 `param_error/elder_id_missing`，而不是把
pydantic 的英文校验文本甩给调用方。

身份是**两道路径**（FR-TC-03）：客户端在传输层 header `X-Anyu-Elder` 里带真值，
本文件用 `resolve_elder_authority()` 复核 body 里的值 —— 两者不一致即
`forbidden/elder_mismatch`，写一条 `tool.call` 审计（`detail.error_code` 落细分码、
`requested_elder` 脱敏），业务行一行不写。业务函数（`ops.py`）只收对齐后的 elder_id，
因此「模型自己编一个 elder_id」在直连与经客户端两条路径上都不成立。

出参统一 ``ToolResult`` 信封（与 weather 同款）：业务失败不抛协议级异常、
``is_error`` 恒 False，客户端按 ``error_code`` 走四分类
（``param_error`` / ``not_found`` / ``transient``），错误文本只给稳定细分码，
不回显内部异常栈（AGENTS §5）。

传输与 weather 一致：``/mcp`` 是 MCP 协议面（streamable-http），``/health`` 是
给 compose 探活的普通 GET（``custom_route`` 注册，不参与协议）。

落库不在这里写 —— 全部转交 ``ops.py``，后者复用 ``app/services`` 的唯一实现
（选择与理由见 ``README.md`` §1）。
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .ops import (
    ELDER_HEADER,
    MAX_MESSAGE_LENGTH,
    MAX_NOTE_LENGTH,
    TRACE_HEADER,
    OpsError,
    audit_rejection,
    resolve_elder_authority,
)
from .ops import (
    create_work_order as _create_work_order,
)
from .ops import (
    send_family_notification as _send_family_notification,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

SERVER_NAME: Final[str] = "mock-operations"
SERVER_TITLE: Final[str] = "Mock Operations Server"
SERVER_SERVICE_NAME: Final[str] = "mock-operations-mcp"

#: 工具名清单（``/health`` 与 README 共用，避免两处各写一份）。
TOOL_NAMES: Final[tuple[str, str]] = ("create_work_order", "send_family_notification")


class ToolResult(BaseModel):
    """工具出参信封：``ok`` 之外，失败时给 ``error_code`` 三分类与可重试位。

    ``data`` 成功时装载落库结果（order_id/order_no 或 notification_id），失败恒 None；
    ``extra="forbid"`` 防上游多字段混入（信封字段集即契约）。
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    detail: str | None = None
    retryable: bool = False


async def _run_tool(
    tool: str,
    body_elder_id: str | None,
    headers: Mapping[str, str] | None,
    call: Callable[[str, str | None], Awaitable[dict[str, Any]]],
) -> ToolResult:
    """先对齐身份权威值，再把 ``OpsError`` 转成信封，并给被拒调用补审计。

    身份两道路径（FR-TC-03）：客户端派发处已经丢弃模型的 `elder_id` 并注入真值
    （第一道）；这里用传输层 header 复核 body 里的值（第二道，直连服务端时才有意义）。
    ``call`` 收的是**对齐后**的 elder_id，业务层因此永远拿不到模型写的身份。
    """
    header_map = headers or {}
    trace_id = header_map.get(TRACE_HEADER)
    authority: str | None = None
    try:
        authority = resolve_elder_authority(body_elder_id, header_map.get(ELDER_HEADER))
        data = await call(authority, trace_id)
    except OpsError as exc:
        # authority 为空说明连身份都没对齐（缺 header / 越权），此时审计不归属任何长者行。
        await audit_rejection(tool, authority, exc, trace_id=trace_id)
        return ToolResult(
            ok=False,
            error_code=exc.error_code,
            detail=exc.detail,
            retryable=exc.retryable,
        )
    return ToolResult(ok=True, data=data)


def build_server() -> MCPServer:
    """装配 mock-operations MCP server（工具名/注解/schema 全部显式声明）。"""
    operations = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        description="Anyu Mock 工单与家属通知工具（落库可查，不接真实外部系统）",
        instructions=(
            "create_work_order(elder_id, type, severity, note?) 建工单；"
            "send_family_notification(elder_id, kind, message) 生成家属通知。"
            "出参为 ToolResult 信封（ok/data/error_code/detail/retryable）："
            "param_error 修正参数后重试，not_found 如实告知不要编造，transient 可重试。"
        ),
    )

    @operations.tool(
        name="create_work_order",
        title="Create Work Order",
        description=(
            "创建工单并落库：成功 {ok,data{order_id,order_no,status,type,severity,elder_id}}；"
            "失败 {ok=False,error_code,detail,retryable}。"
            "type 取八值闭集（missed_medication/sensitive_alert/health_alert/device_fault/"
            "sos/collection_missed/drug_question/follow_up），severity 仅 red|yellow，"
            f"note 可选、≤{MAX_NOTE_LENGTH} 字。"
        ),
        annotations=ToolAnnotations(
            title="Create Work Order",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def create_work_order(
        ctx: Context,
        type: str,
        severity: str,
        elder_id: str | None = None,
        note: str | None = None,
    ) -> ToolResult:
        # ctx 由 SDK 按类型注解注入、不进 schema；elder_id 故意排在必填项之后可选：
        # 它由客户端从 runtime_context 注入（§4.9），模型不该知道也不需要填；真的缺失时由
        # 身份对齐层返回结构化 param_error/elder_id_missing，而不是让 SDK 的 pydantic
        # 报一段调用方看不懂的英文校验文本。
        return await _run_tool(
            "create_work_order",
            elder_id,
            ctx.headers,
            lambda authority, trace_id: _create_work_order(
                authority, type, severity, note, trace_id=trace_id
            ),
        )

    @operations.tool(
        name="send_family_notification",
        title="Send Family Notification",
        description=(
            "生成一条家属通知并落库（不接真实短信/微信）："
            "成功 {ok,data{notification_id,kind,severity,status,elder_id}}；"
            "失败 {ok=False,error_code,detail,retryable}。"
            "kind 取 health_review/medication_escalation/collection_missed/family_report，"
            f"message ≤{MAX_MESSAGE_LENGTH} 字，会写进家属端摘要。"
        ),
        annotations=ToolAnnotations(
            title="Send Family Notification",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def send_family_notification(
        ctx: Context,
        kind: str,
        message: str,
        elder_id: str | None = None,
    ) -> ToolResult:
        return await _run_tool(
            "send_family_notification",
            elder_id,
            ctx.headers,
            lambda authority, trace_id: _send_family_notification(
                authority, kind, message, trace_id=trace_id
            ),
        )

    @operations.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
    async def health(_request: Request) -> Response:
        """存活探针：不碰数据库（库挂了也该先能起进程，业务失败由工具信封表达）。"""
        return JSONResponse({"ok": True, "service": SERVER_SERVICE_NAME, "tools": list(TOOL_NAMES)})

    return operations


def _http_target(settings: Settings) -> tuple[str, int, str]:
    """由 ``mcp_operations_server_url`` 解析 (host, port, path)（path 缺省 → /mcp）。"""
    url = urllib.parse.urlsplit(settings.mcp_operations_server_url)
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    return url.hostname or "127.0.0.1", port, url.path or "/mcp"


def _allowed_hosts(host: str, port: int) -> list[str]:
    """streamable-http 的 Host 白名单（防 DNS rebinding）。

    SDK 默认只放行 ``127.0.0.1`` / ``localhost``，容器内被 backend 用服务名
    ``mock-operations-mcp:7102`` 访问时会回 421 —— 与 weather 同一个坑，按
    「绑定地址 + 本机 + 服务名」放行，``MCP_ALLOWED_HOSTS`` 可追加。
    """
    hosts = {f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"}
    hosts.add(f"{SERVER_SERVICE_NAME}:{port}")
    extra = os.environ.get("MCP_ALLOWED_HOSTS", "")
    hosts.update(item.strip() for item in extra.split(",") if item.strip())
    return sorted(hosts)


def _serve_http() -> int:
    """streamable-http 形态：按配置 URL 绑定 host:port（compose 里是 0.0.0.0:7102）。"""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from app.core.config import get_settings

    host, port, path = _http_target(get_settings())
    allowed = _allowed_hosts(host, port)
    app = server.streamable_http_app(
        streamable_http_path=path,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            allowed_origins=sorted({f"http://{item}" for item in allowed} | {"*"}),
        ),
    )
    logger.info(
        "mock-operations mcp server: streamable-http on %s:%s%s (allowed_hosts=%s)",
        host,
        port,
        path,
        ",".join(allowed),
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：``python -m mcp-servers.mock_operations.server``（stdio，缺省）或 `[http]`。"""
    args = list(sys.argv[1:] if argv is None else argv)
    transport = args[0].lower() if args and args[0].lower() in ("stdio", "http") else "stdio"
    if transport == "stdio":
        server.run("stdio")
    else:
        return _serve_http()
    return 0


#: 生产默认实例（stdio 直接跑；HTTP 复用同一工具面与 /health）。
server: MCPServer = build_server()


if __name__ == "__main__":
    raise SystemExit(main())
