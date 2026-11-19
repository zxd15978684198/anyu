"""W3 · 退房 API（FR-ADM-09）集成测试.

真实 DB + httpx.AsyncClient，夹具口径同 `tests/test_staff_card_api.py`（NullPool 引擎、
一用例一事务、seed 只 flush，请求共享同一 session）。**只连独立测试库**
（`ANYU_TEST_DATABASE_URL`），否则 conftest 的 `require_db_connection` 会拦下。

覆盖 task-3 验收 4：
1. admin 退房 → 200，五个面同时收口：`devices.elder_id` 清空 + 置 offline + 轮换
   `token_hash`、`staff_tokens` 清空、`elders.status='away'`、一条 `elder.checkout` 审计；
2. **旧设备令牌立即失效**：退房后拿旧 secret 换设备会话 → 401 `TOKEN_REVOKED`；
3. 非 admin → 403 `ROLE_FORBIDDEN`；长者不存在 → 404；
4. `archive=true` → `elders.status='archived'`，`archived_at` 进审计。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password, sha256_hex
from app.db import get_session as _app_get_session
from app.models import ElderStaffAssignment, StaffAccount, StaffToken
from app.models.organization import Bed, Device, Elder, Room

_ADMIN_PHONE = "13800000021"
_CARE_PHONE = "13800000022"
_PASSWORD = "test123"
_DEVICE_SECRET = "w3-checkout-device-secret"
_OLD_DEVICE_HASH = sha256_hex(_DEVICE_SECRET)

_TRUNCATE = (
    "TRUNCATE audit_logs, staff_tokens, elder_staff_assignments, staff_accounts, "
    "devices, elders, beds, personas, rooms CASCADE"
)


@pytest.fixture
async def test_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE))
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def test_session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sf = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sf() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def seeded(test_session: AsyncSession) -> dict[str, Any]:
    room = Room(room_no="B-301")
    test_session.add(room)
    await test_session.flush()

    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(room_id=room.room_id, token_hash=_OLD_DEVICE_HASH, status="online")
    elder = Elder(name="周桂兰", status="active")
    admin = StaffAccount(
        name="王管理",
        role="admin",
        phone=_ADMIN_PHONE,
        password_hash=hash_password(_PASSWORD),
        token_version=0,
        status="active",
    )
    care = StaffAccount(
        name="赵护工",
        role="care_worker",
        phone=_CARE_PHONE,
        password_hash=hash_password(_PASSWORD),
        token_version=0,
        status="active",
    )
    test_session.add_all([bed, device, elder, admin, care])
    await test_session.flush()

    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    test_session.add(
        ElderStaffAssignment(
            elder_id=elder.elder_id, staff_id=care.staff_id, relation="primary_care"
        )
    )
    # 该设备上的一张工牌：退房必须连带吊销，否则「人走了、卡还能用」
    test_session.add(
        StaffToken(
            token_hash="w3-card-on-checkout-device",
            staff_id=care.staff_id,
            device_id=device.device_id,
            scope="workorder:handle",
            expires_at=text("now() + interval '1 hour'"),  # type: ignore[arg-type]
        )
    )
    await test_session.flush()

    return {
        "elder_id": elder.elder_id,
        "device_id": device.device_id,
        "room_id": room.room_id,
        "admin_id": admin.staff_id,
        "care_id": care.staff_id,
    }


@pytest.fixture
async def app_for_test(test_session: AsyncSession, seeded: dict[str, Any]) -> FastAPI:
    from fastapi.responses import JSONResponse

    from app.api.auth import router as auth_router
    from app.api.elders import router as elders_router
    from app.core.auth import (
        CredentialExpiredError,
        CredentialInvalidError,
        CredentialRevokedError,
    )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(elders_router)

    async def _override_get_session():
        yield test_session

    app.dependency_overrides[_app_get_session] = _override_get_session

    @app.exception_handler(CredentialInvalidError)
    async def _invalid(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"},
        )

    @app.exception_handler(CredentialExpiredError)
    async def _expired(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(CredentialRevokedError)
    async def _revoked(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    return app


@pytest.fixture
async def ac(app_for_test: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_for_test)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _login(ac: httpx.AsyncClient, phone: str) -> str:
    resp = await ac.post("/api/sessions", json={"phone": phone, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _checkout(
    ac: httpx.AsyncClient, *, token: str, elder_id: uuid.UUID, body: dict[str, Any] | None = None
) -> httpx.Response:
    return await ac.post(
        f"/api/elders/{elder_id}/checkout",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar_one_or_none()


async def test_admin_checkout_releases_room_and_devices(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any]
) -> None:
    token = await _login(ac, _ADMIN_PHONE)

    resp = await _checkout(ac, token=token, elder_id=seeded["elder_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_status"] == "away"
    assert body["archived"] is False
    assert body["devices_unbound"] == 1
    assert body["staff_cards_revoked"] == 1
    assert body["audit_log_id"] is not None

    # ① 设备解绑 + 置 offline + 令牌哈希轮换
    row = (
        await test_session.execute(
            text("SELECT elder_id, status, token_hash FROM devices WHERE device_id = :d"),
            {"d": seeded["device_id"]},
        )
    ).one()
    assert row[0] is None
    assert row[1] == "offline"
    assert row[2] != _OLD_DEVICE_HASH

    # ② 该设备上的工牌被吊销
    cards = await _scalar(
        test_session,
        "SELECT count(*) FROM staff_tokens WHERE device_id = :d",
        d=seeded["device_id"],
    )
    assert cards == 0

    # ③ 房态置 away（各调度链路的在场判据）
    status = await _scalar(
        test_session, "SELECT status FROM elders WHERE elder_id = :e", e=seeded["elder_id"]
    )
    assert status == "away"

    # ④ 一条 elder.checkout 审计，detail 带设备面计数
    detail = await _scalar(
        test_session,
        "SELECT detail FROM audit_logs WHERE action = 'elder.checkout' AND resource_id = :e",
        e=seeded["elder_id"],
    )
    assert detail["devices_unbound"] == 1
    assert detail["staff_cards_revoked"] == 1
    assert detail["elder_status"] == "away"
    assert detail["reason"] == "checkout"


async def test_old_device_secret_is_rejected_after_checkout(
    ac: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    """退房轮换 `token_hash` = 旧设备令牌当场作废（不是等 24h 自然过期）。

    轮换后旧 secret 与库里的 `token_hash` 不再匹配，`auth_service` 走的是
    「密文比对失败」分支 → 401 `TOKEN_INVALID`（等同作废；不区分该码是为了不把
    「猜对了 id 但猜错 secret」和「令牌已轮换」暴露给调用方）。
    """
    token = await _login(ac, _ADMIN_PHONE)
    assert (await _checkout(ac, token=token, elder_id=seeded["elder_id"])).status_code == 200

    resp = await ac.post(
        "/api/device-sessions",
        json={"device_id": str(seeded["device_id"]), "device_secret": _DEVICE_SECRET},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] in {"TOKEN_INVALID", "TOKEN_REVOKED"}


async def test_checkout_requires_admin(ac: httpx.AsyncClient, seeded: dict[str, Any]) -> None:
    token = await _login(ac, _CARE_PHONE)
    resp = await _checkout(ac, token=token, elder_id=seeded["elder_id"])
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_checkout_of_absent_elder_returns_404(
    ac: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    token = await _login(ac, _ADMIN_PHONE)
    resp = await _checkout(ac, token=token, elder_id=uuid.uuid4())
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


async def test_checkout_archive_sets_archived(
    ac: httpx.AsyncClient, test_session: AsyncSession, seeded: dict[str, Any]
) -> None:
    token = await _login(ac, _ADMIN_PHONE)
    resp = await _checkout(
        ac, token=token, elder_id=seeded["elder_id"], body={"archive": True, "reason": "transfer"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["elder_status"] == "archived"
    assert resp.json()["archived"] is True

    status = await _scalar(
        test_session, "SELECT status FROM elders WHERE elder_id = :e", e=seeded["elder_id"]
    )
    assert status == "archived"
    detail = await _scalar(
        test_session,
        "SELECT detail FROM audit_logs WHERE action = 'elder.checkout' AND resource_id = :e",
        e=seeded["elder_id"],
    )
    assert detail["reason"] == "transfer"
    assert detail["archived_at"] is not None
