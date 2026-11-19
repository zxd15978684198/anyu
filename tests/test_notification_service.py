"""`family_report` 推送链：通知建立、1/5/15 重试、三枪失败落 `failed`（FR-RPT-03 / AC-25）。

三条验收口径对应三组用例：

- **建得对**：一条通知代表「一份日报的一个版本」，payload 里带 `report_id` / `version` /
  `report_date`，收件人按 `elder_guardians.status='active'` 解析；没有家属就不建。
- **只发一次**：同一 `(elder_id, report_id, version)` 重跑命中已有行，不产生第二条。
- **失败如实落账**：三次推送都不成功 → `status='failed'` + 一条 `audit_logs`，
  **绝不**写 `delivered`；推进用假 `now`，不 sleep、不真起调度器。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import AuditLog, Elder, ElderGuardian, GuardianAccount, Notification
from app.services import notification_service

#: 固定时间零点：所有重试推进都相对它算，用例之间互不影响。
ANCHOR = datetime.datetime(2026, 11, 12, 7, 0, tzinfo=datetime.UTC)


# --------------------------------------------------------------------- fixtures


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await trans.rollback()
        await session.close()
        await conn.close()


@dataclass
class Seed:
    elder_id: uuid.UUID
    guardian_id: uuid.UUID


async def _seed(db: AsyncSession, *, bind_guardian: bool = True) -> Seed:
    elder_id, guardian_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": "张桂芬", "status": "active"}],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [
            {
                "guardian_id": guardian_id,
                "name": "家属张",
                "password_hash": "x",
                "status": "active",
            }
        ],
    )
    if bind_guardian:
        await db.execute(
            ElderGuardian.__table__.insert(),
            [
                {
                    "elder_id": elder_id,
                    "guardian_id": guardian_id,
                    "relation": "daughter",
                    "status": "active",
                }
            ],
        )
    await db.flush()
    return Seed(elder_id=elder_id, guardian_id=guardian_id)


async def _send_fails(_notification: Notification) -> None:
    """注入的失败通道：模拟外部推送不可用。"""
    raise RuntimeError("channel down")


def _anchor_of(notification: Notification) -> datetime.datetime:
    raw = (notification.payload or {})["delivery"]["anchor_at"]
    return datetime.datetime.fromisoformat(raw)


def _next_retry_of(notification: Notification) -> datetime.datetime | None:
    raw = (notification.payload or {})["delivery"]["next_retry_at"]
    return datetime.datetime.fromisoformat(raw) if raw else None


async def _refresh(db: AsyncSession, notification: Notification) -> Notification:
    await db.refresh(notification)
    return notification


async def _audits(db: AsyncSession) -> list[AuditLog]:
    return list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == notification_service.DAILY_REPORT_FAILED_ACTION
                )
            )
        )
        .scalars()
        .all()
    )


# ----------------------------------------------------------- 建立：payload 与幂等


async def test_notify_daily_report_builds_one_notification_per_version(
    db: AsyncSession,
) -> None:
    seed = await _seed(db)
    report_id = uuid.uuid4()

    first = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=report_id,
        report_date=ANCHOR.date(),
        version=1,
        now=ANCHOR,
    )
    assert first is not None
    second = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=report_id,
        report_date=ANCHOR.date(),
        version=1,
        now=ANCHOR,
    )
    assert second is not None
    assert second.notification_id == first.notification_id, "同一版重跑必须命中已有行"

    payload = first.payload or {}
    assert payload["report_id"] == str(report_id)
    assert payload["version"] == 1
    assert payload["report_date"] == ANCHOR.date().isoformat()
    assert payload["recipients"] == [str(seed.guardian_id)]
    assert payload["delivery"]["attempts"] == 0
    assert _next_retry_of(first) == ANCHOR + datetime.timedelta(minutes=1)

    rows = (
        (await db.execute(select(Notification).where(Notification.kind == "family_report")))
        .scalars()
        .all()
    )
    assert len(rows) == 1, "幂等键 (elder_id, report_id, version) 只允许一条"


async def test_new_version_gets_its_own_notification_marked_updated(
    db: AsyncSession,
) -> None:
    seed = await _seed(db)
    report_id = uuid.uuid4()
    await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=report_id,
        report_date=ANCHOR.date(),
        version=1,
        now=ANCHOR,
    )
    updated = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=uuid.uuid4(),
        report_date=ANCHOR.date(),
        version=2,
        previous_version=1,
        now=ANCHOR,
    )
    assert updated is not None
    payload = updated.payload or {}
    assert payload["updated"] is True
    assert payload["previous_version"] == 1
    assert payload["title"] == "健康日报已更新"


async def test_no_active_guardian_creates_nothing(db: AsyncSession) -> None:
    seed = await _seed(db, bind_guardian=False)
    created = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=uuid.uuid4(),
        report_date=ANCHOR.date(),
        version=1,
        now=ANCHOR,
    )
    assert created is None, "没有收件人就不建通知（建了也没人读得到）"
    rows = (
        (await db.execute(select(Notification).where(Notification.kind == "family_report")))
        .scalars()
        .all()
    )
    assert rows == []


# --------------------------------------------------------- 推送：成功与 1/5/15 重试


async def test_successful_push_marks_delivered(db: AsyncSession) -> None:
    seed = await _seed(db)
    notification = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=uuid.uuid4(),
        report_date=ANCHOR.date(),
        version=1,
        now=ANCHOR,
    )
    assert notification is not None

    # 第一枪排在 1 分钟后：提前扫描必须什么都不做。
    assert await notification_service.deliver_due_daily_reports(db, now=ANCHOR) == 0

    processed = await notification_service.deliver_due_daily_reports(
        db, now=ANCHOR + datetime.timedelta(minutes=1)
    )
    assert processed == 1
    await _refresh(db, notification)
    assert notification.status == "delivered"
    assert notification.delivered_at is not None
    assert _next_retry_of(notification) is None, "送达后不得再排下一枪"
    assert (notification.payload or {})["delivery"]["attempts"] == 1

    # 已送达不在扫描面里（终态不再推）。
    assert (
        await notification_service.deliver_due_daily_reports(
            db, now=ANCHOR + datetime.timedelta(minutes=15)
        )
        == 0
    )


async def test_three_failures_follow_one_five_fifteen_then_land_failed(
    db: AsyncSession,
) -> None:
    seed = await _seed(db)
    notification = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=uuid.uuid4(),
        report_date=ANCHOR.date(),
        version=1,
        now=ANCHOR,
    )
    assert notification is not None
    anchor = _anchor_of(notification)

    # 第 1 枪：+1 分钟失败 → pending，下一枪 +5。
    assert (
        await notification_service.deliver_due_daily_reports(
            db, now=anchor + datetime.timedelta(minutes=1), sender=_send_fails
        )
        == 1
    )
    await _refresh(db, notification)
    assert notification.status == "pending"
    assert notification.delivered_at is None
    assert (notification.payload or {})["delivery"]["attempts"] == 1
    assert (notification.payload or {})["delivery"]["last_error"] == "RuntimeError"
    assert _next_retry_of(notification) == anchor + datetime.timedelta(minutes=5)

    # 没到点：不许抢跑（同一分钟再扫一次是 0，不是重试）。
    assert (
        await notification_service.deliver_due_daily_reports(
            db, now=anchor + datetime.timedelta(minutes=2), sender=_send_fails
        )
        == 0
    )

    # 第 2 枪：+5 分钟失败 → 下一枪 +15。
    assert (
        await notification_service.deliver_due_daily_reports(
            db, now=anchor + datetime.timedelta(minutes=5), sender=_send_fails
        )
        == 1
    )
    await _refresh(db, notification)
    assert notification.status == "pending"
    assert (notification.payload or {})["delivery"]["attempts"] == 2
    assert _next_retry_of(notification) == anchor + datetime.timedelta(minutes=15)

    # 第 3 枪：+15 分钟仍失败 → failed（终态）+ 审计，绝不写 delivered。
    assert (
        await notification_service.deliver_due_daily_reports(
            db, now=anchor + datetime.timedelta(minutes=15), sender=_send_fails
        )
        == 1
    )
    await _refresh(db, notification)
    assert notification.status == "failed"
    assert notification.delivered_at is None, "没送出去就不能有 delivered_at"
    assert _next_retry_of(notification) is None
    assert (notification.payload or {})["delivery"]["attempts"] == 3

    audits = await _audits(db)
    assert len(audits) == 1, "三枪失败必须留一条审计，且只留一条"
    audit = audits[0]
    assert audit.actor_type == "system"
    assert audit.resource_type == "notifications"
    assert audit.resource_id == notification.notification_id
    detail = audit.detail or {}
    assert detail["attempts"] == 3
    assert detail["last_error"] == "RuntimeError"

    # 终态不再被扫到：第四次扫描什么都不做，也不会再补一条审计。
    assert (
        await notification_service.deliver_due_daily_reports(
            db, now=anchor + datetime.timedelta(minutes=60), sender=_send_fails
        )
        == 0
    )
    assert len(await _audits(db)) == 1


async def test_second_push_succeeds_after_first_failure(db: AsyncSession) -> None:
    """第一次通道抖动、第二次好了：链继续走完并落 delivered，不留审计。"""
    seed = await _seed(db)
    notification = await notification_service.notify_daily_report(
        db,
        elder_id=seed.elder_id,
        report_id=uuid.uuid4(),
        report_date=ANCHOR.date(),
        version=1,
        now=ANCHOR,
    )
    assert notification is not None
    anchor = _anchor_of(notification)

    await notification_service.deliver_due_daily_reports(
        db, now=anchor + datetime.timedelta(minutes=1), sender=_send_fails
    )
    await notification_service.deliver_due_daily_reports(
        db, now=anchor + datetime.timedelta(minutes=5)
    )
    await _refresh(db, notification)
    assert notification.status == "delivered"
    assert (notification.payload or {})["delivery"]["attempts"] == 2
    assert await _audits(db) == []
