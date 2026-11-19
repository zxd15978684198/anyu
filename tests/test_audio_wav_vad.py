"""task-9 验收：坏 WAV 头修复（T7 / W1）+ 服务端 VAD 接线（T4 / W2）。

两件事必须同时成立才算修好，缺一条都是假修复：

1. **坏容器不得进 ASR**（a0-F02）：WAV 头解析失败必须 422 `VALIDATION_ERROR`，且必须
   在 `provider.stream()` 之前返回。旧实现 `_wav_duration_ms()` 失败 `return 0`，0 ms 被
   30 s 上限判成「合法短音频」直接放行 —— 垃圾字节一路打到 ASR。本文件用计数型 mock
   ASR 把「没被调用」钉成断言，而不是只看状态码。
2. **VAD 只观测、不改轮次**：30 ms 帧喂 `VadSession`，句末只加 `vad_speech_segments`
   计数与日志；轮次仍由 A2 的 `asr_start` / `asr_end` 驱动。喂完帧后断言 `turn_id`
   仍是 None、状态仍是 OPEN，把「没有偷偷改轮次」钉成回归用例。

不连库、不写 static：ASR/TTS 全部走 `dependency_overrides` 的 mock。
`webrtcvad` 装不上时 VAD 用例 `importorskip` 跳过 —— 宁可跳过，不做伪绿。
"""

from __future__ import annotations

import io
import math
import struct
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import audio as audio_api
from app.audio.asr import ASREvent, ASREventKind, ASRResult
from app.audio.tts import TTSUnavailable
from app.audio.webrtc import MediaConfig, MediaSession, MediaState, PcmFrame
from app.core.auth import current_device
from app.core.runtime_context import Identity

# --------------------------------------------------------------------------- 夹具


class MockASR:
    """计数型 ASR：`stream_calls` 是「ASR 有没有被调用」的唯一证据。"""

    def __init__(self, text: str = "我有点头晕") -> None:
        self.text = text
        self.stream_calls = 0
        self.frames_received = 0

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        # 契约（asr.py 的 ASRProvider）：stream 是协程，返回 AsyncIterator，
        # 不是 async generator —— audio.py 里是 `await asr.stream(...)`。
        self.stream_calls += 1
        return self._events(audio)

    async def _events(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        async for _frame in audio:
            self.frames_received += 1
        yield ASREvent(
            kind=ASREventKind.FINAL,
            result=ASRResult(text=self.text, is_final=True, confidence=None),
            error=None,
        )


class UnavailableTTS:
    """TTS 一律不可用：audio_url=null + 降级话术，且不往 static/ 写文件。"""

    async def synthesize(self, text: str) -> bytes:
        raise TTSUnavailable("tts disabled in test")


def _fake_identity() -> Identity:
    # device_id 必须是规范 UUID（runtime_context._canon 会校验），不能用任意文本
    return Identity(kind="device", device_id="11111111-1111-4111-8111-111111111111", elder_id=None)


def _build_app(asr: Any, tts: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(audio_api.router)
    app.dependency_overrides[current_device] = _fake_identity
    app.dependency_overrides[audio_api._get_default_asr] = lambda: asr
    app.dependency_overrides[audio_api._get_default_tts] = lambda: tts
    return app


def _make_wav(num_samples: int = 16_000, *, sample_rate: int = 16_000) -> bytes:
    """标准 WAV（16k / mono / s16）：与 `_frame_iter()` 的校验口径一致。"""
    pcm = struct.pack(f"<{num_samples}h", *([0] * num_samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def _post_wav(app: FastAPI, payload: bytes) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/api/audio/utterance",
            files={"audio": ("utterance.wav", payload, "audio/wav")},
        )


# ===========================================================================
# T7 / W1 · 坏 WAV 头：解析失败 → None（不是 0）
# ===========================================================================


class TestWavDurationParse:
    def test_valid_wav_returns_ms(self) -> None:
        assert audio_api._wav_duration_ms(_make_wav(16_000)) == 1000

    def test_garbage_returns_none_not_zero(self) -> None:
        """核心回归：旧实现返回 0，0 会被 30 s 上限放行。"""
        assert audio_api._wav_duration_ms(b"not a wav") is None

    def test_riff_without_chunks_returns_none(self) -> None:
        """`b"RIFF" + 96 个 0`：断头容器，必须判定为无法解析。"""
        assert audio_api._wav_duration_ms(b"RIFF" + b"\x00" * 96) is None

    def test_truncated_fmt_chunk_returns_none(self) -> None:
        """fmt 分块声明的长度远超实际字节：wave 抛 EOFError，不能漏成 500。"""
        truncated = (
            b"RIFF"
            + (36).to_bytes(4, "little")
            + b"WAVE"
            + b"fmt "
            + (9_999).to_bytes(4, "little")
            + struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
        )
        assert audio_api._wav_duration_ms(truncated) is None


# ===========================================================================
# T7 / W1 · 接口层：坏头上 422 VALIDATION_ERROR，且不进 ASR
# ===========================================================================


class TestBadWavRejected:
    @pytest.mark.asyncio
    async def test_garbage_body_422_and_asr_not_called(self) -> None:
        asr = MockASR()
        app = _build_app(asr, UnavailableTTS())

        resp = await _post_wav(app, b"not a wav")

        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert body["trace_id"]
        assert "wav" in body["detail"].lower()
        # 钥匙断言：ASR 依赖根本没被调用，坏字节没进 `_frame_iter` / `stream()`
        assert asr.stream_calls == 0
        assert asr.frames_received == 0

    @pytest.mark.asyncio
    async def test_corrupt_riff_header_422(self) -> None:
        """旧 test_audio_security 断言此类输入仍得 200 —— 那正是要被修掉的行为。"""
        asr = MockASR()
        app = _build_app(asr, UnavailableTTS())

        resp = await _post_wav(app, b"RIFF" + b"\x00" * 96)

        assert resp.status_code == 422
        assert resp.json()["code"] == "VALIDATION_ERROR"
        assert asr.stream_calls == 0

    @pytest.mark.asyncio
    async def test_valid_wav_still_ok(self) -> None:
        """修 bug 不能顺手把正常链路弄坏：合法 WAV 仍然 200 且话术不变。"""
        asr = MockASR(text="我想听越剧")
        app = _build_app(asr, UnavailableTTS())

        resp = await _post_wav(app, _make_wav(16_000))

        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "我想听越剧"
        # 回复来自对话链，不再是写死回显；TTS 不可用时仍要有文字（不白屏）
        reply_text = body["reply"]["text"]
        assert reply_text.strip(), "对话链给出了空回复"
        assert "您说的是" not in reply_text, "回复仍是写死的回显话术"
        assert body["reply"]["audio_url"] is None  # TTS 不可用 → 降级，不白屏
        assert asr.stream_calls == 1


# ===========================================================================
# T4 / W2 · 服务端 VAD 接线（旁路观测，不改轮次）
# ===========================================================================


def _config() -> MediaConfig:
    return MediaConfig(
        stun_url="",
        turn_url="",
        turn_username="",
        turn_credential="",
        turn_credential_ttl_seconds=0,
        ice_timeout_seconds=5,
        audio_bitrate_kbps=32,
        vad_aggressiveness=2,
    )


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _silence_frame(index: int = 0) -> bytes:
    """480 样本 / 960 字节（30 ms）：1 Hz 幅值 5 的低能量正弦。

    不用全 0 帧：webrtcvad aggressive=2 对全 0 的判定不稳定，会让用例随机变红。
    """
    frame_samples = 480
    sample_rate = 16_000
    samples: list[int] = []
    for n in range(frame_samples):
        t = (index * frame_samples + n) / sample_rate
        samples.append(int(5 * math.sin(2 * math.pi * 1.0 * t)))
    return _pcm(samples)


def _voiced_frame(index: int) -> bytes:
    """480 样本浊音帧（150/450/1200/2400 Hz 叠加 + 3 Hz 包络）。"""
    frame_samples = 480
    sample_rate = 16_000
    samples: list[int] = []
    for n in range(frame_samples):
        t = (index * frame_samples + n) / sample_rate
        envelope = 0.6 + 0.4 * math.sin(2 * math.pi * 3.0 * t)
        value = (
            0.55 * math.sin(2 * math.pi * 150 * t)
            + 0.25 * math.sin(2 * math.pi * 450 * t)
            + 0.12 * math.sin(2 * math.pi * 1200 * t)
            + 0.08 * math.sin(2 * math.pi * 2400 * t)
        )
        samples.append(int(max(-1.0, min(1.0, value * envelope)) * 20_000))
    return _pcm(samples)


def _speech_pattern() -> list[bytes]:
    """30 帧静音 + 5 帧语音 + 35 帧静音 → 恰好一段完整语音。

    尾部静音必须 ≥ silence_frames(27) + webrtcvad 的 ~3 帧 hangover，否则收不到 speechend。
    """
    frames = [_silence_frame(i) for i in range(30)]
    frames += [_voiced_frame(i) for i in range(5)]
    frames += [_silence_frame(i) for i in range(100, 135)]
    return frames


class TestServerVadWiring:
    @pytest.mark.asyncio
    async def test_speech_segments_counted(self) -> None:
        pytest.importorskip("webrtcvad", reason="服务端 webrtcvad 未安装，跳过 VAD 接线用例")
        session = MediaSession(connection_id="c-vad", trace_id="t-vad", config=_config())
        try:
            assert session.vad_available is True
            frames = [PcmFrame(data=raw) for raw in _speech_pattern()]

            # 只喂 VAD 帧、不给 ASR 帧：证明 VAD 走的是旁路，不依赖轮次输入
            session._dispatch_frames([], frames)

            assert session.vad_speech_segments >= 1
            # 轮次语义未被触碰：没有 turn、状态没跳
            assert session.turn_id is None
            assert session.state is MediaState.OPEN
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_close_flushes_pending_segment(self) -> None:
        pytest.importorskip("webrtcvad", reason="服务端 webrtcvad 未安装，跳过 VAD 接线用例")
        session = MediaSession(connection_id="c-vad2", trace_id="t-vad2", config=_config())
        frames = [PcmFrame(data=_silence_frame(i)) for i in range(10)]
        frames += [PcmFrame(data=_voiced_frame(i)) for i in range(10)]
        session._dispatch_frames([], frames)  # 句尾停在这里：尚未 speechend
        before = session.vad_speech_segments

        await session.close()

        # 收尾 flush 应把悬挂的语音段收口（计数 +1），且不影响 ASR 队列
        assert before == 0  # 句尾停在语音中，正常路径吐不出段
        assert session.vad_speech_segments >= 1

    @pytest.mark.asyncio
    async def test_vad_unavailable_degrades_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """webrtcvad 缺失（构造失败）→ no-op + warn，会话照常可用。"""
        from app.audio import webrtc as webrtc_module

        monkeypatch.setattr(
            webrtc_module,
            "_build_vad_session",
            lambda *_a, **_k: (None, "ModuleNotFoundError"),
        )
        session = MediaSession(connection_id="c-vad3", trace_id="t-vad3", config=_config())
        try:
            assert session.vad_available is False
            assert session.vad_disabled_reason == "vad_unavailable"

            frames = [PcmFrame(data=raw) for raw in _speech_pattern()]
            session._dispatch_frames([], frames)  # 不能抛

            assert session.vad_speech_segments == 0
            assert session.state is MediaState.OPEN
        finally:
            await session.close()
