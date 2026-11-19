"""W3：家属能读到自己绑定长者的日报（裁决 A-01 / 契约 O6′）。

这一组用例锁四件事，每一条都能单独被打回：

- **绑定了就能读**：`elder_guardians.status='active'` → 200，且成功读取也落审计
  （FR-RPT-02 要的是"谁看过"，不只是"谁被拦过"）；
- **没绑定读不到**：403 `SCOPE_FORBIDDEN` + 审计（`reason='scope'`, `bound=False`）；
- **只放行日报**：家属读**档案**（`GET /api/elders`）仍然 403 —— 这是 A-01 的
  核心，放开日报不等于放开档案，任何"顺手也开一下档案"的改动都必须在这里变红；
- **解绑即失效**：`status` 改成 `'unbound'` 后**立刻** 403（`unbound` 是解绑保痕，
  不是"仍可读的历史绑定"；AC-24）。

设备 token 与员工的行为一并钉住：设备一律 403；医生只能读自己负责的长者，
admin 全量，护理员连角色都没有（`ROLE_FORBIDDEN`）——W3 没有顺手放宽任何一条。

## 测试怎么不污染库

照抄 `tests/test_reports_api.py`：业务读写走 `db` 外层事务 + savepoint 覆盖
`get_session`；家属审计自己开会话（`report_service.session_factory`），所以
`isolated_session_factory` 把那个工厂钉在同一条测试连接上，teardown 整体 rollback。

不连库时 `require_db_connection` **fail 而不是 skip**（P0-1 口径）。
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import elders as elders_api
from app.api import reports as reports_api
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    AuditLog,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    StaffAccount,
)
from app.scheduler import health as health_jobs
from app.services import report_service

#: 业务自然日（北京）。与调度任务生成日报同一口径。
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
    """把"自己开会话"的家属审计钉在同一条测试连接上。"""
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
    device_id: uuid.UUID
    elder_id: uuid.UUID
    other_elder_id: uuid.UUID
    day: datetime.date


async def _seed(db: AsyncSession) -> Seed:
    admin_id, doctor_id, care_worker_id = (uuid.uuid4() for _ in range(3))
    guardian_id, device_id, elder_id, other_elder_id = (uuid.uuid4() for _ in range(4))
    day = TODAY

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
    # 医生只负责 elder，护理员负责另一位：两条范围都不许因为 W3 被放宽。
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            {"elder_id": elder_id, "staff_id": doctor_id, "relation": "doctor"},
            {"elder_id": other_elder_id, "staff_id": care_worker_id, "relation": "primary_care"},
        ],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [{"guardian_id": guardian_id, "name": "家属张", "password_hash": "x", "status": "active"}],
    )
    # 唯一一条绑定：guardian ↔ elder（张桂芬），other_elder 未绑定。
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
    return Seed(
        admin_id=admin_id,
        doctor_id=doctor_id,
        care_worker_id=care_worker_id,
        guardian_id=guardian_id,
        device_id=device_id,
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
    #: 每次请求前 `append` 就能切换调用主体；覆盖依赖读的是同一对象。
    holder: list[Identity]


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    # 同时挂 elders：A-01 的"档案仍 403"必须打在真路由上，而不是自己造一个。
    application.include_router(reports_api.router)
    application.include_router(elders_api.router)

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


def _as_device(seed: Seed) -> Identity:
    return Identity(kind="device", device_id=str(seed.device_id))


async def _guardian_audits(db: AsyncSession, seed: Seed) -> list[AuditLog]:
    rows = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.actor_type == "guardian",
                AuditLog.actor_id == seed.guardian_id,
                AuditLog.resource_type == "daily_report",
            )
        )
    ).scalars()
    return list(rows.all())


# ------------------------------------------------------------------ 家属：绑定内


async def test_guardian_reads_bound_elder_report(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """绑定家属读日报 200，且成功读取也留痕（不只是越权才留）。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_id"] == str(seeded.elder_id)
    assert body["report_date"] == seeded.day.isoformat()
    assert isinstance(body["payload"], dict) and body["payload"], "payload 不许是空壳"
    # 响应字段一个字都不许因为 W3 变（契约 O6′：家属读到的就是员工读到的那个形状）。
    assert set(body["payload"]) == {"elder_id", "report_date", "health", "medication", "sources"}

    audits = await _guardian_audits(db, seeded)
    assert len(audits) == 1, "成功读取也必须留痕（FR-RPT-02）"
    assert audits[0].action == "report.read"
    assert audits[0].resource_id == seeded.elder_id
    assert audits[0].detail["bound"] is True
    assert audits[0].detail["reason"] == "guardian_binding"


async def test_report_carries_elder_display_name(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """日报响应带展示名（契约 A-01 替代方案「日报接口自带展示名」）。

    没有这个字段，家属端只能把 `elder_id` 当标题显示——那正是 A-01 留的出口
    被堵死的样子。名字取自 `elders.name`（读取时取，不是生成时快照），
    且 `payload` 形状一个字段都不许多（展示名不进 payload，见 `DailyReportView` 注释）。
    """
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    expected = (
        await db.execute(select(Elder.name).where(Elder.elder_id == seeded.elder_id))
    ).scalar_one()
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_name"] == expected, "展示名必须等于 elders.name"
    assert body["elder_name"] != str(seeded.elder_id), "展示名不许退化成 UUID"
    # 展示名是响应字段，不进 payload —— payload 仍是生成时的快照形状。
    assert "elder_name" not in body["payload"]

    listed = await client.client.get(f"/api/reports/{seeded.elder_id}/list", params={"days": 7})
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items and all(item["elder_name"] == expected for item in items)


async def test_guardian_lists_bound_elder_reports(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`/list` 与 `/` 走同一道闸：绑定家属可读，action 记 `report.list`。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get(f"/api/reports/{seeded.elder_id}/list", params={"days": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["report_id"]

    audits = await _guardian_audits(db, seeded)
    assert [row.action for row in audits] == ["report.list"]
    assert audits[0].detail["bound"] is True


# ----------------------------------------------------------------- 家属：绑定外


async def test_guardian_reads_unbound_elder_forbidden_and_audited(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """未绑定的长者 403 `SCOPE_FORBIDDEN`，且越权同样要留痕。"""
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.other_elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "SCOPE_FORBIDDEN"
    assert len(body["trace_id"]) == 32
    assert "李振华" not in json.dumps(body, ensure_ascii=False), "越权不能漏长者姓名"

    audits = await _guardian_audits(db, seeded)
    assert len(audits) == 1, "越权必须留痕（FR-RPT-02）"
    assert audits[0].action == "report.read"
    assert audits[0].resource_id == seeded.other_elder_id
    assert audits[0].detail["bound"] is False
    assert audits[0].detail["reason"] == "scope"


async def test_guardian_still_cannot_read_elder_archive(
    db: AsyncSession, seeded: Seed, client: Bundle
) -> None:
    """A-01 不许松：日报放行 ≠ 档案放行，`GET /api/elders` 对家属仍然 403。"""
    client.holder.append(_as_guardian(seeded))

    listing = await client.client.get("/api/elders")
    assert listing.status_code == 403, listing.text
    body = listing.json()
    assert body["code"] == "SCOPE_FORBIDDEN"
    assert "items" not in body
    assert "张桂芬" not in json.dumps(body, ensure_ascii=False)

    detail = await client.client.get(f"/api/elders/{seeded.elder_id}")
    assert detail.status_code == 403, detail.text
    assert detail.json()["code"] == "SCOPE_FORBIDDEN"


async def test_unbinding_revokes_report_access_immediately(
    db: AsyncSession,
    seeded: Seed,
    client: Bundle,
    isolated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """解绑（status→'unbound'）后立刻 403：不存在"绑定快照"式缓存。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_guardian(seeded))

    ok = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert ok.status_code == 200, ok.text

    await db.execute(
        update(ElderGuardian)
        .where(
            ElderGuardian.elder_id == seeded.elder_id,
            ElderGuardian.guardian_id == seeded.guardian_id,
        )
        .values(status="unbound")
    )
    await db.flush()

    denied = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "SCOPE_FORBIDDEN"

    audits = await _guardian_audits(db, seeded)
    assert [row.detail["bound"] for row in audits] == [True, False]


# ------------------------------------------------------- 设备 / 员工：行为不变


async def test_device_cannot_read_reports(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    """设备 token 读日报一律 403：W3 只给家属开了口子，设备没有。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_device(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"

    guardian_rows = await _guardian_audits(db, seeded)
    assert guardian_rows == [], "设备越权不属于家属审计，不能冒名留痕"


async def test_staff_report_scope_unchanged(db: AsyncSession, seeded: Seed, client: Bundle) -> None:
    """员工口径一个字都没动：admin 全量、doctor 按负责范围、护理员角色就不许。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await report_service.generate(db, seeded.other_elder_id, seeded.day)
    await db.flush()
    day = seeded.day.isoformat()

    client.holder.append(_as_admin(seeded))
    for elder_id in (seeded.elder_id, seeded.other_elder_id):
        resp = await client.client.get(f"/api/reports/{elder_id}", params={"date": day})
        assert resp.status_code == 200, resp.text

    client.holder[0] = _as_doctor(seeded)
    own = await client.client.get(f"/api/reports/{seeded.elder_id}", params={"date": day})
    assert own.status_code == 200, own.text
    foreign = await client.client.get(f"/api/reports/{seeded.other_elder_id}", params={"date": day})
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "SCOPE_FORBIDDEN"

    client.holder[0] = _as_care_worker(seeded)
    denied = await client.client.get(f"/api/reports/{seeded.elder_id}", params={"date": day})
    assert denied.status_code == 403
    assert denied.json()["code"] == "ROLE_FORBIDDEN"
