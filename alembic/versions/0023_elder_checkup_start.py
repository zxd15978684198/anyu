"""长者自定义采集时刻（0023）

FR-HLT-05「采集时刻可自定义」：`elders` 加一列 `checkup_start varchar(5) NULL`，
存长者自己的 `HH:MM`；NULL 表示「用全局默认」（`settings.health_checkup_start`，19:00）。

## 为什么是 NULL 列、不回填 19:00

回填等于把「今天是 19:00」固化成历史事实：日后全局默认改成 18:30，这批长者会被
钉死在旧时刻上，而且从数据面上再也分不清「他自己选的 19:00」和「当年默认的 19:00」。
所以升级时**一行都不更新**，语义交给调度侧的 `COALESCE(elders.checkup_start, 全局默认)`。

## CHECK 为什么用正则而不是两个 BETWEEN

「小时 00–23、分钟 00–59」在字符串列上用一个正则就表达完，且能一并挡掉 `19:0`、
`1:30`、`19:00:00` 这类长度对但形态错的串；拆成两段还需先 `split_part` 再转型，
非法串会先在转型上炸出 22P02 而不是干净的约束冲突。正则与
`app/models/organization.py` 里 `CheckConstraint(name="checkup_start_format")` 逐字一致。

## 约束名为什么**不能**写死（0020 的老坑，本迁移第一版就踩了）

本库 `Base.metadata.naming_convention` 的 `ck` 模板是 `ck_%(table_name)s_%(constraint_name)s`，
`op.create_check_constraint("ck_elders_checkup_start_format", ...)` 在带 convention 的
环境里会被**二次加前缀**，库里真实名字是
`ck_elders_ck_elders_checkup_start_format`（0020 的
`ck_notifications_ck_notifications_kind` 同款），于是自检按写死的名字查不到、
`DROP CONSTRAINT` 也会 `UndefinedObject`。

所以本迁移不再给 Alembic 传名字，而是：

1. `upgrade` 用 `ALTER TABLE ... ADD CONSTRAINT <期望名> CHECK (...)` 直接下发 DDL——
   裸 DDL 不过 naming_convention，名字就是写下的那个，与模型 `name="checkup_start_format"`
   套模板后的结果（`ck_elders_checkup_start_format`）一致；
2. 加完**回读** `pg_constraint` 里定义含 `checkup_start` 的那条 CHECK 的 `conname`，
   与期望名比对，不一致就报错（宁可升级失败，也不留一个名字对不上模型的约束）；
3. `downgrade` 同样先反查真实名字再 `DROP CONSTRAINT`，不写死。

Revision ID: 0023_elder_checkup_start
Revises: 0022_round5_dedupe
Create Date: 2026-11-12

> 修订号必须 ≤32 字符：`alembic_version.version_num` 是 `varchar(32)`。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_elder_checkup_start"
down_revision: str | None = "0022_round5_dedupe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "elders"
_COLUMN = "checkup_start"

#: 与模型逐字一致（`alembic check` 把注释也算漂移）。
_COLUMN_COMMENT = "自定义采集时刻 HH:MM；NULL = 用全局默认"

#: 小时 00–23、分钟 00–59；等价于「HH:MM 形态 + 两个取值范围」。
_FORMAT_BODY = r"checkup_start ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'"

#: 模型套 naming_convention 之后应有的落库名。只在自检里用于比对，不参与 DDL 猜测。
_EXPECTED_CONSTRAINT = "ck_elders_checkup_start_format"

#: 反查用的定义关键字：`elders` 上没有别的 CHECK 提到这个列。
_DEFINITION_KEYWORD = "%checkup_start%"

#: 反查结果要回填进 DDL，先挡住非法标识符（防御性，实际只会是约束名）。
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(length=5),
            nullable=True,
            comment=_COLUMN_COMMENT,
        ),
    )

    # 裸 DDL 下发，名字不被 naming_convention 二次加前缀（见模块文档）。
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_EXPECTED_CONSTRAINT} CHECK ({_FORMAT_BODY})")

    # 自检：列与约束都得真的在，名字还得与模型口径一致。
    _assert_column_exists()
    actual = _format_constraint_name()
    if actual != _EXPECTED_CONSTRAINT:
        raise RuntimeError(
            "migration self-check failed: constraint name drifted, "
            f"expected {_EXPECTED_CONSTRAINT!r} but database has {actual!r}"
        )

    # 明确不写 UPDATE：见模块文档「为什么不回填 19:00」。
    leftover = _scalar_sync(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} IS NOT NULL")  # noqa: S608
    )
    if leftover:
        raise RuntimeError(
            "migration self-check failed: "
            f"{leftover} elder row(s) already have {_COLUMN}; this migration must not backfill"
        )


def downgrade() -> None:
    # 先反查真名再删约束，最后删列：反过来列没了，按定义反查也会失配。
    name = _format_constraint_name()
    if name is None:
        raise RuntimeError(
            f"migration downgrade failed: no CHECK constraint mentioning {_COLUMN!r} on {_TABLE}"
        )
    if not _SAFE_IDENTIFIER.match(name):
        raise RuntimeError(f"migration downgrade failed: unexpected constraint name {name!r}")
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {name}")
    op.drop_column(_TABLE, _COLUMN)


# ------------------------------------------------------------------ 自检


def _scalar_sync(statement: sa.TextClause, **params: object) -> object:
    """跑一条只读 SQL 取首行首列（自检用；查不到返回 None，不抛 NoResultFound）。"""
    return op.get_bind().execute(statement, params).scalar_one_or_none()


def _format_constraint_name() -> str | None:
    """回读 `elders` 上格式 CHECK 的真实约束名；没有则 None。

    按「CHECK 定义里出现 `checkup_start`」反查，而不是按名字拼——本库 convention
    会给名字二次加前缀（见模块文档），写死名字必然踩空。
    """
    found = _scalar_sync(
        sa.text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = CAST(:table AS regclass) AND contype = 'c' "
            "AND pg_get_constraintdef(oid) ILIKE :keyword "
            "ORDER BY conname LIMIT 1"
        ),
        table=_TABLE,
        keyword=_DEFINITION_KEYWORD,
    )
    return None if found is None else str(found)


def _assert_column_exists() -> None:
    found = _scalar_sync(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        table=_TABLE,
        column=_COLUMN,
    )
    if found is None:
        raise RuntimeError(f"migration self-check failed: {_TABLE}.{_COLUMN} is missing")


__all__: Sequence[str] = ["downgrade", "upgrade"]
