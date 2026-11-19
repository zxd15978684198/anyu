"""日报接口口径：只呈现最新版本 + 家属能读到 `family_report` 通知（FR-RPT-03 / AC-25）。

服务层的版本与推送逻辑在 `tests/test_report_service.py` / `tests/test_notification_service.py`
里测；这里只关心**家属真的看得到**这条链路的两端：

- 同一自然日补录出第二版后，`GET /api/reports/{id}` 与 `/list` 给的都是**新版**，
  列表里一天只出现一次（时间线不该被历史版本撑乱）；
- 生成日报建出来的 `family_report` 通知，绑定家属能在 `/api/notifications` 里读到，
  **未绑定**的家属读不到（收件人是按 `elder_guardians` 解析的，不是全局广播）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import notifications as notifications_api
from app.api import reports as reports_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    Elder,
    ElderGuardian,
    GuardianAccount,
    HealthRecord,
    StaffAccount,
)
from app.scheduler import health as health_jobs
from app.services import report_service

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
def isolated_session_factory(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """越权审计走独立会话；钉在测试连接上，免得把真实行提交进种子库。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)


@dataclass
class Seed:
    admin_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID
    guardian_id: uuid.UUID
    other_guardian_id: uuid.UUID
    day: datetime.date


async def _seed(db: AsyncSession) -> Seed:
    admin_id = uuid.uuid4()
    elder_id, other_elder_id = uuid.uuid4(), uuid.uuid4()
    guardian_id, other_guardian_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [{"staff_id": admin_id, "name": "管理员", "role": "admin", "password_hash": "x"}],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": elder_id, "name": "张桂芬", "status": "active"},
            {"elder_id": other_elder_id, "name": "李振华", "status": "active"},
        ],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [
            {
                "guardian_id": guardian_id,
                "name": "家属张",
                "password_hash": "x",
                "status": "active",
            },
            {
                "guardian_id": other_guardian_id,
                "name": "家属李",
                "password_hash": "x",
                "status": "active",
            },
        ],
    )
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
    # 医生指派：只是为了 admin/doctor 两条读路径的证据链完整，本文件用 admin 断言。
    await db.flush()
    return Seed(
        admin_id=admin_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
        guardian_id=guardian_id,
        other_guardian_id=other_guardian_id,
        day=TODAY,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


@dataclass
class Bundle:
    client: AsyncClient
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(reports_api.router)
    application.include_router(notifications_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db.bind,  # type: ignore[arg-type]
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    holder: list[Identity] = []

    def override_principal() -> Identity:
        return holder[0]

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_principal] = override_principal

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        yield Bundle(client=ac, holder=holder)


def _as_admin(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.admin_id), role="admin")


def _as_guardian(seed: Seed, *, other: bool = False) -> Identity:
    guardian_id = seed.other_guardian_id if other else seed.guardian_id
    return Identity(kind="guardian", guardian_id=str(guardian_id))


async def _add_backfill_record(db: AsyncSession, seed: Seed) -> None:
    """补录一条健康记录：聚合结果变化 → `generate()` 追加第二版。"""
    await db.execute(
        HealthRecord.__table__.insert(),
        [
            {
                "record_id": uuid.uuid4(),
                "elder_id": seed.elder_id,
                "record_date": seed.day,
                "source": "staff_active",
                "grade": "green",
                "systolic_mmhg": 126,
                "diastolic_mmhg": 76,
                "heart_rate_bpm": 70,
                "sleep_hours": 7.5,
                "mood_score": 4,
                "revision": 2,
                "collected_at": datetime.datetime(
                    seed.day.year, seed.day.month, seed.day.day, 2, tzinfo=datetime.UTC
                ),
            }
        ],
    )
    await db.flush()


# ------------------------------------------------------------------ 版本可见性


async def test_api_returns_latest_version_after_backfill(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: None,
) -> None:
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await _add_backfill_record(db, seeded)
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    client.holder.append(_as_admin(seeded))
    response = await client.client.get(f"/api/reports/{seeded.elder_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    # 首版是「当天没有采集」的空块（total=0），第二版才有一条记录；拿到 1 就是新版。
    assert body["payload"]["health"]["total"] == 1
    assert body["report_date"] == seeded.day.isoformat()
    # payload 键集是家属端契约，不许因为版本逻辑加字段。
    assert set(body["payload"]) == {"elder_id", "report_date", "health", "medication", "sources"}


async def test_list_collapses_versions_to_one_row_per_day(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: None,
) -> None:
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await _add_backfill_record(db, seeded)
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    client.holder.append(_as_admin(seeded))
    response = await client.client.get(f"/api/reports/{seeded.elder_id}/list")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1, "同一天的两个版本在列表里只出现一次"
    assert len(body["items"]) == 1
    assert body["items"][0]["payload"]["health"]["total"] == 1, "列表给的是最新版快照"


# ------------------------------------------------------------ 家属读到 family_report


async def test_bound_guardian_reads_family_report_notification(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: None,
) -> None:
    report = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    client.holder.append(_as_guardian(seeded))
    response = await client.client.get("/api/notifications")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "family_report"
    assert item["elder_id"] == str(seeded.elder_id)
    assert item["payload"]["report_id"] == str(report.report_id)
    assert item["payload"]["version"] == 1


async def test_guardian_of_another_elder_does_not_see_it(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: None,
) -> None:
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    client.holder.append(_as_guardian(seeded, other=True))
    response = await client.client.get("/api/notifications")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [], "收件人按绑定关系解析，不是全局广播"
