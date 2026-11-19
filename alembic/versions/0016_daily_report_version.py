"""家属日报增版本号 version（0016）

PRD FR-RPT-03 要求「补录版本更新」并**保留历史版本**（AC-25：首版日报 + 有效补录后
两版并存，新版只发一次「已更新」通知）。原表只有一个 `UNIQUE (elder_id, report_date)`，
同一天只许一行，补录只能覆盖旧版 —— 历史版本没地方放，AC-25 的验收根本无从谈起。

三件事，且只动 `daily_reports` 一表的列与唯一键：

1. ADD COLUMN `version integer NOT NULL DEFAULT 1`（存量行即首版）+ 列注释；
2. 唯一键从 `(elder_id, report_date)` 放宽成 `(elder_id, report_date, version)`
   —— 名字换新，旧名 `uq_daily_report_day` 没有任何地方引用，直接 DROP；
3. 末尾自检新唯一键定义里确实是三列（手写 SQL 写错列名照样能过，见 0015 的教训）。

列注释与 `DEFAULT` 都照 ORM 的 `server_default=text("1")` 写：`alembic check` 把注释和
默认值都算漂移，0013/0015 都因这条踩过一次。

约束名写**全名**、语句用**裸 SQL**：本库 `naming_convention` 里 `uq` 模板没有
`%(constraint_name)s` 令牌，显式名原样落库（旧名 `uq_daily_report_day` 就是证据），
但 CHECK 的 `ck` 模板有该令牌 —— 为了不让人误以为两种约束一样，这里统一裸 SQL。

downgrade 会把三列唯一键退回两列：**同一天已有多个版本时 `ADD CONSTRAINT` 必然失败**，
这是**有意的 fail closed**。回滚一个「允许历史版本」的迁移，凭什么替业务删掉旧版本行？
真要在有数据的库上回滚，请先自己决定保留哪一版，迁移不替你选。

Revision ID: 0016_daily_report_version
Revises: 0015_medication_rtc_failed
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0016_daily_report_version"
down_revision = "0015_medication_rtc_failed"
branch_labels = None
depends_on = None

_TABLE = "daily_reports"
_OLD_UNIQUE = "uq_daily_report_day"
_NEW_UNIQUE = "uq_daily_report_day_version"

_VERSION_COMMENT = "同日版本号：首版为 1，有效补录一次加一；旧版本行不删（FR-RPT-03 / AC-25）"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN version integer NOT NULL DEFAULT 1")
    op.alter_column(
        _TABLE,
        "version",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("1"),
        comment=_VERSION_COMMENT,
    )
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_OLD_UNIQUE}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_NEW_UNIQUE} UNIQUE (elder_id, report_date, version)"
    )
    _assert_unique_columns(("elder_id", "report_date", "version"))
    _assert_constraint_present(_OLD_UNIQUE, expected=False)


def downgrade() -> None:
    _refuse_when_versions_exist()
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_NEW_UNIQUE}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_OLD_UNIQUE} UNIQUE (elder_id, report_date)")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN version")
    _assert_unique_columns(("elder_id", "report_date"), constraint=_OLD_UNIQUE)


def _refuse_when_versions_exist() -> None:
    """同一天存在多个版本时拒绝回滚，并把话说清楚（fail closed）。"""
    duplicated = (
        op.get_bind()
        .execute(
            # 表名来自本文件模块级常量（不是外部输入），S608 在这里是假阳。
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} "  # noqa: S608
                "GROUP BY elder_id, report_date HAVING count(*) > 1) AS duplicated_days"
            )
        )
        .scalar_one()
    )
    if duplicated:
        raise RuntimeError(
            f"{_TABLE} 里有 {duplicated} 天存在多个版本，两列唯一键放不下；"
            "请先决定保留哪一版（本迁移不替业务删数据）"
        )


def _assert_unique_columns(columns: tuple[str, ...], *, constraint: str = _NEW_UNIQUE) -> None:
    """迁移末尾自检：库里生效的唯一键是否正好覆盖预期列。

    为什么值得一次往返查询：`ADD CONSTRAINT ... UNIQUE (...)` 是手写 SQL，列名写错时
    PostgreSQL 只会报「列不存在」——那还好；漏写一列却是合法的，约束会悄悄变松，
    表现是「同一天插进了两行首版」。宁可在这里失败。
    """
    definition = _constraint_definition(constraint)
    if definition is None:  # pragma: no cover - 上一步 ADD CONSTRAINT 已保证它存在
        raise RuntimeError(f"constraint {constraint} not found after ALTER")
    expected = "(" + ", ".join(columns) + ")"
    if expected not in definition:
        raise RuntimeError(f"{constraint} 未覆盖 {expected}，实际定义为：{definition}")


def _assert_constraint_present(name: str, *, expected: bool) -> None:
    """旧唯一键必须真的没了，否则新键形同虚设（两个 UNIQUE 会同时生效）。"""
    present = _constraint_definition(name) is not None
    if present is not expected:
        state = "仍存在" if expected else "未删除"
        raise RuntimeError(f"旧约束 {name} {state}")


def _constraint_definition(name: str) -> str | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = 'daily_reports'::regclass"
            ),
            {"name": name},
        )
        .scalar_one_or_none()
    )


__all__: Sequence[str] = ["downgrade", "upgrade"]
