"""日报版本号 + `family_report` 推送（FR-RPT-03 / AC-25）。

这一组用例守住三条验收口径，逐条对应：

- **保留历史版本**（AC-25）：有效补录要追加 `version`，旧版行仍在表里；
- **只发一次**：同一版重复生成不产生第二条通知，补录的新版各发一条且标题是「已更新」；
- **不伪造送达**：TTS 失败（`audio_url=NULL`）不影响通知建立；通知的失败重试链在
  `tests/test_notification_service.py`，这里只管「生成即建通知」。

`_write_audit` / `_ensure_report_audio` 这类自己开会话或碰磁盘的路径，用例里要么用
savepoint 工厂钉住，要么 monkeypatch 掉——**不动真文件、不写种子库**。
"""

from __future__ import annotations

import datetime
import uuid
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
from app.core.runtime_context import Identity
from app.models import (
    DailyReport,
    Elder,
    ElderGuardian,
    GuardianAccount,
    HealthRecord,
    Notification,
)
from app.scheduler import health as health_jobs
from app.services import report_service

#: 业务自然日（与调度任务同一口径，别在测试里二次算时区）。
TODAY = health_jobs._today()


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
    """外层事务：所有写入最后整体回滚。"""
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


@pytest.fixture
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把 `report_service` 自己开会话的路径（越权审计）钉在同一条测试连接上。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    return factory


@dataclass
class Seed:
    elder_id: uuid.UUID
    guardian_id: uuid.UUID
    day: datetime.date


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
    return Seed(elder_id=elder_id, guardian_id=guardian_id, day=TODAY)


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


async def _add_health_record(
    db: AsyncSession, seed: Seed, *, revision: int, grade: str = "green"
) -> None:
    """补录一条健康记录（聚合结果随之变化，用来触发版本号 +1）。"""
    await db.execute(
        HealthRecord.__table__.insert(),
        [
            {
                "record_id": uuid.uuid4(),
                "elder_id": seed.elder_id,
                "record_date": seed.day,
                "source": "staff_active",
                "grade": grade,
                "systolic_mmhg": 128,
                "diastolic_mmhg": 78,
                "heart_rate_bpm": 72,
                "sleep_hours": 7.0,
                "mood_score": 4,
                "revision": revision,
                "collected_at": datetime.datetime(
                    seed.day.year, seed.day.month, seed.day.day, 2, tzinfo=datetime.UTC
                ),
            }
        ],
    )
    await db.flush()


async def _report_notifications(db: AsyncSession) -> list[Notification]:
    return list(
        (
            await db.execute(
                select(Notification)
                .where(Notification.kind == "family_report")
                .order_by(Notification.created_at)
            )
        )
        .scalars()
        .all()
    )


def _identity(seed: Seed) -> Identity:
    """admin 主体：读日报永远在范围内，用例只关心版本与推送。"""
    return Identity(kind="staff", staff_id=str(uuid.uuid4()), role="admin")


# --------------------------------------------------- 首版：生成即建 family_report 通知


async def test_first_generation_creates_family_report_notification(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    report = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    assert report.version == 1
    notifications = await _report_notifications(db)
    assert len(notifications) == 1, "首版日报必须恰好建一条 family_report 通知"
    notification = notifications[0]
    assert notification.kind == "family_report"
    assert notification.severity == "green", "日报是日常提醒，不该占红色告警池"
    assert notification.status == "generated"
    assert notification.elder_id == seeded.elder_id
    payload = notification.payload or {}
    # payload 是推送展示与幂等的契约：键集逐个钉住，改这里就是改契约。
    assert payload["report_id"] == str(report.report_id)
    assert payload["version"] == 1
    assert payload["report_date"] == seeded.day.isoformat()
    assert payload["elder_id"] == str(seeded.elder_id)
    assert payload["updated"] is False
    assert payload["previous_version"] is None
    assert payload["title"] == "今日健康日报已生成"
    assert payload["recipients"] == [str(seeded.guardian_id)]
    delivery = payload["delivery"]
    assert delivery["attempts"] == 0
    assert delivery["last_error"] is None
    # 第一枪排在创建后 1 分钟（PRD FR-RPT-03 的 1/5/15）。
    anchor = datetime.datetime.fromisoformat(delivery["anchor_at"])
    first_due = datetime.datetime.fromisoformat(delivery["next_retry_at"])
    assert first_due - anchor == datetime.timedelta(minutes=1)
    # anchor 由 `datetime.now(UTC)` 取，created_at 由数据库 `now()` 取：两者只承诺同量级。
    assert abs(anchor - notification.created_at) < datetime.timedelta(seconds=5)


async def test_regenerate_without_changes_reuses_one_manifest(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await report_service.generate(db, seeded.elder_id, seeded.day)
    second = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    assert first.report_id == second.report_id, "内容没变必须返回同一行"
    assert second.version == 1
    rows = await db.scalar(
        select(func.count()).select_from(DailyReport).where(DailyReport.elder_id == seeded.elder_id)
    )
    assert rows == 1
    assert len(await _report_notifications(db)) == 1, "同一版重跑不得重复建通知"


# ------------------------------------------------------------ 补录：追加版本 + 已更新


async def test_backfill_appends_version_and_keeps_history(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await report_service.generate(db, seeded.elder_id, seeded.day)
    await _add_health_record(db, seeded, revision=2)

    second = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    assert second.report_id != first.report_id
    assert second.version == 2, "有效补录必须追加一版"
    versions = list(
        (
            await db.execute(
                select(DailyReport.version).where(
                    DailyReport.elder_id == seeded.elder_id,
                    DailyReport.report_date == seeded.day,
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(versions) == [1, 2], "旧版本行必须留在表里（AC-25 保留历史版本）"

    notifications = await _report_notifications(db)
    assert len(notifications) == 2, "新版各发一次通知，旧版的那条不动"
    latest_payload = notifications[-1].payload or {}
    assert latest_payload["report_id"] == str(second.report_id)
    assert latest_payload["version"] == 2
    assert latest_payload["updated"] is True
    assert latest_payload["previous_version"] == 1
    assert latest_payload["title"] == "健康日报已更新"


async def test_regenerate_after_backfill_is_idempotent(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await _add_health_record(db, seeded, revision=2)
    await report_service.generate(db, seeded.elder_id, seeded.day)
    again = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    assert again.version == 2, "没有新数据时不得继续加版"
    rows = await db.scalar(
        select(func.count()).select_from(DailyReport).where(DailyReport.elder_id == seeded.elder_id)
    )
    assert rows == 2
    assert len(await _report_notifications(db)) == 2


# ------------------------------------------------------------------ 收件人解析


async def test_guardian_without_active_binding_gets_no_notification(
    db: AsyncSession, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _seed(db, bind_guardian=False)

    report = await report_service.generate(db, seed.elder_id, seed.day)
    await db.flush()

    assert report.version == 1, "没有收件人不影响日报本身生成"
    assert await _report_notifications(db) == [], "没有 active 家属就不建通知（没人能读）"


# --------------------------------------------------- 读路径：只呈现最新版本


async def test_get_report_returns_latest_version(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await report_service.generate(db, seeded.elder_id, seeded.day)
    await _add_health_record(db, seeded, revision=2)
    second = await report_service.generate(db, seeded.elder_id, seeded.day)

    view = await report_service.get_report(db, _identity(seeded), seeded.elder_id, seeded.day)
    assert view.report_id == second.report_id != first.report_id
    assert view.payload["health"]["total"] == second.payload["health"]["total"]


async def test_list_reports_shows_one_row_per_day(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await _add_health_record(db, seeded, revision=2)
    await report_service.generate(db, seeded.elder_id, seeded.day)

    views = await report_service.list_reports(db, _identity(seeded), seeded.elder_id)
    assert len(views) == 1, "同一天的多个版本在时间线上只出现一次"
    assert views[0].payload["health"]["total"] == 1, "列表给的是最新版的快照"


# -------------------------------------------------- 音频失败不连坐推送（FR-RPT-04）


async def test_audio_failure_does_not_block_family_report(
    db: AsyncSession,
    seeded: Seed,
    isolated_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTS 失败（`_generate_report_audio` 返回 None）时通知照建、日报照生成。"""

    async def _no_audio(report: DailyReport) -> str | None:
        return None

    monkeypatch.setattr(report_service, "_generate_report_audio", _no_audio)

    report = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    assert report.audio_url is None
    assert len(await _report_notifications(db)) == 1, "语音条是增强项，不得拦住家属推送"
