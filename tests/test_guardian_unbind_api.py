"""批B · 家属解绑接口（`DELETE /api/bindings/{binding_id}`）的接口级测试。

覆盖：
- 鉴权：无 token 401 `TOKEN_INVALID`、非 admin 403 `ROLE_FORBIDDEN`；
- 解绑成功 204 且**无响应体**；绑定行被置 `status='unbound'`（AC-24 保痕，**不删行**）；
- 审计：`action='guardian.unbind'`、`resource_type='elder_guardians'`、`resource_id=elder_id`，
  `detail` 带 binding_id / guardian_id / reason；
- 幂等口径：重复解绑 409 `CONFLICT`，且**不写第二条审计**（审计记「发生过解绑」）；
- `binding_id` 不存在 404 `NOT_FOUND`；
- 解绑后**该家属的有效范围里不再有这位长者**：打真路由
  `GET /api/reports/{elder_id}/list`，解绑前 200、解绑后 403 `SCOPE_FORBIDDEN`
  （家属范围唯一来源是 `elder_service.guardian_elder_ids` → `status='active'`，
  与 `tests/test_family_reports.py` 的越权用例同一口径）。

跑法与 `test_guardians_api.py` 同构：NullPool + 每用例事务回滚 + 真实 ASGI 链路 +
真 JWT（不 override 身份，否则 401/403 的顺序就测不出来了）。
无 PostgreSQL 时由 conftest 的 `require_db_connection()` 直接 fail（`ANYU_ALLOW_DB_SKIP=1` 才 skip）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import guardians as guardians_api
from app.api import reports as reports_api
from app.core.config import get_settings
from app.core.security import hash_password, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, Elder, ElderGuardian, GuardianAccount, StaffAccount
from app.services import report_service
from conftest import require_db_connection

TEST_JWT_SECRET = "guardian-unbind-api-test-secret-not-a-real-key-0123456789"

ADMIN_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbd1")
WORKER_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbd2")
ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee11")
GUARDIAN_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccc11")
BINDING_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddd11")

ADMIN_PHONE = "13800000011"


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """连不上库按 conftest 的统一策略处置（默认 fail，`ANYU_ALLOW_DB_SKIP=1` 才 skip）。"""
    conn = await require_db_connection(db_engine)

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def seeded(db: AsyncConnection) -> AsyncConnection:
    """一位 admin、一位护工、一位长者、一位家属，以及一条 active 绑定。"""
    session = AsyncSession(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session.add_all(
        [
            StaffAccount(
                staff_id=ADMIN_ID,
                name="测试管理员",
                role="admin",
                phone=ADMIN_PHONE,
                password_hash=hash_password("Admin@2026"),
                status="active",
            ),
            StaffAccount(
                staff_id=WORKER_ID,
                name="测试护工",
                role="care_worker",
                phone="13800000012",
                password_hash=hash_password("Care@2026"),
                status="active",
            ),
            Elder(elder_id=ELDER_ID, name="测试长者", status="active"),
            GuardianAccount(
                guardian_id=GUARDIAN_ID,
                name="家属张",
                phone="13900000031",
                password_hash=hash_password("Fam@2026aa"),
                status="active",
            ),
            ElderGuardian(
                id=BINDING_ID,
                elder_id=ELDER_ID,
                guardian_id=GUARDIAN_ID,
                relation="daughter",
                status="active",
            ),
        ]
    )
    await session.commit()
    return db


@pytest.fixture
async def client(
    seeded: AsyncConnection, jwt_env: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(guardians_api.router)
    # 范围断言必须打在真路由上（Q-01 的「解绑后还能读日报」就是这条链路），
    # 所以把日报读接口一起挂上，而不是自己造一个受 scope 保护的假端点。
    application.include_router(reports_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=seeded,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    application.dependency_overrides[get_session] = override_get_session

    # 日报读权限审计走独立会话（`report_service._write_audit`）；不重定向就会去连
    # 真实连接池、把行提交到用例事务之外。绑到本用例的连接上，随回滚一起消失。
    factory = async_sessionmaker(
        bind=seeded, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(report_service, "session_factory", factory)

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _admin_headers() -> dict[str, str]:
    token = sign_staff_access(kind="staff", staff_id=str(ADMIN_ID), role="admin", token_version=0)
    return {"Authorization": f"Bearer {token}"}


def _worker_headers() -> dict[str, str]:
    token = sign_staff_access(
        kind="staff", staff_id=str(WORKER_ID), role="care_worker", token_version=0
    )
    return {"Authorization": f"Bearer {token}"}


def _guardian_headers() -> dict[str, str]:
    token = sign_staff_access(kind="guardian", staff_id=str(GUARDIAN_ID), role="", token_version=0)
    return {"Authorization": f"Bearer {token}"}


def _unbind(client: AsyncClient, binding_id: uuid.UUID, **kwargs: str) -> Response:
    return client.request(
        "DELETE", f"/api/bindings/{binding_id}", headers=_admin_headers(), params=kwargs
    )


async def _binding_status(db: AsyncConnection) -> str:
    session = AsyncSession(bind=db, expire_on_commit=False)
    row = (
        await session.execute(select(ElderGuardian).where(ElderGuardian.id == BINDING_ID))
    ).scalar_one()
    return row.status


async def _unbind_audits(db: AsyncConnection) -> list[AuditLog]:
    session = AsyncSession(bind=db, expire_on_commit=False)
    return list(
        (await session.execute(select(AuditLog).where(AuditLog.action == "guardian.unbind")))
        .scalars()
        .all()
    )


# ---------------------------------------------------------------- 鉴权


async def test_unbind_requires_token(client: AsyncClient) -> None:
    resp = await client.delete(f"/api/bindings/{BINDING_ID}")
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_INVALID"


async def test_unbind_forbidden_for_non_admin(client: AsyncClient) -> None:
    resp = await client.delete(f"/api/bindings/{BINDING_ID}", headers=_worker_headers())
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


# ---------------------------------------------------------------- 解绑成功


async def test_unbind_marks_unbound_without_deleting_row(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    resp = await _unbind(client, BINDING_ID, reason="家属本人申请")
    assert resp.status_code == 204
    assert resp.content == b""

    # 解绑保痕（AC-24）：行还在，只是状态变了。
    assert await _binding_status(seeded) == "unbound"

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    rows = (
        (await session.execute(select(ElderGuardian).where(ElderGuardian.id == BINDING_ID)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_unbind_writes_audit_with_elder_as_resource(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    resp = await _unbind(client, BINDING_ID, reason="家属本人申请")
    assert resp.status_code == 204

    audits = await _unbind_audits(seeded)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.actor_type == "staff"
    assert audit.actor_id == ADMIN_ID
    assert audit.resource_type == "elder_guardians"
    assert audit.resource_id == ELDER_ID
    detail = audit.detail
    assert detail["binding_id"] == str(BINDING_ID)
    assert detail["elder_id"] == str(ELDER_ID)
    assert detail["guardian_id"] == str(GUARDIAN_ID)
    assert detail["status"] == "unbound"
    assert detail["reason"] == "家属本人申请"


# ---------------------------------------------------------------- 幂等口径


async def test_unbind_twice_conflicts_and_does_not_duplicate_audit(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    first = await _unbind(client, BINDING_ID)
    assert first.status_code == 204

    second = await _unbind(client, BINDING_ID)
    assert second.status_code == 409
    assert second.json()["code"] == "CONFLICT"

    assert await _binding_status(seeded) == "unbound"
    # 审计只记「发生过解绑」一次：重复请求是状态冲突，不是第二次解绑。
    assert len(await _unbind_audits(seeded)) == 1


async def test_unbind_unknown_binding_returns_404(client: AsyncClient) -> None:
    resp = await _unbind(client, uuid.uuid4())
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 解绑后的范围


async def test_unbound_guardian_loses_elder_scope(client: AsyncClient) -> None:
    headers = _guardian_headers()

    before = await client.get(f"/api/reports/{ELDER_ID}/list", headers=headers)
    assert before.status_code == 200
    assert before.json()["total"] == 0

    unbind = await _unbind(client, BINDING_ID)
    assert unbind.status_code == 204

    after = await client.get(f"/api/reports/{ELDER_ID}/list", headers=headers)
    assert after.status_code == 403
    assert after.json()["code"] == "SCOPE_FORBIDDEN"
