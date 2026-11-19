"""迭代 8 · 安全告警证据列与三处枚举扩展（0012）

DAG 节点 N02（迭代 8 计划 §3 W1）。两件事必须同一张迁移：新增的 `safety_alert`
事件**必然**携带证据，先建枚举后补列会让中间态写不进去。

1. `risk_events.detail JSONB`：命中证据
   `{rule_id, category, severity, matched, transcript_excerpt, sensitive_at}`。
   没有这一列，「为什么判成急症」事后无法复核，`work_orders.false_alarm`
   也就没有依据可查（迭代 8 计划 §2 第②条）。
2. `risk_events.type` / `work_orders.type` / `notifications.kind` 三处 CHECK
   各追加 `safety_alert`。**不复用既有取值**：`red_emergency` 是急症专用、
   `sos` 是长者自己按求助按钮、`health_review` 是健康红档复核——把安全命中塞进
   它们任何一个，事后按 type 统计就会分不清「这句急症是模型听的还是按钮按的」。
   三类细分（emergency / harm / request_human）只走 `detail.category`：
   type 是稳定枚举，细分是数据。

复查要点（人工逐行读，未用 autogenerate——autogenerate 会按当前库差异重排约束/
注释，反而是漂移来源）：

1. 只动本迁移涉及的 1 列 + 3 个 CHECK + 3 处列注释，**不碰其他表**，不改历史迁移
   （AGENTS §4 禁区 6）；
2. CHECK 先 drop 再 create，名字用 `naming_convention` 的渲染结果
   （`ck_<table>_<name>`）显式写全，与 ORM 逐字一致，否则 `alembic check` 会报
   `rename_constraint`；
3. 三列注释与 ORM `comment=` **逐字一致**（`alembic check` 把列注释也算作漂移，
   两边措辞差一个字就是一串 `modify_comment`）；
4. 新增枚举值是**向后兼容**的：既有行不受影响，旧代码读到新取值只是不认识、
   不会崩；回滚后若库里已有 `safety_alert` 行，downgrade 会因 CHECK 失败——
   这是有意的 fail closed，不许「先删数据再回滚」；
5. `detail` 可空：历史行没有证据，补空值没有意义（编一份假的比空更坏）。

Revision ID: 0012_safety_alert
Revises: 0011_knowledge
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_safety_alert"
down_revision = "0011_knowledge"
branch_labels = None
depends_on = None

# 追加 safety_alert 之后的取值闭集（顺序沿用原注释，追加项放末尾便于对照 diff）。
_RISK_EVENT_TYPES = (
    "'red_emergency', 'health_alert', 'med_risk', 'sensitive', 'sos', 'safety_alert'"
)
_WORK_ORDER_TYPES = (
    "'missed_medication', 'sensitive_alert', 'health_alert', 'device_fault', 'sos', "
    "'collection_missed', 'drug_question', 'follow_up', 'safety_alert'"
)
_NOTIFICATION_KINDS = (
    "'health_review', 'medication_escalation', 'collection_missed', 'family_report', "
    "'safety_alert'"
)

# 回滚时还原的**原始**闭集（与 0011 时的状态逐字一致）。
_RISK_EVENT_TYPES_OLD = "'red_emergency', 'health_alert', 'med_risk', 'sensitive', 'sos'"
_WORK_ORDER_TYPES_OLD = (
    "'missed_medication', 'sensitive_alert', 'health_alert', 'device_fault', 'sos', "
    "'collection_missed', 'drug_question', 'follow_up'"
)
_NOTIFICATION_KINDS_OLD = (
    "'health_review', 'medication_escalation', 'collection_missed', 'family_report'"
)

_DETAIL_COMMENT = (
    "命中证据（脱敏后）：rule_id/category/severity/matched/transcript_excerpt/sensitive_at"
)
_RISK_TYPE_COMMENT = (
    "red_emergency / health_alert / med_risk / sensitive / sos / safety_alert"
)
_WORK_ORDER_TYPE_COMMENT = (
    "missed_medication / sensitive_alert / health_alert / device_fault / "
    "sos / collection_missed / drug_question / follow_up / safety_alert"
)
_NOTIFICATION_KIND_COMMENT = (
    "health_review / medication_escalation / collection_missed / family_report / "
    "safety_alert"
)


def upgrade() -> None:
    """加证据列 + 三处枚举扩展（顺序：先列后约束，便于中途失败时只回滚一步）。

    约束用**裸 SQL** 而不是 `op.create_check_constraint`：本库的 `naming_convention`
    会把传入的名字再套一遍 `ck_%(table_name)s_%(constraint_name)s`，于是
    `create_check_constraint("ck_risk_events_type", ...)` 会落成
    `ck_risk_events_ck_risk_events_type`（第一次跑这张迁移就是这么炸的）。
    裸 SQL 里写**全名**，与库中实际名字逐字一致，也顺手绕开一个历史遗留：
    `notifications` 的三条 CHECK 在库里是**双前缀**
    （`ck_notifications_ck_notifications_kind`，早期迁移用全名建的），
    而 `risk_events` / `work_orders` 是单前缀。改约束时按库中真名走，
    不借这张迁移去纠历史命名——那属于单独一次「约束改名」的活。
    """
    op.add_column(
        "risk_events",
        sa.Column("detail", postgresql.JSONB(), nullable=True, comment=_DETAIL_COMMENT),
    )

    # --- risk_events.type ---------------------------------------------------
    op.execute("ALTER TABLE risk_events DROP CONSTRAINT ck_risk_events_type")
    op.execute(
        f"ALTER TABLE risk_events ADD CONSTRAINT ck_risk_events_type "
        f"CHECK (type IN ({_RISK_EVENT_TYPES}))"
    )
    op.alter_column(
        "risk_events",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=_RISK_TYPE_COMMENT,
    )

    # --- work_orders.type ---------------------------------------------------
    op.execute("ALTER TABLE work_orders DROP CONSTRAINT ck_work_orders_type")
    op.execute(
        f"ALTER TABLE work_orders ADD CONSTRAINT ck_work_orders_type "
        f"CHECK (type IN ({_WORK_ORDER_TYPES}))"
    )
    op.alter_column(
        "work_orders",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=_WORK_ORDER_TYPE_COMMENT,
    )

    # --- notifications.kind -------------------------------------------------
    op.execute(
        "ALTER TABLE notifications DROP CONSTRAINT ck_notifications_ck_notifications_kind"
    )
    op.execute(
        f"ALTER TABLE notifications ADD CONSTRAINT ck_notifications_ck_notifications_kind "
        f"CHECK (kind IN ({_NOTIFICATION_KINDS}))"
    )
    op.alter_column(
        "notifications",
        "kind",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=_NOTIFICATION_KIND_COMMENT,
    )


def downgrade() -> None:
    """逆序还原：约束回到原闭集 → 注释回到原措辞 → 最后删列。

    库中若已存在 `safety_alert` 行，还原 CHECK 会失败并**中止回滚**。
    这是有意的 fail closed：悄悄删掉告警记录比回滚失败危险得多。
    """
    op.execute(
        "ALTER TABLE notifications DROP CONSTRAINT ck_notifications_ck_notifications_kind"
    )
    op.execute(
        f"ALTER TABLE notifications ADD CONSTRAINT ck_notifications_ck_notifications_kind "
        f"CHECK (kind IN ({_NOTIFICATION_KINDS_OLD}))"
    )
    op.alter_column(
        "notifications",
        "kind",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="health_review / medication_escalation / collection_missed / family_report",
    )

    op.execute("ALTER TABLE work_orders DROP CONSTRAINT ck_work_orders_type")
    op.execute(
        f"ALTER TABLE work_orders ADD CONSTRAINT ck_work_orders_type "
        f"CHECK (type IN ({_WORK_ORDER_TYPES_OLD}))"
    )
    op.alter_column(
        "work_orders",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="missed_medication / sensitive_alert / health_alert / device_fault / "
        "sos / collection_missed / drug_question / follow_up",
    )

    op.execute("ALTER TABLE risk_events DROP CONSTRAINT ck_risk_events_type")
    op.execute(
        f"ALTER TABLE risk_events ADD CONSTRAINT ck_risk_events_type "
        f"CHECK (type IN ({_RISK_EVENT_TYPES_OLD}))"
    )
    op.alter_column(
        "risk_events",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="red_emergency / health_alert / med_risk / sensitive / sos",
    )

    op.drop_column("risk_events", "detail")
