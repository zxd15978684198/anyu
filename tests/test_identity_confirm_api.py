"""`POST /api/identity/confirm` 的接口级测试（按住说话 REST 降级链路的身份确认）。

## 这一层测什么

- **契约**：设备令牌鉴权、响应四字段、`created` 语义；
- **安全**：body 的 `elder_id` 必须等于设备绑定的长者（403 `elder_mismatch`），
  未绑定 409、无令牌 401、缺字段/坏 UUID 422 —— 每条都要求**不落库**；
- **幂等**：同一段会话重复确认只留一条 `confirm` thread，且回同一个 `thread_id`；
  已有 `generic` open thread 时**不复用**（那不代表长者点过「是我」）；
- **错误面**：身份服务抛异常 → 503 `identity_unavailable`，且不留下半条 thread。

不测：服务层 `open_thread_from_confirm` 自身的候选集判定与 WS 链路
（`tests/test_identity_service.py` 与 `tests/test_media_bridge_identity.py` 已覆盖）。
本文件跑真数据库事务（外层 rollback），只有 503 那条 monkeypatch 服务函数。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session
from app.models import ConversationThread, Device, Elder, Room
from app.services import identity_service
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "identity-rest-test-secret-not-a-real-key-0123456789ab"

# ---------------------------------------------------------------- 固定 ID（全部虚构）
ROOM_ID = uuid.UUID("c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1")
ELDER_ID = uuid.UUID("c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2")
ELDER_OTHER = uuid.UUID("c3c3c3c3-c3c3-4c3c-8c3c-c3c3c3c3c3c3")
DEVICE_BOUND = uuid.UUID("c4c4c4c4-c4c4-4c4c-8c4c-c4c4c4c4c4c4")
DEVICE_FREE = uuid.UUID("c5c5c5c5-c5c5-4c5c-8c5c-c5c5c5c5c5c5")

SECRET_BOUND = "device-secret-identity-bound-not-a-real-value"
SECRET_FREE = "device-secret-identity-free-not-a-real-value"


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
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def client(db: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    """真实 `create_app()`（含 401/422 全局处理器），只把 get_session 指向测试事务。"""
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
    """两位长者 + 两台设备（一台绑定 ELDER_ID、一台未绑定）。

    第二位长者是必需的：403 用例要证明「拿别人的 elder_id 确认」会被拒，
    只有一位长者时那种请求与「传自己」无法区分。
    """
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="D-101", status="full")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {
                "elder_id": ELDER_ID,
                "name": "陈玉兰",
                "birth_date": date(1945, 5, 20),
                "gender": "female",
                "status": "active",
            },
            {
                "elder_id": ELDER_OTHER,
                "name": "孙志强",
                "birth_date": date(1943, 11, 8),
                "gender": "male",
                "status": "active",
            },
        ],
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": DEVICE_BOUND,
                "room_id": ROOM_ID,
                "elder_id": ELDER_ID,
                "token_hash": sha256_hex(SECRET_BOUND),
                "status": "online",
            },
            {
                "device_id": DEVICE_FREE,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_FREE),
                "status": "offline",
            },
        ],
    )


# ---------------------------------------------------------------- 工具


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_token(device_id: uuid.UUID, secret: str) -> str:
    return sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])


def _bound_headers() -> dict[str, str]:
    return _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))


async def _thread_rows(db: AsyncConnection) -> list[tuple[str, str, uuid.UUID | None, str | None]]:
    """(thread_id, identity_source, elder_id, ended_at) 四列，按开始时间升序。"""
    result = await db.execute(
        select(
            ConversationThread.thread_id,
            ConversationThread.identity_source,
            ConversationThread.elder_id,
            ConversationThread.ended_at,
        ).order_by(ConversationThread.started_at)
    )
    return [(row.thread_id, row.identity_source, row.elder_id, row.ended_at) for row in result]


async def _thread_count(db: AsyncConnection) -> int:
    result = await db.execute(select(func.count()).select_from(ConversationThread))
    return int(result.scalar_one())


# ---------------------------------------------------------------- 正常路径


async def test_confirm_creates_conversation_thread(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """一次确认 = 一条 `identity_source=confirm`、`ended_at IS NULL` 的会话 thread。"""
    response = await client.post(
        "/api/identity/confirm",
        headers=_bound_headers(),
        json={"elder_id": str(ELDER_ID), "ask_id": "ask-001"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # 字段名是前端降级链路的契约（与 WS identity_result 对齐），改名即破坏。
    assert set(body) == {"thread_id", "identity_source", "elder_id", "created"}, body
    assert body["identity_source"] == "confirm"
    assert body["elder_id"] == str(ELDER_ID)
    assert body["created"] is True
    assert len(body["thread_id"]) == 32

    rows = await _thread_rows(db)
    assert len(rows) == 1, rows
    thread_id, source, elder_id, ended_at = rows[0]
    assert thread_id == body["thread_id"]
    assert source == "confirm"
    assert elder_id == ELDER_ID
    assert ended_at is None, "确认态会话不能一落库就是已结束"


async def test_repeat_confirm_reuses_same_thread(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """同一段会话重复确认：只留一条 thread，`created=false` 且 `thread_id` 不变。

    pad 的确认按钮可能被连点两下、前端重试也可能重发；长者看到的应当始终是同一段会话
    （开场白只播一次）。
    """
    first = await client.post(
        "/api/identity/confirm", headers=_bound_headers(), json={"elder_id": str(ELDER_ID)}
    )
    second = await client.post(
        "/api/identity/confirm", headers=_bound_headers(), json={"elder_id": str(ELDER_ID)}
    )

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    first_body, second_body = first.json(), second.json()
    assert first_body["created"] is True
    assert second_body["created"] is False
    assert second_body["thread_id"] == first_body["thread_id"]
    assert second_body["identity_source"] == "confirm"
    assert await _thread_count(db) == 1


async def test_open_generic_thread_is_not_reused(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """已有 `generic` 的未结束会话时，确认要**新建**一条 confirm thread。

    复用 generic 等于凭空替长者确认了一次身份——这正是 C5 裁决要防的事。
    """
    await db.execute(
        ConversationThread.__table__.insert().values(
            thread_id="generic-open-thread-0000000000001",
            elder_id=None,
            device_id=DEVICE_BOUND,
            identity_source="generic",
            started_at=datetime(2026, 11, 11, 1, 0, tzinfo=UTC),
        )
    )

    response = await client.post(
        "/api/identity/confirm", headers=_bound_headers(), json={"elder_id": str(ELDER_ID)}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["thread_id"] != "generic-open-thread-0000000000001"

    rows = await _thread_rows(db)
    assert [row[1] for row in rows] == ["generic", "confirm"], rows
    assert rows[1][2] == ELDER_ID


# ---------------------------------------------------------------- 安全与校验


async def test_mismatched_elder_is_403_and_writes_nothing(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """拿别人的 elder_id 来确认 → 403 `elder_mismatch`，一行都不写（AGENTS §4-9）。"""
    response = await client.post(
        "/api/identity/confirm",
        headers=_bound_headers(),
        json={"elder_id": str(ELDER_OTHER)},
    )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "elder_mismatch", body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and body["trace_id"]

    assert await _thread_count(db) == 0, "身份不匹配却写了 thread：等于把身份绑到别人身上"


async def test_unbound_device_is_409_and_writes_nothing(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """设备未绑定长者 → 409 `device_unbound`，不落 thread。"""
    response = await client.post(
        "/api/identity/confirm",
        headers=_bearer(_device_token(DEVICE_FREE, SECRET_FREE)),
        json={"elder_id": str(ELDER_ID)},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "device_unbound", response.text
    assert await _thread_count(db) == 0


async def test_without_token_is_401(client: AsyncClient, db: AsyncConnection, seeded: None) -> None:
    """没有设备令牌 → 401，鉴权在业务判断之前拦下。"""
    response = await client.post("/api/identity/confirm", json={"elder_id": str(ELDER_ID)})

    assert response.status_code == 401, response.text
    assert await _thread_count(db) == 0


async def test_missing_or_invalid_elder_id_is_422(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """缺 `elder_id` / 非 UUID → 422：确认请求必须点名「确认的是谁」，不给默认值。"""
    missing = await client.post(
        "/api/identity/confirm", headers=_bound_headers(), json={"ask_id": "ask-002"}
    )
    invalid = await client.post(
        "/api/identity/confirm", headers=_bound_headers(), json={"elder_id": "not-a-uuid"}
    )

    assert missing.status_code == 422, missing.text
    assert invalid.status_code == 422, invalid.text
    assert await _thread_count(db) == 0


# ---------------------------------------------------------------- 错误面


async def test_service_failure_is_503_and_writes_nothing(
    client: AsyncClient,
    db: AsyncConnection,
    seeded: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身份服务抛异常 → 503 `identity_unavailable`，且不留下半条 thread。

    这里**必须** monkeypatch：503 只在写库真的失败时出现，正常数据造不出来。
    """

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("identity store down")

    monkeypatch.setattr(identity_service, "open_thread_from_confirm", _boom)

    response = await client.post(
        "/api/identity/confirm", headers=_bound_headers(), json={"elder_id": str(ELDER_ID)}
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "identity_unavailable", body
    assert await _thread_count(db) == 0
