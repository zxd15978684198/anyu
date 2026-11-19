"""房间 REST 薄层（`GET /api/rooms` 2026-11-11；`POST`/`PATCH` 2026-11-12）。

存在的原初理由是给「新建设备」提供 `room_id`：E14 要 `room_id`，而此前全仓没有任何
接口能返回它（`GET /api/elders` 只回 `room_no` 文本）。

2026-11-12 缺口修复（ADM-01）：只读口让房间变成「只能连库维护」的实物，现补
`POST /api/rooms`（建）与 `PATCH /api/rooms/{room_id}`（改名 / 置维护），写业务全部在
`app/services/room_service.py`；本层只做 Pydantic 校验、接当前主体、把领域异常映射成
契约 §7 响应体（AGENTS §2：`api` 不写业务逻辑）。

权限与 `GET /api/devices` 对齐（`require_roles("admin")`），避免出现
「能列房间但列不了设备」的半开状态。

为什么另立 `RoomAPIRoute` 而不是复用 `ContractAPIRoute`：房间的四个领域异常在
`ContractAPIRoute` 里没有对应分支（那张表是按 elders/devices 的异常写的），
而 `app/api/elders.py` 不在本卡写作用域内。这里子类化后**只追加**房间异常分支，
父类的映射逻辑原样保留——两层 catch 顺序是「子类先接自己的，其余落回父类」。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import room_service
from app.services.room_service import (
    RoomConflictError,
    RoomNotFoundError,
    RoomValidationError,
)

RoomStatus = Literal["available", "partial", "full", "maintenance"]


class RoomAPIRoute(ContractAPIRoute):
    """在契约 §7 映射之外追加房间领域异常（404 / 409 / 422）。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        inner_handler = super().get_route_handler()

        async def room_handler(request: Request) -> Response:
            try:
                return await inner_handler(request)
            except RoomNotFoundError:
                return _error_response(404, "NOT_FOUND", "room not found")
            except RoomConflictError:
                return _error_response(409, "CONFLICT", "room_no already exists")
            except RoomValidationError:
                return _error_response(422, "VALIDATION_ERROR", "invalid room payload")

        return room_handler


router = APIRouter(prefix="/api/rooms", tags=["rooms"], route_class=RoomAPIRoute)


class RoomListItemResponse(BaseModel):
    """房间列表项。`bed_count` 是该房间 `beds` 行数（含空床）。"""

    room_id: uuid.UUID
    room_no: str
    status: str
    bed_count: int


class RoomListResponse(BaseModel):
    items: list[RoomListItemResponse]
    total: int


class RoomCreateRequest(BaseModel):
    """建房间：只允许房号 +（可选）初始状态；床位归 `beds` 维护，这里不收。"""

    model_config = ConfigDict(extra="forbid")

    room_no: str = Field(min_length=1, max_length=room_service.MAX_ROOM_NO_LENGTH)
    status: RoomStatus = "available"


class RoomUpdateRequest(BaseModel):
    """改名 / 改状态：白名单字段 + `extra="forbid"`，且必须至少传一个。"""

    model_config = ConfigDict(extra="forbid")

    room_no: str | None = Field(
        default=None, min_length=1, max_length=room_service.MAX_ROOM_NO_LENGTH
    )
    status: RoomStatus | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> RoomUpdateRequest:
        """空 body 的 PATCH 不该静默成功：它会让人以为改动生效了。"""
        if self.room_no is None and self.status is None:
            raise ValueError("at least one of room_no / status is required")
        return self


def _item_response(item: room_service.RoomListItem) -> RoomListItemResponse:
    return RoomListItemResponse(
        room_id=item.room_id,
        room_no=item.room_no,
        status=item.status,
        bed_count=item.bed_count,
    )


@router.get("", response_model=RoomListResponse)
async def list_rooms(
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> RoomListResponse:
    """房间清单（照护台新建设备用）：仅 admin，按 `room_no` 排序，不分页。"""
    result = await room_service.list_rooms(session, admin)
    return RoomListResponse(
        items=[_item_response(item) for item in result.items], total=result.total
    )


@router.post(
    "",
    response_model=RoomListItemResponse,
    status_code=201,
    summary="新建房间（仅 admin）",
)
async def create_room(
    payload: RoomCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> RoomListItemResponse:
    """建房间；房号重复 409，空白/超长房号 422。床位不在这里建。"""
    item = await room_service.create_room(
        session, admin, room_no=payload.room_no, status=payload.status
    )
    return _item_response(item)


@router.patch(
    "/{room_id}",
    response_model=RoomListItemResponse,
    summary="改房号 / 改维护状态（仅 admin）",
)
async def update_room(
    room_id: uuid.UUID,
    payload: RoomUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> RoomListItemResponse:
    """PATCH 语义：传了才改；房号撞车 409，房间不存在 404。"""
    item = await room_service.update_room(
        session,
        admin,
        room_id,
        room_no=payload.room_no,
        status=payload.status,
    )
    return _item_response(item)


__all__ = ["router"]
