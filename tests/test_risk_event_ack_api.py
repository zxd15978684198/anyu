"""风险事件确认端点 `POST /api/risk-events/{event_id}/ack` 的接口级测试。

对应验收：FR-ALT-01（红色事件 2 分钟内无人确认就升级）。这个端点是升级扫描
唯一的人工「已接手」写入点；一旦路径、鉴权或落库被改坏，事件会在 120 秒后
必然升级，而服务层单测仍可能全绿——所以这里钉的是 HTTP 契约本身：

- 三个照护台角色都能确认，身份只从令牌来；
- `status` 必须仍是 `open`（确认 ≠ 结案），`acknowledged_at` 必须落库；
- 重复确认返回原时间戳，不重复写审计（幂等）；
- 事件不存在 404、ID 非法 422、无凭证/设备凭证 401。

隔离方式与 `tests/test_media_api.py` 同构：真实 `create_app()` + 外层事务 +
`get_session` 覆盖成 savepoint，用例结束整体回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session, sign_staff_access
from app.db import get_session
from app.models import AuditLog, Device, Elder, RiskEvent, Room, StaffAccount
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "risk-ack-test-secret-not-a-real-key-0123456789"

S_ADMIN = uuid.UUID("a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1")
S_CARE = uuid.UUID("a2a2a2a2-a2a2-4a2a-8a2a-a2a2a2a2a2a2")
S_DOCTOR = uuid.UUID("a3a3a3a3-a3a3-4a3a-8a3a-a3a3a3a3a3a3")

ROOM_ID = uuid.UUID("b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1")
ELDER_ID = uuid.UUID("b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2")
DEVICE_ID = uuid.UUID("b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3")
DEVICE_SECRET = "risk-ack-device-secret-not-a-real-value"

EVENT_ID = uuid.UUID("b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4")
SOURCE_ID = uuid.UUID("b5b5b5b5-b5b5-4b5b-8b5b-b5b5b5b5b5b5")
UNKNOWN_EVENT = uuid.UUID("b6b6b6b6-b6b6-4b6b-8b6b-b6b6b6b6b6b6")

TOKEN_VERSION = 0
PASSWORD_PLACEHOLDER = "not-a-real-hash"


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
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def client(db: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    application = create_app(get_settings())

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


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """一个房间、一位长者、三员工、一台设备、一条 open 红色事件。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="A-101", status="full")
    )
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER_ID, name="周桂英", status="active")
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": S_ADMIN,
                "name": "管理员",
                "role": "admin",
                "password_hash": PASSWORD_PLACEHOLDER,
                "token_version": TOKEN_VERSION,
                "status": "active",
            },
            {
                "staff_id": S_CARE,
                "name": "护工",
                "role": "care_worker",
                "password_hash": PASSWORD_PLACEHOLDER,
                "token_version": TOKEN_VERSION,
                "status": "active",
            },
            {
                "staff_id": S_DOCTOR,
                "name": "医生",
                "role": "doctor",
                "password_hash": PASSWORD_PLACEHOLDER,
                "token_version": TOKEN_VERSION,
                "status": "active",
            },
        ],
    )
    await db.execute(
        Device.__table__.insert().values(
            device_id=DEVICE_ID,
            room_id=ROOM_ID,
            elder_id=ELDER_ID,
            token_hash=sha256_hex(DEVICE_SECRET),
            status="online",
        )
    )
    await db.execute(
        RiskEvent.__table__.insert().values(
            event_id=EVENT_ID,
            elder_id=ELDER_ID,
            type="safety_alert",
            severity="red",
            source_type="safety",
            source_id=SOURCE_ID,
            status="open",
        )
    )


def _staff_token(staff_id: uuid.UUID, role: str) -> str:
    return sign_staff_access(
        kind="staff", staff_id=staff_id, role=role, token_version=TOKEN_VERSION
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_auth() -> dict[str, str]:
    token = sign_device_session(
        device_id=DEVICE_ID, token_hash_prefix=sha256_hex(DEVICE_SECRET)[:16]
    )
    return _bearer(token)


async def _audit_count(db: AsyncConnection) -> int:
    result = await db.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "risk_event.ack")
    )
    return int(result.scalar_one())


def _assert_contract_error(body: dict[str, object]) -> None:
    assert set(body) == {"detail", "code", "trace_id"}


# ---------------------------------------------------------------- 允许角色


@pytest.mark.parametrize(
    ("staff_id", "role"),
    [
        (S_ADMIN, "admin"),
        (S_CARE, "care_worker"),
        (S_DOCTOR, "doctor"),
    ],
)
async def test_allowed_roles_can_acknowledge(
    client: AsyncClient, seeded: None, staff_id: uuid.UUID, role: str
) -> None:
    """FR-ALT-01：三个照护台角色都能把「我接手了」写进去。"""
    response = await client.post(
        f"/api/risk-events/{EVENT_ID}/ack", headers=_bearer(_staff_token(staff_id, role))
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert uuid.UUID(body["event_id"]) == EVENT_ID
    assert uuid.UUID(body["elder_id"]) == ELDER_ID
    assert body["type"] == "safety_alert"
    assert body["severity"] == "red"
    assert body["status"] == "open", "确认只写 acknowledged_at，不能顺手把险情结案"
    assert body["acknowledged_at"] is not None


# ---------------------------------------------------------------- 落库与审计


async def test_acknowledge_persists_timestamp_and_one_audit_row(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """时间戳必须落库；审计与业务同事务，恰好一行、主体是当前员工。"""
    response = await client.post(
        f"/api/risk-events/{EVENT_ID}/ack", headers=_bearer(_staff_token(S_CARE, "care_worker"))
    )
    assert response.status_code == 200, response.text

    stored = await db.execute(
        select(RiskEvent.acknowledged_at, RiskEvent.status).where(RiskEvent.event_id == EVENT_ID)
    )
    ack_at, status = stored.one()
    assert ack_at is not None
    assert status == "open"

    row = (
        await db.execute(
            select(
                AuditLog.actor_type,
                AuditLog.actor_id,
                AuditLog.resource_type,
                AuditLog.resource_id,
                AuditLog.detail,
            ).where(AuditLog.action == "risk_event.ack")
        )
    ).one()
    assert row.actor_type == "staff"
    assert row.actor_id == S_CARE
    assert row.resource_type == "risk_event"
    assert row.resource_id == EVENT_ID
    assert row.detail is not None
    assert row.detail["event_type"] == "safety_alert"
    assert row.detail["severity"] == "red"


async def test_repeat_acknowledge_is_idempotent(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """重复点击确认：原时间戳原样返回，不推后、不重复写审计。"""
    headers = _bearer(_staff_token(S_CARE, "care_worker"))
    first = await client.post(f"/api/risk-events/{EVENT_ID}/ack", headers=headers)
    second = await client.post(f"/api/risk-events/{EVENT_ID}/ack", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_at = datetime.fromisoformat(first.json()["acknowledged_at"])
    second_at = datetime.fromisoformat(second.json()["acknowledged_at"])
    assert second_at == first_at, "第二次确认不能把「多久才有人接手」的起点推后"
    assert await _audit_count(db) == 1


async def test_already_acknowledged_event_is_not_rewritten(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """库里已有确认时刻（例如升级任务之外的路径写过）时，端点只读不改。"""
    original = datetime(2026, 11, 16, 8, 30, tzinfo=UTC)
    await db.execute(
        RiskEvent.__table__.update()
        .where(RiskEvent.event_id == EVENT_ID)
        .values(acknowledged_at=original)
    )

    response = await client.post(
        f"/api/risk-events/{EVENT_ID}/ack", headers=_bearer(_staff_token(S_ADMIN, "admin"))
    )

    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["acknowledged_at"]) == original
    assert await _audit_count(db) == 0, "没有发生新的确认，就不该凭空多一条审计"


# ---------------------------------------------------------------- 拒绝路径


async def test_unknown_event_is_404(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        f"/api/risk-events/{UNKNOWN_EVENT}/ack",
        headers=_bearer(_staff_token(S_ADMIN, "admin")),
    )

    assert response.status_code == 404, response.text
    body = response.json()
    _assert_contract_error(body)
    assert body["code"] == "NOT_FOUND"


async def test_malformed_event_id_is_400(client: AsyncClient, seeded: None) -> None:
    response = await client.post(
        "/api/risk-events/not-a-uuid/ack",
        headers=_bearer(_staff_token(S_ADMIN, "admin")),
    )

    assert response.status_code == 400, response.text
    body = response.json()
    _assert_contract_error(body)
    assert body["code"] == "BAD_REQUEST"


async def test_missing_credential_is_401(client: AsyncClient, seeded: None) -> None:
    response = await client.post(f"/api/risk-events/{EVENT_ID}/ack")

    assert response.status_code == 401, response.text
    _assert_contract_error(response.json())


async def test_device_credential_is_401(client: AsyncClient, seeded: None) -> None:
    """设备凭证体系不能确认险情：拿错凭证是 401，不是「权限不够」403。"""
    response = await client.post(f"/api/risk-events/{EVENT_ID}/ack", headers=_device_auth())

    assert response.status_code == 401, response.text
    _assert_contract_error(response.json())
