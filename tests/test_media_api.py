"""点播端点 `/api/media/*` 的接口级测试（FR-CON-03 / FR-CON-04，P0）。

## 这一层测什么、不测什么

- **测**：HTTP 契约本身——五个端点的路径与方法、`kind` 校验（缺失/非法 → 422）、
  设备令牌鉴权（员工令牌 401）、`elder_id` 只从设备绑定取（请求体里塞 elder_id 不生效）、
  `play` 真的落 `media_history`、`finish` 的完成度换算、`switch-next` 换段并把上一段记成
  被打断、未授权素材 422 / 不存在 404 / 未绑定设备 409 / 跨长者 403。
- **不测**：`media_service` 内部的挑选顺序、去重排除集、`_mark_interrupted` 细节
  （`tests/test_media_service.py` 已覆盖）。这里只验证端点把这些能力接出来之后，
  **前端拿到的形状**（url 是 `/media/` 路径）和**账号边界**（谁的设备替谁记账）。

## 素材与地址

四条素材刻意覆盖三种 `media_url` 形态：绝对 URL、裸相对路径、已带 `/media/` 前缀。
断言统一要求返回值是 `/media/…` 且不以 `.html` 结尾——因为 nginx 的 MPA 兜底 location
会把未命中路径喂回 `index.html`，`<audio>` 拿到 HTML 时页面只会「点了没反应」，
不会有任何报错（`frontend/nginx.conf:188` 的 `try_files $uri =404` 就是为了这个）。

无 PostgreSQL 时由 conftest 的 `require_db_connection()` 直接 fail
（`ANYU_ALLOW_DB_SKIP=1` 才 skip），与既有 API 用例策略一致。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session, sign_staff_access
from app.db import get_session
from app.models import (
    AuditLog,
    Device,
    Elder,
    MediaHistory,
    MediaLibraryItem,
    Room,
    StaffAccount,
)
from app.services import audit_service, report_service
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "media-rest-test-secret-not-a-real-key-0123456789abcdef"

# ---------------------------------------------------------------- 固定 ID（全部虚构）
ROOM_ID = uuid.UUID("c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1")
ELDER_ID = uuid.UUID("c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2")
ELDER_OTHER = uuid.UUID("c3c3c3c3-c3c3-4c3c-8c3c-c3c3c3c3c3c3")
DEVICE_BOUND = uuid.UUID("c4c4c4c4-c4c4-4c4c-8c4c-c4c4c4c4c4c4")
DEVICE_FREE = uuid.UUID("c5c5c5c5-c5c5-4c5c-8c5c-c5c5c5c5c5c5")
DEVICE_OTHER = uuid.UUID("c6c6c6c6-c6c6-4c6c-8c6c-c6c6c6c6c6c6")
CARE_WORKER_ID = uuid.UUID("c7c7c7c7-c7c7-4c7c-8c7c-c7c7c7c7c7c7")
ADMIN_ID = uuid.UUID("c8c8c8c8-c8c8-4c8c-8c8c-c8c8c8c8c8c8")

# UUID 的字面顺序就是 `pick` 的挑选顺序（服务层 `ORDER BY media_id`），
# 所以 switch-next 该换到哪一段是确定的，不靠运气。
MEDIA_OPERA_A = uuid.UUID("0a000000-0000-4000-8000-000000000001")
MEDIA_OPERA_B = uuid.UUID("0a000000-0000-4000-8000-000000000002")
MEDIA_OPERA_LOCKED = uuid.UUID("0a000000-0000-4000-8000-000000000003")
MEDIA_MOVIE = uuid.UUID("0b000000-0000-4000-8000-000000000001")

SECRET_BOUND = "device-secret-media-bound-not-a-real-value"
SECRET_FREE = "device-secret-media-free-not-a-real-value"
SECRET_OTHER = "device-secret-media-other-not-a-real-value"


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
    """用真实 `create_app()` 装配（含 401/422 全局处理器），只把 get_session 指向测试事务。"""
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


@pytest.fixture(autouse=True)
def isolated_session_factory(
    db: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """越权审计走独立会话；钉到测试连接上，既测真实 SQL 又不提交种子库。"""
    factory = async_sessionmaker(
        bind=db,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(report_service, "session_factory", factory)
    # 媒体越权审计走 `audit_service.write_audit_isolated`，它用的是本模块导入的
    # `session_factory`（不是 report_service 那个）；漏改就会在全局引擎上开新连接。
    monkeypatch.setattr(audit_service, "session_factory", factory)
    return factory


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """两位长者、三台设备（绑定 / 未绑定 / 绑定另一位）、四条素材（三条戏曲一条电影）。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="D-401", status="full")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {
                "elder_id": ELDER_ID,
                "name": "王秀兰",
                "birth_date": date(1946, 5, 12),
                "gender": "female",
                "status": "active",
            },
            {
                "elder_id": ELDER_OTHER,
                "name": "孙德明",
                "birth_date": date(1941, 1, 30),
                "gender": "male",
                "status": "active",
            },
        ],
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": CARE_WORKER_ID,
                "name": "钱护工",
                "role": "care_worker",
                "password_hash": "x",
            },
            {
                "staff_id": ADMIN_ID,
                "name": "王管理",
                "role": "admin",
                "password_hash": "x",
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
            {
                "device_id": DEVICE_OTHER,
                "room_id": ROOM_ID,
                "elder_id": ELDER_OTHER,
                "token_hash": sha256_hex(SECRET_OTHER),
                "status": "online",
            },
        ],
    )
    await db.execute(
        MediaLibraryItem.__table__.insert(),
        [
            {
                "media_id": MEDIA_OPERA_A,
                "type": "opera",
                "title": "梁祝·十八相送",
                "genre": "越剧",
                "era": "传统折子戏",
                "artist": "袁雪芬",
                "media_url": "https://example.invalid/media/opera/yueju-liangzhu.mp3",
                "duration": 1800,
                "copyright_status": "licensed_sample",
            },
            {
                "media_id": MEDIA_OPERA_B,
                "type": "opera",
                "title": "苏三起解",
                "genre": "京剧",
                "era": "传统折子戏",
                "artist": "梅兰芳",
                # 裸相对路径：端点必须自己补上 /media/ 前缀。
                "media_url": "opera/jingju-sujiao.mp3",
                "duration": 1500,
                "copyright_status": "public_domain",
            },
            {
                "media_id": MEDIA_OPERA_LOCKED,
                "type": "opera",
                "title": "未授权选段",
                "genre": "越剧",
                "era": None,
                "artist": None,
                # 已带前缀的形态：不能变成 /media/media/…。
                "media_url": "/media/opera/locked.mp3",
                "duration": 900,
                "copyright_status": "unknown",
            },
            {
                "media_id": MEDIA_MOVIE,
                "type": "movie",
                "title": "城南旧事",
                "genre": "生活片",
                "era": "80 年代",
                "artist": "吴贻弓",
                "media_url": "https://example.invalid/media/movie/chengnan.mp4",
                "duration": 5400,
                "copyright_status": "licensed_sample",
            },
        ],
    )


# ---------------------------------------------------------------- 工具


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_token(device_id: uuid.UUID, secret: str) -> str:
    return sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])


def _staff_token() -> str:
    # `staff_accounts.token_version` 的 server_default 是 0，签发值必须与库里一致，
    # 否则拿到的不是「身份不对」而是 401 TOKEN_REVOKED，测不到我们想测的那道闸。
    return sign_staff_access(
        kind="staff", staff_id=CARE_WORKER_ID, role="care_worker", token_version=0
    )


def _admin_token() -> str:
    return sign_staff_access(kind="staff", staff_id=ADMIN_ID, role="admin", token_version=0)


async def _count(db: AsyncConnection, model: object, *where: object) -> int:
    result = await db.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


def _assert_media_path(url: str) -> None:
    assert url.startswith("/media/"), url
    assert not url.endswith(".html"), "MPA 兜底页面绝不能出现在 <audio src> 里"


# ---------------------------------------------------------------- library


async def test_library_lists_only_licensed_opera(client: AsyncClient, seeded: None) -> None:
    """`kind=opera`：只有两条授权戏曲（未授权那条不进列表），地址全部折成 /media/。"""
    response = await client.get(
        "/api/media/library",
        params={"kind": "opera"},
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "opera"
    assert [item["media_id"] for item in body["items"]] == [
        str(MEDIA_OPERA_A),
        str(MEDIA_OPERA_B),
    ], "未授权素材（MEDIA_OPERA_LOCKED）必须被挡在列表外"
    assert [item["title"] for item in body["items"]] == ["梁祝·十八相送", "苏三起解"]
    for item in body["items"]:
        assert item["kind"] == "opera"
        _assert_media_path(item["url"])
    # 绝对 URL 的 scheme/host 必须被剥掉，裸相对路径必须补上前缀。
    assert body["items"][0]["url"] == "/media/opera/yueju-liangzhu.mp3"
    assert body["items"][1]["url"] == "/media/opera/jingju-sujiao.mp3"
    assert body["items"][0]["duration_seconds"] == 1800


async def test_library_lists_movies_of_their_own_kind(client: AsyncClient, seeded: None) -> None:
    """`kind=movie` 只回电影：戏曲不能混进来（点播类型是路由此岸的第一道过滤）。"""
    response = await client.get(
        "/api/media/library",
        params={"kind": "movie"},
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "movie"
    assert [item["media_id"] for item in body["items"]] == [str(MEDIA_MOVIE)]
    _assert_media_path(body["items"][0]["url"])


async def test_library_rejects_missing_or_unknown_kind(client: AsyncClient, seeded: None) -> None:
    """`kind` 必填，非法值 422：不许默认成戏曲悄悄播错类别。"""
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))

    unknown = await client.get("/api/media/library", params={"kind": "news"}, headers=headers)
    assert unknown.status_code == 422, unknown.text
    body = unknown.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["detail"]
    assert body["trace_id"]

    missing = await client.get("/api/media/library", headers=headers)
    assert missing.status_code == 422, missing.text
    assert missing.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- play


async def test_play_writes_history_for_the_bound_elder(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """播放落 `media_history`：长者来自设备绑定，请求体里塞 elder_id 不生效。"""
    response = await client.post(
        f"/api/media/{MEDIA_OPERA_A}/play",
        json={"elder_id": str(ELDER_OTHER)},  # 冒名：端点不解析请求体，这张牌子在这里无效
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert uuid.UUID(body["history_id"])
    assert body["elder_id"] == str(ELDER_ID)
    assert body["item"]["media_id"] == str(MEDIA_OPERA_A)
    _assert_media_path(body["item"]["url"])

    row = (
        await db.execute(
            select(
                MediaHistory.elder_id,
                MediaHistory.media_id,
                MediaHistory.started_at,
                MediaHistory.finished_at,
                MediaHistory.completion_rate,
            ).where(MediaHistory.history_id == uuid.UUID(body["history_id"]))
        )
    ).one()
    assert row.elder_id == ELDER_ID, "请求体里的 ELDER_OTHER 绝不能覆盖设备绑定"
    assert row.media_id == MEDIA_OPERA_A
    assert row.started_at is not None
    assert row.finished_at is None, "刚开播：还没结束"
    assert row.completion_rate is None, "刚开播：完成度未知（NULL，不是 0）"


async def test_play_rejects_unlicensed_and_unknown_media(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """未授权 → 422；不存在 → 404；两种失败都不许留下半条历史。"""
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))

    locked = await client.post(f"/api/media/{MEDIA_OPERA_LOCKED}/play", headers=headers)
    assert locked.status_code == 422, locked.text
    # 授权失败必须是独立 `code`：与普通参数校验共用 `VALIDATION_ERROR` 会让前端
    # 把「这段没授权」渲染成「我发错了请求」，长者听到的是「出错了，再试试」。
    assert locked.json()["code"] == "COPYRIGHT_UNAUTHORIZED"
    assert locked.json()["detail"]

    missing = await client.post(f"/api/media/{uuid.uuid4()}/play", headers=headers)
    assert missing.status_code == 404, missing.text
    assert missing.json()["code"] == "NOT_FOUND"

    assert await _count(db, MediaHistory) == 0


# ---------------------------------------------------------------- finish


async def test_finish_records_completion_rate(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """播到一半关掉：`completion_rate=0.5`，`finished_at` 由服务端写。"""
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    played = await client.post(f"/api/media/{MEDIA_OPERA_A}/play", headers=headers)
    assert played.status_code == 200, played.text
    history_id = played.json()["history_id"]

    response = await client.post(
        "/api/media/finish",
        json={"history_id": history_id, "progress_ms": 900_000, "duration_ms": 1_800_000},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["history_id"] == history_id
    assert body["elder_id"] == str(ELDER_ID)
    assert body["media_id"] == str(MEDIA_OPERA_A)
    assert body["completion_rate"] == pytest.approx(0.5)
    assert body["finished_at"]

    row = (
        await db.execute(
            select(MediaHistory.finished_at, MediaHistory.completion_rate).where(
                MediaHistory.history_id == uuid.UUID(history_id)
            )
        )
    ).one()
    assert row.finished_at is not None
    assert row.completion_rate == Decimal("0.500")


async def test_finish_without_progress_keeps_rate_unknown(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """只报「结束了」不报播到哪：完成度写 NULL（不知道），不写 0（0 的含义是「被打断」）。"""
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    played = await client.post(f"/api/media/{MEDIA_OPERA_A}/play", headers=headers)
    history_id = played.json()["history_id"]

    response = await client.post(
        "/api/media/finish", json={"history_id": history_id}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["completion_rate"] is None
    rate = (
        await db.execute(
            select(MediaHistory.completion_rate).where(
                MediaHistory.history_id == uuid.UUID(history_id)
            )
        )
    ).scalar_one()
    assert rate is None


async def test_finish_rejects_other_elders_history(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """跨长者：A 的设备去收 B 的会话 → 403，且那条会话原封不动。"""
    other_headers = _bearer(_device_token(DEVICE_OTHER, SECRET_OTHER))
    played = await client.post(f"/api/media/{MEDIA_OPERA_A}/play", headers=other_headers)
    assert played.status_code == 200, played.text
    history_id = played.json()["history_id"]
    assert played.json()["elder_id"] == str(ELDER_OTHER)

    response = await client.post(
        "/api/media/finish",
        json={"history_id": history_id, "progress_ms": 1, "duration_ms": 1_800_000},
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "SCOPE_FORBIDDEN"
    finished_at = (
        await db.execute(
            select(MediaHistory.finished_at).where(MediaHistory.history_id == uuid.UUID(history_id))
        )
    ).scalar_one()
    assert finished_at is None, "越权请求不许顺手改掉别人的会话"


# ---------------------------------------------------------------- switch-next


async def test_switch_next_switches_and_marks_previous_interrupted(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """换一段：给下一段、把上一段记成被打断（completion_rate=0.0，偏好降权信号）。

    这也覆盖控制指令链路：规则层 `play_next` 经 `control_intent_from_rule()` 映射成
    `next`，落到本端点；端点自己不再判规则、不加规则条目。
    """
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    played = await client.post(f"/api/media/{MEDIA_OPERA_A}/play", headers=headers)
    assert played.status_code == 200, played.text
    first_history_id = played.json()["history_id"]

    response = await client.post("/api/media/switch-next", json={}, headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "opera"
    assert body["item"]["media_id"] == str(MEDIA_OPERA_B), "刚播过的不能原样再给"
    _assert_media_path(body["item"]["url"])

    row = (
        await db.execute(
            select(MediaHistory.finished_at, MediaHistory.completion_rate).where(
                MediaHistory.history_id == uuid.UUID(first_history_id)
            )
        )
    ).one()
    assert row.finished_at is not None
    assert row.completion_rate == Decimal("0.000")


async def test_switch_next_without_remaining_media_returns_404(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """两段都播过就真的没有下一段了：404，不许把当前段再塞回来充数。

    `switch_next` 只「挑」不「播」，所以排除集来自真正 `play` 过的历史——这里先播 A、
    换到 B 再播 B，两次播放占满整个授权池，第三次换段才该是空的。
    """
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    played_a = await client.post(f"/api/media/{MEDIA_OPERA_A}/play", headers=headers)
    assert played_a.status_code == 200, played_a.text
    switched = await client.post("/api/media/switch-next", json={}, headers=headers)
    assert switched.status_code == 200, switched.text
    assert switched.json()["item"]["media_id"] == str(MEDIA_OPERA_B)
    played_b = await client.post(f"/api/media/{MEDIA_OPERA_B}/play", headers=headers)
    assert played_b.status_code == 200, played_b.text

    response = await client.post("/api/media/switch-next", json={}, headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 身份边界


async def test_staff_token_cannot_play(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """员工令牌（generic 身份）不是设备身份：401，且不写任何历史。"""
    headers = _bearer(_staff_token())

    picked = await client.post("/api/media/pick", json={}, headers=headers)
    assert picked.status_code == 401, picked.text
    assert picked.json()["code"] == "TOKEN_INVALID"

    played = await client.post(f"/api/media/{MEDIA_OPERA_A}/play", headers=headers)
    assert played.status_code == 401, played.text

    assert await _count(db, MediaHistory) == 0


async def test_unbound_device_cannot_pick(client: AsyncClient, seeded: None) -> None:
    """未配对设备：409 device_unbound——没有长者就没有历史归属，先回配对页。"""
    response = await client.post(
        "/api/media/pick", json={}, headers=_bearer(_device_token(DEVICE_FREE, SECRET_FREE))
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "device_unbound"
    assert body["detail"]


async def test_pick_returns_a_playable_item(client: AsyncClient, seeded: None) -> None:
    """`pick` 默认戏曲，挑到的必须可播（授权过）且地址在 /media/ 下。"""
    response = await client.post(
        "/api/media/pick",
        json={},
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "opera"
    assert body["item"]["media_id"] == str(MEDIA_OPERA_A)
    _assert_media_path(body["item"]["url"])


# ---------------------------------------------------------------- 素材维护（管理员）
#
# FR-ADM-02：本地素材维护页的三个动词。长者端 `/api/media/library` 只读且只列授权
# 素材；这里的管理员视角必须**看见未授权条目**，否则「这条为什么播不了」没有答案。


async def _audit_actions(
    db: AsyncConnection, action: str, actor_id: uuid.UUID | None = None
) -> list[tuple[str, uuid.UUID, dict[str, object] | None]]:
    stmt = select(AuditLog.action, AuditLog.actor_id, AuditLog.detail).where(
        AuditLog.action == action
    )
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    stmt = stmt.order_by(AuditLog.created_at, AuditLog.audit_id)
    return [tuple(row) for row in (await db.execute(stmt)).all()]


async def test_admin_lists_all_items_including_unlicensed(
    client: AsyncClient, seeded: None
) -> None:
    """列表含未授权素材，并明确标 `playable=false`；带齐维护页需要的 ID 与原始地址。"""
    response = await client.get("/api/media/library/items", headers=_bearer(_admin_token()))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert body["limit"] == 50
    assert body["offset"] == 0
    by_id = {item["media_id"]: item for item in body["items"]}
    assert set(by_id) == {
        str(MEDIA_OPERA_A),
        str(MEDIA_OPERA_B),
        str(MEDIA_OPERA_LOCKED),
        str(MEDIA_MOVIE),
    }
    locked = by_id[str(MEDIA_OPERA_LOCKED)]
    assert locked["copyright_status"] == "unknown"
    assert locked["playable"] is False
    assert locked["media_url"] == "/media/opera/locked.mp3"
    assert locked["url"] == "/media/opera/locked.mp3"
    assert by_id[str(MEDIA_OPERA_A)]["playable"] is True


async def test_admin_list_filters_and_paginates(client: AsyncClient, seeded: None) -> None:
    headers = _bearer(_admin_token())

    operas = await client.get("/api/media/library/items", params={"kind": "opera"}, headers=headers)
    assert operas.status_code == 200, operas.text
    assert operas.json()["total"] == 3
    assert {item["kind"] for item in operas.json()["items"]} == {"opera"}

    yueju = await client.get("/api/media/library/items", params={"genre": "越剧"}, headers=headers)
    assert yueju.status_code == 200, yueju.text
    assert {item["media_id"] for item in yueju.json()["items"]} == {
        str(MEDIA_OPERA_A),
        str(MEDIA_OPERA_LOCKED),
    }

    first = await client.get(
        "/api/media/library/items", params={"limit": 2, "offset": 0}, headers=headers
    )
    second = await client.get(
        "/api/media/library/items", params={"limit": 2, "offset": 2}, headers=headers
    )
    assert first.status_code == second.status_code == 200
    first_ids = {item["media_id"] for item in first.json()["items"]}
    second_ids = {item["media_id"] for item in second.json()["items"]}
    assert len(first_ids) == len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)
    assert first.json()["total"] == second.json()["total"] == 4


@pytest.mark.parametrize("limit", [0, 201])
async def test_admin_list_rejects_out_of_range_limit(
    client: AsyncClient, seeded: None, limit: int
) -> None:
    response = await client.get(
        "/api/media/library/items",
        params={"limit": limit},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_care_worker_cannot_list_library_and_is_audited(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    before = len(await _audit_actions(db, "media.library.list", actor_id=CARE_WORKER_ID))
    response = await client.get("/api/media/library/items", headers=_bearer(_staff_token()))

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_actions(db, "media.library.list", actor_id=CARE_WORKER_ID)
    assert len(rows) == before + 1, "每次越权读都必须留一行，且不许多写"
    detail = rows[-1][2]
    assert detail is not None
    assert detail["result"] == "denied"
    assert detail["reason"] == "role_not_allowed"


async def test_device_token_cannot_list_library(client: AsyncClient, seeded: None) -> None:
    response = await client.get(
        "/api/media/library/items",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
    )

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "TOKEN_INVALID"


async def test_admin_patch_updates_only_given_fields_and_audits(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    response = await client.patch(
        f"/api/media/library/{MEDIA_OPERA_A}",
        json={"title": "梁祝·十八相送（修复版）"},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "梁祝·十八相送（修复版）"
    assert body["genre"] == "越剧", "没传的字段不能被动到"
    assert body["artist"] == "袁雪芬"

    stored = (
        await db.execute(
            select(MediaLibraryItem.title, MediaLibraryItem.genre).where(
                MediaLibraryItem.media_id == MEDIA_OPERA_A
            )
        )
    ).one()
    assert stored.title == "梁祝·十八相送（修复版）"
    assert stored.genre == "越剧"

    rows = await _audit_actions(db, "media.library.update", actor_id=ADMIN_ID)
    assert len(rows) == 1
    detail = rows[0][2]
    assert detail is not None
    assert detail["fields"] == ["title"]


async def test_admin_patch_empty_body_is_422(client: AsyncClient, seeded: None) -> None:
    """空补丁回成功会让维护页以为「保存了」，所以必须 422。"""
    response = await client.patch(
        f"/api/media/library/{MEDIA_OPERA_A}",
        json={},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_admin_patch_unknown_item_is_404(client: AsyncClient, seeded: None) -> None:
    response = await client.patch(
        f"/api/media/library/{uuid.uuid4()}",
        json={"title": "不存在"},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


async def test_admin_patch_duplicate_url_is_409(client: AsyncClient, seeded: None) -> None:
    response = await client.patch(
        f"/api/media/library/{MEDIA_OPERA_A}",
        json={"media_url": "opera/jingju-sujiao.mp3"},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CONFLICT"


async def test_admin_patch_kind_maps_to_type_column(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """对外字段叫 `kind`，库里列叫 `type`；换名只能发生在 API 层。"""
    response = await client.patch(
        f"/api/media/library/{MEDIA_OPERA_A}",
        json={"kind": "movie"},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "movie"
    stored = await db.scalar(
        select(MediaLibraryItem.type).where(MediaLibraryItem.media_id == MEDIA_OPERA_A)
    )
    assert stored == "movie"


async def test_admin_patch_explicit_null_clears_optional_field(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """显式 null＝清空；不传＝不动。两种语义不能压成一种。"""
    response = await client.patch(
        f"/api/media/library/{MEDIA_OPERA_A}",
        json={"genre": None},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 200, response.text
    assert response.json()["genre"] is None
    assert response.json()["artist"] == "袁雪芬"
    stored = (
        await db.execute(
            select(MediaLibraryItem.genre, MediaLibraryItem.artist).where(
                MediaLibraryItem.media_id == MEDIA_OPERA_A
            )
        )
    ).one()
    assert stored.genre is None
    assert stored.artist == "袁雪芬"


async def test_admin_patch_invalid_copyright_is_422(client: AsyncClient, seeded: None) -> None:
    response = await client.patch(
        f"/api/media/library/{MEDIA_OPERA_A}",
        json={"copyright_status": "whatever"},
        headers=_bearer(_admin_token()),
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "COPYRIGHT_UNAUTHORIZED"


async def test_care_worker_cannot_patch_and_is_audited(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    before = len(await _audit_actions(db, "media.library.update", actor_id=CARE_WORKER_ID))
    response = await client.patch(
        f"/api/media/library/{MEDIA_OPERA_A}",
        json={"title": "偷改"},
        headers=_bearer(_staff_token()),
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_actions(db, "media.library.update", actor_id=CARE_WORKER_ID)
    assert len(rows) == before + 1
    assert rows[-1][2] is not None
    assert rows[-1][2]["result"] == "denied"


async def test_admin_delete_removes_item_and_audits(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    response = await client.delete(
        f"/api/media/library/{MEDIA_MOVIE}", headers=_bearer(_admin_token())
    )

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert (
        await db.scalar(
            select(MediaLibraryItem.media_id).where(MediaLibraryItem.media_id == MEDIA_MOVIE)
        )
        is None
    )

    rows = await _audit_actions(db, "media.library.delete", actor_id=ADMIN_ID)
    assert len(rows) == 1
    detail = rows[0][2]
    assert detail is not None
    assert detail["title"] == "城南旧事"


async def test_admin_delete_with_history_is_409(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """播放历史是日报与偏好信号；删素材不能静默级联抹掉历史。"""
    await db.execute(
        MediaHistory.__table__.insert().values(
            elder_id=ELDER_ID,
            media_id=MEDIA_OPERA_A,
            completion_rate=Decimal("1.000"),
        )
    )

    response = await client.delete(
        f"/api/media/library/{MEDIA_OPERA_A}", headers=_bearer(_admin_token())
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CONFLICT"
    assert (
        await db.scalar(
            select(MediaLibraryItem.media_id).where(MediaLibraryItem.media_id == MEDIA_OPERA_A)
        )
        == MEDIA_OPERA_A
    )


async def test_admin_delete_unknown_item_is_404(client: AsyncClient, seeded: None) -> None:
    response = await client.delete(
        f"/api/media/library/{uuid.uuid4()}", headers=_bearer(_admin_token())
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


async def test_care_worker_cannot_delete_and_is_audited(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    before = len(await _audit_actions(db, "media.library.delete", actor_id=CARE_WORKER_ID))
    response = await client.delete(
        f"/api/media/library/{MEDIA_MOVIE}", headers=_bearer(_staff_token())
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "ROLE_FORBIDDEN"
    rows = await _audit_actions(db, "media.library.delete", actor_id=CARE_WORKER_ID)
    assert len(rows) == before + 1
    assert rows[-1][2] is not None
    assert rows[-1][2]["result"] == "denied"
