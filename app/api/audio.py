"""按住说话降级链路 REST 路由 —— `POST /api/audio/utterance`（契约 C3，迭代 3 A5 冻结）。

分层（AGENTS §2）：
- 本模块只做：参数校验 → 调 ASR/TTS 适配器 → **调服务层跑一轮真对话** → 序列化。
  写库动作只发生在 `app.services`（`run_text_turn` → `alert_service`），本模块不碰 ORM。
- 2026-11-11 起，ASR 终稿不再拼「您说的是『…』，我记下了」的假回复，而是交给
  `app.services.text_turn_service.run_text_turn` —— 与 `POST /api/dialogue/text`
  **同一个函数**（AGENTS §4-8：一条链路只有一份实现）。急症句因此会真的落
  `risk_events` + 红单 + 通知，而不是只回一句空话。
- `elder_id` 只从设备 JWT 绑定关系取（禁区 9），客户端传参一律忽略。
- 错误统一走 `ErrorResponse`（`{detail, code, trace_id}`），由 app/main.py 全局 handler 兜底。

ASR/TTS 适配器统一走 A1 的 `build_asr()` / `build_tts()` 工厂，**不由本模块自造 Mock**。
配置 `ASR_PROVIDER` / `TTS_PROVIDER` 为空或未知时，A1 工厂自动降级为 Mock 实现
（断网可跑，符合 PRD Mock 边界与 AGENTS §1 "真实 key 不进仓库"的要求）。

身份（声纹）：整段音频在 ASR 之前已经在内存里，顺手做一次**旁路**说话人判定
（`identify_speaker`），结论只塞进响应的 `identity` 字段供前端提示；低置信 / 无样本 /
容器不可用都**不阻断**对话，真身份仍是设备令牌（AGENTS §4-9）。**不凭音频签发登录态。**

`TTSProvider` 的唯一定义在 `app/audio/asr.py`（C1 契约，已冻结）；`app/audio/tts.py` 只是
转手 import 它、`__all__` 未收录，从那儿取会被 mypy 的 `no_implicit_reexport` 判成隐性
再导出，故这里直接从定义处取。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import wave
from collections.abc import AsyncIterator
from typing import Annotated, Final, TypedDict, cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import ErrorResponse
from app.audio.asr import (
    FRAME_BYTES,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    ASRError,
    ASREventKind,
    ASRProvider,
    TTSProvider,
    build_asr,
)
from app.audio.tts import build_tts
from app.core.auth import current_device
from app.core.context import get_trace_id
from app.core.degrade_speech import REST_DEGRADE
from app.core.runtime_context import Identity
from app.db import get_session
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMService, build_llm
from app.services.text_turn_service import (
    RouteText,
    TurnIdentity,
    identify_speaker,
    run_text_turn,
    thread_id_for_device,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"])


# ---------------------------------------------------------------------------
# C3 · 降级 REST 契约（冻结人：A5，消费方：A4 前端降级模式）
# ---------------------------------------------------------------------------


class UtteranceIdentity(TypedDict):
    """本轮说话人的**旁路**结论（只供前端提示，不参与授权）。

    `source="voice"` 才是声纹命中；`device` / `generic` 都表示「没认出，但照常对话」。
    """

    source: str  # voice | device | generic
    matched: bool
    reason: str | None  # low_confidence / vp_unavailable / no_samples / elder_unbound / ...
    confidence: float | None
    elder_id: str | None


class UtteranceReply(TypedDict):
    """端点成功响应里的 reply 对象。"""

    text: str  # 回复话术（走真对话链：L0 话术 / Agent / LLM，失败时降级话术）
    audio_url: str | None  # 服务端合成音频可取地址；无 TTS 时为 null
    tts_degraded: bool  # true = 本轮没有音频，前端应回落到 speechSynthesis / 字幕


class UtteranceResponse(TypedDict):
    """`POST /api/audio/utterance` 的 200 响应体。"""

    text: str  # ASR 终稿文本
    reply: UtteranceReply
    identity: UtteranceIdentity | None  # 声纹旁路结论；未判定（无音频/无设备号）为 null


# ---------------------------------------------------------------------------
# 常量：降级话术 + 音频上限（PRD 硬性数值，改动前先回查 FR-UI-01）
# ---------------------------------------------------------------------------

# 降级话术**唯一源** = app/core/degrade_speech.py（迭代 5 §4.3 收口）。
# 这里只是别名，**不再保留本地 dict 副本**：同一句长者提示若在本文件里改一次、
# 在信号层里再改一次，早晚会出现「页面说 A、数字人说 B」。REST 链要的是书面语，
# 所以取 REST_DEGRADE；WS 口语那套在 app/audio/signaling.py，两者同键不同措辞。
DEGRADE_TEMPLATES: Final[dict[str, str]] = REST_DEGRADE

# FR-UI-01 原文口径是「单次最长 120 秒」。这里曾是 30 秒：松开口 30 秒就把整段掐掉，
# 长者一段话说不到一半就会被拒收，等于把「按住说话」这个唯一降级通路废掉。
# 上限只在这一处定义，前端与测试都从这条语义出发，不各造一个数字。
MAX_AUDIO_SECONDS = 120  # PRD FR-UI-01：降级模式单次最长 120 秒

# 入站体积上限（P1 审计同源问题：原先 `await file.read()` 无上限，整包进内存）。
# 120s × 16kHz × 16bit × 单声道 = 3_840_000B，再加 WAV 容器头的 44B：
# 只按纯 PCM 算上限的话，**刚好录满 120 秒**的文件会多出 44B 被实读闸当成超限拒掉——
# 上限一旦贴近 FR 的边界，这 44B 就从「无所谓」变成「唯一能跑满的时长被悄悄拦下」。
# 体积闸由时长闸派生，两者不许各写一个数——改上限只动 MAX_AUDIO_SECONDS。
WAV_CONTAINER_OVERHEAD_BYTES = 44  # RIFF + fmt(24) + data 头，不含采样
MAX_AUDIO_BYTES = MAX_AUDIO_SECONDS * 16_000 * 2 + WAV_CONTAINER_OVERHEAD_BYTES
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_REQUEST_BYTES = MAX_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"


# ---------------------------------------------------------------------------
# FastAPI 依赖函数 —— 默认走 A1 build_asr/build_tts，可被 dependency_overrides 替换
# ---------------------------------------------------------------------------


async def _get_default_asr() -> ASRProvider:
    """默认 ASR 依赖：A1 build_asr()。配置 ASR_PROVIDER 为空时自动走 MockASRProvider。"""
    return build_asr()


async def _get_default_tts() -> TTSProvider:
    """默认 TTS 依赖：A1 build_tts()。配置 TTS_PROVIDER 为空时自动走 MockTTSProvider。"""
    return build_tts()


# ---------------------------------------------------------------------------
# 对话链依赖（与 `app/api/dialogue.py` 同形，但不 cross-import 那一层）
# ---------------------------------------------------------------------------
#
# 为什么重复这两个 getter：AGENTS §2 禁止模块横向 import，`api.audio → api.dialogue`
# 正是横向依赖（打字入口改一次签名，按住说话就跟着崩）。这几个函数体只有三五行，
# 复制成本远低于跨路由耦合；**真正的编排逻辑只有一份**——都在 text_turn_service。

_llm: LLMService | None = None


def get_llm() -> LLMService:
    """进程级惰性 LLM 客户端（与 `app/main.py` 的装配同一个工厂，不另造适配器）。"""
    global _llm
    if _llm is None:
        from app.core.config import get_settings

        _llm = build_llm(get_settings())
    return _llm


def get_dialogue_service(request: Request) -> DialogueService:
    """请求级依赖：优先复用 lifespan 装配好的 `DialogueService`（含 Redis 历史与 Agent）。"""
    shared = getattr(request.app.state, "dialogue_service", None)
    if isinstance(shared, DialogueService):
        return shared
    return DialogueService(llm=get_llm())


def get_route_text() -> RouteText:
    """路由函数依赖：与话音/打字链路同一套 `wire_route`（L0 → L1），不在这里二次裁决。"""
    from app.router.wiring import wire_route

    return wire_route


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _wav_duration_ms(audio: bytes) -> int | None:
    """从 WAV 头计算时长（ms）。**解析失败返回 `None`，绝不返回 0。**

    为什么不能返回 0：调用方 `utterance()` 用本值判 30 s 上限，0 会被当成"合法短音频"
    **直接放行**，于是坏头字节一路打到 ASR（a0-F02：C3 收到非 WAV 垃圾照样 200）。
    `None` 是「无法判定」而不是「时长为零」，语义必须由调用方映射成 422。

    失败形态必须全兜、只回 `None`：`wave.open` 解析截断/伪造的 RIFF 分块时抛的并不都是
    `wave.Error` —— 头部字段读不满抛 `EOFError`（`_Chunk` 读到文件尾），fmt 分块声明
    长度与实到字节不符时 `struct.unpack` 抛 `struct.error`，`_Chunk.seek` 越界抛
    `RuntimeError`（三例均已实测）。函数体只有 `wave.open` + 三个 getter，任何异常都
    等价于「容器不可解析」，所以这里全兜并把类型写进日志；漏一类就是 500（a0-F02 的
    另一半：坏头 500 和坏头 200 一样不可接受）。
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate <= 0:
                return None
            return int(frames * 1000 / rate)
    except Exception as exc:  # 见 docstring：坏容器的异常类型不可穷举，全兜成 None
        logger.warning(
            "wav_duration: unparseable wav container (len=%d, error_type=%s)",
            len(audio),
            type(exc).__name__,
        )
        return None


def _invalid_audio_response(detail: str) -> JSONResponse:
    """契约 §7 形状的 422 `VALIDATION_ERROR`。

    本模块是普通 `APIRouter`（不是 `ContractAPIRoute`），`HTTPException` 只会产出
    `{"detail": ...}`，缺 `code`/`trace_id`。所以坏音频这一条直接返回 JSONResponse，
    与 `app/main.py` 的全局 422 handler、`app/api/elders.py::_error_response` 保持同形状。
    不 import `_error_response`：那会把 `app.services`/ORM 拖进 audio 路由，违反本模块
    docstring 与 AGENTS §2 的分层约束。
    """
    return JSONResponse(
        status_code=422,
        content={"detail": detail, "code": "VALIDATION_ERROR", "trace_id": get_trace_id() or "-"},
    )


async def _read_upload_file(file: UploadFile, *, limit: int = MAX_AUDIO_BYTES) -> bytes | None:
    """读 multipart UploadFile 到 bytes；超过 `limit` 返回 None（调用方映射 413）。

    只读 `limit + 1` 字节：多读的 1 字节用来区分「正好等于上限」与「超出上限」。
    绝不 `await file.read()` 整包读入 —— P1 审计同源问题：500MB 也能一次进内存。
    """
    data = await file.read(limit + 1)
    return None if len(data) > limit else data


def _payload_too_large_response(detail: str) -> JSONResponse:
    """413 PAYLOAD_TOO_LARGE：入站体积超上限，且在整包读入内存之前/之时返回。"""
    return JSONResponse(
        status_code=413,
        content={"detail": detail, "code": PAYLOAD_TOO_LARGE, "trace_id": get_trace_id() or "-"},
    )


def _declared_content_length(request: Request) -> int | None:
    """读 `Content-Length`（只做上限预判）；缺失或非法返回 None。

    缺失是合法情形（chunked 没有该头），所以 None 不等于「安全」——真正的兜底是
    `_read_upload_file` 的实读长度判定，二者缺一不可。
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


async def _frame_iter(wav_bytes: bytes) -> AsyncIterator[bytes]:
    """WAV 解码 → 校验 16kHz/单声道/16bit → 切 60ms/1920B 帧 → 逐帧 yield。

    A1 的 ASRProvider.stream() 契约（C1 冻结）要求入参是 AsyncIterator[bytes]
    （PCM 帧流），本函数就是把客户端上传的 WAV 字节适配成那个形状。
    非标准 WAV 头会抛 ASRError，由上层 utterance() 捕获走 503 降级。
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()

            if rate != PCM_SAMPLE_RATE or channels != PCM_CHANNELS or sampwidth != PCM_SAMPLE_WIDTH:
                raise ASRError(
                    f"wav format mismatch: got {rate}Hz/{channels}ch/{sampwidth * 8}bit, "
                    f"need {PCM_SAMPLE_RATE}Hz/{PCM_CHANNELS}ch/{PCM_SAMPLE_WIDTH * 8}bit"
                )

            pcm = w.readframes(w.getnframes())
    except wave.Error as exc:
        raise ASRError(f"invalid wav header: {exc}") from exc

    for i in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[i : i + FRAME_BYTES]
        # 尾帧不足 FRAME_BYTES 补零到完整帧——A1 MockASRProvider 的 drain 会吞掉所有帧，
        # FunASR 连接也能正确收尾；不补零会让尾帧被丢弃导致识别截断。
        if len(chunk) < FRAME_BYTES:
            chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
        yield chunk


async def _asr_recognize(asr: ASRProvider, audio_bytes: bytes) -> str:
    """统一 ASR 调用：遍历 stream 直到 FINAL，遇到 ERROR 抛 ASRError。

    MockASRProvider 和 FunASRWSProvider 都走同一 ASRProvider.stream() 协议。
    """
    final_text: str | None = None
    try:
        events = await asr.stream(_frame_iter(audio_bytes))
        async for event in events:
            if event.kind is ASREventKind.FINAL and event.result is not None:
                final_text = event.result.text
            elif event.kind is ASREventKind.ERROR and event.error is not None:
                raise event.error
    except ASRError:
        raise  # 让上层 utterance() 捕获并映射到 503
    except Exception as exc:
        # A1 P1-1 修补：兜底——任何非 ASRError 的异常都包装成 ASRError 再抛
        # （避免 ast.parse SyntaxError 等裸异常冒泡成裸 500）
        logger.exception("utterance: unexpected error in asr.stream()")
        raise ASRError(f"asr internal error: {exc}") from exc

    if final_text is None:
        # MockASRProvider 默认脚本一定吐 FINAL；真 FunASR 理论上也有。
        # 没有 FINAL 文本就当作"ASR 不可用"处理。
        raise ASRError("asr returned no final result")
    if not final_text.strip():
        # 终稿是空白（`ASR_EMPTY_SCRIPT` / 真引擎在静音段吐空串）：与「没有终稿」同义。
        # 不让空文本进对话链——`DialogueService` 拿空串去问模型只会得到一句风马牛不相及
        # 的回复，长者听到的会比「没听清，再说一次」更糟。
        raise ASRError("asr returned blank final result")
    return final_text


async def _tts_synthesize(tts: TTSProvider, text: str) -> str | None:
    """统一 TTS 调用：synthesize() → PCM16 bytes → 包装 WAV → 存 static → 返回 URL。

    A1 MockTTSProvider / EdgeTTSProvider / CloudTTSProvider 都返回 PCM16 原始字节。
    """
    pcm_bytes = await tts.synthesize(text)

    # 包装成 WAV 容器（16k / mono / s16，和 PCM 口径一致）
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(16_000)
        w.writeframes(pcm_bytes)

    # 存到 static/mock/ 目录（相对项目根）——to_thread 包住同步文件 I/O，别堵事件循环
    static_dir = os.path.join(os.getcwd(), "static", "mock")
    await asyncio.to_thread(os.makedirs, static_dir, exist_ok=True)
    filename = f"tts_{int(time.time() * 1000)}_{os.getpid()}.wav"
    filepath = os.path.join(static_dir, filename)
    wav_bytes = wav_buf.getvalue()

    def _write_file() -> None:
        with open(filepath, "wb") as f:
            f.write(wav_bytes)

    await asyncio.to_thread(_write_file)

    # 返回可访问 URL（假设 nginx 把 static/ 挂到 /static/）
    return f"/static/mock/{filename}"


# ---------------------------------------------------------------------------
# POST /api/audio/utterance
# ---------------------------------------------------------------------------


@router.post(
    "/utterance",
    response_model=UtteranceResponse,
    summary="按住说话降级链路：整段上传 WAV → ASR → 真对话链 → TTS 回话",
    responses={
        401: {"model": ErrorResponse, "description": "device token 失效/伪造/过期"},
        413: {"model": ErrorResponse, "description": "音频超 120s 拒收"},
        422: {"model": ErrorResponse, "description": "表单字段缺失/类型错误"},
        503: {"model": ErrorResponse, "description": "ASR 不可用（降级话术）"},
    },
)
async def utterance(
    request: Request,
    identity: Annotated[Identity, Depends(current_device)],
    asr: Annotated[ASRProvider, Depends(_get_default_asr)],
    tts: Annotated[TTSProvider, Depends(_get_default_tts)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[DialogueService, Depends(get_dialogue_service)],
    route_text: Annotated[RouteText, Depends(get_route_text)],
    audio: Annotated[UploadFile, File(...)],
    duration_ms: int | None = Form(default=None),
) -> UtteranceResponse | JSONResponse:
    """按住说话降级端点 —— **WebRTC 主链路不可用时的唯一回退**。

    流程（C3 契约形状不变，④ 的实现换了）：
        ① 鉴权 → Depends(current_device) 已完成；elder_id 从库绑定取（禁区 9）
        ② 读 WAV 文件头，校验 ≤ 120s
        ③ A1 ASR（build_asr）→ 识别终稿；不可用/终稿为空 → 降级话术（503）
        ④ **旁路声纹**（`identify_speaker`，不阻断）→ **真对话链**
           `run_text_turn`（与打字入口同一个函数）→ `reply.text`
           急症/敏感句在这里落 `risk_events` + 红单 + 通知（`alert_service` 单一实现）
        ⑤ A1 TTS（build_tts）→ 合成 PCM → 存 static → audio_url；
           不可用/任何异常 → `audio_url=None` + `tts_degraded=True` + 降级话术前缀（仍 200）

    `identity` 字段只用于前端提示（「这次没认出是您」），**不参与鉴权、不签发登录态**。
    """
    trace_id = get_trace_id() or "-"
    device_id = identity.device_id or "-"
    elder_id = identity.elder_id or "unbound"

    logger.info(
        "utterance start (device=%s, elder=%s, trace_id=%s)",
        device_id,
        elder_id,
        trace_id,
    )

    # --- ② 参数校验 ---
    #    体积闸（P1 审计同源问题）：先看 Content-Length 少读，再按实读长度兜底
    #    （chunked 没有该头，只信 header 会直接漏过 500MB）。
    declared = _declared_content_length(request)
    if declared is not None and declared > MAX_REQUEST_BYTES:
        logger.warning(
            "utterance rejected: content-length too large (declared=%d, limit=%d, trace_id=%s)",
            declared,
            MAX_REQUEST_BYTES,
            trace_id,
        )
        return _payload_too_large_response("audio upload too large")

    audio_bytes = await _read_upload_file(audio)
    if audio_bytes is None:
        logger.warning(
            "utterance rejected: body too large (limit=%d, trace_id=%s)",
            MAX_AUDIO_BYTES,
            trace_id,
        )
        return _payload_too_large_response("audio upload too large")
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="audio file is empty")

    # 用 WAV 头算真实时长；不信任客户端传的 duration_ms
    real_duration_ms = _wav_duration_ms(audio_bytes)
    if real_duration_ms is None:
        # 坏头/截断的容器：无法判定时长 ⇒ 无法证明 ≤120s ⇒ 一律拒收。
        # 这里必须**在调 ASR 之前**返回，坏字节不许进 `_frame_iter` 与 provider.stream()。
        logger.warning(
            "utterance rejected: invalid wav header (len=%d, trace_id=%s)",
            len(audio_bytes),
            trace_id,
        )
        return _invalid_audio_response("audio file is not a valid WAV container")
    if real_duration_ms > MAX_AUDIO_SECONDS * 1000:
        logger.warning(
            "utterance rejected: audio too long (real=%dms, client=%sms, trace_id=%s)",
            real_duration_ms,
            duration_ms,
            trace_id,
        )
        raise HTTPException(
            status_code=413,
            detail=f"audio exceeds {MAX_AUDIO_SECONDS}s limit",
        )

    logger.debug(
        "utterance audio accepted: len=%d, real_duration_ms=%d, client_duration_ms=%s",
        len(audio_bytes),
        real_duration_ms,
        duration_ms,
    )

    # --- ③ ASR（A1 build_asr → stream FINAL；ERROR → 503 降级）---
    try:
        asr_text = await _asr_recognize(asr, audio_bytes)
    except ASRError as exc:
        logger.warning(
            "utterance: asr unavailable → 503 degrade (trace_id=%s, err=%s)",
            trace_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail=DEGRADE_TEMPLATES["asr_unavailable"],
        ) from None

    # --- ④ 旁路声纹 + 真对话链（ASR 终稿与打字入口是同一份输入）---
    #    身份判定只给前端提示：低置信/无样本/容器挂了都不阻断，仍按设备绑定长者继续。
    #    必须在 `run_text_turn` 之前调——它内部可能提交（收口 identity_service 开的
    #    临时线程），放到写库之后提交会把半截告警一起落盘。
    identity_hint: TurnIdentity | None = await identify_speaker(
        session,
        device_id=identity.device_id,
        audio_bytes=audio_bytes,
        trace_id=trace_id,
        log_name="utterance",
    )
    turn = await run_text_turn(
        session,
        text=asr_text,
        elder_text=identity.elder_id,
        thread_id=thread_id_for_device(device_id),
        trace_id=trace_id,
        service=service,
        route_text=route_text,
        log_name="utterance",
    )
    reply_text = turn.reply.text

    logger.info(
        "utterance turn",
        extra={
            "trace_id": trace_id,
            "device_id": device_id,
            "elder_id": elder_id,
            "layer": turn.layer or "-",
            "safety_alert": turn.safety_alert,
            "alert_recorded": turn.alert_recorded,
            "degraded": turn.reply.degraded,
            "identity_source": identity_hint.source if identity_hint is not None else "-",
        },
    )

    # --- ⑤ TTS（A1 build_tts → synthesize PCM → 存 static；拿不到就 audio_url=null）---
    #    长者已经说完这句话了，TTS 出任何岔子都不能把整轮变成 5xx：统一下沉成
    #    「audio_url=null + tts_degraded=true + 话术前缀」，前端用浏览器 speechSynthesis
    #    或大字幕兜底（`TTSUnavailable` 只是最常见的一种）。
    tts_degraded = False
    try:
        audio_url: str | None = await _tts_synthesize(tts, reply_text)
    except Exception as exc:
        logger.warning(
            "utterance: tts unavailable → audio_url=null (trace_id=%s, error_type=%s)",
            trace_id,
            type(exc).__name__,
        )
        audio_url = None
        tts_degraded = True
        reply_text = DEGRADE_TEMPLATES["tts_unavailable"] + reply_text

    # --- ⑥ 组装响应（TypedDict 直接返回，FastAPI 会自动序列化）---
    return UtteranceResponse(
        text=asr_text,
        reply=UtteranceReply(text=reply_text, audio_url=audio_url, tts_degraded=tts_degraded),
        identity=cast(UtteranceIdentity, identity_hint.as_dict())
        if identity_hint is not None
        else None,
    )
