"""照护台「记忆管理」后台服务（迭代 9 · W4b①）。

## 在 迭代 7 `memory_service` 之上只加四件事

| 函数 | 干什么 | 审计动作 |
| --- | --- | --- |
| `export_elder_memories` | 导出该长者记忆为 `list[dict]`（含正文，**不含向量**） | `memory.export` |
| `export_memory_jsonl` | 同上，落成 JSONL 文件，返回行数 | `memory.export` |
| `admin_delete_memory` | 软删（`status='deleted'`），可按 `chunk_ids` 收敛 | `delete_memory`（沿用 迭代 7） |
| `purge_elder_memories` | **硬删**该长者全部行（退房终态） | 无（退房摘要审计承担） |

迭代 7 已经把「写入 / 召回 / 衰减 / 容量淘汰 / 删除」落在 `memory_service`。
本文件**只做后台管理面**：导出与人工删除。因此刻意不重写删除语义 ——
「该长者全部 active → deleted」这条路径直接调用 `memory_service.delete_by_elder`，
连它那条 `delete_memory` 审计也一并复用（AC-14：删了必须有痕迹）。

## 四条设计口径

1. **embedding 永不导出**。导出的是「人看得懂的记忆」，高维 float 既不给人读，
   又是可被离线比对的身份/语义指纹 —— 泄漏它的危害和泄漏正文不是一回事。
   记录里因此没有 `embedding` 键（测试 `test_export_omits_embedding` 盯着这一条）。
2. **审计与业务同事务，不是独立会话**。`report_service._write_audit` 用独立会话提交，
   因为越权拒绝那条路径必然抛异常、请求会话会被 rollback（证据不能跟着消失）。
   导出/删除正好相反：**审计必须和导出/删除一起成败**，否则会出现「记了导出但没导出」
   或「删了没人知道」。所以这里 `session.add(AuditLog(...))` + flush，交给调用方的事务。
3. **写动作包一层 SAVEPOINT**（`session.begin_nested()`）。多语句动作（`UPDATE` + 审计）
   一旦中途失败，savepoint 回滚让「半截状态」在**调用方 catch 住异常继续跑**时也不存在；
   全仓口径仍是「服务层只 flush 不 commit」，savepoint 不是提交，事务边界仍归调用方。
4. **`trace_id` 落在审计 `detail` 里**。`audit_logs` 表没有 `trace_id` 列（模型归 Lead，
   本卡不改 schema），而 AGENTS §8 要求日志能按 trace 串起来：入参给了就写进 detail，
   同时进本文件的每条结构化日志。

## `purge_elder_memories` 的语义（明确写死）

**硬删该长者 `memory_chunks` 的全部行，不论 `status`**（`active` / `superseded` / `deleted`
一视同仁），返回删除行数。

理由：退房是终态。若只硬删 `status='deleted'` 的行，退房后仍会留下 `active` 与
`superseded` 的正文与向量 —— 长者已经离开，这些内容既没有业务用途，也不再有任何
「本人可查可改」的正当性，属于数据最小化原则下该消失的东西。软删（可检索性）与硬删
（可恢复性）因此分成两步：`admin_delete_memory` 先降级为 `deleted`（可被 DBA 找回、
召回立刻停止），`purge_elder_memories` 在退房时彻底抹掉。

本函数**不写审计**：它是 `lifecycle_service.checkout_elder` 的一个步骤，留痕由
`elder.checkout` 摘要审计统一承担（一条摘要比四条分项更好读）。若将来单独暴露给后台
排行榜式的「清空」接口，调用方必须先落一条 `memory.purge` 审计再调它。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from sqlalchemy import CursorResult, Result, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id
from app.models import AuditLog, MemoryChunk
from app.services.memory_service import MEMORY_STATUSES, delete_by_elder

logger = logging.getLogger("app.services.memory_admin_service")

# ------------------------------------------------------------------ 常量

#: 导出动作的审计 action（W4b 引入的点号命名，与 `elder.checkout` 同一套词汇）。
AUDIT_ACTION_EXPORT: Final[str] = "memory.export"

#: 软删动作的审计 action。**沿用 `memory_service.delete_by_elder` 的既有取值**，
#: 不另造 `memory.delete`：同一个动作两种名字，审计检索就会漏掉一半。
AUDIT_ACTION_DELETE: Final[str] = "delete_memory"

#: `audit_logs.actor_type` 的四个合法取值（与 `app/models/audit.py` 的 CHECK 同源）。
AUDIT_ACTOR_TYPES: Final[tuple[str, ...]] = ("staff", "guardian", "system", "device")


# ------------------------------------------------------------------ 校验 / 小工具


def _affected(result: Result[Any]) -> int:
    """`DELETE` / `UPDATE` 的受影响行数。

    `Session.execute` 的静态返回类型统一是 `Result`，`rowcount` 只长在运行期真正返回的
    `CursorResult` 上（SQLAlchemy 2.x 没有按语句类型收窄的重载），故在此收窄一次。
    DML 必返回 `CursorResult`；`TypeError` 分支只在有人把 `SELECT` 结果喂进来时才走。
    """
    if not isinstance(result, CursorResult):
        raise TypeError(f"expected CursorResult from DML, got {type(result).__name__}")
    return int(result.rowcount or 0)


def validate_actor_type(actor_type: str) -> str:
    """校验 `actor_type` 是四个合法取值之一，返回原值。

    在服务层拦一次而不是等 `audit_logs` 的 CHECK 约束在 flush 时炸：
    后者抛的是 `IntegrityError`，日志里只剩一句 SQL 约束名，排查时看不出是调用方传错了
    枚举。`lifecycle_service` 也复用这个校验（同一个词汇表，只有一处定义）。
    """
    if actor_type not in AUDIT_ACTOR_TYPES:
        raise ValueError(f"actor_type must be one of {AUDIT_ACTOR_TYPES}")
    return actor_type


def _validate_status(status: str | None) -> str | None:
    """`status` 只接受列 CHECK 里的三个取值；传别的值直接抛，不静默返回空。"""
    if status is not None and status not in MEMORY_STATUSES:
        raise ValueError(f"status must be one of {MEMORY_STATUSES}")
    return status


def _isoformat(value: datetime.datetime | None) -> str | None:
    """UTC 时间 → ISO 8601 字符串；`None` 原样返回（导出要能区分「从未召回」）。"""
    return None if value is None else value.isoformat()


def _to_record(row: MemoryChunk) -> dict[str, Any]:
    """一行 `memory_chunks` → 导出字典（**故意不含 `embedding`**，见模块 docstring 第 1 条）。"""
    return {
        "chunk_id": str(row.chunk_id),
        "elder_id": str(row.elder_id),
        "content": row.content,
        "category": row.category,
        "importance": float(row.importance),
        "status": row.status,
        "source_thread_id": row.source_thread_id,
        "created_at": _isoformat(row.created_at),
        "last_accessed": _isoformat(row.last_accessed),
    }


async def write_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    resource_type: str,
    resource_id: uuid.UUID,
    detail: dict[str, Any],
    trace_id: str | None = None,
) -> AuditLog:
    """**同事务**写一条 `audit_logs` 并 flush，返回该行（`audit_id` 已就绪）。

    与 `report_service._write_audit` 的分工（那边用独立会话）在模块 docstring 第 2 条。
    `actor_id=None` 时按 `system` 记，`actor_id` 回落成 `resource_id`（长者本人/系统代办）
    —— 与 `memory_service.delete_by_elder`、`voiceprint_service.delete_all_for_elder`
    的既有口径一致，也避开 `audit_logs.actor_id NOT NULL` 的约束。
    """
    validate_actor_type(actor_type)

    payload: dict[str, Any] = dict(detail)
    resolved_trace = trace_id or get_trace_id()
    if resolved_trace:
        # `audit_logs` 没有 trace_id 列；放进 detail 才能在链路追踪里反查到这条审计。
        payload["trace_id"] = resolved_trace

    resolved_actor_type = actor_type if actor_id is not None else "system"
    row = AuditLog(
        actor_type=resolved_actor_type,
        actor_id=actor_id if actor_id is not None else resource_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=payload,
    )
    session.add(row)
    await session.flush()
    return row


async def _load_records(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    status: str | None,
) -> list[dict[str, Any]]:
    """该长者的记忆记录（按写入时间正序，导出要可复现）。

    正序而不是后台列表的倒序：导出是「拿走一份完整档案」，人读的时候按时间读；
    倒序是给翻页用的。排序键带 `chunk_id`，同一批写入（`created_at` 相同）时次序稳定，
    两次导出的行序一致才谈得上 diff。
    """
    conditions = [MemoryChunk.elder_id == elder_id]
    if status is not None:
        conditions.append(MemoryChunk.status == status)

    rows = (
        (
            await session.execute(
                select(MemoryChunk)
                .where(*conditions)
                .order_by(MemoryChunk.created_at.asc(), MemoryChunk.chunk_id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_record(row) for row in rows]


# ------------------------------------------------------------------ 1. 导出


async def export_elder_memories(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    status: str | None = None,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "staff",
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    """导出该长者全部记忆为 `list[dict]`（含正文、**不含 `embedding`**），并写导出审计。

    - `status=None` 导出全部状态（`active` / `superseded` / `deleted`）：后台要的是
      「这个人曾经有什么记忆」，不是「现在还能召回的 3 条」；只给 `active` 会让
      「被归并/被删的记忆去哪了」无从回答。
    - `status` 传了非法值 → `ValueError`（不静默当 `None` 处理：那样会导出一份
      「比你以为的多」的档案，比报错危险）。
    - 导出动作与 `memory.export` 审计**同事务**：调用方 rollback 则两者一起消失。
    - `elder_id` 必须来自服务端 `runtime_context`（AGENTS §4.9），本函数不接受
      「从 content 推断是谁」这种口子。

    返回的每个 dict 形如::

        {"chunk_id", "elder_id", "content", "category", "importance",
         "status", "source_thread_id", "created_at", "last_accessed"}

    `importance` 转 `float`（库里是 `numeric(3,2)`，读出来是 `Decimal`；JSON 不认 `Decimal`），
    时间转 ISO 8601 字符串（`None` 保持 `None`）。
    """
    _validate_status(status)
    validate_actor_type(actor_type)

    async with session.begin_nested():
        records = await _load_records(session, elder_id=elder_id, status=status)
        await write_audit(
            session,
            action=AUDIT_ACTION_EXPORT,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type="memory_chunks",
            resource_id=elder_id,
            detail={
                "count": len(records),
                "status": status or "all",
                "target": "list",
            },
            trace_id=trace_id,
        )

    logger.info(
        "memory_exported",
        extra={
            "trace_id": (trace_id or get_trace_id() or "-"),
            "elder_id": str(elder_id),
            "status": status or "all",
            "count": len(records),
            "target": "list",
        },
    )
    return records


def _write_jsonl_blocking(path: Path, records: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """同步写 JSONL（由 `asyncio.to_thread` 调起，别在事件循环里做阻塞 IO）。

    三个细节：

    - `newline="\n"`：JSONL 的行分隔符规范就是 `\\n`，Windows 上若按平台默认写成
      `\\r\\n`，下游按 `\\n` 切分会在每行尾部留一个 `\\r`，看起来像内容被改过；
    - `ensure_ascii=False`：中文按 UTF-8 原样落盘，导出的文件要能直接给人看；
    - `sort_keys=True`：同一份数据的两次导出逐字节可比（diff 才有意义）。

    **不创建父目录**：导出路径写错时应该报 `FileNotFoundError`，而不是在磁盘上
    静默长出一棵新目录。
    """
    lines = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            lines += 1
    return lines, path.stat().st_size


async def export_memory_jsonl(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    path: str | Path,
    status: str | None = None,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "staff",
    trace_id: str | None = None,
) -> int:
    """把该长者的记忆导出成 JSONL 文件，返回写入的行数。

    与 `export_elder_memories` 共用 `_load_records`，但**不调用它**：那样会写两条
    `memory.export` 审计（一条 `target=list`、一条 `target=jsonl`）并多套一层 savepoint。
    导出只有一次「事实上发生了的导出」，就该只有一条审计。

    事务与文件的关系：审计与文件写入在**同一个 savepoint** 里 —— 文件写失败（磁盘满、
    路径写错）时 savepoint 回滚，审计行随之消失，不会留下「记了导出但没导出」的假账。

    审计 `detail` **不记路径**：路径常由操作者随手命名（`张三_记忆.jsonl`），把长者姓名
    写进审计 detail 等于在留痕里再复制一份 PII。可查性由 `count` / `bytes` / `trace_id`
    与结构化日志承担（日志只记文件后缀与父目录名，见 `extra`）。
    """
    _validate_status(status)
    validate_actor_type(actor_type)

    target = Path(path)
    async with session.begin_nested():
        records = await _load_records(session, elder_id=elder_id, status=status)
        lines, size = await asyncio.to_thread(_write_jsonl_blocking, target, records)
        await write_audit(
            session,
            action=AUDIT_ACTION_EXPORT,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type="memory_chunks",
            resource_id=elder_id,
            detail={
                "count": lines,
                "status": status or "all",
                "target": "jsonl",
                "bytes": size,
            },
            trace_id=trace_id,
        )

    logger.info(
        "memory_exported",
        extra={
            "trace_id": (trace_id or get_trace_id() or "-"),
            "elder_id": str(elder_id),
            "status": status or "all",
            "count": lines,
            "bytes": size,
            "suffix": target.suffix,
            "parent_dir": target.parent.name,
            "target": "jsonl",
        },
    )
    return lines


# ------------------------------------------------------------------ 2. 软删（人工）


async def admin_delete_memory(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    chunk_ids: Sequence[uuid.UUID] | None = None,
    actor_id: uuid.UUID | None,
    trace_id: str | None = None,
) -> int:
    """人工软删该长者的记忆（`status='deleted'`），返回受影响行数；**从不物理删**。

    - `chunk_ids=None`：该长者全部 `active` → `deleted`。这条路径**直接复用**
      `memory_service.delete_by_elder`，连它的 `delete_memory` 审计与
      「有 actor 就是 staff、没有就是 system」的口径一起复用 —— W4b 不重造删除。
    - `chunk_ids=[...]`：只动指定的 chunk（`superseded` 也可以删，只要当前不是
      `deleted`）。`chunk_ids=[]` 是**空操作**（返回 0），不会被当成「全部」——
      这个歧义一旦发生就是删光某位长者的记忆，必须在代码里写死。
    - 跨长者隔离：`chunk_ids` 那条 `UPDATE` 的 `where` 里**始终带 `elder_id`**，
      即使上游把别人的 chunk_id 传进来也改不到。

    审计动作沿用 `delete_memory`（既有词汇），`detail` 里区分 `scope="all"` 与
    `scope="chunks"`。**即使 0 行受影响也写审计**：这条记录说明「有人要求删除」，
    与 `delete_by_elder` 的既有行为一致（它同样在 0 行时留痕）。

    为什么不给 `admin_delete_memory` 加 `actor_type` 参数：复用路径上的
    `delete_by_elder` 只有 `actor_id`，无 actor 即 `system`。两套角色推导会让同一次
    后台删除在看到的不同代码路径里产生不同的 `actor_type`，审计就没法按角色统计了。
    """
    if chunk_ids is None:
        # 复用 迭代 7 的删除 + 审计；外面仍套一层 savepoint，让「UPDATE + 审计」两句话
        # 在调用方 catch 住异常继续跑时也不会留下半截状态（见模块 docstring 第 3 条）。
        async with session.begin_nested():
            deleted = await delete_by_elder(
                session,
                elder_id=elder_id,
                actor_id=actor_id,
                reason="admin_delete",
            )
        logger.info(
            "memory_admin_deleted",
            extra={
                "trace_id": (trace_id or get_trace_id() or "-"),
                "elder_id": str(elder_id),
                "scope": "all",
                "deleted": deleted,
            },
        )
        return deleted

    wanted = list(chunk_ids)
    if not wanted:
        # 显式空列表：没有要删的，别把它降级成「删全部」。
        return 0

    async with session.begin_nested():
        result = await session.execute(
            update(MemoryChunk)
            .where(
                # elder_id 与 chunk_id 两个条件都在：单靠 chunk_id 就是越权删除的入口。
                MemoryChunk.elder_id == elder_id,
                MemoryChunk.chunk_id.in_(wanted),
                MemoryChunk.status != "deleted",
            )
            .values(status="deleted")
            .execution_options(synchronize_session=False)
        )
        deleted = _affected(result)
        await write_audit(
            session,
            action=AUDIT_ACTION_DELETE,
            actor_type="staff",
            actor_id=actor_id,
            resource_type="memory_chunks",
            resource_id=elder_id,
            detail={
                "reason": "admin_delete",
                "scope": "chunks",
                "requested_chunks": len(wanted),
                "deleted_chunks": deleted,
                "soft_delete": True,
            },
            trace_id=trace_id,
        )

    logger.info(
        "memory_admin_deleted",
        extra={
            "trace_id": (trace_id or get_trace_id() or "-"),
            "elder_id": str(elder_id),
            "scope": "chunks",
            "requested": len(wanted),
            "deleted": deleted,
        },
    )
    return deleted


# ------------------------------------------------------------------ 3. 硬删（退房）


async def purge_elder_memories(session: AsyncSession, *, elder_id: uuid.UUID) -> int:
    """硬删该长者 `memory_chunks` 的**全部行**（不论 status），返回删除行数。

    语义见模块 docstring「`purge_elder_memories` 的语义」：退房是终态，`active` /
    `superseded` / `deleted` 一视同仁地抹掉，不留正文也不留向量。

    - **不写审计**：作为 `checkout_elder` 的步骤，留痕由一条 `elder.checkout` 摘要承担。
    - **不 commit**：单条 `DELETE` 本身原子，事务边界归调用方；在退房流程里它处在
      `checkout_elder` 的 savepoint 内，任一步失败整体回滚。
    - 重复调用返回 0（幂等）：重复退房不会因为「已经删过了」而报错。
    """
    result = await session.execute(delete(MemoryChunk).where(MemoryChunk.elder_id == elder_id))
    purged = _affected(result)
    logger.info(
        "memory_purged",
        extra={
            "trace_id": get_trace_id() or "-",
            "elder_id": str(elder_id),
            "purged": purged,
        },
    )
    return purged


__all__ = [
    "AUDIT_ACTION_DELETE",
    "AUDIT_ACTION_EXPORT",
    "AUDIT_ACTOR_TYPES",
    "admin_delete_memory",
    "export_elder_memories",
    "export_memory_jsonl",
    "purge_elder_memories",
    "validate_actor_type",
    "write_audit",
]
