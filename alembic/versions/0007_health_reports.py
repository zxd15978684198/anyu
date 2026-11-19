"""迭代 8 · 健康采集、家属日报与通知三张表（0007）

对应技术设计文档 §3.6 的 迭代 8 建表批次（health_records / daily_reports /
notifications），按本仓「迁移号按落地顺序分配」的先例（0006 audit_logs 提前同例，
见 迭代 8 计划裁决 S-D8-1）落在本仓库第 7 号迁移。

三张表的自我约束都写在模型 docstring 里（app/models/{health,report,notification}.py），
这里复查要点（AGENTS §5）：

1. `health_records` 五列全可空（缺失判黄的口径要求允许整行无值），`grade` 由服务层
   写入且缺失判黄 → 不存在「全空但 grade=green」的合法行；
2. 补录版本链 `superseded_by` 自引用，用了 `use_alter=True`（建表顺序先主键后自引用），
   `downgrade` 直接 drop_table 三个一起收；
3. `notifications.status` 状态机由服务层强制（非法跳转 409），CHECK 兜底六枚举；
4. 枚举一律 CHECK 不用 PG ENUM（ER §5 口径）；时间列 timestamptz、`now()` 缺省。

Revision ID: 0007_health_reports
Revises: 0006_audit_and_constraints
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_health_reports"
down_revision: str | None = "0006_audit_and_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HEALTH_SOURCE = "source IN ('voice_scheduled', 'voice_active', 'staff_scheduled', 'staff_active')"
HEALTH_GRADE = "grade IN ('green', 'yellow', 'red', 'missing')"


def upgrade() -> None:
    # 1) 健康采集记录
    op.create_table(
        "health_records",
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="记录编号",
        ),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者 ID"),
        sa.Column(
            "record_date", sa.Date(), nullable=False, comment="采集归属日期（北京时间对应的自然日）"
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="voice_scheduled / voice_active / staff_scheduled / staff_active（PRD §4.6 正文统一口径）",
        ),
        sa.Column("systolic_mmhg", sa.Integer(), nullable=True, comment="收缩压 mmHg（问题 1）"),
        sa.Column("diastolic_mmhg", sa.Integer(), nullable=True, comment="舒张压 mmHg（问题 2）"),
        sa.Column("heart_rate_bpm", sa.Integer(), nullable=True, comment="静息心率 bpm（问题 3）"),
        sa.Column(
            "sleep_hours", sa.Numeric(4, 1), nullable=True, comment="昨夜睡眠小时数（问题 4）"
        ),
        sa.Column("mood_score", sa.Integer(), nullable=True, comment="情绪自评 1–5（问题 5）"),
        sa.Column(
            "grade",
            sa.Text(),
            nullable=False,
            comment="分级结果：green / yellow / red / missing（缺失判黄）",
        ),
        sa.Column(
            "abnormal_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="判黄/红的具体依据，如 ['systolic>=180']；由服务层 grade_vitals 生成",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="同日版本号，1 起",
        ),
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="被哪个新版本取代；NULL=当前生效版本",
        ),
        sa.Column(
            "recorded_by_staff_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="代填操作者；语音渠道为 NULL",
        ),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="采集提交时刻（UTC 存储）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id", name="pk_health_records"),
        sa.ForeignKeyConstraint(
            ["elder_id"], ["elders.elder_id"], ondelete="CASCADE", name="fk_health_records_elder_id"
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_staff_id"],
            ["staff_accounts.staff_id"],
            ondelete="SET NULL",
            name="fk_health_records_recorded_by",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["health_records.record_id"],
            ondelete="SET NULL",
            name="fk_health_records_superseded_by",
            use_alter=True,
        ),
        sa.UniqueConstraint("elder_id", "record_date", "revision", name="uq_health_version"),
        sa.CheckConstraint(HEALTH_SOURCE, name="ck_health_records_source"),
        sa.CheckConstraint(HEALTH_GRADE, name="ck_health_records_grade"),
        sa.CheckConstraint(
            "mood_score IS NULL OR (mood_score BETWEEN 1 AND 5)",
            name="ck_health_records_mood_range",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_health_records_revision_positive"),
        comment="健康采集记录（五字段 + 分级 + 版本链）",
    )
    op.create_index(
        "ix_health_records_elder_date",
        "health_records",
        ["elder_id", "record_date"],
    )
    # 自引用外键（use_alter）：SQLAlchemy 建表时跳过，需在表建好后单独补
    op.create_foreign_key(
        "fk_health_records_superseded_by",
        "health_records",
        "health_records",
        ["superseded_by"],
        ["record_id"],
        ondelete="SET NULL",
    )

    # 2) 家属日报
    op.create_table(
        "daily_reports",
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="日报编号",
        ),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者 ID"),
        sa.Column("report_date", sa.Date(), nullable=False, comment="日报归属日期"),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="health_block / medication_block / sources 结构化内容",
        ),
        sa.Column(
            "audio_url", sa.Text(), nullable=True, comment="TTS 语音摘要（FR-RPT-04，迭代 8 预留）"
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="生成时刻（UTC 存储）",
        ),
        sa.PrimaryKeyConstraint("report_id", name="pk_daily_reports"),
        sa.ForeignKeyConstraint(
            ["elder_id"], ["elders.elder_id"], ondelete="CASCADE", name="fk_daily_reports_elder_id"
        ),
        sa.UniqueConstraint("elder_id", "report_date", name="uq_daily_report_day"),
        comment="家属日报（健康统计 + 用药占位块，JSONB payload）",
    )

    # 3) 通知
    op.create_table(
        "notifications",
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="通知编号",
        ),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="关联长者；系统级通知为 NULL",
        ),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            comment="health_review / medication_escalation / collection_missed / family_report",
        ),
        sa.Column("severity", sa.Text(), nullable=False, comment="red / yellow / green"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'generated'"),
            comment="generated / pending / delivered / acknowledged / failed / simulated",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="通知内容（如缺采字段、分级依据、日报摘要）；展示前一律脱敏",
        ),
        sa.Column(
            "assignee_staff_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="派给谁的复核任务；不定向通知为 NULL",
        ),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="进入 delivered/simulated 的时刻",
        ),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="护工/医生确认时刻",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("notification_id", name="pk_notifications"),
        sa.ForeignKeyConstraint(
            ["elder_id"], ["elders.elder_id"], ondelete="CASCADE", name="fk_notifications_elder_id"
        ),
        sa.ForeignKeyConstraint(
            ["assignee_staff_id"],
            ["staff_accounts.staff_id"],
            ondelete="SET NULL",
            name="fk_notifications_assignee",
        ),
        sa.CheckConstraint(
            "kind IN ('health_review', 'medication_escalation', 'collection_missed', 'family_report')",
            name="ck_notifications_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('red', 'yellow', 'green')", name="ck_notifications_severity"
        ),
        sa.CheckConstraint(
            "status IN ('generated', 'pending', 'delivered', 'acknowledged', 'failed', 'simulated')",
            name="ck_notifications_status",
        ),
        comment="站内通知（四类 kind × 六状态机，迭代 8 事件投递通道）",
    )
    op.create_index("ix_notifications_status", "notifications", ["status"])
    op.create_index("ix_notifications_elder_created", "notifications", ["elder_id", "created_at"])


def downgrade() -> None:
    # 先删自引用外键（use_alter 建的），再按依赖序 drop
    op.drop_constraint("fk_health_records_superseded_by", "health_records", type_="foreignkey")
    op.drop_index("ix_notifications_elder_created", table_name="notifications")
    op.drop_index("ix_notifications_status", table_name="notifications")
    op.drop_index("ix_health_records_elder_date", table_name="health_records")
    op.drop_table("notifications")
    op.drop_table("daily_reports")
    op.drop_table("health_records")
