"""打字回复的静态音频合成（`app/services/tts_audio_service.py`）不依赖数据库的部分。

为什么单独一份：`tests/test_dialogue_tts_api.py` 走 HTTP + 设备令牌，需要 PostgreSQL；
本文件只测「合成 → 切句时间窗 → 逐字坐标」这段纯计算，用一个假 TTS provider 就能跑，
数据库不可用时仍能守住打字轮的字幕坐标口径。

复跑：uv run pytest tests/test_tts_audio_service.py -q
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app.services.tts_audio_service import synthesize_to_static


def _pcm_frames(*amplitudes: int) -> bytes:
    """每帧 20ms @16k/mono/s16le = 320 样点；用幅度造「有声 / 静音」帧。"""
    return b"".join(struct.pack("<320h", *([amp] * 320)) for amp in amplitudes)


class _FakeTts:
    """按句返回预置 PCM；`synthesize` 的冻结契约是「一句文本 → 一段 PCM」。"""

    def __init__(self, chunks: dict[str, bytes]) -> None:
        self._chunks = chunks
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return self._chunks[text]


@pytest.fixture(autouse=True)
def _static_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """落盘目录按进程 cwd 解析，指到 tmp_path 免得往仓库 static/ 里写测试音频。"""
    monkeypatch.chdir(tmp_path)


async def test_segments_carry_char_offsets_matching_sentence_length() -> None:
    """每句都带 char_ms，长度 = 该句码点数，末位 = 该句时长（否则最后一个字打不出来）。"""
    # 「记得吃药」：前 400ms 有声 → 中间 800ms 静音 → 后 400ms 有声。
    voiced = _pcm_frames(*([12_000] * 20))
    silence = _pcm_frames(*([0] * 40))
    tts = _FakeTts({"记得吃药": voiced + silence + voiced})

    audio = await synthesize_to_static(tts, "记得吃药")

    assert audio is not None
    assert len(audio.segments) == 1
    segment = audio.segments[0]
    assert segment.duration_ms == 1600
    assert len(segment.char_ms) == 4
    assert segment.char_ms[-1] == segment.duration_ms
    # 停顿不吃字：第 2 个字在 400ms 收尾，第 3 个字要等到停顿之后（1400ms）。
    assert segment.char_ms[1] == 400
    assert segment.char_ms[2] == 1400


async def test_multi_sentence_offsets_are_relative_to_each_sentence() -> None:
    """start_ms 累计整段，char_ms 只相对本句——两套坐标不能混。"""
    chunk = _pcm_frames(*([10_000] * 25))  # 500ms
    tts = _FakeTts({"今天天气晴。": chunk, "记得戴帽子。": chunk})

    audio = await synthesize_to_static(tts, "今天天气晴。记得戴帽子。")

    assert audio is not None
    assert [s.start_ms for s in audio.segments] == [0, 500]
    for segment in audio.segments:
        assert segment.char_ms[-1] == 500, "句内坐标被整段起点污染了"
        assert segment.char_ms[0] <= 500


async def test_silent_pcm_still_yields_monotonic_offsets() -> None:
    """Mock/纯静音 PCM 不能返回全 0：那会让整句字幕钉在第一个字上。"""
    tts = _FakeTts({"你好呀": _pcm_frames(*([0] * 30))})

    audio = await synthesize_to_static(tts, "你好呀")

    assert audio is not None
    offsets = audio.segments[0].char_ms
    assert offsets == (200, 400, 600)


async def test_empty_text_returns_none() -> None:
    assert await synthesize_to_static(_FakeTts({}), "   ") is None
