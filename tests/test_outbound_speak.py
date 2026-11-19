"""外呼播报生产实现（`app/audio/outbound_speak.py`）的注入面测试。

验的是「TTL 没接话术」这四个字背后的四件事：
① 合成与入队都用了**该设备**的身份、音色与合成前的播放代际号；
② 没有媒体会话 —— 不许合成，直接失败（由 outbound 降级）；
③ 空音频 —— 不许入队；
④ 播放轨已关（入队返回 False）—— 失败，不能当作说完。

全程不碰 aiortc / 真 TTS / 数据库：音色查找、`build_tts`、播放轨三个接缝都在
模块属性上替换（实现里是惰性 import，调用时取到的就是替换后的对象）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.audio import outbound_speak, webrtc

DEVICE_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"
TEXT = "奶奶，该吃药啦，吃完了跟我说一声。"
VOICE = "zh-CN-XiaoxiaoNeural"


class FakeTTS:
    """最小 TTS provider：记录入参，回固定 PCM（或被要求返回空）。"""

    def __init__(self, pcm: bytes = b"\x00\x00" * 160) -> None:
        self.pcm = pcm
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return self.pcm


def _patch_webrtc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session: bool = True,
    queued: bool = True,
    generation: int | None = 7,
) -> list[dict[str, Any]]:
    """替换播放轨三件套；返回入队记录，供断言设备号 / 音频 / 代际号。"""
    plays: list[dict[str, Any]] = []

    async def fake_play(device_id: str, pcm: bytes, *, generation: int | None = None) -> bool:
        plays.append({"device_id": device_id, "pcm": pcm, "generation": generation})
        return queued

    monkeypatch.setattr(webrtc, "has_media_session", lambda device_id: session)
    monkeypatch.setattr(webrtc, "playback_generation_for_device", lambda device_id: generation)
    monkeypatch.setattr(webrtc, "play_system_tts_for_device", fake_play)
    return plays


def _patch_voice(monkeypatch: pytest.MonkeyPatch, voice: str | None) -> None:
    async def fake_voice(device_id: str) -> str | None:
        return voice

    monkeypatch.setattr(outbound_speak, "_voice_for_device", fake_voice)


def _patch_tts(
    monkeypatch: pytest.MonkeyPatch, pcm: bytes = b"\x00\x00" * 160
) -> tuple[FakeTTS, list[str | None]]:
    """替换 `build_tts`；同时记录每次被请求的音色。"""
    provider = FakeTTS(pcm=pcm)
    voices: list[str | None] = []

    def fake_build_tts(settings: Any = None, *, voice: str | None = None) -> FakeTTS:
        voices.append(voice)
        return provider

    monkeypatch.setattr("app.audio.tts.build_tts", fake_build_tts)
    return provider, voices


async def test_speak_synthesizes_with_persona_voice_and_queues_pcm(monkeypatch) -> None:
    _patch_voice(monkeypatch, VOICE)
    provider, voices = _patch_tts(monkeypatch)
    plays = _patch_webrtc(monkeypatch, generation=7)

    await outbound_speak.speak_text_to_device(DEVICE_ID, TEXT)

    assert voices == [VOICE]
    assert provider.calls == [TEXT]
    assert plays == [{"device_id": DEVICE_ID, "pcm": provider.pcm, "generation": 7}]


async def test_speak_without_media_session_fails_before_tts(monkeypatch) -> None:
    _patch_voice(monkeypatch, VOICE)
    provider, _ = _patch_tts(monkeypatch)
    plays = _patch_webrtc(monkeypatch, session=False)

    with pytest.raises(outbound_speak.OutboundSpeakError):
        await outbound_speak.speak_text_to_device(DEVICE_ID, TEXT)

    assert provider.calls == []
    assert plays == []


async def test_speak_rejects_empty_pcm(monkeypatch) -> None:
    _patch_voice(monkeypatch, None)
    _patch_tts(monkeypatch, pcm=b"")
    plays = _patch_webrtc(monkeypatch)

    with pytest.raises(outbound_speak.OutboundSpeakError):
        await outbound_speak.speak_text_to_device(DEVICE_ID, TEXT)

    assert plays == []


async def test_speak_fails_when_track_already_closed(monkeypatch) -> None:
    _patch_voice(monkeypatch, VOICE)
    _patch_tts(monkeypatch)
    plays = _patch_webrtc(monkeypatch, queued=False)

    with pytest.raises(outbound_speak.OutboundSpeakError):
        await outbound_speak.speak_text_to_device(DEVICE_ID, TEXT)

    assert len(plays) == 1


async def test_speak_rejects_blank_text(monkeypatch) -> None:
    plays = _patch_webrtc(monkeypatch)

    with pytest.raises(outbound_speak.OutboundSpeakError):
        await outbound_speak.speak_text_to_device(DEVICE_ID, "   ")

    assert plays == []


async def test_built_speaker_always_injects_playback(monkeypatch) -> None:
    """`build_outbound_speaker()` 的产物必须把 speak 钉成真实实现，而不是 `None`。"""
    seen: dict[str, Any] = {}

    async def fake_call_and_speak(**kwargs: Any):
        seen.update(kwargs)
        return "sentinel"

    monkeypatch.setattr("app.audio.outbound.call_and_speak", fake_call_and_speak)

    speaker = outbound_speak.build_outbound_speaker()
    result = await speaker(device_id=DEVICE_ID, text=TEXT, offerer=object(), sender=object())

    assert result == "sentinel"
    assert seen["device_id"] == DEVICE_ID
    assert seen["text"] == TEXT
    assert seen["speak"] is outbound_speak.speak_text_to_device
