"""迭代 7 · G 号卡：用药 REST API（权限闸 / 范围闸 / 409 / 按钮确认）。

覆盖的验收条款不是"顺手测一下"，每条都对应 PRD 原文：

- **仅医生可写**（PRD §4.5 + FR-MED-01）：护工 `POST /plans` 必须 403 `ROLE_FORBIDDEN`，
  且 `audit_logs` 里**必须**留一行 —— 只 403 不留痕等于验收没过。
- **只读角色可读**（PRD §4.5）：医生/护工/管理员 `GET` 通过，家属与设备一律 403。
- **范围隔离**：医生只能读自己负责的长者，换一个 elder 立刻 403（不泄露存在性）。
- **冲突 409**（FR-MED-04）：服务层 `MedicationEventConflict` 必须映射成 409 `CONFLICT`，
  不能被吞成 500。
- **代确认渠道**（W2）：console 代确认写 `confirm_channel='button'`、终态
  `taken_reported`；`taken_reported` 只是「已服自报」，测试注释里也不许说成"已服药"。
- **错误契约**（`tests/test_error_contract.py`）：错误体恰好 `{detail, code, trace_id}`。

## 测试怎么不污染库

业务读写走 `db` fixture 的外层事务（`get_session` 被覆盖成 savepoint 模式，请求内的
`commit()` 只释放保存点，teardown 整体 rollback）。越权审计**自己开会话**
（`report_service.session_factory`），所以 `isolated_session_factory` 把它换到同一条
测试连接上——既测到真实 SQL，又不把审计行提交进种子库。

## 服务层缺函数时

`medication_service` 与 `memory_service` 由 B/C 并行落地。计划侧的 `list_plans` /
`create_plan` / `list_events` 若尚未交付，API 会走 `503 SERVICE_NOT_READY`（见
`app/api/medication.py` 的适配块）；对应用例在收集期按函数是否存在 skip，
而不是伪造一段 SQL 进 API 层绕过分层约束。
"""

from __future__ import annotations

import datetime
import importlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver
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
from app.graphs import medication_confirmation_graph as medication_graph
from app.models import (
    AuditLog,
    Elder,
    ElderStaffAssignment,
    MedicationAttempt,
    MedicationEvent,
    MedicationPlan,
    StaffAccount,
)
from app.services import report_service

# ---------------------------------------------------------------- 服务层能力探测


def _svc(name: str) -> object | None:
    """取 `medication_service` 的公开函数；未落地或未实现返回 None。"""
    try:
        module = importlib.import_module("app.services.medication_service")
    except ImportError:
        return None
    candidate = getattr(module, name, None)
    return candidate if callable(candidate) else None


#: 计划侧读写在服务层落地前不可测；签名与 `app/api/medication.py` 适配块一致。
needs_plans = pytest.mark.skipif(
    _svc("list_plans") is None or _svc("create_plan") is None,
    reason="medication_service.list_plans/create_plan 尚未落地",
)

PLAN_BODY = {
    "drug_name": "二甲双胍",
    "dose": "0.5g 1 片",
    "schedule_cron": "08:00",
    "start_date": "2026-11-08",
}


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


@pytest.fixture(autouse=True)
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把越权审计的"自己开会话"钉在同一条测试连接上。"""
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
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    care_worker_id: uuid.UUID
    guardian_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, care_worker_id = (uuid.uuid4() for _ in range(3))
    guardian_id, elder_id, other_elder_id = (uuid.uuid4() for _ in range(3))

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {"staff_id": admin_id, "name": "管理员", "role": "admin", "password_hash": "x"},
            {"staff_id": doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
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
    # 医生只负责 elder_id；other_elder_id 的负责护工是 care_worker —— 用来区分
    # "角色够但范围不够"和"范围够但角色不够"两种 403。
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"},
            {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
            {"elder_id": other_elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
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


def _as_admin(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.admin_id), role="admin")


def _as_doctor(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.doctor_id), role="doctor")


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 主体过滤。

    过滤是必须的，不是保险：`audit_logs` 是只增表，开发库里可能残留上一次跑测的
    同名 action 行；不断言 actor 就会把历史行算进本次结果（实测踩过：3 ≠ 1）。
    """
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    rows = await db.scalars(stmt)
    return list(rows)


async def _make_plan(db: AsyncSession, seed: Seed) -> MedicationPlan:
    """测试自己造一条药单（测试代码不受 API 分层约束）。"""
    plan = MedicationPlan(
        plan_id=uuid.uuid4(),
        elder_id=seed.elder_id,
        drug_name="二甲双胍",
        dose="0.5g 1 片",
        schedule_cron="08:00",
        start_date=datetime.date(2026, 11, 8),
        created_by=seed.doctor_id,
        status="active",
    )
    db.add(plan)
    await db.flush()
    return plan


# ------------------------------------------------------------------ 权限闸（写）


async def test_care_worker_cannot_create_plan_and_is_audited(
    client: Bundle,
    seeded: Seed,
    db: AsyncSession,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FR-MED-01：护工建药单 → 403 且 `audit_logs` 必须有一行。"""
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.post(
        "/api/medication/plans", json={"elder_id": str(seeded.elder_id), **PLAN_BODY}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, "medication.plan.create", actor_id=seeded.care_worker_id)
    assert len(rows) == 1, "越权写必须留痕，否则 FR-MED-01 不算过"
    assert rows[0].actor_type == "staff"
    assert rows[0].actor_id == seeded.care_worker_id
    assert rows[0].resource_id == seeded.elder_id
    assert rows[0].detail["reason"] == "role"
    assert rows[0].detail["role"] == "care_worker"
    assert rows[0].detail["kind"] == "staff"


async def test_admin_cannot_create_plan(client: Bundle, seeded: Seed) -> None:
    """PRD §4.5：管理员对药单**只读**，同样 403。"""
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.post(
        "/api/medication/plans", json={"elder_id": str(seeded.elder_id), **PLAN_BODY}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_guardian_cannot_create_plan(client: Bundle, seeded: Seed) -> None:
    """家属不是 staff 主体：403 走 `SCOPE_FORBIDDEN`（与 elders 接口同口径）。"""
    client.holder[:] = [_as_guardian(seeded)]

    resp = await client.client.post(
        "/api/medication/plans", json={"elder_id": str(seeded.elder_id), **PLAN_BODY}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


@needs_plans
async def test_doctor_create_plan_returns_201(
    client: Bundle,
    seeded: Seed,
) -> None:
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.post(
        "/api/medication/plans", json={"elder_id": str(seeded.elder_id), **PLAN_BODY}
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["elder_id"] == str(seeded.elder_id)
    assert body["drug_name"] == PLAN_BODY["drug_name"]
    assert body["dose"] == PLAN_BODY["dose"], "列名是 dose，不是 dosage"
    assert body["schedule_cron"] == PLAN_BODY["schedule_cron"]
    assert body["status"] == "active"
    assert body["created_by"] == str(seeded.doctor_id)


async def test_create_plan_rejects_bad_schedule(client: Bundle, seeded: Seed) -> None:
    """`schedule_cron` 只接受 `HH:MM`：形状错在进入 handler 前就是 422。"""
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.post(
        "/api/medication/plans",
        json={"elder_id": str(seeded.elder_id), **PLAN_BODY, "schedule_cron": "8:00"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_create_plan_rejects_unknown_field(client: Bundle, seeded: Seed) -> None:
    """`extra="forbid"`：客户端塞 `dosage`（旧口径列名）必须被拒，而不是静默丢弃。"""
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.post(
        "/api/medication/plans", json={"elder_id": str(seeded.elder_id), **PLAN_BODY, "dosage": "x"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------------ 权限闸（读）


async def test_guardian_cannot_list_plans(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_guardian(seeded)]

    resp = await client.client.get(
        "/api/medication/plans", params={"elder_id": str(seeded.elder_id)}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


async def test_doctor_cannot_read_other_elder(client: Bundle, seeded: Seed) -> None:
    """角色够（医生）但范围不够：403 `SCOPE_FORBIDDEN`，且不泄露长者是否存在。"""
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.get(
        "/api/medication/plans", params={"elder_id": str(seeded.other_elder_id)}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


async def test_unknown_elder_returns_404(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_admin(seeded)]

    resp = await client.client.get("/api/medication/plans", params={"elder_id": str(uuid.uuid4())})

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


@needs_plans
async def test_list_plans_is_scoped_to_one_elder(client: Bundle, seeded: Seed) -> None:
    """列表只回本 elder 的药单：跨长者不可见。"""
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.get(
        "/api/medication/plans", params={"elder_id": str(seeded.other_elder_id)}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == [], "别的长者名下没有药单"


# ------------------------------------------------------------------- 代确认（W2）


async def _make_event(db: AsyncSession, seed: Seed) -> MedicationEvent:
    """造一条待确认事件（走服务层 `ensure_event`，与调度器同一入口）。"""
    plan = await _make_plan(db, seed)
    ensure_event = _svc("ensure_event")
    assert ensure_event is not None
    event, _created = await ensure_event(  # type: ignore[misc]
        db,
        plan_id=plan.plan_id,
        elder_id=seed.elder_id,
        scheduled_at=datetime.datetime(2026, 11, 8, 0, 0, tzinfo=datetime.UTC),
        drug_snapshot={"drug_name": plan.drug_name, "dose": plan.dose},
        triggered_by="dialog",
    )
    return event


async def test_confirm_writes_button_channel(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """护工代确认：`confirm_channel='button'` + 终态 `taken_reported`（已服自报）。"""
    event = await _make_event(db, seeded)
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.post(f"/api/medication/events/{event.reminder_id}/confirm", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirm_channel"] == "button"
    assert body["status"] == "confirmed"
    assert body["terminal_state"] == "taken_reported"
    assert body["hitl_resumed"] is False, (
        "这条事件没有走图（没有 HITL 停点）→ 直写业务表，如实报 False"
    )

    await db.refresh(event)
    assert event.confirm_channel == "button"
    assert event.terminal_state == "taken_reported"
    assert event.confirmed_at is not None


async def test_confirm_unknown_reminder_returns_404(client: Bundle, seeded: Seed) -> None:
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.post(f"/api/medication/events/{uuid.uuid4()}/confirm", json={})

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


async def test_confirm_cross_elder_is_forbidden(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """事件归属由库里反查，客户端无法指定；范围外护工 403。"""
    event = await _make_event(db, seeded)
    # 换成另一位**没有**该长者 assignment 的员工：角色够（医生），范围不够。
    other_doctor = Identity(kind="staff", staff_id=str(uuid.uuid4()), role="doctor")
    client.holder[:] = [other_doctor]

    resp = await client.client.post(f"/api/medication/events/{event.reminder_id}/confirm", json={})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


async def test_confirm_conflict_maps_to_409(
    client: Bundle,
    seeded: Seed,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-MED-04：服务层 `MedicationEventConflict` → 409 `CONFLICT`，不能变 500。"""
    medication_service = importlib.import_module("app.services.medication_service")
    conflict_cls = medication_service.MedicationEventConflict

    async def _boom(*args: object, **kwargs: object) -> None:
        raise conflict_cls("同键异内容")

    monkeypatch.setattr(medication_api, "_svc_confirm_event", _boom)
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.post(f"/api/medication/events/{uuid.uuid4()}/confirm", json={})

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "CONFLICT"


async def test_confirm_resumes_the_pending_hitl_stop(
    client: Bundle,
    seeded: Seed,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有 HITL 停点时按钮确认必须**推图**，而不是另写一遍库（FR-MED-05 / AC-17）。

    `hitl_resumed` 报的是「谁落的终态」，不是成败：同一条路径在无停点时返回
    False（见上一条用例），两者都是 200。这里断言 True 之外还要断言事件行确实
    由图的 `wait_confirm` 写出来（`confirm_channel='button'`、终态
    `taken_reported`），否则「说推了图其实没推」也会绿。
    """
    event = await _make_event(db, seeded)
    thread = medication_graph.medication_thread_id(
        elder_id=seeded.elder_id,
        plan_id=event.plan_id,
        scheduled_at=event.scheduled_at,
    )

    async def _deliver(_event: object, _text: str) -> Any:
        return medication_graph.DeliveryResult(receipt="card_rendered", tts_result="ok")

    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    graph = await medication_graph.build_medication_graph(
        checkpointer=MemorySaver(),
        deliver=_deliver,
        session_factory=factory,
    )
    snapshot = dict(event.drug_snapshot)
    await graph.ainvoke(
        medication_graph.initial_state(
            thread_id=thread,
            elder_id=str(seeded.elder_id),
            reminder_id=str(event.reminder_id),
            plan_id=str(event.plan_id),
            scheduled_at=event.scheduled_at,
            drug_snapshot=snapshot,
        ),
        medication_graph.state_config(thread),
    )
    assert await medication_graph.pending_confirmation(graph, thread) is not None

    service = importlib.import_module("app.services.medication_service")

    async def _graph_provider() -> Any:
        return graph

    monkeypatch.setattr(service, "confirmation_graph", _graph_provider)
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.post(f"/api/medication/events/{event.reminder_id}/confirm", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hitl_resumed"] is True, "有停点却没报 True，说明确认根本没打到图上"
    assert body["status"] == "confirmed"
    assert body["terminal_state"] == "taken_reported"
    assert body["confirm_channel"] == "button", "按钮确认的渠道由 resume 载荷带入"

    assert await medication_graph.pending_confirmation(graph, thread) is None
    attempts = await db.scalar(
        select(func.count())
        .select_from(MedicationAttempt)
        .where(MedicationAttempt.event_id == event.reminder_id)
    )
    assert attempts == 1, "推图不是「再发一次提醒」，触达行数不许变"


# -------------------------------------------------------------------- 错误契约


async def test_error_body_matches_contract(client: Bundle, seeded: Seed) -> None:
    """错误体恰好三字段，`trace_id` 与响应头一致（`tests/test_error_contract.py` 同口径）。"""
    client.holder[:] = [_as_guardian(seeded)]

    resp = await client.client.get(
        "/api/medication/plans", params={"elder_id": str(seeded.elder_id)}
    )

    body = resp.json()
    assert resp.status_code == 403
    assert set(body) == {"detail", "code", "trace_id"}
    assert body["detail"]
    assert len(body["trace_id"]) == 32
    assert body["trace_id"] == resp.headers["x-trace-id"]


# ------------------------------------------------ FR-MED-09 一键续方（0020 批次）


async def _plan_with_end_date(
    db: AsyncSession, seed: Seed, *, days: int = 3, elder_id: uuid.UUID | None = None
) -> MedicationPlan:
    """造一条有到期日的药单：续方必须知道旧 `end_date` 才谈得上「接上」。"""
    plan = MedicationPlan(
        plan_id=uuid.uuid4(),
        elder_id=elder_id or seed.elder_id,
        drug_name="阿托伐他汀",
        dose="1 片",
        schedule_cron="20:00",
        start_date=datetime.date.today() - datetime.timedelta(days=30),
        end_date=datetime.date.today() + datetime.timedelta(days=days),
        created_by=seed.doctor_id,
        status="active",
    )
    db.add(plan)
    await db.flush()
    return plan


async def test_doctor_renew_plan_returns_201_and_audits(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """续方 = 新开一条：201 + 新计划，`renewed_from_plan_id` 指回旧计划，审计留痕。"""
    plan = await _plan_with_end_date(db, seeded)
    # 先抓住要用的事实：请求回来后 `db.expire_all()` 会把这些属性作废，异步下再取会炸。
    plan_id = plan.plan_id
    old_end = plan.end_date
    old_drug, old_dose, old_cron = plan.drug_name, plan.dose, plan.schedule_cron
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.post(f"/api/medication/plans/{plan_id}/renew")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["plan_id"] != str(plan_id)
    assert body["renewed_from_plan_id"] == str(plan_id)
    assert body["elder_id"] == str(seeded.elder_id)
    assert body["drug_name"] == old_drug
    assert body["dose"] == old_dose
    assert body["schedule_cron"] == old_cron
    assert old_end is not None
    assert body["start_date"] == (old_end + datetime.timedelta(days=1)).isoformat()
    assert body["end_date"] == (old_end + datetime.timedelta(days=1 + 30)).isoformat()
    assert body["status"] == "active"
    assert body["version"] == 1

    rows = await _audit_rows(db, "medication.plan.renew", actor_id=seeded.doctor_id)
    assert len(rows) == 1, "续方必须留下审计，否则「谁给谁续的方」查不出来"
    assert rows[0].resource_id == uuid.UUID(body["plan_id"])
    assert rows[0].detail["renewed_from_plan_id"] == str(plan_id)
    assert rows[0].detail["previous_end_date"] == old_end.isoformat()

    # 旧计划一行不改：保持 active，按自己的 end_date 自然到期。
    db.expire_all()
    refreshed = (
        await db.execute(select(MedicationPlan).where(MedicationPlan.plan_id == plan_id))
    ).scalar_one()
    assert refreshed.status == "active"
    assert refreshed.end_date == old_end


async def test_care_worker_cannot_renew_plan(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """续方是医嘱行为：护工 403，且越权留痕。"""
    plan = await _plan_with_end_date(db, seeded)
    client.holder[:] = [_as_care_worker(seeded)]

    resp = await client.client.post(f"/api/medication/plans/{plan.plan_id}/renew")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_rows(db, "medication.plan.renew", actor_id=seeded.care_worker_id)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "role"


async def test_doctor_cannot_renew_other_elder_plan(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """角色够但范围不够：403 `SCOPE_FORBIDDEN`（不泄露该长者是否存在）。"""
    plan = await _plan_with_end_date(db, seeded, elder_id=seeded.other_elder_id)
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.post(f"/api/medication/plans/{plan.plan_id}/renew")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


async def test_renew_long_term_plan_is_422(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """长期医嘱（`end_date IS NULL`）没有到期日，续方没有起点 → 422。"""
    plan = await _make_plan(db, seeded)  # end_date 为空
    client.holder[:] = [_as_doctor(seeded)]

    resp = await client.client.post(f"/api/medication/plans/{plan.plan_id}/renew")

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_renew_twice_conflicts(client: Bundle, seeded: Seed, db: AsyncSession) -> None:
    """重复点按钮：第二次 409，不会再开第二条计划（幂等靠「已续过」判定）。"""
    plan = await _plan_with_end_date(db, seeded)
    client.holder[:] = [_as_doctor(seeded)]

    first = await client.client.post(f"/api/medication/plans/{plan.plan_id}/renew")
    assert first.status_code == 201, first.text

    second = await client.client.post(f"/api/medication/plans/{plan.plan_id}/renew")

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "CONFLICT"
    count = await db.scalar(
        select(func.count())
        .select_from(MedicationPlan)
        .where(MedicationPlan.elder_id == seeded.elder_id)
    )
    assert count == 2, "旧计划 + 续方计划，不许有多余的一条"
