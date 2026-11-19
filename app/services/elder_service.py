"""长者档案领域服务（接口契约 E5–E8）。

三条硬口径落在这里，而不是 API 层：

1. **角色与范围判定全部在服务层**（契约 §3「验签 → 角色 → 范围 → 存在」）。
   护工只认 `relation='primary_care'`、医生只认 `relation='doctor'`（A-01 / AC-18），
   不能只按 `staff_id` 过滤；**家属与设备读档案一律 403**（裁决 A-01）。
   家属唯一的读权限是「读日报 / 读通知」，范围取自 `elder_guardians` 里
   `status='active'` 的绑定（W3；`archive_read` 不在放行范围内）。
2. **范围外不泄露存在性**：先判范围再查 `elders`，所以范围外主体拿到的永远是 403，
   不会因为资源不存在而变成 404。
3. **写库只发生在本文件**（AGENTS §2）：API 只调用这里的函数，不拼查询、不写 ORM。

审计口径按契约 A-02：`audit_logs` 表 迭代 9 才建，本轮只写结构化日志，
字段固定为 `trace_id / audit_action / actor / target / result`，**不写姓名、生日、手机号、
token 或任何敏感明文**。
"""

from __future__ import annotations

import datetime
import enum
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError, check_roles
from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.core.security import KIND_GUARDIAN, KIND_STAFF
from app.models import (
    Bed,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    Persona,
    Room,
)

logger = logging.getLogger(__name__)

# R4 的 status 枚举（契约 §2 / TD §3.2）。写进常量而不是散在比较式里：
# 「库里到底认哪几个值」只应有一处定义。
ELDER_STATUSES: tuple[str, ...] = ("active", "away", "transferred", "archived")

# 角色 → 服务层强制的 assignment relation。admin 不在表内，表示不受范围限制。
_RELATION_BY_ROLE: dict[str, str] = {"care_worker": "primary_care", "doctor": "doctor"}

# PATCH 允许字段（契约 E8）。`session_seq` 不在其中：客户端不能碰会话计数器。
# `checkup_start`（FR-HLT-05）在这里，而不是让 API 层自己落库：services 是唯一写库层。
_PATCHABLE_FIELDS: frozenset[str] = frozenset(
    {"name", "birth_date", "gender", "persona_id", "status", "checkup_start"}
)


class ScopeAction(enum.StrEnum):
    """读操作的权限类别 —— 「哪种主体能读」只由这一个开关决定。

    `archive_read` 是长者档案（E6/E7/已绑家属列表），`report_read` 是日报，
    `notification_read` 是通知（本期未落地，先留枚举位，避免下次再改签名）。
    """

    ARCHIVE_READ = "archive_read"
    REPORT_READ = "report_read"
    NOTIFICATION_READ = "notification_read"


#: 家属可用的读操作。**`ARCHIVE_READ` 刻意不在其中**：裁决 A-01 / 契约 O6′
#: 规定家属读长者档案一律 403，放行它就等于把 A-01 拆了。
_GUARDIAN_SCOPE_ACTIONS: frozenset[ScopeAction] = frozenset(
    {ScopeAction.REPORT_READ, ScopeAction.NOTIFICATION_READ}
)

#: 各类读操作被拒时的话术。档案那句是历史口径（迭代 4 起就在用），不要改写。
_DENIAL_MESSAGE: dict[ScopeAction, str] = {
    ScopeAction.ARCHIVE_READ: "elder archive is only readable by staff sessions",
    ScopeAction.REPORT_READ: "daily report is only readable by bound guardians or staff sessions",
    ScopeAction.NOTIFICATION_READ: (
        "notification is only readable by bound guardians or staff sessions"
    ),
}


class ScopeRelation(enum.StrEnum):
    """授权形状（注意：**不是** `elder_staff_assignments.relation` 的取值）。"""

    ALL = "all"  # admin：不限范围
    ASSIGNMENT = "assignment"  # 员工：按 assignment 的 relation 过滤
    GUARDIAN_BINDINGS = "guardian_bindings"  # 家属：按 elder_guardians 的 active 绑定过滤


@dataclass(frozen=True, slots=True)
class ScopeGrant:
    """一次范围判定的结果。`relation` / `guardian_id` 各自只在对应形状下有值。"""

    kind: ScopeRelation
    relation: str | None = None
    guardian_id: uuid.UUID | None = None


class ElderServiceError(Exception):
    """长者领域错误基类：只表达「这次请求被拒」，不构造 HTTP 响应。"""


class ElderNotFoundError(ElderServiceError):
    """范围内主体查了不存在的长者（只有 admin 能走到这里）。"""


class ElderScopeForbiddenError(ElderServiceError):
    """主体种类不符，或长者不在当前员工的责任范围内。"""


class ElderValidationError(ElderServiceError):
    """业务校验失败（如 `archived` 夹带其他字段、persona 不存在）。"""


@dataclass(frozen=True, slots=True)
class ElderDetail:
    """E6 详情：R4 全字段 + 由 `beds` JOIN 得出的 `room_no / bed_no`（裁决 S-05）。

    FR-HLT-05 的三个采集时刻字段：
    `checkup_start` 是长者自己设的值（None = 没设过），
    `checkup_start_effective` 是**实际生效**的时刻（自定义值 or 全局默认），
    `checkup_start_source` 说明这个生效值来自哪边（`elder` / `global`）。
    前端只需要 effective 一个就能显示「他几点采」，另两个用于解释与排障。
    """

    elder_id: uuid.UUID
    name: str
    birth_date: datetime.date | None
    gender: str | None
    persona_id: uuid.UUID | None
    status: str
    session_seq: int
    created_at: datetime.datetime
    room_no: str | None
    bed_no: str | None
    checkup_start: str | None = None
    checkup_start_effective: str | None = None
    checkup_start_source: str | None = None


@dataclass(frozen=True, slots=True)
class ElderListItem:
    """E7 列表项：刻意不含 `birth_date / gender / persona_id`（裁决 S-07）。"""

    elder_id: uuid.UUID
    name: str
    room_no: str | None
    bed_no: str | None
    status: str


@dataclass(frozen=True, slots=True)
class ElderPage:
    items: list[ElderListItem]
    total: int


@dataclass(frozen=True, slots=True)
class ElderGuardianItem:
    """某长者已绑家属（照护台 W4）。

    `relation` / `bound_at` 取自绑定行 `elder_guardians`（绑定级，非账号级）；
    `phone` 保留原文，脱敏由 API 层统一做（与员工/家属列表同一条路径）。
    `binding_id` 是绑定行主键（`elder_guardians.id`），供解绑端点
    `DELETE /api/bindings/{binding_id}` 使用——照护台列表只回显它，不自己猜 ID。
    """

    guardian_id: uuid.UUID
    binding_id: uuid.UUID
    name: str
    phone: str | None
    relation: str
    bound_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class ElderGuardianList:
    items: list[ElderGuardianItem]
    total: int


def _audit(
    identity: Identity,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
    **extra: object,
) -> None:
    """写一条结构化审计日志（A-02）。

    只允许放 ID、字段名与判定结果；调用方不得把姓名、生日、token 等敏感值塞进 `extra`。
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


def _require_admin(identity: Identity, *, action: str, target: str) -> None:
    """写操作角色门禁：非 admin 一律 403（契约 O5 / FR-ADM-01）。

    复用 task-4 的 `check_roles`，让「角色白名单」只有一处实现；拒绝时先落审计再抛，
    否则 E5 的「403 非 admin → 审计」就没有落点。
    """
    try:
        check_roles(identity, ("admin",))
    except RoleForbiddenError:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise


async def _scope_relation(
    session: AsyncSession,
    identity: Identity,
    *,
    action: ScopeAction,
    target: str,
    audit_action: str,
) -> ScopeGrant:
    """读操作的「主体 → 范围」判定（唯一实现），按 `action` 分流。

    - 员工：admin 全量；护工/医生按各自 assignment 的 relation 过滤；
    - 家属：**只**在 `report_read` / `notification_read` 上放行，范围取自
      `elder_guardians` 里 `status='active'` 的绑定；`archive_read` 仍然 403（A-01）；
    - 设备 / 工牌：一律拒绝 —— 工牌不是全权会话（S-24），设备也没有档案视图。

    `session` 眼下只有家属分支用得上（绑定集合由调用方按需查），但签名先留出来：
    哪天员工的 relation 也要查库，调用点不必再改一遍。
    """
    if identity.kind == KIND_STAFF:
        if identity.role == "admin":
            return ScopeGrant(kind=ScopeRelation.ALL)
        relation = _RELATION_BY_ROLE.get(identity.role or "")
        if relation is None:
            # 角色枚举已由 task-4 与 CHECK 约束双重保证；走到这里说明身份被手改坏了。
            _audit(identity, action=audit_action, target=target, result="forbidden", reason="role")
            raise ElderScopeForbiddenError("staff role has no elder scope")
        return ScopeGrant(kind=ScopeRelation.ASSIGNMENT, relation=relation)

    if identity.kind == KIND_GUARDIAN and action in _GUARDIAN_SCOPE_ACTIONS:
        return ScopeGrant(kind=ScopeRelation.GUARDIAN_BINDINGS, guardian_id=_guardian_id(identity))

    _audit(
        identity, action=audit_action, target=target, result="forbidden", reason="principal_kind"
    )
    raise ElderScopeForbiddenError(_DENIAL_MESSAGE[action])


def _guardian_id(identity: Identity) -> uuid.UUID:
    """家属身份里的 guardian_id 已被 Identity 保证为规范 UUID 文本。"""
    if identity.guardian_id is None:  # pragma: no cover - Identity 构造时已保证
        raise ElderScopeForbiddenError("guardian identity is malformed")
    return uuid.UUID(identity.guardian_id)


async def guardian_elder_ids(session: AsyncSession, *, guardian_id: uuid.UUID) -> set[uuid.UUID]:
    """家属当前**有效**绑定的长者集合（`elder_guardians.status='active'`）。

    只认 `active`：`unbound` 是解绑保痕（AC-24），拿它继续授权等于「解绑了还能读」。
    返回 set 而不是 list —— 调用方的语义就是 `elder_id in ...`。
    """
    rows = (
        (
            await session.execute(
                select(ElderGuardian.elder_id).where(
                    ElderGuardian.guardian_id == guardian_id,
                    ElderGuardian.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def require_report_read_scope(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    *,
    action: ScopeAction = ScopeAction.REPORT_READ,
    audit_action: str = "report.read",
) -> None:
    """家属读日报/读通知的范围闸门（家属分支的唯一实现，`report_service` 调它）。

    员工不在这里判：照护台的角色白名单（admin/doctor）属 `report_service`，
    范围仍复用 `get_elder` 的 assignment 口径 —— 两处各判一次角色就会漂移。
    """
    target = f"elder:{elder_id}"
    grant = await _scope_relation(
        session, identity, action=action, target=target, audit_action=audit_action
    )
    if grant.kind is not ScopeRelation.GUARDIAN_BINDINGS:
        return
    guardian_id = grant.guardian_id
    if guardian_id is None:  # pragma: no cover - _scope_relation 已保证
        raise ElderScopeForbiddenError("guardian identity is malformed")
    if elder_id in await guardian_elder_ids(session, guardian_id=guardian_id):
        return
    _audit(identity, action=audit_action, target=target, result="forbidden", reason="scope")
    raise ElderScopeForbiddenError("elder is outside the current guardian scope")


def _staff_id(identity: Identity) -> uuid.UUID:
    """员工身份里的 staff_id 已被 Identity 保证为规范 UUID 文本。"""
    if identity.staff_id is None:  # pragma: no cover - Identity 构造时已保证
        raise ElderScopeForbiddenError("staff identity is malformed")
    return uuid.UUID(identity.staff_id)


def _checkup_start_view(elder: Elder) -> tuple[str | None, str, str]:
    """`(长者自定义值, 生效值, 来源)` —— FR-HLT-05。

    生效值 = `elders.checkup_start or settings.health_checkup_start`，与调度侧的
    `COALESCE(elders.checkup_start, 全局默认)` 完全同一条规则：读接口回显的就是
    采集任务真正会用的那个时刻，不能让页面自己再拼一遍（拼错就会出现「显示 19:00、
    实际 18:30 采」）。`settings` 在调用时读，便于测试用环境变量切换默认时刻。
    """
    if elder.checkup_start:
        return elder.checkup_start, elder.checkup_start, "elder"
    return None, get_settings().health_checkup_start, "global"


def _detail_from(elder: Elder, *, room_no: str | None, bed_no: str | None) -> ElderDetail:
    checkup_start, effective, source = _checkup_start_view(elder)
    return ElderDetail(
        elder_id=elder.elder_id,
        name=elder.name,
        birth_date=elder.birth_date,
        gender=elder.gender,
        persona_id=elder.persona_id,
        status=elder.status,
        session_seq=elder.session_seq,
        created_at=elder.created_at,
        room_no=room_no,
        bed_no=bed_no,
        checkup_start=checkup_start,
        checkup_start_effective=effective,
        checkup_start_source=source,
    )


async def _bed_location(
    session: AsyncSession, elder_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """取长者当前床位与房号；无床位（未入住/已退房）返回 (None, None)。"""
    row = (
        await session.execute(
            select(Bed.bed_no, Room.room_no)
            .join(Room, Room.room_id == Bed.room_id)
            .where(Bed.elder_id == elder_id)
            .order_by(Bed.bed_no)
            .limit(1)
        )
    ).first()
    if row is None:
        return None, None
    return row.bed_no, row.room_no


async def _persona_exists(session: AsyncSession, persona_id: uuid.UUID) -> bool:
    return (
        await session.execute(select(Persona.persona_id).where(Persona.persona_id == persona_id))
    ).first() is not None


async def get_elder(session: AsyncSession, identity: Identity, elder_id: uuid.UUID) -> ElderDetail:
    """E6：admin 全量；护工/医生按各自 relation 判范围；家属/设备 403。"""
    target = f"elder:{elder_id}"
    grant = await _scope_relation(
        session,
        identity,
        action=ScopeAction.ARCHIVE_READ,
        target=target,
        audit_action="elder.read",
    )

    if grant.kind is ScopeRelation.ASSIGNMENT:
        assignment = (
            await session.execute(
                select(ElderStaffAssignment.id)
                .where(
                    ElderStaffAssignment.elder_id == elder_id,
                    ElderStaffAssignment.staff_id == _staff_id(identity),
                    ElderStaffAssignment.relation == grant.relation,
                )
                .limit(1)
            )
        ).first()
        if assignment is None:
            # 范围判定先于存在性判定：范围外主体无论资源在不在，都只看到 403。
            _audit(identity, action="elder.read", target=target, result="forbidden", reason="scope")
            raise ElderScopeForbiddenError("elder is outside the current staff scope")

    elder = await session.get(Elder, elder_id)
    if elder is None:
        _audit(identity, action="elder.read", target=target, result="not_found")
        raise ElderNotFoundError(str(elder_id))

    bed_no, room_no = await _bed_location(session, elder_id)
    return _detail_from(elder, room_no=room_no, bed_no=bed_no)


async def list_elders(
    session: AsyncSession,
    identity: Identity,
    *,
    page: int,
    page_size: int,
    status: str | None,
) -> ElderPage:
    """E7：admin 全量；护工/医生只列各自负责范围；无范围为 empty list，不是 403。"""
    grant = await _scope_relation(
        session,
        identity,
        action=ScopeAction.ARCHIVE_READ,
        target="elder:list",
        audit_action="elder.list",
    )

    filters: list[Any] = []
    if grant.kind is ScopeRelation.ASSIGNMENT:
        filters.append(
            Elder.elder_id.in_(
                select(ElderStaffAssignment.elder_id).where(
                    ElderStaffAssignment.staff_id == _staff_id(identity),
                    ElderStaffAssignment.relation == grant.relation,
                )
            )
        )
    if status is not None:
        filters.append(Elder.status == status)

    total = int(
        (
            await session.execute(select(func.count()).select_from(Elder).where(*filters))
        ).scalar_one()
    )

    rows = (
        await session.execute(
            select(Elder, Bed.bed_no, Room.room_no)
            .outerjoin(Bed, Bed.elder_id == Elder.elder_id)
            .outerjoin(Room, Room.room_id == Bed.room_id)
            .where(*filters)
            .order_by(Elder.elder_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = [
        ElderListItem(
            elder_id=elder.elder_id,
            name=elder.name,
            room_no=room_no,
            bed_no=bed_no,
            status=elder.status,
        )
        for elder, bed_no, room_no in rows
    ]
    return ElderPage(items=items, total=total)


async def list_elder_guardians(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
) -> ElderGuardianList:
    """照护台 W4：某长者已绑家属。admin 全量；护工/医生仅负责范围内（范围外 403）。

    范围判定与 `get_elder` 完全同源（`_scope_relation` + `elder_staff_assignments`），
    且**先判范围再查存在性**：范围外主体永远拿到 403，不会因资源不存在而变成 404。
    只列 `status='active'` 的绑定——`unbound` 是解绑保痕（AC-24），不属于「已绑家属」，
    因此 `total` 恒等于 `len(items)`（本端点不分页）。
    """
    target = f"elder:{elder_id}"
    action = "elder.guardians.read"
    grant = await _scope_relation(
        session,
        identity,
        action=ScopeAction.ARCHIVE_READ,
        target=target,
        audit_action=action,
    )

    if grant.kind is ScopeRelation.ASSIGNMENT:
        assignment = (
            await session.execute(
                select(ElderStaffAssignment.id)
                .where(
                    ElderStaffAssignment.elder_id == elder_id,
                    ElderStaffAssignment.staff_id == _staff_id(identity),
                    ElderStaffAssignment.relation == grant.relation,
                )
                .limit(1)
            )
        ).first()
        if assignment is None:
            _audit(identity, action=action, target=target, result="forbidden", reason="scope")
            raise ElderScopeForbiddenError("elder is outside the current staff scope")

    if await session.get(Elder, elder_id) is None:
        _audit(identity, action=action, target=target, result="not_found")
        raise ElderNotFoundError(str(elder_id))

    rows = (
        await session.execute(
            select(
                ElderGuardian.id,
                GuardianAccount.guardian_id,
                GuardianAccount.name,
                GuardianAccount.phone,
                ElderGuardian.relation,
                ElderGuardian.assigned_at,
            )
            .join(ElderGuardian, ElderGuardian.guardian_id == GuardianAccount.guardian_id)
            .where(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.status == "active",
            )
            .order_by(ElderGuardian.assigned_at, GuardianAccount.guardian_id)
        )
    ).all()

    items = [
        ElderGuardianItem(
            guardian_id=guardian_id,
            binding_id=binding_id,
            name=name,
            phone=phone,
            relation=bound_relation,
            bound_at=bound_at,
        )
        for binding_id, guardian_id, name, phone, bound_relation, bound_at in rows
    ]
    return ElderGuardianList(items=items, total=len(items))


async def create_elder(
    session: AsyncSession,
    identity: Identity,
    *,
    name: str,
    birth_date: datetime.date | None,
    gender: str | None,
    persona_id: uuid.UUID | None,
) -> ElderDetail:
    """E5：仅 admin；`name` 不唯一，允许重名；默认值由数据库给出。"""
    _require_admin(identity, action="elder.create", target="elder:new")

    # 契约 E5 没有 409 口径；persona_id 是请求体里的引用字段，不存在按 422 处理。
    if persona_id is not None and not await _persona_exists(session, persona_id):
        _audit(
            identity,
            action="elder.create",
            target="elder:new",
            result="validation_error",
            reason="persona_not_found",
        )
        raise ElderValidationError("persona does not exist")

    elder = Elder(name=name, birth_date=birth_date, gender=gender, persona_id=persona_id)
    session.add(elder)
    try:
        await session.flush()
        # status / session_seq / created_at 都是 server_default，必须 refresh 才读得到。
        await session.refresh(elder)
    except IntegrityError:
        # 预检查与 flush 之间 persona 被删掉这类竞态：恢复事务后再映射为 422，
        # 不能把 failed transaction 留给请求级依赖去提交。
        await session.rollback()
        _audit(
            identity,
            action="elder.create",
            target="elder:new",
            result="validation_error",
            reason="integrity",
        )
        raise ElderValidationError("elder could not be created") from None

    _audit(
        identity,
        action="elder.create",
        target=f"elder:{elder.elder_id}",
        result="success",
    )
    return _detail_from(elder, room_no=None, bed_no=None)


async def update_elder(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    changes: Mapping[str, Any],
) -> ElderDetail:
    """E8：仅 admin；只改白名单字段；`archived` 只允许改状态（不做物理删除）。"""
    target = f"elder:{elder_id}"
    _require_admin(identity, action="elder.update", target=target)

    unknown = set(changes) - _PATCHABLE_FIELDS
    if unknown:
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="unknown_fields",
        )
        raise ElderValidationError("patch contains fields that are not writable")

    # 角色判定已过，这里才开始查资源：admin 查不到才是 404。
    elder = await session.get(Elder, elder_id)
    if elder is None:
        _audit(identity, action="elder.update", target=target, result="not_found")
        raise ElderNotFoundError(str(elder_id))

    if not changes:
        bed_no, room_no = await _bed_location(session, elder_id)
        return _detail_from(elder, room_no=room_no, bed_no=bed_no)

    new_status = changes.get("status", elder.status)
    if new_status not in ELDER_STATUSES:
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="status",
        )
        raise ElderValidationError("status is not a valid elder status")

    # 「archived 只允许改状态」按生效状态判：已归档的长者也不能借 PATCH 顺手改档案。
    if new_status == "archived" and set(changes) != {"status"}:
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="archived_with_other_fields",
        )
        raise ElderValidationError("an archived elder only allows a status change")

    if changes.get("persona_id") is not None and not await _persona_exists(
        session, changes["persona_id"]
    ):
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="persona_not_found",
        )
        raise ElderValidationError("persona does not exist")

    # 只写白名单字段；`session_seq` 永远不会出现在 changes 里（API 模型 extra=forbid）。
    for field, value in changes.items():
        setattr(elder, field, value)

    try:
        await session.flush()
        await session.refresh(elder)
    except IntegrityError:
        await session.rollback()
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="integrity",
        )
        raise ElderValidationError("elder could not be updated") from None

    _audit(
        identity,
        action="elder.update",
        target=target,
        result="success",
        changed_fields=sorted(changes),
    )
    bed_no, room_no = await _bed_location(session, elder_id)
    return _detail_from(elder, room_no=room_no, bed_no=bed_no)


__all__ = [
    "ELDER_STATUSES",
    "ElderDetail",
    "ElderGuardianItem",
    "ElderGuardianList",
    "ElderListItem",
    "ElderNotFoundError",
    "ElderPage",
    "ElderScopeForbiddenError",
    "ElderServiceError",
    "ElderValidationError",
    "ScopeAction",
    "ScopeRelation",
    "create_elder",
    "get_elder",
    "guardian_elder_ids",
    "list_elder_guardians",
    "list_elders",
    "require_report_read_scope",
    "update_elder",
]
