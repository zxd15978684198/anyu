"""请求层限流回归（攻防计划 §4.6 · S3，实现见 `app/core/rate_limit.py`）。

**为什么这层必须有**：`rate_limit_per_minute` 在 `app/core/config.py` 里躺了很久，
全仓没有读取点 —— 口令可在线爆破、匿名可刷接口，配置看着像功能，其实一行都没生效。
本文件把「接上了」钉成可执行的断言。

钉住的六件事：

1. 额度内放行；
2. 超限 429，响应体是契约 §7 的三键（`detail` 为**字符串**、`code` 固定）、
   带 `Retry-After`，且 429 也带 `X-Trace-Id`（证明限流挂在 TraceId **内层**）；
3. 窗口滑过后恢复；
4. `/health`、`/metrics` 豁免（探针与抓取器不能被限流打到不可用）；
5. test/local 默认放宽、总开关可关（否则现有 1794 例门禁会集体 429）；
6. WebSocket 等非 http scope 直接放行（长连接没有「每分钟请求数」语义）。

**为什么不用真等 60 秒**：窗口时间从 `rate_limit._now()` 取，用假时钟 `advance()`
就能瞬间快进，用例仍是毫秒级。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.core import rate_limit
from app.core.config import Settings
from app.core.context import TRACE_HEADER
from app.core.rate_limit import RateLimitMiddleware
from app.core.trace import TraceIdMiddleware
from app.main import create_app


class _Clock:
    """可控假时钟：patch 掉 `rate_limit._now` 后，`advance()` 即快进窗口。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(rate_limit, "_now", fake)
    return fake


def _settings(limit: int = 2, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        # 本文件测的就是限流本身，故显式打开「test 环境也真跑」的开关；
        # 生产口径仍是 `enforce_in_dev=False`（test/local 放行）。
        "rate_limit_enforce_in_dev": True,
        "rate_limit_per_minute": limit,
    }
    values.update(overrides)
    return Settings(**values)


def _build_app(limit: int = 2, *, with_trace: bool = False, **overrides: Any) -> FastAPI:
    application = FastAPI()
    application.add_middleware(RateLimitMiddleware, settings=_settings(limit, **overrides))
    if with_trace:
        # 与 app/main.py 同序：后添加者在外层（限流在内，trace 在外）
        application.add_middleware(TraceIdMiddleware)

    @application.get("/api/demo/ping")
    async def _ping() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/metrics")
    async def _metrics() -> PlainTextResponse:
        return PlainTextResponse("anyu_up 1\n")

    return application


# ---------------------------------------------------------------------------
# 1. 额度内放行
# ---------------------------------------------------------------------------


def test_requests_within_limit_are_allowed(clock: _Clock) -> None:
    client = TestClient(_build_app(limit=3))

    for _ in range(3):
        assert client.get("/api/demo/ping").status_code == 200


# ---------------------------------------------------------------------------
# 2. 超限 429：契约 §7 的形状 + Retry-After + trace 头
# ---------------------------------------------------------------------------


def test_over_limit_returns_429_with_contract_body(clock: _Clock) -> None:
    client = TestClient(_build_app(limit=2, with_trace=True))
    for _ in range(2):
        assert client.get("/api/demo/ping").status_code == 200

    response = client.get("/api/demo/ping")

    assert response.status_code == 429
    body = response.json()
    assert set(body) == {"detail", "code", "trace_id"}
    assert body["code"] == rate_limit.RATE_LIMIT_CODE
    assert isinstance(body["detail"], str) and body["detail"]
    assert int(response.headers["retry-after"]) >= 1
    # 429 也是响应，必须能被链路追到：头由外层 TraceIdMiddleware 补，体由内层写
    assert response.headers.get(TRACE_HEADER)
    assert body["trace_id"] == response.headers[TRACE_HEADER]


def test_bucket_is_shared_across_ids_but_not_across_routers(clock: _Clock) -> None:
    """同一路由桶共享额度（换资源 id 不能白拿额度），不同桶互不影响。"""
    client = TestClient(_build_app(limit=2))

    assert client.get("/api/demo/ping?id=1").status_code == 200
    assert client.get("/api/demo/ping?id=2").status_code == 200
    assert client.get("/api/demo/ping?id=3").status_code == 429
    # /health 是另一个桶且豁免，不影响也不被影响
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# 3. 窗口滑过后恢复
# ---------------------------------------------------------------------------


def test_window_slides_and_quota_recovers(clock: _Clock) -> None:
    client = TestClient(_build_app(limit=2))

    assert client.get("/api/demo/ping").status_code == 200
    assert client.get("/api/demo/ping").status_code == 200
    assert client.get("/api/demo/ping").status_code == 429

    clock.advance(rate_limit.WINDOW_SECONDS + 0.001)

    assert client.get("/api/demo/ping").status_code == 200


# ---------------------------------------------------------------------------
# 4. 豁免路径
# ---------------------------------------------------------------------------


def test_exempt_paths_are_never_limited(clock: _Clock) -> None:
    client = TestClient(_build_app(limit=1))

    for _ in range(5):
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200

    # 豁免请求不占额度：第一次业务请求仍然放行，第二次才超限
    assert client.get("/api/demo/ping").status_code == 200
    assert client.get("/api/demo/ping").status_code == 429


# ---------------------------------------------------------------------------
# 5. 默认放宽 / 总开关
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["test", "local"])
def test_relaxed_envs_are_not_limited(env: str) -> None:
    settings = Settings(app_env=env, rate_limit_per_minute=1)  # enforce_in_dev 默认 False
    assert rate_limit.limiter_enabled(settings) is False

    client = TestClient(_build_app(limit=1, app_env=env, rate_limit_enforce_in_dev=False))
    for _ in range(5):
        assert client.get("/api/demo/ping").status_code == 200


def test_master_switch_disables_limiting_even_in_prod(clock: _Clock) -> None:
    client = TestClient(_build_app(limit=1, app_env="production", rate_limit_enabled=False))
    for _ in range(5):
        assert client.get("/api/demo/ping").status_code == 200


# ---------------------------------------------------------------------------
# 6. 非 http scope（WebSocket）放行 + 在真实装配里确实挂上了
# ---------------------------------------------------------------------------


async def test_non_http_scope_bypasses_limiter() -> None:
    reached: list[str] = []
    sent: list[dict[str, Any]] = []

    async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
        reached.append(str(scope["type"]))

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.receive"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = RateLimitMiddleware(downstream, settings=_settings(limit=1))
    for _ in range(5):
        await middleware({"type": "websocket", "path": "/ws/signal"}, receive, send)

    assert reached == ["websocket"] * 5
    assert sent == []


def test_create_app_mounts_middleware_inside_trace() -> None:
    """`app/main.py` 的装配必须真的接上限流（避免「模块写好了、没人挂」重演）。"""
    application = create_app(
        Settings(app_env="test", rate_limit_enforce_in_dev=True, rate_limit_per_minute=2)
    )
    client = TestClient(application)

    first = client.get("/openapi.json")
    second = client.get("/openapi.json")
    third = client.get("/openapi.json")

    assert [first.status_code, second.status_code, third.status_code] == [200, 200, 429]
    assert third.headers.get(TRACE_HEADER)
