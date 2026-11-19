"""家属账号与家属绑定 REST 薄层（G1 建家属账号 / E13 绑定）。

owner=建号接口（adhoc-20261106）· 依据 设计记录（员工与家属建号接口）

契约只定义了 E13「绑定已有家属账号」，**没有创建家属账号的端点**（缺口 G1）；
本模块补上 `POST /api/guardians`，与 E13 同属「管理员建号 + 绑定」这一条能力。

权限：仅 `admin`。
范围：绑定的写入侧同时补齐缺口 G2 —— `elder_guardians` 是家属侧范围过滤的唯一依据，
此前没有任何写入口，导致新建家属永远看不到长者。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.api.schemas.console import (
    ElderGuardianItemResponse,
    ElderGuardianListResponse,
    GuardianListItemResponse,
    GuardianListResponse,
)
from app.api.schemas.staff import (
    GuardianBindRequest,
    GuardianBindResponse,
    GuardianCreateRequest,
    GuardianCreateResponse,
)
from app.core.auth import get_current_principal, require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import elder_service, staff_service
from app.services.staff_service import (
    ElderNotFoundError,
    GuardianAlreadyBoundError,
    GuardianAlreadyUnboundError,
    GuardianBindingNotFoundError,
    GuardianLimitExceededError,
    GuardianNotFoundError,
    StaffConflictError,
)

router = APIRouter(tags=["guardians"], route_class=ContractAPIRoute)


@router.post(
    "/api/guardians",
    status_code=status.HTTP_201_CREATED,
    response_model=GuardianCreateResponse,
)
async def create_guardian(
    payload: GuardianCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> GuardianCreateResponse:
    """建家属账号（契约缺口 G1）。手机号跨表唯一，冲突 409。"""
    try:
        row = await staff_service.create_guardian(
            session,
            name=payload.name,
            phone=payload.phone,
            password=payload.password,
        )
    except StaffConflictError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    return GuardianCreateResponse(
        guardian_id=row.guardian_id,
        name=row.name,
        phone_masked=staff_service.mask_phone(row.phone),
        status=row.status,
    )


@router.get("/api/guardians", response_model=GuardianListResponse)
async def list_guardians(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
    page: Annotated[int, Query(ge=1, description="页码，从 1 起")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1–100")] = 20,
    phone: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
) -> GuardianListResponse:
    """家属列表（照护台 W4）：仅 admin；`phone` 给了就精确匹配（查不到即空列表）。

    `phone` 是查询条件而不是响应字段：入参接受完整号码，出参一律脱敏。
    """
    result = await staff_service.list_guardians(
        session, page=page, page_size=page_size, phone=phone
    )
    return GuardianListResponse(
        items=[
            GuardianListItemResponse(
                guardian_id=item.guardian_id,
                name=item.name,
                phone_masked=staff_service.mask_phone(item.phone),
                status=item.status,
            )
            for item in result.items
        ],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/api/elders/{elder_id}/guardians",
    response_model=ElderGuardianListResponse,
)
async def list_elder_guardians(
    elder_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> ElderGuardianListResponse:
    """某长者已绑家属（照护台 W4）：admin 全量；护工/医生仅负责范围内，范围外 403。

    范围与存在性的判定顺序、审计口径全在 `elder_service.list_elder_guardians`，
    本层只装配响应（越界 → 403 `SCOPE_FORBIDDEN`，不存在 → 404 `NOT_FOUND`）。
    """
    result = await elder_service.list_elder_guardians(session, identity, elder_id)
    return ElderGuardianListResponse(
        items=[
            ElderGuardianItemResponse(
                guardian_id=item.guardian_id,
                binding_id=item.binding_id,
                name=item.name,
                phone_masked=staff_service.mask_phone(item.phone),
                relation=item.relation,
                bound_at=item.bound_at,
            )
            for item in result.items
        ],
        total=result.total,
    )


@router.post(
    "/api/elders/{elder_id}/guardians",
    status_code=status.HTTP_201_CREATED,
    response_model=GuardianBindResponse,
)
async def bind_guardian(
    elder_id: uuid.UUID,
    payload: GuardianBindRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> GuardianBindResponse:
    """把已有家属账号绑定到长者（E13）。每位长者 active 监护人最多 2 位。"""
    try:
        binding = await staff_service.bind_guardian(
            session,
            elder_id=elder_id,
            guardian_id=payload.guardian_id,
            relation=payload.relation,
        )
    except ElderNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    except GuardianNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    except GuardianAlreadyBoundError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    except GuardianLimitExceededError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    return GuardianBindResponse(
        elder_id=binding.elder_id,
        guardian_id=binding.guardian_id,
        relation=binding.relation,
        status=binding.status,
    )


@router.delete(
    "/api/bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def unbind_guardian(
    binding_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
    reason: Annotated[str | None, Query(max_length=200, description="解绑原因（进审计）")] = None,
) -> Response:
    """解绑一条家属绑定（AC-24：`status='unbound'` 保痕，不删行）。

    成功 204 无响应体；`binding_id` 不存在 404 `NOT_FOUND`；已解绑 409 `CONFLICT`
    （幂等口径见 `staff_service.unbind_guardian` 文档，重复请求不写第二条审计）；
    非 admin 403 `ROLE_FORBIDDEN`。
    """
    try:
        await staff_service.unbind_guardian(session, binding_id=binding_id, reason=reason)
    except GuardianBindingNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)
    except GuardianAlreadyUnboundError as exc:
        return _error_response(409, "CONFLICT", exc.message)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
