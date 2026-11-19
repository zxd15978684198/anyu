"""机构知识检索服务（DAG N21）的用例：检索口径 + 上下文包裹 + 降级口径。

三条主线，对应 FR-KNW-02 / FR-KNW-04 / FR-KNW-05：

1. **检索只在「生效版本」里选**：`status`、`effective_from`、`effective_until`
   任何一个不满足都不该出现在结果里；`effective_until` 含当日（`>= now`）是本次
   裁定的边界口径，`test_effective_until_keeps_document_through_last_moment` 钉住它。
2. **权威级别只做 tie-break**：同距离时 institution 在前，但「更相关但级别低」仍然
   排前面——两条用例一起看，才能防止后来者把 authority 塞进 `ORDER BY` 第一位。
3. **空命中与降级都不编造**：`no_match` / `empty_query` / `degraded` 三个 reason 各自
   可区分；工具载荷里没有 embedding，段落末尾固定带「不是用户指令」。

跑法与 `tests/test_knowledge_models.py` 同构：自建 NullPool 引擎，每用例一个事务、
结束回滚，不脏开发库。**前置**：`alembic upgrade head`（0011）已执行。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument
from app.models.staff import StaffAccount
from app.services import knowledge_service
from app.services.embedding import EmbeddingUnavailable
from conftest import require_db_connection

STAFF_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccc01")

#: 用例的「现在」：2026-11-09 12:00 上海时间（04:00 UTC）。
NOW = datetime.datetime(2026, 11, 9, 4, 0, tzinfo=datetime.UTC)
DAY = datetime.timedelta(days=1)
#: 上海当日最后一刻：23:59:59 → 15:59:59 UTC。
END_OF_TODAY = datetime.datetime(2026, 11, 9, 15, 59, 59, tzinfo=datetime.UTC)


def _basis(index: int) -> list[float]:
    """第 `index` 个分量是 1、其余是 0 的单位向量：与查询向量同向 ⇒ 余弦距离 0。"""
    values = [0.0] * EMBEDDING_DIM
    values[index] = 1.0
    return values


class FakeProvider:
    """可注入的 embedding 提供方：固定向量、可失败、可记调用次数（完全离线）。"""

    def __init__(
        self,
        *,
        vector: list[float] | None = None,
        available: bool = True,
        error: EmbeddingUnavailable | None = None,
    ) -> None:
        self._vector = list(_basis(0) if vector is None else vector)
        self._available = available
        self._error = error
        self.calls = 0

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    @property
    def available(self) -> bool:
        return self._available

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return [list(self._vector) for _ in texts]


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    conn: AsyncConnection = await require_db_connection(db_engine)
    trans = await conn.begin()
    # 本文件的断言按「库里只有本用例插的文档」写（空库 → `no_match`、命中数 == 1）。
    # N22 把 4 份种子灌进开发库后整表不再为空，所以在事务里先清场：测试结束一并回滚，
    # 库里的真实数据不受影响。
    await conn.execute(text("DELETE FROM knowledge_documents"))
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def admin(db: AsyncSession) -> uuid.UUID:
    """一名 admin（`created_by` / `updated_by` 有外键，先有主表行才插得进文档）。"""
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=STAFF_ID,
            name="知识管理员",
            role="admin",
            password_hash="not-a-real-hash",  # pragma: allowlist secret
            status="active",
        )
    )
    return STAFF_ID


# ------------------------------------------------------------------ 造数助手


async def _add_document(
    db: AsyncSession,
    *,
    title: str,
    frequency: int,
    category: str = "meal",
    authority_level: str = "institution",
    version: int = 1,
    effective_from: datetime.datetime = NOW - DAY,
    effective_until: datetime.datetime | None = None,
    status: str = "active",
    content: str | None = None,
) -> uuid.UUID:
    document_id = uuid.uuid4()
    await db.execute(
        KnowledgeDocument.__table__.insert().values(
            document_id=document_id,
            title=title,
            category=category,
            authority_level=authority_level,
            version=version,
            effective_from=effective_from,
            effective_until=effective_until,
            status=status,
            body=f"{title} 的正文",
            created_by=STAFF_ID,
            updated_by=STAFF_ID,
        )
    )
    await db.execute(
        KnowledgeChunk.__table__.insert().values(
            document_id=document_id,
            chunk_index=0,
            content=content or f"{title}：午餐 11:30 开餐",
            embedding=_basis(frequency),
        )
    )
    return document_id


# ------------------------------------------------------------------ 检索口径


async def test_hits_are_ordered_by_distance(db: AsyncSession, admin: uuid.UUID) -> None:
    await _add_document(db, title="用餐时间", frequency=1, content="午餐 11:30 开餐")
    await _add_document(db, title="探视规定", frequency=0, content="探视需提前预约")

    result = await knowledge_service.search_knowledge_result(
        db, query="午饭几点", now=NOW, embedding_provider=FakeProvider()
    )

    assert result.reason == "ok"
    assert [hit.title for hit in result.hits] == ["探视规定", "用餐时间"]
    assert result.hits[0].distance == pytest.approx(0.0, abs=1e-6)
    assert result.hits[1].distance == pytest.approx(1.0, abs=1e-6)


async def test_hit_carries_source_authority_and_effective_range(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    # 生效起点放在上海昨天 00:00，确保「按日展示」的口径能对上。
    effective_from = datetime.datetime(2026, 11, 8, 16, 0, tzinfo=datetime.UTC)
    document_id = await _add_document(
        db, title="午餐时间", frequency=0, effective_from=effective_from, version=3
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider()
    )

    hit = result.hits[0]
    assert hit.document_id == document_id
    assert hit.title == "午餐时间"
    assert hit.category == "meal"
    assert hit.authority_level == "institution"
    assert hit.version == 3
    assert hit.effective_from == effective_from
    assert hit.effective_until is None
    assert hit.similarity == pytest.approx(1.0, abs=1e-6)


async def test_category_filter_narrows_candidates(db: AsyncSession, admin: uuid.UUID) -> None:
    await _add_document(db, title="用餐时间", frequency=0, category="meal")
    await _add_document(db, title="探视规定", frequency=0, category="policy")

    result = await knowledge_service.search_knowledge_result(
        db, query="几点", category="policy", now=NOW, embedding_provider=FakeProvider()
    )

    assert [hit.title for hit in result.hits] == ["探视规定"]


@pytest.mark.parametrize("status", ["draft", "retired"])
async def test_only_active_documents_are_searchable(
    db: AsyncSession, admin: uuid.UUID, status: str
) -> None:
    await _add_document(db, title="草稿规定", frequency=0, status=status)

    result = await knowledge_service.search_knowledge_result(
        db, query="规定", now=NOW, embedding_provider=FakeProvider()
    )

    assert result.hits == ()
    assert result.reason == "no_match"


async def test_future_effective_from_is_not_searchable(db: AsyncSession, admin: uuid.UUID) -> None:
    await _add_document(db, title="下月生效", frequency=0, effective_from=NOW + DAY)

    result = await knowledge_service.search_knowledge_result(
        db, query="规定", now=NOW, embedding_provider=FakeProvider()
    )

    assert result.hits == ()
    assert result.reason == "no_match"


async def test_expired_document_is_not_returned(db: AsyncSession, admin: uuid.UUID) -> None:
    await _add_document(
        db,
        title="旧用餐时间",
        frequency=0,
        effective_from=NOW - 10 * DAY,
        effective_until=NOW - DAY,
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider()
    )

    assert result.hits == ()
    assert result.reason == "no_match"


async def test_effective_until_keeps_document_through_last_moment(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """边界口径：`effective_until` **含当日**（`>= now`）。

    今天 23:59:59 到期的文档，今天 12:00 可检索；过了 23:59:59 那一刻才失效。
    写成 `> now` 会让「今天到期的规定」今天下午就查不到——正是这次要钉住的分歧。
    """
    await _add_document(
        db,
        title="今天的用餐时间",
        frequency=0,
        effective_from=NOW - 10 * DAY,
        effective_until=END_OF_TODAY,
    )

    today = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider()
    )
    assert [hit.title for hit in today.hits] == ["今天的用餐时间"]

    just_after = await knowledge_service.search_knowledge_result(
        db,
        query="午餐",
        now=END_OF_TODAY + datetime.timedelta(seconds=1),
        embedding_provider=FakeProvider(),
    )
    assert just_after.hits == ()
    assert just_after.reason == "no_match"


async def test_authority_level_breaks_ties_only(db: AsyncSession, admin: uuid.UUID) -> None:
    """同距离时 institution 在前；**距离不同时权威级别不许插队**。"""
    await _add_document(db, title="长者自述的用餐习惯", frequency=0, authority_level="elder_memory")
    await _add_document(db, title="机构用餐时间", frequency=0, authority_level="institution")

    tied = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider(), limit=2
    )
    # 两条距离都是 0：此时才轮到权威级别，institution 在前。
    assert [hit.title for hit in tied.hits] == ["机构用餐时间", "长者自述的用餐习惯"]
    assert tied.hits[0].distance == pytest.approx(tied.hits[1].distance, abs=1e-6)

    # 把 institution 那条推到距离 1：它更不相关，就不许因为权威级别翻到前面。
    await db.execute(
        KnowledgeChunk.__table__.update()
        .where(KnowledgeChunk.content.like("机构用餐时间%"))
        .values(embedding=_basis(1))
    )
    closer = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider(), limit=1
    )
    assert [hit.title for hit in closer.hits] == ["长者自述的用餐习惯"]


async def test_search_respects_limit(db: AsyncSession, admin: uuid.UUID) -> None:
    for index in range(3):
        await _add_document(db, title=f"资料{index}", frequency=index)

    result = await knowledge_service.search_knowledge_result(
        db, query="资料", now=NOW, embedding_provider=FakeProvider(), limit=2
    )

    assert len(result.hits) == 2


# ------------------------------------------------------------------ 入参与失败口径


async def test_empty_query_short_circuits_without_embedding(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    provider = FakeProvider()

    result = await knowledge_service.search_knowledge_result(
        db, query="   ", now=NOW, embedding_provider=provider
    )

    assert result.reason == "empty_query"
    assert result.hits == ()
    assert provider.calls == 0


async def test_overlong_query_raises(db: AsyncSession) -> None:
    with pytest.raises(knowledge_service.KnowledgeValidationError):
        await knowledge_service.search_knowledge_result(
            db, query="药" * (knowledge_service.MAX_QUERY_LENGTH + 1), now=NOW
        )


@pytest.mark.parametrize("category", ["weather", "Meal", ""])
async def test_invalid_category_raises(db: AsyncSession, category: str) -> None:
    with pytest.raises(knowledge_service.KnowledgeValidationError):
        await knowledge_service.search_knowledge_result(
            db, query="午餐", category=category, now=NOW
        )


@pytest.mark.parametrize("limit", [0, 21, -1, True])
async def test_limit_out_of_range_raises(db: AsyncSession, limit: object) -> None:
    with pytest.raises(knowledge_service.KnowledgeValidationError):
        await knowledge_service.search_knowledge_result(
            db,
            query="午餐",
            limit=limit,
            now=NOW,  # type: ignore[arg-type]
        )


async def test_embedding_unavailable_degrades_instead_of_raising(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    await _add_document(db, title="用餐时间", frequency=0)

    result = await knowledge_service.search_knowledge_result(
        db,
        query="午餐",
        now=NOW,
        embedding_provider=FakeProvider(error=EmbeddingUnavailable("timeout", "test")),
    )

    assert result.hits == ()
    assert result.reason == "degraded"
    assert result.degraded is True
    assert result.as_tool_payload() == {"hits": [], "reason": "degraded"}


async def test_provider_not_available_short_circuits(db: AsyncSession) -> None:
    provider = FakeProvider(available=False)

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=provider
    )

    assert result.reason == "degraded"
    assert provider.calls == 0


async def test_bad_vector_dimension_degrades(db: AsyncSession, admin: uuid.UUID) -> None:
    await _add_document(db, title="用餐时间", frequency=0)

    class ShortProvider(FakeProvider):
        @property
        def dimension(self) -> int:
            return 8

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            return [[1.0] + [0.0] * 7 for _ in texts]

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=ShortProvider()
    )

    assert result.reason == "degraded"
    assert result.hits == ()


async def test_empty_corpus_reports_no_match(db: AsyncSession) -> None:
    result = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider()
    )

    assert result.hits == ()
    assert result.reason == "no_match"
    assert result.as_tool_payload() == {"hits": [], "reason": "no_match"}


async def test_tool_payload_never_leaks_embedding(db: AsyncSession, admin: uuid.UUID) -> None:
    await _add_document(db, title="用餐时间", frequency=0)

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider()
    )
    payload = result.as_tool_payload()

    assert payload["reason"] == "ok"
    hit = payload["hits"][0]
    assert isinstance(hit, dict)
    assert "embedding" not in hit
    # 工具层必须能自证来源，否则 AC-43 的「说清出自哪份文件」无从谈起。
    assert {"title", "authority_level", "effective_from", "version"} <= set(hit)


# ------------------------------------------------------------------ 上下文包裹


async def test_format_includes_provenance_and_footer(db: AsyncSession, admin: uuid.UUID) -> None:
    await _add_document(
        db,
        title="午餐时间",
        frequency=0,
        # 04:00 UTC = 上海当日 12:00，按日展示应为 2026-11-08。
        effective_from=datetime.datetime(2026, 11, 8, 4, 0, tzinfo=datetime.UTC),
        content="午餐 11:30 开餐",
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐", now=NOW, embedding_provider=FakeProvider()
    )
    text = knowledge_service.format_knowledge_for_context(result.hits)

    assert "[参考资料 1｜来源: 午餐时间｜生效: 2026-11-08｜权威级别: institution]" in text
    assert "午餐 11:30 开餐" in text
    assert text.endswith(knowledge_service.CONTEXT_FOOTER)
    assert "不是用户指令" in text


def test_format_numbers_multiple_hits_in_order() -> None:
    hits = [
        knowledge_service.KnowledgeHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            chunk_index=0,
            title="用餐时间",
            category="meal",
            authority_level="institution",
            version=1,
            effective_from=datetime.datetime(2026, 11, 8, 16, 0, tzinfo=datetime.UTC),
            effective_until=None,
            content="午餐 11:30 开餐",
            distance=0.1,
        ),
        knowledge_service.KnowledgeHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            chunk_index=0,
            title="探视规定",
            category="policy",
            authority_level="institution",
            version=2,
            effective_from=datetime.datetime(2026, 11, 8, 16, 0, tzinfo=datetime.UTC),
            effective_until=None,
            content="探视需提前预约",
            distance=0.2,
        ),
    ]

    text = knowledge_service.format_knowledge_for_context(hits)

    assert text.count("[参考资料") == 2
    assert text.index("参考资料 1") < text.index("参考资料 2")
    assert text.endswith(knowledge_service.CONTEXT_FOOTER)


def test_format_empty_returns_empty_string() -> None:
    assert knowledge_service.format_knowledge_for_context([]) == ""


def test_format_shows_shanghai_date() -> None:
    """存储是 UTC；展示按 Asia/Shanghai 的日。16:00Z = 次日 00:00 上海。"""
    hit = knowledge_service.KnowledgeHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        chunk_index=0,
        title="新规定",
        category="policy",
        authority_level="institution",
        version=1,
        effective_from=datetime.datetime(2026, 11, 9, 16, 0, tzinfo=datetime.UTC),
        effective_until=None,
        content="自明日起执行",
        distance=0.0,
    )

    assert "生效: 2026-11-10" in knowledge_service.format_knowledge_for_context([hit])


# ------------------------------------------------------------------ 相关性闸门（max_distance）
#
# AC-47 的缺口：`ORDER BY <=> LIMIT k` 只保证「候选集里最近的 k 条」，库非空就恒有 k 条，
# 不设闸门时「库里没有的信息」必然命中。下面四条钉住闸门的三个口径：
# 阈值内的留下、全超阈值 → no_match（不是 degraded）、None 时旧行为一字不变。


async def test_max_distance_keeps_hits_within_threshold(db: AsyncSession, admin: uuid.UUID) -> None:
    """distance ≤ 阈值的命中保留；超阈值的丢弃，且**不看** authority_level 救回来。"""
    await _add_document(db, title="用餐时间", frequency=0, content="午餐 11:30 开餐")
    await _add_document(db, title="探视规定", frequency=1, content="探视需提前预约")

    result = await knowledge_service.search_knowledge_result(
        db, query="午饭几点", now=NOW, embedding_provider=FakeProvider(), max_distance=0.4
    )

    assert [hit.title for hit in result.hits] == ["用餐时间"]
    assert result.reason == "ok"
    assert result.hits[0].distance <= 0.4


async def test_max_distance_all_beyond_threshold_is_no_match(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """候选非空但全部超阈值 → 空结果 + `no_match`（**不是** degraded，也不是 ok）。"""
    await _add_document(db, title="探视规定", frequency=1, content="探视需提前预约")

    result = await knowledge_service.search_knowledge_result(
        db, query="月亮上有没有人", now=NOW, embedding_provider=FakeProvider(), max_distance=0.4
    )

    assert result.hits == ()
    assert result.reason == "no_match"
    assert result.degraded is False
    # 工具层看到的是「查了，没有」，模型因此不会把「没有」读成「工具坏了」。
    assert result.as_tool_payload() == {"hits": [], "reason": "no_match"}


async def test_max_distance_none_keeps_legacy_behaviour(db: AsyncSession, admin: uuid.UUID) -> None:
    """不传 / 显式传 None 都回到旧行为：同样的命中、同样的顺序、同样的 reason。"""
    await _add_document(db, title="用餐时间", frequency=1, content="午餐 11:30 开餐")
    await _add_document(db, title="探视规定", frequency=0, content="探视需提前预约")

    legacy = await knowledge_service.search_knowledge_result(
        db, query="午饭几点", now=NOW, embedding_provider=FakeProvider()
    )
    explicit_none = await knowledge_service.search_knowledge_result(
        db, query="午饭几点", now=NOW, embedding_provider=FakeProvider(), max_distance=None
    )

    assert explicit_none == legacy
    assert [hit.title for hit in legacy.hits] == ["探视规定", "用餐时间"]
    assert legacy.reason == "ok"
    # 旧行为：1.0 的远命中照样返回（闸门才是让它变成 no_match 的原因）。
    assert legacy.hits[1].distance == pytest.approx(1.0, abs=1e-6)


async def test_max_distance_near_zero_only_keeps_near_duplicates(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """阈值接近 0 = 只接受几乎同向的命中（float4 的 `<=>` 可能有 1e-7 级误差，
    故取 1e-6 而不是 0.0：断言的是「闸门严格生效」，不是 pgvector 的数值精确性）。"""
    await _add_document(db, title="用餐时间", frequency=0, content="午餐 11:30 开餐")
    await _add_document(db, title="探视规定", frequency=1, content="探视需提前预约")

    result = await knowledge_service.search_knowledge_result(
        db, query="午饭几点", now=NOW, embedding_provider=FakeProvider(), max_distance=1e-6
    )

    assert [hit.title for hit in result.hits] == ["用餐时间"]
    assert result.reason == "ok"


async def test_max_distance_empty_corpus_still_reports_no_match(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """库本来就空：闸门不改变 conclusion（no_match），也不把它变成 degraded。"""
    result = await knowledge_service.search_knowledge_result(
        db, query="午饭几点", now=NOW, embedding_provider=FakeProvider(), max_distance=0.4
    )

    assert result.hits == ()
    assert result.reason == "no_match"


@pytest.mark.parametrize("max_distance", [-0.1, 2.1, True, "0.4", float("nan"), float("inf")])
async def test_invalid_max_distance_raises(
    db: AsyncSession, admin: uuid.UUID, max_distance: object
) -> None:
    """非法阈值是参数错误，不静默当成「没设闸门」（静默会把配置错伪装成宽松召回）。"""
    with pytest.raises(knowledge_service.KnowledgeValidationError):
        await knowledge_service.search_knowledge_result(
            db,
            query="午饭几点",
            now=NOW,
            embedding_provider=FakeProvider(),
            max_distance=max_distance,  # type: ignore[arg-type]
        )


async def test_empty_query_short_circuits_before_max_distance(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """判定顺序不变：空查询先走 empty_query，不去校验闸门（也不打网络）。"""
    provider = FakeProvider()

    result = await knowledge_service.search_knowledge_result(
        db, query="   ", now=NOW, embedding_provider=provider, max_distance=-1.0
    )

    assert result.reason == "empty_query"
    assert result.hits == ()
    assert provider.calls == 0
