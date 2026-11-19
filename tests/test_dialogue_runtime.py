"""迭代 4 收口 W4/W5 · 对话链路接线的语义测试。

覆盖三件事，都是「接错了不会报错、只会静默做错事」的那类：

1. **编排层**（`DialogueRuntime`）：什么时候该回、什么时候必须装死、回的顺序。
2. **信号层的终稿钩子**（`signaling.set_dialogue_final_handler` / `deliver_asr_event`）：
   `turn_id` 有没有真的被传下去 —— 丢了这个参数，回复音频永远会被 `stale turn` 拒掉。
3. **系统播放入口**（`MediaSession.enqueue_system_tts_pcm`）：开场白能出声，但**不能**
   绕过打断语义 —— 代际号变了必须丢弃，否则长者说话时耳边会响起「您好呀」。

刻意不测的东西：TTS 合成质量、LLM 内容（各有专门文件），以及需要真 Redis 的路径
（`RedisTurnStore` 已在 `tests/test_dialogue_service.py` 用假 client 覆盖）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.audio import media_bridge, signaling
from app.audio import webrtc as webrtc_module
from app.audio.webrtc import ChunkPlacement, MediaSession
from app.core import metrics
from app.core.context import DialogueIdentity, DialogueTurn, PersonaContext
from app.core.degrade_speech import spoken_speech
from app.dialogue_runtime import DialogueRuntime, _alert_elder_id
from app.services.dialogue_service import DialogueReply

# --------------------------------------------------------------------- 替身


class _FakeService:
    """`DialogueService` 的替身：只记调用、按预设返回。"""

    def __init__(self, reply: DialogueReply | None = None) -> None:
        self.reply = reply or DialogueReply(text="今天多云转晴，最高二十度。", degraded=False)
        self.calls: list[dict[str, Any]] = []
        self.raise_on_respond: BaseException | None = None

    async def respond(self, **kwargs: Any) -> DialogueReply:
        self.calls.append(kwargs)
        if self.raise_on_respond is not None:
            raise self.raise_on_respond
        return self.reply


class _FakeTTS:
    """`TTSProvider` 的替身：返回固定 PCM 或抛错。"""

    def __init__(self, pcm: bytes = b"\x00" * 3200, error: BaseException | None = None) -> None:
        self.pcm = pcm
        self.error = error
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.pcm


class _FakeCascade:
    """`media_bridge._Cascade` 的最小替身（只实现 `DialogueRuntime` 读的字段）。"""

    def __init__(
        self,
        *,
        device_id: str = "dev-1",
        trace_id: str = "trace-1",
        resolved: bool = True,
        source: str | None = "voice",
        thread_id: str | None = "thread-abc",
        candidate_elder_id: str | None = "11111111-1111-4111-8111-111111111102",
        candidates: list[dict[str, str]] | None = None,
        greeting: str | None = None,
        voice_id: str | None = None,
        greeting_clock_start: float | None = None,
    ) -> None:
        self.device_id = device_id
        self.trace_id = trace_id
        self.resolved = resolved
        self.source = source
        self.thread_id = thread_id
        self.candidate_elder_id = candidate_elder_id
        self.candidates = candidates if candidates is not None else []
        self.greeting = greeting
        self.voice_id = voice_id
        # 开口截止的计时起点（登录进页面那一刻）。`None` = 没有级联起点，从「现在」算。
        self.greeting_clock_start = greeting_clock_start


@pytest.fixture(autouse=True)
def _isolate_metrics() -> Any:
    """每例前后清空进程内指标，避免计数断言互相污染。"""
    metrics.reset_metrics()
    yield
    metrics.reset_metrics()


@pytest.fixture(autouse=True)
def _clean_cascade_slot() -> Any:
    """每例前后清空级联表与终稿钩子（都是模块级全局状态）。"""
    media_bridge._cascades.clear()
    signaling.set_dialogue_final_handler(None)
    yield
    media_bridge._cascades.clear()
    signaling.set_dialogue_final_handler(None)


def _config(**overrides: Any) -> Any:
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


def _put_cascade(device_id: str, cascade: _FakeCascade) -> None:
    """把替身塞进级联表（`active_cascade` 只按 device_id 查）。"""
    media_bridge._cascades[device_id] = cascade  # type: ignore[assignment]


# ===========================================================================
# 一、DialogueRuntime：什么时候该回、什么时候必须装死
# ===========================================================================


class TestRuntimeSkips:
    """「不回复」的四种理由，每一种都必须安静 —— 这些路径上抛异常会打断 WS 回环。"""

    @pytest.mark.asyncio
    async def test_empty_text_skips_without_touching_service(self) -> None:
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "   ")

        assert service.calls == []

    @pytest.mark.asyncio
    async def test_missing_cascade_skips(self) -> None:
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert service.calls == []

    @pytest.mark.asyncio
    async def test_unresolved_cascade_skips(self) -> None:
        """身份还在比对/等确认时不许猜一个身份去编排（AC-28）。"""
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade(resolved=False, thread_id=None))

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert service.calls == []

    @pytest.mark.asyncio
    async def test_blank_thread_id_skips(self) -> None:
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade(thread_id="   "))

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert service.calls == []

    @pytest.mark.asyncio
    async def test_unknown_source_skips(self) -> None:
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade(source="robot"))

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert service.calls == []

    @pytest.mark.asyncio
    async def test_non_generic_without_elder_id_skips(self) -> None:
        """非 generic 却没 elder_id 是数据不一致：宁可不说，不许按 generic 猜。"""
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade(candidate_elder_id=None))

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert service.calls == []


class TestRuntimeIdentity:
    """身份怎么从级联搬到编排层 —— 传错了记忆就会串人。"""

    @pytest.mark.asyncio
    async def test_voice_identity_and_thread_passed_through(self) -> None:
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade(
            "dev-1",
            _FakeCascade(
                resolved=True,
                thread_id="thread-1",
                candidate_elder_id="elder-1",
                candidates=[{"elder_id": "elder-1", "display_name": "张桂芬"}],
            ),
        )

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert len(service.calls) == 1
        call = service.calls[0]
        assert call["thread_id"] == "thread-1"
        identity: DialogueIdentity = call["identity"]
        assert identity.source == "voice"
        assert identity.elder_id == "elder-1"
        assert identity.display_name == "张桂芬"

    @pytest.mark.asyncio
    async def test_generic_identity_drops_elder_id(self) -> None:
        """generic 必须抹掉 elder_id —— 这是「不写个人数据」的唯一执行点。"""
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade(
            "dev-1",
            _FakeCascade(source="generic", candidate_elder_id="elder-should-be-dropped"),
        )

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        identity: DialogueIdentity = service.calls[0]["identity"]
        assert identity.source == "generic"
        assert identity.elder_id is None
        assert identity.is_generic is True

    def test_generic_alert_uses_bound_elder_only(self) -> None:
        """通用轮次说「胸口疼」也要落红单：只借设备绑定长者做告警归属。

        FR-VPR-03 禁止的是读记忆/写健康/确认用药，没有禁止告警；若这里返回 None，
        `record_safety_alert` 会以 `unbound_elder` 跳过，最急的一条线就断了。
        """
        identity = DialogueIdentity(source="generic")
        _put_cascade("dev-1", _FakeCascade(source="generic", candidate_elder_id="elder-bound"))

        assert _alert_elder_id(identity, "dev-1") == "elder-bound"
        # 设备没有候选（真未绑定）时仍不编身份
        _put_cascade("dev-2", _FakeCascade(source="generic", candidate_elder_id=None))
        assert _alert_elder_id(identity, "dev-2") is None
        # 已确认身份不经过设备候选
        confirmed = DialogueIdentity(source="confirm", elder_id="elder-real")
        assert _alert_elder_id(confirmed, "dev-1") == "elder-real"

    @pytest.mark.asyncio
    async def test_persona_loader_receives_elder_id(self) -> None:
        seen: list[str] = []

        async def loader(elder_id: str) -> Any:
            seen.append(elder_id)
            from app.core.context import PersonaContext

            return PersonaContext(name="小满", personality="温和")

        service = _FakeService()
        runtime = DialogueRuntime(service=service, persona_loader=loader)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade(candidate_elder_id="elder-7"))

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert seen == ["elder-7"]
        assert service.calls[0]["persona"] is not None

    @pytest.mark.asyncio
    async def test_persona_loader_failure_degrades_to_none(self) -> None:
        """人设读不出来只是少了个称呼，不该让这一轮没回复。"""

        async def loader(elder_id: str) -> Any:
            raise RuntimeError("persona table missing")

        service = _FakeService()
        runtime = DialogueRuntime(service=service, persona_loader=loader)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert service.calls[0]["persona"] is None
        assert len(service.calls) == 1


class TestRuntimeDelivery:
    """字幕与音频的先后与失败隔离。"""

    @pytest.mark.asyncio
    async def test_text_delivered_before_audio(self) -> None:
        """顺序必须是「先字幕后音频」：音频卡住时长者不能对着空屏幕等。"""
        order: list[str] = []

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            order.append(f"deliver:{message['type']}")
            return True

        async def fake_enqueue(device_id: str, turn_id: str, pcm: bytes) -> ChunkPlacement:
            order.append("enqueue")
            return ChunkPlacement(frames=1, start_frame=0)

        async def fake_flush(device_id: str, turn_id: str) -> int:
            order.append("flush")
            return 0

        tts = _FakeTTS()
        service = _FakeService()
        runtime = DialogueRuntime(service=service, tts=tts)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", side_effect=fake_deliver),
            patch.object(
                webrtc_module, "enqueue_tts_chunk_placed_for_device", side_effect=fake_enqueue
            ),
            patch.object(webrtc_module, "flush_tts_for_device", side_effect=fake_flush),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        # 字幕必须第一；音频分片入队在后；flush 收尾最后。
        assert order[0] == "deliver:tts_text"
        assert order[-1] == "flush"
        assert "enqueue" in order
        # P0-3 回归：主链路首包打点必须是 `tts_first_packet_ms`，不许再借
        # `llm_first_token_ms` 的名字（那是真 LLM 的指标）。
        timings = metrics.metrics_snapshot()["timings"]
        assert "tts_first_packet_ms" in timings
        assert "llm_first_token_ms" not in timings

    @pytest.mark.asyncio
    async def test_streaming_tts_writes_chunks_as_they_arrive(self) -> None:
        """P2：第一句来一块写一块，不等整句合成完；字幕时长随块增长。"""
        enqueued: list[bytes] = []
        caption_snapshots: list[list[dict[str, Any]]] = []
        frames = 0

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            if message["type"] == "tts_segments":
                caption_snapshots.append([dict(entry) for entry in message["segments"]])
            return True

        async def fake_enqueue(device_id: str, turn_id: str, pcm: bytes) -> ChunkPlacement:
            nonlocal frames
            enqueued.append(pcm)
            placement = ChunkPlacement(frames=1, start_frame=frames)
            frames += 1
            return placement

        class _StreamingTTS:
            async def synthesize(self, text: str) -> bytes:
                return b"\x03" * 3200

            async def synthesize_stream(self, text: str) -> Any:
                yield b"\x01" * 3200
                await asyncio.sleep(0)
                yield b"\x02" * 3200

        service = _FakeService(
            DialogueReply(text="今天多云转晴。明天有小雨。后天会放晴。", degraded=False)
        )
        runtime = DialogueRuntime(service=service, tts=_StreamingTTS())  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", side_effect=fake_deliver),
            patch.object(
                webrtc_module, "enqueue_tts_chunk_placed_for_device", side_effect=fake_enqueue
            ),
            patch.object(webrtc_module, "flush_tts_for_device", return_value=0),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert enqueued[0] == b"\x01" * 3200, "第一块必须立刻入队，不能被整句攒住"
        assert enqueued[1] == b"\x02" * 3200
        per_sentence: dict[str, list[int]] = {}
        for snapshot in caption_snapshots:
            for entry in snapshot:
                per_sentence.setdefault(entry["text"], []).append(entry["duration_ms"])
        assert all(values == sorted(values) for values in per_sentence.values()), (
            "同一句字幕的时长只能增不能回退"
        )
        assert all(entry.get("char_ms") for entry in caption_snapshots[-1])
        assert len(caption_snapshots[-1]) == 3

    @pytest.mark.asyncio
    async def test_tts_first_packet_timeout_sends_error_frame(self, monkeypatch: Any) -> None:
        """TTS 1s 档：首句迟迟不出 → 发 `tts_unavailable` error 帧并停播（不静默）。"""
        delivered: list[dict[str, Any]] = []
        flushed: list[str] = []

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append(message)
            return True

        async def fake_flush(device_id: str, turn_id: str) -> int:
            flushed.append(turn_id)
            return 0

        class _SlowTTS:
            async def synthesize(self, text: str) -> bytes:
                await asyncio.sleep(5.0)
                return b"\x00" * 3200

        monkeypatch.setattr("app.core.timeouts.resolve_timeout_ms", lambda stage: 10)
        runtime = DialogueRuntime(service=_FakeService(), tts=_SlowTTS())  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", side_effect=fake_deliver),
            patch.object(
                webrtc_module,
                "enqueue_tts_chunk_placed_for_device",
                AsyncMock(return_value=ChunkPlacement(frames=1, start_frame=0)),
            ),
            patch.object(webrtc_module, "flush_tts_for_device", side_effect=fake_flush),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert [message["type"] for message in delivered] == ["tts_text", "error"]
        assert delivered[1]["code"] == "tts_unavailable"
        assert delivered[1]["message"] == spoken_speech("tts_unavailable")
        assert flushed == ["turn-1"]
        snapshot = metrics.metrics_snapshot()
        assert any(
            key.startswith("degrade_total{") and "stage=tts" in key and count == 1
            for key, count in snapshot["counters"].items()  # type: ignore[union-attr]
        )
        assert "tts_timeout_ms" in snapshot["timings"]  # type: ignore[operator]
        assert "tts_first_packet_ms" not in snapshot["timings"]  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_tts_failure_keeps_subtitle_and_flags_unavailable(self) -> None:
        """TTS 炸了：字幕照常，另发一条可见降级；不许补第二句 tts_text。"""
        delivered: list[dict[str, Any]] = []

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append(message)
            return True

        service = _FakeService()
        runtime = DialogueRuntime(service=service, tts=_FakeTTS(error=RuntimeError("tts down")))  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", side_effect=fake_deliver),
            patch.object(webrtc_module, "flush_tts_for_device", return_value=0),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert [message["type"] for message in delivered] == ["tts_text", "error"]
        assert delivered[0]["text"] == service.reply.text
        assert delivered[1]["code"] == "tts_unavailable"
        assert delivered[1]["message"] == spoken_speech("tts_unavailable")

    @pytest.mark.asyncio
    async def test_partial_tts_failure_does_not_add_notice(self) -> None:
        """前半句已经进轨：长者听到了声音，就不再补「听不清」的降级提示。"""
        delivered: list[dict[str, Any]] = []

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append(message)
            return True

        class _HalfwayTTS:
            async def synthesize(self, text: str) -> bytes:
                raise AssertionError("应走流式合成")

            async def synthesize_stream(self, text: str) -> Any:
                # 流式实现会缓冲一块再吐（要判 final），所以必须吐两块才算「已经进轨」。
                yield b"\x01" * 3200
                yield b"\x02" * 3200
                raise RuntimeError("tts dropped mid-sentence")

        runtime = DialogueRuntime(
            service=_FakeService(),
            tts=_HalfwayTTS(),  # type: ignore[arg-type]
        )
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", side_effect=fake_deliver),
            patch.object(
                webrtc_module,
                "enqueue_tts_chunk_placed_for_device",
                AsyncMock(return_value=ChunkPlacement(frames=1, start_frame=0)),
            ),
            patch.object(webrtc_module, "flush_tts_for_device", return_value=0),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert not [message for message in delivered if message["type"] == "error"]

    @pytest.mark.asyncio
    async def test_empty_pcm_skips_enqueue(self) -> None:
        calls: list[bytes] = []

        async def fake_enqueue(device_id: str, turn_id: str, pcm: bytes) -> ChunkPlacement:
            calls.append(pcm)
            return ChunkPlacement(frames=1, start_frame=0)

        service = _FakeService()
        runtime = DialogueRuntime(service=service, tts=_FakeTTS(pcm=b""))  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", return_value=True),
            patch.object(
                webrtc_module, "enqueue_tts_chunk_placed_for_device", side_effect=fake_enqueue
            ),
            patch.object(webrtc_module, "flush_tts_for_device", return_value=0),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert calls == []

    @pytest.mark.asyncio
    async def test_offline_device_skips_tts(self) -> None:
        """设备掉线时连 TTS 都不该合成（白花钱，也没人听）。"""
        tts = _FakeTTS()
        service = _FakeService()
        runtime = DialogueRuntime(service=service, tts=tts)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with patch.object(signaling, "deliver", return_value=False):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert tts.texts == []

    @pytest.mark.asyncio
    async def test_degraded_flag_propagates_to_frontend(self) -> None:
        """FR-DLG-05 的降级话术要靠 `degraded=True` 让前端打标记，丢了这字段前端就分不清。"""
        delivered: list[dict[str, Any]] = []

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append(message)
            return True

        service = _FakeService(reply=DialogueReply(text="我这会儿暂时答不上来。", degraded=True))
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with patch.object(signaling, "deliver", side_effect=fake_deliver):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert delivered[0]["degraded"] is True

    @pytest.mark.asyncio
    async def test_reply_source_metric_marks_llm(self) -> None:
        """`reply_source_total{provider}` 是区分「垫层回的」与「真 LLM 回的」的唯一口径。"""
        service = _FakeService()
        runtime = DialogueRuntime(service=service, tts=_FakeTTS())  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", return_value=True),
            patch.object(
                webrtc_module,
                "enqueue_tts_chunk_placed_for_device",
                return_value=ChunkPlacement(frames=1, start_frame=0),
            ),
            patch.object(webrtc_module, "flush_tts_for_device", new=AsyncMock(return_value=0)),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        counters = metrics.metrics_snapshot()["counters"]
        assert any("reply_source_total" in key and "llm" in key for key in counters), counters


class TestRuntimeLifecycle:
    def test_register_installs_handler(self) -> None:
        runtime = DialogueRuntime(service=_FakeService())  # type: ignore[arg-type]
        assert signaling.has_dialogue_final_handler() is False

        runtime.register()

        assert signaling.has_dialogue_final_handler() is True

    def test_register_is_idempotent(self) -> None:
        runtime = DialogueRuntime(service=_FakeService())  # type: ignore[arg-type]
        runtime.register()
        first = signaling._dialogue_handler
        runtime.register()
        assert signaling._dialogue_handler is first

    @pytest.mark.asyncio
    async def test_shutdown_unregisters(self) -> None:
        runtime = DialogueRuntime(service=_FakeService())  # type: ignore[arg-type]
        runtime.register()
        await runtime.shutdown()
        assert signaling.has_dialogue_final_handler() is False

    @pytest.mark.asyncio
    async def test_shutdown_without_register_is_noop(self) -> None:
        runtime = DialogueRuntime(service=_FakeService())  # type: ignore[arg-type]
        await runtime.shutdown()  # 不抛即可
        assert signaling.has_dialogue_final_handler() is False


# ===========================================================================
# 二、信号层：终稿钩子必须把 turn_id 带下去
# ===========================================================================


class TestDialogueFinalHandler:
    """`turn_id` 丢在这条链上的任何一环，回复音频都会被 `stale turn` 拒掉。"""

    @pytest.mark.asyncio
    async def test_handler_receives_device_turn_trace_text(self) -> None:
        seen: list[tuple[str, str, str, str]] = []

        async def handler(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
            seen.append((device_id, turn_id, trace_id, text))

        signaling.set_dialogue_final_handler(handler)
        with patch.object(signaling, "deliver", return_value=True):
            delivered = signaling.deliver_asr_event(
                "dev-1",
                _final_event("今天天气怎么样"),
                turn_id="turn-9",
                trace_id="trace-9",
            )
            assert delivered is True
            await _drain_dialogue_tasks()

        assert seen == [("dev-1", "turn-9", "trace-9", "今天天气怎么样")]

    @pytest.mark.asyncio
    async def test_handler_not_called_without_turn_id(self) -> None:
        """老调用点不传 `turn_id` 时不许瞎编一个 —— 没有 turn 就没法安全写轨。"""
        seen: list[str] = []

        async def handler(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
            seen.append(turn_id)

        signaling.set_dialogue_final_handler(handler)
        with patch.object(signaling, "deliver", return_value=True):
            signaling.deliver_asr_event("dev-1", _final_event("今天天气怎么样"))
            await _drain_dialogue_tasks()

        assert seen == []

    @pytest.mark.asyncio
    async def test_handler_not_called_for_partial(self) -> None:
        seen: list[str] = []

        async def handler(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
            seen.append(text)

        signaling.set_dialogue_final_handler(handler)
        with patch.object(signaling, "deliver", return_value=True):
            signaling.deliver_asr_event(
                "dev-1", _partial_event("今天天"), turn_id="turn-9", trace_id="trace-9"
            )
            await _drain_dialogue_tasks()

        assert seen == []

    @pytest.mark.asyncio
    async def test_handler_not_called_when_delivery_failed(self) -> None:
        """设备已离线：连字幕都没送出去，再花 LLM 的钱去编排就是纯浪费。"""
        seen: list[str] = []

        async def handler(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
            seen.append(text)

        signaling.set_dialogue_final_handler(handler)
        with patch.object(signaling, "deliver", return_value=False):
            signaling.deliver_asr_event(
                "dev-1", _final_event("今天天气怎么样"), turn_id="turn-9", trace_id="trace-9"
            )
            await _drain_dialogue_tasks()

        assert seen == []

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_escape(self) -> None:
        """编排抛错只能记日志：抛回 `deliver_asr_event` 会把 WS 回环带塌。"""

        async def handler(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
            raise RuntimeError("llm exploded")

        signaling.set_dialogue_final_handler(handler)
        with patch.object(signaling, "deliver", return_value=True):
            delivered = signaling.deliver_asr_event(
                "dev-1", _final_event("今天天气怎么样"), turn_id="turn-9", trace_id="trace-9"
            )
            assert delivered is True
            await _drain_dialogue_tasks()  # 不抛即可

    @pytest.mark.asyncio
    async def test_no_handler_registered_keeps_old_behaviour(self) -> None:
        """没注册钩子时 `deliver_asr_event` 行为不变（旧测试与垫层都靠这条）。"""
        with patch.object(signaling, "deliver", return_value=True):
            assert (
                signaling.deliver_asr_event(
                    "dev-1", _final_event("你好"), turn_id="turn-9", trace_id="trace-9"
                )
                is True
            )
            await _drain_dialogue_tasks()

    def test_has_handler_reflects_registration(self) -> None:
        assert signaling.has_dialogue_final_handler() is False

        async def handler(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
            return None

        signaling.set_dialogue_final_handler(handler)
        assert signaling.has_dialogue_final_handler() is True
        signaling.set_dialogue_final_handler(None)
        assert signaling.has_dialogue_final_handler() is False


# ===========================================================================
# 三、系统播放入口：能出声，但不能绕开打断
# ===========================================================================


class TestSystemTtsEnqueue:
    @pytest.mark.asyncio
    async def test_enqueues_without_turn_id(self) -> None:
        """这就是开场白能出声的原因：不要求先有 `begin_turn()`。"""
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            assert session.playback_generation == 0
            ok = await session.enqueue_system_tts_pcm(b"\x00" * 3200)
            assert ok is True
            assert session.playback_queue_depth > 0
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_stale_generation_rejected(self) -> None:
        """取代际号之后长者开口打断过 → 这段开场白必须丢弃。"""
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            generation = session.playback_generation  # 0
            session.barge_in()  # → 1
            ok = await session.enqueue_system_tts_pcm(b"\x00" * 3200, generation=generation)
            assert ok is False
            assert session.playback_queue_depth == 0
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_current_generation_accepted(self) -> None:
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            session.barge_in()
            ok = await session.enqueue_system_tts_pcm(
                b"\x00" * 3200, generation=session.playback_generation
            )
            assert ok is True
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_enqueues_before_track_exists(self) -> None:
        """媒体还没建连（开场白的常态）：**照样入队**，等建连那一刻播。

        2026-11-10 修正。队列在 `__init__` 就建好、与会话同寿命，
        `create_peer_connection()` 用同一个队列造 `PlaybackAudioTrack`，
        所以先入队 = 连通即开口。从前这里返回 `False`，调用方只能把开场白丢掉，
        实测 9 次登录丢 4 次。
        """
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        try:
            assert await session.enqueue_system_tts_pcm(b"\x00" * 3200) is True
            assert session.playback_queue_depth > 0
            # 建连之后仍是同一队列，音频不会凭空消失
            session.create_peer_connection()
            assert session.playback_queue_depth > 0
        finally:
            await session.close()
        assert session.playback_queue_depth == 0, "关闭会话必须把未播完的开场白清掉"

    @pytest.mark.asyncio
    async def test_closed_session_returns_false(self) -> None:
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        await session.close()
        assert await session.enqueue_system_tts_pcm(b"\x00" * 3200) is False

    @pytest.mark.asyncio
    async def test_invalid_pcm_returns_false(self) -> None:
        """WAV 头混进来（TTS 实现写错时的高频事故）只能返回 False，不许把队列搞脏。"""
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            assert await session.enqueue_system_tts_pcm(b"RIFF....WAVEfmt ") is False
            assert session.playback_queue_depth == 0
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_backpressure_returns_false(self) -> None:
        session = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        session.create_peer_connection()
        try:
            # 15 秒上限：塞 20 秒
            ok = await session.enqueue_system_tts_pcm(b"\x00" * (16_000 * 2 * 20))
            assert ok is False
        finally:
            await session.close()


class TestSystemTtsDeviceHelpers:
    @pytest.mark.asyncio
    async def test_unknown_device_returns_false(self) -> None:
        assert await webrtc_module.play_system_tts_for_device("nope", b"\x00" * 3200) is False
        assert await webrtc_module.enqueue_tts_for_device("nope", "turn-1", b"\x00" * 3200) is False

    @pytest.mark.asyncio
    async def test_generation_for_unknown_device_is_none(self) -> None:
        """没有会话时报 `None`：调用方据此跳过合成，而不是编一个 0 出来。"""
        assert webrtc_module.playback_generation_for_device("nope") is None

    @pytest.mark.asyncio
    async def test_generation_for_live_session(self) -> None:
        """会话建好后代际可读；打断后读数跟着变（开场白据此判自己是否过期）。"""
        registry = webrtc_module._signaling_registry()
        session = registry.create_session("dev-gen", "trace-gen")
        session.create_peer_connection()
        try:
            assert webrtc_module.playback_generation_for_device("dev-gen") == 0
            session.barge_in()
            assert webrtc_module.playback_generation_for_device("dev-gen") == 1
        finally:
            await registry.close_session("dev-gen")

    @pytest.mark.asyncio
    async def test_enqueue_for_device_rejects_stale_turn(self) -> None:
        """对话回复仍走 turn 校验：没有 `begin_turn()` 就没有可写的 turn。"""
        registry = webrtc_module._signaling_registry()
        session = registry.create_session("dev-reply", "trace-reply")
        session.create_peer_connection()
        try:
            ok = await webrtc_module.enqueue_tts_for_device("dev-reply", "turn-x", b"\x00" * 3200)
            assert ok is False
        finally:
            await registry.close_session("dev-reply")


# ===========================================================================
# 四、开场白的音频派发（`media_bridge._spawn_greeting_audio`）
# ===========================================================================


class TestGreetingAudio:
    @pytest.mark.asyncio
    async def test_text_still_sent_even_without_loop_audio(self) -> None:
        """没有音频也必须先把文本发出去 —— 字幕是下限保障。"""
        delivered: list[dict[str, Any]] = []
        cascade = _FakeCascade()

        with patch.object(
            media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
        ):
            media_bridge._emit_greeting(cascade, generic=False)  # type: ignore[arg-type]

        assert delivered[0]["type"] == "tts_text"
        assert delivered[0]["degraded"] is False

    @pytest.mark.asyncio
    async def test_greeting_held_until_avatar_ready(self) -> None:
        """数字人没立起来之前一个字都不发：文本与音频等的是同一个就绪信号。

        产品负责人 2026-11-10 要求「不要数字人还没加载出来就说话」。音频好挡（写入播放队列前
        等），文本更容易抢跑（`tts_text` 是同步 `_deliver`），所以两者压在同一道闸上。
        """
        delivered: list[dict[str, Any]] = []
        played: list[bytes] = []

        async def fake_system(device_id: str, pcm: bytes, *, generation: int | None = None) -> bool:
            played.append(pcm)
            return True

        media_bridge.reset_avatar_ready("dev-1")
        # 登录已超 2 秒的级联：就绪即开口，remain=0，本例只验「闸门」，不验 2 秒补等
        # （补等由 `tests/test_greeting_tts_fallback.py` 的 deadline 用例负责）。
        cascade = _FakeCascade(greeting_clock_start=asyncio.get_running_loop().time() - 3.0)
        with (
            patch.object(
                media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
            ),
            patch("app.audio.tts.build_tts", return_value=_FakeTTS(pcm=b"\x00" * 3200)),
            patch.object(webrtc_module, "play_system_tts_for_device", side_effect=fake_system),
            patch.object(webrtc_module, "has_authed_client", return_value=True),
            patch.object(webrtc_module, "playback_generation_for_device", return_value=1),
            patch.object(webrtc_module, "playback_track_ready_for_device", return_value=True),
        ):
            media_bridge._emit_greeting(cascade, generic=False)  # type: ignore[arg-type]
            await asyncio.sleep(0.05)
            assert delivered == [], "数字人还没就绪就发开场白 = 声音先于人出现"
            media_bridge.notify_avatar_ready("dev-1")
            await _drain_greeting_tasks()

        assert delivered[0]["type"] == "tts_text"
        assert any(m["type"] == "tts_segments" for m in delivered), "出声后补字幕计划"
        assert played, "就绪之后音频照样要发"
        media_bridge.reset_avatar_ready("dev-1")

    @pytest.mark.asyncio
    async def test_greeting_speaks_even_without_clock_start_field(self) -> None:
        """级联对象没有 `greeting_clock_start` 时也必须开口。

        回归用例（2026-11-11 实测踩到）：该属性的读取跑在 `create_task` 里，
        `AttributeError` 抛出去没人接，表现为**开场白一个字都不发**，日志只留一行
        `Task exception was never retrieved`。退化口径是「从现在就绪开始算」，
        宁可晚开口，也不能没有开场白。
        """
        delivered: list[dict[str, Any]] = []

        class _BareCascade:
            """只有 `_emit_greeting` 必需的那几个字段，**故意不含** `greeting_clock_start`。"""

            device_id = "dev-1"
            trace_id = "trace-1"
            source = "voice"
            greeting: str | None = None

        media_bridge.notify_avatar_ready("dev-1")
        try:
            with (
                patch.object(
                    media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
                ),
                patch("app.audio.tts.build_tts", return_value=_FakeTTS(pcm=b"\x00" * 3200)),
                patch.object(webrtc_module, "has_authed_client", return_value=True),
                patch.object(webrtc_module, "playback_generation_for_device", return_value=1),
                patch.object(webrtc_module, "playback_track_ready_for_device", return_value=True),
            ):
                media_bridge._emit_greeting(_BareCascade(), generic=False)  # type: ignore[arg-type]
                await _drain_greeting_tasks()
        finally:
            media_bridge.reset_avatar_ready("dev-1")

        assert delivered, "缺字段不该导致开场白静默消失"
        assert delivered[0]["type"] == "tts_text"

    @pytest.mark.asyncio
    async def test_audio_synthesized_and_enqueued(self) -> None:
        """有 loop 时：合成 → 等轨就绪 → 带着代际号入队。"""
        played: list[tuple[bytes, int | None]] = []
        tts = _FakeTTS(pcm=b"\x00" * 3200)

        async def fake_system(device_id: str, pcm: bytes, *, generation: int | None = None) -> bool:
            played.append((pcm, generation))
            return True

        with (
            patch.object(media_bridge, "_deliver", return_value=True),
            patch("app.audio.tts.build_tts", return_value=tts),
            patch.object(webrtc_module, "play_system_tts_for_device", side_effect=fake_system),
            patch.object(webrtc_module, "playback_generation_for_device", return_value=3),
            patch.object(webrtc_module, "playback_track_ready_for_device", return_value=True),
        ):
            media_bridge._emit_greeting(_FakeCascade(), generic=False)  # type: ignore[arg-type]
            await _drain_greeting_tasks()

        assert played and played[0][1] == 3
        assert played[0][0] == b"\x00" * 3200

    @pytest.mark.asyncio
    async def test_generation_snapshot_taken_before_synthesis(self) -> None:
        """代际号必须取「合成之前」那一刻的快照。

        取现值的后果：合成 + 等轨那一两秒里长者开口打断过（`barge_in` 把代际 +1），
        现取就会永远等于当前值，于是「作废这段开场白」的保护被整个抹掉 ——
        长者正在说自己的话，耳边却响起「您好呀，我是小满」。
        """
        seen: list[int] = []
        played: list[tuple[bytes, int | None]] = []
        state = {"generation": 3}

        def fake_generation(device_id: str) -> int:
            seen.append(state["generation"])
            return state["generation"]

        async def fake_system(device_id: str, pcm: bytes, *, generation: int | None = None) -> bool:
            played.append((pcm, generation))
            return True

        async def synth_then_barge(text: str) -> bytes:
            # 模拟「合成期间被打断」：代际在合成过程中前进了一格
            state["generation"] += 1
            return b"\x00" * 3200

        tts = _FakeTTS(pcm=b"\x00" * 3200)
        tts.synthesize = synth_then_barge  # type: ignore[method-assign]

        with (
            patch.object(media_bridge, "_deliver", return_value=True),
            patch("app.audio.tts.build_tts", return_value=tts),
            patch.object(webrtc_module, "play_system_tts_for_device", side_effect=fake_system),
            # 数字人已就绪：本用例验的是音频管线，不是开场白闸门
            patch.object(media_bridge, "wait_avatar_ready", new=AsyncMock(return_value=True)),
            patch.object(
                webrtc_module, "playback_generation_for_device", side_effect=fake_generation
            ),
            patch.object(webrtc_module, "playback_track_ready_for_device", return_value=True),
        ):
            media_bridge._emit_greeting(_FakeCascade(), generic=False)  # type: ignore[arg-type]
            await _drain_greeting_tasks()

        # 只取代际号一次，且取在合成之前（=3，不是合成后的 4）
        assert seen == [3]
        assert played[0][1] == 3

    @pytest.mark.asyncio
    async def test_waits_until_track_ready(self) -> None:
        """轨迟到：先合成、再等，轨一出现就写 —— 不能「试一次就放弃」。

        开场白与媒体建连是竞态：`tts_text` 通常在 `offer` 之前就发下去了，
        轨要等 `getUserMedia`（数百 ms ~ 1s）才有。试一次就放弃会让
        「打开页面听到开场白」变成碰运气。
        """
        played: list[tuple[bytes, int | None]] = []
        probes = {"n": 0}
        poll = media_bridge.GREETING_TRACK_POLL_SECONDS

        async def fake_system(device_id: str, pcm: bytes, *, generation: int | None = None) -> bool:
            played.append((pcm, generation))
            return True

        def flaky_ready(device_id: str) -> bool:
            probes["n"] += 1
            return probes["n"] >= 3  # 前两次探不到，第三次才就绪

        with (
            patch.object(media_bridge, "_deliver", return_value=True),
            patch("app.audio.tts.build_tts", return_value=_FakeTTS(pcm=b"\x00" * 3200)),
            patch.object(webrtc_module, "play_system_tts_for_device", side_effect=fake_system),
            patch.object(webrtc_module, "playback_generation_for_device", return_value=1),
            # 数字人已就绪：本用例验的是「等轨」，不是开场白闸门
            patch.object(media_bridge, "wait_avatar_ready", new=AsyncMock(return_value=True)),
            patch.object(webrtc_module, "playback_track_ready_for_device", side_effect=flaky_ready),
            # 把步长压到 1ms：验的是「会等」而不是真的等 100ms
            patch.object(media_bridge, "GREETING_TRACK_POLL_SECONDS", 0.001),
        ):
            media_bridge._emit_greeting(_FakeCascade(), generic=False)  # type: ignore[arg-type]
            await _drain_greeting_tasks()

        assert played, f"轨就绪后应当写轨（探针调用 {probes['n']} 次，步长 {poll}）"

    @pytest.mark.asyncio
    async def test_enqueues_even_when_track_never_arrives(self) -> None:
        """轨始终不来：等满预算后**照样入队**（2026-11-10 修正，见下一条用例）。

        队列与会话同寿命，`create_peer_connection()` 用同一个队列造播放轨，
        所以「等不到轨」只是「晚一点播」，不是「播不了」。从前这里 wait 满就丢，
        实测 9 次登录丢 4 次 —— 那才叫碰运气。
        """
        played: list[tuple[bytes, int | None]] = []

        async def fake_system(device_id: str, pcm: bytes, *, generation: int | None = None) -> bool:
            played.append((pcm, generation))
            return True

        with (
            patch.object(media_bridge, "_deliver", return_value=True),
            patch("app.audio.tts.build_tts", return_value=_FakeTTS(pcm=b"\x00" * 3200)),
            patch.object(webrtc_module, "play_system_tts_for_device", side_effect=fake_system),
            patch.object(webrtc_module, "playback_generation_for_device", return_value=1),
            # 数字人已就绪：本用例验的是「等满预算也入队」，不是开场白闸门
            patch.object(media_bridge, "wait_avatar_ready", new=AsyncMock(return_value=True)),
            patch.object(webrtc_module, "playback_track_ready_for_device", return_value=False),
            # 预算也压到 2ms，配合 1ms 步长：两次就超预算
            patch.object(media_bridge, "GREETING_TRACK_WAIT_SECONDS", 0.002),
        ):
            media_bridge._emit_greeting(_FakeCascade(), generic=False)  # type: ignore[arg-type]
            await _drain_greeting_tasks()

        assert played, "等满预算也必须入队：轨迟到 ≠ 播不了"
        assert played[0][1] == 1, "入队要带上合成前取的代际号（打断仍然算数）"

    @pytest.mark.asyncio
    async def test_dropped_text_skips_audio(self) -> None:
        """设备离线时连 TTS 都不该合成。"""
        tts = _FakeTTS()

        with (
            patch.object(media_bridge, "_deliver", return_value=False),
            patch("app.audio.tts.build_tts", return_value=tts),
        ):
            media_bridge._emit_greeting(_FakeCascade(), generic=False)  # type: ignore[arg-type]
            await _drain_greeting_tasks()

        assert tts.texts == []

    @pytest.mark.asyncio
    async def test_tts_failure_is_swallowed(self) -> None:
        """TTS 挂了只记日志：身份级联已经成功，不许反过来把它带塌。"""
        tts = _FakeTTS(error=RuntimeError("tts down"))

        with (
            patch.object(media_bridge, "_deliver", return_value=True),
            patch("app.audio.tts.build_tts", return_value=tts),
        ):
            media_bridge._emit_greeting(_FakeCascade(), generic=False)  # type: ignore[arg-type]
            await _drain_greeting_tasks()

        assert tts.texts  # 试过了

    @pytest.mark.asyncio
    async def test_persona_greeting_used_when_present(self) -> None:
        """W5 · G-D9：人设开场白存在时用它，不再用模块常量。"""
        delivered: list[dict[str, Any]] = []

        with patch.object(
            media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
        ):
            media_bridge._emit_greeting(
                _FakeCascade(greeting="您好呀，我是小满，今天感觉怎么样？"),  # type: ignore[arg-type]
                generic=False,
            )

        assert delivered[0]["text"] == "您好呀，我是小满，今天感觉怎么样？"

    @pytest.mark.asyncio
    async def test_falls_back_to_constant_when_lookup_missing(self) -> None:
        """人设没配/读失败：退回模块常量，而不是发一句空字幕。"""
        delivered: list[dict[str, Any]] = []

        with patch.object(
            media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
        ):
            media_bridge._emit_greeting(_FakeCascade(greeting=None), generic=False)  # type: ignore[arg-type]

        assert delivered[0]["text"] == media_bridge.DEFAULT_GREETING_TEXT

    @pytest.mark.asyncio
    async def test_generic_ignores_persona_greeting(self) -> None:
        """generic 会话不许露出人设文案：那是「个性化」，而未确认身份就不该有个性化。"""
        delivered: list[dict[str, Any]] = []

        with patch.object(
            media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
        ):
            # 即便级联上意外留了一句人设开场白，generic 也必须走通用话术
            media_bridge._emit_greeting(_FakeCascade(greeting=None), generic=True)  # type: ignore[arg-type]

        assert delivered[0]["text"] == media_bridge.GENERIC_GREETING_TEXT


class TestPersonaVoiceTTS:
    """人设音色必须真的参与选 provider，而不只是存一列。"""

    @pytest.mark.asyncio
    async def test_reply_uses_persona_voice_factory(self) -> None:
        calls: list[str | None] = []
        providers: dict[str | None, _FakeTTS] = {}

        def factory(voice: str | None) -> _FakeTTS:
            calls.append(voice)
            provider = _FakeTTS()
            providers[voice] = provider
            return provider

        async def loader(elder_id: str) -> PersonaContext:
            return PersonaContext(
                name="小满",
                personality="温和",
                voice_id="zh-CN-XiaoyiNeural",
            )

        async def fake_enqueue(device_id: str, turn_id: str, pcm: bytes) -> ChunkPlacement:
            return ChunkPlacement(frames=1, start_frame=0)

        async def fake_flush(device_id: str, turn_id: str) -> int:
            return 0

        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            persona_loader=loader,
            tts_factory=factory,
        )
        _put_cascade("dev-1", _FakeCascade())

        with (
            patch.object(signaling, "deliver", return_value=True),
            patch.object(
                webrtc_module, "enqueue_tts_chunk_placed_for_device", side_effect=fake_enqueue
            ),
            patch.object(webrtc_module, "flush_tts_for_device", side_effect=fake_flush),
        ):
            await runtime.on_asr_final("dev-1", "turn-1", "trace-1", "今天天气怎么样")

        assert calls == ["zh-CN-XiaoyiNeural"]
        assert providers["zh-CN-XiaoyiNeural"].texts, "回复必须交给这一音色的 provider"


class TestResolveGreeting:
    @pytest.mark.asyncio
    async def test_stores_lookup_result(self) -> None:
        cascade = _FakeCascade(greeting=None)

        async def fake_lookup(session: Any, *, elder_id: Any) -> str | None:
            return "您好呀，我是小满。"

        async def fake_voice(session: Any, *, elder_id: Any) -> str | None:
            return "zh-CN-XiaoyiNeural"

        with (
            patch.object(
                media_bridge.persona_service, "get_persona_greeting", side_effect=fake_lookup
            ),
            patch.object(
                media_bridge.persona_service, "get_persona_voice_id", side_effect=fake_voice
            ),
        ):
            await media_bridge._resolve_greeting(cascade, uuid.UUID(int=7), None)  # type: ignore[arg-type]

        assert cascade.greeting == "您好呀，我是小满。"
        assert cascade.voice_id == "zh-CN-XiaoyiNeural"

    @pytest.mark.asyncio
    async def test_none_elder_id_skips_lookup(self) -> None:
        """generic 路径没有 elder_id：连查都不查，省一次往返。"""
        cascade = _FakeCascade(greeting=None)
        called: list[Any] = []

        async def fake_lookup(session: Any, *, elder_id: Any) -> str | None:
            called.append(elder_id)
            return "不该被用到"

        with patch.object(
            media_bridge.persona_service, "get_persona_greeting", side_effect=fake_lookup
        ):
            await media_bridge._resolve_greeting(cascade, None, None)  # type: ignore[arg-type]

        assert called == []
        assert cascade.greeting is None

    @pytest.mark.asyncio
    async def test_lookup_error_falls_back_to_none(self) -> None:
        cascade = _FakeCascade(greeting=None)

        async def boom(session: Any, *, elder_id: Any) -> str | None:
            raise RuntimeError("db down")

        with patch.object(media_bridge.persona_service, "get_persona_greeting", side_effect=boom):
            await media_bridge._resolve_greeting(cascade, uuid.UUID(int=7), None)  # type: ignore[arg-type]

        assert cascade.greeting is None


# ===========================================================================
# 辅助
# ===========================================================================


def _final_event(text: str) -> Any:
    from app.audio.asr import ASREvent, ASREventKind, ASRResult

    return ASREvent(
        kind=ASREventKind.FINAL,
        result=ASRResult(text=text, is_final=True, confidence=0.9),
        error=None,
    )


def _partial_event(text: str) -> Any:
    from app.audio.asr import ASREvent, ASREventKind, ASRResult

    return ASREvent(
        kind=ASREventKind.PARTIAL,
        result=ASRResult(text=text, is_final=False, confidence=0.5),
        error=None,
    )


async def _drain_dialogue_tasks() -> None:
    """等 scheduling 的 task 跑完（`deliver_asr_event` 刻意不 await）。"""
    for _ in range(5):
        pending = list(signaling._dialogue_tasks)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def _drain_greeting_tasks() -> None:
    for _ in range(5):
        pending = list(media_bridge._greeting_tasks)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "TestDialogueFinalHandler",
    "TestGreetingAudio",
    "TestRuntimeDelivery",
    "TestRuntimeIdentity",
    "TestRuntimeLifecycle",
    "TestRuntimeSkips",
    "TestSystemTtsDeviceHelpers",
    "TestSystemTtsEnqueue",
]

# `DialogueTurn` 在这个文件里没直接用到，但保留导入是为了让 `pytest --collect-only`
# 能顺带验证 `app.core.context` 的公开面没有在重构中改名。
_ = DialogueTurn
