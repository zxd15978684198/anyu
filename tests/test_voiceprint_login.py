"""task-71 · 声纹免登录端点（`POST /api/device-sessions/voiceprint`）的接口级用例。

被测链路（全部真代码，只有两处替身）：

    真实 create_app() → multipart 解析 → 体积闸
    → voiceprint_login_service.login_with_voiceprint   （真）
        → Device 查询（真 SQL，连测试库）
        → voiceprint_service.get_status                （真 SQL：无样本必须**先于**容器拒）
        → voiceprint_service.cosine_compare_against_elder  ← 替身（不依赖真容器）
        → sign_device_session（真签发）
    → DeviceLoginResponse（与 E4 逐字段一致）

替身只有「声纹比对」这一处：真容器 `voiceprint:7003` 不在单测环境里，而本卡要验的是
「设备圈定 → 判定 → 签发 → 错误口径」，不是 CAM++ 推理本身（那段由
`tests/test_voiceprint_service.py` 覆盖）。测试库里的 `voiceprints` 行直接插入，
embedding 是**假密文**——因为比对函数被替换，这列永远不会被解密，故本文件不需要
Fernet 密钥。

为什么专门有一条「无样本不触容器」：`cosine_compare_against_elder` 的实现是**先调
容器再查库**，若登录编排不先看档案，没注册过长者的设备会白跑一次推理（外部依赖
抖动直接变成登录失败）。用例用「容器函数一被调用就炸」的替身把这条顺序钉死。

库不可达时按 conftest 口径 fail（`ANYU_ALLOW_DB_SKIP=1` 才 skip）。
"""

from __future__ import annotations

import io
import logging
import os
import uuid
import wave
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import KIND_DEVICE, TYP_DEVICE, decode_token
from app.db import get_session
from app.models import Bed, Device, Elder, Room
from app.models.voiceprint import Voiceprint
from app.services import voiceprint_service as vs
from conftest import require_db_connection

# 本文件只测「开关打开时」的链路；默认关的行为由 test_voiceprint_login_disabled.py 钉。
os.environ["VOICEPRINT_LOGIN_ENABLED"] = "true"

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "vp-login-test-secret-not-a-real-key-0123456789abcdef"

# ---------------------------------------------------------------- 固定 ID（全部虚构）
ROOM_ID = uuid.UUID("b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1")
BED_ID = uuid.UUID("b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2")
ELDER_ID = uuid.UUID("b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3")
ELDER_EMPTY_ID = uuid.UUID("b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4")
DEVICE_BOUND = uuid.UUID("b5b5b5b5-b5b5-4b5b-8b5b-b5b5b5b5b5b5")
DEVICE_NO_SAMPLES = uuid.UUID("b6b6b6b6-b6b6-4b6b-8b6b-b6b6b6b6b6b6")
DEVICE_LOCKED = uuid.UUID("b7b7b7b7-b7b7-4b7b-8b7b-b7b7b7b7b7b7")
ELDER_NAME = "王秀兰"
ELDER_EMPTY_NAME = "李长顺"

SECRET_SUFFIX = "device-long-term-secret-not-a-real-value"
#: 阈值取自 Settings 默认（0.55）；替身只回显它，用例不复制这套判定逻辑。
THRESHOLD = 0.55
GRANTED_SCORE = 0.87
REJECTED_SCORE = 0.31

LOGIN_PATH = "/api/device-sessions/voiceprint"


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
    """用真实 `create_app()` 装配，只把 get_session 指向测试事务。"""
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
    """一间房、两位长者、三台设备：

    - `DEVICE_BOUND` → `ELDER_ID`（有 1 段 active 声纹样本，可登录）
    - `DEVICE_NO_SAMPLES` → `ELDER_EMPTY_ID`（绑了人但没样本，409）
    - `DEVICE_LOCKED` → 未绑长者、status=locked（403；elder_id 留空是因为
      `uq_devices_elder_id` 部分唯一索引限制了「一位长者一台设备」）
    """
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="C-301", status="full")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {
                "elder_id": ELDER_ID,
                "name": ELDER_NAME,
                "birth_date": date(1946, 7, 1),
                "gender": "female",
                "status": "active",
            },
            {
                "elder_id": ELDER_EMPTY_ID,
                "name": ELDER_EMPTY_NAME,
                "birth_date": date(1948, 3, 2),
                "gender": "male",
                "status": "active",
            },
        ],
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
                "token_hash": "0" * 64,
                "status": "offline",
            },
            {
                "device_id": DEVICE_NO_SAMPLES,
                "room_id": ROOM_ID,
                "elder_id": ELDER_EMPTY_ID,
                "token_hash": "1" * 64,
                "status": "offline",
            },
            {
                "device_id": DEVICE_LOCKED,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": "2" * 64,
                "status": "locked",
            },
        ],
    )
    # 一段 active 样本：内容是假密文，比对函数被替身换掉后这列不会被解密。
    await db.execute(
        Voiceprint.__table__.insert().values(
            elder_id=ELDER_ID,
            version=1,
            sample_index=1,
            embedding=b"\x00" * 128,
            status="active",
        )
    )


# ---------------------------------------------------------------- 工具


def _wav_bytes(seconds: float = 0.1) -> bytes:
    """生成合法 WAV：16kHz / 16bit / 单声道静音。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * int(16_000 * seconds))
    return buffer.getvalue()


async def _login(
    client: AsyncClient,
    device_id: uuid.UUID | str,
    audio: bytes | None = None,
):
    payload = audio if audio is not None else _wav_bytes()
    return await client.post(
        LOGIN_PATH,
        data={"device_id": str(device_id)},
        files={"audio": ("voice.wav", payload, "audio/wav")},
    )


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"detail", "code", "trace_id"}, body
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


def _patch_match(
    monkeypatch: pytest.MonkeyPatch,
    *,
    matched: bool,
    score: float,
    elder_id: uuid.UUID = ELDER_ID,
) -> None:
    """替身：替换「容器 + 库内 cosine」这一整段，返回一个受控判定。

    该替身保留真实现的两个关键语义：阈值 0.55、`elder_id` 只在命中时回填
    （未命中为 None，与 `cosine_compare_against_elder` 的无样本分支一致）。
    """

    async def fake(session: object, *, elder_id: uuid.UUID, audio_bytes: bytes) -> vs.CosineMatch:
        del session, audio_bytes
        return vs.CosineMatch(
            matched=matched,
            score=score,
            threshold=THRESHOLD,
            elder_id=elder_id if matched else None,
        )

    monkeypatch.setattr(vs, "cosine_compare_against_elder", fake)


def _patch_container_bomb(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """容器替身：一旦被调用就记下来 —— 用来证明「无样本不触容器」。"""
    calls: list[str] = []

    async def bomb(audio_bytes: bytes) -> object:
        del audio_bytes
        calls.append("called")
        raise AssertionError("没有声纹样本时不应调用推理容器")

    monkeypatch.setattr(vs, "extract_embedding_from_container", bomb)
    return calls


# ---------------------------------------------------------------- 路由注册


def test_voiceprint_login_route_is_registered() -> None:
    """端点必须真的挂在 /api 下（挂错前缀就是 404，且只在线上才发现）。"""
    from app.main import create_app

    paths = set(create_app(get_settings()).openapi()["paths"])
    assert LOGIN_PATH in paths


# ---------------------------------------------------------------- 命中：签发会话


async def test_voiceprint_hit_issues_a_device_session(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """余弦 ≥ 阈值 → 与 E4 同形状的 24h 设备会话。"""
    _patch_match(monkeypatch, matched=True, score=GRANTED_SCORE)

    response = await _login(client, DEVICE_BOUND)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"access_token", "expires_in", "device"}, body
    assert isinstance(body["access_token"], str) and body["access_token"]
    # 24h 设备会话（与 /api/device-sessions 同一 device_ttl_seconds）
    assert body["expires_in"] == 86_400
    assert body["device"] == {
        "id": str(DEVICE_BOUND),
        "room_id": str(ROOM_ID),
        "elder_id": str(ELDER_ID),
    }
    # 令牌必须是真设备会话，不是装饰字符串：能按 device 类型解出来、sub/dev 指向本设备、
    # tv 等于 token_hash 前 16 位（解绑轮换即失效，契约 S-03）。
    claims = decode_token(body["access_token"], expected_typ=TYP_DEVICE)
    assert claims["dev"] == str(DEVICE_BOUND)
    assert claims["sub"] == f"{KIND_DEVICE}:{DEVICE_BOUND}"
    assert claims["tv"] == "0" * 16


async def test_voiceprint_hit_response_carries_no_similarity(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功响应里也不许出现分数：分数只属于日志。"""
    _patch_match(monkeypatch, matched=True, score=GRANTED_SCORE)

    response = await _login(client, DEVICE_BOUND)

    assert response.status_code == 200, response.text
    text = response.text
    assert "similarity" not in text.lower()
    assert str(GRANTED_SCORE) not in text


# ---------------------------------------------------------------- 低分：401 且不泄露分数


async def test_low_score_is_rejected_without_leaking_similarity(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_match(monkeypatch, matched=False, score=REJECTED_SCORE)

    response = await _login(client, DEVICE_BOUND)

    body = _assert_error(response, 401, "voiceprint_mismatch")
    text = response.text
    # 响应体只允许 detail/code/trace_id 三个键（_assert_error 已断）；
    # 再加一层：分数本身不能出现在任何位置，否则可以按 0.01 试探阈值。
    assert str(REJECTED_SCORE) not in text
    assert "similarity" not in text.lower()
    assert "score" not in text.lower()
    assert body["detail"] == "声纹不匹配，未能登录"


# ---------------------------------------------------------------- 无样本：409 且不触容器


async def test_device_without_samples_is_rejected_before_inference(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑了长者但该长者没有任何 active 样本 → 409；且不白跑一次容器推理。"""
    calls = _patch_container_bomb(monkeypatch)

    response = await _login(client, DEVICE_NO_SAMPLES)

    body = _assert_error(response, 409, "voiceprint_not_enrolled")
    assert body["detail"] == "该设备暂无可用声纹档案"
    assert calls == [], "没有声纹档案的设备不应触发推理容器"


async def test_unknown_device_looks_identical_to_unenrolled(
    client: AsyncClient, seeded: None
) -> None:
    """设备不存在与「有设备但没档案」必须无法区分 —— 否则 device_id 可被枚举。"""
    unknown = uuid.uuid4()

    unknown_response = await _login(client, unknown)
    empty_response = await _login(client, DEVICE_NO_SAMPLES)

    unknown_body = _assert_error(unknown_response, 409, "voiceprint_not_enrolled")
    empty_body = _assert_error(empty_response, 409, "voiceprint_not_enrolled")
    assert unknown_body["detail"] == empty_body["detail"]


# ---------------------------------------------------------------- 容器不可用：503


async def test_container_unavailable_is_503_and_hides_internals(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """推理容器 503/超时 → 503 声纹不可用；内部异常文本不回显。"""

    async def unavailable(
        session: object, *, elder_id: uuid.UUID, audio_bytes: bytes
    ) -> vs.CosineMatch:
        del session, elder_id, audio_bytes
        raise vs.VoiceprintExtractUnavailable("inference container timeout: secret-internal-detail")

    monkeypatch.setattr(vs, "cosine_compare_against_elder", unavailable)

    response = await _login(client, DEVICE_BOUND)

    body = _assert_error(response, 503, "voiceprint_unavailable")
    assert "secret-internal-detail" not in response.text
    assert body["detail"] == "声纹识别暂不可用，请使用设备令牌登录"


async def test_invalid_audio_reported_by_container_is_422(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容器 4xx（音频不可解析）→ 422 VALIDATION_ERROR，与 C4 注册端点同口径。"""

    async def reject_audio(
        session: object, *, elder_id: uuid.UUID, audio_bytes: bytes
    ) -> vs.CosineMatch:
        del session, elder_id, audio_bytes
        raise vs.VoiceprintInvalidAudio("音频无法解析")

    monkeypatch.setattr(vs, "cosine_compare_against_elder", reject_audio)

    response = await _login(client, DEVICE_BOUND)

    body = _assert_error(response, 422, "VALIDATION_ERROR")
    assert body["detail"] == "音频无法解析"


# ---------------------------------------------------------------- 设备锁定：403


async def test_locked_device_is_rejected_with_403(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_match(monkeypatch, matched=True, score=GRANTED_SCORE)

    response = await _login(client, DEVICE_LOCKED)

    _assert_error(response, 403, "device_locked")


# ---------------------------------------------------------------- 入参校验


async def test_empty_audio_is_rejected_with_422(client: AsyncClient, seeded: None) -> None:
    response = await _login(client, DEVICE_BOUND, audio=b"")

    _assert_error(response, 422, "VALIDATION_ERROR")


async def test_oversized_audio_is_rejected_with_413(client: AsyncClient, seeded: None) -> None:
    from app.api.voiceprints import MAX_SAMPLE_AUDIO_BYTES

    response = await _login(client, DEVICE_BOUND, audio=b"\x00" * (MAX_SAMPLE_AUDIO_BYTES + 1))

    _assert_error(response, 413, "PAYLOAD_TOO_LARGE")


# ---------------------------------------------------------------- 日志字段


async def test_log_records_only_device_score_and_outcome(
    client: AsyncClient,
    seeded: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """日志只允许 device_id / 相似度两位小数 / outcome，不许有音频与长者姓名。"""
    _patch_match(monkeypatch, matched=False, score=REJECTED_SCORE)

    with caplog.at_level(logging.INFO, logger="app.services.voiceprint_login_service"):
        audio = _wav_bytes()
        await _login(client, DEVICE_BOUND, audio=audio)

    records = [r for r in caplog.records if r.getMessage() == "voiceprint_login"]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    record = records[0]
    assert record.device_id == str(DEVICE_BOUND)
    # 两位小数：0.31 原样、0.8765 会被 round 成 0.88（本用例值是 0.31）
    assert record.similarity == REJECTED_SCORE
    assert record.outcome == "mismatch"
    rendered = str(record.__dict__)
    assert ELDER_NAME not in rendered, "日志里不许出现长者姓名"
    assert "RIFF" not in rendered, "日志里不许出现音频内容"


async def test_granted_log_marks_outcome_and_score(
    client: AsyncClient,
    seeded: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """命中路径同口径：outcome=granted、相似度仍是两位小数。"""
    _patch_match(monkeypatch, matched=True, score=0.8765)

    with caplog.at_level(logging.INFO, logger="app.services.voiceprint_login_service"):
        await _login(client, DEVICE_BOUND)

    record = next(r for r in caplog.records if r.getMessage() == "voiceprint_login")
    assert record.outcome == "granted"
    assert record.similarity == 0.88
