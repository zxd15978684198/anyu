"""身份认证 HTTP 路由 —— 登录 / 刷新 / 当前身份 / 设备换会话（契约 §4 E1–E4）。

**分层硬约束（AGENTS §2）**：
- 本模块**禁止 import ORM**（StaffAccount / GuardianAccount / Device 等）；
- 本模块**禁止写业务逻辑**，只做三件事：参数校验 → 调 auth_service → 序列化。
- ORM 操作唯一落在 app/services/auth_service.py。

**错误处理**：auth_service 的业务异常在路由函数里捕获 → 映射成 JSONResponse。
task-4 auth 层的异常（Credential* / TokenExpiredError）和其他未预期异常由
**app 级全局 exception handler** 捕获 —— 不在本模块注册，由 app/main.py 和
测试 fixture 负责（见 tests/test_auth_api.py::app_for_test）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import (
    CurrentIdentityResponse,
    DeviceLoginRequest,
    DeviceLoginResponse,
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    RefreshResponse,
)

# 音频上限与 413 码属于 C4 的入站契约（350KB ≈ 10s WAV），复用而不是抄第二个魔数；
# api 之间互相 import 常量已有先例（app/api/devices.py 从 app/api/elders.py 取路由类）。
from app.api.voiceprints import MAX_SAMPLE_AUDIO_BYTES, PAYLOAD_TOO_LARGE
from app.core.auth import (
    RoleForbiddenError,
    ScopeForbiddenError,
    get_current_principal,
    require_roles,
)
from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import auth_service, identity_service, voiceprint_login_service
from app.services.auth_service import (
    AuthConflictError,
    AuthExpiredError,
    AuthInvalidError,
    AuthRevokedError,
)
from app.services.identity_service import (
    StaffCardConflictError,
    StaffCardCredentialError,
    StaffCardNotFoundError,
    StaffCardValidationError,
)
from app.services.voiceprint_service import VoiceprintInvalidAudio

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# 路由内错误映射（仅 auth_service 层的业务异常；task-4 auth 层异常走全局 handler）
# ---------------------------------------------------------------------------


def _handle_service_exception(exc: Exception) -> JSONResponse:
    trace_id = get_trace_id() or "-"
    if isinstance(exc, AuthInvalidError):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code=exc.kind or "TOKEN_INVALID", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, AuthExpiredError):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_EXPIRED", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, AuthRevokedError):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_REVOKED", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, AuthConflictError):
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                detail=str(exc.detail), code="CONFLICT", trace_id=trace_id
            ).model_dump(),
        )
    # 未预期异常 → 500（不回显内部细节）
    logger.exception("unexpected auth error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            detail="内部错误", code="INTERNAL_ERROR", trace_id=trace_id
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# E1：POST /api/sessions — 人账号登录
# ---------------------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=LoginResponse,
    summary="人账号登录（staff / guardian 共用）",
)
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LoginResponse | JSONResponse:
    try:
        result = await auth_service.login_with_phone(
            session, phone=body.phone, password=body.password
        )
    except Exception as exc:
        return _handle_service_exception(exc)

    return LoginResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        principal={
            "kind": result.principal.kind,
            "id": result.principal.id,
            "role": result.principal.role,
            "name": result.principal.name,
        },
    )


# ---------------------------------------------------------------------------
# E2：POST /api/sessions/refresh
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/refresh",
    response_model=RefreshResponse,
    summary="刷新 Access Token",
)
async def refresh(
    body: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RefreshResponse | JSONResponse:
    try:
        result = await auth_service.refresh_access_token(session, refresh_token=body.refresh_token)
    except Exception as exc:
        return _handle_service_exception(exc)

    return RefreshResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
    )


# ---------------------------------------------------------------------------
# E3：GET /api/sessions/current — 需 Bearer token
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/current",
    response_model=CurrentIdentityResponse,
    summary="当前身份详情（需鉴权）",
)
async def current_identity(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> CurrentIdentityResponse | JSONResponse:
    try:
        result = await auth_service.get_current_identity(session, identity=identity)
    except Exception as exc:
        return _handle_service_exception(exc)

    return CurrentIdentityResponse(
        kind=result.kind,
        id=result.id,
        role=result.role,
        name=result.name,
        scope_elder_ids=result.scope_elder_ids,
    )


# ---------------------------------------------------------------------------
# E4：POST /api/device-sessions — 设备换会话
# ---------------------------------------------------------------------------


@router.post(
    "/device-sessions",
    response_model=DeviceLoginResponse,
    summary="设备长期令牌换 24h 会话",
)
async def device_session(
    body: DeviceLoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceLoginResponse | JSONResponse:
    try:
        result = await auth_service.exchange_device_token(
            session, device_id=body.device_id, device_secret=body.device_secret
        )
    except Exception as exc:
        return _handle_service_exception(exc)

    return DeviceLoginResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        device={
            "id": result.device.id,
            "room_id": result.device.room_id,
            "elder_id": result.device.elder_id,
        },
    )


# ---------------------------------------------------------------------------
# E14：POST /api/staff/{staff_id}/cards — 管理员签发工牌（FR-ADM-08）
#      POST /api/staff-card-sessions   — 工牌换短期会话（FR-ADM-08）
# ---------------------------------------------------------------------------
#
# 为什么落在 auth.py 而不是 app/api/staff.py：`/api/staff` 前缀属
# app/api/staff.py（本卡写作用域之外），而「签发凭证」这件事和 E1–E4 是同一族
# （凭证签发/换取/校验），放同一个 router 才能保证四套凭证体系在同一处维护口径。
# 路径写全 `/staff/...`：本 router 挂载时带 `/api` 前缀，与 E1 的 `/sessions` 同理。


class StaffCardIssueRequest(BaseModel):
    """工牌签发请求（管理员）。

    `scope` 至少一项，取值必须落在 `app/core/security.STAFF_CARD_SCOPES` 内 ——
    白名单判定在服务层（表外取值 422），这里只做「非空」这条结构性校验。
    """

    device_id: uuid.UUID
    scope: list[str] = Field(min_length=1, description="动作级范围，如 workorder:handle")
    # 上限（86400s）**不在本层重复声明**：`le=86400` 会在安全内核改上限时悄悄变成
    # 第二个真相源。越界由 `identity_service` 转 `sign_staff_card` 的判定 → 422。
    ttl_seconds: int | None = Field(default=None, gt=0, description="缺省取上限 86400s")


class StaffCardIssuedResponse(BaseModel):
    """签发响应（契约 `docs/接口契约_身份与权限.md:337` 的示例字段 + 主体信息）。

    `token` 只在这一次响应里出现，**不会再从任何读接口拿到**（DB 只存 sha256）。
    """

    token: str
    staff_id: uuid.UUID
    device_id: uuid.UUID
    scope: list[str]
    expires_at: datetime


class StaffCardSessionRequest(BaseModel):
    """工牌换会话请求：卡令牌走 body 而不是 `Authorization`。

    为什么不走 Bearer：同一个 `Authorization` 头在这套 API 里已经表达「当前请求主体」，
    而 E14 的语义是「拿一张卡换另一张令牌」——放 body 才不会让中间件、
    依赖链把它当成已认证主体（那样会绕过 scope 门禁直接进 `resolve_principal`）。
    """

    card_token: str = Field(min_length=1, description="`POST /api/staff/{id}/cards` 返回的 token")


class StaffCardSessionResponse(BaseModel):
    """换会话响应。**`kind` 仍是 `staff_card`**（不升格为全权员工会话）。"""

    access_token: str
    # `noqa: S105`：ruff 见到 `*_type = "bearer"` 就报硬编码口令；这是响应字段值，不是密钥。
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    scope: list[str]
    expires_in: int
    device_id: uuid.UUID
    elder_id: uuid.UUID | None


def _handle_staff_card_exception(exc: Exception) -> JSONResponse:
    """工牌链路的异常映射（404/409/422/401），其余落到 auth 那套（含 500 兜底）。"""
    trace_id = get_trace_id() or "-"
    if isinstance(exc, StaffCardNotFoundError):
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                detail=str(exc), code="NOT_FOUND", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, StaffCardConflictError):
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(detail=str(exc), code="CONFLICT", trace_id=trace_id).model_dump(),
        )
    if isinstance(exc, StaffCardValidationError):
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                detail=str(exc), code="VALIDATION_ERROR", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, StaffCardCredentialError):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(detail=str(exc), code=exc.code, trace_id=trace_id).model_dump(),
        )
    return _handle_service_exception(exc)


def _forbidden(code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=ErrorResponse(
            detail=detail, code=code, trace_id=get_trace_id() or "-"
        ).model_dump(),
    )


class _StaffCardAPIRoute(APIRoute):
    """把**依赖解析期**抛出的 403 也渲染成本模块统一的 `ErrorResponse`。

    为什么要独立路由类：`require_roles("admin")` / `require_scopes(...)` 在依赖解析阶段
    就抛异常，端点函数体里的 try/except 根本没有机会执行；而本 router 不走
    `app/api/elders.ContractAPIRoute`（那是 elders/work-orders 专用），`app/main.py`
    也没有全局的 `RoleForbiddenError` handler —— 不在这里兜，越权会变成 500。
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:  # type: ignore[override]
        handler = super().get_route_handler()

        async def custom(request: Request) -> Response:
            try:
                return await handler(request)
            except ScopeForbiddenError as exc:
                # scope 不足：审计已由 `require_scopes` 在抛出前用独立会话写好了
                return _forbidden("SCOPE_FORBIDDEN", str(exc))
            except RoleForbiddenError as exc:
                return _forbidden("ROLE_FORBIDDEN", str(exc))

        return custom


# 工牌两个端点单独用一条带 `route_class` 的子 router：`router.post()` 的装饰器
# 没有 `route_class` 参数（FastAPI 0.162 的 `api_route()` 不转发它），而 403 必须在
# 依赖解析期就被兜住（见 `_StaffCardAPIRoute` 的说明）。`include_router` 会把子 router
# 的路由挂进主 router，出站前缀与 `app/api/__init__.py` 的注册方式一致。
_staff_card_router = APIRouter(tags=["auth"], route_class=_StaffCardAPIRoute)


@_staff_card_router.post(
    "/staff/{staff_id}/cards",
    response_model=StaffCardIssuedResponse,
    status_code=201,
    summary="签发工牌（管理员，FR-ADM-08）",
)
async def issue_staff_card(
    staff_id: uuid.UUID,
    body: StaffCardIssueRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> StaffCardIssuedResponse | JSONResponse:
    """给某员工在某台设备上签一张受限工牌，写 `staff_tokens` 一行。

    - 201 + `{token, staff_id, device_id, scope, expires_at}`
    - 401 `TOKEN_*`：没带凭证 / 凭证失效（`require_roles` → `current_staff`）
    - 403 `ROLE_FORBIDDEN`：不是 admin
    - 404 `NOT_FOUND`：员工或设备不存在
    - 409 `CONFLICT`：员工已停用 / 设备已锁定
    - 422 `VALIDATION_ERROR`：`scope` 表外取值、`ttl_seconds` 越界

    审计：成功写一条 `staff_card.issue`（`audit_logs`，detail 不含完整令牌）。
    """
    try:
        issued = await identity_service.issue_staff_card(
            session,
            actor_id=uuid.UUID(str(admin.staff_id)),
            staff_id=staff_id,
            device_id=body.device_id,
            scope=body.scope,
            ttl_seconds=body.ttl_seconds,
        )
    except Exception as exc:
        return _handle_staff_card_exception(exc)

    return StaffCardIssuedResponse(
        token=issued.token,
        staff_id=issued.staff_id,
        device_id=issued.device_id,
        scope=list(issued.scope),
        expires_at=issued.expires_at,
    )


@_staff_card_router.post(
    "/staff-card-sessions",
    response_model=StaffCardSessionResponse,
    summary="工牌换短期会话（FR-ADM-08）",
)
async def staff_card_session(
    body: StaffCardSessionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StaffCardSessionResponse | JSONResponse:
    """用工牌令牌换一张**同样受限**的短期令牌（`kind=staff_card`，不升格）。

    - 200 + `{access_token, token_type, scope, expires_in, device_id, elder_id}`
    - 401 `TOKEN_INVALID` / `TOKEN_EXPIRED` / `TOKEN_REVOKED`：卡无效 / 过期 / 已吊销

    `expires_in` = `min(设备 access TTL, 卡剩余寿命)`：卡还剩 5 分钟时不发 2 小时的
    令牌，否则「再换一次卡」就成了绕过 `expires_at` 的后门（卡只有到期失效，R9）。
    """
    try:
        session_result = await identity_service.exchange_staff_card(
            session, card_token=body.card_token
        )
    except Exception as exc:
        return _handle_staff_card_exception(exc)

    return StaffCardSessionResponse(
        access_token=session_result.access_token,
        scope=list(session_result.scope),
        expires_in=session_result.expires_in,
        device_id=session_result.device_id,
        elder_id=session_result.elder_id,
    )


# 子 router 的路由在**定义完之后**才能合并进主 router（装饰器是同步执行的）。
router.include_router(_staff_card_router)


# ---------------------------------------------------------------------------
# E4b：POST /api/device-sessions/voiceprint — 未登录用声纹换会话（task-71）
# ---------------------------------------------------------------------------


def _handle_voiceprint_login_exception(exc: Exception) -> JSONResponse:
    """声纹登录的异常映射：业务异常按自带 status/code，非法音频 422，其余 500。

    这里与 `_handle_service_exception` 分开写，是因为那套映射绑定的是
    `auth_service` 的异常族（401/409 语义都不同）；混在一起会让「设备令牌错误」和
    「声纹不匹配」共用一个码，前端没法分支。
    """
    trace_id = get_trace_id() or "-"
    if isinstance(exc, voiceprint_login_service.VoiceprintLoginError):
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(detail=exc.detail, code=exc.code, trace_id=trace_id).model_dump(),
        )
    if isinstance(exc, VoiceprintInvalidAudio):
        # 容器判定的音频非法（4xx）：与 C4 注册端点同一口径，回 422。
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                detail=str(exc), code="VALIDATION_ERROR", trace_id=trace_id
            ).model_dump(),
        )
    logger.exception("unexpected voiceprint login error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            detail="内部错误", code="INTERNAL_ERROR", trace_id=trace_id
        ).model_dump(),
    )


@router.post(
    "/device-sessions/voiceprint",
    response_model=DeviceLoginResponse,
    summary="声纹识别登录（无需预先持有设备令牌）",
)
async def voiceprint_device_session(
    session: Annotated[AsyncSession, Depends(get_session)],
    device_id: Annotated[uuid.UUID, Form(description="设备 ID（与 E4 用的是同一个 device_id）")],
    audio: Annotated[UploadFile, File(description="16kHz/16bit/单声道 WAV，≤10s")],
) -> DeviceLoginResponse | JSONResponse:
    """用一段声纹换设备会话 —— 登录页「说句话就进去」的后端一半。

    **请求格式：multipart/form-data**，两个字段：
    - `device_id`（文本，UUID）
    - `audio`（文件，16kHz/16bit/单声道 WAV，≤10s / ≤350KB）

    选 multipart 而不是 JSON+base64 的理由：10s 音频约 320KB，base64 会再涨 33%，
    而且浏览器录音本来拿到的就是 Blob，`FormData` 直接塞；多一道编码只多一处坏点。

    **响应与 E4 `/api/device-sessions` 逐字段一致**
    （`access_token` / `expires_in` / `device{id, room_id, elder_id}`），前端登录成功
    之后的分支只写一套；会话令牌同为 24h 设备 JWT（同一 `sign_device_session`）。

    错误码（语义见 `app/services/voiceprint_login_service.py`）：
    - 409 `voiceprint_not_enrolled`：设备不存在 / 未绑长者 / 没有声纹样本 —— **同一个码
      同一句话**，不给 device_id 枚举的入口；
    - 401 `voiceprint_mismatch`：比对未达阈值（响应里**没有**相似度数值）；
    - 403 `device_locked`：设备被管理员锁定；
    - 503 `voiceprint_unavailable`：推理容器或声纹配置暂不可用；
    - 413 `PAYLOAD_TOO_LARGE` / 422 `VALIDATION_ERROR`：音频超限或容器判为非法音频。
    """
    if not get_settings().voiceprint_login_enabled:
        # 2026-11-13 产品负责人裁决：取消声纹免登录。开关键见 Settings.voiceprint_login_enabled。
        return JSONResponse(
            status_code=403,
            content=ErrorResponse(
                detail="声纹登录已停用，请用设备编号与密钥登录",
                code="voiceprint_login_disabled",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    # 体积闸：只读上限 +1 字节，超出即拒（chunked 请求没有 Content-Length，实读长度是
    # 唯一可靠判据）。与 C4 注册端点共用同一个上限常量，不写第二个 350_000。
    raw = await audio.read(MAX_SAMPLE_AUDIO_BYTES + 1)
    if len(raw) > MAX_SAMPLE_AUDIO_BYTES:
        logger.warning(
            "voiceprint login rejected: body too large (read=%d, limit=%d)",
            len(raw),
            MAX_SAMPLE_AUDIO_BYTES,
        )
        return JSONResponse(
            status_code=413,
            content=ErrorResponse(
                detail="音频文件过大（请上传 ≤10s 的 WAV）",
                code=PAYLOAD_TOO_LARGE,
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )
    if not raw:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                detail="音频文件为空",
                code="VALIDATION_ERROR",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    try:
        result = await voiceprint_login_service.login_with_voiceprint(
            session, device_id=device_id, audio_bytes=raw
        )
    except Exception as exc:
        return _handle_voiceprint_login_exception(exc)

    return DeviceLoginResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        device={
            "id": result.device.id,
            "room_id": result.device.room_id,
            "elder_id": result.device.elder_id,
        },
    )
