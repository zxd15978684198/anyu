"""打字输入 → 对话回复（`POST /api/dialogue/text`）。

定位（2026-11-11 · 长者端 footer 的 TextFallback 输入框）：让「打字」与「说话」走
**同一条**对话链，不另造一套能力（AGENTS §4-8）：

    text ──► wire_route（L0 规则层 → L1 Laya）
              ├─ 命中安全规则：alert_service 落 risk_events + 红单 + 通知
              └─► DialogueService.respond
                    ├─ L0 固定话术短路（急症/停/再见：零模型、零历史读写）
                    ├─ L2 → Agent（装配了 agent 时）
                    └─ 其余 → LLM（共享 Redis 短期历史；超时走降级话术）

分层（AGENTS §2）：

- 本模块只做「参数校验 → 调服务 → 序列化」；**不碰 ORM**。整轮编排（路由 → 安全落库 →
  回话）收在 `app.services.text_turn_service.run_text_turn`，按住说话
  （`app/api/audio.py`）调的是**同一个函数**——一条链路只有一份实现。
- 路由能力不在这里重新裁决：调用 `app.router.wiring.wire_route`，与话音链路同源。

身份（AGENTS §4-9）：`elder_id` 只从设备令牌背后的服务端会话取；请求体里的
`elder_id` 被 schema 的 `extra="ignore"` 丢弃，端到端有用例钉住「塞了也不生效」。

线程归属（攻防计划 §4.6-S2 / B3-01）：`thread_id` 由服务端派生，派生后、交给
`run_text_turn` 去读写历史之前，必须过一道 `ensure_thread_access`——判据不在这里
重写，一律走 `identity_service.validate_thread_access`（形态 → 存量兼容 → DB 复核，
拒绝已在那边写 `thread_access_denied` 审计）。这里是「已有 thread_id、即将以它的名义
写库」的唯一收口点；再往下只剩一个字符串，既没有 session 也没有 identity，做不了
归属复核。

失败口径：打了字必须有回音——LLM 超时由 `DialogueService` 兜成降级话术，其余异常
（连接错误、Agent 抛错、路由抛错）在 `run_text_turn` 里兜成 200 + `degraded=true`，
不吐 5xx。

日志：只记 `trace_id` / `device_id` / `elder_id` / `layer` / `safety_alert` /
`degraded` / `elapsed_ms`；**不记输入原文与回复原文**（隐私，AGENTS §4-3）。
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import ErrorResponse
from app.api.schemas.dialogue import (
    DialogueAudioSegment,
    DialogueIdentity,
    DialogueReplyBody,
    DialogueTextRequest,
    DialogueTextResponse,
    DialogueTtsRequest,
    DialogueTtsResponse,
)
from app.audio.asr import TTSProvider
from app.core.auth import current_device
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services.dialogue_service import DialogueService
from app.services.identity_service import validate_thread_access
from app.services.llm_service import LLMService, build_llm
from app.services.text_turn_service import (
    RouteText,
    as_uuid,
    run_text_turn,
    thread_id_for_device,
    thread_id_from_text,
)
from app.services.tts_audio_service import synthesize_to_static

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dialogue", tags=["dialogue"])

_llm: LLMService | None = None


def get_llm() -> LLMService:
    """进程级惰性 LLM 客户端（与 `app/main.py` 的命令行装配同一个工厂）。

    惰性：`/openapi.json`、健康检查这类探测不该在导入期就建 HTTP 客户端；
    进程内复用：与话音链路共享同一份底层连接池配置，不另造适配器。
    """
    global _llm
    if _llm is None:
        from app.core.config import get_settings

        _llm = build_llm(get_settings())
    return _llm


def get_dialogue_service(request: Request) -> DialogueService:
    """请求级依赖：优先复用应用装配好的 `DialogueService`，否则现场构造（无 Redis 历史）。

    为什么先去 `app.state` 找：`app/main.py` 的 lifespan 已建好一份带 Redis 历史与
    Agent 的服务（话音链路在用）。能复用就复用——打字与说话共享短期记忆与 Agent；
    找不到时退回「只有 LLM」的形态：宁可少一层记忆，也不要因为装配缺失让输入框 5xx。
    """
    shared = getattr(request.app.state, "dialogue_service", None)
    if isinstance(shared, DialogueService):
        return shared
    return DialogueService(llm=get_llm())


def get_route_text() -> RouteText:
    """路由函数依赖：与话音链路同一套 `wire_route`（L0 → L1），不在这里二次裁决。"""
    from app.router.wiring import wire_route

    return wire_route


def get_conversation_recorder(request: Request) -> Any:
    """请求级依赖：取 lifespan 装好的会话事件写入器；没装到就是 None（不报错）。

    为什么允许 None：事件流是**旁路证据**，装不上只该少一条回放链路，
    不该让「打字」这条长者端主路径 5xx（与 `get_dialogue_service` 的降级口径一致）。
    """
    return getattr(request.app.state, "conversation_recorder", None)


def get_memory_hook(request: Request) -> Any:
    """请求级依赖：取 lifespan 装好的长期记忆写回钩子；没装到就是 None。

    同上：记忆是旁路，装不上不该拦住回话。缺了它这一轮就退化成「只聊天不记事」。
    """
    return getattr(request.app.state, "memory_hook", None)


def get_tts() -> TTSProvider:
    """TTS provider 依赖：按 `TTS_PROVIDER` 分派（mock / edge+espeak 兜底 / cloud）。

    惰性构造的理由与 `get_llm` 相同：探测路由（`/openapi.json`、健康检查）不该在导入期
    就决定用哪条 TTS 链路；`build_tts()` 只读 Settings，不建立连接，按请求构造即可。
    用例通过 `dependency_overrides[get_tts]` 注入替身，绝不真连 edge-tts / espeak。
    """
    from app.audio.tts import build_tts

    return build_tts()


#: 归属闸拒绝时的**固定**话术：不透露 thread 是否存在、属于谁、还是形态不对。
#: 与 `app/main.py` 的 HTTPException 兜底配套 → 403 的 `code` 固定为 `ROLE_FORBIDDEN`。
_THREAD_ACCESS_DENIED_DETAIL: Final[str] = "thread access denied"


async def ensure_thread_access(
    session: AsyncSession,
    *,
    thread_id: str,
    device_id: str | None,
    elder_id: str | None = None,
    trace_id: str = "-",
) -> None:
    """对话入口的 thread 归属闸：放行静默返回，拒绝抛 403（攻防 §4.6-S2 / B3-01）。

    为什么接在这里（而不是 `dialogue_service` 里）：`thread_id` 在紧邻的上一步由服务端
    派生，下一步就交给 `run_text_turn` 写成「这一轮发生过什么」——这是全链路唯一一个
    「已有 thread_id、即将以它的名义读写会话」的点。`DialogueService` / `RedisTurnStore`
    只拿到一个字符串，没有 `session`、也没有 identity，做不了 `conversation_threads`
    的归属复核（那需要 DB），硬塞进去等于让编排层反向依赖鉴权。

    判据不在这里重写：形态、存量兼容、DB 复核全部交给
    `identity_service.validate_thread_access`（拒绝时它自己写审计，本函数只管 403）。

    查询预算：正常打字链路的 `device:<本设备>` 走「存量兼容」分支，**零 DB 查询**；
    只有数据库里真有这号会话（话音链路的 32 位 hex）才多一次归属查询。
    """
    device_uuid = as_uuid(device_id)
    allowed = device_uuid is not None and await validate_thread_access(
        session,
        thread_id=thread_id,
        device_id=device_uuid,
        elder_id=as_uuid(elder_id),
    )
    if allowed:
        return
    logger.warning(
        "dialogue_thread_access_denied",
        extra={"trace_id": trace_id, "device_id": device_id or "-", "elder": bool(elder_id)},
    )
    raise HTTPException(status_code=403, detail=_THREAD_ACCESS_DENIED_DETAIL)


@router.post(
    "/text",
    response_model=DialogueTextResponse,
    summary="打字输入 → 对话回复（与话音同一条对话链）",
    responses={
        401: {"model": ErrorResponse, "description": "device token 缺失/失效/伪造"},
        403: {
            "model": ErrorResponse,
            "description": "thread 归属校验不通过（跨设备/跨长者/未知/形态非法）",
        },
        422: {"model": ErrorResponse, "description": "text 缺失、纯空白或超 200 字"},
    },
)
async def dialogue_text(
    body: DialogueTextRequest,
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[DialogueService, Depends(get_dialogue_service)],
    route_text: Annotated[RouteText, Depends(get_route_text)],
    recorder: Annotated[Any, Depends(get_conversation_recorder)],
    memory_hook: Annotated[Any, Depends(get_memory_hook)],
) -> DialogueTextResponse:
    """一轮打字对话：交给 `run_text_turn`（路由 → 安全告警落库 → 回话）→ 序列化。

    本函数只负责身份/线程号/日志与响应形状；**不内联任何对话编排**——重复一份编排
    就是第三份实现（按住说话 + 话音 + 打字），AGENTS §4-8 明确禁止。
    """
    started = time.perf_counter()
    trace_id = get_trace_id() or "-"
    device_id = identity.device_id or "-"
    thread_id = thread_id_for_device(device_id)
    # 归属闸：thread_id 已派生、即将交给 run_text_turn 读写会话 —— 就在这里校验它归谁。
    # device: 形态零 DB 查询；DB 里真有会话时才多一次归属查询（≤1 次）。
    await ensure_thread_access(
        session,
        thread_id=thread_id,
        device_id=identity.device_id,
        elder_id=identity.elder_id,
        trace_id=trace_id,
    )
    identity_view = _identity_view(trace_id=trace_id, elder_text=identity.elder_id)

    turn = await run_text_turn(
        session,
        text=body.text,
        elder_text=identity.elder_id,
        thread_id=thread_id,
        trace_id=trace_id,
        service=service,
        route_text=route_text,
        log_name="dialogue_text",
        # 两个旁路：事件流（这一轮发生过什么）与长期记忆（以后值不值得记）。
        # 都是 None 也照常回话。
        recorder=recorder,
        memory_hook=memory_hook,
    )

    logger.info(
        "dialogue_text_turn",
        extra={
            "trace_id": trace_id,
            "device_id": device_id,
            "elder_id": identity.elder_id or "-",
            "layer": turn.layer or "-",
            "safety_alert": turn.safety_alert,
            "degraded": turn.reply.degraded,
            "identity_source": identity_view.source if identity_view else "-",
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        },
    )
    return DialogueTextResponse(
        text=body.text,
        reply=DialogueReplyBody(text=turn.reply.text, audio_url=None),
        degraded=turn.reply.degraded,
        # 真实落库结果：路由命中但落库失败时为 False，前端不能据此变色。
        safety_alert=turn.alert_recorded,
        identity=identity_view,
    )


def _identity_view(*, trace_id: str, elder_text: str | None) -> DialogueIdentity | None:
    """本轮说话人的旁路结论（**只提示，不授权**）。**永不抛**。

    为什么打字链路不跑声纹：本端点手里**没有音频**——打字是文字进来，
    `identify_speaker()` 的第一个判断就是 `if not audio_bytes: return None`
    （`app/services/text_turn_service.py:172`）。硬跑一次只会白等推理容器超时，
    还会在那条路径上顺手开一条没人用的 `conversation_threads`。
    要声纹结论请走 `POST /api/audio/utterance`（它手里有整段音频）。

    两态（与话音链路的第三态 `voice` 区分开，见 `DialogueIdentity` 的 docstring）：

    - 设备绑了长者 → `source="device"` + `reason="no_audio"`：按设备绑定的长者继续。
      **不是**「声纹没认出来」，别让前端把它渲染成识别失败。
    - 设备没绑长者 → `source="generic"` + `reason="elder_unbound"` 且 `elder_id=None`，
      与 AC-28「仍不确定进通用模式不写个人数据」一致。

    为什么吞掉一切异常：身份只是响应里的一行提示，构造失败（比如上游传了非 UUID
    的字符串）不该让长者已经看到的那句话变成 500。缺字段时前端按 null 处理。
    """
    try:
        elder = (elder_text or "").strip()
        if not elder:
            return DialogueIdentity(source="generic", matched=False, reason="elder_unbound")
        return DialogueIdentity(source="device", matched=False, reason="no_audio", elder_id=elder)
    except Exception as exc:
        logger.warning(
            "dialogue_identity_skipped",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        return None


@router.post(
    "/tts",
    response_model=DialogueTtsResponse,
    summary="打字回复 → 合成语音（失败降级为纯字幕，仍 200）",
    responses={
        401: {"model": ErrorResponse, "description": "device token 缺失/失效/伪造"},
        422: {"model": ErrorResponse, "description": "text 缺失、纯空白或超 400 字"},
    },
)
async def dialogue_tts(
    body: DialogueTtsRequest,
    identity: Annotated[Identity, Depends(current_device)],
    tts: Annotated[TTSProvider, Depends(get_tts)],
    recorder: Annotated[Any, Depends(get_conversation_recorder)],
) -> DialogueTtsResponse:
    """把一句回复合成成静态 WAV，返回浏览器可直接播放的 URL 与时长。

    为什么不并进 `/text`：TTS 合成约 0.6× 实时（10s 回复要等 6s），塞进打字响应会让每句
    回复的回显都卡上几秒。先回字幕、再补声音，长者「它理我了」的反馈来得更快；
    前端在拿到字幕后再调本端点，声音与字幕/口型用 `audio_duration_ms` 对齐。

    失败口径：合成失败 / 空 pcm / 写盘失败 → **仍 200**，`audio_url=null`、
    `audio_duration_ms=null`、`degraded=true`（前端退回纯字幕）。判定与日志都在
    `synthesize_to_static`（它已记 `tts_audio_failed` / `tts_audio_skipped`），这里不重复记。

    事件流（2026-11-11 新增）：合成成功写一条 `response_audio_rendered`，语义是
    「服务端已把音频交给前端」，**不是**「长者听到了」。浏览器里的 `<audio>` 播没播，
    服务端无从验证，所以不冒充 `response_segment_delivered`（那个要求服务端亲手写播放器，
    只有话音链路做得到）。降级时不写——没合成出来的声音不存在「已交付」。

    本函数不碰 ORM：写入器自带会话，本模块只把参数转给它（AGENTS §2.1）。
    """
    started = time.perf_counter()
    trace_id = get_trace_id() or "-"
    device_id = identity.device_id or "-"

    audio = await synthesize_to_static(tts, body.text, trace_id=trace_id)
    degraded = audio is None

    logger.info(
        "dialogue_tts",
        extra={
            "trace_id": trace_id,
            "device_id": device_id,
            "elder_id": identity.elder_id or "-",
            "chars": len(body.text),
            "degraded": degraded,
            "audio_duration_ms": audio.duration_ms if audio else None,
            "audio_segments": len(audio.segments) if audio else 0,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        },
    )
    if audio is None:
        return DialogueTtsResponse(audio_url=None, audio_duration_ms=None, degraded=True)

    await _record_audio_rendered(
        recorder,
        device_id=device_id,
        elder_text=identity.elder_id,
        text=body.text,
        audio=audio,
        trace_id=trace_id,
    )
    return DialogueTtsResponse(
        audio_url=audio.url,
        audio_duration_ms=audio.duration_ms,
        audio_segments=[
            DialogueAudioSegment(
                text=segment.text,
                start_ms=segment.start_ms,
                duration_ms=segment.duration_ms,
                char_ms=list(segment.char_ms) if segment.char_ms else None,
            )
            for segment in audio.segments
        ],
        degraded=False,
    )


async def _record_audio_rendered(
    recorder: Any,
    *,
    device_id: str,
    elder_text: str | None,
    text: str,
    audio: Any,
    trace_id: str,
) -> None:
    """写一条 `response_audio_rendered`。**永不抛**。

    事件的 `thread_id` 走 `thread_id_from_text`：本端点没有会话 UUID（打字链路的线程号
    是 `device:<uuid>` 字符串），派生规则必须与 `run_text_turn` 那边逐字一致，
    否则「说了什么」与「声音交出去了」会落在两条互不相干的事件流上。
    """
    if recorder is None:
        return
    thread_id = thread_id_from_text(thread_id_for_device(device_id))
    elder_id = as_uuid(elder_text)
    if thread_id is None or elder_id is None:
        return
    try:
        await recorder.record(
            thread_id=thread_id,
            elder_id=elder_id,
            event_type="response_audio_rendered",
            payload={
                "audio_url": audio.url,
                "duration_ms": audio.duration_ms,
                "segments": len(audio.segments),
                "chars": len(text),
            },
            trace_id=trace_id,
        )
    except Exception as exc:
        logger.warning(
            "dialogue_tts_event_write_failed",
            extra={
                "trace_id": trace_id,
                "device_id": device_id,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )


__all__ = [
    "dialogue_text",
    "dialogue_tts",
    "ensure_thread_access",
    "get_conversation_recorder",
    "get_dialogue_service",
    "get_llm",
    "get_memory_hook",
    "get_route_text",
    "get_tts",
    "router",
]
