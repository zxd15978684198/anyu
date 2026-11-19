"""`POST /api/dialogue/tts`：打字回复也要出声（字幕先到、声音后补）。

钉住的契约（2026-11-11，前端拿到 `/text` 的字幕后调本端点）：

1. 设备令牌鉴权，`text` strip 后 1–400 字；空串/纯空白/超 400 都是 422，不静默截断。
2. 正常：返回 `/static/mock/tts_*.wav` 与 `audio_duration_ms`（PCM 16k/mono/16bit，
   32 字节 = 1ms），`degraded=false`；文件真写到了 `static/mock/`（URL 不是装饰）。
3. 合成失败 / 空 pcm / 写盘失败 → **仍 200**，`audio_url=null`、`audio_duration_ms=null`、
   `degraded=true`：长者已经看到的那句话不该变成错误页。
4. 请求体塞 `elder_id` 被 `extra="ignore"` 丢弃，不影响结果（本端点不涉及身份，AGENTS §4-9）。
5. 日志只记 device_id / 字数 / 时长 / degraded，**不记合成原文**。

替身边界：只换 `get_tts` 依赖（`FakeTTS`），不真连 edge-tts / espeak-ng、不起容器；
合成与应用装配是真的，DB 是真实事务（teardown 回滚）。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import dialogue as dialogue_api
from app.audio.tts import TTSUnavailable
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session
from app.models import Device, Elder, Room
from app.services import tts_audio_service
from conftest import require_db_connection

# 随机 UUID：共享测试库上不与他人固定 ID 抢主键。
ROOM_ID = uuid.uuid4()
ELDER_ID = uuid.uuid4()
DEVICE_ID = uuid.uuid4()

DEVICE_SECRET = "dialogue-tts-device-secret-a"  # pragma: allowlist secret

TEXT = "今天有雨，您出门记得带伞。"
#: 16kHz / mono / s16le：32 字节 = 1 毫秒 → 6400 字节 = 200ms。
PCM = b"\x00" * 6400
PCM_DURATION_MS = 200

_STATIC_URL = re.compile(r"^/static/mock/tts_\d+_\d+\.wav$")


class FakeTTS:
    """`TTSProvider` 替身：记录被合成的文本，返回固定 PCM 或抛指定异常。"""

    name = "fake-tts"

    def __init__(self, *, pcm: bytes = PCM, error: BaseException | None = None) -> None:
        self.pcm = pcm
        self.error = error
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.pcm


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
async def seeded(db: AsyncConnection) -> None:
    """一间房、一位长者、一台绑定设备：鉴权需要设备行，本端点本身不读长者身份。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no=f"TTS-{ROOM_ID.hex[:6]}")
    )
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER_ID, name="配音测试长者", status="active")
    )
    await db.execute(
        Device.__table__.insert().values(
            device_id=DEVICE_ID,
            room_id=ROOM_ID,
            elder_id=ELDER_ID,
            token_hash=sha256_hex(DEVICE_SECRET),
            status="offline",
        )
    )


@pytest.fixture
async def application(db: AsyncConnection, seeded: None) -> AsyncIterator[Any]:
    """真实 `create_app()` 装配，只把 `get_session` 指向测试事务。"""
    from app.main import create_app

    app = create_app(get_settings())

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

    app.dependency_overrides[get_session] = override_get_session
    yield app


@pytest.fixture
def static_snapshot() -> Iterator[set[Path]]:
    """记录跑前已有的 `static/mock/*.wav`，跑完只删本用例新增的（目录已 gitignore）。"""
    root = Path("static/mock")
    before = set(root.glob("*.wav")) if root.is_dir() else set()
    yield before
    if not root.is_dir():
        return
    for path in set(root.glob("*.wav")) - before:
        with suppress(OSError):
            path.unlink()


@pytest.fixture
async def client(application: Any, static_snapshot: set[Path]) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _use_tts(application: Any, tts: Any) -> None:
    application.dependency_overrides[dialogue_api.get_tts] = lambda: tts


def _headers(device_id: uuid.UUID = DEVICE_ID, secret: str = DEVICE_SECRET) -> dict[str, str]:
    token = sign_device_session(
        device_id=device_id,
        token_hash_prefix=sha256_hex(secret)[:16],
    )
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 一、正常路径：URL 可播、时长由 PCM 反推、文件真的落地
# ===========================================================================


async def test_synthesizes_playable_wav(
    application: Any,
    client: AsyncClient,
    fake_tts: FakeTTS,
) -> None:
    _use_tts(application, fake_tts)

    response = await client.post(
        "/api/dialogue/tts", json={"text": f"  {TEXT}  "}, headers=_headers()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert _STATIC_URL.match(body["audio_url"] or ""), f"不是 nginx 可直出的静态地址：{body}"
    assert body["audio_duration_ms"] == PCM_DURATION_MS, (
        "时长必须由 PCM 长度反推（32 字节 = 1ms）：前端要靠它对齐字幕与口型"
    )
    assert body["degraded"] is False
    assert fake_tts.texts == [TEXT], "合成前必须 strip：多出的空白会让 TTS 读出停顿"

    written = Path(body["audio_url"].removeprefix("/static/mock/"))
    assert (Path("static/mock") / written).is_file(), "返回了 URL 但文件没写出来，前端会 404"


async def test_multi_sentence_returns_segments(
    application: Any,
    client: AsyncClient,
    fake_tts: FakeTTS,
) -> None:
    """多句回复要回逐句时间窗：打字机靠它跟着真实语音节奏走（2026-11-11）。"""
    _use_tts(application, fake_tts)

    response = await client.post(
        "/api/dialogue/tts",
        json={"text": "今天天气不错。要不要出去走走？"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    segments = body["audio_segments"]
    assert len(segments) == 2, f"两句必须切成两段：{segments}"
    assert segments[0]["start_ms"] == 0
    assert segments[0]["text"] == "今天天气不错。"
    assert segments[1]["start_ms"] == segments[0]["duration_ms"], (
        "第二句起点必须等于第一句时长（累计口径），否则逐字动画会跳拍"
    )
    assert body["audio_duration_ms"] == (segments[-1]["start_ms"] + segments[-1]["duration_ms"]), (
        "总时长必须等于最后一句的结束点"
    )
    assert len(fake_tts.texts) == 2, "必须逐句合成，才能拿到每句真实时长"


async def test_text_at_the_400_char_limit_is_accepted(
    application: Any,
    client: AsyncClient,
    fake_tts: FakeTTS,
) -> None:
    _use_tts(application, fake_tts)

    response = await client.post("/api/dialogue/tts", json={"text": "字" * 400}, headers=_headers())

    assert response.status_code == 200, response.text
    assert response.json()["degraded"] is False


# ===========================================================================
# 二、三种失败都只降级：仍 200，前端退回纯字幕
# ===========================================================================


async def test_tts_unavailable_degrades_but_stays_200(
    application: Any,
    client: AsyncClient,
) -> None:
    _use_tts(application, FakeTTS(error=TTSUnavailable("edge-tts failed: TLS handshake blocked")))

    response = await client.post("/api/dialogue/tts", json={"text": TEXT}, headers=_headers())

    assert response.status_code == 200, "TTS 掉线不是请求的错，不能把一句话变成 5xx"
    body = response.json()
    assert body == {
        "audio_url": None,
        "audio_duration_ms": None,
        "audio_segments": [],
        "degraded": True,
    }


async def test_empty_pcm_degrades_but_stays_200(
    application: Any,
    client: AsyncClient,
) -> None:
    _use_tts(application, FakeTTS(pcm=b""))

    response = await client.post("/api/dialogue/tts", json={"text": TEXT}, headers=_headers())

    assert response.status_code == 200
    assert response.json() == {
        "audio_url": None,
        "audio_duration_ms": None,
        "audio_segments": [],
        "degraded": True,
    }


async def test_write_failure_degrades_but_stays_200(
    application: Any,
    client: AsyncClient,
    fake_tts: FakeTTS,
) -> None:
    _use_tts(application, fake_tts)

    with patch.object(
        tts_audio_service, "_write_wav", new=AsyncMock(side_effect=OSError("disk full"))
    ):
        response = await client.post("/api/dialogue/tts", json={"text": TEXT}, headers=_headers())

    assert response.status_code == 200, "写盘失败也只是「这段没声音」，不是整轮失败"
    assert response.json() == {
        "audio_url": None,
        "audio_duration_ms": None,
        "audio_segments": [],
        "degraded": True,
    }


# ===========================================================================
# 三、鉴权与入参校验：与 /api/dialogue/text 同一套口径
# ===========================================================================


async def test_missing_token_is_401(client: AsyncClient) -> None:
    response = await client.post("/api/dialogue/tts", json={"text": TEXT})

    assert response.status_code == 401
    assert response.json()["code"]


async def test_forged_token_is_401(client: AsyncClient) -> None:
    response = await client.post(
        "/api/dialogue/tts",
        json={"text": TEXT},
        headers={"Authorization": "Bearer not-a-real-device-token"},
    )

    assert response.status_code == 401
    assert response.json()["code"]


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_blank_text_is_422(client: AsyncClient, text: str) -> None:
    response = await client.post("/api/dialogue/tts", json={"text": text}, headers=_headers())

    assert response.status_code == 422, "纯空白不是「合成一段静音」，是没话可说"


async def test_text_over_400_chars_is_422(client: AsyncClient) -> None:
    response = await client.post("/api/dialogue/tts", json={"text": "字" * 401}, headers=_headers())

    assert response.status_code == 422, "超长多半是误粘贴整段文字，不能整段合成拖住请求"


async def test_missing_text_field_is_422(client: AsyncClient) -> None:
    response = await client.post("/api/dialogue/tts", json={}, headers=_headers())

    assert response.status_code == 422


# ===========================================================================
# 四、身份字段注入无效 + 日志不落原文
# ===========================================================================


async def test_elder_id_in_body_is_ignored(
    application: Any,
    client: AsyncClient,
    fake_tts: FakeTTS,
) -> None:
    _use_tts(application, fake_tts)

    response = await client.post(
        "/api/dialogue/tts",
        json={"text": TEXT, "elder_id": str(uuid.uuid4()), "thread_id": "device:evil"},
        headers=_headers(),
    )

    assert response.status_code == 200, "多出的身份字段应被忽略，而不是报错"
    assert response.json()["degraded"] is False
    assert fake_tts.texts == [TEXT]


async def test_log_records_no_original_text(
    application: Any,
    client: AsyncClient,
    fake_tts: FakeTTS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_tts(application, fake_tts)
    caplog.set_level(logging.INFO, logger="app.api.dialogue")

    response = await client.post("/api/dialogue/tts", json={"text": TEXT}, headers=_headers())
    assert response.status_code == 200

    records = [record for record in caplog.records if record.getMessage() == "dialogue_tts"]
    assert records, "没有 dialogue_tts 日志：线上分不清「没调这个端点」与「合成了但前端没播」"
    record = records[0]
    assert getattr(record, "device_id", None) == str(DEVICE_ID)
    assert getattr(record, "chars", None) == len(TEXT)
    assert getattr(record, "degraded", None) is False

    rendered = caplog.text
    assert TEXT not in rendered, "日志里出现了合成原文：长者回复也是隐私（AGENTS §4-3）"


@pytest.fixture
def fake_tts() -> FakeTTS:
    return FakeTTS()
