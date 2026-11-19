"""长者档案 REST 薄层（接口契约 E5–E8）。

只做四件事：Pydantic 校验、接收当前主体、调用 `app/services/elder_service.py`、
把领域异常映射成契约 §7 的响应体。**不拼查询、不做范围过滤**（AGENTS §2）。

契约要求的错误体是 `{detail, code, trace_id}`（§7），而 task-4 只产出带 `code` 的领域异常，
`app/core/errors.py` 尚不存在且不在本卡写作用域内。因此这里用 `ContractAPIRoute`
在路由内统一映射：它在依赖解析与响应生成的同一层捕获异常，`get_session` 的
rollback/commit 语义不受影响；等 task-7 挂载统一错误处理器后可整体撤掉。
同一路由类还按 §7 区分 400（路径参数格式错 / 非法 JSON）与 422（字段校验失败）。

`ContractAPIRoute` 与 `_error_response` 也被 `app/api/devices.py` 复用 —— 本卡只允许改五个
文件，不新建 `app/api/_errors.py`，所以共享件落在这里。
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import Callable, Coroutine
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.auth import AuthError, RoleForbiddenError, check_roles, get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.models.organization import Device, Elder, Persona
from app.models.relation import ElderGuardian
from app.services import audit_service, elder_service, lifecycle_service
from app.services.device_service import (
    DeviceConflictError,
    DeviceNotFoundError,
    DeviceScopeForbiddenError,
)
from app.services.elder_service import (
    ElderDetail,
    ElderNotFoundError,
    ElderScopeForbiddenError,
    ElderValidationError,
)

ElderStatus = Literal["active", "away", "transferred", "archived"]


def _error_response(status_code: int, code: str, detail: str) -> JSONResponse:
    """契约 §7 的错误体；`trace_id` 由中间件写入 contextvar，这里只负责带出。"""
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, "trace_id": get_trace_id() or "-"},
    )


def _is_request_format_error(exc: RequestValidationError) -> bool:
    """区分「请求格式坏」与「字段校验失败」。

    契约 §7 把**路径参数格式错**与**请求体不是合法 JSON** 归为 400 `BAD_REQUEST`，
    其余 Pydantic 字段问题才是 422 `VALIDATION_ERROR`。FastAPI 默认把两者都塞进
    `RequestValidationError`，只能按 `loc` 与 `type` 分辨：

    - `loc[0] == "path"`：路径参数被 Pydantic 解析失败（如 UUID 写成 `not-a-uuid`）；
    - `type == "json_invalid"`：请求体根本不是合法 JSON（不是字段值不合法）。
    """
    for error in exc.errors():
        if error.get("type") == "json_invalid":
            return True
        location = error.get("loc") or ()
        if location[:1] == ("path",):
            return True
    return False


class ContractAPIRoute(APIRoute):
    """把领域异常映射成契约错误体的路由类。

    为什么用路由类而不是 `app.add_exception_handler`：本卡不允许改 `app/main.py`，
    而错误映射必须随 router 一起被 task-7 挂载，不能要求调用方再补一层配置。
    捕获点在 `original_handler` 之外，因此 `get_session` 已经按异常路径完成 rollback。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        # 返回标注跟着 `APIRoute.get_route_handler` 的声明走（`Coroutine[Any, Any, Response]`
        # 而不是更宽的 `Awaitable[Response]`）：override 只能收窄不能放宽，`Awaitable`
        # 覆盖不了父类的 `Coroutine`，mypy 的 `[override]` 会直接判不兼容。
        original_handler = super().get_route_handler()

        async def contract_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RoleForbiddenError:
                return _error_response(403, "ROLE_FORBIDDEN", "forbidden")
            except AuthError as exc:
                # 凭证缺失/失效/被撤销一律 401；码保留 task-4 的分类（TOKEN_*）。
                return _error_response(401, exc.code, "credential rejected")
            except (ElderScopeForbiddenError, DeviceScopeForbiddenError):
                return _error_response(403, "SCOPE_FORBIDDEN", "forbidden")
            except (ElderNotFoundError, DeviceNotFoundError):
                return _error_response(404, "NOT_FOUND", "not found")
            except DeviceConflictError:
                return _error_response(409, "CONFLICT", "conflict")
            except ElderValidationError:
                return _error_response(422, "VALIDATION_ERROR", "invalid request")
            except RequestValidationError as exc:
                if _is_request_format_error(exc):
                    return _error_response(400, "BAD_REQUEST", "malformed request")
                return _error_response(422, "VALIDATION_ERROR", "request validation failed")

        return contract_handler


router = APIRouter(prefix="/api/elders", tags=["elders"], route_class=ContractAPIRoute)


# ------------------------------------------------------------------ 请求 / 响应模型

#: FR-HLT-05：`HH:MM`，时 00–23、分 00–59。与 `elders.checkup_start` 的 CHECK 约束逐字同源。
_CHECKUP_START_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ElderCreateRequest(BaseModel):
    """E5 请求体。`name` 不唯一，允许重名；`persona_id` 可空。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    birth_date: date | None = None
    gender: str | None = Field(default=None, max_length=20)
    persona_id: uuid.UUID | None = None


class ElderPatchRequest(BaseModel):
    """E8 请求体：**只允许**白名单字段；`extra="forbid"` 让 session_seq 等直接 422。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=50)
    birth_date: date | None = None
    gender: str | None = Field(default=None, max_length=20)
    persona_id: uuid.UUID | None = None
    status: ElderStatus | None = None
    #: FR-HLT-05：自定义采集时刻 `HH:MM`；`null` = 清空自定义、回落到全局默认。
    checkup_start: str | None = Field(
        default=None,
        description="自定义采集时刻 HH:MM（00:00–23:59）；传 null 表示回落到全局默认",
    )

    @field_validator("name", "status")
    @classmethod
    def _reject_explicit_null(cls, value: str | None) -> str:
        """`name` / `status` 是 NOT NULL 列：显式传 null 要在 422，不能落到 500。"""
        if value is None:
            raise ValueError("must not be null")
        return value

    @field_validator("checkup_start")
    @classmethod
    def _validate_checkup_start(cls, value: str | None) -> str | None:
        """`HH:MM` 形态 + 取值范围在入口挡掉（`25:00` 这种要 422，不能落到 DB CHECK 500）。

        与 `elders.checkup_start` 的 CHECK 约束同一套正则：即使有人绕过本层直接写库，
        也会被 `ck_elders_checkup_start_format` 挡住，两处口径必须是同一个。
        """
        if value is None:
            return None
        if not _CHECKUP_START_PATTERN.fullmatch(value):
            raise ValueError("checkup_start must look like HH:MM (00:00-23:59)")
        return value


class ElderPersonaRequest(BaseModel):
    """W1 形象选择请求体：`persona_id = null` 表示**解除分配**（回到内置兜底形象）。"""

    model_config = ConfigDict(extra="forbid")

    persona_id: uuid.UUID | None = None


class PersonaOptionResponse(BaseModel):
    """照护台「数字人角色」的一项（W1 · FR-3D-03）。

    列表页与长者档案的形象下拉共用这一份数据：形象、音色、性格、开场白、口吻示例
    都是角色的固定资产；`assigned_elders` 是当前绑定到该角色的长者数。
    """

    persona_id: uuid.UUID
    name: str
    avatar_url: str | None = None
    voice_id: str
    personality: str
    greeting: str
    tone_examples: list[str] = Field(default_factory=list)
    assigned_elders: int = 0


class PersonaOptionListResponse(BaseModel):
    items: list[PersonaOptionResponse]


class ElderCreateResponse(BaseModel):
    """E5 响应：契约只列这四个字段。"""

    elder_id: uuid.UUID
    status: str
    session_seq: int
    created_at: datetime


class ElderDetailResponse(BaseModel):
    """E6 响应：R4 全字段 + `room_no / bed_no`（S-05）+ 采集时刻三件套（FR-HLT-05）。"""

    elder_id: uuid.UUID
    name: str
    birth_date: date | None
    gender: str | None
    persona_id: uuid.UUID | None
    status: str
    session_seq: int
    created_at: datetime
    room_no: str | None
    bed_no: str | None
    #: 长者自己设的值；`None` = 没设过。
    checkup_start: str | None = None
    #: **实际生效**的时刻：`checkup_start or 全局默认`。前端只需要这一个就能显示「几点采」。
    checkup_start_effective: str | None = None
    #: 生效值来源：`elder` = 长者自定义；`global` = 全局默认。
    checkup_start_source: Literal["elder", "global"] | None = None
    #: 绑定设备数（含停用，按 `devices.elder_id` 计）。原页面有这一格但后端一直没给。
    device_count: int = 0
    #: 已绑定的家属数（只数 `status='active'` 的 `elder_guardians` 行）。
    guardian_count: int = 0


class ElderListItemResponse(BaseModel):
    """E7 列表项：不返 `birth_date / gender / persona_id`（S-07）。"""

    elder_id: uuid.UUID
    name: str
    room_no: str | None
    bed_no: str | None
    status: str


class ElderListResponse(BaseModel):
    items: list[ElderListItemResponse]
    total: int
    page: int
    page_size: int


def _detail_response(
    detail: ElderDetail, *, device_count: int = 0, guardian_count: int = 0
) -> ElderDetailResponse:
    return ElderDetailResponse(
        elder_id=detail.elder_id,
        name=detail.name,
        birth_date=detail.birth_date,
        gender=detail.gender,
        persona_id=detail.persona_id,
        status=detail.status,
        session_seq=detail.session_seq,
        created_at=detail.created_at,
        room_no=detail.room_no,
        bed_no=detail.bed_no,
        checkup_start=detail.checkup_start,
        checkup_start_effective=detail.checkup_start_effective,
        checkup_start_source=detail.checkup_start_source,
        device_count=device_count,
        guardian_count=guardian_count,
    )


# ------------------------------------------------------------------ 端点


@router.post(
    "",
    response_model=ElderCreateResponse,
    status_code=201,
    summary="长者建档（仅 admin）",
)
async def create_elder(
    payload: ElderCreateRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderCreateResponse:
    detail = await elder_service.create_elder(
        session,
        identity,
        name=payload.name,
        birth_date=payload.birth_date,
        gender=payload.gender,
        persona_id=payload.persona_id,
    )
    return ElderCreateResponse(
        elder_id=detail.elder_id,
        status=detail.status,
        session_seq=detail.session_seq,
        created_at=detail.created_at,
    )


def _coerce_tone_examples(value: Any) -> list[str]:
    """把 JSONB 里的口吻示例收敛成字符串数组。

    历史行可能存成 dict，ORM 注解也写着 dict；接口只承诺 `list[str]`，边界处收敛一次。
    """
    if isinstance(value, dict):
        value = value.get("items")
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


@router.get(
    "/personas",
    response_model=PersonaOptionListResponse,
    summary="可分配的数字人形象列表（admin / doctor）",
)
async def list_persona_options(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PersonaOptionListResponse:
    """照护台「数字人角色」列表与长者档案形象下拉的数据源（W1 · FR-3D-03）。

    为什么不复用别处的接口：`personas` 没有对外的列表端点，而 pad 侧的 `avatar-config`
    是「按当前设备 + 长者」收敛后的单个结果，拿不到候选集合。写入口仍是下面的
    `PATCH /{elder_id}/persona`；本端点全程只读。

    路径必须声明在 `/{elder_id}` **之前**，否则 "personas" 会被当成 UUID 路径参数匹配走。
    """
    check_roles(identity, ("admin", "doctor"))
    rows = (
        await session.execute(
            select(
                Persona.persona_id,
                Persona.name,
                Persona.avatar_url,
                Persona.voice_id,
                Persona.personality,
                Persona.greeting,
                Persona.tone_examples,
            ).order_by(Persona.persona_id.asc())
        )
    ).all()
    assigned_rows = (
        await session.execute(
            select(Elder.persona_id, func.count(Elder.elder_id))
            .where(Elder.persona_id.is_not(None))
            .group_by(Elder.persona_id)
        )
    ).all()
    assigned = {persona_id: count for persona_id, count in assigned_rows}
    return PersonaOptionListResponse(
        items=[
            PersonaOptionResponse(
                persona_id=row.persona_id,
                name=row.name,
                avatar_url=row.avatar_url,
                voice_id=row.voice_id,
                personality=row.personality,
                greeting=row.greeting,
                tone_examples=_coerce_tone_examples(row.tone_examples),
                assigned_elders=assigned.get(row.persona_id, 0),
            )
            for row in rows
        ]
    )


@router.get(
    "/{elder_id}",
    response_model=ElderDetailResponse,
    summary="长者详情（admin 全量；护工/医生限本人 relation；家属/设备 403）",
)
async def read_elder(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderDetailResponse:
    detail = await elder_service.get_elder(session, identity, elder_id)
    device_count = (
        await session.scalar(
            select(func.count()).select_from(Device).where(Device.elder_id == elder_id)
        )
    ) or 0
    guardian_count = (
        await session.scalar(
            select(func.count())
            .select_from(ElderGuardian)
            .where(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.status == "active",
            )
        )
    ) or 0
    return _detail_response(
        detail, device_count=int(device_count), guardian_count=int(guardian_count)
    )


@router.get("", response_model=ElderListResponse, summary="长者列表（按角色范围过滤）")
async def list_elders(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1, description="页码，默认 1")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，默认 20，上限 100")] = 20,
    status: Annotated[ElderStatus | None, Query(description="按长者状态过滤")] = None,
) -> ElderListResponse:
    result = await elder_service.list_elders(
        session, identity, page=page, page_size=page_size, status=status
    )
    return ElderListResponse(
        items=[
            ElderListItemResponse(
                elder_id=item.elder_id,
                name=item.name,
                room_no=item.room_no,
                bed_no=item.bed_no,
                status=item.status,
            )
            for item in result.items
        ],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.patch(
    "/{elder_id}",
    response_model=ElderDetailResponse,
    summary="长者档案修改（仅 admin；archived 只允许改状态）",
)
async def update_elder(
    elder_id: uuid.UUID,
    payload: ElderPatchRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderDetailResponse:
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    detail = await elder_service.update_elder(session, identity, elder_id, changes)
    return _detail_response(detail)


class ElderCheckoutRequest(BaseModel):
    """退房请求体（整个 body 可省略，省略 = 普通退房）。

    `archive=True` 时房态落 `archived` 而不是 `away`，归档时刻由服务层写进审计
    `detail.archived_at`（`elders` 表没有该列，不改模型/迁移）。
    `reason` 原样进审计：`checkout` / `transfer` / `deceased` 三件事的数据面处置相同。
    """

    archive: bool = False
    reason: str = Field(default="checkout", min_length=1, max_length=64)


class ElderCheckoutResponse(BaseModel):
    """退房回执：两颗「别人验收时要看」的数 + 房态收口结果。

    `devices_unbound` / `staff_cards_revoked` 让调用方（照护台、验收脚本）不必再查库
    就能确认「设备解绑了、旧令牌与工牌都失效了」；`audit_log_id` 是本次退房的取证编号，
    重复退房（什么都没发生）时为 `null`。
    """

    elder_id: uuid.UUID
    elder_status: str | None
    archived: bool
    memories_purged: int
    voiceprints_removed: int
    sessions_closed: int
    relations_removed: int
    devices_unbound: int
    staff_cards_revoked: int
    audit_log_id: uuid.UUID | None


@router.post(
    "/{elder_id}/checkout",
    response_model=ElderCheckoutResponse,
    summary="退房（仅 admin；解绑设备、吊销令牌、置 away，FR-ADM-09）",
)
async def checkout_elder(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: ElderCheckoutRequest | None = None,
) -> ElderCheckoutResponse:
    """一次退房 = 数据面清理 + 设备解绑 + 工牌吊销 + 房态收口 + 一条审计。

    - 200 + 回执（各步计数、`elder_status`、`audit_log_id`）
    - 403 `ROLE_FORBIDDEN`：非 admin（`check_roles` 先拒，与服务层 E5 同口径）
    - 404 `NOT_FOUND`：长者不存在
    - 422 `VALIDATION_ERROR`：`reason` 为空或超长

    **幂等**：已退过的长者再退一次返回计数全 0、`audit_log_id=null`，不写第二条审计。
    这里的顺序是「先 `get_elder` 拿 404/范围，再动库」：`get_elder` 对 admin 是全量，
    对其它角色按范围过滤 —— 即使将来放开非 admin，也不会退到别人管的长者。
    """
    check_roles(identity, ("admin",))
    # 先确认长者存在（契约 §7 要求 404 与 403 分开，不能让「不存在」长成 200 全 0 回执）。
    await elder_service.get_elder(session, identity, elder_id)

    report = await lifecycle_service.checkout_elder(
        session,
        elder_id=elder_id,
        actor_id=uuid.UUID(str(identity.staff_id)),
        actor_type=identity.kind,
        reason=payload.reason if payload is not None else "checkout",
        archive=payload.archive if payload is not None else False,
    )
    return ElderCheckoutResponse(
        elder_id=report.elder_id,
        elder_status=report.elder_status,
        archived=report.archived,
        memories_purged=report.memories_purged,
        voiceprints_removed=report.voiceprints_removed,
        sessions_closed=report.sessions_closed,
        relations_removed=report.relations_removed,
        devices_unbound=report.devices_unbound,
        staff_cards_revoked=report.staff_cards_revoked,
        audit_log_id=report.audit_log_id,
    )


async def _persona_exists(session: AsyncSession, persona_id: uuid.UUID) -> bool:
    """人设是否存在：只读一列主键做外键预校验。

    为什么不交给数据库的外键约束：那会在 `persona_id` 乱填时报 500（`IntegrityError`
    没有走 `ContractAPIRoute` 的映射），而契约 §7 要求这类「字段值不合法」是 422。
    """
    found = await session.scalar(select(Persona.persona_id).where(Persona.persona_id == persona_id))
    return found is not None


@router.patch(
    "/{elder_id}/persona",
    response_model=ElderDetailResponse,
    summary="长者数字人形象分配（admin；doctor 限本人 relation）",
)
async def update_elder_persona(
    elder_id: uuid.UUID,
    payload: ElderPersonaRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderDetailResponse:
    """把「这位长者用哪个数字人形象」落到 `elders.persona_id`（FR-3D-03）。

    **为什么这个写口在 API 层、而不是 `elder_service.update_elder`**：后者是 E8 的
    **全字段管理口**，且硬性只允许 admin（`app/services/elder_service.py:585`）；
    而照护台给在管长者换形象是医生的日常操作，复用 E8 会把「改档案」与「换形象」
    两套权限口径混成一套。本端点只写**一列外键**、没有领域不变式，读路径仍全部走
    服务层：`elder_service.get_elder` 负责范围过滤（医生对非本人 relation 的长者 403，
    不存在 404），审计与业务写在同一事务里（`audit_service.write_audit` 只 flush，
    由 `get_session` 在请求成功时统一提交）。

    `persona_id = null` 是合法请求：解除分配后 pad 侧回到内置兜底形象
    （`persona_service.get_avatar_config` 的四段回落链）。已归档长者不允许换形象，
    与 E8「archived 只允许改状态」同口径。
    """
    check_roles(identity, ("admin", "doctor"))
    detail = await elder_service.get_elder(session, identity, elder_id)
    if detail.status == "archived":
        raise ElderValidationError("archived elder keeps its persona")

    persona_id = payload.persona_id
    if persona_id is not None and not await _persona_exists(session, persona_id):
        raise ElderValidationError("persona not found")

    await session.execute(
        update(Elder).where(Elder.elder_id == elder_id).values(persona_id=persona_id)
    )
    await audit_service.write_audit(
        session,
        actor_type=identity.kind,
        actor_id=uuid.UUID(str(identity.staff_id)),
        action="elder.persona.update",
        resource_type="elder",
        resource_id=elder_id,
        detail={
            "from_persona_id": str(detail.persona_id) if detail.persona_id else None,
            "to_persona_id": str(persona_id) if persona_id else None,
        },
    )
    # 响应直接由「改之前读到的详情」换掉 persona_id 这一列：同一事务里其它列没动，
    # 不必再查一次（也避开 ORM identity map 拿到旧值的坑）。
    return _detail_response(dataclasses.replace(detail, persona_id=persona_id))


__all__ = [
    "ContractAPIRoute",
    "router",
]
