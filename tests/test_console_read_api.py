"""照护台 W4 后端读接口的接口级测试（四个端点）。

覆盖：
- 四个端点的正常路径：分页字段回显、手机号脱敏、`bound` 派生值、绑定时间；
- 三个 admin 端点的非 admin 403 `ROLE_FORBIDDEN`；
- 分页越界（`page<1`、`page_size` 越界）→ 422 `VALIDATION_ERROR`；
- 长者-家属读接口的范围口径：护工/医生范围内 200、范围外 403 `SCOPE_FORBIDDEN`、
  长者不存在 404 `NOT_FOUND`、家属主体 403、无 token 401；
- 敏感字段：响应里没有 `password` / `token_hash` / 任何完整 11 位手机号；
- `GET /api/elders/{elder_id}/guardians` 回显的 `binding_id` 就是绑定行主键，
  取它去打 `DELETE /api/bindings/{binding_id}` 能真删掉这一条（前端解绑入口的闭环）。

跑法与 `test_staff_api.py` 同构：自建 NullPool 引擎、每用例一个事务回滚、请求走真实
ASGI 链路（`get_session` / `current_staff` / `role` 全真实）。无 PostgreSQL 时由
conftest 的 `require_db_connection()` 直接 fail（`ANYU_ALLOW_DB_SKIP=1` 才 skip）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import devices as devices_api
from app.api import guardians as guardians_api
from app.api import staff as staff_api
from app.core.config import get_settings
from app.core.security import hash_password, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    Bed,
    Device,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    Room,
    StaffAccount,
)
from conftest import require_db_connection

TEST_JWT_SECRET = "console-read-test-secret-not-a-real-key-0123456789abcdef"

ADMIN_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa01")
CAREWORKER_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa02")
DOCTOR_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa03")
GUARDIAN_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccc01")
ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1")
OTHER_ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2")
ROOM_ID = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffff01")
BED_ID = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffff02")
DEVICE_BOUND_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddd01")
DEVICE_FREE_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddd02")

ADMIN_PHONE = "13700001001"
CAREWORKER_PHONE = "13700001002"
DOCTOR_PHONE = "13700001003"
GUARDIAN_PHONE = "13700001004"

#: 设备长期令牌哈希：只入库、永不出现在响应里（`sign_device_session` 取前 16 位做 `tv`）
BOUND_TOKEN_HASH = "a" * 64
FREE_TOKEN_HASH = "b" * 64

LAST_HEARTBEAT = datetime.datetime(2026, 11, 7, 9, 30, tzinfo=datetime.UTC)


# ---------------------------------------------------------------- 夹具


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
    """一套最小照护台数据：三名员工、一位家属、两位长者、一间房一张床、两台设备。

    `ELDER_ID` 绑给护工（primary_care）与医生（doctor），家属已绑；`OTHER_ELDER_ID`
    谁都没绑 —— 用来验证「范围外 403 而不是空列表」。全部随事务回滚。
    """
    session = AsyncSession(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    # 分三段 flush：先账号/长者/房间，再床位与设备（外键依赖前者），最后关系表。
    # 一次 add_all 交给 unit of work 排序会踩 fk_beds_room_id_rooms，显式分段最稳。
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
                staff_id=CAREWORKER_ID,
                name="测试护工",
                role="care_worker",
                phone=CAREWORKER_PHONE,
                password_hash=hash_password("Care@2026"),
                status="active",
            ),
            StaffAccount(
                staff_id=DOCTOR_ID,
                name="测试医生",
                role="doctor",
                phone=DOCTOR_PHONE,
                password_hash=hash_password("Doctor@2026"),
                status="active",
            ),
            GuardianAccount(
                guardian_id=GUARDIAN_ID,
                name="测试家属",
                phone=GUARDIAN_PHONE,
                password_hash=hash_password("Guardian@2026"),
                status="active",
                token_version=0,
            ),
            Elder(elder_id=ELDER_ID, name="测试长者", status="active"),
            Elder(elder_id=OTHER_ELDER_ID, name="范围外长者", status="active"),
            Room(room_id=ROOM_ID, room_no="A-101", status="partial"),
        ]
    )
    await session.flush()

    session.add_all(
        [
            Bed(bed_id=BED_ID, room_id=ROOM_ID, bed_no="1", status="occupied", elder_id=ELDER_ID),
            Device(
                device_id=DEVICE_BOUND_ID,
                room_id=ROOM_ID,
                elder_id=ELDER_ID,
                token_hash=BOUND_TOKEN_HASH,
                status="online",
                last_heartbeat=LAST_HEARTBEAT,
            ),
            Device(
                device_id=DEVICE_FREE_ID,
                room_id=ROOM_ID,
                elder_id=None,
                token_hash=FREE_TOKEN_HASH,
                status="offline",
                last_heartbeat=None,
            ),
        ]
    )
    await session.flush()

    session.add_all(
        [
            ElderStaffAssignment(
                elder_id=ELDER_ID, staff_id=CAREWORKER_ID, relation="primary_care"
            ),
            ElderStaffAssignment(elder_id=ELDER_ID, staff_id=DOCTOR_ID, relation="doctor"),
            ElderGuardian(
                elder_id=ELDER_ID,
                guardian_id=GUARDIAN_ID,
                relation="子",
                status="active",
            ),
        ]
    )
    await session.commit()
    return db


@pytest.fixture
async def client(seeded: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    """三个 router 挂在同一个 app 上：读接口分布在 staff/guardians/devices 三个模块。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(staff_api.router)
    application.include_router(guardians_api.router)
    application.include_router(devices_api.router)

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

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _token(role: str, staff_id: uuid.UUID) -> str:
    return sign_staff_access(kind="staff", staff_id=str(staff_id), role=role, token_version=0)


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token('admin', ADMIN_ID)}"}


def _careworker_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token('care_worker', CAREWORKER_ID)}"}


def _doctor_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token('doctor', DOCTOR_ID)}"}


def _guardian_headers() -> dict[str, str]:
    """家属令牌不带 role（契约 §1：家属不是员工角色）。"""
    token = sign_staff_access(kind="guardian", staff_id=str(GUARDIAN_ID), role="", token_version=0)
    return {"Authorization": f"Bearer {token}"}


def _assert_contract_error(resp: Response, *, status: int, code: str) -> dict[str, object]:
    """契约 §7 错误体：恰三键、`detail` 非空字符串、`trace_id` 32 位。"""
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"detail", "code", "trace_id"}
    assert body["code"] == code
    assert isinstance(body["detail"], str) and body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32
    return body


def _assert_no_sensitive(text: str) -> None:
    """响应文本里不得出现口令字段、令牌哈希或任何完整手机号。"""
    assert "password" not in text
    assert "token_hash" not in text
    for phone in (ADMIN_PHONE, CAREWORKER_PHONE, DOCTOR_PHONE, GUARDIAN_PHONE):
        assert phone not in text


# ---------------------------------------------------------------- GET /api/staff


async def test_staff_list_returns_paged_items(client: AsyncClient) -> None:
    resp = await client.get("/api/staff", headers=_admin_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["total"] >= 3
    assert {item["staff_id"] for item in body["items"]} >= {
        str(ADMIN_ID),
        str(CAREWORKER_ID),
        str(DOCTOR_ID),
    }

    care_worker = next(i for i in body["items"] if i["staff_id"] == str(CAREWORKER_ID))
    assert set(care_worker) == {
        "staff_id",
        "name",
        "role",
        "phone_masked",
        "status",
        "elder_ids",
    }
    assert care_worker["role"] == "care_worker"
    assert care_worker["phone_masked"] == "137****1002"
    # 护工的绑定长者随列表一起返回（一次查询取齐，不是 N+1）
    assert care_worker["elder_ids"] == [str(ELDER_ID)]

    admin = next(i for i in body["items"] if i["staff_id"] == str(ADMIN_ID))
    assert admin["elder_ids"] == []
    _assert_no_sensitive(resp.text)


async def test_staff_list_filters_by_role(client: AsyncClient, seeded: AsyncConnection) -> None:
    resp = await client.get("/api/staff", params={"role": "doctor"}, headers=_admin_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 开发库里有产品负责人手工 seed 的账号，doctor 不止本夹具一位：total 对齐库里 doctor 的
    # 真实条数（基线 + 本夹具增量），再单独校验「返回的全是医生」且本夹具这位在结果里。
    doctors = int(
        (
            await seeded.execute(
                select(func.count()).select_from(StaffAccount).where(StaffAccount.role == "doctor")
            )
        ).scalar_one()
    )
    assert body["total"] == doctors
    assert {item["role"] for item in body["items"]} == {"doctor"}
    assert str(DOCTOR_ID) in {item["staff_id"] for item in body["items"]}


async def test_staff_list_rejects_non_admin(client: AsyncClient) -> None:
    resp = await client.get("/api/staff", headers=_careworker_headers())

    _assert_contract_error(resp, status=403, code="ROLE_FORBIDDEN")


async def test_staff_list_page_bounds_are_422(client: AsyncClient) -> None:
    too_large = await client.get("/api/staff", params={"page_size": 101}, headers=_admin_headers())
    _assert_contract_error(too_large, status=422, code="VALIDATION_ERROR")

    zero_page = await client.get("/api/staff", params={"page": 0}, headers=_admin_headers())
    _assert_contract_error(zero_page, status=422, code="VALIDATION_ERROR")


# ---------------------------------------------------------------- GET /api/guardians


async def test_guardian_list_masks_phone(client: AsyncClient) -> None:
    resp = await client.get("/api/guardians", headers=_admin_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "total", "page", "page_size"}
    # 该端点不按范围过滤，库里可能另有迁移种子数据，只校验本夹具那一条。
    assert body["total"] == len(body["items"])
    assert body["total"] >= 1
    item = next(i for i in body["items"] if i["guardian_id"] == str(GUARDIAN_ID))
    assert set(item) == {"guardian_id", "name", "phone_masked", "status"}
    assert item["phone_masked"] == "137****1004"
    assert item["status"] == "active"
    _assert_no_sensitive(resp.text)


async def test_guardian_list_filters_by_phone(client: AsyncClient) -> None:
    hit = await client.get(
        "/api/guardians", params={"phone": GUARDIAN_PHONE}, headers=_admin_headers()
    )
    assert hit.status_code == 200, hit.text
    assert hit.json()["total"] == 1
    assert hit.json()["items"][0]["guardian_id"] == str(GUARDIAN_ID)

    miss = await client.get(
        "/api/guardians", params={"phone": "13700001999"}, headers=_admin_headers()
    )
    assert miss.status_code == 200, miss.text
    assert miss.json()["total"] == 0
    assert miss.json()["items"] == []


async def test_guardian_list_rejects_non_admin(client: AsyncClient) -> None:
    resp = await client.get("/api/guardians", headers=_doctor_headers())

    _assert_contract_error(resp, status=403, code="ROLE_FORBIDDEN")


async def test_guardian_list_page_bounds_are_422(client: AsyncClient) -> None:
    resp = await client.get("/api/guardians", params={"page_size": 0}, headers=_admin_headers())

    _assert_contract_error(resp, status=422, code="VALIDATION_ERROR")


# ---------------------------------------------------------------- GET /api/devices


async def test_device_list_reports_binding_state(client: AsyncClient) -> None:
    resp = await client.get("/api/devices", headers=_admin_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "total", "page", "page_size"}
    assert body["total"] >= 2

    bound = next(i for i in body["items"] if i["device_id"] == str(DEVICE_BOUND_ID))
    assert set(bound) == {
        "device_id",
        "elder_id",
        "elder_name",
        "room_no",
        "bed_no",
        "bound",
        "last_seen_at",
        # S3（2026-11-13）：设备可用性单独一列，与「是否绑定」是两个概念。
        "status",
    }
    assert bound["bound"] is True
    assert bound["elder_id"] == str(ELDER_ID)
    assert bound["elder_name"] == "测试长者"
    assert bound["room_no"] == "A-101"
    assert bound["bed_no"] == "1"
    assert bound["last_seen_at"] is not None

    free = next(i for i in body["items"] if i["device_id"] == str(DEVICE_FREE_ID))
    assert free["bound"] is False
    assert free["elder_id"] is None
    assert free["elder_name"] is None
    assert free["last_seen_at"] is None
    _assert_no_sensitive(resp.text)


async def test_device_list_filters_by_bound(client: AsyncClient) -> None:
    only_bound = await client.get(
        "/api/devices", params={"bound": "true"}, headers=_admin_headers()
    )
    assert only_bound.status_code == 200, only_bound.text
    assert all(item["bound"] for item in only_bound.json()["items"])
    assert str(DEVICE_FREE_ID) not in {i["device_id"] for i in only_bound.json()["items"]}

    only_free = await client.get(
        "/api/devices", params={"bound": "false"}, headers=_admin_headers()
    )
    assert only_free.status_code == 200, only_free.text
    assert all(not item["bound"] for item in only_free.json()["items"])
    assert str(DEVICE_BOUND_ID) not in {i["device_id"] for i in only_free.json()["items"]}


async def test_device_list_rejects_non_admin(client: AsyncClient) -> None:
    resp = await client.get("/api/devices", headers=_careworker_headers())

    _assert_contract_error(resp, status=403, code="ROLE_FORBIDDEN")


async def test_device_list_page_bounds_are_422(client: AsyncClient) -> None:
    resp = await client.get("/api/devices", params={"page": 0}, headers=_admin_headers())

    _assert_contract_error(resp, status=422, code="VALIDATION_ERROR")


# ------------------------------------------- GET /api/elders/{elder_id}/guardians


async def test_elder_guardians_admin_sees_all(client: AsyncClient) -> None:
    resp = await client.get(f"/api/elders/{ELDER_ID}/guardians", headers=_admin_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "total"}
    assert body["total"] == 1
    item = body["items"][0]
    assert set(item) == {
        "guardian_id",
        "binding_id",
        "name",
        "phone_masked",
        "relation",
        "bound_at",
    }
    assert item["guardian_id"] == str(GUARDIAN_ID)
    # binding_id 是绑定行主键（elder_guardians.id），不是家属账号 ID：必须可解析成
    # UUID，且与 guardian_id 不同——前端拿它去 DELETE，混用就会删错对象。
    binding_id = uuid.UUID(item["binding_id"])
    assert str(binding_id) == item["binding_id"]
    assert binding_id != GUARDIAN_ID
    assert item["relation"] == "子"
    assert item["phone_masked"] == "137****1004"
    assert isinstance(item["bound_at"], str) and item["bound_at"]
    _assert_no_sensitive(resp.text)


async def test_elder_guardians_care_worker_in_scope(client: AsyncClient) -> None:
    resp = await client.get(f"/api/elders/{ELDER_ID}/guardians", headers=_careworker_headers())

    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1


async def test_elder_guardians_care_worker_out_of_scope(client: AsyncClient) -> None:
    resp = await client.get(
        f"/api/elders/{OTHER_ELDER_ID}/guardians", headers=_careworker_headers()
    )

    _assert_contract_error(resp, status=403, code="SCOPE_FORBIDDEN")


async def test_elder_guardians_doctor_out_of_scope(client: AsyncClient) -> None:
    resp = await client.get(f"/api/elders/{OTHER_ELDER_ID}/guardians", headers=_doctor_headers())

    _assert_contract_error(resp, status=403, code="SCOPE_FORBIDDEN")


async def test_elder_guardians_unknown_elder_is_404(client: AsyncClient) -> None:
    """admin 不受范围限制，所以「不存在」才轮到 404（范围外主体永远先吃 403）。"""
    resp = await client.get(f"/api/elders/{uuid.uuid4()}/guardians", headers=_admin_headers())

    _assert_contract_error(resp, status=404, code="NOT_FOUND")


async def test_elder_guardians_guardian_principal_forbidden(client: AsyncClient) -> None:
    resp = await client.get(f"/api/elders/{ELDER_ID}/guardians", headers=_guardian_headers())

    _assert_contract_error(resp, status=403, code="SCOPE_FORBIDDEN")


async def test_elder_guardians_missing_token_401(client: AsyncClient) -> None:
    resp = await client.get(f"/api/elders/{ELDER_ID}/guardians")

    _assert_contract_error(resp, status=401, code="TOKEN_INVALID")


async def test_binding_id_from_list_is_the_unbind_key(client: AsyncClient) -> None:
    """列表回显的 `binding_id` 必须能直接解绑那一条（本轮补字段的唯一目的）。

    解绑本身的语义（204 / 保痕 / 409 / 403）由 `tests/test_guardian_unbind_api.py`
    覆盖，这里只锁「两个端点用的是同一个 ID」，不重复那套用例。
    """
    listing = await client.get(f"/api/elders/{ELDER_ID}/guardians", headers=_admin_headers())
    assert listing.status_code == 200, listing.text
    binding_id = listing.json()["items"][0]["binding_id"]

    resp = await client.request("DELETE", f"/api/bindings/{binding_id}", headers=_admin_headers())
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    # 解绑保痕（status='unbound'）：列表只认 active，所以这位长者名下应清空。
    after = await client.get(f"/api/elders/{ELDER_ID}/guardians", headers=_admin_headers())
    assert after.status_code == 200, after.text
    assert after.json() == {"items": [], "total": 0}
