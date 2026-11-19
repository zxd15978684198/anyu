"""四信号「建议关注」通知枚举扩展（0021）

FR-HLT-10 的四信号聚合（`alert_aggregation.evaluate_attention_signals`）本轮要接上
调度、落库与派单，而技术设计文档的 30 张业务表里**没有**「关注信号」表，所以承载口径
是「`notifications` + `work_orders`」，不新建表。

本迁移只做一件事：**放开 `notifications.kind` 的一档**，加 `attention_signal`
（六值 → 七值）。不放开的后果是服务层建单被表级 CHECK 挡下——`kind` 是闭集，
不是自由文本。

## 约束名为什么反查，不写死

本库 naming_convention 会把模型里的 `CheckConstraint(..., name="kind")` 二次加前缀，
落库成 `ck_notifications_ck_notifications_kind`（第三轮 W7 门禁实测两个库一致）。
0020 第一版按 `ck_notifications_kind` 写死，`DROP CONSTRAINT` 直接
`UndefinedObject: constraint "ck_notifications_kind" of relation "notifications" does not exist`。
所以这里沿用 0020 修好后的口径：按 `pg_constraint` + `pg_get_constraintdef(oid)
ILIKE '%kind%'` 反查真实名，drop / add / 自检 / downgrade 全部同口径。

## downgrade 为什么 fail-closed

把枚举收窄回六值会让库里已有的 `kind='attention_signal'` 行直接违反新 CHECK
（`ADD CONSTRAINT` 在存在违规行时会失败，报错还不如自己说清楚）。所以只要库里还有
这种通知，就拒绝降级并报数量，要求人工先导出/处理这些行——迁移不替业务丢掉不可重建
的「当时为什么提醒过这个人」的事实（与 0015 / 0018 / 0020 同一条理由）。

Revision ID: 0021_attention_notification
Revises: 0020_medication_renew_review
Create Date: 2026-11-12

> 修订号必须 ≤32 字符：`alembic_version.version_num` 是 `varchar(32)`，
> 超了在 UPDATE 版本号时直接 `StringDataRightTruncation`（0018 踩过）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_attention_notification"
down_revision: str | None = "0020_medication_renew_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOTIFICATION_TABLE = "notifications"

#: 见模块文档：真实约束名由 naming_convention 二次加前缀，只能反查。
_KIND_CONSTRAINT_KEYWORD = "kind"

#: 列/表注释必须与 ORM 逐字一致（`alembic check` 把注释也算漂移）。
_KIND_COMMENT = (
    "health_review / medication_escalation / medication_review / collection_missed / "
    "family_report / safety_alert / attention_signal"
)
_OLD_KIND_COMMENT = (
    "health_review / medication_escalation / medication_review / collection_missed / "
    "family_report / safety_alert"
)
_NOTIFICATION_TABLE_COMMENT = "站内通知（六类 kind × 六状态机，迭代 8 事件投递通道）"
_OLD_NOTIFICATION_TABLE_COMMENT = "站内通知（五类 kind × 六状态机，迭代 8 事件投递通道）"

_OLD_KIND_BODY = (
    "kind IN ('health_review', 'medication_escalation', 'medication_review', "
    "'collection_missed', 'family_report', 'safety_alert')"
)
_NEW_KIND_BODY = (
    "kind IN ('health_review', 'medication_escalation', 'medication_review', "
    "'collection_missed', 'family_report', 'safety_alert', 'attention_signal')"
)
_NEW_VALUE = "attention_signal"


def upgrade() -> None:
    _replace_kind_constraint(_NEW_KIND_BODY)
    op.alter_column(
        _NOTIFICATION_TABLE,
        "kind",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=_KIND_COMMENT,
    )
    op.execute(f"COMMENT ON TABLE {_NOTIFICATION_TABLE} IS {_quote(_NOTIFICATION_TABLE_COMMENT)}")

    # 自检：建完立刻回查，别让「迁移跑绿了但模型对不上」拖到 alembic check。
    _assert_constraint_has(_kind_constraint_name(), _NOTIFICATION_TABLE, _NEW_VALUE, expected=True)


def downgrade() -> None:
    _refuse_when_facts_exist()
    op.execute(
        f"COMMENT ON TABLE {_NOTIFICATION_TABLE} IS {_quote(_OLD_NOTIFICATION_TABLE_COMMENT)}"
    )
    op.alter_column(
        _NOTIFICATION_TABLE,
        "kind",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=_OLD_KIND_COMMENT,
    )
    _replace_kind_constraint(_OLD_KIND_BODY)
    _assert_constraint_has(_kind_constraint_name(), _NOTIFICATION_TABLE, _NEW_VALUE, expected=False)


# ------------------------------------------------------------------ 约束替换


def _kind_constraint_name() -> str:
    """回读 `notifications` 上 kind 那条 CHECK 的真实约束名（按定义里的 `kind` 反查）。

    `severity` / `status` 两条的定义里没有 `kind` 这个词，不会误伤。
    """
    name = _scalar(
        sa.text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = CAST(:table AS regclass) AND contype = 'c' "
            "AND pg_get_constraintdef(oid) ILIKE :keyword "
            "ORDER BY conname LIMIT 1"
        ),
        table=_NOTIFICATION_TABLE,
        keyword=f"%{_KIND_CONSTRAINT_KEYWORD}%",
    )
    if name is None:
        raise RuntimeError(
            "migration self-check failed: no CHECK constraint mentioning "
            f"{_KIND_CONSTRAINT_KEYWORD!r} on {_NOTIFICATION_TABLE}"
        )
    return str(name)


def _replace_kind_constraint(body: str) -> None:
    """先删后加，名字从库里反查（本库 naming_convention 会二次加前缀）。"""
    name = _kind_constraint_name()
    op.execute(f"ALTER TABLE {_NOTIFICATION_TABLE} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {_NOTIFICATION_TABLE} ADD CONSTRAINT {name} CHECK ({body})")


# ------------------------------------------------------------------ 自检与守卫


def _quote(value: str) -> str:
    """单引号字面量（`COMMENT ON` 不接受绑定参数）；本文件的注释里没有单引号。"""
    return "'" + value.replace("'", "''") + "'"


def _scalar(statement: sa.TextClause, **params: object) -> object:
    """跑一条只读 SQL 取首行首列（自检用；查不到返回 None，不抛 NoResultFound）。"""
    return op.get_bind().execute(statement, params).scalar_one_or_none()


def _assert_constraint_has(name: str, table: str, value: str, *, expected: bool) -> None:
    """回读 CHECK 定义，确认 `value` 在里面（或不在里面）。"""
    definition = _scalar(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = :name AND conrelid = CAST(:table AS regclass) AND contype = 'c'"
        ),
        name=name,
        table=table,
    )
    if definition is None:
        raise RuntimeError(f"migration self-check failed: constraint {name} is missing")
    present = value in str(definition)
    if present is not expected:
        state = "missing from" if expected else "still present in"
        raise RuntimeError(f"migration self-check failed: {value!r} is {state} {name}")


def _refuse_when_facts_exist() -> None:
    """已有 attention_signal 通知时不许收窄枚举：那些行会立刻违反六值 CHECK。"""
    notifications = _scalar(
        sa.text(
            f"SELECT count(*) FROM {_NOTIFICATION_TABLE} "  # noqa: S608 - 表名来自本文件常量
            "WHERE kind = :kind"
        ),
        kind=_NEW_VALUE,
    )
    if notifications:
        raise RuntimeError(
            "downgrade refused: "
            f"{notifications} notification(s) have kind={_NEW_VALUE!r}; export them first "
            "— narrowing the kind CHECK would reject existing rows"
        )


__all__: Sequence[str] = ["downgrade", "upgrade"]
