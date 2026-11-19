"""N41：分段送达事件的语义测试。

要证的是「送达」这件事真的被记下来了，而不是「代码里有个函数被调用过」：

1. **一段写到播放轨 = 一条事件**，且 `response_id` 与同轮的 `assistant_said` 对得上
   —— 否则回放时只能在「说过这句话」和「送到了几句」之间二选一。
2. **没送到的段不许混进来**：`enqueue` 返回 0 的段是 dropped，段号连续重排。
3. **批量写**：一轮只调一次 `record_many`。TTS 循环里逐条写库会把
   「上一句还在播、下一句已合成」的流水线打断，而那条流水线正是首包 ≤1.5s 的基础。

刻意不测：`conversation_events` 的取号与唯一约束（`tests/test_conversation_event_service.py`）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.audio import media_bridge
from app.audio.tts_sentences import TtsChunk, split_sentences
from app.audio.webrtc import ChunkPlacement
from app.dialogue_runtime import DialogueRuntime
from app.services.dialogue_service import DialogueReply

ELDER_ID = "11111111-1111-4111-8111-111111111102"
THREAD_ID = "8171f6f6-6ebe-485d-967d-f7f5e5191d4c"


class _FakeService:
    def __init__(self, text: str = "杭州今天晴。紫外线有点强。记得戴帽子。") -> None:
        self.reply = DialogueReply(text=text, degraded=False)
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> DialogueReply:
        self.calls.append(kwargs)
        return self.reply


class _FakeCascade:
    def __init__(self, *, candidate_elder_id: str | None = ELDER_ID) -> None:
        self.device_id = "dev-1"
        self.trace_id = "trace-1"
        self.resolved = True
        self.source = "voice"
        self.thread_id = THREAD_ID
        self.candidate_elder_id = candidate_elder_id


class _RecordingRecorder:
    """同时支持单条 `record` 与批量 `record_many` 的替身。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.batches: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> bool:
        self.records.append(kwargs)
        return True

    async def record_many(self, **kwargs: Any) -> bool:
        self.batches.append(kwargs)
        return True


def _put_cascade(cascade: _FakeCascade | None = None) -> None:
    media_bridge._cascades["dev-1"] = cascade or _FakeCascade()  # type: ignore[assignment]


def _patch_tts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frames: int = 1,
    gap_frames: int = 0,
    flush_frames: int = 0,
) -> list[bytes]:
    """把逐句合成与写轨换成替身，返回被写入的 PCM 序列。

    落位游标连续推进（`start_frame` 累加已写帧数）：字幕的 `start_ms` 由它换算，
    恒 0 的替身会让「偏移累计」这条断言变成空过。`gap_frames` 模拟句间静音 ——
    逐句串行合成时它真实存在，正是字幕漂移的来源。
    """
    written: list[bytes] = []
    cursor = 0

    async def _fake_stream(text: str, *, provider: Any = None, **_: Any):
        for index, sentence in enumerate(split_sentences(text)):
            yield TtsChunk(index, sentence, b"\x01" * 32, True)

    async def _fake_enqueue(device_id: str, turn_id: str, pcm: bytes) -> ChunkPlacement:
        nonlocal cursor
        written.append(pcm)
        placement = ChunkPlacement(frames=frames, start_frame=cursor)
        cursor += frames + gap_frames
        return placement

    async def _fake_flush(device_id: str, turn_id: str) -> int:
        return flush_frames

    monkeypatch.setattr("app.audio.tts_sentences.synthesize_sentences_stream", _fake_stream)
    monkeypatch.setattr("app.audio.webrtc.enqueue_tts_chunk_placed_for_device", _fake_enqueue)
    monkeypatch.setattr("app.audio.webrtc.flush_tts_for_device", _fake_flush)
    monkeypatch.setattr("app.dialogue_runtime.signaling.deliver", lambda *a, **k: True)
    return written


# ===========================================================================
# 一、送达即事件
# ===========================================================================


class TestSegmentDelivery:
    @pytest.mark.asyncio
    async def test_each_delivered_segment_is_one_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRecorder()
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            tts=object(),
            recorder=recorder,  # type: ignore[arg-type]
        )
        _put_cascade()
        _patch_tts(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "trace-9", "今天天气怎么样")

        assert len(recorder.batches) == 1, "一轮应该只批量写一次"
        events = recorder.batches[0]["events"]
        assert [e.event_type for e in events] == ["response_segment_delivered"] * 3
        assert [e.payload["segment_index"] for e in events] == [0, 1, 2]
        for event in events:
            assert isinstance(event.payload["delivered_ms"], float)
            assert event.payload["delivered_ms"] >= 0.0

    @pytest.mark.asyncio
    async def test_response_id_links_said_and_delivered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`assistant_said.response_id` 必须等于分段事件的 `response_id`。"""
        recorder = _RecordingRecorder()
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            tts=object(),
            recorder=recorder,  # type: ignore[arg-type]
        )
        _put_cascade()
        _patch_tts(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        said = [r for r in recorder.records if r["event_type"] == "assistant_said"]
        assert len(said) == 1
        response_id = said[0]["payload"]["response_id"]
        assert response_id, "assistant_said 必须带 response_id"
        delivered_ids = {e.payload["response_id"] for e in recorder.batches[0]["events"]}
        assert delivered_ids == {response_id}

    @pytest.mark.asyncio
    async def test_two_turns_get_different_response_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一轮重试/重连会产出新回复，不能共用 id —— 否则回放会把两轮缝成一轮。"""
        recorder = _RecordingRecorder()
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            tts=object(),
            recorder=recorder,  # type: ignore[arg-type]
        )
        _put_cascade()
        _patch_tts(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")
        await runtime.on_asr_final("dev-1", "turn-2", "t", "今天天气怎么样")

        ids = [
            r["payload"]["response_id"]
            for r in recorder.records
            if r["event_type"] == "assistant_said"
        ]
        assert len(ids) == 2
        assert ids[0] != ids[1]


# ===========================================================================
# 二、没送到的段不许混进来
# ===========================================================================


class TestDroppedSegments:
    @pytest.mark.asyncio
    async def test_dropped_chunks_produce_no_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`enqueue` 返回 0 = 没写进播放轨（被打断/设备掉线），不该算送到。"""
        recorder = _RecordingRecorder()
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            tts=object(),
            recorder=recorder,  # type: ignore[arg-type]
        )
        _put_cascade()
        _patch_tts(monkeypatch, frames=0)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert recorder.batches == [], "一段都没送到时不该产生送达事件"

    @pytest.mark.asyncio
    async def test_no_recorder_keeps_baseline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不注入记录器 = 接线前行为：不写事件、也不因为「要写事件」而报错。"""
        service = _FakeService()
        runtime = DialogueRuntime(service=service, tts=object())  # type: ignore[arg-type]
        _put_cascade()
        written = _patch_tts(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert service.calls
        assert len(written) == 3


# ===========================================================================
# 三、逐句字幕计划（方案 B）
# ===========================================================================


class TestCaptionPlan:
    @pytest.mark.asyncio
    async def test_plan_follows_sentence_boundaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """计划必须与**切句结果**一一对应，`start_ms` 按入队帧数累加（不是合成耗时）。

        产品负责人实测「字幕领先语音」：音频逐句排队播，字幕却整段一次性上屏。
        要证的是这份计划能当时间轴用 —— 句数、句文本、累计偏移都对得上。
        """
        text = "今天天气晴。紫外线有点强。记得戴帽子。"
        runtime = DialogueRuntime(
            service=_FakeService(text=text),  # type: ignore[arg-type]
            tts=object(),
        )
        _put_cascade()
        _patch_tts(monkeypatch, frames=5)  # 每句 5 帧 = 100ms

        sent: list[dict[str, Any]] = []

        def _deliver(device_id: str, message: dict[str, Any]) -> bool:
            sent.append(message)
            return True

        monkeypatch.setattr("app.dialogue_runtime.signaling.deliver", _deliver)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        plans = [m for m in sent if m["type"] == "tts_segments"]
        assert plans, "必须下发逐句字幕计划，否则前端只能整段上屏"
        last = plans[-1]["segments"]
        assert [s["text"] for s in last] == ["今天天气晴。", "紫外线有点强。", "记得戴帽子。"]
        assert [s["start_ms"] for s in last] == [0, 100, 200], "偏移必须累计，不能每句都是 0"
        assert [s["duration_ms"] for s in last] == [100, 100, 100]

    @pytest.mark.asyncio
    async def test_no_plan_when_nothing_enqueued(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """一段都没写进播放轨（被打断/掉线）：不下发计划，免得字幕讲一段没声音的话。"""
        runtime = DialogueRuntime(service=_FakeService(), tts=object())  # type: ignore[arg-type]
        _put_cascade()
        _patch_tts(monkeypatch, frames=0)

        sent: list[dict[str, Any]] = []

        def _deliver(device_id: str, message: dict[str, Any]) -> bool:
            sent.append(message)
            return True

        monkeypatch.setattr("app.dialogue_runtime.signaling.deliver", _deliver)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert [m for m in sent if m["type"] == "tts_segments"] == []

    @pytest.mark.asyncio
    async def test_plan_offsets_include_silence_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """句间静音必须体现在 `start_ms` 里 —— 这是「越说越偏」的结构性根因。

        逐句 TTS 串行合成：下一句合成比上一句播放慢时，播放轨补静音把 PTS 往前推。
        按「入队帧数累加」算时间轴看不见这段静音，每句偏一点、句数越多偏得越远。
        """
        text = "今天天气晴。紫外线有点强。记得戴帽子。"
        runtime = DialogueRuntime(service=_FakeService(text=text), tts=object())  # type: ignore[arg-type]
        _put_cascade()
        _patch_tts(monkeypatch, frames=5, gap_frames=3)  # 每句 100ms 音频 + 60ms 静音

        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "app.dialogue_runtime.signaling.deliver",
            lambda device_id, message: sent.append(message) or True,
        )

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        last = [m for m in sent if m["type"] == "tts_segments"][-1]["segments"]
        assert [s["start_ms"] for s in last] == [0, 160, 320], (
            "累加入队帧数会得到 [0, 100, 200]：第三句字幕比声音早 120ms"
        )

    @pytest.mark.asyncio
    async def test_plan_carries_char_offsets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """每段带 `char_ms`：打字机与口型共用这一份句内坐标，不再各自计时。"""
        text = "今天天气晴。紫外线有点强。记得戴帽子。"
        runtime = DialogueRuntime(service=_FakeService(text=text), tts=object())  # type: ignore[arg-type]
        _put_cascade()
        _patch_tts(monkeypatch, frames=5)

        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "app.dialogue_runtime.signaling.deliver",
            lambda device_id, message: sent.append(message) or True,
        )

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        last = [m for m in sent if m["type"] == "tts_segments"][-1]["segments"]
        for segment in last:
            offsets = segment["char_ms"]
            assert len(offsets) == len(segment["text"]), "一个字符一个偏移，标点也算"
            assert offsets == sorted(offsets)
            assert offsets[-1] == segment["duration_ms"]

    @pytest.mark.asyncio
    async def test_flush_tail_extends_last_segment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """收尾补帧也算播放时间：末段时长与末字偏移都要跟着延长。

        不补的话最后一句的字幕/口型会比声音早收 ≤20ms + 重采样残留。
        """
        runtime = DialogueRuntime(  # type: ignore[arg-type]
            service=_FakeService(text="今天天气晴。紫外线有点强。记得戴帽子。"), tts=object()
        )
        _put_cascade()
        _patch_tts(monkeypatch, frames=5, flush_frames=2)

        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "app.dialogue_runtime.signaling.deliver",
            lambda device_id, message: sent.append(message) or True,
        )

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        last = [m for m in sent if m["type"] == "tts_segments"][-1]["segments"]
        assert [s["duration_ms"] for s in last] == [100, 100, 140]
        assert last[-1]["char_ms"][-1] == 140
