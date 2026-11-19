"""Anyu 天气 MCP 服务器包（迭代 6 · W5-half）。

刻意保持轻量：不放 eager 导入（`mcp-servers.weather.server` 是测试与客户端的标准
入口，直接 `importlib.import_module("mcp-servers.weather.server")` 即可；生产入口
`python -m mcp-servers.weather.server [stdio|http]`）。
"""
