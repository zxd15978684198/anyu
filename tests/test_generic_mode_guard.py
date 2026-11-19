"""W2 · 通用模式（generic）的「三不」服务端守卫（FR-VPR-03 / AC-28）。

PRD line 285（FR-VPR-03）：身份无法确定时**不读个人记忆、不写健康记录、不确认用药**，
并提示「需先确认身份」。记忆侧已在 `app/services/dialogue_service.py` 落地（`is_generic`
早就在那儿生效），本用例补的是 **API 侧两道写闸**：

- `POST /api/health/records` —— `app/api/health.py::fill_vitals`
- `POST /api/medication/events/{reminder_id}/confirm` —— `app/api/medication.py::confirm_event`

断言口径（计划 `设计记录（身份闭环缺口补齐）` §3-W2 / §7 S-D4-2）：

1. **403 而不是 422** —— 缺的是「你是谁」这个前置，不是请求参数不合法；
2. `code == "IDENTITY_REQUIRED"`，`detail` 是可直接播报的固定中文提示；
3. 错误体恰好 `{detail, code, trace_id}`，且 `trace_id` 与响应头 `x-trace-id` 一致
   （契约 `ContractAPIRoute`，前端上报问题只认这一个 id）；
4. **不落库、不写审计** —— generic 本就不该产生个人数据、也不该留下痕迹；
5. **回归**：非 generic 主体（已绑定设备 / 员工）仍按原语义工作。新守卫最危险的失败
   模式不是「没挡住」，而是「挡过头」—— 把真正的角色/范围拒绝换成身份拒绝，前端就会
   一直提示「请先确认身份」，而用户其实早就登录了。

## generic 是怎么构造出来的

`Identity`（`app/core/runtime_context.py`）只有已验签的主体种类，**没有 generic 这一档**；
generic 是对话侧 `DialogueIdentity.source` 的判定。API 层的同一事实是「pad 设备还没确认
到长者」——`identity_service.resolve_on_connect` 对 `devices.elder_id IS NULL` 正是直接
落 generic 线程（reason=`elder_unbound`，identity_service.py:252-258）。因此本文件把
generic 主体统一构造成 `Identity(kind="device", device_id=..., elder_id=None)`；已绑定设备
带上 `elder_id`，专门用来验证守卫没有扩大化。

## 测试怎么不污染库

请求内 `get_session` 被换成外层事务上的 savepoint（`commit()` 只释放保存点，teardown 整体
rollback）；越权审计自己开会话，`isolated_session_factory` 把它钉在同一条测试连接上 ——
「不写审计」这个断言因此查的是真实 SQL 的结果，而不是查了张空表。
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
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import health as health_api
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
    HealthRecord,
    MedicationEvent,
    MedicationPlan,
    StaffAccount,
)
from app.services import report_service

# --------------------------------------------------------------------- 固定口径

#: generic 时两个端点各自的提示语（PRD FR-VPR-03「提示需先确认身份」的落地文案）。
HEALTH_DETAIL = "请先确认身份，再记录健康数据"
MEDICATION_DETAIL = "请先确认身份，再确认用药"

#: 一份**本身完全合法**的代填载荷：generic 也拿得出这些字段，拒绝理由只能是身份。
HEALTH_PAYLOAD: dict[str, Any] = {
    "source": "staff_active",
    "systolic": 128,
    "diastolic": 78,
    "heart_rate": 72,
    "sleep_hours": 7,
    "mood_score": 4,
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
    """把越权审计的「自己开会话」钉在同一条测试连接上。"""
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
    care_worker_id: uuid.UUID
    doctor_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID
    generic_device_id: uuid.UUID
    bound_device_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    care_worker_id, doctor_id = (uuid.uuid4() for _ in range(2))
    elder_id, other_elder_id = (uuid.uuid4() for _ in range(2))
    generic_device_id, bound_device_id = (uuid.uuid4() for _ in range(2))

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": care_worker_id,
                "name": "护工王",
                "role": "care_worker",
                "password_hash": "x",
            },
            {"staff_id": doctor_id, "name": "医生李", "role": "doctor", "password_hash": "x"},
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
            {"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
            {"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"},
        ],
    )
    await db.flush()
    return Seed(
        care_worker_id=care_worker_id,
        doctor_id=doctor_id,
        elder_id=elder_id,
        other_elder_id=other_elder_id,
        generic_device_id=generic_device_id,
        bound_device_id=bound_device_id,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


@dataclass
class Bundle:
    client: AsyncClient
    #: 每次请求前改写第一项即可切换调用主体；覆盖的依赖读的是同一对象。
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    """一份应用同时挂 health 与 medication 两个 router：一次覆盖两个守卫。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(health_api.router)
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


# ------------------------------------------------------------------ 主体与断言工具


def _as_generic_device(seed: Seed) -> Identity:
    """通用模式：设备已验签，但**还没确认到任何长者**（`devices.elder_id IS NULL`）。"""
    return Identity(kind="device", device_id=str(seed.generic_device_id), elder_id=None)


def _as_bound_device(seed: Seed) -> Identity:
    """同一台 pad 绑定成功后应当得到的身份：device + elder_id。"""
    return Identity(kind="device", device_id=str(seed.bound_device_id), elder_id=str(seed.elder_id))


def _as_care_worker(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.care_worker_id), role="care_worker")


def _assert_identity_required(response: Response, detail: str) -> None:
    """403 + IDENTITY_REQUIRED + 三个键 + trace_id 与响应头一致（错误契约）。"""
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == health_api.IDENTITY_REQUIRED_CODE
    assert body["code"] == "IDENTITY_REQUIRED"
    assert body["detail"] == detail
    assert set(body) == {"detail", "code", "trace_id"}, "错误体恒为 {detail, code, trace_id}"
    header = response.headers.get("x-trace-id")
    assert header, "TraceIdMiddleware 必须回写 x-trace-id"
    assert body["trace_id"] == header, "响应体与响应头必须是同一个 trace_id"
    assert len(header) == 32


async def _health_record_count(db: AsyncSession, elder_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(HealthRecord)
                .where(HealthRecord.elder_id == elder_id)
            )
        ).scalar_one()
    )


async def _audit_rows(db: AsyncSession, action: str, **filters: Any) -> list[AuditLog]:
    """按动作 + 主体过滤：`audit_logs` 是只增表，不断言 actor 会把历史行算进来。"""
    stmt = select(AuditLog).where(AuditLog.action == action)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    return list(await db.scalars(stmt))


def _svc(name: str) -> object | None:
    """取 `medication_service` 的公开函数；与 `app/api/medication.py` 适配块同一探测口径。"""
    try:
        module = importlib.import_module("app.services.medication_service")
    except ImportError:
        return None
    candidate = getattr(module, name, None)
    return candidate if callable(candidate) else None


async def _make_pending_event(db: AsyncSession, seed: Seed) -> MedicationEvent:
    """造一条待确认事件（走服务层 `ensure_event`，与调度器同一入口，不伪造 SQL）。"""
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

    ensure_event = _svc("ensure_event")
    assert ensure_event is not None, "medication_service.ensure_event 必须已落地"
    event, _created = await ensure_event(  # type: ignore[misc]
        db,
        plan_id=plan.plan_id,
        elder_id=seed.elder_id,
        scheduled_at=datetime.datetime(2026, 11, 8, 0, 0, tzinfo=datetime.UTC),
        drug_snapshot={"drug_name": plan.drug_name, "dose": plan.dose},
        triggered_by="dialog",
    )
    return event


# ------------------------------------------------------------------ 契约本身


def test_identity_required_code_is_shared_between_the_two_endpoints() -> None:
    """两个端点必须用同一个错误码：前端只写一套「先确认身份」的处理逻辑。"""
    assert health_api.IDENTITY_REQUIRED_CODE == "IDENTITY_REQUIRED"
    assert medication_api.IDENTITY_REQUIRED_CODE == health_api.IDENTITY_REQUIRED_CODE


# ------------------------------------------------------------------ 健康：不写


async def test_generic_device_cannot_fill_health_records(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """FR-VPR-03：generic 调代填 → 403 IDENTITY_REQUIRED，且**一条记录都没落**。

    载荷本身是合法且带 `elder_id` 的 —— 拒绝理由只能是「身份没确认」，不是参数问题
    （所以是 403 不是 422，计划 §7 S-D4-2）。
    """
    client.holder[:] = [_as_generic_device(seeded)]

    response = await client.client.post(
        "/api/health/records", json={"elder_id": str(seeded.elder_id), **HEALTH_PAYLOAD}
    )

    _assert_identity_required(response, HEALTH_DETAIL)
    assert await _health_record_count(db, seeded.elder_id) == 0, "generic 不得写健康记录"


# ------------------------------------------------------------------ 用药：不确认


async def test_generic_device_cannot_confirm_medication(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """FR-VPR-03：generic 调代确认 → 403 IDENTITY_REQUIRED + 事件原封不动 + 不写审计。

    守卫挡在 `_require_roles` 之前，所以既没有越权审计行（generic 不该留痕），也没有
    读事件、改状态 —— 这三件事一起断言，才能证明守卫真的在「最前面」。
    """
    event = await _make_pending_event(db, seeded)
    pending_status = event.status
    client.holder[:] = [_as_generic_device(seeded)]

    response = await client.client.post(
        f"/api/medication/events/{event.reminder_id}/confirm", json={}
    )

    _assert_identity_required(response, MEDICATION_DETAIL)

    await db.refresh(event)
    assert event.status == pending_status
    assert event.confirm_channel is None, "generic 不得写入确认渠道"
    assert event.terminal_state is None
    assert event.confirmed_at is None
    assert (
        await _audit_rows(db, "medication.event.confirm", actor_id=seeded.generic_device_id) == []
    ), "generic 不得写审计"


# ------------------------------------------------------------------ 回归：不挡过头


async def test_bound_device_keeps_role_semantics_on_health(client: Bundle, seeded: Seed) -> None:
    """已绑定设备（device + elder_id）**不是** generic：仍按角色口径 403 ROLE_FORBIDDEN。"""
    client.holder[:] = [_as_bound_device(seeded)]

    response = await client.client.post(
        "/api/health/records", json={"elder_id": str(seeded.elder_id), **HEALTH_PAYLOAD}
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "ROLE_FORBIDDEN", "守卫不得把角色拒绝换成身份拒绝"
    assert response.json()["detail"] != HEALTH_DETAIL


async def test_bound_device_keeps_scope_semantics_on_medication(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """已绑定设备调药确认：仍是 403 SCOPE_FORBIDDEN，且**照旧**写一行越权审计。"""
    event = await _make_pending_event(db, seeded)
    client.holder[:] = [_as_bound_device(seeded)]

    response = await client.client.post(
        f"/api/medication/events/{event.reminder_id}/confirm", json={}
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "SCOPE_FORBIDDEN", "非 staff 主体走既有范围口径"
    rows = await _audit_rows(db, "medication.event.confirm", actor_id=seeded.bound_device_id)
    assert len(rows) == 1, "既有越权留痕语义不得被本次改动破坏"
    assert rows[0].actor_type == "device"
    assert rows[0].detail["reason"] == "non_staff_channel"


async def test_generic_device_reads_are_not_guarded(client: Bundle, seeded: Seed) -> None:
    """「只挡写，读接口不动」：读端点仍回角色口径，不会变成 IDENTITY_REQUIRED。"""
    client.holder[:] = [_as_generic_device(seeded)]

    response = await client.client.get(f"/api/health/records/{seeded.elder_id}")

    assert response.status_code == 403, response.text
    assert response.json()["code"] != "IDENTITY_REQUIRED", "读接口不应被写守卫波及"


async def test_care_worker_still_fills_health_records(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """正身份回归：负责护工代填照旧 201，并且真的落了一行（守卫没有误伤正常路径）。"""
    client.holder[:] = [_as_care_worker(seeded)]

    response = await client.client.post(
        "/api/health/records", json={"elder_id": str(seeded.elder_id), **HEALTH_PAYLOAD}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["grade"] == "green"
    assert body["source"] == "staff_active"
    assert await _health_record_count(db, seeded.elder_id) == 1


async def test_care_worker_still_confirms_medication(
    client: Bundle, seeded: Seed, db: AsyncSession
) -> None:
    """正身份回归：护工代确认照旧 200，渠道/终态语义不变（已服自报，不是「已服药」）。"""
    event = await _make_pending_event(db, seeded)
    client.holder[:] = [_as_care_worker(seeded)]

    response = await client.client.post(
        f"/api/medication/events/{event.reminder_id}/confirm", json={}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confirm_channel"] == "button"
    assert body["status"] == "confirmed"
    assert body["terminal_state"] == "taken_reported"
    assert body["hitl_resumed"] is False, "图未接线前必须如实返回 False"

    await db.refresh(event)
    assert event.confirm_channel == "button"
    assert event.terminal_state == "taken_reported"
