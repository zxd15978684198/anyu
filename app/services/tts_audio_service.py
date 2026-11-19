"""把一段文本合成为可播放的静态音频（`/static/mock/*.wav`）。

为什么要有这一层：长者端有两条回话通路 —— 话音轮（WebSocket 里直接推 PCM）与
打字轮（`POST /api/dialogue/text`，纯 HTTP）。打字轮此前**没有声音**：回复只有字幕，
数字人不张嘴，长者会以为「它没理我」。让打字轮也出声，就得在 HTTP 侧把文本合成成文件
并返回一个浏览器能直接播的 URL。

分层（AGENTS §2）：合成 + 落盘属于基础设施能力，放 `services/`；`api/audio.py`
（按住说话）与 `api/dialogue.py`（打字）都调这里，**只有一份实现**。

失败口径：合成失败不抛给上层（打字回复不该因为 TTS 挂了变成 5xx），返回 `None`
并记一条 `tts_audio_failed`。上层把 `audio_url` 置空即可，前端会退回纯字幕。

用药播报（FR-CON-04，2026-11-12 补）：长者点「已服用 / 稍后提醒」后要有一句语音反馈。
话术写在这里而不是前端，理由与阈值写进代码同源（AGENTS §6 L0：口径不进 prompt/页面）：
同一句话要么被合成成 WAV 播出来，要么原样当大字幕上屏 —— 两条路只有一个文本来源，
不会出现「听到的」和「看到的」不一致。合成入口是 `announce_medication()`，
纯文本入口是 `medication_announcement()`（给不跑 TTS 的调用方/测试用）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import wave
from dataclasses import dataclass

from app.audio.asr import TTSProvider
from app.audio.tts_sentences import char_offsets_ms, split_sentences

logger = logging.getLogger(__name__)

#: 合成结果落盘目录（相对进程工作目录，nginx 把仓库根的 `static/` 挂到 `/static/`）。
STATIC_SUBDIR: tuple[str, ...] = ("static", "mock")

#: 16k / mono / 16bit —— 与 `TTSProvider.synthesize()` 返回的 PCM 口径一致。
_SAMPLE_RATE = 16_000
_SAMPLE_WIDTH = 2
_CHANNELS = 1

#: 单条回复的合成上限：超过就别合成了（打字正文最长 200 字，正常远低于此）。
MAX_SYNTH_CHARS = 400


@dataclass(frozen=True, slots=True)
class AudioSegment:
    """一句字幕的时间窗。

    与语音链路的 `tts_segments` 帧同口径：`start_ms` 是从整段起点累计的毫秒，
    `duration_ms` 是该句 PCM 换算出来的时长。前端拿它驱动逐字/逐句，
    口型读的是同一路音频，所以两者不会各按各的节奏走。
    """

    text: str
    start_ms: int
    duration_ms: int
    #: 句内逐字坐标（每个字的结束偏移，相对句首）；空元组 = 没算出来，前端退回匀速比例。
    char_ms: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SynthesizedAudio:
    """合成结果：浏览器可直接用的 URL + 时长 + 逐句时间窗。"""

    url: str
    duration_ms: int
    segments: tuple[AudioSegment, ...] = ()


def pcm_duration_ms(pcm_bytes: int, sample_rate: int = _SAMPLE_RATE) -> int:
    """PCM16 单声道字节数 → 毫秒。至少回 1ms（0 会让前端计划时长为 0）。"""
    if pcm_bytes <= 0:
        return 0
    bytes_per_ms = (sample_rate * _CHANNELS * _SAMPLE_WIDTH) / 1000.0
    return max(1, round(pcm_bytes / bytes_per_ms))


async def synthesize_to_static(
    tts: TTSProvider,
    text: str,
    *,
    trace_id: str = "-",
) -> SynthesizedAudio | None:
    """合成 `text` → 写 WAV 到 `static/mock/` → 返回 URL 与时长。

    返回 `None` 的三种情况：文本为空、超长、合成或落盘失败（均已记日志）。
    """
    spoken = text.strip()
    if not spoken:
        return None
    if len(spoken) > MAX_SYNTH_CHARS:
        logger.warning(
            "tts_audio_skipped",
            extra={"trace_id": trace_id, "reason": "too_long", "chars": len(spoken)},
        )
        return None

    # 逐句合成：一句话一个 PCM 块，拼成一个 WAV 播，但同时记下每句的时间窗。
    # 为什么不用整段一次合成：整段只有总时长，前端只能按平均字速铺开，
    # 而语音在逗号/句号处会停、数字和英文读得慢 —— 字幕必然与口型越走越偏。
    sentences = [part for part in split_sentences(spoken) if part.strip()]
    if not sentences:
        sentences = [spoken]

    chunks: list[bytes] = []
    segments: list[AudioSegment] = []
    cursor_ms = 0
    for index, sentence in enumerate(sentences):
        try:
            chunk = await tts.synthesize(sentence)
        except Exception as exc:  # 任何 TTS 故障都只降级，不冒泡（不能让它把打字回复变成 5xx）
            logger.warning(
                "tts_audio_failed",
                extra={
                    "trace_id": trace_id,
                    "reason": "synthesize",
                    "error_type": type(exc).__name__,
                    "sentence_index": index,
                },
            )
            return None

        if not chunk:
            logger.warning(
                "tts_audio_failed",
                extra={"trace_id": trace_id, "reason": "empty_pcm", "sentence_index": index},
            )
            return None

        duration_ms = pcm_duration_ms(len(chunk))
        segments.append(
            AudioSegment(
                text=sentence,
                start_ms=cursor_ms,
                duration_ms=duration_ms,
                char_ms=tuple(char_offsets_ms(chunk, sentence, duration_ms=duration_ms)),
            )
        )
        cursor_ms += duration_ms
        chunks.append(chunk)

    pcm_bytes = b"".join(chunks)
    wav_bytes = _wrap_wav(pcm_bytes)
    try:
        url = await _write_wav(wav_bytes)
    except OSError as exc:
        logger.warning(
            "tts_audio_failed",
            extra={"trace_id": trace_id, "reason": "write", "error_type": type(exc).__name__},
        )
        return None

    logger.info(
        "tts_audio_ready",
        extra={
            "trace_id": trace_id,
            "chars": len(spoken),
            "pcm_bytes": len(pcm_bytes),
            "duration_ms": cursor_ms or pcm_duration_ms(len(pcm_bytes)),
            "sentences": len(segments),
        },
    )
    return SynthesizedAudio(
        url=url, duration_ms=cursor_ms or pcm_duration_ms(len(pcm_bytes)), segments=tuple(segments)
    )


# ------------------------------------------------------------------ 用药播报（FR-CON-04）

#: 需要播报的用药操作（pad 卡片上两个按钮）。taken=已服用，snooze=稍后提醒。
MEDICATION_ANNOUNCE_ACTIONS: tuple[str, ...] = ("taken", "snooze")

#: 话术表：`(带药名, 不带药名)`。药名缺失（提醒没带名字）时用不带药名的那句，
#: 不拼接空串——「好，已经记下您吃过了。」比「好， 已经记下…」稳。
_MEDICATION_ANNOUNCEMENTS: dict[str, tuple[str, str]] = {
    "taken": ("好，{name}已经记下您吃过了。", "好，已经记下您吃过了。"),
    "snooze": ("好，{name}我过一会儿再提醒您。", "好，我过一会儿再提醒您。"),
}

#: 降级字幕：TTS 不可用时上屏的短句（PRD 口径「语音+文字提示」，两条路同一件事）。
_MEDICATION_SUBTITLES: dict[str, str] = {
    "taken": "已记下：您已服用",
    "snooze": "好，稍后提醒您",
}


@dataclass(frozen=True, slots=True)
class MedicationAnnouncement:
    """一次用药操作的播报载荷：同一句话 + 可选音频。

    `audio is None` 就是「TTS 不可用」的完整表达：调用方据此把 `subtitle` 当大字幕上屏
    （AGENTS §6 降级链：大字幕 + 预设音频 / 浏览器 speechSynthesis），**不影响**确认或
    稍后提醒本身已经落库的结果 —— 播报是增强项，不是主链路的前置条件。
    """

    action: str
    text: str
    subtitle: str
    audio: SynthesizedAudio | None = None

    @property
    def degraded(self) -> bool:
        """没有合成出音频 → 前端必须走字幕/本机 speechSynthesis。"""
        return self.audio is None

    @property
    def audio_url(self) -> str | None:
        """可直接喂给 `<audio src>` 的地址；降级时为 `None`。"""
        return self.audio.url if self.audio is not None else None

    @property
    def duration_ms(self) -> int | None:
        """音频时长（前端用它对齐字幕节奏）；降级时为 `None`。"""
        return self.audio.duration_ms if self.audio is not None else None


def _clean_med_name(med_name: str | None) -> str | None:
    """药名只取首尾空白以内的内容；空串/纯标点按「没给名字」处理。"""
    cleaned = (med_name or "").strip().strip("，,。.、；;：:")
    return cleaned or None


def medication_announcement(action: str, *, med_name: str | None = None) -> MedicationAnnouncement:
    """把「已服用 / 稍后提醒」翻成一句播报（不含音频，纯文本口径）。

    未知 action 抛 `ValueError`：这是调用方写错了枚举（不是长者能给错的输入），
    静默兜底会让「点什么都没声音」变成不可观测的哑链路。
    """
    templates = _MEDICATION_ANNOUNCEMENTS.get(action)
    if templates is None:
        raise ValueError(
            f"unknown medication announcement action: {action!r} "
            f"(expected one of {', '.join(MEDICATION_ANNOUNCE_ACTIONS)})"
        )
    name = _clean_med_name(med_name)
    text = templates[0].format(name=name) if name else templates[1]
    return MedicationAnnouncement(action=action, text=text, subtitle=_MEDICATION_SUBTITLES[action])


async def announce_medication(
    tts: TTSProvider,
    action: str,
    *,
    med_name: str | None = None,
    trace_id: str = "-",
) -> MedicationAnnouncement:
    """合成一句用药播报；**永不因为 TTS 故障抛异常**（降级为字幕，主链路不受影响）。

    失败的处理不是「吞掉」：合成侧已由 `synthesize_to_static` 记 `tts_audio_failed`
    （reason=empty_pcm / write / synthesize），这里再记一条 `medication_announce_degraded`
    把「哪次操作、哪句话没播出来」跟 trace_id 绑在一起，之后能查、能补播。
    """
    announcement = medication_announcement(action, med_name=med_name)
    audio: SynthesizedAudio | None = None
    try:
        audio = await synthesize_to_static(tts, announcement.text, trace_id=trace_id)
    except Exception as exc:  # 防御：合成实现若漏抛，播报也不许把确认链路带崩
        logger.warning(
            "medication_announce_degraded",
            extra={
                "trace_id": trace_id,
                "action": action,
                "reason": "synthesize_raised",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        audio = None

    result = MedicationAnnouncement(
        action=announcement.action,
        text=announcement.text,
        subtitle=announcement.subtitle,
        audio=audio,
    )
    if result.degraded:
        logger.info(
            "medication_announce_degraded",
            extra={
                "trace_id": trace_id,
                "action": action,
                "reason": "no_audio",
                "subtitle": announcement.subtitle,
            },
        )
    else:
        logger.info(
            "medication_announce_ready",
            extra={
                "trace_id": trace_id,
                "action": action,
                "audio_url": result.audio_url,
                "duration_ms": result.duration_ms,
            },
        )
    return result


def _wrap_wav(pcm_bytes: bytes) -> bytes:
    """PCM16 → WAV 容器（浏览器 `<audio>` 只认容器，裸 PCM 不播）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_CHANNELS)
        w.setsampwidth(_SAMPLE_WIDTH)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


async def _write_wav(wav_bytes: bytes) -> str:
    """落盘到 `static/mock/tts_<毫秒>_<pid>.wav`，返回 nginx 可直出的 URL。"""
    static_dir = os.path.join(os.getcwd(), *STATIC_SUBDIR)
    await asyncio.to_thread(os.makedirs, static_dir, exist_ok=True)
    filename = f"tts_{int(time.time() * 1000)}_{os.getpid()}.wav"
    filepath = os.path.join(static_dir, filename)

    def _write() -> None:
        with open(filepath, "wb") as f:
            f.write(wav_bytes)

    await asyncio.to_thread(_write)
    return f"/static/mock/{filename}"


__all__ = [
    "MAX_SYNTH_CHARS",
    "MEDICATION_ANNOUNCE_ACTIONS",
    "MedicationAnnouncement",
    "SynthesizedAudio",
    "announce_medication",
    "medication_announcement",
    "pcm_duration_ms",
    "synthesize_to_static",
]
