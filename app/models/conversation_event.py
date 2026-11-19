"""会话事件流 `conversation_events` ORM（第 27 张表，DAG N10 / 波次 0）。

口径来源：DAG 节点 N10 的任务卡（不是讲义 §4.3 的原始八列）。与讲义相比本表刻意做了两处裁剪：

1. **不建 `call_id` / `turn_id` / `response_id` / `task_id` 四个外键列**——对应的四张表
   本阶段都不建，强行留外键会把事件流绑死在还没设计出来的 schema 上。第一阶段一律
   **弱引用**：`turn_id` / `response_id` 等标识放在 `payload` JSONB 里，需要时再提升成列。
2. `thread_id` 用 **UUID**，与 `conversation_threads.thread_id`（TEXT，见
   `alembic/versions/0004_voice_threads.py`）**不做外键**——两表类型不同，且事件往往
   先于会话登记到达（外呼/离线补报），松耦合更安全。

设计要点：

- `(thread_id, seq)` 唯一：`seq` 按会话维度从 1 单调递增，服务层用 advisory lock 分配；
  唯一约束同时是「同一 seq 重复写入」的兜底（冲突由服务层捕获后返回既有行，不吐 500）。
- 本表**只追加不改写**：没有 `updated_at` / `status` 之类可变列，更正事件用新行表达
  （`<type>_corrected`），历史行永不 UPDATE。
- `event_type` 长度 48：最长取值 `response_segment_delivered`（25 字符）+ `_corrected`
  后缀（10 字符）留足余量。枚举校验放服务层（本表 DDL 不设 CHECK，见迁移 docstring）。
- `elder_id` 是**冗余列**（可从 `thread_id` join 出来），目的是一天几十万行的时序查询
  不必每次 join `conversation_threads`，也方便按长者维度清理。
- `occurred_at` 是事件发生时刻、`created_at` 是落库时刻，两者差值即写入延迟（可观测用）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConversationEvent(Base):
    """一条会话事件；只追加，`(thread_id, seq)` 唯一。"""

    __tablename__ = "conversation_events"
    __table_args__ = (
        # 同一会话内序号唯一；同时承担「重复 seq 写入」的数据库级兜底。
        # 名字写全（不走 naming_convention 的 uq 模板）：`uq` 模板不含
        # `%(constraint_name)s`，只传短名会原样落成 `thread_id_seq`，与库里的
        # `uq_conversation_events_thread_id_seq` 对不上，alembic check 会报漂移。
        UniqueConstraint(
            "thread_id",
            "seq",
            name="uq_conversation_events_thread_id_seq",
        ),
        # 会话内顺序扫描。注意：唯一约束已经建了同列同序的索引，这一条按 N10 口径
        # 显式保留（迁移里同样建两条），若要省写放大可单独删本条，不影响行为。
        Index("ix_conversation_events_thread_id_seq", "thread_id", "seq"),
        Index("ix_conversation_events_trace_id", "trace_id"),
        Index("ix_conversation_events_elder_id_occurred_at", "elder_id", "occurred_at"),
        {"comment": "会话事件流（append-only）：ASR/LLM/TTS/打断/分段送达的时序记录"},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="事件编号（uuid4 由 PG gen_random_uuid() 承担，同 0006/0007/0008/0009）",
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="会话 ID（UUID；与 conversation_threads.thread_id 不做外键，见模块 docstring）",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者 ID（冗余列，免去每次 join 会话表；退房随长者级联清理）",
    )
    seq: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        comment="会话内序号，从 1 单调递增（UNIQUE(thread_id, seq)，由服务层 advisory lock 分配）",
    )
    event_type: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        comment="事件类型：asr_partial/asr_final/llm_reply/tts_first_packet/…（枚举校验在服务层）",
    )
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="事件发生时刻（UTC 存储）",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(),
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="事件载荷；turn_id/response_id/call_id/task_id 一律弱引用放这里",
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="链路 ID（跨服务串联用，可空）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="写入时刻；与 occurred_at 的差值即落库延迟（可观测用）",
    )
