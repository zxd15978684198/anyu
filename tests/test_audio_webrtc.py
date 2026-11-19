"""app/audio/webrtc.py 单元测试 · A3 媒体层验收。

覆盖 A3 执行计划 §6.1 的 12 个单元级场景（A3-01 ~ A3-12）。
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import av
import pytest

from app.audio import webrtc
from app.audio.asr import ASREvent, ASREventKind, ASRResult, ASRUnavailable
from app.audio.webrtc import (
    ASR_FRAME_BYTES,
    CANONICAL_FRAME_BYTES,
    DOWNLINK_FRAME_BYTES,
    DOWNLINK_FRAME_SAMPLES,
    DOWNLINK_SAMPLE_RATE,
    MAX_PLAYBACK_FRAMES,
    AudioNormalizer,
    DualFrameSplitter,
    MediaBackpressureError,
    MediaConfigError,
    MediaFormatError,
    MediaLoopMismatch,
    MediaSession,
    MediaSessionRegistry,
    MediaState,
    MediaStateError,
    PcmFrame,
    PcmFramer,
    PlaybackAudioTrack,
    PreRollBuffer,
    _make_frame,
    _tts_pcm_to_48k_frames,
    validate_downlink_pcm,
)

# --------------------------------------------------------------------------- 工具


def _config(**overrides: Any):
    from app.audio.webrtc import MediaConfig

    kwargs = {
        "stun_url": "",
        "turn_url": "",
        "turn_username": "",
        "turn_credential": "",
        "turn_credential_ttl_seconds": 0,
        "ice_timeout_seconds": 5,
        "audio_bitrate_kbps": 32,
        "vad_aggressiveness": 2,
    }
    kwargs.update(overrides)
    return MediaConfig(**kwargs)


def _pcm_frame(duration_ms: int, *, sample_rate: int = 16_000) -> PcmFrame:
    """构造一段纯 500 Hz 正弦 PCM16 单声道帧（16 kHz）。"""
    import math

    n_samples = int(sample_rate * duration_ms / 1000)
    raw = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        val = int(3000 * math.sin(2 * math.pi * 500 * t))
        val = max(-32768, min(32767, val))
        raw.extend(struct.pack("<h", val))
    return _make_frame(bytes(raw), duration_ms=duration_ms)


def _silence_frame(duration_ms: int) -> PcmFrame:
    return _make_frame(b"\x00" * (16_000 * duration_ms // 1000 * 2), duration_ms=duration_ms)


def _audio_frame(sample_rate: int, channels: int, samples: int) -> av.AudioFrame:
    """构造一个 av.AudioFrame（s16，有正峰值避免 resampler 零输入空跑）。"""
    import math

    frame = av.AudioFrame(
        format="s16",
        layout=f"{'stereo' if channels == 2 else 'mono'}",
        samples=samples,
    )
    frame.sample_rate = sample_rate
    raw = bytearray()
    for i in range(samples * channels):
        t = i / sample_rate
        val = int(2000 * math.sin(2 * math.pi * 440 * t))
        raw.extend(struct.pack("<h", val))
    frame.planes[0].update(bytes(raw))
    return frame


def _valid_sdp() -> str:
    """aiortc 能接受的最小合法 offer SDP（含 ICE ufrag/pwd + rtcp-mux + sendrecv direction）。"""
    return (
        "v=0\r\n"
        "o=- 1730000000000 2 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "a=group:BUNDLE 0\r\n"
        "a=msid-semantic: WMS testmsid\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111 103 104 0 8\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "a=rtcp:9 IN IP4 0.0.0.0\r\n"
        "a=ice-ufrag:testufrag\r\n"
        "a=ice-pwd:testpassword1234567890\r\n"
        "a=fingerprint:sha-256 00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF\r\n"
        "a=setup:actpass\r\n"
        "a=mid:0\r\n"
        "a=rtpmap:111 opus/48000/2\r\n"
        "a=rtpmap:103 ISAC/16000\r\n"
        "a=rtpmap:104 ISAC/32000\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtcp-mux:true\r\n"
        "a=sendrecv\r\n"
    )


# ===========================================================================
# A3-01 · 帧格式转换：48 kHz / 双声道 → 16 kHz / mono / s16
# ===========================================================================


class TestA301FrameConversion:
    def test_48k_stereo_to_16k_mono(self) -> None:
        normalizer = AudioNormalizer()
        total_frames: list[PcmFrame] = []
        for _ in range(5):
            src = _audio_frame(48_000, 2, 960)  # 20 ms stereo 48k
            total_frames.extend(normalizer.feed(src))
        tail = normalizer.flush()
        total_frames.extend(tail)
        assert len(total_frames) >= 1
        for f in total_frames:
            assert f.sample_rate == 16_000
            assert f.channels == 1
            assert f.sample_width == 2

    def test_60_seconds_produces_1000_frames(self) -> None:
        normalizer = AudioNormalizer()
        total_frames: list[PcmFrame] = []
        # 60 s / 20 ms = 3000 个 stereo 48k 源帧
        for _ in range(3000):
            total_frames.extend(normalizer.feed(_audio_frame(48_000, 2, 960)))
        tail = normalizer.flush()
        total_frames.extend(tail)
        # 60 s / 20 ms = 3000 个 canonical
        assert len(total_frames) >= 2990

    def test_output_frame_size_1920(self) -> None:
        """通过 DualFrameSplitter 聚合 → 60 ms ASR 帧 1920 B。"""
        splitter = DualFrameSplitter()
        for _ in range(3):
            splitter.feed(_pcm_frame(20))
        asr_frames, _ = splitter.flush()
        for f in asr_frames:
            assert len(f.data) == ASR_FRAME_BYTES  # 1920
            assert f.duration_ms == 60.0


# ===========================================================================
# A3-02 · 30 / 60 分叉：同一 60 ms 帧 → ASR 1920 B / VAD 两个 960 B
# ===========================================================================


class TestA302DualFrameSplit:
    def test_split_60ms_into_60_asr_and_2x30_vad(self) -> None:
        splitter = DualFrameSplitter()
        # feed() 每次都会 drain 满帧，收集全部
        asr_all: list[PcmFrame] = []
        vad_all: list[PcmFrame] = []
        for _ in range(3):
            a, v = splitter.feed(_pcm_frame(20))
            asr_all.extend(a)
            vad_all.extend(v)
        # flush 处理剩余尾片
        a, v = splitter.flush()
        asr_all.extend(a)
        vad_all.extend(v)

        assert len(asr_all) >= 1
        assert len(asr_all[0].data) == ASR_FRAME_BYTES  # 1920
        assert len(vad_all) >= 2
        for f in vad_all:
            assert len(f.data) == 960

    def test_rejects_non_16k_mono_s16_input(self) -> None:
        splitter = DualFrameSplitter()
        non_canonical = PcmFrame(
            data=b"\x00" * (44_100 * 2),
            sample_rate=44_100,
            channels=1,
        )
        with pytest.raises(MediaFormatError):
            splitter.feed(non_canonical)


# ===========================================================================
# A3-03 · preRoll：说话前 600 ms 缓冲 → 首 10 帧完整补发
# ===========================================================================


class TestA303PreRoll:
    def test_holds_recent_10_frames(self) -> None:
        buf = PreRollBuffer()
        # 12 帧 60 ms → 只保留最后 10
        for _ in range(12):
            buf.append(_pcm_frame(60))
        recent = buf.recent()
        assert len(recent) == 10
        assert buf.duration_ms == 600.0

    def test_rejects_non_60ms_frame(self) -> None:
        buf = PreRollBuffer()
        with pytest.raises(MediaFormatError):
            buf.append(_pcm_frame(30))


# ===========================================================================
# A3-04 · 尾帧零填充：残片 → 零填充到整帧
# ===========================================================================


class TestA304TailPadding:
    def test_10ms_tail_zero_padded(self) -> None:
        framer = PcmFramer(frame_bytes=CANONICAL_FRAME_BYTES, duration_ms=20)
        # 10 ms PCM16 = 320 B
        tail = b"\xab" * 320
        frames = framer.feed(tail)
        assert frames == []
        padded = framer.flush()
        assert padded is not None
        assert len(padded.data) == CANONICAL_FRAME_BYTES
        # 10ms 有效 + 10ms 零填充
        assert padded.data[320:] == b"\x00" * 320

    def test_zero_tail_returns_none(self) -> None:
        framer = PcmFramer()
        assert framer.flush() is None


# ===========================================================================
# A3-05 · loop 绑定：跨 loop 调用 → MediaLoopMismatch
# ===========================================================================


class TestA305LoopBinding:
    def test_create_outside_loop_raises(self) -> None:
        with pytest.raises(MediaLoopMismatch):
            MediaSession(connection_id="c1", trace_id="t1", config=_config())

    def test_create_inside_loop_ok(self) -> None:
        async def _inner() -> None:
            sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
            assert sess.state is MediaState.OPEN
            await sess.close()

        asyncio.run(_inner())


# ===========================================================================
# A3-06 · 幂等关闭：连续关闭 3 次 → 无异常，state=closed
# ===========================================================================


class TestA306IdempotentClose:
    @pytest.mark.asyncio
    async def test_close_three_times_no_error(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        await sess.close()
        await sess.close()
        await sess.close()
        assert sess.state is MediaState.CLOSED


# ===========================================================================
# A3-07 · 打断：barge_in 清队列 + 旧 turn 入队拒绝
# ===========================================================================


class _HangingASR:
    """消费完输入后永不吐终稿：用来钉住 end_turn 不阻塞信令回环。"""

    async def stream(self, audio):
        return self._events(audio)

    async def _events(self, audio):
        async for _ in audio:
            pass
        await asyncio.Event().wait()
        yield  # pragma: no cover - 只为保持 async generator 形态


class _DelayedFinalASR:
    """消费完输入后延迟吐终稿：模拟 FunASR 2pass 的 offline 重跑（实测约 17s）。"""

    def __init__(self, delay: float, text: str = "今天天气怎么样") -> None:
        self._delay = delay
        self._text = text

    async def stream(self, audio):
        return self._events(audio)

    async def _events(self, audio):
        async for _ in audio:
            pass
        await asyncio.sleep(self._delay)
        yield ASREvent(
            kind=ASREventKind.FINAL,
            result=ASRResult(text=self._text, is_final=True, confidence=None),
            error=None,
        )


class TestA307BargeIn:
    @pytest.mark.asyncio
    async def test_barge_in_clears_queue_and_rejects_old_turn(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        turn_id = await sess.begin_turn()
        await asyncio.sleep(0.05)

        # 先 enqueue 一小段 100ms
        await sess.enqueue_tts_pcm(turn_id, b"\x00" * 3200)
        assert sess.playback_queue_depth > 0

        # barge_in：清队列 + 清空 playback_turn_id
        sess.barge_in()
        assert sess.playback_queue_depth == 0
        assert sess.playback_generation == 1

        # 旧 turn_id 再入队 → MediaStateError
        with pytest.raises(MediaStateError):
            await sess.enqueue_tts_pcm(turn_id, b"\x00" * 3200)

        await sess.close()


# ===========================================================================
# P2 · 连续说话：ASR 收尾不堵信令回环，且不误清新 turn
# ===========================================================================


class TestTurnFinalizeBounded:
    @pytest.mark.asyncio
    async def test_end_turn_returns_when_provider_never_finalizes(self) -> None:
        """provider 卡住时 end_turn 不等 provider，也不取消它（离线终稿要跑十几秒）。"""
        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(),
            asr_provider=_HangingASR(),
        )
        await sess.begin_turn()

        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(sess.end_turn(), timeout=0.5)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.4, "end_turn 不该无限期等 provider 的 offline 终稿"
        assert sess.turn_id is None
        await sess.close()

    @pytest.mark.asyncio
    async def test_asr_end_handler_returns_before_finalize(self) -> None:
        """信令处理函数只负责调度收尾，不能 await 到 provider 终稿才返回。"""
        registry = MediaSessionRegistry()
        session = registry.create_session(
            "c1",
            "t1",
            config=_config(),
            asr_provider=_HangingASR(),
        )
        await session.begin_turn()

        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(webrtc._handle_asr_end(registry, "c1"), timeout=0.1)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.05, "asr_end 必须立即回到信令回环"
        assert session._end_turn_task is not None
        await asyncio.wait_for(session._end_turn_task, timeout=0.5)
        await registry.close_session("c1")

    @pytest.mark.asyncio
    async def test_detached_finalize_still_delivers_offline_final(self) -> None:
        """end_turn 返回后，延迟到达的 offline 终稿仍要交给编排层（不能提前取消）。"""
        finals: list[ASREvent] = []

        async def on_event(turn_id: str, event: ASREvent) -> None:
            if event.kind is ASREventKind.FINAL:
                finals.append(event)

        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(),
            asr_provider=_DelayedFinalASR(delay=0.05),
            on_asr_event=on_event,
        )
        turn_id = await sess.begin_turn()
        await asyncio.wait_for(sess.end_turn(turn_id), timeout=0.5)

        for _ in range(20):
            if finals:
                break
            await asyncio.sleep(0.02)

        assert [event.result.text for event in finals if event.result] == ["今天天气怎么样"]
        await sess.close()

    @pytest.mark.asyncio
    async def test_new_turn_survives_late_previous_finalize(self) -> None:
        """上一轮收尾晚到时，begin_turn 取消它后必须保留新 turn_id。"""
        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(),
            asr_provider=_HangingASR(),
        )
        first = await sess.begin_turn()

        ending = asyncio.create_task(sess.end_turn(first))
        sess._end_turn_task = ending
        await asyncio.sleep(0)

        second = await sess.begin_turn()
        assert sess.turn_id == second, "旧轮的 end_turn 不能把新轮状态清空"

        await asyncio.wait_for(ending, timeout=0.5)
        assert sess.turn_id == second
        await sess.close()

    @pytest.mark.asyncio
    async def test_late_final_from_replaced_turn_is_dropped(self) -> None:
        """旧轮终稿晚于新轮开口才到：必须丢弃，不能用旧 turn_id 触发对话。"""
        finals: list[str] = []

        async def on_event(turn_id: str, event: ASREvent) -> None:
            if event.kind is ASREventKind.FINAL:
                finals.append(turn_id)

        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(),
            asr_provider=_DelayedFinalASR(delay=0.08),
            on_asr_event=on_event,
        )
        first = await sess.begin_turn()
        await asyncio.wait_for(sess.end_turn(first), timeout=0.5)
        second = await sess.begin_turn()

        await asyncio.sleep(0.2)

        assert finals == []
        assert sess.turn_id == second
        await sess.close()


# ===========================================================================
# A3-08 · TTS 写轨：1 秒 16 kHz PCM → 50 帧 / 1 秒
# ===========================================================================


class TestA308TTSWriteTrack:
    def test_1s_16k_pcm_produces_50_frames_48k(self) -> None:
        pcm = b"\x00" * (16_000 * 2)  # 1 秒 16k mono s16
        frames = _tts_pcm_to_48k_frames(pcm)
        assert len(frames) == 50
        for f in frames:
            assert f.sample_rate == DOWNLINK_SAMPLE_RATE
            assert len(f.data) == DOWNLINK_FRAME_BYTES
            assert f.sample_count == DOWNLINK_FRAME_SAMPLES

    def test_duration_error_le_20ms(self) -> None:
        pcm = b"\x00" * (16_000 * 2)
        frames = _tts_pcm_to_48k_frames(pcm)
        total_ms = sum(f.duration_ms for f in frames)
        assert abs(total_ms - 1000.0) <= 20.0


# ===========================================================================
# 播放游标坐标：字幕时间轴必须把静音帧算进去
# ===========================================================================


class TestPlaybackCursorCoordinate:
    """`ChunkPlacement.start_frame` 是**播放流**里的绝对落位，不是「入队帧数累加」。

    两者的差就是静音：逐句 TTS 是串行合成的，第 k+1 句合成比第 k 句播放慢时，
    `recv()` 会补静音帧把 PTS 往前推。按入队帧数累加出来的字幕时间轴看不见这段
    静音，句数越多偏得越远 —— 这正是「越说越不同步」的结构性根因。
    """

    @pytest.mark.asyncio
    async def test_played_frames_counts_silence(self) -> None:
        track = PlaybackAudioTrack(asyncio.Queue())
        assert track.played_frames == 0
        for _ in range(3):
            await track.recv()
        assert track.played_frames == 3, "空队列补的静音帧同样占播放时间，必须计数"
        track.stop()

    @pytest.mark.asyncio
    async def test_back_to_back_placement_is_queue_depth(self) -> None:
        """没播过任何东西时，第二片紧接第一片：落位 = 第一片的帧数。"""
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        turn_id = await sess.begin_turn()

        first = await sess.enqueue_tts_pcm_chunk_placed(turn_id, b"\x00" * 3200)
        second = await sess.enqueue_tts_pcm_chunk_placed(turn_id, b"\x00" * 3200)

        assert first.frames > 0
        assert first.start_frame == 0
        assert second.start_frame == first.frames

        await sess.close()

    @pytest.mark.asyncio
    async def test_silence_gap_shifts_start_frame(self) -> None:
        """句子之间真的空了一段：后一句的落位必须把这段静音算进去。"""
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        turn_id = await sess.begin_turn()
        track = sess._playback_track
        assert track is not None

        first = await sess.enqueue_tts_pcm_chunk_placed(turn_id, b"\x00" * 3200)
        gap_frames = 3
        for _ in range(first.frames + gap_frames):
            await track.recv()
        assert sess.playback_queue_depth == 0
        assert track.played_frames == first.frames + gap_frames

        second = await sess.enqueue_tts_pcm_chunk_placed(turn_id, b"\x00" * 3200)

        assert second.start_frame == first.frames + gap_frames, (
            "按入队帧数累加会得到 first.frames，字幕从此永久领先 gap_frames * 20ms"
        )

        await sess.close()

    @pytest.mark.asyncio
    async def test_sub_frame_chunk_places_nothing(self) -> None:
        """不足一帧的残片：frames=0 是正常情形，调用方据此跳过这一段字幕。"""
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        turn_id = await sess.begin_turn()

        placed = await sess.enqueue_tts_pcm_chunk_placed(turn_id, b"\x00" * 32)

        assert placed.frames == 0
        assert sess.playback_queue_depth == 0

        await sess.close()


# ===========================================================================
# A3-09 · 背压：超 15 秒音频 → 拒绝整段
# ===========================================================================


class TestA309Backpressure:
    @pytest.mark.asyncio
    async def test_over_15s_rejected_entirely(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        turn_id = await sess.begin_turn()
        await asyncio.sleep(0.05)

        # 装满队列（750 帧 ≈ 15s），留少量空位
        sess._playback_queue = asyncio.Queue(maxsize=MAX_PLAYBACK_FRAMES)
        for _ in range(700):
            sess._playback_queue.put_nowait(_silence_frame(20))

        # 再入队 2s → 700+100=800 > 750 → 背压
        tts_pcm_2s = b"\x00" * (16_000 * 2 * 2)
        with pytest.raises(MediaBackpressureError):
            await sess.enqueue_tts_pcm(turn_id, tts_pcm_2s)

        # PC 不崩
        assert sess.state is not MediaState.FAILED
        await sess.close()


# ===========================================================================
# A3-10 · ICE 超时：不可达候选 → 5-6 秒内 FAILED
# ===========================================================================


class TestA310ICETimeout:
    @pytest.mark.asyncio
    async def test_ice_timeout_moves_to_failed(self) -> None:
        # `ice_grace_seconds=0`：这一例钉的是「ICE 毫无进展时照旧判死」，
        # 宽限窗专给「ICE 已有进展但 DTLS 还没好」的情形（下一例）。
        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(ice_timeout_seconds=2, ice_grace_seconds=0),
        )
        sess.create_peer_connection()

        answer = await sess.handle_offer(_valid_sdp())
        assert answer
        assert sess.state is MediaState.ANSWER_CREATED

        # ICE 看门狗 2s + 少量 guard
        await asyncio.sleep(2.5)
        # 允许两种中间态：FAILED（还在等 close schedule）或 CLOSED（已经跑完）
        assert sess.state in (MediaState.FAILED, MediaState.CLOSED)

        await asyncio.sleep(0.5)
        # 最终一定是 CLOSED
        assert sess.state is MediaState.CLOSED

    @pytest.mark.asyncio
    async def test_ice_progress_gets_grace_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ICE 侧已有进展 → 到点不判死，等满宽限窗；宽限窗过完仍不通才判死。

        现场：aiortc 的候选对 `SUCCEEDED` 比 `connectionState=connected` 早 1~4s，
        只认后者的看门狗会把「马上要通」的会话判死，开场白音频随即丢失。
        """
        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(ice_timeout_seconds=1, ice_grace_seconds=2),
        )
        sess.create_peer_connection()
        await sess.handle_offer(_valid_sdp())

        # 模拟「ICE checking/completed，但 DTLS 还没好」
        monkeypatch.setattr(sess, "_ice_has_progress", lambda: True)

        await asyncio.sleep(1.4)  # 到点 + 宽限窗刚开始
        assert sess.state is MediaState.ANSWER_CREATED  # 没有被一刀切判死

        await asyncio.sleep(2.4)  # 宽限窗过完
        assert sess.state in (MediaState.FAILED, MediaState.CLOSED)
        await sess.close()


# ===========================================================================
# A3-11 · 重复 offer：同连接第二个 offer → 默认拒绝
# ===========================================================================


class TestA311DuplicateOffer:
    @pytest.mark.asyncio
    async def test_second_offer_rejected(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        await sess.handle_offer(_valid_sdp())

        with pytest.raises(MediaStateError):
            await sess.handle_offer(_valid_sdp())

        await sess.close()


# ===========================================================================
# A3-12 · trace_id 全流程一致
# ===========================================================================


class TestA312TraceId:
    @pytest.mark.asyncio
    async def test_trace_id_preserved_throughout(self) -> None:
        expected = "trace-abc-123"
        sess = MediaSession(connection_id="c1", trace_id=expected, config=_config())
        assert sess.trace_id == expected
        sess.create_peer_connection()
        assert sess.trace_id == expected
        await sess.close()
        assert sess.trace_id == expected


# ===========================================================================
# 额外：validate_downlink_pcm / MediaConfig / MediaSessionRegistry
# ===========================================================================


class TestValidateDownlinkPcm:
    def test_rejects_empty(self) -> None:
        with pytest.raises(MediaFormatError):
            validate_downlink_pcm(b"")

    def test_rejects_odd_length(self) -> None:
        with pytest.raises(MediaFormatError):
            validate_downlink_pcm(b"\x00" * 5)

    def test_rejects_wave_header(self) -> None:
        with pytest.raises(MediaFormatError):
            validate_downlink_pcm(b"RIFF....")


class TestMediaConfigValidation:
    def test_rejects_zero_timeout(self) -> None:
        with pytest.raises(MediaConfigError):
            _config(ice_timeout_seconds=0)

    def test_rejects_invalid_vad(self) -> None:
        with pytest.raises(MediaConfigError):
            _config(vad_aggressiveness=5)

    def test_loopback_stun_excluded(self) -> None:
        c = _config(stun_url="stun:localhost:3478", ice_timeout_seconds=1)
        assert c.ice_servers() == []


class TestMediaSessionRegistry:
    @pytest.mark.asyncio
    async def test_registry_lifecycle(self) -> None:
        reg = MediaSessionRegistry()
        config = _config()

        sess1 = reg.create_session("c1", "t1", config=config)
        assert reg.active_sessions == 1
        assert reg.get_session("c1") is sess1

        await reg.close_session("c1")
        assert reg.active_sessions == 0
        assert reg.get_session("c1") is None

    @pytest.mark.asyncio
    async def test_registry_duplicate_connection(self) -> None:
        reg = MediaSessionRegistry()
        config = _config()
        reg.create_session("c1", "t1", config=config)
        with pytest.raises(MediaStateError):
            reg.create_session("c1", "t1", config=config)
        await reg.close_session("c1")


# ===========================================================================
# A3-13 · 服务端外呼：create_offer / accept_answer（迭代 7 · W4）
# ===========================================================================
#
# 既有 A3 系列只覆盖「浏览器 offer → 本端 answer」这一个方向。反向外呼
# （用药到点主动连过去播报）需要本端当 offerer，而 aiortc 的两侧 API 完全不同：
# 走错方向会在 `setRemoteDescription` 里炸成「offer SDP was rejected」，
# 所以这两个方法各自有正向与拒绝路径的用例。


class TestServerInitiatedOffer:
    @pytest.mark.asyncio
    async def test_create_offer_returns_sdp_and_stays_open(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()

        sdp = await sess.create_offer()

        assert "m=audio" in sdp
        assert "a=ice-ufrag:" in sdp
        assert "a=ice-pwd:" in sdp
        # 只是本地描述，远端还没答 —— 状态不许提前跳成 CONNECTING
        assert sess.state is MediaState.OPEN
        await sess.close()

    @pytest.mark.asyncio
    async def test_second_create_offer_is_rejected(self) -> None:
        """一条连接只谈一次：第二次 create_offer 必须拒，否则 offer/answer 串线。"""
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        await sess.create_offer()

        with pytest.raises(MediaStateError):
            await sess.create_offer()

        await sess.close()

    @pytest.mark.asyncio
    async def test_accept_answer_before_offer_is_rejected(self) -> None:
        """没发过 offer 就收到 answer：说明信令串了，必须显式拒而不是当成 offer 处理。"""
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()

        with pytest.raises(MediaStateError):
            await sess.accept_answer(_valid_sdp())

        await sess.close()

    @pytest.mark.asyncio
    async def test_accept_answer_rejects_garbage_sdp(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        await sess.create_offer()

        with pytest.raises(MediaFormatError):
            await sess.accept_answer("v=0\r\n")

        assert sess.state is MediaState.FAILED
        await sess.close()

    @pytest.mark.asyncio
    async def test_accept_answer_rejects_empty_sdp(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        await sess.create_offer()

        with pytest.raises(MediaFormatError):
            await sess.accept_answer("   ")

        await sess.close()

    @pytest.mark.asyncio
    async def test_answer_from_real_peer_moves_session_to_connecting(self) -> None:
        """用另一端真实的 aiortc answer 走一遍：offer → answer → CONNECTING。

        这条用例是 R1 的回归钉子：`accept_answer` 一旦退回「复用 handle_offer」的写法，
        这里会因为 `type="offer"` 与 `createAnswer()` 立刻失败。
        """
        offerer = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        answerer = MediaSession(connection_id="c2", trace_id="t2", config=_config())
        offerer.create_peer_connection()
        answerer.create_peer_connection()

        offer = await offerer.create_offer()
        answer = await answerer.handle_offer(offer)
        assert "m=audio" in answer

        await offerer.accept_answer(answer)

        assert offerer.state is MediaState.CONNECTING
        assert answerer.state is MediaState.ANSWER_CREATED

        await offerer.close()
        await answerer.close()


# --------------------------------------------------------------------------- ASR 首包超时（迭代 5 §4.2）
class _CollectingSession:
    """只实现 `_consume_asr` 会读到的字段/方法（unbound 调用，避免起真 WebRTC）。"""

    def __init__(self) -> None:
        self.trace_id = "trace-asr"
        self.connection_id = "conn-1"
        self._state = MediaState.CONNECTED
        self.events: list[tuple[str, ASREvent]] = []
        self.failures: list[tuple[Any, str]] = []

    async def _emit_asr_event(self, turn_id: str, event: ASREvent) -> None:
        self.events.append((turn_id, event))

    async def _notify_failure(self, reason: Any, code: str) -> None:
        self.failures.append((reason, code))


class _StuckASR:
    """连接成功但首包永远不来的 ASR（模拟云侧静默）。"""

    async def stream(self, audio: Any) -> Any:
        async def _never() -> Any:
            await asyncio.sleep(10.0)
            yield ASREvent(kind=ASREventKind.FINAL, result=None, error=None)

        return _never()


class _OneFinalASR:
    """首包即终稿的最小 ASR。"""

    async def stream(self, audio: Any) -> Any:
        async def _events() -> Any:
            yield ASREvent(kind=ASREventKind.FINAL, result=None, error=None)

        return _events()


async def test_consume_asr_first_packet_timeout_emits_error(monkeypatch: Any) -> None:
    """ASR 首包超时（默认 2s 档）→ 发 ERROR 事件转字幕，不许静默挂住整轮。"""
    monkeypatch.setattr("app.core.timeouts.resolve_timeout_ms", lambda stage: 10)
    session = _CollectingSession()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    await MediaSession._consume_asr(session, "turn-1", _StuckASR(), queue)  # type: ignore[arg-type]

    assert len(session.events) == 1
    turn_id, event = session.events[0]
    assert turn_id == "turn-1"
    assert event.kind is ASREventKind.ERROR
    assert isinstance(event.error, ASRUnavailable)
    assert session.failures == [], "超时是预期降级，不应升级成任务失败"


async def test_consume_asr_final_event_is_emitted_once(monkeypatch: Any) -> None:
    """正常路径回归：首个 FINAL 事件只 emit 一次，且不再重复消费。"""
    monkeypatch.setattr("app.core.timeouts.resolve_timeout_ms", lambda stage: 100)
    session = _CollectingSession()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    await MediaSession._consume_asr(session, "turn-2", _OneFinalASR(), queue)  # type: ignore[arg-type]

    assert [event.kind for _turn, event in session.events] == [ASREventKind.FINAL]
    assert session.failures == []


# ===========================================================================
# N62f · 反向外呼两个阻断缺陷的回归钉子（2026-11-10）
# ===========================================================================
#
# 缺陷 A：`offer` 曾被下发两次 —— `SignalingOfferer.create_offer()` 内部
# `_deliver` 一次，`outbound._establish` 的 `sender.send(...)` 再一次。设备收到两条
# offer 就建两个 `RTCPeerConnection`、回两个 answer；第二个 answer 落到已 CONNECTING
# 的会话上抛 `MediaFormatError`，会话随即被关，播报以 `MediaClosedError` 收场 ——
# 迭代 7 外呼因此从未跑通。单出口留在 `outbound`（信封形状与「离线 → DeviceOfflineError」
# 都在那儿），这里钉住「create_offer 不再自己投递」与「abandon 把等待位/会话收干净」。
#
# 缺陷 B：系统播报闸门曾要求 `state is OPEN`，而外呼拿到 answer 之后状态是
# CONNECTING / CONNECTED，于是开场白与外呼播报永远入不了队（「连上了但没人说话」）。
# 口径改成与 `_ensure_open()` 一致：只拒 CLOSING / CLOSED / FAILED。


class _StubOfferSession:
    """`SignalingOfferer` 只用到 `create_offer()` 与 `state`。"""

    def __init__(self) -> None:
        self.state = MediaState.OPEN
        self.create_calls = 0

    async def create_offer(self) -> str:
        self.create_calls += 1
        return _valid_sdp()


class _StubRegistry:
    """只实现 `SignalingOfferer` 会用到的两个方法。"""

    def __init__(self, session: _StubOfferSession) -> None:
        self.session = session
        self.closed: list[str] = []

    def create_session(self, device_id: str, trace_id: str, **_callbacks: Any) -> _StubOfferSession:
        return self.session

    async def close_session(self, device_id: str, *, reason: str = "") -> None:
        self.closed.append(reason)


class TestOutboundOfferSingleExit:
    @pytest.mark.asyncio
    async def test_create_offer_does_not_deliver(self, monkeypatch: Any) -> None:
        """缺陷 A：`create_offer()` 只建 offer，投递归 `outbound` 的 sender。"""
        delivered: list[dict[str, Any]] = []

        def _spy(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append(dict(message))
            return True

        session = _StubOfferSession()
        registry = _StubRegistry(session)
        monkeypatch.setattr(webrtc, "_deliver", _spy)
        monkeypatch.setattr(webrtc, "_signaling_registry", lambda: registry)
        offerer = webrtc.SignalingOfferer("dev-single-exit")

        try:
            sdp = await offerer.create_offer()

            assert sdp == _valid_sdp()
            assert session.create_calls == 1
            # 双发的那一份已经删掉：这里一条都不能有（多一条 = 设备收到两条 offer）
            assert delivered == []
            assert webrtc._answer_waiters()["dev-single-exit"].done() is False
        finally:
            await offerer.abandon()

    @pytest.mark.asyncio
    async def test_abandon_clears_waiter_and_closes_session(self, monkeypatch: Any) -> None:
        """下发失败时 `outbound` 调 `abandon()`：等待位收掉、会话关掉，不留悬空。"""
        session = _StubOfferSession()
        registry = _StubRegistry(session)
        monkeypatch.setattr(webrtc, "_signaling_registry", lambda: registry)
        offerer = webrtc.SignalingOfferer("dev-abandon")
        await offerer.create_offer()
        waiter = webrtc._answer_waiters()["dev-abandon"]

        await offerer.abandon()

        assert "dev-abandon" not in webrtc._answer_waiters()
        assert waiter.done() is True
        assert waiter.result() is False
        assert registry.closed[-1] == "offer_not_delivered"


class TestSystemTtsPlayableStates:
    """缺陷 B：系统播报只看「会话是不是已经关了」。"""

    @pytest.mark.asyncio
    async def test_connected_session_accepts_system_tts(self) -> None:
        """外呼的真实时序：answer 之后是 CONNECTING，ICE 通是 CONNECTED —— 都该能播。"""
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            session._state = MediaState.CONNECTED
            ok = await session.enqueue_system_tts_pcm(b"\x00" * 3200)
            assert ok is True
            assert session.playback_queue_depth > 0
        finally:
            session._state = MediaState.OPEN
            await session.close()

    @pytest.mark.asyncio
    async def test_connecting_session_accepts_system_tts(self) -> None:
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            session._state = MediaState.CONNECTING
            assert await session.enqueue_system_tts_pcm(b"\x00" * 3200) is True
        finally:
            session._state = MediaState.OPEN
            await session.close()

    @pytest.mark.asyncio
    async def test_closing_session_rejects_system_tts(self) -> None:
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            session._state = MediaState.CLOSING
            assert await session.enqueue_system_tts_pcm(b"\x00" * 3200) is False
            assert session.playback_queue_depth == 0
        finally:
            session._state = MediaState.OPEN
            await session.close()

    @pytest.mark.asyncio
    async def test_closed_session_rejects_system_tts(self) -> None:
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        await session.close()

        assert await session.enqueue_system_tts_pcm(b"\x00" * 3200) is False
