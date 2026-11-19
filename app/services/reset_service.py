"""演示数据一键重置（FR-ADM-02）。

## 为什么要这个模块

PRD `:596` 把「演示数据重置」列进 迭代 9 **不得裁减**项，但全仓此前只有
`scripts/seed_demo.py`（灌数据）没有「清数据」。演示跑一遍，工单、风险事件、
会话事件、药单、健康记录全变脏，下一次演示就只能手工 `psql` 删——那既没有
二次确认，也不留审计。

## 为什么不 `TRUNCATE`

1. `TRUNCATE` 不能带 `WHERE`，只能整表；而「重置」的语义是**只清演示产生的流水**，
   房间、长者、员工、人设、知识库这些基线主数据必须留着，否则重置就变成
   「重置 + 重跑三个 seed」两件事，违反「一键」。
2. `TRUNCATE` 会隐式提交并抢 `ACCESS EXCLUSIVE` 锁，无法与审计写在同一个事务里。

所以这里一律 `DELETE`，且表名来自本模块的**白名单常量**（`RESET_TABLES`），
不接受任何外部传入的表名。

## 事务边界

与 `work_order_service` / `memory_service` 同口径：本模块**只 `flush`，不 `commit`**，
事务归调用方（`app/api/admin_reset.py` 的 `get_session`）。删除与审计必须在同一个
事务里——「删了 N 行却没留下是谁删的」是不可接受的。
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import check_roles
from app.core.runtime_context import Identity
from app.core.security import canonical_uuid
from app.models.organization import Device
from app.services import audit_service

logger = logging.getLogger(__name__)

#: 删除顺序：**外键依赖倒序**——子表在前、父表在后。
#:
#: 逐条依赖（读 `app/models/*.py` 的 `ForeignKey`）：
#: - `work_order_transitions.order_id` → `work_orders`
#: - `work_orders.event_id` → `risk_events`（SET NULL，但先删子句更省事）
#: - `medication_attempts.reminder_id` → `medication_events`
#: - `medication_events.plan_id` → `medication_plans`
#: - `media_history.media_id` → `media_library`（只删历史，素材表属基线）
#: - 其余（`conversation_events` / `decision_logs` / `notifications` / `health_records` /
#:   `daily_reports` / `memory_chunks` / `voiceprints`）只挂 `elders`，互不依赖。
#:
#: **刻意不清 `audit_logs`**：审计只增不改（TD §3 与 AGENTS §4-10 的留痕口径），
#: 「重置把审计也抹了」等于把事故证据一起删掉，代价远大于演示时审计行多一些。
#:
#: **刻意不清 `medication_plans`**：药单是护工维护的配置而不是演示产生的流水，
#: 且它不在 `BASELINE_TABLES` 里——删了不会随重置恢复，只能重跑 seed。
#: 同理不清 `risk_events` 之外的任何基线主数据。
RESET_TABLES: Final[tuple[str, ...]] = (
    "work_order_transitions",
    "work_orders",
    "risk_events",
    "notifications",
    "conversation_events",
    "conversation_threads",
    "decision_logs",
    "health_records",
    "daily_reports",
    "medication_attempts",
    "medication_events",
    "memory_chunks",
    "media_history",
    "voiceprints",
)

#: **绝不删**的基线主数据。写在这里不是为了删，而是为了让「哪些留下」可断言、
#: 可被测试直接引用（测试拿它跟重置后的行数比对）。
BASELINE_TABLES: Final[tuple[str, ...]] = (
    "rooms",
    "beds",
    "elders",
    "devices",
    "staff_accounts",
    "staff_tokens",
    "guardian_accounts",
    "elder_guardians",
    "elder_staff_assignments",
    "personas",
    "knowledge_documents",
    "knowledge_chunks",
    "media_library",
)

#: 演示设备的固定明文（与 `scripts/seed_demo.py` 的 `DEVICE_SECRET` 同值）。
#: 重置时把 `devices.token_hash` 重算回这个值的 SHA-256，演示即可用同一套
#: 凭证重新登录——不是「轮换」，是「恢复种子值」（重算是幂等的）。
DEVICE_SEED_SECRET: Final[str] = "demo-device-secret-0001"  # noqa: S105  # pragma: allowlist secret

#: 审计动作名。与 `work_order.transition` / `risk_event.close` 同一命名族
#: （`<资源>.<动作>`），所以是 `teaching.reset` 而不是 `reset.teaching`。
RESET_AUDIT_ACTION: Final[str] = "teaching.reset"
RESET_RESOURCE_TYPE: Final[str] = "teaching_reset"

SHANGHAI: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")


class ResetServiceError(Exception):
    """重置相关错误的基类。"""


class ResetConflictError(ResetServiceError):
    """预览行数与确认时回传的行数不符——整体拒绝，一行都不删。"""


class ResetValidationError(ResetServiceError):
    """入参不合法（含确认短语不符）。"""


@dataclass(frozen=True, slots=True)
class ResetPlan:
    """一次重置的「将删/已删」清单。

    `tables` 是**表名 → 行数**；键集合恒等于 `RESET_TABLES`，
    行数是「删了多少」而不是「剩多少」。
    """

    tables: dict[str, int]
    device_tokens_rotated: int

    @property
    def total_rows(self) -> int:
        return sum(self.tables.values())


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _table(name: str) -> Table:
    """按白名单表名取 ORM `Table`。

    表名只来自本模块常量，不接受外部输入；取不到说明白名单与模型漂移了，
    必须当场炸掉而不是静默跳过（跳过等于「以为删了其实没删」）。
    """
    table = Device.metadata.tables.get(name)
    if table is None:
        raise ResetServiceError(f"reset whitelist references unknown table: {name}")
    return table


async def _count_rows(session: AsyncSession, name: str) -> int:
    table = _table(name)
    result = await session.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


async def _table_counts(session: AsyncSession, tables: tuple[str, ...]) -> dict[str, int]:
    return {name: await _count_rows(session, name) for name in tables}


async def preview_teaching_reset(session: AsyncSession) -> ResetPlan:
    """统计「如果现在重置会删掉多少」，**不写任何东西**。

    客户端必须把这里拿到的 `tables` 原样回传给 `apply_teaching_reset`——
    这是二次确认的技术实现，见 `apply_teaching_reset` 的 docstring。
    """
    counts = await _table_counts(session, RESET_TABLES)
    rotated = await _count_devices_needing_reset(session)
    return ResetPlan(tables=counts, device_tokens_rotated=rotated)


async def _count_devices_needing_reset(session: AsyncSession) -> int:
    seed_hash = _sha256_hex(DEVICE_SEED_SECRET)
    result = await session.execute(
        select(func.count())
        .select_from(Device)
        .where((Device.token_hash != seed_hash) | (Device.status != "online"))
    )
    return int(result.scalar_one())


def _diff_counts(expected: dict[str, int], actual: dict[str, int]) -> list[str]:
    """逐表比对，返回人类可读的差异（空列表=一致）。"""
    problems: list[str] = []
    for name in sorted(set(expected) | set(actual)):
        want = expected.get(name, -1)
        got = actual.get(name, -1)
        if want == got:
            continue
        # 缺键时打「未预览」而不是 `None`：客户端漏传与行数变化是两回事，
        # 但两者都必须拒绝（漏传等于「想少确认一张表」）。
        shown_want = "未预览" if name not in expected else str(want)
        shown_got = "未统计" if name not in actual else str(got)
        problems.append(f"{name}: expected={shown_want} actual={shown_got}")
    return problems


async def apply_teaching_reset(
    session: AsyncSession,
    identity: Identity,
    *,
    expected_counts: dict[str, int],
) -> ResetPlan:
    """执行重置。三闸：admin 权限 → `expected_counts` 逐表比对 → 调用方校验确认短语。

    ## `expected_counts` 为什么是二次确认的正确实现

    「点一下确认」挡不住两种事故：**手滑点两次**、**预览与确认之间数据变了**。
    让客户端把预览时的每张表行数原样回传，服务端在删之前重新统计并逐表比对：
    任何一张表对不上就整体拒绝（`ResetConflictError` → 409），**一行都不删**。

    双击天然失效（第二次回传的是删之前的旧行数），竞态也被挡住（预览后新来的
    一条工单会让 `work_orders` 对不上）。

    比对**只针对本次要删的表集合**——客户端多传一个不在集合里的键也算不符，
    否则「传空 dict」就成了绕过二次确认的后门。

    失败顺序有意固定：先查权限、再算实际行数、再比对、最后才删。
    """

    check_roles(identity, ("admin",))

    tables = RESET_TABLES
    known = set(tables)
    unknown = sorted(set(expected_counts) - known)
    if unknown:
        raise ResetValidationError(
            "expected_counts contains tables outside the reset scope: " + ", ".join(unknown)
        )

    actual = await _table_counts(session, tables)
    problems = _diff_counts({k: v for k, v in expected_counts.items()}, actual)
    if problems:
        raise ResetConflictError(
            "teaching reset refused: row counts changed since preview (" + "; ".join(problems) + ")"
        )

    deleted: dict[str, int] = {}
    for name in tables:
        if actual[name] <= 0:
            deleted[name] = 0
            continue
        result = cast(CursorResult[Any], await session.execute(delete(_table(name))))
        deleted[name] = int(result.rowcount or 0)

    rotated = await _restore_device_tokens(session)

    now = datetime.datetime.now(datetime.UTC)
    await audit_service.write_audit(
        session,
        actor_type=audit_service.actor_type_of(identity.kind),
        actor_id=uuid.UUID(canonical_uuid(identity.principal_id)),
        action=RESET_AUDIT_ACTION,
        resource_type=RESET_RESOURCE_TYPE,
        resource_id=audit_service.NIL_RESOURCE_ID,
        detail={
            "deleted": deleted,
            "total_rows": sum(deleted.values()),
            "device_tokens_rotated": rotated,
            "performed_at": now.isoformat(),
        },
    )

    logger.info(
        "teaching reset applied total_rows=%s rotated=%s actor=%s",
        sum(deleted.values()),
        rotated,
        identity.principal_id,
    )
    return ResetPlan(tables=deleted, device_tokens_rotated=rotated)


async def _restore_device_tokens(session: AsyncSession) -> int:
    """把设备凭证与在线状态恢复成种子值，返回被改动的行数。

    刻意**不动** `devices.elder_id` 与 `room_id`：重置是「回到干净起点」，
    不是「解绑」——解绑会让下一次演示连登录都进不去。
    """
    seed_hash = _sha256_hex(DEVICE_SEED_SECRET)
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Device)
            .where((Device.token_hash != seed_hash) | (Device.status != "online"))
            .values(token_hash=seed_hash, status="online")
        ),
    )
    return int(result.rowcount or 0)


__all__ = [
    "BASELINE_TABLES",
    "DEVICE_SEED_SECRET",
    "RESET_AUDIT_ACTION",
    "RESET_TABLES",
    "ResetConflictError",
    "ResetPlan",
    "ResetServiceError",
    "ResetValidationError",
    "apply_teaching_reset",
    "preview_teaching_reset",
]
