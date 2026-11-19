"""长期记忆切片 ORM（pgvector 1024 维）。

列定义对齐 `alembic/versions/0009_memory_medication.py`、技术设计文档
§3.4 `memory_chunks`、PRD §4.9（FR-MEM-01..08）。

设计要点：

- `embedding` 允许 NULL：embedding 供应商不可用时**仍写入原文**（降级不丢记忆），
  召回时无向量的行走纯关键词降级或整条跳过，**绝不臆造**（PRD §4.9「召回失败
  则本轮不加载记忆」）。
- 跨长者隔离靠**强制 `WHERE elder_id`**：本表所有读写入口都在
  `app/services/memory_service.py`，迭代 9 闸门②会静态扫描任何不带 `elder_id`
  的向量查询。
- `status` 三值表达生命周期：`active` 生效 / `superseded` 被高相似新条取代 /
  `deleted` 软删（退房清数据与容量淘汰）。技术设计文档**没有** `superseded_by`
  列，取代关系一律用 status 表达；迭代 7 计划正文提到的 `superseded_by` 属笔误，
  已在计划「偏离登记」留痕。
- `importance` 取 Numeric(3,2) ∈ [0,1]：既是记忆权重，也是 90 天衰减
  （`memory_service.decay`）与容量淘汰（`enforce_capacity`）的排序键。
"""

from __future__ import annotations

import datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EMBEDDING_DIM = 1024
"""与 `settings.embedding_dimension` 对齐的维度（2026-11-11 定为 bge-m3 dense 的 1024）。

三处必须同时改，漏一处就是运行时 `bad_dimension`：本常量、`app/models/knowledge.py` 的同名
常量、`alembic/versions/0013_*.py` 的 `vector(1024)`。两个模型文件刻意不互相 import（见
`knowledge.py` 同名常量注释），所以这里是**有意的重复**，不是抄错。
"""


class MemoryChunk(Base):
    """一条长期记忆切片；写入即落库，召回按 `elder_id` 强隔离。"""

    __tablename__ = "memory_chunks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded', 'deleted')",
            name="status",
        ),
        CheckConstraint("importance BETWEEN 0 AND 1", name="importance"),
        Index("ix_memory_chunks_elder_id_status", "elder_id", "status"),
        {"comment": "长期记忆切片（pgvector 1024 维，跨长者强隔离）"},
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="记忆切片编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者 ID（召回必须带上，AGENTS 红线 9）",
    )
    content: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="记忆正文（脱敏后文本，不存原始录音）"
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM),
        nullable=True,
        comment="向量表示；供应商不可用时为 NULL（降级不丢原文）",
    )
    category: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'private'"),
        comment="private（默认）/ family：林医生多条召回只取这两类",
    )
    importance: Mapped[float] = mapped_column(
        Numeric(3, 2),
        nullable=False,
        server_default=text("1.00"),
        comment="权重 0..1：衰减与容量淘汰的排序键",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'active'"),
        comment="active / superseded / deleted（软删，不物理删）",
    )
    source_thread_id: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="来源会话 thread_id（可空，仅追溯用）"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="写入时刻（UTC 存储）",
    )
    last_accessed: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次被召回的时刻；NULL=从未召回（衰减从 created_at 起算）",
    )
