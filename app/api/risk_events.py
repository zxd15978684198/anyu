"""风险事件的照护台操作（ALT-01 的**确认**端点）。

## 为什么需要这个文件

`risk_events.acknowledged_at` 此前全仓只有读、没有任何写入点：升级扫描
（`app/services/risk_event_service.escalate_overdue_red_events`）一直按
`acknowledged_at IS NULL` 挑「无人确认」的红色事件，但照护台上没有任何按钮能把
「我认领了」写进去。于是「红色事件 2 分钟内无人确认就升级」（AC-12）在实现上是
**必然升级**——只要红色事件存在，120 秒后一律升级，人再怎么处理都拦不住。
本模块补的就是那个写入点的 HTTP 入口。

## 本模块不做什么

- **不改状态机**：`risk_events.status` 的 CHECK 只有 `open/escalated/closed`
  （0012 迁移，本轮禁加迁移）。确认只写 `acknowledged_at`，「险情结没结」仍由
  工单闭环（`app/api/work_orders.py` 的 `close-event`）决定。详见
  `risk_event_service.acknowledge_event` 的 docstring。
- **不做范围过滤**：事件属于哪个长者由 `elder_id` 自带，确认动作本身不改长者数据；
  范围过滤的既有实现在 `work_order_service._scope_filter`（工单看板），事件的读面
  由 `app/api/notifications.py` / 照护台列表负责，本卡不扩面。

## 权限

只允许照护台三个角色（`admin` / `care_worker` / `doctor`），与工单端点
（`_WORK_ORDER_ROLES`）逐字一致。家属、设备、工牌一律 401/403：确认险情是照护
职责，不是家属可见的交互。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import risk_event_service
from app.services.risk_event_service import (
    RiskEventNotFoundError,
    RiskEventValidationError,
)


class RiskEventAPIRoute(ContractAPIRoute):
    """在 `ContractAPIRoute` 的映射表上补两个风险事件异常。

    继承而不是改建父类：父类的映射表是「档案/设备/鉴权」那批异常的地盘，把事件异常
    塞进去会让 `app/api/elders.py` 反向 import `risk_event_service`（`api → api`
    的横向依赖，AGENTS §2 禁止）。不映射的后果与工单端点同一条：领域异常会以 500
    出去，前端把 500 当成「后端挂了」、把 404 当成「接口没做」，两种误判都查错方向。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def risk_event_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RiskEventNotFoundError:
                return _error_response(404, "NOT_FOUND", "risk event not found")
            except RiskEventValidationError:
                return _error_response(422, "VALIDATION_ERROR", "invalid request")

        return risk_event_handler


router = APIRouter(prefix="/api/risk-events", tags=["risk-events"], route_class=RiskEventAPIRoute)

#: 确认权限 —— 与 `app/api/work_orders.py` 的 `_WORK_ORDER_ROLES` 同口径。
_RISK_EVENT_ROLES: tuple[str, ...] = ("admin", "care_worker", "doctor")


class RiskEventAckResponse(BaseModel):
    """一条已确认的风险事件。

    `status` 仍是 `open` 是**预期**：确认只回答「有没有人接手」，
    不回答「险情结没结」（后者由工单闭环推进）。前端不要据此判断「确认失败」。
    """

    event_id: uuid.UUID
    elder_id: uuid.UUID
    type: str
    severity: str
    status: str
    acknowledged_at: datetime.datetime


def _staff_uuid(identity: Identity) -> uuid.UUID:
    """从员工身份取 `staff_id`；`Identity` 构造时已保证是规范 UUID 文本。"""
    if identity.staff_id is None:  # pragma: no cover - require_roles 已保证是员工
        raise RiskEventValidationError("staff identity is malformed")
    return uuid.UUID(str(identity.staff_id))


@router.post("/{event_id}/ack", response_model=RiskEventAckResponse)
async def acknowledge_risk_event(
    session: Annotated[Any, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_RISK_EVENT_ROLES))],
    event_id: Annotated[uuid.UUID, Path()],
) -> RiskEventAckResponse:
    """ALT-01：护工/医生/管理员确认一条红色事件，120 秒后不再升级。

    幂等：重复确认返回原 `acknowledged_at`，不推后时间戳、不重复写审计。
    """
    event = await risk_event_service.acknowledge_event(
        session,
        event_id=event_id,
        staff_id=_staff_uuid(identity),
    )
    return RiskEventAckResponse(
        event_id=event.event_id,
        elder_id=event.elder_id,
        type=event.type,
        severity=event.severity,
        status=event.status,
        acknowledged_at=event.acknowledged_at,
    )


__all__ = ["router"]
