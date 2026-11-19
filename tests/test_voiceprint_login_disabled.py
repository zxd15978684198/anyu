"""声纹免登录在默认配置下必须不可用（2026-11-13 产品负责人裁决）。

只钉两件事：默认值是 False、端点回 403 且错误码可辨识。不碰 DB —— 开关判断放在
读音频之前，正是为了「关掉之后连 multipart 都不必解析」。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings


@pytest.fixture
def app_without_voiceprint_login(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VOICEPRINT_LOGIN_ENABLED", raising=False)
    get_settings.cache_clear()
    from app.main import create_app

    application = create_app(get_settings())
    yield application
    get_settings.cache_clear()


def test_field_defaults_to_false() -> None:
    field = type(get_settings()).model_fields["voiceprint_login_enabled"]
    assert field.default is False


@pytest.mark.asyncio
async def test_endpoint_returns_403_when_disabled(app_without_voiceprint_login) -> None:
    transport = ASGITransport(app=app_without_voiceprint_login)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/device-sessions/voiceprint",
            data={"device_id": "11111111-1111-4111-8111-111111111106"},
            files={"audio": ("a.wav", b"RIFF....", "audio/wav")},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "voiceprint_login_disabled"
