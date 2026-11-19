"""用药计划与事件 REST（console 端，PRD §4.5 FR-MED-01/02/04）。

## 权限口径（PRD §4.5 第 1 段，逐条可回查）

| 端点 | 可读/可写角色 | 越权处置 |
| --- | --- | --- |
| `GET /api/medication/plans` | doctor / care_worker / admin | 家属与设备 403 `SCOPE_FORBIDDEN`；其它员工角色 403 `ROLE_FORBIDDEN` |
| `POST /api/medication/plans` | **仅 doctor** | 护工/管理员 403 `ROLE_FORBIDDEN`；家属/设备 403 `SCOPE_FORBIDDEN` |
| `PATCH /api/medication/plans/{plan_id}` | **仅 doctor** | 同上 |
| `POST /api/medication/plans/{plan_id}/stop` | **仅 doctor** | 同上 |
| `GET /api/medication/events` | doctor / care_worker / admin | 同上 |
| `POST /api/medication/events/{reminder_id}/confirm` | doctor / care_worker / admin | 同上 |

PRD §4.5 原话是「医生增改停查，护工与管理员仅只读」，FR-MED-01 的验收明写「Given 护工
账号尝试修改药单，Then 服务端 403 并记审计」。因此**越权一律先落 `audit_logs` 再抛 403**：
同事务写审计会被 `get_session` 的异常回滚抹掉，所以走服务层的独立会话写入
（与 `app/services/report_service.py` 的隔离闸同一口径）。

## 「改」与「停」的语义（W1 补齐）

PRD:331 FR-MED-01 与 PRD:508 AC-27 要的医生「改」「停」此前只有文字、没有端点；本文件
补上，口径与服务层一一对应：

| 动作 | 端点 | 语义 | 并发 |
| --- | --- | --- | --- |
| 改 | `PATCH /plans/{plan_id}` | `medication_plans` 同一行 UPDATE、`version` 自增；历史事件 `drug_snapshot` 是 JSONB 快照，不被改写 | 可选 `expected_version`，与库中不符 409 `VERSION_CONFLICT` |
| 停 | `POST /plans/{plan_id}/stop` | `status='stopped'`，**不新增** `cancelled` 事件态；重复停用 200 + `already_stopped` | 天然幂等 |

两条都只 `flush` 不 `commit`（事务归 `get_session`），且**成功也写审计**：
`medication.plan.update` / `medication.plan.stop`，detail 带 `plan_id` /
`version_before` / `version_after` / `changed`（或 `reason`）与 `trace_id`。审计走
独立会话（`audit_service.write_audit_isolated`），端点后续序列化失败也不会把证据回滚。

「改」不接受 `elder_id` / `plan_id` / `created_by` / `version` / `status`：归属由服务端
决定，版本由服务端自增，停用只走专用端点（那是唯一有幂等语义的路径）。

## 分层（AGENTS §2）

本模块只做四件事：参数校验、角色/范围过滤、调 `app/services/medication_service.py`、
序列化。**不出现** `session.add/commit`，**不出现** `select(Model)` —— 所有读写口径都在
服务层。`elder_id` 只来自查询参数或请求体里显式目标长者，且**必须**先经
`elder_service.get_elder()` 过一遍 assignment 范围（范围外 403、不存在 404）：
医生给「自己没负责的长者」开药单不会被静默接受（AGENTS §4.9 的落点——身份不来自客户端，
目标资源仍要过范围校验）。

## 字段口径

列名以 `app/models/medication.py` 为准：计划是 `dose`（不是 dosage）+ 单值
`schedule_cron`（本仓只支持 `HH:MM` 简单形态），事件是 `status`（流转态）+
`terminal_state`（四值终态）。**终态不等于「服药成功」**：`taken_reported` 只是
「已服自报」（PRD §4.5 原文），本模块任何响应与 docstring 都不把它说成已服药。

## 与 B 服务层的对齐点

`medication_service.py` 正在并行落地，函数名与返回视图尚未冻结。本文件把所有服务
调用收在末尾 `_svc_*` 适配块里：其余代码只认这几个适配函数，签名落地后只改那一块
（期望签名清单已随本卡报告提交给 Lead）。

## 幂等与冲突

FR-MED-04：同键异内容 → 409。服务层抛 `MedicationEventConflict` 时本模块映射为
409 `CONFLICT`；参数形状错误由 Pydantic 在进入 handler 之前拦成 422 `VALIDATION_ERROR`。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import audit_service, elder_service
from app.services.elder_service import ElderScopeForbiddenError
from app.services.report_service import _write_audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/medication",
    tags=["medication"],
    route_class=ContractAPIRoute,
)

#: PRD §4.5：药单「医生增改停查，护工与管理员仅只读」。
READING_ROLES: frozenset[str] = frozenset({"doctor", "care_worker", "admin"})
WRITING_ROLES: frozenset[str] = frozenset({"doctor"})
#: 代确认是 console 的人工动作；护工是主路径，医生/管理员同样可代办。
CONFIRMING_ROLES: frozenset[str] = frozenset({"doctor", "care_worker", "admin"})

#: 代确认渠道标识，**由端点固定**，不接受客户端传入（否则审计里的渠道不可信）。
CONFIRM_CHANNEL_BUTTON = "button"

#: 确认后的流转态与终态。`taken_reported` = 「已服自报」（PRD §4.5），不等于已服药。
CONFIRMED_STATUS = "confirmed"
CONFIRMED_TERMINAL_STATE = "taken_reported"

#: 药单版本冲突的错误码（W1 并发口径）：409 + `VERSION_CONFLICT`。与事件内容冲突的
#: `CONFLICT` 分开，前端才能提示「刷新后重试」而不是笼统的「冲突」。
VERSION_CONFLICT_CODE = "VERSION_CONFLICT"

#: 身份未确认（generic）时的错误码（计划 S-D4-2 裁决）：403 而不是 422 —— 缺的是
#: 「你是谁」这个前置，不是请求参数不合法。与 `app/api/health.py` 同名同义。
IDENTITY_REQUIRED_CODE = "IDENTITY_REQUIRED"

#: `schedule_cron` 的受限形态：模型注释写死「本项目只支持 HH:MM，其余跳过并告警」，
#: 所以在 API 边界就挡掉 cron 表达式，避免落库后被调度器静默跳过。
_SCHEDULE_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ------------------------------------------------------------------ 请求 / 响应模型


class MedicationPlanCreateRequest(BaseModel):
    """医生建药单的请求体（FR-MED-01 的「增」）。

    `extra="forbid"`：药单是临床相关数据，多传一个未知字段（例如伪造 `created_by`
    或 `version`）直接 422，而不是被静默忽略后当成「写进去了」。
    """

    model_config = ConfigDict(extra="forbid")

    elder_id: uuid.UUID = Field(description="目标长者（后台管理端显式指定，非模型自报）")
    drug_name: str = Field(min_length=1, max_length=100)
    dose: str = Field(min_length=1, max_length=50, description="单次剂量，如「1 片」「5mg」")
    schedule_cron: str = Field(
        description="每日服药时刻，本仓只支持 `HH:MM`（如 `08:00`）",
        examples=["08:00"],
    )
    start_date: datetime.date
    end_date: datetime.date | None = None

    @field_validator("schedule_cron")
    @classmethod
    def _validate_schedule(cls, value: str) -> str:
        """只收 `HH:MM`：格式坏属于**参数错误**（422），不能留给调度器「跳过并告警」。"""
        if not _SCHEDULE_PATTERN.match(value):
            raise ValueError("schedule_cron 只支持 HH:MM（如 08:00）")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> MedicationPlanCreateRequest:
        """停用日不得早于启用日，否则会造出「永远不生效」的药单而没人发现。"""
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        return self


#: PATCH 允许出现的字段名。`expected_version` 是并发控制字段，不参与「至少改一项」判定。
PLAN_PATCH_FIELDS: frozenset[str] = frozenset(
    {"drug_name", "dose", "schedule_cron", "start_date", "end_date"}
)


class MedicationPlanPatchRequest(BaseModel):
    """医生改药单的请求体（FR-MED-01「改」，AC-27 第一步）。

    - 五个字段全可选，但**至少要传一个**：`{}` 与「只传 `expected_version`」都 422。
      「什么都没改」的成功会产生一条 `changed: []` 的审计噪声，也会掩盖前端接错字段。
    - `extra="forbid"`：`elder_id` / `plan_id` / `created_by` / `version` 一律拒收——
      请求体不能改归属，也不能冒充服务端自增的版本号。
    - `end_date: null` 是**有效输入**（清空停用日 = 改回长期），因此「至少改一项」
      按 `model_fields_set` 判，而不是按值是否为 `None`。
    """

    model_config = ConfigDict(extra="forbid")

    drug_name: str | None = Field(default=None, min_length=1, max_length=100)
    dose: str | None = Field(default=None, min_length=1, max_length=50)
    schedule_cron: str | None = Field(
        default=None,
        description="每日服药时刻，本仓只支持 `HH:MM`（如 `08:00`）",
        examples=["08:00"],
    )
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    expected_version: int | None = Field(
        default=None,
        ge=1,
        description="乐观锁：客户端读到的版本号；与库中不符返回 409 VERSION_CONFLICT",
    )

    @field_validator("schedule_cron")
    @classmethod
    def _validate_schedule(cls, value: str | None) -> str | None:
        if value is not None and not _SCHEDULE_PATTERN.match(value):
            raise ValueError("schedule_cron 只支持 HH:MM（如 08:00）")
        return value

    @model_validator(mode="after")
    def _validate_patch(self) -> MedicationPlanPatchRequest:
        touched = self.model_fields_set & PLAN_PATCH_FIELDS
        if not touched:
            raise ValueError(
                "至少传一个可改字段：drug_name / dose / schedule_cron / start_date / end_date"
            )
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date 不能早于 start_date")
        return self


class MedicationPlanStopRequest(BaseModel):
    """医生停用药单的请求体：只有一句可选原因（进审计与日志，`medication_plans` 无备注列）。"""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=200)


class MedicationEventConfirmRequest(BaseModel):
    """护工代确认的请求体：只允许补一句备注，渠道与终态由端点固定。"""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=200)


class MedicationPlanOut(BaseModel):
    """一条药单的公开投影（不含任何「是否已服」的推断）。"""

    plan_id: uuid.UUID
    elder_id: uuid.UUID
    drug_name: str
    dose: str
    schedule_cron: str
    start_date: datetime.date
    end_date: datetime.date | None = None
    status: str
    version: int = 1
    created_by: uuid.UUID | None = None
    #: 续方来源计划（FR-MED-09）：NULL = 非续方新建。前端据此显示「续自 …」，也让
    #: 审计读得出续方链，不必靠 `start_date` 猜测。
    renewed_from_plan_id: uuid.UUID | None = None


class MedicationPlanListOut(BaseModel):
    items: list[MedicationPlanOut]
    total: int
    elder_id: uuid.UUID


class MedicationEventOut(BaseModel):
    """一条用药提醒事件。

    `status` 是流转态（calling/waiting_confirm/confirmed/escalated），
    `terminal_state` 是四值终态（含 `taken_reported` = 已服自报）。
    """

    reminder_id: uuid.UUID
    plan_id: uuid.UUID
    elder_id: uuid.UUID
    scheduled_at: datetime.datetime
    status: str
    terminal_state: str | None = None
    late_taken: bool = False
    retry_count: int = 0
    confirm_channel: str | None = None
    confirmed_at: datetime.datetime | None = None
    triggered_by: str | None = None


class MedicationEventListOut(BaseModel):
    items: list[MedicationEventOut]
    total: int
    elder_id: uuid.UUID
    status: str | None = None


class MedicationEventConfirmOut(BaseModel):
    """代确认响应。`confirm_channel` 恒为 `button`（端点固定，可回查）。"""

    reminder_id: uuid.UUID
    status: str
    terminal_state: str
    confirm_channel: str
    note: str | None = None
    late_taken: bool = False
    #: 这次确认是否由 HITL 图的 `interrupt` resume 完成：True = 图里有待确认停点，
    #: 服务层把回应打回图、由图落终态；False = 图里没有该事件的停点，走了直写。
    #: **不是成功/失败标志**——两条路都会写库，只有「谁落的库」不同。
    hitl_resumed: bool = False


# ------------------------------------------------------------------ 视图 → 响应


def _fields(view: object) -> Mapping[str, Any]:
    """把服务层视图统一成映射：dataclass / ORM 行 / 已序列化的 dict 三种都认。

    服务层视图字段尚未冻结（B 并行），这里先做一次规范化；签名固化后由 Lead 收敛成
    强类型（直接用 `MedicationPlanView` 当响应模型）。**不做业务推断**：取不到的字段
    一律留空，不猜。
    """
    if isinstance(view, Mapping):
        return view
    if dataclasses.is_dataclass(view) and not isinstance(view, type):
        return dataclasses.asdict(view)
    return {key: value for key, value in vars(view).items() if not key.startswith("_")}


def _plan_out(view: object) -> MedicationPlanOut:
    data = _fields(view)
    return MedicationPlanOut(
        plan_id=data["plan_id"],
        elder_id=data["elder_id"],
        drug_name=data["drug_name"],
        dose=data["dose"],
        schedule_cron=data["schedule_cron"],
        start_date=data["start_date"],
        end_date=data.get("end_date"),
        status=data.get("status") or "active",
        version=int(data.get("version") or 1),
        created_by=data.get("created_by"),
        renewed_from_plan_id=data.get("renewed_from_plan_id"),
    )


def _event_out(view: object) -> MedicationEventOut:
    data = _fields(view)
    reminder_id = data.get("reminder_id") or data.get("event_id")
    if reminder_id is None:
        raise MedicationViewError("medication event view has no reminder_id/event_id")
    return MedicationEventOut(
        reminder_id=reminder_id,
        plan_id=data["plan_id"],
        elder_id=data["elder_id"],
        scheduled_at=data["scheduled_at"],
        status=data.get("status") or "",
        terminal_state=data.get("terminal_state"),
        late_taken=bool(data.get("late_taken")),
        retry_count=int(data.get("retry_count") or 0),
        confirm_channel=data.get("confirm_channel"),
        confirmed_at=data.get("confirmed_at"),
        triggered_by=data.get("triggered_by"),
    )


class MedicationViewError(RuntimeError):
    """服务层视图形状不对（缺主键等）：属接线错误，不是客户端参数问题。"""


# ------------------------------------------------------------------ 权限与审计


def _actor(identity: Identity) -> tuple[str, uuid.UUID]:
    """`(actor_type, actor_id)`：审计两列只从服务端身份取（AGENTS §4.9）。"""
    raw = identity.staff_id or identity.guardian_id or identity.device_id
    actor_id = uuid.UUID(raw) if raw else uuid.UUID(int=0)
    return identity.kind, actor_id


async def _audit_denied(
    identity: Identity,
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    reason: str,
) -> None:
    """越权留痕（FR-MED-01 / AGENTS §8「越权 → 拒绝 + 审计 + trace_id」）。

    写库动作落在 `app/services/report_service._write_audit`（服务层才是唯一写库层），
    本模块只传 actor/action/target。它用**独立会话**提交，所以随后的 403 抛异常不会把
    这条证据一起回滚。审计失败只记日志，不改变 403 这个结论。
    """
    actor_type, actor_id = _actor(identity)
    await _write_audit(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail={
            "trace_id": get_trace_id() or "-",
            "reason": reason,
            "role": identity.role,
            "kind": identity.kind,
        },
    )


async def _audit_success(
    identity: Identity,
    *,
    action: str,
    resource_id: uuid.UUID,
    detail: dict[str, Any],
) -> None:
    """服务层写库成功后补一条审计：改/停药单必须能回答「谁把什么改成了什么」。

    走 `audit_service.write_audit_isolated`：独立会话、自己 commit、失败只返回 False
    不抛异常（审计写不进去不能把已经完成的业务动作反悔）。与越权审计共用
    `audit_logs` 表，靠 `action` 与 detail 里的 `reason` 区分成功/拒绝。
    """
    actor_type, actor_id = _actor(identity)
    await audit_service.write_audit_isolated(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type="medication_plan",
        resource_id=resource_id,
        detail={
            **detail,
            "trace_id": get_trace_id() or "-",
            "role": identity.role,
            "kind": identity.kind,
        },
        trace_id=get_trace_id() or "-",
    )


def _is_generic_identity(identity: Identity) -> bool:
    """通用模式（未确认身份）在 HTTP 层的唯一可观测形态：**设备会话未确认到长者**。

    判定与 `app/api/health.py` 的同名函数逐字一致（本卡只允许改这三个文件，暂不抽公共
    模块，两边各自注释指向对方）：`Identity` 没有 generic 这一档，generic 是对话侧
    `DialogueIdentity.source` 的判定；两者在 API 层的交点是 `devices.elder_id IS NULL`
    —— `identity_service.resolve_on_connect` 正是据此落 generic 线程（reason=
    `elder_unbound`，identity_service.py:252-258）。收窄到 `device`：家属有凭证但没有
    `elder_id`，其拒绝口径必须留给角色/范围，不能被身份守卫顶掉。
    """
    return identity.kind == "device" and identity.elder_id is None


async def _require_roles(
    identity: Identity,
    allowed: frozenset[str],
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
) -> None:
    """先判主体种类、再判角色；两种拒绝都**先写审计**再抛。

    家属/设备拿的是有效凭证但没有 console 角色，按 `elders` 的既有口径归
    `SCOPE_FORBIDDEN`（403）而不是 401 —— 401 会让家属以为要重新登录。
    """
    if identity.kind != "staff":
        await _audit_denied(
            identity,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            reason="non_staff_channel",
        )
        raise ElderScopeForbiddenError("medication console is not reachable by family/device")
    if (identity.role or "") not in allowed:
        await _audit_denied(
            identity,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            reason="role",
        )
        raise RoleForbiddenError("staff role is not allowed for this medication operation")


async def _require_elder_scope(
    session: AsyncSession, identity: Identity, elder_id: uuid.UUID
) -> None:
    """长者存在性 + assignment 范围：完全复用长者服务的既有口径。

    admin 全量；医生/护工按本次操作的 relation 判范围，范围外 403（不泄露存在性），
    admin 查不到才是 404。
    """
    await elder_service.get_elder(session, identity, elder_id)


# ------------------------------------------------------------------ 错误映射


class MedicationServiceGapError(RuntimeError):
    """`medication_service` 尚未落地或缺本项目要用的函数。

    服务层由 B 并行开发，`app/api` 不能因为"还没写完"就整体导入失败（那会让所有人的
    `import app.main` 一起炸）。因此这里**延迟导入**，缺函数时显式化成 503，而不是在
    API 层补一段 `session.add` / `select(Model)` 绕过分层硬约束。
    """


def _svc_module() -> Any:
    """取 `medication_service` 模块；未落地时抛 `MedicationServiceGapError`（→ 503）。"""
    try:
        from app.services import medication_service
    except ImportError as exc:  # pragma: no cover - 只在 B 未交付时触发
        raise MedicationServiceGapError("medication_service 尚未落地") from exc
    return medication_service


def _service_error_types(*names: str) -> tuple[type[BaseException], ...]:
    """按类名从服务模块取异常类型；缺失就跳过（服务签名未冻结时的容错）。"""
    module = _svc_module()
    found: list[type[BaseException]] = []
    for name in names:
        candidate = getattr(module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found.append(candidate)
    return tuple(found)


def _service_error(exc: BaseException) -> JSONResponse:
    """服务层领域异常 → 契约 §7 错误体（503 / 409 / 404 / 422 分档）。"""
    if isinstance(exc, MedicationServiceGapError):
        return _error_response(503, "SERVICE_NOT_READY", "medication_service is not available yet")
    if isinstance(exc, _service_error_types("MedicationVersionConflict")):
        # 同为 409 但码不同：`VERSION_CONFLICT` 的处置是「刷新后重试」，
        # `CONFLICT` 的处置是「同一到点键内容不一致，别重试」。混用会让前端只能含糊提示。
        return _error_response(
            409, VERSION_CONFLICT_CODE, "medication plan was modified concurrently"
        )
    if isinstance(exc, _service_error_types("MedicationEventConflict", "MedicationConflictError")):
        return _error_response(
            409, "CONFLICT", "medication event conflicts with an existing record"
        )
    if isinstance(exc, _service_error_types("MedicationPlanAlreadyRenewed")):
        # 重复续方：不是并发冲突（不需要刷新重试），是「这条已经续过了」。
        return _error_response(409, "CONFLICT", "medication plan has already been renewed")
    if isinstance(
        exc,
        _service_error_types(
            "MedicationPlanNotFoundError",
            "MedicationEventNotFoundError",
            "MedicationNotFoundError",
        ),
    ):
        return _error_response(404, "NOT_FOUND", "medication resource not found")
    if isinstance(
        exc,
        _service_error_types("MedicationValidationError", "MedicationInputError"),
    ):
        return _error_response(422, "VALIDATION_ERROR", "invalid medication request")
    raise exc


# ------------------------------------------------------------------ 端点


@router.get("/plans", response_model=MedicationPlanListOut)
async def list_plans(
    elder_id: Annotated[uuid.UUID, Query(description="目标长者")],
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MedicationPlanListOut | JSONResponse:
    """某长者的药单列表。护工/医生/管理员可读；家属与设备 403 + 审计。"""
    await _require_roles(
        identity,
        READING_ROLES,
        action="medication.plan.read",
        resource_type="medication_plan",
        resource_id=elder_id,
    )
    await _require_elder_scope(session, identity, elder_id)

    try:
        views = await _svc_list_plans(session, identity, elder_id)
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    items = [_plan_out(view) for view in views]
    return MedicationPlanListOut(items=items, total=len(items), elder_id=elder_id)


@router.post("/plans", status_code=201, response_model=MedicationPlanOut)
async def create_plan(
    body: MedicationPlanCreateRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MedicationPlanOut | JSONResponse:
    """医生建药单（FR-MED-01「增」）。**仅 doctor**；护工/管理员/家属一律 403 + 审计。"""
    await _require_roles(
        identity,
        WRITING_ROLES,
        action="medication.plan.create",
        resource_type="medication_plan",
        resource_id=body.elder_id,
    )
    await _require_elder_scope(session, identity, body.elder_id)

    try:
        view = await _svc_create_plan(session, identity, body.elder_id, body.model_dump())
    except Exception as exc:
        return _service_error(exc)
    return _plan_out(view)


@router.patch("/plans/{plan_id}", response_model=MedicationPlanOut)
async def update_plan(
    plan_id: uuid.UUID,
    body: MedicationPlanPatchRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MedicationPlanOut | JSONResponse:
    """医生改药单（FR-MED-01「改」，AC-27 第一步）。**仅 doctor**；越权 403 + 审计。

    三个口径：

    1. **归属只能反查**：路径参数只有 `plan_id`，`elder_id` 从库里读出来后才过 assignment
       范围闸（不存在 404、范围外 403），请求体里没有 `elder_id` 可传。
    2. **版本由服务端自增**：客户端只发 `expected_version` 做比对；不符 → 409
       `VERSION_CONFLICT`（不是 400，也不是静默覆盖）。
    3. **成功也写审计**：`medication.plan.update`，detail 带 `version_before` /
       `version_after` / `changed`，没有这条记录「谁改了剂量」就查不出来。
    """
    await _require_roles(
        identity,
        WRITING_ROLES,
        action="medication.plan.update",
        resource_type="medication_plan",
        resource_id=plan_id,
    )

    try:
        plan = await _svc_load_plan(session, plan_id)
    except Exception as exc:
        return _service_error(exc)
    await _require_elder_scope(session, identity, plan.elder_id)

    payload = body.model_dump(exclude_unset=True)
    expected_version = payload.pop("expected_version", None)
    version_before = int(plan.version)
    before = {field: getattr(plan, field) for field in payload}

    try:
        plan = await _svc_update_plan(
            session, plan=plan, changes=payload, expected_version=expected_version
        )
    except Exception as exc:
        return _service_error(exc)

    changed = sorted(field for field, old in before.items() if getattr(plan, field) != old)
    await _audit_success(
        identity,
        action="medication.plan.update",
        resource_id=plan.plan_id,
        detail={
            "plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "version_before": version_before,
            "version_after": int(plan.version),
            "changed": changed,
            "fields": sorted(before),
        },
    )
    return _plan_out(plan)


@router.post("/plans/{plan_id}/stop", response_model=None)
async def stop_plan(
    plan_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: MedicationPlanStopRequest | None = None,
) -> MedicationPlanOut | dict[str, bool] | JSONResponse:
    """医生停用药单（FR-MED-01「停」，AC-27 第二步）。**仅 doctor**；越权 403 + 审计。

    幂等（产品负责人口径）：首次停用 → 200 + 药单对象；已停用再停 → 200 +
    `{"already_stopped": true}`，不重复改状态、不动 `version`。

    停用**不**改任何历史事件：已经发出去、还在等确认的提醒照旧走完（不静默丢弃）；
    还没触达的那条由调度器 `decide_event_action` 判 `skip`（
    `medication_skipped_total{reason=plan_stopped}`），且 `run_medication_scan` 只会为
    `status='active'` 的计划生成新事件，所以停用后的次日不会再冒出提醒。
    """
    await _require_roles(
        identity,
        WRITING_ROLES,
        action="medication.plan.stop",
        resource_type="medication_plan",
        resource_id=plan_id,
    )

    try:
        plan = await _svc_load_plan(session, plan_id)
    except Exception as exc:
        return _service_error(exc)
    await _require_elder_scope(session, identity, plan.elder_id)

    already_stopped = plan.status == "stopped"
    version_before = int(plan.version)
    reason = body.reason if body is not None else None

    try:
        plan = await _svc_stop_plan(session, plan=plan, reason=reason)
    except Exception as exc:
        return _service_error(exc)

    await _audit_success(
        identity,
        action="medication.plan.stop",
        resource_id=plan.plan_id,
        detail={
            "plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "version_before": version_before,
            "version_after": int(plan.version),
            "reason": reason,
            "already_stopped": already_stopped,
        },
    )
    if already_stopped:
        return {"already_stopped": True}
    return _plan_out(plan)


@router.post(
    "/plans/{plan_id}/renew",
    response_model=MedicationPlanOut,
    status_code=201,
)
async def renew_plan(
    plan_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MedicationPlanOut | JSONResponse:
    """医生一键续方（FR-MED-09）。**仅 doctor**；越权 403 + 审计。

    语义是**新开一条计划**而不是改旧行：`start_date = 旧 end_date + 1`、
    `end_date = start + 30`，`renewed_from_plan_id` 指回旧计划。旧计划一行不改，
    仍旧按自己的 `end_date` 自然到期——到期是时间事实，不是状态，不给它加
    `status` 新值（那会连带改掉调度与前端的状态口径）。

    返回 201 + **新**计划对象（前端拿它的 `plan_id` 继续操作，旧计划仍在列表里）。

    - 归属只能反查：`elder_id` 先从旧计划读出，再走 assignment 范围闸。
    - 无可续窗口的四种拒绝：长期医嘱（`end_date` 为空）、已停用、已过期太早？
      不——已过期也允许续（`start_date` 仍取旧 `end_date + 1`，只是新计划第一天就在
      过去，调度按 `start_date <= today` 立即开始生成事件，正是「补开」的语义）；
      真正拒绝的是已停用与长期医嘱，以及**已续过**（409，重复点按钮不会开出第二条）。
    - 成功写审计 `medication.plan.renew`：`resource_id` 是新计划，detail 里同时带
      旧计划与两段窗口，续方链在审计里读得出来。
    """
    await _require_roles(
        identity,
        WRITING_ROLES,
        action="medication.plan.renew",
        resource_type="medication_plan",
        resource_id=plan_id,
    )

    try:
        plan = await _svc_load_plan(session, plan_id)
    except Exception as exc:
        return _service_error(exc)
    await _require_elder_scope(session, identity, plan.elder_id)

    try:
        renewed = await _svc_renew_plan(session, plan=plan, renewed_by=identity.staff_id)
    except Exception as exc:
        return _service_error(exc)

    await _audit_success(
        identity,
        action="medication.plan.renew",
        resource_id=renewed.plan_id,
        detail={
            "plan_id": str(renewed.plan_id),
            "renewed_from_plan_id": str(plan.plan_id),
            "elder_id": str(renewed.elder_id),
            "drug_name": renewed.drug_name,
            "previous_end_date": plan.end_date.isoformat() if plan.end_date else None,
            "start_date": renewed.start_date.isoformat(),
            "end_date": renewed.end_date.isoformat() if renewed.end_date else None,
        },
    )
    return _plan_out(renewed)


@router.get("/events", response_model=MedicationEventListOut)
async def list_events(
    elder_id: Annotated[uuid.UUID, Query(description="目标长者")],
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[
        str | None, Query(description="按流转态过滤：calling/waiting_confirm/confirmed/escalated")
    ] = None,
) -> MedicationEventListOut | JSONResponse:
    """某长者的用药事件列表（只读）。四次触达与终态由调度与子图写，这里只查。"""
    await _require_roles(
        identity,
        READING_ROLES,
        action="medication.event.read",
        resource_type="medication_event",
        resource_id=elder_id,
    )
    await _require_elder_scope(session, identity, elder_id)

    try:
        views = await _svc_list_events(session, identity, elder_id, status)
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    items = [_event_out(view) for view in views]
    return MedicationEventListOut(items=items, total=len(items), elder_id=elder_id, status=status)


@router.post("/events/{reminder_id}/confirm", response_model=MedicationEventConfirmOut)
async def confirm_event(
    reminder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: MedicationEventConfirmRequest | None = None,
) -> MedicationEventConfirmOut | JSONResponse:
    """护工在 console 代确认（HITL resume 入口）。

    三条口径：

    1. **渠道固定**：`confirm_channel='button'` 由本端点写死，客户端传的渠道不采信
       （审计里「谁通过什么渠道确认」必须可信）。
    2. **终态**：`terminal_state='taken_reported'` —— 「已服自报」，PRD §4.5 明写
       **不代表实际服药**；`status` 同步流转到 `confirmed`。
    3. **范围**：事件归属哪个长者只能由服务层从 `reminder_id` 反查，因此 assignment
       范围校验在服务层完成（本端点没有 `elder_id` 可校验，也不允许客户端指定）。

    4. **先接图、接不上才直写**：该 `reminder_id` 对应的图若正停在 `wait_confirm`
       的 `interrupt` 上，服务层把 `{"decision": "confirmed", "channel": "button"}`
       打回去让图自己跑完（终态、触达记录、后续升级都由图裁决），响应
       `hitl_resumed=True`；图里没有停点（调度器还没接上、或这条事件不是从图起的）
       才走直写，`hitl_resumed=False`。**两条路都不在 API 层写 ORM**。

    通用模式（FR-VPR-03 / AC-28）**不确认用药**：守卫挡在角色判定之前 —— generic 不是
    「角色不够」，而是「还不知道是谁」，所以既不走 `_audit_denied`（不写审计，generic
    不该产生个人数据痕迹），也不会读事件、碰库。
    """
    if _is_generic_identity(identity):
        return _error_response(403, IDENTITY_REQUIRED_CODE, "请先确认身份，再确认用药")

    await _require_roles(
        identity,
        CONFIRMING_ROLES,
        action="medication.event.confirm",
        resource_type="medication_event",
        resource_id=reminder_id,
    )

    note = body.note if body is not None else None
    try:
        view, hitl_resumed = await _svc_confirm_event(session, identity, reminder_id, note=note)
    except Exception as exc:
        return _service_error(exc)

    status = CONFIRMED_STATUS
    terminal_state = CONFIRMED_TERMINAL_STATE
    late_taken = False
    if view is not None:
        data = _fields(view)
        status = data.get("status") or status
        terminal_state = data.get("terminal_state") or terminal_state
        late_taken = bool(data.get("late_taken"))
    return MedicationEventConfirmOut(
        reminder_id=reminder_id,
        status=status,
        terminal_state=terminal_state,
        confirm_channel=CONFIRM_CHANNEL_BUTTON,
        note=note,
        late_taken=late_taken,
        hitl_resumed=hitl_resumed,
    )


# ------------------------------------------------------------------ 服务层适配块
#
# 只在这里出现对 `medication_service` 的调用。B 的服务签名落地后，若与下面期望的
# 形状不同，只需要改这四个函数体（报告里已列出期望签名）。


async def _svc_list_plans(
    session: AsyncSession, identity: Identity, elder_id: uuid.UUID
) -> list[Any]:
    """对齐 `medication_service.list_plans(session, *, elder_id, status=None)`。

    服务层不认识身份（W8 定盘口径：角色与范围在 API 层判），因此这里**不再**把
    `identity` 传下去；它留在形参里只是为了让调用点保持「一切经适配块」的形状。
    """
    fn = getattr(_svc_module(), "list_plans", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.list_plans 尚未落地")
    return list(await fn(session, elder_id=elder_id))


async def _svc_create_plan(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    payload: Mapping[str, Any],
) -> Any:
    """对齐 `medication_service.create_plan(session, *, elder_id, drug_name, dose,
    schedule_cron, start_date, created_by, end_date=None)`。

    `created_by` 由这里从 `identity.staff_id` 取死：责任链起点只能来自已验证的身份，
    绝不让请求体自带「创建人」字段（那等于让人自己签自己的名）。
    """
    fn = getattr(_svc_module(), "create_plan", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.create_plan 尚未落地")
    return await fn(
        session,
        elder_id=elder_id,
        drug_name=payload.get("drug_name"),
        dose=payload.get("dose"),
        schedule_cron=payload.get("schedule_cron"),
        start_date=payload.get("start_date"),
        end_date=payload.get("end_date"),
        created_by=identity.staff_id,
    )


async def _svc_list_events(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    status: str | None,
) -> list[Any]:
    """对齐 `medication_service.list_events(session, *, elder_id, status=None, limit=...)`。"""
    fn = getattr(_svc_module(), "list_events", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.list_events 尚未落地")
    return list(await fn(session, elder_id=elder_id, status=status))


async def _svc_confirm_event(
    session: AsyncSession,
    identity: Identity,
    reminder_id: uuid.UUID,
    *,
    note: str | None,
) -> tuple[Any, bool]:
    """对齐 `medication_service` 已落地的签名：

    - `load_event(session, reminder_id) -> MedicationEvent`（范围校验要靠它反查 elder_id）
    - `confirm_event(session, *, event, channel, graph=None, triggered_by=None)
      -> tuple[MedicationEvent, bool]`（bool = 是否 resume 了 HITL 图）

    渠道在这里显式传死，服务层不得自己推断；`note` 只进响应，不落库
    （`medication_events` 没有备注列）。
    """
    module = _svc_module()
    load_event = getattr(module, "load_event", None)
    confirm = getattr(module, "confirm_event", None)
    if load_event is None or confirm is None:
        raise MedicationServiceGapError("medication_service.load_event/confirm_event 尚未落地")

    event = await load_event(session, reminder_id)
    # 事件归属的长者只能从库里反查，客户端无法指定 —— 用服务层读到的事实过范围闸。
    await _require_elder_scope(session, identity, event.elder_id)

    return cast(
        tuple[Any, bool], await confirm(session, event=event, channel=CONFIRM_CHANNEL_BUTTON)
    )


async def _svc_load_plan(session: AsyncSession, plan_id: uuid.UUID) -> Any:
    """对齐 `medication_service.load_plan(session, plan_id) -> MedicationPlan`。

    客户端只给 `plan_id`，归属与当前版本都必须从这一行读出来——先 `session.get` 再
    判范围/版本，是这两个写端点唯一能守住 AGENTS §4.9 的路径。
    """
    fn = getattr(_svc_module(), "load_plan", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.load_plan 尚未落地")
    return await fn(session, plan_id)


async def _svc_update_plan(
    session: AsyncSession,
    *,
    plan: Any,
    changes: Mapping[str, Any],
    expected_version: int | None,
) -> Any:
    """对齐 `medication_service.update_plan(session, *, plan, changes, expected_version=None)`。"""
    fn = getattr(_svc_module(), "update_plan", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.update_plan 尚未落地")
    return await fn(session, plan=plan, changes=changes, expected_version=expected_version)


async def _svc_stop_plan(session: AsyncSession, *, plan: Any, reason: str | None) -> Any:
    """对齐 `medication_service.stop_plan(session, *, plan, reason=None)`。"""
    fn = getattr(_svc_module(), "stop_plan", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.stop_plan 尚未落地")
    return await fn(session, plan=plan, reason=reason)


async def _svc_renew_plan(session: AsyncSession, *, plan: Any, renewed_by: str | None) -> Any:
    """对齐 `medication_service.renew_plan(session, *, plan, renewed_by=None)`。

    `renewed_by` 从 `identity.staff_id` 取死（同 `created_by`）：续出来的新计划
    `created_by` 记的是**按按钮的这位医生**，不是原计划的作者；责任链才不断。

    `Identity.staff_id` 是 JWT 里的**字符串**，服务层要的是 UUID：在这里转，不让服务层
    为了迁就上游而放宽类型。取不到身份时传 None（服务层沿用旧计划的开单医生）。
    """
    fn = getattr(_svc_module(), "renew_plan", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.renew_plan 尚未落地")
    renewed_by_id = uuid.UUID(renewed_by) if renewed_by else None
    return await fn(session, plan=plan, renewed_by=renewed_by_id)


# ------------------------------------------------------------------ FR-MED-07 批量导入


class MedicationImportPlanOut(BaseModel):
    """一行将被创建（`dry_run=true`）或已创建的药单。"""

    line: int
    plan_id: uuid.UUID | None = None
    elder_id: uuid.UUID
    elder_name: str = ""
    drug_name: str
    dose: str
    schedule_cron: str
    start_date: datetime.date
    end_date: datetime.date | None = None


class MedicationImportIssueOut(BaseModel):
    """失败行：`line=0` 表示整文件级问题（空文件、超行数上限、表头缺列）。"""

    line: int
    field: str
    message: str


class MedicationImportSkippedOut(BaseModel):
    """合法但重复而被跳过的行：不写库，但不算失败。"""

    line: int
    drug_name: str
    reason: str
    message: str
    plan_id: uuid.UUID | None = None


class MedicationImportOut(BaseModel):
    """批量导入结果。

    刻意用 200 而不是 422 表达「行级校验没过」：契约里的错误体只有
    `detail/code/trace_id`，装不下「第几行、哪个字段、为什么」，而 console 要的正
    是一张能逐行修的失败表。`accepted=false` 时**一行都没写**（全量预检不过即整体
    不写，不做半截导入）；真正的请求格式/权限问题仍然走 `ContractAPIRoute` 的 4xx。
    """

    accepted: bool
    dry_run: bool
    all_or_nothing: bool = True
    total_rows: int
    will_create: int
    created: list[MedicationImportPlanOut] = Field(default_factory=list)
    skipped: list[MedicationImportSkippedOut] = Field(default_factory=list)
    errors: list[MedicationImportIssueOut] = Field(default_factory=list)


class MedicationMergedDrugOut(BaseModel):
    plan_id: uuid.UUID
    drug_name: str
    dose: str


class MedicationMergedSlotOut(BaseModel):
    """同一时刻的多味药合成一张卡片（多药合并卡片的服务端口径）。"""

    schedule_cron: str
    on_date: datetime.date
    drug_count: int
    summary: str
    drugs: list[MedicationMergedDrugOut]


class MedicationMergedOut(BaseModel):
    elder_id: uuid.UUID
    on_date: datetime.date
    total_slots: int
    slots: list[MedicationMergedSlotOut]


class AttentionSignalOut(BaseModel):
    """一条「建议关注」信号。`level` 只有 attention 一档，没有诊断分级。"""

    code: str
    title: str
    detail: str
    suggestion: str
    level: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime.datetime | None = None


class AttentionReportOut(BaseModel):
    """聚合预警报告（FR-HLT-10）：只提示护工/医生关注，不构成任何诊断。"""

    elder_id: uuid.UUID
    generated_at: datetime.datetime
    window_days: int
    rule_version: str
    has_attention: bool
    codes: list[str]
    disclaimer: str
    signals: list[AttentionSignalOut] = Field(default_factory=list)


def _import_plan_out(item: Any, *, dry_run: bool) -> MedicationImportPlanOut:
    """预检阶段拿到的是 `ImportRow`，写入后是 `ImportedPlan`（多一层 `.plan`）。"""
    if dry_run:
        return MedicationImportPlanOut(
            line=item.line,
            plan_id=None,
            elder_id=item.elder_id,
            elder_name=item.elder_name,
            drug_name=item.drug_name,
            dose=item.dose,
            schedule_cron=item.schedule_cron,
            start_date=item.start_date,
            end_date=item.end_date,
        )
    plan = item.plan
    return MedicationImportPlanOut(
        line=item.line,
        plan_id=plan.plan_id,
        elder_id=plan.elder_id,
        elder_name=item.elder_name,
        drug_name=plan.drug_name,
        dose=plan.dose,
        schedule_cron=plan.schedule_cron,
        start_date=plan.start_date,
        end_date=plan.end_date,
    )


def _import_issue_out(issue: Any) -> MedicationImportIssueOut:
    return MedicationImportIssueOut(line=issue.line, field=issue.field, message=issue.message)


def _import_skip_out(skip: Any) -> MedicationImportSkippedOut:
    return MedicationImportSkippedOut(
        line=skip.line,
        drug_name=skip.drug_name,
        reason=skip.reason,
        message=skip.message,
        plan_id=skip.plan_id,
    )


def _import_preview_out(preview: Any, *, dry_run: bool) -> MedicationImportOut:
    return MedicationImportOut(
        accepted=preview.accepted,
        dry_run=dry_run,
        total_rows=preview.total_rows,
        will_create=preview.created_count,
        created=[_import_plan_out(row, dry_run=True) for row in preview.rows],
        skipped=[_import_skip_out(skip) for skip in preview.skips],
        errors=[_import_issue_out(issue) for issue in preview.issues],
    )


def _import_result_out(result: Any) -> MedicationImportOut:
    return MedicationImportOut(
        accepted=True,
        dry_run=False,
        total_rows=result.total_rows,
        will_create=len(result.created),
        created=[_import_plan_out(item, dry_run=False) for item in result.created],
        skipped=[_import_skip_out(skip) for skip in result.skips],
        errors=[_import_issue_out(issue) for issue in result.issues],
    )


def _merged_out(
    elder_id: uuid.UUID, on_date: datetime.date, slots: Sequence[Any]
) -> MedicationMergedOut:
    return MedicationMergedOut(
        elder_id=elder_id,
        on_date=on_date,
        total_slots=len(slots),
        slots=[
            MedicationMergedSlotOut(
                schedule_cron=slot.schedule_cron,
                on_date=slot.on_date,
                drug_count=slot.drug_count,
                summary=slot.summary,
                drugs=[
                    MedicationMergedDrugOut(
                        plan_id=drug.plan_id,
                        drug_name=drug.drug_name,
                        dose=drug.dose,
                    )
                    for drug in slot.drugs
                ],
            )
            for slot in slots
        ],
    )


def _attention_out(report: Any) -> AttentionReportOut:
    return AttentionReportOut(
        elder_id=report.elder_id,
        generated_at=report.generated_at,
        window_days=report.window_days,
        rule_version=report.rule_version,
        has_attention=report.has_attention,
        codes=list(report.codes),
        disclaimer=report.disclaimer,
        signals=[
            AttentionSignalOut(
                code=signal.code,
                title=signal.title,
                detail=signal.detail,
                suggestion=signal.suggestion,
                level=signal.level,
                evidence=dict(signal.evidence),
                observed_at=signal.observed_at,
            )
            for signal in report.signals
        ],
    )


@router.get("/plans/import/template", response_model=None)
async def download_import_template(
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> Response | JSONResponse:
    """下载批量导入模板（CSV，UTF-8 带 BOM，Excel 双击不乱码）。护工/医生/管理员可下载。"""
    await _require_roles(
        identity,
        READING_ROLES,
        action="medication.import.template",
        resource_type="medication_plan",
        resource_id=uuid.UUID(int=0),
    )
    try:
        payload = _svc_import_template()
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="medication-import-template.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/plans/import", response_model=MedicationImportOut)
async def import_plans(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File(description="CSV 或 XLSX 药单文件")],
    elder_id: Annotated[
        uuid.UUID | None,
        Query(description="页面当前所选长者：行内未写长者时按它归属"),
    ] = None,
    dry_run: Annotated[bool, Query(description="true=只预检不写库")] = False,
) -> MedicationImportOut | JSONResponse:
    """批量导入药单（FR-MED-07）。

    流程：读文件（≤5MB）→ 行级校验（长者存在且在住、药名、剂量、HH:MM、日期区间、
    单次 ≤200 行）→ **全量通过才写库**；任一行不合法则整体不写，失败行的行号 +
    字段 + 原因原样回显（`errors`）。重复行按业务元组
    `(elder_id, 药名, 剂量, 每日时刻, 起始日, 停用日)` 去重，不产生第二条计划。

    只有医生可写（`WRITING_ROLES`）；护工/管理员可下载模板但不能落库。
    """
    await _require_roles(
        identity,
        WRITING_ROLES,
        action="medication.plan.import",
        resource_type="medication_plan",
        resource_id=elder_id or uuid.UUID(int=0),
    )
    if elder_id is not None:
        await _require_elder_scope(session, identity, elder_id)

    limit = _svc_import_max_bytes()
    payload = await file.read(limit + 1)
    if len(payload) > limit:
        return _error_response(413, "PAYLOAD_TOO_LARGE", "import file too large")

    try:
        preview = await _svc_preview_import(session, payload=payload, elder_id=elder_id)
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    # 文件里点名的每一位长者都要过范围闸：医生只能导入自己负责的长者。
    for target in preview.elder_ids:
        await _require_elder_scope(session, identity, target)

    if dry_run or not preview.accepted:
        await _audit_success(
            identity,
            action="medication.plan.import.preview",
            resource_id=elder_id or uuid.UUID(int=0),
            detail={
                "dry_run": dry_run,
                "accepted": preview.accepted,
                "total_rows": preview.total_rows,
                "will_create": preview.created_count,
                "skipped": len(preview.skips),
                "errors": len(preview.issues),
                "elder_ids": [str(item) for item in preview.elder_ids],
            },
        )
        return _import_preview_out(preview, dry_run=dry_run)

    try:
        result = await _svc_apply_import(session, preview=preview, created_by=_actor(identity)[1])
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    await _audit_success(
        identity,
        action="medication.plan.import",
        resource_id=elder_id or uuid.UUID(int=0),
        detail={
            "total_rows": result.total_rows,
            "created": len(result.created),
            "skipped": len(result.skips),
            "elder_ids": [str(item) for item in preview.elder_ids],
        },
    )
    return _import_result_out(result)


@router.get("/plans/merged", response_model=MedicationMergedOut)
async def merged_plan_cards(
    elder_id: Annotated[uuid.UUID, Query(description="目标长者")],
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    on_date: Annotated[
        datetime.date | None,
        Query(description="按哪一天在服算，默认今天（上海）"),
    ] = None,
) -> MedicationMergedOut | JSONResponse:
    """多药合并卡片（FR-MED-07）：把同一位长者同一时刻的多味药并成一张卡。

    只统计启用中且服药区间覆盖 `on_date` 的药单；按每日时刻升序，同一时刻内按药名
    排序，让「08:00 共 3 种药」一次说清，而不是三条各自播报。
    """
    await _require_roles(
        identity,
        READING_ROLES,
        action="medication.plan.read",
        resource_type="medication_plan",
        resource_id=elder_id,
    )
    await _require_elder_scope(session, identity, elder_id)

    try:
        plans = await _svc_list_plans(session, identity, elder_id)
        day = on_date or _svc_today()
        slots = _svc_merge_slots(plans, on_date=day)
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    return _merged_out(elder_id, day, slots)


@router.get("/attention", response_model=AttentionReportOut)
async def attention_signals(
    elder_id: Annotated[uuid.UUID, Query(description="目标长者")],
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AttentionReportOut | JSONResponse:
    """聚合预警信号（FR-HLT-10）：四路信号各自独立出结论，只给「建议关注」。

    四信号：① `mood_score` 连续 ≥2 次 ≤2；② 异常沉默 ≥48h；③ 深夜频繁交互；
    ④ 重复问同一类问题（认知退化信号）。全部落在 `level="attention"` 一档，
    响应里没有、也不允许出现任何诊断结论（AGENTS §4.4）。

    挂在 `/api/medication` 下是登记在案的偏差：本卡写作用域只到用药域 API 文件，
    `app/api/__init__.py` 的路由注册表在作用域之外。
    """
    await _require_roles(
        identity,
        READING_ROLES,
        action="medication.attention.read",
        resource_type="medication_plan",
        resource_id=elder_id,
    )
    await _require_elder_scope(session, identity, elder_id)

    try:
        report = await _svc_attention_report(session, elder_id=elder_id)
    except (ElderScopeForbiddenError, RoleForbiddenError):
        raise
    except Exception as exc:
        return _service_error(exc)

    return _attention_out(report)


def _svc_import_template() -> bytes:
    """对齐 `medication_service.build_import_template_csv() -> bytes`。"""
    fn = getattr(_svc_module(), "build_import_template_csv", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.build_import_template_csv 尚未落地")
    return cast(bytes, fn())


def _svc_import_max_bytes() -> int:
    """单次导入的文件上限，取自服务层常量（不在 API 里另抄一个数字）。"""
    return int(getattr(_svc_module(), "IMPORT_MAX_FILE_BYTES", 5 * 1024 * 1024))


async def _svc_preview_import(
    session: AsyncSession,
    *,
    payload: bytes,
    elder_id: uuid.UUID | None,
) -> Any:
    """对齐 `medication_service.preview_import(session, *, payload, elder_id=None)`。"""
    fn = getattr(_svc_module(), "preview_import", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.preview_import 尚未落地")
    return await fn(session, payload=payload, elder_id=elder_id)


async def _svc_apply_import(
    session: AsyncSession,
    *,
    preview: Any,
    created_by: uuid.UUID,
) -> Any:
    """对齐 `medication_service.apply_import(session, *, preview, created_by)`。"""
    fn = getattr(_svc_module(), "apply_import", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.apply_import 尚未落地")
    return await fn(session, preview=preview, created_by=created_by)


def _svc_today() -> datetime.date:
    """对齐 `medication_service.today_in_shanghai() -> date`（展示口径按上海）。"""
    fn = getattr(_svc_module(), "today_in_shanghai", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.today_in_shanghai 尚未落地")
    return cast(datetime.date, fn())


def _svc_merge_slots(plans: Sequence[Any], *, on_date: datetime.date) -> list[Any]:
    """对齐 `medication_service.merge_active_plans(plans, *, on_date=None)`。"""
    fn = getattr(_svc_module(), "merge_active_plans", None)
    if fn is None:
        raise MedicationServiceGapError("medication_service.merge_active_plans 尚未落地")
    return list(fn(plans, on_date=on_date))


def _alert_module() -> Any:
    """取 `alert_aggregation` 模块；未落地时抛 `MedicationServiceGapError`（→ 503）。"""
    try:
        from app.services import alert_aggregation
    except ImportError as exc:  # pragma: no cover - 只在文件缺失时触发
        raise MedicationServiceGapError("app.services.alert_aggregation 尚未落地") from exc
    return alert_aggregation


async def _svc_attention_report(session: AsyncSession, *, elder_id: uuid.UUID) -> Any:
    """对齐 `alert_aggregation.evaluate_attention_signals(session, *, elder_id, now=None)`。"""
    module = _alert_module()
    fn = getattr(module, "evaluate_attention_signals", None)
    if fn is None:
        raise MedicationServiceGapError("alert_aggregation.evaluate_attention_signals 尚未落地")
    return await fn(session, elder_id=elder_id)


__all__ = [
    "CONFIRMED_STATUS",
    "CONFIRMED_TERMINAL_STATE",
    "CONFIRMING_ROLES",
    "CONFIRM_CHANNEL_BUTTON",
    "IDENTITY_REQUIRED_CODE",
    "PLAN_PATCH_FIELDS",
    "READING_ROLES",
    "VERSION_CONFLICT_CODE",
    "WRITING_ROLES",
    "router",
]
