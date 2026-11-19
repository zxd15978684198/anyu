"""迭代 4 T7 · voiceprint REST 三接口 —— C4 契约原样实现。

契约来源：契约记录 C4（冻结人 A1）。
**分层硬约束（AGENTS §2）**：本模块不 import ORM、不写业务逻辑，
只做：参数校验 → 调 voiceprint_service → 异常映射 → 序列化。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_device
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services.voiceprint_service import (
    VoiceprintDuplicateSample,
    VoiceprintExtractUnavailable,
    VoiceprintInvalidAudio,
    VoiceprintStatus,
    VoiceprintUnavailable,
    delete_all_for_elder,
    get_status,
    register_sample,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voiceprints"])

# ---------------------------------------------------------------------------
# 入站体积上限（P1 审计：原先 `await audio.read()` 整包读入，500MB 也进内存）
# ---------------------------------------------------------------------------

# 业务上限：C4 约束 ≤10s；10s × 16kHz × 16bit × 单声道 = 320_000B，给 10% 余量。
MAX_SAMPLE_AUDIO_BYTES = 350_000
# multipart 信封开销（边界、Content-Disposition 头、sample_index 字段）的合理余量。
# Content-Length 是**整个 multipart 体**的长度，直接用 MAX_SAMPLE_AUDIO_BYTES 判会把
# 合法的满额上传误杀，所以 header 判据留出这段余量；文件本体另由 read 上限兜。
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_REQUEST_BYTES = MAX_SAMPLE_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES

# 新增错误码（只在本文件使用，未动 app/core 的 ErrorResponse；已回报 lead）。
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# 路由内异常映射（voiceprint_service 层业务异常 → 统一 ErrorResponse）
# ---------------------------------------------------------------------------


def _trace_id() -> str:
    return get_trace_id() or "-"


def _payload_too_large_response(detail: str) -> JSONResponse:
    """413 PAYLOAD_TOO_LARGE：入站体积超上限，且**在整包读入内存之前**返回。

    P1 审计：原实现 `await audio.read()` 无上限，500MB 也一次进内存。C4 的业务上限
    是 ≤10s（≈320KB），但「业务上限」和「内存安全上限」是两道闸：前者 422 由
    `_validate_wav_bytes` 判，后者 413 由本函数判，先于任何读取动作生效。
    """
    return JSONResponse(
        status_code=413,
        content={
            "detail": detail,
            "code": PAYLOAD_TOO_LARGE,
            "trace_id": _trace_id(),
        },
    )


def _declared_content_length(request: Request) -> int | None:
    """读 `Content-Length`（只做上限预判）；缺失或非法一律返回 None。

    缺失是合法情形（`Transfer-Encoding: chunked` 就没有该头），所以 None 不能当成
    「安全」——真正的兜底是 `audio.read(上限 + 1)` 的实读长度判定，二者缺一不可。
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _unbound_device_response() -> JSONResponse:
    """C4：status 端点在设备未绑长者时返回 409 device_unbound。

    设计成独立函数是因为 **samples 端点不适用**（注册路径需要先绑长者，但 C4
    明确 device_unbound 只属 status 的分支，samples 端绑长者的前置由 REST 层
    统一拦：unbound 时 status 不可查 → 前端无法显示「去注册」入口，也就到不了
    samples 端点；真调上来的话 service 层 voiceprints 表 FK 约束会拒 ——
    按 C4 契约原样返回 409 更明确）。
    """
    return JSONResponse(
        status_code=409,
        content={
            "detail": "设备尚未绑定长者，请先完成配对",
            "code": "device_unbound",
            "trace_id": _trace_id(),
        },
    )


def _handle_service_exception(exc: Exception, *, endpoint: str) -> JSONResponse:
    """voiceprint_service 层异常的统一映射。

    `endpoint`（register / status / delete）只用于日志区分：此前三个端点都用
    `voiceprint_register_failed` 记，查「查询接口为什么 500」时会误以为是注册挂了。
    """
    if isinstance(exc, VoiceprintDuplicateSample):
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": "duplicate_sample",
                "trace_id": _trace_id(),
            },
        )
    if isinstance(exc, VoiceprintExtractUnavailable):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "声纹识别服务暂时不可用，稍后再试",
                "code": "vp_extract_unavailable",
                "trace_id": _trace_id(),
            },
        )
    if isinstance(exc, VoiceprintUnavailable):
        # 本进程配置错（Fernet 密钥缺失/非法）：不是「推理容器挂了」，是**我们**的
        # 部署问题。C4 没有 500 档，而异常本身的语义就是 503（服务暂不可用，可重试），
        # 故单列一个码，别让它伪装成 internal error 或推理容器不可用。
        logger.exception(
            "voiceprint_%s_failed: config unavailable (%s)",
            endpoint,
            type(exc).__name__,
            extra={
                "trace_id": _trace_id(),
                "reason": "voiceprint_config_unavailable",
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": "声纹服务暂不可用，请稍后再试",
                "code": "vp_unavailable",
                "trace_id": _trace_id(),
            },
        )
    if isinstance(exc, VoiceprintInvalidAudio):
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "code": "VALIDATION_ERROR",
                "trace_id": _trace_id(),
            },
        )
    # 兜底业务异常（VoiceprintError 其他子类）：内部异常文本只进日志，不回显给客户端
    # （P1 审计：原实现把 f"声纹服务异常：{exc}" 原样返回，既不记日志也泄露内部信息）
    logger.exception(
        "voiceprint_%s_failed: service raised %s",
        endpoint,
        type(exc).__name__,
        extra={
            "trace_id": _trace_id(),
            "reason": "voiceprint_service_error",
            "error_type": type(exc).__name__,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "voiceprint service error",
            "code": INTERNAL_ERROR,
            "trace_id": _trace_id(),
        },
    )


# ---------------------------------------------------------------------------
# 参数校验工具
# ---------------------------------------------------------------------------


def _validate_wav_bytes(raw: bytes) -> None:
    """快速校验 WAV 文件头（RIFF/WAVE 标记），并硬卡时长 ≤10s。

    时长上限 16kHz × 16bit × 10s × 1ch = 320KB，给 10% 余量按 350KB 算。
    目的：在推理容器之前就拒掉明显非法的请求，减少容器负载。
    更深层的音频合法性（编码、采样率等）留给推理容器自身判定。
    """
    if len(raw) < 12:
        raise VoiceprintInvalidAudio("音频文件过小（疑似非法 WAV）")
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise VoiceprintInvalidAudio("非 WAV 格式，请上传 16kHz/16bit/单声道 WAV")
    # 350KB ≈ 10s @ 16kHz/16bit/单声道（与入站上限同源，别写成第二个魔数）
    if len(raw) > MAX_SAMPLE_AUDIO_BYTES:
        raise VoiceprintInvalidAudio("音频过长（≤10s）")


def _require_bound_elder(identity: Identity) -> uuid.UUID | None:
    """取 identity.elder_id 并还原成 UUID；None 表示设备未绑长者。

    `Identity` 存的是**规范 UUID 文本**（`runtime_context._canon` 在 `__post_init__`
    里已用 `canonical_uuid` 校验过），因此这里的 `uuid.UUID(...)` 不会抛；还原成
    UUID 是因为下游 `voiceprint_service` 三个函数的 `elder_id` 形参都是 `uuid.UUID`。
    """
    raw = identity.elder_id
    return None if raw is None else uuid.UUID(raw)


# ---------------------------------------------------------------------------
# C4 · POST /api/voiceprints/samples
# ---------------------------------------------------------------------------


@router.post("/voiceprints/samples")
async def register_voiceprint_sample(
    request: Request,
    identity: Annotated[Identity, Depends(current_device)],
    audio: Annotated[UploadFile, File(description="16kHz/16bit/单声道 WAV ≤10s")],
    sample_index: Annotated[int, Form(description="1/2/3 三段注册的样本序号")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if sample_index not in (1, 2, 3):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "sample_index 必须是 1、2、3",
                "code": "VALIDATION_ERROR",
                "trace_id": _trace_id(),
            },
        )

    elder_id = _require_bound_elder(identity)
    if elder_id is None:
        return _unbound_device_response()

    # ① 体积闸（内存安全）：先看 header 少读一个字节，再按实读长度兜底。
    #    header 判据用 MAX_REQUEST_BYTES（含 multipart 信封），实读判据用
    #    MAX_SAMPLE_AUDIO_BYTES（只看文件本体），两者口径不同、都必须过。
    declared = _declared_content_length(request)
    if declared is not None and declared > MAX_REQUEST_BYTES:
        logger.warning(
            "voiceprint upload rejected: content-length too large "
            "(declared=%d, limit=%d, trace_id=%s)",
            declared,
            MAX_REQUEST_BYTES,
            _trace_id(),
        )
        return _payload_too_large_response("音频文件过大（请上传 ≤10s 的 WAV）")

    # ② 只读上限 +1 字节：chunked 没有 Content-Length，只能靠实读长度判定，
    #    多读的 1 字节用来区分「正好等于上限」和「超出上限」。
    raw = await audio.read(MAX_SAMPLE_AUDIO_BYTES + 1)
    if len(raw) > MAX_SAMPLE_AUDIO_BYTES:
        logger.warning(
            "voiceprint upload rejected: body too large (read=%d, limit=%d, trace_id=%s)",
            len(raw),
            MAX_SAMPLE_AUDIO_BYTES,
            _trace_id(),
        )
        return _payload_too_large_response("音频文件过大（请上传 ≤10s 的 WAV）")

    try:
        _validate_wav_bytes(raw)
    except VoiceprintInvalidAudio as exc:
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "code": "VALIDATION_ERROR",
                "trace_id": _trace_id(),
            },
        )

    try:
        status: VoiceprintStatus = await register_sample(
            session=session,
            elder_id=elder_id,
            sample_index=sample_index,  # type: ignore[arg-type]  # int → Literal[1,2,3]
            audio_bytes=raw,
        )
    except Exception as exc:
        return _handle_service_exception(exc, endpoint="register")

    return JSONResponse(
        status_code=200,
        content={
            "elder_id": str(elder_id),
            "version": status.version,
            "sample_index": sample_index,
            "status": status.status,
        },
    )


# ---------------------------------------------------------------------------
# C4 · GET /api/voiceprints/status
# ---------------------------------------------------------------------------


@router.get("/voiceprints/status")
async def get_voiceprint_status(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    elder_id = _require_bound_elder(identity)
    if elder_id is None:
        return _unbound_device_response()

    try:
        status = await get_status(session=session, elder_id=elder_id)
    except Exception as exc:
        return _handle_service_exception(exc, endpoint="status")

    return JSONResponse(
        status_code=200,
        content={
            "elder_id": str(elder_id),
            "status": status.status,
            "registered": status.registered,
            "version": status.version,
        },
    )


# ---------------------------------------------------------------------------
# C4 · DELETE /api/voiceprints
# ---------------------------------------------------------------------------


@router.delete("/voiceprints", status_code=204)
async def delete_voiceprints(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    # 204 成功分支返回的是**无响应体**的 `Response(status_code=204)`，不是 JSONResponse；
    # 契约错误体（409 / 403 / 500）仍由 JSONResponse 出（它是 Response 的子类）。
    # 返回标注按真实上界写 `Response`：写 `JSONResponse` 会与 204 分支自相矛盾。
    elder_id = _require_bound_elder(identity)
    if elder_id is None:
        return _unbound_device_response()

    try:
        # 审计行由服务层与删除同事务写入（C4 要求 DELETE 留痕，AC-14）
        await delete_all_for_elder(
            session=session,
            elder_id=elder_id,
            actor_type="device",
            actor_id=uuid.UUID(identity.device_id),
            detail={"endpoint": "DELETE /api/voiceprints"},
        )
    except Exception as exc:
        return _handle_service_exception(exc, endpoint="delete")

    return Response(status_code=204)
