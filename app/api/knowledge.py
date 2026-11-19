"""机构知识检索与写入 REST（PRD FR-KNW-01 / FR-KNW-03，DAG N22）。

## 为什么这组端点存在

机构知识（作息、餐饮、活动、制度、设施、服务流程）此前**只有模型、迁移与检索服务**，
没有任何读或写的 HTTP 入口：检索服务写好了却没人调，文档表空着 —— 端到端等于没有
（DAG N22 的原话是「写入侧为空，seed 比 API 更急」）。本模块把两段补齐：

| 端点 | 作用 | 允许 | 其它员工角色 | 家属 / 设备 |
| --- | --- | --- | --- | --- |
| `GET /api/knowledge/search` | 按语义检索知识分块 | `admin` / `doctor` / `care_worker` | — | 403 `SCOPE_FORBIDDEN` + 审计 |
| `POST /api/knowledge/documents` | 录入一篇知识（文档 + 分块） | `admin` | 403 `ROLE_FORBIDDEN` + 审计 | 403 `SCOPE_FORBIDDEN` + 审计 |
| `POST /api/knowledge/documents/{id}/supersede` | 换代（新版取旧版而代之） | `admin` | 403 `ROLE_FORBIDDEN` + 审计 | 403 `SCOPE_FORBIDDEN` + 审计 |

## 为什么检索是医生/护工可读、写入只给 admin

知识的**读取**没有隐私风险：作息、探视时间本来就贴在每个病区的墙上，医生与护工
每天都在用。写入不同 —— 它决定「机构口径」是什么（午餐几点、探视到几点），
写错会直接落到长者与家属面前，所以与 `app/api/memory.py` 的管理端闸门同构，仅
`admin`，且越权先写 `audit_logs` 再拒绝（AGENTS §8：越权 → 拒绝 + 审计 + trace_id）。

## 检索**没有** `elder_id`（知识不隔离，记忆才隔离）

FR-MEM-05 的隔离口径是「记忆按长者隔离」；机构知识是平台侧配置，全院长者共享。
本模块因此没有 `elder_id` 参数，服务层 `search_knowledge_result` 也没有 —— 长者专属
知识走 `authority_level='elder_memory'` 与文档归属，不靠 `WHERE elder_id`。
这一条在 `app/services/knowledge_service.py` 的模块 docstring 里也写了一遍，
两处都读得到，避免以后有人"顺手"加一个 `elder_id` 过滤把知识检索变成按人查。

## 降级必须如实回传

`search` 的响应带 `reason`（`ok` / `no_match` / `empty_query` / `degraded`）与
`degraded`。当前进程若没配齐 embedding 配置，`embedding_provider_degraded` 会落到
确定性 provider（SHA-256 噪声、语义能力为零），此时 `reason="degraded"`、
`hits=[]`。**不允许**把它读成「库里没有这条知识」—— `no_match` 才是「查了但没有」，
`degraded` 是「这次根本没法语义查」。同理，本模块日志也不打印知识正文以外的请求内容。

## 分层（AGENTS §2）

本模块只做参数校验、角色过滤、调 `app/services/knowledge_service.py`、序列化。
**不出现** `session.add/commit`，**不出现** `select(KnowledgeChunk)`：文档、分块、
向量与 `audit_logs` 都在 `ingest_document` / `supersede_document` 的同一事务里写完，
本模块只等 `get_session` 收口。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.models.knowledge import KNOWLEDGE_CATEGORIES, KnowledgeDocument
from app.services import knowledge_service
from app.services.elder_service import ElderScopeForbiddenError
from app.services.embedding import DeterministicEmbeddingProvider, build_embedding_provider
from app.services.knowledge_service import (
    KnowledgeConflictError,
    KnowledgeEmbeddingError,
    KnowledgeNotFoundError,
    KnowledgeValidationError,
)
from app.services.report_service import _write_audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/knowledge",
    tags=["knowledge"],
    route_class=ContractAPIRoute,
)

#: 知识读取：医生看病、护工答家属都要用（不做长者范围过滤——知识不隔离）。
READ_ROLES: frozenset[str] = frozenset({"admin", "doctor", "care_worker"})

#: 知识写入：决定「机构口径」是什么，仅管理员（理由见模块 docstring）。
WRITE_ROLES: frozenset[str] = frozenset({"admin"})

#: 检索单次上限直接跟服务层常量走，别在 API 再写一个 20。
MAX_SEARCH_LIMIT = knowledge_service.MAX_SEARCH_LIMIT

#: 检索没有具体资源可指时，审计里的 `resource_id` 用全零 UUID（`audit_logs` 该列 NOT NULL）。
NIL_RESOURCE_ID = uuid.UUID(int=0)


# ------------------------------------------------------------------ 响应 / 请求模型


class KnowledgeHitOut(BaseModel):
    """一条命中：正文 + 来源/生效/权威三要素 + 距离（相似度在同一处换算，见服务层）。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    title: str
    category: str
    authority_level: str
    version: int
    effective_from: datetime.datetime
    effective_until: datetime.datetime | None
    content: str
    distance: float
    similarity: float


class KnowledgeSearchOut(BaseModel):
    """检索结果。`reason` 与 `degraded` 是「为什么是这个结果」，不许让前端猜。"""

    hits: list[KnowledgeHitOut]
    reason: str
    degraded: bool
    limit: int


class KnowledgeDocumentOut(BaseModel):
    """一篇已入库的知识文档（不含正文分块与向量）。"""

    document_id: uuid.UUID
    title: str
    category: str
    version: int
    authority_level: str
    status: str
    effective_from: datetime.datetime
    effective_until: datetime.datetime | None
    chunks: int
    embedding_provider: str
    semantic_embedding: bool


class KnowledgeIngestIn(BaseModel):
    """录入请求。`category` 走服务层白名单校验（非法值 → 422，不是 500）。"""

    title: str = Field(min_length=1, max_length=knowledge_service.TITLE_MAX_LENGTH)
    body: str = Field(min_length=1, max_length=knowledge_service.MAX_BODY_LENGTH)
    category: str = Field(description=f"六类之一：{'/'.join(KNOWLEDGE_CATEGORIES)}")
    version: int = Field(default=1, ge=1)
    authority_level: str = "institution"
    effective_from: datetime.datetime | None = None
    effective_until: datetime.datetime | None = None


class KnowledgeSupersedeIn(BaseModel):
    """换代请求：只带新正文与新起算时间，版本号缺省由服务层 +1。"""

    body: str = Field(min_length=1, max_length=knowledge_service.MAX_BODY_LENGTH)
    version: int | None = Field(default=None, ge=1)
    effective_from: datetime.datetime | None = None
    effective_until: datetime.datetime | None = None


class KnowledgeDocumentSummaryOut(BaseModel):
    """管理列表的一行：元数据 + 分块数，**不含正文**（正文按需再看，列表别拖大响应）。"""

    document_id: uuid.UUID
    title: str
    category: str
    version: int
    authority_level: str
    status: str
    effective_from: datetime.datetime
    effective_until: datetime.datetime | None
    chunks: int


class KnowledgeDocumentPageOut(BaseModel):
    """一页列表。`total` 用同一组过滤条件单独 count，翻页与总数不会打架。"""

    items: list[KnowledgeDocumentSummaryOut]
    total: int
    limit: int
    offset: int


class KnowledgeRetireOut(BaseModel):
    """停用结果。`already_retired=True` 表示这次是重复请求（幂等，不重复写审计）。"""

    document_id: uuid.UUID
    title: str
    version: int
    status: str
    effective_until: datetime.datetime | None
    already_retired: bool


# ------------------------------------------------------------------ 权限与审计


async def _audit_denied(
    identity: Identity,
    *,
    action: str,
    resource_id: uuid.UUID,
    reason: str = "role_not_allowed",
) -> None:
    """越权留痕：写 `audit_logs`（独立会话提交），随后的 403 不会把它回滚掉。"""
    raw = identity.staff_id or identity.guardian_id or identity.device_id
    await _write_audit(
        actor_type=identity.kind,
        actor_id=uuid.UUID(raw) if raw else uuid.UUID(int=0),
        action=action,
        resource_type="knowledge_documents",
        resource_id=resource_id,
        detail={
            "trace_id": get_trace_id() or "-",
            "reason": reason,
            "role": identity.role,
            "kind": identity.kind,
        },
    )


async def _require_roles(
    identity: Identity,
    allowed: frozenset[str],
    *,
    action: str,
    resource_id: uuid.UUID,
) -> None:
    """先判主体种类、再判角色；两种拒绝都**先写审计**再抛 403（同 `app/api/memory.py`）。"""
    if identity.kind != "staff":
        await _audit_denied(identity, action=action, resource_id=resource_id, reason="not_staff")
        raise ElderScopeForbiddenError("knowledge endpoints are not reachable by family/device")
    if (identity.role or "") not in allowed:
        await _audit_denied(identity, action=action, resource_id=resource_id)
        raise RoleForbiddenError(
            f"role {identity.role!r} is not allowed to use knowledge endpoints"
        )


def _actor_id(identity: Identity) -> uuid.UUID:
    """写入人：`created_by` / `updated_by` 是 NOT NULL 外键，只能来自登录主体。"""
    if identity.staff_id is None:  # pragma: no cover - `_require_roles` 已拦住非 staff
        raise RoleForbiddenError("knowledge writes require a staff identity")
    return uuid.UUID(identity.staff_id)


# ------------------------------------------------------------------ 视图 → 响应


def _summary_out(item: knowledge_service.KnowledgeDocumentSummary) -> KnowledgeDocumentSummaryOut:
    return KnowledgeDocumentSummaryOut(
        document_id=item.document_id,
        title=item.title,
        category=item.category,
        version=item.version,
        authority_level=item.authority_level,
        status=item.status,
        effective_from=item.effective_from,
        effective_until=item.effective_until,
        chunks=item.chunks,
    )


def _hit_out(hit: knowledge_service.KnowledgeHit) -> KnowledgeHitOut:
    return KnowledgeHitOut(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        chunk_index=hit.chunk_index,
        title=hit.title,
        category=hit.category,
        authority_level=hit.authority_level,
        version=hit.version,
        effective_from=hit.effective_from,
        effective_until=hit.effective_until,
        content=hit.content,
        distance=hit.distance,
        similarity=hit.similarity,
    )


def _document_out(
    document: KnowledgeDocument, *, chunks: int, provider: str, semantic: bool
) -> KnowledgeDocumentOut:
    """ORM 行 → 响应。只取需要的列，不把 `body` 原样再返一遍（分块里已经有了）。"""
    return KnowledgeDocumentOut(
        document_id=document.document_id,
        title=document.title,
        category=document.category,
        version=document.version,
        authority_level=document.authority_level,
        status=document.status,
        effective_from=document.effective_from,
        effective_until=document.effective_until,
        chunks=chunks,
        embedding_provider=provider,
        semantic_embedding=semantic,
    )


def _knowledge_error(exc: BaseException) -> JSONResponse:
    """服务层领域异常 → 契约 §7 错误体（码用小写或大写，与既有模块一致）。"""
    if isinstance(exc, KnowledgeNotFoundError):
        return _error_response(404, "NOT_FOUND", str(exc))
    if isinstance(exc, KnowledgeConflictError):
        return _error_response(409, "CONFLICT", str(exc))
    if isinstance(exc, KnowledgeValidationError):
        return _error_response(422, "VALIDATION_ERROR", str(exc))
    if isinstance(exc, KnowledgeEmbeddingError):
        return _error_response(503, "SERVICE_NOT_READY", str(exc))
    raise exc


# ------------------------------------------------------------------ 端点


@router.get("/search", response_model=KnowledgeSearchOut)
async def search(
    query: Annotated[str, Query(description="自然语言问题；空串返回 empty_query，不报错")],
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    category: Annotated[
        str | None,
        Query(description=f"六类之一：{'/'.join(KNOWLEDGE_CATEGORIES)}；留空不限类别"),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_SEARCH_LIMIT)
    ] = knowledge_service.DEFAULT_SEARCH_LIMIT,
) -> KnowledgeSearchOut | JSONResponse:
    """按语义检索机构知识（`admin` / `doctor` / `care_worker`）。

    三条边界，改之前先读：

    1. **没有 `elder_id`**：知识不分人（见模块 docstring）；传了也会被 FastAPI 忽略
       （本端点没声明这个参数），不会退化成"按人查知识"。
    2. **`reason="degraded"` 不是空库**：embedding 不可用时返回 `hits=[]` 且
       `degraded=true`，前端/工具必须区分「查不到」与「这次查不了」。
    3. **越权先写审计再 403**：家属与设备不属于本端点主体范围（`SCOPE_FORBIDDEN`），
       员工角色不符是 `ROLE_FORBIDDEN`；两者都留痕（AGENTS §8）。
    """
    await _require_roles(
        identity, READ_ROLES, action="knowledge.search", resource_id=NIL_RESOURCE_ID
    )
    try:
        result = await knowledge_service.search_knowledge_result(
            session, query=query, category=category, limit=limit
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _knowledge_error(exc)

    logger.info(
        "knowledge_searched",
        extra={
            "trace_id": get_trace_id() or "-",
            "actor_id": identity.staff_id,
            "role": identity.role,
            "category": category or "*",
            "limit": limit,
            "hits": len(result.hits),
            "reason": result.reason,
            "degraded": result.degraded,
        },
    )
    return KnowledgeSearchOut(
        hits=[_hit_out(hit) for hit in result.hits],
        reason=result.reason,
        degraded=result.degraded,
        limit=limit,
    )


@router.post("/documents", response_model=KnowledgeDocumentOut, status_code=201)
async def create_document(
    payload: KnowledgeIngestIn,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeDocumentOut | JSONResponse:
    """录入一篇机构知识（**仅 admin**）：文档 + 切块 + 向量 + 审计，一个事务。

    - 重复的 `(title, version)` 返回 **409** 而不是静默覆盖：版本是追溯旧口径的唯一
      依据（FR-KNW-02），覆盖等于让历史凭空消失；
    - `category` 非法（例如把「作息」当中文直接传进来）由服务层白名单拒绝 → 422；
    - 向量不可用 → **503** 且**不落行**：`knowledge_chunks.embedding` 非空是表级约束，
      没有向量就不该入库（N11 模型 docstring 第 4 条）。
    """
    await _require_roles(
        identity, WRITE_ROLES, action="knowledge.write", resource_id=NIL_RESOURCE_ID
    )
    actor_id = _actor_id(identity)
    # 与写入用同一个 provider 实例：再构造一次既浪费连接池，也可能让「写进去的向量」
    # 和「响应里报告的 provider」来自两个不同实现。
    provider = build_embedding_provider()
    try:
        document = await knowledge_service.ingest_document(
            session,
            title=payload.title,
            body=payload.body,
            category=payload.category,
            actor_id=actor_id,
            authority_level=payload.authority_level,
            version=payload.version,
            effective_from=payload.effective_from,
            effective_until=payload.effective_until,
            embedding_provider=provider,
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _knowledge_error(exc)

    chunks = len(knowledge_service.split_into_chunks(payload.body))
    provider_name = type(provider).__name__
    # 降级实现的名字里就带 Deterministic；不按 `available` 判断——它恒为 True（本地纯计算）。
    semantic = not isinstance(provider, DeterministicEmbeddingProvider)
    logger.info(
        "knowledge_document_created",
        extra={
            "trace_id": get_trace_id() or "-",
            "document_id": str(document.document_id),
            "title": document.title,
            "category": document.category,
            "version": document.version,
            "chunks": chunks,
            "actor_id": str(actor_id),
            "embedding_provider": provider_name,
            "semantic_embedding": semantic,
        },
    )
    return _document_out(document, chunks=chunks, provider=provider_name, semantic=semantic)


@router.post(
    "/documents/{document_id}/supersede", response_model=KnowledgeDocumentOut, status_code=201
)
async def supersede(
    document_id: uuid.UUID,
    payload: KnowledgeSupersedeIn,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeDocumentOut | JSONResponse:
    """按版本换代（**仅 admin**）：先建新版、再把旧版置 `retired`，两条审计同事务。

    与 `PUT` 覆盖的区别是**旧版仍可追溯**：旧行的正文与向量都还在，只是
    `status='retired'` 且 `effective_until` 收到新版起点，检索立刻不再命中它
    （服务层 `search` 只取 `status='active'`）。
    """
    await _require_roles(
        identity, WRITE_ROLES, action="knowledge.supersede", resource_id=document_id
    )
    actor_id = _actor_id(identity)
    provider = build_embedding_provider()
    try:
        document = await knowledge_service.supersede_document(
            session,
            document_id=document_id,
            body=payload.body,
            actor_id=actor_id,
            version=payload.version,
            effective_from=payload.effective_from,
            effective_until=payload.effective_until,
            embedding_provider=provider,
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _knowledge_error(exc)

    chunks = len(knowledge_service.split_into_chunks(payload.body))
    provider_name = type(provider).__name__
    semantic = not isinstance(provider, DeterministicEmbeddingProvider)
    logger.info(
        "knowledge_document_superseded",
        extra={
            "trace_id": get_trace_id() or "-",
            "previous_document_id": str(document_id),
            "document_id": str(document.document_id),
            "version": document.version,
            "chunks": chunks,
            "actor_id": str(actor_id),
        },
    )
    return _document_out(document, chunks=chunks, provider=provider_name, semantic=semantic)


@router.get("/documents", response_model=KnowledgeDocumentPageOut)
async def list_documents(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[
        str | None,
        Query(description="draft/active/retired；留空不限状态"),
    ] = None,
    category: Annotated[
        str | None,
        Query(description=f"六类之一：{'/'.join(KNOWLEDGE_CATEGORIES)}；留空不限类别"),
    ] = None,
    q: Annotated[str | None, Query(description="标题子串过滤（不查正文）")] = None,
    limit: Annotated[
        int, Query(ge=1, le=knowledge_service.MAX_LIST_LIMIT)
    ] = knowledge_service.DEFAULT_LIST_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KnowledgeDocumentPageOut | JSONResponse:
    """管理端列表（**仅 admin**）：读**全量**，含 `draft` / `retired`。

    为什么 admin 要读得到非 active 行：检索侧只取 `status='active'`，管理页要观察
    「新版生效后旧版不可检索」这件事，就必须看得见退役行；因此 `status` 在这里是
    **过滤参数**，不是权限闸门（权限在 `WRITE_ROLES`）。

    权限给 admin 而不是 `READ_ROLES`：列表会暴露 `draft`（还没发布的内部口径），
    医生/护工能搜到已生效知识就够，越权读取同样先落审计再 403。
    """
    await _require_roles(
        identity, WRITE_ROLES, action="knowledge.list", resource_id=NIL_RESOURCE_ID
    )
    try:
        items, total = await knowledge_service.list_documents(
            session, status=status, category=category, query=q, limit=limit, offset=offset
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _knowledge_error(exc)

    logger.info(
        "knowledge_documents_listed",
        extra={
            "trace_id": get_trace_id() or "-",
            "actor_id": identity.staff_id,
            "status": status or "*",
            "category": category or "*",
            "has_query": q is not None,
            "limit": limit,
            "offset": offset,
            "returned": len(items),
            "total": total,
        },
    )
    return KnowledgeDocumentPageOut(
        items=[_summary_out(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/documents/{document_id}/retire", response_model=KnowledgeRetireOut)
async def retire(
    document_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeRetireOut | JSONResponse:
    """手动停用一版（**仅 admin**）：置 `retired` + 封口 `effective_until` + `.retire` 审计。

    ## 与 `supersede` 的语义边界（改之前先读）

    - `supersede`：**有替代版**的换代 —— 新版同时建出来，旧版封口时刻 = 新版起点；
    - `retire`：**这一版作废、没有替代版** —— 例如规定被撤销，或这一条录错了。

    两者都落到 `status='retired'`，所以停用后 `search` 立刻不再命中它（检索只取 active），
    不需要再加一个「已停用」判断，也不需要改表。

    ## 幂等

    重复 retire 返回 `already_retired=true` 且不再写审计、不再推进 `effective_until`；
    管理页连点两下不会变成 500，也不会把历史封口时刻改成「现在」。
    """
    await _require_roles(identity, WRITE_ROLES, action="knowledge.retire", resource_id=document_id)
    actor_id = _actor_id(identity)
    try:
        document, already_retired = await knowledge_service.retire_document(
            session, document_id=document_id, actor_id=actor_id
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _knowledge_error(exc)

    logger.info(
        "knowledge_document_retired",
        extra={
            "trace_id": get_trace_id() or "-",
            "document_id": str(document.document_id),
            "version": document.version,
            "already_retired": already_retired,
            "actor_id": str(actor_id),
        },
    )
    return KnowledgeRetireOut(
        document_id=document.document_id,
        title=document.title,
        version=document.version,
        status=document.status,
        effective_until=document.effective_until,
        already_retired=already_retired,
    )


__all__ = [
    "MAX_SEARCH_LIMIT",
    "READ_ROLES",
    "WRITE_ROLES",
    "router",
]
