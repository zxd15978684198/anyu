"""FR-MED-09：用药到期复核 + 一键续方（0020 批次）。

四件事各自可观测，所以分开验：

1. **7 天内到期**：`end_date ∈ [today+1, today+7]` 且 `review_notified_at IS NULL`
   → 一条黄色 `medication_review` 通知派给主治医生，回写该列；同一条计划**一生只提醒
   一次**（通知被 acknowledged 关掉也不重来）。
2. **已过期未续**：没有子计划指向它、且没有未关单 → 建红档；已有黄档未关 →
   原单升红；已经是红档 → 本日跳过（每天最多催一次的上界）。
3. **退房/归档长者不生成**（FR-ADM-09）：复核扫描与事件扫描同一口径，
   `away` / `archived` 长者的药单既不提醒也不生成事件。
4. **续方 = 新开一条**：`start_date = 旧 end_date + 1`、`end_date = start + 30`、
   `renewed_from_plan_id` 指回旧计划；旧计划一行不改；重复续方拒绝（409 语义）；
   长期医嘱与已停用计划拒绝（422 语义）。
"""

from __future__ import annotations

import datetime
import uuid
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import (
    Elder,
    ElderStaffAssignment,
    MedicationEvent,
    MedicationPlan,
    Notification,
    StaffAccount,
)
from app.scheduler import medication as medication_jobs
from app.services import medication_service

SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 夹具（与 test_scheduler_medication.py 同构：外层事务 + NullPool，跑完零残留）
# ---------------------------------------------------------------------------


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
        await session.close()
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def isolated_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """调度任务自己会 commit：`create_savepoint` 让 commit 只释放 savepoint。"""
    factory = async_sessionmaker(
        bind=db.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(medication_jobs, "db_session_factory", factory)
    yield factory


# ---------------------------------------------------------------------------
# 种子数据
# ---------------------------------------------------------------------------


@dataclass
class Seed:
    elder_id: uuid.UUID
    doctor_id: uuid.UUID
    today: datetime.date


async def _seed(
    db: AsyncSession,
    *,
    elder_status: str = "active",
    plan_status: str = "active",
    schedule_cron: str = "08:00",
    end_offset: int | None = None,
) -> Seed:
    today = medication_service.today_in_shanghai()
    plan_end_date = None if end_offset is None else today + datetime.timedelta(days=end_offset)
    elder_id, doctor_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        {"staff_id": doctor_id, "name": "医生赵", "role": "doctor", "password_hash": "x"},
    )
    await db.execute(
        Elder.__table__.insert(),
        {"elder_id": elder_id, "name": "陈玉兰", "status": elder_status},
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        {"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"},
    )
    await db.execute(
        MedicationPlan.__table__.insert(),
        {
            "plan_id": uuid.uuid4(),
            "elder_id": elder_id,
            "drug_name": "阿托伐他汀",
            "dose": "1 片",
            "schedule_cron": schedule_cron,
            "start_date": today - datetime.timedelta(days=30),
            "end_date": plan_end_date,
            "version": 1,
            "created_by": doctor_id,
            "status": plan_status,
        },
    )
    return Seed(elder_id=elder_id, doctor_id=doctor_id, today=today)


async def _load_plan(db: AsyncSession, elder_id: uuid.UUID) -> MedicationPlan:
    """该长者**原始**药单（续方开出来的子计划另算，用 `renewed_from_plan_id IS NULL` 区分）。"""
    db.expire_all()
    return (
        await db.execute(
            select(MedicationPlan).where(
                MedicationPlan.elder_id == elder_id,
                MedicationPlan.renewed_from_plan_id.is_(None),
            )
        )
    ).scalar_one()


async def _reviews(db: AsyncSession, elder_id: uuid.UUID) -> list[Notification]:
    """复核通知（不 `expire_all()`：那会把调用方持有的计划对象一起作废，异步下再取属性会炸）。"""
    rows = (
        await db.execute(
            select(Notification)
            .where(
                Notification.elder_id == elder_id,
                Notification.kind == medication_service.MEDICATION_REVIEW_KIND,
            )
            .order_by(Notification.created_at)
        )
    ).scalars()
    return list(rows)


# ---------------------------------------------------------------------------
# 1. 7 天内到期：建一条黄色通知 + 回写闸门，且只提醒一次
# ---------------------------------------------------------------------------


async def test_due_plan_notifies_once_and_stamps_review_gate(db: AsyncSession) -> None:
    seed = await _seed(db)
    plan = await _load_plan(db, seed.elder_id)
    plan.end_date = seed.today + datetime.timedelta(days=3)
    await db.flush()

    outcome = await medication_service.notify_plan_reviews(db, today=seed.today)
    # 扫描是全库动作，计数可能被数据库里其它用例残留的药单抬高：断言本长者的那条。
    assert outcome.due >= 1 and outcome.created >= 1, "7 天内到期应建一条复核通知"

    reviews = await _reviews(db, seed.elder_id)
    assert len(reviews) == 1
    review = reviews[0]
    assert review.severity == "yellow"
    assert review.assignee_staff_id == seed.doctor_id, "复核通知必须派给主治医生"
    assert review.payload is not None
    assert review.payload["plan_id"] == str(plan.plan_id)
    assert review.payload["record_date"] == seed.today.isoformat()
    assert review.payload["days_to_expiry"] == 3
    assert review.payload["expired"] is False

    refreshed = await _load_plan(db, seed.elder_id)
    assert refreshed.review_notified_at is not None, "建单后必须回写复核闸门"

    # 同一天再扫：列已非空，不再提醒（通知被关掉也不会重来）。
    await medication_service.notify_plan_reviews(db, today=seed.today)
    assert len(await _reviews(db, seed.elder_id)) == 1


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(0, 0), (1, 1), (7, 1), (8, 0)],
    ids=["today", "today+1", "today+7", "today+8"],
)
async def test_due_window_boundaries(db: AsyncSession, offset: int, expected: int) -> None:
    """today+1 与 today+7 在内；today（当天到期）与 today+8 不在。

    每个边界各起一条独立药单：复核闸门是「一生只提醒一次」，同一条药单反复重置闸门
    再扫并不代表真实场景，会把断言变成在数历史通知。
    """
    seed = await _seed(db, end_offset=offset)
    await medication_service.notify_plan_reviews(db, today=seed.today)
    reviews = await _reviews(db, seed.elder_id)
    assert len(reviews) == expected, f"end_date=today+{offset} 应建 {expected} 条复核通知"


# ---------------------------------------------------------------------------
# 2. 已过期未续：黄档升红、红档不重复催
# ---------------------------------------------------------------------------


async def test_expired_plan_escalates_open_review_to_red_once(db: AsyncSession) -> None:
    seed = await _seed(db)
    plan = await _load_plan(db, seed.elder_id)
    plan.end_date = seed.today + datetime.timedelta(days=2)
    await db.flush()

    first = await medication_service.notify_plan_reviews(db, today=seed.today)
    assert first.due >= 1
    [review] = await _reviews(db, seed.elder_id)
    assert review.severity == "yellow"

    # 药单过期（未续方）：同一张未关单升红，而不是再建一条。
    plan.end_date = seed.today - datetime.timedelta(days=1)
    await db.flush()
    await medication_service.notify_plan_reviews(db, today=seed.today)

    reviews = await _reviews(db, seed.elder_id)
    assert len(reviews) == 1, "过期催办复用未关单，不得叠卡片"
    escalated = reviews[0]
    assert escalated.severity == "red"
    assert escalated.payload is not None
    assert escalated.payload["escalated_from"] == "yellow"

    # 已经是红档：本日再扫不再改动、不再建单（每天最多催一次）。
    await medication_service.notify_plan_reviews(db, today=seed.today)
    reviews = await _reviews(db, seed.elder_id)
    assert len(reviews) == 1
    assert reviews[0].severity == "red"


async def test_expired_plan_without_open_review_creates_red(db: AsyncSession) -> None:
    seed = await _seed(db)
    plan = await _load_plan(db, seed.elder_id)
    plan.end_date = seed.today - datetime.timedelta(days=5)
    await db.flush()

    outcome = await medication_service.notify_plan_reviews(db, today=seed.today)
    assert outcome.expired >= 1
    [review] = await _reviews(db, seed.elder_id)
    assert review.severity == "red"
    assert review.payload["expired"] is True
    assert review.payload["end_date"] == plan.end_date.isoformat()


async def test_reviewed_plan_that_was_renewed_is_not_pestered(db: AsyncSession) -> None:
    """续过方的旧计划不再催办：处理过的事实不该继续报警。"""
    seed = await _seed(db)
    plan = await _load_plan(db, seed.elder_id)
    plan.end_date = seed.today - datetime.timedelta(days=2)
    await db.flush()
    await medication_service.renew_plan(db, plan=plan, renewed_by=seed.doctor_id)

    await medication_service.notify_plan_reviews(db, today=seed.today)
    assert await _reviews(db, seed.elder_id) == [], "续过方的旧计划不该再被催"


# ---------------------------------------------------------------------------
# 3. 退房/归档长者：复核与事件都不生成（FR-ADM-09）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("elder_status", ["away", "archived"])
async def test_review_scan_skips_departed_elder(db: AsyncSession, elder_status: str) -> None:
    seed = await _seed(db, elder_status=elder_status, end_offset=3)
    await medication_service.notify_plan_reviews(db, today=seed.today)
    assert await _reviews(db, seed.elder_id) == [], "退房/归档长者的药单不该进复核"


async def test_event_scan_skips_departed_elder(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _seed(db, elder_status="away")
    now = datetime.datetime.now(datetime.UTC)
    await medication_jobs.run_medication_scan(now=now, session_factory=isolated_factory)
    db.expire_all()
    events = (
        await db.execute(select(MedicationEvent).where(MedicationEvent.elder_id == seed.elder_id))
    ).scalars()
    assert list(events) == [], "已退房长者不该再生成用药事件"


async def test_review_scan_job_writes_notification(
    db: AsyncSession, isolated_factory: async_sessionmaker[AsyncSession]
) -> None:
    """08:00 那个 job 本体：切业务日、开事务、commit、返回新建条数。"""
    seed = await _seed(db)
    plan = await _load_plan(db, seed.elder_id)
    plan.end_date = seed.today + datetime.timedelta(days=4)
    await db.flush()

    created = await medication_jobs.run_medication_review_scan(
        now=datetime.datetime.now(datetime.UTC), session_factory=isolated_factory
    )
    assert created >= 1
    [review] = await _reviews(db, seed.elder_id)
    assert review.kind == "medication_review"
    assert review.severity == "yellow"


# ---------------------------------------------------------------------------
# 4. 续方：日期窗口、续方链、幂等
# ---------------------------------------------------------------------------


async def test_renew_plan_creates_next_window_and_links_back(db: AsyncSession) -> None:
    seed = await _seed(db)
    old = await _load_plan(db, seed.elder_id)
    old.end_date = seed.today + datetime.timedelta(days=6)
    await db.flush()
    old_end = old.end_date

    renewed = await medication_service.renew_plan(db, plan=old, renewed_by=seed.doctor_id)

    assert renewed.plan_id != old.plan_id
    assert renewed.start_date == old_end + datetime.timedelta(days=1)
    assert renewed.end_date == renewed.start_date + datetime.timedelta(days=30)
    assert renewed.renewed_from_plan_id == old.plan_id
    assert (renewed.drug_name, renewed.dose, renewed.schedule_cron) == (
        old.drug_name,
        old.dose,
        old.schedule_cron,
    )
    assert renewed.status == "active"
    assert renewed.version == 1, "续方是新医嘱，不是同一药单的修订版"
    assert renewed.created_by == seed.doctor_id

    # 旧计划一行不改：保持 active，按自己的 end_date 自然到期。
    unchanged = await _load_plan(db, seed.elder_id)
    assert unchanged.plan_id == old.plan_id
    assert unchanged.status == "active"
    assert unchanged.end_date == old_end


async def test_renew_plan_is_idempotent_per_source_plan(db: AsyncSession) -> None:
    seed = await _seed(db)
    old = await _load_plan(db, seed.elder_id)
    old.end_date = seed.today + datetime.timedelta(days=1)
    await db.flush()
    await medication_service.renew_plan(db, plan=old, renewed_by=seed.doctor_id)

    with pytest.raises(medication_service.MedicationPlanAlreadyRenewed):
        await medication_service.renew_plan(db, plan=old, renewed_by=seed.doctor_id)

    db.expire_all()
    count = await db.scalar(
        select(func.count())
        .select_from(MedicationPlan)
        .where(MedicationPlan.elder_id == seed.elder_id)
    )
    assert count == 2, "重复续方不得开出第二条计划"


async def test_renew_plan_rejects_long_term_and_stopped(db: AsyncSession) -> None:
    long_term = await _seed(db)
    with pytest.raises(medication_service.MedicationValidationError):
        await medication_service.renew_plan(
            db, plan=await _load_plan(db, long_term.elder_id), renewed_by=long_term.doctor_id
        )

    stopped = await _seed(
        db,
        end_offset=3,
        plan_status="stopped",
    )
    with pytest.raises(medication_service.MedicationValidationError):
        await medication_service.renew_plan(
            db, plan=await _load_plan(db, stopped.elder_id), renewed_by=stopped.doctor_id
        )
