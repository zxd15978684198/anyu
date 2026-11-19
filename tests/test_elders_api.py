"""Task 6 · 长者 CRUD 与设备绑定（接口契约 E5–E11）的接口级测试。

覆盖计划 §4 的 T01–T15，全部跑在 task-3 的真实 PostgreSQL 上：

- 每个用例一条连接、一个外层事务，结束回滚，主库零残留；
- 被测应用在文件内临时装配（不改 `app/main.py`），只 include 本卡两个 router；
- 请求走真实 ASGI 链路（`httpx.ASGITransport`），因此 `Authorization` 头、
  `get_session`、`get_current_principal`、设备 `token_hash` 校验全部是真实代码；
- 每条写操作都回查 DB，失败用例同时断言「没有部分写入」；
- 无 PostgreSQL 时由 conftest 的 `require_db_connection()` 直接 fail（`ANYU_ALLOW_DB_SKIP=1` 才 skip）。

为什么自建 NullPool 引擎而不是直接用 conftest 的 `db_conn`：task-3 夹具的模块级连接池
会把上一个事件循环的 asyncpg 连接留给下一个用例（任务板 P-03），而本文件同时需要
测试事务与 ASGI 请求处在同一事件循环。契约与 `db_conn` 相同：一事务、结束回滚、无库即 skip。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import devices as devices_api
from app.api import elders as elders_api
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.security import sha256_hex, sign_device_session, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import (
    Bed,
    Device,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    Persona,
    Room,
    StaffAccount,
)
from app.services import device_service
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "task6-test-secret-not-a-real-key-0123456789abcdef"

# ------------------------------------------------------------------ 固定 ID（全部虚构）

PERSONA_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
#: W1 形象分配用例用的第二个人设（只在用例内插入，随事务回滚）
PERSONA_ALT = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab")

ROOM_A = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1")
ROOM_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2")
ROOM_C = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3")
ROOM_D = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4")

BED_A1 = uuid.UUID("bed00001-0000-4000-8000-000000000001")
BED_A2 = uuid.UUID("bed00002-0000-4000-8000-000000000002")
BED_B1 = uuid.UUID("bed00003-0000-4000-8000-000000000003")
BED_C1 = uuid.UUID("bed00004-0000-4000-8000-000000000004")
BED_D1 = uuid.UUID("bed00005-0000-4000-8000-000000000005")

E1 = uuid.UUID("e0000001-0000-4000-8000-000000000001")
E2 = uuid.UUID("e0000002-0000-4000-8000-000000000002")
E3 = uuid.UUID("e0000003-0000-4000-8000-000000000003")
E4 = uuid.UUID("e0000004-0000-4000-8000-000000000004")
E5 = uuid.UUID("e0000005-0000-4000-8000-000000000005")
E6 = uuid.UUID("e0000006-0000-4000-8000-000000000006")
E_MISSING = uuid.UUID("e0000999-0000-4000-8000-000000000999")
D_MISSING = uuid.UUID("d0000999-0000-4000-8000-000000000999")

S_ADMIN = uuid.UUID("51000000-0000-4000-8000-000000000001")
S_CARE1 = uuid.UUID("51000000-0000-4000-8000-000000000002")
S_CARE2 = uuid.UUID("51000000-0000-4000-8000-000000000003")
S_CARE_NONE = uuid.UUID("51000000-0000-4000-8000-000000000004")
S_DOCTOR1 = uuid.UUID("51000000-0000-4000-8000-000000000005")

G_BOUND = uuid.UUID("62000000-0000-4000-8000-000000000001")
G_UNBOUND = uuid.UUID("62000000-0000-4000-8000-000000000002")

D_A = uuid.UUID("d0000001-0000-4000-8000-000000000001")
D_B = uuid.UUID("d0000002-0000-4000-8000-000000000002")
D_FREE = uuid.UUID("d0000003-0000-4000-8000-000000000003")
D_OTHER = uuid.UUID("d0000004-0000-4000-8000-000000000004")

SECRET_A = "device-secret-a-not-a-real-value"
SECRET_B = "device-secret-b-not-a-real-value"
SECRET_FREE = "device-secret-free-not-a-real-value"
SECRET_OTHER = "device-secret-other-not-a-real-value"

# `password_hash` 在鉴权链路里只做 NOT NULL 占位，不跑 bcrypt（避免每个用例白付 0.3s）。
_PASSWORD_PLACEHOLDER = "task6-test-row-not-a-real-hash"


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
    """临时装配本卡两个 router；请求与测试事务共用同一事件循环与同一连接。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(elders_api.router)
    application.include_router(devices_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        # join_transaction_mode=create_savepoint：请求内 commit 只释放保存点，
        # 外层测试事务仍可整体回滚，写操作用例才能既「看见真实落库」又零残留。
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
    """一套用于 T01–T15 的虚构机构数据。"""

    admin: uuid.UUID = S_ADMIN
    care1: uuid.UUID = S_CARE1
    care2: uuid.UUID = S_CARE2
    care_none: uuid.UUID = S_CARE_NONE
    doctor1: uuid.UUID = S_DOCTOR1
    guardian_bound: uuid.UUID = G_BOUND
    guardian_unbound: uuid.UUID = G_UNBOUND
    device_a: uuid.UUID = D_A
    device_b: uuid.UUID = D_B
    device_free: uuid.UUID = D_FREE
    device_other: uuid.UUID = D_OTHER


@pytest.fixture
async def world(db: AsyncConnection) -> World:
    """两间多人房 + 两间单人房、6 位长者、5 名员工、2 位家属、4 台设备。"""
    await db.execute(
        Persona.__table__.insert().values(
            persona_id=PERSONA_ID,
            name="小满",
            personality="温和、耐心",
            greeting="您好呀",
            voice_id="voice-demo",
        )
    )
    await db.execute(
        Room.__table__.insert(),
        [
            {"room_id": ROOM_A, "room_no": "A-301", "status": "full"},
            {"room_id": ROOM_B, "room_no": "B-201", "status": "full"},
            {"room_id": ROOM_C, "room_no": "C-101", "status": "full"},
            {"room_id": ROOM_D, "room_no": "D-401", "status": "available"},
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {
                "elder_id": E1,
                "name": "张桂芬",
                "birth_date": date(1948, 3, 12),
                "gender": "female",
                "persona_id": PERSONA_ID,
                "status": "active",
            },
            {
                "elder_id": E2,
                "name": "李建国",
                "birth_date": date(1950, 5, 20),
                "gender": "male",
                "persona_id": PERSONA_ID,
                "status": "active",
            },
            {
                "elder_id": E3,
                "name": "王秀兰",
                "birth_date": None,
                "gender": None,
                "persona_id": None,
                "status": "active",
            },
            {
                "elder_id": E4,
                "name": "陈国强",
                "birth_date": None,
                "gender": None,
                "persona_id": None,
                "status": "active",
            },
            {
                "elder_id": E5,
                "name": "刘桂英",
                "birth_date": None,
                "gender": None,
                "persona_id": None,
                "status": "active",
            },
            {
                "elder_id": E6,
                "name": "周文轩",
                "birth_date": None,
                "gender": None,
                "persona_id": None,
                "status": "archived",
            },
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
                "elder_id": E1,
            },
            {
                "bed_id": BED_A2,
                "room_id": ROOM_A,
                "bed_no": "2 号床",
                "status": "occupied",
                "elder_id": E2,
            },
            {
                "bed_id": BED_B1,
                "room_id": ROOM_B,
                "bed_no": "1 号床",
                "status": "occupied",
                "elder_id": E3,
            },
            {
                "bed_id": BED_C1,
                "room_id": ROOM_C,
                "bed_no": "1 号床",
                "status": "occupied",
                "elder_id": E4,
            },
            {
                "bed_id": BED_D1,
                "room_id": ROOM_D,
                "bed_no": "1 号床",
                "status": "occupied",
                "elder_id": E5,
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
                "staff_id": S_CARE1,
                "name": "护工一",
                "role": "care_worker",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
            {
                "staff_id": S_CARE2,
                "name": "护工二",
                "role": "care_worker",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
            {
                "staff_id": S_CARE_NONE,
                "name": "无范围护工",
                "role": "care_worker",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
            {
                "staff_id": S_DOCTOR1,
                "name": "医生一",
                "role": "doctor",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
        ],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [
            # 护工一只负责 E1；另有一条 relation=doctor 的脏关系，用于证明「不能只看 staff_id」
            {"elder_id": E1, "staff_id": S_CARE1, "relation": "primary_care"},
            {"elder_id": E5, "staff_id": S_CARE1, "relation": "doctor"},
            # 护工二只负责 E2
            {"elder_id": E2, "staff_id": S_CARE2, "relation": "primary_care"},
            # 医生一负责 E1；另有一条 primary_care 脏关系，用于证明医生只认 doctor
            {"elder_id": E1, "staff_id": S_DOCTOR1, "relation": "doctor"},
            {"elder_id": E4, "staff_id": S_DOCTOR1, "relation": "primary_care"},
        ],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [
            {
                "guardian_id": G_BOUND,
                "name": "家属甲",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
            {
                "guardian_id": G_UNBOUND,
                "name": "家属乙",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
        ],
    )
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {"elder_id": E1, "guardian_id": G_BOUND, "relation": "子", "status": "active"},
            {"elder_id": E2, "guardian_id": G_UNBOUND, "relation": "女", "status": "unbound"},
        ],
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": D_A,
                "room_id": ROOM_A,
                "elder_id": E1,
                "token_hash": sha256_hex(SECRET_A),
                "status": "online",
            },
            {
                "device_id": D_B,
                "room_id": ROOM_B,
                "elder_id": E3,
                "token_hash": sha256_hex(SECRET_B),
                "status": "online",
            },
            {
                "device_id": D_FREE,
                "room_id": ROOM_D,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_FREE),
                "status": "offline",
            },
            {
                "device_id": D_OTHER,
                "room_id": ROOM_C,
                "elder_id": E4,
                "token_hash": sha256_hex(SECRET_OTHER),
                "status": "online",
            },
        ],
    )
    return World()


# ------------------------------------------------------------------ 请求与断言辅助


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(staff_id: uuid.UUID, role: str, token_version: int = 1) -> str:
    return sign_staff_access(
        kind="staff", staff_id=staff_id, role=role, token_version=token_version
    )


def _guardian_token(guardian_id: uuid.UUID, token_version: int = 1) -> str:
    return sign_staff_access(
        kind="guardian", staff_id=guardian_id, role="", token_version=token_version
    )


def _device_token(device_id: uuid.UUID, secret: str) -> str:
    return sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    """断言契约 §7 错误体：状态码 + code + detail + 非空 trace_id。"""
    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


async def _count(db: AsyncConnection, model: object) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _elder_row(db: AsyncConnection, elder_id: uuid.UUID) -> object:
    return (
        await db.execute(
            select(
                Elder.name,
                Elder.birth_date,
                Elder.gender,
                Elder.persona_id,
                Elder.status,
                Elder.session_seq,
                Elder.checkup_start,
            ).where(Elder.elder_id == elder_id)
        )
    ).one()


async def _device_row(db: AsyncConnection, device_id: uuid.UUID) -> object:
    return (
        await db.execute(
            select(Device.room_id, Device.elder_id, Device.token_hash, Device.status).where(
                Device.device_id == device_id
            )
        )
    ).one()


def _audit_records(
    caplog: pytest.LogCaptureFixture, action: str, result: str
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "audit_action", None) == action
        and getattr(record, "result", None) == result
    ]


# ------------------------------------------------------------------ T01–T02 · 建档


async def test_t01_admin_creates_elder_and_db_persists(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    response = await client.post(
        "/api/elders",
        json={
            "name": "赵桂香",
            "birth_date": "1949-08-15",
            "gender": "female",
            "persona_id": str(PERSONA_ID),
        },
        headers=_bearer(_staff_token(world.admin, "admin")),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"elder_id", "status", "session_seq", "created_at"}
    assert body["status"] == "active"
    assert body["session_seq"] == 0

    row = await _elder_row(db, uuid.UUID(body["elder_id"]))
    assert row.name == "赵桂香"
    assert row.birth_date == date(1949, 8, 15)
    assert row.gender == "female"
    assert row.persona_id == PERSONA_ID
    assert row.status == "active"
    assert row.session_seq == 0

    # E5 明确允许重名：同名再建一次仍应 201，且两行都在。
    duplicate = await client.post(
        "/api/elders",
        json={"name": "赵桂香", "birth_date": "1949-08-15", "gender": "female"},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert duplicate.status_code == 201
    assert uuid.UUID(duplicate.json()["elder_id"]) != uuid.UUID(body["elder_id"])
    same_name = (
        await db.execute(select(func.count()).select_from(Elder).where(Elder.name == "赵桂香"))
    ).scalar_one()
    assert int(same_name) == 2


async def test_t02_non_admin_create_is_forbidden_and_db_unchanged(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    before = await _count(db, Elder)
    headers_list = [
        _bearer(_staff_token(world.care1, "care_worker")),
        _bearer(_staff_token(world.doctor1, "doctor")),
        _bearer(_guardian_token(world.guardian_bound)),
        _bearer(_device_token(world.device_a, SECRET_A)),
    ]

    for headers in headers_list:
        response = await client.post("/api/elders", json={"name": "不该落库"}, headers=headers)
        _assert_error(response, 403, "ROLE_FORBIDDEN")

    assert await _count(db, Elder) == before


# ------------------------------------------------------------------ T03 · 详情 / 列表字段与分页


async def test_t03_admin_detail_has_r4_fields_and_room_bed(
    client: AsyncClient, world: World
) -> None:
    response = await client.get(
        f"/api/elders/{E1}", headers=_bearer(_staff_token(world.admin, "admin"))
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "elder_id",
        "name",
        "birth_date",
        "gender",
        "persona_id",
        "status",
        "session_seq",
        "created_at",
        "room_no",
        "bed_no",
        "device_count",
        "guardian_count",
        # FR-HLT-05：采集时刻三件套（自定义值 / 生效值 / 来源）。
        "checkup_start",
        "checkup_start_effective",
        "checkup_start_source",
    }
    assert body["name"] == "张桂芬"
    assert body["room_no"] == "A-301"
    assert body["bed_no"] == "1 号床"
    assert body["session_seq"] == 0


async def test_t03_admin_list_trims_private_fields_and_paginates(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    # 开发库里有产品负责人手工 seed 的演示长者，全库行数不固定。断言改成「基线 + 本用例 6 位」：
    # 基线 = 库里除 E1–E6 之外的行；分页期望值按库里真实升序切片（演示行的 elder_id 会插在
    # E1–E6 中间，写死「第 2 页是 E3/E4」在有数据时必假红）。
    ours = [E1, E2, E3, E4, E5, E6]
    baseline = int(
        (
            await db.execute(
                select(func.count()).select_from(Elder).where(Elder.elder_id.not_in(ours))
            )
        ).scalar_one()
    )
    active_baseline = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Elder)
                .where(Elder.status == "active", Elder.elder_id.not_in(ours))
            )
        ).scalar_one()
    )

    response = await client.get(
        "/api/elders?page=2&page_size=2",
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == baseline + 6
    assert body["page"] == 2
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    # 列表按 elder_id 升序：第 2 页必须是库里同一升序的第 3、4 行，证明 offset 真生效。
    all_ids = (await db.execute(select(Elder.elder_id).order_by(Elder.elder_id))).scalars().all()
    assert [uuid.UUID(item["elder_id"]) for item in body["items"]] == list(all_ids[2:4])
    for item in body["items"]:
        assert set(item) == {"elder_id", "name", "room_no", "bed_no", "status"}
        assert "birth_date" not in item
        assert "gender" not in item
        assert "persona_id" not in item

    filtered = await client.get(
        "/api/elders?status=active",
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert filtered.status_code == 200
    # 本用例 6 位里 E6 是 archived，所以 active 增量是 5。
    assert filtered.json()["total"] == active_baseline + 5

    too_large = await client.get(
        "/api/elders?page_size=101", headers=_bearer(_staff_token(world.admin, "admin"))
    )
    _assert_error(too_large, 422, "VALIDATION_ERROR")

    zero_page = await client.get(
        "/api/elders?page=0", headers=_bearer(_staff_token(world.admin, "admin"))
    )
    _assert_error(zero_page, 422, "VALIDATION_ERROR")


# ------------------------------------------------------------------ T04–T07 · 范围过滤


async def test_t04_staff_relation_is_enforced(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    care = _bearer(_staff_token(world.care1, "care_worker"))
    doctor = _bearer(_staff_token(world.doctor1, "doctor"))

    # 正确 relation：护工 primary_care E1、医生 doctor E1。
    assert (await client.get(f"/api/elders/{E1}", headers=care)).status_code == 200
    assert (await client.get(f"/api/elders/{E1}", headers=doctor)).status_code == 200

    with caplog.at_level(logging.INFO):
        # 非负责：护工一没有 E2 的 primary_care；医生一没有 E2 的 doctor。
        _assert_error(await client.get(f"/api/elders/{E2}", headers=care), 403, "SCOPE_FORBIDDEN")
        _assert_error(await client.get(f"/api/elders/{E2}", headers=doctor), 403, "SCOPE_FORBIDDEN")
        # 错误 relation：医生一只有 E4 的 primary_care；护工一只有 E5 的 doctor。
        _assert_error(await client.get(f"/api/elders/{E4}", headers=doctor), 403, "SCOPE_FORBIDDEN")
        _assert_error(await client.get(f"/api/elders/{E5}", headers=care), 403, "SCOPE_FORBIDDEN")

    records = _audit_records(caplog, "elder.read", "forbidden")
    assert records, "范围越权必须留结构化审计日志"
    assert all(record.trace_id for record in records)
    assert all(record.actor and record.target for record in records)
    # 审计只允许出现 ID/动作/结果，姓名与生日不得进日志。
    assert "张桂芬" not in caplog.text
    assert "李建国" not in caplog.text


async def test_t05_out_of_scope_never_leaks_existence(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    care = _bearer(_staff_token(world.care1, "care_worker"))

    with caplog.at_level(logging.INFO):
        existing = await client.get(f"/api/elders/{E2}", headers=care)
        missing = await client.get(f"/api/elders/{E_MISSING}", headers=care)

    body_existing = _assert_error(existing, 403, "SCOPE_FORBIDDEN")
    body_missing = _assert_error(missing, 403, "SCOPE_FORBIDDEN")
    # 范围外主体不能靠 403/404 的差异判断资源是否存在：两者 detail/code 完全一致。
    assert body_existing["detail"] == body_missing["detail"]
    assert body_existing["code"] == body_missing["code"]

    # admin 查不存在的资源才是 404。
    admin = _bearer(_staff_token(world.admin, "admin"))
    _assert_error(await client.get(f"/api/elders/{E_MISSING}", headers=admin), 404, "NOT_FOUND")


async def test_t06_staff_list_only_contains_own_scope(client: AsyncClient, world: World) -> None:
    care1 = (
        await client.get("/api/elders", headers=_bearer(_staff_token(world.care1, "care_worker")))
    ).json()
    assert care1["total"] == 1
    assert [uuid.UUID(item["elder_id"]) for item in care1["items"]] == [E1]

    care2 = (
        await client.get("/api/elders", headers=_bearer(_staff_token(world.care2, "care_worker")))
    ).json()
    assert care2["total"] == 1
    assert [uuid.UUID(item["elder_id"]) for item in care2["items"]] == [E2]

    doctor = (
        await client.get("/api/elders", headers=_bearer(_staff_token(world.doctor1, "doctor")))
    ).json()
    assert doctor["total"] == 1
    assert [uuid.UUID(item["elder_id"]) for item in doctor["items"]] == [E1]

    # 无分配不是 403，而是空列表（契约 O7 / E7）。
    empty = await client.get(
        "/api/elders", headers=_bearer(_staff_token(world.care_none, "care_worker"))
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0


async def test_t07_guardian_archive_read_is_always_forbidden(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    bound = _bearer(_guardian_token(world.guardian_bound))
    unbound = _bearer(_guardian_token(world.guardian_unbound))

    with caplog.at_level(logging.INFO):
        # 已绑定 E1 与未绑定（status=unbound）E2 都必须 403 —— A-01 优先于任务卡概括文字。
        _assert_error(await client.get(f"/api/elders/{E1}", headers=bound), 403, "SCOPE_FORBIDDEN")
        _assert_error(
            await client.get(f"/api/elders/{E2}", headers=unbound), 403, "SCOPE_FORBIDDEN"
        )
        _assert_error(await client.get("/api/elders", headers=bound), 403, "SCOPE_FORBIDDEN")

    assert _audit_records(caplog, "elder.read", "forbidden")
    assert _audit_records(caplog, "elder.list", "forbidden")


# ------------------------------------------------------------------ T08 · PATCH


async def test_t08_admin_patch_persists_and_cannot_touch_session_seq(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    before = await _elder_row(db, E2)
    response = await client.patch(
        f"/api/elders/{E2}",
        json={"name": "李建华", "birth_date": "1951-01-09", "gender": "male"},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "李建华"
    assert body["room_no"] == "A-301" and body["bed_no"] == "2 号床"

    after = await _elder_row(db, E2)
    assert after.name == "李建华"
    assert after.birth_date == date(1951, 1, 9)
    assert after.gender == "male"
    assert after.session_seq == before.session_seq  # 客户端无法改会话计数器

    for headers in (
        _bearer(_staff_token(world.care1, "care_worker")),
        _bearer(_staff_token(world.doctor1, "doctor")),
        _bearer(_guardian_token(world.guardian_bound)),
        _bearer(_device_token(world.device_a, SECRET_A)),
    ):
        forbidden = await client.patch(
            f"/api/elders/{E2}", json={"name": "越权改名"}, headers=headers
        )
        _assert_error(forbidden, 403, "ROLE_FORBIDDEN")
        unchanged = await _elder_row(db, E2)
        assert unchanged.name == "李建华"
        assert unchanged.session_seq == before.session_seq

    # 显式夹带 session_seq 不是「被忽略」，而是 422（extra=forbid），且 DB 不变。
    smuggled = await client.patch(
        f"/api/elders/{E2}",
        json={"session_seq": 7},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    _assert_error(smuggled, 422, "VALIDATION_ERROR")
    assert (await _elder_row(db, E2)).session_seq == before.session_seq


async def test_t08_archived_only_allows_status_change(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    admin = _bearer(_staff_token(world.admin, "admin"))

    mixed = await client.patch(
        f"/api/elders/{E2}", json={"status": "archived", "name": "顺手改名"}, headers=admin
    )
    _assert_error(mixed, 422, "VALIDATION_ERROR")
    assert (await _elder_row(db, E2)).status == "active"
    assert (await _elder_row(db, E2)).name == "李建国"

    # 已归档的长者也不能借 PATCH 顺手改其他字段（生效状态判定）。
    parent = await client.patch(f"/api/elders/{E6}", json={"name": "不该改"}, headers=admin)
    _assert_error(parent, 422, "VALIDATION_ERROR")
    assert (await _elder_row(db, E6)).name == "周文轩"

    only_status = await client.patch(
        f"/api/elders/{E2}", json={"status": "archived"}, headers=admin
    )
    assert only_status.status_code == 200
    assert (await _elder_row(db, E2)).status == "archived"

    missing = await client.patch(
        f"/api/elders/{E_MISSING}", json={"status": "archived"}, headers=admin
    )
    _assert_error(missing, 404, "NOT_FOUND")


# --------------------------------------------------- W5 · FR-HLT-05 采集时刻自定义


async def test_w5_patch_checkup_start_persists_and_detail_echoes_effective(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """PATCH 自定义时刻 → 落库 + 详情回显「生效值 + 来源」；传 null → 回落全局默认。"""
    admin = _bearer(_staff_token(world.admin, "admin"))

    patched = await client.patch(
        f"/api/elders/{E2}", json={"checkup_start": "18:30"}, headers=admin
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["checkup_start"] == "18:30"
    assert body["checkup_start_effective"] == "18:30"
    assert body["checkup_start_source"] == "elder"
    assert (await _elder_row(db, E2)).checkup_start == "18:30"

    # 读接口回显的是**生效值**，不是「设过没有」：页面不需要自己再拼全局默认。
    detail = await client.get(f"/api/elders/{E2}", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["checkup_start_effective"] == "18:30"
    assert detail.json()["checkup_start_source"] == "elder"

    # 显式 null = 清空自定义 → 回落到全局默认，来源变 global。
    cleared = await client.patch(f"/api/elders/{E2}", json={"checkup_start": None}, headers=admin)
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["checkup_start"] is None
    assert body["checkup_start_effective"] == get_settings().health_checkup_start
    assert body["checkup_start_source"] == "global"
    assert (await _elder_row(db, E2)).checkup_start is None


async def test_w5_detail_defaults_to_global_clock_without_custom_value(
    client: AsyncClient, world: World
) -> None:
    """没设过时刻的长者：详情直接给「全局默认 + global」，前端无需自己判断。"""
    detail = await client.get(
        f"/api/elders/{E2}", headers=_bearer(_staff_token(world.admin, "admin"))
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["checkup_start"] is None
    assert body["checkup_start_effective"] == get_settings().health_checkup_start
    assert body["checkup_start_source"] == "global"


async def test_w5_patch_checkup_start_rejects_invalid_clock(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """非法时刻在入口 422（不能落到 DB CHECK 变成 500），且写不进去。"""
    admin = _bearer(_staff_token(world.admin, "admin"))

    for bad in ("25:00", "19:60", "18:3", "1830", "19:00:00", " 19:00", "19：00"):
        response = await client.patch(
            f"/api/elders/{E2}", json={"checkup_start": bad}, headers=admin
        )
        _assert_error(response, 422, "VALIDATION_ERROR")
        assert (await _elder_row(db, E2)).checkup_start is None

    # 边界值必须收下：00:00 与 23:59 是合法时刻。
    for good in ("00:00", "23:59"):
        response = await client.patch(
            f"/api/elders/{E2}", json={"checkup_start": good}, headers=admin
        )
        assert response.status_code == 200, response.text
        assert (await _elder_row(db, E2)).checkup_start == good


async def test_w5_patch_checkup_start_is_admin_only(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """采集时刻是档案字段：护工/医生/家属/设备一律 403，且 DB 不变。"""
    for headers in (
        _bearer(_staff_token(world.care1, "care_worker")),
        _bearer(_staff_token(world.doctor1, "doctor")),
        _bearer(_guardian_token(world.guardian_bound)),
        _bearer(_device_token(world.device_a, SECRET_A)),
    ):
        response = await client.patch(
            f"/api/elders/{E2}", json={"checkup_start": "18:30"}, headers=headers
        )
        _assert_error(response, 403, "ROLE_FORBIDDEN")
    assert (await _elder_row(db, E2)).checkup_start is None


# ------------------------------------------------------------------ T09–T11 · 设备绑定


async def test_t09_bind_success_persists_room_and_elder(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    response = await client.post(
        f"/api/devices/{world.device_free}/bind",
        json={"room_id": str(ROOM_D), "elder_id": str(E5)},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["device_id"]) == world.device_free
    assert uuid.UUID(body["room_id"]) == ROOM_D
    assert uuid.UUID(body["elder_id"]) == E5

    row = await _device_row(db, world.device_free)
    assert row.room_id == ROOM_D
    assert row.elder_id == E5


async def test_t10_second_device_binding_same_elder_conflicts_without_partial_write(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    response = await client.post(
        f"/api/devices/{world.device_free}/bind",
        json={"room_id": str(ROOM_A), "elder_id": str(E1)},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    _assert_error(response, 409, "CONFLICT")

    free_row = await _device_row(db, world.device_free)
    assert free_row.room_id == ROOM_D  # 请求里的 room 没有先落下
    assert free_row.elder_id is None
    assert (await _device_row(db, world.device_a)).elder_id == E1


async def test_t10_unique_index_is_the_last_line_of_defence_and_rollback_recovers(
    db: AsyncConnection, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """绕过 service 预检查，直接让 flush 撞 `uq_devices_elder_id`。

    T10 的接口用例证明「第二台设备绑同一长者 → 409 且无半写入」，但它在预检查处就返回了，
    唯一索引与 `_flush_or_conflict` 的 rollback 并没有被执行。这里把设备对象的 `elder_id`
    直接改成已绑长者，绕开预检查，验证数据库仍是最后一道防线，且冲突后 session 可继续用
    （没有留下 failed transaction）。
    """
    identity = Identity(kind="staff", staff_id=str(world.admin), role="admin")
    # 设备行数基线：开发库另有演示设备，写死 == 4 会假红；这里要证明的是「撞唯一索引
    # 并 rollback 之后没有留下半写入」，所以比的是本用例开始时的条数。
    devices_before = await _count(db, Device)
    session = AsyncSession(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        device = await session.get(Device, world.device_free)
        assert device is not None
        device.elder_id = E1  # E1 已被 D_A 绑定，flush 必撞部分唯一索引

        with (
            caplog.at_level(logging.INFO),
            pytest.raises(device_service.DeviceConflictError),
        ):
            await device_service._flush_or_conflict(
                session,
                identity,
                action="device.bind",
                target=f"device:{world.device_free}",
            )

        # rollback 之后同一 session 仍能读库：否则后续 SQL 会撞 PendingRollbackError。
        assert (await session.get(Device, world.device_free)).elder_id is None
        assert int((await session.scalar(select(func.count()).select_from(Device))) or 0) == (
            devices_before
        ), "冲突回滚后不得多出设备行"
    finally:
        await session.close()

    assert _audit_records(caplog, "device.bind", "conflict")


async def test_t11_cross_room_missing_device_and_non_admin_are_rejected(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    admin = _bearer(_staff_token(world.admin, "admin"))

    # E1 只在 A-301 的床位里，绑到 B-201 必须 409。
    cross_room = await client.post(
        f"/api/devices/{world.device_free}/bind",
        json={"room_id": str(ROOM_B), "elder_id": str(E1)},
        headers=admin,
    )
    _assert_error(cross_room, 409, "CONFLICT")
    assert (await _device_row(db, world.device_free)).elder_id is None

    missing = await client.post(
        f"/api/devices/{D_MISSING}/bind",
        json={"room_id": str(ROOM_D), "elder_id": str(E5)},
        headers=admin,
    )
    _assert_error(missing, 404, "NOT_FOUND")

    _assert_error(
        await client.post(f"/api/devices/{D_MISSING}/unbind", headers=admin), 404, "NOT_FOUND"
    )

    before = await _device_row(db, world.device_free)
    for headers in (
        _bearer(_staff_token(world.care1, "care_worker")),
        _bearer(_staff_token(world.doctor1, "doctor")),
        _bearer(_guardian_token(world.guardian_bound)),
        _bearer(_device_token(world.device_a, SECRET_A)),
    ):
        _assert_error(
            await client.post(
                f"/api/devices/{world.device_free}/bind",
                json={"room_id": str(ROOM_D), "elder_id": str(E5)},
                headers=headers,
            ),
            403,
            "ROLE_FORBIDDEN",
        )

    assert (await _device_row(db, world.device_free)) == before


# ------------------------------------------------------------------ T12–T13 · 解绑与旧令牌失效


async def test_t12_unbind_clears_binding_rotates_hash_and_old_token_401(
    client: AsyncClient, db: AsyncConnection, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    old_token = _device_token(world.device_a, SECRET_A)
    old_hash = sha256_hex(SECRET_A)

    # 先证明旧令牌在解绑前是有效的（否则下面的 401 可能只是签错了）。
    assert (
        await client.get(
            f"/api/devices/{world.device_a}/elder-candidates", headers=_bearer(old_token)
        )
    ).status_code == 200

    # E10 仅 admin：非 admin 必须在触碰数据前被拒，哈希不能先被轮换。
    for headers in (
        _bearer(_staff_token(world.care1, "care_worker")),
        _bearer(_guardian_token(world.guardian_bound)),
        _bearer(_device_token(world.device_a, SECRET_A)),
    ):
        _assert_error(
            await client.post(f"/api/devices/{world.device_a}/unbind", headers=headers),
            403,
            "ROLE_FORBIDDEN",
        )
    before = await _device_row(db, world.device_a)
    assert before.elder_id == E1
    assert before.token_hash == old_hash

    with caplog.at_level(logging.INFO):
        response = await client.post(
            f"/api/devices/{world.device_a}/unbind",
            headers=_bearer(_staff_token(world.admin, "admin")),
        )

    assert response.status_code == 200
    assert response.json()["elder_id"] is None

    row = await _device_row(db, world.device_a)
    assert row.elder_id is None
    assert row.status == "offline"
    assert row.token_hash != old_hash
    assert len(row.token_hash) == 64

    # 走 task-4 的真实设备凭证校验：旧会话的 tv 对不上新 token_hash → 401 TOKEN_REVOKED。
    replay = await client.get(
        f"/api/devices/{world.device_a}/elder-candidates", headers=_bearer(old_token)
    )
    _assert_error(replay, 401, "TOKEN_REVOKED")

    # 新旧哈希与明文 secret 都不得进入日志。
    assert old_hash not in caplog.text
    assert row.token_hash not in caplog.text
    assert SECRET_A not in caplog.text


async def test_t13_bind_with_null_elder_reuses_unbind_semantics(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    old_token = _device_token(world.device_a, SECRET_A)
    old_hash = sha256_hex(SECRET_A)

    response = await client.post(
        f"/api/devices/{world.device_a}/bind",
        json={"room_id": str(ROOM_A), "elder_id": None},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert response.status_code == 200
    assert response.json()["elder_id"] is None

    row = await _device_row(db, world.device_a)
    assert row.elder_id is None
    assert row.status == "offline"
    assert row.token_hash != old_hash  # null bind 不能绕过哈希轮换

    replay = await client.get(
        f"/api/devices/{world.device_a}/elder-candidates", headers=_bearer(old_token)
    )
    _assert_error(replay, 401, "TOKEN_REVOKED")


# ------------------------------------------------------------------ T14–T15 · 候选长者


async def test_t14_candidates_come_from_beds_not_devices_elder_id(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    # 故意把设备的 elder_id 指向本房间之外的长者（E6 无床位），候选仍必须来自床位。
    await db.execute(update(Device).where(Device.device_id == world.device_a).values(elder_id=E6))

    response = await client.get(
        f"/api/devices/{world.device_a}/elder-candidates",
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "double"
    assert {uuid.UUID(item["elder_id"]) for item in body["candidates"]} == {E1, E2}
    assert all(set(item) == {"elder_id", "bed_no", "display_name"} for item in body["candidates"])
    assert all("name" not in item for item in body["candidates"])

    by_elder = {uuid.UUID(item["elder_id"]): item for item in body["candidates"]}
    assert by_elder[E1]["display_name"] == "张*芬"
    assert by_elder[E1]["bed_no"] == "1 号床"
    assert by_elder[E2]["display_name"] == "李*国"
    assert by_elder[E2]["bed_no"] == "2 号床"

    single = await client.get(
        f"/api/devices/{world.device_b}/elder-candidates",
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert single.status_code == 200
    single_body = single.json()
    assert single_body["mode"] == "single"
    assert len(single_body["candidates"]) == 1
    assert uuid.UUID(single_body["candidates"][0]["elder_id"]) == E3
    assert single_body["candidates"][0]["display_name"] == "王*兰"

    # 未绑定设备的 `devices.elder_id` 为 NULL，但房间里有在住长者：仍应能列候选
    # （候选来源是 beds；E4 只在「未绑定且房间无在住长者」时拒绝设备换会话）。
    unbound = await client.get(
        f"/api/devices/{world.device_free}/elder-candidates",
        headers=_bearer(_device_token(world.device_free, SECRET_FREE)),
    )
    assert unbound.status_code == 200
    unbound_body = unbound.json()
    assert unbound_body["mode"] == "single"
    assert [uuid.UUID(item["elder_id"]) for item in unbound_body["candidates"]] == [E5]


async def test_t15_candidate_endpoint_identity_scope(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    admin = _bearer(_staff_token(world.admin, "admin"))
    own = _bearer(_device_token(world.device_a, SECRET_A))
    other_device = _bearer(_device_token(world.device_b, SECRET_B))

    assert (
        await client.get(f"/api/devices/{world.device_a}/elder-candidates", headers=admin)
    ).status_code == 200
    assert (
        await client.get(f"/api/devices/{world.device_a}/elder-candidates", headers=own)
    ).status_code == 200

    with caplog.at_level(logging.INFO):
        _assert_error(
            await client.get(
                f"/api/devices/{world.device_a}/elder-candidates", headers=other_device
            ),
            403,
            "SCOPE_FORBIDDEN",
        )
        _assert_error(
            await client.get(
                f"/api/devices/{world.device_a}/elder-candidates",
                headers=_bearer(_staff_token(world.care1, "care_worker")),
            ),
            403,
            "ROLE_FORBIDDEN",
        )
        _assert_error(
            await client.get(
                f"/api/devices/{world.device_a}/elder-candidates",
                headers=_bearer(_staff_token(world.doctor1, "doctor")),
            ),
            403,
            "ROLE_FORBIDDEN",
        )
        _assert_error(
            await client.get(
                f"/api/devices/{world.device_a}/elder-candidates",
                headers=_bearer(_guardian_token(world.guardian_bound)),
            ),
            403,
            "SCOPE_FORBIDDEN",
        )

    _assert_error(
        await client.get(f"/api/devices/{world.device_a}/elder-candidates"), 401, "TOKEN_INVALID"
    )
    _assert_error(
        await client.get(f"/api/devices/{D_MISSING}/elder-candidates", headers=admin),
        404,
        "NOT_FOUND",
    )
    assert _audit_records(caplog, "device.candidates", "forbidden")


# ------------------------------------------------------------------ 契约 §7 · 400 与 422 的分界


async def test_contract_distinguishes_format_errors_from_field_validation(
    client: AsyncClient, world: World
) -> None:
    """§7：路径参数格式错 / 非法 JSON → 400；字段值不合法 → 422。"""
    admin = _bearer(_staff_token(world.admin, "admin"))

    _assert_error(await client.get("/api/elders/not-a-uuid", headers=admin), 400, "BAD_REQUEST")

    malformed = await client.post(
        "/api/elders",
        content=b"{",
        headers={**admin, "Content-Type": "application/json"},
    )
    _assert_error(malformed, 400, "BAD_REQUEST")

    # 字段值不合法不能被 400 分支吞掉：空 name + 非法日期仍是 422。
    field_error = await client.post(
        "/api/elders",
        json={"name": "", "birth_date": "not-a-date"},
        headers=admin,
    )
    _assert_error(field_error, 422, "VALIDATION_ERROR")

    # 验签先于路径解析：无凭证时即使路径非法也必须是 401，而不是 400。
    _assert_error(await client.get("/api/elders/not-a-uuid"), 401, "TOKEN_INVALID")


# ------------------------------------------------------------------ FR-ADM-03 · 全端点无凭证 401


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/elders", {"name": "无凭证"}),
        ("GET", f"/api/elders/{E1}", None),
        ("GET", "/api/elders", None),
        ("PATCH", f"/api/elders/{E1}", {"name": "无凭证"}),
        ("PATCH", f"/api/elders/{E1}/persona", {"persona_id": None}),
        ("GET", "/api/elders/personas", None),
        ("POST", f"/api/devices/{D_A}/bind", {"room_id": str(ROOM_A), "elder_id": str(E1)}),
        ("POST", f"/api/devices/{D_A}/unbind", None),
        ("GET", f"/api/devices/{D_A}/elder-candidates", None),
    ],
)
async def test_all_protected_endpoints_require_credentials(
    client: AsyncClient, method: str, path: str, payload: dict | None
) -> None:
    response = await client.request(method, path, json=payload)
    _assert_error(response, 401, "TOKEN_INVALID")


# ------------------------------------------------------------------ W1 · 形象分配（FR-3D-03）


async def _insert_second_persona(db: AsyncConnection) -> None:
    await db.execute(
        Persona.__table__.insert().values(
            persona_id=PERSONA_ALT,
            name="老同事",
            personality="爽朗、爱聊年轻时候的事",
            greeting="哎哟，好久不见",
            voice_id="voice-demo-alt",
            avatar_url="/avatars/models/Lisa.vrm",
        )
    )


async def test_w1_admin_assigns_persona_and_db_persists(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """管理员给长者换形象：200 + `elders.persona_id` 真的变了 + 详情回带新值。"""
    await _insert_second_persona(db)

    response = await client.patch(
        f"/api/elders/{E3}/persona",
        json={"persona_id": str(PERSONA_ALT)},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )

    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["persona_id"]) == PERSONA_ALT
    # 其它列不受影响（形象分配只写一列）
    assert body["elder_id"] == str(E3)
    assert body["name"] == "王秀兰"
    assert (await _elder_row(db, E3)).persona_id == PERSONA_ALT

    # 解除分配：null 是合法请求，落库为 NULL
    cleared = await client.patch(
        f"/api/elders/{E3}/persona",
        json={"persona_id": None},
        headers=_bearer(_staff_token(world.admin, "admin")),
    )
    assert cleared.status_code == 200
    assert cleared.json()["persona_id"] is None
    assert (await _elder_row(db, E3)).persona_id is None


async def test_w1_doctor_may_assign_only_within_own_relation(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """医生是在管长者的日常操作人，但范围仍由服务层的 relation 决定。"""
    await _insert_second_persona(db)

    in_scope = await client.patch(
        f"/api/elders/{E1}/persona",
        json={"persona_id": str(PERSONA_ALT)},
        headers=_bearer(_staff_token(world.doctor1, "doctor")),
    )
    assert in_scope.status_code == 200
    assert (await _elder_row(db, E1)).persona_id == PERSONA_ALT

    # E2 不在医生一的 relation 里 → 403，且没有部分写入
    before = await _elder_row(db, E2)
    out_of_scope = await client.patch(
        f"/api/elders/{E2}/persona",
        json={"persona_id": str(PERSONA_ALT)},
        headers=_bearer(_staff_token(world.doctor1, "doctor")),
    )
    _assert_error(out_of_scope, 403, "SCOPE_FORBIDDEN")
    assert (await _elder_row(db, E2)).persona_id == before.persona_id

    # 护工没有这个权限（与 E8 一致：形象分配只开给 admin / 主治医生）
    care = await client.patch(
        f"/api/elders/{E1}/persona",
        json={"persona_id": str(PERSONA_ALT)},
        headers=_bearer(_staff_token(world.care1, "care_worker")),
    )
    _assert_error(care, 403, "ROLE_FORBIDDEN")

    for headers in (
        _bearer(_guardian_token(world.guardian_bound)),
        _bearer(_device_token(world.device_a, SECRET_A)),
    ):
        denied = await client.patch(
            f"/api/elders/{E1}/persona", json={"persona_id": str(PERSONA_ALT)}, headers=headers
        )
        _assert_error(denied, 403, "ROLE_FORBIDDEN")


async def test_w1_invalid_persona_and_archived_elder_are_422_without_write(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """人设不存在 / 长者已归档 → 422，且 `persona_id` 保持原样（不留半截写入）。"""
    admin = _bearer(_staff_token(world.admin, "admin"))
    before = await _elder_row(db, E1)

    missing_persona = await client.patch(
        f"/api/elders/{E1}/persona",
        json={"persona_id": str(uuid.uuid4())},
        headers=admin,
    )
    _assert_error(missing_persona, 422, "VALIDATION_ERROR")
    assert (await _elder_row(db, E1)).persona_id == before.persona_id

    archived = await client.patch(
        f"/api/elders/{E6}/persona", json={"persona_id": str(PERSONA_ID)}, headers=admin
    )
    _assert_error(archived, 422, "VALIDATION_ERROR")
    assert (await _elder_row(db, E6)).persona_id is None

    # 夹带未声明字段不是「被忽略」而是 422（extra=forbid）
    smuggled = await client.patch(
        f"/api/elders/{E1}/persona",
        json={"persona_id": str(PERSONA_ID), "status": "archived"},
        headers=admin,
    )
    _assert_error(smuggled, 422, "VALIDATION_ERROR")
    assert (await _elder_row(db, E1)).status == "active"


async def test_w1_persona_options_list_for_dropdown(
    client: AsyncClient, db: AsyncConnection, world: World
) -> None:
    """照护台「形象」下拉的数据源：admin / doctor 可取，护工 403，且路由不被 `/{elder_id}` 吞掉。"""
    await _insert_second_persona(db)

    response = await client.get(
        "/api/elders/personas", headers=_bearer(_staff_token(world.doctor1, "doctor"))
    )

    # 200 而不是 422「elder_id 不是 UUID」——这条同时守住「/personas 必须声明在 /{elder_id} 之前」
    assert response.status_code == 200
    items = response.json()["items"]
    ids = {uuid.UUID(item["persona_id"]) for item in items}
    assert {PERSONA_ID, PERSONA_ALT} <= ids
    seeded = next(item for item in items if uuid.UUID(item["persona_id"]) == PERSONA_ID)
    assert seeded["name"] == "小满"
    assert seeded["avatar_url"] is None
    assert seeded["voice_id"] == "voice-demo"
    assert seeded["personality"] == "温和、耐心"
    assert seeded["greeting"] == "您好呀"
    assert seeded["tone_examples"] == []
    # 两位在册长者绑在小满上；列表页的「已分配」按 elders.persona_id 聚合
    assert seeded["assigned_elders"] == 2
    # 带 avatar_url 的人设原样透出（前端拿它去 catalog 里找可读的名字做预览）
    with_avatar = next(item for item in items if uuid.UUID(item["persona_id"]) == PERSONA_ALT)
    assert with_avatar["avatar_url"] == "/avatars/models/Lisa.vrm"
    assert with_avatar["voice_id"] == "voice-demo-alt"
    assert with_avatar["assigned_elders"] == 0
    assert set(seeded) == {
        "persona_id",
        "name",
        "avatar_url",
        "voice_id",
        "personality",
        "greeting",
        "tone_examples",
        "assigned_elders",
    }

    care_worker = await client.get(
        "/api/elders/personas", headers=_bearer(_staff_token(world.care1, "care_worker"))
    )
    _assert_error(care_worker, 403, "ROLE_FORBIDDEN")
