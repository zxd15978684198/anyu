"""日报语音条（FR-RPT-04）：文本同源、格式可播、失败不连坐、空则不假装有音频。

四条验收各自对应用例，缺一条都不算做完：

1. **文本同源**（PRD:412「内容与文本摘要一致」）：送给 TTS 的文本必须**逐字等于**从
   已落库 `payload["health"]` / `payload["medication"]` 推出来的那句话（与页面消费的是
   同一份快照），不是"差不多"也不是另拼一份播报词；顺带锁住 `payload` 的键集不变
   （那是家属端契约，见 `tests/test_family_reports.py`）；
2. **格式可播**：落盘必须是 `RIFF/WAVE`、16kHz / 16bit / 单声道，帧数与
   `mock_duration_seconds()` 推出来的一致，且**不是静音**（峰值 > 0）；
3. **失败不连坐**：TTS 抛 `TTSUnavailable` 时日报照常落库、`audio_url` 留 NULL、
   磁盘上不留半截文件，日志里有 `daily report audio generation failed`；
4. **空则不播**：`audio_url` 为空时音频端点返回 404 `daily report audio not found`，
   日报接口原样返回 `audio_url: null`——家属端 `audioUrlOf()` 据此整块不渲染播放器，
   前端侧的反向用例见 `frontend/src/family/reportAudio.test.mjs`。

## 为什么给 TTS 打桩，却不另造波形

`.env` 里的 `TTS_PROVIDER` 可能是 edge（要 ffmpeg + 网络）也可能是 mock，用例不该
由环境决定成败。这里只把「从哪拿 provider」换成假对象，**波形仍由真实的
`MockTTSProvider` 生成**：既不重复实现一套 TTS（AGENTS §4.8），断言也不依赖网络。

## 测试怎么不污染库

与 `tests/test_reports_api.py` 同一套做法：外层事务 + `get_session` 覆盖成 savepoint
会话，teardown 整体 rollback；音频目录整体指向 `tmp_path`，不往仓库 `data/` 写。
"""

from __future__ import annotations

import array
import datetime
import io
import logging
import uuid
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.api import reports as reports_api
from app.audio import tts as tts_module
from app.audio.tts import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    MockTTSProvider,
    TTSUnavailable,
    mock_duration_seconds,
)
from app.core.auth import get_current_principal
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import DailyReport, Elder, ElderGuardian, GuardianAccount, HealthRecord
from app.scheduler import health as health_jobs
from app.services import report_service

#: 被测自然日。与调度任务同一口径（`scheduler_timezone`），避免测试里再算一次时区。
DAY = health_jobs._today()


# --------------------------------------------------------------------- fixtures


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """外层事务：本用例写的行（日报、审计日志）最后整体回滚。"""
    conn = await require_db_connection(db_engine)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await trans.rollback()
        await session.close()
        await conn.close()


@dataclass
class Seed:
    admin_id: uuid.UUID
    guardian_id: uuid.UUID
    stranger_id: uuid.UUID
    elder_id: uuid.UUID
    day: datetime.date


async def _seed(db: AsyncSession) -> Seed:
    admin_id, guardian_id, stranger_id, elder_id = (uuid.uuid4() for _ in range(4))
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": "张桂芬", "status": "active"}],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [
            {
                "guardian_id": guardian_id,
                "name": "家属张",
                "password_hash": "x",
                "status": "active",
            },
            {
                "guardian_id": stranger_id,
                "name": "家属王",
                "password_hash": "x",
                "status": "active",
            },
        ],
    )
    # 只有 guardian_id 绑定了 elder_id；stranger_id 是「别人家的家属」，用来验 403。
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "guardian_id": guardian_id,
                "relation": "daughter",
                "status": "active",
            }
        ],
    )
    # 一条当日绿色记录：让摘要有血压/心率可念，不至于退化成「没有采集到记录」。
    await db.execute(
        HealthRecord.__table__.insert(),
        [
            {
                "record_id": uuid.uuid4(),
                "elder_id": elder_id,
                "record_date": DAY,
                "source": "staff_scheduled",
                "grade": "green",
                "systolic_mmhg": 128,
                "diastolic_mmhg": 78,
                "heart_rate_bpm": 72,
                "sleep_hours": 7.0,
                "mood_score": 4,
                "revision": 1,
                "collected_at": datetime.datetime(
                    DAY.year, DAY.month, DAY.day, 1, tzinfo=datetime.UTC
                ),
            }
        ],
    )
    await db.flush()
    return Seed(
        admin_id=admin_id,
        guardian_id=guardian_id,
        stranger_id=stranger_id,
        elder_id=elder_id,
        day=DAY,
    )


@pytest.fixture
async def seeded(db: AsyncSession) -> Seed:
    return await _seed(db)


@dataclass
class Bundle:
    client: AsyncClient
    #: 每次请求前 append 一项即可切换调用主体；覆盖依赖读的是同一个列表。
    holder: list[Identity] = field(default_factory=list)


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[Bundle]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(reports_api.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db.bind,  # type: ignore[arg-type]
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

    holder: list[Identity] = []

    def override_principal() -> Identity:
        return holder[0]

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_principal] = override_principal

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        yield Bundle(client=ac, holder=holder)


@pytest.fixture
def audio_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把语音条落盘目录整体搬到 `tmp_path`，不碰仓库里的 `data/report_audio`。"""
    root = tmp_path / "report_audio"
    monkeypatch.setattr(report_service, "REPORT_AUDIO_ROOT", root)
    return root


@dataclass
class RecordingTTS:
    """记录被念的文本；波形仍交给真实 `MockTTSProvider`（不另造 TTS）。"""

    texts: list[str] = field(default_factory=list)
    fail_with: Exception | None = None

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.fail_with is not None:
            raise self.fail_with
        return await MockTTSProvider().synthesize(text)


@pytest.fixture
def tts(monkeypatch: pytest.MonkeyPatch) -> RecordingTTS:
    """只替换「provider 从哪来」。

    `report_service._generate_report_audio` 内部是**函数内惰性 import**
    （`from app.audio.tts import build_tts`），每次调用都重新取模块属性，
    所以打在 `app.audio.tts.build_tts` 上就能生效。
    """
    provider = RecordingTTS()
    monkeypatch.setattr(tts_module, "build_tts", lambda: provider)
    return provider


def _as_guardian(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.guardian_id))


def _as_stranger(seed: Seed) -> Identity:
    return Identity(kind="guardian", guardian_id=str(seed.stranger_id))


def _as_admin(seed: Seed) -> Identity:
    return Identity(kind="staff", staff_id=str(seed.admin_id), role="admin")


def _audio_path(root: Path, seed: Seed) -> Path:
    return root / str(seed.elder_id) / f"{seed.day.isoformat()}.wav"


def _read_wav(path: Path) -> tuple[tuple[int, int, int], int, bytes]:
    """读回落盘 WAV：返回 (声道/位宽/采样率, 帧数, PCM 字节)。"""
    with wave.open(io.BytesIO(path.read_bytes()), "rb") as handle:
        header = (handle.getnchannels(), handle.getsampwidth(), handle.getframerate())
        frames = handle.getnframes()
        return header, frames, handle.readframes(frames)


def _stray_files(root: Path) -> list[str]:
    """列残留的临时文件。

    放进同步函数是因为异步用例里做目录遍历会触发 ASYNC240（阻塞式 `rglob`）；
    这里不是被测对象，没必要为了一个断言再包一层 `to_thread`。
    """
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


# ------------------------------------------------------- 服务层：生成即产语音条


async def test_generate_writes_wav_from_the_stored_payload(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS
) -> None:
    """日报落库 → 语音条同步落盘，且念的就是页面看到的那份文本（PRD:412）。"""
    report = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    # ⓪ 本卡不新增 payload 字段（键集是家属端契约 O6）：播报文本从落库的两块现推。
    assert set(report.payload) == {"elder_id", "report_date", "health", "medication", "sources"}
    summary = report_service._summary_text(
        report.report_date, report.payload["health"], report.payload["medication"]
    )
    assert summary.startswith(
        f"{seeded.day.year}年{seeded.day.month}月{seeded.day.day}日的健康日报。"
    )
    assert "血压 128/78 毫米汞柱" in summary, "念的必须是与页面同源的数值"
    assert summary.endswith(report_service.REPORT_DISCLAIMER)

    # ① 文本同源：送给 TTS 的就是这句，一字不差、只念一次。
    assert tts.texts == [summary], "语音必须念落库快照推出的文本，不许另拼一份播报词"

    # ② 地址写进 reports.audio_url（接口地址，不是磁盘路径）。
    expected_url = f"/api/reports/{seeded.elder_id}/audio?date={seeded.day.isoformat()}"
    assert report.audio_url == expected_url

    stored = await db.scalar(
        select(DailyReport).where(
            DailyReport.elder_id == seeded.elder_id, DailyReport.report_date == seeded.day
        )
    )
    assert stored is not None
    assert stored.audio_url == expected_url, "audio_url 必须真的写进 daily_reports 行"

    # ③ 文件可播：RIFF/WAVE + 16kHz/16bit/单声道 + 帧数与 Mock 时长规则一致。
    path = _audio_path(audio_root, seeded)
    assert path.is_file(), f"语音条没落盘：{path}"
    header, frames, pcm = _read_wav(path)
    assert header == (PCM_CHANNELS, PCM_SAMPLE_WIDTH, PCM_SAMPLE_RATE) == (1, 2, 16_000)
    assert frames == round(mock_duration_seconds(summary) * PCM_SAMPLE_RATE)

    # ④ 不是静音：Mock 波形是正弦扫频，峰值必须明显大于 0（防「生成了空文件也算过」）。
    samples = array.array("h")
    samples.frombytes(pcm)
    assert max(abs(sample) for sample in samples) > 1_000


async def test_tts_failure_keeps_report_and_leaves_audio_null(
    db: AsyncSession,
    seeded: Seed,
    audio_root: Path,
    tts: RecordingTTS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TTS 挂了也不能连坐日报：日报照出、`audio_url` 留 NULL、不留半截文件。"""
    tts.fail_with = TTSUnavailable("edge-tts unreachable")
    with caplog.at_level(logging.INFO):
        report = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()

    assert report.payload["health"]["total"] == 1, "日报内容照常生成，缺的只是语音"
    assert report.audio_url is None

    stored = await db.scalar(
        select(DailyReport).where(
            DailyReport.elder_id == seeded.elder_id, DailyReport.report_date == seeded.day
        )
    )
    assert stored is not None, "日报必须照常落库"
    assert stored.audio_url is None

    assert not _audio_path(audio_root, seeded).exists()
    assert [name for name in _stray_files(audio_root) if name.endswith(".tmp")] == [], (
        "不许留临时文件"
    )

    messages = [record.getMessage() for record in caplog.records]
    assert "daily report audio generation failed" in messages


async def test_payload_without_blocks_skips_audio(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS
) -> None:
    """payload 缺健康/用药块（历史行 / 结构变更）时：不合成、不报错、不改 audio_url。"""
    report = await report_service.generate(db, seeded.elder_id, seeded.day)
    calls_before = len(tts.texts)
    report.payload = {"elder_id": str(seeded.elder_id), "report_date": seeded.day.isoformat()}
    await db.flush()

    assert await report_service._generate_report_audio(report) is None
    assert len(tts.texts) == calls_before, "payload 不完整就不该调 TTS"
    assert report.audio_url, "已有地址不被这次空跑改写"
    assert report.audio_url == f"/api/reports/{seeded.elder_id}/audio?date={seeded.day.isoformat()}"


# ---------------------------------- 复用路径：老日报补齐、幂等、不连坐（Lead 裁决 a）


async def test_reuse_path_backfills_audio_for_legacy_report(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS
) -> None:
    """复用路径补齐语音：迭代 8 之前的老日报（`audio_url` 为 NULL）被取回时补上。

    对应 Lead 裁决 (a)——复用路径本身就该保证「日报可播」，回填脚本只是补数据。
    """
    legacy = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    # 还原成「迭代 8 时代」的样子：内容在、语音从没生成过。
    legacy.audio_url = None
    await db.flush()
    legacy_path = _audio_path(audio_root, seeded)
    legacy_path.unlink()
    calls_before = len(tts.texts)

    reused = await report_service.generate(db, seeded.elder_id, seeded.day)

    assert reused.report_id == legacy.report_id, "复用同一行，不许重建日报"
    assert len(tts.texts) == calls_before + 1, "复用路径必须补合成一次"
    assert reused.audio_url == f"/api/reports/{seeded.elder_id}/audio?date={seeded.day.isoformat()}"
    assert legacy_path.is_file(), "补出来的 WAV 必须真的落盘"

    header, _frames, pcm = _read_wav(legacy_path)
    assert header == (PCM_CHANNELS, PCM_SAMPLE_WIDTH, PCM_SAMPLE_RATE)
    samples = array.array("h")
    samples.frombytes(pcm)
    assert max(abs(sample) for sample in samples) > 1_000, "补出来的也必须是能听见的声音"


async def test_reuse_path_skips_synthesis_when_audio_is_ready(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS
) -> None:
    """幂等：地址在、文件也在 → 复用路径不再调 TTS（重复打开日报页不该反复合成）。"""
    first = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    assert first.audio_url and _audio_path(audio_root, seeded).is_file()
    calls_before = len(tts.texts)

    again = await report_service.generate(db, seeded.elder_id, seeded.day)

    assert again.report_id == first.report_id
    assert len(tts.texts) == calls_before, "已有语音条时复用路径必须直接跳过"
    assert again.audio_url == first.audio_url


async def test_reuse_path_rebuilds_when_audio_file_was_cleaned(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS
) -> None:
    """地址在但文件被清掉（清过 `data/` 目录）→ 视为需重建，而不是永远 404。"""
    first = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    url_before = first.audio_url
    path = _audio_path(audio_root, seeded)
    path.unlink()
    calls_before = len(tts.texts)

    reused = await report_service.generate(db, seeded.elder_id, seeded.day)

    assert reused.report_id == first.report_id
    assert len(tts.texts) == calls_before + 1, "文件缺了就必须重建"
    assert reused.audio_url == url_before, "地址由长者+日期推导，重建后不变"
    assert path.is_file(), "重建必须把文件写回去"


async def test_reuse_path_audio_failure_does_not_break_reuse(
    db: AsyncSession,
    seeded: Seed,
    audio_root: Path,
    tts: RecordingTTS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """补语音失败也不连坐：日报照旧复用返回，`audio_url` 留 NULL、日志留痕。"""
    legacy = await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    legacy.audio_url = None
    await db.flush()
    _audio_path(audio_root, seeded).unlink()

    tts.fail_with = TTSUnavailable("tts down")
    with caplog.at_level(logging.INFO):
        reused = await report_service.generate(db, seeded.elder_id, seeded.day)

    assert reused.report_id == legacy.report_id, "复用行为不变：照样返回那一行"
    assert reused.audio_url is None, "补不出来就留 NULL，不许假装有音频"
    assert reused.payload["health"]["total"] == 1, "日报内容照旧"
    messages = [record.getMessage() for record in caplog.records]
    assert "daily report audio generation failed" in messages


# --------------------------------------------------------- 接口：可点播 / 不放宽


async def test_audio_endpoint_streams_wav_for_bound_guardian(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS, client: Bundle
) -> None:
    """绑定家属能点播：200 + `audio/wav` + 真的是 WAV 头。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}/audio", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["cache-control"] == "private, max-age=300", "私人健康数据不许公共缓存"

    body = resp.content
    assert body[:4] == b"RIFF"
    assert body[8:12] == b"WAVE"
    assert len(body) == _audio_path(audio_root, seeded).stat().st_size


async def test_audio_endpoint_rejects_unbound_guardian(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS, client: Bundle
) -> None:
    """没绑定的家属读不了语音条：403，且与「读日报」同一道闸。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_stranger(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}/audio", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


async def test_audio_endpoint_404_when_report_has_no_audio(
    db: AsyncSession,
    seeded: Seed,
    audio_root: Path,
    tts: RecordingTTS,
    client: Bundle,
) -> None:
    """`audio_url` 为空 → 404（前端据此整块不渲染播放器），而日报本身照常可读。"""
    tts.fail_with = TTSUnavailable("no audio today")
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    client.holder.append(_as_guardian(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}/audio", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
    assert resp.json()["detail"] == "daily report audio not found"
    assert resp.json()["trace_id"], "错误体必须带 trace_id（契约要求）"

    # 日报照常可读，且 `audio_url` 就是 null —— 前端闸门的输入口径在此固定下来。
    detail = await client.client.get(
        f"/api/reports/{seeded.elder_id}", params={"date": seeded.day.isoformat()}
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["audio_url"] is None
    assert detail.json()["payload"]["health"]["total"] == 1, "日报内容本身完整，缺的只是语音"


async def test_audio_endpoint_404_when_file_was_cleaned(
    db: AsyncSession, seeded: Seed, audio_root: Path, tts: RecordingTTS, client: Bundle
) -> None:
    """库里有 `audio_url` 但文件被清理：404 而不是 500 —— 磁盘才是事实来源。"""
    await report_service.generate(db, seeded.elder_id, seeded.day)
    await db.flush()
    _audio_path(audio_root, seeded).unlink()
    client.holder.append(_as_admin(seeded))

    resp = await client.client.get(
        f"/api/reports/{seeded.elder_id}/audio", params={"date": seeded.day.isoformat()}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "daily report audio not found"
