"""N62 · 反向外呼的信令接线（迭代 7 · S-D7-3）验收用例。

验收边界（lead 裁决二）：**pad 的信令 WS 已建立并已 auth**。
「长者睡着、pad 未连 WS 时也能被叫醒」是启动时序问题，属 N62b，本文件不覆盖。

## N62f 之后的投递契约（本文件的新前提）

`SignalingOfferer.create_offer()` **只建会话与等待位、不下发任何帧**；`offer` 信封由
调用方（`outbound._establish`）经 `Sender` 下发一次 —— 这是「一次外呼只允许一条
`type=offer` 帧」的唯一实现方式（此前 offerer 内部也 `_deliver`，pad 会收到两条 offer、
建两个 `RTCPeerConnection`、回两个 answer，第二个 answer 触发 `MediaFormatError`）。
下发失败/设备离线时由调用方调 `offerer.abandon()` 清场。

## 每条用例钉住哪个断点（回退实现时会红在哪）

1. `app/audio/signaling.py` 入站白名单元组里的 ``"answer"``
   → `test_inbound_answer_reaches_media_handler`：删掉它，answer 走进
   ``ws_bad_message reason=unknown_type:answer``，媒体层根本收不到。
2. `app/audio/webrtc.py::_handle_answer` 里的 ``await session.accept_answer(sdp)``
   → `test_answer_advances_session_from_open_to_connecting`：删掉它，会话停在
   ``MediaState.OPEN``（只发过本地 offer，远端 SDP 从未接手）。
3. 同函数里的 ``_resolve_answer_waiter(device_id, True)``
   → 同一条用例的后半段：删掉它，等在手边的外呼超时被判 no_answer。
4. N62f 的单出口：把 `create_offer` 里的 ``_deliver`` 加回去 → 第 2/3 条用例的
   ``_drain(conn) == []`` 立刻红；把 `_establish` 的那次下发删掉或发两次 →
   `test_one_outbound_sends_exactly_one_offer_frame` 红。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from starlette.websockets import WebSocketDisconnect

from app.audio import outbound, signaling, webrtc
from app.audio.webrtc import MediaSession, MediaState

DEVICE_ID = "dev-outbound-1"


def _config() -> webrtc.MediaConfig:
    return webrtc.MediaConfig(
        stun_url="",
        turn_url="",
        turn_username="",
        turn_credential="",
        turn_credential_ttl_seconds=0,
        ice_timeout_seconds=5,
        audio_bitrate_kbps=32,
        vad_aggressiveness=2,
    )


class _FakeWebSocket:
    """只实现 `_read_loop` 走到的那几个方法：读文本帧 / 直发 / 关闭。"""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def receive_text(self) -> str:
        if not self._frames:
            raise WebSocketDisconnect(code=1000)
        return self._frames.pop(0)

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def _online_conn(frames: list[str] | None = None) -> signaling.ConnectionContext:
    """把一条已鉴权连接放进 A2 注册表：`signaling.deliver` 才投得出去。"""
    ws = _FakeWebSocket(frames or [])
    conn = signaling.ConnectionContext(
        trace_id="trace-outbound",
        websocket=cast(Any, ws),
        device_id=DEVICE_ID,
        authed=True,
    )
    signaling._registry[DEVICE_ID] = conn
    return conn


def _cleanup() -> None:
    signaling._registry.pop(DEVICE_ID, None)
    signaling.set_media_handler(None)


def _drain(conn: signaling.ConnectionContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not conn.downstream.empty():
        out.append(conn.downstream.get_nowait())
    return out


@pytest.mark.asyncio
async def test_inbound_answer_reaches_media_handler() -> None:
    """入站白名单必须把 `answer` 交给媒体层（断点一）。

    用 `_read_loop` 跑真实分发路径，而不是直接调 `handle_signal_message` —— 后者
    绕过了 `signaling.py` 那行元组，测了也证明不了白名单改对了。
    """
    frames = [
        json.dumps({"type": "answer", "sdp": "v=0\r\n"}),
        # 第二帧 auth 触发 `_reject` 的 return，让 `_read_loop` 正常收尾（不靠抛异常）
        json.dumps({"type": "auth", "token": "x"}),
    ]
    conn = _online_conn(frames)
    seen: list[dict[str, Any]] = []

    async def _handler(_conn: signaling.ConnectionContext, message: dict[str, Any]) -> None:
        seen.append(dict(message))

    signaling.set_media_handler(_handler)
    try:
        await signaling._read_loop(conn)
    finally:
        _cleanup()

    assert [m["type"] for m in seen] == ["answer"], "answer 没有被转发给媒体层"
    assert seen[0]["sdp"] == "v=0\r\n"


@pytest.mark.asyncio
async def test_answer_advances_session_from_open_to_connecting() -> None:
    """服务端 offer → pad answer → CONNECTING，并唤醒等在手边的外呼（断点二、三）。

    状态断言是这条用例的主证据：`accept_answer` 那行没了，会话会停在 OPEN，
    而「没抛异常」在两种情况下长得一模一样 —— 所以必须先断言 OPEN、再断言 CONNECTING。
    """
    conn = _online_conn()
    registry = webrtc._signaling_registry()
    answerer = MediaSession("pad-answerer", "trace-pad", _config())
    answerer.create_peer_connection()
    offerer = webrtc.SignalingOfferer(DEVICE_ID, trace_id="trace-outbound")

    try:
        offer_sdp = await offerer.create_offer()
        # N62f 单出口：offerer 自己一帧都不发 —— 发了就是第二条 offer。
        assert _drain(conn) == [], "create_offer 不该自己下发任何帧"

        # 下发归调用方：这里直接用生产实现（`SignalingSender` + `build_offer_message`），
        # 等价于 `_establish` 里那唯一一次 `sender.send(...)`。
        await outbound.SignalingSender().send(DEVICE_ID, outbound.build_offer_message(offer_sdp))
        delivered = _drain(conn)
        assert [m["type"] for m in delivered] == ["offer"], "offer 没有下发到 pad"
        assert delivered[0]["sdp"] == offer_sdp

        session = registry.get_session(DEVICE_ID)
        assert session is not None
        # 只有本地 offer、远端还没答：不许提前跳 CONNECTING（媒体层已有六例钉这个口径）
        assert session.state is MediaState.OPEN

        origin = await answerer.handle_offer(offer_sdp)
        await webrtc.handle_signal_message(conn, {"type": "answer", "sdp": origin})

        # ↓ 删掉 `_handle_answer` 里的 accept_answer 那行，这里会红
        assert session.state is MediaState.CONNECTING
        # ↓ 删掉 `_resolve_answer_waiter` 那行，这里会等到超时后返回 False
        assert await offerer.wait_answer(0.2) is True
    finally:
        await registry.close_session(DEVICE_ID, reason="test_done")
        await answerer.close()
        _cleanup()


@pytest.mark.asyncio
async def test_wait_answer_times_out_and_leaves_session_open() -> None:
    """pad 不应答：外呼必须自己超时收口，且会话没有被"顺手"推进。

    这条是上一条的对照组 —— 没有它，「CONNECTING」可能来自别的地方而不是那行。
    """
    conn = _online_conn()
    registry = webrtc._signaling_registry()
    offerer = webrtc.SignalingOfferer(DEVICE_ID, trace_id="trace-outbound")

    try:
        await offerer.create_offer()
        # N62f 的关键断言：建 offer 不等于发 offer —— 这里必须是空的。
        assert _drain(conn) == [], "create_offer 不该自己下发任何帧"

        assert await offerer.wait_answer(0.05) is False
        session = registry.get_session(DEVICE_ID)
        assert session is not None
        assert session.state is MediaState.OPEN
    finally:
        await registry.close_session(DEVICE_ID, reason="test_done")
        _cleanup()


@pytest.mark.asyncio
async def test_offer_to_offline_device_degrades_to_no_answer() -> None:
    """pad 的信令 WS 不在线（N62b 的边界场景）：立刻给 no_answer，不留悬空会话。

    这条不覆盖「睡着也能被叫醒」（属 N62b），只钉住「离线时别把外呼挂住」：
    没有它，设备离线会让 `call_and_speak` 白等满 `OUTBOUND_TIMEOUT_SECONDS`。
    """
    registry = webrtc._signaling_registry()  # 故意不注册连接 = 设备不在线
    offerer = webrtc.SignalingOfferer(DEVICE_ID, trace_id="trace-offline")
    sender = outbound.SignalingSender()

    try:
        offer_sdp = await offerer.create_offer()
        # N62f：投得出去与否不再由 create_offer 判断（它看不到线路），会话先留在表里。
        assert registry.get_session(DEVICE_ID) is not None

        # 下发这一步失败，且异常类型必须能区分「设备离线」与「对端超时」。
        with pytest.raises(outbound.DeviceOfflineError):
            await sender.send(DEVICE_ID, outbound.build_offer_message(offer_sdp))

        # 调用方清场：会话关掉、等待位以 False 收口，不留悬空会话。
        await offerer.abandon()
        assert registry.get_session(DEVICE_ID) is None
        assert await offerer.wait_answer(0.05) is False

        # 端到端再确认一次「离线 ≠ 超时」：同一 device_id 走完整外呼，结果必须是
        # no_answer 且远早于超时预算，而不是白等满 10s 的 timeout。
        result = await outbound.call_and_speak(
            device_id=DEVICE_ID,
            text="该吃药了",
            offerer=webrtc.SignalingOfferer(DEVICE_ID, trace_id="trace-offline"),
            sender=outbound.SignalingSender(),
        )
        assert result.rtc_result == outbound.RTC_NO_ANSWER
        assert result.rtc_result != outbound.RTC_TIMEOUT
        assert result.degraded is True
        assert result.elapsed_ms < outbound.OUTBOUND_TIMEOUT_SECONDS * 1000
    finally:
        await registry.close_session(DEVICE_ID, reason="test_done")
        _cleanup()


@pytest.mark.asyncio
async def test_answer_without_session_is_rejected_with_error() -> None:
    """没有 offer 的 answer 必须显式回 error（不许被当成未知类型静默吞掉）。"""
    conn = _online_conn()

    try:
        await webrtc.handle_signal_message(conn, {"type": "answer", "sdp": "v=0\r\n"})
        delivered = _drain(conn)
    finally:
        _cleanup()

    assert [m["type"] for m in delivered] == ["error"]
    assert delivered[0]["code"] == "internal"


class _FrameCountingSender:
    """只数帧的 Sender：记录每一条下发的 C2 信封，不碰真实线路。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, device_id: str, message: Mapping[str, Any]) -> None:
        self.frames.append({"device_id": device_id, **cast("dict[str, Any]", message)})


@pytest.mark.asyncio
async def test_one_outbound_emits_exactly_one_offer_frame() -> None:
    """N62f 的核心不变量：一次外呼**恰好**一条 `type=offer` 帧。

    单出口在 `outbound._establish`（offerer 只建不发）。这条用例跑完整一次
    `call_and_speak` —— 生产 `SignalingOfferer` + 计帧假 Sender + 直接判答的
    wait_answer —— 数的是帧数，不是「有没有」：删掉那次下发会变 0 条、加回
    offerer 内部投递会变 2 条，两种都红。没有它，谁把 `_deliver` 加回去都全绿。
    """
    registry = webrtc._signaling_registry()
    sender = _FrameCountingSender()
    spoken: list[tuple[str, str]] = []

    async def _speak(device_id: str, text: str) -> None:
        spoken.append((device_id, text))

    async def _assume_answered(_seconds: float) -> bool:
        return True

    try:
        result = await outbound.call_and_speak(
            device_id=DEVICE_ID,
            text="该吃药了",
            offerer=webrtc.SignalingOfferer(DEVICE_ID, trace_id="trace-single-exit"),
            sender=sender,
            wait_answer=_assume_answered,
            speak=_speak,
        )
    finally:
        await registry.close_session(DEVICE_ID, reason="test_done")
        _cleanup()

    offer_frames = [frame for frame in sender.frames if frame.get("type") == "offer"]
    assert len(offer_frames) == 1, f"一次外呼必须恰好一条 offer 帧，实际 {len(offer_frames)} 条"
    assert sender.frames == offer_frames, "外呼只应有 offer 帧，不该夹带别的信封"
    assert isinstance(offer_frames[0]["sdp"], str)
    assert offer_frames[0]["sdp"].startswith("v=0")
    assert result.ok is True
    assert result.rtc_result == outbound.RTC_OK
    assert spoken == [(DEVICE_ID, "该吃药了")]
