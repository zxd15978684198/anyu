"""迭代 6 · decision_logs 三级路由决策日志表（0008）

三级路由（L0 规则层 → L1 Laya 五信号 → L2 Agent）每次决策落一行：评测数据源
（分层命中率、敏感召回率、route_ms P95 三项数字都从本表产出）。列定义按 迭代 6
计划 W4「RouteDecision 落库」清单；迁移号按落地顺序分配取 0008（计划 §8
S-D6-M1：0007 号位已被 day8_health_reports 定稿占用，先例同其文件头 S-D8-1）。

模型侧的自我约束声明写在 app/models/decision_log.py docstring；这里复查要点
（AGENTS §5，与 S-D6-M2 静校替代配套）：

1. 单表建表，主键 uuid4 default=PG `gen_random_uuid()`（应用侧不生成，同 0006/0007）；
2. CHECK 用内联常量（照 0007 风格）：`layer IN ('L0','L1','L2')`、
   `confidence IS NULL OR (confidence BETWEEN 0 AND 1)`；约束名按文件字面全名
   `ck_decision_logs_layer` / `ck_decision_logs_confidence` 写入，与 ORM 模型
   `name="layer" / name="confidence"` 经 base.py naming_convention 渲染后一致；
3. `route_ms >= 0` 不加 DB CHECK（Lead W6 指令：口径留痕见模型 docstring 第 3 点）；
4. 不建外键、不建触发器（只增不改，同 0006 audit_logs 口径）；
5. 查询索引两种写法并存：`trace_id` 单列走 op.create_index；`(elder_id, created_at
   DESC)` 照 0006 先例用 op.execute 原生 SQL（避免 alembic 对 DESC 表达式的方言差异）；
6. downgrade：DROP INDEX + 一步 drop_table 收下（无自引用、无外部依赖）；
7. S-D6-M2：本机不可跑 upgrade/活库校验，本迁移与模型的一致性由
   `tests/test_decision_log_model.py` 静态校验兜底；插入运行等真库验证由 Lead 提交
   迁移组时安排。

Revision ID: 0008_decision_logs
Revises: 0007_health_reports
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_decision_logs"
down_revision: str | None = "0007_health_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DECISION_LAYER = "layer IN ('L0', 'L1', 'L2')"
DECISION_CONFIDENCE = "confidence IS NULL OR (confidence BETWEEN 0 AND 1)"


def upgrade() -> None:
    op.create_table(
        "decision_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="日志编号（uuid4 default 由 PG gen_random_uuid() 承担，同 0006/0007）",
        ),
        sa.Column(
            "elder_id",
            sa.Text(),
            nullable=False,
            comment="长者 ID（只能来自服务端 runtime_context 注入，AGENTS 红线 9）",
        ),
        sa.Column(
            "thread_id",
            sa.Text(),
            nullable=True,
            comment="会话 ID（thread_id 串会话；generic/系统级路由可空；不建外键，见模型 docstring 第 5 点）",
        ),
        sa.Column(
            "layer",
            sa.Text(),
            nullable=False,
            comment="L0/L1/L2（三级升序，低层不得被高层覆盖）",
        ),
        sa.Column(
            "rule_id",
            sa.Text(),
            nullable=True,
            comment="命中的 L0 规则 ID；非规则层为 NULL",
        ),
        sa.Column(
            "intent",
            sa.Text(),
            nullable=True,
            comment="识别意图（L1 七类或 L0 固定意图名）；安全否决可无 intent",
        ),
        sa.Column(
            "sensitive",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="是否命中敏感一票否决（default false，缺省保守）",
        ),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=True,
            comment="L1 置信度 0..1（0.85 快决策阈值在服务层）；L0/L2 为 NULL",
        ),
        sa.Column(
            "is_out_of_domain",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="L1 域外标记（五信号之一，S-D6-1；域外直接 L2 且不查记忆）",
        ),
        sa.Column(
            "privacy",
            sa.Text(),
            nullable=True,
            comment="双人间隐私收敛结果（L1 五信号之一）",
        ),
        sa.Column(
            "route_ms",
            sa.Integer(),
            nullable=False,
            comment="本层路由耗时 ms（≥0，不加 DB CHECK，口径见模型 docstring 第 3 点）",
        ),
        sa.Column(
            "degraded",
            sa.Text(),
            nullable=True,
            comment="降级标记（如 laya_unavailable、llm_timeout）；未降级 NULL",
        ),
        sa.Column(
            "trace_id",
            sa.Text(),
            nullable=True,
            comment="单次请求链路 ID（ER C3：与 Langfuse trace 一一对应，metrics JOIN 键）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="记录时刻（UTC 存储）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_decision_logs"),
        sa.CheckConstraint(DECISION_LAYER, name="ck_decision_logs_layer"),
        sa.CheckConstraint(DECISION_CONFIDENCE, name="ck_decision_logs_confidence"),
        comment="三级路由决策日志（评测数据源，只增不改）",
    )
    op.create_index("ix_decision_logs_trace_id", "decision_logs", ["trace_id"])
    # 倒序索引用原生 SQL，避免 alembic 对 DESC 表达式的方言差异（0006 先例）
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_decision_logs_elder_created_at "
        "ON decision_logs (elder_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_decision_logs_elder_created_at")
    op.drop_index("ix_decision_logs_trace_id", table_name="decision_logs")
    op.drop_table("decision_logs")
