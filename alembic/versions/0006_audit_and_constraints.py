"""迭代 4 · 审计表与三处 schema 缺陷（0006）

四项改动，每项都对应一条**已复现的真实缺陷**，不是预防性设计：

1. **建 `audit_logs`**：C4 的 `DELETE /api/voiceprints` 明写「写 audit_logs」，
   而该表原排期 迭代 9。删除动作目前只有一行 logger → AC-14 取证缺口。
   列逐字取技术设计文档 §3.5；`actor_type` 增加 `device`（见 app/models/audit.py 说明）。
2. **`beds.elder_id` 部分唯一索引**：TD §3.2 明写「1 床 1 人，UNIQUE 约束保证，空床 NULL」，
   实际没有约束 → 同一张床可被两条床行指向同一长者（也可被同一长者占两张床）。
   用 `WHERE elder_id IS NOT NULL` 的部分索引，空床可以有多张。
3. **`conversation_threads.elder_id` 外键改 CASCADE**：0004 建的 `ON DELETE SET NULL`
   与同表的跨列 CHECK（非 generic 必须 elder_id 非空）互斥 —— 一旦硬删长者，
   SET NULL 把行改成 NULL 却仍标 identity_source='voice'，CHECK 立刻失败，
   结果是**长者根本删不掉**。会话属于该长者的行为记录，随人级联删除才自洽
   （PRD FR-ADM-09 退房清数据同样是整人清空）。
4. **两个查询索引**：`conversation_threads(elder_id, started_at DESC)`（TD §3.6 明列，
   会话倒序翻页）、`voiceprints(elder_id, status)`（每次声纹比对与状态查询都按这两列过滤）。

人工审查要点（AGENTS §5）：
- 不改 0004/0005 任何一行；差值全部落在本文件
- `audit_logs` 只增不改：本迁移不建 UPDATE/DELETE 触发器，靠服务层不写 update
- 部分唯一索引含 `WHERE`，`downgrade` 必须用同名 drop_index
- 外键重建沿用 0004 的原名 `fk_conversation_threads_elder_id_elders`，downgrade 可逆

Revision ID: 0006_audit_and_constraints
Revises: 0005_voiceprint_embedding
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_audit_and_constraints"
down_revision: str | None = "0005_voiceprint_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) 审计日志表
    op.create_table(
        "audit_logs",
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="审计编号",
        ),
        sa.Column("actor_type", sa.Text(), nullable=False, comment="staff/guardian/system/device（device 为 迭代 4 追加，见模块 docstring）"),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True), nullable=False, comment="操作者 ID：员工 / 家属 / 长者 / 设备，按 actor_type 解释"
        ),
        sa.Column("action", sa.Text(), nullable=False, comment="动作：delete_voiceprint / clear_data / unbind_guardian / medication_plan_update…"),
        sa.Column("resource_type", sa.Text(), nullable=False, comment="操作对象类型，如 voiceprints"),
        sa.Column(
            "resource_id", postgresql.UUID(as_uuid=True), nullable=False, comment="操作对象 ID"
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="操作细节（脱敏后），如删除样本数、来源端点",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_id", name="pk_audit_logs"),
        sa.CheckConstraint(
            "actor_type IN ('staff', 'guardian', 'system', 'device')", name="actor_type"
        ),
        comment="审计日志（敏感操作留痕，只增不改）",
    )

    # 2) 一床一人：空床留 NULL，故用部分唯一索引
    op.create_index(
        "uq_beds_elder_id",
        "beds",
        ["elder_id"],
        unique=True,
        postgresql_where=sa.text("elder_id IS NOT NULL"),
    )

    # 3) 会话外键 SET NULL → CASCADE（与跨列 CHECK 互斥，见模块 docstring 第 3 点）
    op.drop_constraint(
        "fk_conversation_threads_elder_id_elders", "conversation_threads", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_conversation_threads_elder_id_elders",
        "conversation_threads",
        "elders",
        ["elder_id"],
        ["elder_id"],
        ondelete="CASCADE",
    )

    # 4) 两个查询索引；倒序索引用原生 SQL，避免 alembic 对 DESC 表达式的方言差异
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_threads_elder_started_at "
        "ON conversation_threads (elder_id, started_at DESC)"
    )
    op.create_index(
        "ix_voiceprints_elder_id_status", "voiceprints", ["elder_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_voiceprints_elder_id_status", table_name="voiceprints")
    op.execute("DROP INDEX IF EXISTS ix_conversation_threads_elder_started_at")

    op.drop_constraint(
        "fk_conversation_threads_elder_id_elders", "conversation_threads", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_conversation_threads_elder_id_elders",
        "conversation_threads",
        "elders",
        ["elder_id"],
        ["elder_id"],
        ondelete="SET NULL",
    )

    op.drop_index("uq_beds_elder_id", table_name="beds")
    op.drop_table("audit_logs")

