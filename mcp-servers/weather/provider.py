"""天气数据适配层（迭代 6 · W5-half，唯一真实外部接口的 provider）。

职责（设计记录（三级路由） W3 / W5-half 交付口径）：

1. **选源矩阵**：按 `settings.weather_api_provider` 选真实上游 ——
   `""`/`"open-meteo"` → Open-Meteo（免 key，计划 §8 D6-Q1 的「免密钥源」口径；
   `settings.weather_api_key` 为空不影响）；
   `"openweather"`/`"openweathermap"` → OpenWeather（必须带 key，缺失即弃权）；
   其他取值 → 未知供应商，直接 `WeatherUnavailable('unknown_provider')`。
2. **失败分类**（§8 失败四分类中本层负责的三类；「越权」由 mcp_client 层处置）：
   - 参数错：城市空白 / 超长（>40 字符，按「截断垃圾输入」口径拒收）/ 上游明确查无此城
     → `WeatherParamError`（不可重试；
   - 目标不可用：key 缺失（`no_api_key`）、未知供应商、上游 401/403、其余 4xx
     → `WeatherUnavailable`（不可重试）；
   - 临时错误：3s 未返回（httpx 超时，`slow`）、网络错误（`network`）、5xx
     （`http_server_error`）、429（`rate_limited`）、坏载荷（`bad_payload`）
     → `WeatherTransient`（可重试）。
   `exception.error_code` + `detail` 是稳定状态枚举；**信息性话术**（如「查不到，
   我们稍后再试」「天气查询有点慢」）由调用方（mcp_client / Agent 上层）生成，本层
   只给状态、不吐长文案、不回显上游 URL / 响应体。
3. **脱敏**：只产出 `{"city", "temp", "condition", "observed_at"}` 四个键（键集合即
   契约），温度四舍五入到一位小数；对上游响应多余字段（湿度、风速、地标经纬度等）
   **取完即弃**。`observed_at` 是**数据时间**（TD:176「天气数据+数据时间」），带
   `+08:00` 偏移、精度到分钟，供 Reply 层如实说出「几点几分的数据」。

网络红线：请求目标**只限**选源矩阵内的固定 API host（或运营方在
`settings.weather_api_base_url` 显式覆写的自托管镜像）——模型/调用方永远不能注入 URL。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final
from zoneinfo import ZoneInfo

import httpx

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

#: 数据时间的展示时区。AGENTS §8：存储用 UTC、展示用 Asia/Shanghai；天气快照不落库、
#: 只活在工具结果与回复里，所以直接按展示时区出带偏移的 ISO 串，省掉下游再换算。
_DISPLAY_TZ: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")

#: 数据时间格式：精确到分钟，带 `±HH:MM` 偏移（无偏移的裸时间是歧义源，一律拒收）。
_OBSERVED_AT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}"
)

# ------------------------------------------------------------------ 量纲与档位

#: 城市名上限（W5-half 口径）：超过按截断垃圾输入拒收（ParamError）。
CITY_MAX_CHARS: Final[int] = 40

#: provider 单请求预算（连接/读/写各档都按 3s 封顶）；server 工具层另有整段 3s
#: 总预算兜底（mcp-servers/weather/server.py::TOOL_TIMEOUT_SECONDS）。三层加起来
#: 最坏情况不会长过工具层整体 3s。
HTTP_TIMEOUT_SECONDS: Final[float] = 3.0

# ------------------------------------------------------------------ 错误分类码

#: error_code（工具信封 ToolResult.error_code 同名透传）
CODE_PARAM_ERROR: Final[str] = "param_error"
CODE_UNAVAILABLE: Final[str] = "unavailable"
CODE_TRANSIENT: Final[str] = "transient"

# ------------------------------------------------------------------ 细分码（detail）

#: 城市名为空白 / 非字符串
DETAIL_BLANK: Final[str] = "city_blank"
#: 城市名超长（> CITY_MAX_CHARS，按「截断垃圾输入」拒收）
DETAIL_TOO_LONG: Final[str] = "city_too_long"
#: 上游明确表示查无此城（geocoding 空结果 / 404）
DETAIL_CITY_NOT_FOUND: Final[str] = "city_not_found"
#: 选了必须带 key 的源但 `weather_api_key` 为空
DETAIL_NO_API_KEY: Final[str] = "no_api_key"
#: `weather_api_provider` 取值不在选源矩阵内
DETAIL_UNKNOWN_PROVIDER: Final[str] = "unknown_provider"
#: 上游 401/403（key 无效或被拒）
DETAIL_AUTH_REJECTED: Final[str] = "auth_rejected"
#: 上游其它 4xx（非 404，归属不可重试的兼容/配置问题）
DETAIL_HTTP_CLIENT_ERROR: Final[str] = "http_client_error"
#: 预算内未返回（3s 慢）
DETAIL_SLOW: Final[str] = "slow"
#: 连接/传输失败（ConnectError、ReadError 等）
DETAIL_NETWORK: Final[str] = "network"
#: 上游 5xx
DETAIL_SERVER_ERROR: Final[str] = "http_server_error"
#: 上游 429
DETAIL_RATE_LIMITED: Final[str] = "rate_limited"
#: 返回 200 但载荷形状对不上（坏 JSON / 缺字段）
DETAIL_BAD_PAYLOAD: Final[str] = "bad_payload"

# ------------------------------------------------------------------ 异常族


class WeatherError(Exception):
    """天气域错误基类：`error_code`（四分类大类）+ `detail`（细分状态）+ `retryable`。

    子类只改类级默认；`detail` 是必填细分状态码（见上方 DETAIL_* 常量）。
    消息体只有状态码，不携带上游响应内容（红线：错误不回显内部细节）。
    """

    error_code: ClassVar[str] = CODE_TRANSIENT
    retryable: ClassVar[bool] = False

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.error_code}:{detail}")

    def __str__(self) -> str:
        return f"{self.error_code}({self.detail})"


class WeatherParamError(WeatherError):
    """参数错：城市空白/超长/查无此城。调用方应修正参数而不是原样重试。"""

    error_code = CODE_PARAM_ERROR
    retryable = False


class WeatherUnavailable(WeatherError):
    """目标不可用：key 缺失、未知供应商、上游明确拒绝。与瞬时故障区分（不重试）。"""

    error_code = CODE_UNAVAILABLE
    retryable = False


class WeatherTransient(WeatherError):
    """临时错误：慢/断流/5xx/429/坏载荷。调用方按 ≤2 次退避重试后降级话术。"""

    error_code = CODE_TRANSIENT
    retryable = True


# ------------------------------------------------------------------ 工具函数


def normalize_city(city: str) -> str:
    """城市名归一与强校验：剥首尾空白（含全角空格），空白/超长即参数错。

    SDK 入参层已 schema 校验过 `city: str`；直接调用方若传非字符串也在此拦下。
    返回值同时作为缓存键归一输入（大小写折叠在 cache.key_for 内完成）。
    """
    if not isinstance(city, str):
        raise WeatherParamError(DETAIL_BLANK)
    cleaned = city.strip()
    if not cleaned:
        raise WeatherParamError(DETAIL_BLANK)
    if len(cleaned) > CITY_MAX_CHARS:
        raise WeatherParamError(DETAIL_TOO_LONG)
    return cleaned


def _clean_text(value: Any) -> str | None:
    """只接受非空白字符串，其余一律视为「没有」（防上游字段漂移串型）。"""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


#: WMO weather code → 播报用天气短语（open-meteo `current.weather_code`）。未知码兜底。
_WMO_CONDITION: Final[dict[int, str]] = {
    0: "晴",
    1: "晴转多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小雨",
    53: "小雨",
    55: "小雨",
    56: "冻雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "霰",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "阵雪",
    95: "雷阵雨",
    96: "雷阵雨并伴冰雹",
    99: "雷阵雨并伴冰雹",
}


def _wmo_condition(code: int) -> str:
    return _WMO_CONDITION.get(code) or "未知天气"


def _observed_at_from_local(raw: str | None) -> str:
    """上游给的本地墙钟时间（Open-Meteo `current.time`，已按时区偏移换算）→ ISO 带偏移。

    上游返回的是 `2026-11-10T15:00` 这种不带偏移的串，含义是「该城市当地的 15:00」。
    我们请求时已用 `timezone=Asia/Shanghai` 让上游按这个时区出值，所以补上 `+08:00`
    即可，不做二次换算（二次换算会把 15:00 变成 07:00，那是另一种错）。
    """
    if not raw:
        raise WeatherTransient(DETAIL_BAD_PAYLOAD)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise WeatherTransient(DETAIL_BAD_PAYLOAD) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_DISPLAY_TZ)
    return parsed.isoformat(timespec="minutes")


def _observed_at_from_epoch(value: object) -> str:
    """OpenWeather 的 `dt`（UTC 秒）→ 展示时区的 ISO 带偏移。"""
    try:
        epoch = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError) as exc:
        raise WeatherTransient(DETAIL_BAD_PAYLOAD) from exc
    return datetime.fromtimestamp(epoch, tz=_DISPLAY_TZ).isoformat(timespec="minutes")


# ------------------------------------------------------------------ 选源矩阵


class WeatherProvider:
    """真实天气源基类：请求收口（超时/网络/状态分类）+ 脱敏快照成型。

    测试不打真网：`client=` 注入 `httpx.AsyncClient(transport=httpx.MockTransport(...))`；
    生产留空时进程内懒建一次 `httpx.AsyncClient`（`trust_env=False`，防止代理
    环境变量把请求改道到任意第三方 —— 「禁止自由外网访问」的最后一道也是兜底的一道闸）。
    """

    #: 是否强制要求 API key。open-meteo 免 key；openweather 必须。
    requires_key: ClassVar[bool] = True

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        from app.core.config import get_settings

        self._settings: Settings = settings if settings is not None else get_settings()
        self._client = client
        self._owns_client = client is None
        raw_base = base_url if base_url is not None else self._settings.weather_api_base_url
        self._base_url = (raw_base or "").strip().rstrip("/")

    # ---- 生命周期 ---------------------------------------------------------

    async def aclose(self) -> None:
        """关闭本实例自建（非注入）的 http client；注入的由注入方负责。"""
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS),
                trust_env=False,
            )
        return self._client

    # ---- 主入口 -----------------------------------------------------------

    async def fetch(self, city: str) -> dict[str, Any]:
        """按城市取当前天气，返回脱敏快照 `{"city","temp","condition"}`。

        任何失败都以 `WeatherError` 三族异常表达 —— 调用方按
        `error_code / detail / retryable` 转成工具信封，不吞异常、不编造结果。
        """
        cleaned = normalize_city(city)
        self._ensure_ready()
        return await self._fetch_clean(cleaned)

    def _ensure_ready(self) -> None:
        """发请求前能确定 fail-fast 的配置问题（key 缺失）。"""
        if self.requires_key and not self._settings.weather_api_key.strip():
            logger.debug(
                "weather provider: %s requires api_key but config is empty", type(self).__name__
            )
            raise WeatherUnavailable(DETAIL_NO_API_KEY)

    # ---- 请求收口 ---------------------------------------------------------

    async def _request_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        """一次上游调用：超时/网络/状态码/JSON 全部收口成三族异常。"""
        try:
            resp = await self._get_client().get(url, params=params)
        except httpx.TimeoutException as exc:
            raise WeatherTransient(DETAIL_SLOW) from exc
        except httpx.HTTPError as exc:
            raise WeatherTransient(DETAIL_NETWORK) from exc
        self._raise_for_status(resp.status_code)
        try:
            data = resp.json()
        except ValueError as exc:
            raise WeatherTransient(DETAIL_BAD_PAYLOAD) from exc
        if not isinstance(data, dict):
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        return data

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        """HTTP 状态 → 三族异常（5xx/429 可重试；401/403/其余 4xx 不可重试）。"""
        if status_code < 400:
            return
        if status_code >= 500:
            raise WeatherTransient(DETAIL_SERVER_ERROR)
        if status_code == 429:
            raise WeatherTransient(DETAIL_RATE_LIMITED)
        if status_code == 404:
            raise WeatherParamError(DETAIL_CITY_NOT_FOUND)
        if status_code in (401, 403):
            raise WeatherUnavailable(DETAIL_AUTH_REJECTED)
        raise WeatherUnavailable(DETAIL_HTTP_CLIENT_ERROR)

    # ---- 脱敏 -----------------------------------------------------------

    _SNAPSHOT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"city", "temp", "condition", "observed_at"}
    )

    @staticmethod
    def _snapshot(city: str, temp: float, condition: str, observed_at: str) -> dict[str, Any]:
        """成型即限键：除 city/temp/condition/observed_at 之外一个字段都不放出来。

        `observed_at` 必须是 `YYYY-MM-DDTHH:MM±HH:MM`（数据时间，精度到分钟）；
        不合规即按坏载荷处理——宁可整体失败，也不让一个没法读的时刻蒙混进回复。
        """
        if not condition.strip():
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        if _OBSERVED_AT_PATTERN.fullmatch(observed_at) is None:
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        return {
            "city": city,
            "temp": round(temp, 1),
            "condition": condition.strip(),
            "observed_at": observed_at,
        }

    # ---- 子类契约 ---------------------------------------------------------

    async def _fetch_clean(self, cleaned: str) -> dict[str, Any]:
        """已归一城市名进入的真实源取数子流程。"""
        raise NotImplementedError

    # ---- 工厂 ------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> WeatherProvider:
        """选源矩阵：`weather_api_provider`（大小写/首尾空白不敏感）→ 具体实现。

        未知取值宁可与整条工具链一起降级（Unavailable），也不悄悄换源。
        所选来源打一条 info 日志，供验收报告引用（"报告写明所选源"）。
        """
        from app.core.config import get_settings

        cfg = settings if settings is not None else get_settings()
        name = cfg.weather_api_provider.strip().lower()
        if name in ("openweather", "openweathermap"):
            chosen: type[WeatherProvider] = OpenWeatherProvider
        elif name in ("", "open-meteo"):
            chosen = OpenMeteoProvider
        else:
            raise WeatherUnavailable(DETAIL_UNKNOWN_PROVIDER)
        logger.info("weather provider selected: %s (source matrix)", chosen.__name__)
        return chosen(settings=cfg)


class OpenMeteoProvider(WeatherProvider):
    """Open-Meteo（免 key 默认源）：geocoding 查城市 → forecast 取当前天气。

    默认双 host（地理编码 + 预报）；`settings.weather_api_base_url` 非空时按
    「单镜像 host 同时服务两段路径」的口径整体覆写（测试镜像/自托管网关用）。
    """

    requires_key = False

    _GEO_PATH: ClassVar[str] = "/v1/search"
    _FORECAST_PATH: ClassVar[str] = "/v1/forecast"
    _DEFAULT_GEO_BASE: ClassVar[str] = "https://geocoding-api.open-meteo.com"
    _DEFAULT_API_BASE: ClassVar[str] = "https://api.open-meteo.com"

    def _split_bases(self) -> tuple[str, str]:
        if self._base_url:
            return self._base_url, self._base_url
        return self._DEFAULT_GEO_BASE, self._DEFAULT_API_BASE

    async def _fetch_clean(self, cleaned: str) -> dict[str, Any]:
        geo_base, api_base = self._split_bases()

        geo = await self._request_json(
            f"{geo_base}{self._GEO_PATH}",
            params={"name": cleaned, "count": "1", "language": "zh", "format": "json"},
        )
        results = geo.get("results")
        if not isinstance(results, list) or not results:
            raise WeatherParamError(DETAIL_CITY_NOT_FOUND)
        hit = results[0]
        if not isinstance(hit, dict):
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        try:
            latitude = float(hit["latitude"])
            longitude = float(hit["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherTransient(DETAIL_BAD_PAYLOAD) from exc
        echo = _clean_text(hit.get("name")) or cleaned

        forecast = await self._request_json(
            f"{api_base}{self._FORECAST_PATH}",
            params={
                "latitude": repr(latitude),
                "longitude": repr(longitude),
                "current": "temperature_2m,weather_code",
                "timezone": "Asia/Shanghai",
            },
        )
        current = forecast.get("current")
        if not isinstance(current, dict):
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        try:
            temp = float(current["temperature_2m"])
            weather_code = int(current["weather_code"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherTransient(DETAIL_BAD_PAYLOAD) from exc
        observed_at = _observed_at_from_local(_clean_text(current.get("time")))
        return self._snapshot(echo, temp, _wmo_condition(weather_code), observed_at)


class OpenWeatherProvider(WeatherProvider):
    """OpenWeather（需 key 源）：单调用 `current weather`，key 走 appid 参数。"""

    requires_key = True

    _DEFAULT_API_BASE: ClassVar[str] = "https://api.openweathermap.org"
    _CURRENT_PATH: ClassVar[str] = "/data/2.5/weather"

    async def _fetch_clean(self, cleaned: str) -> dict[str, Any]:
        base = self._base_url or self._DEFAULT_API_BASE
        data = await self._request_json(
            f"{base}{self._CURRENT_PATH}",
            params={
                "q": cleaned,
                "appid": self._settings.weather_api_key,
                "units": "metric",
                "lang": "zh_cn",
            },
        )
        conditions = data.get("weather")
        if (
            not isinstance(conditions, list)
            or not conditions
            or not isinstance(conditions[0], dict)
        ):
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        condition = _clean_text(conditions[0].get("description"))
        if condition is None:
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        main = data.get("main")
        if not isinstance(main, dict):
            raise WeatherTransient(DETAIL_BAD_PAYLOAD)
        try:
            temp = float(main["temp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherTransient(DETAIL_BAD_PAYLOAD) from exc
        echo = _clean_text(data.get("name")) or cleaned
        observed_at = _observed_at_from_epoch(data.get("dt"))
        return self._snapshot(echo, temp, condition, observed_at)


__all__ = [
    "CITY_MAX_CHARS",
    "HTTP_TIMEOUT_SECONDS",
    "OpenMeteoProvider",
    "OpenWeatherProvider",
    "WeatherError",
    "WeatherParamError",
    "WeatherProvider",
    "WeatherTransient",
    "WeatherUnavailable",
    "normalize_city",
]
