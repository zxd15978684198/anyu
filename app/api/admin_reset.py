"""演示数据一键重置端点（`FR-ADM-02`）。

## 上游依据

- PRD `:596`：迭代 9 不得裁减项含「演示数据重置 / 退房清数据」
- TD `:1051`：§8.2 迭代 9 落地 `app/api`(audit_export / stats) + `frontend/console`

## 三道闸（缺一不做）

1. **admin 权限**：`require_roles("admin")`，非管理员 403（且由 `audit_service` 侧
   的越权审计口径兜底）。
2. **`expected_counts` 逐表比对**：客户端把 `preview` 拿到的每张表行数原样回传，
   服务端删之前重新统计，任一张不符 → 409 且一行都不删。这既是竞态保护，
   也让「手滑点两次」天然失效。技术实现在 `reset_service.apply_teaching_reset`。
3. **`confirm_phrase` 逐字匹配**：必须是 `重置演示数据`，否则 422。
   前两道闸都是「机器判」，这一道要求人手打中文——防的是「顺手回车」。

## 本模块不做什么

- 不建表、不加迁移（白名单表名在服务层常量里）
- 不自动重播种（重置是「回到干净」，播种是另一件事；页面提示跑 `scripts/seed_demo.py`）
- 不删物理文件（`data/report_audio/*.wav` 属部署面，由清理任务与运维脚本处理）
- **`app_env=production` 时端点一律 404**：即使配了也放行等于给线上留一把删库的钥匙
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import require_roles
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import reset_service

logger = logging.getLogger(__name__)

#: 第三道闸的固定串。改这里等于改产品口径——前端按钮上的提示文案与之逐字一致。
CONFIRM_PHRASE: Final[str] = "重置演示数据"

RESET_ROLES: Final[tuple[str, ...]] = ("admin",)


class TeachingResetAPIRoute(ContractAPIRoute):
    """在本模块把重置领域异常映射成契约错误体。

    不改 `app/api/elders.py` 的映射表：那个类属 elders 模块，且让 elders 反向
    import `reset_service` 会形成 `api → api` 的横向依赖（AGENTS §2 硬约束 2）。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await original_handler(request)
            except reset_service.ResetConflictError as exc:
                return _error_response(409, "CONFLICT", str(exc))
            except reset_service.ResetValidationError as exc:
                return _error_response(422, "VALIDATION_ERROR", str(exc))
            except reset_service.ResetServiceError as exc:
                logger.warning("teaching reset failed", extra={"error": str(exc)[:200]})
                return _error_response(500, "RESET_FAILED", "teaching reset failed")

        return wrapped


router = APIRouter(
    prefix="/api/admin/teaching-reset",
    tags=["admin"],
    route_class=TeachingResetAPIRoute,
)


class ResetPlanResponse(BaseModel):
    """`tables` 是**表名 → 将要删除/已经删除的行数**（不是剩余行数）。"""

    tables: dict[str, int]
    device_tokens_rotated: int
    total_rows: int


class TeachingResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_counts: dict[str, int] = Field(
        description="把 preview 返回的 tables 原样回传；任一张表行数变了就整体拒绝"
    )
    confirm_phrase: str = Field(description=f"必须逐字等于「{CONFIRM_PHRASE}」")


def _plan(plan: reset_service.ResetPlan) -> ResetPlanResponse:
    return ResetPlanResponse(
        tables=plan.tables,
        device_tokens_rotated=plan.device_tokens_rotated,
        total_rows=plan.total_rows,
    )


def _reject_in_production() -> Response | None:
    """生产环境直接 404：不是 403，因为 404 连「这里有个删库端点」都不告诉调用方。"""
    if get_settings().is_production:
        return _error_response(404, "NOT_FOUND", "not found")
    return None


@router.get(
    "/preview",
    response_model=ResetPlanResponse,
    summary="预览演示重置会删掉多少（不写任何东西）",
)
async def preview_teaching_reset(
    identity: Annotated[Identity, Depends(require_roles(*RESET_ROLES))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    blocked = _reject_in_production()
    if blocked is not None:
        return blocked
    plan = await reset_service.preview_teaching_reset(session)
    logger.info(
        "teaching reset preview total_rows=%s actor=%s",
        plan.total_rows,
        identity.principal_id,
    )
    return _plan(plan)


@router.post(
    "",
    response_model=ResetPlanResponse,
    summary="执行演示重置（admin + 行数比对 + 确认短语）",
)
async def apply_teaching_reset(
    body: TeachingResetRequest,
    identity: Annotated[Identity, Depends(require_roles(*RESET_ROLES))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    blocked = _reject_in_production()
    if blocked is not None:
        return blocked
    if body.confirm_phrase != CONFIRM_PHRASE:
        raise reset_service.ResetValidationError(
            f"confirm_phrase must be exactly「{CONFIRM_PHRASE}」"
        )
    plan = await reset_service.apply_teaching_reset(
        session,
        identity,
        expected_counts=body.expected_counts,
    )
    return _plan(plan)


__all__ = ["CONFIRM_PHRASE", "RESET_ROLES", "TeachingResetAPIRoute", "router"]
