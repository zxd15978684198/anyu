"""审计日志 ORM（敏感操作留痕，只增不改）。

列定义逐字对齐 `alembic/versions/0006_audit_and_constraints.py` 与
技术设计文档 §3.5 `audit_logs` 表（TD v1.8 起提前到 迭代 4 建表，原排期 迭代 9）。

为什么提前：C4 契约的 `DELETE /api/voiceprints` 明写「写 audit_logs」，而 迭代 4
上线时该表还不存在，删除动作只有一行 logger——AC-14（操作可追溯）取证缺口。
按 TD §3.5 的列先建表，迭代 9 补其余写入点（工单状态、越权拒绝等）时不再改表结构。

`actor_type` 在 TD 原文 `staff/guardian/system` 之外**增加了 `device`**：本表的第一个
真实写入方是 pad 设备（长者自己在平板上重录声纹），若把设备折算成 `system`，
审计里就看不出「是设备端点了一次删除」还是「后台任务清的数据」。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    """敏感操作审计行：谁、对什么、做了什么、细节。只插入，不更新不删除。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('staff', 'guardian', 'system', 'device')",
            name="actor_type",
        ),
        {"comment": "审计日志（敏感操作留痕，只增不改）"},
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="审计编号",
    )
    actor_type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="staff/guardian/system/device（device 为 迭代 4 追加，见模块 docstring）",
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="操作者 ID：员工 / 家属 / 长者 / 设备，按 actor_type 解释",
    )
    action: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="动作：delete_voiceprint / clear_data / unbind_guardian / medication_plan_update…",
    )
    resource_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="操作对象类型，如 voiceprints"
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="操作对象 ID"
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(), nullable=True, comment="操作细节（脱敏后），如删除样本数、来源端点"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
