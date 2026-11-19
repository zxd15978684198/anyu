"""迭代 4 · 声纹领域服务 — voiceprint_service.py

职责（仅业务逻辑 + 唯一写库层）：
    1. 调独立推理容器（deploy/voiceprint/ · 端口 7003）的 POST /extract 端点拿 embedding
    2. Fernet 加密 embedding → voiceprints.embedding（bytea）落库
    3. 进程内 numpy 做 cosine 比对（解密 → normalize → dot）
    4. 状态查询 / 删除

禁区（AGENTS §2）：
    - 不直接 import 推理容器的 sherpa-onnx（torch 重依赖隔离在容器里）
    - 原始音频 bytes 取完即弃，不写临时文件/日志（AC-36）
    - 不 import conversation_threads（那是 identity_service 的事）
    - 不 import FastAPI（路由层在 app/api/voiceprints.py）
"""

from __future__ import annotations

import io
import logging
import uuid
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import numpy as np
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import CursorResult, Result, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.audit import AuditLog
from app.models.voiceprint import Voiceprint

logger = logging.getLogger("voiceprint_service")

SampleIndex = Literal[1, 2, 3]


def _affected(result: Result[Any]) -> int:
    """`DELETE` 的受影响行数。

    `Session.execute` 的静态返回类型统一是 `Result`，`rowcount` 只长在运行期真正返回的
    `CursorResult` 上（SQLAlchemy 2.x 没有按语句类型收窄的重载），故在此收窄一次。
    DML 必返回 `CursorResult`；`TypeError` 分支只在有人把 `SELECT` 结果喂进来时才走。
    """
    if not isinstance(result, CursorResult):
        raise TypeError(f"expected CursorResult from DML, got {type(result).__name__}")
    return result.rowcount or 0


# ============================================================
# 自定义异常（REST 层将映射为 HTTP 状态码）
# ============================================================


class VoiceprintError(Exception):
    """声纹服务通用异常基类。REST 层映射为 4xx/5xx。"""


class VoiceprintExtractUnavailable(VoiceprintError):
    """推理容器不可用 / 超时 / 模型未加载 → 503。"""


class VoiceprintDuplicateSample(VoiceprintError):
    """(elder_id, version, sample_index) 唯一约束冲突 → 409。"""


class VoiceprintInvalidAudio(VoiceprintError):
    """WAV 非法 / 维度异常 → 422。"""


class VoiceprintUnavailable(VoiceprintError):
    """服务端未配置可用的加密密钥等自身问题 → 503。

    与 `VoiceprintExtractUnavailable`（推理容器不可用）区分：这条是**本进程配置错**，
    必须显式暴露，不能像身份链路那样被 `except Exception` 吞成 generic。
    """


# ============================================================
# Fernet 加解密
# ============================================================


def _get_fernet() -> Fernet:
    settings = get_settings()
    if settings.is_production and settings.voiceprint_fernet_is_placeholder:
        # 生产拿仓库里的占位密钥 = 所有部署共用一把公开钥匙，等于没加密；宁可整条声纹链路不可用。
        raise VoiceprintUnavailable(
            "生产环境禁止使用仓库内的声纹 Fernet 占位密钥，请在 .env 注入 VOICEPRINT_FERNET_KEY"
        )
    try:
        return Fernet(settings.voiceprint_fernet_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        # 配置错必须显式上抛：默认值曾是不可用的 39 字节密钥，导致语音身份静默降级 generic。
        raise VoiceprintUnavailable(
            "voiceprint_fernet_key 未配置或非法（需 32 字节 url-safe base64）"
        ) from exc


def encrypt_embedding(embedding: np.ndarray) -> bytes:
    """归一化 embedding 数组 → bytes → Fernet 加密。"""
    emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if not np.isfinite(emb).all():
        raise VoiceprintInvalidAudio("embedding 包含 NaN/Inf")
    return _get_fernet().encrypt(emb.tobytes())


def decrypt_embedding(ciphertext: bytes) -> np.ndarray:
    """Fernet 解密 bytes → float32 数组。维度由 Settings 配置校验。"""
    settings = get_settings()
    try:
        raw = _get_fernet().decrypt(ciphertext)
    except InvalidToken as exc:
        raise VoiceprintError(f"Fernet 解密失败：密钥不匹配或密文损坏（{exc}）") from exc
    arr = np.frombuffer(raw, dtype=np.float32)
    expected = settings.voiceprint_embedding_dimension
    if arr.shape[0] != expected:
        raise VoiceprintError(f"解密后 embedding 维度 {arr.shape[0]} ≠ 预期 {expected}")
    return arr


# ============================================================
# 推理容器 HTTP 客户端
# ============================================================

# 会话上行的裸 PCM 口径：与 `app/audio/webrtc.py::last_utterance_pcm()` 一致。
_WAV_SAMPLE_RATE = 16_000
_WAV_CHANNELS = 1
_WAV_SAMPLE_WIDTH = 2


def _ensure_wav(audio_bytes: bytes) -> bytes:
    """把裸 PCM 包成 RIFF/WAV；已是 WAV 的原样返回。

    推理容器用 `wave.open()` 读音频，输入必须是 RIFF 容器。历史实现把
    `MediaSession.last_utterance_pcm()` 的裸 s16le 直接命名为 `sample.wav` 上送，
    容器报 `file does not start with RIFF id` → 恒 503，身份链路只能落
    `vp_unavailable`。注册接口上送的本来就是 WAV，不能再包一层，否则同样解析失败。
    """
    if not audio_bytes:
        raise VoiceprintInvalidAudio("音频为空")

    if len(audio_bytes) >= 12 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        return audio_bytes

    if len(audio_bytes) % _WAV_SAMPLE_WIDTH != 0:
        # s16le 每个采样点 2 字节；奇数长度说明上游分帧坏了，不靠补零掩盖。
        raise VoiceprintInvalidAudio("PCM 字节数为奇数，无法按 s16le 解析")

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(_WAV_CHANNELS)
        wav.setsampwidth(_WAV_SAMPLE_WIDTH)
        wav.setframerate(_WAV_SAMPLE_RATE)
        wav.writeframes(audio_bytes)
    return buffer.getvalue()


async def extract_embedding_from_container(audio_bytes: bytes) -> np.ndarray:
    """调推理容器 POST /extract，返回归一化 float32 数组。

    入参可以是 WAV，也可以是会话上行的 16k/mono/s16le 裸 PCM；后者在本函数内
    包上 RIFF 头再上送（容器只认 WAV 容器，见 `_ensure_wav`）。

    失败映射：
        超时 / 网络错误 / 容器 503 → VoiceprintExtractUnavailable (503)
        4xx 客户端错误 → VoiceprintInvalidAudio (422)
        响应 JSON 缺 embedding / 维度不对 → VoiceprintExtractUnavailable (503)
    """
    settings = get_settings()
    url = f"{settings.voiceprint_base_url.rstrip('/')}/extract"
    timeout = httpx.Timeout(settings.voiceprint_extract_timeout_seconds)
    wav_bytes = _ensure_wav(audio_bytes)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                files={"audio": ("sample.wav", wav_bytes, "audio/wav")},
            )
    except httpx.TimeoutException as exc:
        logger.warning("推理容器超时：%s", exc)
        raise VoiceprintExtractUnavailable(f"声纹推理超时：{exc}") from exc
    except httpx.ConnectError as exc:
        logger.warning("推理容器不可达：%s", exc)
        raise VoiceprintExtractUnavailable(f"声纹推理容器不可用：{exc}") from exc

    if resp.status_code == 503:
        detail = (
            resp.json().get("detail", "推理容器暂不可用") if resp.content else "推理容器暂不可用"
        )
        raise VoiceprintExtractUnavailable(detail)
    if resp.status_code >= 400:
        # 不回显容器响应体：容器可能把上送音频的识别文本写进错误里，
        # 而这条消息会经 API 返回并可能进日志（AGENTS §4-3 / §4-10）。
        raise VoiceprintInvalidAudio(
            f"推理容器返回 {resp.status_code}（响应 {len(resp.content)} 字节），"
            "音频或格式不符合要求"
        )

    try:
        data = resp.json()
        emb_list = data["embedding"]
    except (KeyError, ValueError) as exc:
        # 只记形状不记内容：响应体可能带上送音频的文本（AGENTS §4-3）。
        logger.warning(
            "推理容器响应格式异常：status=%s content_type=%s bytes=%s",
            resp.status_code,
            resp.headers.get("content-type", "?"),
            len(resp.content),
        )
        raise VoiceprintExtractUnavailable("推理容器响应格式异常") from exc

    expected = settings.voiceprint_embedding_dimension
    if len(emb_list) != expected:
        raise VoiceprintExtractUnavailable(f"推理容器返回维度 {len(emb_list)} ≠ 预期 {expected}")

    arr = np.asarray(emb_list, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm < 1e-8 or not np.isfinite(arr).all():
        raise VoiceprintExtractUnavailable("推理容器返回了无效 embedding（零范数或含 NaN）")
    return arr / norm


# ============================================================
# 注册 · 状态 · 删除
# ============================================================


@dataclass
class VoiceprintStatus:
    """声纹注册状态（REST GET /status 返回）。"""

    registered: list[int]
    version: int
    status: Literal["none", "partial", "active"]

    @classmethod
    def from_rows(cls, rows: Sequence[Voiceprint]) -> VoiceprintStatus:
        if not rows:
            return cls(registered=[], version=1, status="none")
        version = max(r.version for r in rows)
        current = [r for r in rows if r.version == version and r.status == "active"]
        registered = sorted(r.sample_index for r in current)
        if len(registered) == 3:
            return cls(registered=registered, version=version, status="active")
        elif registered:
            return cls(registered=registered, version=version, status="partial")
        return cls(registered=[], version=version, status="partial")


async def register_sample(
    session: AsyncSession,
    elder_id: uuid.UUID,
    sample_index: SampleIndex,
    audio_bytes: bytes,
) -> VoiceprintStatus:
    """注册一段声纹样本（三段齐自动置 status='active'）。

    写库事务：INSERT voiceprints，唯一约束冲突抛 VoiceprintDuplicateSample。
    原始 audio_bytes 本函数内用完即弃，不存不写日志。
    """
    # 1. 推理容器 → embedding（异常往上抛，REST 层映射）
    emb = await extract_embedding_from_container(audio_bytes)
    # audio_bytes 用完即弃（AC-36）——函数内无任何持久化操作

    # 2. 查询当前版本的已注册样本
    existing = list(
        (
            await session.execute(
                select(Voiceprint).where(
                    Voiceprint.elder_id == elder_id,
                    Voiceprint.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    version = max((r.version for r in existing), default=1)

    # 3. INSERT（唯一约束冲突 → 409）
    # 先检查是否已有同 (elder_id, version, sample_index)
    dup = (
        await session.execute(
            select(Voiceprint).where(
                Voiceprint.elder_id == elder_id,
                Voiceprint.version == version,
                Voiceprint.sample_index == sample_index,
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise VoiceprintDuplicateSample(
            f"elder={elder_id}, version={version}, sample_index={sample_index} 已存在"
        )

    encrypted = encrypt_embedding(emb)
    row = Voiceprint(
        elder_id=elder_id,
        version=version,
        sample_index=sample_index,
        embedding=encrypted,
        status="active",
    )
    session.add(row)

    # 4. 检查三段是否齐了
    existing.append(row)
    current_samples = {
        r.sample_index for r in existing if r.status == "active" and r.version == version
    }
    if current_samples == {1, 2, 3}:
        # 三段齐了 → 不做额外操作（Voiceprint 默认 status=active）
        logger.info("elder=%s 声纹注册完成（version=%d）", elder_id, version)

    await session.commit()
    return VoiceprintStatus.from_rows(existing)


async def get_status(
    session: AsyncSession,
    elder_id: uuid.UUID,
) -> VoiceprintStatus:
    rows = (
        (
            await session.execute(
                select(Voiceprint).where(
                    Voiceprint.elder_id == elder_id,
                    Voiceprint.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    return VoiceprintStatus.from_rows(rows)


async def delete_all_for_elder(
    session: AsyncSession,
    elder_id: uuid.UUID,
    *,
    actor_type: str = "system",
    actor_id: uuid.UUID | None = None,
    detail: dict[str, object] | None = None,
) -> int:
    """删除该长者全部 voiceprints 并写一条 `audit_logs`，返回删除行数。

    审计与删除同一事务提交：要么「删了且有痕迹」，要么都不发生——C4 契约要求
    `DELETE /api/voiceprints` 留痕（AC-14），先删后记会在记流水这步失败时留下无声删除。

    默认 `actor_type="system"`：只有后台/测试会省略 actor；设备端点会显式传
    `actor_type="device"` + `actor_id=device_id`（长者本人在 pad 上操作）。
    """
    result = await session.execute(delete(Voiceprint).where(Voiceprint.elder_id == elder_id))
    count = _affected(result)

    session.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id if actor_id is not None else elder_id,
            action="delete_voiceprint",
            resource_type="voiceprints",
            resource_id=elder_id,
            detail={
                **(detail or {}),
                "deleted_samples": count,
            },
        )
    )
    await session.commit()
    logger.info("删除 elder=%s 的 %d 条 voiceprints（actor=%s）", elder_id, count, actor_type)
    return count


# ============================================================
# 进程内 cosine 比对（供 identity_service 调用）
# ============================================================


@dataclass
class CosineMatch:
    matched: bool
    score: float
    threshold: float
    elder_id: uuid.UUID | None  # matched=True 时填谁的样本命中
    # False = 该长者一条样本都没有，score=0 不代表「比对失败」。
    # 复检必须据此跳过，否则未注册声纹的长者会在确认身份后每句话都被再次追问。
    enrolled: bool = True


async def cosine_compare_against_elder(
    session: AsyncSession,
    elder_id: uuid.UUID,
    audio_bytes: bytes,
) -> CosineMatch:
    """比对新音频 vs 某长者全部已注册声纹样本。返回最高分。

    候选集 1 位（裁决：设备绑定长者 1v1，恒 1 位），精确扫描 ≤3 行。
    """
    settings = get_settings()
    threshold = settings.voiceprint_similarity_threshold

    # 先取样本：一条都没注册时不必调推理容器。否则每次首句都要吃一次容器往返，
    # 容器没起时还会把「尚未注册」误报成 vp_unavailable（用户只该看到低置信确认）。
    rows = (
        (
            await session.execute(
                select(Voiceprint).where(
                    Voiceprint.elder_id == elder_id,
                    Voiceprint.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return CosineMatch(
            matched=False,
            score=0.0,
            threshold=threshold,
            elder_id=None,
            enrolled=False,
        )

    # 推理容器 → 新 embedding
    query_emb = await extract_embedding_from_container(audio_bytes)

    # 解密 + 比对
    best = 0.0
    for row in rows:
        ref = decrypt_embedding(row.embedding)
        ref_norm = np.linalg.norm(ref)
        if ref_norm < 1e-8:
            continue
        score = float(np.dot(query_emb, ref / ref_norm))
        best = max(best, score)

    matched = best >= threshold
    return CosineMatch(
        matched=matched,
        score=best,
        threshold=threshold,
        elder_id=elder_id if matched else None,
    )
