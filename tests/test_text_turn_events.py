"""打字链路的事件流与长期记忆写回（2026-11-11 · task-9）。

钉住的契约（都是「接错了不报错、只会静默少记」的那一类）：

1. **顺序**：一轮打字必须落 `user_said` → `assistant_said`。反了的话回放会看到
   「先答后问」，而回放正是断线续传与口径审计的输入。
2. **旁路性**：事件流写不进去、记忆提取抛错，**都不能**让这一轮没有回音。
   长者要的是回音，不是把一次数据库抖动变成一次说不出话。
3. **不出错的主人**：generic 身份（设备未绑长者）一条事件都不写——事件表
   `elder_id` 有外键，写不存在的长者会炸，「无主事件」也没有回放价值。
4. **线程号是 UUID**：`conversation_events.thread_id` 是 `uuid.UUID NOT NULL`，
   而打字链路的线程号是 `device:<uuid>` 字符串。必须走确定性派生（`uuid5`），
   否则每打一次字就 `ValueError` 一次，且同一台设备的事件会被拆成碎片。
5. **长期记忆不阻塞回话**：写回是后台任务（`asyncio.create_task`），本轮不 await，
   且必须留强引用（async 任务只持弱引用，不留会被 GC 静默回收）。
6. **送达证据不伪造**：`/api/dialogue/tts` 成功写 `response_audio_rendered`
   （服务端已把音频交给前端），**不是** `response_segment_delivered`
   （那个要求服务端亲手写播放器，只有话音链路做得到）。降级不写。

刻意不测：`conversation_events` 的真库取号/冲突（见
`tests/test_conversation_event_service.py`）、话音链路的顺序（见
`tests/test_conversation_event_wiring.py`）、`memory_writeback` 的落库细节
（见 `tests/test_memory_writeback.py`）。本文件的替身边界就是「只换外设」。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import dialogue as dialogue_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session
from app.models import Device, Elder, Room
from app.router.decision import RouteDecision
from app.services import text_turn_service
from app.services.dialogue_service import DialogueReply, DialogueService
from app.services.llm_service import LLMReply
from conftest import require_db_connection

# 随机 UUID：共享测试库上不与他人固定 ID 抢主键；断言只按这些引用。
ROOM_ID = uuid.uuid4()
ELDER_ID = uuid.uuid4()
DEVICE_ID = uuid.uuid4()
UNBOUND_DEVICE_ID = uuid.uuid4()

DEVICE_SECRET = "text-turn-events-secret-a"  # pragma: allowlist secret
UNBOUND_DEVICE_SECRET = "text-turn-events-secret-b"  # pragma: allowlist secret

LLM_TEXT = "我在听，您慢慢说。"


# ===========================================================================
# 替身
# ===========================================================================


class _RecordingRecorder:
    """`ConversationEventRecorder` 的最小接口替身：只记调用，可注入失败。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def record(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return True

    @property
    def event_types(self) -> list[str]:
        return [call["event_type"] for call in self.calls]


class _RecordingMemoryHook:
    """`MemoryTurnHook` 替身：记下每次被调用的入参，可注入失败。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class _FakeService:
    """`DialogueService` 的最小替身：只关心「谁在说、说了什么」。"""

    def __init__(self, text: str = LLM_TEXT, *, degraded: bool = False) -> None:
        self.reply = DialogueReply(text=text, degraded=degraded)
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> DialogueReply:
        self.calls.append(kwargs)
        return self.reply


class FakeLLM:
    """LLM 替身（`DialogueService` 在 L1/L2 之外的回落路径要用）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def count(self) -> int:
        return len(self.calls)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        self.calls.append({"messages": messages, "trace_id": trace_id})
        return LLMReply(text=LLM_TEXT, degraded=False)


class FakeTTS:
    """`TTSProvider` 替身：返回固定 PCM 或抛指定异常。"""

    name = "fake-tts"

    def __init__(self, *, pcm: bytes = b"\x00" * 6400, error: BaseException | None = None) -> None:
        self.pcm = pcm
        self.error = error
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.pcm


async def _plain_route(text: str, **kwargs: Any) -> Any:
    """最普通的 L1 决策（不命中 L0，交模型）。

    用**真的** `RouteDecision` 而不是手搓鸭子对象：这一层还会被
    `decision_log_service.decision_to_log` 消费，字段少一个就 AttributeError，
    用真类型才不会把「替身少字段」误读成业务 bug。
    """
    elder = kwargs.get("elder_id")
    return RouteDecision(
        elder_id=str(elder) if elder else "",
        thread_id=str(kwargs.get("thread_id") or "device:-"),
        layer="L1",
        intent="chat",
        confidence=0.92,
        trace_id=kwargs.get("trace_id"),
    )


# ===========================================================================
# 一、run_text_turn 的事件流与记忆写回（纯服务层，不碰 HTTP）
# ===========================================================================


class TestTextTurnEventStream:
    @pytest.mark.asyncio
    async def test_writes_user_said_then_assistant_said(self) -> None:
        service = _FakeService("今天晴，气温八度。")
        recorder = _RecordingRecorder()

        await text_turn_service.run_text_turn(
            _NoSession(),  # type: ignore[arg-type]
            text="今天天气怎么样",
            elder_text=str(ELDER_ID),
            thread_id="device:" + str(DEVICE_ID),
            trace_id="trace-1",
            service=service,  # type: ignore[arg-type]
            route_text=_plain_route,
            recorder=recorder,  # type: ignore[arg-type]
        )

        assert recorder.event_types == ["user_said", "assistant_said"], (
            "顺序反了回放会看到「先答后问」；各写几条也要管够"
        )
        assert recorder.calls[0]["payload"]["text"] == "今天天气怎么样"
        assert recorder.calls[1]["payload"]["text"] == "今天晴，气温八度。"
        assert recorder.calls[1]["payload"]["degraded"] is False

    @pytest.mark.asyncio
    async def test_generic_identity_writes_nothing(self) -> None:
        """设备未绑长者：一条事件都不写（事件表 elder_id 有外键，写不出来）。"""
        recorder = _RecordingRecorder()

        await text_turn_service.run_text_turn(
            _NoSession(),  # type: ignore[arg-type]
            text="今天天气怎么样",
            elder_text=None,
            thread_id="device:" + str(UNBOUND_DEVICE_ID),
            trace_id="trace-2",
            service=_FakeService(),  # type: ignore[arg-type]
            route_text=_plain_route,
            recorder=recorder,  # type: ignore[arg-type]
        )

        assert recorder.calls == [], "generic 身份没有可归属的长者，不许写事件"

    @pytest.mark.asyncio
    async def test_recorder_error_does_not_break_reply(self) -> None:
        """事件流炸了，回复照常返回——旁路不许升级成主链路故障。"""
        recorder = _RecordingRecorder(error=RuntimeError("db down"))

        turn = await text_turn_service.run_text_turn(
            _NoSession(),  # type: ignore[arg-type]
            text="今天天气怎么样",
            elder_text=str(ELDER_ID),
            thread_id="device:" + str(DEVICE_ID),
            trace_id="trace-3",
            service=_FakeService("照常回答"),  # type: ignore[arg-type]
            route_text=_plain_route,
            recorder=recorder,  # type: ignore[arg-type]
        )

        assert turn.reply.text == "照常回答"
        assert turn.reply.degraded is False

    @pytest.mark.asyncio
    async def test_no_recorder_keeps_baseline(self) -> None:
        """没装 recorder（迭代 5 的调用点）：行为与接线前逐字一致。"""
        turn = await text_turn_service.run_text_turn(
            _NoSession(),  # type: ignore[arg-type]
            text="今天天气怎么样",
            elder_text=str(ELDER_ID),
            thread_id="device:" + str(DEVICE_ID),
            trace_id="trace-4",
            service=_FakeService(),  # type: ignore[arg-type]
            route_text=_plain_route,
        )

        assert turn.reply.text == LLM_TEXT


class TestTextTurnMemoryWriteback:
    @pytest.mark.asyncio
    async def test_memory_hook_called_once_with_reply_text(self) -> None:
        hook = _RecordingMemoryHook()

        await text_turn_service.run_text_turn(
            _NoSession(),  # type: ignore[arg-type]
            text="我对青霉素过敏",
            elder_text=str(ELDER_ID),
            thread_id="device:" + str(DEVICE_ID),
            trace_id="trace-5",
            service=_FakeService("记住了，您对青霉素过敏。"),  # type: ignore[arg-type]
            route_text=_plain_route,
            memory_hook=hook,  # type: ignore[arg-type]
        )
        await _drain_memory_tasks()

        assert len(hook.calls) == 1, "一轮只该写回一次"
        call = hook.calls[0]
        assert call["elder_id"] == str(ELDER_ID)
        assert call["user_text"] == "我对青霉素过敏"
        assert call["assistant_text"] == "记住了，您对青霉素过敏。", (
            "写回的助手侧必须是**实际回复**，不是用户原话——否则提取会把自己说的话当成长者陈述"
        )

    @pytest.mark.asyncio
    async def test_memory_hook_error_does_not_break_reply(self) -> None:
        hook = _RecordingMemoryHook(error=RuntimeError("extract boom"))

        turn = await text_turn_service.run_text_turn(
            _NoSession(),  # type: ignore[arg-type]
            text="我对青霉素过敏",
            elder_text=str(ELDER_ID),
            thread_id="device:" + str(DEVICE_ID),
            trace_id="trace-6",
            service=_FakeService("记住了。"),  # type: ignore[arg-type]
            route_text=_plain_route,
            memory_hook=hook,  # type: ignore[arg-type]
        )
        await _drain_memory_tasks()

        assert turn.reply.text == "记住了。"

    @pytest.mark.asyncio
    async def test_generic_identity_never_calls_memory_hook(self) -> None:
        hook = _RecordingMemoryHook()

        await text_turn_service.run_text_turn(
            _NoSession(),  # type: ignore[arg-type]
            text="我对青霉素过敏",
            elder_text=None,
            thread_id="device:" + str(UNBOUND_DEVICE_ID),
            trace_id="trace-7",
            service=_FakeService(),  # type: ignore[arg-type]
            route_text=_plain_route,
            memory_hook=hook,  # type: ignore[arg-type]
        )
        await _drain_memory_tasks()

        assert hook.calls == [], "generic 身份连会话都不该开，更不该写别人的记忆"


class TestThreadIdDerivation:
    def test_same_device_derives_same_uuid(self) -> None:
        first = text_turn_service.db_thread_id_for_device(str(DEVICE_ID))
        second = text_turn_service.db_thread_id_for_device(str(DEVICE_ID))

        assert first == second, "同一台设备必须落在同一条事件流上，否则回放会把对话拆碎"
        assert isinstance(first, uuid.UUID)

    def test_different_devices_derive_different_uuids(self) -> None:
        assert text_turn_service.db_thread_id_for_device(
            str(DEVICE_ID)
        ) != text_turn_service.db_thread_id_for_device(str(UNBOUND_DEVICE_ID))

    def test_thread_id_from_text_handles_three_forms(self) -> None:
        # 本身就是 UUID（话音链路 conversation_threads 给的那种）→ 原样
        raw = uuid.uuid4()
        assert text_turn_service.thread_id_from_text(str(raw)) == raw
        # device:<uuid> → 与 db_thread_id_for_device 逐字一致
        assert text_turn_service.thread_id_from_text(f"device:{DEVICE_ID}") == (
            text_turn_service.db_thread_id_for_device(str(DEVICE_ID))
        )
        # 转不了 → None（宁可不记，也不写错主人）
        assert text_turn_service.thread_id_from_text("") is None
        assert text_turn_service.thread_id_from_text("some-random-thread") is None


# ===========================================================================
# 二、HTTP 层：/api/dialogue/text 与 /api/dialogue/tts
# ===========================================================================


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """一间房、一位长者、两台设备（一台绑定、一台未绑定）。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no=f"TTE-{ROOM_ID.hex[:6]}")
    )
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER_ID, name="打字事件测试长者", status="active")
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": DEVICE_ID,
                "room_id": ROOM_ID,
                "elder_id": ELDER_ID,
                "token_hash": sha256_hex(DEVICE_SECRET),
                "status": "offline",
            },
            {
                "device_id": UNBOUND_DEVICE_ID,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(UNBOUND_DEVICE_SECRET),
                "status": "offline",
            },
        ],
    )


@pytest.fixture
async def application(db: AsyncConnection, seeded: None) -> AsyncIterator[Any]:
    from app.main import create_app

    app = create_app(get_settings())

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield app


@pytest.fixture
async def client(application: Any) -> AsyncIterator[AsyncClient]:
    fake_llm = FakeLLM()
    application.dependency_overrides[dialogue_api.get_dialogue_service] = lambda: DialogueService(
        llm=fake_llm
    )
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _headers(device_id: uuid.UUID = DEVICE_ID, secret: str = DEVICE_SECRET) -> dict[str, str]:
    token = sign_device_session(
        device_id=device_id,
        token_hash_prefix=sha256_hex(secret)[:16],
    )
    return {"Authorization": f"Bearer {token}"}


class TestDialogueTextApiWiring:
    @pytest.mark.asyncio
    async def test_text_turn_writes_events_through_app_state(self, application: Any) -> None:
        """走真实装配：`app.state.conversation_recorder` 里的记录器必须被调用。"""
        recorder = _RecordingRecorder()
        application.state.conversation_recorder = recorder
        application.state.memory_hook = None

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                "/api/dialogue/text", json={"text": "今天天气怎么样"}, headers=_headers()
            )

        assert response.status_code == 200, response.text
        assert recorder.event_types == ["user_said", "assistant_said"]

    @pytest.mark.asyncio
    async def test_unbound_device_writes_no_events(self, application: Any) -> None:
        recorder = _RecordingRecorder()
        application.state.conversation_recorder = recorder
        application.state.memory_hook = None

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                "/api/dialogue/text",
                json={"text": "今天天气怎么样"},
                headers=_headers(UNBOUND_DEVICE_ID, UNBOUND_DEVICE_SECRET),
            )

        assert response.status_code == 200, response.text
        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_missing_app_state_does_not_break_endpoint(self, application: Any) -> None:
        """lifespan 没装到（属性不存在）也要照常回话，不许 500。"""
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                "/api/dialogue/text", json={"text": "今天天气怎么样"}, headers=_headers()
            )

        assert response.status_code == 200, response.text
        assert response.json()["reply"]["text"]


class TestDialogueTtsApiEvent:
    @pytest.mark.asyncio
    async def test_success_writes_response_audio_rendered(self, application: Any) -> None:
        recorder = _RecordingRecorder()
        application.state.conversation_recorder = recorder
        application.dependency_overrides[dialogue_api.get_tts] = lambda: FakeTTS()

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                "/api/dialogue/tts", json={"text": "今天有雨，记得带伞。"}, headers=_headers()
            )

        assert response.status_code == 200, response.text
        assert recorder.event_types == ["response_audio_rendered"], (
            "合成成功只该写这一条：它不主张「长者听到了」"
        )
        payload = recorder.calls[0]["payload"]
        assert set(payload) >= {"audio_url", "duration_ms", "segments", "chars"}
        assert payload["duration_ms"] == 200
        assert recorder.calls[0]["thread_id"] == text_turn_service.db_thread_id_for_device(
            str(DEVICE_ID)
        ), "必须与 /text 那条事件落在同一条流上，否则串不起一来一回"

    @pytest.mark.asyncio
    async def test_degraded_writes_nothing(self, application: Any) -> None:
        """降级（合成失败）没有「已交付的声音」，不许写 rendered。"""
        recorder = _RecordingRecorder()
        application.state.conversation_recorder = recorder
        application.dependency_overrides[dialogue_api.get_tts] = lambda: FakeTTS(
            error=RuntimeError("tts boom")
        )

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                "/api/dialogue/tts", json={"text": "今天有雨。"}, headers=_headers()
            )

        assert response.status_code == 200, response.text
        assert response.json()["degraded"] is True
        assert recorder.calls == []


# ===========================================================================
# 辅助
# ===========================================================================


class _NoSession:
    """`AsyncSession` 的最简替身：本文件的路径不用 session（告警未命中）。"""

    async def rollback(self) -> None:  # pragma: no cover - 只在异常路径被调
        return None


async def _drain_memory_tasks() -> None:
    """等已经排进事件循环的记忆任务跑完。

    `_schedule_memory_writeback` 用的是 `asyncio.create_task`，函数返回时任务只是**排上了**，
    还没执行。测试要断言「hook 被调用过」，必须让一次调度落地——但**不能**在
    业务代码里 await（那就是把 2–4 秒的提取塞进 1.5 秒预算）。
    """
    tasks = [task for task in text_turn_service._MEMORY_TASKS if not task.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
