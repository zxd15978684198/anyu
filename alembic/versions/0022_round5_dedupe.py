"""通知 / 工单幂等去重键（0022）

FR-ALT-04「页面刷新 / 重试 / 重启不重复建单」。现状取证：`notifications` 与
`work_orders` 都只有业务侧的逻辑查重（如 `find_open_daily_notification` 按
`payload.record_date` 查、`find_open_work_order` 按 elder+type 查），**没有一行是
数据库强制的**——两个请求同时查到「还没有」就各建一行，刷新页面 / 调度重启重跑
同样会多出一行。

本迁移只做一件事：给两张表各加一个可空去重键 + **部分唯一索引**，把「同键只能有
一行」从代码约定升级为库约束。

- `notifications`：`dedupe_key varchar(120)` NULL +
  `uq_notifications_dedupe_key (elder_id, kind, dedupe_key) WHERE dedupe_key IS NOT NULL`
- `work_orders`：`dedupe_key varchar(120)` NULL +
  `uq_work_orders_dedupe_key (elder_id, type, dedupe_key) WHERE dedupe_key IS NOT NULL`

## 为什么可空 + 部分索引

历史行没有键（迁移前建的通知/工单），人工重发（`resend`）与人工补派也不该复用
业务键；把「没有键」写成 NULL、把唯一性收窄到 `dedupe_key IS NOT NULL`，可以让
存量行零改动通过迁移，且**不改变**「多条无键行可以并存」的既有语义。

另注：PostgreSQL 的普通唯一索引把 NULL 视为互不相等，`WHERE dedupe_key IS NOT NULL`
不影响正确性，但能让索引更小、意图更明确（部分索引只索引真正参与去重的行）。

## downgrade

直接 drop index + drop column，可回滚；键是服务层传入的业务摘要（如
`medication:plan_id:scheduled_at`），**不是**业务事实，删掉不损失可重建的信息——
重复行本身仍在，只是失去库级兜底。

Revision ID: 0022_round5_dedupe
Revises: 0021_attention_notification
Create Date: 2026-11-12

> 修订号必须 ≤32 字符：`alembic_version.version_num` 是 `varchar(32)`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_round5_dedupe"
down_revision: str | None = "0021_attention_notification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOTIFICATION_TABLE = "notifications"
_WORK_ORDER_TABLE = "work_orders"

_NOTIFICATION_INDEX = "uq_notifications_dedupe_key"
_WORK_ORDER_INDEX = "uq_work_orders_dedupe_key"

_DEDUPE_KEY_TYPE = sa.String(length=120)

_NOTIFICATION_COMMENT = "幂等去重键（FR-ALT-04）；同 elder_id + kind 下唯一，人工重发不复用"
_WORK_ORDER_COMMENT = "幂等去重键（FR-ALT-04）；同 elder_id + type 下唯一，人工补派不复用"

_PARTIAL_PREDICATE = sa.text("dedupe_key IS NOT NULL")


def upgrade() -> None:
    _add_dedupe_key(
        table=_NOTIFICATION_TABLE,
        comment=_NOTIFICATION_COMMENT,
    )
    op.create_index(
        _NOTIFICATION_INDEX,
        _NOTIFICATION_TABLE,
        ["elder_id", "kind", "dedupe_key"],
        unique=True,
        postgresql_where=_PARTIAL_PREDICATE,
    )
    _add_dedupe_key(
        table=_WORK_ORDER_TABLE,
        comment=_WORK_ORDER_COMMENT,
    )
    op.create_index(
        _WORK_ORDER_INDEX,
        _WORK_ORDER_TABLE,
        ["elder_id", "type", "dedupe_key"],
        unique=True,
        postgresql_where=_PARTIAL_PREDICATE,
    )

    # 迁移自称做完了，就要能自证：列在、索引在、且索引确实是部分唯一索引。
    for table in (_NOTIFICATION_TABLE, _WORK_ORDER_TABLE):
        _require(_column_exists(table, "dedupe_key"), f"{table}.dedupe_key 未创建")
    for name in (_NOTIFICATION_INDEX, _WORK_ORDER_INDEX):
        definition = _index_definition(name)
        _require(definition is not None, f"索引 {name} 未创建")
        _require("UNIQUE" in definition.upper(), f"索引 {name} 不是唯一索引")
        _require("WHERE" in definition.upper(), f"索引 {name} 不是部分索引")


def downgrade() -> None:
    op.drop_index(_WORK_ORDER_INDEX, table_name=_WORK_ORDER_TABLE)
    op.drop_column(_WORK_ORDER_TABLE, "dedupe_key")
    op.drop_index(_NOTIFICATION_INDEX, table_name=_NOTIFICATION_TABLE)
    op.drop_column(_NOTIFICATION_TABLE, "dedupe_key")

    for table in (_NOTIFICATION_TABLE, _WORK_ORDER_TABLE):
        _require(not _column_exists(table, "dedupe_key"), f"{table}.dedupe_key 未删除")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _add_dedupe_key(*, table: str, comment: str) -> None:
    op.add_column(
        table,
        sa.Column("dedupe_key", _DEDUPE_KEY_TYPE, nullable=True, comment=comment),
    )


def _scalar(statement: str, **params: object) -> object:
    return op.get_bind().execute(sa.text(statement), params).scalar()


def _column_exists(table: str, column: str) -> bool:
    found = _scalar(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :table "
        "AND column_name = :column",
        table=table,
        column=column,
    )
    return found is not None


def _index_definition(name: str) -> str | None:
    definition = _scalar(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :name",
        name=name,
    )
    return str(definition) if definition is not None else None


__all__: Sequence[str] = ["downgrade", "upgrade"]
