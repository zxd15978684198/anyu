"""迭代 8 日报（T4）：聚合口径、用药四类终态、幂等、家属隔离闸、漏采派单。

这几个性质都是验收条款，不是"顺手测一下"：

- **幂等**（FR-RPT-01）：同一 (elder_id, report_date) 重复生成必须返回同一行；
- **用药四类分列**（FR-RPT-01 + TD §3.4）：`medication` 块按 `terminal_state` 分列
  已服自报/拒服/已送达未确认/通知失败，`pending` 单独计，**不得合并**、不得编造；
- **隔离闸**（FR-RPT-02）：家属读**未绑定**长者的日报 403，且**必须**落 audit_logs
  （W3 起已绑定的家属可以读，那条口径在 `tests/test_family_reports.py`）；
- **漏采派单**（FR-HLT-05）：20:00 扫当日无记录的在住长者 → collection_missed 派负责护工。

## 测试怎么不污染库

- 业务读写都走 `db` fixture 的**外层事务**：`get_session` 被覆盖成 savepoint 模式，
  请求内 `commit()` 只释放保存点，teardown 整体 rollback。
- 调度任务与越权审计**自己开会话**（`session_factory`），若不放血会把真实行提交进
  种子库。所以 `isolated_session_factory` fixture 把这两处的工厂换成绑定到同一条
  测试连接的 savepoint 工厂——既测到真实 SQL，又不留痕。

不连库时 `require_db_connection` 会 **fail 而不是 skip**（P0-1 口径），
只有显式 `ANYU_ALLOW_DB_SKIP=1` 才降级为 skip。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import reports as reports_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    AuditLog,
    DailyReport,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    HealthRecord,
    MedicationEvent,
    MedicationPlan,
    Notification,
    StaffAccount,
)
from app.scheduler import health as health_jobs
from app.scheduler import reports as report_jobs
from app.services import report_service

#: 业务自然日（北京）。与调度任务同一口径，避免测试里再算一次时区。
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
    """外层事务：测试里所有写入最后整体回滚。"""
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
    """把"自己开会话"的组件（调度任务 / 越权审计）钉在同一条测试连接上。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    monkeypatch.setattr(health_jobs, "session_factory", factory)
    monkeypatch.setattr(report_jobs, "session_factory", factory)
    return factory


@dataclass
class Seed:
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    guardian_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID
    day: datetime.date


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, care_worker_id = (uuid.uuid4() for _ in range(3))
    guardian_id, elder_id, other_elder_id = (uuid.uuid4() for _ in range(3))
    day = TODAY

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": admin_id,
                "name": "管理员",
                "role": "admin",
                "password_hash": "x",
            },
            {
                "staff_id": doctor_id,
                "name": "医生李",
                "role": "doctor",
                "password_hash": "x",
            },
            {
                "staff_id": care_worker_id,
                "name": "护工王",
                "role": "care_worker",
                "password_hash": "x",
            },
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": elder_id, "name": "张桂芬", "status": "active"},
            {"elder_id": other_elder_id, "name": "李振华", "status": "active"},
        ],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"},
            {"elder_id": other_elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
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
            }
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

    # 当日两条生效记录（黄/红）+ 一条已被补录取代的旧版本。
    #
    # 逐条 insert 而不是一次塞列表：`insert().values([...])` 遇到**键不一致**的 dict
    # 会按 keyset 分组编译，漏掉的列直接取 NULL（实测 `assignee_staff_id` 就是这样丢的，
    # 且不报错）。单条 insert 没有这个坑。
    superseded_id, red_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        HealthRecord.__table__.insert(),
        [
            {
                "record_id": red_id,
                "elder_id": elder_id,
                "record_date": day,
                "source": "voice_active",
                "grade": "red",
                "systolic_mmhg": 185,
                "diastolic_mmhg": 96,
                "heart_rate_bpm": 88,
                "sleep_hours": 5.5,
                "mood_score": 2,
                "abnormal_reasons": ["收缩压 185 ≥ 180"],
                "revision": 3,
                "collected_at": datetime.datetime(
                    day.year, day.month, day.day, 1, tzinfo=datetime.UTC
                ),
            }
        ],
    )
    await db.execute(
        HealthRecord.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "record_date": day,
                "source": "staff_scheduled",
                "grade": "yellow",
                "systolic_mmhg": 142,
                "diastolic_mmhg": 88,
                "heart_rate_bpm": 76,
                "sleep_hours": 6.5,
                "mood_score": 3,
                "revision": 2,
                "collected_at": datetime.datetime(
                    day.year, day.month, day.day, 0, tzinfo=datetime.UTC
                ),
            }
        ],
    )
    await db.execute(
        HealthRecord.__table__.insert(),
        [
            {
                "record_id": superseded_id,
                "elder_id": elder_id,
                "record_date": day,
                "source": "voice_scheduled",
                "grade": "green",
                "revision": 1,
                "superseded_by": red_id,
                "collected_at": datetime.datetime(
                    day.year, day.month, day.day - 1, 23, tzinfo=datetime.UTC
                ),
            }
        ],
    )
    await db.flush()
    return Seed(
        admin_id=admin_id,
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        guardian_id=guardian_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
        day=day,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


@dataclass
class Bundle:
    client: AsyncClient
    #: 每次请求前 `append`/改第一项就能切换调用主体；覆盖依赖读的是同一对象。
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(reports_api.router)

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


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


# ------------------------------------------------------------------- 服务层口径


async def test_generate_is_idempotent_and_excludes_superseded(
    db: AsyncSession, seeded: Seed
) -> None:
    first = await report_service.generate(db, seeded.elder_id, seeded.day)
    second = await report_service.generate(db, seeded.elder_id, seeded.day)

    assert first.report_id == second.report_id, "同一 (elder, date) 必须返回同一行"
    count = await db.scalar(
        select(func.count()).select_from(DailyReport).where(DailyReport.elder_id == seeded.elder_id)
    )
    assert count == 1


# ------------------------------------------------------- 用药块（FR-RPT-01 / TD §3.4）

#: 日报的自然日口径 —— 与 `report_service._day_window` 同一份配置（不是硬编码上海）。
_REPORT_TZ = ZoneInfo(get_settings().scheduler_timezone)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    """报告时区某日某时刻 → UTC，模拟真实写进 `scheduled_at` 的值（timestamptz）。"""
    local = datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=_REPORT_TZ)
    return local.astimezone(datetime.UTC)


async def _seed_plan(db: AsyncSession, *, elder_id: uuid.UUID, doctor_id: uuid.UUID) -> uuid.UUID:
    """造一张 active 药单（日报用它区分「有药单但今天没到点」与「没有用药计划」）。"""
    plan_id = uuid.uuid4()
    await db.execute(
        MedicationPlan.__table__.insert(),
        [
            {
                "plan_id": plan_id,
                "elder_id": elder_id,
                "drug_name": "氨氯地平",
                "dose": "1 片",
                "schedule_cron": "08:00",
                "start_date": TODAY,
                "version": 1,
                "created_by": doctor_id,
                "status": "active",
            }
        ],
    )
    return plan_id


async def _seed_event(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID,
    elder_id: uuid.UUID,
    when: datetime.datetime,
    terminal_state: str | None,
    status: str,
) -> None:
    """一条用药事件；`terminal_state=None` = 到点流程还没结束（调用中/等确认）。

    `status` 只是满足 `ck_medication_events_status` 的取值，日报**只读**
    `terminal_state`——两列不是同一件事（TD §3.4）。
    """
    await db.execute(
        MedicationEvent.__table__.insert(),
        [
            {
                "plan_id": plan_id,
                "elder_id": elder_id,
                "scheduled_at": when,
                "drug_snapshot": {"drug_name": "氨氯地平", "dose": "1 片"},
                "payload_hash": uuid.uuid4().hex,
                "triggered_by": "scheduler",
                "status": status,
                "terminal_state": terminal_state,
            }
        ],
    )


async def test_medication_block_counts_four_terminal_states(db: AsyncSession, seeded: Seed) -> None:
    """四类终态分别计数、`pending` 单列，且绝不合并（FR-RPT-01 的核心验收）。"""
    plan_id = await _seed_plan(db, elder_id=seeded.elder_id, doctor_id=seeded.doctor_id)
    events = [
        ((7, 0), "taken_reported", "confirmed"),
        ((8, 0), "taken_reported", "confirmed"),
        ((12, 0), "refused", "confirmed"),
        ((18, 0), "missed_unconfirmed", "escalated"),
        ((20, 0), "notification_failed", "escalated"),
        ((21, 0), None, "waiting_confirm"),
    ]
    for (hour, minute), terminal_state, status in events:
        await _seed_event(
            db,
            plan_id=plan_id,
            elder_id=seeded.elder_id,
            when=_at(seeded.day, hour, minute),
            terminal_state=terminal_state,
            status=status,
        )

    payload = (await report_service.generate(db, seeded.elder_id, seeded.day)).payload
    medication = payload["medication"]

    assert medication["state"] == "collected"
    assert medication["counts"] == {
        "taken_reported": 2,
        "refused": 1,
        "missed_unconfirmed": 1,
        "notification_failed": 1,
        "pending": 1,
    }
    assert medication["total"] == 6, "分项之和必须等于总数"
    assert medication["plans_active"] == 1
    assert "summary" not in medication, "真实统计块不该再带那句占位说明"
    assert payload["sources"]["medication"] == "medication_events"


async def test_medication_block_uses_report_timezone_day_window(
    db: AsyncSession, seeded: Seed
) -> None:
    """按报告时区的自然日取窗口：北京 00:30 在 UTC 还是前一天，按日期硬比会漏。"""
    plan_id = await _seed_plan(db, elder_id=seeded.elder_id, doctor_id=seeded.doctor_id)
    prev_day = seeded.day - datetime.timedelta(days=1)
    next_day = seeded.day + datetime.timedelta(days=1)
    await _seed_event(
        db,
        plan_id=plan_id,
        elder_id=seeded.elder_id,
        when=_at(seeded.day, 0, 30),
        terminal_state="taken_reported",
        status="confirmed",
    )
    await _seed_event(
        db,
        plan_id=plan_id,
        elder_id=seeded.elder_id,
        when=_at(prev_day, 23, 50),
        terminal_state="missed_unconfirmed",
        status="escalated",
    )
    await _seed_event(
        db,
        plan_id=plan_id,
        elder_id=seeded.elder_id,
        when=_at(next_day, 0, 10),
        terminal_state="refused",
        status="confirmed",
    )

    counts = (await report_service.generate(db, seeded.elder_id, seeded.day)).payload["medication"][
        "counts"
    ]
    assert counts == {
        "taken_reported": 1,
        "refused": 0,
        "missed_unconfirmed": 0,
        "notification_failed": 0,
        "pending": 0,
    }


async def test_medication_block_does_not_leak_other_elder(db: AsyncSession, seeded: Seed) -> None:
    """别人的药单和事件不得进这位长者的日报（统计必须带 elder_id 过滤）。"""
    other_plan = await _seed_plan(db, elder_id=seeded.other_elder_id, doctor_id=seeded.doctor_id)
    await _seed_event(
        db,
        plan_id=other_plan,
        elder_id=seeded.other_elder_id,
        when=_at(seeded.day, 9),
        terminal_state="refused",
        status="confirmed",
    )

    medication = (await report_service.generate(db, seeded.elder_id, seeded.day)).payload[
        "medication"
    ]
    assert medication["total"] == 0
    assert medication["plans_active"] == 0
    assert medication["state"] == "not_configured", "没有药单也没有事件 → 无计划，不是 0 次"


async def test_payload_medication_block_without_plan_is_not_configured(
    db: AsyncSession, seeded: Seed
) -> None:
    report = await report_service.generate(db, seeded.elder_id, seeded.day)
    payload = report.payload

    # 没药单、没事件：逐字比对，保证没有"看起来像真数据"的编造（PRD 不伪造原则）。
    assert payload["medication"] == {
        "module": "medication",
        "state": "not_configured",
        "counts": {
            "taken_reported": 0,
            "refused": 0,
            "missed_unconfirmed": 0,
            "notification_failed": 0,
            "pending": 0,
        },
        "total": 0,
        "plans_active": 0,
    }
    health = payload["health"]
    assert health["state"] == "collected"
    # 三条生效记录（红/黄 + staff 录的一条），被取代的旧版本不计入。
    assert health["counts"] == {"green": 0, "yellow": 1, "red": 1, "missing": 0}
    assert health["total"] == 2
    assert health["latest"]["grade"] == "red"
    assert health["latest"]["systolic_mmhg"] == 185


async def test_backfill_rewrites_only_legacy_placeholder(db: AsyncSession, seeded: Seed) -> None:
    """历史回填：只补 `pending_day7` 行，健康块不动，重复执行不再改（幂等）。"""
    plan_id = await _seed_plan(db, elder_id=seeded.elder_id, doctor_id=seeded.doctor_id)
    await _seed_event(
        db,
        plan_id=plan_id,
        elder_id=seeded.elder_id,
        when=_at(seeded.day, 8),
        terminal_state="taken_reported",
        status="confirmed",
    )

    legacy_health = {
        "state": "missing",
        "counts": {"green": 0, "yellow": 0, "red": 0, "missing": 0},
        "total": 0,
        "sources": {},
        "latest": None,
    }
    report = DailyReport(
        elder_id=seeded.elder_id,
        report_date=seeded.day,
        payload={
            "elder_id": str(seeded.elder_id),
            "report_date": seeded.day.isoformat(),
            "health": legacy_health,
            "medication": {
                "module": "medication",
                "state": "pending_day7",
                "summary": "用药模块属 迭代 7 交付，暂未启用",
            },
            "sources": {
                "health": "health_records",
                "medication": "not_enabled_day7",
                "note": "用药块为占位，不伪造统计",
            },
        },
    )
    db.add(report)
    await db.flush()

    assert await report_service.refresh_medication_block(db, report) is True
    assert report.payload["medication"]["counts"]["taken_reported"] == 1
    assert report.payload["medication"]["state"] == "collected"
    assert report.payload["sources"]["medication"] == "medication_events"
    assert report.payload["health"] == legacy_health, "健康块一个字都不该动"

    # 第二遍：已是新口径 → 不动。`payload` 是生成时快照，不许跟着今天的事件滚动。
    assert await report_service.refresh_medication_block(db, report) is False


async def test_generate_without_records_reports_missing(db: AsyncSession, seeded: Seed) -> None:
    report = await report_service.generate(db, seeded.other_elder_id, seeded.day)
    health = report.payload["health"]
    assert health["state"] == "missing"
    assert health["total"] == 0
    assert health["counts"] == {"green": 0, "yellow": 0, "red": 0, "missing": 0}


# ------------------------------------------------------------------------ API


async def test_admin_reads_report(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_admin(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_id"] == str(seeded.elder_id)
    assert body["report_date"] == seeded.day.isoformat()
    assert body["payload"]["medication"]["state"] == "not_configured"
    assert body["payload"]["medication"]["counts"]["taken_reported"] == 0


async def test_doctor_in_scope_reads_and_out_of_scope_forbidden(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    client.holder.append(_as_doctor(seeded))
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    ok = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert ok.status_code == 200, ok.text

    # 不在自己负责范围（other_elder 的负责医生是护工那条 primary_care，不是他）。
    denied = await client.client.get(
        f"/api/reports/{seeded.other_elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "SCOPE_FORBIDDEN"


async def test_care_worker_cannot_read_reports(seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_care_worker(seeded))
    resp = await client.client.get(f"/api/reports/{seeded.other_elder_id}")
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_guardian_unbound_elder_forbidden_and_audited(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """未绑定的长者仍是 403 + 审计（W3 只放行绑定内，见 test_family_reports.py）。"""
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.other_elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "SCOPE_FORBIDDEN"
    assert len(body["trace_id"]) == 32

    audit = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.actor_type == "guardian",
                    AuditLog.actor_id == seeded.guardian_id,
                    AuditLog.action == "report.read",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1, "家属越权必须留痕（FR-RPT-02）"
    assert audit[0].resource_id == seeded.other_elder_id
    assert audit[0].detail["bound"] is False
    assert audit[0].detail["reason"] == "scope"


async def test_missing_report_is_404(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    client.holder.append(_as_admin(seeded))
    resp = await client.client.get(f"/api/reports/{seeded.elder_id}", params={"date": "2000-01-01"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_list_returns_recent_reports(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_admin(seeded))

    resp = await client.client.get(f"/api/reports/{seeded.elder_id}/list", params={"days": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["report_date"] == seeded.day.isoformat()


# ------------------------------------------------------------------- 调度任务


async def _notes(db: AsyncSession, kind: str) -> list[Notification]:
    """按 kind 取通知行。

    开发库里还有产品负责人手工 seed 的**在住**演示长者，两个日任务会一并给它落通知，
    所以断言只取**本用例自己那两位**长者的行，返回值则用「前后行数差」核对，
    不把全表总数写死（否则有历史行就假红）。
    """
    return list(
        (await db.execute(select(Notification).where(Notification.kind == kind))).scalars().all()
    )


async def test_missed_scan_creates_assigned_notification_once(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    before = await _notes(db, "collection_missed")
    created = await health_jobs.run_missed_scan()
    after = await _notes(db, "collection_missed")
    # 返回值必须等于本轮真的新插入的行数；库里另有演示长者，写死 == 1 会假红。
    assert created == len(after) - len(before)

    # 只数本用例的两位长者：当日无记录的 other_elder 该被派单，有记录的 elder 不该。
    rows = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.elder_id.in_([seeded.elder_id, seeded.other_elder_id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert [note.elder_id for note in rows] == [seeded.other_elder_id], (
        "只有当日无记录的 other_elder 该被派单"
    )
    note = rows[0]
    assert note.assignee_staff_id == seeded.care_worker_id, "派给 primary_care 护工"
    assert note.severity == "red"
    assert note.payload["business_date"] == TODAY.isoformat()

    # 再跑一次：幂等，不刷第二条。
    again = await health_jobs.run_missed_scan()
    assert again == 0
    ours = [
        item
        for item in await _notes(db, "collection_missed")
        if item.elder_id == seeded.other_elder_id
    ]
    assert len(ours) == 1, "重跑后本用例的长者仍只有一条派单"


async def test_checkup_start_skips_elders_with_records(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    before = await _notes(db, "health_review")
    created = await health_jobs.run_health_checkup_start()
    after = await _notes(db, "health_review")
    assert created == len(after) - len(before), "返回值必须等于本轮新增提醒数"

    # 只查本用例的两位长者：other_elder 当日无记录 → 一条 health_review；
    # elder 当日已有记录 → 一条都不该有（有的话这里会变成两行）。
    kinds = (
        (
            await db.execute(
                select(Notification.kind).where(
                    Notification.elder_id.in_([seeded.elder_id, seeded.other_elder_id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(kinds) == ["health_review"]


async def test_daily_report_job_generates_for_active_elders(
    db: AsyncSession, seeded: Seed, isolated_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # 在住长者的**库内真值**做期望：本用例 2 位 + 开发库既有的演示长者，不写死 2。
    active = int(
        (await db.scalar(select(func.count()).select_from(Elder).where(Elder.status == "active")))
        or 0
    )
    before = int((await db.scalar(select(func.count()).select_from(DailyReport))) or 0)

    made = await report_jobs.run_daily_report_generation()
    assert made == active, "每位在住长者各生成一次"

    # 本用例的两位长者各一行（只数自己造的，不数全表）。
    ours = int(
        (
            await db.scalar(
                select(func.count())
                .select_from(DailyReport)
                .where(DailyReport.elder_id.in_([seeded.elder_id, seeded.other_elder_id]))
            )
        )
        or 0
    )
    assert ours == 2, "本用例的两位在住长者各一行"
    first_total = int((await db.scalar(select(func.count()).select_from(DailyReport))) or 0)
    assert first_total >= before + 2

    # 再跑一遍：服务层幂等，行数不变。
    assert await report_jobs.run_daily_report_generation() == active
    assert int((await db.scalar(select(func.count()).select_from(DailyReport))) or 0) == first_total
