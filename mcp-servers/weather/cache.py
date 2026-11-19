"""天气 Redis 缓存层（迭代 6 · W5-half，mcp-servers/weather）。

缓存失败不致命（同样走 §8 失败分类的收口）：
- redis 包缺失 / 连接失败 / 单次操作失败（拒绝、超时、断链）→ **静默退化 no-op**，
  只打 `logger.debug`（每事件一行），绝不向上抛 —— 结果好坏永远由 provider 决定；
- get 拿到坏载荷（坏 JSON / 缺三键）按「未命中」处置，让 provider 重取有效数据。

键口径与 `app/services/dialogue_service.py:67` 的 ``{prefix}:dialogue:{thread_id}``
同源：``{redis_key_prefix}:weather:{city}``（城市名 strip + casefold 归一，
「Shanghai」与「shanghai」同键）。TTL 取 ``settings.weather_cache_ttl_seconds``（600s）。
值 JSON 序列化，只存 provider 快照三键 ``{city,temp,condition}``（限集，防多余字段回流）。

测试不打真 Redis：``client=`` 显式注入最小假客户端（get/set/delete + ex TTL 语义，
见 ``tests/test_weather_server.py::FakeRedisClient``）；fakeredis 未安装，无需探测。
生产路径（client 缺省）在首次使用时才惰性建 redis 客户端、才触发 get_settings()
（避免 import 副作用，与本包其余模块同一口径）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar, Final

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

CACHE_SEGMENT: Final[str] = "weather"

#: 惰性 redis 客户端的超时护栏：redis 不可用时单次操作最多拖 0.5s
#: （工具层整段 3s 预算的兜底口径），失败即退化、不重试。
REDIS_CONNECT_TIMEOUT_SECONDS: Final[float] = 0.5
REDIS_OP_TIMEOUT_SECONDS: Final[float] = 2.0

#: 「最近一次成功快照」副本的 TTL（FR-CON-02 ②，失败兜底专用）。
#: 主键只有 10 分钟 TTL，provider 一挂就没有任何可回的话；副本留 24h，
#: 但硬上限就是一天：跨天的旧天气本来就不该冒充「今天的实况」，宁可说查不到。
STALE_SNAPSHOT_TTL_SECONDS: Final[int] = 86_400


class WeatherCache:
    """键空间 ``{prefix}:weather:{city}`` 的 get/put/invalidate + 失败无害化。

    ``client=`` 注入测试替身（或替代实现）；缺省时延迟加载
    ``redis.asyncio.Redis.from_url(settings.redis_url, decode_responses=True)``，
    首次构建失败只置一次性失败旗，不再反复尝试（每事件仍 debug 一行）。
    """

    #: 快照合法载荷的键集（值域校验用；与 provider 限集保持一致）
    _SNAPSHOT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"city", "temp", "condition", "observed_at"}
    )

    def __init__(
        self,
        client: Any | None = None,
        *,
        settings: Settings | None = None,
        key_prefix: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._settings = settings
        self._key_prefix_override = key_prefix
        self._ttl_override = ttl_seconds
        self._explicit_client: Any | None = client
        self._lazy_client: Any | None = None
        self._factory_failed = False
        self._cfg_ready = False
        self._prefix = ""
        self._ttl = 0

    # ---------------------------------------------------------------- 配置解析

    def _config(self) -> tuple[str, int]:
        """惰性解析 (键前缀, TTL)：显式 override > Settings（settings 缺省时才读 env）。"""
        if self._cfg_ready:
            return self._prefix, self._ttl
        if self._settings is None:
            from app.core.config import get_settings

            self._settings = get_settings()
        prefix = (
            self._key_prefix_override
            if self._key_prefix_override is not None
            else self._settings.redis_key_prefix
        )
        ttl = (
            self._ttl_override
            if self._ttl_override is not None
            else self._settings.weather_cache_ttl_seconds
        )
        self._prefix = f"{prefix}:{CACHE_SEGMENT}:"
        self._ttl = int(ttl)
        self._cfg_ready = True
        return self._prefix, self._ttl

    def key_for(self, city: str) -> str:
        """缓存键：``{prefix}:weather:{city}``（城市 strip+casefold，与写入侧同源）。"""
        prefix, _ = self._config()
        return f"{prefix}{city.strip().casefold()}"

    def stale_key_for(self, city: str) -> str:
        """失败兜底副本的键：``{prefix}:weather:last:{city}``（城市同一套归一）。"""
        prefix, _ = self._config()
        return f"{prefix}last:{city.strip().casefold()}"

    # ---------------------------------------------------------------- 客户端解析

    def _usable_client(self) -> Any | None:
        """注入优先；缺省时惰性建生产客户端，构建失败置一次性失败旗。"""
        if self._explicit_client is not None:
            return self._explicit_client
        if self._factory_failed:
            return None
        if self._lazy_client is None:
            self._config()
            self._lazy_client = self._build_default_client()
            if self._lazy_client is None:
                self._factory_failed = True
        return self._lazy_client

    def _build_default_client(self) -> Any | None:
        """经典 redis 异步客户端：坏环境（包缺失/URL 不可解）→ None，交上层降级。"""
        # 先确保 Settings 已解析（`_config()` 由 `_cfg_ready` 缓存，重复调用零成本）；
        # 调用方 `_usable_client()` 在进来之前已经调过一次，这里只是把「非 None」写进类型。
        self._config()
        settings = self._settings
        if settings is None:  # pragma: no cover - `_config()` 之后不可能为 None
            return None
        try:
            import redis.asyncio as redis_asyncio
        except Exception as exc:
            logger.debug("weather cache: redis package unavailable, degrade no-op(%s)", exc)
            return None
        try:
            from redis.exceptions import RedisError
        except Exception as exc:
            logger.debug("weather cache: redis exceptions module unavailable(%s)", exc)
            return None
        try:
            client = redis_asyncio.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
                socket_timeout=REDIS_OP_TIMEOUT_SECONDS,
            )
        except RedisError as exc:
            logger.debug("weather cache: redis client build failed, degrade no-op(%s)", exc)
            return None
        except ValueError as exc:
            logger.debug("weather cache: redis url invalid, degrade no-op(%s)", exc)
            return None
        return client

    # ---------------------------------------------------------------- 三操作

    async def get(self, city: str) -> dict[str, Any] | None:
        """命中返回三键快照；未命中/缓存不可用/坏载荷一律返回 None。"""
        client = self._usable_client()
        key = self.key_for(city)
        if client is None:
            logger.debug("weather cache: %s cache disabled, treat as miss", key)
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:
            logger.debug(
                "weather cache: get %s failed, degrade to miss(%s: %s)",
                key,
                type(exc).__name__,
                exc,
            )
            return None
        return self._decode(raw)

    async def put(self, city: str, snapshot: dict[str, Any]) -> None:
        """以 TTL=``weather_cache_ttl_seconds`` 写入约定的 json 键；不可用时 no-op。"""
        client = self._usable_client()
        if client is None:
            logger.debug("weather cache: put skipped (cache disabled)")
            return
        if not isinstance(snapshot, dict):
            logger.debug("weather cache: put skipped (payload not dict)")
            return
        key = self.key_for(city)
        try:
            _, ttl = self._config()
            payload = json.dumps(snapshot, ensure_ascii=False)
            await client.set(key, payload, ex=ttl)
        except Exception as exc:
            logger.debug(
                "weather cache: put %s failed, ignored(%s: %s)",
                key,
                type(exc).__name__,
                exc,
            )
            return
        # 同一份快照再存一份长 TTL 副本（`stale_key_for`）：主键过期/被淘汰之后，
        # provider 一旦失败仍能报出「这是几点查到的」，而不是让长者在坏天气时
        # 彻底听不到话。副本单独兜异常：写失败不能否定主键已写入的事实。
        try:
            await client.set(
                self.stale_key_for(city),
                payload,
                ex=STALE_SNAPSHOT_TTL_SECONDS,
            )
        except Exception as exc:
            logger.debug(
                "weather cache: put stale copy %s failed, ignored(%s: %s)",
                key,
                type(exc).__name__,
                exc,
            )

    async def get_stale(self, city: str) -> dict[str, Any] | None:
        """provider 失败时的兜底读取（``stale_key_for`` 的长 TTL 副本）。

        语义与 :meth:`get` 完全一致：未命中 / 缓存不可用 / 坏载荷一律 None，
        **绝不抛** —— 兜底路径自己再抛，就等于把「查不到」升级成「对话崩了」。
        """
        client = self._usable_client()
        key = self.stale_key_for(city)
        if client is None:
            logger.debug("weather cache: %s stale cache disabled, treat as miss", key)
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:
            logger.debug(
                "weather cache: get stale %s failed, degrade to miss(%s: %s)",
                key,
                type(exc).__name__,
                exc,
            )
            return None
        return self._decode(raw)

    async def invalidate(self, city: str) -> None:
        """两个键一起删；未命中 / 不可用一律静默（no-op 语义，不暴露差异）。

        兜底副本必须跟着删：invalidate 的唯一用法是「这条数据已经确定不对了」，
        只删主键会让下一次 provider 失败又把同一条错数据端给长者。
        """
        client = self._usable_client()
        key = self.key_for(city)
        if client is None:
            logger.debug("weather cache: %s invalidate skipped (cache disabled)", key)
            return
        for target in (key, self.stale_key_for(city)):
            try:
                await client.delete(target)
            except Exception as exc:
                logger.debug(
                    "weather cache: invalidate %s failed, ignored(%s: %s)",
                    target,
                    type(exc).__name__,
                    exc,
                )

    # ---------------------------------------------------------------- 内部

    def _decode(self, raw: Any) -> dict[str, Any] | None:
        """载荷防线：坏 JSON / 非字典 / 缺三键 → 一律按未命中处置。"""
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.debug("weather cache: payload not json, treat as miss(%s)", exc)
            return None
        if not isinstance(payload, dict) or not payload.keys() >= self._SNAPSHOT_KEYS:
            logger.debug("weather cache: payload mismatch, treat as miss")
            return None
        return payload

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
    ) -> WeatherCache:
        """生产入口：`WeatherCache.from_settings()`（键/TTL 由 Settings 推导）。"""
        return cls(client, settings=settings)


__all__ = [
    "CACHE_SEGMENT",
    "REDIS_CONNECT_TIMEOUT_SECONDS",
    "REDIS_OP_TIMEOUT_SECONDS",
    "STALE_SNAPSHOT_TTL_SECONDS",
    "WeatherCache",
]
