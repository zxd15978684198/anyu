"""用药到期复核与一键续方（0020）

FR-MED-09 要做三件事，其中两件需要落库位置，第三件顺带扩了通知枚举：

1. **续方链路要能回溯**：`medication_plans` 加 `renewed_from_plan_id uuid`（自引用 FK，
   **可空**：非续方新建的计划为 NULL）。续方 = 新开一条计划，旧计划保持 `active`
   自然到期——不加 `status` 新值（到期是时间事实，不是状态），所以只能靠这一列
   表达「这条是接着哪条开的」。`ondelete=SET NULL`：旧计划被物理删掉时续方行
   仍在，只是不再指回（本仓没有删除路径，这纯粹是不让外键挡住）。
2. **复核提醒要幂等**：加 `review_notified_at timestamptz`（**可空**）。每日 08:00
   扫描 `end_date ∈ [today+1, today+7]` 的 active 计划，只在
   `review_notified_at IS NULL` 时建 `medication_review` 通知并回写本列。
   用列而不是「查有没有旧通知」做闸门：通知可能被 acknowledged 关掉，那时
   再扫一遍就会重复提醒同一个药单。
3. **0020 附带 kind 枚举扩展**：新通知 kind=`medication_review` 必须同时放开
   `notifications.kind` 的 CHECK（`ck_notifications_kind` 加一个值）、
   `app/models/notification.py` 的 CHECK/列注释/表注释，与
   `notification_service.NOTIFICATION_KINDS`。三处不一起改，服务层建单会被
   表级 CHECK 挡下（`kind` 现在是闭集，不是自由文本）。

`renewed_from_plan_id` 建索引：复核扫描要反查「这条旧计划是不是已经被续过」
（`plan_id IN (SELECT renewed_from_plan_id ...)`），没有索引就是逐行扫计划表。

## downgrade 为什么 fail-closed

回滚会物理删掉续方来源与复核闸门：续方链断掉后再也拼不回「这条新计划接的是谁」，
而 `review_notified_at` 一删，下一次 08:00 扫描会把已经提醒过的药单再提醒一遍。
只要库里已有 `renewed_from_plan_id IS NOT NULL` / `review_notified_at IS NOT NULL`
的计划，或有 `kind='medication_review'` 的通知，就拒绝降级并报数量，要求人工先
导出这些行再决定——迁移不替业务丢掉不可重建的事实（与 0015 / 0018 同一条理由）。

Revision ID: 0020_medication_renew_review
Revises: 0019_medication_late_taken
Create Date: 2026-11-12

> 修订号必须 ≤32 字符：`alembic_version.version_num` 是 `varchar(32)`，
> 超了在 UPDATE 版本号时直接 `StringDataRightTruncation`（0018 踩过）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_medication_renew_review"
down_revision: str | None = "0019_medication_late_taken"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLAN_TABLE = "medication_plans"
_NOTIFICATION_TABLE = "notifications"

_RENEWED_COLUMN = "renewed_from_plan_id"
_NOTIFIED_COLUMN = "review_notified_at"
_RENEWED_FK = "fk_medication_plans_renewed_from_plan"
_RENEWED_INDEX = "ix_medication_plans_renewed_from_plan_id"
#: `notifications.kind` 那条 CHECK 的名字**不写死**：本库 naming_convention 会二次加
#: 前缀，模型里 `CheckConstraint(..., name="kind")` 落库成
#: `ck_notifications_ck_notifications_kind`（W7 门禁实测两个库一致），而第一版
#: 0020 按 `ck_notifications_kind` 写死，`DROP CONSTRAINT` 直接
#: `UndefinedObject: constraint "ck_notifications_kind" of relation "notifications"
#: does not exist`。改成按定义反查（见 `_kind_constraint_name`），下次改名不再炸。
_KIND_CONSTRAINT_KEYWORD = "kind"

# 列/表注释必须与 ORM 逐字一致（`alembic check` 把注释也算漂移）。
_RENEWED_COMMENT = "续方来源计划 ID；NULL=非续方新建（旧计划保持 active 自然到期，FR-MED-09）"
_NOTIFIED_COMMENT = "到期复核已通知时刻（UTC）；NULL=未通知过，每日 08:00 复核扫描回写（FR-MED-09）"
_KIND_COMMENT = (
    "health_review / medication_escalation / medication_review / collection_missed / "
    "family_report / safety_alert"
)
_OLD_KIND_COMMENT = (
    "health_review / medication_escalation / collection_missed / family_report / safety_alert"
)
_NOTIFICATION_TABLE_COMMENT = "站内通知（五类 kind × 六状态机，迭代 8 事件投递通道）"
_OLD_NOTIFICATION_TABLE_COMMENT = "站内通知（四类 kind × 六状态机，迭代 8 事件投递通道）"

_OLD_KIND_BODY = (
    "kind IN ('health_review', 'medication_escalation', 'collection_missed', "
    "'family_report', 'safety_alert')"
)
_NEW_KIND_BODY = (
    "kind IN ('health_review', 'medication_escalation', 'medication_review', "
    "'collection_missed', 'family_report', 'safety_alert')"
)

_NEW_PLAN_COLUMNS = (_RENEWED_COLUMN, _NOTIFIED_COLUMN)


def upgrade() -> None:
    op.add_column(
        _PLAN_TABLE,
        sa.Column(
            _RENEWED_COLUMN,
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=_RENEWED_COMMENT,
        ),
    )
    op.add_column(
        _PLAN_TABLE,
        sa.Column(
            _NOTIFIED_COLUMN,
            sa.DateTime(timezone=True),
            nullable=True,
            comment=_NOTIFIED_COMMENT,
        ),
    )
    op.create_foreign_key(
        _RENEWED_FK,
        _PLAN_TABLE,
        _PLAN_TABLE,
        [_RENEWED_COLUMN],
        ["plan_id"],
        ondelete="SET NULL",
    )
    op.create_index(_RENEWED_INDEX, _PLAN_TABLE, [_RENEWED_COLUMN])

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
    for column in _NEW_PLAN_COLUMNS:
        _assert_column_present(_PLAN_TABLE, column)
    _assert_foreign_key_present(_RENEWED_FK, _PLAN_TABLE)
    _assert_index_present(_RENEWED_INDEX)
    _assert_constraint_has(
        _kind_constraint_name(), _NOTIFICATION_TABLE, "medication_review", expected=True
    )


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
    _assert_constraint_has(
        _kind_constraint_name(), _NOTIFICATION_TABLE, "medication_review", expected=False
    )

    op.drop_index(_RENEWED_INDEX, table_name=_PLAN_TABLE)
    op.drop_constraint(_RENEWED_FK, _PLAN_TABLE, type_="foreignkey")
    for column in reversed(_NEW_PLAN_COLUMNS):
        op.drop_column(_PLAN_TABLE, column)


# ------------------------------------------------------------------ 约束替换


def _kind_constraint_name() -> str:
    """回读 `notifications` 上 kind 那条 CHECK 的真实约束名。

    本库 naming_convention 会二次加前缀（模型写 `name="kind"`，落库成
    `ck_notifications_ck_notifications_kind`），换环境/换 convention 又可能是别的
    名字，所以按「CHECK 定义里出现 kind」反查——severity / status 两条的定义里
    没有 `kind` 这个词，不会误伤。
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


def _assert_column_present(table: str, column: str) -> None:
    found = _scalar(
        sa.text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        table=table,
        column=column,
    )
    if not found:
        raise RuntimeError(f"migration self-check failed: column {table}.{column} is missing")


def _assert_index_present(name: str) -> None:
    found = _scalar(
        sa.text("SELECT count(*) FROM pg_indexes WHERE indexname = :name"),
        name=name,
    )
    if not found:
        raise RuntimeError(f"migration self-check failed: index {name} is missing")


def _assert_foreign_key_present(name: str, table: str) -> None:
    found = _scalar(
        sa.text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = :name AND conrelid = CAST(:table AS regclass) AND contype = 'f'"
        ),
        name=name,
        table=table,
    )
    if not found:
        raise RuntimeError(f"migration self-check failed: foreign key {name} is missing")


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
    """续方来源、复核闸门与 medication_review 通知都不可重建，有存量就拒绝降级。"""
    plans = _scalar(
        sa.text(
            f"SELECT count(*) FROM {_PLAN_TABLE} "  # noqa: S608 - 表名来自本文件常量
            "WHERE renewed_from_plan_id IS NOT NULL OR review_notified_at IS NOT NULL"
        )
    )
    if plans:
        raise RuntimeError(
            "downgrade refused: "
            f"{plans} medication plan(s) carry a renewal lineage or a review-notified stamp; "
            "export them first — dropping these columns loses facts that cannot be rebuilt"
        )
    notifications = _scalar(
        sa.text(
            f"SELECT count(*) FROM {_NOTIFICATION_TABLE} "  # noqa: S608 - 表名来自本文件常量
            "WHERE kind = 'medication_review'"
        )
    )
    if notifications:
        raise RuntimeError(
            "downgrade refused: "
            f"{notifications} notification(s) have kind='medication_review'; export them first "
            "— narrowing ck_notifications_kind would reject existing rows"
        )


__all__: Sequence[str] = ["downgrade", "upgrade"]
