"""N25b · `POST /api/memory/chunks`：来源系数（explicit vs inferred）与写入校验。

覆盖的验收点：

- **来源真的透传到服务层**（讲义 §8.3）：同样的基础 `importance=1.0`，
  `inferred` 从库读回 **严格小于** `explicit`，比例约 1 : 0.6
  （`memory_service.MEMORY_SOURCE_COEFFICIENTS`）——这正是本卡要证明的
  「行为推断进来的记忆权重必须低于明确陈述」；
- **缺省按 explicit**：老客户端不带 `source` 时落库结果与加字段前一致（1.0）；
- **非法来源 422 且不写库**：`count(*)` 真查库，不接受只看响应码；
- **非 admin 403 且不写库**：医生 `ROLE_FORBIDDEN`、家属 `SCOPE_FORBIDDEN`；
- **`elder_id` 只认查询参数**：body 里塞同名值被忽略（AGENTS §4 第 9 条）；
- **降级可观测**：响应 `embedding_provider` / `semantic_embedding` 与日志字段一致，
  别让人把 deterministic 兜底当成语义检索在工作；
- **同内容第二次写入会被 supersede**：这是 `remember()` 的既有去重口径（余弦 >0.9），
  本文件把它锁成事实，供 N27b 对齐（判定键不是文本哈希）。

## 测试怎么不污染库

与 `tests/test_memory_api.py` 同口径：业务读写走 `db` fixture 的外层事务（请求内
`commit()` 只释放保存点，最后整体回滚）；越权审计由服务层「自己开会话」写入，故用
`isolated_session_factory` 把它钉在同一条测试连接上，不落开发库。

## provider 为什么在用例里固定成 deterministic

`build_embedding_provider()` 在 `.env` 配了 key 时会打真实 HTTP。测试要的是
「权重的算术」而不是「供应商可用性」，所以默认把 provider 换成确定性实现；另有一条
用例换成假语义 provider，单独证明 `semantic_embedding=True` 这条标记不是写死的。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import memory as memory_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Elder, MemoryChunk, StaffAccount
from app.models.memory import EMBEDDING_DIM
from app.services import report_service
from app.services.embedding import DeterministicEmbeddingProvider

#: 两条不同内容的记忆：**不能**用同一句——确定性 provider 对同一文本给出同一条向量，
#: 第二次写入会按「余弦 1.0 > 0.9」把第一条 supersede，读回来就只剩一行了。
CONTENT_EXPLICIT = "女儿上周日来看过我，带了桂花糕。"
CONTENT_INFERRED = "他最近下午常在阳台坐着，可能喜欢安静。"


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
    """外层事务：用例里所有写入最后整体回滚。"""
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
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把越权审计的「自己开会话」钉在同一条测试连接上（同 test_memory_api.py）。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    return factory


@pytest.fixture(autouse=True)
def deterministic_provider(monkeypatch: pytest.MonkeyPatch) -> DeterministicEmbeddingProvider:
    """固定 provider：不让用例去打真实 embedding API，也不受 `.env` 影响。"""
    provider = DeterministicEmbeddingProvider()
    monkeypatch.setattr(memory_api, "_EMBEDDING_PROVIDER", provider)
    return provider


@dataclass
class Seed:
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    guardian_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, guardian_id = (uuid.uuid4() for _ in range(3))
    elder_id, other_elder_id = (uuid.uuid4() for _ in range(2))

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": admin_id, "name": "管理员", "role": "admin", "password_hash": "x"},
            {"staff_id": doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": elder_id, "name": "张桂芬", "status": "active"},
            {"elder_id": other_elder_id, "name": "李振华", "status": "active"},
        ],
    )
    await db.flush()
    return Seed(
        admin_id=admin_id,
        doctor_id=doctor_id,
        guardian_id=guardian_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    """只建账号与长者，**不预置记忆**：行数断言从 0 起算最干净。"""
    return await _seed(db)


@dataclass
class Bundle:
    client: AsyncClient
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(memory_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db.bind,  # type: ignore[arg-type]
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    holder: list[Identity] = []

    def override_principal() -> Identity:
        return holder[0]

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_principal] = override_principal

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        yield Bundle(client=ac, holder=holder)


def _as_admin(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.admin_id), role="admin")


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


# --------------------------------------------------------------------- 小工具


async def _chunk_rows(db: AsyncSession, elder_id: uuid.UUID) -> list[MemoryChunk]:
    rows = await db.scalars(
        select(MemoryChunk).where(MemoryChunk.elder_id == elder_id).order_by(MemoryChunk.created_at)
    )
    return list(rows)


async def _chunk_count(db: AsyncSession, elder_id: uuid.UUID) -> int:
    """真查库的行数：422 用例必须用它证明「没写」，而不是看响应码猜。"""
    return (
        await db.scalar(
            select(func.count()).select_from(MemoryChunk).where(MemoryChunk.elder_id == elder_id)
        )
    ) or 0


def _payload(content: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content}
    body.update(overrides)
    return body


async def _post(client: Bundle, elder_id: uuid.UUID, body: dict[str, Any], **params: Any) -> Any:
    return await client.client.post(
        "/api/memory/chunks",
        params={"elder_id": str(elder_id), **params},
        json=body,
    )


# --------------------------------------------------- 来源系数（本卡的核心断言）


async def test_inferred_weight_is_strictly_lower_than_explicit(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """explicit ×1.0 vs inferred ×0.6：从库读回 importance，前者严格大于后者。"""
    client.holder[:] = [_as_admin(seeded)]

    explicit = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT, source="explicit"))
    inferred = await _post(client, seeded.elder_id, _payload(CONTENT_INFERRED, source="inferred"))

    assert explicit.status_code == 201, explicit.text
    assert inferred.status_code == 201, inferred.text
    assert explicit.json()["importance"] == pytest.approx(1.0)
    assert inferred.json()["importance"] == pytest.approx(0.6)

    rows = {row.content: row for row in await _chunk_rows(db, seeded.elder_id)}
    explicit_row = rows[CONTENT_EXPLICIT]
    inferred_row = rows[CONTENT_INFERRED]

    # 从库读回（不是拿响应自证）：Decimal 转 float 后比较，容差留给 numeric(3,2) 的存储。
    assert float(explicit_row.importance) > float(inferred_row.importance)
    assert float(inferred_row.importance) == pytest.approx(0.6, abs=0.005)
    assert float(explicit_row.importance) == pytest.approx(1.0, abs=0.005)
    # 比例约 1 : 0.6 —— 直接把验收口径写死，改了系数就会红。
    assert float(inferred_row.importance) / float(explicit_row.importance) == pytest.approx(
        0.6, abs=0.01
    )
    # 来源不落库（TD 不新增列）：写就折算进 importance，别指望从行里回读 source。
    assert not hasattr(explicit_row, "source") or "source" not in {
        column.name for column in MemoryChunk.__table__.columns
    }


async def test_source_defaults_to_explicit(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """不带 `source` 的老客户端按 explicit 落库（行为不变，权重不被悄悄改小）。"""
    client.holder[:] = [_as_admin(seeded)]

    resp = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT))

    assert resp.status_code == 201, resp.text
    assert resp.json()["importance"] == pytest.approx(1.0)
    rows = await _chunk_rows(db, seeded.elder_id)
    assert len(rows) == 1
    assert float(rows[0].importance) == pytest.approx(1.0, abs=0.005)


async def test_explicit_importance_is_scaled_by_source(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """base importance 也参与：0.8 的 inferred 落库是 0.48，不是 0.8×1.0。"""
    client.holder[:] = [_as_admin(seeded)]

    resp = await _post(
        client, seeded.elder_id, _payload(CONTENT_INFERRED, source="inferred", importance=0.8)
    )

    assert resp.status_code == 201, resp.text
    rows = await _chunk_rows(db, seeded.elder_id)
    assert float(rows[0].importance) == pytest.approx(0.48, abs=0.005)


# ------------------------------------------------------------- 校验与不写库


async def test_invalid_source_returns_422_and_writes_nothing(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """`source="guess"` 在 Pydantic 层就 422：handler 不执行，库行数不变。"""
    client.holder[:] = [_as_admin(seeded)]
    before = await _chunk_count(db, seeded.elder_id)

    resp = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT, source="guess"))

    assert resp.status_code == 422, resp.text
    assert resp.json().get("code") == "VALIDATION_ERROR"
    after = await _chunk_count(db, seeded.elder_id)
    assert after == before == 0, "422 必须不写库（真查 count，不看响应码）"


async def test_blank_content_returns_422_and_writes_nothing(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """只有空白的正文能过 min_length，但服务层 strip 后为空 → 422，且不留半条行。"""
    client.holder[:] = [_as_admin(seeded)]
    before = await _chunk_count(db, seeded.elder_id)

    resp = await _post(client, seeded.elder_id, _payload("   ", source="explicit"))

    assert resp.status_code == 422, resp.text
    assert resp.json().get("code") == "VALIDATION_ERROR"
    assert await _chunk_count(db, seeded.elder_id) == before == 0


async def test_oversized_content_returns_422(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """超 200 字在请求模型就拒（`remember` 的拒收是兜底，不该靠它当主校验）。"""
    client.holder[:] = [_as_admin(seeded)]

    resp = await _post(client, seeded.elder_id, _payload("桂花糕" * 80))

    assert resp.status_code == 422, resp.text
    assert await _chunk_count(db, seeded.elder_id) == 0


# --------------------------------------------------------------- 权限与标识


async def test_doctor_cannot_write(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """医生 403 `ROLE_FORBIDDEN`，且不写库（与 GET 同口径，护工/医生不在 FR-MEM-02）。"""
    client.holder[:] = [_as_doctor(seeded)]

    resp = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT))

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    assert await _chunk_count(db, seeded.elder_id) == 0


async def test_guardian_cannot_write(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """家属 403 `SCOPE_FORBIDDEN`，同样不写库。"""
    client.holder[:] = [_as_guardian(seeded)]

    resp = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT))

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"
    assert await _chunk_count(db, seeded.elder_id) == 0


async def test_elder_id_in_body_is_ignored(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """body 里的 elder_id 不采信：只认查询参数（AGENTS §4 第 9 条）。"""
    client.holder[:] = [_as_admin(seeded)]

    resp = await _post(
        client,
        seeded.elder_id,
        _payload(CONTENT_EXPLICIT, elder_id=str(seeded.other_elder_id)),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["elder_id"] == str(seeded.elder_id)
    assert await _chunk_count(db, seeded.elder_id) == 1
    assert await _chunk_count(db, seeded.other_elder_id) == 0


# ------------------------------------------------- 降级可观测 / 去重口径


async def test_degraded_provider_is_marked_in_body_and_log(
    client: Bundle, seeded: Seed, caplog: pytest.LogCaptureFixture
) -> None:
    """deterministic 兜底必须能被看见：响应两个字段 + 日志键一致，日志不记正文。"""
    client.holder[:] = [_as_admin(seeded)]
    caplog.set_level(logging.INFO, logger="app.api.memory")

    resp = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT, source="inferred"))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["embedding_provider"] == "DeterministicEmbeddingProvider"
    assert body["semantic_embedding"] is False

    records = [record for record in caplog.records if record.message == "memory_written"]
    assert len(records) == 1
    record = records[0]
    assert record.embedding_degraded is True  # type: ignore[attr-defined]
    assert record.semantic_embedding is False  # type: ignore[attr-defined]
    assert record.source == "inferred"  # type: ignore[attr-defined]
    assert record.importance == pytest.approx(0.6)  # type: ignore[attr-defined]
    assert CONTENT_EXPLICIT not in caplog.text, "记忆正文不得进日志（红线 10）"


async def test_semantic_provider_is_marked_true(
    client: Bundle, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换成真的语义 provider（假实现）时标记必须翻成 True——证明它跟 provider 走，不是写死。"""

    class _FakeSemanticProvider:
        dimension = EMBEDDING_DIM
        available = True

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.01] * EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr(memory_api, "_EMBEDDING_PROVIDER", _FakeSemanticProvider())
    client.holder[:] = [_as_admin(seeded)]

    resp = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT, source="inferred"))

    assert resp.status_code == 201, resp.text
    assert resp.json()["semantic_embedding"] is True
    assert resp.json()["embedding_provider"] == "_FakeSemanticProvider"


async def test_same_content_twice_supersedes_previous(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """同内容第二次写入 → 旧行 `superseded`，active 只剩一条（对齐 N27b 的去重口径）。"""
    client.holder[:] = [_as_admin(seeded)]

    first = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT, source="explicit"))
    second = await _post(client, seeded.elder_id, _payload(CONTENT_EXPLICIT, source="inferred"))

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    rows = await _chunk_rows(db, seeded.elder_id)
    active = [row for row in rows if row.status == "active"]
    superseded = [row for row in rows if row.status == "superseded"]
    assert len(rows) == 2, "冲突不是物理删除：旧行留着可回查"
    assert len(active) == 1 and len(superseded) == 1
    # 判定键是「同长者 + 同类别 + active + 有向量 + 余弦 > 0.9」，不是文本哈希：
    # 确定性 provider 下相同文本向量一致（cosine=1.0）才命中；换真语义模型时近义改写也会命中。
    assert superseded[0].content == CONTENT_EXPLICIT
    assert float(active[0].importance) == pytest.approx(0.6, abs=0.005)
