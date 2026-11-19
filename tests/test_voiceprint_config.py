"""声纹加密密钥的配置自检（2026-11-07 P0 回归）。

背景：`voiceprint_fernet_key` 的默认值曾 base64 解码后只有 39 字节（Fernet 要 32），
导致默认配置下：
  - 注册声纹 → `voiceprint_service.encrypt_embedding` 抛 `ValueError` → 500；
  - 比对声纹 → 同处异常被身份级联的 `except Exception` 吞掉 → 静默降级 generic。
即「语音身份永不命中，且页面上看不出任何异常」。

本文件钉住三件事：
1. 默认 key 必须可用（否则本地开发一上手就是坏功能）；
2. 非法 key 在非 test 环境必须 fail-fast（不允许带病启动）；
3. 非法 key 在 test 环境只记 error（单测要能构造非法配置）。
"""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI

from app.core.config import get_settings
from app.main import create_app
from app.services import voiceprint_service


def _valid_key(seed: bytes = b"anyu-dev-voiceprint-key!!!") -> str:
    return base64.urlsafe_b64encode(seed).decode()


def test_default_key_is_usable() -> None:
    """默认配置的 key 必须能构造 Fernet（32 字节 url-safe base64）。"""
    settings = get_settings()
    assert settings.voiceprint_fernet_ready is True


def test_ready_rejects_wrong_length() -> None:
    """历史上那个 39 字节的默认值必须被判为不可用。"""
    broken = base64.urlsafe_b64encode(b"x" * 39).decode()
    from app.core.config import Settings

    assert Settings(voiceprint_fernet_key=broken).voiceprint_fernet_ready is False


def test_ready_rejects_empty_and_garbage() -> None:
    from app.core.config import Settings

    assert Settings(voiceprint_fernet_key="").voiceprint_fernet_ready is False
    assert Settings(voiceprint_fernet_key="not-base64!!").voiceprint_fernet_ready is False


def test_create_app_fails_fast_outside_test_env() -> None:
    """非 test 环境带病启动必须直接失败，而不是跑起来之后静默降级。"""
    from app.core.config import Settings

    settings = Settings(app_env="development", voiceprint_fernet_key="bad-key")
    with pytest.raises(RuntimeError, match="voiceprint_fernet_key"):
        create_app(settings)


def test_create_app_allows_test_env_with_warning() -> None:
    """test 环境允许非法 key（便于构造失败用例），但不得抛错。"""
    from app.core.config import Settings

    settings = Settings(app_env="test", voiceprint_fernet_key="bad-key")
    app = create_app(settings)
    assert isinstance(app, FastAPI)


def test_bad_key_raises_typed_error_not_value_error() -> None:
    """非法 key 在服务层必须抛 VoiceprintUnavailable，供上层映射为可见降级。"""
    from app.core.config import Settings

    settings = Settings(app_env="test", voiceprint_fernet_key="bad-key")
    get_settings.cache_clear()
    try:
        import app.services.voiceprint_service as vp

        original = vp.get_settings
        vp.get_settings = lambda: settings  # type: ignore[assignment]
        try:
            with pytest.raises(voiceprint_service.VoiceprintUnavailable):
                vp.encrypt_embedding(__import__("numpy").ones(4, dtype="float32"))
        finally:
            vp.get_settings = original  # type: ignore[assignment]
    finally:
        get_settings.cache_clear()


def test_production_rejects_repository_placeholder_key() -> None:
    """生产环境用仓库里的占位密钥必须整条链路不可用：那是所有部署共用的公开钥匙。"""
    import numpy as np

    from app.core.config import Settings

    placeholder = Settings(app_env="production")
    assert placeholder.voiceprint_fernet_is_placeholder is True

    import app.services.voiceprint_service as vp

    original = vp.get_settings
    vp.get_settings = lambda: placeholder  # type: ignore[assignment]
    try:
        with pytest.raises(voiceprint_service.VoiceprintUnavailable, match="占位密钥"):
            vp.encrypt_embedding(np.ones(4, dtype="float32"))
    finally:
        vp.get_settings = original  # type: ignore[assignment]


def test_production_accepts_explicit_key() -> None:
    """换成显式注入的真密钥后，生产环境照常可用。"""
    import numpy as np

    from app.core.config import Settings

    explicit = Settings(app_env="production", voiceprint_fernet_key=_valid_key(b"z" * 32))
    assert explicit.voiceprint_fernet_is_placeholder is False

    import app.services.voiceprint_service as vp

    original = vp.get_settings
    vp.get_settings = lambda: explicit  # type: ignore[assignment]
    try:
        # CAM++ 固定 192 维，短向量会在解密后的维度校验处被判错，这里按真实维度造。
        cipher = vp.encrypt_embedding(np.ones(192, dtype="float32"))
        assert isinstance(cipher, bytes)
        assert vp.decrypt_embedding(cipher).shape == (192,)
    finally:
        vp.get_settings = original  # type: ignore[assignment]
