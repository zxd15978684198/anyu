"""迭代 4 收口 · C4 声纹 REST 三端点的接口级测试（补充 G-02：此前 REST 层零覆盖）。

覆盖范围（服务层用例已在 `tests/test_voiceprint_service.py`，这里只测 REST 契约）：

- 路由已挂载（G-01 的回归）：三个端点都能被解析到，不再是 404；
- 鉴权：无 token → 401；未绑定设备的 token → 409 `device_unbound`（C4 口径）；
- `POST /api/voiceprints/samples`：坏 WAV → 422 `VALIDATION_ERROR`（不进推理）；
  `sample_index` 越界 → 422；成功 → 200 且返回四键；重复上传 → 409 `duplicate_sample`；
  推理容器不可用 → 503 `vp_extract_unavailable`；
- `GET /api/voiceprints/status`：返回 `{elder_id, status, registered, version}`；
- `DELETE /api/voiceprints`：204 且 body 为空。

服务层用 monkeypatch 隔离：本文件的关注点是「HTTP → 参数校验 → 异常映射 → 序列化」，
embedding 提取/加密/入库由 `test_voiceprint_service.py` 独立覆盖，避免重复造容器。
无 PostgreSQL 时由 conftest 的 `require_db_connection()` 直接 fail（`ANYU_ALLOW_DB_SKIP=1` 才 skip）。
"""

from __future__ import annotations

import io
import logging
import uuid
import wave
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import voiceprints as voiceprints_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session
from app.models import Bed, Device, Elder, Room
from app.services.voiceprint_service import (
    VoiceprintDuplicateSample,
    VoiceprintExtractUnavailable,
    VoiceprintInvalidAudio,
    VoiceprintStatus,
    VoiceprintUnavailable,
)
from conftest import require_db_connection

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "vp-rest-test-secret-not-a-real-key-0123456789abcdef"

# ---------------------------------------------------------------- 固定 ID（全部虚构）
ROOM_ID = uuid.UUID("a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1")
BED_ID = uuid.UUID("a2a2a2a2-a2a2-4a2a-8a2a-a2a2a2a2a2a2")
ELDER_ID = uuid.UUID("a3a3a3a3-a3a3-4a3a-8a3a-a3a3a3a3a3a3")
DEVICE_BOUND = uuid.UUID("a4a4a4a4-a4a4-4a4a-8a4a-a4a4a4a4a4a4")
DEVICE_FREE = uuid.UUID("a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5")

SECRET_BOUND = "device-secret-voiceprint-bound-not-a-real-value"
SECRET_FREE = "device-secret-voiceprint-free-not-a-real-value"


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


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """一台绑定长者的设备 + 一台未绑定设备。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="B-201", status="full")
    )
    await db.execute(
        Elder.__table__.insert().values(
            elder_id=ELDER_ID,
            name="王秀兰",
            birth_date=date(1946, 7, 1),
            gender="female",
            status="active",
        )
    )
    await db.execute(
        Bed.__table__.insert().values(
            bed_id=BED_ID,
            room_id=ROOM_ID,
            bed_no="1 号床",
            status="occupied",
            elder_id=ELDER_ID,
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


def _wav_bytes(seconds: float = 0.1) -> bytes:
    """生成合法 WAV：16kHz / 16bit / 单声道静音。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * int(16_000 * seconds))
    return buffer.getvalue()


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


def _fake_status(status: str = "partial") -> VoiceprintStatus:
    return VoiceprintStatus(status=status, registered=[1], version=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 路由已挂载（G-01 回归）


def test_voiceprint_routes_are_registered() -> None:
    """三个端点必须出现在路由表里（此前模块写好但没登记，线上是 404）。"""
    from app.main import create_app

    paths = set(create_app(get_settings()).openapi()["paths"])
    assert "/api/voiceprints/samples" in paths
    assert "/api/voiceprints/status" in paths
    assert "/api/voiceprints" in paths


# ---------------------------------------------------------------- 鉴权


async def test_missing_token_returns_401(client: AsyncClient, seeded: None) -> None:
    """没带 `Authorization` 头 → 401 `TOKEN_INVALID`。

    期望值必须写字面量：本用例原先写 `_assert_error(response, 401, response.json()["code"])`，
    把响应自己带的 code 当期望值传进去，断言恒真 —— code 拼错、被改成别的值、甚至被
    换成 500 都测不出来。无 token 走 `app/core/auth.py` 的 `CredentialMissingError`，
    其 `code` 是 `TOKEN_INVALID`（不是 `MISSING_TOKEN`），所以这里钉死这个字面量。
    """
    response = await client.get("/api/voiceprints/status")
    _assert_error(response, 401, "TOKEN_INVALID")


async def test_unbound_device_returns_409_device_unbound(client: AsyncClient, seeded: None) -> None:
    """C4：status 在设备未绑长者时返回 409 device_unbound（不是 403/404）。"""
    response = await client.get(
        "/api/voiceprints/status", headers=_bearer(_device_token(DEVICE_FREE, SECRET_FREE))
    )
    _assert_error(response, 409, "device_unbound")


# ---------------------------------------------------------------- samples


async def test_register_sample_rejects_broken_wav(client: AsyncClient, seeded: None) -> None:
    """坏 WAV 在 REST 层就被拒，不进推理容器。"""
    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("bad.wav", b"not-a-wav-at-all", "audio/wav")},
        data={"sample_index": "1"},
    )
    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_register_sample_rejects_out_of_range_index(
    client: AsyncClient, seeded: None
) -> None:
    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("ok.wav", _wav_bytes(), "audio/wav")},
        data={"sample_index": "4"},
    )
    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_register_sample_success(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_register_sample(**kwargs: object) -> VoiceprintStatus:
        assert kwargs["sample_index"] == 1
        return _fake_status("partial")

    monkeypatch.setattr(voiceprints_api, "register_sample", fake_register_sample)

    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("ok.wav", _wav_bytes(), "audio/wav")},
        data={"sample_index": "1"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"elder_id", "version", "sample_index", "status"}
    assert body["elder_id"] == str(ELDER_ID)
    assert body["sample_index"] == 1
    assert body["status"] == "partial"


async def test_register_sample_duplicate_returns_409(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_register_sample(**kwargs: object) -> VoiceprintStatus:
        raise VoiceprintDuplicateSample("该样本已存在")

    monkeypatch.setattr(voiceprints_api, "register_sample", fake_register_sample)

    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("ok.wav", _wav_bytes(), "audio/wav")},
        data={"sample_index": "1"},
    )
    _assert_error(response, 409, "duplicate_sample")


async def test_register_sample_container_down_returns_503(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_register_sample(**kwargs: object) -> VoiceprintStatus:
        raise VoiceprintExtractUnavailable("容器不可用")

    monkeypatch.setattr(voiceprints_api, "register_sample", fake_register_sample)

    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("ok.wav", _wav_bytes(), "audio/wav")},
        data={"sample_index": "2"},
    )
    _assert_error(response, 503, "vp_extract_unavailable")


async def test_register_sample_config_unavailable_returns_503_vp_unavailable(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本进程声纹配置错（Fernet 密钥缺失）→ 503 `vp_unavailable`，不是 500。

    P2-D：`VoiceprintUnavailable` 与 `VoiceprintExtractUnavailable` 是兄弟类，
    REST 层只映射了后者，密钥配错时三个端点全 500 `INTERNAL_ERROR` —— 而 C4 无
    500 档，异常本身的语义（可重试的服务暂不可用）就是 503。
    """

    async def fake_register_sample(**kwargs: object) -> VoiceprintStatus:
        raise VoiceprintUnavailable("FERNET_KEY 未配置")

    monkeypatch.setattr(voiceprints_api, "register_sample", fake_register_sample)

    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("ok.wav", _wav_bytes(), "audio/wav")},
        data={"sample_index": "1"},
    )
    body = _assert_error(response, 503, "vp_unavailable")
    assert "声纹" in str(body["detail"])


async def test_register_sample_service_invalid_audio_returns_422(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_register_sample(**kwargs: object) -> VoiceprintStatus:
        raise VoiceprintInvalidAudio("容器判定非人声")

    monkeypatch.setattr(voiceprints_api, "register_sample", fake_register_sample)

    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("ok.wav", _wav_bytes(), "audio/wav")},
        data={"sample_index": "3"},
    )
    _assert_error(response, 422, "VALIDATION_ERROR")


# ---------------------------------------------------------------- P1 · 入站体积上限


async def test_register_sample_413_when_content_length_over_limit(
    client: AsyncClient, seeded: None
) -> None:
    """Content-Length 超上限 → 413 `PAYLOAD_TOO_LARGE`（整包读入内存之前就拒）。

    审计 P1④：原实现 `await audio.read()` 无上限，500MB 也会一次进内存。本用例发的
    body 远超 `MAX_REQUEST_BYTES`，若实现退化成「先读后校验」，这里照样能过 —— 所以
    另一个用例（chunked，无 Content-Length）用来钉死「实读长度兜底」这一半。
    """
    oversized = b"\x00" * (voiceprints_api.MAX_REQUEST_BYTES + 1_000)
    response = await client.post(
        "/api/voiceprints/samples",
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
        files={"audio": ("big.wav", oversized, "audio/wav")},
        data={"sample_index": "1"},
    )
    body = _assert_error(response, 413, voiceprints_api.PAYLOAD_TOO_LARGE)
    assert "过大" in body["detail"]


async def test_register_sample_413_when_chunked_body_over_limit(
    client: AsyncClient, seeded: None
) -> None:
    """chunked 无 Content-Length、实际超长 → 同样 413。

    只信 header 是过不了这个用例的：`content=<async generator>` 让 httpx 走
    `Transfer-Encoding: chunked`，请求里根本没有 `Content-Length`，唯一能拦住它的
    就是 `audio.read(上限 + 1)` 的实读长度判定。
    """
    boundary = "----voiceprint-p1-boundary"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="sample_index"\r\n\r\n1\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="big.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    payload = head + b"\x00" * (voiceprints_api.MAX_SAMPLE_AUDIO_BYTES + 1_000) + tail

    async def _body() -> AsyncIterator[bytes]:
        for offset in range(0, len(payload), 64 * 1024):
            yield payload[offset : offset + 64 * 1024]

    response = await client.post(
        "/api/voiceprints/samples",
        headers={
            **_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=_body(),
    )
    assert "content-length" not in {k.lower() for k in response.request.headers}, (
        "本用例的前提是请求不带 Content-Length，否则测不到实读兜底"
    )
    _assert_error(response, 413, voiceprints_api.PAYLOAD_TOO_LARGE)


async def test_register_sample_500_hides_internal_text(
    client: AsyncClient,
    seeded: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """兜底 500：不回显内部异常原文，但日志必须留下（含 traceback）。

    审计 P1④后半：原实现 `detail = f"声纹服务异常：{exc}"` 把内部路径/SQL 直接回给
    客户端，且 `logger` 一行都没打 —— 线上只能靠猜。现在对外固定话术 + 对内 exception。
    """
    internal_text = "VoiceprintError at /srv/vp/infer.py:42: cuda out of memory (key=sk-live-xxx)"

    async def fake_register_sample(**kwargs: object) -> VoiceprintStatus:
        raise RuntimeError(internal_text)

    monkeypatch.setattr(voiceprints_api, "register_sample", fake_register_sample)

    with caplog.at_level(logging.ERROR, logger="app.api.voiceprints"):
        response = await client.post(
            "/api/voiceprints/samples",
            headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
            files={"audio": ("ok.wav", _wav_bytes(), "audio/wav")},
            data={"sample_index": "1"},
        )

    body = _assert_error(response, 500, voiceprints_api.INTERNAL_ERROR)
    assert body["detail"] == "voiceprint service error"
    # 断言「原文没漏出去」：整段响应文本里不得出现异常内容
    assert "cuda out of memory" not in response.text
    assert "sk-live-xxx" not in response.text
    assert "/srv/vp/infer.py" not in response.text
    # 对内必须留下记录，且带 traceback（logger.exception 的 exc_info）
    records = [rec for rec in caplog.records if "voiceprint_register_failed" in rec.getMessage()]
    assert records, [rec.getMessage() for rec in caplog.records]
    assert records[0].exc_info is not None


# ---------------------------------------------------------------- status / delete


async def test_status_returns_four_keys(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_status(**kwargs: object) -> VoiceprintStatus:
        return VoiceprintStatus(status="active", registered=[1, 2, 3], version=2)

    monkeypatch.setattr(voiceprints_api, "get_status", fake_get_status)

    response = await client.get(
        "/api/voiceprints/status", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"elder_id", "status", "registered", "version"}
    assert body == {
        "elder_id": str(ELDER_ID),
        "status": "active",
        "registered": [1, 2, 3],
        "version": 2,
    }


async def test_delete_returns_204_empty_body(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[object] = []

    async def fake_delete(**kwargs: object) -> None:
        called.append(kwargs["elder_id"])

    monkeypatch.setattr(voiceprints_api, "delete_all_for_elder", fake_delete)

    response = await client.delete(
        "/api/voiceprints", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 204, response.text
    assert response.content == b""
    # identity.elder_id 在设备身份里是 str（runtime_context 口径），服务层按 SQL 比较，不受影响
    assert [str(x) for x in called] == [str(ELDER_ID)]
