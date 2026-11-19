"""健康记录增主诉与撤销留痕（0018）

两块缺口各自需要一个落库位置，且都只动 `health_records` 一张表：

1. **HLT-07 主诉链路是死代码**：`health_checkup_graph.start_complaint_checkup()` 与
   `health_service.record_complaint()` 生产零调用，模型也没有承载主诉原文的列——主诉
   只被塞进 `notifications.payload` 的 JSONB 里。可通知是「给人的收件箱」，复核单被
   关掉/改派后原文就跟着走；而主诉恰恰是复核医生最需要的事实。加 `complaint Text`
   （**可空**：定时采集、代填补录本来就没有主诉）。
2. **HLT-04 健康记录无撤销**：只有补录版本链（`superseded_by` 软删），没有「这条记录
   本身不该采/采错」的语义。加 `revoked_at timestamptz` + `revoked_by uuid` 两列做
   软删留痕：**不删行、不改历史值**，日报与趋势查询都加 `revoked_at IS NULL`。

## 为什么新增 `revoked_by` 外键而不是复用 `recorded_by_staff_id`

两者语义不同：`recorded_by_staff_id` 是「谁录的」，撤销可能是另一位医生复核后做的。
合成一列会让审计问答（谁录的 / 谁撤的）无法区分。`ondelete=SET NULL` 与
`recorded_by_staff_id` 同口径：员工注销后撤销这件事仍然成立，只是不再指向人。

## downgrade 为什么 fail-closed

回滚会**物理删除**主诉原文与撤销痕迹（软删语义一旦失去列就不可恢复）。只要库里已有
任一 `complaint IS NOT NULL` 或 `revoked_at IS NOT NULL` 的行，就直接拒绝降级并报数量，
要求人工先导出这些行再决定——迁移不替业务丢掉不可重建的事实（与 0015 同一条理由）。

Revision ID: 0018_health_complaint_revoke
Revises: 0017_health_checkup_sessions
Create Date: 2026-11-12

> 修订号必须 ≤32 字符：`alembic_version.version_num` 是 `varchar(32)`，第一版
> `0018_day10_health_complaint_revoke`（35 字符）在 UPDATE 版本号时直接
> `StringDataRightTruncation`。**这是本迁移踩过的坑**，后续同批迁移不复用。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_health_complaint_revoke"
down_revision: str | None = "0017_health_checkup_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "health_records"
_REVOKED_FK = "fk_health_records_revoked_by"

# 列注释必须与 `app/models/health.py` 逐字一致（`alembic check` 把注释也算漂移）。
_COMPLAINT_COMMENT = "长者主动主诉原文；NULL=非主诉发起（HLT-07，原文只留业务行不进审计日志）"
_REVOKED_AT_COMMENT = "撤销时刻（UTC）；NULL=未撤销。撤销是软删留痕，不删行（HLT-04）"
_REVOKED_BY_COMMENT = "执行撤销的员工 staff_id（医生/管理员）；NULL=未撤销或员工已注销"

_NEW_COLUMNS = ("complaint", "revoked_at", "revoked_by")


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("complaint", sa.Text(), nullable=True, comment=_COMPLAINT_COMMENT),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=_REVOKED_AT_COMMENT,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "revoked_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=_REVOKED_BY_COMMENT,
        ),
    )
    op.create_foreign_key(
        _REVOKED_FK,
        _TABLE,
        "staff_accounts",
        ["revoked_by"],
        ["staff_id"],
        ondelete="SET NULL",
    )

    # 自检：列与 FK 建完立刻回查，别让「迁移跑绿了但模型对不上」拖到 alembic check。
    for column in _NEW_COLUMNS:
        _assert_column_present(column)
    _assert_foreign_key_present(_REVOKED_FK)


def downgrade() -> None:
    _refuse_when_facts_exist()
    op.drop_constraint(_REVOKED_FK, _TABLE, type_="foreignkey")
    for column in reversed(_NEW_COLUMNS):
        op.drop_column(_TABLE, column)


# ------------------------------------------------------------------ 自检与守卫


def _scalar(statement: sa.TextClause, **params: object) -> object:
    """跑一条只读 SQL 取首行首列（自检用；查不到返回 None，不抛 NoResultFound）。"""
    return op.get_bind().execute(statement, params).scalar_one_or_none()


def _assert_column_present(column: str) -> None:
    found = _scalar(
        sa.text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        table=_TABLE,
        column=column,
    )
    if not found:
        raise RuntimeError(f"migration self-check failed: column {column} is missing")


def _assert_foreign_key_present(name: str) -> None:
    found = _scalar(
        sa.text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = :name AND conrelid = CAST(:table AS regclass) AND contype = 'f'"
        ),
        name=name,
        table=_TABLE,
    )
    if not found:
        raise RuntimeError(f"migration self-check failed: foreign key {name} is missing")


def _refuse_when_facts_exist() -> None:
    """主诉原文与撤销痕迹不可重建，有存量行就拒绝降级（理由见模块 docstring）。"""
    affected = _scalar(
        sa.text(
            f"SELECT count(*) FROM {_TABLE} "  # noqa: S608 - 表名来自本文件常量
            "WHERE complaint IS NOT NULL OR revoked_at IS NOT NULL"
        )
    )
    if affected:
        raise RuntimeError(
            "downgrade refused: "
            f"{affected} health record(s) carry a complaint or a revocation timestamp; "
            "export them first — dropping these columns loses facts that cannot be rebuilt"
        )


__all__: Sequence[str] = ["downgrade", "upgrade"]
