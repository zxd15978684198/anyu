"""FR-CON-03 后半（W6，2026-11-12）：暂停 / 继续 → 下发 `media_control` → 播放器原位续播。

## 这条链路为什么必须钉住

规则层在 迭代 6 就产出了 `pause` / `resume` 两个 intent（`app/router/rules.py:105/112`），
但 `media_service.CONTROL_INTENTS` 没有映射 → `control_intent_from_rule()` 返回 `None`
→ `DialogueRuntime._push_media_control()` 直接 return → pad 收不到任何消息；
更糟的是 `rule_replies.INSTANT_REPLIES["pause"]` 还回着「这台只能整个关掉、不能暂停」
的**假话**。三处各自看着都对，接不起来就是「长者说暂停，唱段照旧响」。

本文件钉四层（自下而上，缺一层链路就断）：

1. 规则层：说「暂停 / 继续」命中哪个 intent；
2. 服务层：intent → 动作串（`pause` / `resume`），且不再落回 `None`；
3. 编排层：动作串真的变成一条 WS `media_control`，`seq` 自增；
4. 回话：`INSTANT_REPLIES` 里这两句是实话（说「停在这儿」，不说「不能暂停」）。

前端（`MediaPlayer.tsx` 的 `pausedAtRef` + seek、`realtime.ts` 白名单）不在 pytest
覆盖内，由 `pnpm --dir frontend build` 类型检查 + 人工验收；这里只保证**服务端把话
说对了、把指令发出去了**。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.audio import signaling
from app.dialogue_runtime import DialogueRuntime
from app.router import rule_replies
from app.router.rules import match_l0
from app.services import media_service
from app.services.dialogue_service import DialogueReply

DEVICE = "dev-pause-1"


class _FakeService:
    """最小 `DialogueService` 替身：`_push_media_control` 用不到它的返回值。"""

    async def respond(self, **kwargs: Any) -> DialogueReply:
        return DialogueReply(text="好嘞。", degraded=False)


async def _fake_route(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    signaling.set_dialogue_final_handler(None)
    yield
    signaling.set_dialogue_final_handler(None)


# ---------------------------------------------------------------- 1. 规则层


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("暂停", "pause"),
        ("先暂停一下", "pause"),
        ("暂停播放", "pause"),
        ("继续", "resume"),
        ("接着听", "resume"),
    ],
)
def test_rule_layer_yields_pause_and_resume_intents(text: str, intent: str) -> None:
    decision = match_l0(text)
    assert decision is not None, f"{text!r} 应命中 L0 固定意图"
    assert decision.intent == intent


# ---------------------------------------------------------------- 2. 服务层映射


@pytest.mark.parametrize("intent", ["pause", "resume"])
def test_control_intent_maps_to_itself(intent: str) -> None:
    """此前这里返回 `None`（表里没有键），整条链路就断在这一行。"""
    assert media_service.control_intent_from_rule(intent) == intent


def test_every_rule_intent_has_a_mapping_or_is_deliberately_unmapped() -> None:
    """守住「规则层新增 intent 却忘了加映射」：已知媒体 intent 必须都能翻译。"""
    known = {"play_next", "stop", "pause", "resume", "volume_up", "volume_down"}
    known |= {"play_yueju", "play_jingju"}
    missing = {intent for intent in known if media_service.control_intent_from_rule(intent) is None}
    assert missing == set()


# ---------------------------------------------------------------- 3. 编排层下行


@pytest.mark.parametrize(("text", "action"), [("暂停", "pause"), ("接着听", "resume")])
async def test_pause_and_resume_reach_the_wire(text: str, action: str) -> None:
    runtime = DialogueRuntime(service=_FakeService(), router=_fake_route)  # type: ignore[arg-type]
    decision = match_l0(text)
    assert decision is not None
    delivered: list[dict[str, Any]] = []

    def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
        delivered.append(dict(message))
        return True

    with patch.object(signaling, "deliver", side_effect=fake_deliver):
        runtime._push_media_control(DEVICE, decision, trace_id="trace-1", turn_id="turn-1")

    assert delivered == [{"type": "media_control", "action": action, "seq": 1}]


async def test_pause_then_resume_share_one_increasing_seq() -> None:
    """同一台设备上「暂停 → 继续」`seq` 必须递增：前端靠它丢迟到指令。"""
    runtime = DialogueRuntime(service=_FakeService(), router=_fake_route)  # type: ignore[arg-type]
    delivered: list[dict[str, Any]] = []

    def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
        delivered.append(dict(message))
        return True

    with patch.object(signaling, "deliver", side_effect=fake_deliver):
        for text in ("暂停", "继续"):
            decision = match_l0(text)
            assert decision is not None
            runtime._push_media_control(DEVICE, decision, trace_id="trace-1", turn_id="turn-1")

    assert [message["action"] for message in delivered] == ["pause", "resume"]
    assert delivered[1]["seq"] > delivered[0]["seq"]


# ---------------------------------------------------------------- 4. 回话是不是实话


def test_pause_reply_is_truthful_now() -> None:
    """接通后旧话术就是假话：不能再出现「不能暂停」。"""
    reply = rule_replies.INSTANT_REPLIES["pause"]
    assert "不能暂停" not in reply
    assert "继续" in reply, "要告诉长者怎么续上"


def test_resume_reply_promises_continuation() -> None:
    assert "接着" in rule_replies.INSTANT_REPLIES["resume"]
