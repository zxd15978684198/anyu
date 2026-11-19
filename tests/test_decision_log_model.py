"""迭代 6 · decision_logs 模型 + 迁移 0008 静态一致性测试（S-D6-M2 静校替代）。

本机 docker/alembic 不可验证库，一律不上库，三面静校：
1. ORM metadata 口径符合计划 W4 清单（列名/类型/可空/缺省/CHECK 文本/索引列序）；
2. 迁移 0008 文件静态校验（revision 链、create_table/drop_table 调用数、无上述
   白名单之外的破坏性语句）；
3. 模型 ↔ 迁移列名集合差集相等（防漂移，S-D6-M2 的替代护栏）。

模型按 Lead 指令未注册进 app/models/__init__.py（另一会话 WIP），直接 import 模块。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.models.decision_log import DecisionLog

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO_ROOT / "alembic" / "versions" / "0008_decision_logs.py"

_COLUMNS = {
    "id",
    "elder_id",
    "thread_id",
    "layer",
    "rule_id",
    "intent",
    "sensitive",
    "confidence",
    "is_out_of_domain",
    "privacy",
    "route_ms",
    "degraded",
    "trace_id",
    "created_at",
}
_NOT_NULL = {"id", "elder_id", "layer", "sensitive", "is_out_of_domain", "route_ms", "created_at"}
_NULLABLE = {"thread_id", "rule_id", "intent", "confidence", "privacy", "degraded", "trace_id"}
# 非空且必须由写入方提供的列：id/created_at 有 server_default，两个布尔有 default=False
_REQUIRED_NO_DEFAULT = {"elder_id", "layer", "route_ms"}


def _migration_text() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


# ---------------------------------------------------------------------------
# 1. ORM metadata 口径
# ---------------------------------------------------------------------------
def test_table_name_and_column_set() -> None:
    table = DecisionLog.__table__

    assert table.name == "decision_logs"
    assert set(table.columns.keys()) == _COLUMNS
    assert set(_NOT_NULL) | set(_NULLABLE) == _COLUMNS


def test_column_types() -> None:
    cols = DecisionLog.__table__.columns

    for name in (
        "elder_id",
        "thread_id",
        "layer",
        "rule_id",
        "intent",
        "privacy",
        "degraded",
        "trace_id",
    ):
        assert isinstance(cols[name].type, sa.Text), name
    for name in ("sensitive", "is_out_of_domain"):
        assert isinstance(cols[name].type, sa.Boolean), name
    assert isinstance(cols["id"].type, postgresql.UUID)
    assert isinstance(cols["confidence"].type, sa.Float)
    assert isinstance(cols["route_ms"].type, sa.Integer)
    created_at = cols["created_at"].type
    assert isinstance(created_at, sa.DateTime) and created_at.timezone is True


def test_column_nullability_and_required_defaults() -> None:
    cols = DecisionLog.__table__.columns

    assert all(not cols[name].nullable for name in _NOT_NULL)
    assert all(cols[name].nullable for name in _NULLABLE)
    # 必填三分账：业务必填 / ORM python default / DB server_default
    required = {
        n
        for n, c in cols.items()
        if not c.nullable and c.default is None and c.server_default is None
    }
    assert required == _REQUIRED_NO_DEFAULT
    assert cols["sensitive"].default.arg is False
    assert cols["is_out_of_domain"].default.arg is False
    assert cols["sensitive"].server_default is not None
    # server_default 用 text() 时 .arg 是 TextClause，字面比对走 str()
    assert str(cols["sensitive"].server_default.arg) == "false"
    assert str(cols["is_out_of_domain"].server_default.arg) == "false"
    assert str(cols["id"].server_default.arg) == "gen_random_uuid()"
    assert str(cols["created_at"].server_default.arg) == "now()"


def test_check_texts_live_in_metadata() -> None:
    checks = {
        c.name: str(c.sqltext)
        for c in DecisionLog.__table__.constraints
        if isinstance(c, sa.CheckConstraint)
    }
    # SQLA 2.1 在建表期就按 naming_convention 把短名渲染成全名（与迁移文件全名一致）
    assert checks.keys() == {"ck_decision_logs_layer", "ck_decision_logs_confidence"}
    # 口径钉死：枚举三值 + 置信 0..1 全文比对，越界写入必被库层挡住
    norm = {name: _norm(src) for name, src in checks.items()}
    assert norm["ck_decision_logs_layer"] == "layer IN ('L0', 'L1', 'L2')"
    assert (
        norm["ck_decision_logs_confidence"] == "confidence IS NULL OR (confidence BETWEEN 0 AND 1)"
    )


def test_indexes_column_order() -> None:
    idx = {ix.name: ix for ix in DecisionLog.__table__.indexes}
    assert set(idx) == {"ix_decision_logs_elder_created_at", "ix_decision_logs_trace_id"}

    elder_exprs = [str(e) for e in idx["ix_decision_logs_elder_created_at"].expressions]
    assert len(elder_exprs) == 2
    assert elder_exprs[0].endswith(".elder_id")
    # 倒序语义写在表达式里（text("created_at DESC")，0006 conversation_threads 同款）
    assert elder_exprs[1] == "created_at DESC"

    trace_exprs = [str(e) for e in idx["ix_decision_logs_trace_id"].expressions]
    assert len(trace_exprs) == 1 and trace_exprs[0].endswith(".trace_id")


def test_table_comment_pk_and_no_foreign_keys() -> None:
    table = DecisionLog.__table__

    assert table.comment == "三级路由决策日志（评测数据源，只增不改）"

    pks = [c for c in table.constraints if isinstance(c, sa.PrimaryKeyConstraint)]
    assert (
        len(pks) == 1
        and list(pks[0].columns.keys()) == ["id"]
        and pks[0].name == "pk_decision_logs"
    )
    assert not any(isinstance(c, sa.ForeignKeyConstraint) for c in table.constraints)
    assert not any(isinstance(c, sa.UniqueConstraint) for c in table.constraints)


# ---------------------------------------------------------------------------
# 2. ORM 实例构造合法性（不上库：走 ORM 缺省，metadata 即承接层）
# ---------------------------------------------------------------------------
def test_orm_instance_full_route_event() -> None:
    row = DecisionLog(
        id=uuid.uuid4(),
        elder_id="elder-001",
        thread_id="thread-001",
        layer="L1",
        rule_id=None,
        intent="weather",
        confidence=0.93,
        privacy="couple",
        route_ms=42,
        degraded=None,
        trace_id="trace-001",
    )

    assert row.confidence == 0.93 and row.rule_id is None
    assert row.route_ms == 42
    assert row.trace_id == "trace-001"
    # 未 flush 时 ORM default 不回填（flush 时才生效，DB 侧另有 server_default false 兜底）
    assert row.sensitive is None and row.is_out_of_domain is None


def test_orm_instance_minimal_l0_emergency() -> None:
    row = DecisionLog(
        elder_id="elder-001", layer="L0", rule_id="R-EMERGENCY", sensitive=True, route_ms=3
    )

    assert row.rule_id == "R-EMERGENCY"
    unset = sorted(_NULLABLE - {"rule_id"})
    assert row.sensitive is True
    for name in unset:
        assert getattr(row, name) is None, name
    assert row.id is None  # uuid4 default 由 PG gen_random_uuid() 承担，应用不填


# ---------------------------------------------------------------------------
# 3. 迁移文件静态校验
# ---------------------------------------------------------------------------
def test_migration_revision_chain() -> None:
    src = _migration_text()

    assert _MIGRATION.name == "0008_decision_logs.py"
    assert 'revision: str = "0008_decision_logs"' in src
    assert 'down_revision: str | None = "0007_health_reports"' in src
    assert "Create Date: 2026-11-07" in src


def test_migration_ops_shape_has_no_other_destructive_statements() -> None:
    src = _migration_text()

    # 单表进出：一次 create_table / 一次 drop_table，目标都是 decision_logs
    assert len(re.findall(r"\bop\.create_table\(", src)) == 1
    assert len(re.findall(r"\bop\.drop_table\(", src)) == 1
    assert re.findall(r'op\.create_table\(\s*"(\w+)"', src) == ["decision_logs"]
    assert re.findall(r'op\.drop_table\("(\w+)"', src) == ["decision_logs"]
    assert len(re.findall(r"\bop\.create_index\(", src)) == 1
    # 破坏性/变形语句白名单之外一律禁止（任务口径：除收尾 drop_table 外不得有）
    for banned in (
        "op.add_column(",
        "op.drop_column(",
        "op.alter_column(",
        "op.rename_table(",
        "op.rename_column(",
        "op.drop_constraint(",
        "op.create_constraint(",
        "op.bulk_insert(",
        "delete_table",
        "DELETE FROM",
        "TRUNCATE",
        "DROP TABLE",
    ):
        assert banned not in src, banned
    # op.execute 只承载 DESC 索引的原生 SQL（0006 先例），up/down 各一处
    assert len(re.findall(r"\bop\.execute\(", src)) == 2
    assert "CREATE INDEX IF NOT EXISTS ix_decision_logs_elder_created_at" in src
    assert "ON decision_logs (elder_id, created_at DESC)" in src
    assert "DROP INDEX IF EXISTS ix_decision_logs_elder_created_at" in src


def test_migration_upgrade_then_downgrade_order() -> None:
    src = _migration_text()

    up = src.index("def upgrade() -> None:")
    down = src.index("def downgrade() -> None:")
    assert (
        up
        < src.index("op.create_table(")
        < src.index('op.create_index("ix_decision_logs_trace_id"')
        < down
    )
    assert down < src.index('op.drop_table("decision_logs")')


def test_migration_constraints_and_defaults_match_model() -> None:
    src = _migration_text()

    # CHECK 常量原文 + 全名约束（与 ORM naming_convention 渲染结果逐字一致）
    assert "layer IN ('L0', 'L1', 'L2')" in src
    assert "confidence IS NULL OR (confidence BETWEEN 0 AND 1)" in src
    assert 'name="ck_decision_logs_layer"' in src
    assert 'name="ck_decision_logs_confidence"' in src
    assert 'name="pk_decision_logs"' in src
    # 索引名以首个位置参数出现（op.create_index / op.execute 内 SQL 字面）
    assert '"ix_decision_logs_trace_id"' in src
    assert "ix_decision_logs_elder_created_at" in src
    # server defaults：主键 uuid4 / 布尔 ×2 false / created_at now()
    assert 'server_default=sa.text("gen_random_uuid()")' in src
    assert src.count('server_default=sa.text("false")') == 2
    assert 'server_default=sa.text("now()")' in src


# ---------------------------------------------------------------------------
# 4. 模型 ↔ 迁移列一致性（防漂移）
# ---------------------------------------------------------------------------
def test_model_and_migration_column_sets_are_equal() -> None:
    src = _migration_text()

    migration_cols = set(re.findall(r'sa\.Column\(\s*"(\w+)"', src))
    model_cols = set(DecisionLog.__table__.columns.keys())

    assert migration_cols - model_cols == set(), "迁移多列（漂移）"
    assert model_cols - migration_cols == set(), "模型多列（迁移缺）"
