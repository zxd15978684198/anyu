"""Anyu Mock Operations MCP 服务器包（迭代 8 · N20）。

承载两个 Mock 工具：`create_work_order`（工单落库）与 `send_family_notification`
（家属通知落库）。刻意保持轻量：不放 eager 导入 —— 实现入口是
`mcp-servers.mock_operations.server`（HTTP 形态由 `python -m mcp-servers.mock_operations.server http`
拉起，`/mcp` 是 MCP 协议面，`/health` 是存活探针）。
"""
