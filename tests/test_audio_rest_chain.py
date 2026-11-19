"""P0-1 验收：按住说话（`POST /api/audio/utterance`）走**真对话链**，不再是写死回话。

钉住的四件事（2026-11-11，审计判定的 P0-1）：

1. **回复来自对话链**：ASR 终稿进 `run_text_turn` → `DialogueService`，回复里既没有
   「您说的是『…』，我记下了」这种假回话，也不是把 ASR 文本原样抛回去；模型替身被
   真实调用一次是硬证据。
2. **急症句真落库**：说「我胸口疼」走**真路由**（`wire_route`，L0 命中、零模型），
   `risk_events` 里必须真有一行 `safety_alert` 且关联红单——只回一句「已通知」而库
   里没东西，是这轮审计要抓的原始缺陷。
3. **一条链路一份实现**：同一句话分别走 `/api/dialogue/text` 与按住说话，落到
   `DialogueService` 的回复必须逐字相同（两次调用、同一条编排）。
4. **旁路不阻断**：声纹低置信 / 无样本 / 设备未绑长者都只影响响应里的 `identity`
   提示，对话照常给回音；TTS 拿不到就 `audio_url=null` + `tts_degraded=true`。

反向证据：`test_reply_is_not_the_hardcoded_echo`——若有人把 ④ 改回写死模板，
`fake_llm.count` 会变成 0 且回复里出现「您说的是」，该用例必红。
"""

from __future__ import annotations

import io
import struct
import uuid
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import audio as audio_api
from app.api import dialogue as dialogue_api
from app.audio.asr import ASREvent, ASREventKind, ASRResult
from app.audio.tts import TTSUnavailable
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session
from app.models import Device, Elder, Room
from app.models.work_order import RiskEvent, WorkOrder
from app.router.decision import RouteDecision
from app.router.rule_replies import SAFETY_REPLIES
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMReply
from conftest import require_db_connection

# 随机 UUID / 随机设备密钥：共享测试库上不与他人固定主键冲突。
ROOM_ID = uuid.uuid4()
ELDER_ID = uuid.uuid4()
DEVICE_ID = uuid.uuid4()
UNBOUND_DEVICE_ID = uuid.uuid4()

DEVICE_SECRET = "audio-rest-chain-secret-a"  # pragma: allowlist secret
UNBOUND_DEVICE_SECRET = "audio-rest-chain-secret-b"  # pragma: allowlist secret

LLM_TEXT = "我在听，您慢慢说。"
ECHO_PREFIX = "您说的是"


# --------------------------------------------------------------------------- 夹具


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
    """一间房 + 一台绑定长者的设备 + 一台未绑定设备（generic 身份路径）。

    与 `tests/test_dialogue_text_api.py` 同款 Core 插入：`db` 是 AsyncConnection，
    ORM unit-of-work 不保证 rooms 先于 devices 落库，显式按依赖顺序写。
    """
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no=f"AC-{ROOM_ID.hex[:6]}")
    )
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER_ID, name="按住说话测试长者", status="active")
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


class FakeLLM:
    """LLM 替身：`count` 是「这句话真的进过模型」的唯一证据。"""

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


class MockASR:
    """终稿固定的 ASR 替身（与 `test_audio_wav_vad.py` 同款协议）。"""

    def __init__(self, text: str = "今天有点闷") -> None:
        self.text = text
        self.stream_calls = 0

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        self.stream_calls += 1
        return self._events(audio)

    async def _events(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        async for _frame in audio:
            pass
        yield ASREvent(
            kind=ASREventKind.FINAL,
            result=ASRResult(text=self.text, is_final=True, confidence=None),
            error=None,
        )


class MockTTS:
    """返回固定 PCM；`_tts_synthesize` 会把它包成 WAV 落到 static/mock。"""

    async def synthesize(self, text: str) -> bytes:
        return struct.pack("<160h", *([0] * 160))


class UnavailableTTS:
    async def synthesize(self, text: str) -> bytes:
        raise TTSUnavailable("tts disabled in test")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def application(db: AsyncConnection, seeded: None) -> AsyncIterator[Any]:
    """真实 `create_app()` 装配；只把 `get_session` 指到测试事务。"""
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
async def client(application: Any, fake_llm: FakeLLM) -> AsyncIterator[AsyncClient]:
    service = DialogueService(llm=fake_llm)
    application.dependency_overrides[audio_api.get_dialogue_service] = lambda: service
    # 打字入口也指到同一个替身：`test_audio_and_text_share_one_implementation`
    # 要比较两条入口的回复，任何一侧用真 LLM 都会让比较失去意义。
    application.dependency_overrides[dialogue_api.get_dialogue_service] = lambda: service
    # 默认可用的 TTS：回复文本不会被降级前缀污染，断言可以逐字比较。
    # 降级路径由 `test_tts_failure_marks_degraded_and_keeps_the_text_reply` 单独覆盖。
    application.dependency_overrides[audio_api._get_default_tts] = lambda: MockTTS()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _headers(device_id: uuid.UUID = DEVICE_ID, secret: str = DEVICE_SECRET) -> dict[str, str]:
    token = sign_device_session(
        device_id=device_id,
        token_hash_prefix=sha256_hex(secret)[:16],
    )
    return {"Authorization": f"Bearer {token}"}


def _use_asr(application: Any, text: str) -> MockASR:
    asr = MockASR(text)
    application.dependency_overrides[audio_api._get_default_asr] = lambda: asr
    return asr


def _use_tts(application: Any, provider: Any) -> None:
    application.dependency_overrides[audio_api._get_default_tts] = lambda: provider


def _use_route(
    application: Any,
    route: Callable[..., Awaitable[RouteDecision]],
) -> None:
    # 两条入口都换掉：打字入口若仍用真 wire_route，会去等 Laya 超时再降级，
    # 回复文本与音频入口不一致，`test_audio_and_text_share_one_implementation` 会假红。
    application.dependency_overrides[audio_api.get_route_text] = lambda: route
    application.dependency_overrides[dialogue_api.get_route_text] = lambda: route


async def _l1_route(text: str, **kwargs: Any) -> RouteDecision:
    """最普通的 L1 决策：没命中 L0，交模型。"""
    elder = kwargs.get("elder_id")
    return RouteDecision(
        elder_id=str(elder) if elder else "",
        thread_id=str(kwargs.get("thread_id") or "device:-"),
        layer="L1",
        intent="chat",
        confidence=0.92,
        trace_id=kwargs.get("trace_id"),
    )


def _make_wav(num_samples: int = 8_000, *, sample_rate: int = 16_000) -> bytes:
    """标准 WAV（16k / mono / s16），0.5s 静音。"""
    pcm = struct.pack(f"<{num_samples}h", *([0] * num_samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def _post_utterance(
    client: AsyncClient,
    device_id: uuid.UUID = DEVICE_ID,
    secret: str = DEVICE_SECRET,
) -> Any:
    return await client.post(
        "/api/audio/utterance",
        files={"audio": ("utterance.wav", _make_wav(), "audio/wav")},
        headers=_headers(device_id, secret),
    )


async def _risk_events(db: AsyncConnection) -> list[Any]:
    result = await db.execute(
        select(
            RiskEvent.event_id,
            RiskEvent.elder_id,
            RiskEvent.type,
            RiskEvent.severity,
            RiskEvent.detail,
        ).where(RiskEvent.elder_id == ELDER_ID)
    )
    return list(result.all())


async def _work_orders_for(db: AsyncConnection, event_id: uuid.UUID) -> list[Any]:
    result = await db.execute(
        select(WorkOrder.order_no, WorkOrder.type, WorkOrder.status).where(
            WorkOrder.event_id == event_id
        )
    )
    return list(result.all())


# ===========================================================================
# 1 · 回复来自对话链
# ===========================================================================


async def test_utterance_reply_comes_from_the_dialogue_chain(
    application: Any, client: AsyncClient, fake_llm: FakeLLM
) -> None:
    """ASR 终稿 → 模型 → 回复；`text` 仍是 ASR 终稿，`reply.text` 是模型的话。"""
    _use_asr(application, "今天有点闷")
    _use_route(application, _l1_route)

    response = await _post_utterance(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "今天有点闷"
    assert body["reply"]["text"] == LLM_TEXT
    assert fake_llm.count == 1
    prompt_blob = "".join(message["content"] for message in fake_llm.calls[0]["messages"])
    assert "今天有点闷" in prompt_blob, "ASR 终稿必须真的进模型 prompt，而不是只回一句套话"


async def test_reply_is_not_the_hardcoded_echo(
    application: Any, client: AsyncClient, fake_llm: FakeLLM
) -> None:
    """反向证据：写死「您说的是『…』，我记下了」时，本用例必红。"""
    _use_asr(application, "帮我倒杯水")
    _use_route(application, _l1_route)

    response = await _post_utterance(client)

    assert response.status_code == 200, response.text
    reply_text = response.json()["reply"]["text"]
    assert ECHO_PREFIX not in reply_text, f"回复仍是写死模板：{reply_text!r}"
    assert fake_llm.count == 1, "回到写死模板时模型根本不会被调用"


async def test_emergency_utterance_lands_a_risk_event(
    application: Any, client: AsyncClient, db: AsyncConnection, fake_llm: FakeLLM
) -> None:
    """急症句：真路由命中 → 零模型 → risk_events + 红单都真落库。"""
    _use_asr(application, "我胸口疼")
    # 不覆盖 route：用真 `wire_route`（L0 安全规则先于一切）

    response = await _post_utterance(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["text"] == SAFETY_REPLIES["safety_emergency"]
    assert fake_llm.count == 0, "急症命中后不得再问模型"

    events = await _risk_events(db)
    assert len(events) == 1, "急症必须真落一条 risk_event，而不是只回一句已通知"
    event = events[0]
    assert event.type == "safety_alert"
    assert event.severity == "red"
    assert event.detail is not None, "建了风险事件但没填命中证据（detail 为 NULL）"
    assert event.detail["rule_id"] == "safety_emergency"

    orders = await _work_orders_for(db, event.event_id)
    assert len(orders) == 1
    assert orders[0].type == "safety_alert"


async def test_audio_and_text_share_one_implementation(
    application: Any, client: AsyncClient, fake_llm: FakeLLM
) -> None:
    """同一句话走两条入口，落到服务层的回复逐字相同（同一条编排，不是两份实现）。"""
    _use_asr(application, "今天有点闷")
    _use_route(application, _l1_route)

    text_response = await client.post(
        "/api/dialogue/text",
        json={"text": "今天有点闷"},
        headers=_headers(),
    )
    audio_response = await _post_utterance(client)

    assert text_response.status_code == 200, text_response.text
    assert audio_response.status_code == 200, audio_response.text
    assert audio_response.json()["reply"]["text"] == text_response.json()["reply"]["text"]
    assert fake_llm.count == 2


# ===========================================================================
# 2 · 身份是旁路：不阻断、不签发
# ===========================================================================


async def test_unregistered_voiceprint_is_reported_but_does_not_block(
    application: Any, client: AsyncClient, fake_llm: FakeLLM
) -> None:
    """绑定了长者但没注册声纹：identity=no_samples，对话照常。"""
    _use_asr(application, "今天有点闷")
    _use_route(application, _l1_route)

    response = await _post_utterance(client)

    assert response.status_code == 200, response.text
    body = response.json()
    identity = body["identity"]
    assert identity is not None
    assert identity["source"] == "device"
    assert identity["matched"] is False
    assert identity["reason"] == "no_samples"
    assert identity["elder_id"] == str(ELDER_ID)
    assert body["reply"]["text"] == LLM_TEXT, "没认出声音也必须给回音"


async def test_unbound_device_reports_generic_and_still_replies(
    application: Any, client: AsyncClient, fake_llm: FakeLLM
) -> None:
    _use_asr(application, "有人吗")
    _use_route(application, _l1_route)

    response = await _post_utterance(client, UNBOUND_DEVICE_ID, UNBOUND_DEVICE_SECRET)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity"] is not None
    assert body["identity"]["source"] == "generic"
    assert body["identity"]["reason"] == "elder_unbound"
    assert body["identity"]["elder_id"] is None
    assert body["reply"]["text"] == LLM_TEXT


# ===========================================================================
# 3 · TTS：拿得到就返回，拿不到就降级标记
# ===========================================================================


async def test_tts_failure_marks_degraded_and_keeps_the_text_reply(
    application: Any, client: AsyncClient
) -> None:
    _use_asr(application, "今天有点闷")
    _use_route(application, _l1_route)
    _use_tts(application, UnavailableTTS())

    response = await _post_utterance(client)

    assert response.status_code == 200, response.text
    reply = response.json()["reply"]
    assert reply["audio_url"] is None
    assert reply["tts_degraded"] is True
    assert reply["text"].startswith("抱歉，语音播报暂时不可用")


async def test_tts_success_returns_audio_url(application: Any, client: AsyncClient) -> None:
    _use_asr(application, "今天有点闷")
    _use_route(application, _l1_route)

    response = await _post_utterance(client)

    assert response.status_code == 200, response.text
    reply = response.json()["reply"]
    assert reply["tts_degraded"] is False
    assert reply["audio_url"] is not None
    assert reply["audio_url"].startswith("/static/mock/")


# ===========================================================================
# 4 · ASR 终稿为空：不让空文本进对话链
# ===========================================================================


async def test_blank_asr_final_is_treated_as_unavailable(
    application: Any, client: AsyncClient, fake_llm: FakeLLM
) -> None:
    _use_asr(application, "   ")

    response = await _post_utterance(client)

    assert response.status_code == 503
    assert response.json()["detail"] == "抱歉，语音识别暂时不可用。您可以直接输入文字，或稍后再试。"
    assert fake_llm.count == 0, "空文本不得进模型"
