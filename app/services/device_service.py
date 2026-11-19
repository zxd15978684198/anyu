"""设备绑定领域服务（接口契约 E9–E11、E13–E14）。

关键口径：

- **`device : elder` 1:1**：service 预检查 + 数据库部分唯一索引 `uq_devices_elder_id` 双保险；
  捕获 `IntegrityError` 后必须 `rollback()` 再映射 409，不能留下 failed transaction。
- **解绑即失效**（AC-24）：`elder_id = NULL`、`status = 'offline'`、轮换 `token_hash`。
  `bind` 传 `elder_id=null` 走**同一个** `_clear_binding`，不存在绕过哈希轮换的后门。
- **候选集只来自床位**：`beds WHERE room_id = devices.room_id AND elder_id IS NOT NULL`，
  不拿 `devices.elder_id` 当集合，因此不会混入他房长者。
- 新 `token_hash` 由 `secrets.token_urlsafe` 生成后做 SHA-256。
  解绑/轮换场景明文在这个函数里就被丢弃（`_clear_binding`）；
  只有 E13/E14（重发凭证 / 新建设备）才把明文**交给调用方一次**（`_issue_secret`），
  由 API 层原样回显，不落库、不记录、不进审计。
- **房间缺失归 409 不归 404**（E9 与 E14 同口径）：契约只把「设备不存在」定义为 404，
  房间是请求体里的目标而非路径资源，缺了等价于「目标房间没有床位」。

审计（契约 A-02 + FR-ADM-04 / AC-24）分两层，缺一不可：

- 结构化日志：`trace_id / audit_action / actor / target / result / reason`，
  给运维现场排障用，不含 token、哈希或长者姓名。
- `audit_logs` 落库：`action / actor_type / actor_id / resource_type / resource_id /
  detail / trace_id`，给「谁在何时对谁做了什么」的事后追责用。凭证类变更（绑定、解绑、
  重发凭证、禁用）与业务写**同事务**——审计写不进去就让操作失败，不允许「操作成功但查不到人」。
- 被拒绝的请求（403/409/404）走独立会话写审计：这些路径最终会抛域异常、请求事务回滚，
  同事务写等于把证据一起回滚掉。
"""

from __future__ import annotations

import datetime
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import DEVICE_STATUS_LOCKED, RoleForbiddenError, check_roles
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.core.security import KIND_DEVICE, KIND_GUARDIAN, KIND_STAFF, KIND_STAFF_CARD, sha256_hex
from app.models import Bed, Device, Elder, Room
from app.services.audit_service import write_audit, write_audit_isolated

logger = logging.getLogger(__name__)


class DeviceServiceError(Exception):
    """设备领域错误基类。"""


class DeviceNotFoundError(DeviceServiceError):
    """admin 查询不存在的设备；设备主体自身缺行按撤销处理（S-25），不会走到这里。"""


class DeviceScopeForbiddenError(DeviceServiceError):
    """设备主体查别的设备，或非 admin / 非设备的其他角色访问候选接口。"""


class DeviceConflictError(DeviceServiceError):
    """绑定冲突：长者已绑其他设备、长者不在目标房间床位、目标房间不存在。"""


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    device_id: uuid.UUID
    room_id: uuid.UUID
    elder_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ElderCandidate:
    elder_id: uuid.UUID
    bed_no: str
    display_name: str


@dataclass(frozen=True, slots=True)
class CandidateList:
    room_id: uuid.UUID
    mode: str
    candidates: list[ElderCandidate]


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """E13 重发凭证的返回体：明文 secret 只在这里出现一次。"""

    device_id: uuid.UUID
    device_secret: str
    rotated_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class CreatedDevice:
    """E14 新建设备的返回体：明文 secret 同样只出现一次。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    elder_id: uuid.UUID | None
    device_secret: str
    status: str


#: `audit_logs.actor_type` 的 CHECK 只有 staff / guardian / system / device 四个取值
#: （`app/models/audit.py:34`）。工牌也是员工，所以按 staff 落，主体细节放 `detail.actor`。
_ACTOR_TYPE_BY_KIND: dict[str, str] = {
    KIND_STAFF: "staff",
    KIND_STAFF_CARD: "staff",
    KIND_GUARDIAN: "guardian",
    KIND_DEVICE: "device",
}

#: 审计 target 的 `前缀:uuid` → `audit_logs.resource_type`。
_RESOURCE_TYPE_BY_TARGET_PREFIX: dict[str, str] = {
    "device": "devices",
    "room": "rooms",
    "elder": "elders",
    "staff": "staff_accounts",
}

#: 审计结果取值：只有 success 与业务写同事务，其余都是「拒绝」，需要独立会话留证。
RESULT_SUCCESS: str = "success"


def _audit_actor(identity: Identity) -> tuple[str, uuid.UUID]:
    """把 `runtime_context` 的 Identity 压成 `audit_logs` 的 (actor_type, actor_id)。"""
    actor_type = _ACTOR_TYPE_BY_KIND.get(identity.kind)
    if actor_type is None:
        raise DeviceServiceError(f"unsupported audit actor kind: {identity.kind!r}")
    return actor_type, uuid.UUID(identity.principal_id)


def _audit_resource(target: str) -> tuple[str, uuid.UUID]:
    """把 `device:<uuid>` 这类审计目标拆成 (resource_type, resource_id)。"""
    prefix, _, raw = target.partition(":")
    resource_type = _RESOURCE_TYPE_BY_TARGET_PREFIX.get(prefix)
    if resource_type is None:
        raise DeviceServiceError(f"unsupported audit target: {target!r}")
    try:
        return resource_type, uuid.UUID(raw)
    except ValueError:
        raise DeviceServiceError(f"audit target is not a uuid: {target!r}") from None


async def _audit(
    session: AsyncSession,
    identity: Identity,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
) -> None:
    """写一条设备域审计：结构化日志 + `audit_logs` 行（FR-ADM-04）。

    与旧版本相比只多了一个 `session` 参数：日志行原样保留（既有 caplog 断言与
    「不得泄露明文 secret」的用例都依赖它），新增的是库内可查的审计行。

    分流规则本身就是安全要求：

    - `result == "success"` → `write_audit`（同一请求事务）。写失败**上抛**，
      让整个操作失败；凭证类变更「先成功再补审计」在崩溃窗口里会留下无人可查的
      令牌轮换，正是 FR-ADM-04 要禁止的。
    - 其余（forbidden / not_found / conflict）→ `write_audit_isolated`（独立会话）。
      调用方随后必抛域异常，请求事务会回滚；只有独立会话能把「有人试过」留下来。
      独立会话写失败只记日志，不改变这次请求的 403/404/409 结论。
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
    logger.info("audit", extra=fields)

    actor_type, actor_id = _audit_actor(identity)
    resource_type, resource_id = _audit_resource(target)
    detail: dict[str, object] = {
        "actor": f"{identity.kind}:{identity.principal_id}",
        "result": result,
        "target": target,
    }
    if reason is not None:
        detail["reason"] = reason

    if result == RESULT_SUCCESS:
        await write_audit(
            session,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
        )
        return

    await write_audit_isolated(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
    )


async def _require_admin(
    session: AsyncSession, identity: Identity, *, action: str, target: str
) -> None:
    """E9/E10/E13/E14/禁用仅 admin；复用 task-4 的角色判定并落审计。"""
    try:
        check_roles(identity, ("admin",))
    except RoleForbiddenError:
        await _audit(
            session, identity, action=action, target=target, result="forbidden", reason="role"
        )
        raise


def _clear_binding(device: Device) -> None:
    """解绑的唯一定义：清长者、置 offline、轮换长期令牌哈希。

    `bind(elder_id=null)` 与 `unbind` 都调用本函数，防止出现一条不轮换哈希的旁路。
    新哈希来自密码学安全随机值，明文 secret 在函数内即被丢弃，不返回也不记录。
    """
    device.elder_id = None
    device.status = "offline"
    device.token_hash = sha256_hex(secrets.token_urlsafe(32))


def _issue_secret() -> tuple[str, str]:
    """生成一对 (明文 secret, SHA-256 哈希)。

    与 `_clear_binding` 的区别：解绑只需要让旧凭证失效，明文可以当场丢弃；
    签发凭证必须把明文**交给调用方一次**，因此这里显式返回，由调用方负责
    「只回显一次、不落库、不进日志、不进审计」。
    """
    plaintext = secrets.token_urlsafe(32)
    return plaintext, sha256_hex(plaintext)


def _binding(device: Device) -> DeviceBinding:
    return DeviceBinding(
        device_id=device.device_id, room_id=device.room_id, elder_id=device.elder_id
    )


def _mask_display_name(name: str) -> str:
    """候选姓名脱敏：姓 + `*` + 末字（裁决 S-06，例 张桂芬 → 张*芬）。"""
    if len(name) <= 1:
        return name
    return f"{name[0]}*{name[-1]}"


async def bind_device(
    session: AsyncSession,
    identity: Identity,
    device_id: uuid.UUID,
    *,
    room_id: uuid.UUID,
    elder_id: uuid.UUID | None,
) -> DeviceBinding:
    """E9：admin 把设备绑到房间（及可选长者）。

    长者必须在目标房间的床位里，否则 409；长者已绑其他设备也是 409。
    预检查负责给出可读原因，唯一索引负责兜住并发；两者都在，不是二选一。
    """
    target = f"device:{device_id}"
    await _require_admin(session, identity, action="device.bind", target=target)

    device = await session.get(Device, device_id)
    if device is None:
        await _audit(session, identity, action="device.bind", target=target, result="not_found")
        raise DeviceNotFoundError(str(device_id))

    room = await session.get(Room, room_id)
    if room is None:
        # 契约 E9 只把「设备不存在」定义为 404；房间不存在等价于目标房间没有床位，归 409。
        await _audit(
            session, identity, action="device.bind", target=target, result="conflict", reason="room"
        )
        raise DeviceConflictError("target room does not exist")

    if elder_id is None:
        # 显式绑定为「空长者」＝解绑语义，必须同样轮换哈希（AC-24）。
        device.room_id = room_id
        _clear_binding(device)
        await _flush_or_conflict(session, identity, action="device.bind", target=target)
        await _audit(session, identity, action="device.bind", target=target, result="success")
        return _binding(device)

    in_room = (
        await session.execute(
            select(Bed.bed_id).where(Bed.room_id == room_id, Bed.elder_id == elder_id).limit(1)
        )
    ).first()
    if in_room is None:
        await _audit(
            session,
            identity,
            action="device.bind",
            target=target,
            result="conflict",
            reason="elder_not_in_room",
        )
        raise DeviceConflictError("elder is not in a bed of the target room")

    other_device = (
        await session.execute(
            select(Device.device_id)
            .where(Device.elder_id == elder_id, Device.device_id != device_id)
            .limit(1)
        )
    ).first()
    if other_device is not None:
        await _audit(
            session,
            identity,
            action="device.bind",
            target=target,
            result="conflict",
            reason="elder_already_bound",
        )
        raise DeviceConflictError("elder is already bound to another device")

    device.room_id = room_id
    device.elder_id = elder_id
    await _flush_or_conflict(session, identity, action="device.bind", target=target)
    await _audit(session, identity, action="device.bind", target=target, result="success")
    return _binding(device)


async def unbind_device(
    session: AsyncSession, identity: Identity, device_id: uuid.UUID
) -> DeviceBinding:
    """E10：admin 解绑；旧设备会话因 token_hash 轮换而立即 401。"""
    target = f"device:{device_id}"
    await _require_admin(session, identity, action="device.unbind", target=target)

    device = await session.get(Device, device_id)
    if device is None:
        await _audit(session, identity, action="device.unbind", target=target, result="not_found")
        raise DeviceNotFoundError(str(device_id))

    _clear_binding(device)
    await _flush_or_conflict(session, identity, action="device.unbind", target=target)
    await _audit(session, identity, action="device.unbind", target=target, result="success")
    return _binding(device)


async def issue_credential(
    session: AsyncSession, identity: Identity, device_id: uuid.UUID
) -> IssuedCredential:
    """E13：admin 给已存在设备重发长期凭证（裁决 D2-A）。

    走的是「先解绑再签发」：`_clear_binding` 已经把 `elder_id` 清空、`status` 置
    `offline`、旧哈希轮换掉，这里再把新哈希写进去并**把明文返回一次**。

    为什么不是「换个哈希但保留绑定」：旧凭证明文早已丢失（`unbind` 时就丢了），
    现场重发后如果长者绑定还在，同一台平板换凭证不需要人工重绑；但那样一来
    「凭证失效」与「绑定关系状态」就解耦了，排查掉线时分不清是凭证问题还是绑定问题。
    统一成「重发 = 重新上线一台空设备」，语义只有一种。
    """
    target = f"device:{device_id}"
    await _require_admin(session, identity, action="device.credential", target=target)

    device = await session.get(Device, device_id)
    if device is None:
        await _audit(
            session, identity, action="device.credential", target=target, result="not_found"
        )
        raise DeviceNotFoundError(str(device_id))

    plaintext, hashed = _issue_secret()
    _clear_binding(device)
    device.token_hash = hashed
    rotated_at = datetime.datetime.now(datetime.UTC)

    await _flush_or_conflict(session, identity, action="device.credential", target=target)
    await _audit(session, identity, action="device.credential", target=target, result="success")
    return IssuedCredential(
        device_id=device.device_id, device_secret=plaintext, rotated_at=rotated_at
    )


async def disable_device(
    session: AsyncSession,
    identity: Identity,
    device_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> DeviceBinding:
    """FR-ADM-04：admin 主动禁用一台设备（`devices.status = 'locked'`）。

    禁用本身就是一次凭证失效：`app/services/auth_service.py` 的登录分支对 `locked`
    直接拒绝，`app/scheduler/devices.py` 也把 `locked` 当成「管理员主动停用」跳过掉线扫描。
    因此这条路径必须留下能追到人的审计行——否则一台平板是被谁、在什么时候、以什么理由
    停掉的，事后只能靠猜。

    `reason` 只进审计 detail（例如 `lost` / `returned` / `maintenance`），不参与任何判定。
    """
    target = f"device:{device_id}"
    await _require_admin(session, identity, action="device.disable", target=target)

    device = await session.get(Device, device_id)
    if device is None:
        await _audit(session, identity, action="device.disable", target=target, result="not_found")
        raise DeviceNotFoundError(str(device_id))

    if device.status == DEVICE_STATUS_LOCKED:
        # 重复禁用不改状态：返 409 而不是静默成功，避免「以为刚禁用的」把上一轮的
        # 禁用人与理由覆盖掉。
        await _audit(
            session,
            identity,
            action="device.disable",
            target=target,
            result="conflict",
            reason="already_disabled",
        )
        raise DeviceConflictError("device is already disabled")

    device.status = DEVICE_STATUS_LOCKED
    await _flush_or_conflict(session, identity, action="device.disable", target=target)
    await _audit(
        session, identity, action="device.disable", target=target, result="success", reason=reason
    )
    return _binding(device)


async def create_device(
    session: AsyncSession,
    identity: Identity,
    *,
    room_id: uuid.UUID,
    elder_id: uuid.UUID | None = None,
) -> CreatedDevice:
    """E14：admin 新建设备并当场签发长期凭证。

    新设备一律 `status='offline'`、`last_heartbeat=None`：还没连过线，
    把自己报成 online 只会让照护台显示一个永远不心跳的「在线」设备。
    """
    target = f"room:{room_id}"
    await _require_admin(session, identity, action="device.create", target=target)

    room = await session.get(Room, room_id)
    if room is None:
        # 与 E9 同口径：房间不是 HTTP 路径资源，缺失归 409（目标房间不存在）。
        await _audit(
            session,
            identity,
            action="device.create",
            target=target,
            result="conflict",
            reason="room",
        )
        raise DeviceConflictError("target room does not exist")

    if elder_id is not None:
        in_room = (
            await session.execute(
                select(Bed.bed_id).where(Bed.room_id == room_id, Bed.elder_id == elder_id).limit(1)
            )
        ).first()
        if in_room is None:
            await _audit(
                session,
                identity,
                action="device.create",
                target=target,
                result="conflict",
                reason="elder_not_in_room",
            )
            raise DeviceConflictError("elder is not in a bed of the target room")

    plaintext, hashed = _issue_secret()
    device = Device(room_id=room_id, elder_id=elder_id, token_hash=hashed, status="offline")
    session.add(device)
    await _flush_or_conflict(session, identity, action="device.create", target=target)

    created_target = f"device:{device.device_id}"
    await _audit(session, identity, action="device.create", target=created_target, result="success")
    return CreatedDevice(
        device_id=device.device_id,
        room_id=device.room_id,
        elder_id=device.elder_id,
        device_secret=plaintext,
        status=device.status,
    )


async def _flush_or_conflict(
    session: AsyncSession, identity: Identity, *, action: str, target: str
) -> None:
    """flush；唯一约束冲突时 rollback 恢复事务，再映射 409。

    不 rollback 的后果不是「多一条错误日志」，而是同请求后续任何 SQL 都撞
    `current transaction is aborted`，且依赖层会把半成品一起提交或报 500。
    """
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await _audit(
            session, identity, action=action, target=target, result="conflict", reason="integrity"
        )
        raise DeviceConflictError("device binding violates a unique constraint") from None


async def list_elder_candidates(
    session: AsyncSession, identity: Identity, device_id: uuid.UUID
) -> CandidateList:
    """E11：admin 或设备本人读取本房间候选长者（单人间 1 位 / 双人间 2 位）。

    候选来源严格是 `beds`：即使 `devices.elder_id` 为空（刚解绑），只要床位上还有人，
    平板仍需要列出候选供声纹身份使用。
    """
    target = f"device:{device_id}"
    if identity.kind == KIND_STAFF:
        try:
            check_roles(identity, ("admin",))
        except RoleForbiddenError:
            await _audit(
                session,
                identity,
                action="device.candidates",
                target=target,
                result="forbidden",
                reason="role",
            )
            raise
    elif identity.kind == KIND_DEVICE:
        if identity.device_id != str(device_id):
            await _audit(
                session,
                identity,
                action="device.candidates",
                target=target,
                result="forbidden",
                reason="other_device",
            )
            raise DeviceScopeForbiddenError("a device may only query its own candidates")
    else:
        await _audit(
            session,
            identity,
            action="device.candidates",
            target=target,
            result="forbidden",
            reason="principal_kind",
        )
        raise DeviceScopeForbiddenError("candidate list is not available to this principal")

    device = await session.get(Device, device_id)
    if device is None:
        await _audit(
            session, identity, action="device.candidates", target=target, result="not_found"
        )
        raise DeviceNotFoundError(str(device_id))

    rows = (
        await session.execute(
            select(Bed.elder_id, Bed.bed_no, Elder.name)
            .join(Elder, Elder.elder_id == Bed.elder_id)
            .where(Bed.room_id == device.room_id, Bed.elder_id.is_not(None))
            .order_by(Bed.bed_no)
        )
    ).all()

    candidates: list[ElderCandidate] = []
    for elder_id, bed_no, name in rows:
        # SQL 侧已经用 `Bed.elder_id.is_not(None)` 过滤过（空床没有长者），但
        # `beds.elder_id` 列本身可空，ORM 把元组第一项标注成 `UUID | None`。这条
        # `assert` 只是把 WHERE 已经保证的事实告诉类型检查器；除非有人删掉那句过滤，
        # 它永远不成立。
        assert elder_id is not None
        candidates.append(
            ElderCandidate(
                elder_id=elder_id,
                bed_no=bed_no,
                display_name=_mask_display_name(name),
            )
        )
    # PRD §3.2：单人间 1 位、双人间 2 位。0 位（空房）按 single 返回空列表。
    mode = "double" if len(candidates) == 2 else "single"
    return CandidateList(room_id=device.room_id, mode=mode, candidates=candidates)


@dataclass(frozen=True, slots=True)
class DeviceListItem:
    """照护台设备列表项。

    `bound` 是 `elder_id` 是否存在的派生值（解绑即 `elder_id = NULL`，AC-24），
    不额外读 `status` 判断——解绑把状态置 `offline`，但「离线」与「未绑定」不是一回事。
    刻意不含 `token_hash`：它不是对外字段，连查询都不参与。
    """

    device_id: uuid.UUID
    elder_id: uuid.UUID | None
    elder_name: str | None
    room_no: str | None
    bed_no: str | None
    bound: bool
    status: str | None
    last_seen_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class DevicePage:
    items: list[DeviceListItem]
    total: int


async def list_devices(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    bound: bool | None,
) -> DevicePage:
    """照护台设备列表（W4）：`bound` 可空；房间取设备自身 `room_id`，床位取所绑长者的床位。

    只 SELECT 展示字段（含 `last_heartbeat` 作为 `last_seen_at`），**不选整行**，
    因此 `token_hash` 不会因为「顺手 select(Device)」而进入响应体。
    """
    filters = []
    if bound is True:
        filters.append(Device.elder_id.is_not(None))
    elif bound is False:
        filters.append(Device.elder_id.is_(None))

    total = int(
        (
            await session.execute(select(func.count()).select_from(Device).where(*filters))
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(
                Device.device_id,
                Device.elder_id,
                Elder.name,
                Room.room_no,
                Bed.bed_no,
                Device.status,
                Device.last_heartbeat,
            )
            .outerjoin(Elder, Elder.elder_id == Device.elder_id)
            .outerjoin(Room, Room.room_id == Device.room_id)
            .outerjoin(Bed, Bed.elder_id == Device.elder_id)
            .where(*filters)
            .order_by(Device.device_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = [
        DeviceListItem(
            device_id=device_id,
            elder_id=elder_id,
            elder_name=elder_name,
            room_no=room_no,
            bed_no=bed_no,
            bound=elder_id is not None,
            status=status,
            last_seen_at=last_heartbeat,
        )
        for device_id, elder_id, elder_name, room_no, bed_no, status, last_heartbeat in rows
    ]
    return DevicePage(items=items, total=total)


# ------------------------------------------------------------------ 心跳

#: FR-ADM-10 心跳的最小写入间隔（秒）。同一设备一分钟内只落一次，避免每个请求一条
#: UPDATE 把鉴权路径的写放大成性能问题。
HEARTBEAT_MIN_INTERVAL_SECONDS: int = 60


async def touch_heartbeat(
    session: AsyncSession,
    device_id: str,
    *,
    now: datetime.datetime | None = None,
) -> bool:
    """刷新 `devices.last_heartbeat`（FR-ADM-10），返回本次是否真的写了。

    三条设计取舍：

    1. **条件写在 SQL 里**，不做「先读再写」：`last_heartbeat IS NULL OR
       last_heartbeat < cutoff` 让并发的两个请求只有一个能命中，不需要 Redis 锁，
       也不会因为两次读之间插进一次写而多写一条。
    2. **不放进 `app/core/auth.py`**：那里的 `_load_device_row` 明确只取凭证事实、
       不碰心跳（心跳属在线判定，不属鉴权）；而且 AGENTS §2 规定 services 是唯一
       写库层。
    3. **不 commit**：提交留在调用方（API 层），与其它服务函数一致。
    """
    stamp = now or datetime.datetime.now(datetime.UTC)
    cutoff = stamp - datetime.timedelta(seconds=HEARTBEAT_MIN_INTERVAL_SECONDS)
    result = await session.execute(
        update(Device)
        .where(
            Device.device_id == uuid.UUID(device_id),
            or_(Device.last_heartbeat.is_(None), Device.last_heartbeat < cutoff),
        )
        .values(last_heartbeat=stamp)
        .execution_options(synchronize_session=False)
    )
    return bool(cast(CursorResult[Any], result).rowcount)


__all__ = [
    "CandidateList",
    "CreatedDevice",
    "DeviceBinding",
    "DeviceConflictError",
    "DeviceListItem",
    "DeviceNotFoundError",
    "DevicePage",
    "DeviceScopeForbiddenError",
    "DeviceServiceError",
    "ElderCandidate",
    "IssuedCredential",
    "bind_device",
    "create_device",
    "disable_device",
    "issue_credential",
    "list_devices",
    "list_elder_candidates",
    "touch_heartbeat",
    "unbind_device",
]
