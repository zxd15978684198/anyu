"""`app/audio/outbound.py` 的行为与契约测试（迭代 7 W4 · FR-MED-06 / AC-41）。

覆盖六条验收：

① 成功路径 `ok=True` / `rtc_result='ok'`，且下发的就是既有 C2 `offer`（闭集未扩）；
② ICE 失败 → `ice_failed` + 降级 + `fallback_send_text` 被调用；
③ 超时（10ms 预算 + 200ms 慢 answer）→ `timeout` + 降级；
④ `no_answer`（设备不在线 / 对端明确拒接）；
⑤ `elapsed_ms` 与打点（`metrics_snapshot()` 里有 `outbound_*` 键）；
⑥ 不存在任何新增 C2 消息类型（对 `signaling.__all__` 声明的类型字面量做快照断言）。

**真实链路一条都不碰**：假 offerer / 假 sender 替代 aiortc 与 WebSocket，
超时走真实事件循环（阈值与慢协程差 20 倍，调度抖动不影响判定）。
`app/core/metrics.py` 是进程级全局 dict，故有 autouse 夹具在每用例前后各 `reset_metrics()`
一次（同 `test_timeouts.py`），否则前一个用例打的点会漏进后一个用例。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from typing import Any, Literal, get_args, get_origin, get_type_hints

import pytest

from app.audio import outbound, signaling
from app.core import metrics as metrics_core

DEVICE_ID = "device-outbound-0001"
REMINDER_TEXT = "奶奶，该吃药啦，吃完了跟我说一声。"
OFFER_SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

#: 超时用例的预算（秒）：比慢协程小一个数量级，必然先超时。
SHORT_BUDGET_S = 0.01
#: 慢 answer 的时长（秒）。
SLOW_ANSWER_S = 0.2

#: C2 消息类型闭集快照（迭代 4 冻结）。**本卡不许扩**：加一个值这里就红。
EXPECTED_MESSAGE_TYPES = frozenset(
    {
        "answer",
        "asr_end",
        "asr_final",
        "asr_partial",
        "asr_start",
        "auth",
        "auth_fail",
        "auth_ok",
        "client_diag",
        "error",
        "ice",
        "identity_ask",
        "identity_confirm",
        "identity_result",
        "offer",
        "ping",
        "pong",
        "tts_text",
    }
)


@pytest.fixture(autouse=True)
def fresh_metrics() -> Iterator[None]:
    """隔离进程级计数器：每用例从空快照开始、结束后也清掉。"""
    metrics_core.reset_metrics()
    yield
    metrics_core.reset_metrics()


# ---------------------------------------------------------------- 假实现


class FakeOfferer:
    """假 offerer：`create_offer` 返回固定 SDP，`wait_answer` 按预设行为作答。

    `abandon` 是可选清场方法（生产实现 `SignalingOfferer` 有）：下发失败时
    `outbound` 会调它收掉会话与等待位 —— 缺陷 A 的单出口配套动作。
    """

    def __init__(self, *, answer: bool = True) -> None:
        self.answer = answer
        self.offer_error: BaseException | None = None
        self.answer_error: BaseException | None = None
        self.abandon_error: BaseException | None = None
        self.answer_delay_s = 0.0
        self.offer_calls = 0
        self.abandon_calls = 0
        self.budgets: list[float] = []

    async def create_offer(self) -> str:
        self.offer_calls += 1
        if self.offer_error is not None:
            raise self.offer_error
        return OFFER_SDP

    async def wait_answer(self, timeout_seconds: float) -> bool:
        self.budgets.append(timeout_seconds)
        if self.answer_delay_s > 0:
            await asyncio.sleep(self.answer_delay_s)
        if self.answer_error is not None:
            raise self.answer_error
        return self.answer

    async def abandon(self) -> None:
        self.abandon_calls += 1
        if self.abandon_error is not None:
            raise self.abandon_error


class BareOfferer:
    """只有 `create_offer`、没有 `wait_answer` 的最小 offerer（等价于未提供等待能力）。"""

    async def create_offer(self) -> str:
        return OFFER_SDP


class FakeSender:
    """假 sender：记录下发的每一条 C2 消息；`offline=True` 时抛 `DeviceOfflineError`。"""

    def __init__(self, *, offline: bool = False) -> None:
        self.offline = offline
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, device_id: str, message: dict[str, Any]) -> None:
        if self.offline:
            raise outbound.DeviceOfflineError(device_id)
        self.sent.append((device_id, message))


class Recorder:
    """可调用记录器：兼作 `speak` / `fallback_send_text`，可选注入异常。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error = error

    async def __call__(self, device_id: str, text: str) -> None:
        self.calls.append((device_id, text))
        if self.error is not None:
            raise self.error


async def _call(**overrides: Any) -> outbound.OutboundResult:
    """按默认的成功配置跑一次外呼；`overrides` 覆盖同名关键字参数。"""
    kwargs: dict[str, Any] = {
        "device_id": DEVICE_ID,
        "text": REMINDER_TEXT,
        "offerer": FakeOfferer(),
        "sender": FakeSender(),
        "speak": Recorder(),
        "fallback_send_text": Recorder(),
    }
    kwargs.update(overrides)
    return await outbound.call_and_speak(**kwargs)


def _declared_message_types() -> frozenset[str]:
    """`signaling.__all__` 里声明的 C2 消息类型（各 TypedDict 的 `type` 字面量）。

    `signaling.py` 用了 `from __future__ import annotations`，`__annotations__` 里是字符串，
    必须走 `get_type_hints` 解析后才能拿到 `Literal[...]` 本体。
    """
    found: set[str] = set()
    for name in signaling.__all__:
        obj = getattr(signaling, name, None)
        if not isinstance(obj, type):
            continue
        literal = get_type_hints(obj).get("type")
        if get_origin(literal) is Literal:
            found.update(arg for arg in get_args(literal) if isinstance(arg, str))
    return frozenset(found)


# ---------------------------------------------------------------- ① 成功路径


async def test_success_returns_ok_and_rtc_ok() -> None:
    speak = Recorder()

    result = await _call(speak=speak)

    assert result.ok is True
    assert result.rtc_result == outbound.RTC_OK
    assert result.degraded is False
    assert result.reason is None
    assert result.elapsed_ms >= 0
    assert isinstance(result.elapsed_ms, int)
    assert speak.calls == [(DEVICE_ID, REMINDER_TEXT)]


async def test_success_sends_existing_offer_envelope() -> None:
    sender = FakeSender()

    result = await _call(sender=sender)

    assert result.ok is True
    assert len(sender.sent) == 1
    device_id, message = sender.sent[0]
    assert device_id == DEVICE_ID
    # 复用既有 C2 `offer`：两字段、一个不多一个不少（闭集未扩）
    assert message == {"type": "offer", "sdp": OFFER_SDP}
    assert set(message) == set(get_type_hints(signaling.OfferMsg)) == {"type", "sdp"}


async def test_one_call_emits_exactly_one_offer_frame() -> None:
    """缺陷 A 的回归钉子：一枪只允许一条 `type=offer` 帧（数帧数，不是看有没有）。

    2026-11-10 之前 `SignalingOfferer.create_offer()` 内部也 `_deliver` 一次，加上
    这里的 `sender.send(...)`，设备收到两条 offer → 建两个 `RTCPeerConnection` →
    回两个 answer → 第二个 answer 落在已 CONNECTING 的会话上抛 `MediaFormatError`
    → 会话被关 → 播报以 `MediaClosedError` 失败，迭代 7 外呼因此从未跑通。
    """
    sender = FakeSender()

    result = await _call(sender=sender)

    assert result.rtc_result == outbound.RTC_OK
    offer_frames = [message for _, message in sender.sent if message.get("type") == "offer"]
    assert len(offer_frames) == 1
    assert len(sender.sent) == 1


async def test_success_keeps_offerer_session() -> None:
    """成功路径不许清场：会话还等着收 answer。"""
    offerer = FakeOfferer()

    result = await _call(offerer=offerer)

    assert result.rtc_result == outbound.RTC_OK
    assert offerer.abandon_calls == 0


async def test_success_passes_default_budget_to_waiter() -> None:
    offerer = FakeOfferer()

    result = await _call(offerer=offerer)

    assert result.ok is True
    assert offerer.offer_calls == 1
    assert offerer.budgets == [outbound.OUTBOUND_TIMEOUT_SECONDS]


async def test_explicit_wait_answer_overrides_offerer() -> None:
    offerer = FakeOfferer(answer=False)  # offerer 自己会答「没等到」，但这个用例不走它
    budgets: list[float] = []

    async def wait_answer(timeout_seconds: float) -> bool:
        budgets.append(timeout_seconds)
        return True

    result = await _call(offerer=offerer, wait_answer=wait_answer)

    assert result.rtc_result == outbound.RTC_OK
    assert budgets == [outbound.OUTBOUND_TIMEOUT_SECONDS]
    assert offerer.budgets == []


# ---------------------------------------------------------------- ② ICE 失败


async def test_ice_failure_degrades_to_text() -> None:
    offerer = FakeOfferer()
    offerer.answer_error = outbound.IceFailedError("ice connectivity checks failed")
    speak = Recorder()
    fallback = Recorder()

    result = await _call(offerer=offerer, speak=speak, fallback_send_text=fallback)

    assert result.ok is False
    assert result.rtc_result == outbound.RTC_ICE_FAILED
    assert result.degraded is True
    assert result.reason is not None and "ICE" in result.reason
    # 建连没过就不该播报，但文字必须发出去（老人听不到比看不到更糟）
    assert speak.calls == []
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


# ---------------------------------------------------------------- ③ 超时


async def test_timeout_degrades_to_text() -> None:
    offerer = FakeOfferer()
    offerer.answer_delay_s = SLOW_ANSWER_S
    fallback = Recorder()

    result = await _call(
        offerer=offerer,
        timeout_seconds=SHORT_BUDGET_S,
        fallback_send_text=fallback,
    )

    assert result.ok is False
    assert result.rtc_result == outbound.RTC_TIMEOUT
    assert result.degraded is True
    assert result.reason is not None and f"{SHORT_BUDGET_S:g}s" in result.reason
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


async def test_non_positive_budget_degrades_without_offer() -> None:
    offerer = FakeOfferer()
    fallback = Recorder()

    result = await _call(offerer=offerer, timeout_seconds=0, fallback_send_text=fallback)

    assert result.rtc_result == outbound.RTC_TIMEOUT
    assert result.degraded is True
    # 预算非正就不发 offer：不建一条注定没人认领的 PeerConnection
    assert offerer.offer_calls == 0
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


# ---------------------------------------------------------------- ④ 对方未接


async def test_no_answer_when_device_offline() -> None:
    fallback = Recorder()

    result = await _call(sender=FakeSender(offline=True), fallback_send_text=fallback)

    assert result.ok is False
    assert result.rtc_result == outbound.RTC_NO_ANSWER
    assert result.degraded is True
    assert result.reason is not None and "在线" in result.reason
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


async def test_offer_not_delivered_abandons_session_and_reports_no_answer() -> None:
    """设备离线：立刻 `no_answer` + 让 offerer 清场（不留悬空会话），不许干等满 10s。"""
    offerer = FakeOfferer()

    result = await _call(offerer=offerer, sender=FakeSender(offline=True))

    assert result.rtc_result == outbound.RTC_NO_ANSWER
    assert result.degraded is True
    assert offerer.abandon_calls == 1
    # 「窗口内立刻」是硬要求：离线判定不能退化成等满预算
    assert result.elapsed_ms < outbound.OUTBOUND_TIMEOUT_SECONDS * 1000


async def test_abandon_failure_does_not_mask_offline() -> None:
    """清场自己失败时，结果仍按「设备离线」判，不能被清场异常改写成 rtc_failed。"""
    offerer = FakeOfferer()
    offerer.abandon_error = RuntimeError("close_session exploded")

    result = await _call(offerer=offerer, sender=FakeSender(offline=True))

    assert result.rtc_result == outbound.RTC_NO_ANSWER
    assert offerer.abandon_calls == 1


async def test_no_answer_when_peer_rejects() -> None:
    offerer = FakeOfferer()
    offerer.answer_error = outbound.AnswerRejectedError("elder pressed decline")

    result = await _call(offerer=offerer)

    assert result.rtc_result == outbound.RTC_NO_ANSWER
    assert result.degraded is True
    assert result.reason is not None and "拒绝" in result.reason


# ---------------------------------------------------------------- 其他失败分类


async def test_create_offer_failure_degrades_without_sending_offer() -> None:
    offerer = FakeOfferer()
    offerer.offer_error = RuntimeError("peer connection exploded")
    sender = FakeSender()
    fallback = Recorder()

    result = await _call(offerer=offerer, sender=sender, fallback_send_text=fallback)

    assert result.rtc_result == outbound.RTC_FAILED
    assert result.degraded is True
    assert sender.sent == []  # 连 offer 都没出去
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


async def test_speak_failure_degrades() -> None:
    speak = Recorder(error=RuntimeError("tts unavailable"))
    fallback = Recorder()

    result = await _call(speak=speak, fallback_send_text=fallback)

    assert result.rtc_result == outbound.RTC_FAILED
    assert result.degraded is True
    assert speak.calls == [(DEVICE_ID, REMINDER_TEXT)]
    assert result.reason is not None and "播报失败" in result.reason
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


async def test_missing_speak_is_failure_not_silent_success() -> None:
    """没注入 `speak` = 只建连没说话，必须报失败并降级（2026-11-17 的谎报回执）。

    此前返回 `RTC_OK`，投递层因此写 `receipt=tts_finished`：横幅亮了、库里记着
    「语音说完了」，长者耳朵里一个字都没有。
    """
    fallback = Recorder()

    result = await _call(speak=None, fallback_send_text=fallback)

    assert result.ok is False
    assert result.rtc_result == outbound.RTC_FAILED
    assert result.degraded is True
    assert result.degraded_text_sent is True
    assert result.reason is not None and "未注入" in result.reason
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


async def test_missing_wait_answer_degrades() -> None:
    fallback = Recorder()

    result = await _call(offerer=BareOfferer(), fallback_send_text=fallback)

    assert result.rtc_result == outbound.RTC_FAILED
    assert result.degraded is True
    assert result.reason is not None and "wait_answer" in result.reason
    assert fallback.calls == [(DEVICE_ID, REMINDER_TEXT)]


async def test_fallback_failure_is_reported_in_reason() -> None:
    fallback = Recorder(error=RuntimeError("ws gone"))

    result = await _call(sender=FakeSender(offline=True), fallback_send_text=fallback)

    assert result.ok is False
    assert result.degraded is True  # 降级失败也仍然是「已走降级」
    assert result.reason is not None and "降级播报也失败" in result.reason


async def test_default_fallback_uses_tts_text_envelope() -> None:
    offerer = FakeOfferer(answer=False)  # 窗口内没等到 answer
    sender = FakeSender()

    result = await _call(
        offerer=offerer,
        sender=sender,
        timeout_seconds=0.05,
        fallback_send_text=None,  # 不注入 → 退回 sender.send(tts_text)
    )

    assert result.rtc_result == outbound.RTC_TIMEOUT
    assert len(sender.sent) == 2
    assert sender.sent[0][1] == {"type": "offer", "sdp": OFFER_SDP}
    degrade = sender.sent[1][1]
    assert degrade == {"type": "tts_text", "text": REMINDER_TEXT, "degraded": True}
    assert set(degrade) == set(get_type_hints(signaling.TtsTextMsg))


# ---------------------------------------------------------------- ⑤ 计时与打点


async def test_elapsed_ms_counts_speak_time() -> None:
    async def slow_speak(device_id: str, text: str) -> None:
        await asyncio.sleep(0.05)

    result = await _call(speak=slow_speak)

    assert result.ok is True
    assert result.elapsed_ms >= 40


async def test_metrics_record_calls_and_elapsed() -> None:
    result = await _call()

    snapshot = metrics_core.metrics_snapshot()
    assert snapshot["counters"] == {"outbound_calls_total{result=ok}": 1}
    assert set(snapshot["timings"]) == {"outbound_call_ms"}
    slot = snapshot["timings"]["outbound_call_ms"]
    assert slot["count"] == 1
    # 落进 timings 的就是返回值里那一个数，两条路径不许各算一次
    assert slot["sum_ms"] == pytest.approx(result.elapsed_ms)
    assert slot["max_ms"] == pytest.approx(result.elapsed_ms)


async def test_metrics_label_carries_rtc_result() -> None:
    offerer = FakeOfferer()
    offerer.answer_delay_s = SLOW_ANSWER_S

    await _call(offerer=offerer, timeout_seconds=SHORT_BUDGET_S)

    counters = metrics_core.metrics_snapshot()["counters"]
    assert counters == {"outbound_calls_total{result=timeout}": 1}
    assert "outbound_call_ms" in metrics_core.metrics_snapshot()["timings"]


async def test_metrics_can_be_suppressed() -> None:
    result = await _call(metrics_enabled=False)

    assert result.ok is True
    snapshot = metrics_core.metrics_snapshot()
    assert snapshot["counters"] == {}
    assert snapshot["timings"] == {}


async def test_rtc_result_stays_inside_closed_set() -> None:
    """失败分类只许落在闭集里（落库映射靠它，多一个值下游就漏判）。"""
    offerer = FakeOfferer()
    offerer.answer_error = outbound.IceFailedError("ice")

    result = await _call(offerer=offerer)

    assert result.rtc_result in outbound.RTC_RESULTS


# ---------------------------------------------------------------- ⑥ 契约闭集


def test_c2_closed_set_is_unchanged() -> None:
    """反向外呼复用的是既有 C2 消息类型，本卡没有新增任何类型。"""
    declared = _declared_message_types()

    assert declared == EXPECTED_MESSAGE_TYPES
    # 复用的这两个就在闭集里，不是另起一套
    assert outbound.OFFER_MESSAGE_TYPE in declared
    assert outbound.DEGRADE_MESSAGE_TYPE in declared


def test_outbound_envelopes_match_signaling() -> None:
    """两个信封与 `signaling` 的 TypedDict 逐字段一致（含 Field 名与字面量）。"""
    offer = outbound.build_offer_message(OFFER_SDP)
    offer_hints = get_type_hints(signaling.OfferMsg)
    assert set(offer) == set(offer_hints) == {"type", "sdp"}
    assert get_args(offer_hints["type"]) == (outbound.OFFER_MESSAGE_TYPE,)
    assert offer == {"type": "offer", "sdp": OFFER_SDP}

    degrade = outbound.build_degrade_text_message(REMINDER_TEXT)
    tts_hints = get_type_hints(signaling.TtsTextMsg)
    assert set(degrade) == set(tts_hints) == {"type", "text", "degraded"}
    assert get_args(tts_hints["type"]) == (outbound.DEGRADE_MESSAGE_TYPE,)
    assert degrade == {"type": "tts_text", "text": REMINDER_TEXT, "degraded": True}


# ---------------------------------------------------------------- SignalingSender


async def test_signaling_sender_maps_deliver_false_to_offline() -> None:
    delivered: list[tuple[str, dict[str, Any]]] = []

    def deliver(device_id: str, message: Mapping[str, Any]) -> bool:
        if device_id != DEVICE_ID:
            return False
        delivered.append((device_id, dict(message)))
        return True

    sender = outbound.SignalingSender(deliver)
    await sender.send(DEVICE_ID, outbound.build_offer_message(OFFER_SDP))

    assert delivered == [(DEVICE_ID, {"type": "offer", "sdp": OFFER_SDP})]
    with pytest.raises(outbound.DeviceOfflineError):
        await sender.send("device-missing", outbound.build_offer_message(OFFER_SDP))


async def test_signaling_sender_defaults_to_signaling_deliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不注入 deliver 时惰性取 `signaling.deliver`（生产路径，测试里替换掉真投递）。"""
    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_deliver(device_id: str, message: Mapping[str, Any]) -> bool:
        captured.append((device_id, dict(message)))
        return True

    monkeypatch.setattr(signaling, "deliver", fake_deliver)

    sender = outbound.SignalingSender()
    await sender.send(DEVICE_ID, outbound.build_degrade_text_message(REMINDER_TEXT))

    assert captured == [(DEVICE_ID, {"type": "tts_text", "text": REMINDER_TEXT, "degraded": True})]
