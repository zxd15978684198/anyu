"""会话中途声纹复检（S1 / B2-06）的接线与降级用例 —— **DB-free**。

钉三件事：
  1. 复检结论 → 处置：同一人放行；低置信写审计 + 走**既有** `identity_ask` 降级；
  2. 复检自身失败（异常 / 超时）不得打断对话，只按「不复检」放行；
  3. 阈值边界取 `>=`（0.55 命中、0.5499 不命中），缺省阈值来自 settings。

**已知局限（产品负责人 2026-11-13 裁决接受）**：本任务不碰 `app/audio/webrtc.py`，
它用 `session.spawn` 并发触发复检，所以**复检结论自下一句生效** —— 换人那一句
本身仍已进入对话编排。本文件只钉 `media_bridge` 内的处置逻辑，不覆盖该时序。

全部替身：`identity_service.recheck_session_identity` /
`audit_session_recheck_denied`、`media_bridge._session_scope` /
`_apply_confirm_request` / `_deliver`。不连库、不连容器。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.audio import media_bridge
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.services import identity_service
from app.services.identity_service import (
    IdentityDecision,
    SessionRecheckResult,
    classify_session_voiceprint,
)

DEVICE_ID = "11111111-1111-4111-8111-111111111111"
DEVICE_UUID = uuid.UUID(DEVICE_ID)
ELDER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER_ELDER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
THREAD_ID = f"device:{DEVICE_ID}"
OTHER_THREAD_ID = f"device:{DEVICE_ID}:2"


# --------------------------------------------------------------------------- 替身


class _Recorder:
    """收集替身调用；`result` / `error` / `delay` 控制假复检的行为。"""

    def __init__(self) -> None:
        self.recheck_calls: list[dict[str, Any]] = []
        self.audit_calls: list[dict[str, Any]] = []
        self.ask_calls: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self.result: SessionRecheckResult | None = None
        self.error: BaseException | None = None
        self.delay: float = 0.0


def _identity() -> Identity:
    return Identity(kind="device", device_id=DEVICE_ID, elder_id=str(ELDER_ID))


def _cascade(**overrides: Any) -> media_bridge._Cascade:
    kwargs: dict[str, Any] = {
        "device_id": DEVICE_ID,
        "trace_id": "trace-recheck",
        "resolved": True,
        "source": "voice",
        "thread_id": THREAD_ID,
        "identity": _identity(),
    }
    kwargs.update(overrides)
    return media_bridge._Cascade(**kwargs)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    cascade: media_bridge._Cascade,
) -> None:
    async def fake_recheck(
        session: Any,
        *,
        device_id: uuid.UUID,
        current_thread_id: str,
        audio_bytes: bytes,
    ) -> SessionRecheckResult:
        rec.recheck_calls.append(
            {
                "device_id": device_id,
                "thread_id": current_thread_id,
                "audio": audio_bytes,
            }
        )
        if rec.delay:
            await asyncio.sleep(rec.delay)
        if rec.error is not None:
            raise rec.error
        assert rec.result is not None
        return rec.result

    async def fake_audit(**kwargs: Any) -> None:
        rec.audit_calls.append(kwargs)

    async def fake_ask(
        cascade_arg: media_bridge._Cascade,
        identity_arg: Identity,
        decision: IdentityDecision,
        session: Any,
    ) -> None:
        rec.ask_calls.append(
            {
                "elder_id": decision.elder_id,
                "reason": decision.reason,
                "thread_id": decision.thread_id,
            }
        )
        # 模拟 `_apply_confirm_request` 的可见副作用：ask_id + 在途定时器。
        cascade_arg.ask_id = "ask-recheck"
        cascade_arg.timeout_task = asyncio.create_task(asyncio.sleep(0))

    @asynccontextmanager
    async def fake_scope(injected: Any):
        yield SimpleNamespace(name="fake-session")

    def fake_deliver(device_id: str, message: dict[str, Any]) -> bool:
        rec.delivered.append(message)
        return True

    monkeypatch.setattr(identity_service, "recheck_session_identity", fake_recheck)
    monkeypatch.setattr(identity_service, "audit_session_recheck_denied", fake_audit)
    monkeypatch.setattr(media_bridge, "_session_scope", fake_scope)
    monkeypatch.setattr(media_bridge, "_apply_confirm_request", fake_ask)
    monkeypatch.setattr(media_bridge, "_deliver", fake_deliver)
    monkeypatch.setitem(media_bridge._cascades, DEVICE_ID, cascade)


def _same_person(score: float = 0.81) -> SessionRecheckResult:
    return SessionRecheckResult(
        status="same_person", score=score, threshold=0.55, elder_id=ELDER_ID
    )


def _suspected(score: float = 0.31) -> SessionRecheckResult:
    return SessionRecheckResult(
        status="suspected_replacement",
        score=score,
        threshold=0.55,
        elder_id=ELDER_ID,
    )


# --------------------------------------------------------------------------- 用例


async def test_same_person_passes_without_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """① 高于阈值 → 放行：不写审计、不弹层、状态不变。"""
    rec = _Recorder()
    rec.result = _same_person()
    cascade = _cascade()
    _install(monkeypatch, rec, cascade)

    acted = await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")

    assert acted is False
    assert len(rec.recheck_calls) == 1
    assert rec.recheck_calls[0]["thread_id"] == THREAD_ID
    assert rec.audit_calls == []
    assert rec.ask_calls == []
    assert cascade.resolved is True
    assert cascade.recheck_pending is False


async def test_low_confidence_audits_then_asks_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """② 低于阈值 → 写审计 + 走既有 identity_ask；同一窗口内第二句不再弹层。"""
    rec = _Recorder()
    rec.result = _suspected()
    cascade = _cascade()
    _install(monkeypatch, rec, cascade)

    acted = await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")

    assert acted is True
    assert len(rec.audit_calls) == 1
    audit = rec.audit_calls[0]
    assert audit["thread_id"] == THREAD_ID
    assert audit["device_id"] == DEVICE_UUID
    assert audit["elder_id"] == ELDER_ID
    assert audit["score"] == 0.31
    assert audit["threshold"] == 0.55
    # 复用的是现成的低置信话术，不新造 reason
    assert len(rec.ask_calls) == 1
    assert rec.ask_calls[0]["reason"] == "low_confidence"
    assert rec.ask_calls[0]["elder_id"] == ELDER_ID
    assert cascade.recheck_pending is True
    # 身份本身没被推翻（换了才翻），只是这一次降级
    assert cascade.resolved is True

    # 同一 30s 窗口内第二句：不重复跑复检、不再叠一条 ask
    second = await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")
    assert second is False
    assert len(rec.recheck_calls) == 1
    assert len(rec.audit_calls) == 1
    assert len(rec.ask_calls) == 1


async def test_denied_cooldown_suppresses_second_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """② 续：弹层结束后 60s 内再次低置信 → 仍留痕，但不再弹层（R-C 抗误报）。"""
    rec = _Recorder()
    rec.result = _suspected()
    cascade = _cascade()
    _install(monkeypatch, rec, cascade)

    await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")
    assert len(rec.ask_calls) == 1

    # 长者已确认（confirm 会清掉 pending），紧接着又说了一句低置信
    cascade.recheck_pending = False
    await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")

    assert len(rec.audit_calls) == 2  # 每次判定都留痕
    assert len(rec.ask_calls) == 1  # 冷却窗口内不再审讯长者


async def test_recheck_exception_passes_through(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """③ 复检抛异常 → 放行、不崩、只 warn，绝不打断对话。"""
    rec = _Recorder()
    rec.error = RuntimeError("voiceprint container exploded")
    cascade = _cascade()
    _install(monkeypatch, rec, cascade)

    with caplog.at_level(logging.WARNING, logger="app.audio.identity"):
        acted = await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")

    assert acted is False
    assert rec.audit_calls == []
    assert rec.ask_calls == []
    assert cascade.resolved is True
    assert any(record.message == "identity_recheck_failed" for record in caplog.records)


async def test_recheck_timeout_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """③ 续：复检超时（容器卡住）→ 按「不复检」放行，不等满 30s。"""
    rec = _Recorder()
    rec.result = _same_person()
    rec.delay = 5.0
    cascade = _cascade()
    _install(monkeypatch, rec, cascade)
    # 把预算压到 50ms：余量 0 + 假 settings 的 extract 预算
    monkeypatch.setattr(media_bridge, "_RECHECK_TIMEOUT_MARGIN_S", 0.0)
    monkeypatch.setattr(
        media_bridge,
        "get_settings",
        lambda: SimpleNamespace(voiceprint_extract_timeout_seconds=0.05),
    )

    started = time.monotonic()
    acted = await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")
    elapsed = time.monotonic() - started

    assert acted is False
    assert elapsed < 1.0
    assert rec.audit_calls == []
    assert rec.ask_calls == []


def test_threshold_boundary_is_inclusive_and_from_settings() -> None:
    """④ 阈值边界：恰好 0.55 命中，0.5499 不命中；缺省阈值来自 settings。"""
    threshold = get_settings().voiceprint_similarity_threshold
    assert threshold == pytest.approx(0.55)

    assert classify_session_voiceprint(threshold) == "same_person"
    assert classify_session_voiceprint(0.5499) == "suspected_replacement"
    assert classify_session_voiceprint(0.55, threshold=0.55) == "same_person"
    assert classify_session_voiceprint(0.5499, threshold=0.55) == "suspected_replacement"
    # 阈值可被显式传入（media_bridge 用 voiceprint_service 回传的阈值复核）
    assert classify_session_voiceprint(0.8, threshold=0.9) == "suspected_replacement"


async def test_switched_identity_updates_thread_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补：命中另一位已绑定长者 → 沿用服务层新 thread，并下发 identity_result。"""
    rec = _Recorder()
    rec.result = SessionRecheckResult(
        status="switched",
        score=0.77,
        threshold=0.55,
        elder_id=OTHER_ELDER_ID,
        decision=IdentityDecision(
            source="voice",
            elder_id=OTHER_ELDER_ID,
            thread_id=OTHER_THREAD_ID,
            confidence=0.77,
        ),
    )
    cascade = _cascade()
    _install(monkeypatch, rec, cascade)

    acted = await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")

    assert acted is True
    assert cascade.thread_id == OTHER_THREAD_ID
    assert cascade.source == "voice"
    assert rec.audit_calls == []  # 换人是正常切 thread，不是「拒绝」
    assert rec.delivered[-1] == {
        "type": "identity_result",
        "source": "voice",
        "elder_id": str(OTHER_ELDER_ID),
    }


async def test_generic_session_skips_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """补：generic 会话没有本人身份可保护 → 连复检都不跑。"""
    rec = _Recorder()
    rec.result = _suspected()
    cascade = _cascade(source="generic", thread_id=None)
    _install(monkeypatch, rec, cascade)

    acted = await media_bridge.resolve_identity_from_audio(DEVICE_ID, b"pcm-16k")

    assert acted is False
    assert rec.recheck_calls == []
    assert rec.audit_calls == []
