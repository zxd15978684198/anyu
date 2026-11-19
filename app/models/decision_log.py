"""迭代 6 · decision_logs 三级路由决策日志 ORM（评测数据源，只作路由留痕）。

列定义与 `alembic/versions/0008_decision_logs.py` 逐字对齐（S-D6-M2：本机
docker/alembic 不可用，不跑 upgrade，以「模型↔迁移一致性对照测试 + 迁移静态校验」
替代 alembic check，见 tests/test_decision_log_model.py）。

列名/类型按 迭代 6 计划 W4「RouteDecision 落库」清单（Lead 2026-11-07 W6 口径）：
三级路由（L0 规则层 → L1 Laya 五信号 → L2 Agent）每次决策写一行，一行就是一次
「哪一层拍的板、带什么信号、花了多少毫秒」。

与 TD §3.6 原表格的字段映射（差异登记，均由计划 §8 S-D6-3「留痕表」口径承载）：
- `log_id` → `id`；`route_layer`(rule/laya/agent) → `layer`(L0/L1/L2，AGENTS §6
  三级升序口径)；`latency_ms` → `route_ms`；`escalated_to_s2` 由 `layer='L2'` 表达；
- `input_text` / `model_version` / `asr_confidence` 不在本轮 W4 清单——评测归因如果
  需要它们，回计划 §8 登记裁决后再补列，不静默扩表。

约束复查要点（AGENTS §5）：
1. 主键 `id`：UUID uuid4 default——照 audit.py 写法 `UUID(as_uuid=True)` +
   `server_default=gen_random_uuid()`，应用侧不生成 uuid；文本列一律 `Text`
   （本仓惯例，指令里的 String 语义列同样落 Text，先例见 audit.py / conversation.py）。
2. `layer` NOT NULL + CHECK `layer IN ('L0','L1','L2')`（0007 内联常量风格）；
   `confidence` 可空 + CHECK `0..1`——L0/L2 未过 Laya，无置信可填；0.85 快决策阈值
   是 PRD §2.3 的服务层口径，不进 DB；越界写入由 CHECK 在库层兜底。
3. `route_ms`（本层路由耗时，整数毫秒，≥0）：不加 DB CHECK（Lead W6 指令，口径留痕
   在此）——负值视为写入方 bug，由服务层断言与评测 P95 统计兜出，不设豁免。
4. `sensitive` / `is_out_of_domain` NOT NULL default false（ORM `default=False` 与
   DB `server_default=false` 双重缺省），缺省保守、不漏敏感标记。
5. 不建任何外键（计划 W4 清单未列 FK；S-D6-3 只作路由留痕）——`thread_id` 串会话、
   `trace_id` 串单次请求（ER C3：thread_id 与 trace_id 不同层）；`elder_id` 只能来自
   服务端 runtime_context 注入（AGENTS 红线 9），客户端与模型传入一律丢弃。
6. 索引：`(elder_id, created_at DESC)`（按长者倒序翻决策日志，照 0006
   conversation_threads 先例：模型侧 `text("created_at DESC")`、迁移侧 op.execute
   原生 SQL，避免 alembic 对 DESC 表达式的方言漂移）；`trace_id` 单列索引
   （与 Langfuse trace 对账查询用）。
7. 只增不改（同 0006 audit_logs 口径）：不建 UPDATE/DELETE 触发器，靠服务层不写
   update、不删行。
8. 模型不进 app/models/__init__.py 也能被测试直连 import；包注册（__init__ 清单）留
   Lead 合并迁移组时一并做，本文件不越权。

Rev：模型名 `DecisionLog` / 表名 `decision_logs` / PK 命名约定 `pk_decision_logs` /
CHECK 渲染名 `ck_decision_logs_layer`、`ck_decision_logs_confidence`（base.py
naming_convention 拼接，迁移侧同名字面写入）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 内联 CHECK 常量（照 0007 文件顶常量风格）；迁移文件用同字面量的全名版本写入
_LAYER_CHECK = "layer IN ('L0', 'L1', 'L2')"
_CONFIDENCE_CHECK = "confidence IS NULL OR (confidence BETWEEN 0 AND 1)"

_DESC_INDEX = "ix_decision_logs_elder_created_at"


class DecisionLog(Base):
    """一条三级路由决策：哪一层拍的板、带什么信号、花了多少毫秒。只插入，不更新。"""

    __tablename__ = "decision_logs"
    __table_args__ = (
        CheckConstraint(_LAYER_CHECK, name="layer"),
        CheckConstraint(_CONFIDENCE_CHECK, name="confidence"),
        Index(_DESC_INDEX, "elder_id", text("created_at DESC")),
        {"comment": "三级路由决策日志（评测数据源，只增不改）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="日志编号（uuid4 default 由 PG gen_random_uuid() 承担，同 0006/0007）",
    )
    elder_id: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="长者 ID（只能来自服务端 runtime_context 注入，AGENTS 红线 9）",
    )
    thread_id: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="会话 ID（thread_id 串会话；generic/系统级路由可空；不建外键，见模型 docstring 第 5 点）",
    )
    layer: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="L0/L1/L2（三级升序，低层不得被高层覆盖）"
    )
    rule_id: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="命中的 L0 规则 ID；非规则层为 NULL"
    )
    intent: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="识别意图（L1 七类或 L0 固定意图名）；安全否决可无 intent"
    )
    sensitive: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="是否命中敏感一票否决（default false，缺省保守）",
    )
    confidence: Mapped[float | None] = mapped_column(
        Float(),
        nullable=True,
        comment="L1 置信度 0..1（0.85 快决策阈值在服务层）；L0/L2 为 NULL",
    )
    is_out_of_domain: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="L1 域外标记（五信号之一，S-D6-1；域外直接 L2 且不查记忆）",
    )
    privacy: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="双人间隐私收敛结果（L1 五信号之一）"
    )
    route_ms: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        comment="本层路由耗时 ms（≥0，不加 DB CHECK，口径见模型 docstring 第 3 点）",
    )
    degraded: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="降级标记（如 laya_unavailable、llm_timeout）；未降级 NULL"
    )
    trace_id: Mapped[str | None] = mapped_column(
        Text(),
        index=True,
        nullable=True,
        comment="单次请求链路 ID（ER C3：与 Langfuse trace 一一对应，metrics JOIN 键）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="记录时刻（UTC 存储）",
    )
