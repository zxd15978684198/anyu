"""固定话术音频包（P4）：manifest 命中零网络、miss 回落、音色与路径边界。

音频包的价值只有一个：**固定话术不发任何 TTS 请求也能出声**。所以这里的断言都落在
「内层 provider 有没有被调用」上，而不是「听起来快不快」。
"""

from __future__ import annotations

import json
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.audio import tts as tts_mod
from app.audio.tts import VoicePackTTSProvider, build_tts
from app.audio.voice_pack import (
    load_manifest,
    read_wav_pcm,
    resolve_entry_file,
    text_sha256,
)
from app.core.config import Settings

VOICE = "zh-CN-XiaoxiaoNeural"
TEXT = "好，已经记下您吃过了。"
PCM = b"\x01\x00" * 4000  # 250ms @16k/16bit/mono


class _Inner:
    name = "inner"

    def __init__(self) -> None:
        self.synthesize_calls = 0
        self.stream_calls = 0

    async def synthesize(self, text: str) -> bytes:
        self.synthesize_calls += 1
        return b"\x02\x00" * 400

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        self.stream_calls += 1
        yield b"\x02\x00" * 400


def _write_wav(path: Path, pcm: bytes = PCM, *, rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def _pack(tmp_path: Path, *, voice: str = VOICE, file: str = "taken.wav") -> Path:
    if file == "taken.wav":
        _write_wav(tmp_path / file)
    manifest = {
        "version": 1,
        "entries": {
            text_sha256(TEXT): {
                "text": TEXT,
                "voice": voice,
                "file": file,
                "duration_ms": 50,
            }
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def test_text_sha256_ignores_surrounding_whitespace() -> None:
    assert text_sha256(f"  {TEXT}\n") == text_sha256(TEXT)


def test_load_manifest_rejects_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")

    assert load_manifest(path) is None


def test_resolve_entry_file_rejects_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.wav"
    _write_wav(outside)
    root = tmp_path / "pack"
    root.mkdir()

    assert resolve_entry_file(root, {"file": "../outside.wav"}) is None


def test_read_wav_pcm_rejects_wrong_rate(tmp_path: Path) -> None:
    path = tmp_path / "bad.wav"
    _write_wav(path, rate=48_000)

    with pytest.raises(ValueError):
        read_wav_pcm(path)


@pytest.mark.asyncio
async def test_hit_uses_pack_without_inner_call(tmp_path: Path) -> None:
    inner = _Inner()
    provider = VoicePackTTSProvider(inner, _pack(tmp_path), voice=VOICE)

    pcm = await provider.synthesize(TEXT)

    assert pcm == PCM
    assert inner.synthesize_calls == 0


@pytest.mark.asyncio
async def test_stream_hit_chunks_pack_without_inner_call(tmp_path: Path) -> None:
    inner = _Inner()
    provider = VoicePackTTSProvider(inner, _pack(tmp_path), voice=VOICE)

    chunks = [chunk async for chunk in provider.synthesize_stream(TEXT)]

    assert b"".join(chunks) == PCM
    assert len(chunks) > 1, "音频包也要分块，避免一次性把整句塞给写轨"
    assert inner.stream_calls == 0


@pytest.mark.asyncio
async def test_miss_delegates_to_inner(tmp_path: Path) -> None:
    inner = _Inner()
    provider = VoicePackTTSProvider(inner, _pack(tmp_path), voice=VOICE)

    pcm = await provider.synthesize("这句话不在音频包里。")

    assert pcm == b"\x02\x00" * 400
    assert inner.synthesize_calls == 1


@pytest.mark.asyncio
async def test_voice_mismatch_delegates_to_inner(tmp_path: Path) -> None:
    inner = _Inner()
    provider = VoicePackTTSProvider(inner, _pack(tmp_path), voice="zh-CN-YunxiNeural")

    pcm = await provider.synthesize(TEXT)

    assert pcm == b"\x02\x00" * 400
    assert inner.synthesize_calls == 1, "换了角色音色必须 miss，不能播旧角色声音"


@pytest.mark.asyncio
async def test_missing_file_falls_back_to_inner(tmp_path: Path) -> None:
    inner = _Inner()
    provider = VoicePackTTSProvider(inner, _pack(tmp_path, file="missing.wav"), voice=VOICE)

    pcm = await provider.synthesize(TEXT)

    assert pcm == b"\x02\x00" * 400
    assert inner.synthesize_calls == 1


def test_build_tts_wraps_pack_when_manifest_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _pack(tmp_path)
    monkeypatch.setattr(tts_mod, "EdgeTTSProvider", lambda **kwargs: _Inner())

    provider = build_tts(
        Settings(
            _env_file=None,
            tts_provider="edge",
            tts_voice_id=VOICE,
            voice_pack_dir=str(tmp_path),
        )
    )

    assert isinstance(provider, VoicePackTTSProvider)
    assert provider.manifest_path == manifest


def test_build_tts_skips_pack_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pack(tmp_path)
    monkeypatch.setattr(tts_mod, "EdgeTTSProvider", lambda **kwargs: _Inner())

    provider = build_tts(
        Settings(
            _env_file=None,
            tts_provider="edge",
            voice_pack_dir=str(tmp_path),
            voice_pack_enabled=False,
        )
    )

    assert not isinstance(provider, VoicePackTTSProvider)


def test_build_tts_skips_pack_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tts_mod, "EdgeTTSProvider", lambda **kwargs: _Inner())

    provider = build_tts(
        Settings(_env_file=None, tts_provider="edge", voice_pack_dir=str(tmp_path))
    )

    assert not isinstance(provider, VoicePackTTSProvider)


def test_baked_pack_manifest_is_readable() -> None:
    """仓库里实际 bake 出来的包必须能读：防止脚本产物格式与运行期解析漂移。"""
    path = Path("static/voices/manifest.json")
    if not path.is_file():
        pytest.skip("尚未运行 scripts/bake_voice_pack.py")

    entries = load_manifest(path)
    assert entries
    assert all(isinstance(entry.get("voice"), str) for entry in entries.values())
