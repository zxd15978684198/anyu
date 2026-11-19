"""N43：turn 段状态落库的接线测试。

要证的是三件在线上会咬人的事：

1. **CAS 真的用上了**：先 `read` 拿版本、再带 `expected_version` 写。
   如果实现里写死 `expected_version=0`，第二次写就会永远冲突 —— 表现为「状态只在第一轮生效」，
   而日志里只有一句 warning，很容易被当成偶发。
2. **冲突只重试一次**：第二次再冲突说明中间又插进了更新的一轮，再重试就是在覆盖新状态。
3. **旁路不许连累主链路**：状态中心挂掉（Redis 抖、权限写错）只发 warning，
   `on_asr_final` 照常返回，字幕照常投递。

刻意不测：Lua 脚本本身的原子性（`tests/test_state_version.py` 已用假客户端钉住）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.audio import media_bridge
from app.core.state_store import StateRecord, StateVersionConflict
from app.dialogue_runtime import DialogueRuntime
from app.services.dialogue_service import DialogueReply

ELDER_ID = "11111111-1111-4111-8111-111111111102"
THREAD_ID = "8171f6f6-6ebe-485d-967d-f7f5e5191d4c"


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> DialogueReply:
        self.calls.append(kwargs)
        return DialogueReply(text="今天晴，八度。", degraded=False)


class _FakeCascade:
    device_id = "dev-1"
    trace_id = "trace-1"
    resolved = True
    source = "voice"
    thread_id = THREAD_ID
    candidate_elder_id = ELDER_ID


class _FakeState:
    """按 CAS 语义演一遍：expected 不等于当前版本就抛冲突。"""

    def __init__(self, *, conflicts: int = 0, boom: Exception | None = None) -> None:
        self._versions: dict[str, int] = {}
        self._writes: list[tuple[str, int, dict[str, Any]]] = []
        self._reads = 0
        self._pending_conflicts = conflicts
        self._boom = boom

    async def read(self, key: str) -> StateRecord | None:
        self._reads += 1
        if self._boom is not None and self._boom.__class__ is RuntimeError:
            raise self._boom
        version = self._versions.get(key, 0)
        if version == 0:
            return None
        return StateRecord(key=key, kind="turn", version=version, value={})

    async def write(self, key: str, expected_version: int, value: Any) -> int:
        if self._boom is not None and self._boom.__class__ is not RuntimeError:
            raise self._boom
        if self._pending_conflicts > 0:
            self._pending_conflicts -= 1
            # 有人抢先写了：版本跳到前面，逼调用方重读
            self._versions[key] = expected_version + 5
            raise StateVersionConflict(
                key=key, expected=expected_version, actual=expected_version + 5
            )
        current = self._versions.get(key, 0)
        if expected_version != current:
            raise StateVersionConflict(key=key, expected=expected_version, actual=current)
        self._versions[key] = current + 1
        self._writes.append((key, expected_version, dict(value)))
        return current + 1

    @property
    def writes(self) -> list[tuple[str, int, dict[str, Any]]]:
        return self._writes


def _put_cascade() -> None:
    media_bridge._cascades["dev-1"] = _FakeCascade()  # type: ignore[assignment]


def _patch_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """把音频与投递全部静音，只留状态这一条被测路径。"""
    monkeypatch.setattr("app.dialogue_runtime.signaling.deliver", lambda *a, **k: False)


class TestTurnStateCommit:
    @pytest.mark.asyncio
    async def test_first_write_uses_version_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeState()
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
        )
        _put_cascade()
        _patch_quiet(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "trace-9", "今天天气怎么样")

        assert len(state.writes) == 1
        key, expected, value = state.writes[0]
        assert key == f"turn:{THREAD_ID}"
        assert expected == 0
        assert value["user_text"] == "今天天气怎么样"
        assert value["reply_text"] == "今天晴，八度。"
        assert value["trace_id"] == "trace-9"
        assert value["route"] is None  # 本用例没接路由

    @pytest.mark.asyncio
    async def test_second_turn_reads_current_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """第二次写必须带上第一次的版本号，否则线上只有首轮生效。"""
        state = _FakeState()
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
        )
        _put_cascade()
        _patch_quiet(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "第一句")
        await runtime.on_asr_final("dev-1", "turn-2", "t", "第二句")

        assert [w[1] for w in state.writes] == [0, 1]

    @pytest.mark.asyncio
    async def test_single_conflict_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeState(conflicts=1)
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
        )

        await runtime._commit_turn_state(
            thread_id=THREAD_ID,
            user_text="hi",
            reply_text="hello",
            route="L2",
            trace_id="t",
        )

        assert len(state.writes) == 1, "冲突一次应该重读后写成功"

    @pytest.mark.asyncio
    async def test_double_conflict_gives_up_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _FakeState(conflicts=5)
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
        )

        await runtime._commit_turn_state(
            thread_id=THREAD_ID,
            user_text="hi",
            reply_text="hello",
            route=None,
            trace_id="t",
        )

        assert state.writes == [], "两次都冲突就不该再覆盖"

    @pytest.mark.asyncio
    async def test_state_failure_does_not_break_the_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """状态中心抛错时，这一轮该怎么回还怎么回。"""
        state = _FakeState(boom=RuntimeError("redis down"))
        service = _FakeService()
        runtime = DialogueRuntime(service=service, state=state)  # type: ignore[arg-type]
        _put_cascade()
        _patch_quiet(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert service.calls, "状态写失败不该让对话不发生"

    @pytest.mark.asyncio
    async def test_no_state_store_keeps_baseline(self) -> None:
        runtime = DialogueRuntime(service=_FakeService())  # type: ignore[arg-type]
        await runtime._commit_turn_state(
            thread_id=THREAD_ID,
            user_text="hi",
            reply_text="hello",
            route=None,
            trace_id="t",
        )


class TestWritePermissionSegments:
    @pytest.mark.asyncio
    async def test_runtime_only_touches_turn_kind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """写 cascade/media 段会被 `WRITE_PERMISSIONS` 拒绝 —— 这里钉住「只碰 turn」。"""
        state = _FakeState()
        runtime = DialogueRuntime(
            service=_FakeService(),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
        )
        _put_cascade()
        _patch_quiet(monkeypatch)

        await runtime.on_asr_final("dev-1", "turn-1", "t", "今天天气怎么样")

        assert {key.split(":", 1)[0] for key, _, _ in state.writes} == {"turn"}
