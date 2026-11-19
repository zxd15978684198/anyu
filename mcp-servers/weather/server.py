"""get_weather MCP 工具服务（迭代 6 · W5-half，mcp-servers/weather）。

工具面：唯一工具 ``get_weather(city)`` —— 返回**结构化信封**（pydantic ``ToolResult``，
自动发布 outputSchema；错误不抛异常、不上抛协议级 TinyError，理由：客户端可在两种
传输（stdio / streamable-http）下确定性解析 ``error_code / detail / retryable``，
不会出现「错误文本被服务端截留」的盲区）。``is_error`` 恒 False —— 业务失败也以
``ok=False`` 信封表达，与 W4 期 mcp_client 的消费契约对齐（显式信封优于隐式异常）。

失败四分类（§8）：本工具负责其中三类；「越权」由 mcp_client 层处置。
- 参数错（``param_error``，不重试）：city 空白/超长/查无此城（provider 404）；
- 目标不存在与目标不可用（``unavailable``，不重试）：key 缺失、未知供应商、上游 401/403、其余 4xx；
- 临时错误（``transient``，可重试）：**3s 工具层整段超时（``slow``）**、网络断瞬、
  5xx、429、坏载荷；未捕获异常兜底为 ``transient``+``internal``（不外泄堆栈）。

红线：模型/调用方不可注入 URL —— 网络出口只有 provider 的固定 API host（或运营方
显式覆写的 ``weather_api_base_url``）；本模块不做任何自由外网访问。
配置延迟加载：``build_server()`` 不读 .env（provider/cache 首次调用工具时才组装）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import urllib.parse
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from .provider import (
    CODE_TRANSIENT,
    DETAIL_SLOW,
    HTTP_TIMEOUT_SECONDS,
    WeatherError,
    WeatherProvider,
    normalize_city,
)

if TYPE_CHECKING:
    from app.core.config import Settings

from .cache import WeatherCache

logger = logging.getLogger(__name__)

SERVER_NAME: Final[str] = "weather"
SERVER_TITLE: Final[str] = "Weather Server"
SERVER_SERVICE_NAME: Final[str] = "weather-mcp"

#: 工具层整段预算（cache 读 + provider 两次顺序请求 + cache 写合计）。
#: 一次 `get_weather` 顺序发两枪上游：geocoding 查城市 → forecast 取天气，每枪各允许
#: `HTTP_TIMEOUT_SECONDS`。把「单请求预算」直接当「整段预算」会让第二枪永远挤不进同一段
#: 3s（实测 geocoding 0.72s + forecast 1.04~2.5s，网络抖动即判 `slow`）。
#: 故整段 = 两枪 + 2s 余量（cache 读写、容器网络抖动）。
TOOL_TIMEOUT_SECONDS: Final[float] = HTTP_TIMEOUT_SECONDS * 2 + 2.0

#: 未识别异常的兜底细分码（不外泄内部细节）
DETAIL_INTERNAL: Final[str] = "internal"


class ToolResult(BaseModel):
    """get_weather 的出参信封（即结构化输出 schema 的根）。

    ``data`` 成功时装载 provider 快照（city/temp/condition/observed_at 限集）；
    失败时 ``data=None`` 且 ``error_code/detail/retryable`` 给出稳定状态；
    ``error_code`` 取值同 provider 三族大类（param_error/unavailable/transient）。
    字段集即契约，``extra="forbid"`` 防上游多字段混入。
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    detail: str | None = None
    retryable: bool = False
    #: True = 这份 data 是「provider 失败时的兜底缓存」，不是刚查到的实况
    #: （FR-CON-02 ②）。对话侧据此把 observed_at 说成「这是几点查到的」，
    #: 不许把旧数据当今天的实况播报；TTL 内的正常命中恒为 False。
    stale: bool = False


def _envelope(err: WeatherError) -> ToolResult:
    """WeatherError → 信封：大类/细分/可重试位与 provider 一一对应。"""
    return ToolResult(
        ok=False,
        error_code=err.error_code,
        detail=err.detail,
        retryable=err.retryable,
    )


class _WeatherState:
    """工具依赖注入点：测试拖入 provider/cache 替身；生产惰性按 Settings 组装。

    首次使用才走 ``get_settings()``——import 本模块绝不读 .env（``build_server``
    就是给测试的透明工厂：不传参时行为与生产完全一致）。
    """

    def __init__(self, provider: WeatherProvider | None, cache: WeatherCache | None) -> None:
        self._provider_injected = provider
        self._cache_injected = cache
        self._provider: WeatherProvider | None = provider
        self._cache: WeatherCache | None = cache

    @property
    def provider(self) -> WeatherProvider:
        if self._provider is None:
            self._provider = WeatherProvider.from_settings()
        return self._provider

    @property
    def cache(self) -> WeatherCache:
        if self._cache is None:
            self._cache = WeatherCache.from_settings()
        return self._cache

    async def aclose(self) -> None:
        """关停路径：provider 自建 http client（如有）交还。"""
        provider, self._provider = self._provider, None
        if isinstance(provider, WeatherProvider):
            await provider.aclose()


async def _resolve(state: _WeatherState, cleaned_city: str) -> tuple[dict[str, Any], bool]:
    """cache → miss → provider → 回填 cache。命中返回 (快照, True)。

    命中的是 **TTL 内**（10 分钟）的快照：它是「刚查过」而不是「查失败了」，
    所以调用方不要把命中记成 stale；stale 只由 :func:`_stale_fallback` 产生。
    """
    cached = await state.cache.get(cleaned_city)
    if cached is not None:
        return cached, True
    snapshot = await state.provider.fetch(cleaned_city)
    await state.cache.put(cleaned_city, snapshot)
    return snapshot, False


async def _stale_fallback(state: _WeatherState, cleaned_city: str) -> ToolResult | None:
    """provider 失败后的兜底：最近一次成功快照 + ``stale=True``（FR-CON-02 ②）。

    只认 ``STALE_SNAPSHOT_TTL_SECONDS``（24h）内的副本：跨天的旧天气报出来也是
    误导，宁可走「查不到」（③）。缓存层永不抛，所以这里不需要再包 try。
    """
    snapshot = await state.cache.get_stale(cleaned_city)
    if snapshot is None:
        return None
    logger.info(
        "get_weather: provider failed, serving stale snapshot (observed_at=%s)",
        snapshot.get("observed_at"),
    )
    return ToolResult(ok=True, data=snapshot, stale=True)


async def get_weather_impl(city: str, state: _WeatherState) -> ToolResult:
    """``get_weather`` 的纯函数本体（无 SDK 依赖，测试可直接调用）。"""
    try:
        cleaned = normalize_city(city)
    except WeatherError as exc:
        return _envelope(exc)
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            snapshot, _ = await _resolve(state, cleaned)
    except TimeoutError:
        logger.warning(
            "get_weather: tool-level budget (%ss) exceeded -> slow", TOOL_TIMEOUT_SECONDS
        )
        # 超时同样先试兜底：长者要的是「昨天的天气也比一句查不到强」，
        # 但 stale=True 让对话侧必须说出「这是几点查到的」。
        stale = await _stale_fallback(state, cleaned)
        if stale is not None:
            return stale
        return ToolResult(ok=False, error_code=CODE_TRANSIENT, detail=DETAIL_SLOW, retryable=True)
    except WeatherError as exc:
        # 参数类失败（城市空/查无此地）与外部失败共用同一兜底：缓存里真有这个
        # 城市的旧快照就报旧快照，没有就原样回错误信封。
        stale = await _stale_fallback(state, cleaned)
        if stale is not None:
            return stale
        return _envelope(exc)
    except Exception as exc:
        logger.exception("get_weather: unexpected failure (masked as transient/internal)")
        logger.debug("get_weather: unexpected failure type: %s (%s)", type(exc).__name__, exc)
        stale = await _stale_fallback(state, cleaned)
        if stale is not None:
            return stale
        return ToolResult(
            ok=False, error_code=CODE_TRANSIENT, detail=DETAIL_INTERNAL, retryable=False
        )
    return ToolResult(ok=True, data=snapshot)


def build_server(
    provider: WeatherProvider | None = None,
    cache: WeatherCache | None = None,
    settings: Settings | None = None,
) -> MCPServer:
    """装配天气 MCP server（命名/注解与 schema 全部显式声明）。

    ``provider/cache`` 缺省 = 生产惰性路径；tests 传 替身 即用。
    实现选择 mcp 2.x 的 ``mcp.server.mcpserver.MCPServer``
    （FastMCP 在 2.x 已更名，本模块即按该 SDK 写）。
    """
    del settings  # 预留：如需把覆盖值透传给 provider/cache 再打开
    state = _WeatherState(provider, cache)
    weather = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        description="Anyu 结伴天气工具（唯一真实外呼工具）",
        instructions="get Weather 入参为城市名（≤40 字符）；出参为 ToolResult 信封"
        "（ok/data/error_code/detail/retryable），失败时按 error_code 分类处置。",
    )

    @weather.tool(
        name="get_weather",
        title="Get Weather",
        description="查城市当前天气：成功 {ok,data{city,temp,condition,observed_at},stale}，observed_at 是数据时间（ISO，带 +08:00，精度到分钟）；stale=True 表示数据来自失败兜底缓存（须说明是几点查到的、可能不是最新），失败 {ok=False,error_code,detail,retryable}",
        annotations=ToolAnnotations(
            title="Get Weather",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_weather(city: str) -> ToolResult:
        return await get_weather_impl(city, state)

    return weather


def _http_target(settings: Settings) -> tuple[str, int, str]:
    """由 ``mcp_weather_server_url`` 解析 (host, port, path)（path 缺省 → /mcp）。"""
    url = urllib.parse.urlsplit(settings.mcp_weather_server_url)
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    return url.hostname or "127.0.0.1", port, url.path or "/mcp"


def _allowed_hosts(host: str, port: int) -> list[str]:
    """streamable-http 的 Host 白名单（防 DNS rebinding）。

    SDK 默认只放行 ``127.0.0.1`` 与 ``localhost``，容器内被 backend 用服务名
    ``weather-mcp:7101`` 访问时会直接回 **421 Misdirected Request**——这正是
    迭代 11 编排接线现场踩到的坑（工具层因此永远判不可达）。这里按「绑定地址 +
    本机 + 服务名」三类放行，并允许用 ``MCP_ALLOWED_HOSTS`` 追加（逗号分隔，
    部署时换成真实域名即可）。
    """
    hosts = {f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"}
    hosts.add(f"{SERVER_SERVICE_NAME}:{port}")
    extra = os.environ.get("MCP_ALLOWED_HOSTS", "")
    hosts.update(item.strip() for item in extra.split(",") if item.strip())
    return sorted(hosts)


def _serve_http() -> int:
    """streamable-http 形态：按配置 URL 绑定 host:port（预留 W3 集成的部署入口）。"""
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
        "weather mcp server: streamable-http on %s:%s%s (allowed_hosts=%s)",
        host,
        port,
        path,
        ",".join(allowed),
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：``python -m mcp-servers.weather.server``（stdio，缺省）或 `[http]`。"""
    args = list(sys.argv[1:] if argv is None else argv)
    transport = args[0].lower() if args and args[0].lower() in ("stdio", "http") else "stdio"
    if transport == "stdio":
        # `MCPServer.run()` 是**同步阻塞**入口（内部走 anyio.run），不是协程：
        # 之前 `asyncio.run(server.run())` 会先同步跑完整个 server，再把返回的 None
        # 交给 asyncio.run 抛 ValueError。这里直接调同步入口。
        server.run("stdio")
    else:
        return _serve_http()
    return 0


#: 生产默认实例（stdio 直接跑；HTTP 复用同一工具面）
server: MCPServer = build_server()


if __name__ == "__main__":
    raise SystemExit(main())
