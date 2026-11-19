"""房间写接口（ADM-01，2026-11-12）的接口级测试。

补的是 `POST /api/rooms` 与 `PATCH /api/rooms/{room_id}` 的**契约边界**——此前
`tests/test_devices_api.py` 只覆盖 `GET /api/rooms`（两个用例），写口一行测试都没有。
房间是「新建设备必须拿到 `room_id`」的唯一来源，写口一旦退回「只能连库维护」，
设备生命周期又会断一次。

三条比「接口 200」更重要的断言：

1. **房号冲突忽略大小写**：`A-101` 存在时 `a-101` 必须 409，否则库里的 `UNIQUE`
   是大小写敏感的，会同时存在两个「同一间房」；
2. **空白房号不得落库**：`"   "` 经 `min_length=1` 能过 Pydantic，必须在服务层
   被拒成 422——否则房间列表里会出现一张看不见名字的房；
3. **空 PATCH 不得静默成功**：`{}` 若返回 200，改页会让人以为改动生效了。

夹具与 `tests/test_devices_api.py` 同构（真 PostgreSQL、每用例一事务、结束回滚、
无库即 fail）：ASGI 请求与测试事务必须共用同一事件循环与同一连接。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import rooms as rooms_api
from app.core.config import get_settings
from app.core.security import sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Room, StaffAccount
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "rooms-api-test-secret-not-a-real-key-0123456789"

# ------------------------------------------------------------------ 固定 ID（全部虚构）

ROOM_A = uuid.UUID("cf000000-0000-4000-8000-00000000000a")
ROOM_B = uuid.UUID("cf000000-0000-4000-8000-00000000000b")
ROOM_MISSING = uuid.UUID("cf000000-0000-4000-8000-0000000009ff")

S_ADMIN = uuid.UUID("cfd00000-0000-4000-8000-000000000001")
S_CARE = uuid.UUID("cfd00000-0000-4000-8000-000000000002")

# `password_hash` 在鉴权链路里只做 NOT NULL 占位，不跑 bcrypt。
_PASSWORD_PLACEHOLDER = "rooms-test-row-not-a-real-hash"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """注入测试密钥；结束后清 Settings 缓存，避免把密钥泄漏给同进程后续文件。"""
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
    """每用例一个事务、结束回滚；连不上库按 conftest 的统一策略处置（默认 fail）。"""
    conn = await require_db_connection(db_engine)

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def client(db: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    """只装配 rooms router——本文件不碰设备与登录，少装一个就少一处无关失败。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(rooms_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db,
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

    application.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


# ------------------------------------------------------------------ 种子数据


@dataclass(frozen=True)
class World:
    admin: uuid.UUID = S_ADMIN
    care: uuid.UUID = S_CARE


@pytest.fixture
async def world(db: AsyncConnection) -> World:
    await db.execute(
        Room.__table__.insert(),
        [
            {"room_id": ROOM_A, "room_no": "A-101", "status": "full"},
            {"room_id": ROOM_B, "room_no": "B-202", "status": "partial"},
        ],
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": S_ADMIN,
                "name": "管理员",
                "role": "admin",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
            {
                "staff_id": S_CARE,
                "name": "护工一",
                "role": "care_worker",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
        ],
    )
    return World()


# ------------------------------------------------------------------ 辅助


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(staff_id: uuid.UUID, role: str) -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role=role, token_version=1)


def _admin(world: World) -> dict[str, str]:
    return _bearer(_staff_token(world.admin, "admin"))


def _care(world: World) -> dict[str, str]:
    return _bearer(_staff_token(world.care, "care_worker"))


def _assert_contract_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    """断言契约错误体：状态码 + code + detail + 非空 32 位 trace_id。"""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


def _assert_validation_error(response: Response) -> None:
    """入参校验失败统一落契约错误体 `{detail, code, trace_id}`。

    实测：本仓库的 `RequestValidationError` 处理器同样把 Pydantic 的逐字段列表
    收敛成 `detail="request validation failed"`，与 `room_service` 抛
    `RoomValidationError` 走的 422 是**同一个形状**——所以这里不该再断言
    `detail` 是列表（那会把「服务层拒绝」与「入参格式错」拆成两套断言）。
    """
    _assert_contract_error(response, 422, "VALIDATION_ERROR")


# ------------------------------------------------------------------ 鉴权边界


async def test_rooms_list_requires_token(client: AsyncClient, world: World) -> None:
    """没有 token 必须是 401 而不是 404——路由存在、只是进不去。"""
    response = await client.get("/api/rooms")
    _assert_contract_error(response, 401, "TOKEN_INVALID")


async def test_create_room_rejects_care_worker(client: AsyncClient, world: World) -> None:
    """房间写口只对 admin 开；护工拿合法 token 也必须 403。"""
    response = await client.post("/api/rooms", json={"room_no": "C-303"}, headers=_care(world))
    _assert_contract_error(response, 403, "ROLE_FORBIDDEN")


# ------------------------------------------------------------------ POST /api/rooms


async def test_create_room_returns_201_with_zero_beds(client: AsyncClient, world: World) -> None:
    """新建房间 201，`bed_count` 为 0（床位归 `beds` 维护，建房间不建床）。"""
    response = await client.post("/api/rooms", json={"room_no": "C-303"}, headers=_admin(world))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["room_no"] == "C-303"
    assert body["status"] == "available"
    assert body["bed_count"] == 0
    uuid.UUID(body["room_id"])  # 必须是合法 UUID


async def test_create_room_accepts_explicit_status(client: AsyncClient, world: World) -> None:
    response = await client.post(
        "/api/rooms",
        json={"room_no": "C-404", "status": "maintenance"},
        headers=_admin(world),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "maintenance"


async def test_create_room_duplicate_is_case_insensitive(client: AsyncClient, world: World) -> None:
    """库里 A-101 已存在，提交 a-101 必须 409——否则「同一间房」会出现两份。"""
    response = await client.post("/api/rooms", json={"room_no": "a-101"}, headers=_admin(world))
    _assert_contract_error(response, 409, "CONFLICT")


async def test_create_room_blank_room_no_is_rejected(client: AsyncClient, world: World) -> None:
    """`"   "` 能过 `min_length=1`，必须在服务层被拒成 422（不是 201）。"""
    response = await client.post("/api/rooms", json={"room_no": "   "}, headers=_admin(world))
    _assert_contract_error(response, 422, "VALIDATION_ERROR")


async def test_create_room_too_long_is_rejected(client: AsyncClient, world: World) -> None:
    response = await client.post("/api/rooms", json={"room_no": "R" * 200}, headers=_admin(world))
    _assert_validation_error(response)


async def test_create_room_forbids_unknown_field(client: AsyncClient, world: World) -> None:
    """`extra="forbid"`：多传一个字段必须 422，而不是被静默丢掉。"""
    response = await client.post(
        "/api/rooms",
        json={"room_no": "C-505", "bed_count": 3},
        headers=_admin(world),
    )
    _assert_validation_error(response)


# ------------------------------------------------------------------ PATCH /api/rooms/{id}


async def test_patch_room_renames(client: AsyncClient, world: World) -> None:
    response = await client.patch(
        f"/api/rooms/{ROOM_A}", json={"room_no": "A-102"}, headers=_admin(world)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["room_id"] == str(ROOM_A)
    assert body["room_no"] == "A-102"


async def test_patch_room_status_to_maintenance(client: AsyncClient, world: World) -> None:
    response = await client.patch(
        f"/api/rooms/{ROOM_B}", json={"status": "maintenance"}, headers=_admin(world)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "maintenance"


async def test_patch_room_ignores_unset_fields(client: AsyncClient, world: World) -> None:
    """只传 status 时房号不动（PATCH 是「传了才改」）。"""
    response = await client.patch(
        f"/api/rooms/{ROOM_A}", json={"status": "partial"}, headers=_admin(world)
    )
    assert response.status_code == 200, response.text
    assert response.json()["room_no"] == "A-101"


async def test_patch_room_empty_body_is_rejected(client: AsyncClient, world: World) -> None:
    """`{}` 不能静默成功——改页会让人以为改动生效了。"""
    response = await client.patch(f"/api/rooms/{ROOM_A}", json={}, headers=_admin(world))
    _assert_validation_error(response)


async def test_patch_room_missing_is_404(client: AsyncClient, world: World) -> None:
    response = await client.patch(
        f"/api/rooms/{ROOM_MISSING}", json={"room_no": "X-999"}, headers=_admin(world)
    )
    _assert_contract_error(response, 404, "NOT_FOUND")


async def test_patch_room_rename_conflict_is_409(client: AsyncClient, world: World) -> None:
    """把 A-101 改成已存在的 B-202（或大小写变体）必须 409。"""
    response = await client.patch(
        f"/api/rooms/{ROOM_A}", json={"room_no": "b-202"}, headers=_admin(world)
    )
    _assert_contract_error(response, 409, "CONFLICT")


async def test_patch_room_invalid_status_is_rejected(client: AsyncClient, world: World) -> None:
    response = await client.patch(
        f"/api/rooms/{ROOM_A}", json={"status": "closed"}, headers=_admin(world)
    )
    _assert_validation_error(response)


async def test_patch_room_requires_admin(client: AsyncClient, world: World) -> None:
    response = await client.patch(
        f"/api/rooms/{ROOM_A}", json={"room_no": "A-777"}, headers=_care(world)
    )
    _assert_contract_error(response, 403, "ROLE_FORBIDDEN")
