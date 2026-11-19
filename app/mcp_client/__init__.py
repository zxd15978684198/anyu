"""MCP 客户端：工具发现 / 调用 / 超时 / 越权审计（迭代 6.3）。

身份只能从服务端 runtime_context 注入，模型自报的 elder_id 一律丢弃（AC-29）。
"""

from app.mcp_client.client import (
    McpClient,
    McpToolError,
    ToolForbidden,
    ToolNotFound,
    ToolParamError,
    ToolResult,
    ToolStatus,
    ToolTransient,
)
from app.mcp_client.registry import McpRegistry, ToolDescriptor

__all__ = [
    "McpClient",
    "McpRegistry",
    "McpToolError",
    "ToolDescriptor",
    "ToolForbidden",
    "ToolNotFound",
    "ToolParamError",
    "ToolResult",
    "ToolStatus",
    "ToolTransient",
]
