"""用药终态增 late_taken_reported（0019）

## 为什么需要这次迁移

MED-03：长者「没服药 / 不应答」已经落成终态（`refused` / `missed_unconfirmed` /
`notification_failed`）之后，仍可能在晚些时候补一句「我刚吃了」。当前
`medication_service._resume_or_write()` 对确认动作**无条件**写
`status='confirmed', terminal_state='taken_reported'`——等于把「当时判定没吃」这条历史
抹掉，日报的四类分列也随之失真（事后补确认既不是按时服药，也不是未确认）。

所以新增第五个终态 `late_taken_reported`（事后追认），并规定：**只追加、不覆盖**。
原终态保留在库里，补确认只把终态推进到 `late_taken_reported`。

## 为什么是「新增值」而不是「替换」

存量行全部落在原四值内，`DROP CONSTRAINT` + `ADD CONSTRAINT`（五值）对它们是恒真命题，
迁移在任何数据量的库上都能通过；反过来若删掉某个旧值，存量行会让 `ADD CONSTRAINT` 直接
失败——这正是 §2 风险条列出的那类「收紧 CHECK 与存量数据冲突」。

## 约束名与裸 SQL

`ck_medication_events_terminal_state` 是全名：本库命名约定的
`ck_%(table_name)s_%(constraint_name)s` 会把 `op.create_check_constraint` 传入的短名
再套一层前缀（0015 docstring 记着这个坑），所以沿用 0015 的手法，用裸 SQL 写库里真实全名。

## downgrade 为什么 fail-closed

退回四值时，已有 `late_taken_reported` 的行会让 `ADD CONSTRAINT` 失败。这是**有意的**：
回滚一个「归因更细」的迁移，凭什么替业务把这些事件改写成「按时服药」或「未确认」？
真要在有数据的库上回滚，请先自己决定归类，迁移不替你选（与 0015 同口径，但这里先做
一次显式清点并给出数量，失败信息比原生 CheckViolation 更好读）。

Revision ID: 0019_medication_late_taken
Revises: 0018_health_complaint_revoke
Create Date: 2026-11-12

> 修订号必须 ≤32 字符：`alembic_version.version_num` 是 `varchar(32)`（0018 第一版
> 踩过，UPDATE 版本号时 `StringDataRightTruncation`），本文件沿用短名。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_medication_late_taken"
down_revision: str | None = "0018_health_complaint_revoke"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "medication_events"
_CONSTRAINT = "ck_medication_events_terminal_state"
_NEW_VALUE = "late_taken_reported"

_NEW_VALUES = (
    "'taken_reported', 'refused', 'missed_unconfirmed', 'notification_failed', "
    "'late_taken_reported'"
)
_OLD_VALUES = "'taken_reported', 'refused', 'missed_unconfirmed', 'notification_failed'"

# 列注释必须与 `app/models/medication.py` 逐字一致（`alembic check` 把注释也算漂移）。
_NEW_COMMENT = (
    "五值终态；未终结为 NULL（映射见 service.TERMINAL_MAP）。"
    "late_taken_reported=原终态已是未服/未确认，事后补确认只能追认不能覆盖（MED-03）"
)
_OLD_COMMENT = "四值终态；未终结为 NULL（映射见 service.TERMINAL_MAP）"


def _check_sql(values: str) -> str:
    """`terminal_state` 的 CHECK 体（NULL 放行：未终结的事件本来就还没有终态）。"""
    return f"terminal_state IS NULL OR terminal_state IN ({values})"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK ({_check_sql(_NEW_VALUES)})")
    op.alter_column(
        _TABLE,
        "terminal_state",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=_NEW_COMMENT,
    )
    _assert_constraint_has(_NEW_VALUE, expected=True)


def downgrade() -> None:
    _refuse_when_present()
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK ({_check_sql(_OLD_VALUES)})")
    op.alter_column(
        _TABLE,
        "terminal_state",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=_OLD_COMMENT,
    )
    _assert_constraint_has(_NEW_VALUE, expected=False)


# ------------------------------------------------------------------ 自检与守卫


def _constraint_definition() -> str | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = CAST(:table AS regclass)"
            ),
            {"name": _CONSTRAINT, "table": _TABLE},
        )
        .scalar_one_or_none()
    )


def _assert_constraint_has(value: str, *, expected: bool) -> None:
    """迁移末尾自检：库里生效的 CHECK 定义是否包含（或不包含）某个取值。

    `ALTER TABLE ... ADD CONSTRAINT` 用的是一段手写 SQL 字符串，列名写错或漏引号在
    语法上都能过（PostgreSQL 会当布尔表达式照收），表现却是「约束悄悄允许了非法值」。
    宁可在这里失败。
    """
    definition = _constraint_definition()
    if definition is None:  # pragma: no cover - 上一步 DROP/ADD 已保证它存在
        raise RuntimeError(f"constraint {_CONSTRAINT} not found after ALTER")
    present = f"'{value}'" in definition
    if present is not expected:
        state = "缺失" if expected else "残留"
        raise RuntimeError(f"{_CONSTRAINT} 中 {value} {state}，实际定义为：{definition}")


def _refuse_when_present() -> None:
    """有 late_taken_reported 行就拒绝降级（理由见模块 docstring 的 fail-closed 段）。"""
    affected = (
        op.get_bind()
        .execute(
            sa.text(f"SELECT count(*) FROM {_TABLE} WHERE terminal_state = :state"),  # noqa: S608
            {"state": _NEW_VALUE},
        )
        .scalar_one_or_none()
    )
    if affected:
        raise RuntimeError(
            "downgrade refused: "
            f"{affected} medication event(s) already carry terminal_state='{_NEW_VALUE}'; "
            "decide how they should be reclassified (taken_reported? missed_unconfirmed?) first"
        )


__all__: Sequence[str] = ["downgrade", "upgrade"]
