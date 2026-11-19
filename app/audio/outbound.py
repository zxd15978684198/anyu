"""反向 WebRTC 外呼与降级（迭代 7 W4 · PRD FR-MED-06 / FR-UI-10 · AC-41）。

## 这个模块做什么

到点（用药提醒 / 健康采集）由服务端**主动**建一条 WebRTC：服务端出 offer →
pad 自动接听回 answer → 数字人开口播报。三条失败路径（ICE 不通 / 预算内没等到
answer / 对方明确拒接）**一律**降级成 WS 文本播报，并把分类交回调用方落
`medication_attempts.rtc_result`。**本模块不写库**：落库归 services（AGENTS §2 分层）。

## 为什么复用 C2 的 `offer/answer`，不新增消息类型

C2 信封是 迭代 4 冻结的**闭集**（`signaling.py` 顶部「改动必须走契约变更流程」；
迭代 7 裁决 S-D7-3 明写「外呼复用 C2 的 offer/answer，不新增消息类型」）。反向外呼
需要表达的语义——把一个 SDP 交给对端、拿回一个 SDP——与既有 `offer` / `answer`
**逐字段相同**，差别只在「谁先说」。为这点差别新增一种消息类型，代价是三处立刻分叉：
契约文档、`signaling.py` 的信封、前端 `protocol.ts` 的联合类型；收益只是省掉一次
方向判断。所以本模块只做**方向反转**：

- 下行 offer = `signaling.OfferMsg` 的形状（`type` + `sdp`，一个字段不多、一个不少）；
- 降级播报 = `signaling.TtsTextMsg` 的形状（`degraded` 恒显式 `True`，FR-DLG-05）。

`build_offer_message` / `build_degrade_text_message` 只把这两个形状原样拼出来，
**运行期不 import `signaling`**（类型走 `TYPE_CHECKING`）：本模块因此不背
FastAPI / WebSocket / 数据库依赖，scheduler 引它更轻。二者与 C2 的一致性由
`tests/test_outbound.py` 直接对 `signaling` 的 TypedDict 做断言兜住。

## 为什么失败一律降级到 WS 文本

对长者来说「外呼失败」是不存在的概念：他只会看到屏幕黑着、没人说话。**听不到**
比「听不到但能看到大字」糟得多，而用药提醒是 P0 触达，少一次播报就是一次漏服风险
（PRD FR-MED-06：建连失败降级为 WebSocket 下发页面播报，触达仍可达）。因此本模块
**不存在「只返回失败、什么都不发」的分支**：每个非 `ok` 的结果都已走过降级播报
（`degraded=True`），调用方不需要再补一次，否则长者会看到两遍文字。

## 依赖注入（为什么不 import webrtc 的全局单例）

`create_offer` 的动作只能落在 `MediaSession` 上，而 `app/audio/webrtc.py` 不在本卡
写作用域内；更要紧的是：把「建连动作」做成注入面，测试才能用假 offerer 把 ICE 失败 /
超时 / 拒接三条路径**不启动 aiortc、不真连网**地跑出来。注入面全部 async，
失败用异常表达（不用返回值猜）：

| 形参 | 形状 | 失败约定 |
| --- | --- | --- |
| `offerer` | `create_offer() -> str`；可选 `wait_answer(seconds) -> bool` | 见下方异常表 |
| `sender` | `send(device_id, message) -> None` | 设备不在线 → `DeviceOfflineError` |
| `wait_answer` | `(seconds) -> bool`，覆盖 `offerer` 的同名方法 | 拒接 → `AnswerRejectedError`；ICE → `IceFailedError` |
| `speak` | `(device_id, text) -> None` | 任何异常都按「播报失败」降级；**不注入也按失败算**（装配错误，不许谎报说完） |
| `fallback_send_text` | `(device_id, text) -> None` | 不注入时退回 `sender.send(tts_text)` |

## 失败分类（`rtc_result` 闭集）

| 触发 | `rtc_result` | `degraded` | `ok` |
| --- | --- | --- | --- |
| 建连成功且播报成功 | `ok` | False | True |
| ICE 连通失败 | `ice_failed` | True | False |
| 预算内没等到 answer（含等待自身超时） | `timeout` | True | False |
| 设备不在线 / 对端明确拒绝 | `no_answer` | True | False |
| 未注入 `speak`（生产装配错误，只建连没说话） | `rtc_failed` | True | False |
| 其余（createOffer 异常、播报失败、未注入 wait_answer…） | `rtc_failed` | True | False |

`timeout` 与 `no_answer` **刻意分开**：前者是「叫了没人应」，后者是「明确说没接」；
混成一个数就看不出是前端没自动应答，还是长者按了拒绝。落库口径由
`app/services/medication_service.failure_reason_for_outbound` 统一裁决（本模块不写
`failure_reason`，只回 `rtc_result` + `degraded_text_sent`）：
`timeout` / `no_answer` / `ice_failed` / 其余失败一律归 `rtc_failed`，但**已知设备离线**
归 `offline`、建连失败而卡片真的送达归 `tts_failed`——后两种只有调用方知道，
所以由服务层那个纯函数接住，这里不猜。

## 打点

`counter("outbound_calls_total", result=<rtc_result>)` +
`observe_ms("outbound_call_ms", elapsed_ms)`，两条路径都打（成功与降级各一次）。
`metrics_enabled=False` 时本模块直接不打点——`app/core/metrics.py` 自己的开关照常生效，
这个形参只让调用方在压测/单测里**显式**关掉，而不必改全局配置。

日志只记 `text_len`，**不记播报正文**：正文里有药名，属于个人敏感信息（AGENTS §4.3）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from app.core.context import get_trace_id
from app.core.metrics import counter, observe_ms

if TYPE_CHECKING:
    from app.audio.signaling import OfferMsg, TtsTextMsg

logger = logging.getLogger("app.audio.outbound")

# ------------------------------------------------------------------ 常量

#: 外呼预算（秒）：PRD FR-MED-06 / 迭代 7 §W4 的「超时 10s」。
#: 先落模块常量而不是 settings：`app/core/config.py` 本卡只读且尚未加 `outbound_*` 字段，
#: 等配置项落地后只需把签名默认值改成 `None` + 内部读 settings，调用方一行不动。
OUTBOUND_TIMEOUT_SECONDS: Final[float] = 10.0

#: C2 闭集里被复用的两个类型名（见模块 docstring；不复用之外的任何字符串）。
OFFER_MESSAGE_TYPE: Final[str] = "offer"
DEGRADE_MESSAGE_TYPE: Final[str] = "tts_text"

#: `rtc_result` 闭集。新增取值必须同步技术文档 rtc_status 口径与落库映射。
RTC_OK: Final[str] = "ok"
RTC_FAILED: Final[str] = "rtc_failed"
RTC_TIMEOUT: Final[str] = "timeout"
RTC_NO_ANSWER: Final[str] = "no_answer"
RTC_ICE_FAILED: Final[str] = "ice_failed"
RTC_RESULTS: Final[frozenset[str]] = frozenset(
    {RTC_OK, RTC_FAILED, RTC_TIMEOUT, RTC_NO_ANSWER, RTC_ICE_FAILED}
)

#: 打点名（与 `app/core/metrics.py` 的 key 口径一致：裸 name + 标签）。
METRIC_CALLS: Final[str] = "outbound_calls_total"
METRIC_ELAPSED: Final[str] = "outbound_call_ms"


# ------------------------------------------------------------------ 结果与异常


@dataclass(frozen=True, slots=True)
class OutboundResult:
    """一次外呼的结果：成功与失败**同一个类型**，调用方按 `ok` 分支即可。

    - `ok`：建连成功且播报成功（未注入 `speak` 按失败处理：只建连没说话不算说过）；
    - `rtc_result`：`RTC_RESULTS` 中的一个，直接给调用方写 `medication_attempts`；
    - `degraded`：是否已走降级播报（恒等于 `not ok`；降级自身失败也仍然为 True）；
    - `degraded_text_sent`：**降级文本是否真的送到设备**。`degraded=True` 只说明
      「没走成语音」，不代表卡片发出去了；用药触达要按 PRD FR-MED-06 区分
      「降级成功（回执 `card_rendered`）」与「两路都没送到（回执 `none`）」，
      所以把这一位显式回传，而不是让调用方猜；
    - `reason`：给日志/汇报看的中文原因，成功时为 `None`；
    - `elapsed_ms`：整条外呼（建连 + 播报 + 降级）的耗时，毫秒取整。
    """

    ok: bool
    rtc_result: str
    degraded: bool
    reason: str | None
    elapsed_ms: int
    degraded_text_sent: bool = False


class OutboundError(Exception):
    """外呼链路的可归类失败基类。注入实现抛它的子类，本模块据此分类。"""


class DeviceOfflineError(OutboundError):
    """目标设备当前没有已鉴权的 WS 连接 → `no_answer`。"""


class AnswerRejectedError(OutboundError):
    """对端明确拒绝接听（或回了不可用的 answer）→ `no_answer`。"""


class IceFailedError(OutboundError):
    """ICE 连通性失败 → `ice_failed`。"""


class _MissingWaitAnswer(OutboundError):
    """`offerer` 与调用方都没提供等待 answer 的能力 → `rtc_failed`（不能假装接通）。"""


# ------------------------------------------------------------------ 注入面


class Offerer(Protocol):
    """服务端 offer 的发起方。

    生产实现是 `SignalingOfferer`（`webrtc.py`）；测试注入假实现。

    ## 只建不发的分工（2026-11-10 N62f 缺陷 A）

    `create_offer()` **只负责生成 SDP（含 setLocalDescription）**，投递由本模块的
    `Sender` 完成 —— 一次外呼只允许出现一条 `type=offer` 帧。此前 `SignalingOfferer`
    内部也 `_deliver` 一次，两处各发一条：pad 收到两条 offer 就建两个
    `RTCPeerConnection`、回两个 answer，第二个 answer 落到已 CONNECTING 的会话上
    触发 `MediaFormatError` → 会话被关 → 播报 `MediaClosedError`，迭代 7 外呼从未跑通。

    选择把出口留在 `Sender` 侧而不是 `Offerer` 侧，理由是：信封形状
    （`build_offer_message` / `signaling.OfferMsg`）与「设备离线 → `DeviceOfflineError`」
    本来就都在本模块，出口和协议待在同一个文件里才不会再次分叉。
    """

    async def create_offer(self) -> str:
        """生成服务端 SDP offer（内含 setLocalDescription 完成）；**不负责下发**。"""
        ...


class Sender(Protocol):
    """C2 信封投递方。生产实现见 `SignalingSender`。

    `message` 声明成 `Mapping[str, Any]` 而不是 `dict[str, Any]`：C2 信封是
    `TypedDict`（`signaling.OfferMsg` / `TtsTextMsg`），而 TypedDict 在类型上**不是**
    `dict[str, Any]` 的子类型（mypy 的可变容器不变性），却可以安全地赋给
    `Mapping[str, Any]`。这里只是读一条消息、不做写入，`Mapping` 的宽容度与本模块
    自己的 `DeliverFn`、`signaling.deliver` 完全一致；运行时对象仍是普通 dict。
    """

    async def send(self, device_id: str, message: Mapping[str, Any]) -> None:
        """把一条 C2 消息下发到设备；设备不在线请抛 `DeviceOfflineError`。"""
        ...


#: 等待 answer：`(秒) -> 是否收到 answer`。超时返回 False，明确拒接抛异常。
WaitAnswer = Callable[[float], Awaitable[bool]]
#: 播报：`(device_id, text) -> None`。
SpeakText = Callable[[str, str], Awaitable[None]]
#: 降级播报（WS 文本）：`(device_id, text) -> None`。
FallbackSendText = Callable[[str, str], Awaitable[None]]
#: `signaling.deliver` 的形状：同步、返回是否投递成功。
DeliverFn = Callable[[str, Mapping[str, Any]], bool]


# ------------------------------------------------------------------ C2 信封（复用闭集）


def build_offer_message(sdp: str) -> OfferMsg:
    """C2 `offer` 信封（服务端主动下发版）。

    字段与 `signaling.OfferMsg` **完全一致**：方向反转不改形状，所以闭集没有被扩
    （`tests/test_outbound.py` 对这一点有硬断言）。
    """
    return {"type": "offer", "sdp": sdp}


def build_degrade_text_message(text: str) -> TtsTextMsg:
    """C2 `tts_text` 信封（降级播报）。

    `degraded` 恒显式 `True`：这一条就是「播报走不下去了，只能给你看字」的那个分支，
    与问候的 `degraded=False` 必须能区分（FR-DLG-05）。
    """
    return {"type": "tts_text", "text": text, "degraded": True}


class SignalingSender:
    """把 A2 的 `signaling.deliver`（同步、返回 bool）适配成 `Sender`（async、失败抛异常）。

    `deliver` 可替换，默认**惰性**取 `app.audio.signaling.deliver`：本模块导入期不拉进
    FastAPI / WebSocket / 数据库依赖，scheduler 引它更轻；测试也不必起 WS 连接。
    """

    def __init__(self, deliver: DeliverFn | None = None) -> None:
        self._deliver = deliver

    async def send(self, device_id: str, message: Mapping[str, Any]) -> None:
        deliver = self._deliver
        if deliver is None:
            from app.audio import signaling

            deliver = signaling.deliver
        if not deliver(device_id, message):
            raise DeviceOfflineError(f"device {device_id} has no authed ws connection")


# ------------------------------------------------------------------ 主流程


async def call_and_speak(
    *,
    device_id: str,
    text: str,
    offerer: Offerer,
    sender: Sender,
    timeout_seconds: float = OUTBOUND_TIMEOUT_SECONDS,
    metrics_enabled: bool | None = None,
    wait_answer: WaitAnswer | None = None,
    speak: SpeakText | None = None,
    fallback_send_text: FallbackSendText | None = None,
) -> OutboundResult:
    """发起一次反向外呼并播报。**任何失败都已完成降级，本函数不向上抛异常。**

    参数：
        device_id: 目标 pad 的设备 ID（`medication_attempts` 的触达对象）。
        text: 要播报的正文（用药提醒话术等）。日志只记长度，不记内容。
        offerer: 出 offer 的一侧，另可提供 `wait_answer(seconds)`。
        sender: C2 信封投递方，设备不在线抛 `DeviceOfflineError`。
        timeout_seconds: 建连预算（秒）。建连与播报**各给一份**预算，不共享剩余额度：
            建连已经吃掉的时间不该让播报必然超时；两段相加才是 `elapsed_ms`。
        metrics_enabled: `False` = 本次不打点；`None`/`True` = 交给
            `app/core/metrics.py` 的全局开关裁决（本项目默认开）。
        wait_answer: 等待 answer 的协程，优先于 `offerer.wait_answer`。
        speak: 播报回调；生产必须注入（`app/audio/outbound_speak.build_outbound_speaker`）。
            不注入按 `rtc_failed` 降级：只建连不播报却回 `ok`，会让回执写
            `tts_finished`——长者根本没听到声音。
        fallback_send_text: 降级播报回调；不注入时退回 `sender.send(tts_text)`。

    返回：
        `OutboundResult`。`rtc_result` 落在 `RTC_RESULTS` 内，交调用方落库。

    异常口径：
        - 注入实现的失败全部转成 `OutboundResult`（含未预期异常，只记一条 warning）：
          定时任务不能被一次外呼打挂；
        - 外层取消（`CancelledError`）原样向上抛：`asyncio.timeout` 只把**本函数自己的**
          超时转成 `timeout`，调度器关停时的取消语义不能被吞掉。
    """
    started = time.perf_counter()
    budget = max(float(timeout_seconds), 0.0)

    if budget <= 0.0:
        # 预算非正 = 立刻降级（同 `app/core/timeouts.py` 的非正预算口径），不发 offer：
        # 一条注定没人认领的外呼会白白把 pad 的 PeerConnection 建起来再扔掉。
        rtc_result: str = RTC_TIMEOUT
        reason: str | None = "外呼预算为 0，未发起建连"
    else:
        rtc_result, reason = await _establish(
            device_id=device_id,
            offerer=offerer,
            sender=sender,
            budget=budget,
            wait_answer=wait_answer,
        )
        if rtc_result == RTC_OK:
            rtc_result, reason = await _announce(
                device_id=device_id,
                text=text,
                speak=speak,
                budget=budget,
            )

    # 降级是否真的送达：`_degrade_to_text` 返回 None 才是发出去且 `deliver` 成功
    # （返回非 None 是失败说明）。这一位要一路带到 `OutboundResult`，因为
    # 「建连失败但卡片到了」与「两路都没到」在用药回执里是两种落库结果。
    degraded_text_sent = False
    if rtc_result != RTC_OK:
        note = await _degrade_to_text(
            device_id=device_id,
            text=text,
            sender=sender,
            fallback_send_text=fallback_send_text,
        )
        if note is None:
            degraded_text_sent = True
        else:
            reason = f"{reason}；{note}" if reason else note

    # 计时用 perf_counter：Windows 上 monotonic 只有 ~15.6ms 粒度，
    # 会把「毫秒级建连」记成 0 或 16（迭代 5 E1 实测踩过，见 core/timeouts.py）。
    # round() 对 float 返回 int，故 elapsed_ms 天然是整数毫秒。
    elapsed_ms = round((time.perf_counter() - started) * 1000.0)

    if metrics_enabled is not False:
        counter(METRIC_CALLS, result=rtc_result)
        observe_ms(METRIC_ELAPSED, float(elapsed_ms))

    extra = {
        "trace_id": _trace_id(),
        "device_id": device_id,
        "rtc_result": rtc_result,
        "degraded": rtc_result != RTC_OK,
        "degraded_text_sent": degraded_text_sent,
        "elapsed_ms": elapsed_ms,
        "text_len": len(text),
        "reason": reason,
    }
    if rtc_result == RTC_OK:
        logger.info("outbound call ok", extra=extra)
    else:
        logger.warning("outbound call degraded", extra=extra)

    return OutboundResult(
        ok=rtc_result == RTC_OK,
        rtc_result=rtc_result,
        degraded=rtc_result != RTC_OK,
        reason=reason,
        elapsed_ms=elapsed_ms,
        degraded_text_sent=degraded_text_sent,
    )


# ------------------------------------------------------------------ 内部阶段


async def _abandon_offer(offerer: Offerer, *, device_id: str) -> None:
    """下发失败时让 offerer 收掉会话与等待位。

    `abandon` 是**可选**方法（`Offerer` 只约定 `create_offer`）：测试里的假 offerer
    可以没有它，生产实现（`SignalingOfferer`）有。清场失败不改判结果 —— 真正的
    失败原因（离线/超时）不能被清场异常盖住，所以这里只记日志、别的不抛。
    """
    abandon = getattr(offerer, "abandon", None)
    if not callable(abandon):
        return
    try:
        await abandon()
    except Exception as exc:  # 清场失败只记一条，不掩盖真正的失败原因
        logger.warning(
            "outbound abandon failed",
            extra={
                "trace_id": _trace_id(),
                "device_id": device_id,
                "error_type": type(exc).__name__,
            },
        )


async def _establish(
    *,
    device_id: str,
    offerer: Offerer,
    sender: Sender,
    budget: float,
    wait_answer: WaitAnswer | None,
) -> tuple[str, str | None]:
    """建连阶段：createOffer → 下发 C2 `offer` → 等 `answer`。返回 `(rtc_result, reason)`。

    整段共用一个预算：三段里任何一段慢掉，老人都只关心「10 秒内有没有人说话」。

    下发只有这一处（缺陷 A 的单出口）：`offerer.create_offer()` 只建会话与等待位，
    `sender.send(...)` 是唯一把 `offer` 送到设备的地方。发不出去时立刻 `abandon`
    那个会话 —— 所以「设备离线」仍在毫秒级返回 `no_answer`，不会退化成干等满 10s。
    """
    try:
        async with asyncio.timeout(budget):
            sdp = await offerer.create_offer()
            try:
                await sender.send(device_id, build_offer_message(sdp))
            except (Exception, asyncio.CancelledError):
                # 没发出去：收掉 offerer 刚建的会话与等待位，别留下悬空会话。
                await _abandon_offer(offerer, device_id=device_id)
                raise
            waiter = (
                wait_answer if wait_answer is not None else getattr(offerer, "wait_answer", None)
            )
            if not callable(waiter):
                raise _MissingWaitAnswer
            if not await waiter(budget):
                # 窗口内没有 answer：与「明确拒绝」分开统计（见模块 docstring）。
                return RTC_TIMEOUT, f"{budget:g}s 内没有等到 answer"
    except TimeoutError:
        return RTC_TIMEOUT, f"建连超过 {budget:g}s（createOffer / 下发 / 等 answer 合计）"
    except DeviceOfflineError:
        return RTC_NO_ANSWER, "设备当前没有在线连接"
    except AnswerRejectedError:
        return RTC_NO_ANSWER, "对端明确拒绝接听"
    except IceFailedError:
        return RTC_ICE_FAILED, "ICE 连通失败"
    except _MissingWaitAnswer:
        return RTC_FAILED, "offerer 与调用方都没有提供 wait_answer，无法确认接听"
    except Exception as exc:  # 未预期异常也只降级：定时任务不能被一次外呼打挂
        logger.warning(
            "outbound establish failed",
            extra={
                "trace_id": _trace_id(),
                "device_id": device_id,
                "error_type": type(exc).__name__,
            },
        )
        return RTC_FAILED, f"外呼链路异常：{type(exc).__name__}"
    return RTC_OK, None


async def _announce(
    *,
    device_id: str,
    text: str,
    speak: SpeakText | None,
    budget: float,
) -> tuple[str, str | None]:
    """播报阶段。返回 `(rtc_result, reason)`。

    单独再给一份预算（不扣建连已用的时间）：播报慢的典型原因是 TTS 首包，而它有自己的
    三档超时（`app/core/timeouts.py`）；这里只兜「播报调用根本不返回」这一种最坏情况。
    """
    if speak is None:
        logger.warning(
            "outbound speak callback missing",
            extra={"trace_id": _trace_id(), "device_id": device_id, "text_len": len(text)},
        )
        return RTC_FAILED, "未注入播报回调（装配错误，只建连没说话）"
    try:
        async with asyncio.timeout(budget):
            await speak(device_id, text)
    except TimeoutError:
        return RTC_TIMEOUT, f"播报超过 {budget:g}s"
    except IceFailedError:
        return RTC_ICE_FAILED, "播报阶段 ICE 断开"
    except Exception as exc:
        logger.warning(
            "outbound announce failed",
            extra={
                "trace_id": _trace_id(),
                "device_id": device_id,
                "error_type": type(exc).__name__,
            },
        )
        return RTC_FAILED, f"播报失败：{type(exc).__name__}"
    return RTC_OK, None


async def _degrade_to_text(
    *,
    device_id: str,
    text: str,
    sender: Sender,
    fallback_send_text: FallbackSendText | None,
) -> str | None:
    """降级播报（WS 文本）。返回「连降级也没成」的补充说明，成功则 `None`。

    不向上抛：调用方要的是「这一次触达结果如何」，不是第二条异常路径。
    """
    try:
        if fallback_send_text is not None:
            await fallback_send_text(device_id, text)
        else:
            await sender.send(device_id, build_degrade_text_message(text))
    except Exception as exc:
        logger.warning(
            "outbound degraded text failed",
            extra={
                "trace_id": _trace_id(),
                "device_id": device_id,
                "text_len": len(text),
                "error_type": type(exc).__name__,
            },
        )
        return f"降级播报也失败（{type(exc).__name__}）"
    return None


def _trace_id() -> str:
    """当前链路 trace_id（AGENTS §8：每一行日志都带 trace_id）；无上下文时记 `-`。"""
    return get_trace_id() or "-"


__all__ = [
    "DEGRADE_MESSAGE_TYPE",
    "METRIC_CALLS",
    "METRIC_ELAPSED",
    "OFFER_MESSAGE_TYPE",
    "OUTBOUND_TIMEOUT_SECONDS",
    "RTC_FAILED",
    "RTC_ICE_FAILED",
    "RTC_NO_ANSWER",
    "RTC_OK",
    "RTC_RESULTS",
    "RTC_TIMEOUT",
    "AnswerRejectedError",
    "DeliverFn",
    "DeviceOfflineError",
    "FallbackSendText",
    "IceFailedError",
    "Offerer",
    "OutboundError",
    "OutboundResult",
    "Sender",
    "SignalingSender",
    "SpeakText",
    "WaitAnswer",
    "build_degrade_text_message",
    "build_offer_message",
    "call_and_speak",
]
