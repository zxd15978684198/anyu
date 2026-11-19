"""建号领域服务：员工账号、家属账号、家属绑定（T2）。

owner=建号接口（adhoc-20261106）· 依据 设计记录（员工与家属建号接口）

分层定位（AGENTS §2）：`app/services/` 是唯一写库层，本模块只被 `app/api/` 调用。
审计（FR-ADM-04）经 `app/services/audit_service.py` 写 `audit_logs`，这是本模块唯一的
跨服务依赖：建号、改角色/停用、建家属、绑定家属都是不可逆的权限事实，只留日志等于
「事后查不到人」。审计与业务写同事务，写不进去就让操作失败。

口径要点：
- 手机号在 `staff_accounts` 与 `guardian_accounts` 之间**全局唯一** —— 与登录侧
  `login_with_phone` 的跨表碰撞检查保持同一判据（否则登录会出现二义性）。
- 改角色或停用一律 `token_version += 1`，旧 Token 立即失效（AC-24 / FR-ADM-04）。
- 每位长者 `status='active'` 的监护人 ≤ 2：不落 DDL，按 `app/models/relation.py` 头注释的
  「服务层校验」执行，只数 active，`unbound` 不占名额；并发绑定靠长者行锁串行化。
- **校验前置于写库**：抛域异常时不留已 flush 的脏行 —— 调用方 `get_session` 在请求正常
  返回时会 commit，异常若被 API 层捕获成 404/409，脏行就会被一起提交进库。
- 只 `flush()` 不 `commit()`：事务边界交给调用方（API 依赖或测试夹具）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.runtime_context import get_identity
from app.core.security import (
    KIND_DEVICE,
    KIND_GUARDIAN,
    KIND_STAFF,
    KIND_STAFF_CARD,
    STAFF_ROLES,
    hash_password,
)
from app.models.guardian import GuardianAccount
from app.models.organization import Elder
from app.models.relation import ElderGuardian, ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services.audit_service import write_audit

logger = logging.getLogger(__name__)

#: 员工 role → elder_staff_assignments.relation（该列 CHECK 只有 primary_care / doctor）
_ROLE_TO_RELATION: dict[str, str] = {
    "care_worker": "primary_care",
    "doctor": "doctor",
}

#: 每位长者 active 监护人上限（PRD FR-ADM-02 / ER §5.2，服务层校验）
MAX_ACTIVE_GUARDIANS_PER_ELDER = 2


class StaffServiceError(Exception):
    """建号服务的领域异常基类：`message` 可直接回给调用方（不含内部细节）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StaffConflictError(StaffServiceError):
    """手机号已被占用（跨员工/家属两表）。"""


class StaffNotFoundError(StaffServiceError):
    """员工账号不存在。"""


class StaffRoleConflictError(StaffServiceError):
    """角色不允许该操作（如给 admin 分配长者）。"""


class ElderNotFoundError(StaffServiceError):
    """长者不存在。"""


class GuardianNotFoundError(StaffServiceError):
    """家属账号不存在。"""


class GuardianAlreadyBoundError(StaffServiceError):
    """该家属已绑定这位长者。"""


class GuardianLimitExceededError(StaffServiceError):
    """该长者的 active 监护人已满 2 位。"""


class GuardianBindingNotFoundError(StaffServiceError):
    """绑定行不存在（binding_id 不对，或该行已被生命周期清理硬删）。"""


class GuardianAlreadyUnboundError(StaffServiceError):
    """绑定行已是 `unbound`（解绑保痕，AC-24）：重复解绑是状态冲突而不是成功。"""


def mask_phone(phone: str | None) -> str:
    """手机号脱敏：`13800000001` → `138****0001`（响应体与日志统一用它）。"""
    if not phone:
        return ""
    if len(phone) < 7:  # pragma: no cover - 入库前已校验 11 位
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


#: `audit_logs.actor_type` 只有 staff / guardian / system / device 四个合法取值
#: （`app/models/audit.py:34`）；工牌是员工主体，按 staff 落，细节在 `detail.actor`。
_AUDIT_ACTOR_TYPE_BY_KIND: dict[str, str] = {
    KIND_STAFF: "staff",
    KIND_STAFF_CARD: "staff",
    KIND_GUARDIAN: "guardian",
    KIND_DEVICE: "device",
}


def _audit_actor() -> tuple[str, uuid.UUID]:
    """审计主体只从服务端 `runtime_context` 取。

    这四个函数都不收 `identity` 参数（签名由既有 API 层固定），也不能收：
    AGENTS §4 第 9 条——身份只能由服务端注入，客户端或模型传什么都不能当主体。
    请求上下文里没有身份，说明有代码路径绕过了认证层，直接失败而不是写一条
    「actor=未知」的审计把问题掩盖成看起来正常。
    """
    identity = get_identity()
    if identity is None:
        raise StaffServiceError("建号/绑定必须在已认证请求内执行：审计主体缺失")
    actor_type = _AUDIT_ACTOR_TYPE_BY_KIND.get(identity.kind)
    if actor_type is None:
        raise StaffServiceError(f"无法识别的审计主体类型：{identity.kind}")
    return actor_type, uuid.UUID(identity.principal_id)


async def _audit(
    session: AsyncSession,
    actor: tuple[str, uuid.UUID],
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    detail: dict[str, object],
) -> None:
    """写一条建号域审计（FR-ADM-04 / AC-24）。

    不吞异常：凭证与权限变更一旦「操作成功但审计没落」，事后既无法追责也无法证明
    旧 Token 是被谁作废的。`write_audit` 只 flush 不 commit，与业务写同生共死。
    detail 里不放手机号原文与任何口令/哈希。

    `actor` 由调用方在**写库之前**取好（`_audit_actor()`）：上下文里没有身份时
    应该在动任何行之前就失败，而不是写半截再抛。
    """
    actor_type, actor_id = actor
    await write_audit(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
    )


async def _phone_taken(session: AsyncSession, phone: str) -> bool:
    """手机号在两套账号表里是否已被占用。"""
    staff_hit = await session.execute(
        select(StaffAccount.staff_id).where(StaffAccount.phone == phone).limit(1)
    )
    if staff_hit.scalar_one_or_none() is not None:
        return True
    guardian_hit = await session.execute(
        select(GuardianAccount.guardian_id).where(GuardianAccount.phone == phone).limit(1)
    )
    return guardian_hit.scalar_one_or_none() is not None


async def _ensure_elders(session: AsyncSession, elder_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """整批校验长者存在，返回去重后的 id 列表（一条 IN 查询，不逐个往返）。

    去重是必须的：`elder_staff_assignments` 的唯一约束是 (elder_id, staff_id, relation)，
    同一个 id 传两遍会在 flush 时撞唯一约束 —— 客户端的重复项不该变成 500。
    整批先查完再返回，是为了让「有长者不存在」这条路径在写任何行之前就结束。
    """
    wanted = list(dict.fromkeys(elder_ids))
    if not wanted:
        return wanted
    found = set(
        (await session.execute(select(Elder.elder_id).where(Elder.elder_id.in_(wanted))))
        .scalars()
        .all()
    )
    missing = [elder_id for elder_id in wanted if elder_id not in found]
    if missing:
        raise ElderNotFoundError(f"长者不存在：{', '.join(str(elder_id) for elder_id in missing)}")
    return wanted


async def _lock_elder(session: AsyncSession, elder_id: uuid.UUID) -> None:
    """校验长者存在并锁住该行（`SELECT … FOR UPDATE`），供「先数后写」的绑定名额使用。

    没有行锁时，两个并发绑定会各自数到 1 位再各插一行，落成 3 位 active 监护人；
    DDL 的唯一约束是 (elder_id, guardian_id)，看不出位次，拦不住这种超发。
    只锁一位长者，且事务内不再取第二把长者锁，不会构成死锁环。
    """
    hit = await session.execute(
        select(Elder.elder_id).where(Elder.elder_id == elder_id).with_for_update()
    )
    if hit.scalar_one_or_none() is None:
        raise ElderNotFoundError(f"长者不存在：{elder_id}")


async def _clear_staff_assignments(session: AsyncSession, *, staff_id: uuid.UUID) -> int:
    """删除该员工的全部长者绑定，返回删除行数（裁决 D-01 的服务层落地）。

    只在「改成管理员」时调用：管理员的权限不按长者收窄（`_ROLE_TO_RELATION` 里没有
    admin），留着改档前的 `primary_care` / `doctor` 绑定，等于让一个管理员继续带着
    别人名下的长者范围 —— 而这批绑定本来是照护分工，不是管理员权限的来源。

    `elder_staff_assignments` 没有 status 列，所以是**删行**而不是打标记，与
    `app/services/lifecycle_service.py::_release_relations` 的处置一致（同一张表不允许
    出现两套解除语义）。行数回填审计，回答「这次改档顺带清掉了几条线」。
    """
    result = await session.execute(
        delete(ElderStaffAssignment)
        .where(ElderStaffAssignment.staff_id == staff_id)
        .execution_options(synchronize_session=False)
    )
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def create_staff(
    session: AsyncSession,
    *,
    name: str,
    role: str,
    phone: str,
    password: str,
    elder_ids: Sequence[uuid.UUID] = (),
) -> StaffAccount:
    """建员工账号（可选同时分配长者）。

    顺序固定为：角色映射 → 手机号跨表查重 → 长者存在性 → 写 staff → 写 assignment。
    前三步任一失败都在写库之前结束（见模块 docstring 的「校验前置于写库」）。
    """
    # 审计主体先取：上下文里没有身份时必须在动任何行之前失败（校验前置于写库）。
    actor = _audit_actor()

    if role not in STAFF_ROLES:
        raise StaffRoleConflictError(f"未知员工角色：{role}")

    relation: str | None = _ROLE_TO_RELATION.get(role)
    # 三个合法角色里只有 admin 没有映射：管理员不绑长者（裁决 D-01）
    if relation is None and elder_ids:
        raise StaffRoleConflictError("管理员账号不参与长者绑定（裁决 D-01）")

    if await _phone_taken(session, phone):
        raise StaffConflictError("手机号已被其他账号使用")

    assign_elder_ids = await _ensure_elders(session, elder_ids)

    staff = StaffAccount(
        name=name,
        role=role,
        phone=phone,
        password_hash=hash_password(password),
        status="active",
    )
    session.add(staff)
    await session.flush()

    # relation 为 None 只可能是 admin，而 admin 带 elder_ids 已在上方被拒；
    # 带上 relation is not None 的条件，既让类型收窄成立，也避免 admin 空转一次 flush。
    if assign_elder_ids and relation is not None:
        for elder_id in assign_elder_ids:
            session.add(
                ElderStaffAssignment(elder_id=elder_id, staff_id=staff.staff_id, relation=relation)
            )
        await session.flush()

    await _audit(
        session,
        actor,
        action="staff.create",
        resource_type="staff_accounts",
        resource_id=staff.staff_id,
        detail={
            "role": role,
            "phone_masked": mask_phone(phone),
            "assignments": [str(elder_id) for elder_id in assign_elder_ids],
            "relation": relation,
        },
    )
    logger.info(
        "staff created: staff_id=%s role=%s phone=%s assignments=%d",
        staff.staff_id,
        role,
        mask_phone(phone),
        len(assign_elder_ids),
    )
    return staff


async def update_staff(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    role: str | None = None,
    status: str | None = None,
) -> StaffAccount:
    """改角色 / 停用。任一字段真的变了就让旧 Token 失效（`token_version += 1`）。

    改成 **admin** 时额外清空该员工的长者绑定（裁决 D-01，见 `_clear_staff_assignments`）：
    管理员不参与长者绑定，改档后需重新分配。

    取值合法性由请求模型（`Literal`）与表 CHECK 约束兜底，服务层不再重复判定；
    这里只保证「传了但值没变」不会误伤 —— 空 PATCH 与同值 PATCH 都不加版本号。
    """
    actor = _audit_actor()

    staff = (
        await session.execute(select(StaffAccount).where(StaffAccount.staff_id == staff_id))
    ).scalar_one_or_none()
    if staff is None:
        raise StaffNotFoundError(f"员工账号不存在：{staff_id}")

    changed: list[str] = []
    cleared = 0
    if role is not None and role != staff.role:
        staff.role = role
        changed.append("role")
        # 升为管理员的同时解除全部长者绑定（裁决 D-01）：管理员权限不按长者收窄，
        # 保留旧绑定就是留下一条「管理员仍被算作某几位长者的负责护工」的假分工。
        # 重新分配由管理员在照护台显式做，不在这里替他补关系。
        if role == "admin":
            cleared = await _clear_staff_assignments(session, staff_id=staff.staff_id)
    if status is not None and status != staff.status:
        staff.status = status
        changed.append("status")
    if changed:
        staff.token_version = staff.token_version + 1

    await session.flush()
    # 空 PATCH / 同值 PATCH 也写一条（changed 为空）：审计要回答「谁在这个时间点动过
    # 这个账号」，而不只是「值曾经变过」；`token_version` 说明旧凭证有没有被作废。
    await _audit(
        session,
        actor,
        action="staff.update",
        resource_type="staff_accounts",
        resource_id=staff.staff_id,
        detail={
            "changed": changed,
            "role": staff.role,
            "status": staff.status,
            "token_version": staff.token_version,
            "credentials_revoked": bool(changed),
            "assignments_cleared": cleared,
        },
    )
    logger.info(
        "staff updated: staff_id=%s changed=%s token_version=%d",
        staff.staff_id,
        ",".join(changed) or "none",
        staff.token_version,
    )
    return staff


async def create_guardian(
    session: AsyncSession,
    *,
    name: str,
    phone: str,
    password: str,
) -> GuardianAccount:
    """建家属账号（契约缺口 G1）。手机号同样跨两表查重。"""
    actor = _audit_actor()

    if await _phone_taken(session, phone):
        raise StaffConflictError("手机号已被其他账号使用")

    guardian = GuardianAccount(
        name=name,
        phone=phone,
        password_hash=hash_password(password),
        status="active",
    )
    session.add(guardian)
    await session.flush()

    await _audit(
        session,
        actor,
        action="guardian.create",
        resource_type="guardian_accounts",
        resource_id=guardian.guardian_id,
        detail={"phone_masked": mask_phone(phone), "status": guardian.status},
    )
    logger.info("guardian created: guardian_id=%s", guardian.guardian_id)
    return guardian


async def bind_guardian(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    guardian_id: uuid.UUID,
    relation: str,
) -> ElderGuardian:
    """把家属账号绑定到长者（E13 / 契约缺口 G2 的写入侧）。

    判定顺序：长者存在（并加行锁）→ 家属存在 → 同对未绑定 → active 名额未满 → 插入。
    `status` 显式写 `active`：不依赖 INSERT 的 server_default 回填，响应体可直接取值。
    """
    actor = _audit_actor()

    await _lock_elder(session, elder_id)

    guardian = (
        await session.execute(
            select(GuardianAccount.guardian_id).where(GuardianAccount.guardian_id == guardian_id)
        )
    ).scalar_one_or_none()
    if guardian is None:
        raise GuardianNotFoundError(f"家属账号不存在：{guardian_id}")

    existing = (
        await session.execute(
            select(ElderGuardian.id).where(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.guardian_id == guardian_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise GuardianAlreadyBoundError("该家属已绑定这位长者")

    # 只数 active：`unbound` 是解绑保痕（AC-24），不占 2 个名额
    active_count = (
        await session.execute(
            select(func.count())
            .select_from(ElderGuardian)
            .where(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.status == "active",
            )
        )
    ).scalar_one()
    if active_count >= MAX_ACTIVE_GUARDIANS_PER_ELDER:
        raise GuardianLimitExceededError(
            f"该长者 active 监护人已达 {MAX_ACTIVE_GUARDIANS_PER_ELDER} 位"
        )

    binding = ElderGuardian(
        elder_id=elder_id,
        guardian_id=guardian_id,
        relation=relation,
        status="active",
    )
    session.add(binding)
    await session.flush()

    await _audit(
        session,
        actor,
        action="guardian.bind",
        resource_type="elder_guardians",
        resource_id=binding.id,
        detail={
            "elder_id": str(elder_id),
            "guardian_id": str(guardian_id),
            "relation": relation,
            "status": binding.status,
        },
    )
    logger.info(
        "guardian bound: elder_id=%s guardian_id=%s relation=%s",
        elder_id,
        guardian_id,
        relation,
    )
    return binding


# ------------------------------------------------------- 家属解绑（批B / AC-24）


async def unbind_guardian(
    session: AsyncSession,
    *,
    binding_id: uuid.UUID,
    reason: str | None = None,
) -> ElderGuardian:
    """解绑一条家属绑定（AC-24 解绑保痕：`status='unbound'`，**不删行**）。

    ## 为什么只改 status，不删行

    `elder_guardians.status ∈ ('active','unbound')`（`app/models/relation.py:64`）而且
    AC-24 要求「解绑保痕」：家属端要能回答「这位长者曾经绑过谁」。删行会把痕迹连同
    `assigned_at` 一起抹掉，所以这里与 `lifecycle_service` ④ 关系解除同一口径 ——
    打 `unbound` 标记。副作用是 `unbound` 行不占「每位长者最多 2 位 active 家属」的名额
    （`bind_guardian` 只数 active，见 :405-420），绑错一次不会再永久占位。

    模型没有 `unbound_at` / `unbound_reason` 列（技术设计文档 §3.3 未定义），本轮
    **不新增列**：解绑时刻由 `audit_logs.created_at` 承载，解绑原因进审计 `detail.reason`。

    ## 为什么重复解绑报错而不是幂等 204

    DELETE 的常见约定是幂等，但这里 204 会被前端读成「刚刚解绑成功」，掩盖两种真实情况：
    ①这条绑定本来就是 `unbound`（谁解的、什么时候解的都在审计里，应当让操作者看见）；
    ②`binding_id` 根本不存在。两者分开报（409 / 404），前端才能给出「已解绑」与
    「刷新列表，这条绑定已不存在」两种不同提示。审计只记「发生过解绑」这一件事，
    重复请求不会写第二条。

    ## 为什么拆在 `staff_service`

    与 `bind_guardian` 同生共死：同一张表、同一套 `_audit` 写法、同一个
    「actor 只从 `runtime_context` 取」的约定（AGENTS §4 第 9 条）。另建一个
    `guardian_service` 只会让「绑定/解绑」两半落在两个模块里，判据迟早分叉。
    """
    actor = _audit_actor()

    binding = (
        await session.execute(select(ElderGuardian).where(ElderGuardian.id == binding_id))
    ).scalar_one_or_none()
    if binding is None:
        raise GuardianBindingNotFoundError(f"绑定不存在：{binding_id}")
    if binding.status == "unbound":
        raise GuardianAlreadyUnboundError("该绑定已解绑，无需重复操作")

    binding.status = "unbound"
    await session.flush()

    await _audit(
        session,
        actor,
        action="guardian.unbind",
        resource_type="elder_guardians",
        # 审计的 resource 定位到**长者**：解绑影响的是「谁能看这位长者」，
        # 家属侧看板也是按 elder_id 聚合的；binding_id 放 detail。
        resource_id=binding.elder_id,
        detail={
            "binding_id": str(binding.id),
            "elder_id": str(binding.elder_id),
            "guardian_id": str(binding.guardian_id),
            "relation": binding.relation,
            "status": binding.status,
            "reason": reason or "-",
        },
    )
    logger.info(
        "guardian unbound: elder_id=%s guardian_id=%s binding_id=%s",
        binding.elder_id,
        binding.guardian_id,
        binding.id,
    )
    return binding


# ------------------------------------------------------- 照护台读接口（W4）


@dataclass(frozen=True, slots=True)
class StaffListItem:
    """员工列表项。

    这里**保留** `phone` 原文而不是直接给脱敏值：脱敏是「对外视图」的事，由 API 层
    用 `mask_phone` 统一做（与 `_to_response` 同一条路径），服务层不重复一遍。
    """

    staff_id: uuid.UUID
    name: str
    role: str
    phone: str | None
    status: str
    elder_ids: list[uuid.UUID]


@dataclass(frozen=True, slots=True)
class StaffPage:
    items: list[StaffListItem]
    total: int


@dataclass(frozen=True, slots=True)
class GuardianListItem:
    """家属列表项（`guardian_accounts` 无 `created_at` 列，故不含创建时间）。"""

    guardian_id: uuid.UUID
    name: str
    phone: str | None
    status: str


@dataclass(frozen=True, slots=True)
class GuardianPage:
    items: list[GuardianListItem]
    total: int


async def _elder_ids_by_staff(
    session: AsyncSession, staff_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """一页员工的绑定长者：一次查询取齐，避免 N+1。"""
    if not staff_ids:
        return {}
    rows = (
        await session.execute(
            select(ElderStaffAssignment.staff_id, ElderStaffAssignment.elder_id)
            .where(ElderStaffAssignment.staff_id.in_(staff_ids))
            .order_by(ElderStaffAssignment.staff_id, ElderStaffAssignment.elder_id)
        )
    ).all()
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for staff_id, elder_id in rows:
        grouped.setdefault(staff_id, []).append(elder_id)
    return grouped


async def list_staff(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    role: str | None,
) -> StaffPage:
    """照护台员工列表（W4）：`role` 可空，排序键固定 `staff_id` 保证翻页稳定。

    分页边界（`page >= 1`、`1 <= page_size <= 100`）由 API 层的 `Query(ge=, le=)`
    拦截，服务层不再重复判一遍——两处判定迟早分叉。
    """
    filters = [StaffAccount.role == role] if role is not None else []
    total = int(
        (
            await session.execute(select(func.count()).select_from(StaffAccount).where(*filters))
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(StaffAccount)
                .where(*filters)
                .order_by(StaffAccount.staff_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    grouped = await _elder_ids_by_staff(session, [row.staff_id for row in rows])
    items = [
        StaffListItem(
            staff_id=row.staff_id,
            name=row.name,
            role=row.role,
            phone=row.phone,
            status=row.status,
            elder_ids=grouped.get(row.staff_id, []),
        )
        for row in rows
    ]
    return StaffPage(items=items, total=total)


async def list_guardians(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    phone: str | None,
) -> GuardianPage:
    """照护台家属列表（W4）：`phone` 给了就精确匹配（查不到即空列表，不是 404）。"""
    filters = [GuardianAccount.phone == phone] if phone is not None else []
    total = int(
        (
            await session.execute(select(func.count()).select_from(GuardianAccount).where(*filters))
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(GuardianAccount)
                .where(*filters)
                .order_by(GuardianAccount.guardian_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [
        GuardianListItem(
            guardian_id=row.guardian_id,
            name=row.name,
            phone=row.phone,
            status=row.status,
        )
        for row in rows
    ]
    return GuardianPage(items=items, total=total)


__all__ = [
    "MAX_ACTIVE_GUARDIANS_PER_ELDER",
    "ElderNotFoundError",
    "GuardianAccount",
    "GuardianAlreadyBoundError",
    "GuardianAlreadyUnboundError",
    "GuardianBindingNotFoundError",
    "GuardianLimitExceededError",
    "GuardianListItem",
    "GuardianNotFoundError",
    "GuardianPage",
    "StaffAccount",
    "StaffConflictError",
    "StaffListItem",
    "StaffNotFoundError",
    "StaffPage",
    "StaffRoleConflictError",
    "StaffServiceError",
    "bind_guardian",
    "create_guardian",
    "create_staff",
    "list_guardians",
    "list_staff",
    "mask_phone",
    "unbind_guardian",
    "update_staff",
]
