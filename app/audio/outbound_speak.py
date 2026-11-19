"""外呼播报的生产实现：TTS → PCM → 该设备的下行播放轨（PRD FR-MED-06 / FR-UI-10）。

## 为什么需要这个文件

`outbound.call_and_speak` 的 `speak` 是注入面：建连成功之后，它只负责调用
`speak(device_id, text)`，自己不碰 TTS 与播放轨（`outbound.py` 刻意不依赖 aiortc）。
迭代 7 装配 `build_medication_deliver()` 时没传 `speaker`，于是每次用药外呼都是
「接通了、横幅亮了、回执写 `tts_finished`，但一个字都没说出来」。本模块补的就是
那个缺失的 `speak`：把话术合成成 16k/16bit/mono PCM，写进**该设备**的播放轨。

## 口径

- 音色取「设备 → 绑定长者 → 人设音色」（`persona_service.get_persona_voice_id`）；
  读不到就回落到 `TTS_VOICE_ID`（`build_tts` 内部裁决），**读库失败不阻断播报**。
- 播放代际号在**合成之前**取（同 `media_bridge._emit_greeting`）：合成这几百毫秒里
  长者开口打断过，`playback_generation` 会 +1，这段提醒就不该再跟在他的话音后面响。
- 没会话 / 空正文 / 空音频 / 播放轨已关 —— 一律抛 `OutboundSpeakError`。
  `outbound._announce` 会把任何异常转成 `rtc_failed` 并降级 WS 文本，
  回执因此变成 `card_rendered` 或 `none`，不再谎报「语音说完了」。

日志只记 `text_len` 与 `pcm_bytes`，**不记播报正文**（正文含药名，AGENTS §4.3）。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.context import get_trace_id

if TYPE_CHECKING:
    from app.audio.medication_delivery import OutboundSpeaker
    from app.audio.outbound import Offerer, OutboundResult, Sender

logger = logging.getLogger(__name__)


class OutboundSpeakError(RuntimeError):
    """播报没有真正送出去（没会话 / 合成空音频 / 播放轨已关）。"""


def _trace_id() -> str:
    return get_trace_id() or "-"


async def _voice_for_device(device_id: str) -> str | None:
    """设备 → 绑定长者 → 人设音色；任何一步取不到都返回 `None`（回落默认音色）。"""
    try:
        parsed = uuid.UUID(device_id)
    except (AttributeError, TypeError, ValueError):
        return None
    try:
        from app.db import session_factory
        from app.models.organization import Device
        from app.services import persona_service

        async with session_factory() as session:
            elder_id = await session.scalar(
                select(Device.elder_id).where(Device.device_id == parsed)
            )
            if elder_id is None:
                return None
            return await persona_service.get_persona_voice_id(session, elder_id=elder_id)
    except Exception as exc:  # 音色读不到只是「用默认嗓子」，不该让提醒没声音
        logger.warning(
            "outbound speak voice lookup failed",
            extra={
                "trace_id": _trace_id(),
                "device_id": device_id,
                "error_type": type(exc).__name__,
            },
        )
        return None


async def speak_text_to_device(device_id: str, text: str) -> None:
    """把一句播报合成成 PCM 并入队到该设备的下行播放轨。

    失败一律抛异常，由 `outbound` 转成降级；这里**不吞异常**——吞掉就又会回到
    「回执说说完、耳朵里没声音」的老问题。
    """
    body = (text or "").strip()
    if not body:
        raise OutboundSpeakError("播报正文为空")

    from app.audio import webrtc

    if not webrtc.has_media_session(device_id):
        raise OutboundSpeakError("设备没有媒体会话（未接听或已挂断）")

    generation = webrtc.playback_generation_for_device(device_id)

    from app.audio.tts import build_tts

    pcm = await build_tts(voice=await _voice_for_device(device_id)).synthesize(body)
    if not pcm:
        raise OutboundSpeakError("TTS 返回空音频")

    queued = await webrtc.play_system_tts_for_device(device_id, pcm, generation=generation)
    if not queued:
        raise OutboundSpeakError("播放轨已关闭，音频没有写入")

    logger.info(
        "outbound speak queued",
        extra={
            "trace_id": _trace_id(),
            "device_id": device_id,
            "text_len": len(body),
            "pcm_bytes": len(pcm),
            "generation": generation,
        },
    )


def build_outbound_speaker() -> OutboundSpeaker:
    """构造 `build_medication_deliver(speaker=...)` 要的生产实现。

    形状与 `outbound.call_and_speak` 一致（关键字参数），本函数只把 `speak` 钉死成
    `speak_text_to_device`——投递层不必知道 TTS 与播放轨的存在。
    """

    async def speaker(
        *,
        device_id: str,
        text: str,
        offerer: Offerer,
        sender: Sender,
    ) -> OutboundResult:
        from app.audio.outbound import call_and_speak

        return await call_and_speak(
            device_id=device_id,
            text=text,
            offerer=offerer,
            sender=sender,
            speak=speak_text_to_device,
        )

    return speaker
