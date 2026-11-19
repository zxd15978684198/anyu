"""照护台 / 管理员只读运维统计（迭代 9 §4 W4c：`GET /api/stats/overview|security`）。

console 顶部两块看板的供数端点：一块是「今日概览」（长者 / 设备 / 工单 / 用药 / 记忆），
一块是「安全态势」（审计量 / 越权拦截 / 注入拦截 / 导出）。**只读、无业务判定**。

## 为什么聚合 SQL 写在本模块而不是 `app/services/`

AGENTS §2 把 `app/services/` 定为唯一**写**库层（`session.add` / `flush` / `commit`）；
本模块一条写语句都没有，发出去的全是 `select(count ...)` 单行聚合，也没有跨实体的
业务规则要复用。为此再造一个只做转发的 `stats_service`，只会把「一次请求几条 SQL」
这件事藏到第二层——而这条预算（见下）恰恰是要被人一眼数得出来的。

## 「今天」的口径

与 `app/services/health_service.py:81-90` 的 `today_in_shanghai()` **同源**：直接调它
取日期，再把当日 `00:00:00+08:00` ~ 次日 `00:00:00+08:00` 换算成 UTC 的**半开区间**
`[start, end)` 去比 `timestamptz` 列。

为什么不写 `BETWEEN` 或 `created_at::date = current_date`：前者会把次日 `00:00:00.000`
也框进来（跨日重算一次）；后者的 `::date` 走的是**会话时区**，容器跑 UTC 时「今天」
会整体错位 8 小时——这正是 `today_in_shanghai()` 当初被抽出来的原因。

## 鉴权：仅 admin，且拒绝先留痕

`kind="staff"` 且 `role="admin"` 才放行；其余一律 **先写一条越权审计，再抛**
`RoleForbiddenError`。`ContractAPIRoute`（`app/api/elders.py`）把它映射成
`403 {"detail", "code": "ROLE_FORBIDDEN", "trace_id"}`，与 `/api/audit/*`、
`/api/memory/*` 同一套契约，不另写一份错误体。

判 `kind` 用的 `KIND_STAFF` 从 `app/core/security.py` 取（唯一定义处，`__all__` 已收录）；
`app/core/runtime_context.py` 只是转手 import 且没进它自己的 `__all__`，从那儿取会被 mypy
的 `no_implicit_reexport` 判成隐性再导出。取法与 `app/services/elder_service.py:33`、
`report_service.py:53` 等既有消费方一致。

审计走 `audit_service.write_audit_isolated`：**独立会话提交**。这一点是必须的——
随后抛出的 403 会让 `get_session` 回滚请求事务，同事务写审计等于自己把证据抹掉
（`app/services/report_service.py` 的 `_write_audit` 注释里记着这个坑）。

## 查询预算

一次请求最多 8 条 count：overview 6 条、security 1 条。每组同表计数合并成
`count(*) FILTER (WHERE ...)` 的单行聚合，既不产生 N+1，也不会「每加一个指标
就多一次数据库往返」。新增指标时请复用手上这条聚合，不要新开一条 select。

## `denied_requests` 怎么数

`audit_logs` 没有「是否被拒」这一列，拒绝结论只存在于 `action` 与 `detail` JSONB 里，
历史上落过三种形态（都是从代码里 grep 出来的真实取值，未新造词）：

1. **动作即结论**：`action="tool.denied"`（`app/agents/companion.py:630`，MCP 工具闸门
   的白名单/预算/熔断/schema 四类拒绝），`detail` 里带 `gate` / `tool` / `trace_id`；
2. **服务层形态**：`_audit(..., result="forbidden", reason="role"|"scope"|"principal_kind")`
   —— `app/services/elder_service.py:129-152`、`device_service.py`、`health_service.py`；
3. **端点层形态**：`detail={"reason": "non_admin" | "role" | "non_staff_channel", ...}`
   —— `app/api/memory.py`、`medication.py`、`audit_export.py` 与本模块。

所以判定是 `action = 'tool.denied' OR detail->>'result' = 'forbidden'
OR detail->>'reason' IN (...)`。`delete_memory` 那类 `reason="checkout"` 的正常审计
不会被误算进来；`result="success"` / `"not_found"` / `"conflict"` 也不会。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.api.schemas.auth import ErrorResponse
from app.core.auth import RoleForbiddenError, get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.core.security import KIND_STAFF
from app.db import get_session
from app.models import (
    AuditLog,
    Device,
    Elder,
    MedicationEvent,
    MedicationPlan,
    MemoryChunk,
    Notification,
    WorkOrder,
)
from app.services import audit_service
from app.services.health_service import today_in_shanghai
from app.services.notification_service import CLOSED_NOTIFICATION_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/stats",
    tags=["stats"],
    route_class=ContractAPIRoute,
)

#: 概览与安全态势只给管理员（照护台的管理员页签）；护工/医生/家属/设备一律 403。
ADMIN_ROLES: Final[frozenset[str]] = frozenset({"admin"})

#: 越权审计的 action 取值，与 `audit.read` / `memory.read` 的 `资源.动作` 命名一致。
ACTION_OVERVIEW: Final[str] = "stats.overview.read"
ACTION_SECURITY: Final[str] = "stats.security.read"

#: 审计行的 `resource_type`；统计是全局视图，没有单一被访问对象。
STATS_RESOURCE_TYPE: Final[str] = "stats"

#: `Asia/Shanghai` 固定 +08:00（无夏令时）。与 `health_service._SHANGHAI` 同一构造，
#: 不引 `ZoneInfo` 是为了不依赖运行环境是否装了 tzdata（Windows 默认没有）。
SHANGHAI: Final[datetime.tzinfo] = datetime.timezone(
    datetime.timedelta(hours=8), name="Asia/Shanghai"
)

#: 工单「未结」集合：六态里去掉 `resolved` / `closed`（技术设计 §3 的工单状态机）。
OPEN_WORK_ORDER_STATUSES: Final[tuple[str, ...]] = (
    "new",
    "acknowledged",
    "in_progress",
    "reopened",
)

#: 注入拦截的审计 action（`app/core/injection.py:7-11` 的接线约定）。
INJECTION_ACTION: Final[str] = "injection_blocked"

#: MCP 工具闸门的拒绝动作：`action` 本身就是结论（`app/agents/companion.py:630`），
#: `detail.gate` 区分 whitelist / reject_budget / reject_breaker / schema 四类。
TOOL_DENIED_ACTION: Final[str] = "tool.denied"

#: 导出动作：审计导出 + 记忆导出。前者复用服务层常量，后者在
#: `app/services/memory_admin_service.py:72` 的 `AUDIT_ACTION_EXPORT`；不 import 那个模块
#: 是为了不与并行开发中的卡片产生导入期耦合（它坏了会连带 stats 起不来）。
MEMORY_EXPORT_ACTION: Final[str] = "memory.export"
EXPORT_ACTIONS: Final[tuple[str, ...]] = (audit_service.EXPORT_ACTION, MEMORY_EXPORT_ACTION)

#: `detail->>'result'` 的拒绝取值（服务层 `_audit` 形态）。
DENIED_RESULT: Final[str] = "forbidden"
#: `detail->>'reason'` 的拒绝取值（端点层形态 + 服务层形态）。
DENIED_REASONS: Final[tuple[str, ...]] = (
    "role",
    "scope",
    "principal_kind",
    "non_admin",
    "non_staff_channel",
)


# ------------------------------------------------------------------ 响应模型


class EldersStats(BaseModel):
    """长者档案计数。"""

    total: int = Field(description="全部长者档案数（含 away/transferred/archived）")
    active: int = Field(description="`elders.status='active'` 的长者数")


class DevicesStats(BaseModel):
    """设备计数。"""

    total: int = Field(description="全部设备数")
    online: int = Field(description="`devices.status='online'` 的设备数")


class WorkOrdersStats(BaseModel):
    """工单计数。

    `red` / `yellow` 是 **open 集合内**按 `severity` 的拆分，因此恒有
    `red + yellow <= open`：已结的红单不再计入红单待办，否则看板会长期飘红。
    """

    open: int = Field(description="未结工单数（new/acknowledged/in_progress/reopened）")
    red: int = Field(description="未结工单里的红色（`severity='red'`）")
    yellow: int = Field(description="未结工单里的黄色（`severity='yellow'`）")


class MedicationStats(BaseModel):
    """用药计数。「今天」= Asia/Shanghai 自然日。"""

    plans_active: int = Field(description="`medication_plans.status='active'` 的长期药单数")
    events_today: int = Field(description="`scheduled_at` 落在今日的用药事件数")
    confirmed_today: int = Field(
        description="今日用药事件里 `status='confirmed'` 的条数（refused/missed 不算）"
    )


class HealthStats(BaseModel):
    """健康侧计数。红标是「复核队列」口径，不是工单口径（PRD FR-HLT-03）。"""

    red_review_pending: int = Field(
        description="未处理的健康红标复核通知数（`kind='health_review'` 且未 acknowledged/failed）"
    )


class MemoryStats(BaseModel):
    """长期记忆计数。"""

    chunks_active: int = Field(description="`memory_chunks.status='active'` 的切片数")


class StatsOverview(BaseModel):
    """`GET /api/stats/overview` 的响应体。"""

    elders: EldersStats
    devices: DevicesStats
    work_orders: WorkOrdersStats
    health: HealthStats
    medication: MedicationStats
    memory: MemoryStats
    generated_at: datetime.datetime = Field(
        description="本响应的生成时刻（UTC，ISO 8601，带时区偏移）"
    )


class SecurityStats(BaseModel):
    """`GET /api/stats/security` 的响应体。

    带 `_today` 后缀的三个字段是「当日」，其余两个是**累计**（不随日切归零）——
    看板把「今天新发生多少」与「一共拦了多少」分开显示。
    """

    audit_today: int = Field(description="今日写入的审计行数")
    denied_requests: int = Field(
        description=(
            "累计越权拒绝数：`action='tool.denied'`（工具闸门）"
            "或 `detail` 里带拒绝结论的审计行（判定见模块文档）"
        )
    )
    injection_blocked: int = Field(description="累计提示注入拦截数（action='injection_blocked'）")
    exports_today: int = Field(description="今日导出次数（audit.export + memory.export）")
    generated_at: datetime.datetime = Field(
        description="本响应的生成时刻（UTC，ISO 8601，带时区偏移）"
    )


# ------------------------------------------------------------------ 权限与审计


def _actor_id(identity: Identity) -> uuid.UUID:
    """`Identity.principal_id` → 审计的 `actor_id`；坏文本退化成全零 UUID。

    与 `app/api/audit_export.py:117-125` 的 `_principal_uuid` 同口径：审计是旁路记录，
    ID 解析不了也不能让 403 变成 500。
    """
    try:
        return uuid.UUID(str(identity.principal_id))
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - 身份已保证规范
        return uuid.UUID(int=0)


def _actor_type(identity: Identity) -> str:
    """`Identity.kind` → `audit_logs.actor_type` 的 CHECK 取值（工牌记成 staff）。"""
    try:
        return audit_service.actor_type_of(identity.kind)
    except audit_service.AuditQueryError:  # pragma: no cover - kind 已由 Identity 限定
        logger.warning(
            "audit actor_type unresolved",
            extra={"kind": identity.kind},
        )
        return identity.kind


async def _audit_denied(identity: Identity, *, action: str, reason: str) -> None:
    """写一条越权审计（FR-ADM-08 / AGENTS §8「越权 → 拒绝 + 审计 + trace_id」）。

    只放 ID 与判定结果，不放姓名、手机号等敏感值；`trace_id` 由写入口并进 `detail`，
    这里再显式传一次，保证「错误体里的 trace_id」与「审计里的 trace_id」同一个值。
    """
    trace_id = get_trace_id() or "-"
    detail: dict[str, Any] = {
        "reason": reason,
        "role": identity.role,
        "kind": identity.kind,
    }
    written = await audit_service.write_audit_isolated(
        actor_type=_actor_type(identity),
        actor_id=_actor_id(identity),
        action=action,
        resource_type=STATS_RESOURCE_TYPE,
        resource_id=audit_service.NIL_RESOURCE_ID,
        detail=detail,
        trace_id=trace_id,
    )
    if not written:  # 审计失败只告警：403 这个结论不因旁路记录失败而改变
        logger.warning(
            "stats denial audit failed",
            extra={"trace_id": trace_id, "audit_action": action},
        )


async def _require_admin(identity: Identity, *, action: str) -> None:
    """先判主体种类、再判角色；两种拒绝都**先写审计**再抛。

    家属/设备持有效凭证但没有照护台管理员角色：仍归 `ROLE_FORBIDDEN`（本卡的口径），
    细节差异（`kind` / `reason`）留在审计里，不用两种错误码表达。
    """
    if identity.kind != KIND_STAFF:
        await _audit_denied(identity, action=action, reason="non_staff_channel")
        raise RoleForbiddenError("stats endpoints are admin-only (FR-ADM-08)")
    if (identity.role or "") not in ADMIN_ROLES:
        await _audit_denied(identity, action=action, reason="non_admin")
        raise RoleForbiddenError("stats endpoints are admin-only (FR-ADM-08)")


# ------------------------------------------------------------------ 时间窗


def _today_window_utc() -> tuple[datetime.datetime, datetime.datetime]:
    """`Asia/Shanghai` 今日的 `[start, end)`，换算成 UTC 的 aware datetime。"""
    today = today_in_shanghai()
    start_local = datetime.datetime.combine(today, datetime.time.min, tzinfo=SHANGHAI)
    end_local = start_local + datetime.timedelta(days=1)
    return start_local.astimezone(datetime.UTC), end_local.astimezone(datetime.UTC)


def _now() -> datetime.datetime:
    """响应里的 `generated_at`：UTC aware，序列化成带偏移的 ISO 8601。"""
    return datetime.datetime.now(datetime.UTC)


# ------------------------------------------------------------------ 查询（只读聚合）

#: 计数聚合语句的类型：**行形状随指标增减**（每个辅助函数 `select(count(), count(), …)`
#: 的列数各不相同）。SQLAlchemy 2.x 的 `Select` 是 `TypedReturnsRows[Unpack[_Ts]]`——变长
#: 泛型，一个类型参数 = 一列，所以写死 `Select[Any]` 只表示「恰好一列」，多列聚合会被判成
#: `Incompatible return value type (got "Select[int, int]", expected "Select[Any]")`。
#: `*tuple[Any, ...]` 才是「任意列数」的写法（PEP 646 星号解包，与 `Unpack[...]` 等价）：
#: 七个辅助函数统一用这个别名收口，既不逐处 `cast`，也不给每个函数写一遍具体列数
#: （那样新增一个指标就要跟着改签名），更不为此放宽 mypy 配置。
type CountStmt = Select[*tuple[Any, ...]]


def _elders_stmt() -> CountStmt:
    """① 长者 total / active。"""
    return select(
        func.count().label("total"),
        func.count().filter(Elder.status == "active").label("active"),
    ).select_from(Elder)


def _devices_stmt() -> CountStmt:
    """② 设备 total / online。"""
    return select(
        func.count().label("total"),
        func.count().filter(Device.status == "online").label("online"),
    ).select_from(Device)


def _work_orders_stmt() -> CountStmt:
    """③ 工单 open / red / yellow（红黄是 open 的子集）。"""
    open_cond = WorkOrder.status.in_(OPEN_WORK_ORDER_STATUSES)
    return select(
        func.count().filter(open_cond).label("open"),
        func.count().filter(and_(open_cond, WorkOrder.severity == "red")).label("red"),
        func.count().filter(and_(open_cond, WorkOrder.severity == "yellow")).label("yellow"),
    ).select_from(WorkOrder)


def _medication_plans_stmt() -> CountStmt:
    """④ 在用长期药单数。"""
    return select(func.count()).select_from(MedicationPlan).where(MedicationPlan.status == "active")


def _medication_events_stmt(start_utc: datetime.datetime, end_utc: datetime.datetime) -> CountStmt:
    """⑤ 今日用药事件数 + 其中已确认条数。

    时间轴用 `scheduled_at`（计划触发时刻）而不是 `created_at`：看板问的是
    「今天该吃的药吃了几次」。
    """
    return (
        select(
            func.count().label("events"),
            func.count().filter(MedicationEvent.status == "confirmed").label("confirmed"),
        )
        .select_from(MedicationEvent)
        .where(
            MedicationEvent.scheduled_at >= start_utc,
            MedicationEvent.scheduled_at < end_utc,
        )
    )


def _health_red_review_stmt() -> CountStmt:
    """⑤b 未处理的红标复核通知数（红档 46 只数工单，红标单独给一个数）。"""
    return (
        select(func.count())
        .select_from(Notification)
        .where(Notification.kind == "health_review")
        .where(Notification.status.notin_(CLOSED_NOTIFICATION_STATUSES))
    )


def _memory_chunks_stmt() -> CountStmt:
    """⑥ 有效记忆切片数。"""
    return select(func.count()).select_from(MemoryChunk).where(MemoryChunk.status == "active")


def _security_stmt(start_utc: datetime.datetime, end_utc: datetime.datetime) -> CountStmt:
    """⑦ 安全态势：一条聚合出四个数（迄今审计总量只用于内部核对，不返回）。

    审计表是只增表且量级最大的一环，因此四个指标压在同一条 `FILTER` 聚合里；
    `exports_today` 同时受「动作」与「今日窗口」两个条件约束。
    """
    window = and_(AuditLog.created_at >= start_utc, AuditLog.created_at < end_utc)
    denied = or_(
        AuditLog.action == TOOL_DENIED_ACTION,
        AuditLog.detail["result"].astext == DENIED_RESULT,
        AuditLog.detail["reason"].astext.in_(DENIED_REASONS),
    )
    return select(
        func.count().filter(window).label("audit_today"),
        func.count().filter(denied).label("denied"),
        func.count().filter(AuditLog.action == INJECTION_ACTION).label("injection"),
        func.count()
        .filter(and_(AuditLog.action.in_(EXPORT_ACTIONS), window))
        .label("exports_today"),
    ).select_from(AuditLog)


# ------------------------------------------------------------------ 端点


@router.get(
    "/overview",
    response_model=StatsOverview,
    summary="照护台概览统计（管理员）",
    description=(
        "长者 / 设备 / 工单 / 用药 / 记忆五组计数。只读、无副作用；"
        "所有计数为 0 时返回 0（不是 null）。「今天」按 Asia/Shanghai 自然日。"
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "缺少或无效凭证（契约 §7：code 取 `TOKEN_*`）",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "凭证有效但非管理员（code=`ROLE_FORBIDDEN`，并已落越权审计）",
        },
    },
)
async def read_stats_overview(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StatsOverview:
    """一次请求 6 条 count：① 长者 ② 设备 ③ 工单 ④ 药单 ⑤ 用药事件 ⑥ 记忆。"""
    await _require_admin(identity, action=ACTION_OVERVIEW)
    start_utc, end_utc = _today_window_utc()

    elders_total, elders_active = (await session.execute(_elders_stmt())).one()
    devices_total, devices_online = (await session.execute(_devices_stmt())).one()
    orders_open, orders_red, orders_yellow = (await session.execute(_work_orders_stmt())).one()
    plans_active = await session.scalar(_medication_plans_stmt())
    red_review_pending = await session.scalar(_health_red_review_stmt()) or 0
    events_today, confirmed_today = (
        await session.execute(_medication_events_stmt(start_utc, end_utc))
    ).one()
    chunks_active = await session.scalar(_memory_chunks_stmt())

    return StatsOverview(
        elders=EldersStats(total=elders_total, active=elders_active),
        devices=DevicesStats(total=devices_total, online=devices_online),
        work_orders=WorkOrdersStats(open=orders_open, red=orders_red, yellow=orders_yellow),
        health=HealthStats(red_review_pending=int(red_review_pending)),
        medication=MedicationStats(
            plans_active=plans_active or 0,
            events_today=events_today,
            confirmed_today=confirmed_today,
        ),
        memory=MemoryStats(chunks_active=chunks_active or 0),
        generated_at=_now(),
    )


@router.get(
    "/security",
    response_model=SecurityStats,
    summary="安全态势统计（管理员）",
    description=(
        "审计量 / 越权拒绝数 / 注入拦截数 / 导出次数。只读、无副作用。"
        "`denied_requests` 与 `injection_blocked` 是累计值，其余`_today` 字段按 "
        "Asia/Shanghai 自然日。"
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "缺少或无效凭证（契约 §7：code 取 `TOKEN_*`）",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "凭证有效但非管理员（code=`ROLE_FORBIDDEN`，并已落越权审计）",
        },
    },
)
async def read_stats_security(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SecurityStats:
    """一次请求 1 条 count（四个指标压在同一条 `FILTER` 聚合里）。"""
    await _require_admin(identity, action=ACTION_SECURITY)
    start_utc, end_utc = _today_window_utc()
    audit_today, denied, injection, exports_today = (
        await session.execute(_security_stmt(start_utc, end_utc))
    ).one()

    return SecurityStats(
        audit_today=audit_today,
        denied_requests=denied,
        injection_blocked=injection,
        exports_today=exports_today,
        generated_at=_now(),
    )


__all__ = [
    "ACTION_OVERVIEW",
    "ACTION_SECURITY",
    "ADMIN_ROLES",
    "DevicesStats",
    "EldersStats",
    "MedicationStats",
    "MemoryStats",
    "SecurityStats",
    "StatsOverview",
    "WorkOrdersStats",
    "router",
]
