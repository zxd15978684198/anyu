"""设备生命周期补齐（E12 / E13 / E14，2026-11-11）的接口级测试。

覆盖 `设计记录（设备绑定补齐）` 的批次 B2–B4 三个新端点：

- **E12 `GET /api/rooms`** —— 新建设备必须能拿到 `room_id`，此前全仓没有接口能返回它；
- **E13 `POST /api/devices/{id}/credential`** —— 解绑后凭证丢失没有任何找回通道；
- **E14 `POST /api/devices`** —— 能解绑却不能新建，设备生命周期是断的。

三条契约级断言（比「接口 200」更重要）：

1. `device_secret` 明文**只在响应里出现一次**：库里只有 SHA-256，审计日志里既无明文也无哈希；
2. E13 的语义是「先解绑再签发」：调完之后 `elder_id` 必须为空、`status` 必须是 `offline`，
   否则现场分不清「凭证问题」还是「绑定问题」；
3. 新设备一律 `offline`，不得自称 `online`——没有心跳的 online 只会让照护台显示假在线。

夹具与 `tests/test_elders_api.py` 同构（真 PostgreSQL、每用例一事务、结束回滚、无库即 fail），
理由见该文件 §模块 docstring：task-3 的模块级连接池会把上一个事件循环的 asyncpg 连接
留给下一个用例，而这里同样需要「测试事务」与「ASGI 请求」共用一个事件循环。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import auth as auth_api
from app.api import devices as devices_api
from app.api import rooms as rooms_api
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.security import KIND_STAFF, sha256_hex, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, Bed, Device, Elder, Room, StaffAccount
from app.services import device_service
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "devices-lifecycle-test-secret-not-a-real-key-0123456789"

# ------------------------------------------------------------------ 固定 ID（全部虚构）

ROOM_A = uuid.UUID("cc000000-0000-4000-8000-00000000000a")
ROOM_B = uuid.UUID("cc000000-0000-4000-8000-00000000000b")
ROOM_EMPTY = uuid.UUID("cc000000-0000-4000-8000-0000000000ee")
ROOM_MISSING = uuid.UUID("cc000000-0000-4000-8000-0000000009ff")

BED_A1 = uuid.UUID("cbed0000-0000-4000-8000-0000000000a1")
BED_A2 = uuid.UUID("cbed0000-0000-4000-8000-0000000000a2")
BED_B1 = uuid.UUID("cbed0000-0000-4000-8000-0000000000b1")

ELDER_A1 = uuid.UUID("ce000000-0000-4000-8000-0000000000a1")
ELDER_A2 = uuid.UUID("ce000000-0000-4000-8000-0000000000a2")
ELDER_B1 = uuid.UUID("ce000000-0000-4000-8000-0000000000b1")

S_ADMIN = uuid.UUID("cd000000-0000-4000-8000-000000000001")
S_CARE = uuid.UUID("cd000000-0000-4000-8000-000000000002")
S_DOCTOR = uuid.UUID("cd000000-0000-4000-8000-000000000003")

DEVICE_BOUND = uuid.UUID("cde00000-0000-4000-8000-000000000001")
DEVICE_FREE = uuid.UUID("cde00000-0000-4000-8000-000000000002")
DEVICE_MISSING = uuid.UUID("cde00000-0000-4000-8000-0000000009ff")

SECRET_BOUND = "device-bound-secret-not-a-real-value"
SECRET_FREE = "device-free-secret-not-a-real-value"

# `password_hash` 在鉴权链路里只做 NOT NULL 占位，不跑 bcrypt。
_PASSWORD_PLACEHOLDER = "devices-lifecycle-test-row-not-a-real-hash"


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
    """临时装配 devices + rooms + auth 三个 router。

    auth 是必需的：`test_issue_credential_invalidates_old_secret_at_login` 要验证
    「重发凭证之后旧 secret 在校验入口被拒」，而那个入口就是 `POST /api/device-sessions`。
    本文件的 auth router 按 `app/api/__init__.py` 的注册口径补 `/api` 前缀（路由内只写 `/sessions` 等）。

    请求与测试事务共用同一事件循环与同一连接。
    """
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(devices_api.router)
    application.include_router(rooms_api.router)
    application.include_router(auth_api.router, prefix="/api")

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
    """两间有人的房 + 一间空房、3 位长者、3 名员工、2 台设备。"""

    admin: uuid.UUID = S_ADMIN
    care: uuid.UUID = S_CARE
    doctor: uuid.UUID = S_DOCTOR
    device_bound: uuid.UUID = DEVICE_BOUND
    device_free: uuid.UUID = DEVICE_FREE


@pytest.fixture
async def world(db: AsyncConnection) -> World:
    await db.execute(
        Room.__table__.insert(),
        [
            {"room_id": ROOM_A, "room_no": "A-101", "status": "full"},
            {"room_id": ROOM_B, "room_no": "B-202", "status": "partial"},
            {"room_id": ROOM_EMPTY, "room_no": "Z-999", "status": "available"},
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": ELDER_A1, "name": "张桂芬", "status": "active"},
            {"elder_id": ELDER_A2, "name": "李建国", "status": "active"},
            {"elder_id": ELDER_B1, "name": "王秀兰", "status": "active"},
        ],
    )
    await db.execute(
        Bed.__table__.insert(),
        [
            {
                "bed_id": BED_A1,
                "room_id": ROOM_A,
                "bed_no": "1 号床",
                "status": "occupied",
                "elder_id": ELDER_A1,
            },
            {
                "bed_id": BED_A2,
                "room_id": ROOM_A,
                "bed_no": "2 号床",
                "status": "occupied",
                "elder_id": ELDER_A2,
            },
            {
                "bed_id": BED_B1,
                "room_id": ROOM_B,
                "bed_no": "1 号床",
                "status": "occupied",
                "elder_id": ELDER_B1,
            },
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
            {
                "staff_id": S_DOCTOR,
                "name": "医生一",
                "role": "doctor",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
        ],
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": DEVICE_BOUND,
                "room_id": ROOM_A,
                "elder_id": ELDER_A1,
                "token_hash": sha256_hex(SECRET_BOUND),
                "status": "online",
            },
            {
                "device_id": DEVICE_FREE,
                "room_id": ROOM_B,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_FREE),
                "status": "offline",
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


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    """断言契约错误体：状态码 + code + detail + 非空 32 位 trace_id。"""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


async def _device_row(db: AsyncConnection, device_id: uuid.UUID) -> object:
    return (
        await db.execute(
            select(Device.room_id, Device.elder_id, Device.token_hash, Device.status).where(
                Device.device_id == device_id
            )
        )
    ).one()


def _audit_records(caplog: pytest.LogCaptureFixture, action: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "audit_action", None) == action]


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """把整条日志（含 extra 字段）拼成一段文本，用于「明文没被写进日志」的断言。"""
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        for key, value in vars(record).items():
            if key.startswith("_"):
                continue
            parts.append(f"{key}={value}")
    return "\n".join(parts)


# ------------------------------------------------------------------ E12 · 房间清单


async def test_rooms_admin_lists_rooms_with_bed_count(client: AsyncClient, world: World) -> None:
    """E12：admin 能拿到房间清单与床位数（新建设备所需的最小信息）。"""
    response = await client.get("/api/rooms", headers=_admin(world))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    by_no = {item["room_no"]: item for item in body["items"]}
    assert set(by_no) == {"A-101", "B-202", "Z-999"}
    assert by_no["A-101"]["bed_count"] == 2
    assert by_no["B-202"]["bed_count"] == 1
    assert by_no["Z-999"]["bed_count"] == 0
    assert by_no["A-101"]["room_id"] == str(ROOM_A)
    # 按 room_no 排序，前端下拉框顺序稳定
    assert [item["room_no"] for item in body["items"]] == ["A-101", "B-202", "Z-999"]


async def test_rooms_are_admin_only(client: AsyncClient, world: World) -> None:
    """护工与医生不能列房间：与 `GET /api/devices` 同一权限面，避免半开状态。"""
    for staff_id, role in ((world.care, "care_worker"), (world.doctor, "doctor")):
        response = await client.get("/api/rooms", headers=_bearer(_staff_token(staff_id, role)))
        _assert_error(response, 403, "ROLE_FORBIDDEN")


# ------------------------------------------------------------------ E14 · 新建设备


async def test_create_device_returns_secret_once_and_persists_offline(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E14：201 + 明文凭证；库里只有哈希，且状态必须是 offline。"""
    response = await client.post(
        "/api/devices",
        json={"room_id": str(ROOM_EMPTY)},
        headers=_admin(world),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"device_id", "room_id", "elder_id", "device_secret", "status"}
    assert body["room_id"] == str(ROOM_EMPTY)
    assert body["elder_id"] is None
    # 没连过线就不许自称在线：假 online 只会让照护台显示一个永不心跳的设备。
    assert body["status"] == "offline"
    secret = body["device_secret"]
    assert isinstance(secret, str) and len(secret) >= 32

    created_id = uuid.UUID(body["device_id"])
    row = await _device_row(db, created_id)
    assert row.room_id == ROOM_EMPTY
    assert row.elder_id is None
    assert row.status == "offline"
    # 明文不落库：库里只存 SHA-256，且能与回显的明文对上。
    assert row.token_hash == sha256_hex(secret)
    assert row.token_hash != secret


async def test_create_device_with_elder_in_room_binds_immediately(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E14 可选 `elder_id`：长者必须在该房间的床位里。"""
    response = await client.post(
        "/api/devices",
        json={"room_id": str(ROOM_B), "elder_id": str(ELDER_B1)},
        headers=_admin(world),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["elder_id"] == str(ELDER_B1)
    row = await _device_row(db, uuid.UUID(body["device_id"]))
    assert row.elder_id == ELDER_B1


async def test_create_device_rejects_elder_from_another_room(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """他房长者不能顺手绑进这台设备；失败必须无部分写入。"""
    response = await client.post(
        "/api/devices",
        json={"room_id": str(ROOM_EMPTY), "elder_id": str(ELDER_A1)},
        headers=_admin(world),
    )

    _assert_error(response, 409, "CONFLICT")
    rows = (await db.execute(select(Device.device_id).where(Device.room_id == ROOM_EMPTY))).all()
    assert rows == []


async def test_create_device_unknown_room_is_conflict(client: AsyncClient, world: World) -> None:
    """房间不是路径资源：缺失归 409（与 E9 绑定同口径），不是 404。"""
    response = await client.post(
        "/api/devices",
        json={"room_id": str(ROOM_MISSING)},
        headers=_admin(world),
    )
    _assert_error(response, 409, "CONFLICT")


async def test_create_device_requires_admin(client: AsyncClient, world: World) -> None:
    response = await client.post(
        "/api/devices",
        json={"room_id": str(ROOM_EMPTY)},
        headers=_bearer(_staff_token(world.care, "care_worker")),
    )
    _assert_error(response, 403, "ROLE_FORBIDDEN")


async def test_create_device_forbids_extra_fields(client: AsyncClient, world: World) -> None:
    """`extra="forbid"`：夹带 `device_id` 之类字段直接 422，不给「顺手指定主键」的后门。"""
    response = await client.post(
        "/api/devices",
        json={"room_id": str(ROOM_EMPTY), "device_id": str(DEVICE_FREE)},
        headers=_admin(world),
    )
    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_create_device_never_logs_plaintext_secret(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """凭证明文不许进日志（含 extra 字段）：这是「只回显一次」的实现边界。"""
    caplog.set_level(logging.DEBUG)
    response = await client.post(
        "/api/devices", json={"room_id": str(ROOM_EMPTY)}, headers=_admin(world)
    )
    assert response.status_code == 201
    secret = response.json()["device_secret"]

    log_text = _all_log_text(caplog)
    assert secret not in log_text
    assert sha256_hex(secret) not in log_text
    # 审计确实落了，只是不带敏感值
    assert [r for r in _audit_records(caplog, "device.create") if r.result == "success"]


# ------------------------------------------------------------------ E13 · 重发凭证


async def test_issue_credential_rotates_and_clears_binding(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """E13：新凭证生效、旧凭证作废、长者绑定被清空、状态置 offline（裁决 D2-A）。"""
    before = await _device_row(db, DEVICE_BOUND)
    assert before.elder_id == ELDER_A1 and before.status == "online"

    response = await client.post(f"/api/devices/{DEVICE_BOUND}/credential", headers=_admin(world))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"device_id", "device_secret", "rotated_at"}
    assert body["device_id"] == str(DEVICE_BOUND)
    new_secret = body["device_secret"]
    assert isinstance(new_secret, str) and len(new_secret) >= 32
    assert new_secret != SECRET_BOUND

    after = await _device_row(db, DEVICE_BOUND)
    # 先解绑再签发：绑定必须清空，否则排查时分不清是凭证问题还是绑定问题。
    assert after.elder_id is None
    assert after.status == "offline"
    assert after.token_hash == sha256_hex(new_secret)
    assert after.token_hash != before.token_hash


async def test_issue_credential_invalidates_old_secret_at_login(
    client: AsyncClient, world: World
) -> None:
    """旧凭证必须立即失效——这是「解绑即失效」之外的另一条失效入口。"""
    old_login = await client.post(
        "/api/device-sessions",
        json={"device_id": str(DEVICE_BOUND), "device_secret": SECRET_BOUND},
    )
    assert old_login.status_code == 200, old_login.text

    issued = await client.post(f"/api/devices/{DEVICE_BOUND}/credential", headers=_admin(world))
    new_secret = issued.json()["device_secret"]

    stale = await client.post(
        "/api/device-sessions",
        json={"device_id": str(DEVICE_BOUND), "device_secret": SECRET_BOUND},
    )
    _assert_error(stale, 401, "TOKEN_INVALID")

    fresh = await client.post(
        "/api/device-sessions",
        json={"device_id": str(DEVICE_BOUND), "device_secret": new_secret},
    )
    assert fresh.status_code == 200, fresh.text


async def test_issue_credential_missing_device_is_404(client: AsyncClient, world: World) -> None:
    response = await client.post(f"/api/devices/{DEVICE_MISSING}/credential", headers=_admin(world))
    _assert_error(response, 404, "NOT_FOUND")


async def test_issue_credential_requires_admin(client: AsyncClient, world: World) -> None:
    for staff_id, role in ((world.care, "care_worker"), (world.doctor, "doctor")):
        response = await client.post(
            f"/api/devices/{DEVICE_BOUND}/credential",
            headers=_bearer(_staff_token(staff_id, role)),
        )
        _assert_error(response, 403, "ROLE_FORBIDDEN")


async def test_issue_credential_never_logs_plaintext_secret(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = await client.post(f"/api/devices/{DEVICE_BOUND}/credential", headers=_admin(world))
    assert response.status_code == 200
    secret = response.json()["device_secret"]

    log_text = _all_log_text(caplog)
    assert secret not in log_text
    assert sha256_hex(secret) not in log_text
    assert [r for r in _audit_records(caplog, "device.credential") if r.result == "success"]


# --------------------------------------------------- 审计入库（FR-ADM-04 / AC-24）

#: `audit_logs` 行的只读形状（裸 `AsyncConnection` 上 `select(AuditLog)` 拿不到 ORM 实体，
#: 只能取列元组）。
AuditRow = Any


async def _audit_rows(
    db: AsyncConnection, action: str, *, resource_id: uuid.UUID | None = None
) -> list[AuditRow]:
    """真库读 `audit_logs`：凭证类变更必须能在库里追到人，不拿 caplog 当证据。

    同一个动作在同一个用例里可能有多条（被拒绝的尝试也会留痕，见 `_audit` 的分流），
    所以断言「真正生效的那一条」时统一再用 `_success_rows` 过滤。
    """
    stmt = select(
        AuditLog.actor_type,
        AuditLog.actor_id,
        AuditLog.action,
        AuditLog.resource_type,
        AuditLog.resource_id,
        AuditLog.detail,
        AuditLog.created_at,
    ).where(AuditLog.action == action)
    if resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    return list((await db.execute(stmt)).all())


async def _success_rows(
    db: AsyncConnection, action: str, *, resource_id: uuid.UUID
) -> list[AuditRow]:
    rows = await _audit_rows(db, action, resource_id=resource_id)
    return [row for row in rows if row.detail and row.detail.get("result") == "success"]


async def test_unbind_writes_audit_row(
    client: AsyncClient, world: World, db: AsyncConnection
) -> None:
    """设备解绑：一条 success 审计行，能答「谁/何时/对哪台/为什么」。"""
    response = await client.post(f"/api/devices/{DEVICE_BOUND}/unbind", headers=_admin(world))
    assert response.status_code == 200, response.text

    rows = await _success_rows(db, "device.unbind", resource_id=DEVICE_BOUND)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.actor_type == "staff"
    assert row.actor_id == world.admin
    assert row.resource_type == "devices"
    assert row.created_at is not None
    assert row.detail["actor"] == f"staff:{world.admin}"
    assert row.detail["target"] == f"device:{DEVICE_BOUND}"
    assert row.detail.get("trace_id")


async def test_issue_credential_writes_audit_row(
    client: AsyncClient, world: World, db: AsyncConnection
) -> None:
    """长期令牌轮换：库里留一条审计，且审计里不出现明文 secret 或它的哈希。"""
    response = await client.post(f"/api/devices/{DEVICE_BOUND}/credential", headers=_admin(world))
    assert response.status_code == 200, response.text
    secret = response.json()["device_secret"]

    rows = await _success_rows(db, "device.credential", resource_id=DEVICE_BOUND)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.actor_type == "staff"
    assert row.actor_id == world.admin
    assert row.resource_type == "devices"

    serialized = str(row.detail)
    assert secret not in serialized
    assert sha256_hex(secret) not in serialized


async def test_create_device_writes_audit_row(
    client: AsyncClient, world: World, db: AsyncConnection
) -> None:
    """新建设备（当场签发凭证）同样是一次凭证签发，必须留痕。"""
    response = await client.post(
        "/api/devices", json={"room_id": str(ROOM_EMPTY)}, headers=_admin(world)
    )
    assert response.status_code == 201, response.text
    created = uuid.UUID(response.json()["device_id"])

    rows = await _success_rows(db, "device.create", resource_id=created)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.actor_type == "staff" and row.actor_id == world.admin
    assert row.resource_type == "devices"


async def test_disable_device_writes_audit_row_and_locks(world: World, db: AsyncConnection) -> None:
    """设备禁用（`status='locked'`）：写库 + 审计带理由，第二次禁用不改动已记的痕迹。"""
    session = AsyncSession(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    identity = Identity(kind=KIND_STAFF, staff_id=str(world.admin), role="admin")
    try:
        binding = await device_service.disable_device(
            session, identity, DEVICE_BOUND, reason="lost"
        )
        assert binding.device_id == DEVICE_BOUND

        row = await _device_row(db, DEVICE_BOUND)
        assert row.status == "locked"

        rows = await _success_rows(db, "device.disable", resource_id=DEVICE_BOUND)
        assert len(rows) == 1, rows
        assert rows[0].actor_type == "staff"
        assert rows[0].actor_id == world.admin
        assert rows[0].resource_type == "devices"
        assert rows[0].detail["reason"] == "lost"

        # 重复禁用：409 而不是静默成功，避免把上一轮的禁用人与理由盖掉。
        with pytest.raises(device_service.DeviceConflictError):
            await device_service.disable_device(session, identity, DEVICE_BOUND)
        assert len(await _success_rows(db, "device.disable", resource_id=DEVICE_BOUND)) == 1
    finally:
        await session.close()


async def test_audit_failure_aborts_credential_change(
    client: AsyncClient,
    world: World,
    db: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """负例：审计写不进去时，凭证变更**必须失败**，不许「操作成功但查不到人」。

    这里是设备解绑——旧设备会话靠 `token_hash` 轮换失效，轮换成功却没有审计行，
    就等于出现了一次无人认领的凭证作废（FR-ADM-04 明确禁止）。
    """
    before = await _device_row(db, DEVICE_BOUND)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit_logs is unavailable")

    monkeypatch.setattr(device_service, "write_audit", _boom)

    with pytest.raises(RuntimeError, match="audit_logs is unavailable"):
        await client.post(f"/api/devices/{DEVICE_BOUND}/unbind", headers=_admin(world))

    after = await _device_row(db, DEVICE_BOUND)
    assert after.elder_id == before.elder_id
    assert after.token_hash == before.token_hash
    assert after.status == before.status
    assert await _audit_rows(db, "device.unbind", resource_id=DEVICE_BOUND) == []


async def test_disable_route_locks_device_and_audits(
    client: AsyncClient, world: World, db: AsyncConnection
) -> None:
    """禁用走 HTTP 全链路：200 + `status='locked'` + 库里一条 `device.disable` 审计。"""
    response = await client.post(
        f"/api/devices/{DEVICE_BOUND}/disable", json={"reason": "lost"}, headers=_admin(world)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"device_id": str(DEVICE_BOUND), "status": "locked"}

    row = await _device_row(db, DEVICE_BOUND)
    assert row.status == "locked"

    rows = await _success_rows(db, "device.disable", resource_id=DEVICE_BOUND)
    assert len(rows) == 1, rows
    assert rows[0].actor_type == "staff"
    assert rows[0].actor_id == world.admin
    assert rows[0].detail["reason"] == "lost"


async def test_disable_route_requires_admin(
    client: AsyncClient, world: World, db: AsyncConnection
) -> None:
    """非 admin 禁用：403，且设备状态不被改动、不产生成功审计。"""
    response = await client.post(
        f"/api/devices/{DEVICE_BOUND}/disable",
        headers=_bearer(_staff_token(world.care, "care_worker")),
    )
    _assert_error(response, 403, "ROLE_FORBIDDEN")

    assert (await _device_row(db, DEVICE_BOUND)).status != "locked"
    assert await _success_rows(db, "device.disable", resource_id=DEVICE_BOUND) == []
