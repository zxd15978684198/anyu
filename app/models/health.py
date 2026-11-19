"""健康采集记录 ORM（五字段 + 分级 + 版本链）。

列定义对齐 `alembic/versions/0007_day8_health_daily_notifications.py` 与
PRD §4.6（5 个问题对应 5 个结构化字段）。

设计要点：
- `source` 沿用 PRD §4.6 正文统一口径四枚举（二维 channel×trigger 折叠），
  而不是 FR-HLT-06 行内的旧措辞 `staff_report`（正文明写「与 早期设计方案对齐」）。
- 补录走**新 revision 行**（UNIQUE (elder_id, record_date, revision)），
  不 UPDATE 原行 → FR-HLT-04「补录追加版本不覆盖原始值」；原值的
  `superseded_by` 指向新行（软删语义，与 audit_logs 的「只增」同为留痕）。
- `grade` 由服务层 `grade_vitals` 纯函数得出后写入，分级阈值全部在代码里
  （PRD §4.6：单分量判红收缩 ≥180 或 舒张 ≥120；黄 140–179 / 90–119；
  缺失判黄；情绪/睡眠只入黄不产红），不写进任何 prompt。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_DATE = datetime.date


class HealthRecord(Base):
    """一次采集的五个字段与分级结果；补录即新版本行，原始行不改。"""

    __tablename__ = "health_records"
    __table_args__ = (
        UniqueConstraint("elder_id", "record_date", "revision", name="uq_health_version"),
        CheckConstraint(
            "source IN ('voice_scheduled', 'voice_active', 'staff_scheduled', 'staff_active')",
            name="source",
        ),
        CheckConstraint("grade IN ('green', 'yellow', 'red', 'missing')", name="grade"),
        CheckConstraint("mood_score IS NULL OR (mood_score BETWEEN 1 AND 5)", name="mood_range"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_health_records_elder_date", "elder_id", "record_date"),
        {"comment": "健康采集记录（五字段 + 分级 + 版本链）"},
    )

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="记录编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE", name="fk_health_records_elder_id"),
        nullable=False,
        comment="长者 ID",
    )
    record_date: Mapped[_DATE] = mapped_column(
        Date(), nullable=False, comment="采集归属日期（北京时间对应的自然日）"
    )
    source: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="voice_scheduled / voice_active / staff_scheduled / staff_active（PRD §4.6 正文统一口径）",
    )
    systolic_mmhg: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="收缩压 mmHg（问题 1）"
    )
    diastolic_mmhg: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="舒张压 mmHg（问题 2）"
    )
    heart_rate_bpm: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="静息心率 bpm（问题 3）"
    )
    sleep_hours: Mapped[float | None] = mapped_column(
        Numeric(4, 1), nullable=True, comment="昨夜睡眠小时数（问题 4）"
    )
    mood_score: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="情绪自评 1–5（问题 5）"
    )
    grade: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="分级结果：green / yellow / red / missing（缺失判黄）"
    )
    abnormal_reasons: Mapped[list[str] | None] = mapped_column(
        JSONB(),
        nullable=True,
        comment="判黄/红的具体依据，如 ['systolic>=180']；由服务层 grade_vitals 生成",
    )
    revision: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("1"), comment="同日版本号，1 起"
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "health_records.record_id",
            ondelete="SET NULL",
            name="fk_health_records_superseded_by",
            use_alter=True,
        ),
        nullable=True,
        comment="被哪个新版本取代；NULL=当前生效版本",
    )
    recorded_by_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "staff_accounts.staff_id", ondelete="SET NULL", name="fk_health_records_recorded_by"
        ),
        nullable=True,
        comment="代填操作者；语音渠道为 NULL",
    )
    complaint: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="长者主动主诉原文；NULL=非主诉发起（HLT-07，原文只留业务行不进审计日志）",
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="撤销时刻（UTC）；NULL=未撤销。撤销是软删留痕，不删行（HLT-04）",
    )
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "staff_accounts.staff_id",
            ondelete="SET NULL",
            name="fk_health_records_revoked_by",
        ),
        nullable=True,
        comment="执行撤销的员工 staff_id（医生/管理员）；NULL=未撤销或员工已注销",
    )
    collected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="采集提交时刻（UTC 存储）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class HealthCheckupSession(Base):
    """当日一场采集的「问到哪一题、等到什么时候、已经答了什么」（FR-HLT-01 / AC-11）。

    ## 为什么单独成表

    60 秒超时要求「下一轮扫描必须知道上一问还在等谁、等到几点」——这个事实既不属于
    采集结果（`health_records` 只在终态写一行），也不属于提醒通知（`notifications`
    是给人的收件箱）。塞进任一张表的 JSONB 都会让「按到期时间扫表」退化成全表过滤，
    并把两个互不相关的生命周期绑在一起（任务卡 §3 明确要求独立建表）。

    ## 为什么 `(elder_id, record_date)` 唯一

    `health_thread_id()` 也按天收敛（`health:{elder_id}:{day}`），一位长者一天只跑一场
    采集。这样「19:00 补跑」「超时扫描」「长者回答」三条路径争的是同一行，靠
    `claim` 的条件 UPDATE 天然互斥，不需要额外的分布式锁。

    ## `status` 四态

    | status | 含义 | 谁写的 |
    | --- | --- | --- |
    | `asking` | 有一题挂起在等回答（`expires_at` 就是这一题的截止） | 起问 / 回答 / 超时推进 |
    | `awaiting_review` | 五问已答完且判黄/红，等人工复核裁决 | 图跑到 `review` 的 `interrupt` |
    | `completed` | 采集已落库（`health_records` 已有本场版本） | 终态落库后 |
    | `ended` | 长者主动结束；**已答项照样落库** | 长者说「不测了」 |

    前两态是「还活着」的会话（`expires_at` / 复核裁决还在等）；后两态是终态，
    扫描与回答入口都只认 `asking`，所以终态行不会被二次推进。
    """

    __tablename__ = "health_checkup_sessions"
    __table_args__ = (
        UniqueConstraint("elder_id", "record_date", name="uq_health_checkup_day"),
        CheckConstraint(
            "status IN ('asking', 'awaiting_review', 'completed', 'ended')",
            name="status",
        ),
        CheckConstraint("source IN ('voice_scheduled', 'voice_active')", name="source"),
        CheckConstraint("attempt_no >= 1", name="attempt_positive"),
        CheckConstraint("timeouts >= 0", name="timeouts_non_negative"),
        # 超时扫描的查询形状：status='asking' AND expires_at <= now()，按到期时间排序。
        Index("ix_health_checkup_due", "status", "expires_at"),
        {"comment": "健康采集会话（每题 60 秒超时、断点续问）"},
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="会话编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "elders.elder_id", ondelete="CASCADE", name="fk_health_checkup_sessions_elder_id"
        ),
        nullable=False,
        comment="长者 ID",
    )
    record_date: Mapped[_DATE] = mapped_column(
        Date(), nullable=False, comment="会话归属日期（北京时间自然日，与 health_records 同口径）"
    )
    thread_id: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="LangGraph 线程键（health:{elder_id}:{record_date}），断线续问与复核 resume 都靠它",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'asking'"),
        comment="asking / awaiting_review / completed / ended",
    )
    source: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'voice_scheduled'"),
        comment="voice_scheduled=19:00 定时采集，voice_active=长者主动发起（PRD §4.6 正文口径）",
    )
    current_key: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="当前挂起的问题字段（systolic/diastolic/...）；终态为 NULL"
    )
    prompt: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(),
        nullable=True,
        comment="当前问题的 interrupt 载荷（问句 + 第几次 + 上限），pad 拉取后直接播报",
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="本题的回答截止（起问时刻 + 60 秒）；NULL=不等任何人（复核中或终态）",
    )
    attempt_no: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("1"),
        comment="当前这道题问到了第几次（1=首问，2=重问）；超过 MAX_ATTEMPTS 由图上直接标缺失",
    )
    timeouts: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("0"),
        comment="本场累计无答超时次数（排障与话术用，不参与分级）",
    )
    answers: Mapped[dict[str, float | int] | None] = mapped_column(
        JSONB(),
        nullable=True,
        comment="已解析出的五字段快照；长者主动结束时据此照样落库（不丢已采集数据）",
    )
    missing: Mapped[list[str] | None] = mapped_column(
        JSONB(),
        nullable=True,
        comment="已判定缺失的字段（两次无效/超时）；落库时按缺失判黄（PRD §4.6）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="会话进入终态的时刻（completed / ended）；NULL=仍在进行",
    )
