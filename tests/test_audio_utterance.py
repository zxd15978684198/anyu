"""app/api/audio.py utterance 端点集成测试。

复用 test_auth_api.py 的 fixture 结构：真实 DB + NullPool 引擎 + 每用例事务 rollback。
核心复用：device-sessions 端点换会话拿 device_token → 调 utterance。

分层验证（AGENTS §2 硬约束）：audio.py **不 import services/models**，
只做参数校验 → Mock ASR/TTS → 序列化；本测试用真实 app + 真实 DB 验证完整链路。
"""

from __future__ import annotations

import io
import json
import struct
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import audio as audio_api
from app.audio.asr import MockASRProvider
from app.core.auth import (
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialRevokedError,
)
from app.core.config import get_settings
from app.core.security import TokenExpiredError, sha256_hex
from app.db import get_session as _app_get_session
from app.models.organization import Bed, Device, Elder, Room
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMReply

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEVICE_SECRET_PLAIN = "demo-device-secret-0001"  # pragma: allowlist secret
# 时长上限只有一个真源（audio.py 的 MAX_AUDIO_SECONDS，口径见 PRD FR-UI-01 的 120 秒）。
# 本文件曾把它抄成字面量 30——上限一改，抄的那份就变成「用例替错误实现作证」。
_MAX_AUDIO_SECONDS = audio_api.MAX_AUDIO_SECONDS


# ---------------------------------------------------------------------------
# 假 LLM 与假路由（R-7：让本文件不依赖任何外部服务）
# ---------------------------------------------------------------------------


class _EchoLLM:
    """回显假模型：同一个用户输入永远得到同一句回复。

    行为刻意保持"输入决定输出"，好让「两次请求回复必须逐字相同」这类断言
    由代码确定性保证，而不是由线上模型每次都愿意给同一句话来保证。
    """

    __slots__ = ()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        last = messages[-1]["content"] if messages else ""
        return LLMReply(text=f"我在听呢。您刚说的是：{last}", degraded=False)


async def _no_route_decision(*_args: Any, **_kwargs: Any) -> None:
    """假路由：一律不裁决，让请求直接落到 LLM 分支（不碰 Laya / Redis）。"""
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def audio_test_engine() -> AsyncIterator[AsyncEngine]:
    """NullPool 引擎 + 启动时清 DB。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "TRUNCATE elder_guardians, elder_staff_assignments, "
                    "guardian_accounts, staff_accounts, staff_tokens, "
                    "devices, elders, beds, personas, rooms CASCADE"
                )
            )
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def audio_test_session(audio_test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sf = async_sessionmaker(audio_test_engine, expire_on_commit=False)
    async with sf() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def audio_seeded_db(audio_test_session: AsyncSession) -> dict[str, Any]:
    """写入 seed 数据（只 flush 不 commit）。"""
    room = Room(room_no="A-301")
    audio_test_session.add(room)
    await audio_test_session.flush()

    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(
        room_id=room.room_id,
        token_hash=sha256_hex(_DEVICE_SECRET_PLAIN),
        status="offline",
    )
    audio_test_session.add_all([bed, device])
    await audio_test_session.flush()

    elder = Elder(name="张桂芬", status="active")
    audio_test_session.add(elder)
    await audio_test_session.flush()

    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    await audio_test_session.flush()

    return {
        "elder_id": elder.elder_id,
        "device_id": device.device_id,
    }


@pytest.fixture
async def app_for_audio(
    audio_test_session: AsyncSession, audio_seeded_db: dict[str, Any]
) -> FastAPI:
    """audio 路由 + 全局 exception handler（完整复制 auth 层 handler）。"""
    app = FastAPI()
    app.include_router(audio_api.router)

    async def _override_get_session():
        yield audio_test_session

    app.dependency_overrides[_app_get_session] = _override_get_session
    # ASR 固定用 Mock：本文件的用例断言的是「Mock 话术 + 契约形状」。
    # 若沿用 `.env` 里的 ASR_PROVIDER（开发机上常被设成 funasr），用例就依赖外部容器状态，
    # 容器没起时会退化成 503 —— 那是环境问题，不该让用例红。
    app.dependency_overrides[audio_api._get_default_asr] = lambda: MockASRProvider()
    # R-7：LLM 与路由也在这里钉死。
    # 原先只盖了 ASR，LLM 走真 provider（`get_llm()` → `build_llm(settings)` 打外网），
    # 于是 `test_client_elder_id_ignored` 断言的「两次回复逐字相同」依赖线上模型稳定，
    # provider 降级时其中一次会返回 `llm_timeout` 话术，用例无规律变红（实测在无本次改动
    # 的树上同样复现）。这里换成回显假模型 + 无路由，用例只依赖本进程内代码：
    # 同输入 → 同回复由确定性保证，不再由网速保证。
    app.dependency_overrides[audio_api.get_dialogue_service] = lambda: DialogueService(
        llm=_EchoLLM()
    )
    app.dependency_overrides[audio_api.get_route_text] = lambda: _no_route_decision

    # 全局 exception handler —— audio.py 鉴权用 Depends(current_device)
    # 会走 get_current_principal → 抛 Credential*Error，必须这里兜
    @app.exception_handler(CredentialInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"},
        )

    @app.exception_handler(CredentialExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(CredentialRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(TokenExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": "token has expired", "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    return app


@pytest.fixture
async def ac_audio(app_for_audio: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_for_audio)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def real_device_token(
    audio_test_session: AsyncSession, audio_seeded_db: dict[str, Any]
) -> str:
    """用全量 app 换 device access token（走真实 device-sessions 端点）。"""
    from app.api.auth import router as auth_router
    from app.core.auth import (
        CredentialExpiredError,
        CredentialInvalidError,
        CredentialRevokedError,
    )
    from app.core.security import TokenExpiredError
    from app.services.auth_service import (
        AuthConflictError,
        AuthExpiredError,
        AuthInvalidError,
        AuthRevokedError,
    )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")

    async def _override_get_session():
        yield audio_test_session

    app.dependency_overrides[_app_get_session] = _override_get_session

    # auth 层 handler（完整抄 test_auth_api.py::app_for_test）
    @app.exception_handler(AuthInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={
                "detail": str(exc.detail),
                "code": exc.kind or "TOKEN_INVALID",
                "trace_id": "-",
            },
        )

    @app.exception_handler(AuthExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc.detail), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(AuthRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc.detail), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(AuthConflictError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc.detail), "code": "CONFLICT", "trace_id": "-"},
        )

    @app.exception_handler(CredentialInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"},
        )

    @app.exception_handler(CredentialExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(CredentialRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(TokenExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": "token has expired", "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/device-sessions",
            json={
                "device_id": str(audio_seeded_db["device_id"]),
                "device_secret": _DEVICE_SECRET_PLAIN,
            },
        )
        assert resp.status_code == 200, f"device-sessions failed: {resp.status_code} {resp.text}"
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# 测试辅助：构造最小合法 WAV
# ---------------------------------------------------------------------------


def _make_wav(num_samples: int, sample_rate: int = 16_000) -> bytes:
    """生成全 0 PCM16 WAV（单声道）。"""
    raw = struct.pack(f"<{num_samples}h", *([0] * num_samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)
    return buf.getvalue()


def _silent_wav(duration_ms: int, sample_rate: int = 16_000) -> bytes:
    """生成指定时长、**内容真实**的全 0 PCM16 WAV。

    与 `_make_wav` 的区别只在采样用 `b"\\x00\\x00"` 批量拼：120 秒是 1_920_000 个采样，
    逐个 pack 一个 list 只为造静音，既慢又占内存。
    """
    frames = b"\x00\x00" * (sample_rate * duration_ms // 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(frames)
    return buf.getvalue()


def _wav_header_only(duration_ms: int, sample_rate: int = 16_000) -> bytes:
    """只声明时长、不携带采样的 WAV 头（44B）。

    时长闸在 ASR 之前就按 WAV 头算完，用它做 119/120/121 秒的边界判定可以只看「闸的口径」，
    不必为每个用例真的搬几 MB 静音。Mock ASR 的后台 drain 一帧都收不到也照脚本收敛，
    所以哪怕 data 块为空，链路仍能走完——这点由下面的真字节用例做交叉验证。
    """
    num_frames = sample_rate * duration_ms // 1000
    data_size = num_frames * 2  # 单声道 16bit
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    head = struct.pack("<4sI", b"RIFF", 4 + 8 + len(fmt) + 8 + data_size) + b"WAVE"
    return (
        head
        + struct.pack("<4sI", b"fmt ", len(fmt))
        + fmt
        + struct.pack("<4sI", b"data", data_size)
    )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_token_401(ac_audio: httpx.AsyncClient) -> None:
    """伪造 token → 401 + ErrorResponse 包装（含 detail / trace_id）。"""
    wav = _make_wav(16_000)  # 1s
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": "Bearer fake.fake.fake"},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 401, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "detail" in body and "trace_id" in body


@pytest.mark.asyncio
async def test_utterance_success(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """正常路径：鉴权通过 → Mock ASR 成功 → Mock TTS 成功 → 200。"""
    wav = _make_wav(16_000)  # 1s
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    # 契约 C3 响应形状
    assert "text" in body
    assert "reply" in body and "text" in body["reply"] and "audio_url" in body["reply"]
    # 回复来自对话链（run_text_turn），不再是 迭代 3 的写死回显；
    # 具体文案取决于路由/模型可用性，这里只钉「有话说、且不是写死话术」。
    reply_text = body["reply"]["text"]
    assert reply_text.strip(), "对话链给出了空回复"
    assert "您说的是" not in reply_text, "回复仍是写死的回显话术"


def test_audio_limits_share_one_source() -> None:
    """时长闸与体积闸必须同源（PRD FR-UI-01）。

    这条断言钉的是「改上限只需动一个数字」：如果哪天有人把 MAX_AUDIO_BYTES 写回字面量，
    时长放开了、体积还卡在旧值，越靠近上限反而越容易被拒——两个闸互相打架时，
    长者看到的永远是「说话有点长」，而代码里写着 120 秒。
    """
    assert _MAX_AUDIO_SECONDS == 120, "FR-UI-01：单次最长 120 秒"
    assert audio_api.MAX_AUDIO_BYTES == (
        _MAX_AUDIO_SECONDS * 16_000 * 2 + audio_api.WAV_CONTAINER_OVERHEAD_BYTES
    )
    assert (
        audio_api.MAX_REQUEST_BYTES
        == audio_api.MAX_AUDIO_BYTES + audio_api.MULTIPART_OVERHEAD_BYTES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_ms", [119_000, 120_000])
async def test_utterance_up_to_limit_accepted(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
    duration_ms: int,
) -> None:
    """119 / 120 秒都在限内 → 200。服务端判据是 `>`，录满上限的那一句必须收得下。"""
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("limit.wav", _wav_header_only(duration_ms), "audio/wav")},
        data={"duration_ms": str(duration_ms)},
    )
    assert resp.status_code == 200, f"{duration_ms}ms 被拒收：{resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_utterance_exactly_limit_with_real_bytes_accepted(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """刚好 120 秒的**真实字节** WAV 也要收下（容器头 44B 不能把它顶成超体积）。

    上面的 header-only 用例只证明「时长闸认 120 秒」，这一条补另一半：实读体积闸
    （`read(上限 + 1)`）如果只按纯 PCM 算额度，录满 120 秒的文件恰好多 44B 被拒，
    而 119 秒的用例仍然全绿——边界上最该跑通的一格是唯一漏掉的。
    """
    wav = _silent_wav(120_000)
    assert len(wav) == 120 * 16_000 * 2 + audio_api.WAV_CONTAINER_OVERHEAD_BYTES
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("limit.wav", wav, "audio/wav")},
        data={"duration_ms": "120000"},
    )
    assert resp.status_code == 200, f"刚好 120 秒被拒收：{resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_utterance_one_second_over_limit_413(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """121 秒 → 413，且提示里的秒数来自同一真源。

    旧用例传 31 秒（`_make_wav(16_000 * 31)`）：在 30 秒上限下它撞的其实是**体积闸**，
    改到 120 秒之后 31 秒已经合法，这条用例只剩「随便什么 413」的形状，必须换成真越界。
    """
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("long.wav", _wav_header_only(121_000), "audio/wav")},
        data={"duration_ms": "121000"},
    )
    assert resp.status_code == 413, f"got {resp.status_code}: {resp.text}"
    detail = str(resp.json().get("detail", ""))
    assert f"{_MAX_AUDIO_SECONDS}s" in detail, f"413 提示没有引用真源上限：{detail}"


@pytest.mark.asyncio
async def test_utterance_empty_file_422(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """空音频文件 → 422。"""
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_client_elder_id_ignored(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """客户端塞 elder_id / thread_id 一律丢弃：响应必须与「不塞」的对照组逐字一致（禁区 9）。

    旧写法只断言 `status_code == 200`——端点就算真把客户端的 elder_id 当身份用，
    用例照样绿（恒真）。这里改成对照组：同一段音频字节、同一个设备令牌，只差伪造字段，
    `text` 与 `reply.text` 必须完全相同，且响应序列化后不得出现伪造值。
    端点若哪天改成「未知字段直接 422」，422 分支把「拒收」这一语义也钉住，
    不会退化成「随便什么状态码都放过」。
    """
    wav = _make_wav(16_000)  # 1s；两个请求用**同一份**字节，识别结果才可比
    headers = {"Authorization": f"Bearer {real_device_token}"}
    forged_elder = "00000000-0000-0000-0000-000000000000"
    forged_thread = "forged-thread-not-a-real-session"

    baseline = await ac_audio.post(
        "/api/audio/utterance",
        headers=headers,
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert baseline.status_code == 200, f"对照组就失败了：{baseline.status_code} {baseline.text}"

    forged = await ac_audio.post(
        "/api/audio/utterance",
        headers=headers,
        data={"elder_id": forged_elder, "thread_id": forged_thread},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )

    if forged.status_code == 422:
        # 合规的另一半：显式拒收未知字段，而且必须**因为这两个字段**被拒
        assert (
            forged_elder in forged.text or "elder_id" in forged.text or "thread_id" in forged.text
        ), forged.text
        return

    assert forged.status_code == 200, f"got {forged.status_code}: {forged.text}"
    body = forged.json()
    base_body = baseline.json()

    # ① 同输入 → 同识别结果、同话术：多传字段没有改变任何语义
    assert body["text"] == base_body["text"]
    assert body["reply"]["text"] == base_body["reply"]["text"]

    # ② 服务端确实没用它们：伪造值不得出现在任何一段回显里（elder_id 只来自设备绑定）
    serialized = json.dumps(body, ensure_ascii=False)
    assert forged_elder not in serialized, serialized
    assert forged_thread not in serialized, serialized
    assert forged_elder not in body["text"]
    assert forged_elder not in body["reply"]["text"]
