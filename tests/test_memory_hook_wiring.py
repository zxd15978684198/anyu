"""N27b：长期记忆写回接线的语义测试。

覆盖三件「接错了不会报错、只会静默不记」的事：

1. **时机**：写回挂在**回复之后**，不占长者的等待时间；设备掉线也要记
   （记忆的来源是这次对话，不是这次送达）。
2. **旁路性**：钩子抛异常、钩子卡住，都不能让这一轮对话不成立，也不能
   让未取回的异常留在事件循环里。
3. **钩子自身**：空转写不调模型、模型说「没什么可记」时不开 session、
   写回失败先 rollback 再收敛成 None。

刻意不测：提取质量（`tests/test_memory_extraction.py`）与去重/降权/容量
（`tests/test_memory_writeback.py`）。这两处的口径各自有主，这里只测「串起来」。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.dialogue_runtime as runtime_mod
from app.audio import media_bridge
from app.core.context import DialogueIdentity
from app.dialogue_runtime import DialogueRuntime
from app.services.dialogue_service import DialogueReply
from app.services.memory_extraction import MemoryCandidate
from app.services.memory_turn_hook import MemoryTurnHook
from app.services.memory_writeback import WritebackResult

ELDER_ID = "11111111-1111-4111-8111-111111111102"
THREAD_ID = "8171f6f6-6ebe-485d-967d-f7f5e5191d4c"

# --------------------------------------------------------------------- 替身


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


class _RecordingHook:
    """最小接口替身：记录每一次调用，可注入异常或延迟。"""

    def __init__(self, *, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error
        self.delay = delay
        self.started = asyncio.Event()

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error


class _FakeSession:
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


async def _settle(runtime: DialogueRuntime) -> None:
    """等后台记忆任务跑完（**不 cancel**，与 shutdown 的收尾区分开）。"""
    pending = list(runtime._memory_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ===========================================================================
# 一、时机与载荷
# ===========================================================================


class TestWiringTiming:
    @pytest.mark.asyncio
    async def test_hook_receives_both_sides_of_turn(self) -> None:
        hook = _RecordingHook()
        runtime = DialogueRuntime(service=_FakeService(), memory_hook=hook)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "trace-9", "我对青霉素过敏")
        await _settle(runtime)

        assert len(hook.calls) == 1
        call = hook.calls[0]
        assert call["user_text"] == "我对青霉素过敏"
        assert call["assistant_text"] == "杭州今天晴，气温八度。"
        assert call["thread_id"] == THREAD_ID
        assert str(call["elder_id"]) == ELDER_ID

    @pytest.mark.asyncio
    async def test_hook_does_not_block_the_reply(self) -> None:
        """钩子卡住时，回复路径不 waiting —— 这是「回复之后」的全部意义。"""
        hook = _RecordingHook(delay=5.0)
        runtime = DialogueRuntime(service=_FakeService(), memory_hook=hook)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        loop = asyncio.get_running_loop()
        started = loop.time()
        await runtime.on_asr_final("dev-1", "turn-1", "t", "我喜欢越剧")
        elapsed = loop.time() - started

        assert elapsed < 1.0, f"回复被记忆写回拖住了：{elapsed:.2f}s"
        await hook.started.wait()
        await runtime._drain_memory_tasks()

    @pytest.mark.asyncio
    async def test_hook_runs_even_when_device_is_offline(self) -> None:
        """字幕投递失败时仍要记：长者确实说过这句话，只是没送到耳朵里。"""
        hook = _RecordingHook()
        runtime = DialogueRuntime(service=_FakeService(), memory_hook=hook)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.dialogue_runtime.signaling.deliver", lambda *a, **k: False)
            await runtime.on_asr_final("dev-1", "turn-1", "t", "我腿疼")

        await _settle(runtime)
        assert [c["user_text"] for c in hook.calls] == ["我腿疼"]

    @pytest.mark.asyncio
    async def test_no_hook_keeps_baseline(self) -> None:
        service = _FakeService()
        runtime = DialogueRuntime(service=service)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert service.calls
        assert runtime._memory_tasks == set()

    @pytest.mark.asyncio
    async def test_skipped_turn_does_not_touch_memory(self) -> None:
        """身份未定 → 整轮不编排，连记忆也不该碰（否则会把 generic 轮记进上一位长者）。"""
        hook = _RecordingHook()
        runtime = DialogueRuntime(service=_FakeService(), memory_hook=hook)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade(candidate_elder_id=None))

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert hook.calls == []


# ===========================================================================
# 二、旁路性：钩子出问题也不能连累主链路
# ===========================================================================


class TestHookIsolation:
    @pytest.mark.asyncio
    async def test_hook_crash_does_not_break_turn(self) -> None:
        hook = _RecordingHook(error=RuntimeError("llm exploded"))
        service = _FakeService()
        runtime = DialogueRuntime(service=service, memory_hook=hook)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")
        await _settle(runtime)

        assert service.calls, "主链路必须照常编排"
        assert hook.calls, "钩子仍应被尝试调用"

    @pytest.mark.asyncio
    async def test_hook_is_bounded_by_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """卡住的后台任务必须被时限收掉，否则会一直占着 DB 连接。"""
        monkeypatch.setattr(runtime_mod, "MEMORY_TURN_TIMEOUT_S", 0.05)
        hook = _RecordingHook(delay=5.0)
        runtime = DialogueRuntime(service=_FakeService(), memory_hook=hook)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")
        await hook.started.wait()
        await _settle(runtime)

        assert runtime._memory_tasks == set(), "超时后任务应已结束，不该仍挂着"

    @pytest.mark.asyncio
    async def test_shutdown_cancels_pending_tasks(self) -> None:
        hook = _RecordingHook(delay=5.0)
        runtime = DialogueRuntime(service=_FakeService(), memory_hook=hook)  # type: ignore[arg-type]
        _put_cascade("dev-1", _FakeCascade())
        runtime.register()

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")
        await hook.started.wait()
        assert runtime._memory_tasks, "关停前应当有一个在跑的任务"

        await runtime.shutdown()

        assert runtime._memory_tasks == set()
        assert runtime._registered is False


# ===========================================================================
# 三、钩子自身：三条「不该发生」的边界
# ===========================================================================


class TestMemoryTurnHook:
    @pytest.mark.asyncio
    async def test_blank_text_never_calls_the_model(self) -> None:
        calls: list[Any] = []

        async def _extract(*a: Any, **k: Any) -> list[MemoryCandidate]:
            calls.append((a, k))
            return []

        hook = MemoryTurnHook(llm=object(), session_factory=_FakeSession, extract_memories=_extract)
        result = await hook(elder_id=ELDER_ID, thread_id=THREAD_ID, user_text="   ")

        assert result is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_empty_candidates_skip_the_session(self) -> None:
        opened: list[Any] = []

        async def _extract(*a: Any, **k: Any) -> list[MemoryCandidate]:
            return []

        def _factory() -> _FakeSession:
            session = _FakeSession()
            opened.append(session)
            return session

        hook = MemoryTurnHook(llm=object(), session_factory=_factory, extract_memories=_extract)
        result = await hook(elder_id=ELDER_ID, thread_id=THREAD_ID, user_text="今天天气不错")

        assert result is None
        assert opened == [], "模型没给出候选时不该开 session"

    @pytest.mark.asyncio
    async def test_success_commits_and_returns_result(self) -> None:
        session = _FakeSession()
        seen: list[dict[str, Any]] = []

        async def _extract(*a: Any, **k: Any) -> list[MemoryCandidate]:
            return [
                MemoryCandidate(
                    content="喜欢越剧", category="private", source="inferred", importance_hint=0.6
                )
            ]

        async def _writeback(sess: Any, **kwargs: Any) -> WritebackResult:
            seen.append({"session": sess, **kwargs})
            return WritebackResult(written=1)

        hook = MemoryTurnHook(
            llm=object(),
            session_factory=lambda: session,
            extract_memories=_extract,
            writeback_memories=_writeback,
        )
        result = await hook(elder_id=ELDER_ID, thread_id=THREAD_ID, user_text="我想听越剧")

        assert result is not None
        assert result.written == 1
        assert session.committed is True
        assert seen[0]["source_thread_id"] == THREAD_ID
        assert str(seen[0]["elder_id"]) == ELDER_ID

    @pytest.mark.asyncio
    async def test_writeback_failure_rolls_back_and_returns_none(self) -> None:
        session = _FakeSession()

        async def _extract(*a: Any, **k: Any) -> list[MemoryCandidate]:
            return [
                MemoryCandidate(
                    content="喜欢越剧", category="private", source="inferred", importance_hint=0.6
                )
            ]

        async def _writeback(sess: Any, **kwargs: Any) -> WritebackResult:
            raise RuntimeError("insert failed")

        hook = MemoryTurnHook(
            llm=object(),
            session_factory=lambda: session,
            extract_memories=_extract,
            writeback_memories=_writeback,
        )
        result = await hook(elder_id=ELDER_ID, thread_id=THREAD_ID, user_text="我想听越剧")

        assert result is None
        assert session.rolled_back is True
        assert session.committed is False

    @pytest.mark.asyncio
    async def test_extraction_failure_is_swallowed(self) -> None:
        async def _extract(*a: Any, **k: Any) -> list[MemoryCandidate]:
            raise RuntimeError("llm down")

        hook = MemoryTurnHook(llm=object(), session_factory=_FakeSession, extract_memories=_extract)
        result = await hook(elder_id=ELDER_ID, thread_id=THREAD_ID, user_text="你好")

        assert result is None
