"""审计查询与导出 REST（admin 端，PRD AC-14 操作可追溯 / FR-ADM-08 审计留痕）。

## 为什么两个端点都只给 admin

`audit_logs` 是**全站**操作记录：谁在什么时候读了谁的记忆、改了谁的药单、导出了什么。
它天然横跨所有长者，也是「护工能看到哪些长者」这类范围规则的唯一反查手段。给护工或医生
开读权限，等于把范围隔离自己交出去（能看审计就能知道隔壁长者发生了什么）。

所以本模块两个端点**一律 admin**，非 admin 一律 `403` + 错误码 `ROLE_FORBIDDEN`：

| 端点 | 允许 | 其它员工角色 | 家属 / 设备 |
| --- | --- | --- | --- |
| `GET /api/audit/logs` | admin | 403 `ROLE_FORBIDDEN` + 审计 | 403 `ROLE_FORBIDDEN` + 审计 |
| `GET /api/audit/logs/export` | admin | 403 `ROLE_FORBIDDEN` + 审计 | 403 `ROLE_FORBIDDEN` + 审计 |

> 与 `app/api/memory.py` 的差异：那边把家属/设备记成 `403 SCOPE_FORBIDDEN`（按"长者范围"
> 语义区分）。本卡验收口径写明「非 admin **一律** 403 / `ROLE_FORBIDDEN`」，故这里不做
> `SCOPE_FORBIDDEN` 分流；两者都是 403，前端只需看 `code` 决定文案。

## 越权也要留痕（AGENTS §8：越权 → 拒绝 + 审计 + trace_id）

拒绝路径**必然**抛 403，`get_session` 在异常路径上会 rollback，所以审计必须走
`audit_service.write_audit_isolated`（独立会话、立即提交），否则「谁试图读审计」这件事
会被自己抹掉。写失败只记日志、不上抛 —— 审计是旁路，不能把 403 变成 500。

## trace_id 三处同源

`get_trace_id()` 读的是 `TraceIdMiddleware` 写进 contextvar 的入站值，本模块把它同时放进：

1. 审计 `detail["trace_id"]`（`audit_service` 写入时自动合并）；
2. 契约错误体 `{"detail", "code", "trace_id"}`；
3. 日志 `extra`。

导出响应头另由中间件回写 `X-Trace-Id`，本模块不重复设置。

## 分层（AGENTS §2）

本模块只做参数校验（Pydantic/FastAPI 的 `Query`）、角色过滤、调
`app/services/audit_service.py`、把结果序列化成响应。**不出现** `session.add/commit`，
**不出现** `select(AuditLog)` —— 排序、分页、脱敏、留痕全在服务层。
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import audit_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/audit",
    tags=["audit"],
    route_class=ContractAPIRoute,
)

#: 审计端点是 admin 独占（模块 docstring 第 1 节）。
ADMIN_ROLES: Final[frozenset[str]] = frozenset({"admin"})

#: NDJSON：一行一个 JSON 对象，`application/x-ndjson` 是它的标准媒体类型。
#: 不用 `application/json` 是因为后者语义上是一个 JSON 文档，浏览器与代理会想整个缓冲再解析，
#: 而 JSONL 的优势正是「边下边解析、坏一行不毁全量」——媒体类型说错了优势就没了。
NDJSON_MEDIA_TYPE: Final[str] = "application/x-ndjson"

#: `actor_type` 的合法取值，与 `audit_logs` 的 CHECK 约束一致；交给 FastAPI 做 422 前置校验。
AuditActorType = Literal["staff", "guardian", "system", "device"]


# ------------------------------------------------------------------ 响应模型


class AuditLogOut(BaseModel):
    """一条审计记录的公开投影（键集合与 `audit_service.EXPORT_KEYS` 一致）。

    `trace_id` 在表里没有独立列，由服务层从 `detail` 提升上来；老数据没有这条时是 `None`，
    字段依然**存在**（键集合稳定，前端不用做 `in` 判断）。
    """

    audit_id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID
    action: str
    resource_type: str
    resource_id: uuid.UUID
    detail: dict[str, Any] | None = None
    trace_id: str | None = None
    created_at: datetime.datetime


class AuditLogListOut(BaseModel):
    """分页外壳：`items` + `total` + 回显的 `limit/offset`。"""

    items: list[AuditLogOut]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ 错误体


def _error_response(status_code: int, code: str, detail: str) -> JSONResponse:
    """契约 §7 错误体；`trace_id` 由中间件写入 contextvar，这里只负责带出。"""
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, "trace_id": get_trace_id() or "-"},
    )


# ------------------------------------------------------------------ 权限


def _principal_uuid(identity: Identity) -> uuid.UUID:
    """身份主键 → `audit_logs.actor_id`（NOT NULL）。

    取不到（理论上不会）时退化成全零 UUID 而不是抛错：**拒绝请求**是 403 的主要结论，
    不能因为「审计里的 actor_id 拼不出来」把 403 变成 500。
    """
    try:
        return uuid.UUID(str(identity.principal_id))
    except (AttributeError, TypeError, ValueError):
        return uuid.UUID(int=0)


async def _require_admin(identity: Identity, *, action: str, detail: Mapping[str, Any]) -> None:
    """admin 闸门：非 admin **先写越权审计**再抛 `RoleForbiddenError`（→ 403）。"""
    if identity.kind == "staff" and (identity.role or "") in ADMIN_ROLES:
        return

    payload: dict[str, Any] = {
        "reason": "non_admin",
        "role": identity.role,
        "kind": identity.kind,
    }
    payload.update(detail)

    try:
        actor_type: str | None = audit_service.actor_type_of(identity.kind)
    except audit_service.AuditQueryError:
        # 身份种类与 CHECK 取值对不上：留痕留不下来，但 403 必须照发。
        logger.error(
            "audit_denied_actor_type_unknown",
            extra={"trace_id": get_trace_id() or "-", "kind": identity.kind},
        )
        actor_type = None

    if actor_type is not None:
        await audit_service.write_audit_isolated(
            actor_type=actor_type,
            actor_id=_principal_uuid(identity),
            action=action,
            resource_type=audit_service.AUDIT_RESOURCE_TYPE,
            resource_id=audit_service.NIL_RESOURCE_ID,
            detail=payload,
            trace_id=get_trace_id() or "-",
        )

    raise RoleForbiddenError("audit endpoints are admin-only (FR-ADM-08)")


# ------------------------------------------------------------------ 端点


@router.get("/logs", response_model=AuditLogListOut)
async def list_audit_logs(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_type: Annotated[
        AuditActorType | None,
        Query(description="按主体种类过滤：staff / guardian / system / device"),
    ] = None,
    action: Annotated[
        str | None, Query(max_length=100, description="按动作精确过滤，如 audit.export")
    ] = None,
    actor_id: Annotated[uuid.UUID | None, Query(description="按操作者主键过滤")] = None,
    start: Annotated[
        datetime.datetime | None, Query(description="created_at 下界（含），ISO 8601")
    ] = None,
    end: Annotated[
        datetime.datetime | None, Query(description="created_at 上界（含），ISO 8601")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=audit_service.MAX_LIMIT)] = audit_service.DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditLogListOut | JSONResponse:
    """审计日志分页查询（**仅 admin**），按 `created_at DESC, audit_id DESC` 稳定倒序。

    `total` 与 `items` 用**同一组过滤条件**，所以「翻到底」不会出现 total 说还有、页面却空
    的情况。参数非法（时区窗反向、limit 越界等）由服务层抛 `AuditQueryError` → 422
    `VALIDATION_ERROR`；`actor_type` 的枚举与 UUID/时间格式错更早一步被 FastAPI 拦成 422。
    """
    await _require_admin(
        identity,
        action=audit_service.READ_ACTION,
        detail={"endpoint": "audit.logs", "filters": _filters_label(actor_type, action, actor_id)},
    )

    try:
        rows = await audit_service.list_audits(
            session,
            actor_type=actor_type,
            action=action,
            actor_id=actor_id,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        total = await audit_service.count_audits(
            session,
            actor_type=actor_type,
            action=action,
            actor_id=actor_id,
            start=start,
            end=end,
        )
    except audit_service.AuditQueryError as exc:
        return _error_response(422, "VALIDATION_ERROR", str(exc))

    logger.info(
        "audit_logs_listed",
        extra={
            "trace_id": get_trace_id() or "-",
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
            "total": total,
        },
    )
    return AuditLogListOut(
        items=[AuditLogOut(**audit_service.serialize_audit(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/logs/export")
async def export_audit_logs(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_type: Annotated[AuditActorType | None, Query(description="同 GET /logs")] = None,
    action: Annotated[str | None, Query(max_length=100, description="同 GET /logs")] = None,
    actor_id: Annotated[uuid.UUID | None, Query(description="同 GET /logs")] = None,
    start: Annotated[datetime.datetime | None, Query(description="同 GET /logs")] = None,
    end: Annotated[datetime.datetime | None, Query(description="同 GET /logs")] = None,
) -> Response:
    """导出过滤后的审计日志为 JSONL（**仅 admin**），最多 ``MAX_EXPORT_ROWS`` 行。

    三件事按顺序做，顺序本身是口径：

    1. **admin 闸门**（越权在此留痕并 403，不会进入导出）；
    2. **导出**（服务层已脱敏；`detail` 里的手机号等敏感值在落盘前就没了）；
    3. **留痕** `action='audit.export'`，`detail` 带 `filters` 与 `rows` —— 事后能回答
       「谁在什么时候导走了多大范围的数据」。

    留痕失败**不让导出失败**（fail-open）：admin 拉一份审计通常是为了排查/合规取证，
    因为旁路记录写失败就拒绝交付数据，代价比「少一条留痕 + 一条 error 日志」更大。
    这是与"写业务库必须同事务写审计"相反的选择，理由见 `audit_service.record_export`。

    行数上限在服务层，本端点不暴露 `limit` 参数：导出不是分页接口，要更小的切片请给
    `start`/`end` 或 `action`，要更大的范围请拆成多次导出（每次都会留痕）。
    """
    await _require_admin(
        identity,
        action=audit_service.EXPORT_ACTION,
        detail={
            "endpoint": "audit.logs.export",
            "filters": _filters_label(actor_type, action, actor_id),
        },
    )

    trace_id = get_trace_id() or "-"
    filters: dict[str, Any] = {
        "actor_type": actor_type,
        "action": action,
        "actor_id": actor_id,
        "start": start,
        "end": end,
    }

    try:
        records = await audit_service.export_audits(
            session,
            actor_type=actor_type,
            action=action,
            actor_id=actor_id,
            start=start,
            end=end,
            max_rows=audit_service.MAX_EXPORT_ROWS,
        )
    except audit_service.AuditQueryError as exc:
        return _error_response(422, "VALIDATION_ERROR", str(exc))

    try:
        await audit_service.record_export(
            actor_type=audit_service.actor_type_of(identity.kind),
            actor_id=_principal_uuid(identity),
            trace_id=trace_id,
            filters=filters,
            rows=len(records),
        )
    except Exception:  # pragma: no cover - 留痕失败不改变导出结果（fail-open，见 docstring）
        logger.exception(
            "audit_export_record_failed",
            extra={"trace_id": trace_id, "rows": len(records)},
        )

    body = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    filename = f"audit_logs_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')}.ndjson"

    logger.info(
        "audit_logs_exported",
        # `filename` 是 LogRecord 保留字（logging 会抛 KeyError: Attempt to overwrite
        # 'filename'），故改名 `export_filename`——与 迭代 5 那次 `name` 撞车的教训同源。
        extra={"trace_id": trace_id, "rows": len(records), "export_filename": filename},
    )
    return Response(
        # 空结果也要给一个合法的空响应体（0 字节），而不是 `"\n"` —— 空行会被下游当坏行。
        content=body.encode("utf-8"),
        media_type=NDJSON_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # 审计数据不缓存：中间代理缓存一份"某次导出"等于把权限校验也缓存了。
            "Cache-Control": "no-store",
        },
    )


# ------------------------------------------------------------------ 日志辅助


def _filters_label(actor_type: str | None, action: str | None, actor_id: uuid.UUID | None) -> str:
    """把过滤条件压成一行短标签进审计 `detail`（不记起止时间，避免 detail 变噪音）。"""
    parts: list[str] = []
    if actor_type:
        parts.append(f"actor_type={actor_type}")
    if action:
        parts.append(f"action={action}")
    if actor_id:
        parts.append(f"actor_id={actor_id}")
    return ",".join(parts) or "-"


__all__ = [
    "ADMIN_ROLES",
    "NDJSON_MEDIA_TYPE",
    "AuditLogListOut",
    "AuditLogOut",
    "router",
]
