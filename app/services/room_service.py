"""房间读写服务（`GET /api/rooms` 2026-11-11，`POST`/`PATCH` 2026-11-12）。

为什么需要它：照护台「新建设备」必须先选房间，而 E14 的入参是 `room_id`。
在此之前全仓没有任何接口能拿到 `room_id`（`GET /api/elders` 只回 `room_no` 文本），
所以设备生命周期**在物理上无法闭环**——能解绑、不能新建。

2026-11-12 补写口（缺口审计 ADM-01）：原实现在 docstring 里写着「房间是物理实体，
由运维在库里维护，不做接口」，结果照护台既不能建房间也不能把房间置为维护态——
新网点上线必须手工连库。现在补 `create_room` / `update_room` 两个写口，
**只允许管理员**，且只做「房间号 + 维护状态」这两件运维真实会改的事：
床位仍由 `beds` 表维护（建房间不顺手建床，避免用房间接口去做床位管理）。

为什么不分页：房间是物理实体，数量在两位数，翻页只会让前端多写一层状态。
返回 `items + total` 的形状与其它照护台读接口一致，但省掉 `page/page_size`。

为什么只给 admin：房间清单本身不含个人信息，但它决定设备落位，属设备管理的上下文；
与 `GET /api/devices`（`require_roles("admin")`）保持同一权限面，避免出现
「护工能列房间但列不了设备」的半开状态。

**已知约束**：`rooms.room_no` 在库里没有唯一索引（`alembic/versions/0001_identity_skeleton.py`
建表时未加），本卡只允许改 `room_service`/`rooms.py`，不得新增迁移，所以重名拦截是
「服务层先查后写」的最佳努力——并发下两个同名房间仍可能都落库。补齐需要一条
`uq_rooms_room_no` 迁移，归迁移卡的组（T2）处理。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError, check_roles
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.models import Bed, Room

logger = logging.getLogger(__name__)

#: `rooms.status` 的 CHECK 取值（`app/models/organization.py:63`）。
#: 前三个由 beds 聚合推导，`maintenance` 是运维手工置的——写口只该改 `maintenance`，
#: 但既然允许 PATCH，就把四值都放行，由调用方决定语义，服务层不替它猜。
ROOM_STATUSES = ("available", "partial", "full", "maintenance")

#: 房号长度上限（`rooms.room_no` 是 `Text()`，库里不限长；接口限长只为挡住脏输入）。
MAX_ROOM_NO_LENGTH = 32


class RoomServiceError(Exception):
    """房间领域错误基类。"""


class RoomNotFoundError(RoomServiceError):
    """房间不存在 → 404。"""


class RoomValidationError(RoomServiceError):
    """入参不合法（空白房号、超长、未知状态）→ 422。"""


class RoomConflictError(RoomServiceError):
    """房号重复 → 409。"""


@dataclass(frozen=True, slots=True)
class RoomListItem:
    room_id: uuid.UUID
    room_no: str
    status: str
    bed_count: int


@dataclass(frozen=True, slots=True)
class RoomList:
    items: list[RoomListItem]
    total: int


def _audit(
    identity: Identity,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
) -> None:
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


def _require_admin(identity: Identity, *, action: str, target: str) -> None:
    """房间写口仅 admin；复用 task-4 的角色判定并落审计（与 `device_service` 同款）。"""
    try:
        check_roles(identity, ("admin",))
    except RoleForbiddenError:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise


def _normalize_room_no(raw: str) -> str:
    """房号去掉首尾空白后校验；返回归一值供写库与查重共用。

    不做大小写折叠：`A-301` 与 `a-301` 在现实里是同一个房间，但把用户输入改写成
    大写会让「回显的房号」与「他刚才输入的内容」不一致，所以只折叠**查重**。
    """
    room_no = raw.strip()
    if not room_no:
        raise RoomValidationError("room_no must not be blank")
    if len(room_no) > MAX_ROOM_NO_LENGTH:
        raise RoomValidationError(f"room_no must be at most {MAX_ROOM_NO_LENGTH} chars")
    return room_no


def _require_status(status: str) -> str:
    if status not in ROOM_STATUSES:
        raise RoomValidationError(f"status must be one of {ROOM_STATUSES}")
    return status


async def _room_no_taken(
    session: AsyncSession, room_no: str, *, exclude_room_id: uuid.UUID | None = None
) -> bool:
    stmt = select(Room.room_id).where(func.lower(Room.room_no) == room_no.lower())
    if exclude_room_id is not None:
        stmt = stmt.where(Room.room_id != exclude_room_id)
    return (await session.execute(stmt.limit(1))).first() is not None


async def _bed_count(session: AsyncSession, room_id: uuid.UUID) -> int:
    count = await session.scalar(select(func.count(Bed.bed_id)).where(Bed.room_id == room_id))
    return int(count or 0)


async def _record(session: AsyncSession, room: Room) -> RoomListItem:
    return RoomListItem(
        room_id=room.room_id,
        room_no=room.room_no,
        status=room.status,
        bed_count=await _bed_count(session, room.room_id),
    )


async def list_rooms(session: AsyncSession, identity: Identity) -> RoomList:
    """E12：admin 列出全部房间及其床位数（按 `room_no` 排序）。"""
    try:
        check_roles(identity, ("admin",))
    except RoleForbiddenError:
        _audit(identity, action="room.list", target="room:list", result="forbidden")
        raise

    bed_count = (
        select(func.count(Bed.bed_id))
        .where(Bed.room_id == Room.room_id)
        .correlate(Room)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(Room.room_id, Room.room_no, Room.status, bed_count).order_by(Room.room_no)
        )
    ).all()

    items = [
        RoomListItem(room_id=room_id, room_no=room_no, status=status, bed_count=int(count or 0))
        for room_id, room_no, status, count in rows
    ]
    _audit(identity, action="room.list", target="room:list", result="success")
    return RoomList(items=items, total=len(items))


async def create_room(
    session: AsyncSession,
    identity: Identity,
    *,
    room_no: str,
    status: str = "available",
) -> RoomListItem:
    """新建房间（仅 admin）。床位另由 `beds` 维护，这里不建床。

    新房间默认 `available`：一间没有床的房间当然是空房，传 `maintenance` 表示
    「已建好但暂不投用」，两种语义都留口。
    """
    target = f"room:{room_no.strip() or '-'}"
    _require_admin(identity, action="room.create", target=target)

    normalized = _normalize_room_no(room_no)
    _require_status(status)
    if await _room_no_taken(session, normalized):
        _audit(identity, action="room.create", target=target, result="conflict", reason="room_no")
        raise RoomConflictError("room_no already exists")

    room = Room(room_id=uuid.uuid4(), room_no=normalized, status=status)
    session.add(room)
    await session.flush()
    _audit(identity, action="room.create", target=f"room:{room.room_id}", result="success")
    return await _record(session, room)


async def update_room(
    session: AsyncSession,
    identity: Identity,
    room_id: uuid.UUID,
    *,
    room_no: str | None = None,
    status: str | None = None,
) -> RoomListItem:
    """改名 / 改状态（仅 admin）。两个字段都可缺省，缺省即不改。

    为什么要允许改房号：现场贴错牌、房间重编号是运维真实会碰到的事，而房间号是
    照护台选房间的唯一可读标识；没有写口时只能连库改，改错没人留痕。
    """
    target = f"room:{room_id}"
    _require_admin(identity, action="room.update", target=target)

    room = await session.get(Room, room_id)
    if room is None:
        _audit(identity, action="room.update", target=target, result="not_found")
        raise RoomNotFoundError(str(room_id))

    # 先校验全部入参再落任何字段：只改一半就抛异常会让「改房号成功、改状态失败」
    # 变成一次半截写入（会话层虽会 rollback，但报错信息会指向错的原因）。
    normalized = _normalize_room_no(room_no) if room_no is not None else None
    next_status = _require_status(status) if status is not None else None

    if normalized is not None and normalized != room.room_no:
        if await _room_no_taken(session, normalized, exclude_room_id=room.room_id):
            _audit(
                identity, action="room.update", target=target, result="conflict", reason="room_no"
            )
            raise RoomConflictError("room_no already exists")
        room.room_no = normalized
    if next_status is not None:
        room.status = next_status

    await session.flush()
    _audit(identity, action="room.update", target=target, result="success")
    return await _record(session, room)


__all__ = [
    "MAX_ROOM_NO_LENGTH",
    "ROOM_STATUSES",
    "RoomConflictError",
    "RoomList",
    "RoomListItem",
    "RoomNotFoundError",
    "RoomServiceError",
    "RoomValidationError",
    "create_room",
    "list_rooms",
    "update_room",
]
