"""media_library.media_url 加唯一约束（同一份素材只能有一行）

Revision ID: 0014_media_url_unique
Revises: 0013_embedding_dim_1024
Create Date: 2026-11-12

## 为什么要这条约束

`media_url` 是素材的**天然身份**：`media/manifest.json` 用它当幂等键，
`scripts/seed_media.py` 靠「先 SELECT 再决定 insert/update」保证复跑不增行。

但那套「先查后写」在并发下不成立：两个进程同时查到「没有」，就会各自插入一行，
库里出现两条一模一样的素材，长者端面板里同一段戏出现两次，而播放历史会分叉到
两个 `media_id` 上（完播率统计因此失真）。

约束落地后，重复插入**报错**而不是静默留脏数据；`seed_media.py` 里每条插入都包在
SAVEPOINT 中，撞上唯一约束时回滚该条并按更新处理，所以并发跑不会再炸整批。

## 为什么 upgrade 要先查重复

建唯一索引前若已有重复行，PostgreSQL 会直接 fail 并指出重复值。这里的预检查
只是把失败信息说清楚（列出重复的 `media_url` 与行数），**不代为删除**：删哪一条
等于替运营决定「哪份是正版素材」，必须人工确认后单独清理再重跑迁移。

## 反向迁移

只丢约束，不动数据：约束去掉后重复行仍可能被重新写入，但这是回到迁移前的世界，
不是数据丢失。

## 与本项目其它表的口径

- 不用 `CREATE UNIQUE INDEX`：`create_unique_constraint` 产生的就是唯一索引，
  且约束名写进 `media_library` 的表定义更清楚是谁管这条规则。
- 不复用 `order_no` 那套「唯一约束 + savepoint 重试」（见 `work_order_service`）：
  素材 URL 重复没有「改个号再来」的余地，重试只会撞同一条。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_media_url_unique"
down_revision = "0013_embedding_dim_1024"
branch_labels = None
depends_on = None

_TABLE = "media_library"
_COLUMN = "media_url"
_CONSTRAINT = "uq_media_library_media_url"


def _assert_no_duplicates() -> None:
    """已有重复行就大声失败（并列出是哪几个 URL 重复），不悄悄删。"""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"SELECT {_COLUMN}, count(*) AS n FROM {_TABLE} "
            f"GROUP BY {_COLUMN} HAVING count(*) > 1 ORDER BY n DESC, {_COLUMN}"
        )
    ).all()
    if not rows:
        return
    detail = "；".join(f"{url}（{count} 行）" for url, count in rows)
    raise RuntimeError(
        f"{_TABLE}.{_COLUMN} 已有重复行，无法建唯一约束：{detail}。"
        "请人工确认保留哪一条后清理，再重跑 `alembic upgrade head`。"
    )


def upgrade() -> None:
    _assert_no_duplicates()
    op.create_unique_constraint(_CONSTRAINT, _TABLE, [_COLUMN])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
