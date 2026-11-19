"""照护台通知中心（PRD §4.7；迭代 8 计划 §4 W3；N31/N32 补重发与撤销）。

六件事：列通知、确认（ack）、推送尝试（deliver）、端侧回执（receipt）、重发
（resend）、10 秒内撤销（revoke）。状态机在 `app/services/notification_service.py`，
本文件不判 status 能不能跳（唯一例外是把领域异常翻译成契约错误体）。

`POST /{id}/deliver` 是**推送尝试**：本仓不接微信/短信/电话（AGENTS §4.5），
所以它只把 `generated → pending` 并记 `payload.pushed_at`，**不再写 `delivered`**
（PRD line 398：只有客户端回执算送达）。演示分支仍可用 `?simulated=true` 直接
进终态 `simulated`。

`POST /{id}/receipt` 是**端侧回执**：只认设备凭证（`kind=device`）且设备的
`elder_id` 必须与通知的 `elder_id` 一致，否则 403；`pending → delivered` 并写
`delivered_at`。已 `delivered` 再回执是幂等的 200 原状态（不改成 acknowledged）。

`POST /{id}/resend` 与 `POST /{id}/revoke` 是同一行通知上的两个**运营动作**，
都不新建通知行：重发只是「再投一次」（`payload.resend_count` +1），撤销是「发错了，
10 秒内收回」（状态置 `failed` + 同事务写审计）。两者的拒绝码刻意分开：
状态不对是 409 `invalid_transition`，过了窗口是 409 `revoke_window_expired`。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import notification_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/notifications",
    tags=["notifications"],
    route_class=ContractAPIRoute,
)


class NotificationOut(BaseModel):
    notification_id: uuid.UUID
    elder_id: uuid.UUID | None = None
    kind: str
    severity: str
    status: str
    payload: dict[str, Any] | None = None
    assignee_staff_id: uuid.UUID | None = None
    delivered_at: datetime.datetime | None = None
    acknowledged_at: datetime.datetime | None = None
    #: 与 `acknowledged_at` 同值的验收直读别名（PRD line 398 / FR-ALT-03）。
    confirmed_at: datetime.datetime | None = None
    created_at: datetime.datetime


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    total: int
    page: int
    page_size: int


def _out(item: notification_service.NotificationItem) -> NotificationOut:
    return NotificationOut(
        notification_id=item.notification_id,
        elder_id=item.elder_id,
        kind=item.kind,
        severity=item.severity,
        status=item.status,
        payload=item.payload,
        assignee_staff_id=item.assignee_staff_id,
        delivered_at=item.delivered_at,
        acknowledged_at=item.acknowledged_at,
        confirmed_at=item.acknowledged_at,
        created_at=item.created_at,
    )


def _notification_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, notification_service.NotificationNotFoundError):
        return _error_response(404, "NOT_FOUND", "notification not found")
    if isinstance(exc, notification_service.NotificationScopeForbiddenError):
        # 回执越权：设备只能替自己绑定的长者确认送达（FR-ALT-03 红线）。
        return _error_response(
            403,
            "SCOPE_FORBIDDEN",
            "notification does not belong to this device's elder",
        )
    if isinstance(exc, notification_service.RevokeWindowExpiredError):
        # 与「状态不对」分开：窗口过了是「晚了」，不是「本来就不该撤」。
        return _error_response(
            409,
            "revoke_window_expired",
            f"notification can only be revoked within {exc.window_seconds}s of delivery",
        )
    if isinstance(exc, notification_service.InvalidTransitionError):
        # 与 lead 约定的码：小写 invalid_transition，detail 里带两侧状态便于排障。
        return _error_response(
            409,
            "invalid_transition",
            f"cannot move notification from {exc.from_status} to {exc.to_status}",
        )
    if isinstance(exc, notification_service.NotificationValidationError):
        return _error_response(422, "VALIDATION_ERROR", str(exc))
    if isinstance(exc, RoleForbiddenError):
        return _error_response(403, "ROLE_FORBIDDEN", "role is not allowed to use notifications")
    raise exc  # pragma: no cover - 未分类异常交给全局 handler


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: Annotated[str | None, Query(description="按状态过滤")] = None,
    kind: Annotated[str | None, Query(description="按类型过滤")] = None,
    severity: Annotated[str | None, Query(description="按严重度过滤（red/yellow/green）")] = None,
) -> NotificationListOut | JSONResponse:
    """当前员工可见的通知（admin 全量；护工/医生看负责范围 + 派给本人的）。"""
    try:
        result = await notification_service.list_for_staff(
            session,
            identity,
            page=page,
            page_size=page_size,
            status=status,
            kind=kind,
            severity=severity,
        )
    except (
        notification_service.NotificationServiceError,
        RoleForbiddenError,
    ) as exc:
        return _notification_error(exc)
    return NotificationListOut(
        items=[_out(item) for item in result.items],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.post("/{notification_id}/deliver", response_model=NotificationOut)
async def deliver_notification(
    notification_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    simulated: Annotated[bool, Query(description="true 时进模拟终态")] = False,
) -> NotificationOut | JSONResponse:
    """推送尝试：generated/pending → pending，记 `payload.pushed_at`；不外联。

    真正把状态推到 `delivered` 的只有端侧回执 `POST /{id}/receipt`——「送达」
    是客户端说收到，不是员工点一下按钮（PRD line 398）。
    """
    try:
        item = await notification_service.deliver(
            session, identity, notification_id, simulated=simulated
        )
    except (
        notification_service.NotificationServiceError,
        RoleForbiddenError,
    ) as exc:
        return _notification_error(exc)
    return _out(item)


@router.post("/{notification_id}/receipt", response_model=NotificationOut)
async def receipt_notification(
    notification_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationOut | JSONResponse:
    """端侧回执：只认设备凭证，pending → delivered；重复回执幂等 200。"""
    try:
        item = await notification_service.receipt(session, identity, notification_id)
    except (
        notification_service.NotificationServiceError,
        RoleForbiddenError,
    ) as exc:
        return _notification_error(exc)
    return _out(item)


@router.post("/{notification_id}/ack", response_model=NotificationOut)
async def acknowledge_notification(
    notification_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationOut | JSONResponse:
    """确认通知：delivered → acknowledged；终态再确认一律 409。"""
    try:
        item = await notification_service.acknowledge(session, identity, notification_id)
    except (
        notification_service.NotificationServiceError,
        RoleForbiddenError,
    ) as exc:
        return _notification_error(exc)
    return _out(item)


@router.post("/{notification_id}/resend", response_model=NotificationOut)
async def resend_notification(
    notification_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationOut | JSONResponse:
    """重发：同一条通知再投一次（不新建行）；终态一律 409 `invalid_transition`。"""
    try:
        item = await notification_service.resend(session, identity, notification_id)
    except (
        notification_service.NotificationServiceError,
        RoleForbiddenError,
    ) as exc:
        return _notification_error(exc)
    return _out(item)


@router.post("/{notification_id}/revoke", response_model=NotificationOut)
async def revoke_notification(
    notification_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    reason: Annotated[
        str | None,
        Query(max_length=100, description="撤销原因，写进审计（可选）"),
    ] = None,
) -> NotificationOut | JSONResponse:
    """10 秒内撤销：置 `failed` + 同事务写审计；超窗 409 `revoke_window_expired`。"""
    try:
        item = await notification_service.revoke(
            session,
            identity,
            notification_id,
            reason=reason,
        )
    except (
        notification_service.NotificationServiceError,
        RoleForbiddenError,
    ) as exc:
        return _notification_error(exc)
    return _out(item)


__all__ = ["NotificationListOut", "NotificationOut", "router"]
