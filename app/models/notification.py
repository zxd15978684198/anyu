"""通知 ORM（五类通知 + 六状态状态机，告警/日报/漏采/到期复核共用）。

对齐 `alembic/versions/0007_day8_health_daily_notifications.py` 与
PRD §4.7（通知状态机 generated→pending→delivered→acknowledged/failed/simulated）。

设计要点：
- 本表是 迭代 8 阶段**唯一的事件投递通道**：健康红/黄复核（health_review）、
  用药升级（medication_escalation，迭代 7 起）、漏采派单（collection_missed）、
  日报推送（family_report）、用药到期复核（medication_review，0020 批次的
  FR-MED-09 补入）。`work_orders` 表属 迭代 7 批次（迭代 8 计划裁决
  S-D8-2），本日红档事件的「复核任务」以 health_review 通知承载。
- `status` 迁移由服务层 `notification_service` 状态机强制，非法跳转 409；
  表级 CHECK 兜底六枚举。
- `kind`/`severity` 枚举小写下划线，与 ER §5「枚举一律 CHECK 不用 PG ENUM」一致。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Notification(Base):
    """一条待送达/已送达的站内通知；状态机见 notification_service。"""

    __tablename__ = "notifications"
    # 允许非 Mapped 的普通注解（deduplicated 是返回对象上的临时标记，不是表列）
    __allow_unmapped__ = True
    __table_args__ = (
        CheckConstraint(
            "kind IN ('health_review', 'medication_escalation', 'medication_review', "
            "'collection_missed', 'family_report', 'safety_alert', 'attention_signal', "
            "'work_order')",
            name="kind",
        ),
        CheckConstraint("severity IN ('red', 'yellow', 'green')", name="severity"),
        CheckConstraint(
            "status IN ('generated', 'pending', 'delivered', 'acknowledged', 'failed', 'simulated')",
            name="status",
        ),
        Index("ix_notifications_status", "status"),
        Index("ix_notifications_elder_created", "elder_id", "created_at"),
        Index(
            "uq_notifications_dedupe_key",
            "elder_id",
            "kind",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
        {"comment": "站内通知（七类 kind × 六状态机，迭代 8 事件投递通道）"},
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="通知编号",
    )
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE", name="fk_notifications_elder_id"),
        nullable=True,
        comment="关联长者；系统级通知为 NULL",
    )
    kind: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="health_review / medication_escalation / medication_review / collection_missed / "
        "family_report / safety_alert / attention_signal",
    )
    severity: Mapped[str] = mapped_column(Text(), nullable=False, comment="red / yellow / green")
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'generated'"),
        comment="generated / pending / delivered / acknowledged / failed / simulated",
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(), nullable=True, comment="通知内容（如缺采字段、分级依据、日报摘要）；展示前一律脱敏"
    )
    assignee_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "staff_accounts.staff_id", ondelete="SET NULL", name="fk_notifications_assignee"
        ),
        nullable=True,
        comment="派给谁的复核任务；不定向通知为 NULL",
    )
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="进入 delivered/simulated 的时刻"
    )
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="护工/医生确认时刻"
    )
    # 返回对象上的临时标记，不是表列：命中幂等键时置 True。服务层在实例上赋值覆盖，
    # 用 ClassVar 声明供 mypy 识别，同时明确告诉 SQLAlchemy 不要映射成列。
    deduplicated: bool

    dedupe_key: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="幂等去重键（FR-ALT-04）；同 elder_id + kind 下唯一，人工重发不复用",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
