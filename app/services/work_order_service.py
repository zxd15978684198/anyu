"""工单落库的唯一实现（N21b · AGENTS §4-8「一个能力只允许一种形态」）。

## 为什么要有这个模块

抽离前有两份逐行雷同的建单实现，各带一份 `_next_order_no`：

- `app/services/medication_service.py`：漏服升级（`missed_medication` / red）与
  拒服跟进（`follow_up` / yellow）；
- `app/services/alert_service.py`：安全告警红单（`safety_alert`）。

根因不是有人想复制，而是 `medication_service._insert_work_order` 是私有函数、
也没有可复用入口。N21 的 MCP 工具 `create_work_order` 若照旧只能再抄一份，
就会变成第三份。现在三处都调本模块的 `create_work_order()`，全仓
`WorkOrder(...)` 构造只剩这里一处（验收：`git grep -c "WorkOrder(" app/` → 1）。

## 事务边界

本模块**只 add / flush，绝不 commit**。事务由调用方掌握：HTTP 路径归
`app.db.get_session`（每请求提交/回滚），scheduler 自带会话。「建单 + 改状态」
需要原子生效时共享同一个 session 即可（漏服升级就是这样）。

## 单号与并发（只在这里讲一次，别再抄一份）

`order_no` 是给人念的 `WO-YYYYMMDD-NNNN`（上海自然日的当日序号），所以不用
UUID / 全局序列代替。生成方式是 `max(order_no) + 1`，**本身不是原子的**：两个
并发事务可能读到同一个最大值、算出同一个序号。真正的并发保护是库上的
`uq_work_orders_order_no` 唯一约束，不是这条 SELECT。

撞号后必须走 savepoint，理由三条：

1. PostgreSQL 里任何语句报错都会把**整个事务**标记为 aborted，后续语句一律
   `InFailedSqlTransaction`；`async with session.begin_nested()` 给出 savepoint，
   失败只回滚到该点，外层事务还能继续用。
2. savepoint 回滚后失败的实例仍挂在 session 上（pending），不 `expunge` 的话
   下一次 `flush()` 会把它再刷一遍，撞同一个唯一键——所以 `_expunge_quietly()`。
3. 重算序号再插一次。默认隔离级别 READ COMMITTED 下，第二次 SELECT 能看到
   已提交的竞争行，序号自然往后跳。

只重试一次（共两次尝试）：连续两次撞号说明序号被并发吃掉两次，与其在锁竞争里
无界空转，不如把失败明确抛给调用方（可诊断、可告警）。重试次数与抽离前的两份
实现一致，没有顺手改行为；`MAX_ATTEMPTS = 2` 是全仓唯一的撞号重试口径。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError, check_roles
from app.core.context import get_trace_id
from app.core.redact import mask_name
from app.core.runtime_context import Identity
from app.core.security import canonical_uuid
from app.models import Elder, StaffAccount
from app.models.relation import ElderStaffAssignment
from app.models.work_order import (
    WORK_ORDER_STATUSES,
    RiskEvent,
    WorkOrder,
    WorkOrderTransition,
)
from app.services import audit_service, notification_service

logger = logging.getLogger(__name__)

SHANGHAI: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")
WORK_ORDER_PREFIX: Final[str] = "WO"
"""单号前缀；格式 `WO-YYYYMMDD-NNNN` 是人工口头/纸面沟通口径。"""

MAX_ATTEMPTS: Final[int] = 2
"""单号撞唯一键后的最大尝试次数（首次 + 重算 1 次）。"""

#: 幂等去重键长度上限（FR-ALT-04）：与 0022 迁移的 `varchar(120)` 逐字一致。
MAX_DEDUPE_KEY_LENGTH: Final[int] = 120

#: 去重键的部分唯一索引名（撞键时用它区分「去重命中」与「单号撞号」）。
DEDUPE_WORK_ORDER_INDEX: Final[str] = "uq_work_orders_dedupe_key"


# ------------------------------------------------------------------ 状态机


ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "new": frozenset({"acknowledged", "closed"}),
    "acknowledged": frozenset({"in_progress", "closed"}),
    "in_progress": frozenset({"resolved", "closed"}),
    "resolved": frozenset({"closed", "reopened"}),
    "closed": frozenset({"reopened"}),
    "reopened": frozenset({"acknowledged", "in_progress", "closed"}),
}
"""工单六态的合法迁移（PRD:392 状态机逐条落成数据）。

**「误报」不是状态**：PRD 说「误报可关」，落法是 `to_status="closed"` +
`false_alarm=True` + 必填 `false_alarm_reason`。把它做成第七个状态会让
`WORK_ORDER_STATUSES` 与库上 CHECK 同时失效，且「误报」与「已关闭」在
看板筛选里本就要合并（关掉的单子都不在待办队列）。

`closed → reopened` 只有这一条出边：关单是可撤销的（管理员重开，PRD:392），
但重开不是「回到 new」——它带着上一次处置的历史，所以回到 `reopened`。
"""

ACTIVE_STATUSES: Final[frozenset[str]] = frozenset(
    {"new", "acknowledged", "in_progress", "reopened"}
)
"""「未处理」四态 = 看板默认视图（PRD:397「未处理列表即天然交接清单」）。"""

_RELATION_BY_ROLE: Final[dict[str, str]] = {"care_worker": "primary_care", "doctor": "doctor"}
"""员工角色 → `elder_staff_assignments.relation`。与 `elder_service._RELATION_BY_ROLE`
同值：范围口径只登记一次，两边必须一致，否则同一个护工在档案页与看板看到不同长者。"""


class WorkOrderServiceError(Exception):
    """建单失败；调用方按自己的错误类型翻译，或直接上抛给 API 层。"""


class WorkOrderNoConflict(WorkOrderServiceError):
    """当日单号连续两次撞唯一键（并发极端情况），本次派单失败。"""


class WorkOrderNotFoundError(WorkOrderServiceError):
    """工单不存在（API 层映射 404 `NOT_FOUND`）。"""


class WorkOrderConflictError(WorkOrderServiceError):
    """状态迁移非法 / 前置条件不满足（API 层映射 409 `CONFLICT`）。"""


class WorkOrderValidationError(WorkOrderServiceError):
    """入参不满足业务规则（如关单未填处置结果；API 层映射 422 `VALIDATION_ERROR`）。"""


class WorkOrderScopeForbiddenError(WorkOrderServiceError):
    """主体种类或角色不允许看工单（API 层映射 403 `SCOPE_FORBIDDEN`）。"""


@dataclasses.dataclass(frozen=True, slots=True)
class TransitionRow:
    """一条状态变更历史（`GET /api/work-orders/{id}` 的 `transitions` 元素）。"""

    transition_id: uuid.UUID
    from_status: str
    to_status: str
    actor_id: uuid.UUID | None
    actor_name: str | None
    at: datetime.datetime
    note: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class WorkOrderListItem:
    """看板一行。`elder_name` 已脱敏；`is_overdue_red` 供前端「置顶」视觉标记。"""

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


@dataclasses.dataclass(frozen=True, slots=True)
class WorkOrderPage:
    items: tuple[WorkOrderListItem, ...]
    total: int
    page: int
    page_size: int


@dataclasses.dataclass(frozen=True, slots=True)
class WorkOrderDetail:
    order: WorkOrderListItem
    transitions: tuple[TransitionRow, ...]
    event_id: uuid.UUID | None
    event_status: str | None
    false_alarm_reason: str | None
    actual_handled_at: datetime.datetime | None


def shanghai_today() -> datetime.date:
    """单号里的日期用上海时区（「今天的单」），存储仍是 UTC（AGENTS §8）。"""
    return datetime.datetime.now(datetime.UTC).astimezone(SHANGHAI).date()


async def next_order_no(session: AsyncSession, *, day: datetime.date | None = None) -> str:
    """`WO-YYYYMMDD-NNNN`：当日已用最大序号 + 1（0001 起，补零四位）。

    非原子，见模块文档「单号与并发」；并发正确性由 `uq_work_orders_order_no`
    兜底。`day` 仅供测试钉住日期，生产调用方不要传（默认上海今日）。
    """
    target = day if day is not None else shanghai_today()
    prefix = f"{WORK_ORDER_PREFIX}-{target:%Y%m%d}-"
    result = await session.execute(
        select(func.max(WorkOrder.order_no)).where(WorkOrder.order_no.like(f"{prefix}%"))
    )
    current = result.scalar_one_or_none()
    seq = 1
    if current:
        tail = str(current).rsplit("-", 1)[-1]
        if tail.isdigit():
            seq = int(tail) + 1
    return f"{prefix}{seq:04d}"


def _expunge_quietly(session: AsyncSession, obj: Any) -> None:
    """savepoint 回滚后把失败的 pending 对象踢出 session，避免污染后续 flush。"""
    try:
        if obj in session:
            session.expunge(obj)
    except Exception:  # pragma: no cover - expunge 本身失败不影响主流程
        logger.debug("expunge failed for %r", type(obj).__name__)


async def create_work_order(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    type: str,
    severity: str,
    note: str,
    event_id: uuid.UUID | None = None,
    assignee_staff_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
) -> WorkOrder:
    """建一张 `status='new'` / `false_alarm=False` 的工单；单号撞键时重算一次。

    与抽离前两处实现逐字段对齐：

    | 字段 | 取值 | 说明 |
    | --- | --- | --- |
    | `order_id` | `uuid4()`（可注入） | ORM 有 `gen_random_uuid()` 默认，但显式生成让调用方 flush 前就能拿到 ID |
    | `order_no` | `next_order_no()` | 唯一约束 `uq_work_orders_order_no` 兜底 |
    | `status` | 固定 `"new"` | 停在照护台「待接手」队列，**不得**自造成已接手 |
    | `false_alarm` | 固定 `False` | 建单即视为真实告警 |
    | `assignee_id` | `assignee_staff_id` | 安全告警会预先指派主责护工；用药侧传 None |

    `event_id` 指向 **`risk_events.event_id`**（TD §3.4 外键口径），不是
    `medication_events.reminder_id`。用药两条人工链路都已先建风险事件再挂单：
    拒服（`medication_service.refuse_event`）与漏服升级
    （`medication_service.escalate_to_work_order`）都会传 `risk.event_id`；
    只有安全告警那条来源（`decision_logs`）没有风险事件行，才留空。

    `dedupe_key`（FR-ALT-04）：调用方给一个**业务摘要**（如
    `device_offline:<device_id>:<business_date>:seq1`），同 `elder_id + type + dedupe_key`
    下只允许一行（库级部分唯一索引 `uq_work_orders_dedupe_key` 兜底）。

    - 命中已有行：**返回原行**、不新建、不报 409，返回对象上带 `deduplicated=True`；
    - 并发：两个请求同时查不到就同时插入，后到的被唯一索引挡住（`IntegrityError`），
      这里 rollback 掉这一行再把原行查回来返回——**不占用**撞号重试次数；
    - 不传 `dedupe_key`：行为与改造前完全一致。

    `deduplicated` 是返回对象上的临时属性，不是表里的列（改成包装类会打断全部调用方）。

    不 commit；撞号重试的口径见模块文档。
    """
    key = _normalise_dedupe_key(dedupe_key)
    if key is not None:
        existing = await _find_by_dedupe_key(session, elder_id=elder_id, type=type, dedupe_key=key)
        if existing is not None:
            return _mark_deduplicated(existing, type=type, dedupe_key=key)

    day = shanghai_today()
    order: WorkOrder | None = None
    last_error: IntegrityError | None = None
    for _attempt in range(MAX_ATTEMPTS):
        candidate = WorkOrder(
            order_id=order_id or uuid.uuid4(),
            order_no=await next_order_no(session, day=day),
            elder_id=elder_id,
            event_id=event_id,
            type=type,
            severity=severity,
            status="new",
            false_alarm=False,
            assignee_id=assignee_staff_id,
            note=note,
            dedupe_key=key,
        )
        try:
            async with session.begin_nested():
                session.add(candidate)
        except IntegrityError as exc:
            last_error = exc
            _expunge_quietly(session, candidate)
            if key is not None and _is_dedupe_conflict(exc):
                # 并发同键：不是单号撞号，别浪费重试次数，直接回查原行。
                existing = await _find_by_dedupe_key(
                    session, elder_id=elder_id, type=type, dedupe_key=key
                )
                if existing is not None:
                    logger.info(
                        "work_order deduplicated",
                        extra={
                            "order_id": str(existing.order_id),
                            "order_no": existing.order_no,
                            "type": type,
                            "dedupe_key": key,
                        },
                    )
                    return _mark_deduplicated(existing, type=type, dedupe_key=key)
                raise
            logger.warning("work_order order_no 冲突，重算一次：%s", exc.orig)
            continue
        order = candidate
        break

    if order is None:  # pragma: no cover - 唯一键连续两次冲突才会走到
        raise WorkOrderNoConflict("工单号连续两次冲突，派单失败") from last_error
    order.deduplicated = False
    return order


def _normalise_dedupe_key(dedupe_key: str | None) -> str | None:
    """去除首尾空白；空串视作没传。超长直接拒绝（列是 varchar(120)，静默截断会撞键）。"""
    if dedupe_key is None:
        return None
    key = str(dedupe_key).strip()
    if not key:
        return None
    if len(key) > MAX_DEDUPE_KEY_LENGTH:
        raise WorkOrderValidationError(
            f"dedupe_key too long ({len(key)} > {MAX_DEDUPE_KEY_LENGTH})"
        )
    return key


async def _find_by_dedupe_key(
    session: AsyncSession, *, elder_id: uuid.UUID, type: str, dedupe_key: str
) -> WorkOrder | None:
    result = await session.execute(
        select(WorkOrder)
        .where(
            WorkOrder.elder_id == elder_id,
            WorkOrder.type == type,
            WorkOrder.dedupe_key == dedupe_key,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def _mark_deduplicated(order: WorkOrder, *, type: str, dedupe_key: str) -> WorkOrder:
    order.deduplicated = True
    return order


def _is_dedupe_conflict(exc: IntegrityError) -> bool:
    """只认去重索引；`order_no` 撞号走原来的重算分支。"""
    return DEDUPE_WORK_ORDER_INDEX in str(exc.orig)


async def find_open_work_order(
    session: AsyncSession, *, elder_id: uuid.UUID, type: str
) -> WorkOrder | None:
    """该长者名下某类型**未关单**（`ACTIVE_STATUSES` 内）的工单，没有则 ``None``。

    用途是派单前的**去重闸门**：定时扫描类任务（设备掉线、漏采补派）每轮都会重新
    撞上同一场故障，判据必须是「这类单还挂着没关」而不是「有没有派过」——后者会把
    「关单之后故障复发」当成重复而永远不再派。

    `elder_id + type` 两列就够：`work_orders` 没有设备维度，而设备:长者 1:1
    （`uq_devices_elder_id` 部分唯一索引），所以「这台设备的掉线单」等价于「这位长者
    名下的 `device_fault` 单」；漏采同理按 `(elder_id, 'collection_missed')`。

    只读：不建单、不改状态，调用方负责在 `None` 时才 `create_work_order()`。
    按 `created_at` 升序取最早一条——同类型多张在途单时，返回「最早那张」是确定性的
    （去重只看「有没有」，具体是哪张不影响判定）。
    """
    return (
        (
            await session.execute(
                select(WorkOrder)
                .where(
                    WorkOrder.elder_id == elder_id,
                    WorkOrder.type == type,
                    WorkOrder.status.in_(ACTIVE_STATUSES),
                )
                .order_by(WorkOrder.created_at, WorkOrder.order_id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


# ------------------------------------------------------------------ 范围与审计


def _audit(
    identity: Identity,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
    **extra: object,
) -> None:
    """写一条结构化审计日志（与 `device_service._audit` / `elder_service._audit` 同口径）。

    只允许放 ID、字段名与判定结果；**不得**把姓名、备注正文、处置结果塞进 `extra`。
    `note` 是长者相关的自由文本（可能含病情），它的落点是 `work_orders.note`，
    不是审计日志——审计只回答「谁在何时对哪张单做了什么」。
    """
    fields: dict[str, object] = {
        "trace_id": get_trace_id() or "-",
        "audit_action": action,
        "actor": f"{identity.kind}:{identity.principal_id}",
        "target": target,
        "result": result,
    }
    if reason is not None:
        fields["reason"] = reason
    fields.update(extra)
    logger.info("audit", extra=fields)


def _staff_uuid(identity: Identity) -> uuid.UUID:
    """员工身份里的 `staff_id` 已被 `Identity` 保证为规范 UUID 文本。"""
    if identity.staff_id is None:  # pragma: no cover - Identity 构造时已保证
        raise WorkOrderScopeForbiddenError("staff identity is malformed")
    return uuid.UUID(canonical_uuid(identity.staff_id))


async def _scope_relation(
    session: AsyncSession,
    identity: Identity,
    *,
    action: str,
    target: str,
) -> str | None:
    """主体 → 范围形状：`None` 表示全量（admin），否则为 assignment 的 relation。

    与 `elder_service._scope_relation` 同一条口径（角色白名单只有一处实现：
    `check_roles` 不用于这里，因为这里要区分 admin 与「有范围的角色」）。

    **工牌与员工同口径**（FR-ADM-08 的「动作级 scope」落到这里）：工牌是同一名
    员工的第二套凭证，`scope` 只决定「能不能执行这类动作」，不决定「能看谁的单
    子」——范围仍由 `staff_id` 在 `elder_staff_assignments` 上的关系与角色白名单
    算出，和为同一名员工签发的普通 access 完全一致。给工牌另写一套更窄/更宽的数据
    面收敛，等于承认「同一人换个凭证就换了可见范围」，那才是权限漏洞。

    工牌身份按模型不带 `role`（`runtime_context.py:169` 的 `_must_be_absent`），
    所以这里回查 `staff_accounts` 同一行拿角色；员工身份则直接用身份里的 `role`。
    家属 / 设备一律拒绝：工单是照护台内部队列，家属看的是通知与日报。
    """
    if not identity.is_staff_principal:
        _audit(identity, action=action, target=target, result="forbidden", reason="principal_kind")
        raise WorkOrderScopeForbiddenError("only staff can read work orders")
    role = identity.role
    if role is None:
        role = await _staff_card_role(session, _staff_uuid(identity))
        if role is None:
            # 工牌指向的账号不存在或已停用：换凭证后越权/失效都走拒绝，不放行。
            _audit(identity, action=action, target=target, result="forbidden", reason="role")
            raise WorkOrderScopeForbiddenError("staff role has no work order scope")
    if role == "admin":
        return None
    relation = _RELATION_BY_ROLE.get(role)
    if relation is None:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise WorkOrderScopeForbiddenError("staff role has no work order scope")
    return relation


async def _staff_card_role(session: AsyncSession, staff_id: uuid.UUID) -> str | None:
    """工牌主体回查员工角色（只认 active 账号，停用即刻失去范围）。"""
    return (
        await session.execute(
            select(StaffAccount.role).where(
                StaffAccount.staff_id == staff_id,
                StaffAccount.status == "active",
            )
        )
    ).scalar_one_or_none()


def _scope_filter(relation: str | None, staff_id: uuid.UUID) -> Any:
    """把范围形状落成 `WorkOrder.elder_id IN (...)` 过滤条件；全量返回 `None`。

    子查询而不是先查一遍 set：看板有分页，先把全部 elder_id 拉到 Python 再过滤
    会让 `total` 与 `items` 两条 SQL 的口径悄悄分叉。
    """
    if relation is None:
        return None
    return WorkOrder.elder_id.in_(
        select(ElderStaffAssignment.elder_id).where(
            ElderStaffAssignment.staff_id == staff_id,
            ElderStaffAssignment.relation == relation,
        )
    )


# ------------------------------------------------------------------ 读


async def list_work_orders(
    session: AsyncSession,
    identity: Identity,
    *,
    status: str | None = None,
    severity: str | None = None,
    elder_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> WorkOrderPage:
    """E15：工单看板。

    默认只返回**未处理**四态（`ACTIVE_STATUSES`）——PRD:397「未处理列表即天然
    交接清单」；要看已关闭的单子必须显式传 `status="closed"`。

    排序固定 `severity` 红在前 + `created_at ASC`（老的先看）。**排序在 SQL 里做**，
    不是前端排：有分页时前端只能排当前页，第 2 页的红档永远不会置顶（计划 §4 D1）。

    范围过滤在服务端：admin 全量，护工/医生按 `elder_staff_assignments` 的 relation
    收敛（AGENTS §4.9，不看客户端传什么）。无范围的人得到空列表，不是 403。
    """
    relation = await _scope_relation(
        session, identity, action="work_order.list", target="work_order:list"
    )
    filters: list[Any] = []
    scope = _scope_filter(relation, _staff_uuid(identity)) if relation else None
    if scope is not None:
        filters.append(scope)
    if status is None:
        filters.append(WorkOrder.status.in_(ACTIVE_STATUSES))
    else:
        if status not in WORK_ORDER_STATUSES:
            raise WorkOrderValidationError(f"unknown status {status!r}")
        filters.append(WorkOrder.status == status)
    if severity is not None:
        if severity not in ("red", "yellow"):
            raise WorkOrderValidationError(f"unknown severity {severity!r}")
        filters.append(WorkOrder.severity == severity)
    if elder_id is not None:
        filters.append(WorkOrder.elder_id == elder_id)

    total = int(
        (
            await session.execute(select(func.count()).select_from(WorkOrder).where(*filters))
        ).scalar_one()
    )

    # 红档在前：`case` 比 `severity DESC` 明确（red/yellow 的字典序恰好也是 r > y，
    # 但那是巧合；换一个 severity 取值就会静默错序）。
    severity_rank = case((WorkOrder.severity == "red", 0), else_=1)
    rows = (
        await session.execute(
            select(WorkOrder, Elder.name, StaffAccount.name)
            .join(Elder, Elder.elder_id == WorkOrder.elder_id)
            .outerjoin(StaffAccount, StaffAccount.staff_id == WorkOrder.assignee_id)
            .where(*filters)
            .order_by(severity_rank, WorkOrder.created_at.asc(), WorkOrder.order_no.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = tuple(
        WorkOrderListItem(
            order_id=order.order_id,
            order_no=order.order_no,
            elder_id=order.elder_id,
            elder_name=mask_name(elder_name),
            type=order.type,
            severity=order.severity,
            status=order.status,
            assignee_id=order.assignee_id,
            # 责任人姓名是**员工**姓名，照护台内部可见，此处脱敏会让人认不出同事。
            assignee_name=assignee_name,
            false_alarm=order.false_alarm,
            note=order.note,
            created_at=order.created_at,
            closed_at=order.closed_at,
        )
        for order, elder_name, assignee_name in rows
    )
    return WorkOrderPage(items=items, total=total, page=page, page_size=page_size)


async def get_work_order(
    session: AsyncSession, identity: Identity, order_id: uuid.UUID
) -> WorkOrderDetail:
    """E16：工单详情 + 完整状态变更历史（按 `at ASC`）。

    「可查完整状态变更历史」就是 FR-ALT-02 的第一条验收，所以 `transitions`
    不是可选字段——没有它这条验收不成立。
    """
    relation = await _scope_relation(
        session, identity, action="work_order.read", target=f"work_order:{order_id}"
    )
    order = await session.get(WorkOrder, order_id)
    if order is None:
        _audit(
            identity,
            action="work_order.read",
            target=f"work_order:{order_id}",
            result="not_found",
        )
        raise WorkOrderNotFoundError(f"work order {order_id} not found")
    await _require_in_scope(session, identity, order, relation=relation, action="work_order.read")

    elder = await session.get(Elder, order.elder_id)
    assignee_name: str | None = None
    if order.assignee_id is not None:
        assignee = await session.get(StaffAccount, order.assignee_id)
        assignee_name = assignee.name if assignee is not None else None

    transition_rows = (
        await session.execute(
            select(WorkOrderTransition, StaffAccount.name)
            .outerjoin(StaffAccount, StaffAccount.staff_id == WorkOrderTransition.actor_id)
            .where(WorkOrderTransition.order_id == order_id)
            .order_by(WorkOrderTransition.at.asc(), WorkOrderTransition.transition_id.asc())
        )
    ).all()

    event_status: str | None = None
    if order.event_id is not None:
        event = await session.get(RiskEvent, order.event_id)
        event_status = event.status if event is not None else None

    item = WorkOrderListItem(
        order_id=order.order_id,
        order_no=order.order_no,
        elder_id=order.elder_id,
        elder_name=mask_name(elder.name) if elder is not None else "",
        type=order.type,
        severity=order.severity,
        status=order.status,
        assignee_id=order.assignee_id,
        assignee_name=assignee_name,
        false_alarm=order.false_alarm,
        note=order.note,
        created_at=order.created_at,
        closed_at=order.closed_at,
    )
    transitions = tuple(
        TransitionRow(
            transition_id=row.transition_id,
            from_status=row.from_status,
            to_status=row.to_status,
            actor_id=row.actor_id,
            actor_name=actor_name,
            at=row.at,
            note=row.note,
        )
        for row, actor_name in transition_rows
    )
    return WorkOrderDetail(
        order=item,
        transitions=transitions,
        event_id=order.event_id,
        event_status=event_status,
        false_alarm_reason=order.false_alarm_reason,
        actual_handled_at=order.actual_handled_at,
    )


async def _require_in_scope(
    session: AsyncSession,
    identity: Identity,
    order: WorkOrder,
    *,
    relation: str | None,
    action: str,
) -> None:
    """单条的范围内判定（与列表同源，用同一张 assignment 表）。

    列表用 SQL 过滤、详情用这条判定：两条路径必须给出同一个答案，否则会出现
    「列表里看不见、但知道 order_id 就能查详情」的越权。
    """
    if relation is None:
        return
    hit = (
        await session.execute(
            select(ElderStaffAssignment.id).where(
                ElderStaffAssignment.elder_id == order.elder_id,
                ElderStaffAssignment.staff_id == _staff_uuid(identity),
                ElderStaffAssignment.relation == relation,
            )
        )
    ).scalar_one_or_none()
    if hit is None:
        _audit(
            identity,
            action=action,
            target=f"work_order:{order.order_id}",
            result="forbidden",
            reason="elder_scope",
        )
        raise WorkOrderScopeForbiddenError("work order is out of your scope")


# ------------------------------------------------------------------ 状态流转


async def transition(
    session: AsyncSession,
    identity: Identity,
    order_id: uuid.UUID,
    *,
    to_status: str,
    note: str | None = None,
    false_alarm: bool = False,
    false_alarm_reason: str | None = None,
    actual_handled_at: datetime.datetime | None = None,
    event_id: uuid.UUID | None = None,
) -> WorkOrder:
    """E17：把一张工单推到下一个状态，**同事务**写状态变更历史与审计。

    三件事必须在同一个事务里（本函数只 flush，不 commit，事务归调用方）：

    1. 改 `WorkOrder.status`（及 `false_alarm*` / `actual_handled_at` / `closed_at`）；
    2. 写一行 `WorkOrderTransition`（from → to + actor + at + note）；
    3. 写一条审计（`action="work_order.transition"`）。

    拆成两次提交就会造出「状态变了但历史里没有」的工单——那正是 FR-ALT-02
    「任一工单可查完整状态变更历史」要禁止的形态。

    ## 关闭约束（PRD:392 五条，逐条在下面校验）

    - 关单必填处置结果 `note`；误报关单改为必填 `false_alarm_reason`，且
      **不删任何东西**（`event_id` 指向的风险事件原样保留，PRD「保留原始输入」）；
    - 红色事件关单还要填 `actual_handled_at`（实际处置时刻，用于响应时长统计）；
    - 重开只有 admin 能做，且必填「重开原因」；
    - 风险事件的关闭**不在本函数里顺手做**，见 `close_risk_event`——PRD 的语义是
      「工单关闭**之后**才可以」，不是「关单时自动关事件」。

    ## 不变量

    非法迁移在**写任何行之前**就被拒（`WorkOrderConflictError` → 409）。
    """
    if to_status not in WORK_ORDER_STATUSES:
        raise WorkOrderValidationError(f"unknown status {to_status!r}")
    # `event_id` 只在关单时有意义：其它迁移顺带带一个 event_id 属于调用方写错，
    # 在写任何行之前就拒（422），别让它悄悄变成「关了个不相干的事件」。
    if event_id is not None and to_status != "closed":
        raise WorkOrderValidationError("只有关闭工单时可以同时关闭风险事件")

    relation = await _scope_relation(
        session, identity, action="work_order.transition", target=f"work_order:{order_id}"
    )
    order = await session.get(WorkOrder, order_id)
    if order is None:
        _audit(
            identity,
            action="work_order.transition",
            target=f"work_order:{order_id}",
            result="not_found",
        )
        raise WorkOrderNotFoundError(f"work order {order_id} not found")
    await _require_in_scope(
        session, identity, order, relation=relation, action="work_order.transition"
    )

    from_status = order.status
    allowed = ALLOWED_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        _audit(
            identity,
            action="work_order.transition",
            target=f"work_order:{order_id}",
            result="conflict",
            reason="illegal_transition",
            from_status=from_status,
            to_status=to_status,
        )
        raise WorkOrderConflictError(
            f"cannot move work order from {from_status!r} to {to_status!r}"
        )

    # ---- 关闭分支的三条必填校验（先校验，再动任何字段）
    if to_status == "closed":
        if false_alarm:
            if not (false_alarm_reason or "").strip():
                raise WorkOrderValidationError("标注误报时必须填写误报原因")
        elif not (note or "").strip():
            raise WorkOrderValidationError("关闭工单必须填写处置结果")
        if order.severity == "red" and actual_handled_at is None:
            raise WorkOrderValidationError("红色工单关单必须填写实际处置时间")

    if to_status == "reopened":
        try:
            check_roles(identity, ("admin",))
        except RoleForbiddenError:
            _audit(
                identity,
                action="work_order.transition",
                target=f"work_order:{order_id}",
                result="forbidden",
                reason="reopen_admin_only",
            )
            raise
        if not (note or "").strip():
            raise WorkOrderValidationError("重开工单必须填写重开原因")

    # ---- 复合关闭：关单 + 关事件同一事务（第五轮 FR-ALT-02）
    # 把「工单已关、风险还开着」堵死在服务层：先校验事件归属，任何一条不成立
    # 就在动字段之前抛错；调用方回滚整笔事务，工单与事件都保持原样。
    risk_event: RiskEvent | None = None
    if to_status == "closed" and event_id is not None:
        if order.event_id is None:
            raise WorkOrderConflictError("该工单没有关联风险事件，不能按事件关闭")
        if order.event_id != event_id:
            raise WorkOrderConflictError("event_id 与工单关联的风险事件不一致")
        risk_event = await session.get(RiskEvent, event_id)
        if risk_event is None:
            raise WorkOrderConflictError("关联的风险事件不存在")
        if risk_event.elder_id != order.elder_id:
            raise WorkOrderConflictError("关联的风险事件不属于该工单的长者")
        if risk_event.status == "closed":
            raise WorkOrderConflictError("关联的风险事件已关闭，无需重复关闭")

    now = datetime.datetime.now(datetime.UTC)
    order.status = to_status
    if to_status == "closed":
        order.closed_at = now
        order.false_alarm = bool(false_alarm)
        order.false_alarm_reason = false_alarm_reason if false_alarm else None
        if actual_handled_at is not None:
            order.actual_handled_at = actual_handled_at
        if risk_event is not None:
            # 同一事务里的第二步：事件与工单一起落，任一步失败两边都不留痕。
            risk_event.status = "closed"
            risk_event.closed_at = now
    if note is not None and note.strip():
        # 处置结果落在工单 `note`（当前处置口径）；历史备注在 transitions 行里各留各的。
        order.note = note.strip()

    session.add(
        WorkOrderTransition(
            transition_id=uuid.uuid4(),
            order_id=order.order_id,
            from_status=from_status,
            to_status=to_status,
            actor_id=_staff_uuid(identity),
            at=now,
            note=(note or "").strip() or None,
        )
    )
    await session.flush()

    await audit_service.write_audit(
        session,
        actor_type=audit_service.actor_type_of(identity.kind),
        actor_id=_staff_uuid(identity),
        action="work_order.transition",
        resource_type="work_order",
        resource_id=order.order_id,
        detail={
            "from_status": from_status,
            "to_status": to_status,
            "false_alarm": bool(false_alarm),
            # 复合关闭时把事件也写进同一条审计：审计里能直接看出「谁在什么时候
            # 关的单、关的是哪条风险」（FR-ALT-02 的验收口径）。
            "risk_event_closed": str(risk_event.event_id) if risk_event is not None else None,
            # `audit_logs.actor_type` 只有四个 CHECK 取值，工牌照旧记成 `staff`；
            # 「这次是工牌还是密码会话」靠这一键区分，否则工牌动作在审计里查不出。
            "principal_kind": identity.kind,
        },
        trace_id=get_trace_id(),
    )

    if risk_event is not None:
        await audit_service.write_audit(
            session,
            actor_type=audit_service.actor_type_of(identity.kind),
            actor_id=_staff_uuid(identity),
            action="risk_event.close",
            resource_type="risk_event",
            resource_id=risk_event.event_id,
            detail={"work_order_id": str(order.order_id), "via": "work_order.transition"},
            trace_id=get_trace_id(),
        )

    logger.info(
        "work_order transition order_no=%s %s→%s actor=%s:%s",
        order.order_no,
        from_status,
        to_status,
        identity.kind,
        identity.principal_id,
    )
    # C1（2026-11-13 产品负责人裁决）：接单/关闭后给该长者发一条家属可见的通知。
    # 消息里只放单号与目标状态，不放处置结果——家属要的是「有人在处理」，
    # 处置细节留在照护台（PRD 家属端只读日报与提醒）。
    if to_status in ("acknowledged", "closed"):
        await notification_service.create_notification(
            session,
            kind="work_order",
            severity=order.severity,
            elder_id=order.elder_id,
            payload={
                "order_no": order.order_no,
                "order_id": str(order.order_id),
                "to_status": to_status,
            },
            dedupe_key=f"work_order:{order.order_id}:{to_status}",
        )
    return order


async def close_risk_event(
    session: AsyncSession, identity: Identity, order_id: uuid.UUID
) -> RiskEvent:
    """E18 的服务层：工单**已关闭**之后，把关联风险事件一起闭环。

    为什么单独一个函数、不在 `transition` 里顺手改：

    - PRD:392 的原文是「工单关闭**后**风险事件才可关闭」——这是两步，不是一个
      原子动作。允许「关单 + 同时关事件」就等于允许跳过工单闭环直接抹掉事件。
    - 事件是「发生了什么」的事实记录，工单是「谁去处理」。事件被关闭应当有人
      显式决定，不该是关单的副作用。

    幂等：事件已是 `closed` 时直接返回（不报错）。工单未关闭 → 409。
    """
    relation = await _scope_relation(
        session, identity, action="risk_event.close", target=f"work_order:{order_id}"
    )
    order = await session.get(WorkOrder, order_id)
    if order is None:
        raise WorkOrderNotFoundError(f"work order {order_id} not found")
    await _require_in_scope(session, identity, order, relation=relation, action="risk_event.close")
    if order.event_id is None:
        raise WorkOrderConflictError("该工单没有关联风险事件")
    if order.closed_at is None:
        raise WorkOrderConflictError("工单未关闭，不能先关闭风险事件")

    event = await session.get(RiskEvent, order.event_id)
    if event is None:  # pragma: no cover - 外键 ON DELETE SET NULL 保证不会悬空
        raise WorkOrderConflictError("关联的风险事件已被删除")
    if event.status != "closed":
        event.status = "closed"
        event.closed_at = datetime.datetime.now(datetime.UTC)
        await session.flush()
        await audit_service.write_audit(
            session,
            actor_type=audit_service.actor_type_of(identity.kind),
            actor_id=_staff_uuid(identity),
            action="risk_event.close",
            resource_type="risk_event",
            resource_id=event.event_id,
            detail={"work_order_id": str(order.order_id)},
            trace_id=get_trace_id(),
        )
    return event


__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "MAX_ATTEMPTS",
    "TransitionRow",
    "WorkOrderConflictError",
    "WorkOrderDetail",
    "WorkOrderListItem",
    "WorkOrderNoConflict",
    "WorkOrderNotFoundError",
    "WorkOrderPage",
    "WorkOrderScopeForbiddenError",
    "WorkOrderServiceError",
    "WorkOrderValidationError",
    "close_risk_event",
    "create_work_order",
    "find_open_work_order",
    "get_work_order",
    "list_work_orders",
    "next_order_no",
    "shanghai_today",
    "transition",
]
