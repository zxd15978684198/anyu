"""长期记忆查询与删除 REST（admin 端，PRD §4.9 FR-MEM-02 / FR-ADM-09）。

## 为什么管理端只有 admin

FR-MEM-02 的原话是「长者本人可查看/纠正/删除，管理员可协助」——**护工与医生不在列表里**。
记忆里有情绪、家庭关系、健康困扰之类的私密叙述，属于「最小必要」之外的信息：护工做
日常照护不需要读它，医生做诊疗有 HIS 与健康档案。因此**管理端**三个端点都仅 `admin`
（长者本人端另有 `/me/*` 三个端点，见下一节）：

| 端点 | 允许 | 其它员工角色 | 家属 / 设备 |
| --- | --- | --- | --- |
| `GET /api/memory/chunks` | admin | 403 `ROLE_FORBIDDEN` + 审计 | 403 `SCOPE_FORBIDDEN` + 审计 |
| `POST /api/memory/chunks` | admin | 403 `ROLE_FORBIDDEN` + 审计 | 403 `SCOPE_FORBIDDEN` + 审计 |
| `DELETE /api/memory/elder/{elder_id}` | admin | 403 `ROLE_FORBIDDEN` + 审计 | 403 `SCOPE_FORBIDDEN` + 审计 |

**`POST` 是管理端写入 + 验收用，不是通用写记忆接口。** PRD 没有明文定义「记忆写入
API」——FR-MEM-01 只规定「写入记忆」这个能力，FR-MEM-02 只规定长者本人查看/纠正/删除、
管理员协助。所以它是**实现补充**（已登记 TD 偏差段），用来让 `inferred` 来源系数在生产
路径上真正可达（讲义 §8.3）；长者端 / Agent 的写入链（对话结束 → `remember`）另排期，
见 `app/services/memory_service.py` 的 `remember`。别把它当成第二套记忆写入形态（AGENTS
§4 第 8 条）：两者最终都只走 `memory_service.remember` 这一个写入口。

`GET` 读的是**记忆正文**，比列表统计敏感得多，所以越权同样留痕而不是只返 403
（AGENTS §8：越权 → 拒绝 + 审计 + trace_id）。

## 长者本人端 `/me/*`（FR-MEM-02 前半句）：与 admin 三端点**并存**，不替换

「长者本人可查看/纠正/删除」此前零端点（契约 `docs/接口契约_记忆.md` §10-G1），本轮补齐：

| 端点 | 允许 | 员工（含 admin） | 家属 | 未绑定长者的设备 |
| --- | --- | --- | --- | --- |
| `GET /api/memory/me/chunks` | `kind=device` | 403 `SCOPE_FORBIDDEN` + 审计 | 403 `SCOPE_FORBIDDEN` + 审计 | 403 `SCOPE_FORBIDDEN` |
| `PATCH /api/memory/me/chunks/{chunk_id}` | 同上 | 同上 | 同上 | 同上 |
| `DELETE /api/memory/me/chunks/{chunk_id}` | 同上 | 同上 | 同上 | 同上 |

三条硬口径：

1. **`elder_id` 只从 `identity.elder_id` 取**（设备令牌绑定的长者，服务端从 `devices`
   表读出来的绑定关系）。三个端点**没有** `elder_id` 查询参数，请求体里塞的同名值也没有
   字段可落（AGENTS §4 第 9 条：「标识可以来自请求，权限不能来自请求」——本人端连标识都
   不来自请求）。
2. **跨长者一律 404，不是 403**：403 会泄露「这条记录存在但不属于你」，是可探测面；
   服务层 `load_chunk` 把 `(elder_id, chunk_id)` 放进同一个 `WHERE`，取不到就是 404。
3. **越权与跨人都不静默**：前者先写 `audit_logs` 再 403（理由同管理端），后者的
   `memory.chunk.delete` / `memory.chunk.correct` 审计由服务层在**同一事务**里写。

## `elder_id` 只从路径/查询参数来（AGENTS §4 第 9 条）

红线第 9 条是「`elder_id` / `thread_id` 只能从服务端注入，客户端与模型传入的值一律丢弃」。
落到本模块有两条具体做法，写在这里以便回查：

1. **不接受请求体里的 `elder_id`**：`GET` 只认查询参数 `elder_id`，`DELETE` 只认路径
   参数 `{elder_id}`，两个端点都**没有**请求体（`DELETE` 连 body 都不读）。任何"顺带传
   一个 elder_id 进 body"的调用方会被当成没有这个参数；将来若有人加 body，先读这一段。
2. **参数只是"声称"，还要过鉴权范围**：`elder_id` 拿到后必过
   `elder_service.get_elder()`——admin 全量、非 admin 直接 403、不存在的长者 404。
   删除这种不可逆动作尤其不能只看"调用方说他是谁"。

AGENTS §4 第 9 条的完整场景是「模型自报身份」，本模块没有模型参与，但同一原则适用于任何
客户端可控的标识：**标识可以来自请求，权限不能来自请求**。

## 分层（AGENTS §2）

本模块只做参数校验、角色过滤、调 `app/services/memory_service.py`、序列化。**不出现**
`session.add/commit`，**不出现** `select(MemoryChunk)`。删除与审计的原子性由服务层保证
（`delete_by_elder` 在同一事务里写 `audit_logs`，见其 docstring）。

## 日志

记忆正文**不打印**（AGENTS §8 + 红线 10）：本模块只记 `elder_id` / 条数 / 理由，
不记 `content` 任何片段，也不把它塞进异常消息。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import elder_service, memory_service
from app.services.elder_service import ElderScopeForbiddenError
from app.services.embedding import (
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
    build_embedding_provider,
)
from app.services.report_service import _write_audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    route_class=ContractAPIRoute,
)

#: FR-MEM-02：查看/删除记忆只给管理员（护工、医生不在列表里，见模块 docstring）。
ADMIN_ROLES: frozenset[str] = frozenset({"admin"})

#: 默认退房理由；`DELETE` 的 `reason` 只从查询参数取，绝不从 body 取。
DEFAULT_DELETE_REASON = "checkout"

#: 单页上限：记忆单长者最多 500 条（`memory_service.CAPACITY_PER_ELDER`），
#: 一次全拉出来会把 admin 页面拖死，也不方便分页。
MAX_PAGE_SIZE = 200


# ------------------------------------------------------------------ 响应模型


class MemoryChunkOut(BaseModel):
    """一条记忆切片的公开投影。

    `content` 是脱敏后的正文（TD §3.4）；`embedding` **不返回**——1536 个浮点数对前端
    没有意义，泄露向量也没有好处。
    """

    chunk_id: uuid.UUID
    elder_id: uuid.UUID
    content: str
    category: str = "private"
    importance: float = 1.0
    status: str = "active"
    created_at: datetime.datetime | None = None
    last_accessed: datetime.datetime | None = None


class MemoryChunkListOut(BaseModel):
    items: list[MemoryChunkOut]
    total: int
    elder_id: uuid.UUID
    limit: int
    offset: int
    status: str | None = None


class MemoryDeleteOut(BaseModel):
    """退房清数据的响应：条数 + 审计留痕标记，可被前端直接展示。"""

    elder_id: uuid.UUID
    deleted_chunks: int
    reason: str
    #: 审计与服务层删除同事务写入（`memory_service.delete_by_elder`），故恒为 True。
    audit_logged: bool = True
    #: 软删（`status='deleted'`）而不是物理 DELETE：向量物理残留由定期清理任务处理。
    soft_delete: bool = True
    trace_id: str


class MemoryMergeOut(BaseModel):
    """记忆归并的响应（FR-MEM-07）：本轮并掉几条 + 每条的痕迹在哪张表里。

    `merged` 是**被并掉的条数**（不是组数、不是保留数）：一条 active、N 条 superseded
    时 `merged=N`。`elders` / `failed` 只在全量跑时非零有意义——单长者入口恒为 1 / 0。

    `audit_logged` = `merged > 0`：空跑不写审计（见 `memory_service.merge_duplicates`），
    所以这里的 False 不是失败，是「这一轮没什么可并」。
    """

    elder_id: uuid.UUID | None = None
    merged: int
    elders: int
    failed: int
    threshold: float
    audit_logged: bool
    trace_id: str


#: 记忆来源：`explicit` = 长者/家属**明确陈述**，`inferred` = 系统**从行为推断**。
#: 必须与 `memory_service.MEMORY_SOURCE_COEFFICIENTS` 的键一致：两处分叉时
#: `remember()` 会抛 `ValueError`，而请求层应当用 422 更早拒掉（本模块的做法）。
MemorySource = Literal["explicit", "inferred"]


class MemoryWriteIn(BaseModel):
    """管理端写入一条记忆的请求体（本版唯一写入口，见模块 docstring）。

    **没有 `elder_id` 字段**：目标长者只从查询参数取，body 里塞了什么都不会被采信
    （AGENTS §4 第 9 条，见模块 docstring 第 1 条）。

    `source` 默认 `explicit`：老客户端不带这个字段时系数是 1.0，落库的 `importance`
    与加字段前完全一致；只有显式传 `inferred` 才按 0.6 打折。默认值选 1.0 而不是 0.6，
    是因为「没声明来源」不能反推成「推断」——把既有调用方的权重悄悄改小等于改数据。
    """

    content: str = Field(
        min_length=1,
        max_length=memory_service.MAX_CONTENT_LENGTH,
        description="记忆正文，≤200 字；超长拒收，不静默截断",
    )
    category: Literal["private", "family"] = Field(
        default="private", description="private 仅本人可见；family 为 TD 预留取值"
    )
    importance: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="基础重要性；落库前按 source 折算并夹回 [0,1]",
    )
    source: MemorySource = Field(
        default="explicit",
        description="explicit=明确陈述（×1.0）；inferred=行为推断（×0.6，讲义 §8.3）",
    )


class MemoryChunkWriteOut(MemoryChunkOut):
    """写入响应：在切片投影上补两个**可观测标记**。

    为什么需要多这两个字段：未配 embedding key 时 `build_embedding_provider()` 会降级成
    `DeterministicEmbeddingProvider`（SHA-256 派生的噪声向量，见 `app/services/embedding.py`），
    写库照常成功、HTTP 也是 2xx，肉眼完全看不出「语义检索其实没在工作」。把 provider 类名
    和 `semantic_embedding` 直接写进响应与日志，复盘时才能一眼分辨这条记忆的向量能否用于
    召回质量评估（红线：不得用它产出召回准确率结论）。
    """

    embedding_provider: str = Field(description="本次实际使用的 provider 类名")
    semantic_embedding: bool = Field(
        description="是否产出真实语义向量；False 表示仅联调兜底（SHA-256 噪声）"
    )


class MemoryCorrectIn(BaseModel):
    """长者本人纠正一条记忆的请求体（`PATCH /api/memory/me/chunks/{chunk_id}`）。

    **没有 `elder_id` 字段**，且 `extra="ignore"`：目标长者只来自设备令牌的
    `identity.elder_id`，body 里塞同名值连"字段校验失败"都不会触发——直接不采信
    （AGENTS §4 第 9 条）。

    只允许改 `content`：`importance` / `category` / `source` 都是写入期的判定结果，
    让本人端改它们等于让被记录者调整自己的权重，会破坏衰减与容量淘汰的可比性。
    """

    model_config = ConfigDict(extra="ignore")

    content: str = Field(
        min_length=1,
        max_length=memory_service.MAX_CONTENT_LENGTH,
        description="纠正后的正文，≤200 字；超长在入口 422，不静默截断",
    )


class MemoryChunkDeleteOut(BaseModel):
    """长者本人删除一条记忆的响应。

    `deleted=False` 表示这条此前已经是 `deleted`（重复点击），**不是失败**：
    重复操作照样写了一条审计（`detail.already_deleted=True`），返回 200 让前端无需特判。
    """

    chunk_id: uuid.UUID
    deleted: bool
    status: str = "deleted"
    #: 软删：行还在（`status='deleted'`），向量物理残留由清理任务处理。
    soft_delete: bool = True
    #: 审计与服务层删除**同事务**写入（`memory_service.delete_chunk`），故恒为 True。
    audit_logged: bool = True


# ------------------------------------------------------------------ embedding provider


#: 进程内单例。`build_embedding_provider()` 在配了 key 时返回带 `httpx.AsyncClient`
#: 的 HTTP 实现——每个请求新建一个就会漏连接池；降级实现无状态，复用零成本。
#: 单测通过替换 `memory_api._EMBEDDING_PROVIDER` 控制它（见 tests/test_memory_api_source.py）。
_EMBEDDING_PROVIDER: EmbeddingProvider | None = None


def _embedding_provider() -> EmbeddingProvider:
    """取（必要时构造）本进程的 embedding provider。

    构造失败不在这里兜底：`build_embedding_provider()` 本身只会返回两个实现之一，
    真出意外应当 500 暴露出来，而不是让写入悄悄换一个 provider（那会改变向量空间）。
    """
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is None:
        _EMBEDDING_PROVIDER = build_embedding_provider()
    return _EMBEDDING_PROVIDER


# ------------------------------------------------------------------ 视图 → 响应


def _fields(view: object) -> Mapping[str, Any]:
    """把服务层返回统一成映射：dataclass / ORM 行 / dict 三种都认（视图未冻结）。"""
    if isinstance(view, Mapping):
        return view
    if dataclasses.is_dataclass(view) and not isinstance(view, type):
        return dataclasses.asdict(view)
    return {key: value for key, value in vars(view).items() if not key.startswith("_")}


def _chunk_out(view: object) -> MemoryChunkOut:
    data = _fields(view)
    chunk_id = data.get("chunk_id") or data.get("id")
    if chunk_id is None:
        raise MemoryViewError("memory chunk view has no chunk_id")
    return MemoryChunkOut(
        chunk_id=chunk_id,
        elder_id=data["elder_id"],
        content=data.get("content") or "",
        category=data.get("category") or "private",
        importance=float(data.get("importance") or 0.0),
        status=data.get("status") or "active",
        created_at=data.get("created_at"),
        last_accessed=data.get("last_accessed"),
    )


class MemoryViewError(RuntimeError):
    """服务层视图形状不对（缺主键等）：属接线错误，不是客户端参数问题。"""


class MemoryServiceGapError(RuntimeError):
    """服务层缺少本项目需要的读函数（`list_chunks`）。

    这不是客户端错误，也不是"没有数据"：把它显式化成 503 而不是在 API 里补一段
    `select(MemoryChunk)`（分层硬约束），否则这条 SQL 会绕过 `elder_id` 强制过滤的
    迭代 9 静态扫描。补法见随卡报告。
    """


# ------------------------------------------------------------------ 权限与审计


async def _audit_denied(
    identity: Identity,
    *,
    action: str,
    resource_id: uuid.UUID,
    reason: str = "non_admin",
) -> None:
    """越权留痕：写 `audit_logs`（服务层独立会话），随后的 403 不会把它回滚掉。"""
    raw = identity.staff_id or identity.guardian_id or identity.device_id
    await _write_audit(
        actor_type=identity.kind,
        actor_id=uuid.UUID(raw) if raw else uuid.UUID(int=0),
        action=action,
        resource_type="memory_chunks",
        resource_id=resource_id,
        detail={
            "trace_id": get_trace_id() or "-",
            "reason": reason,
            "role": identity.role,
            "kind": identity.kind,
        },
    )


async def _require_admin(identity: Identity, *, action: str, resource_id: uuid.UUID) -> None:
    """先判主体种类、再判角色；两种拒绝都**先写审计**再抛 403。"""
    if identity.kind != "staff":
        await _audit_denied(identity, action=action, resource_id=resource_id)
        raise ElderScopeForbiddenError("memory console is not reachable by family/device")
    if (identity.role or "") not in ADMIN_ROLES:
        await _audit_denied(identity, action=action, resource_id=resource_id)
        raise RoleForbiddenError("memory endpoints are admin-only (FR-MEM-02)")


async def _require_elder_device(
    identity: Identity, *, action: str, resource_id: uuid.UUID
) -> uuid.UUID:
    """本人端闸门：只认设备主体，且设备必须绑定了长者；返回该长者 ID。

    为什么不用 `current_device` 依赖：它走 `_require_kind`，主体种类不符时给 **401**
    （「拿错了凭证体系」）。本人端对员工/家属应当是 **403**——这些凭证本身有效，
    只是不在这个端点的主体范围内（契约 §7 的 403 `SCOPE_FORBIDDEN`），401 会误导前端
    去重新登录。两种拒绝都先写审计再抛（理由同管理端）。

    未绑定长者的设备（`elder_id is None`）也是 403：它没有「自己」可以看/改/删，
    不能退回成「按查询参数指定一个长者」——那正是 AGENTS §4 第 9 条禁止的口子。
    """
    if identity.kind != "device":
        await _audit_denied(
            identity, action=action, resource_id=resource_id, reason="not_elder_device"
        )
        raise ElderScopeForbiddenError(
            "memory self-service is only reachable by the elder's own device"
        )
    if identity.elder_id is None:
        await _audit_denied(
            identity, action=action, resource_id=resource_id, reason="device_unbound"
        )
        raise ElderScopeForbiddenError("elder device is not bound to an elder")
    # `Identity.__post_init__` 已把 elder_id 规范成小写连字 UUID 文本，这次转换不可能失败；
    # 真失败说明身份被手改坏了，让它冒成 500 比悄悄按「没绑定」降级更容易被发现。
    return uuid.UUID(identity.elder_id)


# ------------------------------------------------------------------ 端点


@router.get("/chunks", response_model=MemoryChunkListOut)
async def list_chunks(
    elder_id: Annotated[uuid.UUID, Query(description="目标长者；只从查询参数取，不读请求体")],
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[
        Literal["active", "superseded", "deleted"] | None,
        Query(description="按生命周期过滤：active / superseded / deleted；其它值 422"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemoryChunkListOut | JSONResponse:
    """某长者的记忆切片列表（**仅 admin**）。

    返回正文，所以先过角色闸门再过长者范围闸门；`elder_id` 只来自查询参数，
    任何请求体里的同名值都不会被读取（AGENTS §4 第 9 条，见模块 docstring 第 1 条）。
    """
    await _require_admin(identity, action="memory.read", resource_id=elder_id)
    await elder_service.get_elder(session, identity, elder_id)

    try:
        views = await _svc_list_chunks(session, elder_id, status, limit, offset)
    except MemoryServiceGapError as exc:
        logger.error(
            "memory_service_gap",
            extra={"elder_id": str(elder_id), "missing": "list_chunks"},
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": str(exc),
                "code": "SERVICE_NOT_READY",
                "trace_id": get_trace_id() or "-",
            },
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    items = [_chunk_out(view) for view in views]
    return MemoryChunkListOut(
        items=items,
        total=len(items),
        elder_id=elder_id,
        limit=limit,
        offset=offset,
        status=status,
    )


@router.post("/chunks", response_model=MemoryChunkWriteOut, status_code=201)
async def create_chunk(
    payload: MemoryWriteIn,
    elder_id: Annotated[uuid.UUID, Query(description="目标长者；只从查询参数取，不读请求体")],
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MemoryChunkWriteOut | JSONResponse:
    """管理端补录一条记忆（**仅 admin**；本版唯一写入端点）。

    ## 这个端点的性质：管理端写入 + 验收用，不是通用写记忆接口

    - PRD **没有明文定义**「记忆写入 API」：FR-MEM-01 只规定「写入记忆」这个能力，
      FR-MEM-02 只规定长者本人查看/纠正/删除、管理员协助。本端点是**实现补充**
      （已登记 TD 偏差段），目的是让讲义 §8.3 的来源系数在生产路径上可达
      （`inferred` 行为推断 ×0.6 < `explicit` 明确陈述 ×1.0），而不是给长者端或
      Agent 用的写入口。
    - 长者端 / Agent 的自动写入（对话轮结束 → `remember`）另行接线；两条路径最终
      都只走 `memory_service.remember()`，本端点**不自己** `session.add(MemoryChunk)`，
      否则去重 / 容量 / 衰减会出现两套口径（AGENTS §4 第 8 条：一个能力一种形态）。

    ## 权限与标识

    - `admin` 独占，理由同 `GET /chunks`（记忆含私密叙述，护工/医生不在 FR-MEM-02 列表里）；
      越权同样**先写 `audit_logs(action='memory.write')` 再抛 403**，不只返码。
    - `elder_id` 只从查询参数取，请求体模型里没有这个字段，塞了也不采信；拿到后仍要过
      `elder_service.get_elder()` 的范围闸门（admin 全量 / 不存在 404，AGENTS §4 第 9 条）。

    ## 来源系数与校验

    `source` 原样透传给 `remember()`：`explicit` ×1.0、`inferred` ×0.6，折算后夹回 [0,1]。
    非法取值（如 `source="guess"`）在 Pydantic 校验层就 422，**handler 不执行、库不落行**；
    正文只有空白等 `remember()` 自己抛的 `ValueError` 在这里也映射成 422，而不是 500。

    ## 事务与可观测

    `remember()` 只 `flush` 不 `commit`，事务由 `get_session` 收口。provider 降级时
    `embedding=None` 也照写（宁可写入也不静默丢数据），响应里的 `semantic_embedding=False`
    与日志 `embedding_degraded` 是这件事的唯一标记——不要把它们当语义检索可用的证据。
    """
    await _require_admin(identity, action="memory.write", resource_id=elder_id)
    await elder_service.get_elder(session, identity, elder_id)

    provider = _embedding_provider()
    try:
        row = await memory_service.remember(
            session,
            elder_id=elder_id,
            content=payload.content,
            embedding_provider=provider,
            category=payload.category,
            importance=payload.importance,
            source=payload.source,
        )
    except ValueError as exc:
        # 客户端参数问题（正文只有空白、类别/来源非法……），不是服务故障。
        # 此时 `remember` 还没 `session.add`，事务里不会留下半条记忆。
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "code": "VALIDATION_ERROR",
                "trace_id": get_trace_id() or "-",
            },
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    provider_name = type(provider).__name__
    # 降级实现的名字里就带 Deterministic；不按 `available` 判断——它恒为 True（本地纯计算），
    # 用它判断会把「能算但没语义」误读成「语义可用」。
    semantic = not isinstance(provider, DeterministicEmbeddingProvider)
    importance = float(row.importance)
    logger.info(
        "memory_written",
        extra={
            "trace_id": get_trace_id() or "-",
            "elder_id": str(elder_id),
            "chunk_id": str(row.chunk_id),
            "category": row.category,
            "source": payload.source,
            "importance": importance,
            "embedding_provider": provider_name,
            "semantic_embedding": semantic,
            "embedding_degraded": not semantic,
            "has_embedding": row.embedding is not None,
            "actor_id": identity.staff_id,
        },
    )
    return MemoryChunkWriteOut(
        **_chunk_out(row).model_dump(),
        embedding_provider=provider_name,
        semantic_embedding=semantic,
    )


@router.delete("/elder/{elder_id}", response_model=MemoryDeleteOut)
async def delete_elder_memory(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    reason: Annotated[str, Query(max_length=100)] = DEFAULT_DELETE_REASON,
) -> MemoryDeleteOut | JSONResponse:
    """退房清数据：软删该长者全部 active 记忆（**仅 admin**，FR-ADM-09）。

    - `elder_id` 只来自**路径**参数，`reason` 只来自**查询**参数，本端点没有请求体；
    - 删除与 `audit_logs` 在服务层同一事务里完成（不留"删了没记录"），本端点只负责
      admin 闸门、范围闸门与把条数回给前端；
    - 日志只记条数与理由，**不记正文**（AGENTS §8 / 红线 10）；
    - 软删后 `memory_service.recall` 的 `status='active'` 过滤立刻生效，
      下一轮对话不再注入这些记忆（FR-MEM-05）。
    """
    await _require_admin(identity, action="memory.delete", resource_id=elder_id)
    await elder_service.get_elder(session, identity, elder_id)

    actor_id = uuid.UUID(identity.staff_id) if identity.staff_id else None
    try:
        deleted = await memory_service.delete_by_elder(
            session,
            elder_id=elder_id,
            actor_id=actor_id,
            reason=reason,
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    trace_id = get_trace_id() or "-"
    logger.info(
        "memory_elder_cleared",
        extra={
            "trace_id": trace_id,
            "elder_id": str(elder_id),
            "deleted_chunks": deleted,
            "reason": reason,
            "actor_id": str(actor_id) if actor_id else None,
        },
    )
    return MemoryDeleteOut(
        elder_id=elder_id,
        deleted_chunks=deleted,
        reason=reason,
        trace_id=trace_id,
    )


# ------------------------------------------------------------------ 周归并（FR-MEM-07）


@router.post("/merge", response_model=MemoryMergeOut)
async def merge_memories(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    elder_id: Annotated[
        uuid.UUID | None,
        Query(description="只归并这一位长者；不传则遍历全部在住长者（手动演示入口）"),
    ] = None,
    threshold: Annotated[
        float,
        Query(
            gt=0.0,
            le=1.0,
            description="同类别内余弦相似度高于此值视为重复；默认 0.90（未经线上数据校准）",
        ),
    ] = memory_service.MERGE_SIMILARITY,
) -> MemoryMergeOut | JSONResponse:
    """手动触发记忆归并（**仅 admin**，FR-MEM-07）。

    PRD §5.3 把「记忆衰减归并自动化」降级为手动触发演示，因此除了
    `app/scheduler/memory.py` 的每周一 03:30 定时任务，这里必须有一条可现场复现的路：

    - `elder_id` 只来自**查询参数**，任何请求体里的同名值都不采信（AGENTS §4 第 9 条）；
    - 不传 `elder_id` = 全量跑一遍：逐长者处理、单长者失败 continue 并计数，
      循环与事务边界在 `memory_service.merge_all_active_elders`（与定时任务同一份实现）；
    - 传了就多过一道长者范围闸门（`elder_service.get_elder`），越权在**归并之前**就被挡住；
    - `audit_logs` 由服务层在**同一事务**里写，本端点不另写审计；空跑不写审计；
    - 日志只记条数与相似度，**不记正文**（AGENTS §4 第 11 条）。
    """
    subject = elder_id if elder_id is not None else uuid.UUID(int=0)
    await _require_admin(identity, action="memory.merge", resource_id=subject)
    trace_id = get_trace_id() or "-"

    try:
        if elder_id is None:
            report = await memory_service.merge_all_active_elders(session, threshold=threshold)
        else:
            await elder_service.get_elder(session, identity, elder_id)
            merged = await memory_service.merge_duplicates(
                session, elder_id=elder_id, threshold=threshold
            )
            report = memory_service.MergeReport(
                elders=1, failed=0, merged=merged, threshold=threshold
            )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    logger.info(
        "memory_merged_by_admin",
        extra={
            "trace_id": trace_id,
            "elder_id": str(elder_id) if elder_id is not None else "*",
            "merged": report.merged,
            "elders": report.elders,
            "failed": report.failed,
            "threshold": report.threshold,
            "actor_id": identity.staff_id,
        },
    )
    return MemoryMergeOut(
        elder_id=elder_id,
        merged=report.merged,
        elders=report.elders,
        failed=report.failed,
        threshold=report.threshold,
        audit_logged=report.merged > 0,
        trace_id=trace_id,
    )


# ------------------------------------------------------------------ 长者本人端（FR-MEM-02）


@router.get("/me/chunks", response_model=MemoryChunkListOut)
async def list_my_chunks(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[
        Literal["active", "superseded", "deleted"] | None,
        Query(description="默认只看 active；显式传 deleted 才能看到已删除条目"),
    ] = "active",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemoryChunkListOut | JSONResponse:
    """长者本人查看自己的记忆切片（**只认设备令牌**，FR-MEM-02）。

    - `elder_id` **没有**查询参数：只取设备令牌绑定的 `identity.elder_id`（AGENTS §4 第 9 条）；
    - 默认只列 `active`：`superseded` 是被更新版本取代的旧条、`deleted` 是已撤回的，
      混在默认列表里会让长者以为「这些东西还在」；要看历史必须显式传 `status`；
    - `total` 口径与 M1 一致：**当前页条数**，不是总数（契约 §3.3）。
    """
    elder_id = await _require_elder_device(
        identity, action="memory.read", resource_id=uuid.UUID(int=0)
    )
    try:
        views = await _svc_list_chunks(session, elder_id, status, limit, offset)
    except MemoryServiceGapError as exc:
        logger.error(
            "memory_service_gap",
            extra={"elder_id": str(elder_id), "missing": "list_chunks"},
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": str(exc),
                "code": "SERVICE_NOT_READY",
                "trace_id": get_trace_id() or "-",
            },
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    items = [_chunk_out(view) for view in views]
    logger.info(
        "memory_self_listed",
        extra={
            "trace_id": get_trace_id() or "-",
            "elder_id": str(elder_id),
            "status": status or "*",
            "rows": len(items),
        },
    )
    return MemoryChunkListOut(
        items=items,
        total=len(items),
        elder_id=elder_id,
        limit=limit,
        offset=offset,
        status=status,
    )


@router.patch("/me/chunks/{chunk_id}", response_model=MemoryChunkOut)
async def correct_my_chunk(
    chunk_id: uuid.UUID,
    payload: MemoryCorrectIn,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MemoryChunkOut | JSONResponse:
    """长者本人纠正一条记忆的正文（FR-MEM-02；**只认设备令牌**）。

    三条边界（改之前先读）：

    1. **跨长者 404 不是 403**：`memory_service.correct_chunk` 的 `WHERE` 同时带
       `elder_id` 与 `chunk_id`，取不到即 `MemoryChunkNotFoundError` → 404；403 会泄露
       「这条存在但不属于你」。
    2. **不改 `importance`**：正文错别字与「这条有多重要」是两件事，纠正不该把权重
       重置回写入默认值（服务层 `correct_chunk` 只写 `content` + 重算 `embedding`）。
    3. **正文会重算向量**：provider 可用时按新正文重算；不可用时 `embedding=None`
       （`recall` 跳过它，但正文仍可查可改），不保留与新正文不符的旧向量。
    """
    elder_id = await _require_elder_device(
        identity, action=memory_service.ACTION_CHUNK_CORRECT, resource_id=chunk_id
    )
    provider = _embedding_provider()
    try:
        row = await memory_service.correct_chunk(
            session,
            elder_id=elder_id,
            chunk_id=chunk_id,
            content=payload.content,
            embedding_provider=provider,
            actor_id=uuid.UUID(identity.device_id) if identity.device_id else None,
        )
    except ValueError as exc:
        # 正文只有空白等 `_normalise_content` 抛的参数问题；长度越界已在 Pydantic 入口 422。
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "code": "VALIDATION_ERROR",
                "trace_id": get_trace_id() or "-",
            },
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    return _chunk_out(row)


@router.delete("/me/chunks/{chunk_id}", response_model=MemoryChunkDeleteOut)
async def delete_my_chunk(
    chunk_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MemoryChunkDeleteOut | JSONResponse:
    """长者本人删除自己的一条记忆（软删，FR-MEM-02；**只认设备令牌**）。

    - 软删而非物理删：`status='deleted'`，行还在（审计与回查需要它作为物证）；
    - **跨长者与不存在同为 404**（不区分，避免遍历 UUID 探测）；
    - 重复删除返回 200 + `deleted=false`，**且仍写一条审计**
      （`detail.already_deleted=true`）——重复操作本身是事实，吞掉等于丢痕迹（契约 §7.3）。
    """
    elder_id = await _require_elder_device(
        identity, action=memory_service.ACTION_CHUNK_DELETE, resource_id=chunk_id
    )
    try:
        deleted = await memory_service.delete_chunk(
            session,
            elder_id=elder_id,
            chunk_id=chunk_id,
            actor_id=uuid.UUID(identity.device_id) if identity.device_id else None,
        )
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    return MemoryChunkDeleteOut(chunk_id=chunk_id, deleted=deleted)


# ------------------------------------------------------------------ 错误映射


def _service_error(exc: BaseException) -> JSONResponse:
    """服务层领域异常 → 契约 §8 错误体。"""
    not_found = getattr(memory_service, "MemoryChunkNotFoundError", None)
    if isinstance(not_found, type) and isinstance(exc, not_found):
        return JSONResponse(
            status_code=404,
            content={
                "detail": "memory resource not found",
                "code": "NOT_FOUND",
                "trace_id": get_trace_id() or "-",
            },
        )
    raise exc


# ------------------------------------------------------------------ 服务层适配块
#
# 只在这里出现对 `memory_service` 的**读**调用。`delete_by_elder` 已有且签名稳定，
# 直接在端点里调用；`list_chunks` 尚缺，缺的时候显式化成 503（见 MemoryServiceGapError）。


async def _svc_list_chunks(
    session: AsyncSession,
    elder_id: uuid.UUID,
    status: str | None,
    limit: int,
    offset: int,
) -> list[Any]:
    """期望签名（已报 Lead 让 C 补）：

    `async def list_chunks(session, *, elder_id, status=None, limit=50, offset=0)
    -> Sequence[MemoryChunk]`，实现为
    `WHERE elder_id=:elder_id [AND status=:status] ORDER BY created_at DESC LIMIT :limit OFFSET :offset`。

    缺这个函数时**不**在 API 里补 SQL：那会绕过「所有记忆查询必须带 elder_id」的
    迭代 9 静态闸门（`memory_service` 模块 docstring 第 3 条）。
    """
    list_fn = getattr(memory_service, "list_chunks", None)
    if list_fn is None:
        raise MemoryServiceGapError(
            "memory_service.list_chunks 尚未落地：admin 记忆列表暂不可用（已报 Lead 补）"
        )
    return list(
        await list_fn(
            session,
            elder_id=elder_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    )


__all__ = [
    "ADMIN_ROLES",
    "DEFAULT_DELETE_REASON",
    "MAX_PAGE_SIZE",
    "router",
]
