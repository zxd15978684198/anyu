"""`persona_service.get_persona_greeting` 的读口测试（迭代 4 收口 W5 · G-D9）。

这条读口存在的理由写在契约里（`契约记录` 的已知偏离）：
开场白本该来自 `personas.greeting`，代码却一直用 `app/audio/media_bridge.py` 的两个
模块常量。补上读口之后要保证三件事：

1. **能读到**：`elders.persona_id → personas.greeting` 这条链通。
2. **读不到时返 `None` 而不是抛**：开场白缺失只是「换个说法」，不该让平板连不上。
3. **不串人**：`elder_id` 是唯一入参，查的是这一位的 persona，不是别人的。

需要真实 PG（`db_conn` fixture 每测事务回滚），与 `tests/test_identity_cascade.py`
同一套 DB 口径。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.organization import Elder, Persona
from app.services.persona_service import get_persona_greeting, get_persona_voice_id
from conftest import require_db_connection

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """本文件自建 NullPool 引擎（与 `test_identity_cascade.py` 同构）。

    复用 `app.db.engine` 会把上一个用例（上一个事件循环）的连接带进来，取连接时
    报 `'NoneType' object has no attribute 'send'`；NullPool 每用例现连现关。
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_conn(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚；连不上库按 conftest 的统一策略处置。"""
    conn = await require_db_connection(db_engine)

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def db_session(db_conn: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """把夹具那条未提交事务借给被测代码；残留由夹具回滚。"""
    session = AsyncSession(bind=db_conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()


async def _make_persona(session: AsyncSession, *, greeting: str = "您好呀，我是小满。") -> Persona:
    persona = Persona(
        name="小满",
        personality="温和耐心的陪伴者",
        greeting=greeting,
        voice_id="mock",
    )
    session.add(persona)
    await session.flush()
    return persona


async def _make_elder(
    session: AsyncSession, *, persona: Persona | None = None, name: str = "张桂芬"
) -> Elder:
    elder = Elder(name=name, status="active")
    session.add(elder)
    await session.flush()
    if persona is not None:
        elder.persona_id = persona.persona_id
        await session.flush()
    return elder


class TestGreetingLookup:
    async def test_returns_persona_greeting(self, db_session: AsyncSession) -> None:
        persona = await _make_persona(db_session, greeting="您好呀，我是小满，今天感觉怎么样？")
        elder = await _make_elder(db_session, persona=persona)

        assert await get_persona_greeting(db_session, elder_id=elder.elder_id) == (
            "您好呀，我是小满，今天感觉怎么样？"
        )

    async def test_elder_without_persona_returns_none(self, db_session: AsyncSession) -> None:
        """没绑人设：返回 `None`，调用方用通用话术兜底（不是报错）。"""
        elder = await _make_elder(db_session)

        assert await get_persona_greeting(db_session, elder_id=elder.elder_id) is None

    async def test_blank_greeting_returns_none(self, db_session: AsyncSession) -> None:
        """空白开场白按「没配」处理：发一句空字幕比发通用话术更糟。"""
        persona = await _make_persona(db_session, greeting="   ")
        elder = await _make_elder(db_session, persona=persona)

        assert await get_persona_greeting(db_session, elder_id=elder.elder_id) is None

    async def test_unknown_elder_returns_none(self, db_session: AsyncSession) -> None:
        """elder 不存在也返 `None`：这是正常情形（库被清过 / 设备没绑人），不是异常。"""
        assert await get_persona_greeting(db_session, elder_id=uuid.uuid4()) is None

    async def test_does_not_leak_other_elder_greeting(self, db_session: AsyncSession) -> None:
        """两位长者两套人设：查 A 必须拿到 A 的，不许退化成「随便第一条」。"""
        persona_a = await _make_persona(db_session, greeting="A 的开场白")
        persona_b = await _make_persona(db_session, greeting="B 的开场白")
        elder_a = await _make_elder(db_session, persona=persona_a, name="张桂芬")
        elder_b = await _make_elder(db_session, persona=persona_b, name="李国强")

        assert await get_persona_greeting(db_session, elder_id=elder_a.elder_id) == "A 的开场白"
        assert await get_persona_greeting(db_session, elder_id=elder_b.elder_id) == "B 的开场白"

    async def test_strips_whitespace(self, db_session: AsyncSession) -> None:
        """库里的换行/缩进不该原样进 `tts_text`（前端字幕会多出空行）。"""
        persona = await _make_persona(db_session, greeting="  您好呀，我是小满。  \n")
        elder = await _make_elder(db_session, persona=persona)

        assert (
            await get_persona_greeting(db_session, elder_id=elder.elder_id) == "您好呀，我是小满。"
        )


class TestVoiceLookup:
    async def test_returns_persona_voice(self, db_session: AsyncSession) -> None:
        persona = await _make_persona(db_session)
        persona.voice_id = "zh-CN-XiaoyiNeural"
        await db_session.flush()
        elder = await _make_elder(db_session, persona=persona)

        assert await get_persona_voice_id(db_session, elder_id=elder.elder_id) == (
            "zh-CN-XiaoyiNeural"
        )

    async def test_elder_without_persona_returns_none(self, db_session: AsyncSession) -> None:
        elder = await _make_elder(db_session)

        assert await get_persona_voice_id(db_session, elder_id=elder.elder_id) is None

    async def test_blank_voice_returns_none(self, db_session: AsyncSession) -> None:
        persona = await _make_persona(db_session)
        persona.voice_id = "   "
        await db_session.flush()
        elder = await _make_elder(db_session, persona=persona)

        assert await get_persona_voice_id(db_session, elder_id=elder.elder_id) is None

    async def test_does_not_leak_other_elder_voice(self, db_session: AsyncSession) -> None:
        persona_a = await _make_persona(db_session)
        persona_a.voice_id = "zh-CN-XiaoyiNeural"
        persona_b = await _make_persona(db_session)
        persona_b.voice_id = "zh-CN-YunxiNeural"
        await db_session.flush()
        elder_a = await _make_elder(db_session, persona=persona_a, name="张桂芬")
        elder_b = await _make_elder(db_session, persona=persona_b, name="李国强")

        assert await get_persona_voice_id(db_session, elder_id=elder_a.elder_id) == (
            "zh-CN-XiaoyiNeural"
        )
        assert await get_persona_voice_id(db_session, elder_id=elder_b.elder_id) == (
            "zh-CN-YunxiNeural"
        )


class TestGreetingNeverRaises:
    async def test_db_error_is_swallowed(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """读库炸了也只能返 `None`：开场白读不到不许让「平板打开」失败。"""

        async def boom(*args: Any, **kwargs: Any) -> str | None:
            raise RuntimeError("connection reset by peer")

        monkeypatch.setattr("app.services.persona_service._persona_greeting", boom)

        assert await get_persona_greeting(db_session, elder_id=uuid.uuid4()) is None


__all__ = ["TestGreetingLookup", "TestGreetingNeverRaises", "TestVoiceLookup"]
