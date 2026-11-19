"""`memory_recall.build_memory_recaller` 的接线单测（记忆召回读路径的最后一段）。

## 测什么 / 不测什么

这里测的是**接线**，不是召回质量：

- 身份闸门：generic / 非法 UUID → `None`，且连会话都不开；
- 正常身份：闭包只认「这一句话」，结果里只有该长者的正文，`top_k` 透传；
- 副作用：`recall` 刷新的 `last_accessed` 真的落库（闭包的 `commit` 生效）；
- 缺省 provider 走模块级惰性构建 + 缓存；显式注入则完全不碰构建器；
- provider 抛 `EmbeddingUnavailable` 时**本层不抛**：`memory_service._embed_text`
  已把它降级成 `None`，于是 `recall` 返回 `[]`、闭包返回 `[]`。这条是**据实现断真**
  （读 `memory_service.py:212` 的 `_embed_text`），不是「猜异常会上抛」；
- 本层不吞的异常由**唯一**收敛点 `companion._recall_memories` 收敛成 `[]`
  （AGENTS §6「召回失败 → 本轮不加载记忆」）。

不测语义相似度：`DeterministicEmbeddingProvider` 的向量是 SHA-256 噪声，没有语义
（见 `app/services/embedding.py` 的 S-D7-1 红线）。正常路径只需要「写进去的两句能原样
取回来」，用 `top_k` / elder 过滤这类**结构**断言，而不是「相似度 0.93 说明模型懂老人」。
召回加上相关性下限（`RECALL_MIN_SIMILARITY`）之后，噪声向量连「同一位长者的其他记忆」
都会被挡掉，所以正常路径一律用本文件的 `_FixedProvider` 显式给相似度：想测接线就不能
让闸门替我们决定召回什么。

## 为什么用真库

本模块价值的一半在「SQL 真的带上了 `elder_id`」，换 sqlite 就测不到 pgvector 的
`<=>` 与那条强制过滤。夹具沿用 `tests/test_memory_service.py` 的口径：`db_conn`
每用例一个事务、结束回滚；`app.db.engine` 是模块级连接池，而 pytest-asyncio 每用例
一个新事件循环，故用例结束 dispose 掉池里的空闲连接（仓库已登记的 P-03 坑）。
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

import app.services.memory_recall as memory_recall_module
from app.core.context import DialogueIdentity
from app.db import engine as app_engine
from app.models import Elder, MemoryChunk
from app.services.embedding import (
    EMBEDDING_DIMENSION,
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
    EmbeddingUnavailable,
)
from app.services.memory_recall import RECALL_TOP_K, build_memory_recaller
from app.services.memory_service import remember

LOGGER_NAME = "app.services.memory_recall"


# ============================================================
# 夹具与小工具
# ============================================================


@pytest.fixture(autouse=True)
async def _release_pool() -> AsyncIterator[None]:
    """每个用例结束把 `app.db.engine` 池里的连接还掉。

    与 `tests/test_memory_service.py` 同一个理由（P-03）：`engine` 是模块级连接池，
    而 pytest-asyncio 每用例一个新事件循环，池里留着上个循环建的 asyncpg 连接，
    下个用例取到就会报「Event loop is closed」。只 dispose 空闲连接，不影响别的文件。
    """
    yield
    await app_engine.dispose()


def _session_factory(
    conn: AsyncConnection,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """造一个「与 `db_conn` 夹具共享同一事务」的会话工厂。

    闭包内部会 `commit()`（刷 `last_accessed`），而会话绑在夹具那条**已开启**的连接
    事务上：SQLAlchemy 2.0 的默认 `join_transaction_mode="conditional_savepoint"` 在
    「连接已在事务中且无 SAVEPOINT」时退化成 `rollback_only` —— `commit()` 不会提交
    夹具的外层事务，用例结束照旧整体回滚，数据零残留（这正是我们要的隔离性）。
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    return factory


def _unused_session_factory() -> AbstractAsyncContextManager[AsyncSession]:
    """本该被跳过的工厂：一被调用就炸，用来证明「身份不可用时不发 SQL」。"""
    raise AssertionError("identity 不可用时不该开数据库会话")


async def _seed_elder(conn: AsyncConnection) -> uuid.UUID:
    """`memory_chunks.elder_id` 的外键目标；占位姓名，不写真实长者数据（AGENTS §4.3）。"""
    elder_id = uuid.uuid4()
    await conn.execute(Elder.__table__.insert().values(elder_id=elder_id, name="测试长者"))
    return elder_id


async def _remember_many(
    conn: AsyncConnection,
    elder_id: uuid.UUID,
    contents: Sequence[str],
    provider: EmbeddingProvider,
) -> list[uuid.UUID]:
    """走真实写入路径（`memory_service.remember`）落几条 active 记忆。"""
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        chunk_ids: list[uuid.UUID] = []
        for content in contents:
            row = await remember(
                session, elder_id=elder_id, content=content, embedding_provider=provider
            )
            chunk_ids.append(row.chunk_id)
        await session.flush()
        return chunk_ids
    finally:
        await session.close()


async def _insert_chunk(
    conn: AsyncConnection,
    *,
    elder_id: uuid.UUID,
    content: str,
    embedding: list[float] | None,
) -> uuid.UUID:
    """直接落一行（绕过 `remember` 的正文校验），用于构造「空白正文」这类脏行。"""
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        row = MemoryChunk(
            elder_id=elder_id,
            content=content,
            embedding=embedding,
            category="private",
            importance=1.0,
            status="active",
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row.chunk_id
    finally:
        await session.close()


async def _fetch_chunks(conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID]) -> list[MemoryChunk]:
    """从库里重读这些行（新会话 + `populate_existing`，绕开会话内旧副本）。"""
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        rows = (
            (
                await session.execute(
                    select(MemoryChunk)
                    .where(MemoryChunk.chunk_id.in_(list(chunk_ids)))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)
    finally:
        await session.close()


def _record(caplog: pytest.LogCaptureFixture, message: str) -> logging.LogRecord:
    """取指定事件名的最后一条日志记录（`extra` 的键直接挂在 record 上）。"""
    records = [record for record in caplog.records if record.getMessage() == message]
    assert records, f"缺少日志记录：{message}"
    return records[-1]


class _FixedProvider:
    """按正文给向量的可控 provider；未登记的文本落到 `fallback`。

    为什么不用 `DeterministicEmbeddingProvider`：它的向量是 SHA-256 噪声，跨文本余弦
    落在 ±0.05 内。召回加上相关性下限（`RECALL_MIN_SIMILARITY`）之后，「随便查一句也
    能取回记忆」再也不会发生 —— 想测接线就必须自己指定相似度。
    """

    def __init__(
        self,
        vectors: dict[str, list[float]],
        *,
        fallback: list[float] | None = None,
    ) -> None:
        self._vectors = dict(vectors)
        self._fallback = _unit(0) if fallback is None else fallback
        self.calls: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    @property
    def available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectors.get(text, self._fallback) for text in texts]


def _unit(index: int) -> list[float]:
    """第 `index` 个正交基向量：与同基向量余弦 1.0，与其他基向量余弦 0.0。"""
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[index] = 1.0
    return vector


def _rotated(similarity: float) -> list[float]:
    """与 `_unit(0)` 余弦恰好为 `similarity` 的单位向量。"""
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[0] = similarity
    vector[1] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


class _ExplodingProvider:
    """每次 `embed` 都抛 `EmbeddingUnavailable`：模拟上游超时后的召回降级。"""

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    @property
    def available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailable("timeout", "simulated")


# ============================================================
# A. 身份闸门
# ============================================================


async def test_generic_identity_returns_none_without_opening_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-28：未识别身份进通用模式，**不读**个人数据 —— 连数据库都不碰。"""
    identity = DialogueIdentity(source="generic")
    assert identity.is_generic
    assert identity.elder_id is None

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recaller = build_memory_recaller(identity, session_factory=_unused_session_factory)

    assert recaller is None
    record = _record(caplog, "memory_recall_skipped")
    assert getattr(record, "reason", None) == "generic_identity"
    assert record.levelno == logging.INFO
    # 没有 `memory_recall_built`：跳过路径不该留下「构建过」的假象
    assert "memory_recall_built" not in caplog.text


async def test_non_generic_identity_without_elder_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """防御分支：`DialogueIdentity` 构造器已拦掉这种组合，上游绕过时仍然只跳过、不抛。"""
    identity = cast(
        DialogueIdentity,
        SimpleNamespace(is_generic=False, elder_id=None, source="device"),
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recaller = build_memory_recaller(identity, session_factory=_unused_session_factory)

    assert recaller is None
    assert getattr(_record(caplog, "memory_recall_skipped"), "reason", None) == "generic_identity"


async def test_invalid_elder_id_returns_none_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """坏 id 是上游判定的坏值：只跳过并 warn，绝不抛；也不把未经校验的原值写进日志。"""
    identity = DialogueIdentity(source="voice", elder_id="not-a-uuid")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recaller = build_memory_recaller(identity, session_factory=_unused_session_factory)

    assert recaller is None
    record = _record(caplog, "memory_recall_skipped")
    assert getattr(record, "reason", None) == "invalid_elder_id"
    assert record.levelno == logging.WARNING
    assert "not-a-uuid" not in caplog.text


# ============================================================
# B. 正常路径（真库）
# ============================================================


async def test_returns_active_memories_of_bound_elder(
    db_conn: AsyncConnection, caplog: pytest.LogCaptureFixture
) -> None:
    """绑定 elder_id 的闭包返回该长者的正文列表，并把 `last_accessed` 落库。"""
    elder_id = await _seed_elder(db_conn)
    provider = _FixedProvider({"我晚上九点睡觉": _unit(0), "我喜欢听越剧": _rotated(0.8)})
    chunk_ids = await _remember_many(
        db_conn, elder_id, ["我晚上九点睡觉", "我喜欢听越剧"], provider
    )
    identity = DialogueIdentity(source="device", elder_id=str(elder_id), display_name="王奶奶")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recaller = build_memory_recaller(
            identity,
            session_factory=_session_factory(db_conn),
            embedding_provider=provider,
        )

    assert recaller is not None
    texts = list(await recaller("我晚上九点睡觉"))

    assert set(texts) == {"我晚上九点睡觉", "我喜欢听越剧"}
    assert all(text == text.strip() for text in texts)

    # `recall` 只改不提交，闭包的 commit 必须让这次「被想起」真的落库。
    rows = await _fetch_chunks(db_conn, chunk_ids)
    assert len(rows) == 2
    assert all(row.last_accessed is not None for row in rows)

    # 日志只带身份与计数，绝不带正文（红线 10）。
    built = _record(caplog, "memory_recall_built")
    assert getattr(built, "elder_id", None) == str(elder_id)
    assert getattr(built, "source", None) == "device"
    assert getattr(built, "top_k", None) == RECALL_TOP_K
    assert "我晚上九点睡觉" not in caplog.text
    assert "我喜欢听越剧" not in caplog.text


async def test_other_elders_memories_never_leak(db_conn: AsyncConnection) -> None:
    """跨长者隔离：拿 B 的记忆原句当查询，A 的召回结果里也不许出现 B 的正文。

    两句都映射到同一个基向量（余弦 1.0），所以「B 的正文没出现」只可能是 `elder_id`
    过滤生效，而不是相关性下限顺手挡掉的。
    """
    elder_a = await _seed_elder(db_conn)
    elder_b = await _seed_elder(db_conn)
    provider = _FixedProvider({"我孙子叫小宝": _unit(0), "我不吃香菜": _unit(0)})
    await _remember_many(db_conn, elder_a, ["我孙子叫小宝"], provider)
    await _remember_many(db_conn, elder_b, ["我不吃香菜"], provider)

    recaller = build_memory_recaller(
        DialogueIdentity(source="device", elder_id=str(elder_a)),
        session_factory=_session_factory(db_conn),
        embedding_provider=provider,
    )
    assert recaller is not None

    texts = list(await recaller("我不吃香菜"))
    assert texts == ["我孙子叫小宝"]
    assert "我不吃香菜" not in texts


async def test_top_k_is_passed_through(db_conn: AsyncConnection) -> None:
    """`top_k` 原样透传给 `memory_service.recall`：三条都过下限、top_k=2 就只回两条。"""
    elder_id = await _seed_elder(db_conn)
    contents = ["我喜欢听越剧", "我晚上九点睡觉", "我孙子叫小宝"]
    provider = _FixedProvider(
        {
            "我喜欢听越剧": _unit(0),
            "我晚上九点睡觉": _rotated(0.9),
            "我孙子叫小宝": _rotated(0.8),
        }
    )
    await _remember_many(db_conn, elder_id, contents, provider)

    recaller = build_memory_recaller(
        DialogueIdentity(source="confirm", elder_id=str(elder_id)),
        session_factory=_session_factory(db_conn),
        embedding_provider=provider,
        top_k=2,
    )
    assert recaller is not None

    texts = list(await recaller("我喜欢听越剧"))
    assert len(texts) == 2
    assert set(texts) <= set(contents)


async def test_blank_content_rows_are_filtered_out(db_conn: AsyncConnection) -> None:
    """空白正文行既不能进上下文，也不能占掉一条 top-k 名额的错觉（本层过滤）。"""
    elder_id = await _seed_elder(db_conn)
    provider = DeterministicEmbeddingProvider()
    vector = (await provider.embed(["我喜欢听越剧"]))[0]
    await _insert_chunk(db_conn, elder_id=elder_id, content="   ", embedding=vector)
    await _remember_many(db_conn, elder_id, ["我喜欢听越剧"], provider)

    recaller = build_memory_recaller(
        DialogueIdentity(source="device", elder_id=str(elder_id)),
        session_factory=_session_factory(db_conn),
        embedding_provider=provider,
    )
    assert recaller is not None

    assert list(await recaller("我喜欢听越剧")) == ["我喜欢听越剧"]


# ============================================================
# C. 降级路径
# ============================================================


async def test_embedding_unavailable_degrades_to_empty_list(db_conn: AsyncConnection) -> None:
    """provider 抛 `EmbeddingUnavailable`：本层不抛、返回 `[]`。

    真凶在 `memory_service._embed_text`：它把 `EmbeddingUnavailable` 收敛成 `None`，
    于是 `recall` 直接返回 `[]`（并打 `memory_recall_degraded_total{reason=timeout}`）。
    断言实现现状，而不是「异常会上抛」—— AGENTS §6 的降级语义就是这层「不加载记忆」。
    """
    elder_id = await _seed_elder(db_conn)
    ok_provider = DeterministicEmbeddingProvider()
    (chunk_id,) = await _remember_many(db_conn, elder_id, ["我孙子叫小宝"], ok_provider)

    recaller = build_memory_recaller(
        DialogueIdentity(source="device", elder_id=str(elder_id)),
        session_factory=_session_factory(db_conn),
        embedding_provider=_ExplodingProvider(),
    )
    assert recaller is not None

    assert list(await recaller("孙子")) == []
    # 没命中就没有「被召回」这件事：`last_accessed` 保持 NULL。
    (row,) = await _fetch_chunks(db_conn, [chunk_id])
    assert row.last_accessed is None


async def test_missing_provider_is_built_lazily_and_cached(
    db_conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 provider：**第一轮召回时**才构建，之后走模块级缓存不再构建。"""
    seed_provider = memory_recall_module.build_embedding_provider()
    elder_id = await _seed_elder(db_conn)
    await _remember_many(db_conn, elder_id, ["我喜欢听越剧"], seed_provider)

    monkeypatch.setattr(memory_recall_module, "_EMBEDDING_PROVIDER", None)
    builds: list[int] = []

    def _counting_build() -> EmbeddingProvider:
        builds.append(1)
        return seed_provider

    monkeypatch.setattr(memory_recall_module, "build_embedding_provider", _counting_build)
    identity = DialogueIdentity(source="device", elder_id=str(elder_id))

    recaller = build_memory_recaller(identity, session_factory=_session_factory(db_conn))
    assert recaller is not None
    assert builds == []  # 建工厂时不碰 provider（惰性）

    assert list(await recaller("我喜欢听越剧")) == ["我喜欢听越剧"]
    assert builds == [1]
    assert memory_recall_module._EMBEDDING_PROVIDER is seed_provider

    assert list(await recaller("我喜欢听越剧")) == ["我喜欢听越剧"]
    assert builds == [1]  # 第二轮命中缓存


async def test_explicit_provider_never_touches_module_builder(
    db_conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式注入 provider 时，模块级构建器一次都不该被调用（否则会白建连接池）。"""
    provider = DeterministicEmbeddingProvider()
    elder_id = await _seed_elder(db_conn)
    await _remember_many(db_conn, elder_id, ["我喜欢听越剧"], provider)

    def _boom() -> EmbeddingProvider:
        raise AssertionError("显式注入 provider 时不该调用 build_embedding_provider")

    monkeypatch.setattr(memory_recall_module, "build_embedding_provider", _boom)
    recaller = build_memory_recaller(
        DialogueIdentity(source="device", elder_id=str(elder_id)),
        session_factory=_session_factory(db_conn),
        embedding_provider=provider,
    )
    assert recaller is not None

    assert list(await recaller("我喜欢听越剧")) == ["我喜欢听越剧"]


async def test_raising_recaller_is_converged_by_companion() -> None:
    """本层的「异常不吞」之所以安全：上层只有一个收敛点，且已按 AGENTS §6 收敛成 []。"""
    from app.agents.companion import _recall_memories

    async def _boom(_: str) -> Sequence[str]:
        raise RuntimeError("session pool exhausted")

    assert list(await _recall_memories(_boom, "我喜欢听越剧", trace_id=None)) == []
