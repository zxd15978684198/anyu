"""通知 kind 增加 work_order（0024）

C1「工单流转 → 家属通知」：工单被接单/关闭后给该长者发一条 `kind="work_order"`
通知，家属端按 elder 范围可见。`notifications.kind` 是 DB CHECK，加新取值必须迁移。

## 为什么名字不能写死（0020 / 0023 的老坑）

本库 `Base.metadata.naming_convention` 的 `ck` 模板是
`ck_%(table_name)s_%(constraint_name)s`；历史迁移写死过完整名字，库里真实名字是
`ck_notifications_ck_notifications_kind`。所以这里先按**定义内容**反查（定义里含
`health_review` 的那条），再 DROP，然后按期望名重建，最后回读校验定义。

Revision ID: 0024_work_order_kind
Revises: 0023_elder_checkup_start
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0024_work_order_kind"
down_revision = "0023_elder_checkup_start"
branch_labels = None
depends_on = None

_KINDS_NEW = (
    "health_review",
    "medication_escalation",
    "medication_review",
    "collection_missed",
    "family_report",
    "safety_alert",
    "attention_signal",
    "work_order",
)
_KINDS_OLD = _KINDS_NEW[:-1]

_DROP_OLD = """
DO $$
DECLARE
    cname text;
BEGIN
    SELECT conname INTO cname
      FROM pg_constraint
     WHERE conrelid = 'notifications'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%health_review%';
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE notifications DROP CONSTRAINT %I', cname);
    END IF;
END
$$;
"""

_VERIFY = """
SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = 'notifications'::regclass
   AND conname = 'ck_notifications_kind';
"""


def _render(kinds: tuple[str, ...]) -> str:
    return ", ".join("'{0}'".format(kind) for kind in kinds)


def _apply(kinds: tuple[str, ...]) -> None:
    conn = op.get_bind()
    # psycopg 把裸 SQL 里的 `%` 当占位符，DO 块里的 LIKE '%health_review%' 需要转义。
    conn.exec_driver_sql(_DROP_OLD.replace("%", "%%"))
    conn.exec_driver_sql(
        "ALTER TABLE notifications ADD CONSTRAINT ck_notifications_kind "
        "CHECK (kind IN (" + _render(kinds) + "))"
    )
    row = conn.exec_driver_sql(_VERIFY).first()
    if row is None:
        raise RuntimeError("ck_notifications_kind was not created")
    for kind in kinds:
        if "'" + kind + "'" not in row[0]:
            raise RuntimeError("constraint definition is missing " + kind)


def upgrade() -> None:
    _apply(_KINDS_NEW)


def downgrade() -> None:
    _apply(_KINDS_OLD)
