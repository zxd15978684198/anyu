"""N27b · `memory_writeback.writeback_memories` 的用例。

要证明的四件事（对应任务卡的四条硬要求）：

1. **内容哈希才是幂等键**：同批重复、跨调用重复、空白折叠后重复都只落一行，
   且既有的 active 行既不被 supersede 也不被复制；
2. **source 权重显式传**：`inferred` 先被压到 ≤0.5 再乘 `remember()` 的 0.6 系数，
   `explicit` 原样 —— 断言的是库里的具体数值差，不是「调用过」；
3. **容量收口复用既有实现**：`enforce_capacity` 恰好被调一次、实参是同一个长者，
   返回值原样映射进 `capacity_evicted`；一条没写进去时**不调**（`None` 的语义）；
4. **绝不抛**：非法 `elder_id` 返回全零；单条 `remember` 抛异常只跳过该条，
   其余照写，整批不抛。

与 `tests/test_memory_api_source.py` 同口径：业务写入走 `db` fixture 的外层事务，
用例结束整体回滚，不污染开发库；embedding provider 固定成 deterministic，
用例不受 `.env` 里有没有配 key 影响。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import Elder, MemoryChunk
from app.services import memory_service, memory_writeback
from app.services.embedding import DeterministicEmbeddingProvider
from app.services.memory_extraction import MemoryCandidate

#: 两条**不同**正文：deterministic provider 下不同文本近似正交，不会互相 supersede，
#: 这样才能同时断言「explicit 不被压」与「inferred 被压」两个数值。
CONTENT_EXPLICIT = "女儿叫王芳，每周日来看我。"
CONTENT_INFERRED = "最近总是点越剧。"


# --------------------------------------------------------------------- fixtures


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """外层事务：用例里的写入最后整体回滚。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await trans.rollback()
        await session.close()
        await conn.close()


@pytest.fixture(autouse=True)
def deterministic_provider(monkeypatch: pytest.MonkeyPatch) -> DeterministicEmbeddingProvider:
    """固定 embedding provider：不让用例去打真实 API，也不受 `.env` 影响。"""
    provider = DeterministicEmbeddingProvider()
    monkeypatch.setattr(memory_writeback, "_EMBEDDING_PROVIDER", provider)
    return provider


@pytest.fixture
async def elder_id(db: AsyncSession) -> uuid.UUID:
    elder = uuid.uuid4()
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder, "name": "张桂芬", "status": "active"}],
    )
    await db.flush()
    return elder


# --------------------------------------------------------------------- 小工具


def _candidate(
    content: str,
    *,
    source: str = "explicit",
    category: str = "private",
    importance_hint: float = 1.0,
) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        category=category,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        importance_hint=importance_hint,
    )


async def _rows(db: AsyncSession, elder_id: uuid.UUID) -> list[MemoryChunk]:
    result = await db.scalars(
        select(MemoryChunk).where(MemoryChunk.elder_id == elder_id).order_by(MemoryChunk.created_at)
    )
    return list(result)


async def _count(db: AsyncSession, elder_id: uuid.UUID, *, status: str | None = None) -> int:
    query = select(func.count()).select_from(MemoryChunk).where(MemoryChunk.elder_id == elder_id)
    if status is not None:
        query = query.where(MemoryChunk.status == status)
    return int((await db.execute(query)).scalar_one())


# ------------------------------------------------------- 1. 内容哈希 = 幂等键


async def test_same_content_twice_in_one_batch_writes_once(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """同批两条同内容 → 只写 1 行，另一条记 `skipped_duplicate`。"""
    result = await memory_writeback.writeback_memories(
        db,
        elder_id=elder_id,
        candidates=[_candidate(CONTENT_EXPLICIT), _candidate(CONTENT_EXPLICIT)],
    )

    assert len(result.written) == 1
    assert result.skipped_duplicate == 1
    assert result.skipped_filtered == 0
    assert result.failed == 0
    # 不变量：每个候选恰好落进一个桶，没有「悄悄消失」的条目。
    assert (
        len(result.written) + result.skipped_duplicate + result.skipped_filtered + result.failed
    ) == 2
    assert await _count(db, elder_id) == 1


async def test_second_call_with_same_content_is_duplicate(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """跨调用重放同一句：`skipped_duplicate=1`，库里仍然 1 行且保持 active。

    这条专门盯「不许用 supersede 当幂等」：如果实现先 `remember` 再查重，库里会多一行；
    如果实现拿 supersede 当去重，旧行会被标成 `superseded`，同样会红。
    """
    first = await memory_writeback.writeback_memories(
        db, elder_id=elder_id, candidates=[_candidate(CONTENT_EXPLICIT)]
    )
    second = await memory_writeback.writeback_memories(
        db, elder_id=elder_id, candidates=[_candidate(CONTENT_EXPLICIT)]
    )

    assert len(first.written) == 1
    assert second.written == ()
    assert second.skipped_duplicate == 1
    assert await _count(db, elder_id) == 1
    assert await _count(db, elder_id, status="active") == 1
    assert await _count(db, elder_id, status="superseded") == 0
    assert [row.content for row in await _rows(db, elder_id)] == [CONTENT_EXPLICIT]


async def test_whitespace_normalisation_counts_as_same_content(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """`"  喜欢  听越剧 "` 与 `"喜欢 听越剧"` 归一化后同键 → 只写一行。"""
    result = await memory_writeback.writeback_memories(
        db,
        elder_id=elder_id,
        candidates=[
            _candidate("  喜欢  听越剧 "),
            _candidate("喜欢 听越剧"),
        ],
    )

    assert len(result.written) == 1
    assert result.skipped_duplicate == 1
    rows = await _rows(db, elder_id)
    assert len(rows) == 1
    # 落库的是归一化正文（连续空白已折叠），不是原样带三个空格的版本。
    assert rows[0].content == "喜欢 听越剧"


async def test_existing_active_row_is_kept_without_supersede(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """库里已有同内容的 active 行：不写、不 supersede、不改它。"""
    await memory_service.remember(
        db,
        elder_id=elder_id,
        content=CONTENT_EXPLICIT,  # type: ignore[arg-type]
        embedding_provider=memory_writeback._embedding_provider(),
        importance=1.0,
        source="explicit",
    )

    result = await memory_writeback.writeback_memories(
        db, elder_id=elder_id, candidates=[_candidate(CONTENT_EXPLICIT)]
    )

    assert result.written == ()
    assert result.skipped_duplicate == 1
    rows = await _rows(db, elder_id)
    assert len(rows) == 1
    assert rows[0].status == "active"


# --------------------------------------------------- 2. source 权重显式传递


async def test_inferred_importance_is_capped_then_weighted(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """`explicit` 0.8 → 0.80；`inferred` 0.8 → min(0.8, 0.5)=0.5 → ×0.6 = 0.30。"""
    result = await memory_writeback.writeback_memories(
        db,
        elder_id=elder_id,
        candidates=[
            _candidate(CONTENT_EXPLICIT, source="explicit", importance_hint=0.8),
            _candidate(CONTENT_INFERRED, source="inferred", importance_hint=0.8),
        ],
    )

    assert len(result.written) == 2
    by_content = {row.content: float(row.importance) for row in await _rows(db, elder_id)}
    assert by_content[CONTENT_EXPLICIT] == pytest.approx(0.80, abs=0.005)
    assert by_content[CONTENT_INFERRED] == pytest.approx(0.30, abs=0.005)
    # 差值是本卡的核心断言：推断出来的记忆权重不到明确陈述的一半。
    assert by_content[CONTENT_INFERRED] < by_content[CONTENT_EXPLICIT] * 0.5


async def test_inferred_hint_below_cap_is_not_raised(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """cap 是上限不是下限：hint=0.3 的 inferred 仍是 0.3×0.6=0.18，不会被抬到 0.5。"""
    await memory_writeback.writeback_memories(
        db,
        elder_id=elder_id,
        candidates=[_candidate(CONTENT_INFERRED, source="inferred", importance_hint=0.3)],
    )

    rows = await _rows(db, elder_id)
    assert float(rows[0].importance) == pytest.approx(0.18, abs=0.005)


# --------------------------------------------------------- 3. 空输入与容量收口


async def test_empty_candidates_returns_zero_result(
    db: AsyncSession, elder_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空序列：全零结果，且连 `remember` 都不碰（不建 provider、不查库）。"""

    async def _boom(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - 不该被调
        raise AssertionError("空批次不该调用 remember")

    monkeypatch.setattr(memory_service, "remember", _boom)

    result = await memory_writeback.writeback_memories(db, elder_id=elder_id, candidates=[])

    assert result.written == ()
    assert result.skipped_duplicate == 0
    assert result.skipped_filtered == 0
    assert result.capacity_evicted is None
    assert await _count(db, elder_id) == 0


async def test_enforce_capacity_called_once_and_result_mapped(
    db: AsyncSession, elder_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写进 1 条 → `enforce_capacity` 恰好调 1 次，返回值映射进 `capacity_evicted`。"""
    calls: list[dict[str, Any]] = []

    async def _spy(session: AsyncSession, *, elder_id: uuid.UUID, cap: int = 500) -> int:
        calls.append({"elder_id": elder_id, "cap": cap})
        return 2

    monkeypatch.setattr(memory_service, "enforce_capacity", _spy)

    result = await memory_writeback.writeback_memories(
        db, elder_id=elder_id, candidates=[_candidate(CONTENT_EXPLICIT)]
    )

    assert len(result.written) == 1
    assert result.capacity_evicted == 2
    assert len(calls) == 1
    assert calls[0]["elder_id"] == elder_id


async def test_capacity_not_called_when_nothing_written(
    db: AsyncSession, elder_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一条都没写进去时不跑容量收口：`capacity_evicted is None`（区别于 0）。"""
    calls: list[int] = []

    async def _spy(session: AsyncSession, *, elder_id: uuid.UUID, cap: int = 500) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(memory_service, "enforce_capacity", _spy)
    await memory_writeback.writeback_memories(
        db, elder_id=elder_id, candidates=[_candidate(CONTENT_EXPLICIT)]
    )

    second = await memory_writeback.writeback_memories(
        db, elder_id=elder_id, candidates=[_candidate(CONTENT_EXPLICIT)]
    )

    assert second.written == ()
    assert second.capacity_evicted is None
    assert len(calls) == 1, "第二次全是重复，不该再跑容量收口"


# ------------------------------------------------------------- 4. 绝不向外抛


async def test_invalid_elder_id_returns_zero_and_does_not_raise(db: AsyncSession) -> None:
    """非法 `elder_id` 字符串 → 全零结果，不抛（旁路失败不能带走对话主链路）。"""
    result = await memory_writeback.writeback_memories(
        db,
        elder_id="not-a-uuid",
        candidates=[_candidate(CONTENT_EXPLICIT)],
    )

    assert result.written == ()
    assert result.skipped_duplicate == 0
    assert result.skipped_filtered == 0
    assert result.capacity_evicted is None


async def test_single_remember_failure_skips_only_that_item(
    db: AsyncSession, elder_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单条 `remember` 抛异常：计入 `failed`（不是 `skipped_filtered`），其余照写、不抛。"""
    original = memory_service.remember

    async def _flaky(session: AsyncSession, **kwargs: Any) -> Any:
        if kwargs["content"] == "会炸的句子":
            raise RuntimeError("boom")
        return await original(session, **kwargs)

    monkeypatch.setattr(memory_service, "remember", _flaky)

    candidates = [_candidate("会炸的句子"), _candidate(CONTENT_EXPLICIT)]
    result = await memory_writeback.writeback_memories(db, elder_id=elder_id, candidates=candidates)

    assert len(result.written) == 1
    assert result.failed == 1
    # 内容没问题、只是库写不进 —— 不许串到「内容不合格」那个桶里。
    assert result.skipped_filtered == 0
    assert result.skipped_duplicate == 0
    assert len(candidates) == (
        len(result.written) + result.skipped_duplicate + result.skipped_filtered + result.failed
    )
    assert [row.content for row in await _rows(db, elder_id)] == [CONTENT_EXPLICIT]


async def test_source_thread_id_is_passed_through(
    db: AsyncSession, elder_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`source_thread_id` 必须原样进 `remember`：否则回溯不到「哪一轮记的」。"""
    seen: list[Any] = []
    original = memory_service.remember

    async def _spy(session: AsyncSession, **kwargs: Any) -> Any:
        seen.append(kwargs.get("source_thread_id"))
        return await original(session, **kwargs)

    monkeypatch.setattr(memory_service, "remember", _spy)

    await memory_writeback.writeback_memories(
        db,
        elder_id=elder_id,
        candidates=[_candidate(CONTENT_EXPLICIT)],
        source_thread_id="thread-abc",
    )

    assert seen == ["thread-abc"]


async def test_category_is_passed_through(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """`family` 类候选落库时类别不能被默认成 private（家属可见性是另一条口径）。"""
    await memory_writeback.writeback_memories(
        db,
        elder_id=elder_id,
        candidates=[_candidate(CONTENT_EXPLICIT, category="family")],
    )

    rows = await _rows(db, elder_id)
    assert rows[0].category == "family"


async def test_oversized_and_blank_candidates_are_filtered(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """空白与超长正文在调用 `remember` 前就被本层挡下，记 `skipped_filtered`。"""
    result = await memory_writeback.writeback_memories(
        db,
        elder_id=elder_id,
        candidates=[_candidate("   "), _candidate("桂" * 201)],
    )

    assert result.written == ()
    assert result.skipped_filtered == 2
    assert result.failed == 0, "内容不合格不能算成「库写不进」"
    assert await _count(db, elder_id) == 0


async def test_blank_content_counts_as_filtered_not_failed(
    db: AsyncSession, elder_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空白内容走「本层过滤」，连 `remember` 都不该被调用（防 `failed` 串味）。"""

    async def _boom(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - 不该被调
        raise AssertionError("空白内容不该进 remember")

    monkeypatch.setattr(memory_service, "remember", _boom)

    result = await memory_writeback.writeback_memories(
        db, elder_id=elder_id, candidates=[_candidate("   \n\t ")]
    )

    assert result.written == ()
    assert result.skipped_filtered == 1
    assert result.failed == 0
    assert result.skipped_duplicate == 0
    assert result.capacity_evicted is None
    assert await _count(db, elder_id) == 0
