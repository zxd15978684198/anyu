"""记忆周归并（FR-MEM-07）的定向用例。

## 怎么分两类

- **纯逻辑**：阈值非法、调度注册字段 —— 不碰库。
- **真库**：`merge_duplicates` / `merge_all_active_elders` / `run_memory_merge`。
  一律用 `db_conn` 夹具（每用例一个外层事务、结束回滚），不建 `anyu_test` 库
  （口径见 `tests/conftest.py` 模块 docstring）。

## 为什么直接插 `MemoryChunk` 行，而不走 `remember()`

`remember()` 会先过 `_supersede_conflicts`（同名去重 / 矛盾取代）——用它造「两条重复的
active 记忆」，第一条还没插进去就已经被第二条取代了，测不到归并。所以这里按需要直接
构造 `status='active'` 的行，把「库里本来就有两条重复」这个前提显式摆出来。

向量也不是语义模型：`_axis` / `_rotated_by` 只把「我想让这两条多像」变成显式参数，
用来钉住 0.90 阈值两侧与传递闭包。**不得据此得出召回准确率结论**。
"""

from __future__ import annotations

import datetime
import math
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models import AuditLog, Elder, MemoryChunk
from app.scheduler import memory as memory_jobs
from app.services import memory_service
from app.services.embedding import EMBEDDING_DIMENSION
from app.services.memory_service import (
    ACTION_MERGE,
    MERGE_SIMILARITY,
    merge_all_active_elders,
    merge_duplicates,
)

NOW = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.UTC)


# ============================================================
# 夹具与小工具
# ============================================================


@pytest.fixture(autouse=True)
async def _release_pool() -> AsyncIterator[None]:
    """每个用例结束把 `app.db.engine` 池里的连接还掉（理由见 `test_memory_service.py`）。"""
    yield
    from app.db import engine as app_engine

    await app_engine.dispose()


def _session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码；残留由夹具回滚。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


def _axis(index: int) -> list[float]:
    """N 维第 `index` 个正交基向量（单位长度）。"""
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[index] = 1.0
    return vector


def _rotated_by(degrees: float) -> list[float]:
    """在 (0,1) 平面上把 `_axis(0)` 转 `degrees` 度，余弦即 `cos(degrees)`。"""
    radians = math.radians(degrees)
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[0] = math.cos(radians)
    vector[1] = math.sin(radians)
    return vector


#: 与 `_axis(0)` 余弦 0.95 的向量（≈18.19°）。
_SIMILAR_95 = _rotated_by(math.degrees(math.acos(0.95)))
#: 与 `_axis(0)` 余弦 0.88 的向量（阈值另一侧）。
_SIMILAR_88 = _rotated_by(math.degrees(math.acos(0.88)))
#: 与 `_SIMILAR_95` 余弦 0.95、与 `_axis(0)` 余弦约 0.805：用来测传递闭包。
_SIMILAR_95_AGAIN = _rotated_by(2 * math.degrees(math.acos(0.95)))


async def _seed_elder(conn: AsyncConnection, *, status: str = "active") -> uuid.UUID:
    """`memory_chunks.elder_id` 的外键目标；占位姓名，不写真实长者数据（AGENTS §4.3）。"""
    elder_id = uuid.uuid4()
    await conn.execute(
        Elder.__table__.insert().values(elder_id=elder_id, name="测试长者", status=status)
    )
    return elder_id


async def _add_chunk(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    embedding: list[float] | None,
    category: str = "private",
    importance: float = 0.5,
    last_accessed: datetime.datetime | None = None,
    status: str = "active",
    content: str = "占位记忆正文",
) -> uuid.UUID:
    row = MemoryChunk(
        elder_id=elder_id,
        content=content,
        embedding=embedding,
        category=category,
        importance=Decimal(str(importance)),
        status=status,
        last_accessed=last_accessed,
    )
    session.add(row)
    await session.flush()
    return row.chunk_id


async def _fetch_status(session: AsyncSession, chunk_id: uuid.UUID) -> str:
    """从库里重读状态（绕开 identity map 里可能过期的副本）。"""
    stmt = (
        select(MemoryChunk.status)
        .where(MemoryChunk.chunk_id == chunk_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def _merges(session: AsyncSession) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.action == ACTION_MERGE)
        .order_by(AuditLog.audit_id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars().all())


# ============================================================
# merge_duplicates：核心口径
# ============================================================


async def test_merge_supersedes_duplicate_and_writes_audit(db_conn: AsyncConnection) -> None:
    """两条高度相似（0.95）→ 一条 active、一条 superseded，审计里能查到被并的那条。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    kept = await _add_chunk(
        session,
        elder_id=elder_id,
        embedding=_axis(0),
        importance=0.9,
        last_accessed=NOW - datetime.timedelta(days=2),
        content="我喜欢越剧",
    )
    loser = await _add_chunk(
        session,
        elder_id=elder_id,
        embedding=_SIMILAR_95,
        importance=0.3,
        last_accessed=NOW - datetime.timedelta(days=1),
        content="我平时爱听越剧",
    )

    merged = await merge_duplicates(session, elder_id=elder_id)

    assert merged == 1
    assert await _fetch_status(session, kept) == "active"
    assert await _fetch_status(session, loser) == "superseded"

    audits = await _merges(session)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.actor_type == "system"
    assert audit.actor_id == elder_id
    assert audit.resource_type == "memory_chunks"
    assert audit.resource_id == kept
    assert audit.detail["kept_id"] == str(kept)
    assert audit.detail["merged_ids"] == [str(loser)]
    assert audit.detail["category"] == "private"
    assert audit.detail["threshold"] == MERGE_SIMILARITY
    assert audit.detail["similarity"] == pytest.approx(0.95, abs=1e-3)
    # 正文一律不进审计（AGENTS §4 第 11 条）。
    assert "越剧" not in str(audit.detail)


async def test_merge_is_idempotent(db_conn: AsyncConnection) -> None:
    """再跑一次：`merged=0`，且不追加第二条审计（空跑不是事实）。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    await _add_chunk(session, elder_id=elder_id, embedding=_axis(0), importance=0.9)
    await _add_chunk(session, elder_id=elder_id, embedding=_SIMILAR_95, importance=0.2)

    assert await merge_duplicates(session, elder_id=elder_id) == 1
    assert await merge_duplicates(session, elder_id=elder_id) == 0
    assert len(await _merges(session)) == 1


async def test_merge_keeps_highest_importance(db_conn: AsyncConnection) -> None:
    """保留 importance 最高的那条；平手取 last_accessed 更新的那条。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    low = await _add_chunk(session, elder_id=elder_id, embedding=_axis(0), importance=0.2)
    high = await _add_chunk(session, elder_id=elder_id, embedding=_SIMILAR_95, importance=0.8)

    assert await merge_duplicates(session, elder_id=elder_id) == 1
    assert await _fetch_status(session, high) == "active"
    assert await _fetch_status(session, low) == "superseded"

    # 平手：两条 importance 相同，旧的被并掉。
    elder_two = await _seed_elder(db_conn)
    older = await _add_chunk(
        session,
        elder_id=elder_two,
        embedding=_axis(0),
        importance=0.5,
        last_accessed=NOW - datetime.timedelta(days=30),
    )
    newer = await _add_chunk(
        session,
        elder_id=elder_two,
        embedding=_SIMILAR_95,
        importance=0.5,
        last_accessed=NOW - datetime.timedelta(days=1),
    )
    assert await merge_duplicates(session, elder_id=elder_two) == 1
    assert await _fetch_status(session, newer) == "active"
    assert await _fetch_status(session, older) == "superseded"


async def test_merge_respects_threshold(db_conn: AsyncConnection) -> None:
    """相似度 0.88 在默认 0.90 阈值之下：不并；把阈值调到 0.99 时 0.95 也不并。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    await _add_chunk(session, elder_id=elder_id, embedding=_axis(0), importance=0.9)
    await _add_chunk(session, elder_id=elder_id, embedding=_SIMILAR_88, importance=0.8)

    assert await merge_duplicates(session, elder_id=elder_id) == 0
    assert await _merges(session) == []

    strict = await _seed_elder(db_conn)
    first = await _add_chunk(session, elder_id=strict, embedding=_axis(0), importance=0.9)
    second = await _add_chunk(session, elder_id=strict, embedding=_SIMILAR_95, importance=0.8)
    assert await merge_duplicates(session, elder_id=strict, threshold=0.99) == 0
    assert await _fetch_status(session, first) == "active"
    assert await _fetch_status(session, second) == "active"


async def test_merge_stays_within_category(db_conn: AsyncConnection) -> None:
    """同向量但跨类别（private / family）不并：那是两种可见范围，不是同一事实的两种说法。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    private = await _add_chunk(
        session, elder_id=elder_id, embedding=_axis(0), category="private", importance=0.9
    )
    family = await _add_chunk(
        session, elder_id=elder_id, embedding=_axis(0), category="family", importance=0.5
    )

    assert await merge_duplicates(session, elder_id=elder_id) == 0
    assert await _fetch_status(session, private) == "active"
    assert await _fetch_status(session, family) == "active"


async def test_merge_skips_chunks_without_embedding(db_conn: AsyncConnection) -> None:
    """没有向量的条目无从比较，一律不动（与 `decay` 的降级口径一致）。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    with_vector = await _add_chunk(session, elder_id=elder_id, embedding=_axis(0), importance=0.9)
    no_vector = await _add_chunk(session, elder_id=elder_id, embedding=None, importance=0.5)

    assert await merge_duplicates(session, elder_id=elder_id) == 0
    assert await _fetch_status(session, with_vector) == "active"
    assert await _fetch_status(session, no_vector) == "active"


async def test_merge_uses_transitive_closure(db_conn: AsyncConnection) -> None:
    """A≈B、B≈C、A≁C：三条并成一组（不留"中间被并、端点还 active"的半截状态）。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    first = await _add_chunk(session, elder_id=elder_id, embedding=_axis(0), importance=0.9)
    middle = await _add_chunk(session, elder_id=elder_id, embedding=_SIMILAR_95, importance=0.6)
    last = await _add_chunk(session, elder_id=elder_id, embedding=_SIMILAR_95_AGAIN, importance=0.4)

    assert await merge_duplicates(session, elder_id=elder_id) == 2
    assert await _fetch_status(session, first) == "active"
    assert await _fetch_status(session, middle) == "superseded"
    assert await _fetch_status(session, last) == "superseded"

    audits = await _merges(session)
    assert len(audits) == 1
    assert set(audits[0].detail["merged_ids"]) == {str(middle), str(last)}


async def test_merge_rejects_invalid_threshold(db_conn: AsyncConnection) -> None:
    """阈值必须是 (0,1]：0 与 >1 都拒绝（既不静默全并，也不静默空跑）。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    for bad in (0.0, -0.1, 1.01):
        with pytest.raises(ValueError):
            await merge_duplicates(session, elder_id=elder_id, threshold=bad)


# ============================================================
# merge_all_active_elders：遍历与容错
# ============================================================


async def test_merge_all_active_elders_skips_non_active(db_conn: AsyncConnection) -> None:
    """只归并在住长者：转出/离院的记忆不参与（与衰减、容量淘汰同一在住口径）。"""
    session = _session(db_conn)
    in_house = await _seed_elder(db_conn)
    await _add_chunk(session, elder_id=in_house, embedding=_axis(0), importance=0.9)
    await _add_chunk(session, elder_id=in_house, embedding=_SIMILAR_95, importance=0.2)

    moved_out = await _seed_elder(db_conn, status="transferred")
    await _add_chunk(session, elder_id=moved_out, embedding=_axis(0), importance=0.9)
    await _add_chunk(session, elder_id=moved_out, embedding=_SIMILAR_95, importance=0.2)

    report = await merge_all_active_elders(session)

    assert report.merged == 1
    assert report.elders == 1
    assert report.failed == 0
    assert report.threshold == MERGE_SIMILARITY


async def test_merge_all_active_elders_continues_after_one_failure(
    db_conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一位长者失败只回滚他那一段，其余照跑（不可逆的状态翻转不该被别人的失败抹掉）。"""
    session = _session(db_conn)
    broken = await _seed_elder(db_conn)
    healthy = await _seed_elder(db_conn)
    await _add_chunk(session, elder_id=healthy, embedding=_axis(0), importance=0.9)
    await _add_chunk(session, elder_id=healthy, embedding=_SIMILAR_95, importance=0.2)

    original = memory_service.merge_duplicates

    async def _flaky(sess: AsyncSession, *, elder_id: uuid.UUID, threshold: float = 0.9) -> int:
        if elder_id == broken:
            raise RuntimeError("boom")
        return await original(sess, elder_id=elder_id, threshold=threshold)

    monkeypatch.setattr(memory_service, "merge_duplicates", _flaky)

    report = await merge_all_active_elders(session)

    assert report.failed == 1
    assert report.elders == 1
    assert report.merged == 1


# ============================================================
# 调度：每周一 03:30 + 任务体
# ============================================================


def test_register_memory_jobs_registers_monday_0330() -> None:
    """job id 是 `anyu:memory-merge`，cron 是周一 03:30，且只注册一条。"""
    scheduler = AsyncIOScheduler()
    memory_jobs.register_memory_jobs(scheduler)

    jobs = scheduler.get_jobs()
    assert [job.id for job in jobs] == ["anyu:memory-merge"]
    trigger = str(jobs[0].trigger)
    assert "day_of_week='mon'" in trigger
    assert "hour='3'" in trigger
    assert "minute='30'" in trigger


async def test_run_memory_merge_counts_and_commits(
    db_conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务体把服务层结果带回来并 commit（外层事务里只放 savepoint，不真提交）。"""
    factory = async_sessionmaker(
        bind=db_conn,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(memory_jobs, "db_session_factory", factory)

    session = _session(db_conn)
    elder_id = await _seed_elder(db_conn)
    kept = await _add_chunk(session, elder_id=elder_id, embedding=_axis(0), importance=0.9)
    loser = await _add_chunk(session, elder_id=elder_id, embedding=_SIMILAR_95, importance=0.2)

    assert await memory_jobs.run_memory_merge() == 1
    assert await _fetch_status(session, kept) == "active"
    assert await _fetch_status(session, loser) == "superseded"


async def test_run_memory_merge_swallows_whole_run_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整轮失败（例如连接池拿不到连接）只记日志并返回 0，不抛给调度器。"""

    def _boom() -> None:
        raise RuntimeError("no pool")

    monkeypatch.setattr(memory_jobs, "db_session_factory", _boom)

    assert await memory_jobs.run_memory_merge() == 0
