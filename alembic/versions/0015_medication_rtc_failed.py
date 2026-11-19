"""用药事件 failure_reason 增 rtc_failed（0015）

PRD FR-MED-06 要求用药提醒走**反向 WebRTC 外呼**，建连失败再降级到 WS 页面播报；
`failure_reason` 因此需要一个「语音这条路没打通」的取值。原先四值里没有合适位置：
`no_answer` 是「响了没人接」（长者可能就在房间），`tts_failed` 是「播报环节失败」，
把 ICE 打不通 / SDP 互换没完成塞进任何一个都会污染统计口径——运维按值排查时会跑错方向。

只有三件事，且都只动 `medication_events` 一表一列上的约束与注释：

1. DROP 旧 CHECK、ADD 新 CHECK（五值）；
2. 列注释同步（`alembic check` 把注释也算漂移，0013 已经因注释漂移踩过一次）；
3. 末尾自检约束定义里确实出现了 `rtc_failed`。

约束名写**全名**、语句用**裸 SQL**：本库 `naming_convention` 的 `ck_%(table_name)s_
%(constraint_name)s` 会把 `op.create_check_constraint` 传入的名字再套一遍前缀，
落成 `ck_medication_events_ck_medication_events_failure_reason`（0012 的注释里记着这
个坑）。裸 SQL 里写库里真实的全名，与 ORM `CheckConstraint(name="failure_reason")`
经命名约定生成的 `ck_medication_events_failure_reason` 逐字一致。

downgrade 退回四值：已经落库的 `rtc_failed` 行会让 `ADD CONSTRAINT` 失败——这是**有意
的 fail closed**。回滚一个「归因更细」的迁移凭什么替业务改写历史事件？真要在有数据的
库上回滚，请先自己决定这些行归到 `no_answer` 还是 `tts_failed`，迁移不替你选。

Revision ID: 0015_medication_rtc_failed
Revises: 0014_media_url_unique
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0015_medication_rtc_failed"
down_revision = "0014_media_url_unique"
branch_labels = None
depends_on = None

_TABLE = "medication_events"
_CONSTRAINT = "ck_medication_events_failure_reason"

_NEW_COMMENT = "no_answer / denied / tts_failed / offline / rtc_failed"
_OLD_COMMENT = "no_answer / denied / tts_failed / offline"

_NEW_VALUES = "'no_answer', 'denied', 'tts_failed', 'offline', 'rtc_failed'"
_OLD_VALUES = "'no_answer', 'denied', 'tts_failed', 'offline'"


def _check_sql(values: str) -> str:
    """`failure_reason` 的 CHECK 体（NULL 放行：未终结的事件本来就还没有失败原因）。"""
    return f"failure_reason IS NULL OR failure_reason IN ({values})"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK ({_check_sql(_NEW_VALUES)})"
    )
    op.alter_column(
        _TABLE,
        "failure_reason",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=_NEW_COMMENT,
    )
    _assert_constraint_has("rtc_failed", expected=True)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK ({_check_sql(_OLD_VALUES)})"
    )
    op.alter_column(
        _TABLE,
        "failure_reason",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=_OLD_COMMENT,
    )
    _assert_constraint_has("rtc_failed", expected=False)


def _assert_constraint_has(value: str, *, expected: bool) -> None:
    """迁移末尾自检：库里生效的 CHECK 定义是否包含（或不包含）某个取值。

    为什么值得一次往返查询：`ALTER TABLE ... ADD CONSTRAINT` 用的是一段手写 SQL 字符串，
    写错列名或漏掉一个引号语法上都能过（PostgreSQL 会当成布尔表达式照单全收），
    表现却是「约束悄悄允许了非法值」——比迁移直接报错难查得多。宁可在这里失败。
    """
    definition = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = 'medication_events'::regclass"
            ),
            {"name": _CONSTRAINT},
        )
        .scalar_one_or_none()
    )
    if definition is None:  # pragma: no cover - 上一步 DROP/ADD 已保证它存在
        raise RuntimeError(f"constraint {_CONSTRAINT} not found after ALTER")
    present = f"'{value}'" in definition
    if present is not expected:
        state = "缺失" if expected else "残留"
        raise RuntimeError(f"{_CONSTRAINT} 中 {value} {state}，实际定义为：{definition}")


__all__: Sequence[str] = ["downgrade", "upgrade"]
