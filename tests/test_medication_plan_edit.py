"""用药计划「改」与「停」（PRD:331 FR-MED-01、PRD:508 AC-27）。

覆盖六件事：

1. 改剂量 → **事实修订**（讲义 §10.3）：旧行收口为只读历史、新行 `version` 自增后生效，
   历史事件 `drug_snapshot` 原样不动；
2. `expected_version` 与库中不符 → 409 `VERSION_CONFLICT`，且一行都不改；
3. 请求体校验：空体 422、非法 `schedule_cron` 422、改归属字段 422；
4. 停用 → `status='stopped'`；重复停用幂等 200 + `already_stopped`；
5. 停用后 `run_medication_scan` 不再为它建新事件；未触达事件被 `decide_event_action`
   判 `skip` 并记 `medication_skipped_total`，已触达事件照旧走；
6. 护工 PATCH/stop → 403 `ROLE_FORBIDDEN`，`audit_logs` 落 `medication.plan.update` /
   `medication.plan.stop` 两条拒绝记录。

隔离方式与 `tests/test_medication_api.py` 同款：真库 + `NullPool` 单连接 + 外层事务，
请求体与任务体都用 `join_transaction_mode="create_savepoint"` 的会话工厂，测试结束
整体回滚，不留残留。
"""

from __future__ import annotations

import datetime
import uuid
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import medication as medication_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    AuditLog,
    Elder,
    ElderStaffAssignment,
    MedicationEvent,
    MedicationPlan,
    StaffAccount,
)
from app.scheduler import medication as medication_jobs
from app.services import audit_service, medication_service, report_service

SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    connection = await require_db_connection(db_engine)
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture(autouse=True)
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把审计写入也接到测试连接上，否则 403 的审计行落到真库、回滚不掉。"""
    factory = async_sessionmaker(
        bind=db.bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    monkeypatch.setattr(audit_service, "session_factory", factory)
    return factory


@dataclass
class Seed:
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    doctor_id, care_worker_id = uuid.uuid4(), uuid.uuid4()
    elder_id, other_elder_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": doctor_id, "name": "王医生", "role": "doctor", "password_hash": "x"},
            {
                "staff_id": care_worker_id,
                "name": "李护工",
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
            {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
        ],
    )
    await db.flush()
    return Seed(
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


@dataclass
class Bundle:
    client: AsyncClient
    #: 每次请求前改写第一项即可切换调用主体；覆盖依赖读的是同一对象。
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(medication_api.router)

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


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


async def _make_plan(
    db: AsyncSession,
    seed: Seed,
    *,
    elder_id: uuid.UUID | None = None,
    drug_name: str = "二甲双胍",
    dose: str = "0.5g 1 片",
    schedule_cron: str = "08:00",
    start_date: datetime.date | None = None,
    status: str = "active",
) -> MedicationPlan:
    """测试自己造药单（测试代码不受 API 分层约束）。"""
    plan = MedicationPlan(
        plan_id=uuid.uuid4(),
        elder_id=elder_id or seed.elder_id,
        drug_name=drug_name,
        dose=dose,
        schedule_cron=schedule_cron,
        start_date=start_date or datetime.date(2026, 11, 8),
        created_by=seed.doctor_id,
        version=1,
        status=status,
    )
    db.add(plan)
    await db.flush()
    return plan


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 主体过滤：`audit_logs` 只增，开发库可能残留同名行。"""
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    rows = await db.scalars(stmt)
    return list(rows)


# ------------------------------------------------------------------ 改：成功路径


async def test_patch_dose_creates_new_version_and_retires_old_row(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """AC-27 的「改」（讲义 §10.3 事实修订）：旧行成为只读历史，新行才是生效版本。

    旧口径是「同一行 UPDATE + version+1」；§10.3 之后药单是「某段时间按某剂量服药」的
    事实，原地改写会抹掉「改单日之前是 0.5g」这段历史，所以这里钉的是两条：
    旧行剂量/时刻一格不动，接口返回的那一条是新 plan_id。
    """
    plan = await _make_plan(db, seeded)
    event, created = await medication_service.ensure_event(
        db,
        plan_id=plan.plan_id,
        elder_id=plan.elder_id,
        scheduled_at=datetime.datetime(2026, 11, 9, 0, 0, tzinfo=datetime.UTC),
        drug_snapshot={"drug_name": "二甲双胍", "dose": "0.5g 1 片", "plan_version": 1},
    )
    assert created is True
    await db.flush()

    client.holder[:] = [_as_doctor(seeded)]
    resp = await client.client.patch(
        f"/api/medication/plans/{plan.plan_id}",
        json={"dose": "0.25g 半片", "expected_version": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] != str(plan.plan_id), "修订必须开新行，不能原地改"
    assert body["dose"] == "0.25g 半片"
    assert body["drug_name"] == "二甲双胍"
    assert body["version"] == 2
    assert body["status"] == "active"

    today = medication_service.today_in_shanghai()
    assert body["start_date"] == today.isoformat(), "新版本从改单日生效"

    reminder_id = event.reminder_id
    old_id = plan.plan_id
    new_id = uuid.UUID(body["plan_id"])
    db.expire_all()

    old = await db.get(MedicationPlan, old_id)
    assert old is not None
    assert old.dose == "0.5g 1 片", "旧行是历史事实：剂量不被改写"
    assert old.schedule_cron == "08:00"
    assert old.status == "stopped"
    assert old.version == 1
    # 旧版本失效日 = 改单日前一天，但不早于它自己的 start_date。
    assert old.end_date == max(old.start_date, today - datetime.timedelta(days=1))

    new = await db.get(MedicationPlan, new_id)
    assert new is not None
    assert new.version == 2 and new.status == "active", "新行才是活性版本"
    assert new.dose == "0.25g 半片"
    assert new.start_date == today

    old_event = await db.get(MedicationEvent, reminder_id)
    assert old_event is not None
    assert old_event.drug_snapshot["dose"] == "0.5g 1 片"
    assert old_event.drug_snapshot["plan_version"] == 1

    # 审计跟着「改单产物」走：resource_id 是新 plan_id（detail 语义与旧口径一致）。
    rows = await _audit_rows(
        db, "medication.plan.update", actor_id=seeded.doctor_id, resource_id=new_id
    )
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["version_before"] == 1
    assert detail["version_after"] == 2
    assert detail["changed"] == ["dose"]


async def test_patch_without_expected_version_still_works(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """`expected_version` 是可选的：不传就按「读到什么改什么」，仍要自增。"""
    plan = await _make_plan(db, seeded)
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.patch(
        f"/api/medication/plans/{plan.plan_id}",
        json={"schedule_cron": "09:30"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["schedule_cron"] == "09:30"
    assert resp.json()["version"] == 2


# ------------------------------------------------------------------ 改：拒绝路径


async def test_patch_with_stale_expected_version_returns_409(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    plan = await _make_plan(db, seeded, dose="0.5g 1 片")
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.patch(
        f"/api/medication/plans/{plan.plan_id}",
        json={"dose": "0.25g 半片", "expected_version": 7},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "VERSION_CONFLICT"
    assert body["trace_id"] == resp.headers["x-trace-id"]

    plan_id = plan.plan_id
    db.expire_all()
    refreshed = await db.get(MedicationPlan, plan_id)
    assert refreshed is not None
    assert refreshed.dose == "0.5g 1 片"
    assert refreshed.version == 1


async def test_patch_with_empty_body_returns_422(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_doctor(seeded)]
    resp = await client.client.patch(f"/api/medication/plans/{uuid.uuid4()}", json={})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_patch_with_invalid_schedule_cron_returns_422(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    plan = await _make_plan(db, seeded)
    client.holder[:] = [_as_doctor(seeded)]
    resp = await client.client.patch(
        f"/api/medication/plans/{plan.plan_id}",
        json={"schedule_cron": "every 8 hours"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_patch_rejects_identity_fields(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """归属与版本号只能服务端定：请求体里塞 `elder_id` 直接 422。"""
    plan = await _make_plan(db, seeded)
    client.holder[:] = [_as_doctor(seeded)]
    resp = await client.client.patch(
        f"/api/medication/plans/{plan.plan_id}",
        json={"dose": "0.25g 半片", "elder_id": str(seeded.other_elder_id)},
    )
    assert resp.status_code == 422, resp.text


async def test_patch_unknown_plan_returns_404(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_doctor(seeded)]
    resp = await client.client.patch(
        f"/api/medication/plans/{uuid.uuid4()}", json={"dose": "0.25g 半片"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


async def test_patch_other_elder_plan_is_scope_forbidden(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """医生的范围闸来自 assignment：不在自己负责范围内的药单一律 403。"""
    plan = await _make_plan(db, seeded, elder_id=seeded.other_elder_id)
    client.holder[:] = [_as_doctor(seeded)]
    resp = await client.client.patch(
        f"/api/medication/plans/{plan.plan_id}", json={"dose": "0.25g 半片"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


# ------------------------------------------------------------------ 停


async def test_stop_plan_marks_stopped_and_is_idempotent(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    plan = await _make_plan(db, seeded)
    client.holder[:] = [_as_doctor(seeded)]

    first = await client.client.post(
        f"/api/medication/plans/{plan.plan_id}/stop", json={"reason": "医嘱调整"}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "stopped"
    assert body["version"] == 1

    plan_id = plan.plan_id
    db.expire_all()
    refreshed = await db.get(MedicationPlan, plan_id)
    assert refreshed is not None
    assert refreshed.status == "stopped"

    second = await client.client.post(f"/api/medication/plans/{plan.plan_id}/stop")
    assert second.status_code == 200, second.text
    assert second.json() == {"already_stopped": True}

    rows = await _audit_rows(
        db, "medication.plan.stop", actor_id=seeded.doctor_id, resource_id=plan.plan_id
    )
    assert len(rows) == 2
    reasons = {row.detail["already_stopped"]: row.detail["reason"] for row in rows}
    assert reasons == {False: "医嘱调整", True: None}


async def test_stop_after_todays_window_does_not_backfill_missed_reminder(
    client: Bundle,
    seeded: Seed,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """20:00 停掉当天 08:00 的药单：窗口已过，当天未触达的提醒不得回头补发。

    停止本身只负责「窗口收口 + status=stopped」，「不补发」这一步由
    `decide_event_action(plan_status="stopped")` 兜住。这条用例把两者的依赖钉死：
    将来谁把 stopped 分支删了或提前返回，这里先红，而不是等夜里长者被补发一次提醒。
    """
    recorded: list[tuple[str, dict[str, Any]]] = []

    def fake_counter(name: str, *, value: int = 1, **labels: Any) -> None:
        recorded.append((name, labels))

    monkeypatch.setattr(medication_jobs, "counter", fake_counter)

    today = medication_service.today_in_shanghai()
    plan = await _make_plan(db, seeded, schedule_cron="08:00", start_date=today)
    client.holder[:] = [_as_doctor(seeded)]

    stop_at = datetime.datetime.combine(today, datetime.time(20, 0), tzinfo=SHANGHAI)
    resp = await client.client.post(f"/api/medication/plans/{plan.plan_id}/stop")
    assert resp.status_code == 200, resp.text

    plan_id = plan.plan_id
    db.expire_all()
    stopped = await db.get(MedicationPlan, plan_id)
    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.end_date == today, "停用日即最后有效日"

    kwargs: dict[str, Any] = {
        "scheduled_at": datetime.datetime.combine(today, datetime.time(8, 0), tzinfo=SHANGHAI),
        "attempts_sent": 0,
        "last_sent_at": None,
        "now": stop_at,
        "attempts": 4,
        "wait_seconds": 60,
        "interval_minutes": 5,
    }

    skipped = medication_jobs.decide_event_action(**kwargs, plan_status=stopped.status)
    assert skipped.action == "skip"
    assert skipped.reason == "plan_stopped"
    assert recorded == [("medication_skipped_total", {"reason": "plan_stopped"})]

    # 反证：同一组参数若计划仍是 active，就不会走 plan_stopped（此刻已过窗口，
    # 按既有节奏是 escalate）—— 上面的 skip 不是恒真，靠的正是 stopped 分支。
    live = medication_jobs.decide_event_action(**kwargs, plan_status="active")
    assert live.action != "skip"


async def test_stopped_plan_generates_no_new_events(
    db: AsyncSession,
    seeded: Seed,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """停用生效点：`run_medication_scan` 只为 `status='active'` 的计划建事件。"""
    now = datetime.datetime.now(datetime.UTC)
    day = medication_jobs._business_date(now)
    scan_at = medication_jobs.scheduled_at_for(day, 9, 0, SHANGHAI)
    active = await _make_plan(db, seeded, schedule_cron="08:00", start_date=day)
    stopped = await _make_plan(db, seeded, schedule_cron="08:00", start_date=day, status="stopped")

    created = await medication_jobs.run_medication_scan(
        now=scan_at, session_factory=isolated_session_factory
    )
    assert created >= 1

    active_id, stopped_id = active.plan_id, stopped.plan_id
    db.expire_all()
    active_events = list(
        await db.scalars(select(MedicationEvent).where(MedicationEvent.plan_id == active_id))
    )
    stopped_events = list(
        await db.scalars(select(MedicationEvent).where(MedicationEvent.plan_id == stopped_id))
    )
    assert len(active_events) == 1
    assert stopped_events == []


def test_decide_event_action_skips_untouched_event_of_stopped_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未触达 → skip + 计数；已触达 → 不 skip（不静默丢掉在途提醒）。"""
    recorded: list[tuple[str, dict[str, Any]]] = []

    def fake_counter(name: str, *, value: int = 1, **labels: Any) -> None:
        recorded.append((name, labels))

    monkeypatch.setattr(medication_jobs, "counter", fake_counter)

    scheduled = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=datetime.UTC)
    base: dict[str, Any] = {
        "scheduled_at": scheduled,
        "attempts_sent": 0,
        "last_sent_at": None,
        "now": scheduled,
        "attempts": 4,
        "wait_seconds": 60,
        "interval_minutes": 5,
    }

    stopped = medication_jobs.decide_event_action(**base, plan_status="stopped")
    assert stopped.action == "skip"
    assert stopped.reason == "plan_stopped"
    assert recorded == [("medication_skipped_total", {"reason": "plan_stopped"})]

    active = medication_jobs.decide_event_action(**base, plan_status="active")
    assert active.action == "touch"
    assert active.attempt_no == 1

    unknown = medication_jobs.decide_event_action(**base, plan_status=None)
    assert unknown.action == "touch"

    touched = medication_jobs.decide_event_action(
        **{**base, "attempts_sent": 1, "last_sent_at": scheduled},
        plan_status="stopped",
    )
    assert touched.action != "skip"


# ------------------------------------------------------------------ 权限闸


async def test_care_worker_cannot_patch_or_stop_and_is_audited(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    plan = await _make_plan(db, seeded)
    client.holder[:] = [_as_care_worker(seeded)]

    patch_resp = await client.client.patch(
        f"/api/medication/plans/{plan.plan_id}", json={"dose": "0.25g 半片"}
    )
    assert patch_resp.status_code == 403, patch_resp.text
    assert patch_resp.json()["code"] == "ROLE_FORBIDDEN"

    stop_resp = await client.client.post(f"/api/medication/plans/{plan.plan_id}/stop")
    assert stop_resp.status_code == 403, stop_resp.text
    assert stop_resp.json()["code"] == "ROLE_FORBIDDEN"

    update_rows = await _audit_rows(
        db,
        "medication.plan.update",
        actor_id=seeded.care_worker_id,
        resource_id=plan.plan_id,
    )
    stop_rows = await _audit_rows(
        db, "medication.plan.stop", actor_id=seeded.care_worker_id, resource_id=plan.plan_id
    )
    assert len(update_rows) == 1
    assert len(stop_rows) == 1
    assert update_rows[0].detail["reason"] == "role"
    assert stop_rows[0].detail["reason"] == "role"

    plan_id = plan.plan_id
    db.expire_all()
    refreshed = await db.get(MedicationPlan, plan_id)
    assert refreshed is not None
    assert refreshed.dose == "0.5g 1 片"
    assert refreshed.status == "active"
    assert refreshed.version == 1
