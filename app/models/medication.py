"""用药计划 / 用药事件 / 触达尝试 ORM（三张表）。

列定义对齐 `alembic/versions/0009_memory_medication.py` 与技术设计文档
§3.4 `medication_plans` / `medication_events` / `medication_attempts`；业务口径
来自 PRD §4.5（T0/+5/+10/+15 四次触达、每次等 60s、四次无响应升级人工）。

设计要点：

- **幂等键 1**：`uq_medication_events_plan_id_scheduled_at` —— 同一计划同一到点
  时刻只有一个事件（PRD §4.5 幂等口径）。服务层 `ensure_event` 先查后插，撞
  唯一键即返回已有行；同键但 `payload_hash` 不同则 409（同键异内容）。
- **幂等键 2**：`uq_medication_attempts_event_id_attempt_no` —— 同一次触达只记
  一行，重试不产生重复触达记录。
- `payload_hash` = 药名/剂量/时刻的快照哈希：用于「同键异内容」判定，避免把
  药师改剂量后的新计划误判成重复请求。
- `terminal_state` 只允许技术设计文档的**四值**（`taken_reported` / `refused` /
  `missed_unconfirmed` / `notification_failed`）；迭代 7 计划 §W2 列的「六终态」
  是流程概念（含 cancelled/superseded），落地按 TD 收敛为四值 + `status` 表达，
  映射表在 `medication_service.TERMINAL_MAP`（偏离登记 S-D7-C1）。
- `retry_count BETWEEN 0 AND 3`：第 4 次触达后不再有「下一次」，与
  `attempt_no BETWEEN 1 AND 4` 一致。
- 无物理删除：计划 `status='stopped'`，事件留终态，触达记录只增。
- **续方是新建一条计划**（FR-MED-09）：`renewed_from_plan_id` 指回旧计划，旧计划
  保持 `active` 自然到期，不加 `status` 新值——到期是时间事实，不是状态。
  `review_notified_at` 只在「到期前 7 天复核提醒」发出时回写，是**幂等闸门**
  （NULL 才提醒），不随计划版本变化清理。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_DATE = datetime.date


class MedicationPlan(Base):
    """医生下发的用药计划；护工/管理员只读，改剂量即新 version。"""

    __tablename__ = "medication_plans"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'stopped')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_medication_plans_elder_id_status", "elder_id", "status"),
        Index("ix_medication_plans_renewed_from_plan_id", "renewed_from_plan_id"),
        {"comment": "用药计划（仅医生可写，PRD §4.5）"},
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="计划编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者 ID",
    )
    drug_name: Mapped[str] = mapped_column(Text(), nullable=False, comment="药品名")
    dose: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="单次剂量（如「1 片」「5mg」，自由文本）"
    )
    schedule_cron: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="服药时刻；本项目只支持 'HH:MM' 简单形态（如 '08:00'），其余跳过并告警",
    )
    start_date: Mapped[_DATE] = mapped_column(Date(), nullable=False, comment="开始日期（含）")
    end_date: Mapped[_DATE | None] = mapped_column(
        Date(), nullable=True, comment="结束日期（含）；NULL=长期医嘱"
    )
    version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("1"),
        comment="版本号，1 起；改剂量走新版本而不是改旧行",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_accounts.staff_id", ondelete="RESTRICT"),
        nullable=False,
        comment="开单医生 staff_id（PRD §4.5：仅医生可写）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'active'"),
        comment="active / stopped（停医嘱不改删行）",
    )
    renewed_from_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medication_plans.plan_id", ondelete="SET NULL"),
        nullable=True,
        comment="续方来源计划 ID；NULL=非续方新建（旧计划保持 active 自然到期，FR-MED-09）",
    )
    review_notified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="到期复核已通知时刻（UTC）；NULL=未通知过，每日 08:00 复核扫描回写（FR-MED-09）",
    )


class MedicationEvent(Base):
    """一个到点时刻的用药事件；确认流程的状态机载体。"""

    __tablename__ = "medication_events"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "scheduled_at",
            name="uq_medication_events_plan_id_scheduled_at",
        ),
        CheckConstraint(
            "status IN ('calling', 'waiting_confirm', 'confirmed', 'escalated')",
            name="status",
        ),
        CheckConstraint(
            "terminal_state IS NULL OR terminal_state IN "
            "('taken_reported', 'refused', 'missed_unconfirmed', 'notification_failed', "
            "'late_taken_reported')",
            name="terminal_state",
        ),
        CheckConstraint("triggered_by IN ('scheduler', 'dialog')", name="triggered_by"),
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN "
            "('no_answer', 'denied', 'tts_failed', 'offline', 'rtc_failed')",
            name="failure_reason",
        ),
        CheckConstraint(
            "confirm_channel IS NULL OR confirm_channel IN ('voice', 'button')",
            name="confirm_channel",
        ),
        CheckConstraint("retry_count BETWEEN 0 AND 3", name="retry_count"),
        Index("ix_medication_events_elder_id_scheduled_at", "elder_id", "scheduled_at"),
        {"comment": "用药事件（幂等键 plan_id+scheduled_at）"},
    )

    reminder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="提醒（事件）编号；对外接口用它定位一次用药",
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medication_plans.plan_id", ondelete="CASCADE"),
        nullable=False,
        comment="来源计划",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者 ID（冗余自计划，便于按长者直查）",
    )
    scheduled_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="计划到点时刻（UTC 存储）；与 plan_id 组成幂等键",
    )
    drug_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB(),
        nullable=False,
        comment="下发时刻的药名/剂量快照：计划后续改版不影响历史事件",
    )
    payload_hash: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="drug_snapshot 的 sha256：同键异内容判定（409）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="事件生成时刻（UTC）",
    )
    triggered_by: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="scheduler（定时扫描）/ dialog（语音里主动说「我要吃药」）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'calling'"),
        comment="calling / waiting_confirm / confirmed / escalated",
    )
    terminal_state: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment=(
            "五值终态；未终结为 NULL（映射见 service.TERMINAL_MAP）。"
            "late_taken_reported=原终态已是未服/未确认，事后补确认只能追认不能覆盖（MED-03）"
        ),
    )
    late_taken: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default=text("false"),
        comment="迟服标记（+5/+10/+15 触达后才确认即为 true）",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="no_answer / denied / tts_failed / offline / rtc_failed",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("0"),
        comment="已触达次数 0..3（第 4 次触达对应 3）",
    )
    confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认时刻（UTC）"
    )
    confirm_channel: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="voice / button（护工代确认走 button）"
    )
    graph_thread_id: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="LangGraph thread_id：断点恢复用；不建外键（跨 schema）",
    )


class MedicationAttempt(Base):
    """一次触达尝试（T0/+5/+10/+15 各一行），只增不改。"""

    __tablename__ = "medication_attempts"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "attempt_no", name="uq_medication_attempts_event_id_attempt_no"
        ),
        CheckConstraint("attempt_no BETWEEN 1 AND 4", name="attempt_no"),
        CheckConstraint(
            "receipt IS NULL OR receipt IN ('card_rendered', 'tts_finished', 'none')",
            name="receipt",
        ),
        CheckConstraint(
            "tts_result IS NULL OR tts_result IN ('ok', 'tts_failed')", name="tts_result"
        ),
        CheckConstraint(
            "response_channel IS NULL OR response_channel IN ('voice', 'button', 'none')",
            name="response_channel",
        ),
        {"comment": "用药触达尝试（幂等键 event_id+attempt_no）"},
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="尝试编号",
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medication_events.reminder_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用药事件（medication_events.reminder_id）",
    )
    attempt_no: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="第几次触达 1..4（T0/+5/+10/+15）"
    )
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="触达下发时刻（UTC）"
    )
    receipt: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="回执：card_rendered / tts_finished / none"
    )
    tts_result: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="语音播报结果：ok / tts_failed"
    )
    response: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="长者回应原话的脱敏短文本（可空）"
    )
    response_channel: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="回应渠道：voice / button / none"
    )
