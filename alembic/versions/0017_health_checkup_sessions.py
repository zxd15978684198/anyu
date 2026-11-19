"""0017 · 健康采集会话表（19:00 起问 + 每题 60 秒超时，PRD §4.6 FR-HLT-01 / AC-11）

## 为什么需要这次迁移

`app/graphs/health_checkup_graph.py` 的五问采集一直是「图能跑、没人驱动」：19:00 的
`run_health_checkup_start` 只给当日无记录的长者建一条 `health_review` 占位通知，
既没有起问，也没有 60 秒超时——FR-HLT-01 的两条硬要求都没落地。要让
「起问 → 等 60 秒 → 无答重问一次 → 再无效标缺失继续」这条链在进程重启后仍能接上，
必须有一处记录「当前问到哪一题、等到几点、已经答了什么」。本表就是那处记录。

## 做了什么

1. 新建 `health_checkup_sessions`，一位长者一天一行（`uq_health_checkup_day`）——
   `health_thread_id()` 同样按天收敛，三条写路径（起问 / 回答 / 超时扫描）争同一行；
2. `status` 四态 CHECK（asking / awaiting_review / completed / ended），`source` 两值
   （voice_scheduled / voice_active，与 `health_records.source` 的四值口径取交集）；
3. `ix_health_checkup_due (status, expires_at)`：超时扫描固定查
   `status='asking' AND expires_at <= now()`，没有这个索引每 30 秒扫一次全表；
4. **不动任何历史表**：`health_records` / `notifications` 结构一个字节没改。
   AC-11 的「标缺失继续」用图上已有的 `missing` 语义，落库仍走
   `health_service.persist_checkup_state`（黄/红照旧进 `health_review` 复核任务）。

## 命名约定（踩过的坑 · 本条已实测）

模型侧 `Base.metadata` 带 `ck_%(table_name)s_%(constraint_name)s` 约定，而 `op.create_table`
**也会**套用这份约定（`alembic` 从 `target_metadata` 取 `naming_convention`）——于是把
`CheckConstraint("status IN (...)", name="ck_health_checkup_sessions_status")` 写进
`create_table` 会得到双层前缀 `ck_health_checkup_sessions_ck_health_checkup_sessions_status`，
与模型名不一致，`alembic check` 立刻报约束名漂移（本迁移第一版就是这样炸的：
自检回查 `ck_health_checkup_sessions_status` 查不到，整条 DDL 事务回滚）。

所以 CHECK 约束一律**不进 `create_table`**，改在建表之后用
`op.execute("ALTER TABLE ... ADD CONSTRAINT <全名> CHECK (...))")` 写死全名；
PK / UNIQUE / FK 的名字不含 `%(constraint_name)s` 令牌，不受约定影响，留在 `create_table`
里即可（与 `0015_medication_rtc_failed` 用裸 SQL 写全名是同一手法）。

## downgrade 为什么 fail-closed

终态会话（completed / ended）只是流程留痕，采集结果在 `health_records` 里不缺，可以丢；
但**进行中**的会话（asking / awaiting_review）一丢表就再也续不上——长者已答的几问在
LangGraph checkpoint 里（另一套 schema，不由本迁移回滚），而「正在等哪一题、等到几点」
只有本表有。所以检测到进行中的会话就直接拒绝降级，要求人工确认后再执行。

Revision ID: 0017_health_checkup_sessions
Revises: 0016_daily_report_version
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_health_checkup_sessions"
down_revision: str | None = "0016_daily_report_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "health_checkup_sessions"
_STATUS_CHECK = "status IN ('asking', 'awaiting_review', 'completed', 'ended')"
_SOURCE_CHECK = "source IN ('voice_scheduled', 'voice_active')"
_OPEN_STATUSES = ("asking", "awaiting_review")

_PRIMARY_KEY = "pk_health_checkup_sessions"
_UNIQUE_DAY = "uq_health_checkup_day"
_ELDER_FK = "fk_health_checkup_sessions_elder_id"
_STATUS_NAME = "ck_health_checkup_sessions_status"
_SOURCE_NAME = "ck_health_checkup_sessions_source"
_ATTEMPT_NAME = "ck_health_checkup_sessions_attempt_positive"
_TIMEOUTS_NAME = "ck_health_checkup_sessions_timeouts_non_negative"
_DUE_INDEX = "ix_health_checkup_due"

# CHECK 约束用裸 SQL 添加，所以这里成对列出「全名 + 表达式」，名字与模型侧约定结果逐字一致。
_CHECK_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    (_STATUS_NAME, _STATUS_CHECK),
    (_SOURCE_NAME, _SOURCE_CHECK),
    (_ATTEMPT_NAME, "attempt_no >= 1"),
    (_TIMEOUTS_NAME, "timeouts >= 0"),
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="会话编号",
        ),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者 ID"),
        sa.Column(
            "record_date",
            sa.Date(),
            nullable=False,
            comment="会话归属日期（北京时间自然日，与 health_records 同口径）",
        ),
        sa.Column(
            "thread_id",
            sa.Text(),
            nullable=False,
            comment="LangGraph 线程键（health:{elder_id}:{record_date}），断线续问与复核 resume 都靠它",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'asking'"),
            nullable=False,
            comment="asking / awaiting_review / completed / ended",
        ),
        sa.Column(
            "source",
            sa.Text(),
            server_default=sa.text("'voice_scheduled'"),
            nullable=False,
            comment="voice_scheduled=19:00 定时采集，voice_active=长者主动发起（PRD §4.6 正文口径）",
        ),
        sa.Column(
            "current_key",
            sa.Text(),
            nullable=True,
            comment="当前挂起的问题字段（systolic/diastolic/...）；终态为 NULL",
        ),
        sa.Column(
            "prompt",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="当前问题的 interrupt 载荷（问句 + 第几次 + 上限），pad 拉取后直接播报",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="本题的回答截止（起问时刻 + 60 秒）；NULL=不等任何人（复核中或终态）",
        ),
        sa.Column(
            "attempt_no",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="当前这道题问到了第几次（1=首问，2=重问）；超过 MAX_ATTEMPTS 由图上直接标缺失",
        ),
        sa.Column(
            "timeouts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="本场累计无答超时次数（排障与话术用，不参与分级）",
        ),
        sa.Column(
            "answers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="已解析出的五字段快照；长者主动结束时据此照样落库（不丢已采集数据）",
        ),
        sa.Column(
            "missing",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="已判定缺失的字段（两次无效/超时）；落库时按缺失判黄（PRD §4.6）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="会话进入终态的时刻（completed / ended）；NULL=仍在进行",
        ),
        sa.PrimaryKeyConstraint("session_id", name=_PRIMARY_KEY),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            ondelete="CASCADE",
            name=_ELDER_FK,
        ),
        sa.UniqueConstraint("elder_id", "record_date", name=_UNIQUE_DAY),
        comment="健康采集会话（每题 60 秒超时、断点续问）",
    )
    # CHECK 必须用裸 SQL 写全名（理由见模块 docstring「命名约定」节）：
    # op.create_table 会把 Base.metadata 的 ck_%(table_name)s_%(constraint_name)s
    # 再套一层，传全名进去会变成双层前缀，与模型对不上。
    for name, expression in _CHECK_CONSTRAINTS:
        # 表名与约束名来自本文件模块级常量（不是外部输入），S608 在这里是假阳。
        op.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({expression})"  # noqa: S608
        )
    op.create_index(_DUE_INDEX, _TABLE, ["status", "expires_at"])

    # 自检：建完立刻回查，避免「迁移跑绿了但约束名/列名与模型不一致」拖到 alembic check 才暴露。
    _assert_constraint_present(_PRIMARY_KEY)
    _assert_constraint_present(_UNIQUE_DAY)
    _assert_constraint_present(_STATUS_NAME)
    _assert_column_present("expires_at")


def downgrade() -> None:
    _refuse_when_in_flight()
    op.drop_index(_DUE_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
    if _table_exists():
        raise RuntimeError(f"downgrade failed: table {_TABLE} still exists")


# ------------------------------------------------------------------ 自检与守卫


def _scalar(statement: sa.TextClause, **params: object) -> object:
    """跑一条只读 SQL 取首行首列（自检用；查不到返回 None，不抛 NoResultFound）。"""
    return op.get_bind().execute(statement, params).scalar_one_or_none()


def _constraint_definition(name: str) -> str | None:
    return _scalar(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = :name AND conrelid = CAST(:table AS regclass)"
        ),
        name=name,
        table=_TABLE,
    )


def _assert_constraint_present(name: str) -> None:
    if _constraint_definition(name) is None:
        raise RuntimeError(f"migration self-check failed: constraint {name} is missing")


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


def _table_exists() -> bool:
    return _scalar(sa.text("SELECT to_regclass(:table) IS NOT NULL"), table=_TABLE) is True


def _refuse_when_in_flight() -> None:
    """进行中的会话不可降级（理由见模块 docstring 的 fail-closed 段）。"""
    if not _table_exists():
        return
    in_flight = _scalar(
        sa.text("SELECT count(*) FROM health_checkup_sessions WHERE status = ANY(:states)"),
        states=list(_OPEN_STATUSES),
    )
    if in_flight:
        raise RuntimeError(
            "downgrade refused: "
            f"{in_flight} health checkup session(s) are still in flight "
            f"({', '.join(_OPEN_STATUSES)}); finish or end them first"
        )


__all__: Sequence[str] = ["downgrade", "upgrade"]
