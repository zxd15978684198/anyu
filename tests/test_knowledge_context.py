"""知识链路集成用例（DAG N21 检索 × N23 上下文组装，只读 + 新测试）。

## 为什么要有这个文件

`tests/test_knowledge.py`（检索层 28 例）与 `tests/test_context_build.py`（组装层 23 例）
各自都是绿的，但两边都拿着**自己造的假数据**：检索层只看 `KnowledgeHit`，组装层只看
`_Hit` 这种最小字段对象。于是「一条真实知识命中 → 渲染进 knowledge 层 → 进 prompt」
这条链**没有任何一条用例覆盖**——两边都绿而接线是空的，这正是模块级单测最容易骗人的地方。

本文件把三段串起来跑真链路（真库、真 `ingest_document`、真 `search_knowledge_result`、
真 `build_context`），只证四件事：

1. **命中即渲染且可核验**：上下文里同时出现标题、生效日期、权威级别与正文
   （AC-43：模型说得出「哪份文件、从哪天生效、什么级别」）。
2. **未生效版本不得混入**：同标题、`effective_from` 在未来的新版即使距离更近，
   也不许出现在命中里，更不许出现在 prompt 里（FR-KNW-02）。
3. **无命中不补造**：空查询 / 已过期 / 类别不匹配三种情况，知识层**整段缺席**，
   且上下文里不出现「根据我的经验」这类补造措辞（FR-KNW-05 / AC-47）。
4. **预算裁剪保住最相关一条**：预算只放得下一条时保留第一条（最相关），
   其余被裁且 `dropped["knowledge"]` 如实计数；两两比较顺序，避免「检索顺序」与
   「渲染顺序」各说各话。

第 5 条单独成例：**同一 query 两次检索必须同序**。`ORDER BY distance, authority_rank,
effective_from DESC, chunk_id` 的最后一项是 tie-break —— 没有它，距离完全相同的两条
在两次查询里可能换位，评测抖动会被误读成模型不稳。

## 跑法

```bash
export ANYU_TEST_DATABASE_URL=postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test  # pragma: allowlist secret
uv run pytest tests/test_knowledge_context.py -q
```

必须有 `ANYU_TEST_DATABASE_URL`：`tests/conftest.py` 据此改写 `DATABASE_URL`，
裸跑会把 `TRUNCATE` 打到开发库（AGENTS §9 第三条）。夹具与 `tests/test_knowledge.py`
同构：自建 NullPool 引擎，每例一个外层事务、结束回滚，不脏库。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.context_build import ContextBudget, build_context
from app.models.knowledge import EMBEDDING_DIM
from app.models.staff import StaffAccount
from app.services import knowledge_service
from app.services.alert_aggregation import FORBIDDEN_WORDING
from app.services.knowledge_service import CONTEXT_FOOTER
from conftest import require_db_connection

#: 固定「此刻」= 2026-11-09 12:00 上海时间（04:00 UTC），边界断言不用 `now()`。
NOW = datetime.datetime(2026, 11, 9, 4, 0, tzinfo=datetime.UTC)
DAY = datetime.timedelta(days=1)
STAFF_ID = uuid.UUID("c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1")

PREFIX = "你是小伴，陪长者聊天。"

#: 补造措辞黑名单。与 `alert_aggregation.FORBIDDEN_WORDING`（诊断口径）分开列：
#: 那边防的是医疗结论，这边防的是「没查到就编一段」——两类红线，两类措辞。
FABRICATION_WORDING: tuple[str, ...] = (
    "根据我的经验",
    "凭我的经验",
    "据我所知",
    "一般来说",
    "通常来说",
    "我记得",
)


def _basis(index: int) -> list[float]:
    """第 `index` 个分量是 1、其余是 0 的单位向量。

    这样「距离」是可控的：查询向量取 `_basis(0)` 时，同轴文档距离 0（最相关），
    第 1 轴的距离是 1（次相关）。用例不依赖真实 embedding，也就不会因为换模型而抖动。
    """
    values = [0.0] * EMBEDDING_DIM
    values[index] = 1.0
    return values


class AxisProvider:
    """把一个固定轴向量喂给整段文本：写入与检索各用一个实例，完全离线。"""

    def __init__(self, axis: int) -> None:
        self._axis = axis
        self.calls = 0

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    @property
    def available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [_basis(self._axis) for _ in texts]


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
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def admin(db: AsyncSession) -> uuid.UUID:
    """一名 admin：`created_by` / `updated_by` 有外键，先有主表行才插得进文档。"""
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


# ------------------------------------------------------------------ 造数与断言助手


async def _ingest(
    db: AsyncSession,
    *,
    title: str,
    body: str,
    category: str = "meal",
    authority_level: str = "institution",
    version: int = 1,
    effective_from: datetime.datetime,
    effective_until: datetime.datetime | None = None,
    axis: int = 0,
) -> uuid.UUID:
    """走**真实写入口** `ingest_document`（含分块、审计、向量非空校验），不用裸 INSERT。

    裸 INSERT 只证明「表能写」，证明不了「写进去的东西检得出来」——而本文件要证的
    正是后半句，所以写入必须走生产用的那条路径。
    """
    document = await knowledge_service.ingest_document(
        db,
        title=title,
        body=body,
        category=category,
        authority_level=authority_level,
        version=version,
        effective_from=effective_from,
        effective_until=effective_until,
        actor_id=STAFF_ID,
        embedding_provider=AxisProvider(axis),
    )
    return document.document_id


def _unique_title(base: str) -> str:
    """标题加随机后缀：测试库若残留同名文档，(title, version) 唯一键会撞，报错与业务无关。"""
    return f"{base}-{uuid.uuid4().hex[:8]}"


def _context_texts(ctx: Any) -> list[str]:
    """只取每条消息的 content：本文件只关心「发到 prompt 里的字节」。"""
    return [message["content"] for message in ctx.messages]


def _assert_no_fabrication(texts: Sequence[str]) -> None:
    """上下文里既不许有诊断口径，也不许有「没查到就编一段」的口吻。"""
    joined = "\n".join(texts)
    for wording in (*FORBIDDEN_WORDING, *FABRICATION_WORDING):
        assert wording not in joined, f"上下文出现了红线措辞：{wording!r}"


# ------------------------------------------------------------------ ① 命中即渲染


async def test_active_version_hit_is_rendered_with_source_effective_and_authority(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """一条真知识命中后，上下文同时带标题、生效日期、权威级别与正文（AC-43）。"""
    title = _unique_title("午餐时间规定")
    await _ingest(
        db,
        title=title,
        body="午餐 11:30 开餐，13:00 收餐。",
        version=1,
        effective_from=NOW - DAY,
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", now=NOW, embedding_provider=AxisProvider(0)
    )
    assert result.reason == "ok"
    assert len(result.hits) == 1

    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        estimate_tokens=len,
    )

    # 知识层是最后一条 system 消息：调用方在本函数返回后追加本轮用户话（模块 docstring）。
    assert ctx.messages[0] == {"role": "system", "content": PREFIX}
    assert ctx.messages[-1]["role"] == "system"
    text = ctx.messages[-1]["content"]
    assert f"[参考资料 1｜来源: {title}" in text
    # NOW - DAY = 2026-11-08 04:00 UTC → 上海 12:00，展示成日期
    assert "｜生效: 2026-11-08" in text
    assert "｜权威级别: institution]" in text
    assert "午餐 11:30 开餐" in text
    assert CONTEXT_FOOTER in text
    assert ctx.dropped["knowledge"] == 0
    _assert_no_fabrication(_context_texts(ctx))


async def test_unborn_version_never_reaches_context(db: AsyncSession, admin: uuid.UUID) -> None:
    """同标题的未来版本即使更近，也不得进命中、不得进 prompt（FR-KNW-02）。"""
    title = _unique_title("午餐时间规定")
    await _ingest(
        db,
        title=title,
        body="午餐 11:30 开餐，13:00 收餐。",
        version=1,
        effective_from=NOW - DAY,
    )
    await _ingest(
        db,
        title=title,
        body="（新版尚未生效）午餐 12:00 开餐。",
        version=2,
        effective_from=NOW + DAY,
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", now=NOW, embedding_provider=AxisProvider(0)
    )

    assert [hit.version for hit in result.hits] == [1]
    assert all("尚未生效" not in hit.content for hit in result.hits)

    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        estimate_tokens=len,
    )
    text = ctx.messages[-1]["content"]
    assert "午餐 11:30 开餐" in text
    assert "12:00" not in text, "未生效版本混进了 prompt"
    assert "尚未生效" not in text


# ------------------------------------------------------------------ ② 无命中不补造


async def test_empty_query_leaves_no_knowledge_layer(db: AsyncSession, admin: uuid.UUID) -> None:
    """空查询不触发检索、不产出知识层，也不产出任何补造段落。"""
    await _ingest(
        db, title=_unique_title("午餐时间规定"), body="午餐 11:30 开餐。", effective_from=NOW - DAY
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="   ", now=NOW, embedding_provider=AxisProvider(0)
    )
    assert result.reason == "empty_query"
    assert result.hits == ()

    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        estimate_tokens=len,
    )

    assert ctx.messages == ({"role": "system", "content": PREFIX},)
    assert ctx.dropped["knowledge"] == 0
    assert "参考资料" not in _context_texts(ctx)[0]
    _assert_no_fabrication(_context_texts(ctx))


async def test_expired_document_leaves_no_knowledge_layer(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """已过期文档不算命中：上下文里没有知识层，而不是「硬塞一条过期的」。"""
    await _ingest(
        db,
        title=_unique_title("旧的就餐规定"),
        body="旧版：午餐 10:30 开餐。",
        effective_from=NOW - 2 * DAY,
        effective_until=NOW - DAY,
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", now=NOW, embedding_provider=AxisProvider(0)
    )
    assert result.reason == "no_match"
    assert result.hits == ()

    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        estimate_tokens=len,
    )

    assert ctx.messages == ({"role": "system", "content": PREFIX},)
    assert ctx.dropped["knowledge"] == 0
    _assert_no_fabrication(_context_texts(ctx))


async def test_category_mismatch_leaves_no_knowledge_layer(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """类别过滤后无命中：知识层缺席，不会「退一步把别的类别拿来用」。"""
    await _ingest(
        db,
        title=_unique_title("午餐时间规定"),
        body="午餐 11:30 开餐。",
        category="meal",
        effective_from=NOW - DAY,
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", category="schedule", now=NOW, embedding_provider=AxisProvider(0)
    )
    assert result.reason == "no_match"

    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        estimate_tokens=len,
    )

    assert ctx.messages == ({"role": "system", "content": PREFIX},)
    assert "11:30" not in "".join(_context_texts(ctx))
    _assert_no_fabrication(_context_texts(ctx))


# ------------------------------------------------------------------ ③ 预算裁剪


async def test_budget_keeps_most_relevant_hit_only(db: AsyncSession, admin: uuid.UUID) -> None:
    """预算只放得下一条时，保留最相关那条，其余被裁且计数如实（dropped > 0）。"""
    await _ingest(
        db,
        title=_unique_title("午餐时间规定"),
        body="午餐 11:30 开餐，13:00 收餐。",
        effective_from=NOW - DAY,
        axis=0,
    )
    await _ingest(
        db,
        title=_unique_title("加餐安排"),
        body="下午三点供应点心。",
        category="activity",
        effective_from=NOW - DAY,
        axis=1,
    )

    result = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", now=NOW, embedding_provider=AxisProvider(0)
    )
    assert len(result.hits) == 2
    # 距离 0 的那条（第 0 轴）必须排第一，否则「最相关」无从谈起。
    assert result.hits[0].distance == pytest.approx(0.0)
    assert result.hits[1].distance == pytest.approx(1.0)

    roomy = ContextBudget(total_tokens=100_000)
    full_ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        budget=roomy,
        estimate_tokens=len,
    )
    full_text = full_ctx.messages[-1]["content"]
    assert len(full_ctx.messages) == 2

    single_ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits[:1],
        budget=roomy,
        estimate_tokens=len,
    )
    single_text = single_ctx.messages[-1]["content"]
    assert len(full_text) > len(single_text)

    # 预算 = 前缀 + 第一条的**真实渲染字节**：多一个字符都塞不下第二条。
    tight = ContextBudget(
        total_tokens=len(PREFIX) + len(single_text),
        prefix_reserve=len(PREFIX),
        recent_reserve=0,
        summary_reserve=0,
        state_reserve=0,
    )
    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        budget=tight,
        estimate_tokens=len,
    )

    assert ctx.messages[-1]["content"] == single_text
    assert ctx.dropped["knowledge"] == 1
    assert result.hits[1].content not in ctx.messages[-1]["content"]
    assert ctx.used_tokens == len(PREFIX) + len(single_text)


async def test_zero_budget_for_knowledge_drops_every_hit(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """知识层余量为 0 时整层消失，且不计「部分渲染」——空层不产出占位。"""
    await _ingest(
        db, title=_unique_title("午餐时间规定"), body="午餐 11:30 开餐。", effective_from=NOW - DAY
    )
    result = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", now=NOW, embedding_provider=AxisProvider(0)
    )
    assert len(result.hits) == 1

    tight = ContextBudget(
        total_tokens=len(PREFIX),
        prefix_reserve=len(PREFIX),
        recent_reserve=0,
        summary_reserve=0,
        state_reserve=0,
    )
    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=result.hits,
        budget=tight,
        estimate_tokens=len,
    )

    assert ctx.messages == ({"role": "system", "content": PREFIX},)
    assert ctx.dropped["knowledge"] == 1
    assert ctx.used_tokens == len(PREFIX)


# ------------------------------------------------------------------ ④ 可复现性


async def test_equal_distance_hits_are_ordered_deterministically(
    db: AsyncSession, admin: uuid.UUID
) -> None:
    """距离完全相同的两条必须每次同序：authority 只做 tie-break，顺序不能随查询计划漂。"""
    await _ingest(
        db,
        title=_unique_title("机构食堂规定"),
        body="午餐 11:30 开餐。",
        authority_level="institution",
        effective_from=NOW - DAY,
        axis=0,
    )
    await _ingest(
        db,
        title=_unique_title("长者自述用餐时间"),
        body="长者说他一般 12 点吃午饭。",
        authority_level="elder_memory",
        effective_from=NOW - DAY,
        axis=0,
    )

    first = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", now=NOW, embedding_provider=AxisProvider(0)
    )
    second = await knowledge_service.search_knowledge_result(
        db, query="午餐几点开餐", now=NOW, embedding_provider=AxisProvider(0)
    )

    assert len(first.hits) == 2
    # 距离是真平手：这样「顺序稳定」才是在验 tie-break，而不是在验距离不同。
    assert first.hits[0].distance == pytest.approx(first.hits[1].distance)

    first_ids = [hit.chunk_id for hit in first.hits]
    second_ids = [hit.chunk_id for hit in second.hits]
    assert first_ids == second_ids, "同一 query 两次检索顺序不一致，tie-break 失效"

    authorities = [hit.authority_level for hit in first.hits]
    assert authorities.index("institution") < authorities.index("elder_memory"), (
        "同距离时机构规定应排在「长者自述」之前（FR-KNW-02 的 tie-break 口径）"
    )
