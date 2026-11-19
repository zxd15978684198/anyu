"""TTS 掉线时，开场白仍要有逐句字幕计划（TDD 红阶段，实现未落地）。

## 为什么需要这组用例

容器里 edge-tts 连 ``speech.platform.bing.com:443`` 在 TLS 层被拦，
``build_tts().synthesize()`` 抛 ``TTSUnavailable``。当前实现的后果是：

- 开场白只下发 ``tts_text``，**没有** ``tts_segments`` →
  前端拿不到时间轴，字幕只能整段砸出来、口型不动；
- 长者看到的是「没说话文字先出来」或者「8 秒后整段冒出来」。

约定行为（本文件是契约，实现按它落地；落地前跑应当红）：

1. ``TTSUnavailable`` 时**仍投递**一条 ``tts_segments``：文本 = 开场白原文，
   ``start_ms == 0``，``duration_ms`` 按「中文 ~230ms/字、下限 1200ms」估算
   （允许 ±20% 容差），并记日志 ``identity_greeting_caption_estimated``
   （字段含 ``chars``、``duration_ms``）。
2. 同一情况**不**投递音频帧：``play_system_tts_for_device`` 不被调用。
3. TTS 正常时行为不变：一条 ``tts_segments``，``duration_ms`` 由 PCM 长度换算
   （``round(pcm_bytes / 32)``），且**不**出现估算日志。
4. 开场白时机：从**登录进页面**那一刻起算，总共
   ``GREETING_SPEAK_DEADLINE_SECONDS`` 秒；数字人就绪后只补「剩余时间」
   （``max(0, deadline − 已用)``），登录已超 deadline 则就绪即开口、不补负。
   闸门日志 ``identity_greeting_avatar_gate`` 含 ``greeting_delay_ms`` 与 ``since_login_ms``。

不测实现细节：不 patch 私有函数名、不查内部变量，只断言「下发了什么消息 / 打了什么日志」。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.audio import media_bridge
from app.audio import webrtc as webrtc_module
from app.audio.tts import TTSUnavailable

#: 开场白原文（任意固定句即可：字数决定估算时长的期望值）。
GREETING = "您好呀，我在这儿陪着您，想聊点什么就跟我说。"
#: 16kHz / mono / s16le：32 字节 = 1 毫秒 → 6400 字节 = 200ms。
PCM = b"\x00" * 6400
_PCM_BYTES_PER_MS = 32

#: 与 media_bridge 的估算口径一致（改口径必须同步改这里，否则用例失去意义）。
_ESTIMATE_MS_PER_CHAR = 230.0
_ESTIMATE_MIN_MS = 1200

#: 用例里把「从登录起算的 2 秒截止」压到 200ms：验的是「有没有按截止补等」，不是真的等 2 秒。
_DEADLINE = 0.2


class _Cascade:
    """`media_bridge._Cascade` 的最小替身：只看 device_id / trace_id / greeting / 计时起点。"""

    def __init__(
        self,
        *,
        device_id: str = "dev-caption",
        trace_id: str = "trace-caption",
        greeting: str | None = GREETING,
        voice_id: str | None = None,
        greeting_clock_start: float | None = None,
    ) -> None:
        self.device_id = device_id
        self.trace_id = trace_id
        self.greeting = greeting
        self.voice_id = voice_id
        self.greeting_clock_start = greeting_clock_start


class _TTS:
    """`TTSProvider` 替身：返回固定 PCM，或抛指定异常。"""

    def __init__(self, *, pcm: bytes = PCM, error: BaseException | None = None) -> None:
        self.pcm = pcm
        self.error = error
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.pcm


def _stack(*patches: Any) -> ExitStack:
    """把多条 patch 压进一个 ExitStack（五行 with 的缩进噪音太伤可读性）。"""
    stack = ExitStack()
    for item in patches:
        stack.enter_context(item)
    return stack


async def _drain_greeting_tasks() -> None:
    """等 media_bridge 甩到事件循环上的开场白任务跑完。"""
    for _ in range(5):
        pending = list(media_bridge._greeting_tasks)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _expected_duration_ms(text: str) -> int:
    return max(_ESTIMATE_MIN_MS, round(len(text) * _ESTIMATE_MS_PER_CHAR))


def _playback_pins() -> tuple[Any, Any]:
    """把「等轨 / 代际」钉死，避免用例真的等 10 秒或依赖真实会话。"""
    return (
        patch.object(webrtc_module, "playback_track_ready_for_device", return_value=True),
        patch.object(webrtc_module, "playback_generation_for_device", return_value=0),
    )


# ===========================================================================
# 一、TTSUnavailable：字幕计划要顶上来，音频不能说有就有
# ===========================================================================


class TestCaptionPlanWhenTtsUnavailable:
    @pytest.mark.asyncio
    async def test_delivers_estimated_segments(self, caplog: pytest.LogCaptureFixture) -> None:
        """TTS 挂掉时字幕计划是唯一的时间轴来源：必须下发，且时长按字数估算。"""
        delivered: list[dict[str, Any]] = []
        caplog.set_level(logging.INFO, logger="app.audio.identity")

        with _stack(
            patch.object(
                media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
            ),
            patch(
                "app.audio.tts.build_tts",
                return_value=_TTS(error=TTSUnavailable("edge-tts failed: TLS handshake blocked")),
            ),
            patch.object(
                webrtc_module, "play_system_tts_for_device", new=AsyncMock(return_value=False)
            ),
            *_playback_pins(),
        ):
            media_bridge._spawn_greeting_audio(_Cascade(), text=GREETING)
            await _drain_greeting_tasks()

        segments = [m for m in delivered if m["type"] == "tts_segments"]
        assert segments, (
            "TTS 掉线时一条字幕计划都没下发：前端只能整段砸字幕、口型不动"
            f"（实际投递 {[m['type'] for m in delivered]}）"
        )
        assert len(segments) == 1, f"开场白只应有一条计划，实际 {len(segments)} 条"
        segment = segments[0]["segments"][0]
        assert segment["text"] == GREETING
        assert segment["start_ms"] == 0

        expected = _expected_duration_ms(GREETING)
        low, high = expected * 0.8, expected * 1.2
        assert low <= segment["duration_ms"] <= high, (
            f"估算时长 {segment['duration_ms']}ms 超出 {expected}ms 的 ±20%："
            "前端打字机的节奏会明显快于/慢于朗读速度"
        )
        assert segment["duration_ms"] >= _ESTIMATE_MIN_MS, (
            "再短的开场白也要够长者读完（下限 1200ms）"
        )

        estimated = [
            record
            for record in caplog.records
            if record.getMessage() == "identity_greeting_caption_estimated"
        ]
        assert estimated, (
            "没有 identity_greeting_caption_estimated 日志：线上无法区分"
            "「TTS 挂了但字幕在走」与「整条链路都断了」"
        )
        record = estimated[0]
        assert getattr(record, "chars", None) == len(GREETING)
        assert getattr(record, "duration_ms", None) == segment["duration_ms"], (
            "日志里的 duration_ms 必须与实际下发的计划一致，否则排查时对不上前端时间轴"
        )

    @pytest.mark.asyncio
    async def test_does_not_emit_audio_frame(self) -> None:
        """没有合成结果就不许走播放入口：那等于承诺一段不存在的音频。"""
        delivered: list[dict[str, Any]] = []
        play = AsyncMock(return_value=True)

        with _stack(
            patch.object(
                media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
            ),
            patch(
                "app.audio.tts.build_tts",
                return_value=_TTS(error=TTSUnavailable("edge-tts failed: TLS handshake blocked")),
            ),
            patch.object(webrtc_module, "play_system_tts_for_device", new=play),
            *_playback_pins(),
        ):
            media_bridge._spawn_greeting_audio(_Cascade(), text=GREETING)
            await _drain_greeting_tasks()

        assert play.await_count == 0, (
            "TTS 合成失败却调用了 play_system_tts_for_device："
            "播放队列里会多出一段空音频，长者这边是「有声但没内容」"
        )


# ===========================================================================
# 二、TTS 正常：行为不变，时长由 PCM 反推
# ===========================================================================


class TestCaptionPlanWhenTtsSucceeds:
    @pytest.mark.asyncio
    async def test_keeps_pcm_derived_duration(self, caplog: pytest.LogCaptureFixture) -> None:
        delivered: list[dict[str, Any]] = []
        tts = _TTS()
        play = AsyncMock(return_value=True)
        caplog.set_level(logging.INFO, logger="app.audio.identity")

        with _stack(
            patch.object(
                media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
            ),
            patch("app.audio.tts.build_tts", return_value=tts),
            patch.object(webrtc_module, "play_system_tts_for_device", new=play),
            *_playback_pins(),
        ):
            media_bridge._spawn_greeting_audio(_Cascade(), text=GREETING)
            await _drain_greeting_tasks()

        assert tts.texts == [GREETING]
        assert play.await_count == 1, "TTS 成功时音频照旧要进播放队列"

        segments = [m for m in delivered if m["type"] == "tts_segments"]
        assert len(segments) == 1
        segment = segments[0]["segments"][0]
        assert segment["text"] == GREETING
        assert segment["duration_ms"] == round(len(PCM) / _PCM_BYTES_PER_MS), (
            "正常路径的时长必须由 PCM 长度反推（32 字节 = 1ms），不许走字数估算"
        )

        estimated = [
            record
            for record in caplog.records
            if record.getMessage() == "identity_greeting_caption_estimated"
        ]
        assert not estimated, "TTS 正常还打估算日志，会让人以为时长是猜的"

    @pytest.mark.asyncio
    async def test_empty_pcm_still_delivers_estimated_caption(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """合成「不报错但吐空字节」时同样要有字幕计划，且不许进播放队列。

        空 stdout 是 espeak 静音/ffmpeg 空转的真实形态：它不抛异常，但前端拿到的是
        0 字节音频 —— 没有时间轴、口型不动，比抛异常更难排查（异常至少还有 error_type）。
        """
        delivered: list[dict[str, Any]] = []
        play = AsyncMock(return_value=True)
        caplog.set_level(logging.INFO, logger="app.audio.identity")
        empty = _TTS(pcm=b"")

        with _stack(
            patch.object(
                media_bridge, "_deliver", side_effect=lambda d, m: delivered.append(m) or True
            ),
            patch("app.audio.tts.build_tts", return_value=empty),
            patch.object(webrtc_module, "play_system_tts_for_device", new=play),
            *_playback_pins(),
        ):
            media_bridge._spawn_greeting_audio(_Cascade(), text=GREETING)
            await _drain_greeting_tasks()

        assert empty.texts == [GREETING]
        segments = [m for m in delivered if m["type"] == "tts_segments"]
        assert segments, (
            "空 pcm 时一条字幕计划都没下发：音频是 0 字节、字幕也没有，"
            f"前端只能等 8s 兜底（实际投递 {[m['type'] for m in delivered]}）"
        )
        assert len(segments) == 1, f"开场白只应有一条计划，实际 {len(segments)} 条"
        segment = segments[0]["segments"][0]
        assert segment["text"] == GREETING
        assert segment["start_ms"] == 0

        expected = _expected_duration_ms(GREETING)
        low, high = expected * 0.8, expected * 1.2
        assert low <= segment["duration_ms"] <= high, (
            f"空 pcm 的估算时长 {segment['duration_ms']}ms 超出 {expected}ms 的 ±20%"
        )

        assert play.await_count == 0, (
            "空 pcm 也调了 play_system_tts_for_device：播放队列会多一段静音"
        )

        empties = [
            record
            for record in caplog.records
            if record.getMessage() == "identity_greeting_tts_empty"
        ]
        assert empties, "空 pcm 没打 identity_greeting_tts_empty：线上分不清「静音」与「字幕没发」"
        estimated = [
            record
            for record in caplog.records
            if record.getMessage() == "identity_greeting_caption_estimated"
        ]
        assert estimated, "空 pcm 走了兜底却没有估算日志：时间轴无从追溯"
        assert getattr(estimated[0], "error_type", None) == "empty_pcm", (
            "估算日志的 error_type 应区分 empty_pcm 与真实异常，否则排查时看不出是哪种失败"
        )


# ===========================================================================
# 三、问候语时机：从登录起算 2 秒截止，就绪后只补剩余时间
# ===========================================================================
# 用户 2026-11-11 裁决：「开场白的 2 秒**从登录进入页面那一刻**起算，总共 2 秒」。
# 不是「就绪后再等 2 秒」—— 就绪花掉的时间要从这 2 秒里扣掉，登录本身慢于 2 秒时
# 就绪即开口。下面三条分别钉住：补剩余、不补负、超时兜底不追加。


class TestGreetingVoice:
    @pytest.mark.asyncio
    async def test_uses_persona_voice_for_greeting_tts(self) -> None:
        tts = _TTS()

        with (
            patch("app.audio.tts.build_tts", return_value=tts) as factory,
            _stack(
                patch.object(media_bridge, "_deliver", return_value=True),
                patch.object(
                    webrtc_module, "play_system_tts_for_device", new=AsyncMock(return_value=True)
                ),
                *_playback_pins(),
            ),
        ):
            media_bridge._spawn_greeting_audio(
                _Cascade(voice_id="zh-CN-XiaoyiNeural"), text=GREETING
            )
            await _drain_greeting_tasks()

        factory.assert_called_once_with(voice="zh-CN-XiaoyiNeural")
        assert tts.texts == [GREETING]


class TestGreetingAvatarDelay:
    @pytest.mark.asyncio
    async def test_waits_until_deadline_when_avatar_ready_early(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """就绪很早 → 补到「登录 + deadline」才开口（给长者时间看清谁在说话）。"""
        delivered: list[tuple[float, dict[str, Any]]] = []
        loop = asyncio.get_running_loop()
        started = loop.time()

        async def fake_wait(device_id: str) -> bool:
            return True  # 立刻就绪：剩余时间 ≈ 全部 deadline

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append((time.monotonic(), message))
            return True

        caplog.set_level(logging.INFO, logger="app.audio.identity")

        with _stack(
            patch.object(media_bridge, "_has_media_session", return_value=True),
            patch.object(media_bridge, "wait_avatar_ready", new=AsyncMock(side_effect=fake_wait)),
            patch.object(media_bridge, "_deliver", side_effect=fake_deliver),
            patch.object(media_bridge, "GREETING_SPEAK_DEADLINE_SECONDS", _DEADLINE),
            patch("app.audio.tts.build_tts", return_value=_TTS()),
            patch.object(
                webrtc_module, "play_system_tts_for_device", new=AsyncMock(return_value=True)
            ),
            *_playback_pins(),
        ):
            media_bridge._emit_greeting(_Cascade(greeting_clock_start=started), generic=False)
            await _drain_greeting_tasks()

        text_deliveries = [(t, m) for t, m in delivered if m["type"] == "tts_text"]
        assert text_deliveries, "开场白文本没下发"
        # 起点是「登录」，不是「就绪」：用 loop.time() 量同一条时钟
        elapsed = loop.time() - started
        assert elapsed >= _DEADLINE, (
            f"从登录起 {elapsed * 1000:.1f}ms 就开口了，约定是补到 {_DEADLINE * 1000:.0f}ms："
            "VRM 还在首帧姿态上，声音和人对不上"
        )

        gate_logs = [
            record
            for record in caplog.records
            if record.getMessage() == "identity_greeting_avatar_gate"
        ]
        assert gate_logs, "没有 identity_greeting_avatar_gate 日志：线上看不出开口闸门走了哪条路"
        assert getattr(gate_logs[0], "greeting_delay_ms", None) is not None, (
            "开场白闸门日志必须带 greeting_delay_ms，否则线上看不出到底补等了多久"
        )
        assert getattr(gate_logs[0], "since_login_ms", None) is not None, (
            "开场白闸门日志必须带 since_login_ms，否则线上看不出 2 秒预算实际用了多少"
        )

    @pytest.mark.asyncio
    async def test_no_wait_when_login_already_slower_than_deadline(self) -> None:
        """登录本身就超过 deadline（如 3s）→ 就绪后**立刻**开口，不补负、不追加等待。

        这是本轮最关键的验收：旧实现「等就绪 + 再等一段」会在这里再拖一个 deadline，
        旧测试也验不出来（它只断言「等了」，没断言「该等 0 时真的没等」）。
        """
        delivered: list[tuple[float, dict[str, Any]]] = []
        loop = asyncio.get_running_loop()

        async def fake_wait(device_id: str) -> bool:
            return True

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append((time.monotonic(), message))
            return True

        with _stack(
            patch.object(media_bridge, "_has_media_session", return_value=True),
            patch.object(media_bridge, "wait_avatar_ready", new=AsyncMock(side_effect=fake_wait)),
            patch.object(media_bridge, "_deliver", side_effect=fake_deliver),
            patch.object(media_bridge, "GREETING_SPEAK_DEADLINE_SECONDS", _DEADLINE),
            patch("app.audio.tts.build_tts", return_value=_TTS()),
            patch.object(
                webrtc_module, "play_system_tts_for_device", new=AsyncMock(return_value=True)
            ),
            *_playback_pins(),
        ):
            # 登录→就绪已经用掉 3 秒，远超 0.2s 的 deadline
            media_bridge._emit_greeting(
                _Cascade(greeting_clock_start=loop.time() - 3.0), generic=False
            )
            signalled_at = time.monotonic()
            await _drain_greeting_tasks()

        text_deliveries = [(t, m) for t, m in delivered if m["type"] == "tts_text"]
        assert text_deliveries, "登录已超 deadline 时开场白文本也得下发（不许静默丢掉）"
        elapsed = text_deliveries[0][0] - signalled_at
        assert elapsed < 0.05, (
            f"登录已超 deadline 却又等了 {elapsed * 1000:.1f}ms："
            "2 秒是从登录起算的总时长，超时应当就绪即开口，不能再追加"
        )

    @pytest.mark.asyncio
    async def test_timeout_fallback_does_not_delay(self) -> None:
        """等满 5 秒兜底放行时**不再**补等：5 秒早已越过 2 秒截止，remain=0。"""
        delivered: list[tuple[float, dict[str, Any]]] = []
        ready_at: dict[str, float] = {}
        loop = asyncio.get_running_loop()

        async def fake_wait(device_id: str) -> bool:
            ready_at["t"] = time.monotonic()
            return False

        def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
            delivered.append((time.monotonic(), message))
            return True

        with _stack(
            patch.object(media_bridge, "_has_media_session", return_value=True),
            patch.object(media_bridge, "wait_avatar_ready", new=AsyncMock(side_effect=fake_wait)),
            patch.object(media_bridge, "_deliver", side_effect=fake_deliver),
            patch.object(media_bridge, "GREETING_SPEAK_DEADLINE_SECONDS", _DEADLINE),
            patch("app.audio.tts.build_tts", return_value=_TTS()),
            patch.object(
                webrtc_module, "play_system_tts_for_device", new=AsyncMock(return_value=True)
            ),
            *_playback_pins(),
        ):
            # 模拟「等数字人一直等到 5s 上限」：起点前移 5 秒，早越过 deadline
            media_bridge._emit_greeting(
                _Cascade(greeting_clock_start=loop.time() - 5.0), generic=False
            )
            await _drain_greeting_tasks()

        text_deliveries = [(t, m) for t, m in delivered if m["type"] == "tts_text"]
        assert text_deliveries, "兜底放行时开场白文本也没下发"
        elapsed = text_deliveries[0][0] - ready_at["t"]
        assert elapsed < _DEADLINE / 2, (
            f"超时兜底后又等了 {elapsed * 1000:.1f}ms："
            "长者已白等 5 秒，2 秒截止也早过了，这里不能追加等待"
        )
