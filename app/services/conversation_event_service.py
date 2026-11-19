"""会话事件流 `conversation_events` 的**唯一写入口**（DAG N20 / 波次 1）。

## 为什么只留一个写入口

事件流是会话回放、断线续传、口径审计的共同底座：ASR 分段、模型回复、工具调用、打断、
分段送达都要按发生顺序落一行。写入如果散落在 `audio/`、`dialogue_runtime`、`agents/` 各处，
「先取号还是先插入」这种顺序就没人守得住，迟早出现两行同 `seq` 或者回放缺段。
所以按 AGENTS §2，`app/services/` 是唯一写库层：其他模块只许调本模块的四个函数。

## seq 怎么取号（并发下不重号）

`(thread_id, seq)` 有唯一约束，但**光靠约束不够**——两笔写入都读到 `MAX(seq)=3` 时，
第二笔会撞约束报 500，而不是拿到 4。取号必须与插入在**同一事务**里完成，并且让同一会话的
取号串行化：

1. `SELECT pg_advisory_xact_lock(:key)`——事务级咨询锁，`key` 由 `thread_id` 稳定折叠而来。
   选它而不是 `SELECT ... FOR UPDATE`：空会话根本没有行可锁，`MAX(seq)` 也锁不住「未来要插入
   的行」（幻读），行锁在这里是假的。事务级锁在 `COMMIT`/`ROLLBACK` 时由 PG 自动释放，
   调用方不需要记得解锁。
2. 拿到锁后 `SELECT COALESCE(MAX(seq), 0)`，从 `+1` 开始分配**一整段**连续区间。
3. 唯一约束作为最后兜底：万一有别的路径绕过本模块写入（历史数据回填、手工修数），
   冲突会被捕获在 `SAVEPOINT` 里——回滚这一小段、重新取号重试一次，**不连坐调用方的整个事务**。

批量追加（`append_events`）因此只加一次锁、只查一次 `MAX`：一次取号区间，绝不逐行 `MAX()`。

## `event_type` 白名单为什么在服务层

0010 迁移的 docstring 写明了分工：DDL 不设 `CHECK`，枚举校验留给服务层（后续要收口成
`CHECK` 再加 0011 下发即可）。本模块据此把九个取值钉死在 `EVENT_TYPES` 里，非法值
**在碰数据库之前**就 `ValueError`——否则占位符（`"llm_reply"`、`"tts_first_packet"` 之类）
会静默写进库，等回放的时候才发现没人认识这个类型。

## `elder_id` 为什么每个函数都必填

`elder_id` 是为时序查询冗余出来的列（见 `app/models/conversation_event.py` 的设计要点），
但它同时是**闸门②**：如果查询只按 `thread_id` 过滤，任何一个拿到别人 `thread_id` 的调用方
都能读到别人的会话事件。所以四个函数的签名里 `elder_id` 都是必填，`list_events` / `latest_seq`
还会把它并进 `WHERE`——本模块不提供「只按 thread_id 跨长者查询」的口子。

## 事务边界

本模块**不 commit**：与 `memory_service`、`health_service` 同口径，事务由调用方掌握
（API 的 `get_session`、子图/调度器的会话上下文）。这样「一条语音回合里的五条事件」
要么一起落库，要么一起不落，不会回放出半截对话。
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation_event import ConversationEvent

logger = logging.getLogger(__name__)

#: `event_type` 白名单（DDL 不设 CHECK，校验在这里）。
#:
#: 前九个取值与任务卡 N20 逐字一致；`response_audio_rendered` 是 2026-11-11 追加的
#: 第十个，专给**打字链路**用。两者语义不可互相顶替：
#:   * `response_segment_delivered` = 服务端亲手把这一段 PCM 写进了播放器，**可验证**
#:     （话音链路 `MediaSession.enqueue_tts_pcm` 拒绝的段不记）。
#:   * `response_audio_rendered` = 服务端已合成音频并把 URL 交给前端，**无法验证**
#:     浏览器是否真的播了（打字链路的 `<audio>` 在客户端）。
#: 拿后者冒充前者就是伪造一条无法验证的事实——所以宁可多一个取值。
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "user_said",
        "asr_partial",
        "asr_final",
        "assistant_said",
        "tool_called",
        "tool_result",
        "response_segment_delivered",
        "response_audio_rendered",
        "interrupted",
        "turn_failed",
    }
)

#: 与 `String(48)` 对齐：最长取值 `response_segment_delivered`（25）+ `_corrected`（10）仍有余量。
MAX_EVENT_TYPE_LENGTH: Final[int] = 48
#: 与 `trace_id: String(64)` 对齐。
MAX_TRACE_ID_LENGTH: Final[int] = 64
#: 回放默认一次取多少条（列表接口的默认页大小）。
DEFAULT_LIST_LIMIT: Final[int] = 200
#: 单次回放上限：再大就应该用游标分页，而不是一次把整场会话拉进内存。
MAX_LIST_LIMIT: Final[int] = 1000
#: 唯一约束名（`UniqueConstraint(..., name=...)` 全名）：冲突兜底时用它认账。
SEQ_CONFLICT_CONSTRAINT: Final[str] = "uq_conversation_events_thread_id_seq"


@dataclass(frozen=True, slots=True)
class NewEvent:
    """批量追加时的一条事件。

    `append_event` 的五个关键字参数就是这里的字段；`payload` 里的
    `turn_id` / `response_id` / `call_id` / `task_id` 一律是弱引用（不建外键，见模型 docstring），
    所以这里不做任何 ID 类型校验，只要求是 JSON 对象。
    """

    event_type: str
    payload: dict[str, Any] | None = None
    trace_id: str | None = None
    occurred_at: datetime.datetime | None = None


# ============================================================
# 校验（全部在碰数据库之前完成）
# ============================================================


def _as_uuid(value: uuid.UUID | str, *, name: str) -> uuid.UUID:
    """接受 `UUID` 或字符串形式的 UUID，其余一律 `ValueError`。

    字符串是给 API/脚本层用的便捷口径；类型错了要在入口就炸，而不是等 asyncpg
    抛一个「expected UUID, got int」的方言错误。
    """
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            raise ValueError(f"{name} must be a UUID string") from None
    raise ValueError(f"{name} must be a UUID")


def _checked_event_type(event_type: str) -> str:
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {sorted(EVENT_TYPES)}")
    if len(event_type) > MAX_EVENT_TYPE_LENGTH:
        raise ValueError(f"event_type must be at most {MAX_EVENT_TYPE_LENGTH} characters")
    return event_type


def _checked_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object (dict) or None")
    return payload


def _checked_trace_id(trace_id: str | None) -> str | None:
    if trace_id is None:
        return None
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise ValueError("trace_id must be a non-empty string or None")
    if len(trace_id) > MAX_TRACE_ID_LENGTH:
        raise ValueError(f"trace_id must be at most {MAX_TRACE_ID_LENGTH} characters")
    return trace_id


def _checked_occurred_at(occurred_at: datetime.datetime | None) -> datetime.datetime | None:
    """事件发生时刻必须带时区（AGENTS §8：存储一律 UTC）。

    naive datetime 会被 PG 按会话时区解释，同一份代码在 CI（UTC）和开发机（+08:00）
    会落出不同时刻——这类偏差在时序回放里最难查，所以在入口就拒绝。
    """
    if occurred_at is None:
        return None
    if not isinstance(occurred_at, datetime.datetime):
        raise ValueError("occurred_at must be a datetime or None")
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware (store UTC)")
    return occurred_at


def _checked_after_seq(after_seq: int) -> int:
    if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
        raise ValueError("after_seq must be an int >= 0")
    return after_seq


def _checked_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be an int within 1..{MAX_LIST_LIMIT}")
    return limit


# ============================================================
# 取号
# ============================================================


def _lock_key(thread_id: uuid.UUID) -> int:
    """把 `thread_id` 稳定折叠成 64 位有符号整数，作为咨询锁的键。

    用 blake2b 而不是 `hash()`：内置 `hash()` 对 bytes 每进程加盐（`PYTHONHASHSEED`），
    同一会话在两个 worker 里会算出不同键，锁就形同虚设。碰撞概率 2^-64，可接受。
    """
    digest = hashlib.blake2b(thread_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def _allocate_seq_start(session: AsyncSession, thread_id: uuid.UUID) -> int:
    """在当前事务内锁住会话并返回本次可用区间的起点（现有最大 seq + 1）。"""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(thread_id)})
    stmt = select(func.coalesce(func.max(ConversationEvent.seq), 0)).where(
        ConversationEvent.thread_id == thread_id
    )
    current = int((await session.execute(stmt)).scalar_one())
    return current + 1


def _is_seq_conflict(exc: IntegrityError) -> bool:
    return SEQ_CONFLICT_CONSTRAINT in str(getattr(exc, "orig", exc))


def _build_row(
    *,
    thread_id: uuid.UUID,
    elder_id: uuid.UUID,
    seq: int,
    event_type: str,
    payload: dict[str, Any],
    trace_id: str | None,
    occurred_at: datetime.datetime | None,
) -> ConversationEvent:
    row = ConversationEvent(
        thread_id=thread_id,
        elder_id=elder_id,
        seq=seq,
        event_type=event_type,
        payload=payload,
        trace_id=trace_id,
    )
    # 不给 occurred_at 时交给 DDL 的 `now()`：落库时刻即发生时刻，差值 0 表示「实时事件」。
    if occurred_at is not None:
        row.occurred_at = occurred_at
    return row


async def _append(
    session: AsyncSession,
    thread_id: uuid.UUID,
    rows_builder: Callable[[int], list[ConversationEvent]],
) -> list[ConversationEvent]:
    """取号 → 建行 → flush，唯一约束冲突时在 SAVEPOINT 内重试一次。

    为什么用 `SAVEPOINT` 而不是直接 `rollback()`：调用方的事务里可能还有同一回合的其它写入
    （记忆、审计、状态中心），为了一次序号冲突把它们一起回滚，等于把「重试一次」变成
    「整回合重做」。回滚到保存点只丢掉这一批未落库的行。
    """
    last_error: IntegrityError | None = None
    for attempt in range(2):
        try:
            async with session.begin_nested():
                start = await _allocate_seq_start(session, thread_id)
                rows = rows_builder(start)
                session.add_all(rows)
                await session.flush()
        except IntegrityError as exc:
            if not _is_seq_conflict(exc) or attempt > 0:
                raise
            # 走到这里说明有绕过咨询锁的写入抢先占了号：重新取号再来一次。
            last_error = exc
            logger.warning(
                "conversation_event_seq_conflict_retry",
                extra={"thread_id": str(thread_id), "attempt": attempt + 1},
            )
            continue
        return rows

    # 两次都撞号：这不是并发，是时序被别的东西持续改写，如实抛给调用方（不吞）。
    assert last_error is not None
    raise last_error


def _log_appended(
    *,
    thread_id: uuid.UUID,
    elder_id: uuid.UUID,
    rows: Sequence[ConversationEvent],
    trace_id: str | None,
) -> None:
    logger.info(
        "conversation_events_appended",
        extra={
            "thread_id": str(thread_id),
            "elder_id": str(elder_id),
            "count": len(rows),
            "first_seq": rows[0].seq,
            "last_seq": rows[-1].seq,
            "event_types": [row.event_type for row in rows],
            "trace_id": trace_id,
        },
    )


# ============================================================
# 写入口
# ============================================================


async def append_event(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID | str,
    elder_id: uuid.UUID | str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
    occurred_at: datetime.datetime | None = None,
) -> ConversationEvent:
    """追加一条事件，返回已经 `flush()` 过的行（`seq` 已填好）。

    取号与插入在同一个事务、同一段咨询锁里完成；本函数**不 commit**，事务边界归调用方。
    非法 `event_type` / `payload` / `trace_id` / naive `occurred_at` 一律 `ValueError`，
    且**在碰数据库之前**就拒绝——调用方拿到的异常意味着「一行都没写」。
    """
    thread = _as_uuid(thread_id, name="thread_id")
    elder = _as_uuid(elder_id, name="elder_id")
    kind = _checked_event_type(event_type)
    body = _checked_payload(payload)
    trace = _checked_trace_id(trace_id)
    when = _checked_occurred_at(occurred_at)

    rows = await _append(
        session,
        thread,
        lambda start: [
            _build_row(
                thread_id=thread,
                elder_id=elder,
                seq=start,
                event_type=kind,
                payload=body,
                trace_id=trace,
                occurred_at=when,
            )
        ],
    )
    _log_appended(thread_id=thread, elder_id=elder, rows=rows, trace_id=trace)
    return rows[0]


async def append_events(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID | str,
    elder_id: uuid.UUID | str,
    events: Sequence[NewEvent],
) -> list[ConversationEvent]:
    """一次事务追加一批事件，`seq` 连续递增且只取一次号。

    空序列直接返回 `[]`（不来一把没人用的锁）；任何一条非法则整批拒绝、一行不写。
    逐行 `MAX(seq)+1` 在并发下必然重号，所以这里只有**一次**取号（见模块 docstring）。
    """
    thread = _as_uuid(thread_id, name="thread_id")
    elder = _as_uuid(elder_id, name="elder_id")
    if not events:
        return []

    drafts: list[tuple[str, dict[str, Any], str | None, datetime.datetime | None]] = []
    for index, event in enumerate(events):
        if not isinstance(event, NewEvent):
            raise TypeError(f"events[{index}] must be a NewEvent")
        drafts.append(
            (
                _checked_event_type(event.event_type),
                _checked_payload(event.payload),
                _checked_trace_id(event.trace_id),
                _checked_occurred_at(event.occurred_at),
            )
        )

    def _rows(start: int) -> list[ConversationEvent]:
        return [
            _build_row(
                thread_id=thread,
                elder_id=elder,
                seq=start + offset,
                event_type=kind,
                payload=body,
                trace_id=trace,
                occurred_at=when,
            )
            for offset, (kind, body, trace, when) in enumerate(drafts)
        ]

    rows = await _append(session, thread, _rows)
    _log_appended(thread_id=thread, elder_id=elder, rows=rows, trace_id=drafts[0][2])
    return rows


# ============================================================
# 读入口（一律带 elder_id，不给「按 thread_id 跨长者查询」的口子）
# ============================================================


async def list_events(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID | str,
    elder_id: uuid.UUID | str,
    after_seq: int = 0,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[ConversationEvent]:
    """按 `seq` 升序回放 `seq > after_seq` 的事件（游标分页，不含 `after_seq` 本身）。"""
    thread = _as_uuid(thread_id, name="thread_id")
    elder = _as_uuid(elder_id, name="elder_id")
    cursor = _checked_after_seq(after_seq)
    size = _checked_limit(limit)

    stmt = (
        select(ConversationEvent)
        .where(
            ConversationEvent.thread_id == thread,
            ConversationEvent.elder_id == elder,
            ConversationEvent.seq > cursor,
        )
        .order_by(ConversationEvent.seq.asc())
        .limit(size)
    )
    return list((await session.execute(stmt)).scalars().all())


async def latest_seq(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID | str,
    elder_id: uuid.UUID | str,
) -> int:
    """该会话当前最大 `seq`；一条事件都没有时返回 0（与取号起点 `+1` 对齐）。"""
    thread = _as_uuid(thread_id, name="thread_id")
    elder = _as_uuid(elder_id, name="elder_id")

    stmt = select(func.coalesce(func.max(ConversationEvent.seq), 0)).where(
        ConversationEvent.thread_id == thread,
        ConversationEvent.elder_id == elder,
    )
    return int((await session.execute(stmt)).scalar_one())


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "EVENT_TYPES",
    "MAX_EVENT_TYPE_LENGTH",
    "MAX_LIST_LIMIT",
    "MAX_TRACE_ID_LENGTH",
    "SEQ_CONFLICT_CONSTRAINT",
    "NewEvent",
    "append_event",
    "append_events",
    "latest_seq",
    "list_events",
]
