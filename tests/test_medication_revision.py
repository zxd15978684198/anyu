"""用药计划「事实修订」语义（讲义 §10.3 / DAG 节点 N26）。

三条不变量（本文件就是它们的证据）：

1. **改 = 旧行失效 + 新行生效**：旧行 `status='stopped'` 且 `end_date` 收口到改单日
   前一天，新行带修订后的字段与 `version+1` 生效；旧行的药名/剂量/时刻一格不动——
   那是「那天按什么剂量发的药」的唯一证据（事件快照之外的第二份）。
2. **读取只取生效版本**：调度、日汇总、合并卡片都按 `status='active'` 过滤；停用后
   生效版本为 0 条，历史行仍可查（列表接口不隐藏历史，前端靠状态徽标区分）。
3. **只能修订「在效期内」的版本**：已停用、已过 `end_date` 的行只读；空提交（值与
   当前完全相同）不开新版本。

夹具与 `tests/test_medication_plan_edit.py` 同款（单连接 + 外层事务回滚），不抄第二套
断言口径：本文件只测「版本链」，编辑语义的鉴权/审计留在原文件。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

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
    MedicationPlan,
    StaffAccount,
)
from app.services import audit_service, medication_service, report_service

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
    """审计写入走独立 session：接不回测试连接就会把行落到真库、回滚不掉。"""
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
    elder_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    doctor_id, elder_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        StaffAccount.__table__.insert(),
        [{"staff_id": doctor_id, "name": "王医生", "role": "doctor", "password_hash": "x"}],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": f"张桂芬{uuid.uuid4().hex[:6]}", "status": "active"}],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [{"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"}],
    )
    await db.flush()
    return Seed(doctor_id=doctor_id, elder_id=elder_id)


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


async def _make_plan(
    db: AsyncSession,
    seed: Seed,
    *,
    drug_name: str = "二甲双胍",
    dose: str = "0.5g 1 片",
    schedule_cron: str = "08:00",
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    version: int = 1,
    status: str = "active",
) -> MedicationPlan:
    plan = MedicationPlan(
        plan_id=uuid.uuid4(),
        elder_id=seed.elder_id,
        drug_name=drug_name,
        dose=dose,
        schedule_cron=schedule_cron,
        start_date=start_date
        or (medication_service.today_in_shanghai() - datetime.timedelta(days=5)),
        end_date=end_date,
        created_by=seed.doctor_id,
        version=version,
        status=status,
    )
    db.add(plan)
    await db.flush()
    return plan


async def _rows(db: AsyncSession, seed: Seed) -> list[MedicationPlan]:
    """该长者名下全部药单行（按 version 排序，方便断言版本链）。"""
    result = await db.scalars(
        select(MedicationPlan)
        .where(MedicationPlan.elder_id == seed.elder_id)
        .order_by(MedicationPlan.version)
    )
    return list(result)


async def _row_count(db: AsyncSession, seed: Seed) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(MedicationPlan)
            .where(MedicationPlan.elder_id == seed.elder_id)
        )
        or 0
    )


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 主体过滤：`audit_logs` 只增，开发库里可能残留同名行。"""
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    return list(await db.scalars(stmt))


# ------------------------------------------------------------------ 改剂量：旧行失效 + 新行生效


async def test_dose_revision_retires_old_row_and_creates_new_version(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """改剂量 → 旧行 stopped 且窗口收口，新行 active、version+1，历史两行都查得到。"""
    today = medication_service.today_in_shanghai()
    plan = await _make_plan(db, seeded, start_date=today - datetime.timedelta(days=5))
    old_id = plan.plan_id

    client.holder[:] = [_as_doctor(seeded)]
    resp = await client.client.patch(
        f"/api/medication/plans/{old_id}",
        json={"dose": "0.25g 半片", "expected_version": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["dose"] == "0.25g 半片"
    assert body["version"] == 2
    assert body["status"] == "active"
    assert body["plan_id"] != str(old_id), "修订必须开新行，不能原地改"
    assert body["start_date"] == today.isoformat(), "新版本从改单日生效"
    assert body["drug_name"] == "二甲双胍"
    assert body["schedule_cron"] == "08:00"

    db.expire_all()
    retired = await db.get(MedicationPlan, old_id)
    assert retired is not None
    assert retired.status == "stopped"
    assert retired.version == 1, "旧行的版本号不因被取代而自增"
    assert retired.dose == "0.5g 1 片", "旧行的剂量是历史事实，一格不动"
    assert retired.end_date == today - datetime.timedelta(days=1), "旧行窗口收口到改单日前一天"

    rows = await _rows(db, seeded)
    assert len(rows) == 2
    assert [row.version for row in rows] == [1, 2]
    assert [row.status for row in rows] == ["stopped", "active"]

    active = await medication_service.list_plans(db, elder_id=seeded.elder_id, status="active")
    assert [row.plan_id for row in active] == [uuid.UUID(body["plan_id"])], "读取只取生效版本"

    # 历史两行在列表接口里都查得到（前端靠状态徽标区分，不隐藏历史）。
    listed = await client.client.get(f"/api/medication/plans?elder_id={seeded.elder_id}")
    assert listed.status_code == 200, listed.text
    assert {item["plan_id"] for item in listed.json()["items"]} == {
        str(old_id),
        body["plan_id"],
    }

    # 审计落在**新版本**的 plan_id 上：改单这个动作的产物是新版本，旧行只是被收口。
    audits = await _audit_rows(db, "medication.plan.update", actor_id=seeded.doctor_id)
    assert len(audits) == 1
    assert str(audits[0].resource_id) == body["plan_id"]
    assert audits[0].detail["version_before"] == 1
    assert audits[0].detail["version_after"] == 2
    assert audits[0].detail["changed"] == ["dose"]


async def test_revision_chain_keeps_one_active_version(seeded: Seed, db: AsyncSession) -> None:
    """连改两次：三行、版本 1/2/3 单调，任何时刻只有最后一行 active。"""
    today = medication_service.today_in_shanghai()
    plan = await _make_plan(db, seeded, start_date=today - datetime.timedelta(days=5))

    first = await medication_service.update_plan(db, plan=plan, changes={"dose": "0.25g 半片"})
    second = await medication_service.update_plan(
        db, plan=first, changes={"dose": "0.125g 四分之一片"}
    )

    assert second.plan_id not in {plan.plan_id, first.plan_id}
    assert (plan.version, first.version, second.version) == (1, 2, 3)
    assert (plan.status, first.status, second.status) == ("stopped", "stopped", "active")

    active = await medication_service.list_plans(db, elder_id=seeded.elder_id, status="active")
    assert [row.plan_id for row in active] == [second.plan_id]
    assert len(await _rows(db, seeded)) == 3


async def test_revision_keeps_old_cron_and_drug_name_facts(seeded: Seed, db: AsyncSession) -> None:
    """改时刻不改写历史：旧行仍是旧时刻，新行才是新时刻。"""
    today = medication_service.today_in_shanghai()
    plan = await _make_plan(db, seeded, start_date=today - datetime.timedelta(days=5))

    revised = await medication_service.update_plan(
        db, plan=plan, changes={"schedule_cron": "20:00"}
    )

    await db.refresh(plan)
    assert plan.schedule_cron == "08:00"
    assert revised.schedule_cron == "20:00"
    assert revised.start_date == today


async def test_revision_inherits_end_date_when_not_changed(seeded: Seed, db: AsyncSession) -> None:
    """没改停用日就沿着原窗口走：新行 end_date = 旧窗口的 end_date（不是收口日）。"""
    today = medication_service.today_in_shanghai()
    end_date = today + datetime.timedelta(days=10)
    plan = await _make_plan(
        db, seeded, start_date=today - datetime.timedelta(days=5), end_date=end_date
    )

    revised = await medication_service.update_plan(db, plan=plan, changes={"dose": "0.25g 半片"})

    assert revised.end_date == end_date
    await db.refresh(plan)
    assert plan.end_date == today - datetime.timedelta(days=1), "旧行窗口按改单日收口"


async def test_revision_of_not_started_plan_keeps_its_start_date(
    seeded: Seed, db: AsyncSession
) -> None:
    """还没到开始日的药单，改剂量不能被提前到今天生效（否则等于提前开吃）。"""
    today = medication_service.today_in_shanghai()
    future_start = today + datetime.timedelta(days=3)
    plan = await _make_plan(db, seeded, start_date=future_start)

    revised = await medication_service.update_plan(db, plan=plan, changes={"dose": "0.25g 半片"})

    assert revised.start_date == future_start, "新版本不得早于原计划开始日"
    await db.refresh(plan)
    assert plan.end_date == future_start, "旧行收口不越过自己的开始日"
    assert plan.status == "stopped"


# ------------------------------------------------------------------ 停用：读不到生效版本


async def test_stop_plan_leaves_no_effective_version(seeded: Seed, db: AsyncSession) -> None:
    """停用 → status=stopped + 窗口收口到停用日；生效版本 0 条，行仍在。"""
    today = medication_service.today_in_shanghai()
    plan = await _make_plan(db, seeded, start_date=today - datetime.timedelta(days=5))

    await medication_service.stop_plan(db, plan=plan, reason="医嘱调整")

    assert plan.status == "stopped"
    assert plan.end_date == today, "停用日即最后有效日（含）"
    assert plan.version == 1, "停用是生命周期动作，不动内容版本"
    assert await medication_service.list_plans(db, elder_id=seeded.elder_id, status="active") == []
    assert len(await _rows(db, seeded)) == 1

    again = await medication_service.stop_plan(db, plan=plan)
    assert again is plan, "重复停用幂等：返回原行，不新增、不改版本"
    assert plan.version == 1


async def test_update_rejects_stopped_plan(seeded: Seed, db: AsyncSession) -> None:
    """已停用的行只读：要做新决定就新建药单，不能把历史行「复活」。"""
    plan = await _make_plan(db, seeded, status="stopped")

    with pytest.raises(medication_service.MedicationValidationError) as excinfo:
        await medication_service.update_plan(db, plan=plan, changes={"dose": "0.25g 半片"})

    assert "已停用" in str(excinfo.value)
    assert await _row_count(db, seeded) == 1


async def test_update_rejects_expired_plan(seeded: Seed, db: AsyncSession) -> None:
    """已过 end_date 的计划不能再改：疗程结束了，改它等于伪造医嘱。"""
    today = medication_service.today_in_shanghai()
    plan = await _make_plan(
        db,
        seeded,
        start_date=today - datetime.timedelta(days=10),
        end_date=today - datetime.timedelta(days=1),
    )

    with pytest.raises(medication_service.MedicationValidationError) as excinfo:
        await medication_service.update_plan(db, plan=plan, changes={"dose": "0.25g 半片"})

    assert "结束" in str(excinfo.value)
    assert await _row_count(db, seeded) == 1


async def test_end_date_exactly_today_is_still_revisable(seeded: Seed, db: AsyncSession) -> None:
    """边界：`end_date == 今天`（最后一天）仍可修订 —— 只挡「已经过去」。"""
    today = medication_service.today_in_shanghai()
    plan = await _make_plan(
        db,
        seeded,
        start_date=today - datetime.timedelta(days=5),
        end_date=today,
    )

    revised = await medication_service.update_plan(db, plan=plan, changes={"dose": "0.25g 半片"})

    assert revised.status == "active"
    assert revised.end_date == today


# ------------------------------------------------------------------ 不开新版本的三种情形


async def test_update_without_actual_change_keeps_single_row(
    seeded: Seed, db: AsyncSession
) -> None:
    """值没变 → 不开新版本、不 bump version，返回原行（空提交不制造历史）。"""
    plan = await _make_plan(db, seeded)

    same = await medication_service.update_plan(db, plan=plan, changes={"dose": "0.5g 1 片"})

    assert same is plan
    assert plan.version == 1
    assert plan.status == "active"
    assert await _row_count(db, seeded) == 1


async def test_stale_expected_version_creates_no_new_row(seeded: Seed, db: AsyncSession) -> None:
    """乐观锁不符 → 409 语义，且**一行都不新增**（旧行仍是唯一生效版本）。"""
    plan = await _make_plan(db, seeded)

    with pytest.raises(medication_service.MedicationVersionConflict):
        await medication_service.update_plan(
            db, plan=plan, changes={"dose": "0.25g 半片"}, expected_version=7
        )

    assert plan.status == "active"
    assert plan.version == 1
    assert await _row_count(db, seeded) == 1


async def test_revision_window_conflict_does_not_touch_old_row(
    seeded: Seed, db: AsyncSession
) -> None:
    """把停用日改到今天之前 → 参数错，且旧行**不能**先被改成 stopped 再抛异常。"""
    today = medication_service.today_in_shanghai()
    plan = await _make_plan(
        db,
        seeded,
        start_date=today - datetime.timedelta(days=10),
        end_date=today + datetime.timedelta(days=10),
    )

    with pytest.raises(medication_service.MedicationValidationError) as excinfo:
        await medication_service.update_plan(
            db, plan=plan, changes={"end_date": today - datetime.timedelta(days=3)}
        )

    assert "修订后" in str(excinfo.value)
    await db.refresh(plan)
    assert plan.status == "active", "校验失败时旧行必须原样"
    assert plan.end_date == today + datetime.timedelta(days=10)
    assert await _row_count(db, seeded) == 1
