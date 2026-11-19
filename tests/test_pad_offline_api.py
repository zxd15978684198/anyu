"""迭代 6–10 收口：`/api/pad/medications/today` 与 `/api/pad/offline/report` 的接口级测试。

覆盖两件事（更细的服务层口径在 `tests/test_offline_fallback.py`，这里只验「接口通不通」）：

- 鉴权：无 token → 401；设备未绑长者 → 403 `device_not_bound`（C7 错误体形状）；
- 契约：绑定设备无当日事件时回**空数组**而不是 404（端侧缓存要能安全地写 0 条）；
- 未知道具：上报一个库里不存在的 `reminder_id` → 单条降级成 `missing`，
  整份上报仍是 200 —— 一块平板掉线时会带着好几条提醒，不能一条脏数据打挂一批。

跑法与 `test_pad_avatar.py` 同构：自建 NullPool 引擎，每用例一个事务、结束回滚，
请求走真实 ASGI 链路（`get_session` / `current_device` / `token_hash` 全是真实代码）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import pad as pad_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Device, Elder, Room
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "pad-offline-test-secret-not-a-real-key-0123456789"

ROOM_ID = uuid.UUID("ddddddd0-dddd-4ddd-8ddd-ddddddddddd1")
ELDER_ID = uuid.UUID("eeeeeee0-eeee-4eee-8eee-eeeeeeeeeee1")
DEVICE_BOUND = uuid.UUID("fffffff0-ffff-4fff-8fff-fffffffffff1")
DEVICE_UNBOUND = uuid.UUID("fffffff0-ffff-4fff-8fff-fffffffffff2")

SECRET_BOUND = "pad-offline-secret-bound-not-a-real-value"
SECRET_UNBOUND = "pad-offline-secret-unbound-not-a-real-value"

UNKNOWN_REMINDER = uuid.UUID("99999990-9999-4999-8999-999999999999")


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
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(pad_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        # create_savepoint：请求里的 commit 只释放保存点，外层测试事务仍可整体回滚。
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
    """一位长者 + 一台已绑定设备 + 一台未绑定设备；刻意不建任何用药事件。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="B-201", status="full")
    )
    await db.execute(
        Elder.__table__.insert().values(
            elder_id=ELDER_ID,
            name="王秀兰",
            birth_date=date(1946, 7, 8),
            gender="female",
            persona_id=None,
            status="active",
        )
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
                "device_id": DEVICE_UNBOUND,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_UNBOUND),
                "status": "online",
            },
        ],
    )


def _token(device_id: uuid.UUID, secret: str) -> str:
    return sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])


def _auth(device_id: uuid.UUID, secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(device_id, secret)}"}


# ---------------------------------------------------------------- 鉴权


async def test_requires_device_token(client: AsyncClient, seeded: None) -> None:
    response = await client.get("/api/pad/medications/today")
    assert response.status_code == 401


async def test_unbound_device_is_403(client: AsyncClient, seeded: None) -> None:
    response = await client.get(
        "/api/pad/medications/today", headers=_auth(DEVICE_UNBOUND, SECRET_UNBOUND)
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "device_not_bound"
    assert set(body) == {"detail", "code", "trace_id"}


# ---------------------------------------------------------------- 当日药单


async def test_today_reminders_empty_when_no_events(client: AsyncClient, seeded: None) -> None:
    """没有当日事件时回空数组：端侧要能安全地「今天没有药」写进 IndexedDB。"""

    response = await client.get(
        "/api/pad/medications/today", headers=_auth(DEVICE_BOUND, SECRET_BOUND)
    )
    assert response.status_code == 200
    assert response.json() == {"reminders": []}


# ---------------------------------------------------------------- 掉线/恢复上报


async def test_unknown_reminder_degrades_to_missing(client: AsyncClient, seeded: None) -> None:
    """单条脏数据不能打挂整份上报：整份 200，该条标 missing。"""

    payload = {
        "kind": "offline",
        "at": datetime(2026, 11, 9, 12, 0, tzinfo=None).isoformat(),
        "events": [
            {
                "reminderId": str(UNKNOWN_REMINDER),
                "scheduledAt": datetime(2026, 11, 9, 12, 0).isoformat(),
                "attemptsSent": 0,
                "announced": False,
            }
        ],
    }
    response = await client.post(
        "/api/pad/offline/report", json=payload, headers=_auth(DEVICE_BOUND, SECRET_BOUND)
    )
    assert response.status_code == 200
    [row] = response.json()["results"]
    assert row["reminderId"] == str(UNKNOWN_REMINDER)
    assert row["action"] == "missing"
    assert row["reason"] == "event_not_found"


async def test_online_report_rejects_foreign_device(client: AsyncClient, seeded: None) -> None:
    """未绑定设备连上报都不该被受理（否则等于允许对着别人的药单发指令）。"""

    payload = {
        "kind": "online",
        "events": [{"reminderId": str(UNKNOWN_REMINDER)}],
    }
    response = await client.post(
        "/api/pad/offline/report", json=payload, headers=_auth(DEVICE_UNBOUND, SECRET_UNBOUND)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "device_not_bound"


# ------------------------------------------- 第③层：掉线与用药窗口重叠即通知护工
#
# 下面两例是 audit-reverse 指出的反向缺口：此前只有 `missing` 分支有用例，
# 把 `app/api/pad.py` 里 `if body.kind == "offline":` 整块删掉（→ 第③层永不通知）
# 也不会有任何用例变红。服务层自己的落库断言在 `tests/test_offline_fallback.py`，
# 这里只钉「API 把服务层的两种结果如实翻译成 notified / skipped」。


def _offline_payload() -> dict[str, object]:
    return {
        "kind": "offline",
        "at": datetime(2026, 11, 9, 8, 0).isoformat(),
        "events": [
            {
                "reminderId": str(UNKNOWN_REMINDER),
                "scheduledAt": datetime(2026, 11, 9, 8, 0).isoformat(),
                "windowEnd": datetime(2026, 11, 9, 8, 1).isoformat(),
                "attemptsSent": 1,
                "announced": False,
            }
        ],
    }


async def test_offline_overlap_is_reported_as_notified(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窗口重叠 → `notified`，且同一条只通知一次（幂等由服务层保证，这里只校验调用次数）。"""

    calls: list[uuid.UUID] = []

    async def fake_notify(_session: object, *, reminder_id: uuid.UUID) -> object:
        calls.append(reminder_id)
        return object()

    monkeypatch.setattr(pad_api.notification_service, "notify_offline_overlap", fake_notify)
    response = await client.post(
        "/api/pad/offline/report",
        json=_offline_payload(),
        headers=_auth(DEVICE_BOUND, SECRET_BOUND),
    )
    assert response.status_code == 200
    [row] = response.json()["results"]
    assert (row["action"], row["reason"]) == ("notified", "offline_overlap")
    assert calls == [UNKNOWN_REMINDER]


async def test_offline_without_overlap_is_reported_as_skipped(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已闭环/已超窗 → 服务层返回 None，接口如实回 `skipped`，不假装通知过。"""

    async def fake_notify(_session: object, *, reminder_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(pad_api.notification_service, "notify_offline_overlap", fake_notify)
    response = await client.post(
        "/api/pad/offline/report",
        json=_offline_payload(),
        headers=_auth(DEVICE_BOUND, SECRET_BOUND),
    )
    assert response.status_code == 200
    [row] = response.json()["results"]
    assert (row["action"], row["reason"]) == ("skipped", "no_overlap")
