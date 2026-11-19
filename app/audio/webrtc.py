"""A3 · WebRTC 媒体层（迭代 3 M1–M5 代码层）。

已实现：

- `MediaConfig`：只读 `app.core.config.Settings` 的真实字段，构造 aiortc ICE 配置；
- `MediaState` / `MediaFailureReason` / 媒体层异常；
- `MediaSession` + `MediaSessionRegistry`：`connection_id` 级 registry、running loop 捕获、
  任务登记、幂等关闭、`active_sessions` / `pending_tasks` 归零；
- `handle_offer` / `add_ice_candidate`：offer/answer、ICE 缓存与添加（兼容 C2 的
  `RTCIceCandidate` JSON 字符串和原始 SDP candidate）、ICE 超时看门狗与失败回调；
- `begin_turn` / `end_turn`：600 ms preRoll、60 ms ASR 帧、A1 `ASRProvider.stream()`；
- `enqueue_tts_pcm` / `barge_in`：TTS 16→48 kHz 写轨、有界队列、播放代际；
- 音频纯工具：PyAV 有状态重采样、20 ms 规范帧、60/30 ms 双缓冲。

**尚未接**：A2 的 `/ws/signal` 实际路由、真实浏览器 H1–H3 联调。本文件不直接发
WebSocket、不切 REST、不写业务回复。

服务端 VAD（`app/audio/vad.py` 的 `VadSession`）已在会话内接线，但**只做旁路观测**：
30 ms 帧喂进去，句末只加 `vad_speech_segments` 计数与一条日志，**不参与轮次判定**
（轮次仍由 A2 的 `asr_start` / `asr_end` 驱动）。`webrtcvad` 缺失或运行期异常一律降级
为 no-op，绝不让兜底能力反过来炸掉主链路。

约束（与 A3 任务卡一致）：

- 不写库、不发 WebSocket、不切 REST、不写业务回复；
- 只读真实 Settings，引用不存在的 `WEBRTC_ICE_SERVERS` 即错；
- localhost 只用 host candidate，不依赖未启动的 coturn；
- 日志只带 `trace_id / connection_id / turn_id / state / reason`，不打印 token、SDP、
  完整 ICE candidate 或原始音频。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

import av
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.sdp import candidate_from_sdp

from app.audio.asr import (
    ASRError,
    ASREvent,
    ASREventKind,
    ASRProvider,
    ASRUnavailable,
    build_asr,
)
from app.audio.reply_stub import ReplyStub
from app.core.config import Settings, get_settings
from app.core.timeouts import STAGE_ASR, with_timeout

# 迭代 6 W4：垫层接线真实路由（lazy LayaClient）与设备身份解析——
# 这两个都是函数级引用，import 本身不建任何连接 / 不读 .env（app/db.py 惰性）。
from app.router import wiring

if TYPE_CHECKING:
    from app.audio.signaling import ConnectionContext
    from app.audio.vad import VadSession

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 音频常数
# 16 bit / mono / 16 kHz 是 A3 内部唯一规范格式；所有帧长都由它推导。
TARGET_SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2

CANONICAL_FRAME_MS = 20
CANONICAL_FRAME_SAMPLES = 320
CANONICAL_FRAME_BYTES = 640

ASR_FRAME_MS = 60
ASR_FRAME_SAMPLES = 960
ASR_FRAME_BYTES = 1_920

VAD_FRAME_MS = 30
VAD_FRAME_SAMPLES = 480
VAD_FRAME_BYTES = 960

PRE_ROLL_MS = 600
PRE_ROLL_FRAMES = PRE_ROLL_MS // ASR_FRAME_MS  # 10 个 60 ms 帧

# 下行占位：answer 必须带音频 m-line；TTS 写轨在 M4 接入前先输出静音。
DOWNLINK_SAMPLE_RATE = 48_000
DOWNLINK_FRAME_MS = 20
DOWNLINK_FRAME_SAMPLES = 960

# 一个 turn 最多缓存 500 个 60 ms 帧（30 秒语音），超出即背压并记录丢弃。
MAX_ASR_QUEUE_FRAMES = 500
# 本轮上行 PCM 快照的上限（8 秒 @ 16 kHz mono s16le = 256 KB）。声纹首句判定只要几秒，
# 留 8s 足够覆盖「唤醒词 + 一句话」，同时保证长会话不涨内存。
UTTERANCE_PCM_MAX_BYTES = 16_000 * 2 * 8
# 下行播放队列最多 15 秒音频（750 个 20 ms 帧）；超限整段拒绝。
MAX_PLAYBACK_FRAMES = 750
DOWNLINK_FRAME_BYTES = DOWNLINK_FRAME_SAMPLES * SAMPLE_WIDTH_BYTES

_LOOPBACK_ICE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ICE_URL_SCHEMES = frozenset({"stun", "turn", "turns"})

AsrEventCallback = Callable[[str, ASREvent], Awaitable[None]]


# ------------------------------------------------------------------ 状态与异常


class MediaState(StrEnum):
    """PeerConnection / 媒体会话状态（对齐 A3 执行计划 §3.4）。"""

    OPEN = "open"
    OFFER_RECEIVED = "offer_received"
    ANSWER_CREATED = "answer_created"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class MediaFailureReason(StrEnum):
    """结构化失败原因；只表达媒体层事实，不发明 C2 未冻结的 WS 错误码。"""

    ICE_FAILED = "ice_failed"
    ICE_TIMEOUT = "ice_timeout"
    TRACK_ENDED = "track_ended"
    FORMAT_ERROR = "format_error"
    BACKPRESSURE = "backpressure"
    CLOSED = "closed"
    LOOP_MISMATCH = "loop_mismatch"
    CONFIG_ERROR = "config_error"
    STATE_ERROR = "state_error"
    TASK_EXCEPTION = "task_exception"


# A2 收到后负责记录/转发并调用 registry.close_session 移除会话；A3 不直接发 WS。
MediaFailureCallback = Callable[[str, MediaFailureReason, str], Awaitable[None]]


class MediaError(Exception):
    """媒体层错误基类：只表达「这次媒体操作被拒」，不构造 HTTP/WS 响应。"""


class MediaLoopMismatch(MediaError):
    """在非创建时的 running loop 上调用媒体对象。

    aiortc 内部任务绑定创建时的 loop；跨 loop 调用不会「凑合能跑」，而是明确失败，
    否则表现是任务永不执行、PC 状态卡死。
    """


class MediaClosedError(MediaError):
    """会话已关闭或正在关闭，后续操作被拒。"""


class MediaStateError(MediaError):
    """状态不允许该操作（如重复创建 PC、重复 connection_id）。"""


class MediaFormatError(MediaError):
    """PCM / 帧格式不满足契约（奇数长度、WAV 头、非 16k mono s16 等）。"""


class MediaConfigError(MediaError):
    """媒体配置不合法（只含数值边界，不回显凭据）。"""


class MediaBackpressureError(MediaError):
    """下行 TTS 队列超过上限；整段拒绝，不做部分入队或静默截断。"""


# ------------------------------------------------------------------ 结构化日志


def _log_event(
    event: str,
    *,
    trace_id: str,
    connection_id: str,
    turn_id: str | None,
    state: str,
    reason: str | None,
    **extra: object,
) -> None:
    """固定五个媒体日志字段，额外字段只允许 ID / 计数 / 状态，不放音频与凭据。"""
    fields: dict[str, object] = {
        "trace_id": trace_id or "-",
        "connection_id": connection_id,
        "turn_id": turn_id,
        "state": state,
        "reason": reason,
    }
    fields.update(extra)
    logger.info(event, extra=fields)


# ------------------------------------------------------------------ 配置


def _ice_url_host(url: str) -> str:
    """从 `stun:host:port` / `turn:host:port` 取 host；解析不了返回空串。

    不用 `urlsplit`：ICE URL 的 scheme 后用冒号而不是 `//`，标准 URL 解析器拿不到 host。
    """
    scheme, separator, remainder = url.partition(":")
    if not separator or scheme.lower() not in _ICE_URL_SCHEMES:
        return ""
    host_part = remainder.lstrip("/")
    if host_part.startswith("["):
        end = host_part.find("]")
        return host_part[1:end].lower() if end > 0 else ""
    return host_part.split(":", 1)[0].strip().lower()


def _is_loopback_ice_url(url: str) -> bool:
    """localhost / 127.0.0.1 / ::1 的 STUN/TURN 不加入配置。

    本机验收靠 aiortc 默认的 host candidate；把未启动的 `stun:localhost:3478` 塞进去
    只会让 ICE 等超时。非本机 URL 才表示真正的 STUN/TURN 依赖。
    """
    host = _ice_url_host(url)
    return not host or host in _LOOPBACK_ICE_HOSTS


def _normalize_ice_candidate(
    candidate: str,
    sdp_mid: str | None,
    sdp_mline_index: int | None,
) -> tuple[str, str | None, int | None]:
    """把 C2 的 candidate 归一化为 aiortc 需要的 SDP candidate + mid/index。

    A4 发送的是 `JSON.stringify(RTCIceCandidate)`，C2 也把 `candidate` 定义为
    ICE candidate JSON 字符串；A2 可能原样转发。这里同时兼容原始 SDP 字符串。
    显式传入的 `sdp_mid` / `sdp_mline_index` 优先于 JSON 内字段。
    """
    raw = candidate.strip()
    if not raw.startswith("{"):
        return candidate, sdp_mid, sdp_mline_index
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return candidate, sdp_mid, sdp_mline_index
    if not isinstance(payload, dict):
        return candidate, sdp_mid, sdp_mline_index

    value = payload.get("candidate")
    if value is None:
        normalized_candidate = ""
    elif isinstance(value, str):
        normalized_candidate = value
    else:
        return candidate, sdp_mid, sdp_mline_index

    mid_value = payload.get("sdpMid")
    if sdp_mid is None and isinstance(mid_value, str):
        sdp_mid = mid_value
    index_value = payload.get("sdpMLineIndex")
    if (
        sdp_mline_index is None
        and isinstance(index_value, int)
        and not isinstance(index_value, bool)
    ):
        sdp_mline_index = index_value
    return normalized_candidate, sdp_mid, sdp_mline_index


@dataclass(frozen=True, slots=True)
class MediaConfig:
    """媒体层实际使用的 Settings 投影；只读，不新增配置字段。"""

    stun_url: str
    turn_url: str
    turn_username: str
    turn_credential: str
    turn_credential_ttl_seconds: int
    ice_timeout_seconds: int
    audio_bitrate_kbps: int
    vad_aggressiveness: int
    # 有默认值：既有的直接构造（含 `tests/test_audio_webrtc.py` 的 `_config()`）不必改，
    # 默认值就是 `Settings.turn_port` 的默认值，两条入口不会分叉。
    turn_port: int = 3478
    # TURNS/DTLS 端口（2026-11-09 产品负责人要求「顺便把 TURN 也换成 TLS」）。
    # `None` = 不起 TLS 那条 URL；服务端自己的 `ice_servers()` 不受影响（同机走 3478 即可）。
    turn_tls_port: int | None = 5349
    # ICE 看门狗宽限（秒）：到点但 ICE 侧已有进展时再等这么久才判死。
    # 默认值 = `Settings.webrtc_ice_grace_seconds` 的默认值，两条入口不分叉。
    ice_grace_seconds: int = 10

    def __post_init__(self) -> None:
        if self.ice_timeout_seconds <= 0:
            raise MediaConfigError("webrtc_ice_timeout_seconds must be positive")
        if self.ice_grace_seconds < 0:
            raise MediaConfigError("webrtc_ice_grace_seconds must not be negative")
        if self.audio_bitrate_kbps <= 0:
            raise MediaConfigError("webrtc_audio_bitrate_kbps must be positive")
        if not 0 <= self.vad_aggressiveness <= 3:
            raise MediaConfigError("webrtc_vad_aggressiveness must be in 0..3")
        if self.turn_credential_ttl_seconds < 0:
            raise MediaConfigError("turn_credential_ttl_seconds must not be negative")
        if not 1 <= self.turn_port <= 65535:
            raise MediaConfigError("turn_port must be in 1..65535")
        if self.turn_tls_port is not None and not 1 <= self.turn_tls_port <= 65535:
            raise MediaConfigError("turn_tls_port must be None or in 1..65535")

    @classmethod
    def from_settings(cls, settings: Settings) -> MediaConfig:
        """按真实字段名读取；**不存在 `WEBRTC_ICE_SERVERS`**。"""
        return cls(
            stun_url=settings.stun_url,
            turn_url=settings.turn_url,
            turn_username=settings.turn_username,
            turn_credential=settings.turn_credential,
            turn_credential_ttl_seconds=settings.turn_credential_ttl_seconds,
            ice_timeout_seconds=settings.webrtc_ice_timeout_seconds,
            ice_grace_seconds=settings.webrtc_ice_grace_seconds,
            audio_bitrate_kbps=settings.webrtc_audio_bitrate_kbps,
            vad_aggressiveness=settings.webrtc_vad_aggressiveness,
            turn_port=settings.turn_port,
            # `Settings.turn_tls_port <= 0` 表示「不下发 turns:」；翻译成 None，
            # 免得 __post_init__ 把「关闭」当成非法端口。
            turn_tls_port=settings.turn_tls_port if settings.turn_tls_port > 0 else None,
        )

    def ice_servers(self) -> list[RTCIceServer]:
        """构造 aiortc ICE server 列表。

        - STUN：URL 非空且不是 loopback 才加入；
        - TURN：URL / 用户名 / 凭据 / TTL 全部非空且不是 loopback 才加入。
        """
        servers: list[RTCIceServer] = []
        if self.stun_url.strip() and not _is_loopback_ice_url(self.stun_url):
            servers.append(RTCIceServer(urls=[self.stun_url]))
        if (
            self.turn_url.strip()
            and self.turn_username.strip()
            and self.turn_credential.strip()
            and self.turn_credential_ttl_seconds > 0
            and not _is_loopback_ice_url(self.turn_url)
        ):
            servers.append(
                RTCIceServer(
                    urls=[self.turn_url],
                    username=self.turn_username,
                    credential=self.turn_credential,
                )
            )
        return servers

    def ice_servers_for_client(self, host: str) -> list[dict[str, Any]]:
        """下发给**浏览器**的 ICE 配置（W2 · `GET /api/audio/ice-config`）。

        与上面 `ice_servers()` 的三点差别，每一点都是刻意的：

        1. **不过滤 loopback**：服务端自己连 TURN 时 loopback 没有意义（同机直连走
           host candidate），而浏览器拿到的 host 来自请求头，`localhost` 正是本机联调
           的正当值。过滤掉它会让「本机浏览器 → 本机 coturn」这条路永远只拿到 STUN。
        2. **host 由调用方传入**，URL 在这里现拼 `stun:<host>:<turn_port>` /
           `turn:<host>:<turn_port>`；`stun_url` / `turn_url` 在这条路上**只当开关**
           （非空 = 启用），不再贡献主机名 —— 这正是同一份 `.env` 在本机（localhost）
           与服务器（公网域名）都正确的原因。
        3. 返回 `dict` 而不是 `RTCIceServer`：API 层只做 JSON 序列化，不需要（也不应该）
           为了下发配置去依赖 aiortc 的远端类型。

        降级口径（产品负责人 2026-11-09 裁决）：TURN 三项（`turn_url` / `turn_username` /
        `turn_credential`）任一为空 → 只给 STUN；STUN 也没开 → `[]`（前端退化为无参
        建连，不许抛错）。`host` 为空同样返回 `[]`：拼出 `turn::3478` 是坏 URI，
        宁可不给也不能把一个必然失败的地址塞进 `RTCPeerConnection`。
        """
        normalized_host = host.strip()
        if not normalized_host:
            return []

        # STUN 与 TURN 共用 coturn 的 `--listening-port`，所以这两种 URL 用同一个 turn_port。
        urls: list[str] = []
        if self.stun_url.strip():
            urls.append(f"stun:{normalized_host}:{self.turn_port}")
        turn_ready = bool(
            self.turn_url.strip() and self.turn_username.strip() and self.turn_credential.strip()
        )
        if turn_ready:
            urls.append(f"turn:{normalized_host}:{self.turn_port}")
            # TURNS：coturn 的 `--tls-listening-port`，与 3478 明文 TURN **同时下发**。
            # 浏览器对 turns: 不做「忽略证书错误」那一套：证书不在受信根里时这条直接连不上，
            # 但同一个 RTCIceServer 里的 turn: 会照常走 UDP 中继，所以两条并列不会把链路拖死，
            # 装了受信证书（手机装 CA / 桌面导入根存储）之后自然升级到 TLS。
            if self.turn_tls_port is not None:
                urls.append(f"turns:{normalized_host}:{self.turn_tls_port}?transport=tcp")
        if not urls:
            return []

        server: dict[str, Any] = {"urls": urls}
        if turn_ready:
            # 口令明文随响应下发是 W2 的既定口径（长期静态口令），残余风险见
            # docs/SECURITY.md §2.5 与 docs/接口契约_音频与信令.md §6。
            server["username"] = self.turn_username
            server["credential"] = self.turn_credential
        return [server]


# ------------------------------------------------------------------ 音频纯工具


@dataclass(frozen=True, slots=True)
class PcmFrame:
    """一段 PCM16 little-endian 帧；`data` 是原始字节，不含容器头。"""

    data: bytes
    sample_rate: int = TARGET_SAMPLE_RATE
    channels: int = 1
    sample_width: int = SAMPLE_WIDTH_BYTES

    @property
    def sample_count(self) -> int:
        return len(self.data) // (self.sample_width * self.channels)

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1000.0 / self.sample_rate


def _make_frame(data: bytes, *, duration_ms: int) -> PcmFrame:
    expected = int(TARGET_SAMPLE_RATE * duration_ms / 1000) * SAMPLE_WIDTH_BYTES
    if len(data) != expected:
        raise MediaFormatError(
            f"expected {expected} bytes for {duration_ms} ms mono s16, got {len(data)}"
        )
    return PcmFrame(data=data)


class PcmFramer:
    """把任意长度的 PCM16 字节流聚合成固定长度帧；尾片可零填充。"""

    def __init__(
        self,
        *,
        frame_bytes: int = CANONICAL_FRAME_BYTES,
        duration_ms: int = CANONICAL_FRAME_MS,
    ) -> None:
        if frame_bytes <= 0 or frame_bytes % SAMPLE_WIDTH_BYTES != 0:
            raise ValueError("frame_bytes must be a positive multiple of 2")
        self._frame_bytes = frame_bytes
        self._duration_ms = duration_ms
        self._buffer = bytearray()

    def feed(self, pcm: bytes) -> list[PcmFrame]:
        if len(pcm) % SAMPLE_WIDTH_BYTES != 0:
            raise MediaFormatError("PCM16 data must have even length")
        self._buffer.extend(pcm)
        frames: list[PcmFrame] = []
        while len(self._buffer) >= self._frame_bytes:
            chunk = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]
            frames.append(_make_frame(chunk, duration_ms=self._duration_ms))
        return frames

    def flush(self) -> PcmFrame | None:
        """把不足一帧的尾片零填充后发出；没有尾片返回 None。"""
        if not self._buffer:
            return None
        padding = self._frame_bytes - len(self._buffer)
        data = bytes(self._buffer) + b"\x00" * padding
        self._buffer.clear()
        return _make_frame(data, duration_ms=self._duration_ms)

    def reset(self) -> None:
        self._buffer.clear()


class AudioNormalizer:
    """aiortc `AudioFrame` → 16k/16bit/mono 的 20 ms 规范帧。

    PyAV 的 `AudioResampler` 是**有状态**的：输入格式变化时重建；输出样本先进入
    `PcmFramer` 再切帧。禁止 `[::3]`、`np.repeat` 等抽点/插值替代重采样。
    """

    def __init__(self) -> None:
        self._resampler: av.AudioResampler | None = None
        self._input_signature: tuple[str, str, int] | None = None
        self._framer = PcmFramer()

    def feed(self, frame: av.AudioFrame) -> list[PcmFrame]:
        signature = self._frame_signature(frame)
        if self._resampler is None or signature != self._input_signature:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=TARGET_SAMPLE_RATE
            )
            self._input_signature = signature

        resampler = self._resampler
        if resampler is None:  # pragma: no cover - 上面的分支已保证
            raise MediaFormatError("audio resampler is not initialised")

        frames: list[PcmFrame] = []
        for output in resampler.resample(frame):
            frames.extend(self._framer.feed(_audio_frame_bytes(output)))
        return frames

    def flush(self) -> list[PcmFrame]:
        """结束输入：先冲 resampler 内部延迟，再零填充 20 ms 尾帧。"""
        frames: list[PcmFrame] = []
        if self._resampler is not None:
            for output in self._resampler.resample(None):
                frames.extend(self._framer.feed(_audio_frame_bytes(output)))
        tail = self._framer.flush()
        if tail is not None:
            frames.append(tail)
        self.reset()
        return frames

    def reset(self) -> None:
        self._resampler = None
        self._input_signature = None
        self._framer.reset()

    @staticmethod
    def _frame_signature(frame: av.AudioFrame) -> tuple[str, str, int]:
        fmt = frame.format.name if frame.format is not None else ""
        layout = frame.layout.name if frame.layout is not None else ""
        return str(fmt or ""), str(layout or ""), int(frame.sample_rate)


def _audio_frame_bytes(frame: av.AudioFrame) -> bytes:
    """从重采样后的 s16/mono 帧取 PCM 字节。

    `s16` 是 packed 单平面；多平面说明重采样目标不对，直接报格式错误，不做静默拼接。
    """
    if len(frame.planes) != 1:
        raise MediaFormatError("resampled audio frame must have exactly one plane")
    # AudioPlane 的底层 buffer 可能带对齐填充；必须按 samples 截断，不能整块当成 PCM。
    expected = frame.samples * SAMPLE_WIDTH_BYTES
    raw = bytes(frame.planes[0])
    if len(raw) < expected:
        raise MediaFormatError("resampled audio plane is shorter than its sample count")
    data = raw[:expected]
    if len(data) % SAMPLE_WIDTH_BYTES != 0:
        raise MediaFormatError("resampled PCM16 data must have even length")
    return data


class DualFrameSplitter:
    """同一 20 ms 规范流分叉成 60 ms ASR 帧与 30 ms VAD 帧。

    只做字节聚合，不二次重采样：3 个 20 ms 帧 = 1 个 60 ms ≈ 2 个 30 ms 帧。
    """

    def __init__(self) -> None:
        self._asr_buffer = bytearray()
        self._vad_buffer = bytearray()

    def feed(self, frame: PcmFrame) -> tuple[list[PcmFrame], list[PcmFrame]]:
        self._validate_source(frame)
        self._asr_buffer.extend(frame.data)
        self._vad_buffer.extend(frame.data)
        return (
            self._drain(self._asr_buffer, ASR_FRAME_BYTES, ASR_FRAME_MS),
            self._drain(self._vad_buffer, VAD_FRAME_BYTES, VAD_FRAME_MS),
        )

    def flush(self) -> tuple[list[PcmFrame], list[PcmFrame]]:
        """结束 turn：两个缓冲的尾片都零填充成整帧，最多增加 59 ms 静音。"""
        return (
            self._flush(self._asr_buffer, ASR_FRAME_BYTES, ASR_FRAME_MS),
            self._flush(self._vad_buffer, VAD_FRAME_BYTES, VAD_FRAME_MS),
        )

    def discard(self) -> None:
        """barge-in：丢弃未发送残留，不补零填充。"""
        self.reset()

    def reset(self) -> None:
        """开始新 turn 前清空两个缓冲。"""
        self._asr_buffer.clear()
        self._vad_buffer.clear()

    @staticmethod
    def _validate_source(frame: PcmFrame) -> None:
        if (
            frame.sample_rate != TARGET_SAMPLE_RATE
            or frame.channels != 1
            or frame.sample_width != SAMPLE_WIDTH_BYTES
        ):
            raise MediaFormatError("splitter input must be 16 kHz/16 bit/mono PCM")

    @staticmethod
    def _drain(buffer: bytearray, frame_bytes: int, duration_ms: int) -> list[PcmFrame]:
        frames: list[PcmFrame] = []
        while len(buffer) >= frame_bytes:
            chunk = bytes(buffer[:frame_bytes])
            del buffer[:frame_bytes]
            frames.append(_make_frame(chunk, duration_ms=duration_ms))
        return frames

    @classmethod
    def _flush(cls, buffer: bytearray, frame_bytes: int, duration_ms: int) -> list[PcmFrame]:
        frames = cls._drain(buffer, frame_bytes, duration_ms)
        if buffer:
            padding = frame_bytes - len(buffer)
            data = bytes(buffer) + b"\x00" * padding
            buffer.clear()
            frames.append(_make_frame(data, duration_ms=duration_ms))
        return frames


class PreRollBuffer:
    """600 ms preRoll：只保存最近 10 个 60 ms ASR 帧。"""

    def __init__(self, capacity_frames: int = PRE_ROLL_FRAMES) -> None:
        if capacity_frames <= 0:
            raise ValueError("capacity_frames must be positive")
        self._frames: deque[PcmFrame] = deque(maxlen=capacity_frames)

    def append(self, frame: PcmFrame) -> None:
        if (
            frame.sample_rate != TARGET_SAMPLE_RATE
            or frame.channels != 1
            or frame.sample_width != SAMPLE_WIDTH_BYTES
            or frame.sample_count != ASR_FRAME_SAMPLES
        ):
            raise MediaFormatError("pre-roll only accepts 60 ms 16 kHz mono s16 frames")
        self._frames.append(frame)

    def recent(self) -> tuple[PcmFrame, ...]:
        return tuple(self._frames)

    def clear(self) -> None:
        self._frames.clear()

    @property
    def duration_ms(self) -> float:
        return sum(frame.duration_ms for frame in self._frames)


def validate_downlink_pcm(pcm: bytes) -> None:
    """校验 TTS 下行原始 PCM：偶数长度、无 RIFF/WAVE 容器头、非空。

    采样率由 C1 契约保证为 16 kHz；raw PCM 本身无法从字节反推采样率，不做猜测。
    """
    if not pcm:
        raise MediaFormatError("downlink PCM must not be empty")
    if len(pcm) % SAMPLE_WIDTH_BYTES != 0:
        raise MediaFormatError("downlink PCM must have even length")
    if pcm[:4] in (b"RIFF", b"WAVE"):
        raise MediaFormatError("downlink PCM must not contain a RIFF/WAVE header")


# ------------------------------------------------------------------ 下行占位轨与 ASR 帧迭代


def _tts_pcm_to_48k_frames(pcm: bytes) -> list[PcmFrame]:
    """16 kHz/16 bit/mono raw PCM → 48 kHz/20 ms/1920 B 帧。

    TTS 是整段输入，每次新起一个有状态 resampler，避免跨越两段回复残留重采样状态。
    """
    source = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // SAMPLE_WIDTH_BYTES)
    source.sample_rate = TARGET_SAMPLE_RATE
    source.planes[0].update(pcm)

    resampler = av.AudioResampler(format="s16", layout="mono", rate=DOWNLINK_SAMPLE_RATE)
    raw = bytearray()

    def _collect(outputs: list[av.AudioFrame]) -> None:
        for output in outputs:
            # AudioPlane 可能带对齐填充，必须按 samples 截断。
            raw.extend(bytes(output.planes[0])[: output.samples * SAMPLE_WIDTH_BYTES])

    _collect(resampler.resample(source))
    _collect(resampler.resample(None))

    frames: list[PcmFrame] = []
    for start in range(0, len(raw) - DOWNLINK_FRAME_BYTES + 1, DOWNLINK_FRAME_BYTES):
        frames.append(
            PcmFrame(
                data=bytes(raw[start : start + DOWNLINK_FRAME_BYTES]),
                sample_rate=DOWNLINK_SAMPLE_RATE,
            )
        )
    remainder = len(raw) % DOWNLINK_FRAME_BYTES
    if remainder:
        tail = bytes(raw[-remainder:]) + b"\x00" * (DOWNLINK_FRAME_BYTES - remainder)
        frames.append(PcmFrame(data=tail, sample_rate=DOWNLINK_SAMPLE_RATE))
    return frames


class _Stream48kResampler:
    r"""**状态化** 16 kHz int16 mono → 48 kHz 重采样器（迭代 5 F1）。

    ## 为什么不能复用 `_tts_pcm_to_48k_frames`

    那个函数每次调用都新起一个 `av.AudioResampler`，并且立刻 `resample(None)` 把
    内部缓冲冲干净 —— 对「整段一次输入」是对的（避免跨回复残留状态），但对**按句
    逐片输入**是错的：每句边界都会丢掉不足一帧的残样、并重置滤波器的历史样本，
    结果是句与句之间出现咔哒声与时间抖动。

    照搬本人既有工程 asr 的 `_Stream48kResampler`（providers/anyu_agent.py:125）
    的做法：重采样器常驻，`feed()` 只喂不冲；不足一帧的残样由调用方（`MediaSession`
    的 `_playback_carry`）留到下一片；整轮结束时 `flush()` 收尾。

    ## `flush()` 之后这个实例就废了

    `flush()` 会把内部 `_resampler` 置 `None`，下次 `feed()` 会重建。所以整轮结束后
    调用方应当把它丢掉（`MediaSession` 就是这么做的），不要跨轮复用。
    """

    def __init__(self, target_rate: int = DOWNLINK_SAMPLE_RATE) -> None:
        self.target_rate = target_rate
        self._rate: int | None = None
        self._resampler: av.AudioResampler | None = None

    def _ensure(self, rate: int) -> av.AudioResampler:
        if self._resampler is None or rate != self._rate:
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self.target_rate)
            self._rate = rate
        return self._resampler

    def feed(self, pcm: bytes, rate: int = TARGET_SAMPLE_RATE) -> bytes:
        """喂入一片 raw PCM，返回本次可用的 48 kHz 字节（不保证 20 ms 对齐）。"""
        samples = len(pcm) // SAMPLE_WIDTH_BYTES
        if samples <= 0:
            return b""
        resampler = self._ensure(rate)
        source = av.AudioFrame(format="s16", layout="mono", samples=samples)
        source.sample_rate = rate
        source.planes[0].update(pcm[: samples * SAMPLE_WIDTH_BYTES])
        return _frames_to_bytes(resampler.resample(source))

    def flush(self) -> bytes:
        """取出重采样器内部残留（未凑满一帧的尾巴），并把实例复位。"""
        resampler = self._resampler
        if resampler is None:
            return b""
        self._resampler = None
        self._rate = None
        return _frames_to_bytes(resampler.resample(None))


def _frames_to_bytes(frames: list[av.AudioFrame]) -> bytes:
    """`av` 输出帧 → 原始字节；按 `samples` 截断，别把对齐填充带上。"""
    out = bytearray()
    for frame in frames:
        out.extend(bytes(frame.planes[0])[: frame.samples * SAMPLE_WIDTH_BYTES])
    return bytes(out)


@dataclass(frozen=True, slots=True)
class ChunkPlacement:
    """一片 PCM 在**播放流**里的落位：入队帧数 + 首帧的绝对播放序号。

    为什么需要 `start_frame`：`PlaybackAudioTrack` 队列空时会补静音帧且序号照增，
    所以「已入队帧数」累加出来的时间轴**不含静音间隙**。逐句串行合成一旦慢于播放，
    真实音频里就多出一段字幕时间轴上不存在的静音，偏差随句数累加。
    播放序号把静音一并计入，字幕因此与耳朵听到的顺序严格对齐。
    """

    frames: int
    start_frame: int


class PlaybackAudioTrack(MediaStreamTrack):
    """下行 TTS 播放轨：队列有帧就播，空队列补 48 kHz 静音。

    队列由 `MediaSession` 持有并在 `barge_in` 时清空；本类只负责实时出帧、PTS 单调递增。
    同一时刻最多允许 1 个在途 20 ms 帧完成，下一帧起新内容/静音。
    """

    kind = "audio"

    def __init__(self, queue: asyncio.Queue[PcmFrame]) -> None:
        super().__init__()
        self._queue = queue
        self._pts = 0

    @property
    def played_frames(self) -> int:
        """已出帧数（**含静音帧**）。由 PTS 换算，不另设计数器以免两处状态漂移。"""
        return self._pts // DOWNLINK_FRAME_SAMPLES

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        # 按实时节奏出帧，避免 aiortc 发送器空转占满 CPU。
        await asyncio.sleep(DOWNLINK_FRAME_MS / 1000)
        if self.readyState != "live":
            raise MediaStreamError

        frame = av.AudioFrame(format="s16", layout="mono", samples=DOWNLINK_FRAME_SAMPLES)
        frame.sample_rate = DOWNLINK_SAMPLE_RATE

        pcm: bytes | None = None
        if not self._queue.empty():
            item = self._queue.get_nowait()
            if (
                item.sample_rate == DOWNLINK_SAMPLE_RATE
                and item.channels == 1
                and item.sample_width == SAMPLE_WIDTH_BYTES
                and item.sample_count == DOWNLINK_FRAME_SAMPLES
            ):
                pcm = item.data
        if pcm is not None:
            frame.planes[0].update(pcm)

        frame.pts = self._pts
        frame.time_base = Fraction(1, DOWNLINK_SAMPLE_RATE)
        self._pts += DOWNLINK_FRAME_SAMPLES
        return frame


async def _asr_frame_iterator(queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
    """把会话队列暴露成 C1 `ASRProvider.stream(audio)` 需要的异步迭代器。"""
    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


# ------------------------------------------------------------------ 服务端 VAD 兜底
# 定位（AGENTS §1「一个能力只允许一种形态」）：端侧 TEN-VAD 是主链路，这里只是服务端
# 兜底观测。**不引入第二种 VAD 实现**，只复用 `app/audio/vad.py` 的 `VadSession`。
#
# 三条硬约束：
# 1. 只旁路消费 30 ms 帧，句末只加计数器 + 日志，**不改轮次语义**；
# 2. 依赖缺失（webrtcvad 装不上）→ 降级 no-op + warn，会话照常跑；
# 3. 运行期任何异常同样降级 no-op：兜底能力不许反过来炸掉主链路。


def _build_vad_session(aggressiveness: int) -> tuple[VadSession | None, str | None]:
    """构造服务端 VAD 会话，返回 `(session, error_type)`。

    失败时 `session is None`、`error_type` 是异常类型名（**只回类型，不回路径/参数**）。
    失败原因必须由调用方带 `trace_id` 打日志——本函数没跑在请求上下文里，自己打日志
    就漏了链路字段（AGENTS §8）。

    这里用函数内 import 而不是模块顶层 import：`app/audio/vad.py` 顶层 `import webrtcvad`，
    顶层 import 会让整个媒体层跟着一个可选依赖陪葬；运行期按需 import 才能把失败
    收敛成「本次会话无 VAD」。
    """
    try:
        from app.audio.vad import VadSession as _VadSession

        return _VadSession(aggressiveness=aggressiveness), None
    except Exception as exc:  # 可选依赖的失败形态不可穷举（ImportError/OSError/…），必须全兜
        return None, type(exc).__name__


# ------------------------------------------------------------------ 会话与 registry


class MediaSession:
    """一个 `connection_id` 的媒体会话。

    已接：running loop 捕获、PC 创建、offer/answer、ICE candidate 缓存/添加、ICE 超时
    看门狗与失败回调、收轨重采样分帧、600 ms preRoll、`begin_turn`/`end_turn`、ASR 事件
    回调、TTS 16→48 kHz 写轨、`barge_in` 播放代际、幂等关闭与资源观测。
    未接：真实浏览器/A2 WS 联调、A5 VAD 判定回调；H1/H2/H3 仍待联调。
    """

    def __init__(
        self,
        connection_id: str,
        trace_id: str,
        config: MediaConfig,
        *,
        asr_provider: ASRProvider | None = None,
        on_asr_event: AsrEventCallback | None = None,
        on_media_failed: MediaFailureCallback | None = None,
    ) -> None:
        if not connection_id:
            raise ValueError("connection_id must not be empty")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise MediaLoopMismatch("MediaSession must be created inside a running loop") from exc

        self.connection_id = connection_id
        self.trace_id = trace_id or "-"
        self.config = config
        self._loop = loop
        self._state = MediaState.OPEN
        self._turn_id: str | None = None
        self._pc: RTCPeerConnection | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._asr_provider = asr_provider
        self._on_asr_event = on_asr_event
        self._on_media_failed = on_media_failed
        self._ice_timeout_task: asyncio.Task[None] | None = None
        self._failure_close_task: asyncio.Task[None] | None = None
        self._pre_roll = PreRollBuffer()
        self._splitter = DualFrameSplitter()
        self._normalizer = AudioNormalizer()
        self._asr_queue: asyncio.Queue[bytes | None] | None = None
        self._asr_task: asyncio.Task[None] | None = None
        # `asr_end` 的后台收尾任务：只做 flush 尾帧 + 摘出旧任务，不等离线终稿。
        # 旧 provider 由独立的 watcher 盯到自然结束（见 `_watch_detached_finalize`）。
        self._end_turn_task: asyncio.Task[None] | None = None
        self._audio_track: MediaStreamTrack | None = None
        self._playback_queue: asyncio.Queue[PcmFrame] = asyncio.Queue(maxsize=MAX_PLAYBACK_FRAMES)
        self._playback_track: PlaybackAudioTrack | None = None
        self._playback_turn_id: str | None = None
        self._playback_generation = 0
        # 按句流式（迭代 5 F1）：重采样器与残样跨句子常驻，整轮结束才 flush。
        # 见 `_Stream48kResampler` 的 docstring —— 每句新起一个会把句边界切出咔哒声。
        self._playback_resampler: _Stream48kResampler | None = None
        self._playback_carry = bytearray()
        self._offer_seen = False
        self._pending_ice: list[tuple[str, str | None, int | None]] = []
        self._pending_ice_keys: set[tuple[str, str | None, int | None]] = set()
        self._ice_seen: set[tuple[str, str | None, int | None]] = set()
        self._asr_frames_total = 0
        self._vad_frames_total = 0
        # 本轮上行 PCM（声纹首句判定用；见 `last_utterance_pcm`）。有界，防长会话涨内存。
        self._utterance_pcm = bytearray()
        self._asr_dropped_frames = 0
        # 服务端 VAD 兜底：会话内自建实例（不改 media_bridge 的接线层），失败即 no-op。
        self._vad_session: VadSession | None
        self._vad_session, _vad_error = _build_vad_session(config.vad_aggressiveness)
        self._vad_speech_segments = 0
        self._vad_disabled_reason: str | None = "vad_unavailable" if _vad_error else None
        if self._vad_session is None:
            logger.warning(
                "server vad unavailable, degrade to no-op",
                extra={
                    "trace_id": self.trace_id,
                    "connection_id": self.connection_id,
                    "turn_id": None,
                    "state": self._state.value,
                    "reason": "vad_unavailable",
                    "error_type": _vad_error or "-",
                },
            )

    @property
    def state(self) -> MediaState:
        return self._state

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    @property
    def pending_tasks(self) -> int:
        return len(self._tasks)

    @property
    def peer_connection(self) -> RTCPeerConnection | None:
        return self._pc

    @property
    def asr_frames_received(self) -> int:
        return self._asr_frames_total

    @property
    def vad_frames_received(self) -> int:
        return self._vad_frames_total

    def last_utterance_pcm(self) -> bytes:
        """本轮上行语音的 PCM 快照（16 kHz / mono / s16le），上限 `UTTERANCE_PCM_MAX_BYTES`。

        用途（2026-11-10「D」方案）：登录时不主动索取确认，等老人**第一次开口**
        后用这段真音频跑声纹；命中就直接确认并播个人开场白，命中不了才弹确认层。
        只在内存里保留到本轮结束，不落库、不进日志（AGENTS §4 第 10 条）。
        """
        return bytes(self._utterance_pcm)

    @property
    def asr_frames_dropped(self) -> int:
        return self._asr_dropped_frames

    @property
    def vad_speech_segments(self) -> int:
        """服务端 VAD 判定出的完整语音段数；**只做观测，不驱动轮次**。"""
        return self._vad_speech_segments

    @property
    def vad_available(self) -> bool:
        """本会话的服务端 VAD 是否在工作；False 表示已降级为 no-op。"""
        return self._vad_session is not None

    @property
    def vad_disabled_reason(self) -> str | None:
        """VAD 降级原因；正常工作时为 None。"""
        return self._vad_disabled_reason

    @property
    def playback_generation(self) -> int:
        return self._playback_generation

    @property
    def has_playback_track(self) -> bool:
        """下行播放轨是否已就绪（`create_peer_connection()` 之后为 True）。

        开场白的「等轨」逻辑用它判断能不能写（见 `playback_track_ready_for_device`）。
        只读，不改任何状态。
        """
        return self._playback_track is not None

    @property
    def playback_queue_depth(self) -> int:
        return self._playback_queue.qsize()

    def create_peer_connection(self) -> RTCPeerConnection:
        """创建 aiortc PC；一个连接只允许一个，重复创建是状态错误。"""
        self._ensure_loop()
        self._ensure_open()
        if self._pc is not None:
            raise MediaStateError("peer connection already exists for this connection")

        servers = self.config.ice_servers()
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        # 先挂下行播放轨：浏览器 offer 为 sendrecv 时，answer 才会回传音频 m-line。
        self._playback_track = PlaybackAudioTrack(self._playback_queue)
        self._pc.addTrack(self._playback_track)
        self._install_handlers(self._pc)
        _log_event(
            "peer connection created",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="peer_connection_created",
            ice_server_count=len(servers),
            host_only=not servers,
        )
        return self._pc

    def _install_handlers(self, pc: RTCPeerConnection) -> None:
        """挂 track / connectionstate 处理器；不直接发 WS、不写业务逻辑。"""

        @pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            if track.kind != "audio":
                track.stop()
                return
            if self._state in (MediaState.CLOSING, MediaState.CLOSED):
                track.stop()
                return
            self._audio_track = track
            self.spawn(self._consume_track(track))

        @pc.on("connectionstatechange")
        async def _on_connection_state_change() -> None:
            await self._sync_connection_state(pc.connectionState)

    async def _sync_connection_state(self, connection_state: str) -> None:
        if self._state in (MediaState.CLOSING, MediaState.CLOSED):
            # 关闭过程里 aiortc 还会发 disconnected/closed，不能再起 watchdog。
            _log_event(
                "peer connection state changed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason="connection_state",
                connection_state=connection_state,
            )
            return
        if connection_state == "connected":
            self._state = MediaState.CONNECTED
            self._cancel_ice_watchdog()
        elif connection_state == "disconnected":
            # 先降为 CONNECTING：断线重连计时未恢复前，watchdog 必须能判失败。
            self._state = MediaState.CONNECTING
            self._start_ice_watchdog("disconnected")
        elif connection_state == "failed":
            await self._fail_media(MediaFailureReason.ICE_FAILED, "connection_failed")
            return
        elif connection_state == "closed" and self._state not in (
            MediaState.CLOSING,
            MediaState.CLOSED,
        ):
            await self._fail_media(MediaFailureReason.ICE_FAILED, "connection_closed")
            return
        # 不把 "closed" 直接写成 CLOSED：A2 仍需调用 close 才能取消任务/清缓冲；
        # close 自己负责把状态落到 CLOSED。
        _log_event(
            "peer connection state changed",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="connection_state",
            connection_state=connection_state,
        )

    def _start_ice_watchdog(self, phase: str) -> None:
        """启动/重启 ICE 看门狗；connected 后取消，超时→FAILED→自动 close。"""
        self._cancel_ice_watchdog()
        if self._state in (MediaState.CLOSED, MediaState.FAILED):
            return
        if self._state is MediaState.CONNECTED:
            return
        self._ice_timeout_task = self.spawn(self._ice_watchdog(phase))

    async def _ice_watchdog(self, phase: str) -> None:
        try:
            await asyncio.sleep(self.config.ice_timeout_seconds)
        except asyncio.CancelledError:
            raise
        if self._state is MediaState.CONNECTED:
            return
        # 2026-11-10 修正：aiortc 的候选对 `SUCCEEDED` 常比 `pc.connectionState`
        # 变 connected 早 1~4s（日志里两类事件的时间差就是这个）。按 5s 一刀切
        # 会把「马上要通」的会话判死，开场白随即丢失
        # （`media failure reason=ice_timeout` 之后 `identity_greeting_audio
        # played=false track_ready=false`）。所以只要 ICE 侧已有进展，再等一个宽限窗。
        if self._ice_has_progress():
            grace = self.config.ice_grace_seconds
            if grace > 0:
                _log_event(
                    "ice watchdog grace",
                    trace_id=self.trace_id,
                    connection_id=self.connection_id,
                    turn_id=self._turn_id,
                    state=self._state.value,
                    reason="ice_progress",
                    grace_seconds=grace,
                )
                try:
                    await asyncio.sleep(grace)
                except asyncio.CancelledError:
                    raise
                if self._is_connected():
                    return
        await self._fail_media(MediaFailureReason.ICE_TIMEOUT, f"{phase}_timeout")

    def _is_connected(self) -> bool:
        """宽限窗结束后再读一次状态：`await` 期间 `_state` 可能已被别的任务推进。"""
        return self._state is MediaState.CONNECTED

    def _ice_has_progress(self) -> bool:
        """ICE/DTLS 侧是否已经出现过进展（用于给看门狗一次宽限）。

        `iceConnectionState` 在 aiortc 里会先走到 `checking/completed`，而
        `connectionState` 要等 DTLS 才变 `connected`；只认后者的看门狗会误杀。
        """
        pc = self._pc
        if pc is None:
            return False
        ice_state = getattr(pc, "iceConnectionState", None)
        conn_state = getattr(pc, "connectionState", None)
        return ice_state in ("checking", "completed", "connected") or conn_state in (
            "connecting",
            "connected",
        )

    def _cancel_ice_watchdog(self) -> None:
        task = self._ice_timeout_task
        self._ice_timeout_task = None
        if task is None or task.done():
            return
        # watchdog 自身触发失败时不能 cancel 自己，否则 close 的 gather 会自锁。
        if task is not asyncio.current_task():
            task.cancel()

    async def _fail_media(self, reason: MediaFailureReason, detail: str) -> None:
        """结构化失败：FAILED → 回调 A2 → 调度一次 detached close。"""
        if self._state in (MediaState.CLOSING, MediaState.CLOSED, MediaState.FAILED):
            return
        self._state = MediaState.FAILED
        self._cancel_ice_watchdog()
        # 当前失败任务不再登记：A2 可能在回调里 close，本任务不能被 close 取消。
        current = asyncio.current_task()
        if current is not None:
            self._tasks.discard(current)
        _log_event(
            "media failure",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason=reason.value,
            detail=detail,
        )
        await self._notify_failure(reason, detail)
        self._schedule_failure_close(reason.value)

    async def _notify_failure(self, reason: MediaFailureReason, detail: str) -> None:
        callback = self._on_media_failed
        if callback is None:
            return
        try:
            await callback(self.connection_id, reason, detail)
        except Exception as exc:
            _log_event(
                "media failure callback failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason="callback_error",
                error_type=type(exc).__name__,
            )

    def _schedule_failure_close(self, reason: str) -> None:
        if self._failure_close_task is not None and not self._failure_close_task.done():
            return

        async def _close_after_failure() -> None:
            try:
                await self.close(reason=reason)
            except Exception as exc:
                logger.warning(
                    "media close after failure failed",
                    extra={
                        "trace_id": self.trace_id,
                        "connection_id": self.connection_id,
                        "turn_id": self._turn_id,
                        "state": self._state.value,
                        "reason": MediaFailureReason.CLOSED.value,
                        "error_type": type(exc).__name__,
                    },
                )

        # 刻意不放进 _tasks：close() 会取消 _tasks，自等待会自锁。
        self._failure_close_task = self._loop.create_task(_close_after_failure())

    async def handle_offer(self, sdp: str) -> str:
        """A2 传入浏览器 offer；返回 answer SDP。重复 offer 直接拒绝（D4）。"""
        self._ensure_loop()
        self._ensure_open()
        if not isinstance(sdp, str) or not sdp.strip():
            raise MediaFormatError("offer SDP must not be empty")
        if self._offer_seen:
            raise MediaStateError("duplicate offer for this connection")

        pc = self._pc
        if pc is None:
            pc = self.create_peer_connection()

        _log_event(
            "offer received",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="offer_received",
            sdp_length=len(sdp),
            mline_count=sdp.count("m="),
            has_fingerprint="a=fingerprint:" in sdp,
        )
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        except Exception as exc:
            self._state = MediaState.FAILED
            raise MediaFormatError("offer SDP was rejected") from exc
        self._offer_seen = True
        self._state = MediaState.OFFER_RECEIVED

        pending = self._pending_ice
        self._pending_ice = []
        self._pending_ice_keys.clear()
        for candidate, sdp_mid, sdp_mline_index in pending:
            await self._apply_ice_candidate(pc, candidate, sdp_mid, sdp_mline_index)

        try:
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
        except Exception as exc:
            self._state = MediaState.FAILED
            raise MediaFormatError("answer creation failed") from exc

        self._state = MediaState.ANSWER_CREATED
        self._start_ice_watchdog("connect")
        local = pc.localDescription
        if local is None:
            raise MediaStateError("answer SDP is not available")
        _log_event(
            "answer created",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="answer_created",
            sdp_length=len(local.sdp),
            mline_count=local.sdp.count("m="),
        )
        return local.sdp

    async def create_offer(self) -> str:
        """服务端主动外呼：本端当 offerer，返回 offer SDP（迭代 7 · W4）。

        与 `handle_offer` 方向相反：那次是浏览器当 offerer、本端答；这次是本端起意
        通知长者端（用药到点 / 反向外呼播报）。两条路径共用同一个
        `RTCPeerConnection` 与同一份 `_pending_ice`，差别只在谁先
        `setLocalDescription`。

        状态刻意**停在 `OPEN`**：此刻 SDP 只是本地描述，远端还没答，会话没有进入
        `CONNECTING`。`_offer_seen` 置位才是真正的闸门 —— 一条连接只谈一次，第二次
        协商必须新开会话（`MediaStateError`），否则 offer/answer 会串。

        ICE 候选在拿到 answer 之前一律进 `_pending_ice` 缓存（`add_ice_candidate`
        的既有逻辑：远端描述为空就缓存），`accept_answer` 落地后统一补放。
        """
        self._ensure_loop()
        self._ensure_open()
        if self._offer_seen:
            raise MediaStateError("connection already negotiated an offer")

        pc = self._pc
        if pc is None:
            pc = self.create_peer_connection()

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
        except Exception as exc:
            self._state = MediaState.FAILED
            raise MediaFormatError("offer creation failed") from exc

        local = pc.localDescription
        if local is None:
            raise MediaStateError("offer SDP is not available")
        self._offer_seen = True
        _log_event(
            "offer created",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="offer_created",
            sdp_length=len(local.sdp),
            mline_count=local.sdp.count("m="),
            has_fingerprint="a=fingerprint:" in local.sdp,
        )
        return local.sdp

    async def accept_answer(self, sdp: str) -> None:
        """收到长者端对 `create_offer` 的 answer：落到远端描述，进 `CONNECTING`。

        为什么不复用 `handle_offer`：那个函数里 `setRemoteDescription(type="offer")`
        与 `createAnswer()` 是写死的 —— 拿 answer SDP 走它会直接炸在 aiortc 内部，
        错误还会被包装成「offer SDP was rejected」，排查时南辕北辙。
        """
        self._ensure_loop()
        self._ensure_open()
        if not isinstance(sdp, str) or not sdp.strip():
            raise MediaFormatError("answer SDP must not be empty")
        pc = self._pc
        if pc is None or not self._offer_seen:
            raise MediaStateError("no local offer to accept an answer for")

        _log_event(
            "answer received",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="answer_received",
            sdp_length=len(sdp),
            mline_count=sdp.count("m="),
            has_fingerprint="a=fingerprint:" in sdp,
        )
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        except Exception as exc:
            self._state = MediaState.FAILED
            raise MediaFormatError("answer SDP was rejected") from exc

        pending = self._pending_ice
        self._pending_ice = []
        self._pending_ice_keys.clear()
        for candidate, sdp_mid, sdp_mline_index in pending:
            await self._apply_ice_candidate(pc, candidate, sdp_mid, sdp_mline_index)

        self._state = MediaState.CONNECTING
        self._start_ice_watchdog("connect")
        _log_event(
            "answer applied",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="answer_applied",
            replayed_candidates=len(pending),
        )

    async def add_ice_candidate(
        self,
        candidate: str,
        sdp_mid: str | None = None,
        sdp_mline_index: int | None = None,
    ) -> None:
        """A2 传入浏览器 ICE candidate；兼容 C2 的 JSON 字符串与原始 SDP。

        offer 前到达则缓存，重复 candidate 忽略。JSON 内字段只在显式参数为空时生效。
        """
        self._ensure_loop()
        self._ensure_open()
        candidate, sdp_mid, sdp_mline_index = _normalize_ice_candidate(
            candidate, sdp_mid, sdp_mline_index
        )
        pc = self._pc
        if pc is None or not self._offer_seen or pc.remoteDescription is None:
            key = (candidate, sdp_mid, sdp_mline_index)
            if key in self._pending_ice_keys:
                return
            self._pending_ice_keys.add(key)
            self._pending_ice.append((candidate, sdp_mid, sdp_mline_index))
            _log_event(
                "ice candidate cached",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason="ice_candidate_cached",
                pending_candidates=len(self._pending_ice),
            )
            return
        await self._apply_ice_candidate(pc, candidate, sdp_mid, sdp_mline_index)

    async def _apply_ice_candidate(
        self,
        pc: RTCPeerConnection,
        candidate: str,
        sdp_mid: str | None,
        sdp_mline_index: int | None,
    ) -> None:
        """解析并添加一个 candidate；无效/缺 mid 只记录，不让媒体会话崩溃。"""
        key = (candidate, sdp_mid, sdp_mline_index)
        if key in self._ice_seen:
            return
        self._ice_seen.add(key)

        raw = candidate.strip()
        if not raw:
            await pc.addIceCandidate(None)
            _log_event(
                "ice candidates complete",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason="ice_candidates_complete",
            )
            return
        if sdp_mid is None and sdp_mline_index is None:
            _log_event(
                "ice candidate rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.FORMAT_ERROR.value,
                error_type="missing_sdp_mid",
            )
            return
        if raw.startswith("candidate:"):
            raw = raw[len("candidate:") :]
        try:
            parsed = candidate_from_sdp(raw)
        except (AssertionError, ValueError):
            _log_event(
                "ice candidate rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.FORMAT_ERROR.value,
                error_type="invalid_candidate_sdp",
            )
            return

        parsed.sdpMid = sdp_mid
        parsed.sdpMLineIndex = sdp_mline_index
        try:
            await pc.addIceCandidate(parsed)
        except Exception as exc:
            _log_event(
                "ice candidate rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.FORMAT_ERROR.value,
                error_type=type(exc).__name__,
            )
            return
        _log_event(
            "ice candidate added",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="ice_candidate_added",
        )

    async def begin_turn(self) -> str:
        """开始新 ASR turn：生成 turn_id，先补 600 ms preRoll，再持续喂 60 ms 帧。

        上一轮的离线终稿可能还在跑（FunASR 2pass 实测 5.55s 音频要等约 17s），
        所以这里**不等待、也不取消**它：旧任务已被 `end_turn` 摘出会话槽位，
        继续在自己的队列上跑；它的迟到事件由 `_emit_asr_event` 按 turn_id 丢弃。
        信令回环因此只付 flush 尾帧的成本，连续说话不会再丢新一句的开头帧。
        """
        self._ensure_loop()
        self._ensure_open()
        self._cancel_turn(reason="turn_replaced")
        # 上一 turn 的 <60 ms 残片不再跨 turn 复用；preRoll 仍保留最近 600 ms。
        self._splitter.reset()

        turn_id = uuid.uuid4().hex
        self._turn_id = turn_id
        self._playback_turn_id = turn_id
        # 新一轮开始：清掉上一轮的上行快照（声纹只认「本轮」的音频）。
        self._utterance_pcm.clear()
        # 新一轮开始：上一轮按句流式残留的重采样器状态与残样必须丢掉，
        # 否则新回复的第一帧会接在上一句没播完的尾巴上。
        self._reset_playback_stream()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=MAX_ASR_QUEUE_FRAMES)
        self._asr_queue = queue
        pre_roll_frames = self._pre_roll.recent()
        for frame in pre_roll_frames:
            queue.put_nowait(frame.data)

        provider = self._asr_provider or build_asr()
        self._asr_provider = provider
        self._asr_task = self.spawn(self._consume_asr(turn_id, provider, queue))
        _log_event(
            "asr turn started",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=turn_id,
            state=self._state.value,
            reason="turn_started",
            pre_roll_frames=len(pre_roll_frames),
            pre_roll_ms=sum(frame.duration_ms for frame in pre_roll_frames),
        )
        return turn_id

    async def _watch_detached_finalize(self, task: asyncio.Task[None], turn_id: str) -> None:
        """盯住已摘出会话槽位的旧 ASR 任务直到它自己收口。

        离线终稿要等 FunASR 重跑整段音频（实测 5.55s 音频约 17s），不能拿信令
        回环去等。这里只负责在正常结束时记一条日志；会话关闭时 watcher 被取消，
        顺手取消被盯的 provider 任务，避免遗留连接。
        """
        started = time.perf_counter()
        try:
            await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
        except Exception as exc:
            _log_event(
                "detached asr finalize failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="finalize_failed",
                error_type=type(exc).__name__,
            )
            return
        _log_event(
            "detached asr finalize done",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=turn_id,
            state=self._state.value,
            reason="finalize_done",
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )

    async def end_turn(self, turn_id: str | None = None) -> None:
        """结束当前 ASR turn：flush 20/60 ms 尾帧、发哨兵、等 provider 自然收口。"""
        self._ensure_loop()
        current = self._turn_id
        if current is None:
            return
        if turn_id is not None and turn_id != current:
            _log_event(
                "stale end_turn ignored",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="stale_turn",
            )
            return

        queue = self._asr_queue
        task = self._asr_task
        # 尾帧必须在 turn_id 仍有效时进入队列；否则 _push_asr_frame 会直接丢弃。
        for canonical in self._normalizer.flush():
            asr_frames, vad_frames = self._splitter.feed(canonical)
            self._dispatch_frames(asr_frames, vad_frames)
        asr_tail, vad_tail = self._splitter.flush()
        self._dispatch_frames(asr_tail, vad_tail)

        # 哨兵只投一次且**不阻塞**：队列满说明 provider 已经消费不过来，
        # 继续 await 会把「新一句开口」也一起堵住；随后由下面的有界等待/取消收口。
        if queue is not None and task is not None and not task.done():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                _log_event(
                    "asr turn finalize queue full",
                    trace_id=self.trace_id,
                    connection_id=self.connection_id,
                    turn_id=current,
                    state=self._state.value,
                    reason="queue_full",
                )

        # 只清理「仍属于本 turn」的槽位。`begin_turn` 可能已经开始下一轮并替换了
        # `_turn_id/_asr_queue/_asr_task`，这里绝不能把新轮的状态误清成 None。
        if self._turn_id == current:
            self._turn_id = None
        if self._asr_queue is queue:
            self._asr_queue = None
        if self._asr_task is task:
            self._asr_task = None

        if task is not None:
            # **不在信令路径上等离线终稿**，也不在 1.5s 后取消它：FunASR 2pass 的
            # offline 结果要重跑整段音频（实测 5.55s 音频约 17s），提前取消等于每轮
            # 都只有 partial、永远拿不到 asr_final。旧任务继续跑，watcher 只负责收尾
            # 日志；会话关闭时 watcher 被取消并顺手取消 provider。
            self.spawn(self._watch_detached_finalize(task, current))

        _log_event(
            "asr turn ended",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=current,
            state=self._state.value,
            reason="turn_ended",
        )

    async def enqueue_system_tts_pcm(self, pcm: bytes, *, generation: int | None = None) -> bool:
        """**系统播报**入口（迭代 4 收口 W5 · G-D13）：不要求 `turn_id`，按播放代际校验。

        ## 为什么需要第二个入口

        `enqueue_tts_pcm` 的 `turn_id` 校验承担的是「用户一开口，上一句立刻作废」，
        这是打断语义，不能去掉。但开场白这类**系统主动播报**发生在用户说话之前：
        此刻 `_playback_turn_id` 还是 `None`（它只在 `begin_turn()` 里赋值），
        于是同一段音频会被 `:1252` 无条件拒绝。两条需求撞在同一处校验上。

        **换校验依据，不取消校验**：`_playback_generation` 本来就是代际号
        （`barge_in()` `:1300` 加一、`close()` `:1617` 加一），只是先前只进日志没人读它。
        调用方在**开始合成之前**取一次 `playback_generation`，入队时比对：
        期间用户打断过（代际变了）就丢弃 —— 保护力度与 turn 校验等价，
        只是把「哪一轮」换成了「哪一代」。

        ## 返回值而不是抛异常

        `enqueue_tts_pcm` 抛异常是因为它服务于「一轮对话」，失败需要上报到那一轮；
        系统播报没有「那一轮」可上报，且开场白晚到或被打断都是**正常情形**，
        所以这里返回 `bool`：`True` 已入队，`False` 已丢弃（原因写进日志与 metrics）。

        ## 哪些状态算「还能播」（2026-11-10 N62f 缺陷 B）

        这里曾经要求 `self._state is MediaState.OPEN`，把 `CONNECTING`/`CONNECTED`
        也一并拒了。但服务端外呼的时序恰好相反：`create_offer()` 之后状态仍是 OPEN，
        `accept_answer()` 才推进到 CONNECTING，等 ICE 通了是 CONNECTED —— 也就是说
        **音频真正该播的那一刻永远不会是 OPEN**，`play_system_tts_for_device()` 的开场白
        同样卡在这里，表现为「连上了但没人说话」。

        口径与 `_ensure_open()`（`:2180`）对齐：只拒已关/失败（`CLOSING`/`CLOSED`/`FAILED`），
        `OPEN`/`CONNECTING`/`CONNECTED` 都放行；轨是否建好仍由下面单独判断。

        ## 轨还没建好也**照收**（2026-11-10 修正）

        媒体连接建立之前 `_playback_track` 是 `None`；这是 `auth_ok` 之后、`offer`
        之前那段窗口的常态，而且**经常长达数秒**（页面首次加载、ICE 拉取慢）。此前这里
        直接返回 `False`，调用方只好把开场白丢掉 —— 实测 9 次登录丢 4 次，就是「开场白
        经常没声音」。队列与会话同寿命，建连时用同一个队列造轨，所以先入队就等于
        「连通那一刻开口」，既不丢也不抢在用户说话之前（代际校验仍在）。
        """
        self._ensure_loop()
        if self._state in (MediaState.CLOSING, MediaState.CLOSED, MediaState.FAILED):
            # 与 `_ensure_open()` 同口径：只有「已经关了」才拒，CONNECTING/CONNECTED 照播。
            _log_event(
                "system tts rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=None,
                state=self._state.value,
                reason="session_closed",
            )
            return False
        if self._playback_track is None:
            # 轨还没建好（`auth_ok` 之后、`offer` 之前，实测可达数秒）也照收：
            # 播放队列在 `__init__` 就建好、与会话同寿命，`create_peer_connection()`
            # 用的是**同一个队列**造的 `PlaybackAudioTrack`，所以先入队的音频会在建连
            # 那一刻自然播出去。反过来「轨没好就丢」正是「开场白经常没声音」的根因
            # （2026-11-10 实测：9 次登录 4 次丢，且丢掉的那几次服务端连 offer 都没收到）。
            # 会话若最终没建起来（页面关了/没发 offer），`close()` 会 `_drain_playback_queue`
            # 把这段音频一起清掉，不会留到下一次会话里突然开口。
            _log_event(
                "system tts queued before track",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=None,
                state=self._state.value,
                reason="track_pending",
                generation=generation,
            )

        # 代际校验：调用方在**开始合成前**取过一次代际号；期间用户打断（代际变了）
        # 就丢弃。`None` 表示调用方没取（内部调用/测试），按当前代际放行。
        if generation is not None and generation != self._playback_generation:
            _log_event(
                "system tts rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=None,
                state=self._state.value,
                reason="stale_generation",
                expected=generation,
                current=self._playback_generation,
            )
            return False

        try:
            validate_downlink_pcm(pcm)
        except MediaError as exc:
            # 注意是 `MediaError` 而不是 `MediaStateError`：`validate_downlink_pcm`
            # 抛的是 `MediaFormatError`（兄弟类），只捕前者会让一个坏 PCM 直接冒泡成
            # 500 —— 系统播报的错误一律降级成「这次没播」，不上抛。见
            # `tests/test_dialogue_runtime.py::TestSystemTtsEnqueue::test_invalid_pcm_returns_false`。
            _log_event(
                "system tts rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=None,
                state=self._state.value,
                reason="invalid_pcm",
                detail=str(exc),
            )
            return False

        frames = _tts_pcm_to_48k_frames(pcm)
        queue = self._playback_queue
        if queue.qsize() + len(frames) > MAX_PLAYBACK_FRAMES:
            _log_event(
                "system tts backpressure",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=None,
                state=self._state.value,
                reason=MediaFailureReason.BACKPRESSURE.value,
                queued_frames=queue.qsize(),
                incoming_frames=len(frames),
            )
            return False

        for frame in frames:
            queue.put_nowait(frame)
        _log_event(
            "system tts enqueued",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=None,
            state=self._state.value,
            reason="system_tts_enqueued",
            frames=len(frames),
            duration_ms=sum(frame.duration_ms for frame in frames),
            queue_depth=queue.qsize(),
            generation=self._playback_generation,
        )
        return True

    async def enqueue_tts_pcm(self, turn_id: str, pcm: bytes) -> None:
        """TTS 16 kHz raw PCM → 48 kHz/20 ms 帧入有界队列；旧 turn 与超限整段拒绝。"""
        self._ensure_loop()
        self._ensure_open()
        if self._playback_track is None:
            raise MediaStateError("peer connection is not created")

        if self._playback_turn_id is None or turn_id != self._playback_turn_id:
            _log_event(
                "tts pcm rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="stale_turn",
            )
            raise MediaStateError("stale turn for TTS playback")

        validate_downlink_pcm(pcm)
        frames = _tts_pcm_to_48k_frames(pcm)
        queue = self._playback_queue
        if queue.qsize() + len(frames) > MAX_PLAYBACK_FRAMES:
            _log_event(
                "tts backpressure",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason=MediaFailureReason.BACKPRESSURE.value,
                queued_frames=queue.qsize(),
                incoming_frames=len(frames),
            )
            await self._notify_failure(MediaFailureReason.BACKPRESSURE, "playback_queue_full")
            raise MediaBackpressureError("TTS playback queue is full")

        for frame in frames:
            queue.put_nowait(frame)
        _log_event(
            "tts pcm enqueued",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=turn_id,
            state=self._state.value,
            reason="tts_enqueued",
            frames=len(frames),
            duration_ms=sum(frame.duration_ms for frame in frames),
            queue_depth=queue.qsize(),
        )

    async def enqueue_tts_pcm_chunk_placed(self, turn_id: str, pcm: bytes) -> ChunkPlacement:
        """**按句流式**写轨：一片 PCM 进来，只把凑满的 20 ms 帧入队（迭代 5 F1）。

        与 `enqueue_tts_pcm` 的区别只有一处：**不 flush 重采样器**。不足一帧的
        残样留在 `self._playback_carry`，等下一句的字节接上；整轮结束时调用
        `flush_tts_pcm()` 把尾巴补齐并复位。

        ## 为什么必须这样

        `enqueue_tts_pcm` 走的是 `_tts_pcm_to_48k_frames`，它对**整段**输入是正确
        的，但按句逐次调用会把每句末尾的残样丢掉并重置重采样器历史 —— 听感上是
        句与句之间的咔哒声。实测「今天天气不错。适合出去走走！」两句分开处理时，
        第一句末尾丢 3~8 ms、第二句开头补零 3~8 ms。

        ## 返回值

        `ChunkPlacement(frames, start_frame)`。`frames == 0` 是正常情形（这一片还不够
        一帧），**不是错误**；此时 `start_frame` 无意义。背压照旧抛
        `MediaBackpressureError`：按句写轨时队列压力比整段更大，必须让调用方知道。

        `start_frame` 必须在**入队的同一同步段内**取（见下方注释），跨 await 取会被
        `recv()` 抢跑，坐标就错一帧。

        ## turn 校验不变

        与 `enqueue_tts_pcm` 完全一致 —— 用户一开口，`barge_in()` 把
        `_playback_turn_id` 置 `None`，后续每一句都会被这里拒掉。这正是打断语义。
        """
        self._ensure_loop()
        self._ensure_open()
        if self._playback_track is None:
            raise MediaStateError("peer connection is not created")

        if self._playback_turn_id is None or turn_id != self._playback_turn_id:
            _log_event(
                "tts pcm chunk rejected",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="stale_turn",
            )
            raise MediaStateError("stale turn for TTS playback")

        validate_downlink_pcm(pcm)
        frames = self._stream_pcm_to_frames(pcm)
        if not frames:
            return ChunkPlacement(frames=0, start_frame=0)

        queue = self._playback_queue
        if queue.qsize() + len(frames) > MAX_PLAYBACK_FRAMES:
            _log_event(
                "tts backpressure",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason=MediaFailureReason.BACKPRESSURE.value,
                queued_frames=queue.qsize(),
                incoming_frames=len(frames),
            )
            await self._notify_failure(MediaFailureReason.BACKPRESSURE, "playback_queue_full")
            raise MediaBackpressureError("TTS playback queue is full")

        # 落位 = 已出帧数（含静音）+ 队列里排在前面的帧数。从这里到 put_nowait 之间
        # **没有 await**，所以 recv() 不可能插进来改这两个数；写成两次调用就会错帧。
        start_frame = self._playback_track.played_frames + queue.qsize()
        for frame in frames:
            queue.put_nowait(frame)
        return ChunkPlacement(frames=len(frames), start_frame=start_frame)

    async def flush_tts_pcm(self, turn_id: str) -> int:
        """收尾：冲出重采样器残样 + 给不足一帧的 carry 补零，然后复位流式状态。

        整轮合成正常结束时由调用方（`dialogue_runtime`）调一次。turn 已经过期
        时**不做任何事**直接返回 0 —— 用户已经打断，尾巴没必要再播。
        """
        self._ensure_loop()
        if self._state is not MediaState.OPEN or self._playback_track is None:
            self._reset_playback_stream()
            return 0
        if self._playback_turn_id is None or turn_id != self._playback_turn_id:
            self._reset_playback_stream()
            return 0

        tail = bytearray()
        if self._playback_resampler is not None:
            tail.extend(self._playback_resampler.flush())
        tail.extend(self._playback_carry)

        enqueued = 0
        if tail:
            remainder = len(tail) % DOWNLINK_FRAME_BYTES
            if remainder:
                tail.extend(b"\x00" * (DOWNLINK_FRAME_BYTES - remainder))
            queue = self._playback_queue
            whole = len(tail) // DOWNLINK_FRAME_BYTES
            if queue.qsize() + whole <= MAX_PLAYBACK_FRAMES:
                for start in range(0, len(tail), DOWNLINK_FRAME_BYTES):
                    queue.put_nowait(
                        PcmFrame(
                            data=bytes(tail[start : start + DOWNLINK_FRAME_BYTES]),
                            sample_rate=DOWNLINK_SAMPLE_RATE,
                        )
                    )
                enqueued = whole
        self._reset_playback_stream()
        _log_event(
            "tts pcm flushed",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=turn_id,
            state=self._state.value,
            reason="sentence_stream_end",
            frames=enqueued,
            queue_depth=self._playback_queue.qsize(),
        )
        return enqueued

    def _stream_pcm_to_frames(self, pcm: bytes) -> list[PcmFrame]:
        """16 kHz 片 → 48 kHz 完整 20 ms 帧；不足一帧的进 `_playback_carry`。"""
        if self._playback_resampler is None:
            self._playback_resampler = _Stream48kResampler(DOWNLINK_SAMPLE_RATE)
        raw = self._playback_resampler.feed(pcm, TARGET_SAMPLE_RATE)
        if raw:
            self._playback_carry.extend(raw)
        whole = len(self._playback_carry) // DOWNLINK_FRAME_BYTES
        frames: list[PcmFrame] = []
        for index in range(whole):
            start = index * DOWNLINK_FRAME_BYTES
            frames.append(
                PcmFrame(
                    data=bytes(self._playback_carry[start : start + DOWNLINK_FRAME_BYTES]),
                    sample_rate=DOWNLINK_SAMPLE_RATE,
                )
            )
        if whole:
            del self._playback_carry[: whole * DOWNLINK_FRAME_BYTES]
        return frames

    def _reset_playback_stream(self) -> None:
        """丢掉重采样器与残样；`barge_in` / `close` / 新 turn 都要调。"""
        self._playback_resampler = None
        self._playback_carry.clear()

    def barge_in(self) -> None:
        """打断下行播放：递增播放代际、清空队列、旧 turn 后续入队全部拒绝。"""
        self._ensure_loop()
        self._ensure_open()
        old_turn = self._playback_turn_id
        self._playback_generation += 1
        cleared = self._drain_playback_queue()
        self._playback_turn_id = None
        # 按句流式：打断时残样与重采样器状态也一起丢 —— 留着会让下一轮的
        # 第一帧接上一轮尾巴，听感上就是「上一句没说完又续了半句」。
        self._reset_playback_stream()
        _log_event(
            "playback barged",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=old_turn,
            state=self._state.value,
            reason="barge_in",
            cleared_frames=cleared,
            generation=self._playback_generation,
        )

    def _drain_playback_queue(self) -> int:
        cleared = 0
        while True:
            try:
                self._playback_queue.get_nowait()
            except asyncio.QueueEmpty:
                return cleared
            cleared += 1

    def _cancel_turn(self, *, reason: str) -> None:
        old_turn = self._turn_id
        if self._asr_task is not None:
            self._asr_task.cancel()
            self._asr_task = None
        if self._asr_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._asr_queue.put_nowait(None)
            self._asr_queue = None
        self._turn_id = None
        if old_turn is not None:
            _log_event(
                "asr turn cancelled",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=old_turn,
                state=self._state.value,
                reason=reason,
            )

    async def _consume_asr(
        self,
        turn_id: str,
        provider: ASRProvider,
        queue: asyncio.Queue[bytes | None],
    ) -> None:
        try:
            try:
                # ASR 档超时（迭代 5 §4.2「首包 2s」）：`stream()` 本身要建连/握手
                # （FunASR 的 `_connect` 就在里面 await），这一段也必须算进首包预算。
                stream_result = await with_timeout(
                    provider.stream(_asr_frame_iterator(queue)), stage=STAGE_ASR
                )
            except ASRError as exc:
                await self._emit_asr_event(
                    turn_id, ASREvent(kind=ASREventKind.ERROR, result=None, error=exc)
                )
                return
            if not stream_result.ok or stream_result.value is None:
                # 连接阶段超时：交给外层 `except ASRError` 统一发 error 帧（含话术）。
                raise ASRUnavailable(
                    f"ASR 连接 {stream_result.timeout_ms}ms 内未就绪（未产出任何帧）"
                )
            events = stream_result.value
            try:
                first = await with_timeout(events.__anext__(), stage=STAGE_ASR)
            except StopAsyncIteration:
                first = None
            first_event = first.value if first is not None else None
            if first is not None and (not first.ok or first_event is None):
                aclose = getattr(events, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
                raise ASRUnavailable(f"ASR 首包 {first.timeout_ms}ms 内没有结果（已连接但未产出）")
            if first_event is not None:
                await self._emit_asr_event(turn_id, first_event)
                if first_event.kind is not ASREventKind.PARTIAL:
                    return
            async for event in events:
                await self._emit_asr_event(turn_id, event)
        except asyncio.CancelledError:
            raise
        except ASRError as exc:
            await self._emit_asr_event(
                turn_id, ASREvent(kind=ASREventKind.ERROR, result=None, error=exc)
            )
        except Exception as exc:
            _log_event(
                "asr task failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TASK_EXCEPTION.value,
                error_type=type(exc).__name__,
            )
            await self._notify_failure(MediaFailureReason.TASK_EXCEPTION, "asr_task_error")
        finally:
            _log_event(
                "asr turn finished",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="turn_finished",
            )

    async def _emit_asr_event(self, turn_id: str, event: ASREvent) -> None:
        # 不打印转写文本；只记录长度、is_final 与 confidence（允许 None）。
        if event.error is not None:
            extra: dict[str, object] = {
                "error_type": type(event.error).__name__,
                "text_len": 0,
                "confidence": None,
            }
        elif event.result is not None:
            extra = {
                "is_final": event.result.is_final,
                "text_len": len(event.result.text),
                "confidence": event.result.confidence,
            }
        else:
            extra = {"text_len": 0, "confidence": None}
        _log_event(
            "asr event",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=turn_id,
            state=self._state.value,
            reason=event.kind.value,
            **extra,
        )

        # 旧轮的离线终稿可能在下一轮开口之后才到：`_turn_id` 已指向新轮时必须丢掉，
        # 否则会用旧 turn_id 触发对话与 TTS，播放侧再按 stale_turn 拒绝，白跑一轮。
        if self._turn_id is not None and self._turn_id != turn_id:
            _log_event(
                "stale asr event dropped",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="stale_turn",
                event_kind=event.kind.value,
            )
            return

        if self._on_asr_event is None:
            return
        try:
            await self._on_asr_event(turn_id, event)
        except Exception as exc:
            _log_event(
                "asr event callback failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="callback_error",
                error_type=type(exc).__name__,
            )

    async def _consume_track(self, track: MediaStreamTrack) -> None:
        try:
            while True:
                frame = await track.recv()
                if not isinstance(frame, av.AudioFrame):
                    raise MediaFormatError("audio track returned a non-audio frame")
                self._process_uplink_frame(frame)
        except MediaStreamError:
            self._flush_uplink_tail()
            _log_event(
                "audio track ended",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TRACK_ENDED.value,
            )
            await self._fail_media(MediaFailureReason.TRACK_ENDED, "audio_track_ended")
        except asyncio.CancelledError:
            raise
        except MediaFormatError as exc:
            _log_event(
                "audio format error",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.FORMAT_ERROR.value,
                error_type=type(exc).__name__,
            )
            await self._notify_failure(MediaFailureReason.FORMAT_ERROR, "audio_format_error")
        except Exception as exc:
            _log_event(
                "audio track failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TASK_EXCEPTION.value,
                error_type=type(exc).__name__,
            )
            await self._notify_failure(MediaFailureReason.TASK_EXCEPTION, "audio_track_task_error")

    def _process_uplink_frame(self, frame: av.AudioFrame) -> None:
        for canonical in self._normalizer.feed(frame):
            asr_frames, vad_frames = self._splitter.feed(canonical)
            self._dispatch_frames(asr_frames, vad_frames)

    def _flush_uplink_tail(self) -> None:
        for canonical in self._normalizer.flush():
            asr_frames, vad_frames = self._splitter.feed(canonical)
            self._dispatch_frames(asr_frames, vad_frames)
        asr_frames, vad_frames = self._splitter.flush()
        self._dispatch_frames(asr_frames, vad_frames)

    def _dispatch_frames(self, asr_frames: list[PcmFrame], vad_frames: list[PcmFrame]) -> None:
        self._asr_frames_total += len(asr_frames)
        self._vad_frames_total += len(vad_frames)
        # 旁路：VAD 只消费 30 ms 帧做观测，返回值（语音段字节）**不进 ASR 队列、不改轮次**。
        self._feed_vad_frames(vad_frames)
        for asr in asr_frames:
            self._pre_roll.append(asr)
            self._push_asr_frame(asr.data)

    def _feed_vad_frames(self, vad_frames: list[PcmFrame]) -> None:
        """把 30 ms 帧喂给服务端 VAD；句末只加计数 + 一条日志。

        显式声明：**轮次仍由 A2 的 `asr_start` / `asr_end` 驱动**，本函数不碰
        `_turn_id` / `_asr_queue` / `_pre_roll`，因此不可能改变现有轮次语义。
        任何异常都把本会话 VAD 关掉并降级（`vad_available` → False），主链路继续。
        """
        session = self._vad_session
        if session is None or not vad_frames:
            return
        for frame in vad_frames:
            try:
                segment = session.feed_frame(frame.data)
            except Exception as exc:
                self._disable_vad("vad_error", type(exc).__name__)
                return
            if segment is not None:
                self._record_vad_segment(segment, reason="vad_speech_segment")

    def _record_vad_segment(self, segment: bytes, *, reason: str) -> None:
        """记一个完整语音段：只加计数器与日志，不回灌任何音频。"""
        self._vad_speech_segments += 1
        _log_event(
            "vad speech segment",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason=reason,
            bytes_total=len(segment),
            duration_ms=len(segment) * 1000 // (TARGET_SAMPLE_RATE * SAMPLE_WIDTH_BYTES),
            segments=self._vad_speech_segments,
        )

    def _disable_vad(self, reason: str, error_type: str) -> None:
        """运行期异常时摘掉 VAD（幂等）；会话不受影响。"""
        if self._vad_session is None:
            return
        self._vad_session = None
        self._vad_disabled_reason = reason
        logger.warning(
            "server vad disabled, degrade to no-op",
            extra={
                "trace_id": self.trace_id,
                "connection_id": self.connection_id,
                "turn_id": self._turn_id,
                "state": self._state.value,
                "reason": reason,
                "error_type": error_type,
            },
        )

    def _flush_vad(self, *, reason: str) -> None:
        """会话收尾：把未结束的语音段收口（只计数/日志，**不影响 ASR 队列与轮次**）。"""
        session = self._vad_session
        if session is None:
            return
        try:
            segment = session.flush()
        except Exception as exc:
            # 先不置 None：_disable_vad 靠它判断「是否已摘掉」并负责打日志
            self._disable_vad("vad_error", type(exc).__name__)
            return
        self._vad_session = None
        if segment is not None:
            self._record_vad_segment(segment, reason=reason)

    def _push_asr_frame(self, data: bytes) -> None:
        queue = self._asr_queue
        if queue is None or self._turn_id is None:
            return
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            self._asr_dropped_frames += 1
            if self._asr_dropped_frames == 1 or self._asr_dropped_frames % 10 == 0:
                _log_event(
                    "asr frame dropped",
                    trace_id=self.trace_id,
                    connection_id=self.connection_id,
                    turn_id=self._turn_id,
                    state=self._state.value,
                    reason=MediaFailureReason.BACKPRESSURE.value,
                    dropped_frames=self._asr_dropped_frames,
                )
            return
        # 声纹首句判定用的快照：只在内存里留到本轮结束，**不落库、不进日志**。
        if len(self._utterance_pcm) < UTTERANCE_PCM_MAX_BYTES:
            self._utterance_pcm.extend(data)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """把媒体任务绑定到创建时的 loop，并登记到会话；关闭时统一取消。"""
        self._ensure_loop()
        self._ensure_open()
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def close(self, *, reason: str = "closed") -> None:
        """幂等关闭：CLOSING → 取消任务 → 关 PC → CLOSED。重复调用直接返回。"""
        self._ensure_loop()
        if self._state in (MediaState.CLOSING, MediaState.CLOSED):
            return

        self._state = MediaState.CLOSING
        self._cancel_ice_watchdog()
        _log_event(
            "media session closing",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason=reason,
            pending_tasks=len(self._tasks),
        )

        self._cancel_turn(reason="closed")
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

        self._normalizer.reset()
        self._splitter.reset()
        # 收尾：把截到会话末尾的语音段收口（只计数/日志，轮次早已由 asr_end 结束）。
        self._flush_vad(reason="vad_speech_segment_closed")
        self._pre_roll.clear()
        self._playback_turn_id = None
        self._playback_generation += 1
        self._drain_playback_queue()
        self._reset_playback_stream()
        if self._audio_track is not None:
            self._audio_track.stop()
            self._audio_track = None
        if self._playback_track is not None:
            self._playback_track.stop()
            self._playback_track = None
        self._asr_provider = None

        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception as exc:  # 关闭必须继续，不能留下半关闭会话
                logger.warning(
                    "peer connection close failed",
                    extra={
                        "trace_id": self.trace_id,
                        "connection_id": self.connection_id,
                        "turn_id": self._turn_id,
                        "state": self._state.value,
                        "reason": MediaFailureReason.CLOSED.value,
                        "error_type": type(exc).__name__,
                    },
                )
        self._pc = None
        self._state = MediaState.CLOSED
        _log_event(
            "media session closed",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason=reason,
            pending_tasks=len(self._tasks),
        )

    def _ensure_loop(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise MediaLoopMismatch("media call requires a running event loop") from exc
        if running is not self._loop:
            raise MediaLoopMismatch(
                f"media session {self.connection_id} belongs to another event loop"
            )

    def _ensure_open(self) -> None:
        if self._state in (MediaState.CLOSING, MediaState.CLOSED, MediaState.FAILED):
            raise MediaClosedError(f"media session {self.connection_id} is closed")

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _log_event(
                "media task failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TASK_EXCEPTION.value,
                error_type=type(error).__name__,
            )


class MediaSessionRegistry:
    """`connection_id` → `MediaSession` 的 registry；不提供模块级单例。

    调用方（A2 接线层）应持有实例并在同一 running loop 内使用；跨 loop 调用明确失败。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, MediaSession] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    @property
    def pending_tasks(self) -> int:
        return sum(session.pending_tasks for session in self._sessions.values())

    def create_session(
        self,
        connection_id: str,
        trace_id: str,
        *,
        config: MediaConfig | None = None,
        asr_provider: ASRProvider | None = None,
        on_asr_event: AsrEventCallback | None = None,
        on_media_failed: MediaFailureCallback | None = None,
    ) -> MediaSession:
        """建立会话占位；不创建 PC、不做鉴权、不解析 WS。"""
        self._ensure_loop()
        if connection_id in self._sessions:
            raise MediaStateError(f"media session {connection_id} already exists")

        media_config = config or MediaConfig.from_settings(get_settings())
        session = MediaSession(
            connection_id,
            trace_id,
            media_config,
            asr_provider=asr_provider,
            on_asr_event=on_asr_event,
            on_media_failed=on_media_failed,
        )
        self._sessions[connection_id] = session
        _log_event(
            "media session created",
            trace_id=session.trace_id,
            connection_id=connection_id,
            turn_id=None,
            state=session.state.value,
            reason="session_created",
        )
        return session

    def get_session(self, connection_id: str) -> MediaSession | None:
        return self._sessions.get(connection_id)

    async def close_session(self, connection_id: str, *, reason: str = "closed") -> bool:
        """关闭并移除一个会话；未知 connection_id 视为已关闭，返回 False。"""
        self._ensure_loop()
        session = self._sessions.pop(connection_id, None)
        if session is None:
            return False
        await session.close(reason=reason)
        return True

    async def close_all(self, *, reason: str = "shutdown") -> None:
        """关闭全部会话；异常不阻断其余会话的清理。"""
        self._ensure_loop()
        sessions = list(self._sessions.values())
        self._sessions.clear()
        if sessions:
            await asyncio.gather(
                *(session.close(reason=reason) for session in sessions),
                return_exceptions=True,
            )

    def _ensure_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise MediaLoopMismatch("media session registry requires a running loop") from exc
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            raise MediaLoopMismatch("media session registry belongs to another event loop")


# ------------------------------------------------------------------ A2 信令接线
# A2 唯一公开面：set_media_handler(handler) / deliver / deliver_asr_event。
# 本层不触碰 WebSocket 实例、不解析 WS、不写业务回复。

_SIGNALING_REGISTRIES: WeakKeyDictionary[asyncio.AbstractEventLoop, MediaSessionRegistry] = (
    WeakKeyDictionary()
)
_INTERNAL_ERROR_MESSAGE = "出了点小状况，请再说一遍。"


def _signaling_registry() -> MediaSessionRegistry:
    """每个事件循环独立 registry；测试多 loop 与生产单 loop 都不串会话。"""
    loop = asyncio.get_running_loop()
    registry = _SIGNALING_REGISTRIES.get(loop)
    if registry is None:
        registry = MediaSessionRegistry()
        _SIGNALING_REGISTRIES[loop] = registry
    return registry


def install_signaling_handler() -> None:
    """把 A3 handler 注册到 A2；应用启动时调用一次，重复调用幂等。"""
    from app.audio import signaling

    signaling.set_media_handler(handle_signal_message)


def uninstall_signaling_handler() -> None:
    """注销 A2 handler（测试/关闭用）；不影响 A2 自身连接。"""
    from app.audio import signaling

    signaling.set_media_handler(None)


def _deliver(device_id: str, message: Mapping[str, Any]) -> bool:
    from app.audio import signaling

    return signaling.deliver(device_id, message)


def _deliver_error(device_id: str) -> None:
    _deliver(
        device_id,
        {"type": "error", "code": "internal", "message": _INTERNAL_ERROR_MESSAGE},
    )


# --------------------------------------------------- 迭代 7 · 反向外呼的 answer 等待位
# 服务端当 offerer 时，`outbound.Offerer.wait_answer` 需要一个「上行 answer 到了」的信号。
# 与 registry 一样按事件循环隔离：测试多 loop 与生产单 loop 都不串设备。
_ANSWER_WAITERS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Future[bool]]] = (
    WeakKeyDictionary()
)


def _answer_waiters() -> dict[str, asyncio.Future[bool]]:
    loop = asyncio.get_running_loop()
    waiters = _ANSWER_WAITERS.get(loop)
    if waiters is None:
        waiters = {}
        _ANSWER_WAITERS[loop] = waiters
    return waiters


def _resolve_answer_waiter(device_id: str, answered: bool) -> bool:
    """把 answer 结果交给正在等它的外呼；没有等待者（或已超时取消）返回 False。"""
    waiter = _answer_waiters().get(device_id)
    if waiter is None or waiter.done():
        return False
    waiter.set_result(answered)
    return True


async def enqueue_tts_chunk_placed_for_device(
    device_id: str, turn_id: str, pcm: bytes
) -> ChunkPlacement:
    """**按句流式**写轨的 device_id 入口（迭代 5 F1），返回播放流落位。

    与 `enqueue_tts_for_device` 的区别：不 flush 重采样器，返回
    `ChunkPlacement(frames, start_frame)`（`frames == 0` = 这一片还不够一帧，属正常
    情形）。整轮结束时调用方必须调 `flush_tts_for_device` 一次，否则最后一句的尾巴
    会留在缓冲里不出来。字幕时间轴靠 `start_frame` 换算，不能只拿帧数累加。

    会话不在 / turn 过期 / 背压一律返回 `ChunkPlacement(0, 0)` 并记一条日志 —— 与
    `enqueue_tts_for_device` 相同的降级口径：音频送不出去不影响字幕。
    """
    session = _signaling_registry().get_session(device_id)
    if session is None:
        return ChunkPlacement(frames=0, start_frame=0)
    try:
        return await session.enqueue_tts_pcm_chunk_placed(turn_id, pcm)
    except Exception as exc:  # 过期 turn / 未建轨 / 背压 → 这一句不播
        _log_event(
            "dialogue pcm chunk rejected",
            trace_id=session.trace_id,
            connection_id=device_id,
            turn_id=turn_id,
            state=session.state.value,
            reason="reply_audio_dropped",
            error_type=type(exc).__name__,
        )
        return ChunkPlacement(frames=0, start_frame=0)


async def flush_tts_for_device(device_id: str, turn_id: str) -> int:
    """整轮合成结束时调用：把最后一句的残样补齐成完整帧。返回入队帧数。"""
    session = _signaling_registry().get_session(device_id)
    if session is None:
        return 0
    try:
        return await session.flush_tts_pcm(turn_id)
    except Exception as exc:
        _log_event(
            "dialogue pcm flush skipped",
            trace_id=session.trace_id,
            connection_id=device_id,
            turn_id=turn_id,
            state=session.state.value,
            reason="flush_skipped",
            error_type=type(exc).__name__,
        )
        return 0


async def enqueue_tts_for_device(device_id: str, turn_id: str, pcm: bytes) -> bool:
    """把**对话回复**的 PCM 写进该设备的播放轨（G-D6 的 device_id → session 入口）。

    只做两件事：按 device_id 找到会话、把异常翻译成 `bool`。返回值语义：
    `True` 已入队；`False` 会话不在 / turn 已过期 / 背压。调用方（编排层）
    据此决定是否记 `reply_audio_dropped`，**不需要也不应该重试**：
    被拒的那些原因（刚被打断、队列满了）重试只会让延迟更差。
    """
    session = _signaling_registry().get_session(device_id)
    if session is None:
        return False
    try:
        await session.enqueue_tts_pcm(turn_id, pcm)
    except Exception as exc:  # 过期 turn / 未建轨 / 背压都按「这次没送出去」处理
        _log_event(
            "dialogue pcm rejected",
            trace_id=session.trace_id,
            connection_id=device_id,
            turn_id=turn_id,
            state=session.state.value,
            reason="reply_audio_dropped",
            error_type=type(exc).__name__,
        )
        return False
    return True


async def play_system_tts_for_device(
    device_id: str, pcm: bytes, *, generation: int | None = None
) -> bool:
    """把**系统播报**（开场白等）的 PCM 写进该设备的播放轨（G-D13 的入口）。

    与 `enqueue_tts_for_device` 的唯一区别是**不要求 turn_id**：系统播报发生在
    用户说话之前，此时根本没有 turn。保护力度由 `generation` 承担 ——
    调用方在开始合成前取一次 `playback_generation()`，入队时比对，
    期间被打断就丢弃（详见 `MediaSession.enqueue_system_tts_pcm`）。
    """
    session = _signaling_registry().get_session(device_id)
    if session is None:
        return False
    return await session.enqueue_system_tts_pcm(pcm, generation=generation)


def playback_generation_for_device(device_id: str) -> int | None:
    """读该设备当前的播放代际号；会话不在时返回 `None`。

    调用方在**开始合成之前**取一次，合成完连同音频一起交给
    `play_system_tts_for_device(generation=...)`。这样「合成用了 800ms」这段窗口里
    如果长者开口打断过，那段开场白就不会突然跟在他自己的话音后面播出来。
    """
    session = _signaling_registry().get_session(device_id)
    if session is None:
        return None
    return session.playback_generation


def has_media_session(device_id: str) -> bool:
    """该设备当前是否挂着媒体会话（已建 PeerConnection、有播放轨）。"""
    return _signaling_registry().get_session(device_id) is not None


def has_authed_client(device_id: str) -> bool:
    """该设备是否有一条**已鉴权**的信令连接。

    比 `has_media_session` 宽松：浏览器在 `auth_ok` 之后、`offer` 到达之前这段窗口里没有
    PeerConnection，但页面上已经有形象（或正在加载）——开场白闸门要挡的正是这段时间。
    （2026-11-10 实测：按「有会话」判断时，开场白在 `vrm_load` 上报之前就发出去了。）

    单测 / 无浏览器环境没有注册表条目 → 返回 False → 走同步下发，不给测试白等 12s。
    """
    from app.audio.signaling import authed_connection

    return authed_connection(device_id) is not None


def playback_track_ready_for_device(device_id: str) -> bool:
    """该设备是否已经有可写的下行播放轨（= 媒体建连已完成）。

    ## 存在理由

    开场白与媒体建连是**竞态**（`docs/plans/day04/day04-对话闭环收口计划.md` §5-W5
    的时序图）：服务端在 `auth_ok` 刚发完就去查库判身份并下发开场白，而浏览器的
    `offer` 要等 `getUserMedia` 返回（数百 ms ~ 1s）才到。谁的先到不确定。

    所以调用方不能「试一次就放弃」，而要**等到轨可用**再写（有上限）。这个函数就是
    那个「可以试了吗」的探针：轨没建好返回 `False`（继续等），会话整个没了也返回
    `False`（由调用方用自己的期限决定放弃）。
    """
    session = _signaling_registry().get_session(device_id)
    return session is not None and session.has_playback_track


def _spawn_first_utterance_identity(registry: MediaSessionRegistry, device_id: str) -> None:
    """首句终稿到达：把本轮上行 PCM 交给身份级联做一次判定（D 方案）。

    只在「登录时没有索取确认」的场景下有活干（级联仍在等首句）；一旦
    身份已定，`resolve_identity_from_audio` 会立刻返回 False，代价是一次字典查表。
    容错口径：没有会话 / 没有音频 / 会话正在关闭，都静默跳过，绝不挡 ASR 主链。
    """
    session = registry.get_session(device_id)
    if session is None:
        return
    if session.state in (MediaState.CLOSING, MediaState.CLOSED, MediaState.FAILED):
        return
    pcm = session.last_utterance_pcm()
    if not pcm:
        return
    # 延迟导入：`app.audio.media_bridge` 模块级就 import 本模块，顶部再 import 会成环。
    from app.audio import media_bridge

    async def _resolve() -> None:
        await media_bridge.resolve_identity_from_audio(device_id, pcm)

    session.spawn(_resolve())


def _asr_callback(
    registry: MediaSessionRegistry, device_id: str, trace_id: str
) -> AsrEventCallback:
    # B1 垫层（plan day05 §1 B2）：每连接一个实例 —— turn 去重状态跟着连接走，
    # 文字降级下行绑定本设备。垫层失败不外抛，本 callback 的业务语义仍只有
    # 「deliver_asr_event + 一条日志」。
    # 迭代 6 W4（plan day06 §4-W4）：注入真实路由三件套——router=wire_route
    # （惰性 LayaClient，L0 短路/L1 决策）、identity=绑死本设备的身份解析
    # （device→elder/thread，库异常安全回 (None, None)）、trace_id 与本连接
    # 全链路同源（decision_logs ↔ 日志 ↔ /metrics 三方一致，plan §6 验收行）。
    # 未注入路径（迭代 5 基线断言）不受影响：ReplyStub 缺省即占位行为。
    stub = ReplyStub(
        deliver=lambda message: _deliver(device_id, message),
        router=wiring.wire_route,
        identity=lambda: wiring.resolve_identity(device_id),
        trace_id=trace_id,
    )

    async def _reply(turn_id: str, event: ASREvent) -> None:
        session = registry.get_session(device_id)
        if session is None:
            return
        outcome = await stub.reply(
            turn_id=turn_id,
            session=session,
            asr_text=event.result.text if event.result is not None else "",
        )
        _log_event(
            "reply stub",
            trace_id=trace_id,
            connection_id=device_id,
            turn_id=turn_id,
            state=session.state.value,
            reason="reply_stub",
            outcome=outcome.value,
        )

    async def _on_asr_event(turn_id: str, event: ASREvent) -> None:
        from app.audio import media_bridge, signaling

        # 终稿的 `turn_id`/`trace_id` 必须交给编排层：`enqueue_tts_pcm` 的 stale-turn
        # 校验（:1252）要求回复用**同一个** turn_id，丢了它就只能重造一个然后被拒。
        dialogue_ready = signaling.has_dialogue_final_handler()
        is_final = event.kind is ASREventKind.FINAL
        first_identity = is_final and media_bridge.needs_first_utterance_identity(device_id)

        if first_identity:
            # 首句：字幕先下发，但**先不调度对话**。身份判定（声纹 + 必要时开
            # provisional generic thread）完成后才调度，否则 `_resolve()` 仍是未定，
            # 这一句会被 `skipped unresolved` 吞掉（P1）。
            delivered = signaling.deliver_asr_event(
                device_id, event, turn_id=None, trace_id=trace_id
            )
            _log_event(
                "asr event delivery",
                trace_id=trace_id,
                connection_id=device_id,
                turn_id=turn_id,
                state="-",
                reason="asr_event",
                kind="final",
                delivered=delivered,
            )
            if not delivered:
                return

            session = registry.get_session(device_id)
            pcm = session.last_utterance_pcm() if session is not None else b""
            budget = get_settings().voiceprint_extract_timeout_seconds + 0.3
            try:
                async with asyncio.timeout(budget):
                    if pcm:
                        await media_bridge.resolve_identity_from_audio(device_id, pcm)
                    else:
                        # 没拿到本轮 PCM：无法判定身份，直接按通用模式放行这一句。
                        await media_bridge.ensure_generic_thread(device_id)
            except TimeoutError:
                logger.warning(
                    "first_utterance_identity_timeout",
                    extra={"trace_id": trace_id, "connection_id": device_id, "turn_id": turn_id},
                )
                await media_bridge.ensure_generic_thread(device_id)
            except Exception:
                logger.exception(
                    "first_utterance_identity_failed",
                    extra={"trace_id": trace_id, "connection_id": device_id, "turn_id": turn_id},
                )
                await media_bridge.ensure_generic_thread(device_id)

            if dialogue_ready:
                signaling.schedule_dialogue_final(
                    device_id, turn_id, trace_id, event.result.text if event.result else ""
                )
                return
            # 没有真编排时仍走垫层（单测/未装配场景），语义与普通终稿一致。
            session = registry.get_session(device_id)
            if session is None or session.state in (
                MediaState.CLOSING,
                MediaState.CLOSED,
                MediaState.FAILED,
            ):
                return
            session.spawn(_reply(turn_id, event))
            return

        delivered = signaling.deliver_asr_event(
            device_id,
            event,
            turn_id=turn_id if dialogue_ready else None,
            trace_id=trace_id,
        )
        kind = str(getattr(event.kind, "value", event.kind))
        _log_event(
            "asr event delivery",
            trace_id=trace_id,
            connection_id=device_id,
            turn_id=turn_id,
            state="-",
            reason="asr_event",
            kind=kind,
            delivered=delivered,
        )

        # 只有终稿触发回灌。回复**不在这里 await**：`_consume_asr` 收尾与
        # `end_turn` 都在等这个 callback，把秒级 TTS 合成塞进来会把 WS 信令回环
        # 一起拖住（用户打断的 asr_start 就得排队）。挂到会话任务上，接受它的
        # 正常生命周期：会话关闭时统一取消、过期 turn 由 enqueue_tts_pcm 拒绝。
        if not is_final:
            return
        # D 方案（2026-11-10 产品负责人裁决）：登录不再索取确认，改成「老人开口后」
        # 用这一句的真音频补一次身份判定（声纹命中即确认，否则弹确认层）。
        # 与「真编排是否装配」无关 —— 身份判定与对话回复是两条并行支路，
        # 所以这里排在 `dialogue_ready` 提前返回**之前**。
        _spawn_first_utterance_identity(registry, device_id)
        if dialogue_ready:
            # 真编排已装配（`app/main.py` lifespan 注册了回调），
            # `deliver_asr_event` 内部已经把这一轮交给编排层了；
            # 这里再走垫层就是「一句话回两遍」。二选一是硬约束。
            return
        session = registry.get_session(device_id)
        # 会话已关/正在关时 spawn 会抛 MediaClosedError，且被它拒绝的协程对象会漏成
        # 「never awaited」；单线程事件循环里「查完立刻 spawn」之间没有 await，不会竞态。
        if session is None or session.state in (
            MediaState.CLOSING,
            MediaState.CLOSED,
            MediaState.FAILED,
        ):
            return
        session.spawn(_reply(turn_id, event))

    return _on_asr_event


def _failure_callback(
    registry: MediaSessionRegistry, device_id: str, trace_id: str
) -> MediaFailureCallback:
    async def _on_media_failed(connection_id: str, reason: MediaFailureReason, detail: str) -> None:
        _log_event(
            "media failure delivery",
            trace_id=trace_id,
            connection_id=connection_id,
            turn_id=None,
            state=MediaState.FAILED.value,
            reason=reason.value,
            detail=detail,
        )
        await registry.close_session(device_id, reason=reason.value)
        _deliver_error(device_id)

    return _on_media_failed


class SignalingOfferer:
    """服务端当 offerer 的外呼侧（结构上满足 `outbound.Offerer`）。

    迭代 7 · S-D7-3：外呼复用 C2 既有 `offer/answer`、不新增消息类型，角色反向。

    ## 单出口（2026-11-10 N62f 缺陷 A）

    `offer` 信封**只由 `outbound._establish` 经 `Sender` 下发一次**，本类不再自己
    `_deliver`。此前两处各发一条：pad 收到两条 offer 就建两个 `RTCPeerConnection`、
    回两个 answer，第二个 answer 落到已 CONNECTING 的会话上触发 `MediaFormatError`
    → 会话被关 → 播报 `MediaClosedError`，迭代 7 外呼因此从未跑通。

    分工：本类负责**会话与等待位**（`create_session` → `create_offer` → 登记 waiter），
    下发与「设备离线」判定归 `Sender`（`signaling.deliver` 返回 False 即抛
    `DeviceOfflineError`，`outbound` 映射成 `no_answer`）。因此 `create_offer()` 不再有
    「未投递」分支；发不出去时由 `outbound` 调 `abandon()` 清场。
    """

    def __init__(self, device_id: str, *, trace_id: str | None = None) -> None:
        self._device_id = device_id
        self._trace_id = trace_id or f"outbound:{device_id}"

    async def create_offer(self) -> str:
        registry = _signaling_registry()
        # 上一次外呼留下的会话（可能还挂着旧 offer）先收掉，否则 create_session 冲突。
        await registry.close_session(self._device_id, reason="outbound_replaced")
        session = registry.create_session(
            self._device_id,
            self._trace_id,
            on_asr_event=_asr_callback(registry, self._device_id, self._trace_id),
            on_media_failed=_failure_callback(registry, self._device_id, self._trace_id),
        )
        sdp = await session.create_offer()

        # 先登记等待位再交回调用方：answer 可能在下发调用返回前就到（回环/局域网）。
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        _answer_waiters()[self._device_id] = waiter

        _log_event(
            "outbound offer created",
            trace_id=self._trace_id,
            connection_id=self._device_id,
            turn_id=None,
            state=session.state.value,
            reason="offer_ready",
            sdp_length=len(sdp),
        )
        return sdp

    async def abandon(self) -> None:
        """下发失败时的清场：撤等待位 + 关会话，不留悬空 session。

        由 `outbound._abandon_offer` 在 `sender.send` 抛错时调用（设备离线最常见）。
        """
        waiter = _answer_waiters().pop(self._device_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(False)
        await _signaling_registry().close_session(self._device_id, reason="offer_not_delivered")
        _log_event(
            "outbound offer abandoned",
            trace_id=self._trace_id,
            connection_id=self._device_id,
            turn_id=None,
            state=MediaState.CLOSED.value,
            reason="offer_not_delivered",
        )

    async def wait_answer(self, seconds: float) -> bool:
        """等上行 answer 被消费；超时 / 设备离线 / 没有在等的 offer 一律 False（不抛）。"""
        waiter = _answer_waiters().get(self._device_id)
        if waiter is None:
            return False
        try:
            return await asyncio.wait_for(waiter, seconds)
        except TimeoutError:
            return False
        finally:
            _answer_waiters().pop(self._device_id, None)


def _require_session(registry: MediaSessionRegistry, device_id: str) -> MediaSession:
    session = registry.get_session(device_id)
    if session is None:
        raise MediaStateError("media session is not ready")
    return session


async def _handle_offer(
    conn: ConnectionContext,
    registry: MediaSessionRegistry,
    device_id: str,
    message: Mapping[str, Any],
) -> None:
    sdp = message.get("sdp")
    if not isinstance(sdp, str) or not sdp.strip():
        raise MediaFormatError("offer SDP must be a non-empty string")

    # 同设备重连：A2 已踢旧连接；这里先收旧媒体，避免 create_session 冲突。
    await registry.close_session(device_id, reason="replaced")
    session = registry.create_session(
        device_id,
        conn.trace_id,
        on_asr_event=_asr_callback(registry, device_id, conn.trace_id),
        on_media_failed=_failure_callback(registry, device_id, conn.trace_id),
    )

    async def _close_on_disconnect() -> None:
        # 旧连接 callback 不能误关同设备新连接：必须确认槽位仍是本 session。
        if registry.get_session(device_id) is session:
            await registry.close_session(device_id, reason="ws_disconnect")

    conn.disconnect_callbacks.append(_close_on_disconnect)
    answer = await session.handle_offer(sdp)
    _deliver(device_id, {"type": "answer", "sdp": answer})


async def _handle_ice(
    registry: MediaSessionRegistry, device_id: str, message: Mapping[str, Any]
) -> None:
    session = _require_session(registry, device_id)
    candidate = message.get("candidate")
    if not isinstance(candidate, str):
        raise MediaFormatError("ice candidate must be a JSON string")
    await session.add_ice_candidate(candidate)


async def _handle_answer(
    registry: MediaSessionRegistry, device_id: str, message: Mapping[str, Any]
) -> None:
    """pad 对服务端外呼 offer 的应答（迭代 7 · S-D7-3 反向角色）。

    `accept_answer` 是**唯一**能把会话从 OPEN 推到 CONNECTING 的一行：没有它，
    服务端会以为正在谈、实际从没接手过远端 SDP，直到 ICE 看门狗把会话判死。
    """
    sdp = message.get("sdp")
    if not isinstance(sdp, str) or not sdp.strip():
        raise MediaFormatError("answer SDP must be a non-empty string")
    session = _require_session(registry, device_id)
    await session.accept_answer(sdp)
    waited = _resolve_answer_waiter(device_id, True)
    _log_event(
        "outbound answer applied",
        trace_id=session.trace_id,
        connection_id=device_id,
        turn_id=None,
        state=session.state.value,
        reason="answer_applied" if waited else "answer_without_waiter",
    )


async def _handle_asr_start(registry: MediaSessionRegistry, device_id: str) -> None:
    session = _require_session(registry, device_id)
    # asr_start = 前端开口：先停旧 TTS 写轨/清队列，再开新 ASR turn。
    session.barge_in()
    await session.begin_turn()


async def _handle_asr_end(registry: MediaSessionRegistry, device_id: str) -> None:
    session = _require_session(registry, device_id)
    # 收尾（flush 尾帧、把 provider 摘出会话槽位）放到后台：信令回环必须马上回去读
    # 下一帧，否则长者紧接着说第二句时，`asr_start` 会排在收尾后面，开头帧直接丢掉。
    # **调度时就固定 turn_id**：后台任务可能晚于下一轮 `asr_start` 才跑，若那时再读
    # `session.turn_id`，会把新轮误当成旧轮收尾。
    turn_id = session.turn_id
    if turn_id is None:
        return
    if session._end_turn_task is not None and not session._end_turn_task.done():
        return
    session._end_turn_task = session.spawn(session.end_turn(turn_id))


async def handle_signal_message(conn: ConnectionContext, message: dict[str, Any]) -> None:
    """A2 上行消息入口：C2 的 offer / answer / ice / asr_start / asr_end。

    `answer` 是 迭代 7 · S-D7-3 的反向角色（服务端外呼、pad 应答），闭集里不新增类型。
    """
    device_id = conn.device_id
    msg_type = message.get("type")
    if device_id is None or not isinstance(msg_type, str):
        return

    _log_event(
        "media signal received",
        trace_id=conn.trace_id,
        connection_id=device_id,
        turn_id=None,
        state="-",
        reason=msg_type,
        message_type=msg_type,
    )

    registry = _signaling_registry()
    try:
        if msg_type == "offer":
            await _handle_offer(conn, registry, device_id, message)
        elif msg_type == "answer":
            await _handle_answer(registry, device_id, message)
        elif msg_type == "ice":
            await _handle_ice(registry, device_id, message)
        elif msg_type == "asr_start":
            await _handle_asr_start(registry, device_id)
        elif msg_type == "asr_end":
            await _handle_asr_end(registry, device_id)
        else:
            _deliver_error(device_id)
    except MediaError as exc:
        if msg_type in ("offer", "answer"):
            await registry.close_session(device_id, reason=f"{msg_type}_failed")
        logger.warning(
            "media signal rejected",
            extra={
                "trace_id": conn.trace_id,
                "connection_id": device_id,
                "reason": msg_type,
                "error_type": type(exc).__name__,
            },
        )
        _deliver_error(device_id)
    except Exception as exc:
        # 信令边界必须兜底，不能让 A2 的 WS 端点因单条消息异常退出。
        if msg_type in ("offer", "answer"):
            await registry.close_session(device_id, reason="handler_error")
        logger.exception(
            "media signal handler failed",
            extra={
                "trace_id": conn.trace_id,
                "connection_id": device_id,
                "reason": msg_type,
                "error_type": type(exc).__name__,
            },
        )
        _deliver_error(device_id)


__all__ = [
    "ASR_FRAME_BYTES",
    "ASR_FRAME_MS",
    "ASR_FRAME_SAMPLES",
    "CANONICAL_FRAME_BYTES",
    "CANONICAL_FRAME_MS",
    "CANONICAL_FRAME_SAMPLES",
    "DOWNLINK_FRAME_BYTES",
    "DOWNLINK_FRAME_MS",
    "DOWNLINK_FRAME_SAMPLES",
    "DOWNLINK_SAMPLE_RATE",
    "MAX_ASR_QUEUE_FRAMES",
    "MAX_PLAYBACK_FRAMES",
    "PRE_ROLL_FRAMES",
    "PRE_ROLL_MS",
    "SAMPLE_WIDTH_BYTES",
    "TARGET_SAMPLE_RATE",
    "VAD_FRAME_BYTES",
    "VAD_FRAME_MS",
    "VAD_FRAME_SAMPLES",
    "AudioNormalizer",
    "ChunkPlacement",
    "DualFrameSplitter",
    "MediaBackpressureError",
    "MediaClosedError",
    "MediaConfig",
    "MediaConfigError",
    "MediaError",
    "MediaFailureCallback",
    "MediaFailureReason",
    "MediaFormatError",
    "MediaLoopMismatch",
    "MediaSession",
    "MediaSessionRegistry",
    "MediaState",
    "MediaStateError",
    "PcmFrame",
    "PcmFramer",
    "PreRollBuffer",
    "SignalingOfferer",
    "enqueue_tts_chunk_placed_for_device",
    "enqueue_tts_for_device",
    "flush_tts_for_device",
    "handle_signal_message",
    "install_signaling_handler",
    "uninstall_signaling_handler",
    "validate_downlink_pcm",
]
