"""会话事件流唯一写入口（DAG N20 / 第 27 张表）的定向用例。

## 钉的是什么

1. **seq 从 1 起、同会话内连续**：写一条是 1，再写是 2；批量三条是 1,2,3——批量必须是
   **一次取号**，逐行 `MAX()+1` 在并发下必然重号（用例 2 与用例 10 分别从静态和并发两侧钉住）；
2. **并发不重号**：两个连接各自 `append` 后 `commit`，最终 `seq` 集合必须是 {n+1, n+2}。
   这条用例会**真的提交**（咨询锁要跨事务才成立），所以在 `finally` 里按 `thread_id` /
   `elder_id` 精确清掉自己造的两行，不碰任何既有数据；
3. **非法输入一行都不写**：`event_type` 不在白名单、naive `occurred_at`，抛 `ValueError`
   且 `count` 不变——「先校验后落库」是可断言的契约，不是口头承诺；
4. **闸门②**：`list_events` / `latest_seq` 必须同时按 `elder_id` 过滤。用例 7 故意用**同一个
   `thread_id`** 挂到两个长者名下，证明「只按 thread_id 查」的口子确实不存在。

## 夹具

真库用例用 conftest 的 `db_conn`（每用例一个事务、结束回滚），外加 `_release_pool` 处置
P-03（模块级引擎池跨事件循环复用会报 `Event loop is closed`）。
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.services.conversation_event_service as event_service
from app.db import engine as app_engine
from app.models import Elder
from app.models.conversation_event import ConversationEvent
from app.services.conversation_event_service import (
    EVENT_TYPES,
    NewEvent,
    append_event,
    append_events,
    latest_seq,
    list_events,
)

# ============================================================
# 夹具与小工具
# ============================================================


@pytest.fixture(autouse=True)
async def _release_pool() -> AsyncIterator[None]:
    """每个用例结束把 `app.db.engine` 池里的连接还掉（仓库已登记的 P-03 坑）。"""
    yield
    await app_engine.dispose()


def _session(conn: AsyncConnection) -> AsyncSession:
    return AsyncSession(bind=conn, expire_on_commit=False)


async def _seed_elder(conn: AsyncConnection) -> uuid.UUID:
    """`conversation_events.elder_id` 的外键目标；占位姓名，不写真实长者数据（AGENTS §4.3）。"""
    elder_id = uuid.uuid4()
    await conn.execute(Elder.__table__.insert().values(elder_id=elder_id, name="测试长者"))
    return elder_id


async def _count(session: AsyncSession, thread_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(ConversationEvent)
        .where(ConversationEvent.thread_id == thread_id)
    )
    return int((await session.execute(stmt)).scalar_one())


def _new_thread() -> uuid.UUID:
    return uuid.uuid4()


# ============================================================
# seq 分配
# ============================================================


async def test_append_event_assigns_one_then_two(db_conn: AsyncConnection) -> None:
    """第一条 1、第二条 2；返回的行已经 flush 过（seq / 服务端默认列都不为 None）。"""
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    first = await append_event(
        session,
        thread_id=thread_id,
        elder_id=elder_id,
        event_type="user_said",
        payload={"text": "我想听越剧"},
        trace_id="trace-n20-1",
    )
    second = await append_event(
        session,
        thread_id=thread_id,
        elder_id=elder_id,
        event_type="asr_final",
        payload={"text": "我想听越剧", "confidence": 0.91},
    )

    assert (first.seq, second.seq) == (1, 2)
    # 服务端默认值（event_id / occurred_at / created_at）在 flush 后就能读到，调用方不必 refresh
    assert first.event_id is not None
    assert first.occurred_at is not None
    assert first.created_at is not None
    assert first.payload == {"text": "我想听越剧"}
    assert first.trace_id == "trace-n20-1"
    assert await latest_seq(session, thread_id=thread_id, elder_id=elder_id) == 2


async def test_append_events_uses_one_contiguous_range(db_conn: AsyncConnection) -> None:
    """批量三条 → 1,2,3，且只取一次号（`latest_seq` 立刻是 3）。"""
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    rows = await append_events(
        session,
        thread_id=thread_id,
        elder_id=elder_id,
        events=[
            NewEvent(event_type="asr_partial", payload={"text": "我"}),
            NewEvent(event_type="asr_partial", payload={"text": "我想"}),
            NewEvent(event_type="user_said", payload={"text": "我想听越剧"}),
        ],
    )

    assert [row.seq for row in rows] == [1, 2, 3]
    assert await latest_seq(session, thread_id=thread_id, elder_id=elder_id) == 3
    assert await _count(session, thread_id) == 3


async def test_empty_batch_is_a_no_op(db_conn: AsyncConnection) -> None:
    """空批量返回 `[]`，不来一把没人用的锁、也不写行。"""
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    assert await append_events(session, thread_id=thread_id, elder_id=elder_id, events=[]) == []
    assert await _count(session, thread_id) == 0


async def test_latest_seq_is_zero_for_an_empty_thread(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)

    session = _session(db_conn)
    assert await latest_seq(session, thread_id=_new_thread(), elder_id=elder_id) == 0


async def test_stale_seq_collision_is_retried_inside_a_savepoint(
    db_conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取号竞态（拿到已占用的号）→ 撞唯一约束 → 重取一次，**不连坐调用方事务**。

    这里把内部取号函数临时改成「第一次故意返回 1」（而 1 已被占用），模拟一个绕过咨询锁
    的写入抢先落库的现场：第一次 flush 撞 `uq_conversation_events_thread_id_seq`，服务层
    回滚到 SAVEPOINT 后重新取号。断言点有两个——最终 seq 是 2，且**先前那条 seq=1 还在**
    （若实现改成 `session.rollback()`，先前那条会被一起回滚，本用例立刻红）。
    """
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    kept = await append_event(
        session, thread_id=thread_id, elder_id=elder_id, event_type="user_said"
    )
    assert kept.seq == 1

    real_allocate = event_service._allocate_seq_start
    calls = {"count": 0}

    async def stale_once(session_: AsyncSession, thread_id_: uuid.UUID) -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            return 1  # 故意撞已经落库的 seq=1
        return await real_allocate(session_, thread_id_)

    monkeypatch.setattr(event_service, "_allocate_seq_start", stale_once)

    row = await append_event(
        session, thread_id=thread_id, elder_id=elder_id, event_type="assistant_said"
    )

    assert calls["count"] == 2, "必须重试一次，而不是把 IntegrityError 直接抛给调用方"
    assert row.seq == 2
    assert await _count(session, thread_id) == 2, "调用方事务里先前那条不能被一起回滚"


# ============================================================
# 回放
# ============================================================


async def test_list_events_returns_only_rows_after_cursor(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    await append_events(
        session,
        thread_id=thread_id,
        elder_id=elder_id,
        events=[
            NewEvent(event_type="user_said", payload={"text": "一"}),
            NewEvent(event_type="assistant_said", payload={"text": "二"}),
            NewEvent(event_type="user_said", payload={"text": "三"}),
        ],
    )

    rows = await list_events(session, thread_id=thread_id, elder_id=elder_id, after_seq=1)

    assert [row.seq for row in rows] == [2, 3]
    assert [row.event_type for row in rows] == ["assistant_said", "user_said"]
    # 游标是「严格大于」：after_seq=3 之后没有更多
    assert await list_events(session, thread_id=thread_id, elder_id=elder_id, after_seq=3) == []


async def test_list_events_respects_limit(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    await append_events(
        session,
        thread_id=thread_id,
        elder_id=elder_id,
        events=[NewEvent(event_type="asr_partial", payload={"i": index}) for index in range(5)],
    )

    rows = await list_events(session, thread_id=thread_id, elder_id=elder_id, limit=2)

    assert [row.seq for row in rows] == [1, 2]


async def test_queries_are_scoped_by_elder_not_just_thread(db_conn: AsyncConnection) -> None:
    """闸门②：同一个 `thread_id` 挂在两个长者名下时，各自的查询只看得到自己的行。

    真实链路里 `thread_id` 与长者是一对一的，但服务层不能把这个不变量当成防御——一旦
    上游传错/复用线程，只按 `thread_id` 过滤就会读出别人的会话事件。
    """
    elder_a = await _seed_elder(db_conn)
    elder_b = await _seed_elder(db_conn)
    shared_thread = _new_thread()
    session = _session(db_conn)

    await append_event(session, thread_id=shared_thread, elder_id=elder_a, event_type="user_said")
    await append_event(
        session, thread_id=shared_thread, elder_id=elder_b, event_type="assistant_said"
    )

    rows_a = await list_events(session, thread_id=shared_thread, elder_id=elder_a)
    rows_b = await list_events(session, thread_id=shared_thread, elder_id=elder_b)

    assert [row.elder_id for row in rows_a] == [elder_a]
    assert [row.elder_id for row in rows_b] == [elder_b]

    # seq 是「每个 thread 一条序列」，不是每个长者一条：两行分别拿了 1 和 2
    # （`UNIQUE(thread_id, seq)` 就是这么定的），所以 elder_b 的最后一条是 2。
    assert await latest_seq(session, thread_id=shared_thread, elder_id=elder_a) == 1
    assert await latest_seq(session, thread_id=shared_thread, elder_id=elder_b) == 2

    # 游标也按长者隔离：拿 elder_a 的最新游标回放 elder_b，仍能读到它自己的那条
    assert [
        row.seq
        for row in await list_events(
            session, thread_id=shared_thread, elder_id=elder_b, after_seq=1
        )
    ] == [2]
    # 反过来，elder_a 在 1 之后没有自己的行了——不会因为 elder_b 的 2 而多出一条
    assert await list_events(session, thread_id=shared_thread, elder_id=elder_a, after_seq=1) == []


# ============================================================
# 拒绝：非法输入一行都不落库
# ============================================================


async def test_invalid_event_type_is_rejected_without_writing(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    with pytest.raises(ValueError, match="event_type must be one of"):
        await append_event(
            session,
            thread_id=thread_id,
            elder_id=elder_id,
            event_type="llm_reply",  # 讲义里的占位名，不在九取值白名单里
        )

    assert await _count(session, thread_id) == 0


async def test_invalid_event_in_a_batch_rejects_the_whole_batch(db_conn: AsyncConnection) -> None:
    """批量里有一条非法 → 整批拒绝（不允许前两条先落库、第三条才报错）。"""
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    with pytest.raises(ValueError, match="event_type must be one of"):
        await append_events(
            session,
            thread_id=thread_id,
            elder_id=elder_id,
            events=[
                NewEvent(event_type="user_said"),
                NewEvent(event_type="tts_first_packet"),  # 非法
            ],
        )

    assert await _count(session, thread_id) == 0


async def test_naive_occurred_at_is_rejected(db_conn: AsyncConnection) -> None:
    """naive 时间一律拒绝（AGENTS §8：存储一律 UTC，不靠会话时区猜）。"""
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)

    with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
        await append_event(
            session,
            thread_id=thread_id,
            elder_id=elder_id,
            event_type="user_said",
            occurred_at=datetime.datetime(2026, 11, 10, 12, 0, 0),
        )

    assert await _count(session, thread_id) == 0


async def test_timezone_aware_occurred_at_is_kept(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    thread_id = _new_thread()
    session = _session(db_conn)
    when = datetime.datetime(2026, 11, 10, 4, 0, 0, tzinfo=datetime.UTC)

    row = await append_event(
        session,
        thread_id=thread_id,
        elder_id=elder_id,
        event_type="asr_final",
        occurred_at=when,
    )

    assert row.occurred_at == when


def test_event_type_whitelist_is_frozen() -> None:
    """十个取值逐字冻结：少一个会让真实链路在写入时炸，多一个会让占位类型混进事件流。

    `response_audio_rendered` 是 2026-11-11 产品负责人裁决新增的第十个（打字链路「服务端已把
    音频交给前端」）。它与 `response_segment_delivered`（服务端亲手写播放器）**不可互换**
    ——后者是可验证的送达，前者只是交付，冒充就是伪造事实。加值本身不需要迁移：
    `alembic/versions/0010_conversation_events.py` 明确 DDL 不设 `event_type` CHECK。
    """
    assert (
        frozenset(
            {
                "user_said",
                "asr_partial",
                "asr_final",
                "assistant_said",
                "tool_called",
                "tool_result",
                "response_segment_delivered",
                "response_audio_rendered",
                "interrupted",
                "turn_failed",
            }
        )
        == EVENT_TYPES
    )


# ============================================================
# 真并发：两个连接、各自提交，最终序号不得重叠
# ============================================================


async def _run_two_committed_appends(elder_id: uuid.UUID, thread_id: uuid.UUID) -> list[Any]:
    """两个独立连接在同一个起跑线上各写一条并 commit，返回两行的 seq。"""
    url = app_engine.url
    engines = [create_async_engine(url, poolclass=NullPool) for _ in range(2)]
    started = asyncio.Event()

    async def _job(engine: Any) -> int:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await started.wait()
            row = await append_event(
                session,
                thread_id=thread_id,
                elder_id=elder_id,
                event_type="user_said",
                payload={"text": "并发写入"},
            )
            await session.commit()
            return row.seq

    try:
        tasks = [asyncio.create_task(_job(engine)) for engine in engines]
        await asyncio.sleep(0)
        started.set()
        return await asyncio.gather(*tasks)
    finally:
        for engine in engines:
            await engine.dispose()


async def test_two_concurrent_appends_do_not_share_a_seq() -> None:
    """两笔并发提交：`seq` 必须是 {1, 2}（咨询锁串行化），且库里没有第三行。

    这条用例**会真提交**——事务级咨询锁只有在事务边界上才有意义，把两边塞进同一个
    `db_conn` 事务里测不到任何东西。所以自己造一个一次性长者 + 随机 `thread_id`，
    在 `finally` 里按 id 精确删除（`conversation_events` 对 `elders` 是 ON DELETE CASCADE）。
    """
    elder_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    setup_engine = create_async_engine(app_engine.url, poolclass=NullPool)

    try:
        async with setup_engine.begin() as conn:
            await conn.execute(
                Elder.__table__.insert().values(elder_id=elder_id, name="并发测试长者")
            )

        seqs = await _run_two_committed_appends(elder_id, thread_id)
        assert sorted(seqs) == [1, 2], f"并发写入重号了：{seqs}"

        async with setup_engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(ConversationEvent.seq, ConversationEvent.elder_id)
                    .where(ConversationEvent.thread_id == thread_id)
                    .order_by(ConversationEvent.seq)
                )
            ).all()
        assert [row.seq for row in rows] == [1, 2]
        assert {row.elder_id for row in rows} == {elder_id}
    finally:
        async with setup_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM conversation_events WHERE thread_id = :thread"),
                {"thread": thread_id},
            )
            await conn.execute(
                text("DELETE FROM elders WHERE elder_id = :elder"),
                {"elder": elder_id},
            )
        await setup_engine.dispose()
