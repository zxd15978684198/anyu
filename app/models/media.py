"""娱乐素材库与点播历史 ORM（两张表）。

列定义对齐 `alembic/versions/0009_memory_medication.py` 与技术设计文档
§3.5 `media_library` / `media_history`；业务口径来自 PRD §4.10（戏曲/电影点播）。

设计要点：

- **素材红线（PRD FR-CON-03/05）**：`media_url` 只指向**样例授权或公有领域**素材，
  不抓在线资源、不做 AI 生成；`copyright_status` 非空是这条红线的落库凭证，
  服务层遇到无授权素材必须拒绝播放而不是「先放了再说」。
- `type` 只有两值 `opera` / `movie`：戏曲与电影的点播形态不同（戏曲按剧种/流派，
  电影按年代/题材），迭代 7 只做戏曲，电影留 P2（`type` 仍先支持两值以免后续改表）。
- `media_history` 一行 = 一次播放会话：`started_at` 必填、`finished_at` 可空
  （正在播/被打断）、`completion_rate` 是完播率 0..1。
- **打断即偏好信号**：用户说「换一段」时，未完成会话的 `completion_rate` 写 0.0
  （服务层 `switch_next` 负责），后续推荐按完播率降权——这是「不喜欢的节目别
  再推」的最朴素实现，不需要另建反馈表。
- 跨长者隔离同记忆：历史按 `elder_id` 查，「换一段」的排除集只从本人历史里取。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MediaLibraryItem(Base):
    """一条授权素材（戏曲选段或电影）；只读引用，不在服务里生成内容。"""

    __tablename__ = "media_library"
    __table_args__ = (
        # 同一份素材只应有一行：`media_url` 是素材的天然身份（清单以它为幂等键）。
        # 没有这条约束时，两个进程同时跑 `scripts/seed_media.py`（都是「先查没有，
        # 再插入」）会各插一行，长者端面板里同一段戏出现两次；约束让这种情况报错，
        # 而不是留下静默脏数据。加约束见迁移 `0014_media_url_unique`。
        UniqueConstraint("media_url", name="uq_media_library_media_url"),
        CheckConstraint("type IN ('opera', 'movie')", name="type"),
        CheckConstraint("duration IS NULL OR duration > 0", name="duration_positive"),
        {"comment": "娱乐素材库（仅样例授权或公有领域素材，PRD FR-CON-03/05）"},
    )

    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="素材编号",
    )
    type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="opera（戏曲）/ movie（电影，P2）"
    )
    title: Mapped[str] = mapped_column(Text(), nullable=False, comment="节目名（播报用）")
    genre: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="剧种/流派或电影题材（如 越剧 / 京剧 / 生活片）"
    )
    era: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="年代/流派标注（如 80 年代 / 传统折子戏）"
    )
    artist: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="表演者/主演（播报时可用）"
    )
    media_url: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="素材地址（站内静态资源或授权外链，不含签名密钥）"
    )
    duration: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="时长（秒）；未知为 NULL"
    )
    copyright_status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="授权状态（如 public_domain / licensed_sample）：非授权素材禁止播放",
    )


class MediaHistory(Base):
    """一次播放会话；一行 = 一次点播，用于「换一段」排除与偏好降权。"""

    __tablename__ = "media_history"
    __table_args__ = (
        CheckConstraint(
            "completion_rate IS NULL OR (completion_rate BETWEEN 0 AND 1)",
            name="completion_rate",
        ),
        Index("ix_media_history_elder_id_started_at", "elder_id", "started_at"),
        {"comment": "点播历史（播放会话与完播率）"},
    )

    history_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="播放会话编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者 ID（跨长者隔离）",
    )
    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_library.media_id", ondelete="CASCADE"),
        nullable=False,
        comment="点播的素材",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="开始播放时刻（UTC）",
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="结束时刻；正在播/被打断时回填为打断时刻",
    )
    completion_rate: Mapped[float | None] = mapped_column(
        Numeric(4, 3),
        nullable=True,
        comment="完播率 0..1；打断写 0.0（偏好降权信号），未知为 NULL",
    )
