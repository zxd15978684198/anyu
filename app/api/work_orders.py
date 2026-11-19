"""照护台工单看板（E15/E16/E17）。

依据 PRD §4.7（`docs/安语3D数字人_PRD.md:376-392`）、FR-ALT-02（`:397`，P0，
验收「任一工单可查到完整状态变更历史；看板中红档工单排在普通工单之前」）、
TD §3.4（`docs/安语3D数字人_技术设计文档.md:506`「`work_orders` 工单表 —— 护工
看板数据源」）、TD `:524`（`work_order_transitions` 的必要性）。

## 为什么这三个端点值得单独一个模块

补之前的状态：工单**能建不能动**——`app/services/work_order_service.py` 只有
`create_work_order`，`work_order_transitions` 表建成至今零行写入。工单建出来
就停在 `new`，护工在照护台上看不到它，也没法确认/处理/解决/关闭。FR-ALT-02
的第一条验收「可查完整状态变更历史」按现状无法成立。

## 本模块不做什么

- **不建单**：建单唯一入口是 `app/services/work_order_service.create_work_order`，
  由 `alert_service` / `medication_service` / `app/api/sos.py` 调用。本模块只读 + 流转。
- **不做通知状态机与 2 分钟升级**：那是 FR-ALT-01（通知面），已有
  `notification_service.escalate_unacknowledged`。`risk_events.status` 的
  `escalated` 升级另开一件（见计划 §7 R1）。
- **不接 `staff_tokens.scope`**：工牌换令牌属 迭代 9 的 E14，流转端点只挂动作级
  `require_scopes("workorder:handle")`；DB 侧 `staff_tokens.scope` 的切分口径仍是
  契约 §8 S-22 的留白，改表（text → text[]）是另一件事。

## 权限口径

三个端点都要求员工主体：**读端点与服务层**走
`require_roles("admin","care_worker","doctor")`，**流转端点**（E17）多一层
`require_scopes("workorder:handle")`（FR-ADM-08 的动作级 scope）——
全权员工会话按角色放行，工牌要求卡上确实持有 `workorder:handle`，越 scope → 403
`SCOPE_FORBIDDEN` 且由 `require_scopes` 写一条 `scope_denied` 审计。
**范围过滤在服务层**（`work_order_service._scope_relation` + `_scope_filter`）：
admin 全量，护工/医生按 `elder_staff_assignments.relation` 收敛。与
`app/services/elder_service.py` 的档案范围同源，不看客户端传什么（AGENTS §4.9）。

家属 / 设备 / 工牌一律 401 或 403：工单是照护台内部队列，家属看的是通知与日报。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import ScopeForbiddenError, require_roles, require_scopes
from app.core.runtime_context import Identity
from app.db import get_session
from app.models.work_order import WORK_ORDER_STATUSES
from app.services import work_order_service
from app.services.work_order_service import (
    WorkOrderConflictError,
    WorkOrderNotFoundError,
    WorkOrderScopeForbiddenError,
    WorkOrderValidationError,
)


class WorkOrderAPIRoute(ContractAPIRoute):
    """在 `ContractAPIRoute` 的映射表上补四个工单领域异常。

    为什么是继承而不是改建 `app/api/elders.py` 的 `ContractAPIRoute`：那个类属
    `app/api/elders.py`（本卡写作用域之外），且它的映射表是「档案/设备」那批
    异常的地盘；把工单异常塞进去会让 elders 模块 import work_order_service，
    形成 `api → api` 的横向依赖。继承只新增分支、不改父类行为，父类已有的
    401/403/HTTP 校验分支照旧生效。

    为什么不能不给：不映射的话 `WorkOrderNotFoundError` 会以 500 出去，
    契约 §7 要求 404 `NOT_FOUND`，前端 `isEndpointMissing` 会把 404 当成
    「接口没做」、把 500 当成「后端挂了」——两种误判都会让人查错方向。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def work_order_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except WorkOrderNotFoundError:
                return _error_response(404, "NOT_FOUND", "work order not found")
            except WorkOrderScopeForbiddenError:
                return _error_response(403, "SCOPE_FORBIDDEN", "forbidden")
            except WorkOrderConflictError as exc:
                # `detail` 带出服务层的原因（例：「event_id 与工单关联的风险事件不一致」）：
                # 前端要把原因原样告诉护工，只说 "conflict" 等于没说。
                return _error_response(409, "CONFLICT", str(exc) or "conflict")
            except WorkOrderValidationError:
                return _error_response(422, "VALIDATION_ERROR", "invalid request")
            except ScopeForbiddenError:
                # 工牌 scope 门禁（ADM-08）：401 是「凭证问题」，这里必须是 403，
                # 且 audit_logs 已由 `require_scopes` 用独立会话写好一条 scope_denied。
                return _error_response(403, "SCOPE_FORBIDDEN", "forbidden")

        return work_order_handler


router = APIRouter(prefix="/api/work-orders", tags=["work-orders"], route_class=WorkOrderAPIRoute)

#: 看板三个角色 —— 与 `docs/接口契约_身份与权限.md` 的 role 枚举一致。
_WORK_ORDER_ROLES: tuple[str, ...] = ("admin", "care_worker", "doctor")


# ------------------------------------------------------------------ 响应模型


class WorkOrderItemResponse(BaseModel):
    """看板一行。`elder_name` 已脱敏（姓 + `*` + 末字）。"""

    order_id: uuid.UUID
    order_no: str
    elder_id: uuid.UUID
    elder_name: str
    type: str
    severity: str
    status: str
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    false_alarm: bool
    note: str | None
    created_at: datetime.datetime
    closed_at: datetime.datetime | None


class WorkOrderListResponse(BaseModel):
    items: list[WorkOrderItemResponse]
    total: int
    page: int
    page_size: int


class TransitionResponse(BaseModel):
    """一条状态变更历史。`actor_name` 为 `None` 时前端显示「系统」。"""

    transition_id: uuid.UUID
    from_status: str
    to_status: str
    actor_id: uuid.UUID | None
    actor_name: str | None
    at: datetime.datetime
    note: str | None


class WorkOrderDetailResponse(WorkOrderItemResponse):
    """详情 = 列表项 + 完整流转历史 + 关联风险事件状态。"""

    transitions: list[TransitionResponse]
    event_id: uuid.UUID | None
    event_status: str | None
    false_alarm_reason: str | None
    actual_handled_at: datetime.datetime | None


class TransitionRequest(BaseModel):
    """E17 请求体。

    `extra="forbid"`：多传一个字段就是调用方写错了，不该被静默忽略（与
    `DeviceBindRequest` / `MedicationConfirmRequest` 同口径）。

    `to_status` 用 `Literal` 而不是自由字符串：非法状态名在**请求校验**阶段就
    422，不进服务层；合法状态名但非法迁移才由服务层给 409——两个错误级别不同
    （「写错了」vs「当前状态不允许」），不该混成同一个响应。
    """

    model_config = ConfigDict(extra="forbid")

    to_status: str = Field(max_length=32)
    note: str | None = Field(default=None, max_length=2000)
    false_alarm: bool = False
    false_alarm_reason: str | None = Field(default=None, max_length=500)
    actual_handled_at: datetime.datetime | None = None
    #: 关单时顺带关闭关联风险事件（第五轮 FR-ALT-02「同一事务复合关闭」）。
    #: 只有 `to_status="closed"` 时允许携带；服务端校验它必须等于工单当前的
    #: `event_id`，任何不一致都整笔回滚，故不可能出现「单关了、风险还开着」。
    event_id: uuid.UUID | None = None


# ------------------------------------------------------------------ 序列化


def _item(order: work_order_service.WorkOrderListItem) -> WorkOrderItemResponse:
    return WorkOrderItemResponse(
        order_id=order.order_id,
        order_no=order.order_no,
        elder_id=order.elder_id,
        elder_name=order.elder_name,
        type=order.type,
        severity=order.severity,
        status=order.status,
        assignee_id=order.assignee_id,
        assignee_name=order.assignee_name,
        false_alarm=order.false_alarm,
        note=order.note,
        created_at=order.created_at,
        closed_at=order.closed_at,
    )


# ------------------------------------------------------------------ 端点


@router.get("", response_model=WorkOrderListResponse)
async def list_work_orders(
    session: Annotated[Any, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_WORK_ORDER_ROLES))],
    status: Annotated[
        str | None,
        Query(description="留空 = 未处理四态（new/acknowledged/in_progress/reopened）"),
    ] = None,
    severity: Annotated[str | None, Query(description="red / yellow")] = None,
    elder_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> WorkOrderListResponse:
    """E15：工单看板。默认未处理，**红档在 SQL 里排在最前**（计划 §4 D1）。

    排序为什么在服务端：有分页时前端只能排当前页，第 2 页的红档永远不会置顶。
    """
    if status is not None and status not in WORK_ORDER_STATUSES:
        raise WorkOrderValidationError(f"unknown status {status!r}")
    result = await work_order_service.list_work_orders(
        session,
        identity,
        status=status,
        severity=severity,
        elder_id=elder_id,
        page=page,
        page_size=page_size,
    )
    return WorkOrderListResponse(
        items=[_item(order) for order in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get("/{order_id}", response_model=WorkOrderDetailResponse)
async def get_work_order(
    session: Annotated[Any, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_WORK_ORDER_ROLES))],
    order_id: Annotated[uuid.UUID, Path()],
) -> WorkOrderDetailResponse:
    """E16：工单详情 + 完整状态变更历史（按 `at ASC`）。

    「可查完整状态变更历史」就是 FR-ALT-02 的第一条验收，所以 `transitions`
    不是可选字段。
    """
    detail = await work_order_service.get_work_order(session, identity, order_id)
    base = _item(detail.order)
    return WorkOrderDetailResponse(
        **base.model_dump(),
        transitions=[
            TransitionResponse(
                transition_id=row.transition_id,
                from_status=row.from_status,
                to_status=row.to_status,
                actor_id=row.actor_id,
                actor_name=row.actor_name,
                at=row.at,
                note=row.note,
            )
            for row in detail.transitions
        ],
        event_id=detail.event_id,
        event_status=detail.event_status,
        false_alarm_reason=detail.false_alarm_reason,
        actual_handled_at=detail.actual_handled_at,
    )


@router.post("/{order_id}/transitions", response_model=WorkOrderDetailResponse)
async def transition_work_order(
    session: Annotated[Any, Depends(get_session)],
    identity: Annotated[
        Identity, Depends(require_scopes("workorder:handle", roles=_WORK_ORDER_ROLES))
    ],
    order_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[TransitionRequest, Body()],
) -> WorkOrderDetailResponse:
    """E17：把工单推到下一个状态。

    返回的是**流转之后的完整详情**（含刚写进去的那条 transitions 行），
    不是只有状态字段：前端拿到响应就能直接刷新时间线，不必再发一次 GET。
    """
    await work_order_service.transition(
        session,
        identity,
        order_id,
        to_status=payload.to_status,
        note=payload.note,
        false_alarm=payload.false_alarm,
        false_alarm_reason=payload.false_alarm_reason,
        actual_handled_at=payload.actual_handled_at,
        event_id=payload.event_id,
    )
    detail = await work_order_service.get_work_order(session, identity, order_id)
    base = _item(detail.order)
    return WorkOrderDetailResponse(
        **base.model_dump(),
        transitions=[
            TransitionResponse(
                transition_id=row.transition_id,
                from_status=row.from_status,
                to_status=row.to_status,
                actor_id=row.actor_id,
                actor_name=row.actor_name,
                at=row.at,
                note=row.note,
            )
            for row in detail.transitions
        ],
        event_id=detail.event_id,
        event_status=detail.event_status,
        false_alarm_reason=detail.false_alarm_reason,
        actual_handled_at=detail.actual_handled_at,
    )


@router.post("/{order_id}/close-event", response_model=WorkOrderDetailResponse)
async def close_risk_event(
    session: Annotated[Any, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_WORK_ORDER_ROLES))],
    order_id: Annotated[uuid.UUID, Path()],
) -> WorkOrderDetailResponse:
    """E18：工单**已关闭**之后，把关联风险事件一起闭环（PRD:392）。

    单独一个端点而不是关单的副作用：PRD 的原文是「工单关闭**后**风险事件才可
    关闭」——这是两步。允许「关单 + 同时关事件」就等于允许跳过工单闭环直接
    抹掉事件。工单未关闭时调用 → 409。
    """
    await work_order_service.close_risk_event(session, identity, order_id)
    detail = await work_order_service.get_work_order(session, identity, order_id)
    base = _item(detail.order)
    return WorkOrderDetailResponse(
        **base.model_dump(),
        transitions=[
            TransitionResponse(
                transition_id=row.transition_id,
                from_status=row.from_status,
                to_status=row.to_status,
                actor_id=row.actor_id,
                actor_name=row.actor_name,
                at=row.at,
                note=row.note,
            )
            for row in detail.transitions
        ],
        event_id=detail.event_id,
        event_status=detail.event_status,
        false_alarm_reason=detail.false_alarm_reason,
        actual_handled_at=detail.actual_handled_at,
    )


__all__ = ["router"]
