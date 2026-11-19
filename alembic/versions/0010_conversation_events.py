"""迭代 10 · 会话事件流 `conversation_events`（0010）

DAG N10（波次 0）：新建第 27 张表。列定义逐字对齐任务卡 N10；

1. **只新增一张表**，不改任何既有表结构，也不动历史迁移（AGENTS §4 禁区 6）；
2. 主键 uuid4 + PG `gen_random_uuid()`，与其他九张业务表同口径；
3. `UNIQUE(thread_id, seq)` 落成 `uq_conversation_events_thread_id_seq`（与 ORM 全名一致，
   避免 `alembic check` 报「remove uq_ / add thread_id_seq」）；`Index(..., "thread_id", "seq")`
   落成 `ix_conversation_events_thread_id_seq`——两条按 N10 口径**都建**（前者唯一、
   后者普通），功能上后者被前者覆盖，保留是为了与任务卡列出的索引清单逐条对齐；
4. **不建** `call_id` / `turn_id` / `response_id` / `task_id`：对应四张表不建，
   弱引用放 `payload`（见 `app/models/conversation_event.py` docstring）；
5. **不设 `event_type` CHECK**：N10 的口径只列了唯一约束与三个索引，枚举校验
   留给服务层（若后续要收口成 DDL 约束，加一条 0011 下发即可，不必改本迁移）；
6. `thread_id` 是 UUID 且**不建外键**：`conversation_threads.thread_id` 是 TEXT，
   两表类型不同；且事件常先于会话登记到达（外呼/离线补报）；
7. 列注释与 ORM `comment=` **逐字一致**：`alembic check` 会把注释也算进漂移，
   两边措辞不同就会报一串 `modify_comment`（本文件首版就踩了，已对齐）；
8. downgrade 逆序 drop 索引与表，无外部依赖、无自引用，可回滚。

Revision ID: 0010_conversation_events
Revises: 0009_memory_medication
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010_conversation_events"
down_revision: str | None = "0009_memory_medication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_COMMENT = "会话事件流（append-only）：ASR/LLM/TTS/打断/分段送达的时序记录"


def upgrade() -> None:
    op.create_table(
        "conversation_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="事件编号（uuid4 由 PG gen_random_uuid() 承担，同 0006/0007/0008/0009）",
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="会话 ID（UUID；与 conversation_threads.thread_id 不做外键，见模块 docstring）",
        ),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="长者 ID（冗余列，免去每次 join 会话表；退房随长者级联清理）",
        ),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="会话内序号，从 1 单调递增（UNIQUE(thread_id, seq)，由服务层 advisory lock 分配）",
        ),
        sa.Column(
            "event_type",
            sa.String(length=48),
            nullable=False,
            comment="事件类型：asr_partial/asr_final/llm_reply/tts_first_packet/…（枚举校验在服务层）",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="事件发生时刻（UTC 存储）",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="事件载荷；turn_id/response_id/call_id/task_id 一律弱引用放这里",
        ),
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=True,
            comment="链路 ID（跨服务串联用，可空）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="写入时刻；与 occurred_at 的差值即落库延迟（可观测用）",
        ),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name=op.f("fk_conversation_events_elder_id_elders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_conversation_events")),
        sa.UniqueConstraint(
            "thread_id",
            "seq",
            name=op.f("uq_conversation_events_thread_id_seq"),
        ),
        comment=TABLE_COMMENT,
    )
    op.create_index(
        "ix_conversation_events_thread_id_seq",
        "conversation_events",
        ["thread_id", "seq"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_events_trace_id",
        "conversation_events",
        ["trace_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_events_elder_id_occurred_at",
        "conversation_events",
        ["elder_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_events_elder_id_occurred_at", table_name="conversation_events")
    op.drop_index("ix_conversation_events_trace_id", table_name="conversation_events")
    op.drop_index("ix_conversation_events_thread_id_seq", table_name="conversation_events")
    op.drop_table("conversation_events")
