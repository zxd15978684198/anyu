"""记忆管理后台服务（`app/services/memory_admin_service.py`）的真库用例（迭代 9 · W4b①）。

## 为什么直接调 service 而不是走 API

路由不在本卡写作用域内（`app/api/**` 归另一张卡）。服务层是唯一写库层，它的导出契约、
审计 detail 键名、软删/硬删边界都会被 SECURITY.md 与台账引用，所以这里逐条钉住**返回值与
落库结果**，而不是状态码。

## 库与事务

按 `tests/test_staff_service.py` 的范式自建 `NullPool` engine（避免 `Event loop is closed`），
外层开一条事务、`AsyncSession` 用 `join_transaction_mode="create_savepoint"` 挂在上面；
每个用例结束后整体 `rollback`，不给开发库留垃圾数据。连不上库由 `conftest` 统一处置
（默认 fail，`ANYU_ALLOW_DB_SKIP=1` 才 skip）。

## 一个必须记住的 PG 细节

`created_at` 的列默认值是 `now()`，而 PG 的 `now()` 是**事务开始时间** —— 同一事务里
插入的多行拿到的是同一个时间戳。所以：排序用例必须显式给 `created_at`；
审计行的先后顺序不可断言（只能按 `action` / `detail` 过滤后断言内容）。
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import AuditLog, Elder
from app.models.memory import EMBEDDING_DIM, MemoryChunk
from app.services import memory_admin_service as admin
from conftest import require_db_connection

ELDER_ID = uuid.UUID("bbbb2222-2222-4222-8222-222222222101")
OTHER_ELDER_ID = uuid.UUID("bbbb2222-2222-4222-8222-222222222102")
ACTOR_ID = uuid.UUID("bbbb2222-2222-4222-8222-222222222201")
STRANGER_CHUNK_ID = uuid.UUID("bbbb2222-2222-4222-8222-2222222229ff")

TRACE_ID = "trace-w4b-0001"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    conn: AsyncConnection = await require_db_connection(engine)
    trans = await conn.begin()
    sess = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        await sess.close()
        await trans.rollback()
        await conn.close()


# ------------------------------------------------------------------ 造数


async def _seed_elder(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> uuid.UUID:
    session.add(Elder(elder_id=elder_id, name="长者甲", status="active"))
    await session.flush()
    return elder_id


async def _seed_chunk(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID = ELDER_ID,
    content: str = "喜欢听越剧",
    status: str = "active",
    importance: float = 1.0,
    embedding: list[float] | None = None,
    created_at: datetime.datetime | None = None,
    source_thread_id: str | None = None,
) -> uuid.UUID:
    chunk = MemoryChunk(
        elder_id=elder_id,
        content=content,
        status=status,
        importance=importance,
        embedding=embedding,
        source_thread_id=source_thread_id,
    )
    if created_at is not None:
        chunk.created_at = created_at
    session.add(chunk)
    await session.flush()
    return chunk.chunk_id


async def _audits(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID = ELDER_ID,
    action: str | None = None,
) -> list[AuditLog]:
    """按 `resource_id`（本卡一律写长者 id）取审计行；顺序不可依赖（见模块 docstring）。"""
    stmt = select(AuditLog).where(AuditLog.resource_id == elder_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    rows = await session.execute(stmt.execution_options(populate_existing=True))
    return list(rows.scalars().all())


async def _chunk_status(session: AsyncSession, chunk_id: uuid.UUID) -> str | None:
    row = await session.execute(
        select(MemoryChunk.status)
        .where(MemoryChunk.chunk_id == chunk_id)
        .execution_options(populate_existing=True)
    )
    return row.scalar_one_or_none()


def _now_minus(minutes: int) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=minutes)


# ------------------------------------------------------------------ 1. 导出（list）


async def test_export_returns_content_and_metadata(session: AsyncSession) -> None:
    """导出含正文与元数据，且**不含 embedding**；importance 已转 float。"""
    await _seed_elder(session)
    await _seed_chunk(
        session,
        content="老伴姓王，喜欢越剧《梁山伯与祝英台》",
        importance=0.95,
        source_thread_id="thread-1",
    )

    records = await admin.export_elder_memories(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "chunk_id",
        "elder_id",
        "content",
        "category",
        "importance",
        "status",
        "source_thread_id",
        "created_at",
        "last_accessed",
    }
    assert record["content"] == "老伴姓王，喜欢越剧《梁山伯与祝英台》"
    assert record["elder_id"] == str(ELDER_ID)
    assert record["importance"] == pytest.approx(0.95)
    assert isinstance(record["importance"], float)
    assert record["source_thread_id"] == "thread-1"
    # 从未召回 → None 而不是空字符串：导出要能区分「没召回过」与「召回于纪元」
    assert record["last_accessed"] is None


async def test_export_omits_embedding(session: AsyncSession) -> None:
    """即使库里存了真实维度的向量，导出字典里也不能出现 `embedding` 键（PII/指纹）。"""
    await _seed_elder(session)
    await _seed_chunk(session, embedding=[0.5] * EMBEDDING_DIM)

    records = await admin.export_elder_memories(session, elder_id=ELDER_ID)

    assert records
    assert "embedding" not in records[0]
    # 向量若被导出会以 "[0.5, 0.5, ..." 的形式出现；不能只断言 "0.5" 不在串里——
    # 时间戳小数位恰好以 5 开头（如 .555390）时，那个子串本来就会出现，会假失败。
    assert "[0.5" not in json.dumps(records, ensure_ascii=False)


async def test_export_filters_by_status(session: AsyncSession) -> None:
    """`status="deleted"` 只导已删的；`None` 导全部三种状态。"""
    await _seed_elder(session)
    await _seed_chunk(session, content="活动记忆", status="active")
    await _seed_chunk(session, content="被归并", status="superseded")
    await _seed_chunk(session, content="被删除", status="deleted")

    everything = await admin.export_elder_memories(session, elder_id=ELDER_ID)
    deleted = await admin.export_elder_memories(session, elder_id=ELDER_ID, status="deleted")

    assert sorted(r["status"] for r in everything) == ["active", "deleted", "superseded"]
    assert [r["content"] for r in deleted] == ["被删除"]


async def test_export_orders_chronologically(session: AsyncSession) -> None:
    """正序（旧 → 新）：导出是「拿走一份完整档案」，人按时间读。"""
    await _seed_elder(session)
    await _seed_chunk(session, content="第三条", created_at=_now_minus(1))
    await _seed_chunk(session, content="第一条", created_at=_now_minus(3))
    await _seed_chunk(session, content="第二条", created_at=_now_minus(2))

    records = await admin.export_elder_memories(session, elder_id=ELDER_ID)

    assert [r["content"] for r in records] == ["第一条", "第二条", "第三条"]


async def test_export_writes_single_audit_with_counts_and_trace(session: AsyncSession) -> None:
    """导出写恰好一条 `memory.export`，detail 带计数/范围/target，trace_id 进 detail。"""
    await _seed_elder(session)
    await _seed_chunk(session, content="一")
    await _seed_chunk(session, content="二")

    await admin.export_elder_memories(
        session, elder_id=ELDER_ID, actor_id=ACTOR_ID, trace_id=TRACE_ID
    )

    audits = await _audits(session, action=admin.AUDIT_ACTION_EXPORT)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.actor_type == "staff"
    assert audit.actor_id == ACTOR_ID
    assert audit.resource_type == "memory_chunks"
    assert audit.resource_id == ELDER_ID
    assert audit.detail["count"] == 2
    assert audit.detail["status"] == "all"
    assert audit.detail["target"] == "list"
    # audit_logs 没有 trace_id 列（模型归 Lead，本卡不改 schema）→ 放 detail 里可反查
    assert audit.detail["trace_id"] == TRACE_ID


async def test_export_without_actor_is_logged_as_system(session: AsyncSession) -> None:
    """没有操作者时按 `system` 记，`actor_id` 回落成 resource_id（避开 NOT NULL）。"""
    await _seed_elder(session)
    await _seed_chunk(session)

    await admin.export_elder_memories(session, elder_id=ELDER_ID)

    audit = (await _audits(session, action=admin.AUDIT_ACTION_EXPORT))[0]
    assert audit.actor_type == "system"
    assert audit.actor_id == ELDER_ID


async def test_export_rejects_unknown_status_before_writing_audit(session: AsyncSession) -> None:
    """非法 status 直接抛，**不**静默当 None，也不留审计。"""
    await _seed_elder(session)
    await _seed_chunk(session)

    with pytest.raises(ValueError, match="status must be one of"):
        await admin.export_elder_memories(session, elder_id=ELDER_ID, status="archived")

    assert await _audits(session, action=admin.AUDIT_ACTION_EXPORT) == []


async def test_export_rejects_unknown_actor_type(session: AsyncSession) -> None:
    await _seed_elder(session)

    with pytest.raises(ValueError, match="actor_type must be one of"):
        await admin.export_elder_memories(session, elder_id=ELDER_ID, actor_type="robot")

    assert await _audits(session, action=admin.AUDIT_ACTION_EXPORT) == []


async def test_export_of_empty_elder_still_audits_zero(session: AsyncSession) -> None:
    """0 行也留痕：审计记的是「有人执行了导出」，不是「导出了几条」。"""
    await _seed_elder(session)

    records = await admin.export_elder_memories(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert records == []
    audit = (await _audits(session, action=admin.AUDIT_ACTION_EXPORT))[0]
    assert audit.detail["count"] == 0


# ------------------------------------------------------------------ 2. 导出（JSONL）


async def test_export_jsonl_writes_one_line_per_memory(
    session: AsyncSession, tmp_path: Path
) -> None:
    """返回行数 = 实际行数 = 记忆条数；中文按 UTF-8 原样落盘、不含向量。"""
    await _seed_elder(session)
    await _seed_chunk(session, content="喜欢听越剧")
    await _seed_chunk(session, content="每天七点吃药", embedding=[0.25] * EMBEDDING_DIM)
    target = tmp_path / "memories.jsonl"

    lines = await admin.export_memory_jsonl(
        session, elder_id=ELDER_ID, path=target, actor_id=ACTOR_ID
    )

    assert lines == 2
    text = target.read_text(encoding="utf-8")
    raw_lines = text.splitlines()
    assert len(raw_lines) == 2
    for raw in raw_lines:
        record = json.loads(raw)
        assert "embedding" not in record
    # ensure_ascii=False：文件要能直接给人看，不能是一串 \u8d8a\u5267
    assert "喜欢听越剧" in text
    assert "\\u" not in text


async def test_export_jsonl_sorts_keys_for_stable_diff(
    session: AsyncSession, tmp_path: Path
) -> None:
    """`sort_keys=True`：同一份数据的两次导出逐字节可比。"""
    await _seed_elder(session)
    await _seed_chunk(session)
    target = tmp_path / "memories.jsonl"

    await admin.export_memory_jsonl(session, elder_id=ELDER_ID, path=target)

    record = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert list(record) == sorted(record)


async def test_export_jsonl_empty_elder_writes_empty_file(
    session: AsyncSession, tmp_path: Path
) -> None:
    """没有记忆时仍建出空文件并返回 0：调用方不用为「空导出」分叉一个分支。"""
    await _seed_elder(session)
    target = tmp_path / "empty.jsonl"

    lines = await admin.export_memory_jsonl(session, elder_id=ELDER_ID, path=target)

    assert lines == 0
    assert target.exists()
    assert target.read_text(encoding="utf-8") == ""


async def test_export_jsonl_audits_bytes_but_not_path(
    session: AsyncSession, tmp_path: Path
) -> None:
    """审计记 `count`/`bytes`/`target`，**不记路径**（路径常含长者姓名，别在留痕里复制 PII）。"""
    await _seed_elder(session)
    await _seed_chunk(session, content="一")
    target = tmp_path / "张三_记忆.jsonl"

    await admin.export_memory_jsonl(
        session, elder_id=ELDER_ID, path=target, actor_id=ACTOR_ID, trace_id=TRACE_ID
    )

    audit = (await _audits(session, action=admin.AUDIT_ACTION_EXPORT))[0]
    assert audit.detail["target"] == "jsonl"
    assert audit.detail["count"] == 1
    assert audit.detail["bytes"] == target.stat().st_size
    assert audit.detail["trace_id"] == TRACE_ID
    assert "path" not in audit.detail
    assert "张三" not in json.dumps(audit.detail, ensure_ascii=False)


# ------------------------------------------------------------------ 3. 软删


async def test_admin_delete_all_soft_deletes_only_active(session: AsyncSession) -> None:
    """`chunk_ids=None` 复用 迭代 7 的 `delete_by_elder`：只把 active 降级，正文留着。"""
    await _seed_elder(session)
    active_a = await _seed_chunk(session, content="活动一")
    active_b = await _seed_chunk(session, content="活动二")
    superseded = await _seed_chunk(session, content="已归并", status="superseded")

    deleted = await admin.admin_delete_memory(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert deleted == 2
    assert await _chunk_status(session, active_a) == "deleted"
    assert await _chunk_status(session, active_b) == "deleted"
    assert await _chunk_status(session, superseded) == "superseded"

    # 软删不物理删：正文明明还在，只是 status 变了
    content = await session.execute(
        select(MemoryChunk.content)
        .where(MemoryChunk.chunk_id == active_a)
        .execution_options(populate_existing=True)
    )
    assert content.scalar_one() == "活动一"


async def test_admin_delete_all_reuses_day7_audit(session: AsyncSession) -> None:
    """审计沿用 `delete_memory`（同一个动作两个名字会让检索漏掉一半）。"""
    await _seed_elder(session)
    await _seed_chunk(session)

    await admin.admin_delete_memory(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    audit = (await _audits(session, action=admin.AUDIT_ACTION_DELETE))[0]
    assert admin.AUDIT_ACTION_DELETE == "delete_memory"
    assert audit.detail["soft_delete"] is True
    assert audit.detail["deleted_chunks"] == 1
    assert audit.detail["reason"] == "admin_delete"
    assert audit.actor_type == "staff"


async def test_admin_delete_specific_chunks(session: AsyncSession) -> None:
    """按 id 删只动点名的那些，其余保持 active；审计 detail 标 `scope="chunks"`。"""
    await _seed_elder(session)
    first = await _seed_chunk(session, content="一")
    second = await _seed_chunk(session, content="二")
    untouched = await _seed_chunk(session, content="三")

    deleted = await admin.admin_delete_memory(
        session, elder_id=ELDER_ID, chunk_ids=[first, second], actor_id=ACTOR_ID
    )

    assert deleted == 2
    assert await _chunk_status(session, first) == "deleted"
    assert await _chunk_status(session, second) == "deleted"
    assert await _chunk_status(session, untouched) == "active"

    audit = (await _audits(session, action=admin.AUDIT_ACTION_DELETE))[0]
    assert audit.detail["scope"] == "chunks"
    assert audit.detail["requested_chunks"] == 2
    assert audit.detail["deleted_chunks"] == 2


async def test_admin_delete_cannot_reach_other_elders_chunks(session: AsyncSession) -> None:
    """跨长者隔离：`where` 里始终带 `elder_id`，上游传别人的 id 也改不到。"""
    await _seed_elder(session)
    await _seed_elder(session, OTHER_ELDER_ID)
    mine = await _seed_chunk(session, content="我的")
    theirs = await _seed_chunk(session, elder_id=OTHER_ELDER_ID, content="别人的")

    deleted = await admin.admin_delete_memory(
        session, elder_id=ELDER_ID, chunk_ids=[mine, theirs], actor_id=ACTOR_ID
    )

    assert deleted == 1
    assert await _chunk_status(session, mine) == "deleted"
    assert await _chunk_status(session, theirs) == "active"


async def test_admin_delete_empty_chunk_list_is_noop(session: AsyncSession) -> None:
    """`chunk_ids=[]` 是空操作，**不会**被降级成「删全部」。"""
    await _seed_elder(session)
    chunk = await _seed_chunk(session)

    deleted = await admin.admin_delete_memory(
        session, elder_id=ELDER_ID, chunk_ids=[], actor_id=ACTOR_ID
    )

    assert deleted == 0
    assert await _chunk_status(session, chunk) == "active"
    assert await _audits(session, action=admin.AUDIT_ACTION_DELETE) == []


async def test_admin_delete_audits_even_when_nothing_matches(session: AsyncSession) -> None:
    """0 行受影响也留痕：这条记录说明「有人要求删除」，与 迭代 7 的口径一致。"""
    await _seed_elder(session)

    deleted = await admin.admin_delete_memory(
        session, elder_id=ELDER_ID, chunk_ids=[STRANGER_CHUNK_ID], actor_id=ACTOR_ID
    )

    assert deleted == 0
    audit = (await _audits(session, action=admin.AUDIT_ACTION_DELETE))[0]
    assert audit.detail["deleted_chunks"] == 0
    assert audit.detail["requested_chunks"] == 1


# ------------------------------------------------------------------ 4. 硬删（退房）


async def test_purge_removes_every_status(session: AsyncSession) -> None:
    """硬删一视同仁：active / superseded / deleted 全抹掉（退房是终态）。"""
    await _seed_elder(session)
    await _seed_chunk(session, content="活动", status="active")
    await _seed_chunk(session, content="被归并", status="superseded")
    await _seed_chunk(session, content="已删", status="deleted")

    purged = await admin.purge_elder_memories(session, elder_id=ELDER_ID)

    assert purged == 3
    remaining = await session.execute(
        select(MemoryChunk.chunk_id)
        .where(MemoryChunk.elder_id == ELDER_ID)
        .execution_options(populate_existing=True)
    )
    assert list(remaining.scalars().all()) == []


async def test_purge_is_idempotent(session: AsyncSession) -> None:
    """重复退房不能因为「已经删过了」而报错，第二次返回 0。"""
    await _seed_elder(session)
    await _seed_chunk(session)

    assert await admin.purge_elder_memories(session, elder_id=ELDER_ID) == 1
    assert await admin.purge_elder_memories(session, elder_id=ELDER_ID) == 0


async def test_purge_leaves_other_elders_and_writes_no_audit(session: AsyncSession) -> None:
    """只动目标长者；硬删本身不写审计（留痕由 `elder.checkout` 摘要承担）。"""
    await _seed_elder(session)
    await _seed_elder(session, OTHER_ELDER_ID)
    await _seed_chunk(session, content="我的")
    await _seed_chunk(session, elder_id=OTHER_ELDER_ID, content="别人的")

    purged = await admin.purge_elder_memories(session, elder_id=ELDER_ID)

    assert purged == 1
    theirs = await session.execute(
        select(MemoryChunk.content)
        .where(MemoryChunk.elder_id == OTHER_ELDER_ID)
        .execution_options(populate_existing=True)
    )
    assert list(theirs.scalars().all()) == ["别人的"]
    assert await _audits(session) == []


async def test_validate_actor_type_exports_the_shared_vocabulary() -> None:
    """`lifecycle_service` 复用同一个校验：词汇表只有一处定义。"""
    assert admin.AUDIT_ACTOR_TYPES == ("staff", "guardian", "system", "device")
    for actor_type in admin.AUDIT_ACTOR_TYPES:
        assert admin.validate_actor_type(actor_type) == actor_type
    with pytest.raises(ValueError, match="actor_type must be one of"):
        admin.validate_actor_type("elder")


def test_export_record_shape_is_documented() -> None:
    """导出字段清单是契约（SECURITY.md 会引用），这里钉住键名与顺序无关性。"""
    keys: set[str] = {
        "chunk_id",
        "elder_id",
        "content",
        "category",
        "importance",
        "status",
        "source_thread_id",
        "created_at",
        "last_accessed",
    }
    sample: dict[str, Any] = dict.fromkeys(keys, "")
    assert "embedding" not in sample
