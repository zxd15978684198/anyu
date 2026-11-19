"""媒体控制下行（FR-CON-03）：规则层判定的动作真的变成一条 WS 消息。

为什么单开一个文件：`tests/test_media_api.py` 覆盖的是 HTTP 五端点，
`tests/test_dialogue_runtime.py` 覆盖的是「什么时候该回」；这条链路横跨两者——
路由层给出 `decision.intent`，编排层翻成 `media_control` 下行，前端才动播放器。
接错的后果是**静默的**：长者说「换一段」，数字人嘴上答「好嘞」，唱段照旧。

覆盖六件事：
1. 六个动作标识各自翻成什么（`next` / `stop` / `volume_up` / `volume_down` / 两个剧种）；
2. `seq` 逐条自增（前端靠它丢弃迟到指令）；
3. 非媒体意图不下发；
4. `decision is None`（路由挂了）不下发 —— 不能因为路由失败就瞎发一条；
5. 设备掉线（`deliver` 返回 False）不抛、不打断这一轮；
6. 投递发生在回复之前（先停旧唱段，再听数字人说话）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.audio import media_bridge, signaling
from app.core.context import DialogueIdentity
from app.dialogue_runtime import DialogueRuntime
from app.router.decision import RouteDecision
from app.services.dialogue_service import DialogueReply

DEVICE = "dev-media-1"
ELDER = "11111111-1111-4111-8111-111111111102"


class _FakeService:
    """最小 `DialogueService` 替身：只记调用、返回固定回复。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> DialogueReply:
        self.calls.append(kwargs)
        return DialogueReply(text="好嘞。", degraded=False)


class _FakeCascade:
    """`media_bridge._Cascade` 的最小替身（`_resolve` 只读这几个字段）。"""

    def __init__(self) -> None:
        self.device_id = DEVICE
        self.trace_id = "trace-1"
        self.resolved = True
        self.source = "voice"
        self.thread_id = "thread-media"
        self.candidate_elder_id = ELDER
        self.candidates: list[dict[str, str]] = []
        self.greeting = None
        self.greeting_clock_start = None


def _decision(
    intent: str | None,
    *,
    layer: str = "L0",
    rule_id: str | None = "play_control",
) -> RouteDecision:
    return RouteDecision(
        elder_id=ELDER,
        thread_id="thread-media",
        layer=layer,
        rule_id=rule_id,
        intent=intent,
        trace_id="trace-1",
    )


def _runtime(decision: RouteDecision | None) -> tuple[DialogueRuntime, _FakeService]:
    service = _FakeService()

    async def fake_route(*args: Any, **kwargs: Any) -> RouteDecision | None:
        return decision

    runtime = DialogueRuntime(service=service, router=fake_route)  # type: ignore[arg-type]
    return runtime, service


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    media_bridge._cascades.clear()
    signaling.set_dialogue_final_handler(None)
    yield
    media_bridge._cascades.clear()
    signaling.set_dialogue_final_handler(None)


async def _run(
    runtime: DialogueRuntime, delivered: list[dict[str, Any]], *, ok: bool = True
) -> None:
    media_bridge._cascades[DEVICE] = _FakeCascade()  # type: ignore[assignment]

    def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
        delivered.append(dict(message))
        return ok

    with patch.object(signaling, "deliver", side_effect=fake_deliver):
        await runtime.on_asr_final(DEVICE, "turn-1", "trace-1", "换一段")


def _controls(delivered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in delivered if message["type"] == "media_control"]


# ---------------------------------------------------------------- 动作翻译


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("play_next", {"action": "next"}),
        ("stop", {"action": "stop"}),
        ("volume_up", {"action": "volume_up"}),
        ("volume_down", {"action": "volume_down"}),
        ("play_yueju", {"action": "play", "kind": "opera", "genre": "越剧"}),
        ("play_jingju", {"action": "play", "kind": "opera", "genre": "京剧"}),
    ],
)
@pytest.mark.asyncio
async def test_intent_is_translated_to_media_control(intent: str, expected: dict[str, str]) -> None:
    runtime, _ = _runtime(_decision(intent))
    delivered: list[dict[str, Any]] = []
    await _run(runtime, delivered)

    controls = _controls(delivered)
    assert len(controls) == 1
    got = {key: value for key, value in controls[0].items() if key not in {"type", "seq"}}
    assert got == expected
    # `seq` 是给前端的去重键，必须是非零整数
    assert isinstance(controls[0]["seq"], int) and controls[0]["seq"] >= 1


@pytest.mark.asyncio
async def test_seq_increases_per_device() -> None:
    """连说两句「换一段」：第二条 `seq` 更大，前端才能判出哪条更新。"""
    runtime, _ = _runtime(_decision("play_next"))
    delivered: list[dict[str, Any]] = []

    await _run(runtime, delivered)
    await _run(runtime, delivered)

    controls = _controls(delivered)
    assert [message["action"] for message in controls] == ["next", "next"]
    assert controls[1]["seq"] > controls[0]["seq"]


# ---------------------------------------------------------------- 不该发的情形


@pytest.mark.parametrize("intent", [None, "weather_query", "chat", "play_opera_unknown"])
@pytest.mark.asyncio
async def test_non_media_intent_sends_nothing(intent: str | None) -> None:
    runtime, _ = _runtime(_decision(intent))
    delivered: list[dict[str, Any]] = []
    await _run(runtime, delivered)
    assert _controls(delivered) == []


@pytest.mark.asyncio
async def test_missing_decision_sends_nothing() -> None:
    """路由挂了（`decision is None`）：照常回话，但不许瞎猜一个动作发下去。"""
    runtime, service = _runtime(None)
    delivered: list[dict[str, Any]] = []
    await _run(runtime, delivered)

    assert _controls(delivered) == []
    assert len(service.calls) == 1, "路由失败不影响这一轮回话"


@pytest.mark.asyncio
async def test_delivery_failure_does_not_break_the_turn() -> None:
    """设备掉线：投递返回 False，整轮照常走完（不抛、不吞掉回复）。"""
    runtime, service = _runtime(_decision("stop"))
    delivered: list[dict[str, Any]] = []
    await _run(runtime, delivered, ok=False)

    assert len(_controls(delivered)) == 1
    assert len(service.calls) == 1


# ---------------------------------------------------------------- 顺序


@pytest.mark.asyncio
async def test_media_control_is_delivered_before_the_reply() -> None:
    """先停旧唱段，再听数字人回话：`media_control` 必须排在 `tts_text` 之前。

    反了会怎样：长者说「停」，唱段还在响，数字人已经开始说「好的」，两句叠在一起。
    """
    runtime, _ = _runtime(_decision("stop"))
    delivered: list[dict[str, Any]] = []
    await _run(runtime, delivered)

    types = [message["type"] for message in delivered]
    assert types.index("media_control") < types.index("tts_text")


@pytest.mark.asyncio
async def test_generic_identity_still_gets_control() -> None:
    """身份不明（generic）也能切歌：控制指令不写库、不带 `elder_id`，不属于个人数据。"""

    class _GenericCascade(_FakeCascade):
        def __init__(self) -> None:
            super().__init__()
            self.source = "generic"
            self.candidate_elder_id = None

    runtime, _ = _runtime(_decision("volume_up"))
    delivered: list[dict[str, Any]] = []
    media_bridge._cascades[DEVICE] = _GenericCascade()  # type: ignore[assignment]

    def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
        delivered.append(dict(message))
        return True

    with patch.object(signaling, "deliver", side_effect=fake_deliver):
        await runtime.on_asr_final(DEVICE, "turn-1", "trace-1", "大声点")

    assert [message["action"] for message in _controls(delivered)] == ["volume_up"]


def test_identity_fixture_matches_elder_shape() -> None:
    """守住替身与真实身份的同形：`DialogueIdentity` 的 `elder_id` 是字符串。"""
    identity = DialogueIdentity(source="voice", elder_id=ELDER)
    assert identity.elder_id == ELDER
