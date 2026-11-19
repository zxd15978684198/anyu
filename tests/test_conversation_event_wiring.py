"""W1 · N40：会话事件流接线的语义测试。

覆盖的是「接错了不报错、只会静默少记」的那类问题：

1. **顺序**：`user_said` 必须先于 `assistant_said` 落库。反过来的话，回放时会看到
   「先有回答、后有提问」，而回放正是断线续传与口径审计的输入。
2. **旁路性**：事件流写不进去（记录器抛、会话工厂坏、Redis/PG 抖）**绝不能**让这一轮
   对话不成立。长者听到回复是主链路，事件流是底座。
3. **缺省不记**：没有注入记录器时，行为与接线前逐字一致（迭代 5 的路径零改动）。

刻意不测：`conversation_events` 表的取号/冲突/隔离（已在
`tests/test_conversation_event_service.py` 用真库覆盖），以及 ASR partial 与分段送达
（N41 的范围）。
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest

from app.audio import media_bridge
from app.core.context import DialogueIdentity
from app.dialogue_runtime import DialogueRuntime
from app.services.conversation_event_recorder import ConversationEventRecorder
from app.services.dialogue_service import DialogueReply

# --------------------------------------------------------------------- 替身

ELDER_ID = "11111111-1111-4111-8111-111111111102"
THREAD_ID = "8171f6f6-6ebe-485d-967d-f7f5e5191d4c"


class _FakeService:
    def __init__(self, reply: DialogueReply | None = None) -> None:
        self.reply = reply or DialogueReply(text="杭州今天晴，气温八度。", degraded=False)
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> DialogueReply:
        self.calls.append(kwargs)
        return self.reply


class _FakeCascade:
    def __init__(
        self,
        *,
        device_id: str = "dev-1",
        resolved: bool = True,
        source: str = "voice",
        thread_id: str = THREAD_ID,
        candidate_elder_id: str | None = ELDER_ID,
    ) -> None:
        self.device_id = device_id
        self.trace_id = "trace-1"
        self.resolved = resolved
        self.source = source
        self.thread_id = thread_id
        self.candidate_elder_id = candidate_elder_id


class _RecordingRecorder:
    """记录调用顺序的替身（`ConversationEventRecorder` 的最小接口）。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def record(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return True


class _FakeSession:
    """`AsyncSession` 的最小替身：只关心 commit 与退出。"""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _put_cascade(device_id: str, cascade: _FakeCascade) -> None:
    media_bridge._cascades[device_id] = cascade  # type: ignore[assignment]


def _identity() -> DialogueIdentity:
    return DialogueIdentity(elder_id=ELDER_ID, source="voice", confidence=0.9)


# ===========================================================================
# 一、顺序与载荷
# ===========================================================================


class TestWiringOrder:
    @pytest.mark.asyncio
    async def test_user_said_recorded_before_assistant_said(self) -> None:
        service = _FakeService()
        recorder = _RecordingRecorder()
        runtime = DialogueRuntime(service=service, recorder=recorder)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "trace-9", "今天天气怎么样")

        types = [c["event_type"] for c in recorder.calls]
        assert types == ["user_said", "assistant_said"]
        assert recorder.calls[0]["payload"]["text"] == "今天天气怎么样"
        assert recorder.calls[1]["payload"]["text"] == "杭州今天晴，气温八度。"
        # 两行同属一轮：thread/elder/trace 必须一致，否则回放会裂成两段
        for call in recorder.calls:
            assert str(call["thread_id"]) == THREAD_ID
            assert str(call["elder_id"]) == ELDER_ID
            assert call["trace_id"] == "trace-9"

    @pytest.mark.asyncio
    async def test_user_said_carries_char_count(self) -> None:
        """字数入事件是为了回放时能区分「短确认」与「长叙述」，不是给模型看的。"""
        recorder = _RecordingRecorder()
        runtime = DialogueRuntime(service=_FakeService(), recorder=recorder)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "t", "我要吃药了")

        assert recorder.calls[0]["payload"]["chars"] == 5


# ===========================================================================
# 二、旁路性：写不进去也不能让这一轮不成立
# ===========================================================================


class TestSideChannelSafety:
    @pytest.mark.asyncio
    async def test_recorder_raising_does_not_break_turn(self) -> None:
        """记录器违约（抛异常）时，回复照样产出、服务照样被调用。"""
        service = _FakeService()
        recorder = _RecordingRecorder(error=RuntimeError("db down"))
        runtime = DialogueRuntime(service=service, recorder=recorder)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert service.calls, "主链路必须照常编排"
        # 第一行抛了，第二行仍会尝试写 —— 旁路失败不短路后续
        assert [c["event_type"] for c in recorder.calls] == ["user_said", "assistant_said"]

    @pytest.mark.asyncio
    async def test_no_recorder_keeps_baseline(self) -> None:
        """不注入记录器 = 接线前的行为，一行事件都不写、也不报错。"""
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert service.calls

    @pytest.mark.asyncio
    async def test_skipped_turn_records_nothing(self) -> None:
        """身份未定 → 整轮不编排，也不该留下孤立的 user_said。"""
        recorder = _RecordingRecorder()
        runtime = DialogueRuntime(service=_FakeService(), recorder=recorder)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade(candidate_elder_id=None))

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert recorder.calls == []


# ===========================================================================
# 三、Recorder 自身：事务与失败收敛
# ===========================================================================


class TestRecorder:
    @pytest.mark.asyncio
    async def test_commit_called_and_true_returned(self) -> None:
        session = _FakeSession()
        recorder = ConversationEventRecorder(lambda: session)  # type: ignore[arg-type]
        seen: list[dict[str, Any]] = []

        async def _fake_append(sess: Any, **kwargs: Any) -> None:
            seen.append({"session": sess, **kwargs})

        with patch(
            "app.services.conversation_event_service.append_event", side_effect=_fake_append
        ):
            ok = await recorder.record(
                thread_id=THREAD_ID,
                elder_id=ELDER_ID,
                event_type="user_said",
                payload={"text": "你好"},
                trace_id="t-1",
            )

        assert ok is True
        assert session.committed is True
        assert seen[0]["session"] is session
        assert seen[0]["event_type"] == "user_said"

    @pytest.mark.asyncio
    async def test_failure_returns_false_without_raising(self) -> None:
        session = _FakeSession()
        recorder = ConversationEventRecorder(lambda: session)  # type: ignore[arg-type]

        with patch(
            "app.services.conversation_event_service.append_event",
            side_effect=RuntimeError("insert failed"),
        ):
            ok = await recorder.record(
                thread_id=uuid.uuid4(),
                elder_id=uuid.uuid4(),
                event_type="user_said",
            )

        assert ok is False
        assert session.committed is False

    @pytest.mark.asyncio
    async def test_record_many_empty_is_noop(self) -> None:
        session = _FakeSession()
        recorder = ConversationEventRecorder(lambda: session)  # type: ignore[arg-type]

        ok = await recorder.record_many(
            thread_id=THREAD_ID, elder_id=ELDER_ID, events=[], trace_id=None
        )

        assert ok is True
        assert session.committed is False
