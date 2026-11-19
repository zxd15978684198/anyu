# owner=A3 · 演示凭证端点 `GET /api/pad/demo-login`
"""演示登录凭证端点（本机演示专用）。

为什么单独一个文件：设备密钥在库里只有 sha256（`devices.token_hash`），明文查不回来，
演示时靠这个端点把 `scripts/seed_demo.py` 种进去的那套凭证交给登录页「填入」按钮。
三条边界必须被用例钉住：

1. 生产环境一律 404 —— 不因为「配了变量」就放行；
2. 未配置（任一项为空）也是 404 —— `.env.example` 留空，真机部署不会意外带演示凭证；
3. 该端点**不需要设备令牌**（登录页此时还没有令牌），也不写库、不签发令牌。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import pad as pad_api
from app.core.config import get_settings
from app.main import TraceIdMiddleware

DEMO_ID = "11111111-1111-4111-8111-111111111106"
DEMO_SECRET = "demo-device-secret-0001"  # pragma: allowlist secret


@pytest.fixture
def demo_env(monkeypatch: pytest.MonkeyPatch):
    """按需注入演示凭证与 app_env；用例结束后清缓存，避免污染同进程其它文件。"""

    def _apply(*, device_id: str = "", device_secret: str = "", app_env: str = "local") -> None:
        monkeypatch.setenv("DEMO_DEVICE_ID", device_id)
        monkeypatch.setenv("DEMO_DEVICE_SECRET", device_secret)
        monkeypatch.setenv("APP_ENV", app_env)
        get_settings.cache_clear()

    yield _apply
    get_settings.cache_clear()


@pytest.fixture
async def client() -> AsyncClient:
    """只挂 pad router：该端点无依赖，不需要库会话。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(pad_api.router)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def test_returns_credentials_when_configured(client: AsyncClient, demo_env) -> None:
    demo_env(device_id=DEMO_ID, device_secret=DEMO_SECRET)

    response = await client.get("/api/pad/demo-login")

    assert response.status_code == 200
    # 只回两项：多一个字段就等于把「配置长什么样」漏给前端
    assert response.json() == {"device_id": DEMO_ID, "device_secret": DEMO_SECRET}


async def test_404_when_not_configured(client: AsyncClient, demo_env) -> None:
    demo_env()  # 两项都空 = 未开启演示

    response = await client.get("/api/pad/demo-login")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "demo_login_unavailable"
    assert set(body) == {"detail", "code", "trace_id"}
    assert body["trace_id"] == response.headers["x-trace-id"]


async def test_404_when_only_one_field_is_set(client: AsyncClient, demo_env) -> None:
    """半配置比不配置更危险：会让人以为「按钮没了是页面 bug」。"""
    demo_env(device_id=DEMO_ID, device_secret="")

    response = await client.get("/api/pad/demo-login")

    assert response.status_code == 404
    assert response.json()["code"] == "demo_login_unavailable"


async def test_404_in_production_even_when_configured(client: AsyncClient, demo_env) -> None:
    """生产环境即使配了也 404：演示凭证绝不能在真机上被匿名取走。"""
    demo_env(device_id=DEMO_ID, device_secret=DEMO_SECRET, app_env="production")

    response = await client.get("/api/pad/demo-login")

    assert response.status_code == 404
    assert response.json()["code"] == "demo_login_unavailable"


async def test_does_not_require_device_token(client: AsyncClient, demo_env) -> None:
    """登录页此时还没有令牌：带不带 Authorization 都必须能取到（否则按钮永远不出现）。"""
    demo_env(device_id=DEMO_ID, device_secret=DEMO_SECRET)

    anonymous = await client.get("/api/pad/demo-login")
    with_bogus_header = await client.get(
        "/api/pad/demo-login", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert anonymous.status_code == 200
    assert with_bogus_header.status_code == 200
