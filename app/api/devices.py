"""设备绑定 REST 薄层（接口契约 E9–E11、E13–E14）。

鉴权、范围与绑定规则全部在 `app/services/device_service.py`；本文件只做 Pydantic 校验、
接收当前主体、调用服务并复用 `app/api/elders.py` 的 `ContractAPIRoute` 做错误体映射
（本卡不允许新建共享文件，理由见该类的 docstring）。

**凭证明文只出现一次**：E13/E14 的响应体里带 `device_secret`，它是 `token_hash` 的原文。
服务层返回后本层原样透传，不写日志、不进审计、不落库；刷新页面后无法再取回，
只能重新签发。这是「只回显一次」的实现边界。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.api.schemas.console import DeviceListItemResponse, DeviceListResponse
from app.audio.signaling import close_device_sessions
from app.core.auth import get_current_principal, require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import device_service

router = APIRouter(prefix="/api/devices", tags=["devices"], route_class=ContractAPIRoute)


async def _revoke_ws_sessions(device_id: uuid.UUID) -> None:
    """凭证失效后立刻踢掉该设备**已建立**的信令连接（L9 验证 F-2）。

    分层说明（AGENTS §2.1）：services 不许 import audio，所以「解绑后踢线」这一步
    落在 api 层——本文件本来就在 api→audio 的允许方向（`app/api/audio.py` 等先例）。
    覆盖三条会让 `token_hash` / `status` 失配的路径：解绑、重发凭证、禁用。新连接
    由握手期的 `resolve_principal` 拦（401 / 4401），老连接只能在这里主动关。
    """
    await close_device_sessions(str(device_id))


class DeviceBindRequest(BaseModel):
    """E9 请求体。`elder_id` 必须显式给出；显式 `null` 走解绑语义并轮换哈希。"""

    model_config = ConfigDict(extra="forbid")

    room_id: uuid.UUID
    elder_id: uuid.UUID | None


class DeviceCreateRequest(BaseModel):
    """E14 请求体：新设备落在哪个房间，可选直接绑一位在住长者。"""

    model_config = ConfigDict(extra="forbid")

    room_id: uuid.UUID
    elder_id: uuid.UUID | None = None


class DeviceBindingResponse(BaseModel):
    """E9/E10 响应：契约给出的三个绑定字段。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    elder_id: uuid.UUID | None


class IssuedCredentialResponse(BaseModel):
    """E13 响应：明文凭证只在此处出现一次。"""

    device_id: uuid.UUID
    device_secret: str
    rotated_at: datetime.datetime


class DeviceDisableRequest(BaseModel):
    """禁用请求体：`reason` 只进审计 detail（如 lost / returned / maintenance）。"""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=64)


class DeviceDisabledResponse(BaseModel):
    """禁用响应：只有设备号与终态，不回显凭证相关内容。"""

    device_id: uuid.UUID
    status: Literal["locked"]


class CreatedDeviceResponse(BaseModel):
    """E14 响应：新设备 + 首次签发的明文凭证。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    elder_id: uuid.UUID | None
    device_secret: str
    status: str


class ElderCandidateResponse(BaseModel):
    """E11 候选字段严格为三项，姓名只以脱敏形式出现（S-06）。"""

    elder_id: uuid.UUID
    bed_no: str
    display_name: str


class ElderCandidatesResponse(BaseModel):
    room_id: uuid.UUID
    mode: Literal["single", "double"]
    candidates: list[ElderCandidateResponse]


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
    page: Annotated[int, Query(ge=1, description="页码，从 1 起")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1–100")] = 20,
    bound: Annotated[
        bool | None, Query(description="true=已绑长者，false=未绑；不传即全部")
    ] = None,
) -> DeviceListResponse:
    """设备列表（照护台 W4）：仅 admin；响应不含 `token_hash` / `secret`。

    `bound` 是派生值（`elder_id` 是否存在），不靠 `status` 推断：解绑会把状态置
    `offline`，但离线设备也可能仍在绑定中。
    """
    result = await device_service.list_devices(session, page=page, page_size=page_size, bound=bound)
    return DeviceListResponse(
        items=[
            DeviceListItemResponse(
                device_id=item.device_id,
                elder_id=item.elder_id,
                elder_name=item.elder_name,
                room_no=item.room_no,
                bed_no=item.bed_no,
                bound=item.bound,
                status=item.status,
                last_seen_at=item.last_seen_at,
            )
            for item in result.items
        ],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=CreatedDeviceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新建设备并签发长期凭证（仅 admin）",
)
async def create_device(
    payload: DeviceCreateRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CreatedDeviceResponse:
    """E14：新设备一律 `offline`，`device_secret` 只在此响应出现一次。"""
    created = await device_service.create_device(
        session,
        identity,
        room_id=payload.room_id,
        elder_id=payload.elder_id,
    )
    return CreatedDeviceResponse(
        device_id=created.device_id,
        room_id=created.room_id,
        elder_id=created.elder_id,
        device_secret=created.device_secret,
        status=created.status,
    )


@router.post(
    "/{device_id}/credential",
    response_model=IssuedCredentialResponse,
    summary="重发设备长期凭证（仅 admin；先解绑再签发）",
)
async def issue_credential(
    device_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IssuedCredentialResponse:
    """E13：裁决 D2-A —— 重发凭证 = 先解绑再签发，旧凭证立即失效。

    返回的 `device_secret` 明文只出现这一次；服务端只留 SHA-256 哈希。
    """
    issued = await device_service.issue_credential(session, identity, device_id)
    await _revoke_ws_sessions(device_id)
    return IssuedCredentialResponse(
        device_id=issued.device_id,
        device_secret=issued.device_secret,
        rotated_at=issued.rotated_at,
    )


@router.post(
    "/{device_id}/disable",
    response_model=DeviceDisabledResponse,
    summary="禁用设备（仅 admin；置 locked，旧凭证立即失效）",
)
async def disable_device(
    device_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: DeviceDisableRequest | None = None,
) -> DeviceDisabledResponse:
    """FR-ADM-04：管理员主动停用一台平板（`status='locked'`）。

    禁用即一次凭证失效：`app/services/auth_service.py` 的登录分支对 `locked` 直接拒绝，
    `app/scheduler/devices.py` 也把 `locked` 当成主动停用跳过掉线扫描。审计 `device.disable`
    与服务调用同事务落库，写不进去则本请求失败（不允许「停掉了但查不到谁停的」）。
    """
    await device_service.disable_device(
        session,
        identity,
        device_id,
        reason=payload.reason if payload is not None else None,
    )
    await _revoke_ws_sessions(device_id)
    return DeviceDisabledResponse(device_id=device_id, status="locked")


@router.post(
    "/{device_id}/bind",
    response_model=DeviceBindingResponse,
    summary="设备绑定房间与长者（仅 admin）",
)
async def bind_device(
    device_id: uuid.UUID,
    payload: DeviceBindRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceBindingResponse:
    binding = await device_service.bind_device(
        session,
        identity,
        device_id,
        room_id=payload.room_id,
        elder_id=payload.elder_id,
    )
    return DeviceBindingResponse(
        device_id=binding.device_id,
        room_id=binding.room_id,
        elder_id=binding.elder_id,
    )


@router.post(
    "/{device_id}/unbind",
    response_model=DeviceBindingResponse,
    summary="设备解绑（仅 admin；清长者、置 offline、轮换 token_hash）",
)
async def unbind_device(
    device_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceBindingResponse:
    binding = await device_service.unbind_device(session, identity, device_id)
    await _revoke_ws_sessions(device_id)
    return DeviceBindingResponse(
        device_id=binding.device_id,
        room_id=binding.room_id,
        elder_id=binding.elder_id,
    )


@router.get(
    "/{device_id}/elder-candidates",
    response_model=ElderCandidatesResponse,
    summary="本房间候选长者（admin 或设备本人）",
)
async def list_elder_candidates(
    device_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderCandidatesResponse:
    result = await device_service.list_elder_candidates(session, identity, device_id)
    return ElderCandidatesResponse(
        room_id=result.room_id,
        mode=result.mode,
        candidates=[
            ElderCandidateResponse(
                elder_id=item.elder_id,
                bed_no=item.bed_no,
                display_name=item.display_name,
            )
            for item in result.candidates
        ],
    )


__all__ = ["router"]
