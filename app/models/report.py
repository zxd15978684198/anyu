"""家属日报 ORM（健康统计 + 用药统计，幂等生成）。

对齐 `alembic/versions/0007_day8_health_daily_notifications.py` 与 PRD §4.8。

设计要点：
- `payload` 是 JSONB 而不是拆列：日报是**展示物**，健康块/用药块/来源标注的
  结构随版本演化（FR-RPT-01 要求四类用药状态区分），拆列会造成大改迁移。
- 用药块在 迭代 7 交付前固定输出
  `{"module": "medication", "state": "pending_day7", ...}` 占位（迭代 8 计划
  裁决 S-D8-3），**不得伪造用药统计**；迭代 7 的 `medication_events` 落地后已
  改由 `report_service` 输出四类终态计数，旧占位由
  `scripts/backfill_report_medication.py` 一次性回填。
- 幂等：UNIQUE (elder_id, report_date, version)，同一份内容重复生成返回已有行
  （report_service）；`version` 是同一天的**补录版本号**，有效补录追加一版、旧版本
  行留在表里（FR-RPT-03「保留历史版本」/ AC-25）。
- `audio_url` 为 FR-RPT-04（TTS 语音条）预留列，迭代 8 只留字段不做生成。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_DATE = datetime.date


class DailyReport(Base):
    """某长者某自然日的家属日报；同内容重复生成幂等返回，补录追加版本。"""

    __tablename__ = "daily_reports"
    __table_args__ = (
        # 唯一键从「一天一行」放宽成「一天一版」：FR-RPT-03 的补录版本更新要让新旧
        # 版本并存（AC-25「保留历史版本」），旧约束会直接挡住第二版 INSERT。
        UniqueConstraint("elder_id", "report_date", "version", name="uq_daily_report_day_version"),
        {"comment": "家属日报（健康统计 + 用药统计块，JSONB payload）"},
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="日报编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE", name="fk_daily_reports_elder_id"),
        nullable=False,
        comment="长者 ID",
    )
    report_date: Mapped[_DATE] = mapped_column(Date(), nullable=False, comment="日报归属日期")
    version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("1"),
        comment="同日版本号：首版为 1，有效补录一次加一；旧版本行不删（FR-RPT-03 / AC-25）",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, comment="health_block / medication_block / sources 结构化内容"
    )
    audio_url: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="TTS 语音摘要（FR-RPT-04，迭代 8 预留）"
    )
    generated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="生成时刻（UTC 存储）",
    )
