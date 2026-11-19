"""用药提醒的反向外呼投递（PRD FR-MED-06，task-4）。

为什么单独一个模块而不是塞进 `app/scheduler/medication.py`：调度层只管「什么时候
触达」，`Deliver` 是它刻意留出的接缝（测试注入假投递，生产注入本模块的产物）。
把「查设备 → 生成话术 → 反向 WebRTC 外呼 → 换回执」放在 audio 层，一是让调度层不必
import WebRTC/信令，二是这条链路能脱离 APScheduler 直接单测。

分层口径（AGENTS §2.1）：本模块属于 `app/audio`，只向下 import `services` / `models`
/ `core`；对 `app/scheduler` 仅在 `TYPE_CHECKING` 下取类型，**运行时零依赖**——否则就
成了 audio ↔ scheduler 的横向 import，那正是 §2.1 禁止的。

失败一律吞掉并回 `"none"`：用药触达是 P0 提醒，投递侧任何异常都不该把调度循环打断
（`run_medication_touch` 还要继续写库、算重试、必要时升级派单）。降级后的可见性靠日志
与回执承担，不靠异常。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audio import outbound, webrtc
from app.core.context import get_trace_id
from app.models.organization import Device
from app.services import medication_service

if TYPE_CHECKING:  # 只为类型：运行时导它会违反分层约束（见模块 docstring）
    from app.scheduler.medication import Deliver, MedicationTouchTarget

logger = logging.getLogger(__name__)

RECEIPT_TTS_FINISHED: Final[str] = "tts_finished"
RECEIPT_CARD_RENDERED: Final[str] = "card_rendered"
"""建连失败但 WS 文本卡片送达（PRD line 314 的降级成功口径）。"""

RECEIPT_NONE: Final[str] = "none"

DEVICE_STATUS_LOCKED: Final[str] = "locked"
"""锁定态设备不发起外呼：护工停用/维护中的设备，接通了也不是长辈本人。"""

REASON_NO_DEVICE: Final[str] = "no_device"
REASON_DEVICE_LOCKED: Final[str] = "device_locked"

SessionFactory = async_sessionmaker[AsyncSession]


class OutboundSpeaker(Protocol):
    """`outbound.call_and_speak` 的结构签名（只约本模块实际传入的四个形参）。

    用 Protocol 而不是 `Callable[..., Awaitable[OutboundResult]]`：后者只有位置参数，
    注入方按位置传参时会静默错位（`device_id` 与 `text` 对调，排障要半小时）。
    """

    async def __call__(
        self,
        *,
        device_id: str,
        text: str,
        offerer: outbound.Offerer,
        sender: outbound.Sender,
    ) -> outbound.OutboundResult: ...


def _trace_id() -> str:
    """日志里的链路号；调度线程里没有请求上下文，缺省 `-` 而不是空串。"""
    return get_trace_id() or "-"


def _default_session_factory() -> SessionFactory:
    """惰性取 `app.db.session_factory`：导入本模块不应连带建一次引擎。"""
    from app.db import session_factory

    return session_factory


def _log_extra(target: MedicationTouchTarget, **extra: object) -> dict[str, object]:
    """统一日志字段：只放 id 与轮次，**药名/剂量/正文一律不进日志**（AGENTS §8）。"""
    payload: dict[str, object] = {
        "trace_id": _trace_id(),
        "reminder_id": str(target.reminder_id),
        "attempt_no": target.attempt_no,
    }
    payload.update(extra)
    return payload


async def _load_device(factory: SessionFactory, elder_id: uuid.UUID) -> tuple[str, str] | None:
    """按 `elders` 1:1 取设备的 `(device_id, status)`；没有绑定设备时返回 None。

    为什么查完立刻关会话：外呼最多要等 10 秒（`OUTBOUND_TIMEOUT_SECONDS`），把连接
    攥在手里纯粹浪费池子；而且调度线程写库发生在投递**之后**，中间不该占着一个会话。
    """
    async with factory() as session:
        row = (
            await session.execute(
                select(Device.device_id, Device.status).where(Device.elder_id == elder_id)
            )
        ).first()
    if row is None:
        return None
    return str(row.device_id), str(row.status)


def receipt_for_outbound(result: outbound.OutboundResult) -> str:
    """外呼结果 → `medication_attempts.receipt`（三值闭集，PRD line 314）。

    优先级即语义：`ok` 才是「语音说完了」；没说完但**卡片真的到了**算降级成功；
    其余（含降级也失败）一律 `none`，让调度层照常走重试与升级。
    """
    if result.ok:
        return RECEIPT_TTS_FINISHED
    if result.degraded_text_sent:
        return RECEIPT_CARD_RENDERED
    return RECEIPT_NONE


def build_medication_deliver(
    *,
    session_factory: SessionFactory | None = None,
    speaker: OutboundSpeaker | None = None,
) -> Deliver:
    """构造调度层用的 `Deliver`：一次触达 → 一个回执字符串。

    两个默认值都可替换，目的只有一个：让调用点（`app/main.py` 的装配）与测试共用
    同一份实现，而不是各写一条「查设备 + 拼话术 + 映射回执」的平行逻辑（AGENTS §4 禁区 8）。
    """
    factory = session_factory
    speak = speaker or outbound.call_and_speak

    async def deliver(target: MedicationTouchTarget) -> str | None:
        try:
            resolved = factory or _default_session_factory()
            device = await _load_device(resolved, target.elder_id)
        except Exception as exc:  # 查库失败不能挡住调度，按「没送到」处理
            logger.warning(
                "medication touch device lookup failed",
                extra=_log_extra(target, error_type=type(exc).__name__),
            )
            return RECEIPT_NONE

        if device is None:
            # info 而不是 warning：长者还没配设备是数据状态，不是故障，别把它刷成告警。
            logger.info(
                "medication touch skipped",
                extra=_log_extra(target, reason=REASON_NO_DEVICE),
            )
            return RECEIPT_NONE

        device_id, status = device
        if status == DEVICE_STATUS_LOCKED:
            logger.info(
                "medication touch skipped",
                extra=_log_extra(target, reason=REASON_DEVICE_LOCKED),
            )
            return RECEIPT_NONE

        try:
            text = medication_service.reminder_speech_text(target.drug_snapshot, target.attempt_no)
            result = await speak(
                device_id=device_id,
                text=text,
                # 传 None 而不是 "-"：`SignalingOfferer` 自带 `outbound:{device_id}`
                # 兜底，比一个空链路号更有用。
                offerer=webrtc.SignalingOfferer(device_id, trace_id=get_trace_id()),
                sender=outbound.SignalingSender(),
            )
        except Exception as exc:  # 话术/信令/网络任一处抛，都不许冒泡到调度循环
            logger.warning(
                "medication touch outbound failed",
                extra=_log_extra(target, error_type=type(exc).__name__),
            )
            return RECEIPT_NONE

        receipt = receipt_for_outbound(result)
        logger.info(
            "medication touch outbound finished",
            extra=_log_extra(
                target,
                receipt=receipt,
                rtc_result=result.rtc_result,
                degraded=result.degraded,
                degraded_text_sent=result.degraded_text_sent,
                elapsed_ms=result.elapsed_ms,
            ),
        )
        return receipt

    return deliver


__all__ = [
    "DEVICE_STATUS_LOCKED",
    "RECEIPT_CARD_RENDERED",
    "RECEIPT_NONE",
    "RECEIPT_TTS_FINISHED",
    "OutboundSpeaker",
    "build_medication_deliver",
    "receipt_for_outbound",
]
