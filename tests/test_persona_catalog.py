"""人设音色与 edge allowlist 的一致性。

2026-11-16 的线上现象：人设表写入了 edge-tts 不存在的音色（Xiaomeng/Xiaohan/
Xiaoyan/Xiaorui），ASR 与 LLM 都通，TTS 在最后一米返回 0 字节，长者整段听不到回复。
本模块把「清单里的音色必须真实可用」钉成测试，避免下一次 seed 又把坏值写回库。
"""

from __future__ import annotations

import logging

import pytest
from scripts.persona_catalog import PERSONA_CATALOG

from app.audio.tts import (
    EDGE_DEFAULT_VOICE,
    EDGE_SUPPORTED_VOICES,
    EdgeTTSProvider,
    FallbackTTSProvider,
    build_tts,
)
from app.core.config import Settings


def _edge_from(provider: object) -> EdgeTTSProvider:
    assert isinstance(provider, FallbackTTSProvider)
    assert isinstance(provider.primary, EdgeTTSProvider)
    return provider.primary


def test_catalog_voices_are_supported_by_edge() -> None:
    unsupported = [
        (asset["name"], asset["voice_id"])
        for asset in PERSONA_CATALOG
        if asset["voice_id"] not in EDGE_SUPPORTED_VOICES
    ]
    assert not unsupported, f"人设清单含 edge 不支持的音色：{unsupported}"


def test_default_voice_is_in_allowlist() -> None:
    assert EDGE_DEFAULT_VOICE in EDGE_SUPPORTED_VOICES


def test_build_tts_falls_back_when_persona_voice_is_unsupported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.audio.tts")

    provider = build_tts(
        Settings(
            tts_provider="edge",
            tts_bridge_url="",
            voice_pack_enabled=False,
        ),
        voice="zh-CN-XiaomengNeural",
    )

    assert _edge_from(provider).voice == EDGE_DEFAULT_VOICE
    assert "unsupported edge voice" in caplog.text


def test_build_tts_keeps_supported_persona_voice() -> None:
    provider = build_tts(
        Settings(
            tts_provider="edge",
            tts_bridge_url="",
            voice_pack_enabled=False,
        ),
        voice="zh-CN-XiaoyiNeural",
    )

    assert _edge_from(provider).voice == "zh-CN-XiaoyiNeural"
