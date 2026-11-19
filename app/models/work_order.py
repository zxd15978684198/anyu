"""风险事件 / 工单 / 工单流转 ORM（三张表）。

列定义对齐 `alembic/versions/0009_memory_medication.py` 与技术设计文档
§3.4 `risk_events` / `work_orders` / `work_order_transitions`；业务口径来自
PRD §4.5（四次无响应派护工）、§4.6（红标异常派单）、§4.7（敏感告警）。

设计要点：

- **风险事件是「发生了什么」**（安全/健康/用药/敏感），**工单是「谁去处理」**：
  一个 risk_event 可派生多个工单（如先派护工、24h 未闭环再升级），因此
  `work_orders.event_id` 可空——不是所有工单都源于风险事件。
- 工单**状态流转必须留痕**：每次改状态同事务写 `work_order_transitions`
  （from_status → to_status + actor + 时刻），不允许静默改状态。
- `order_no` 人类可读单号（`WO-YYYYMMDD-NNNN`）唯一：照护台口头/纸面沟通用，
  服务层撞唯一键重试一次。
- `false_alarm` 是误报标记（PRD §4.7 复盘需要），**不是**删除或改状态；
  误报也要保留 `false_alarm_reason`。
- 只增不改：工单 `status` 是当前态，历史全在 transitions 表。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

WORK_ORDER_STATUSES = (
    "new",
    "acknowledged",
    "in_progress",
    "resolved",
    "closed",
    "reopened",
)
"""工单六态闭集（技术设计文档 §3.4 与 PRD §4.7 一致）。"""

_STATUS_IN = "('" + "', '".join(WORK_ORDER_STATUSES) + "')"


class RiskEvent(Base):
    """安全/健康/用药/敏感四类风险事件的事实记录。"""

    __tablename__ = "risk_events"
    __table_args__ = (
        CheckConstraint(
            "type IN ('red_emergency', 'health_alert', 'med_risk', 'sensitive', 'sos', "
            "'safety_alert')",
            name="type",
        ),
        CheckConstraint("severity IN ('red', 'yellow')", name="severity"),
        CheckConstraint("status IN ('open', 'escalated', 'closed')", name="status"),
        Index("ix_risk_events_elder_id_status", "elder_id", "status"),
        {"comment": "风险事件（安全/健康/用药/敏感的事实记录）"},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="风险事件编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者 ID",
    )
    type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="red_emergency / health_alert / med_risk / sensitive / sos / safety_alert",
    )
    severity: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="red（须立即人工）/ yellow（当班跟进）"
    )
    source_type: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="来源对象类型（如 medication_events / health_records / decision_logs）",
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="来源对象 ID（不建外键：来源表跨四种，软引用）",
    )
    detail: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(),
        nullable=True,
        comment="命中证据（脱敏后）：rule_id/category/severity/matched/transcript_excerpt/sensitive_at",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'open'"),
        comment="open / escalated（已派单）/ closed",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="事件发生时刻（UTC）",
    )
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="响应人点击「已知悉」的时刻"
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="事件闭环时刻"
    )


class WorkOrder(Base):
    """派给护工/护士的工单；状态流转全部留痕在 transitions 表。"""

    __tablename__ = "work_orders"
    # 允许非 Mapped 的普通注解（deduplicated 是返回对象上的临时标记，不是表列）
    __allow_unmapped__ = True
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_work_orders_order_no"),
        CheckConstraint(
            "type IN ('missed_medication', 'sensitive_alert', 'health_alert', "
            "'device_fault', 'sos', 'collection_missed', 'drug_question', 'follow_up', "
            "'safety_alert')",
            name="type",
        ),
        CheckConstraint("severity IN ('red', 'yellow')", name="severity"),
        CheckConstraint(f"status IN {_STATUS_IN}", name="status"),
        Index(
            "ix_work_orders_status_severity_created_at",
            "status",
            "severity",
            "created_at",
        ),
        Index(
            "uq_work_orders_dedupe_key",
            "elder_id",
            "type",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
        {"comment": "工单（照护台派单；状态流转见 work_order_transitions）"},
    )

    # 幂等命中标记（仅返回对象上有，不是表列）；ClassVar 供 mypy 识别。
    deduplicated: bool

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="工单编号",
    )
    order_no: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="人类可读单号 WO-YYYYMMDD-NNNN（唯一，口头/纸面沟通用）",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者 ID",
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("risk_events.event_id", ondelete="SET NULL"),
        nullable=True,
        comment="来源风险事件；可直接派人处理（如家属要求回访）时为 NULL",
    )
    type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="missed_medication / sensitive_alert / health_alert / device_fault / "
        "sos / collection_missed / drug_question / follow_up / safety_alert",
    )
    severity: Mapped[str] = mapped_column(Text(), nullable=False, comment="red / yellow")
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'new'"),
        comment="new / acknowledged / in_progress / resolved / closed / reopened",
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_accounts.staff_id", ondelete="SET NULL"),
        nullable=True,
        comment="受理人 staff_id；未派单为 NULL",
    )
    false_alarm: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default=text("false"),
        comment="误报标记（复盘用；不等于删除）",
    )
    false_alarm_reason: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="误报原因（false_alarm=true 时必填，服务层校验）"
    )
    actual_handled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际处置时刻（用于响应时长统计）"
    )
    note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="处置备注（脱敏后文本）"
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="幂等去重键（FR-ALT-04）；同 elder_id + type 下唯一，人工补派不复用",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="建单时刻（UTC）",
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="关单时刻"
    )


class WorkOrderTransition(Base):
    """工单状态流转的一条记录；只增不改，工单当前态在 work_orders.status。"""

    __tablename__ = "work_order_transitions"
    __table_args__ = (
        CheckConstraint(f"from_status IN {_STATUS_IN}", name="from_status"),
        CheckConstraint(f"to_status IN {_STATUS_IN}", name="to_status"),
        Index("ix_work_order_transitions_order_id_at", "order_id", "at"),
        {"comment": "工单状态流转留痕（谁在何时把单子从什么状态推到什么状态）"},
    )

    transition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="流转记录编号",
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_orders.order_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属工单",
    )
    from_status: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="变更前状态（六态之一）"
    )
    to_status: Mapped[str] = mapped_column(Text(), nullable=False, comment="变更后状态（六态之一）")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_accounts.staff_id", ondelete="SET NULL"),
        nullable=True,
        comment="操作人 staff_id；系统自动流转（如升级派单）为 NULL",
    )
    at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="流转时刻（UTC）",
    )
    note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="流转说明（如「四次用药未响应，自动升级」）"
    )
