"""表级测试：`conversation_events`（DAG N10，第 27 张表）。

只验**数据库层**能不能挡住该挡的东西，不验业务（服务层口径另有用例）：

1. 同一 thread 不同 seq 两行都能插进去 —— 事件流的基本形态；
2. 同一 `(thread_id, seq)` 再插一次 → `IntegrityError`（唯一约束生效，服务层据此做幂等）；
3. 不传 `payload` / `occurred_at` → 走 server default：`payload='{}'`、两个时间列都非空；
4. 两个 thread 各自都用 seq=1 互不干扰 —— 唯一性是**按会话**的，不是全局的。

跑法与 `tests/test_pad_offline_api.py` 同构：自建 NullPool 引擎，每用例开一个事务、结束回滚，
所以不脏开发库；用例内做「期望失败」的插入要包在 `begin_nested()` 里，否则一条 IntegrityError
会把整个外层事务打成 aborted，后面的断言就全废了。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import ConversationEvent, Elder
from conftest import require_db_connection

ELDER_ID = uuid.UUID("aaaaaaa0-aaaa-4aaa-8aaa-aaaaaaaaaa01")
THREAD_ID = uuid.UUID("11111110-1111-4111-8111-111111111111")
OTHER_THREAD_ID = uuid.UUID("22222220-2222-4222-8222-222222222222")


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
    """一位长者（`elder_id` 有外键，必须先有主表行）。"""
    await db.execute(
        Elder.__table__.insert().values(
            elder_id=ELDER_ID,
            name="王秀兰",
            status="active",
        )
    )


def _row(
    *,
    thread_id: uuid.UUID = THREAD_ID,
    seq: int,
    event_type: str = "turn_started",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "thread_id": thread_id,
        "elder_id": ELDER_ID,
        "seq": seq,
        "event_type": event_type,
    }
    if payload is not None:
        values["payload"] = payload
    return values


async def _count(db: AsyncConnection, thread_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(ConversationEvent)
        .where(ConversationEvent.thread_id == thread_id)
    )
    return int(result.scalar_one())


async def test_two_rows_same_thread_different_seq_succeed(
    db: AsyncConnection, seeded: None
) -> None:
    await db.execute(ConversationEvent.__table__.insert().values(_row(seq=1)))
    await db.execute(
        ConversationEvent.__table__.insert().values(
            _row(seq=2, event_type="llm_reply", payload={"segment_index": 0})
        )
    )

    rows = (
        await db.execute(
            select(ConversationEvent.seq, ConversationEvent.event_type)
            .where(ConversationEvent.thread_id == THREAD_ID)
            .order_by(ConversationEvent.seq)
        )
    ).all()

    assert [row.seq for row in rows] == [1, 2]
    assert [row.event_type for row in rows] == ["turn_started", "llm_reply"]


async def test_duplicate_thread_seq_raises_integrity_error(
    db: AsyncConnection, seeded: None
) -> None:
    await db.execute(ConversationEvent.__table__.insert().values(_row(seq=1)))

    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(ConversationEvent.__table__.insert().values(_row(seq=1)))

    # 冲突没有把前一行冲掉，也没有多出一条
    assert await _count(db, THREAD_ID) == 1


async def test_defaults_fill_payload_and_timestamps(db: AsyncConnection, seeded: None) -> None:
    await db.execute(ConversationEvent.__table__.insert().values(_row(seq=1)))

    row = (
        await db.execute(
            select(
                ConversationEvent.payload,
                ConversationEvent.occurred_at,
                ConversationEvent.created_at,
                ConversationEvent.trace_id,
            ).where(ConversationEvent.thread_id == THREAD_ID)
        )
    ).one()

    assert row.payload == {}
    assert row.occurred_at is not None
    assert row.created_at is not None
    assert row.trace_id is None


async def test_same_seq_in_another_thread_is_allowed(db: AsyncConnection, seeded: None) -> None:
    await db.execute(ConversationEvent.__table__.insert().values(_row(seq=1)))
    await db.execute(
        ConversationEvent.__table__.insert().values(_row(thread_id=OTHER_THREAD_ID, seq=1))
    )

    assert await _count(db, THREAD_ID) == 1
    assert await _count(db, OTHER_THREAD_ID) == 1
