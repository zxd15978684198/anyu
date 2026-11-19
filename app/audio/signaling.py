"""WS /ws/signal —— WebRTC 信令通道：首帧鉴权、心跳保活、信令转发与断线清理。

A2 单元（设计记录（信令WS与鉴权））的四件事：**连接、鉴权、转发、清理**。

边界与口径：

- 传输：文本帧 JSON，每消息一个对象；消息类型以本文件顶部的 C2 契约块为准
  （迭代 3 冻结的 C2 **原样拷贝**，改动必须走契约变更流程）。
- 鉴权：首帧必须 `auth{token}`（连接后 3000ms 活体窗口内，见 `WS_AUTH_TIMEOUT_MS`）；复用 `app/core/auth.py` 的
  `resolve_principal`（组装 Bearer 头），不新造第二条鉴权入口；任务卡写的
  `verify_device_token` 是口径简称，实际函数名以代码为准。**不改 core/auth* 一行**。
- 身份：`elder_id` 只从服务端设备绑定（Identity）读，客户端传的一律丢弃（AGENTS 禁区 9）。
- 分层：`app/audio` 不写库（AGENTS §2）；room_id 经 `device_service.list_elder_candidates`
  只读复用（audio → services 单向依赖），不 import ORM。
- trace：TraceIdMiddleware 对 websocket scope 不注入（`app/core/trace.py` 的
  `scope["type"] != "http"` 直通分支），本层 accept 时自补 new_trace_id，连接 finally 里 reset。
- 审计：按裁决 A-02/S-08 只落结构化日志（trace_id / audit_action / actor / target / result），
  `audit_logs` 表 迭代 9 建后回填；日志**不回显 token**（S-23 ④）。
- 心跳：单向 C→S ping / S→C pong；45s 无任何帧判死（契约会裁决①）。
- 对 A3/A1 只暴露两个公开面：**C2 消息类型（TypedDict）+ 投递接口**
  （`deliver` / `deliver_asr_event` / `set_media_handler`）；A3 不得触碰
  WebSocket 实例、注册表与连接内部状态。
- 凭证吊销出口：`close_device_sessions(device_id)` 由 **api 层**（设备解绑 / 重发凭证 /
  禁用）调用，主动关掉该设备**已建立**的连接并回 4403。services 层不得 import 本模块
  （AGENTS §2.1），audio 也不写库，所以这条线只能由 api → audio 触发。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal, NotRequired, TypedDict
from uuid import UUID

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio import media_bridge
from app.audio.asr import ASREvent, ASREventKind
from app.core.auth import (
    AuthError,
    CredentialExpiredError,
    resolve_principal,
)
from app.core.context import get_trace_id, new_trace_id, reset_trace_id, set_trace_id
from app.core.degrade_speech import SPOKEN_DEGRADE
from app.core.metrics import counter, gauge, observe_ms
from app.core.runtime_context import Identity, reset_identity, set_identity
from app.core.security import KIND_DEVICE
from app.db import get_session
from app.services.device_service import list_elder_candidates

logger = logging.getLogger("app.audio.signaling")

# ------------------------------------------------------------------ 模块常量
# 迭代 3 config.py 只读（A1 域），心跳/超时参数先落这里为模块常量；
# 初值出处 adhoc-20261105 §六，实测后回填并同步 A5（README 端点表）。

# auth 首帧预算（毫秒）：accept 后 3000ms 内必须收到 auth{token}。
# 这是**防挂死的活体窗口，不是安全边界**：鉴权强度完全由 token 校验（签名 / exp / 设备绑定状态）
# 决定，窗口放宽不降低安全性，只避免「客户端首帧被主线程长任务推迟」被误判成鉴权失败。
# 依据（2026-11-13 P1 实测）：pad 刷新时 10.7MB VRM 解析与着色器编译占满主线程，
# ws.onopen 里同步发出的 auth 帧实际到达可达 0.5–1s；旧 300ms 窗口下服务端判 timeout，
# 下发 auth_fail 后 4401 关闭，前端按会话失效清 sessionStorage，页面被踢回登录页。
WS_AUTH_TIMEOUT_MS = 3000
# 客户端 ping 周期（契约裁决①：单向心跳，C→S）；服务端只回 pong，不做主动探测
WS_PING_INTERVAL_S = 20
# 45s 无任何帧（含业务帧）判死清理
WS_DEAD_AFTER_S = 45
# 判死检查轮询间隔（实现细节，测试 monkeypatch 用）
_DEAD_CHECK_INTERVAL_S = 1.0

# 自定义关闭码（4000–4999 为应用私有区间）
WS_CLOSE_AUTH_FAILED = 4401  # 鉴权失败（契约定值）
WS_CLOSE_SUPERSEDED = 4002  # 同设备踢旧留新（契约会裁决③）
# 凭证被吊销（解绑 / 重发凭证）：与 4002 分开，前端据此提示「设备已被管理员解绑、请重新登录」，
# 而不是「有人在别处登录了」。复用 4002 会让两种原因在端侧无法区分（S-08 排查口径）。
WS_CLOSE_DEVICE_REVOKED = 4403

# S7-3/4 观测口径（2026-11-13）：名字与标签是 Grafana 面板/告警的取值面，改名要走口径变更。
#: 下行 error 帧累计数，按降级 code 分样本；code 取 `_ERROR_TALKBACK` 的闭集键，
#: 不在集内的归 `internal` —— 标签基数必须可控，否则一个野 code 长一个样本。
ERROR_FRAME_COUNTER: Final = "error_frame_total"
#: 当前在线的 WS 信令连接数（UpDownCounter：accept 后 +1、连接收尾 -1）。
WS_CONNECTIONS_GAUGE: Final = "ws_connections"

router = APIRouter()

# ------------------------------------------------------------------ C2 契约（原样拷贝，冻结 v1.0）
# 方向标记：C→S 客户端发，S→C 服务端发。


class AuthMsg(TypedDict):  # C→S，连接后 3000ms 活体窗口内必须首发
    type: Literal["auth"]
    token: str  # 设备会话 access_token（E4 签发）；token 不进 URL


class AuthOkMsg(TypedDict):  # S→C
    type: Literal["auth_ok"]
    elder_id: str | None  # 展示用；业务归属一律服务端裁决
    room_id: str


class AuthFailMsg(TypedDict):
    """S→C，随后以 4401 关闭。"""

    type: Literal["auth_fail"]
    code: Literal["timeout", "invalid", "expired"]


__all__ = [
    "AnswerMsg",
    "AsrEndMsg",
    "AsrFinalMsg",
    "AsrPartialMsg",
    "AsrStartMsg",
    "AuthFailMsg",
    "AuthMsg",
    "AuthOkMsg",
    "ClientDiagMsg",
    "ErrorMsg",
    "IceMsg",
    "IdentityAskCandidate",
    "IdentityAskMsg",
    "IdentityConfirmMsg",
    "IdentityResultMsg",
    "OfferMsg",
    "PingMsg",
    "PongMsg",
    "TtsTextMsg",
]


class OfferMsg(TypedDict):
    """双向，谁先发起谁发（迭代 7 · S-D7-3）。

    C→S：浏览器主动建链（C2 原方向）。
    S→C：反向外呼，服务端当 offerer（用药到点主动连过去播报）。
    """

    type: Literal["offer"]
    sdp: str


class AnswerMsg(TypedDict):
    """双向，答对方那侧发（迭代 7 · S-D7-3，与 OfferMsg 成对反向）。

    S→C：服务端答浏览器（C2 原方向）。
    C→S：pad 答服务端外呼的 offer。
    """

    type: Literal["answer"]
    sdp: str


class IceMsg(TypedDict):
    """双向；candidate 为 ICE candidate JSON 字符串。"""

    type: Literal["ice"]
    candidate: str


class PingMsg(TypedDict):
    """C→S，20s 周期。"""

    type: Literal["ping"]


class PongMsg(TypedDict):
    """S→C；45s 无任何帧 → 服务端判死清理。"""

    type: Literal["pong"]


class AsrStartMsg(TypedDict):
    """C→S，TEN-VAD speechstart（preRoll 帧随其后补发）。"""

    type: Literal["asr_start"]


class AsrEndMsg(TypedDict):
    """C→S，speechend（0.8s 静音）。"""

    type: Literal["asr_end"]


class AsrPartialMsg(TypedDict):
    """S→C，字幕预显。"""

    type: Literal["asr_partial"]
    text: str


class AsrFinalMsg(TypedDict):
    """S→C，终稿字幕。"""

    type: Literal["asr_final"]
    text: str


class TtsTextMsg(TypedDict):
    """S→C，大字幕渲染数字人回复（打断时前端立即停播）。

    C6 `TtsTextMsgV2`：`degraded` 是新增字段（契约里标注「可选、缺省 False」）。
    服务端恒显式下发：问候走本地话术恒 False，将来 LLM 降级模板话术才置 True
    （FR-DLG-05）。写成必填是刻意的——「字段在不在」和「值是多少」分开判，
    少发一次就会在类型层被抓住。
    """

    type: Literal["tts_text"]
    text: str
    degraded: bool


class ErrorMsg(TypedDict):  # S→C，服务端降级/异常通知
    type: Literal["error"]
    code: Literal["asr_unavailable", "llm_timeout", "tool_breaker", "internal"]
    message: str  # 已脱敏的长者可读话术


# ------------------------------------------------------------------ C5 契约（原样拷贝，身份级联四消息）
# 方向标记同 C2：C→S 客户端发，S→C 服务端发。
# 这四条与 `app/audio/media_bridge.py` 的级联逻辑成对存在：字段名改动必须同步
# 前端 `frontend/src/pad/protocol.ts` 与契约 §C5，单改一边就是联调事故。


class IdentityAskCandidate(TypedDict):
    """identity_ask.candidates 的元素（C5：候选集恒 1 位）。"""

    elder_id: str
    display_name: str  # 服务端已脱敏（张桂芬 → 张*芬，设备服务 S-06）


class IdentityAskMsg(TypedDict):  # S→C
    """请长者/护工确认「是您吗」。起点在服务端，30s 无 confirm 则转 generic。"""

    type: Literal["identity_ask"]
    ask_id: str  # 服务端生成；identity_confirm 必须原样带回
    reason: Literal["low_confidence", "vp_unavailable"]  # 闭集，不自创第四个值
    candidates: list[IdentityAskCandidate]  # 恒 1 位
    timeout_s: int  # 恒 30（服务端判定，不以前端为准）


class IdentityConfirmMsg(TypedDict):  # C→S
    """点击「是我」大按钮（C5 裁决 ①：点选交互，不做语音确认）。"""

    type: Literal["identity_confirm"]
    ask_id: str
    elder_id: str


class IdentityResultMsg(TypedDict):  # S→C
    """身份终态通知；generic 时 elder_id=null，下游跳过全部 elder 绑定功能。"""

    type: Literal["identity_result"]
    source: Literal["voice", "confirm", "generic"]
    elder_id: str | None


class ClientDiagMsg(TypedDict):  # C→S，仅记日志不回执
    """端侧自检上报（VRM 加载 / WebRTC / VAD / ASR / 首句播放），只用于排障，不参与业务判定。"""

    type: Literal["client_diag"]
    stage: Literal["vrm_load", "webrtc", "vad", "asr", "playback"]
    ok: bool
    error: str
    #: 仅 `stage="playback"` 带：`tts_text` 到「第一段音频真的出声」的毫秒数。
    #: 上限由服务端截断（`_DIAG_ELAPSED_MAX_MS`），端侧算错也不会写出一个离谱的样本。
    elapsed_ms: NotRequired[int]


# ------------------------------------------------------------------ 连接上下文与注册表


@dataclass
class ConnectionContext:
    """一条 WS 连接的全部服务端状态。

    `websocket` 是执行计划 §3 W1 字段清单之外的实现必需项：踢旧留新（裁决③）要拿到
    旧连接的 WebSocket 才能发 close 帧。对 A3 仍不暴露（只读上层字段即可）。
    """

    trace_id: str
    websocket: WebSocket
    device_id: str | None = None
    elder_id: str | None = None
    room_id: str | None = None
    authed: bool = False
    last_frame_at: float = field(default_factory=time.monotonic)
    downstream: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    disconnect_callbacks: list[Callable[[], object]] = field(default_factory=list)


# 进程内连接注册表：device_id → 连接上下文（契约会裁决③：同设备踢旧留新，操作加锁）
_registry: dict[str, ConnectionContext] = {}
# 同一设备**全部在册连接**（含正在退场的那条）。`_registry` 只留最新一条（顶号语义），
# 但「凭证被吊销要踢干净」必须能看到顶号瞬间还没执行完 finally 的旧连接，否则会漏关一条。
# 与 `_registry` 同锁维护：注册时加入、`_cleanup` 与 `close_device_sessions` 时摘除。
# 用 list 而非 set：`ConnectionContext` 是可变 dataclass（不可哈希），只按对象身份去重。
_sessions: dict[str, list[ConnectionContext]] = {}
_registry_lock = asyncio.Lock()

# A3 注册的上行处理回调（offer / ice / asr_start / asr_end 的消费者）
UplinkHandler = Callable[[ConnectionContext, dict[str, Any]], Awaitable[None]]
_media_handler: UplinkHandler | None = None


def set_media_handler(handler: UplinkHandler | None) -> None:
    """A3 注册上行回调；传 None 注销。联调前为 None，此时上行信令回 error internal。"""
    global _media_handler
    _media_handler = handler


# 对话编排的注入点（迭代 4 收口 W4 · 计划 G-D5）。
#
# 为什么单独开一个 handler 而不是塞进 `_media_handler`：`_media_handler` 消费的是
# **上行信令**（offer / ice / asr_start / asr_end），而这里消费的是**下行结果**
# （`asr_final` 拿到转写文本之后要不要接着说话）。两者触发源不同、时序也不同
# （下行结果可能在媒体会话还没建好时就到），混在一个回调里会让「谁该 await 谁」
# 变得说不清。契约 C6 也明确「零新消息类型」，所以只在 FINAL 分支上开一个钩子。
DialogueFinalHandler = Callable[[str, str, str, str], Awaitable[None]]
_dialogue_handler: DialogueFinalHandler | None = None

#: 在途的编排任务强引用（见 `_schedule_dialogue_invoke`）。
_dialogue_tasks: set[asyncio.Task[None]] = set()


def set_dialogue_final_handler(handler: DialogueFinalHandler | None) -> None:
    """注册「终稿到达」回调；传 None 注销。

    形参：`(device_id, turn_id, trace_id, text)` —— `text` 是这一轮的转写文本。

    **不带音频**：本分支的 VAD 只做旁路观测（`app/audio/webrtc.py:1509` 只计数、
    不保留字节），硬加一个恒为 `None` 的 `audio_bytes` 形参只会让调用方误以为它可用。
    需要整段语音做重新识别时再单独补一条保留链路，不在 迭代 4 里假装有。
    """
    global _dialogue_handler
    _dialogue_handler = handler


def has_dialogue_final_handler() -> bool:
    """是否已注册终稿回调（`webrtc` 用它决定走真编排还是走垫层）。

    为什么需要这个判断：`app/main.py` 的 lifespan 装配完成后走真编排；
    而只起 `create_app()` 不跑 lifespan 的测试、以及尚未接编排列的部署，
    仍然要有「听到话之后回一句」的能力 —— 那条路由 `app/audio/reply_stub.py` 兜。
    两种都走会出现「一句话回两遍」，所以这里必须二选一。
    """
    return _dialogue_handler is not None


async def _invoke_dialogue_handler(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
    """调用已注册的终稿回调；**异常只记日志不外抛**（与 `deliver_asr_event` 的容错口径一致）。

    外抛的后果很具体：这个回调挂在新采集事件处理的调用链上，
    这里抛一次就会把整轮采集（以及 `end_turn` 的收尾）一起打断 ——
    编排层出问题不该让「听到话」这件事也失败。
    """
    handler = _dialogue_handler
    if handler is None:
        return
    try:
        await handler(device_id, turn_id, trace_id, text)
    except Exception as exc:  # 编排失败只记日志，绝不影响上行链路
        logger.warning(
            "dialogue_final_handler_failed",
            extra={
                "trace_id": trace_id,
                "device_id": device_id,
                "turn_id": turn_id,
                "text_len": len(text),
                "error_type": type(exc).__name__,
            },
        )


def _log_extra(conn: ConnectionContext, **fields: object) -> dict[str, object]:
    """结构化日志公共字段：每一行都带 trace_id（AGENTS §8）。"""
    extra: dict[str, object] = {
        "trace_id": conn.trace_id,
        "device_id": conn.device_id or "-",
    }
    extra.update(fields)
    return extra


def _audit_extra(conn: ConnectionContext, *, result: str, actor: str) -> dict[str, object]:
    """审计日志五字段（迭代 2 服务层审计模式，A-02/S-08：只落日志不写库）。"""
    return {
        "trace_id": conn.trace_id,
        "audit_action": "ws_auth_failed",
        "actor": actor,
        "target": "/ws/signal",
        "result": result,
    }


# ------------------------------------------------------------------ 下行投递接口（A3/A1 唯一公开面）

# error.code → 已脱敏的长者可读话术（C2 ErrorMsg.message 口径：不透出内部细节）
# 迭代 5 §4.3 收口：**唯一源** = app/core/degrade_speech.py::SPOKEN_DEGRADE（口语风格）。
# 本层只保留模块级别名，不再持有本地 dict 副本；测试与 A3/A1 继续按
# `signaling._ERROR_TALKBACK[<code>]` 取用（键名、措辞均不变）。
_ERROR_TALKBACK: Final[dict[str, str]] = SPOKEN_DEGRADE


def authed_connection(device_id: str) -> ConnectionContext | None:
    """该设备当前那条**已鉴权**的信令连接（没有/未鉴权返回 None）。

    `auth_ok` 之后、`offer` 之前浏览器就已经有页面了，但还没有 PeerConnection；
    开场白要等「数字人立起来」时需要问的正是这段时间，所以需要一个不依赖媒体会话的探针。
    """
    conn = _registry.get(device_id)
    if conn is None or not conn.authed:
        return None
    return conn


def deliver(device_id: str, message: Mapping[str, Any]) -> bool:
    """下行投递接口：把一条 C2 消息放进目标连接的发送队列。

    放不进（设备不在线 / 未鉴权）返回 False，调用方自行决定降级；本接口不抛异常。
    线程安全口径：仅允许在事件循环内调用；队列无界，用 put_nowait（同步上下文
    可直接调用，无需 await）。
    """
    conn = _registry.get(device_id)
    if conn is None or not conn.authed:
        return False
    conn.downstream.put_nowait(dict(message))
    return True


async def close_device_sessions(
    device_id: str,
    *,
    code: int = WS_CLOSE_DEVICE_REVOKED,
    reason: str = "device_unbound",
) -> int:
    """主动关闭某设备**全部在册连接**，返回实际关闭条数（凭证吊销唯一出口）。

    为什么需要它：鉴权只在握手做一次，`token_hash` 轮换只挡住**新**连接，已经在线的
    连接不会被解绑动作打断——不主动关，被解绑的设备会一直收下行消息直到自己断线
    （L9 验证 F-2：解绑后 15s 连接仍 open 并继续收到 `tts_text`）。

    分层：调用方只能是 api 层（AGENTS §2.1 —— services 不 import audio，audio 不写库）。
    本接口只碰连接对象与内存注册表，**不写库、不抛异常**；关不掉的连接交给它自己的
    `_cleanup` 收尾。
    """
    async with _registry_lock:
        targets = list(_sessions.pop(device_id, []))
        slot = _registry.get(device_id)
        if slot is not None and slot in targets:
            del _registry[device_id]
        for conn in targets:
            # 先摘鉴权态：关的过程中若有并发投递（deliver / authed_connection），
            # 会因未鉴权直接返回 False，不会把消息塞进一条马上就死的连接。
            conn.authed = False
    for conn in targets:
        with contextlib.suppress(Exception):
            await conn.websocket.close(code=code, reason=reason)
        logger.info(
            "ws_device_revoked",
            extra=_log_extra(conn, reason=reason, close_code=code),
        )
    return len(targets)


def deliver_asr_event(
    device_id: str,
    event: ASREvent,
    *,
    turn_id: str | None = None,
    trace_id: str | None = None,
) -> bool:
    """C1 `ASREvent` → C2 下行消息映射（A1/A3 消费）。

    C1 已冻结落地（`app/audio/asr.py`），正式 import 消费。
    `ERROR` 只映射固定脱敏话术，不透出 `ASRError.message` 内部细节。

    `turn_id` / `trace_id` 是 **迭代 4 收口 W4 追加的可选参数**（不传时行为与旧版完全一致）：
    终稿到达时要把「哪一轮、哪条链路」交给编排层，而这两个值只有采集链路（`webrtc`）
    手里有。走位置参数会打断既有 5 处调用与测试，故用仅关键字参数追加。

    **FINAL 分支在发完 `asr_final` 之后**调用已注册的编排回调（G-D5 的落点）。
    时机刻意放在下发之后：字幕先到屏幕，回复再慢慢合成 —— 反过来的话，
    长者会在没有任何反馈的情况下多等一段 LLM + TTS 的时间。
    """
    if event.kind is ASREventKind.PARTIAL:
        return deliver(
            device_id, {"type": "asr_partial", "text": event.result.text if event.result else ""}
        )
    if event.kind is ASREventKind.FINAL:
        text = event.result.text if event.result else ""
        delivered = deliver(device_id, {"type": "asr_final", "text": text})
        # 只有真发出去才继续编排：设备已经掉线时再往 LLM 花钱毫无意义。
        if delivered and turn_id is not None:
            _schedule_dialogue_invoke(device_id, turn_id, trace_id or "-", text)
        return delivered
    if event.kind is ASREventKind.ERROR:
        delivered = deliver(
            device_id,
            {
                "type": "error",
                "code": "asr_unavailable",
                "message": _ERROR_TALKBACK["asr_unavailable"],
            },
        )
        # 只有真发出去才计数：设备已掉线时「想发」不等于「发了」，那种情况由连接侧指标反映。
        if delivered:
            counter(ERROR_FRAME_COUNTER, code="asr_unavailable")
        return delivered
    return False


def schedule_dialogue_final(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
    """把一条已下发的终稿交给编排层（媒体层先做首句身份判定时用）。

    与 `deliver_asr_event` 内部的自动调度共用 `_schedule_dialogue_invoke`：调用方
    必须先自行发送 `asr_final`（`turn_id=None` 关闭自动调度），再调用本函数，
    保证「字幕先到、身份后定、回复最后开始」且不会重复编排。
    """
    _schedule_dialogue_invoke(device_id, turn_id, trace_id, text)


def _schedule_dialogue_invoke(device_id: str, turn_id: str, trace_id: str, text: str) -> None:
    """把编排回调挂到事件循环上（**不 await**），并兜住「没有运行中循环」的测试场景。

    为什么不 await：`deliver_asr_event` 的调用方 `_on_asr_event` 挂在采集链路上，
    一条 `asr_final` 要等 LLM + TTS 全部完成才能返回的话，用户下一次 `asr_start`
    （打断）就得排队 —— 打断失效比回复慢更不可接受。

    为什么 `get_running_loop` 要 try：同步单测里直接调 `deliver_asr_event` 时
    没有运行中的事件循环，`asyncio.create_task` 会抛 `RuntimeError`，
    而那会让一条本来只该「发个下行消息」的纯函数变成会炸的函数。
    """
    handler = _dialogue_handler
    if handler is None:
        return
    coro = _invoke_dialogue_handler(device_id, turn_id, trace_id, text)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的循环：把协程关掉（避免 never-awaited 警告），只记一条日志。
        coro.close()
        logger.info(
            "dialogue_final_handler_skipped",
            extra={"trace_id": trace_id, "device_id": device_id, "turn_id": turn_id},
        )
        return
    task = loop.create_task(coro)
    # 持有强引用直到完成：否则 task 可能在执行前被 GC 回收，
    # 日志里会留下「Task was destroyed but it is pending」这种没法定位的噪音。
    _dialogue_tasks.add(task)
    task.add_done_callback(_dialogue_tasks.discard)


# ------------------------------------------------------------------ 内部实现


def _parse_message(raw: str) -> dict[str, Any] | None:
    """文本帧 → 消息对象；非 JSON / 非对象 / 缺 type → None（按非法消息处理，裁决④）。"""
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
        return None
    return parsed


async def _receive_text_frame(conn: ConnectionContext) -> str | None:
    """读一帧文本；二进制帧返回 None（由调用方按非法帧处理）。

    为什么必须在这里接住：Starlette 的 `receive_text()` 实现是
    `message["text"]`，二进制帧没有 "text" 键，抛的是 **KeyError** 而不是
    WebSocketDisconnect。不接的话它会一路冒到端点外的 ASGI 层，Uvicorn 以
    1011（Internal Error）关连接 —— 未认证客户端发一帧二进制就能把连接打掉
    （审计 P1，2026-11-07）。C2 里没有「二进制消息」这种类型，所以它只是
    一条非法帧：记录 + 回 error / 继续跑，语义与 malformed_frame 一致。
    """
    try:
        return await conn.websocket.receive_text()
    except KeyError:
        logger.warning("ws_bad_message", extra=_log_extra(conn, reason="binary_frame"))
        return None


async def _direct_send(websocket: WebSocket, message: Mapping[str, Any]) -> None:
    """绕过下行队列的直发（仅鉴权阶段 / 关闭前使用；连接级消息必须先于 close 落地）。"""
    await websocket.send_text(json.dumps(dict(message), ensure_ascii=False))


async def _reject(conn: ConnectionContext, *, result: str) -> None:
    """鉴权失败统一出口：审计日志 → auth_fail → 4401 关闭。"""
    logger.warning("audit", extra=_audit_extra(conn, result=result, actor="unknown"))
    with contextlib.suppress(Exception):
        await _direct_send(conn.websocket, {"type": "auth_fail", "code": result})
    with contextlib.suppress(Exception):
        await conn.websocket.close(code=WS_CLOSE_AUTH_FAILED)


async def _authenticate(conn: ConnectionContext, session: AsyncSession) -> Identity | None:
    """首帧鉴权（W2）。返回 None 表示连接已被 auth_fail + 4401 关闭。

    失败 actor 一律记 `unknown`：失败路径无法可靠取得「已验证」的主体 ID，
    宁缺毋错（不回显 token 内容，S-23 ④）。
    """
    try:
        first_raw = await asyncio.wait_for(
            _receive_text_frame(conn), timeout=WS_AUTH_TIMEOUT_MS / 1000
        )
    except TimeoutError:  # = asyncio.TimeoutError（3.11+ 别名），ruff UP041
        await _reject(conn, result="timeout")
        return None
    except WebSocketDisconnect:
        raise

    if first_raw is None:
        # auth 前收到二进制帧：C2 口径「auth 前收到其他任何消息 → invalid + 4401」
        await _reject(conn, result="invalid")
        return None

    first = _parse_message(first_raw)
    if first is None or first.get("type") != "auth" or not isinstance(first.get("token"), str):
        # auth 前收到其他任何消息 / 非法帧 → invalid（C2 语义约定）
        await _reject(conn, result="invalid")
        return None

    try:
        identity = await resolve_principal(f"Bearer {first['token']}", session)
    except CredentialExpiredError:
        await _reject(conn, result="expired")
        return None
    except AuthError:
        await _reject(conn, result="invalid")
        return None

    # 本端点只收设备会话（C2 AuthMsg.token 口径）：其余三类主体按 invalid 拒
    if identity.kind != KIND_DEVICE or identity.device_id is None:
        await _reject(conn, result="invalid")
        return None

    conn.device_id = identity.device_id
    conn.elder_id = identity.elder_id
    conn.last_frame_at = time.monotonic()
    return identity


async def _register(conn: ConnectionContext) -> None:
    """接管注册表槽位；同设备旧连接踢下线（裁决③：踢旧留新，注册表操作加锁）。"""
    assert conn.device_id is not None  # _authenticate 通过后必非空
    async with _registry_lock:
        old = _registry.get(conn.device_id)
        _registry[conn.device_id] = conn
        peers = _sessions.setdefault(conn.device_id, [])
        if not any(existing is conn for existing in peers):
            peers.append(conn)
    if old is not None:
        logger.warning(
            "ws_superseded",
            extra=_log_extra(old, reason="same_device_reconnect"),
        )
        with contextlib.suppress(Exception):
            await old.websocket.close(code=WS_CLOSE_SUPERSEDED)


async def _writer(conn: ConnectionContext) -> None:
    """单一 writer 协程：排空下行队列逐帧发送（绑定本连接所在 loop，P1 风险 A2 侧落点）。"""
    try:
        while True:
            message = await conn.downstream.get()
            await conn.websocket.send_text(json.dumps(message, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except Exception:
        # 发送失败 = 连接已死：关掉 WebSocket 让主循环尽快感知（断线三来源之一）
        logger.info("ws_writer_failed", extra=_log_extra(conn))
        with contextlib.suppress(Exception):
            await conn.websocket.close()


async def _dead_watcher(conn: ConnectionContext) -> None:
    """判死任务（W3）：周期检查 45s 无任何帧 → 正常 close 并走断线清理。"""
    try:
        while True:
            await asyncio.sleep(_DEAD_CHECK_INTERVAL_S)
            if time.monotonic() - conn.last_frame_at > WS_DEAD_AFTER_S:
                logger.info(
                    "ws_dead",
                    extra=_log_extra(
                        conn,
                        idle_seconds=round(time.monotonic() - conn.last_frame_at, 3),
                    ),
                )
                await conn.websocket.close()
                return
    except asyncio.CancelledError:
        raise


async def _send_error(conn: ConnectionContext, code: str) -> None:
    """error 下行（裁决④：脱敏长者可读话术，不关连接）。

    每入队一帧记一笔 `error_frame_total{code}`。code 先过一遍闭集：`_ERROR_TALKBACK`
    之外的 code 一律按 `internal` 计数（话术本身也是这么取的），保证标签基数有界。
    """
    conn.downstream.put_nowait(
        {
            "type": "error",
            "code": code,
            "message": _ERROR_TALKBACK.get(code, _ERROR_TALKBACK["internal"]),
        }
    )
    counter(ERROR_FRAME_COUNTER, code=code if code in _ERROR_TALKBACK else "internal")


# client_diag 的 stage 闭集（C5）：不在集内的一律按内部错误码记，不建新枚举值。
# `playback` 是 迭代 5 P1-6 追加的第六节点（前端首段音频出声），仍只记日志 + 打点，不回执。
_DIAG_STAGES = frozenset({"vrm_load", "avatar_ready", "webrtc", "vad", "asr", "playback"})
# 端侧上报的 error 文本上限：排障够用，同时防止一条超长串把日志刷爆
_DIAG_ERROR_MAX = 200
# 端侧上报的 elapsed_ms 上限：端侧时钟抖动或算错不该造出一个 1e9 的耗时样本。
# 30s 的取法：比任何一档超时（2s/3s/1s）都宽得多，真超时早就走了降级路径，留足余量给
# 「录音回放/冷启首帧」这类慢用例；超过就按上限记，样本仍在分布里但不会把 P95 带飞。
_DIAG_ELAPSED_MAX_MS: Final = 30_000


def _handle_client_diag(conn: ConnectionContext, message: Mapping[str, Any]) -> None:
    """端侧自检上报（C5）：**只记日志 + 打点，不回执**。

    为什么不回 error：这条消息本身就是「客户端某处坏了」的上报，回一个 error 只会
    在客户端已经出问题的路径上再叠一层错误处理；排查要的是服务端日志里那一行。

    为什么要打点：PRD:543 的 1.5s 口径是「首段回答音频开始播放」，这一半在浏览器里，
    服务端看不到。`stage="playback"` 的 `elapsed_ms` 就是 ⑥ 前端播放节点唯一的来源，
    落到 `/metrics` 的 `client_playback_ms`（nearest-rank P50/P95 与其它指标同一套算法）。
    """
    stage = message.get("stage")
    ok = message.get("ok")
    # 先落到局部变量再 isinstance：直接对 `message.get("error")` 判类型，
    # 第二次 get() 是另一个表达式，mypy 不会把收窄结果带过来。
    raw_error = message.get("error")
    error = raw_error if isinstance(raw_error, str) else ""
    stage_key = stage if stage in _DIAG_STAGES else "unknown"
    counter("client_diag_total", stage=stage_key, ok=str(bool(ok)).lower())
    # 数字人立起来 = 可以开口了（产品负责人 2026-11-10 要求：先出人、再说话）。
    # 两个信号都算「立起来」：
    #   · `vrm_load ok=true` —— 3D 首帧真的画出来了；
    #   · `avatar_ready`     —— 端侧认为「屏幕上已经有形象了」，含 FR-3D-02 的静态头像降级
    #     （没 WebGL / 加载失败 / 后端没配形象）。降级头像也是一张能看的图，长者端不该因为
    #     GPU 不给力就永远听不到开场白；真的一张图都没有时，仍由 media_bridge 的 12s 上限放行。
    # 只认「成功」的上报。
    if stage_key in ("vrm_load", "avatar_ready") and bool(ok) and conn.device_id:
        from app.audio import media_bridge

        media_bridge.notify_avatar_ready(conn.device_id)
    elapsed_raw = message.get("elapsed_ms")
    # bool 是 int 的子类，`True` 不该被当成 1ms 的播放耗时
    elapsed_ms = (
        min(max(float(elapsed_raw), 0.0), float(_DIAG_ELAPSED_MAX_MS))
        if isinstance(elapsed_raw, (int, float)) and not isinstance(elapsed_raw, bool)
        else None
    )
    if elapsed_ms is not None and stage_key == "playback":
        observe_ms("client_playback_ms", elapsed_ms)
    logger.info(
        "ws_client_diag",
        extra=_log_extra(
            conn,
            stage=stage_key,
            stage_raw=str(stage),
            ok=bool(ok),
            elapsed_ms=elapsed_ms,
            error=error[:_DIAG_ERROR_MAX],
        ),
    )


async def _handle_identity_confirm(conn: ConnectionContext, message: Mapping[str, Any]) -> None:
    """identity_confirm 转交级联层（C5 裁决 ③：不匹配/重复由级联层静默忽略）。

    分发层不在这里做 ask_id / elder_id 校验：那份状态只有 `media_bridge` 有，
    两处各校验一遍迟早分叉（一边回 error 一边不回）。
    """
    await media_bridge.handle_identity_confirm(conn, message)


async def _read_loop(conn: ConnectionContext) -> None:
    """auth 后的消息主循环（W4）：任何合法帧都刷新 last_frame_at（W3：业务帧同样续命）。"""
    websocket = conn.websocket
    while True:
        raw = await _receive_text_frame(conn)
        conn.last_frame_at = time.monotonic()
        if raw is None:
            # 二进制帧（C2 无此消息类型）：回一条 error 继续听，绝不静默断、绝不 1011
            await _send_error(conn, "internal")
            continue
        message = _parse_message(raw)
        if message is None:
            logger.warning(
                "ws_bad_message",
                extra=_log_extra(conn, reason="malformed_frame"),
            )
            await _send_error(conn, "internal")
            continue

        msg_type = message["type"]
        if msg_type == "ping":
            conn.downstream.put_nowait({"type": "pong"})
            continue
        if msg_type == "auth":
            # 裁决②：连接内重复 auth 按非法凭证处理（防连接内换 token 旁路）
            logger.warning(
                "audit",
                extra=_audit_extra(conn, result="invalid", actor=f"device:{conn.device_id}"),
            )
            await _direct_send(websocket, {"type": "auth_fail", "code": "invalid"})
            await websocket.close(code=WS_CLOSE_AUTH_FAILED)
            return
        if msg_type == "identity_confirm":
            # C5：不匹配/重复/候选集外一律静默忽略，绝不回 error、绝不关连接（裁决③）
            await _handle_identity_confirm(conn, message)
            continue
        if msg_type == "client_diag":
            # C5：只记日志，不回执
            _handle_client_diag(conn, message)
            continue
        if msg_type in ("offer", "answer", "ice", "asr_start", "asr_end"):
            # 迭代 7 · S-D7-3：外呼复用 C2 既有信封、不新增类型，offer/answer 反向角色。
            # 所以 `answer` 也走媒体层 handler —— C2 冻结时它只有「服务端答浏览器」一个方向，
            # 没有反向用例的话，删掉这里的 "answer" 只会静默走进 unknown_type（连接不关），
            # 外呼就永远停在「offer 已下发、无人应答」。
            if msg_type in ("offer", "answer") and not isinstance(message.get("sdp"), str):
                logger.warning(
                    "ws_bad_message", extra=_log_extra(conn, reason=f"{msg_type}_sdp_not_str")
                )
                await _send_error(conn, "internal")
                continue
            handler = _media_handler
            if handler is None:
                logger.warning("ws_no_media_handler", extra=_log_extra(conn, msg_type=msg_type))
                await _send_error(conn, "internal")
                continue
            await handler(conn, message)
            continue

        # 未知消息类型（裁决④）：不关连接
        logger.warning("ws_bad_message", extra=_log_extra(conn, reason=f"unknown_type:{msg_type}"))
        await _send_error(conn, "internal")


async def _cleanup(conn: ConnectionContext) -> None:
    """断线清理统一收敛（W5）：三来源（客户端主动断 / 心跳判死 / 服务端异常）都走这里。

    只清 A2 域：注册表条目、disconnect_callbacks（A3 借此关 PeerConnection / ASR 会话 /
    TTS 在途任务 / 帧缓冲）。**不释放 PeerConnection 与音频轨**（A3 域，F8）。
    """
    if conn.device_id:
        async with _registry_lock:
            # 只有槽位仍是本连接时才移除：踢旧场景下新连接已接管，不能误删
            if _registry.get(conn.device_id) is conn:
                del _registry[conn.device_id]
            # 多连接登记表无条件摘除自己（顶号留下的旧连接也只能在这里退出）
            peers = _sessions.get(conn.device_id)
            if peers is not None:
                peers[:] = [existing for existing in peers if existing is not conn]
                if not peers:
                    del _sessions[conn.device_id]
    for callback in conn.disconnect_callbacks:
        try:
            outcome = callback()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.warning(
                "ws_disconnect_callback_failed",
                extra=_log_extra(conn),
                exc_info=True,
            )
    logger.info("ws_disconnected", extra=_log_extra(conn))


@router.websocket("/ws/signal")
async def signal_endpoint(
    websocket: WebSocket,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """WS /ws/signal 主端点（F9：无 /api 前缀）。

    trace 在 accept 前注入（auth 失败日志也要可查），连接 finally 里精确 reset
    （contextvar 严格成对，R4：WS 长连接不串号）。
    """
    trace_token = set_trace_id(new_trace_id())
    conn = ConnectionContext(trace_id=get_trace_id() or "-", websocket=websocket)
    dead_task: asyncio.Task[None] | None = None
    writer_task: asyncio.Task[None] | None = None
    identity_token = None
    # 只有 accept 真的成功过才在收尾里 -1：握手前就失败的连接从未计入，
    # 无条件减会把这个进程的 ws_connections 慢慢减成负数。
    accepted = False
    try:
        await websocket.accept()
        accepted = True
        gauge(WS_CONNECTIONS_GAUGE, value=1)
        dead_task = asyncio.create_task(_dead_watcher(conn))

        identity = await _authenticate(conn, session)
        if identity is None:
            return  # auth_fail + 4401 已发

        # room_id 只读复用 device_service（app/audio → services 单向依赖；不 import ORM）
        candidates = await list_elder_candidates(session, identity, UUID(identity.device_id))
        conn.room_id = str(candidates.room_id)
        conn.authed = True

        await _register(conn)

        # 身份注入（W2 步骤 6）：identity 只来自服务端鉴权；writer 在注入后创建，
        # task 创建时复制 contextvars，下行发送带身份上下文
        identity_token = set_identity(identity)
        writer_task = asyncio.create_task(_writer(conn))

        conn.downstream.put_nowait(
            {"type": "auth_ok", "elder_id": conn.elder_id, "room_id": conn.room_id}
        )
        logger.info("ws_authed", extra=_log_extra(conn, room_id=conn.room_id))

        # C5 身份级联：auth_ok 之后立刻判定身份（voice / confirm / generic），
        # 首屏问候不依赖 LLM（C6）。任何异常都在级联层降级 generic，不挡住宿舍会话。
        # **不传请求级 session**（P1-A）：cascade 的确认/超时操作可能在端点返回后
        # 才发生，那时本请求的会话已随 teardown 关闭；每条级联操作自开短会话。
        await media_bridge.start_identity_cascade(conn, identity)

        await _read_loop(conn)
    except WebSocketDisconnect:
        pass  # 客户端主动断：走 finally 清理
    finally:
        if accepted:
            gauge(WS_CONNECTIONS_GAUGE, value=-1)
        for task in (dead_task, writer_task):
            if task is not None:
                task.cancel()
        # 30s 定时器是会话的延伸：连接没了必须取消，否则到点会写一行没人认领的 thread。
        # 必须带 conn（归属守卫）：同设备重连时 _cascades 里已经是新连接那条级联，
        # 这条 finally 属于旧连接，无条件 pop 会把新连接的定时器一起删掉（P1 竞态）。
        await media_bridge.cancel_identity_cascade(conn.device_id, conn=conn)
        if identity_token is not None:
            reset_identity(identity_token)
        await _cleanup(conn)
        reset_trace_id(trace_token)
