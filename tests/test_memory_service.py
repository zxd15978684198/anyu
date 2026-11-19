"""`memory_service` 五步 + embedding 供应商的单测（迭代 7 · B 号子 agent）。

## 怎么分两类

- **纯逻辑**（不碰库）：`cosine_similarity`、`DeterministicEmbeddingProvider`、
  `HttpEmbeddingProvider` 四类失败、`build_embedding_provider` 二选一。
  这些用例没有 `db_conn` 参数，没库也能跑。
- **真库**：`remember` / `recall` / `decay` / `enforce_capacity` / `delete_by_elder`。
  一律用 `db_conn` 夹具（每用例一个事务、结束回滚），不建 `anyu_test` 库
  （口径见 `tests/conftest.py` 模块 docstring）。

## 为什么大量用「可控 provider」而不是 determinstic provider

确定性 provider 的相似度是 SHA-256 噪声：拿它只能测「相同文本 = 同向量」，
测不了「相似度 0.89 不归并、0.91 归并」这种阈值边界（那必须让两个向量的余弦**恰好**
是 0.95）。所以阈值/排序类用例用 `_FixedProvider`（按文本查表返回预设单位向量），
它同样**不是**语义模型，只是把「我想让这两个向量多像」变成显式参数。

## S-D7-1 的自查

`test_deterministic_provider_docstring_states_no_semantics` 会反向断言文档里那句红线还在。
这条测试不验证功能，验证的是「后来者不能悄悄把兜底 provider 说成语义模型」——
计划 S-D7-1 要求它写在代码里，那就要有东西盯着它别被删掉。
"""

from __future__ import annotations

import datetime
import json
import math
import re
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

import app.services.memory_service as memory_service_module
from app.core.metrics import metrics_snapshot, reset_metrics
from app.db import engine as app_engine
from app.models import AuditLog, Elder, MemoryChunk
from app.services.embedding import (
    EMBEDDING_DIMENSION,
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
    EmbeddingUnavailable,
    HttpEmbeddingProvider,
    build_embedding_provider,
)
from app.services.memory_service import (
    CAPACITY_PER_ELDER,
    DECAY_DAYS,
    DECAY_FACTOR,
    DEFAULT_LIST_LIMIT,
    MAX_CONTENT_LENGTH,
    MAX_LIST_LIMIT,
    MEMORY_STATUSES,
    RECALL_CATEGORIES,
    RECALL_MIN_SIMILARITY,
    SUPERSEDE_SCAN_LIMIT,
    SUPERSEDE_SIMILARITY,
    cosine_similarity,
    decay,
    delete_by_elder,
    enforce_capacity,
    list_chunks,
    memories_contradict,
    recall,
    remember,
)

BASE_URL = "http://embedding.test/v1"
MODEL = "text-embedding-3-small"


# ============================================================
# 夹具与小工具
# ============================================================


@pytest.fixture(autouse=True)
async def _release_pool() -> AsyncIterator[None]:
    """每个用例结束把 `app.db.engine` 池里的连接还掉。

    为什么需要：`pytest-asyncio` 每个用例一个新事件循环
    （`pyproject.toml` 的 `asyncio_default_fixture_loop_scope = "function"`），而
    `app.db.engine` 是**模块级**连接池 —— 池里留着上一个循环里建的 asyncpg 连接，
    下一个用例从池里取到它就会报「Event loop is closed / 'NoneType' object has no
    attribute 'send'」。这是仓库已登记的 P-03 坑，其他数据库用例靠自建 `NullPool`
    引擎绕开；本文件按派单要求用 conftest 的 `db_conn`，就在用例结束时清空池。

    只 dispose 空闲连接，`engine` 本身仍可继续用，不影响别的文件。
    """
    yield
    await app_engine.dispose()


@pytest.fixture(autouse=True)
def _metrics_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制打点开着并每次清零快照。

    `app.core.metrics._enabled()` 每次现读 `get_settings().metrics_enabled`，
    所以替换模块级的 `get_settings` 就够了：不依赖 `.env` 里到底开没开，也不去改配置。
    """
    reset_metrics()
    monkeypatch.setattr(
        "app.core.metrics.get_settings",
        lambda: SimpleNamespace(metrics_enabled=True),
    )


def _counters() -> dict[str, int]:
    snapshot = metrics_snapshot()["counters"]
    assert isinstance(snapshot, dict)
    return snapshot


def _axis(index: int) -> list[float]:
    """N 维第 `index` 个正交基向量（单位长度，其余分量 0）。"""
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[index] = 1.0
    return vector


def _rotated(similarity: float) -> list[float]:
    """与 `_axis(0)` 余弦**恰好**为 `similarity` 的 N 维单位向量。"""
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[0] = similarity
    vector[1] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


def _body(entries: list[tuple[int, list[float]]]) -> dict[str, Any]:
    """拼一个 OpenAI 兼容 `/embeddings` 响应体；`entries` 是 (index, vector) 序列。"""
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in entries
        ],
        "model": MODEL,
    }


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _http(handler: Callable[[httpx.Request], httpx.Response], *, api_key: str = "test-key"):
    """注入 MockTransport 的 Http provider：不起真实网络，四类失败都能造出来。"""
    return HttpEmbeddingProvider(
        base_url=BASE_URL,
        model=MODEL,
        api_key=api_key,
        timeout_ms=1500,
        client=_transport(handler),
    )


def _unreachable(request: httpx.Request) -> httpx.Response:
    """本该不被调用的 handler：用来证明「这次真的没发请求」。"""
    raise AssertionError(f"unexpected embedding request: {request.url}")


class _FixedProvider:
    """按文本查表返回预设向量的 provider（可控相似度，**非语义**）。"""

    def __init__(self, vectors: dict[str, list[float]], *, available: bool = True) -> None:
        self._vectors = vectors
        self._available = available
        self.calls: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    @property
    def available(self) -> bool:
        return self._available

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self._vectors[text]) for text in texts]


class _BrokenProvider:
    """返回维度不对的向量：`bad_payload` 的降级路径（写入仍要成功）。"""

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    @property
    def available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class _ExplodingProvider:
    """每次调用都抛 `EmbeddingUnavailable`：模拟上游超时后的召回降级。"""

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    @property
    def available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailable("timeout", "simulated")


def _session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码；残留由夹具回滚。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


async def _seed_elder(conn: AsyncConnection) -> uuid.UUID:
    """`memory_chunks.elder_id` 的外键目标；占位姓名，不写真实长者数据（AGENTS §4.3）。"""
    elder_id = uuid.uuid4()
    await conn.execute(Elder.__table__.insert().values(elder_id=elder_id, name="测试长者"))
    return elder_id


async def _fetch(session: AsyncSession, chunk_id: uuid.UUID) -> MemoryChunk:
    """从库里重读一行（`populate_existing` 绕开 identity map 里的过期副本）。"""
    stmt = (
        select(MemoryChunk)
        .where(MemoryChunk.chunk_id == chunk_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def _chunks_of(session: AsyncSession, elder_id: uuid.UUID) -> list[MemoryChunk]:
    """该长者的全部切片（含 superseded / deleted），按写入顺序。"""
    stmt = (
        select(MemoryChunk)
        .where(MemoryChunk.elder_id == elder_id)
        .order_by(MemoryChunk.created_at.asc(), MemoryChunk.chunk_id.asc())
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _insert_chunk(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    content: str,
    importance: float = 1.0,
    status: str = "active",
    embedding: list[float] | None = None,
    created_at: datetime.datetime | None = None,
    last_accessed: datetime.datetime | None = None,
) -> MemoryChunk:
    """直接落一行（绕过 `remember` 的冲突判定），用于构造衰减/容量/过滤场景。"""
    row = MemoryChunk(
        elder_id=elder_id,
        content=content,
        embedding=embedding,
        category="private",
        importance=importance,
        status=status,
        created_at=created_at,
    )
    if last_accessed is not None:
        row.last_accessed = last_accessed
    session.add(row)
    await session.flush()
    return row


def _days_ago(days: int) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=days)


# ============================================================
# A. cosine_similarity 纯函数
# ============================================================


def test_cosine_similarity_identical_is_one() -> None:
    vector = _rotated(0.6)
    assert cosine_similarity(vector, list(vector)) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_and_opposite() -> None:
    assert cosine_similarity(_axis(0), _axis(1)) == pytest.approx(0.0)
    assert cosine_similarity(_axis(0), [-value for value in _axis(0)]) == pytest.approx(-1.0)
    # 尺度无关：向量乘正数不改相似度
    assert cosine_similarity(_axis(0), [5.0 * value for value in _axis(0)]) == pytest.approx(1.0)


def test_cosine_similarity_matches_rotated_angle() -> None:
    for similarity in (0.0, 0.35, 0.9, 0.95):
        assert cosine_similarity(_axis(0), _rotated(similarity)) == pytest.approx(
            similarity, abs=1e-9
        )


def test_cosine_similarity_zero_vector_returns_zero() -> None:
    """零向量没有方向 —— 返回 0.0 而不是抛错（空记忆不该让整轮回滚）。"""
    assert cosine_similarity(_axis(0), [0.0] * EMBEDDING_DIMENSION) == 0.0
    assert cosine_similarity([0.0] * 3, [0.0] * 3) == 0.0


def test_cosine_similarity_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        cosine_similarity(_axis(0), _axis(1)[:10])  # 维度不等
    with pytest.raises(ValueError):
        cosine_similarity([], [])
    with pytest.raises(ValueError):
        cosine_similarity([1.0, float("nan")], [1.0, 2.0])
    with pytest.raises(ValueError):
        cosine_similarity([1.0, float("inf")], [1.0, 2.0])


def test_cosine_similarity_clips_float_noise() -> None:
    """浮点误差可能给 1.0000000000000002；夹回 [-1,1] 让「>0.9」判断稳定。"""
    big = [1e8, 1e8, 1e-8]
    assert cosine_similarity(big, list(big)) <= 1.0
    assert cosine_similarity(big, list(big)) >= -1.0


# ============================================================
# B. DeterministicEmbeddingProvider
# ============================================================


async def test_deterministic_provider_dimension_and_unit_length() -> None:
    provider = DeterministicEmbeddingProvider()
    assert provider.dimension == EMBEDDING_DIMENSION
    assert provider.available is True

    vectors = await provider.embed(["我喜欢听越剧", "今天天气不错"])
    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == EMBEDDING_DIMENSION
        norm = math.sqrt(sum(value * value for value in vector))
        assert norm == pytest.approx(1.0, abs=1e-9)


async def test_deterministic_provider_is_stable_and_order_preserving() -> None:
    provider = DeterministicEmbeddingProvider()
    first = await provider.embed(["同一句话"])
    second = await provider.embed(["同一句话"])
    assert first == second  # 联调时同一句永远同一向量

    batch = await provider.embed(["甲", "乙"])
    assert batch[0] == (await provider.embed(["甲"]))[0]  # 批量与单条同口径
    assert batch[1] == (await provider.embed(["乙"]))[0]
    assert batch[0] != batch[1]


async def test_deterministic_provider_has_no_semantics() -> None:
    """反向断言：不同句子的相似度与「意思像不像」无关（噪声级）。"""
    provider = DeterministicEmbeddingProvider()
    close_meaning = await provider.embed(["我喜欢越剧", "我讨厌越剧"])
    similarity = cosine_similarity(close_meaning[0], close_meaning[1])
    assert abs(similarity) < 0.2  # 语义相反 ≠ 向量相反，它根本不懂「越剧」


def test_deterministic_provider_docstring_states_no_semantics() -> None:
    """S-D7-1：那句红线必须留在文档里，删掉就红。"""
    class_doc = DeterministicEmbeddingProvider.__doc__ or ""
    module_doc = __import__("app.services.embedding", fromlist=["__doc__"]).__doc__ or ""
    for doc in (class_doc, module_doc):
        assert "非语义检索" in doc
        assert "不能作为召回质量证据" in doc
        assert "联调兜底" in doc


async def test_deterministic_provider_rejects_bad_dimension() -> None:
    with pytest.raises(ValueError):
        DeterministicEmbeddingProvider(dimension=0)
    assert DeterministicEmbeddingProvider(dimension=8).dimension == 8


# ============================================================
# C. HttpEmbeddingProvider（httpx.MockTransport，零真实网络）
# ============================================================


async def test_http_provider_posts_to_embeddings_and_keeps_order() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["model"] = payload["model"]
        seen["input"] = payload["input"]
        seen["authorization"] = request.headers.get("authorization")
        # 故意倒序回：验证按 index 归位，而不是「数组第几个算第几句」
        return httpx.Response(200, json=_body([(1, _axis(1)), (0, _axis(2))]))

    provider = _http(handler)
    vectors = await provider.embed(["第一句", "第二句"])

    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE_URL}/embeddings"
    assert seen["model"] == MODEL
    assert seen["input"] == ["第一句", "第二句"]
    assert seen["authorization"] == "Bearer test-key"
    assert vectors == [_axis(2), _axis(1)]


async def test_http_provider_empty_input_skips_network() -> None:
    provider = _http(_unreachable)
    assert await provider.embed([]) == []


async def test_http_provider_not_configured() -> None:
    provider = _http(_unreachable, api_key="")
    assert provider.available is False
    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await provider.embed(["随便"])
    assert excinfo.value.reason == "not_configured"


async def test_http_provider_timeout_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await _http(handler).embed(["超时"])
    assert excinfo.value.reason == "timeout"


async def test_http_provider_network_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await _http(handler).embed(["断网"])
    assert excinfo.value.reason == "network"


async def test_http_provider_bad_status_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream down"})

    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await _http(handler).embed(["上游挂了"])
    assert excinfo.value.reason == "bad_status"
    assert "503" in str(excinfo.value)


async def test_http_provider_bad_payload_not_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
        )

    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await _http(handler).embed(["坏载荷"])
    assert excinfo.value.reason == "bad_payload"


async def test_http_provider_bad_payload_wrong_dimension() -> None:
    """维度 ≠ 期望维度一律 `bad_payload`：不截断、不补零（否则向量会悄悄失真）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body([(0, [0.1] * 10)]))

    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await _http(handler).embed(["维度不对"])
    assert excinfo.value.reason == "bad_payload"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"data": []}, id="empty-data"),
        pytest.param({"data": [{"index": 0, "embedding": "not-a-list"}]}, id="embedding-not-list"),
        pytest.param({"object": "list"}, id="missing-data"),
        pytest.param("just-a-string", id="body-not-object"),
        pytest.param(
            {"data": [{"index": 5, "embedding": [0.1] * EMBEDDING_DIMENSION}]},
            id="index-out-of-range",
        ),
    ],
)
async def test_http_provider_bad_payload_variants(payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await _http(handler).embed(["x"])
    assert excinfo.value.reason == "bad_payload"


async def test_http_provider_rejects_non_finite_components() -> None:
    """NaN/inf 会让 pgvector 的余弦距离整列失真，必须当坏载荷丢掉。"""
    vector = list(_axis(0))
    vector[3] = float("nan")

    def handler(request: httpx.Request) -> httpx.Response:
        # 手工拼 body：httpx 的 `json=` 走严格序列化会先拒掉 NaN，而真实供应商
        # 完全可能吐出裸 NaN（Python 的 json.loads 默认接受它）。
        return httpx.Response(200, content=json.dumps(_body([(0, vector)])).encode())

    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await _http(handler).embed(["NaN"])
    assert excinfo.value.reason == "bad_payload"


# ============================================================
# D. build_embedding_provider
# ============================================================


def test_build_provider_uses_http_when_configured() -> None:
    settings = SimpleNamespace(
        embedding_base_url=BASE_URL,
        embedding_model=MODEL,
        embedding_api_key="test-key",
        embedding_timeout_ms=1234,
    )
    provider = build_embedding_provider(settings)
    assert isinstance(provider, HttpEmbeddingProvider)
    assert provider.available is True
    assert provider.dimension == EMBEDDING_DIMENSION


def test_build_provider_degrades_without_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="app.services.embedding")
    settings = SimpleNamespace(
        embedding_base_url=BASE_URL,
        embedding_model=MODEL,
        embedding_api_key="",
        embedding_timeout_ms=1234,
    )
    provider = build_embedding_provider(settings)
    assert isinstance(provider, DeterministicEmbeddingProvider)
    assert any(record.msg == "embedding_provider_degraded" for record in caplog.records)


def test_build_provider_tolerates_missing_settings_fields() -> None:
    """`app/core/config.py` 还没补 embedding_* 四项时也得能起服务（getattr 兜底）。"""
    provider = build_embedding_provider(SimpleNamespace())
    assert isinstance(provider, DeterministicEmbeddingProvider)


# ============================================================
# E. remember 写入
# ============================================================


async def test_remember_persists_active_chunk(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()

    row = await remember(
        session,
        elder_id=elder_id,
        content="  我喜欢听越剧  ",
        embedding_provider=provider,
        source_thread_id="thread-1",
    )

    assert row.content == "我喜欢听越剧"  # 首尾空白被去掉
    assert row.status == "active"
    assert row.category == "private"
    assert float(row.importance) == pytest.approx(1.0)
    assert row.embedding is not None
    assert len(row.embedding) == EMBEDDING_DIMENSION
    assert row.source_thread_id == "thread-1"
    assert row.last_accessed is None  # 还没被召回

    stored = await _fetch(session, row.chunk_id)
    assert stored.elder_id == elder_id


@pytest.mark.parametrize("content", ["", "   ", "\n\t "])
async def test_remember_rejects_blank_content(db_conn: AsyncConnection, content: str) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    with pytest.raises(ValueError):
        await remember(
            session,
            elder_id=elder_id,
            content=content,
            embedding_provider=DeterministicEmbeddingProvider(),
        )
    assert await _chunks_of(session, elder_id) == []


async def test_remember_rejects_overlong_content_instead_of_truncating(
    db_conn: AsyncConnection,
) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()

    with pytest.raises(ValueError):
        await remember(
            session,
            elder_id=elder_id,
            content="记" * (MAX_CONTENT_LENGTH + 1),
            embedding_provider=provider,
        )
    assert MAX_CONTENT_LENGTH == 200

    # 恰好 200 字可以写进去：边界是「>200 才拒」，不是「>=」
    row = await remember(
        session, elder_id=elder_id, content="记" * MAX_CONTENT_LENGTH, embedding_provider=provider
    )
    assert len(row.content) == MAX_CONTENT_LENGTH


async def test_remember_validates_category_and_importance(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()

    with pytest.raises(ValueError):
        await remember(
            session,
            elder_id=elder_id,
            content="感冒药",
            embedding_provider=provider,
            category="shared",
        )
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            await remember(
                session,
                elder_id=elder_id,
                content=f"权重 {bad}",
                embedding_provider=provider,
                importance=bad,
            )
    # family 是本版预留取值，允许写入
    row = await remember(
        session,
        elder_id=elder_id,
        content="女儿周六来",
        embedding_provider=provider,
        category="family",
    )
    assert row.category == "family"


async def test_remember_degrades_without_provider(db_conn: AsyncConnection) -> None:
    """provider 不可用**不阻断写入**：正文照存，向量为 NULL，并打点。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    unconfigured = _http(_unreachable, api_key="")

    row = await remember(
        session, elder_id=elder_id, content="孙子上小学二年级", embedding_provider=unconfigured
    )

    assert row.embedding is None
    assert row.status == "active"
    assert _counters().get("memory_write_degraded_total{reason=not_configured}") == 1


async def test_remember_degrades_on_bad_vector_dimension(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    row = await remember(
        session, elder_id=elder_id, content="维度不对的向量", embedding_provider=_BrokenProvider()
    )

    assert row.embedding is None
    assert _counters().get("memory_write_degraded_total{reason=bad_payload}") == 1


# ============================================================
# F. 相似度 > 0.9 → 旧条 superseded
# ============================================================


async def test_remember_supersedes_near_duplicate(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()

    first = await remember(
        session, elder_id=elder_id, content="我每天早上吃降压药", embedding_provider=provider
    )
    second = await remember(
        session, elder_id=elder_id, content="我每天早上吃降压药", embedding_provider=provider
    )

    old = await _fetch(session, first.chunk_id)
    assert old.status == "superseded"  # 确定性 provider：同一句 → 相似度 1.0
    assert (await _fetch(session, second.chunk_id)).status == "active"

    chunks = await _chunks_of(session, elder_id)
    # 两条在同一事务里写入 → `now()` 相同，created_at 无法用来定序，只断言集合
    assert {chunk.status for chunk in chunks} == {"superseded", "active"}
    assert len(chunks) == 2


async def test_remember_supersedes_when_similar_and_contradictory(db_conn: AsyncConnection) -> None:
    """FR-MEM-03：够像（0.95 > 0.9）**且**说的相反 → 旧条 superseded，新条生效。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider(
        {
            "我喜欢越剧": _axis(0),
            "我不喜欢越剧了": _rotated(0.95),
        }
    )

    old = await remember(
        session, elder_id=elder_id, content="我喜欢越剧", embedding_provider=provider
    )
    new = await remember(
        session, elder_id=elder_id, content="我不喜欢越剧了", embedding_provider=provider
    )

    assert (await _fetch(session, old.chunk_id)).status == "superseded"
    assert (await _fetch(session, new.chunk_id)).status == "active"
    assert SUPERSEDE_SIMILARITY == 0.9


async def test_remember_keeps_similar_but_not_contradictory(db_conn: AsyncConnection) -> None:
    """高相似但**不矛盾**的两条必须并存（本卡的核心口径）。

    「我喜欢越剧」与「我喜欢京剧」余弦 0.95 —— 旧实现会在这里把第一条抹掉，而那只是
    长者的第二份偏好。相似度只能收窄候选，取代必须再过 `memories_contradict`。
    """
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider(
        {
            "我喜欢越剧": _axis(0),
            "我喜欢京剧": _rotated(0.95),
        }
    )

    first = await remember(
        session, elder_id=elder_id, content="我喜欢越剧", embedding_provider=provider
    )
    second = await remember(
        session, elder_id=elder_id, content="我喜欢京剧", embedding_provider=provider
    )

    assert (await _fetch(session, first.chunk_id)).status == "active"
    assert (await _fetch(session, second.chunk_id)).status == "active"
    assert {chunk.status for chunk in await _chunks_of(session, elder_id)} == {"active"}


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # 一正一负：同一个谓词被否定，就是改口
        ("我喜欢越剧", "我不喜欢越剧了", True),
        ("我不吃香菜", "我吃香菜", True),
        ("我去过北京", "我没去过北京", True),
        ("我住养老院", "我不住养老院", True),
        # 两侧都是否定：各说各的忌口，必须并存
        ("我不吃香菜", "我不吃辣", False),
        # 高相似但说的是两件事：两份偏好并存
        ("我喜欢越剧", "我喜欢京剧", False),
        # 只差细节（时间/人名）：判不出来就不取代
        ("我晚上九点睡觉", "我晚上十点睡觉", False),
        ("我女儿叫王芳", "我女儿叫王芳", False),  # 同一句由去重那条规则处理，不是矛盾
        # 反义对：两侧都是肯定词，靠词义对立判
        ("我爱听评弹", "我讨厌听评弹", True),
        # 两条互不相干的事实：没有共用的谓词，不判矛盾
        ("我不吃香菜", "我讨厌下雨", False),
    ],
)
def test_memories_contradict_is_deterministic(old: str, new: str, expected: bool) -> None:
    """矛盾判定是纯函数：同一对输入永远同一个结果，来回跑两遍也不许翻脸。"""
    assert memories_contradict(old, new) is expected
    assert memories_contradict(old, new) is expected
    # 对称性：谁在左谁在右不影响结论
    assert memories_contradict(new, old) is expected


def test_two_similarity_constants_are_separate() -> None:
    """两个阈值分属两个常量，不许复用（PRD FR-MEM-03 / FR-MEM-05 是两件事）。"""
    assert RECALL_MIN_SIMILARITY != SUPERSEDE_SIMILARITY
    # 取代阈值必须更严：误判方向是「删数据」，召回下限松一点才不会漏召回
    assert RECALL_MIN_SIMILARITY < SUPERSEDE_SIMILARITY
    assert 0.0 < RECALL_MIN_SIMILARITY < 1.0
    assert SUPERSEDE_SCAN_LIMIT >= 1


async def test_remember_keeps_similar_but_below_threshold(db_conn: AsyncConnection) -> None:
    """0.89 < 0.9：两条都留着 —— 阈值只能「足够像」才合并，否则会吃掉细节。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider({"旧条": _axis(0), "很接近但不重复": _rotated(0.89)})

    old = await remember(session, elder_id=elder_id, content="旧条", embedding_provider=provider)
    near = await remember(
        session, elder_id=elder_id, content="很接近但不重复", embedding_provider=provider
    )

    assert (await _fetch(session, old.chunk_id)).status == "active"
    assert (await _fetch(session, near.chunk_id)).status == "active"


async def test_supersede_is_scoped_to_same_elder_and_category(db_conn: AsyncConnection) -> None:
    """跨长者、跨类别都不许归并：那会把家属可见的记忆悄悄删掉 / 越权。"""
    elder_a = await _seed_elder(db_conn)
    elder_b = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider(
        {
            "同一句": _axis(0),
            "同向量不同类别": _axis(0),
        }
    )

    private_a = await remember(
        session, elder_id=elder_a, content="同一句", embedding_provider=provider
    )
    await remember(session, elder_id=elder_b, content="同一句", embedding_provider=provider)
    family_a = await remember(
        session,
        elder_id=elder_a,
        content="同向量不同类别",
        embedding_provider=provider,
        category="family",
    )

    assert (await _fetch(session, private_a.chunk_id)).status == "active"
    assert (await _fetch(session, family_a.chunk_id)).status == "active"


async def test_supersede_does_not_touch_deleted_or_other_category(
    db_conn: AsyncConnection,
) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider({"候选旧条": _axis(0), "新条": _axis(0)})

    deleted = await _insert_chunk(
        session, elder_id=elder_id, content="已删的旧条", status="deleted", embedding=_axis(0)
    )
    new = await remember(session, elder_id=elder_id, content="新条", embedding_provider=provider)

    assert (await _fetch(session, deleted.chunk_id)).status == "deleted"
    assert (await _fetch(session, new.chunk_id)).status == "active"


# ============================================================
# G. recall 召回
# ============================================================


async def test_recall_isolates_elders(db_conn: AsyncConnection) -> None:
    """跨长者隔离：A 写的记忆 B 一条也召不回（闸门②/AC-39 的核心断言）。"""
    elder_a = await _seed_elder(db_conn)
    elder_b = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()

    chunk_a = await remember(
        session, elder_id=elder_a, content="我喜欢听越剧", embedding_provider=provider
    )
    chunk_b = await remember(
        session, elder_id=elder_b, content="我喜欢听越剧", embedding_provider=provider
    )

    hits_a = await recall(
        session, elder_id=elder_a, query="我喜欢听越剧", embedding_provider=provider
    )
    hits_b = await recall(
        session, elder_id=elder_b, query="我喜欢听越剧", embedding_provider=provider
    )

    assert [hit.chunk_id for hit in hits_a] == [chunk_a.chunk_id]
    assert [hit.chunk_id for hit in hits_b] == [chunk_b.chunk_id]
    # 两个长者写了同一句话，各自都还是 active（跨长者不触发归并）
    assert (await _fetch(session, chunk_a.chunk_id)).status == "active"
    assert (await _fetch(session, chunk_b.chunk_id)).status == "active"


async def test_recall_orders_by_similarity_and_limits_top_k(db_conn: AsyncConnection) -> None:
    """排序与 top_k 照旧，但低于相关性下限的命中在 SQL 里就被挡掉（FR-MEM-05）。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider(
        {
            "查询": _axis(0),
            "最像": _axis(0),  # cos = 1.0
            "次像": _rotated(0.7),  # cos = 0.7
            "不像": _axis(5),  # cos = 0.0 < 下限 → 不进上下文
        }
    )
    for content in ("不像", "最像", "次像"):
        await remember(session, elder_id=elder_id, content=content, embedding_provider=provider)

    top2 = await recall(
        session, elder_id=elder_id, query="查询", embedding_provider=provider, top_k=2
    )
    assert [hit.content for hit in top2] == ["最像", "次像"]

    all_hits = await recall(session, elder_id=elder_id, query="查询", embedding_provider=provider)
    assert [hit.content for hit in all_hits] == ["最像", "次像"]


async def test_recall_keeps_hits_at_or_above_the_floor(db_conn: AsyncConnection) -> None:
    """下限是「≥」不是「>」：贴着下限的条目必须留下。

    向量列是 float4，`1.0 - cos` 在存储时会被舍到 1e-8 量级，正好等于下限的那条可能
    被判成「略超」—— 所以这里用「下限 +0.01」代表贴着下限的一侧、用「下限 -0.02」
    代表被挡的一侧，锁的是闭区间语义而不是浮点字面相等。
    """
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider(
        {
            "查询": _axis(0),
            "贴着下限": _rotated(RECALL_MIN_SIMILARITY + 0.01),
            "低于下限": _rotated(RECALL_MIN_SIMILARITY - 0.02),
        }
    )
    await remember(session, elder_id=elder_id, content="贴着下限", embedding_provider=provider)
    await remember(session, elder_id=elder_id, content="低于下限", embedding_provider=provider)

    hits = await recall(session, elder_id=elder_id, query="查询", embedding_provider=provider)
    assert [hit.content for hit in hits] == ["贴着下限"]


async def test_recall_filtered_empty_is_not_degraded(db_conn: AsyncConnection) -> None:
    """全被下限挡掉 = 空结果，不是降级：不打 `memory_recall_degraded_total`。

    「没有相关记忆」和「召回坏了」是两码事。混为一谈会让运维把「长者没提过这件事」
    当成 provider 故障告警，也会让上层误走降级话术。
    """
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider({"查询": _axis(0), "毫不相干": _axis(3)})
    await remember(session, elder_id=elder_id, content="毫不相干", embedding_provider=provider)

    reset_metrics()
    hits = await recall(session, elder_id=elder_id, query="查询", embedding_provider=provider)

    assert hits == []
    assert not [key for key in _counters() if key.startswith("memory_recall_degraded_total")], (
        "召回被下限挡空不是降级，不该打 degraded 点"
    )


async def test_recall_updates_last_accessed(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()
    chunk = await remember(
        session, elder_id=elder_id, content="我晚上九点睡觉", embedding_provider=provider
    )
    assert chunk.last_accessed is None

    hits = await recall(
        session, elder_id=elder_id, query="我晚上九点睡觉", embedding_provider=provider
    )
    assert len(hits) == 1
    assert hits[0].last_accessed is not None

    stored = await _fetch(session, chunk.chunk_id)
    assert stored.last_accessed is not None


async def test_recall_skips_inactive_and_embedded_less_rows(db_conn: AsyncConnection) -> None:
    """FR-MEM-05「召回后二次过滤」：superseded / deleted / 无向量的一律不进上下文。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = _FixedProvider({"查询": _axis(0), "活着": _axis(0)})

    alive = await remember(session, elder_id=elder_id, content="活着", embedding_provider=provider)
    superseded = await _insert_chunk(
        session, elder_id=elder_id, content="被取代", status="superseded", embedding=_axis(0)
    )
    deleted = await _insert_chunk(
        session, elder_id=elder_id, content="已删", status="deleted", embedding=_axis(0)
    )
    no_vector = await _insert_chunk(
        session, elder_id=elder_id, content="没向量", status="active", embedding=None
    )

    hits = await recall(session, elder_id=elder_id, query="查询", embedding_provider=provider)
    assert [hit.chunk_id for hit in hits] == [alive.chunk_id]
    assert superseded.chunk_id not in {hit.chunk_id for hit in hits}
    assert deleted.chunk_id not in {hit.chunk_id for hit in hits}
    assert no_vector.chunk_id not in {hit.chunk_id for hit in hits}


async def test_recall_after_supersede_returns_only_new_chunk(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()
    await remember(session, elder_id=elder_id, content="我不吃香菜", embedding_provider=provider)
    latest = await remember(
        session, elder_id=elder_id, content="我不吃香菜", embedding_provider=provider
    )

    hits = await recall(session, elder_id=elder_id, query="我不吃香菜", embedding_provider=provider)
    assert [hit.chunk_id for hit in hits] == [latest.chunk_id]


async def test_recall_degrades_without_usable_vector(db_conn: AsyncConnection) -> None:
    """召不到就返回空 + 打点，**不得臆造**（AGENTS §6 降级列）。"""
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    await remember(
        session,
        elder_id=elder_id,
        content="我孙子叫小宝",
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert (
        await recall(
            session,
            elder_id=elder_id,
            query="孙子",
            embedding_provider=_http(_unreachable, api_key=""),
        )
    ) == []
    assert (
        await recall(
            session, elder_id=elder_id, query="孙子", embedding_provider=_ExplodingProvider()
        )
    ) == []
    assert _counters().get("memory_recall_degraded_total{reason=not_configured}") == 1
    assert _counters().get("memory_recall_degraded_total{reason=timeout}") == 1


async def test_recall_rejects_empty_query_and_bad_top_k(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()

    assert await recall(session, elder_id=elder_id, query="   ", embedding_provider=provider) == []
    assert _counters().get("memory_recall_degraded_total{reason=empty_query}") == 1

    with pytest.raises(ValueError):
        await recall(session, elder_id=elder_id, query="随便", embedding_provider=provider, top_k=0)


# ============================================================
# H. decay 衰减
# ============================================================


def test_decay_constants_match_plan() -> None:
    assert DECAY_DAYS == 90
    assert DECAY_FACTOR == 0.5
    assert CAPACITY_PER_ELDER == 500
    assert RECALL_CATEGORIES == ("private", "family")


async def test_decay_halves_stale_importance(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    stale = await _insert_chunk(
        session, elder_id=elder_id, content="很久没提的事", created_at=_days_ago(91)
    )
    fresh = await _insert_chunk(
        session, elder_id=elder_id, content="昨天刚说的", created_at=_days_ago(1)
    )
    recently_accessed = await _insert_chunk(
        session,
        elder_id=elder_id,
        content="很老但常被召回",
        created_at=_days_ago(300),
        last_accessed=_days_ago(3),
    )

    changed = await decay(session, elder_id=elder_id)
    assert changed == 1
    assert float((await _fetch(session, stale.chunk_id)).importance) == pytest.approx(0.5)
    assert float((await _fetch(session, fresh.chunk_id)).importance) == pytest.approx(1.0)
    assert float((await _fetch(session, recently_accessed.chunk_id)).importance) == pytest.approx(
        1.0
    )


async def test_decay_skips_superseded_and_deleted(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    superseded = await _insert_chunk(
        session,
        elder_id=elder_id,
        content="被取代",
        status="superseded",
        created_at=_days_ago(200),
    )
    deleted = await _insert_chunk(
        session, elder_id=elder_id, content="已删", status="deleted", created_at=_days_ago(200)
    )

    assert await decay(session, elder_id=elder_id) == 0
    assert float((await _fetch(session, superseded.chunk_id)).importance) == pytest.approx(1.0)
    assert float((await _fetch(session, deleted.chunk_id)).importance) == pytest.approx(1.0)


async def test_decay_scoped_by_elder_and_global_mode(db_conn: AsyncConnection) -> None:
    elder_a = await _seed_elder(db_conn)
    elder_b = await _seed_elder(db_conn)
    session = _session(db_conn)
    stale_a = await _insert_chunk(
        session, elder_id=elder_a, content="A 的旧事", created_at=_days_ago(120)
    )
    stale_b = await _insert_chunk(
        session, elder_id=elder_b, content="B 的旧事", created_at=_days_ago(120)
    )

    assert await decay(session, elder_id=elder_a) == 1
    assert float((await _fetch(session, stale_b.chunk_id)).importance) == pytest.approx(1.0)

    # elder_id=None 是全库维护任务的形态（APScheduler 每日跑），至少覆盖到 B
    assert await decay(session) >= 1
    assert float((await _fetch(session, stale_b.chunk_id)).importance) == pytest.approx(0.5)
    # A 已被上面两轮各衰减一次 → 0.25：衰减按「距上次访问的时长」逐轮乘算，不是幂等操作
    assert float((await _fetch(session, stale_a.chunk_id)).importance) == pytest.approx(0.25)


async def test_decay_rejects_bad_params(db_conn: AsyncConnection) -> None:
    session = _session(db_conn)
    for kwargs in ({"days": 0}, {"factor": 1.0}, {"factor": 0.0}, {"factor": 2.0}):
        with pytest.raises(ValueError):
            await decay(session, **kwargs)


# ============================================================
# I. enforce_capacity 容量淘汰
# ============================================================


async def test_enforce_capacity_soft_deletes_lowest_importance(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)

    important = await _insert_chunk(
        session, elder_id=elder_id, content="重要的事", importance=0.9, last_accessed=_days_ago(1)
    )
    low_recent = await _insert_chunk(
        session,
        elder_id=elder_id,
        content="低权重且较新",
        importance=0.4,
        last_accessed=_days_ago(10),
    )
    low_old = await _insert_chunk(
        session,
        elder_id=elder_id,
        content="低权重且最久",
        importance=0.4,
        last_accessed=_days_ago(30),
    )

    assert await enforce_capacity(session, elder_id=elder_id, cap=2) == 1
    assert (await _fetch(session, low_old.chunk_id)).status == "deleted"  # 同权重时最旧的先走
    assert (await _fetch(session, low_recent.chunk_id)).status == "active"
    assert (await _fetch(session, important.chunk_id)).status == "active"


async def test_enforce_capacity_noop_under_cap(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    chunk = await _insert_chunk(session, elder_id=elder_id, content="只有一条")

    assert await enforce_capacity(session, elder_id=elder_id, cap=CAPACITY_PER_ELDER) == 0
    assert (await _fetch(session, chunk.chunk_id)).status == "active"
    with pytest.raises(ValueError):
        await enforce_capacity(session, elder_id=elder_id, cap=-1)


async def test_enforce_capacity_isolated_by_elder(db_conn: AsyncConnection) -> None:
    elder_a = await _seed_elder(db_conn)
    elder_b = await _seed_elder(db_conn)
    session = _session(db_conn)
    for index in range(3):
        await _insert_chunk(session, elder_id=elder_a, content=f"A{index}", importance=0.1)
    untouched = await _insert_chunk(
        session, elder_id=elder_b, content="B 的唯一一条", importance=0.1
    )

    assert await enforce_capacity(session, elder_id=elder_a, cap=1) == 2
    assert (await _fetch(session, untouched.chunk_id)).status == "active"
    assert (
        len([chunk for chunk in await _chunks_of(session, elder_a) if chunk.status == "active"])
        == 1
    )


# ============================================================
# J. delete_by_elder 删除 + 审计（同事务）
# ============================================================


async def test_delete_by_elder_soft_deletes_and_audits(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()
    actor_id = uuid.uuid4()

    first = await remember(
        session, elder_id=elder_id, content="第一条", embedding_provider=provider
    )
    second = await remember(
        session, elder_id=elder_id, content="第二条", embedding_provider=provider
    )

    deleted = await delete_by_elder(session, elder_id=elder_id, actor_id=actor_id)
    assert deleted == 2
    assert (await _fetch(session, first.chunk_id)).status == "deleted"
    assert (await _fetch(session, second.chunk_id)).status == "deleted"

    # 同事务：不 commit 也能在**同一条连接**上查到审计行
    audit_stmt = select(AuditLog).where(AuditLog.resource_id == elder_id)
    audits = list((await session.execute(audit_stmt)).scalars().all())
    assert len(audits) == 1
    audit = audits[0]
    assert audit.action == "delete_memory"
    assert audit.resource_type == "memory_chunks"
    assert audit.actor_type == "staff"
    assert audit.actor_id == actor_id
    assert isinstance(audit.detail, dict)
    assert audit.detail["deleted_chunks"] == 2
    assert audit.detail["reason"] == "checkout"

    # 删完就召不回（FR-MEM-02「删除后立即停止检索注入」）
    assert (
        await recall(session, elder_id=elder_id, query="第一条", embedding_provider=provider) == []
    )


async def test_delete_by_elder_without_actor_is_system(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    session = _session(db_conn)
    await remember(
        session,
        elder_id=elder_id,
        content="退房清理",
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert await delete_by_elder(session, elder_id=elder_id, reason="checkout") == 1
    audit = (
        await session.execute(select(AuditLog).where(AuditLog.resource_id == elder_id))
    ).scalar_one()
    assert audit.actor_type == "system"
    assert audit.actor_id == elder_id  # 无操作者时用长者 ID 占位，actor_id 是 NOT NULL


async def test_delete_by_elder_is_isolated_and_idempotent(db_conn: AsyncConnection) -> None:
    elder_a = await _seed_elder(db_conn)
    elder_b = await _seed_elder(db_conn)
    session = _session(db_conn)
    provider = DeterministicEmbeddingProvider()
    chunk_a = await remember(
        session, elder_id=elder_a, content="A 的记忆", embedding_provider=provider
    )
    chunk_b = await remember(
        session, elder_id=elder_b, content="B 的记忆", embedding_provider=provider
    )

    assert await delete_by_elder(session, elder_id=elder_a) == 1
    assert (await _fetch(session, chunk_b.chunk_id)).status == "active"
    # 再删一次只动 active，不重复计数（软删幂等）
    assert await delete_by_elder(session, elder_id=elder_a) == 0
    assert (await _fetch(session, chunk_a.chunk_id)).status == "deleted"


# ============================================================
# K. 闸门② 静态自查
# ============================================================


def test_every_vector_query_is_scoped_by_elder_id() -> None:
    """迭代 9 闸门② 的本地小抄：本模块每一处 `cosine_distance(` 所在函数都必须带 `elder_id`。

    「带 elder_id」不是某个查询的技巧，而是**每一条**向量查询的红线：
    漏一处就是越权召回 B 长者记忆的入口。
    """
    source = Path(memory_service_module.__file__).read_text(encoding="utf-8")
    chunks = [
        chunk for chunk in re.split(r"\nasync def |\ndef ", source) if "cosine_distance(" in chunk
    ]
    assert len(chunks) >= 2, "至少 recall 与 supersede 两处向量查询"
    for chunk in chunks:
        assert "elder_id" in chunk, f"向量查询缺少 elder_id：{chunk.strip().splitlines()[0]}"


def test_protocol_is_structural() -> None:
    """`memory_service` 只依赖协议：两个内置实现都满足它，换供应商不用改五个函数。"""
    assert isinstance(DeterministicEmbeddingProvider(), EmbeddingProvider)
    assert isinstance(_http(_unreachable), EmbeddingProvider)


# ============================================================
# F. list_chunks（照护台「记忆管理」列表）
# ============================================================
#
# 与 recall 的分工：recall 按相似度取 top-k 并刷新 last_accessed；list_chunks 只按
# 创建时间倒序翻页，**不参与记忆的新旧排序**。因此这里直接往表里塞行（要控制
# created_at / status / last_accessed），不走 remember —— remember 会触发归并，
# 反而把待验证的列表语义搅乱。


async def _seed_chunk(
    conn: AsyncConnection,
    elder_id: uuid.UUID,
    *,
    content: str,
    created_at: datetime.datetime,
    status: str = "active",
) -> uuid.UUID:
    """塞一条记忆行，created_at 由用例指定（列表排序就是被验的东西）。"""
    chunk_id = uuid.uuid4()
    await conn.execute(
        MemoryChunk.__table__.insert().values(
            chunk_id=chunk_id,
            elder_id=elder_id,
            content=content,
            category="private",
            importance=1.0,
            status=status,
            created_at=created_at,
        )
    )
    return chunk_id


async def test_list_chunks_orders_by_created_at_desc(db_conn: AsyncConnection) -> None:
    elder_id = await _seed_elder(db_conn)
    base = datetime.datetime(2026, 11, 1, 8, 0, tzinfo=datetime.UTC)
    oldest = await _seed_chunk(db_conn, elder_id, content="最早", created_at=base)
    middle = await _seed_chunk(
        db_conn, elder_id, content="中间", created_at=base + datetime.timedelta(hours=1)
    )
    newest = await _seed_chunk(
        db_conn, elder_id, content="最新", created_at=base + datetime.timedelta(hours=2)
    )

    rows = await list_chunks(_session(db_conn), elder_id=elder_id)

    assert [row.chunk_id for row in rows] == [newest, middle, oldest]


async def test_list_chunks_scopes_to_single_elder(db_conn: AsyncConnection) -> None:
    """`WHERE elder_id` 是硬绑：别人的记忆一条都不许出现在响应里（FR-ADM-06）。"""
    mine = await _seed_elder(db_conn)
    theirs = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=datetime.UTC)
    kept = await _seed_chunk(db_conn, mine, content="我的记忆", created_at=now)
    await _seed_chunk(db_conn, theirs, content="别人的记忆", created_at=now)

    rows = await list_chunks(_session(db_conn), elder_id=mine)

    assert [row.chunk_id for row in rows] == [kept]
    assert all(row.elder_id == mine for row in rows)


@pytest.mark.parametrize("status", ["active", "superseded", "deleted"])
async def test_list_chunks_filters_by_each_status(db_conn: AsyncConnection, status: str) -> None:
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 3, 10, 0, tzinfo=datetime.UTC)
    for index, candidate in enumerate(MEMORY_STATUSES):
        await _seed_chunk(
            db_conn,
            elder_id,
            content=f"{candidate}-{index}",
            created_at=now + datetime.timedelta(minutes=index),
            status=candidate,
        )

    rows = await list_chunks(_session(db_conn), elder_id=elder_id, status=status)

    assert len(rows) == 1
    assert rows[0].status == status
    assert rows[0].content.startswith(status)


async def test_list_chunks_paginates_without_repeating_rows(db_conn: AsyncConnection) -> None:
    """翻页不许重复/漏行：同一 created_at 的并列行靠 chunk_id 做次序键。"""
    elder_id = await _seed_elder(db_conn)
    same_instant = datetime.datetime(2026, 11, 4, 11, 0, tzinfo=datetime.UTC)
    seeded = {
        await _seed_chunk(db_conn, elder_id, content=f"第{i}条", created_at=same_instant)
        for i in range(6)
    }

    first = await list_chunks(_session(db_conn), elder_id=elder_id, limit=4)
    second = await list_chunks(_session(db_conn), elder_id=elder_id, limit=4, offset=4)

    assert len(first) == 4
    assert len(second) == 2
    seen = [row.chunk_id for row in first] + [row.chunk_id for row in second]
    assert len(seen) == len(set(seen)) == 6
    assert set(seen) == seeded


async def test_list_chunks_does_not_touch_last_accessed(db_conn: AsyncConnection) -> None:
    """护工翻列表不该把记忆「刷新成刚被用过」——否则衰减与容量淘汰的判据被污染。"""
    elder_id = await _seed_elder(db_conn)
    now = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.UTC)
    chunk_id = await _seed_chunk(db_conn, elder_id, content="从没被召回", created_at=now)

    session = _session(db_conn)
    await list_chunks(session, elder_id=elder_id)

    stored = (
        await db_conn.execute(
            select(MemoryChunk.last_accessed).where(MemoryChunk.chunk_id == chunk_id)
        )
    ).scalar_one()
    assert stored is None


@pytest.mark.parametrize(
    ("limit", "offset", "status"),
    [
        (0, 0, None),
        (MAX_LIST_LIMIT + 1, 0, None),
        (DEFAULT_LIST_LIMIT, -1, None),
        (DEFAULT_LIST_LIMIT, 0, "archived"),
    ],
)
async def test_list_chunks_rejects_invalid_arguments(
    db_conn: AsyncConnection, limit: int, offset: int, status: str | None
) -> None:
    """参数错就抛 ValueError，不静默返回空列表（那会把配置错误伪装成「没有记忆」）。"""
    elder_id = await _seed_elder(db_conn)
    with pytest.raises(ValueError):
        await list_chunks(
            _session(db_conn), elder_id=elder_id, limit=limit, offset=offset, status=status
        )
