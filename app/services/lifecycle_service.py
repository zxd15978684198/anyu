"""退房生命周期编排（迭代 9 · W4b②；迭代 10 W3 扩到「房态/设备面」，FR-ADM-09）。

## 一次退房 = 一个 savepoint 里的七个步骤 + 一条摘要审计

| 步骤 | 落哪张表 | 怎么处置 |
| --- | --- | --- |
| ① 记忆清理 | `memory_chunks` | **硬删**该长者全部行（`memory_admin_service.purge_elder_memories`） |
| ② 声纹清理 | `voiceprints` | **硬删**（`embedding` 是可离线比对的生物特征，不能留） |
| ③ 会话收口 | `conversation_threads` | 未结束（`ended_at IS NULL`）的写 `ended_at=now()` |
| ④ 关系解除 | `elder_guardians` / `elder_staff_assignments` | 有 status 列的**打标记**，没有的**删行** |
| ⑤ 设备解绑 | `devices` | 该长者名下设备：`elder_id=NULL`、`status='offline'`、**轮换 `token_hash`**（旧设备令牌立即失效） |
| ⑥ 工牌吊销 | `staff_tokens` | 删掉**这些设备上**签出的工牌（设备已解绑，卡再有效也没有可用的房间端） |
| ⑦ 房态收口 | `elders` | `status='away'`（`archive=True` 时 `'archived'`）；归档时刻只进审计 `detail.archived_at` |
| ⑧ 留痕 | `audit_logs` | 一条 `elder.checkout`，`detail` 带上面各步的计数与状态迁移 |

七步 + 审计都写在**同一个 `SAVEPOINT`** 里：任一步抛错，`ROLLBACK TO SAVEPOINT` 把前面
已经执行的删除一并抹掉，调用方即使 catch 住异常继续跑，也看不到「记忆删了、声纹还在」
的半退房状态。仍然**不 commit** —— 事务边界归调用方（`get_session` 成功提交、异常回滚）。

## 为什么设备面也归本函数（迭代 10 口径修正）

迭代 9 把「房态/床位/设备流转」留给另一张卡，结果是退房只清了数据面、长者仍挂在
`active`，设备仍绑着已退房的长者 —— `elders.status` 才是各调度链路的**唯一**在场判据
（`app/scheduler/health.py:107`、`maintenance.py:244`、`reports.py:62` 都按 `status='active'`
过滤）。所以 迭代 10 把「解绑 + 轮换令牌 + 置 away」并进来，退房才真正意味着「人不在房间里，
任何按在场状态过滤的链路都不再为他产生事件」。

设备解绑与 `app/services/device_service._clear_binding` **同一口径**（清 elder、置 offline、
轮换 `token_hash`）；不调用那个函数是因为它在 `device_service` 里、且需要 `Identity`，
在这里复用会引入 services 之间的横向依赖并绕开本函数的 SAVEPOINT 语义。

## 为什么硬删这些设备上的 `staff_tokens`

工牌 = 「某员工 × 某设备 × 一组 scope」，设备既然已经解绑且令牌轮换，这张卡在实际链路上
既拿不到该长者、也不再对应任何在场设备；留着它只会让 `staff_tokens` 表持续积累对不上任何
设备的幽灵凭证。删行 = 立即吊销，且 `exchange_staff_card` 会用「行不存在」回 401
`TOKEN_REVOKED`，与「卡只有到期失效」不冲突（那条说的是**卡本身**不主动复核 DB scope）。


## 为什么表结构差异导致两种「解除」

- `elder_guardians.status ∈ ('active','unbound')`，而且 AC-24 明确「解绑保痕」：
  家属端要看得到「这位长者曾经绑过谁」，所以**打 `unbound` 标记，不删行**。
- `elder_staff_assignments` 只有 `(elder_id, staff_id, relation)` 唯一约束，**没有 status 列**
  （`relation ∈ ('primary_care','doctor')`）：没有标记可打，只能**删行**。
  这是技术设计文档 §3.3 的列定义决定的，不是这里偷懒；`detail.relations_removed`
  记的是「打标记 + 删行」的合计数，两类明细在结构化日志里分开记。

## 为什么不用 `voiceprint_service.delete_all_for_elder`

那个函数**自己 `commit()`**，并且另写一条 `delete_voiceprint` 审计。用它会带来两个后果：
①前一步已提交，后一步失败时退房无法整体回滚（正是本卡要避免的「半途」）；
②一次退房出现两条审计（一条 `delete_voiceprint` + 一条 `elder.checkout`），
摘要的意义被打散。所以这里直接一条 `DELETE`，留痕交给摘要。

## 幂等

重复退房返回**计数全 0 且 `audit_log_id is None`** 的报告，不抛错：
四步都是「匹配 0 行就返回 0」的写法（`DELETE`/`UPDATE` 的 `rowcount`），第二次自然全 0；
没有任何删除发生，就不再写第二条 `elder.checkout` —— 审计记录的是「发生过退房」，
不是「有人又问了一次」。这也决定了 `CheckoutReport.audit_log_id` 必须是可空的。

## 不在本函数的职责里

- 不动 `beds`（`bed.elder_id`）：床位归属由出入院流程按「新长者入住才占床」决定，
  退房只把长者置 away —— 立刻释放床位会让「同日再入住」与「护理记录对账」互相打架，
  且 `beds` 的约束与调度链路无关（没有调度按 `beds.elder_id` 过滤）。
- 不删长者行、不删 `devices` 行：退房是状态迁移，不是数据删除；设备是资产，解绑后
  留给下一位入住者。
- 不做权限判断：`actor_type` / `actor_id` 由 API 层从 `runtime_context` 注入（AGENTS §4.9）。
"""

from __future__ import annotations

import datetime
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import CursorResult, Result, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id
from app.core.security import sha256_hex
from app.models import (
    Device,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    StaffToken,
    Voiceprint,
)
from app.models.conversation import ConversationThread
from app.services.memory_admin_service import (
    purge_elder_memories,
    validate_actor_type,
    write_audit,
)

logger = logging.getLogger("app.services.lifecycle_service")

#: 退房摘要的审计 action（与 `memory.export` 同一套点号词汇）。
AUDIT_ACTION_CHECKOUT: Final[str] = "elder.checkout"


def _now() -> datetime.datetime:
    """统一 UTC（AGENTS §8「存储一律 UTC，展示层转 Asia/Shanghai」）。"""
    return datetime.datetime.now(tz=datetime.UTC)


def _affected(result: Result[Any]) -> int:
    """`DELETE` / `UPDATE` 的受影响行数。

    `Session.execute` 的静态返回类型统一是 `Result`，`rowcount` 只长在运行期真正返回的
    `CursorResult` 上（SQLAlchemy 2.x 没有按语句类型收窄的重载），故在此收窄一次。
    DML 必返回 `CursorResult`；`TypeError` 分支只在有人把 `SELECT` 结果喂进来时才走。
    """
    if not isinstance(result, CursorResult):
        raise TypeError(f"expected CursorResult from DML, got {type(result).__name__}")
    return int(result.rowcount or 0)


@dataclass(frozen=True, slots=True)
class CheckoutReport:
    """一次退房的结果；四个计数即 `audit_logs.detail` 的四个键。

    `frozen=True` + `slots=True`：这是一份回执，不该被调用方改写；字段固定也方便
    API 层直接序列化（键名就是审计 detail 的键名，排查时两边对得上）。

    `audit_log_id` 可空是**语义**而不是技术细节：计数全 0 的重复退房什么都没做，
    也就不留审计，此时为 `None`（见模块 docstring「幂等」）。
    """

    elder_id: uuid.UUID
    memories_purged: int
    voiceprints_removed: int
    sessions_closed: int
    relations_removed: int
    audit_log_id: uuid.UUID | None
    # 迭代 10（FR-ADM-09）新增的房态/设备面计数。全部带默认值：迭代 9 的构造点与用例
    # 按位置写 6 个字段，加默认值才不会让「只是加了一个字段」变成一次破坏性改动。
    devices_unbound: int = 0
    staff_cards_revoked: int = 0
    elder_status: str | None = None
    archived: bool = False


async def _remove_voiceprints(session: AsyncSession, *, elder_id: uuid.UUID) -> int:
    """硬删该长者的全部声纹样本（不分 `active` / `retired`），返回删除行数。

    只按 `elder_id` 过滤、不按 `version` 收窄：退房要抹掉的是**这个人的**生物特征，
    「保留上一版声纹」在这里没有任何正当理由。
    """
    result = await session.execute(delete(Voiceprint).where(Voiceprint.elder_id == elder_id))
    return _affected(result)


async def _close_sessions(session: AsyncSession, *, elder_id: uuid.UUID) -> int:
    """把该长者未结束的会话回填 `ended_at`，返回收口行数。

    `where ended_at IS NULL` 是关键：已经结束的会话不能被退房覆盖成一个新的结束时间
    （那会把「上次说话是什么时候」改掉，日报与排障都依赖它）。`generic` 模式下
    `elder_id` 为 NULL 的会话不属于任何长者，退房自然也不该动。
    """
    result = await session.execute(
        update(ConversationThread)
        .where(
            ConversationThread.elder_id == elder_id,
            ConversationThread.ended_at.is_(None),
        )
        .values(ended_at=_now())
        .execution_options(synchronize_session=False)
    )
    return _affected(result)


async def _release_relations(session: AsyncSession, *, elder_id: uuid.UUID) -> tuple[int, int]:
    """解除家属/护工关系，返回 `(打标记数, 删行数)`。

    两张表的处置不同，原因见模块 docstring「为什么表结构差异导致两种解除」：
    `elder_guardians` 有 `status` → `active` 改 `unbound`（AC-24 解绑保痕）；
    `elder_staff_assignments` 没有 status 列 → 删行。

    只统计**实际发生变化**的行：已经 `unbound` 的家属不会在重复退房时被再数一次。
    """
    unbound = _affected(
        await session.execute(
            update(ElderGuardian)
            .where(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.status == "active",
            )
            .values(status="unbound")
            .execution_options(synchronize_session=False)
        )
    )
    removed = _affected(
        await session.execute(
            delete(ElderStaffAssignment).where(ElderStaffAssignment.elder_id == elder_id)
        )
    )
    return unbound, removed


def _clear_device_binding(device: Device) -> None:
    """解绑设备的唯一定义：清长者、置 offline、轮换长期令牌哈希。

    逐字段与 `app/services/device_service._clear_binding` 保持一致（那条路径服务于
    E9/E10 的显式解绑）。不直接调用它，是因为它住在 `device_service` 里、签名要
    `Identity`，在这里复用会引入 services 之间的横向依赖（AGENTS §2 禁止）。
    """
    device.elder_id = None
    device.status = "offline"
    device.token_hash = sha256_hex(secrets.token_urlsafe(32))


async def _release_devices(session: AsyncSession, *, elder_id: uuid.UUID) -> tuple[int, int]:
    """解绑该长者名下设备并吊销这些设备上签出的工牌，返回 `(解绑设备数, 吊销工牌数)`。

    两步的先后有讲究：先按 `elder_id` 取出设备并**在内存里**解绑，再按这批 device_id
    删工牌。若反序（先删工牌、再按 `elder_id` 找设备），中间任何一步失败都会让
    「卡没了、设备还绑着」这种更糟的状态暴露给同一 savepoint 之外的读者。
    """
    devices = (
        (await session.execute(select(Device).where(Device.elder_id == elder_id))).scalars().all()
    )
    if not devices:
        return 0, 0

    device_ids = [device.device_id for device in devices]
    for device in devices:
        _clear_device_binding(device)

    revoked = _affected(
        await session.execute(delete(StaffToken).where(StaffToken.device_id.in_(device_ids)))
    )
    # 让 ORM 的设备改写在后续同一事务的 `SELECT` 前落成 UPDATE（否则读到的还是旧绑定）。
    await session.flush()
    return len(devices), revoked


#: 普通退房允许的源状态：`away` 已经在场外、`archived` 不允许被「再退一次」复活成 away。
_CHECKOUT_SOURCE_STATUSES: Final[tuple[str, ...]] = ("active", "transferred")
#: 归档允许把已 away 的长者升级为 archived（`archived` 自身仍是终态，不再变）。
_ARCHIVE_SOURCE_STATUSES: Final[tuple[str, ...]] = ("active", "away", "transferred")


async def _close_room_state(
    session: AsyncSession, *, elder_id: uuid.UUID, archive: bool
) -> tuple[str | None, bool]:
    """把长者置 `away`（或 `archived`），返回 `(收口后的状态, 本次是否真的迁移了)`。

    判据刻意写成「源状态白名单」而不是「无条件 UPDATE」：无条件写 `away` 会让
    重复退房每次都 rowcount=1，从而每次都多写一条审计，把「幂等」破坏掉。
    """
    target = "archived" if archive else "away"
    sources = _ARCHIVE_SOURCE_STATUSES if archive else _CHECKOUT_SOURCE_STATUSES
    changed = _affected(
        await session.execute(
            update(Elder)
            .where(Elder.elder_id == elder_id, Elder.status.in_(sources))
            .values(status=target)
            .execution_options(synchronize_session=False)
        )
    )
    current = (
        await session.execute(select(Elder.status).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    return current, changed > 0


async def checkout_elder(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_type: str = "staff",
    trace_id: str | None = None,
    reason: str = "checkout",
    archive: bool = False,
) -> CheckoutReport:
    """退房：一个 savepoint 里完成七步清理/收口 + 一条摘要审计。

    - **原子**：七步与审计同 `SAVEPOINT`，任一步抛错整体回滚（不 commit，事务归调用方）。
    - **幂等**：重复退房返回计数全 0 的报告，`audit_log_id=None`，不抛错也不重复审计
      （源状态白名单保证「已经 away 的长者再退一次」不产生任何行变更）。
    - `reason` 原样写进审计 `detail.reason`：退房（`checkout`）、转院（`transfer`）、
      身故（`deceased`）在业务上是三件事，但数据面处置相同，用同一入口 + 不同 reason
      比复制三个函数更好维护；取值由调用方决定，本函数不枚举白名单（不臆造业务口径）。
    - `archive=True` 时房态落到 `archived` 而不是 `away`，归档时刻写进
      `detail.archived_at`（`elders` 表**没有** `archived_at` 列，本项目不改模型/迁移，
      归档时间以审计为准）。
    - `actor_type` 先校验再动库：非法取值应在**一条语句都没执行**时抛
      `ValueError`，而不是等 `audit_logs` 的 CHECK 在 flush 时炸出一句约束名。

    返回 `CheckoutReport`；`audit_log_id` 就是本次摘要审计的 `audit_id`，
    可直接回给后台做「这次退房的取证编号」。
    """
    validate_actor_type(actor_type)
    moment = _now()

    async with session.begin_nested():
        memories_purged = await purge_elder_memories(session, elder_id=elder_id)
        voiceprints_removed = await _remove_voiceprints(session, elder_id=elder_id)
        sessions_closed = await _close_sessions(session, elder_id=elder_id)
        relations_unbound, relations_deleted = await _release_relations(session, elder_id=elder_id)
        relations_removed = relations_unbound + relations_deleted
        devices_unbound, staff_cards_revoked = await _release_devices(session, elder_id=elder_id)
        elder_status, status_changed = await _close_room_state(
            session, elder_id=elder_id, archive=archive
        )

        total = (
            memories_purged
            + voiceprints_removed
            + sessions_closed
            + relations_removed
            + devices_unbound
            + staff_cards_revoked
            + (1 if status_changed else 0)
        )
        if total == 0:
            # 幂等路径：什么都没发生，不留审计（否则重复退房会堆一串 0 计数记录）。
            report = CheckoutReport(
                elder_id=elder_id,
                memories_purged=0,
                voiceprints_removed=0,
                sessions_closed=0,
                relations_removed=0,
                audit_log_id=None,
                elder_status=elder_status,
                archived=False,
            )
        else:
            detail: dict[str, Any] = {
                "reason": reason,
                "memories_purged": memories_purged,
                "voiceprints_removed": voiceprints_removed,
                "sessions_closed": sessions_closed,
                "relations_removed": relations_removed,
                "relations_unbound": relations_unbound,
                "relations_deleted": relations_deleted,
                "devices_unbound": devices_unbound,
                "staff_cards_revoked": staff_cards_revoked,
                "elder_status": elder_status,
                "status_changed": status_changed,
                "archived_at": moment.isoformat() if archive and status_changed else None,
            }
            audit = await write_audit(
                session,
                action=AUDIT_ACTION_CHECKOUT,
                actor_type=actor_type,
                actor_id=actor_id,
                resource_type="elders",
                resource_id=elder_id,
                detail=detail,
                trace_id=trace_id,
            )
            # flush 后 PG 的 INSERT..RETURNING 通常已经带回 audit_id；再 refresh 一次
            # 是照 memory_service.remember 的既有写法，避免依赖驱动是否回填服务端默认值。
            await session.refresh(audit)
            report = CheckoutReport(
                elder_id=elder_id,
                memories_purged=memories_purged,
                voiceprints_removed=voiceprints_removed,
                sessions_closed=sessions_closed,
                relations_removed=relations_removed,
                audit_log_id=audit.audit_id,
                devices_unbound=devices_unbound,
                staff_cards_revoked=staff_cards_revoked,
                elder_status=elder_status,
                archived=archive and status_changed,
            )

    logger.info(
        "elder_checked_out",
        extra={
            "trace_id": (trace_id or get_trace_id() or "-"),
            "elder_id": str(elder_id),
            "reason": reason,
            "memories_purged": report.memories_purged,
            "voiceprints_removed": report.voiceprints_removed,
            "sessions_closed": report.sessions_closed,
            "relations_removed": report.relations_removed,
            "relations_unbound": relations_unbound,
            "relations_deleted": relations_deleted,
            "devices_unbound": report.devices_unbound,
            "staff_cards_revoked": report.staff_cards_revoked,
            "elder_status": report.elder_status,
            "archived": report.archived,
            "audit_log_id": (str(report.audit_log_id) if report.audit_log_id else None),
        },
    )
    return report


__all__ = [
    "AUDIT_ACTION_CHECKOUT",
    "CheckoutReport",
    "checkout_elder",
]
