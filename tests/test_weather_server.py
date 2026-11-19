"""天气 MCP 服务器单测（迭代 6 · W5-half 验收面，mcp-servers/weather）。

全程不打真网、不打真 Redis：
- provider HTTP 全部经 ``httpx.MockTransport`` 注入（慢速场景用超预算延迟，用例内把
  整段预算临时收窄到 0.5s，避免真睡 8 秒）；
- Redis 一律注入 ``FakeRedisClient`` 替身（get/set/delete + ex TTL 语义 + fail 开关
  + 时钟注入）；redis 包缺失的惰性退化路径用 ``sys.modules`` 探针；
- e2e 用 ``build_server()`` 实例化 ``MCPServer`` 后直接 ``call_tool``，断言结构化信封
  （ok / data / error_code / detail / retryable / stale 六键）。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from typing import Any

import httpx
import pytest

TOOL_NAME: str = "get_weather"

PROVIDER = importlib.import_module("mcp-servers.weather.provider")
CACHE = importlib.import_module("mcp-servers.weather.cache")
SERVER = importlib.import_module("mcp-servers.weather.server")

build_server = SERVER.build_server
WeatherCache = CACHE.WeatherCache

WeatherError = PROVIDER.WeatherError
WeatherParamError = PROVIDER.WeatherParamError
WeatherUnavailable = PROVIDER.WeatherUnavailable
WeatherTransient = PROVIDER.WeatherTransient
WeatherProvider = PROVIDER.WeatherProvider
OpenMeteoProvider = PROVIDER.OpenMeteoProvider
OpenWeatherProvider = PROVIDER.OpenWeatherProvider

_FAMILY: dict[str, type[WeatherError]] = {
    "param_error": WeatherParamError,
    "unavailable": WeatherUnavailable,
    "transient": WeatherTransient,
}

_SUNNY: dict[str, Any] = {
    "city": "上海",
    "temp": 28.4,
    "condition": "多云",
    "observed_at": "2026-11-10T15:00+08:00",
}


# ------------------------------------------------------------------ 替身


class _StubSettings:
    """无 .env 的最小配置替身（只暴露 provider/cache 消费的属性）。"""

    weather_api_provider = ""
    weather_api_base_url = ""
    weather_api_key = ""
    redis_key_prefix = ""
    redis_url = ""
    weather_cache_ttl_seconds = 600


def _settings(**overrides: Any) -> _StubSettings:
    return type("StubSettings", (_StubSettings,), overrides)()


class FakeRedisClient:
    """最小 fake redis 客户端：get/set/delete + ex TTL 语义 + fail 开关 + 时钟。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self._store: dict[str, str] = {}
        self._deadline: dict[str, float] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[str] = []
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("fake redis down")

    async def get(self, key: str) -> str | None:
        self.calls.append(f"GET {key}")
        self._check()
        if key not in self._store:
            return None
        deadline = self._deadline.get(key)
        if deadline is not None and self.now > deadline:
            self._store.pop(key, None)
            self._deadline.pop(key, None)
            return None
        return self._store[key]

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.calls.append(f"SET {key}")
        self._check()
        self.ttls[key] = int(ex) if ex is not None else 0
        self._store[key] = value
        self._deadline[key] = float("inf") if ex in (None, 0) else self.now + ex

    async def delete(self, key: str) -> None:
        self.calls.append(f"DELETE {key}")
        self._check()
        self._store.pop(key, None)
        self._deadline.pop(key, None)


class _StubProvider:
    """离线 provider 替身：记录 fetch 调用，返送快照 / 异常 / 延迟。"""

    def __init__(
        self,
        snapshot: dict[str, Any] | None = _SUNNY,
        exc: Exception | None = None,
        *,
        delay: float = 0.0,
    ) -> None:
        self._snapshot = snapshot
        self._exc = exc
        self._delay = delay
        self.fetched: list[str] = []

    async def fetch(self, city: str) -> dict[str, Any]:
        self.fetched.append(city)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        if self._snapshot is None:
            pytest.fail("_StubProvider 需要快照或异常至少其一")
        return dict(self._snapshot)


def _mock_client(handler: Any) -> httpx.AsyncClient:
    """给 provider 的测试 httpx 客户端：沿 provider 超时常量，禁代理环境改道。"""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(PROVIDER.HTTP_TIMEOUT_SECONDS),
        trust_env=False,
    )


def _status_handler(status: int) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "upstream controlled response"})

    return handler


def _landmine_client() -> httpx.AsyncClient:
    """保险栓：断言「不应触网」的路径一旦触网立即在测试里炸出来。"""

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected outbound request: {request.url}")

    return _mock_client(boom)


def _weather(cache: WeatherCache, provider: Any) -> Any:
    return build_server(provider=provider, cache=cache)


def _hint(annotation: Any, camel_key: str, snake_key: str) -> Any:
    """兼容 mcp_types 字段命名（camelCase 字段名/别名歧义），两个拼写都试。"""
    return getattr(annotation, camel_key, getattr(annotation, snake_key, None))


# ------------------------------------------------------------------ 工具信封（e2e）


class TestEnvelopeContract:
    """get_weather 结构化信封契约（六键：ok/data/error_code/detail/retryable/stale）。"""

    async def test_success_envelope(self):
        provider = _StubProvider()
        weather = _weather(WeatherCache(client=FakeRedisClient()), provider)
        result = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert result.structured_content == {
            "ok": True,
            "data": _SUNNY,
            "error_code": None,
            "detail": None,
            "retryable": False,
            "stale": False,
        }
        assert result.is_error is False
        assert provider.fetched == ["Shanghai"]

    async def test_blank_city_is_param_error(self):
        provider = _StubProvider()
        weather = _weather(WeatherCache(client=FakeRedisClient()), provider)
        result = await weather.call_tool(TOOL_NAME, {"city": "   "})
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "param_error",
            "detail": "city_blank",
            "retryable": False,
            "stale": False,
        }
        assert provider.fetched == []

    async def test_41chars_city_is_param_error(self):
        provider = _StubProvider()
        weather = _weather(WeatherCache(client=FakeRedisClient()), provider)
        result = await weather.call_tool(TOOL_NAME, {"city": "x" * 41})
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "param_error",
            "detail": "city_too_long",
            "retryable": False,
            "stale": False,
        }
        assert provider.fetched == []

    async def test_no_api_key_envelope(self):
        provider = OpenWeatherProvider(
            settings=_settings(weather_api_key=""), client=_landmine_client()
        )
        weather = _weather(WeatherCache(client=FakeRedisClient()), provider)
        result = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "unavailable",
            "detail": "no_api_key",
            "retryable": False,
            "stale": False,
        }

    async def test_upstream_500_envelope(self):
        provider = OpenMeteoProvider(
            settings=_settings(), client=_mock_client(_status_handler(500))
        )
        weather = _weather(WeatherCache(client=FakeRedisClient()), provider)
        result = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "transient",
            "detail": "http_server_error",
            "retryable": True,
            "stale": False,
        }

    async def test_upstream_429_envelope(self):
        provider = OpenMeteoProvider(
            settings=_settings(), client=_mock_client(_status_handler(429))
        )
        weather = _weather(WeatherCache(client=FakeRedisClient()), provider)
        result = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert result.structured_content["error_code"] == "transient"
        assert result.structured_content["detail"] == "rate_limited"
        assert result.structured_content["retryable"] is True

    async def test_city_not_found_envelope(self):
        provider = OpenMeteoProvider(
            settings=_settings(), client=_mock_client(_status_handler(404))
        )
        weather = _weather(WeatherCache(client=FakeRedisClient()), provider)
        result = await weather.call_tool(TOOL_NAME, {"city": "人造星球市"})
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "param_error",
            "detail": "city_not_found",
            "retryable": False,
            "stale": False,
        }

    async def test_tool_slow_envelope(self, monkeypatch: pytest.MonkeyPatch):
        # 整段预算按实现常量临时收窄到 0.5s，让用例不必真睡 8 秒；
        # 断言的是「超预算 → transient/slow」这条分支，不是某个具体秒数。
        monkeypatch.setattr(SERVER, "TOOL_TIMEOUT_SECONDS", 0.5)
        provider = _StubProvider(delay=0.8)
        result = await _weather(WeatherCache(client=FakeRedisClient()), provider).call_tool(
            TOOL_NAME, {"city": "Shanghai"}
        )
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "transient",
            "detail": "slow",
            "retryable": True,
            "stale": False,
        }

    async def test_unexpected_failure_envelope(self):
        provider = _StubProvider(_SUNNY, exc=RuntimeError("kaboom"))
        result = await _weather(WeatherCache(client=FakeRedisClient()), provider).call_tool(
            TOOL_NAME, {"city": "Shanghai"}
        )
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "transient",
            "detail": "internal",
            "retryable": False,
            "stale": False,
        }

    async def test_cache_failure_does_not_break_result(self):
        provider = _StubProvider()
        result = await _weather(
            WeatherCache(client=FakeRedisClient(fail=True)), provider
        ).call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert result.structured_content == {
            "ok": True,
            "data": _SUNNY,
            "error_code": None,
            "detail": None,
            "retryable": False,
            "stale": False,
        }
        assert provider.fetched == ["Shanghai"]

    async def test_schema_requires_city(self):
        weather = _weather(WeatherCache(client=FakeRedisClient()), _StubProvider())
        with pytest.raises(Exception) as exc_info:
            await weather.call_tool(TOOL_NAME, {})
        assert "validation error" in str(exc_info.value)

    async def test_list_tools_exposes_annotation_and_schema(self):
        weather = _weather(WeatherCache(client=FakeRedisClient()), _StubProvider())
        tools = await weather.list_tools()
        assert [tool.name for tool in tools] == [TOOL_NAME]
        annotation = tools[0].annotations
        schema = tools[0].input_schema
        assert annotation.read_only_hint is True
        assert annotation.destructive_hint is False
        assert annotation.open_world_hint is True
        # 断言契约面：type=object、city 必填、只这一个入参。字段级 title/description
        # 是 mcp SDK 按 Python 注解自动生成的实现细节，不进契约断言。
        assert schema["type"] == "object"
        assert schema["required"] == ["city"]
        assert set(schema["properties"]) == {"city"}
        assert schema["properties"]["city"]["type"] == "string"


class TestCacheSecondCall:
    async def test_hit_on_second_call_skips_upstream(self):
        fake = FakeRedisClient()
        provider = _StubProvider()
        weather = _weather(WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600), provider)

        first = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        second = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})

        assert first.structured_content["data"] == _SUNNY
        assert second.structured_content["data"] == _SUNNY
        assert provider.fetched == ["Shanghai"]  # 第二次只走缓存
        assert fake.ttls["anyu:weather:shanghai"] == 600

    async def test_ttl_expiry_refetches(self):
        fake = FakeRedisClient()
        provider = _StubProvider()
        weather = _weather(WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600), provider)
        await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        fake.advance(601)
        # TTL 过期后缓存必然 miss；真实断言在缓存单测里，这里只驱动过期路径。
        assert await WeatherCache(client=fake).get("Shanghai") is None
        third = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert third.structured_content["data"] == _SUNNY
        assert len(provider.fetched) == 2


# ------------------------------------------------------------------ provider 单测


def _meteo_pair(_request: httpx.Request) -> httpx.Response:
    """合法 open-meteo 双段响应（geo 命中上海 → forecast 给当前温度）。"""
    raise AssertionError("由 handler 内部分流，不应直接调用")


class TestProviderRouting:
    async def test_open_meteo_happy_path_is_exact_snapshot(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v1/search"):
                calls.append("geo")
                return httpx.Response(
                    200,
                    json={"results": [{"latitude": 31.23, "longitude": 121.47, "name": "上海"}]},
                )
            calls.append("forecast")
            return httpx.Response(
                200,
                json={
                    "current": {
                        "temperature_2m": 28.4,
                        "weather_code": 2,
                        "humidity": 63,
                        "time": "2026-11-10T15:00",
                    }
                },
            )

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(handler))
        snapshot = await provider.fetch("Shanghai")
        assert snapshot == {
            "city": "上海",
            "temp": 28.4,
            "condition": "多云",
            "observed_at": "2026-11-10T15:00+08:00",
        }
        assert calls == ["geo", "forecast"]

    async def test_missing_data_time_is_bad_payload(self):
        """上游不给 `current.time` 时必须整体失败，不能悄悄少一个键。

        TD:176 的口径是「天气数据+数据时间」；少了数据时间，回复就会变成
        「今天晴，28 度」这种没有出处的说法，等于验收第 3 条直接失守。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v1/search"):
                return httpx.Response(
                    200,
                    json={"results": [{"latitude": 31.23, "longitude": 121.47, "name": "上海"}]},
                )
            return httpx.Response(
                200, json={"current": {"temperature_2m": 28.4, "weather_code": 2}}
            )

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(handler))
        with pytest.raises(WeatherTransient) as err_info:
            await provider.fetch("Shanghai")
        assert err_info.value.error_code == "transient"

    async def test_base_url_override_hits_mirror_only(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.url.host}{request.url.path}")
            return httpx.Response(
                200,
                json=(
                    {"results": [{"latitude": 31.23, "longitude": 121.47, "name": "上海"}]}
                    if request.url.path.endswith("/v1/search")
                    else {
                        "current": {
                            "temperature_2m": 28.4,
                            "weather_code": 2,
                            "time": "2026-11-10T15:00",
                        }
                    }
                ),
            )

        provider = OpenMeteoProvider(
            settings=_settings(weather_api_base_url="http://mirror.invalid"),
            client=_mock_client(handler),
        )
        snapshot = await provider.fetch("Shanghai")
        assert snapshot["temp"] == 28.4
        assert seen == [
            "mirror.invalid/v1/search",
            "mirror.invalid/v1/forecast",
        ]


class TestProviderErrorMatrix:
    @pytest.mark.parametrize(
        ("status", "detail", "code"),
        [
            (500, "http_server_error", "transient"),
            (429, "rate_limited", "transient"),
            (404, "city_not_found", "param_error"),
            (401, "auth_rejected", "unavailable"),
            (403, "auth_rejected", "unavailable"),
            (400, "http_client_error", "unavailable"),
        ],
    )
    async def test_status_matrix(self, status: int, detail: str, code: str):
        provider = OpenMeteoProvider(
            settings=_settings(), client=_mock_client(_status_handler(status))
        )
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Shanghai")
        error = err_info.value
        assert type(error) is _FAMILY[code]
        assert (error.error_code, error.detail) == (code, detail)

    async def test_timeout_becomes_slow(self):
        async def slow(request: httpx.Request) -> httpx.Response:
            # MockTransport 不施加 client timeout（handler 之外的超时配置不生效），
            # 必须由 handler 自己抛传输层超时来模拟「上游超过 3s 预算未返回」。
            raise httpx.ReadTimeout("mock upstream read timeout", request=request)

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(slow))
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Shanghai")
        error = err_info.value
        assert type(error) is WeatherTransient
        assert error.detail == "slow"

    async def test_network_failure_becomes_network(self):
        def down(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(down))
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Shanghai")
        error = err_info.value
        assert type(error) is WeatherTransient
        assert error.detail == "network"

    async def test_city_miss_is_param_not_transient(self):
        def no_city(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": []})

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(no_city))
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("不存在之地")
        error = err_info.value
        assert type(error) is WeatherParamError
        assert error.detail == "city_not_found"

    @pytest.mark.parametrize(
        ("geo_payload", "expected_detail"),
        [
            ({"results": [{}]}, "bad_payload"),  # 击中却缺经纬度 → 形状漂移
            ({}, "city_not_found"),  # 无 results 键 → 查无此城
            ({"results": "oops"}, "city_not_found"),  # results 非列表 → 查无此城
        ],
    )
    async def test_geo_payload_shape_drift(self, geo_payload: dict[str, Any], expected_detail: str):
        def drifting(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=geo_payload)

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(drifting))
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Shanghai")
        error = err_info.value
        assert error.detail == expected_detail

    async def test_bad_json_becomes_bad_payload(self):
        def broken_json(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not-json{")

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(broken_json))
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Shanghai")
        error = err_info.value
        assert type(error) is WeatherTransient
        assert error.detail == "bad_payload"

    async def test_temperature_non_numeric_is_bad_payload(self):
        def weird(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"latitude": 1.0, "longitude": 2.0, "name": "上海"}],
                    # 单响应同时给 results 与 current 的混合形态不可达：
                    # forecast 独立调用，所以这里做两步分流：
                },
            )

        def split(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v1/search"):
                return httpx.Response(
                    200,
                    json={"results": [{"latitude": 31.23, "longitude": 121.47, "name": "上海"}]},
                )
            return httpx.Response(
                200, json={"current": {"temperature_2m": "warm", "weather_code": 2}}
            )

        provider = OpenMeteoProvider(settings=_settings(), client=_mock_client(split))
        del weird
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Shanghai")
        error = err_info.value
        assert type(error) is WeatherTransient
        assert error.detail == "bad_payload"


class TestOpenWeatherProvider:
    async def test_empty_key_short_circuits_without_network(self):
        provider = OpenWeatherProvider(
            settings=_settings(weather_api_key=""), client=_landmine_client()
        )
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Beijing")
        error = err_info.value
        assert (error.error_code, error.detail) == ("unavailable", "no_api_key")

    async def test_openweather_happy_path(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "name": "北京",
                    "weather": [{"description": "晴"}],
                    "main": {"temp": 5.2},
                    "dt": 1794294000,
                },
            )

        provider = OpenWeatherProvider(
            settings=_settings(weather_api_key="sk-test"), client=_mock_client(handler)
        )
        snapshot = await provider.fetch("Beijing")
        assert snapshot == {
            "city": "北京",
            "temp": 5.2,
            "condition": "晴",
            "observed_at": "2026-11-10T15:00+08:00",
        }

    async def test_missing_main_block_is_bad_payload(self):
        def broken(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"name": "北京", "weather": [{"description": "晴"}]})

        provider = OpenWeatherProvider(
            settings=_settings(weather_api_key="sk-test"), client=_mock_client(broken)
        )
        with pytest.raises(WeatherError) as err_info:
            await provider.fetch("Beijing")
        error = err_info.value
        assert type(error) is WeatherTransient
        assert error.detail == "bad_payload"


class TestProviderFactory:
    def test_unknown_provider_is_unavailable(self):
        with pytest.raises(WeatherError) as err_info:
            WeatherProvider.from_settings(_settings(weather_api_provider="sunrise-magic"))
        error = err_info.value
        assert type(error) is WeatherUnavailable
        assert error.detail == "unknown_provider"

    @pytest.mark.parametrize("value", ["", "open-meteo"])
    def test_open_meteo_is_default(self, value: str):
        provider = WeatherProvider.from_settings(_settings(weather_api_provider=value))
        assert isinstance(provider, OpenMeteoProvider)
        assert provider.requires_key is False

    @pytest.mark.parametrize("value", ["openweather", "openweathermap"])
    def test_openweather_aliases(self, value: str):
        provider = WeatherProvider.from_settings(
            _settings(weather_api_provider=value, weather_api_key="sk-test")
        )
        assert isinstance(provider, OpenWeatherProvider)
        assert provider.requires_key is True


class TestNormalizeCity:
    def test_strips_whitespace(self):
        assert PROVIDER.normalize_city(" 蛇口镇 ") == "蛇口镇"

    def test_blank_is_param_error(self):
        for blank in ("", "   "):
            with pytest.raises(WeatherError) as err_info:
                PROVIDER.normalize_city(blank)
            error = err_info.value
            assert type(error) is WeatherParamError
            assert error.detail == "city_blank"

    def test_non_string_is_param_error(self):
        with pytest.raises(WeatherError) as err_info:
            PROVIDER.normalize_city(123)
        error = err_info.value
        assert type(error) is WeatherParamError
        assert error.detail == "city_blank"

    def test_too_long_is_param_error(self):
        with pytest.raises(WeatherError) as err_info:
            PROVIDER.normalize_city("x" * 41)
        error = err_info.value
        assert type(error) is WeatherParamError
        assert error.detail == "city_too_long"

    def test_boundary_40_chars_passes(self):
        assert len(PROVIDER.normalize_city("x" * 40)) == 40


# ------------------------------------------------------------------ cache 单测


class TestWeatherCacheUnit:
    async def test_put_get_roundtrip(self):
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        await cache.put("Shanghai", _SUNNY)
        assert await cache.get("Shanghai") == _SUNNY
        assert fake.ttls["anyu:weather:shanghai"] == 600
        # put 同时写一份 24h 的失败兜底副本（FR-CON-02 ②）；读主键仍是 GET 一次。
        assert fake.ttls["anyu:weather:last:shanghai"] == CACHE.STALE_SNAPSHOT_TTL_SECONDS
        assert fake.calls == [
            "SET anyu:weather:shanghai",
            "SET anyu:weather:last:shanghai",
            "GET anyu:weather:shanghai",
        ]

    async def test_ttl_expiry_reads_as_miss(self):
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        await cache.put("Shanghai", _SUNNY)
        fake.advance(600)
        assert await cache.get("Shanghai") == _SUNNY  # TTL 当边界内仍命中
        fake.advance(1)
        assert await cache.get("Shanghai") is None

    async def test_invalidate_then_miss(self):
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        await cache.put("Shanghai", _SUNNY)
        await cache.invalidate("Shanghai")
        assert await cache.get("Shanghai") is None
        assert "DELETE anyu:weather:shanghai" in fake.calls

    async def test_casefold_key_shared(self):
        cache = WeatherCache(client=FakeRedisClient(), key_prefix="anyu", ttl_seconds=600)
        await cache.put("Shanghai", _SUNNY)
        assert await cache.get("shanghai") == _SUNNY
        assert await cache.get("SHANGHAI") == _SUNNY

    async def test_fail_client_degrades(self):
        cache = WeatherCache(client=FakeRedisClient(fail=True), key_prefix="anyu", ttl_seconds=600)
        assert await cache.get("Shanghai") is None
        await cache.put("Shanghai", _SUNNY)  # 不抛
        await cache.invalidate("Shanghai")  # 不抛
        assert await cache.get("Shanghai") is None

    async def test_corrupt_payload_reads_as_miss(self):
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        await fake.set("anyu:weather:shanghai", "not-json")
        assert await cache.get("Shanghai") is None

    async def test_missing_triple_keys_reads_as_miss(self):
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        await fake.set("anyu:weather:shanghai", '{"city": "上海"}')
        assert await cache.get("Shanghai") is None

    async def test_non_dict_snapshot_rejected(self):
        cache = WeatherCache(client=FakeRedisClient(), key_prefix="anyu", ttl_seconds=600)
        await cache.put("Shanghai", 12345)  # 非 dict 直接不写
        assert await cache.get("Shanghai") is None

    async def test_missing_redis_module_degrades(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(sys.modules, "redis", None)
        cache = WeatherCache(key_prefix="anyu", ttl_seconds=600)
        assert await cache.get("Shanghai") is None
        await cache.put("Shanghai", _SUNNY)  # 不抛
        await cache.invalidate("Shanghai")  # 不抛
        assert await cache.get("Shanghai") is None


# ------------------------------------------------------------------ 失败兜底（FR-CON-02 ②③）


class TestStaleCacheCopy:
    """兜底副本（`stale_key_for`）的 TTL / 失败无害化 / 与 invalidate 的联动。"""

    @staticmethod
    def _cache(client: FakeRedisClient) -> WeatherCache:
        return WeatherCache(client=client, key_prefix="anyu", ttl_seconds=600)

    async def test_stale_copy_outlives_main_key(self):
        fake = FakeRedisClient()
        cache = self._cache(fake)
        await cache.put("Shanghai", _SUNNY)
        fake.advance(601)  # 主键 600s 已过期
        assert await cache.get("Shanghai") is None
        assert await cache.get_stale("Shanghai") == _SUNNY

    async def test_stale_copy_expires_after_a_day(self):
        fake = FakeRedisClient()
        cache = self._cache(fake)
        await cache.put("Shanghai", _SUNNY)
        fake.advance(CACHE.STALE_SNAPSHOT_TTL_SECONDS + 1)
        assert await cache.get_stale("Shanghai") is None

    async def test_stale_read_never_raises(self):
        cache = self._cache(FakeRedisClient(fail=True))
        assert await cache.get_stale("Shanghai") is None

    async def test_invalidate_removes_both_keys(self):
        fake = FakeRedisClient()
        cache = self._cache(fake)
        await cache.put("Shanghai", _SUNNY)
        await cache.invalidate("Shanghai")
        assert await cache.get("Shanghai") is None
        assert await cache.get_stale("Shanghai") is None


class TestStaleFallback:
    """provider 失败 → 最近一次成功快照 + ``stale=True``；没有快照 → 明确失败。"""

    async def test_provider_failure_serves_recent_snapshot(self):
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        warm = await _weather(cache, _StubProvider()).call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert warm.structured_content["stale"] is False
        fake.advance(601)  # 主键过期、副本仍在 —— 正是要兜底的时刻
        provider = _StubProvider(exc=WeatherTransient("network"))
        result = await _weather(cache, provider).call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert result.structured_content == {
            "ok": True,
            "data": _SUNNY,
            "error_code": None,
            "detail": None,
            "retryable": False,
            "stale": True,
        }
        assert provider.fetched == ["Shanghai"]

    async def test_slow_provider_falls_back_to_snapshot(self, monkeypatch: pytest.MonkeyPatch):
        # 把工具预算压到 50ms：不真等 3 秒，断言的是「预算打穿 → 走兜底」这条路径。
        monkeypatch.setattr(SERVER, "TOOL_TIMEOUT_SECONDS", 0.05)
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        await _weather(cache, _StubProvider()).call_tool(TOOL_NAME, {"city": "Shanghai"})
        fake.advance(601)
        result = await _weather(cache, _StubProvider(delay=0.2)).call_tool(
            TOOL_NAME, {"city": "Shanghai"}
        )
        assert result.structured_content["ok"] is True
        assert result.structured_content["stale"] is True

    async def test_cache_hit_within_ttl_is_not_stale(self):
        cache = WeatherCache(client=FakeRedisClient(), key_prefix="anyu", ttl_seconds=600)
        await cache.put("Shanghai", _SUNNY)
        provider = _StubProvider(exc=WeatherTransient("network"))
        result = await _weather(cache, provider).call_tool(TOOL_NAME, {"city": "Shanghai"})
        # TTL 内命中是「刚查过」，不是「查失败了」，不能冒充失败兜底。
        assert result.structured_content["ok"] is True
        assert result.structured_content["stale"] is False
        assert provider.fetched == []

    async def test_no_snapshot_fails_without_fabricating(self):
        provider = _StubProvider(exc=WeatherTransient("network"))
        weather = _weather(
            WeatherCache(client=FakeRedisClient(), key_prefix="anyu", ttl_seconds=600),
            provider,
        )
        result = await weather.call_tool(TOOL_NAME, {"city": "Shanghai"})
        assert result.structured_content == {
            "ok": False,
            "data": None,
            "error_code": "transient",
            "detail": "network",
            "retryable": True,
            "stale": False,
        }

    async def test_day_old_snapshot_is_not_served(self):
        fake = FakeRedisClient()
        cache = WeatherCache(client=fake, key_prefix="anyu", ttl_seconds=600)
        await _weather(cache, _StubProvider()).call_tool(TOOL_NAME, {"city": "Shanghai"})
        fake.advance(CACHE.STALE_SNAPSHOT_TTL_SECONDS + 1)  # 副本也过期
        result = await _weather(cache, _StubProvider(exc=WeatherTransient("network"))).call_tool(
            TOOL_NAME, {"city": "Shanghai"}
        )
        # 跨天的旧天气不许冒充实况：宁可回失败信封，由对话侧说「查不到」。
        assert result.structured_content["ok"] is False
        assert result.structured_content["stale"] is False
