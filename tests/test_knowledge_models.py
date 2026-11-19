"""表级测试：`knowledge_documents` / `knowledge_chunks`（机构知识两表）。

只验**数据库层**能不能挡住该挡的东西，不验业务（生效版本怎么选、权限怎么判在服务层）：

1. 一条文档 + 两条分块插得进去，`created_at` / `updated_at` 走 server default；
2. 重复 `(title, version)` → `IntegrityError`（同一版本只存一份）；
3. `effective_until <= effective_from` → `IntegrityError`（区间必须正向；空值放行）；
4. `category` / `authority_level` / `status` 非法值 → `IntegrityError`（三处 CHECK）；
5. `version = 0` → `IntegrityError`（版本号从 1 起）；
6. 同文档内重复 `chunk_index` → `IntegrityError`；
7. 删文档 → 分块随 `ON DELETE CASCADE` 一起走（不留孤儿）。

跑法与 `tests/test_conversation_events_model.py` 同构：自建 NullPool 引擎，每用例开一个事务、
结束回滚，所以不脏开发库；用例内做「期望失败」的插入要包在 `begin_nested()` 里，否则一条
IntegrityError 会把整个外层事务打成 aborted，后面的断言就全废了。

**别在这个文件的 `AsyncConnection` 上 `select(实体)`**：`select(KnowledgeDocument)` 走的是
Core 行处理（compile 出全部列、结果类型是 `CursorResult`），`scalar_one()` 拿到的是首列
`document_id`，接着 `row.title` 就是
`AttributeError: 'asyncpg.pgproto.pgproto.UUID' object has no attribute 'title'`。
要读实体字段就显式选列，本文件是 `select(KnowledgeDocument.title, ...)` 再 `.one()`。

**前置**：`alembic upgrade head`（0011）已执行；否则本文件会因缺表而红——这是对的，
迁移没跑就不该假装 schema 存在。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument
from app.models.staff import StaffAccount
from conftest import require_db_connection

STAFF_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccc01")
DOCUMENT_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddd01")
EFFECTIVE_FROM = datetime.datetime(2026, 11, 1, 0, 0, tzinfo=datetime.UTC)
EFFECTIVE_UNTIL = datetime.datetime(2027, 11, 1, 0, 0, tzinfo=datetime.UTC)
EMBEDDING = [0.0] * EMBEDDING_DIM

pytestmark = pytest.mark.usefixtures("seeded")


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
    # 本文件的口径是「知识表里只有本用例插的行」（`_count` 数整表、多个用例断言 0/1）。
    # N22 把 4 份种子灌进了开发库，整表不再为空 —— 在事务里先清场，测试结束一并回滚，
    # 既不碰库里的真实数据，也不用把每条断言改成「按 id 过滤」的写法。
    await conn.execute(text("DELETE FROM knowledge_documents"))
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """一名 admin 账号（`created_by` / `updated_by` 有外键，必须先有主表行）。"""
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=STAFF_ID,
            name="知识管理员",
            role="admin",
            password_hash="not-a-real-hash",  # pragma: allowlist secret
            status="active",
        )
    )


def _document(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "document_id": DOCUMENT_ID,
        "title": "午餐时间",
        "category": "meal",
        "authority_level": "institution",
        "version": 1,
        "effective_from": EFFECTIVE_FROM,
        "effective_until": EFFECTIVE_UNTIL,
        "status": "active",
        "body": "午餐 11:30 开餐、13:00 收餐；可提前 10 分钟入场。",
        "created_by": STAFF_ID,
        "updated_by": STAFF_ID,
    }
    values.update(overrides)
    return values


def _chunk(*, chunk_index: int, content: str) -> dict[str, object]:
    return {
        "document_id": DOCUMENT_ID,
        "chunk_index": chunk_index,
        "content": content,
        "embedding": EMBEDDING,
    }


async def _insert_document(db: AsyncConnection, **overrides: object) -> None:
    await db.execute(KnowledgeDocument.__table__.insert().values(_document(**overrides)))


async def _count(db: AsyncConnection, model: type[KnowledgeDocument] | type[KnowledgeChunk]) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def test_document_with_two_chunks_roundtrip(db: AsyncConnection) -> None:
    await _insert_document(db)
    await db.execute(
        KnowledgeChunk.__table__.insert().values(_chunk(chunk_index=0, content="11:30"))
    )
    await db.execute(
        KnowledgeChunk.__table__.insert().values(_chunk(chunk_index=1, content="13:00"))
    )

    # 显式列选择，不 select 实体：AsyncConnection 上 `select(Entity)` 走 Core 行处理，
    # scalar_one() 会拿到首列（document_id），兄弟用例也是这个写法
    document = (
        await db.execute(
            select(
                KnowledgeDocument.title,
                KnowledgeDocument.category,
                KnowledgeDocument.effective_until,
                KnowledgeDocument.created_at,
                KnowledgeDocument.updated_at,
            ).where(KnowledgeDocument.document_id == DOCUMENT_ID)
        )
    ).one()
    assert document.title == "午餐时间"
    assert document.category == "meal"
    assert document.effective_until == EFFECTIVE_UNTIL
    # server default 生效：两个时间列都由 PG now() 填上（UTC 存储）
    assert document.created_at is not None
    assert document.updated_at is not None

    chunks = (
        await db.execute(
            select(KnowledgeChunk.chunk_index, KnowledgeChunk.content)
            .where(KnowledgeChunk.document_id == DOCUMENT_ID)
            .order_by(KnowledgeChunk.chunk_index)
        )
    ).all()
    assert [row.chunk_index for row in chunks] == [0, 1]
    assert [row.content for row in chunks] == ["11:30", "13:00"]


async def test_duplicate_title_version_raises_integrity_error(db: AsyncConnection) -> None:
    await _insert_document(db)

    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await _insert_document(db)

    # 唯一约束拦在库层：没有多出第二行
    assert await _count(db, KnowledgeDocument) == 1


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        pytest.param("category", "weather", id="category"),
        pytest.param("authority_level", "family", id="authority-level"),
        pytest.param("status", "archived", id="status"),
    ],
)
async def test_enum_check_rejects_bad_value(
    db: AsyncConnection, field: str, bad_value: str
) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await _insert_document(db, **{field: bad_value})

    assert await _count(db, KnowledgeDocument) == 0


@pytest.mark.parametrize(
    "effective_until",
    [
        pytest.param(EFFECTIVE_FROM, id="equal"),
        pytest.param(EFFECTIVE_FROM - datetime.timedelta(days=1), id="earlier"),
    ],
)
async def test_effective_until_must_be_after_effective_from(
    db: AsyncConnection, effective_until: datetime.datetime
) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await _insert_document(db, effective_until=effective_until)

    assert await _count(db, KnowledgeDocument) == 0


async def test_open_ended_effective_range_is_allowed(db: AsyncConnection) -> None:
    # NULL = 长期有效：CHECK 写成 `effective_until IS NULL OR ...`，空值必须放行
    await _insert_document(db, effective_until=None)

    row = (await db.execute(select(KnowledgeDocument.effective_until))).scalar_one()
    assert row is None


async def test_version_must_be_positive(db: AsyncConnection) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await _insert_document(db, version=0)

    assert await _count(db, KnowledgeDocument) == 0


async def test_duplicate_chunk_index_raises_integrity_error(db: AsyncConnection) -> None:
    await _insert_document(db)
    await db.execute(
        KnowledgeChunk.__table__.insert().values(_chunk(chunk_index=0, content="11:30"))
    )

    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                KnowledgeChunk.__table__.insert().values(_chunk(chunk_index=0, content="重复"))
            )

    assert await _count(db, KnowledgeChunk) == 1


async def test_deleting_document_cascades_chunks(db: AsyncConnection) -> None:
    await _insert_document(db)
    await db.execute(
        KnowledgeChunk.__table__.insert().values(_chunk(chunk_index=0, content="11:30"))
    )
    await db.execute(
        KnowledgeChunk.__table__.insert().values(_chunk(chunk_index=1, content="13:00"))
    )

    await db.execute(
        KnowledgeDocument.__table__.delete().where(KnowledgeDocument.document_id == DOCUMENT_ID)
    )

    assert await _count(db, KnowledgeDocument) == 0
    assert await _count(db, KnowledgeChunk) == 0
