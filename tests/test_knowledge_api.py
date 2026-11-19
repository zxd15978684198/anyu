"""N22 机构知识 HTTP 面（`app/api/knowledge.py`）与工具注册（`search_institution_knowledge`）。

这张卡要证明的四件事：

1. **检索的读权限是 admin/doctor/care_worker，写权限只有 admin**（FR-KNW-01/02）。
   家属与设备不属于本端点主体范围（`SCOPE_FORBIDDEN`），医生/护工写是 `ROLE_FORBIDDEN`——
   两条越权路径都必须先落 `audit_logs` 再 403，不能静默。
2. **检索没有 `elder_id`**：知识不分人，传了也不会被采信（本端点根本没声明这个参数）。
3. **`degraded` 与 `no_match` 是两件事**：embedding 不可用时返回 `hits=[]` +
   `reason="degraded"`，工具层据此说「查询出了点小状况」，而不是「没有这条规定」。
4. **换代不覆盖历史**：`supersede` 新建版本、旧版转 `retired`，检索立刻不再命中旧版。

写法沿用 `tests/test_memory_api.py`：请求内 `commit()` 只释放保存点，外层事务整体回滚；
越权审计走 `_write_audit` 的「自己开会话」，用 `isolated_session_factory` 钉在同一条连接上。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.agents import tool_registry
from app.api import knowledge as knowledge_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, StaffAccount
from app.models.knowledge import EMBEDDING_DIM
from app.services import knowledge_service, report_service
from app.services.embedding import DeterministicEmbeddingProvider

SCHEDULE_BODY = (
    "每天 06:30 响起床音乐，早餐 07:30 至 08:30。\n\n熄灯时间 21:30，之后请保持房间安静。\n"
)


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
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    return factory


@dataclass
class Seed:
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    guardian_id: uuid.UUID
    device_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, care_worker_id = (uuid.uuid4() for _ in range(3))
    guardian_id, device_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": admin_id, "name": "王管理", "role": "admin", "password_hash": "x"},
            {"staff_id": doctor_id, "name": "王医生", "role": "doctor", "password_hash": "x"},
            {
                "staff_id": care_worker_id,
                "name": "李护工",
                "role": "care_worker",
                "password_hash": "x",
            },
        ],
    )
    await db.flush()
    return Seed(
        admin_id=admin_id,
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        guardian_id=guardian_id,
        device_id=device_id,
    )


@dataclass
class Seeded(Seed):
    document_id: uuid.UUID
    title: str
    first_chunk: str


@pytest.fixture
async def seeded(db: AsyncSession) -> Seeded:
    seed = await _seed(db)
    title = f"机构作息安排-{uuid.uuid4().hex[:8]}"
    document = await knowledge_service.ingest_document(
        db,
        title=title,
        body=SCHEDULE_BODY,
        category="schedule",
        actor_id=seed.admin_id,
        embedding_provider=DeterministicEmbeddingProvider(),
    )
    await db.flush()
    first_chunk = knowledge_service.split_into_chunks(SCHEDULE_BODY)[0]
    return Seeded(
        **seed.__dict__,
        document_id=document.document_id,
        title=title,
        first_chunk=first_chunk,
    )


@dataclass
class Bundle:
    client: AsyncClient
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(knowledge_api.router)

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


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


def _as_device(seed: Seed) -> Identity:
    return Identity(kind="device", device_id=str(seed.device_id))


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 主体过滤：`audit_logs` 只增，开发库里可能残留同名 action 行。"""
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    return list(await db.scalars(stmt))


class _UnavailableProvider:
    """`available=False` 的替身：覆盖「embedding 没配好」这一路，不去碰网络。"""

    dimension = EMBEDDING_DIM
    available = False

    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - 不该被调到
        raise AssertionError("unavailable provider must not be asked to embed")


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr(knowledge_service, "build_embedding_provider", lambda *a, **k: provider)


# --------------------------------------------------------------- 检索：正常路径


async def test_admin_can_search_and_hits_carry_provenance(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get("/api/knowledge/search", params={"query": seeded.first_chunk})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reason"] == "ok"
    assert body["degraded"] is False
    assert body["limit"] == knowledge_service.DEFAULT_SEARCH_LIMIT
    hit = next(item for item in body["hits"] if item["title"] == seeded.title)
    assert hit["chunk_index"] == 0
    assert hit["category"] == "schedule"
    assert hit["authority_level"] == "institution"
    assert hit["version"] == 1
    assert hit["effective_from"].endswith("Z")
    assert hit["effective_until"] is None
    assert 0.0 <= hit["similarity"] <= 1.0
    assert "embedding" not in hit


@pytest.mark.parametrize("who", ["doctor", "care_worker"])
async def test_doctor_and_care_worker_may_search(client: Bundle, seeded: Seeded, who: str) -> None:
    client.holder[:] = [_as_doctor(seeded) if who == "doctor" else _as_care_worker(seeded)]

    resp = await client.client.get("/api/knowledge/search", params={"query": "熄灯时间"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] in {"ok", "no_match"}


async def test_empty_query_returns_empty_query_without_touching_embedding(
    client: Bundle, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.holder[:] = [_as_admin(seeded)]

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("empty query must not construct an embedding provider")

    monkeypatch.setattr(knowledge_service, "build_embedding_provider", _boom)

    resp = await client.client.get("/api/knowledge/search", params={"query": "   "})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reason"] == "empty_query"
    assert body["hits"] == []


async def test_invalid_category_is_a_validation_error(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get(
        "/api/knowledge/search", params={"query": "熄灯", "category": "weather"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_limit_above_max_is_rejected(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get(
        "/api/knowledge/search",
        params={"query": "熄灯", "limit": knowledge_service.MAX_SEARCH_LIMIT + 1},
    )

    assert resp.status_code == 422, resp.text


async def test_degraded_embeddings_are_visible_not_silently_empty(
    client: Bundle, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.holder[:] = [_as_admin(seeded)]
    _install_provider(monkeypatch, _UnavailableProvider())

    resp = await client.client.get("/api/knowledge/search", params={"query": "熄灯时间"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hits"] == []
    assert body["degraded"] is True
    assert body["reason"] == "degraded"


# --------------------------------------------------------------- 检索：越权


@pytest.mark.parametrize("who", ["guardian", "device"])
async def test_non_staff_principals_are_scope_forbidden_and_audited(
    client: Bundle, seeded: Seeded, db: AsyncSession, who: str
) -> None:
    identity = _as_guardian(seeded) if who == "guardian" else _as_device(seeded)
    client.holder[:] = [identity]
    actor_id = uuid.UUID(identity.guardian_id or identity.device_id or "")

    resp = await client.client.get("/api/knowledge/search", params={"query": "熄灯时间"})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"
    rows = await _audit_rows(db, "knowledge.search", actor_id=actor_id)
    assert len(rows) == 1
    assert rows[0].actor_type == identity.kind
    # 非 staff 主体与「staff 角色不符」是两条不同的审计理由，别混成一个。
    assert rows[0].detail["reason"] == "not_staff"
    assert rows[0].detail["trace_id"]


# --------------------------------------------------------------- 写入


async def test_admin_can_create_document(client: Bundle, seeded: Seeded, db: AsyncSession) -> None:
    client.holder[:] = [_as_admin(seeded)]
    title = f"本周活动室安排-{uuid.uuid4().hex[:8]}"

    resp = await client.client.post(
        "/api/knowledge/documents",
        json={
            "title": title,
            "body": "周一 15:00 戏曲鉴赏。\n\n周三 10:00 书法。\n",
            "category": "activity",
            "effective_from": "2026-11-01",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == title
    assert body["category"] == "activity"
    assert body["status"] == "active"
    assert body["version"] == 1
    assert body["chunks"] == 2
    assert body["embedding_provider"] == "DeterministicEmbeddingProvider"
    assert body["semantic_embedding"] is False
    rows = await _audit_rows(db, "knowledge.document.ingest", actor_id=seeded.admin_id)
    # 同一个 admin 在 seeded fixture 里也写过一篇，所以按新文档 id 认领，别数总数。
    assert uuid.UUID(body["document_id"]) in {row.resource_id for row in rows}


async def test_duplicate_title_and_version_conflicts(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]
    title = f"重复标题-{uuid.uuid4().hex[:8]}"
    payload = {"title": title, "body": "正文一段。\n", "category": "policy"}

    first = await client.client.post("/api/knowledge/documents", json=payload)
    second = await client.client.post("/api/knowledge/documents", json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "CONFLICT"


async def test_write_with_chinese_category_is_rejected(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.post(
        "/api/knowledge/documents",
        json={"title": "中文类别", "body": "正文。\n", "category": "作息"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("who", ["doctor", "care_worker"])
async def test_non_admin_staff_cannot_write_and_is_audited(
    client: Bundle, seeded: Seeded, db: AsyncSession, who: str
) -> None:
    identity = _as_doctor(seeded) if who == "doctor" else _as_care_worker(seeded)
    client.holder[:] = [identity]

    resp = await client.client.post(
        "/api/knowledge/documents",
        json={
            "title": f"越权写入-{uuid.uuid4().hex[:8]}",
            "body": "正文。\n",
            "category": "policy",
        },
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, "knowledge.write", actor_id=uuid.UUID(str(identity.staff_id)))
    assert len(rows) == 1
    assert rows[0].detail["role"] == identity.role


# --------------------------------------------------------------- 换代


async def test_supersede_retires_previous_version(
    client: Bundle, seeded: Seeded, db: AsyncSession
) -> None:
    from app.models import KnowledgeDocument

    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.post(
        f"/api/knowledge/documents/{seeded.document_id}/supersede",
        json={"body": "熄灯时间改为 22:00。\n"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["document_id"] != str(seeded.document_id)
    assert body["version"] == 2
    assert body["status"] == "active"

    previous = await db.get(KnowledgeDocument, seeded.document_id)
    assert previous is not None
    assert previous.status == "retired"
    assert previous.effective_until is not None
    rows = await _audit_rows(db, "knowledge.document.supersede", actor_id=seeded.admin_id)
    assert len(rows) == 1
    assert rows[0].detail["previous_version"] == 1


async def test_supersede_unknown_document_is_404(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.post(
        f"/api/knowledge/documents/{uuid.uuid4()}/supersede", json={"body": "新口径。\n"}
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------- 管理面：列表


async def test_non_admin_cannot_list_documents_and_is_audited(
    client: Bundle, seeded: Seeded, db: AsyncSession
) -> None:
    """医生能搜已生效知识，但**不能**看 draft/retired 全量列表：403 且留痕。"""
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.get("/api/knowledge/documents")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, "knowledge.list", actor_id=seeded.doctor_id)
    assert len(rows) == 1
    assert rows[0].detail["role"] == "doctor"


async def test_admin_list_filters_by_status_and_category(
    client: Bundle, seeded: Seeded, db: AsyncSession
) -> None:
    from app.models import KnowledgeDocument

    client.holder[:] = [_as_admin(seeded)]
    draft_title = f"草稿制度-{uuid.uuid4().hex[:8]}"
    db.add(
        KnowledgeDocument(
            document_id=uuid.uuid4(),
            title=draft_title,
            category="policy",
            version=1,
            effective_from=datetime.now(UTC),
            authority_level="institution",
            status="draft",
            body="草稿正文。",
            created_by=seeded.admin_id,
            updated_by=seeded.admin_id,
        )
    )
    await db.flush()

    drafts = await client.client.get("/api/knowledge/documents", params={"status": "draft"})
    assert drafts.status_code == 200, drafts.text
    draft_body = drafts.json()
    assert draft_body["total"] >= 1
    assert {item["status"] for item in draft_body["items"]} == {"draft"}
    assert draft_title in {item["title"] for item in draft_body["items"]}

    policies = await client.client.get("/api/knowledge/documents", params={"category": "policy"})
    assert policies.status_code == 200, policies.text
    assert policies.json()["items"], "库里应有 policy 文档（草稿 + 种子里的应急流程）"
    assert {item["category"] for item in policies.json()["items"]} == {"policy"}

    narrowed = await client.client.get(
        "/api/knowledge/documents", params={"category": "policy", "q": draft_title}
    )
    assert narrowed.status_code == 200, narrowed.text
    assert [item["title"] for item in narrowed.json()["items"]] == [draft_title]


async def test_admin_list_rejects_unknown_status(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get("/api/knowledge/documents", params={"status": "archived"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------- 管理面：停用


async def test_retire_is_idempotent_and_audited_once(
    client: Bundle, seeded: Seeded, db: AsyncSession
) -> None:
    client.holder[:] = [_as_admin(seeded)]

    first = await client.client.post(f"/api/knowledge/documents/{seeded.document_id}/retire")
    second = await client.client.post(f"/api/knowledge/documents/{seeded.document_id}/retire")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["already_retired"] is False
    assert second.json()["already_retired"] is True
    assert first.json()["status"] == "retired"
    assert second.json()["effective_until"] == first.json()["effective_until"]

    rows = await _audit_rows(db, "knowledge.document.retire", actor_id=seeded.admin_id)
    assert len(rows) == 1
    assert rows[0].resource_id == seeded.document_id


async def test_retired_document_disappears_from_search(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    before = await client.client.get(
        "/api/knowledge/search", params={"query": seeded.first_chunk, "category": "schedule"}
    )
    assert before.status_code == 200, before.text
    assert seeded.document_id in {uuid.UUID(hit["document_id"]) for hit in before.json()["hits"]}

    retired = await client.client.post(f"/api/knowledge/documents/{seeded.document_id}/retire")
    assert retired.status_code == 200, retired.text

    after = await client.client.get(
        "/api/knowledge/search", params={"query": seeded.first_chunk, "category": "schedule"}
    )
    assert after.status_code == 200, after.text
    assert after.json()["degraded"] is False
    assert seeded.document_id not in {uuid.UUID(hit["document_id"]) for hit in after.json()["hits"]}


async def test_retire_unknown_document_is_404(client: Bundle, seeded: Seeded) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.post(f"/api/knowledge/documents/{uuid.uuid4()}/retire")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------- 工具注册


class _NeverCalledBackend:
    """MCP 后端替身：本地工具不该走到它。"""

    async def call(self, name: str, args: dict[str, Any]) -> tool_registry.ToolResult:
        raise AssertionError(f"local tool {name} must not hit the MCP backend")


async def test_tool_registry_has_the_knowledge_tool_whitelisted() -> None:
    assert "search_institution_knowledge" in tool_registry.WHITELIST
    assert "search_institution_knowledge" in tool_registry.LOCAL_TOOLS
    assert set(tool_registry.default_local_backends()) == tool_registry.LOCAL_TOOLS
    registry = tool_registry.ToolRegistry(backend=_NeverCalledBackend())
    assert registry.is_whitelisted("search_institution_knowledge")


async def test_tool_without_query_is_a_param_error() -> None:
    registry = tool_registry.ToolRegistry(backend=_NeverCalledBackend())

    outcome = await registry.call("search_institution_knowledge", {})

    assert outcome.result.status == tool_registry.TOOL_STATUS_PARAM_ERROR
    assert outcome.result.content["reason"] == "missing_field:query"
    assert outcome.record.executed is False
    assert outcome.record.gate == "schema"


async def test_tool_returns_hits_from_the_local_knowledge_backend(
    db: AsyncSession, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真后端链路：注册表 → KnowledgeToolBackend → knowledge_service → 数据库。"""
    from app import db as db_module

    bind = db.bind
    assert bind is not None
    monkeypatch.setattr(
        db_module,
        "session_factory",
        async_sessionmaker(bind=bind, expire_on_commit=False),  # type: ignore[arg-type]
    )
    registry = tool_registry.ToolRegistry(backend=_NeverCalledBackend())

    outcome = await registry.call("search_institution_knowledge", {"query": seeded.first_chunk})

    assert outcome.result.status == tool_registry.TOOL_STATUS_OK, outcome.result
    assert outcome.record.executed is True
    hits = outcome.result.content["hits"]
    assert any(hit["title"] == seeded.title for hit in hits)
    assert outcome.result.content["reason"] == "ok"
