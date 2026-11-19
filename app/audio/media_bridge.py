"""A2↔A3 接线层 · 会话身份级联（迭代 4 T2 · 契约 C5/C6）。

**为什么单独一个模块**：身份级联要把三样东西接起来 —— 连接上下文（A2 域）、
身份决策（`app/services/identity_service.py`）、以及 30s 定时器。把它塞进
`signaling.py` 会让「消息分发」和「身份业务」搅在一起；塞进 `webrtc.py` 又拿不到
`AsyncSession`、还要让媒体层碰数据库。收成这一层之后：

- `signaling.py` 只在三个时刻调用本模块：鉴权通过后 `start`、收到 `identity_confirm`
  时 `handle`、连接关闭时 `cancel`；
- `webrtc.py` **一行都不改**（它只管 offer/ICE/ASR 上行）。

分层（AGENTS §2）：`audio → services` 单向。本模块只调 services，不 import models/ORM。

## 三条时序（C5 裁决 ②、C6）

```
voice   : identity_result{source=voice,  elder_id=…}  → tts_text(人设问候)
confirm : identity_ask{ask_id, reason, candidates, timeout_s=30}
           ├─ 30s 内 identity_confirm 命中 → identity_result{confirm, elder_id} → tts_text
           └─ 30s 无有效 confirm          → identity_result{generic, null}      → tts_text(通用)
generic : identity_result{source=generic, elder_id=null} → tts_text(通用话术)
```

## 定时器口径（S-D1 裁决）

`asyncio.create_task` + `asyncio.sleep(IDENTITY_ASK_TIMEOUT_S)`，起点是**服务端把
`identity_ask` 放进下行队列的那一刻**（C5 裁决 ②：起点在服务端，不在客户端）。
连接关闭必须 `cancel_identity_cascade`，否则 30s 后会有一个孤儿任务：给一条已死的
连接发消息，并且真的往 `conversation_threads` 写一行没人认领的 thread。

## 为什么「不匹配 / 重复 confirm」一律静默忽略

C5 裁决 ③：`ask_id` 不匹配、重复 confirm、候选集外 `elder_id` 都**不回 error、不关连接**。
理由是体验：长者/护工误触一个按钮不该把整场对话打断；而且回 error 等于给外部一个
可探测的枚举面（猜 ask_id 拿不同响应）。所以只 warn 日志 + 不产生 thread。

## 一处已知偏离（已在任务卡与回报里登记）

C6 要求 greeting 取 `personas.greeting`，但 `persona_service` 目前只暴露
`get_avatar_config()`，没有 greeting 读口；`app/services/**` 不在本任务写作用域，
`app/audio/**` 又不允许 import ORM 直读表。故先用本模块的两个常量兜住，
读口补齐后只需改 `_greeting_text()` 一个函数。

## 会话中途静默复检（S1 / B2-06，2026-11-11 攻防计划 §4.6）

身份已定的会话，每条 ASR 终稿都会经 `resolve_identity_from_audio` 进来；身份定了
之后它原本直接 return，于是 `identity_service.recheck_session_identity` 成了死代码。
现在 `resolved` 分支改为调用 `_recheck_session_identity_quietly`：低置信 → 写审计
（`session.identity.recheck_denied`）+ 复用既有 `identity_ask` 降级；复检自身任何
失败（异常/超时/容器不可用）一律按「不复检」放行，绝不断开对话。

**已知局限（产品负责人 2026-11-13 裁决接受）**：调用方 `webrtc.py` 用
`session.spawn(...)` 并发触发，本句在此之前已经进入对话编排，所以**复检结论自下一句
起生效**；「换人那一句本身不进本人 thread」需要改 `webrtc.py`（不在本任务写作用域）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.tts_sentences import char_offsets_ms
from app.core import metrics
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.db import session_factory
from app.services import identity_service, persona_service
from app.services.device_service import list_elder_candidates
from app.services.identity_service import IdentityDecision
from app.services.voiceprint_service import VoiceprintUnavailable

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期需要的循环引用
    from app.audio.signaling import ConnectionContext

logger = logging.getLogger("app.audio.identity")

# C5 IdentityAskMsg.timeout_s 恒 30（契约定值，不得按心情调整）
IDENTITY_ASK_TIMEOUT_S = 30.0

#: 会话中途复检的**超时余量**（秒）。复检要跑一次声纹容器 extract，已有预算在
#: `settings.voiceprint_extract_timeout_seconds`（当前 5.0s）；这里再留 2s 给
#: 「取会话 + 一次 DB 往返 + 事件循环调度」。合成一个上限而不是新增配置项：
#: `app/core/config.py` 不在本任务写作用域，且这不是需要运维调的参数。
_RECHECK_TIMEOUT_MARGIN_S: Final[float] = 2.0

#: 复检拒绝后的**冷却窗口**（秒）。低置信不只出现在换人时：离麦、感冒、背景噪声
#: 都会掉分（R-C，产品负责人 2026-11-13 裁决接受）。没有冷却时长者每说一句都会被问一次
#: 「请确认是您吗」，陪伴就变成了审讯。60s 只压频率、不消除误报 —— 真换人的信号
#: 过了窗口仍会再次触发。
_RECHECK_DENY_COOLDOWN_S: Final[float] = 60.0

# C5：候选集恒 1 位（设备 ↔ 长者 1v1，裁决 v2.3）
_CANDIDATE_LIMIT = 1

# C6 personas.greeting 的临时兜底（见模块 docstring「一处已知偏离」）
DEFAULT_GREETING_TEXT = "您好呀，我在这儿陪着您，想聊点什么就跟我说。"
GENERIC_GREETING_TEXT = "您好，我在呢。有什么想聊的，随时跟我说一声。"

#: 等下行播放轨就绪的上限（秒）。取值依据见 `_spawn_greeting_audio` 的注释：
#: PRD line 571 要求「平板打开 5 秒内立起 3D 数字人并开口说话」，前端 `AvatarStage`
#: 自己的加载预算就是 `LOAD_TIMEOUT_MS = 5000`（`frontend/src/pad/avatar/AvatarStage.tsx:53`）。
#: 2026-11-10 修正：实测「TTS 合成 + offer/answer + ICE」在冷启动容器上要 6~10s，
#: 原来 5s 封顶正好卡在临界点，造成约 9% 的会话把开场白判成「等不到轨」
#: （`identity_greeting_audio played=false track_ready=false`）。这里只放宽「等轨」，
#: 不影响 PRD 的 5s 展示预算——轨没建好时音频照样入队（见 `enqueue_system_tts_pcm`），
#: 建连那一刻自然播出，长者听到的第一句话不会因此消失。
GREETING_TRACK_WAIT_SECONDS: Final[float] = 10.0
#: 轮询步长。50ms × 200 次 = 10s；比 16ms（一帧）粗、比 200ms 细，
#: 在「多等最多 50ms」与「不空转 600 次」之间取中。
GREETING_TRACK_POLL_SECONDS: Final[float] = 0.05
#: 下行 PCM 口径：16kHz / mono / s16le → 32 字节 = 1 毫秒。开场白字幕计划靠它反推时长。
_PCM_BYTES_PER_MS: Final[float] = 32.0

#: 等端侧「数字人已立起来」的上限（秒）。取 5s 的理由：PRD 要求「平板打开 5 秒内立起数字人
#: **并开口说话**」，等更久就等于用沉默换画面；而端侧连「内置剪影」出现都会发 `avatar_ready`
#: （见 `AvatarStage.announceReady`），正常路径远早于 5s，这个上限只兜「端侧一个信号都没发」。
AVATAR_READY_WAIT_SECONDS: Final[float] = 5.0

#: 开场白**开口截止时间**（秒）—— 起点是「登录进入页面」那一刻（`auth_ok` 之后的
#: `start_identity_cascade`），**总时长 2 秒**，不是「就绪之后再等 2 秒」。
#: 2026-11-11 产品负责人裁决：「开场白的 2 秒从登录进入页面那一刻起算，总共 2 秒」。
#: 实现口径：等完数字人之后只补 `max(0.0, 2.0 − 已用时间)` —— 登录→就绪已经花掉的时间
#: 要从这 2 秒里扣掉；登录本身就慢于 2 秒时不补负、也不回退，就绪即开口。
#: 为什么要有这道截止：VRM 加载完那一刻画面还在首帧姿态上，立刻出声会让人觉得声音和
#: 人对不上，留一点点时间让人看清「谁在说话」；但绝不能让长者干等，2 秒是硬上限。
GREETING_SPEAK_DEADLINE_SECONDS: Final[float] = 2.0

#: TTS 不可用时估算逐句字幕时长用的「每字毫秒数」。取值依据：实测 edge-tts
#: 中文普通话正常语速 ≈ 23 字/秒（22 字 → 5112ms，≈232ms/字），取 230ms/字
#: 并把下限定在 1200ms，保证再短的句子也够长者读完。
_ESTIMATE_MS_PER_CHAR: Final[float] = 230.0
_ESTIMATE_MIN_MS: Final[int] = 1200
#: 每个设备一条「数字人已就绪」事件；由端侧 `client_diag stage=vrm_load ok=true` 置位。
#: 键是 device_id（同设备重连沿用同一条），下次建级联时 `reset_avatar_ready` 清掉。
_avatar_ready_events: dict[str, asyncio.Event] = {}


def notify_avatar_ready(device_id: str) -> None:
    """端侧上报「数字人已渲染出来」——放行被压着的开场白。"""
    event = _avatar_ready_events.get(device_id)
    if event is None:
        event = asyncio.Event()
        _avatar_ready_events[device_id] = event
    event.set()


def reset_avatar_ready(device_id: str) -> None:
    """新一轮会话开始：把上一次的上报清掉，否则第二次登录会「立刻就绪」。"""
    _avatar_ready_events.pop(device_id, None)


async def wait_avatar_ready(device_id: str) -> bool:
    """等数字人立起来；等满 `AVATAR_READY_WAIT_SECONDS` 返回 False（照说，只是不再等）。"""
    event = _avatar_ready_events.get(device_id)
    if event is None:
        event = asyncio.Event()
        _avatar_ready_events[device_id] = event
    try:
        await asyncio.wait_for(event.wait(), timeout=AVATAR_READY_WAIT_SECONDS)
    except TimeoutError:
        return False
    return True


# 候选姓名脱敏兜底：候选集里查不到这位长者时，也不能把 UUID 念给长者听
_ANONYMOUS_DISPLAY_NAME = "这位长者"

# C5 IdentityAskMsg.reason 是**闭集**（low_confidence / vp_unavailable），
# 而 identity_service 还会返回 no_audio（无音频首轮）。闭集不能自创值，故映射：
# 「没听到声音」在语义上就是「声纹证据不足」，归 low_confidence。
_ASK_REASON_BY_SERVICE_REASON: dict[str, str] = {
    "low_confidence": "low_confidence",
    "no_audio": "low_confidence",
    "vp_unavailable": "vp_unavailable",
}


# ------------------------------------------------------------------ 会话级联状态


@dataclass
class _Cascade:
    """一次连接的身份级联状态（每个 device_id 至多一份）。

    字段都是「事件循环内单写」：`resolved` / `source` / `thread_id` 由
    `handle_identity_confirm` 或超时任务中的一个写入，两者通过 `lock` 串行化，
    谁先写 `resolved=True` 谁就赢得这次判定（另一个只读后退出）。
    """

    device_id: str
    trace_id: str
    # 归属连接（强引用，不存 id()）：同一台设备重连时新旧两条连接共用 device_id，
    # 取消/确认都要能分辨「这条级联是谁建的」。存对象而非 id(conn)，既避免 id 复用
    # 误判，也保证判定用的是同一性（is）而不是相等性。
    owner: ConnectionContext | None = None
    # 候选集（C5 candidates，恒 1 位）：[{"elder_id": str, "display_name": str}]
    candidates: list[dict[str, str]] = field(default_factory=list)
    ask_id: str | None = None
    candidate_elder_id: str | None = None
    reason: str | None = None
    resolved: bool = False
    # 会话中途复检（S1 / B2-06）：已确认身份后又被判「疑似换人」时置位，表示当前有
    # 一条**复检**弹层在等 confirm。它与 `resolved` 不冲突：`resolved` 描述「身份是否
    # 已定」，`recheck_pending` 只描述「是否还有一条复检 ask 在途」。**故意不把
    # `resolved` 翻回 False**：那样后续句会退回首轮校验路径，重复跑声纹、重复弹层。
    recheck_pending: bool = False
    # 上一次复检拒绝的时刻（`loop.time()` 秒数），用于 `_RECHECK_DENY_COOLDOWN_S`。
    last_recheck_denied_at: float | None = None
    source: str | None = None
    thread_id: str | None = None
    # 首句通用模式（P1）：身份未定时先开一条 provisional generic thread 让这一句能进
    # 对话，同时照常发 `identity_ask`。`confirm_pending=True` 表示「已能对话，但身份
    # 还没确认」；`provisional_thread_id` 在确认/超时时被替换或落定。
    confirm_pending: bool = False
    provisional_thread_id: str | None = None
    timeout_task: asyncio.Task[None] | None = None
    # 本轮该说的开场白（W5 · G-D9）：由 `_resolve_greeting` 从 `personas.greeting` 取，
    # 取不到就留 None，`_emit_greeting` 退回模块常量。
    # **为什么存在级联上而不是现读**：`_emit_greeting` 是同步函数（被同步路径 `_apply_terminal`
    # 调用），读库是 async；把「读」提到有会话的地方做完，「发」就能保持同步不变形。
    greeting: str | None = None
    # 开场白所用音色（`personas.voice_id`）；None 表示按 Settings 默认音色。
    voice_id: str | None = None
    # 开场白 2 秒截止的**计时起点**（`loop.time()` 秒数）：登录进页面那一刻，
    # 即 `start_identity_cascade` 建级联时。等完数字人后用它扣掉已用时间
    # （见 `_hold_for_avatar`）。`None` 表示没有级联起点（测试直接造替身、
    # 或同步路径下没有 loop），此时按「现在」起算，不凭空补时间。
    greeting_clock_start: float | None = None
    # 仅测试注入（把身份写库收进外层事务里回滚）；生产恒为 None，走自开短会话。
    session: AsyncSession | None = None
    # D 方案（2026-11-10）：登录不索取确认，等首句音频再验。那时不在
    # `start_identity_cascade` 的调用栈里，故把当前设备的 `Identity` 挂在级联上，
    # 弹确认层时才有候选集可用。
    identity: Identity | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# device_id → 级联状态。键与 signaling 的 `_registry` 同域（一个设备一条连接），
# 但生命周期独立：连接断开时由 `cancel_identity_cascade` 显式移除。
_cascades: dict[str, _Cascade] = {}

#: 在途的开场白任务强引用（见 `_spawn_greeting_audio`）。
#: 不持有强引用的话，task 可能在执行前被 GC 回收，日志里只剩「Task was destroyed」。
#: 合成任务产出 bytes、播放任务产出 None，这里只做「防 GC」，不读返回值。
_greeting_tasks: set[asyncio.Task[Any]] = set()


def active_cascade(device_id: str | None) -> _Cascade | None:
    """只读查看某设备的级联状态（日志、测试用；不要拿它改状态）。"""
    if device_id is None:
        return None
    return _cascades.get(device_id)


def needs_first_utterance_identity(device_id: str | None) -> bool:
    """这句终稿是否需要先跑首句身份判定，再交给对话编排。

    只有「级联存在、身份未定、且拿得到设备身份」时才为真。调用方（`webrtc`）
    据此把「先判定、后调度对话」串起来，避免首句被 `_resolve()` 判成未定而丢弃。
    """
    cascade = active_cascade(device_id)
    return cascade is not None and not cascade.resolved and cascade.identity is not None


async def ensure_generic_thread(device_id: str | None) -> None:
    """首句身份判定超时/失败时兜底：开 generic thread 并放行本轮对话。"""
    cascade = active_cascade(device_id)
    if cascade is None:
        return
    await _apply_generic_fallback(cascade)


@asynccontextmanager
async def _session_scope(injected: AsyncSession | None) -> AsyncIterator[AsyncSession]:
    """取一个会话：生产每次自开短会话；`injected` 分支**仅供测试**。

    为什么不复用 WS 端点的请求级会话（P1-A，审计 2026-11-07）：30s 定时器要在端点
    返回**之后**才跑，而那时 `Depends(get_session)` 的会话已随请求关闭；注入会话还
    没人 commit / rollback / close，confirm 与超时任务会在端点结束后继续用它。
    所以生产调用点（`signaling.signal_endpoint`）**不传** session，走下面的
    `session_factory()` 自开路径；测试为了把写库收进外层事务回滚，才显式注入。
    """
    if injected is not None:
        yield injected
        return
    async with session_factory() as session:
        yield session


# ------------------------------------------------------------------ 下行出口


def _deliver(device_id: str, message: Mapping[str, Any]) -> bool:
    """下发一条 C2 消息。局部 import signaling 规避模块级循环引用。"""
    from app.audio import signaling

    return signaling.deliver(device_id, message)


def _log_extra(cascade: _Cascade, **fields: object) -> dict[str, object]:
    extra: dict[str, object] = {
        "trace_id": cascade.trace_id,
        "device_id": cascade.device_id,
    }
    extra.update(fields)
    return extra


def _emit_result(cascade: _Cascade, *, source: str, elder_id: str | None) -> None:
    """下发 identity_result 并计数（C5 裁决 ⑤ 第 3 个打点位：下发才算）。"""
    message = {"type": "identity_result", "source": source, "elder_id": elder_id}
    if not _deliver(cascade.device_id, message):
        logger.info("identity_result_dropped", extra=_log_extra(cascade, source=source))
        return
    metrics.counter("identity_result_sent", source=source)


def greeting_caption(text: str, pcm_bytes: int, *, pcm: bytes | None = None) -> dict[str, object]:
    """开场白的逐字字幕计划（`tts_segments`），让开场白也走打字机。

    ## 为什么服务端算得出时长

    开场白的音频不经过对话回复的逐句流水线（走系统播报），前端拿不到
    `tts_segments`，字幕就只能整段砸出来 —— 和「随口型流式输出」不一致。

    这里用合成结果反推时长：16kHz / mono / s16le = **32 字节 = 1 毫秒**，
    所以 `duration_ms = pcm_bytes // 32`。前端在**首帧出声**时打锚点，
    计划里的 `start_ms` 从 0 起算，天然对齐（`frontend/src/pad/subtitlePlan.ts`）。

    ## 句内节奏

    给了 `pcm` 就顺带算 `char_ms`（能量包络）：开场白只有一段，匀速揭示会在
    「您好呀，」的逗号处让字先跑完、声音还在。没有 `pcm` 时只发时长，前端退回匀速。
    """
    duration_ms = max(1, round(pcm_bytes / _PCM_BYTES_PER_MS))
    segment: dict[str, object] = {"text": text, "start_ms": 0, "duration_ms": duration_ms}
    if pcm:
        offsets = char_offsets_ms(pcm, text, duration_ms=duration_ms)
        if offsets:
            segment["char_ms"] = offsets
    return {"type": "tts_segments", "segments": [segment]}


def estimated_caption(text: str) -> dict[str, object]:
    """TTS 不可用时按字数估算的逐句计划（`tts_segments`）。

    ## 为什么失败也要发计划

    没有计划，前端只能等 `REPLY_TEXT_FALLBACK_MS`（8s）把整段文本一次性砸出来 ——
    长者看到的是「半天没反应，然后一整句突然出现」，而「正在说」也永远不会亮
    （没有音频能量、也没有时间轴）。2026-11-11 edge-tts 在容器里 SSL 握手被拦，
    开场白全部落到这条路径，用户看到的就是「打字机没做、口型和字幕不同步」。

    计划在手，字幕就能按估算节奏逐字出现、播放态也能按时收口；代价只是
    「字幕和真实语音差几十毫秒」——比整段砸出来好得多。

    ## 估算口径

    `duration_ms = max(1200, round(字符数 × 230))`：中文普通话正常语速约
    23 字/秒（实测 22 字 → 5112ms）。按字符计（`len(text)`）而不是按词组，
    标点也算在内——标点处 TTS 本来就会停顿。
    """
    chars = len(text.strip()) or len(text) or 1
    duration_ms = max(_ESTIMATE_MIN_MS, round(chars * _ESTIMATE_MS_PER_CHAR))
    return {
        "type": "tts_segments",
        # 显式告诉前端「这份计划没有音频」：前端据此立刻用墙钟逐字，而不是停在 0
        # 等一个永远不会来的首帧能量（带音频的计划才等，见 realtime.playbackElapsedMs）。
        "estimated": True,
        "segments": [{"text": text, "start_ms": 0, "duration_ms": int(duration_ms)}],
    }


def _emit_greeting(cascade: _Cascade, *, generic: bool) -> None:
    """下发开场白（C6：`tts_text` + **TTS 音频**，不依赖 LLM）。

    `degraded` 必须显式发 `False`：C6 的 `TtsTextMsgV2.degraded` 语义是「这句是
    LLM 降级模板话术」（FR-DLG-05），而开场白本来就不经过 LLM，恒为正常文本。
    字段缺省虽然是 False，但契约要求字段**存在**（前端按 `msg.degraded === true`
    打降级标记），漏发会让「字段在不在」与「值是多少」两件事混在一起，故显式下发。

    ## 音频（迭代 4 收口 W5 · G-D3）

    契约原文写的是「下发 `tts_text` **+ TTS 音频**」，而代码此前只做了前半句
    —— 于是 PRD 里「平板打开几秒内立起 3D 数字人**并开口说话**」那条验收一直没达成
    （「5 秒内立起数字人」见 PRD 的 FR-3D-02 与 §5 验收条目；具体行号会随文档修订漂移，
    这里不钉行号，改按条款名引用）。

    音频走**系统播报**入口（`webrtc.play_system_tts_for_device`）而不是对话回复的
    `enqueue_tts_pcm`：开场白发生在用户开口**之前**，此时 `_playback_turn_id` 还是 `None`，
    走 turn 校验必然被拒（`webrtc.py:1252`）；而且开场白与媒体建连是竞态，
    媒体可能还没就绪 —— 那些情况都只是「这次没声音」，**不许**因此让身份级联失败。

    合成是异步的、下发是尽力而为的，本函数**永远不抛也不 await**：它被
    `_apply_terminal` 等同步路径调用，改成 async 会传染整条级联链。
    """
    text = cascade.greeting or (GENERIC_GREETING_TEXT if generic else DEFAULT_GREETING_TEXT)
    message = {"type": "tts_text", "text": text, "degraded": False}

    # 数字人还没立起来就先别开口（产品负责人 2026-11-10 要求：「不要数字人还没加载出来就说话」）：
    # 有媒体会话（真浏览器）时把「发文本 + 合成入队」整体压到 `vrm_load ok=true` 之后；
    # 没有媒体会话（单测 / 无浏览器的调试）就没有「等数字人」这件事，保持同步下发不变。
    if _has_media_session(cascade.device_id):
        _spawn_greeting_after_avatar(cascade, text=text, message=message, generic=generic)
        return

    if not _deliver(cascade.device_id, message):
        logger.info("identity_greeting_dropped", extra=_log_extra(cascade, generic=generic))
        return
    _spawn_greeting_audio(cascade, text=text)


def _has_media_session(device_id: str) -> bool:
    """该设备当前有没有真浏览器连着（决定开场白要不要等数字人）。

    用「已鉴权的信令连接」而不是「已建 PeerConnection」：开场白在 `auth_ok` 之后立刻发，
    那时 offer 通常还没到（实测差 ~1s），按会话判断等于闸门永远不生效。
    单测/无浏览器环境没有注册表条目 → False → 同步下发，不给用例白等。
    """
    from app.audio.webrtc import has_authed_client

    return has_authed_client(device_id)


def _spawn_greeting_after_avatar(
    cascade: _Cascade,
    *,
    text: str,
    message: dict[str, object],
    generic: bool,
) -> None:
    """把开场白压到「数字人已就绪」之后再发（文本与音频一起等）。

    为什么连文本也等：字幕和语音必须同一时刻出现。文本先到、音频后到（或反过来）就是
    长者看到的「没说话文字先出来」。这里一次压住两样，等的是同一个信号。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("identity_greeting_audio_skipped", extra=_log_extra(cascade, reason="no_loop"))
        return

    async def _pre_synthesize() -> bytes:
        """把开场白的 TTS 合成提到「等数字人」之前跑（两步并行）。

        为什么：合成实测 3.1s（edge 桥），而要求是「从登录起算 2 秒开口」。
        串行做就是 2s + 3.1s ≈ 5s —— 正是产品负责人 2026-11-11 报的「屏幕到 5 秒才说话」。
        合成与等待互不依赖，所以先起任务，2 秒截止到点时直接拿现成的 PCM 入队。
        """
        from app.audio.tts import build_tts

        return await build_tts(voice=getattr(cascade, "voice_id", None)).synthesize(text)

    synth_task = loop.create_task(_pre_synthesize())
    _greeting_tasks.add(synth_task)
    synth_task.add_done_callback(_greeting_tasks.discard)

    async def _hold_for_avatar() -> None:
        # 开口截止（2026-11-11 产品负责人裁决）：「开场白的 2 秒从登录进入页面那一刻起算，
        # 总共 2 秒」—— 不是「就绪后再等 2 秒」。所以这里只补**剩余**时间：
        # 等数字人花掉的那段已经从 2 秒里扣掉了，登录本身就慢于 2 秒时 remain=0，
        # 就绪即开口（不补负、不回退）。从前是「等就绪 + 再睡一段」，两段串起来实测 5.3 秒。
        loop = asyncio.get_running_loop()
        # `getattr` 而不是直接取属性：本函数跑在 `create_task` 里，抛出去没人接，
        # 结果是**开场白彻底不说**且只在日志里留一行 Task exception（2026-11-11 实测踩到）。
        # 拿不到起点就退化成「从现在算」，宁可晚开口也不要没有开场白。
        clock_start = getattr(cascade, "greeting_clock_start", None)
        started = clock_start if clock_start is not None else loop.time()
        try:
            ready = await asyncio.wait_for(
                wait_avatar_ready(cascade.device_id), timeout=AVATAR_READY_WAIT_SECONDS
            )
        except TimeoutError:
            ready = False

        await_gate_at = loop.time()
        remain = max(0.0, GREETING_SPEAK_DEADLINE_SECONDS - (await_gate_at - started))
        if remain > 0:
            await asyncio.sleep(remain)

        spoken_at = loop.time()
        logger.info(
            "identity_greeting_avatar_gate",
            extra=_log_extra(
                cascade,
                avatar_ready=ready,
                generic=generic,
                # 数字人就绪后**真正补等**了多久（0 表示登录已超 2 秒，就绪即开口）
                greeting_delay_ms=int(remain * 1000),
                # 从登录进页面到开口这一刻的总耗时（这 2 秒预算的实际用量）
                since_login_ms=int((spoken_at - started) * 1000),
            ),
        )

        if not _deliver(cascade.device_id, message):
            logger.info("identity_greeting_dropped", extra=_log_extra(cascade, generic=generic))
            return
        _spawn_greeting_audio(cascade, text=text, synth_task=synth_task)

    task = loop.create_task(_hold_for_avatar())
    _greeting_tasks.add(task)
    task.add_done_callback(_greeting_tasks.discard)


async def _resolve_greeting(
    cascade: _Cascade, elder_id: UUID | None, session: AsyncSession
) -> None:
    """把 `personas.greeting` 读到级联上（W5 · G-D9）；读不到就留 None。

    ## 只在「身份已定且非 generic」时读

    generic 会话没有 `elder_id`（库里 `identity_source='generic'` 要求 `elder_id IS NULL`），
    既查不到人设，也**不该**露出任何个性化文案 —— 那段固定用 `GENERIC_GREETING_TEXT`。

    ## 为什么不是每个人设都换开场白

    `personas.greeting` 是种子数据里的固定句（`scripts/seed_personas.py` 的
    「您好呀，我是小满，今天感觉怎么样？」）。改成读库是为了让**换人设就换话术**成为
    数据操作，而不是改代码。读失败（库抖、人设被删）一律退回模块常量 —— 开场白
    读不到不许让连不上（`get_persona_greeting` 自身已吞异常，这里再兜一层是防
    `UUID()` 解析失败这类调用方错误）。
    """
    if elder_id is None:
        return
    try:
        cascade.greeting = await persona_service.get_persona_greeting(session, elder_id=elder_id)
        cascade.voice_id = await persona_service.get_persona_voice_id(session, elder_id=elder_id)
    except Exception as exc:  # 人设读口任何意外都退回常量
        logger.warning(
            "identity_greeting_lookup_failed",
            extra=_log_extra(cascade, error_type=type(exc).__name__),
        )
        cascade.greeting = None
        cascade.voice_id = None


def _spawn_greeting_audio(
    cascade: _Cascade,
    *,
    text: str,
    synth_task: asyncio.Task[bytes] | None = None,
) -> None:
    """把「合成开场白 + 等轨就绪 + 写播放轨」挂到事件循环上（不 await）。

    ## 为什么要等轨（这是 W5 的核心）

    开场白下发的时刻，浏览器的 `offer` 通常还没到（时序图见
    `docs/plans/day04/day04-对话闭环收口计划.md` §5-W5）：
    `auth_ok` → 判身份 → 下发 `tts_text` 全在服务端几十毫秒内完成，而前端要先
    `getUserMedia`（数百 ms ~ 1s）才能发 `offer`，服务端收到 offer 才
    `create_peer_connection()` 才有播放轨。**先到的是文本，轨迟到**。

    所以这里**等到轨可用再写**（`GREETING_TRACK_WAIT_SECONDS` 封顶）。等满了也照样入队
    （2026-11-10 修正）：队列与会话同寿命、建连用的是同一个队列，所以「等不到」并不等于
    「播不了」，只是「播得晚一点」。从前等满就丢，实测 9 次登录丢 4 次 —— 那才叫碰运气。

    ## 为什么先取代际号

    `playback_generation_for_device` 在**合成之前**取一次，合成完带着它去入队。
    这段窗口里长者如果开口打断过，`barge_in()` 会把代际 +1，于是这段开场白被丢弃 ——
    否则会出现「长者已经在说自己的话，耳边突然响起『您好呀，我是小满』」。

    ## 放弃都要留日志

    没有运行中的循环 / TTS 失败 —— 都记一条结构化日志再放弃。
    静默失败会让「有字幕没声音」变成一个查不出来的现象。

    ## 没有运行中的循环时安静跳过

    同步单测里直接调 `_emit_greeting` 没有 loop，`create_task` 会抛 `RuntimeError`；
    那会让一条本该只是「发个下行消息」的路径变成会炸的路径。测试里真正要验的
    「音频有没有被合成」由 `tests/test_dialogue_runtime.py` 用显式注入的假 TTS 覆盖。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("identity_greeting_audio_skipped", extra=_log_extra(cascade, reason="no_loop"))
        return

    async def _play() -> None:
        from app.audio.webrtc import (
            play_system_tts_for_device,
            playback_generation_for_device,
            playback_track_ready_for_device,
        )

        # ① 先取代际号快照，再合成（顺序不能反，见下面 ③ 的说明）
        generation = playback_generation_for_device(cascade.device_id)

        # ② 合成（与「等轨」串行：合成本身就要几百 ms，那段等待不算白等）
        try:
            if synth_task is not None:
                # 预合成（2026-11-11 产品负责人：「我说的是延迟 2 秒，不是 5 秒」）：
                # 合成 3s 与「等数字人 + 补到 2 秒截止」并行，截止到点就能直接开口。
                pcm = await synth_task
            else:
                from app.audio.tts import build_tts

                pcm = await build_tts(voice=getattr(cascade, "voice_id", None)).synthesize(text)
        except Exception as exc:  # TTS 任何失败都只降级为「没声音」，但字幕计划照发
            logger.warning(
                "identity_greeting_tts_failed",
                extra=_log_extra(cascade, error_type=type(exc).__name__),
            )
            # 没有音频也要有时间轴：否则前端只能等 8s 兜底把整段文本砸出来，
            # 长者看到的是「半天没反应 + 一整句突然出现」，正是 2026-11-11 报的
            # 「打字机没做、口型和字幕不同步」。估算口径见 `estimated_caption`。
            caption = estimated_caption(text)
            _deliver(cascade.device_id, caption)
            logger.info(
                "identity_greeting_caption_estimated",
                extra=_log_extra(
                    cascade,
                    chars=len(text),
                    duration_ms=caption["segments"][0]["duration_ms"],  # type: ignore[index]
                    error_type=type(exc).__name__,
                ),
            )
            return
        if not pcm:
            logger.info("identity_greeting_tts_empty", extra=_log_extra(cascade))
            caption = estimated_caption(text)
            _deliver(cascade.device_id, caption)
            logger.info(
                "identity_greeting_caption_estimated",
                extra=_log_extra(
                    cascade,
                    chars=len(text),
                    duration_ms=caption["segments"][0]["duration_ms"],  # type: ignore[index]
                    error_type="empty_pcm",
                ),
            )
            return

        # ③ 等轨就绪：合成期间可能已经建好了，所以先探一次再进循环。
        #    等满预算仍没有轨（页面还没把 offer 送上来）**也照样入队**：队列与会话同寿命，
        #    建连那一刻自然播出去。从前这里直接 return，丢了就永远没声音 —— 实测 9 次
        #    登录丢 4 次，是「开场白经常没声音」的根因（2026-11-10）。
        waited = 0.0
        track_ready = playback_track_ready_for_device(cascade.device_id)
        while not track_ready and waited < GREETING_TRACK_WAIT_SECONDS:
            await asyncio.sleep(GREETING_TRACK_POLL_SECONDS)
            waited += GREETING_TRACK_POLL_SECONDS
            track_ready = playback_track_ready_for_device(cascade.device_id)

        # ④ 写轨。**用的是 ① 那一刻的代际快照**，不是现在的：
        #    ①→④ 这段窗口（合成 + 等轨，可能一两秒）里长者只要开口说过话，
        #    `barge_in()` 就把代际 +1 了，这段开场白必须作废 —— 长者正在说自己的话，
        #    这时候播「您好呀，我是小满」比不播差得多。
        #    如果在这里现取代际，就等于把这道保护整个抹掉：永远等于当前值，永远通过。
        played = await play_system_tts_for_device(cascade.device_id, pcm, generation=generation)
        logger.info(
            "identity_greeting_audio",
            extra=_log_extra(
                cascade,
                played=played,
                pcm_bytes=len(pcm),
                waited_ms=int(waited * 1000),
                track_ready=track_ready,
                generation=generation,
            ),
        )
        # 音频真播出去了才发字幕计划：没播就没有时间轴，发下去只会让打字机
        # 在没有声音的时候空跑一遍（长者看到字在动、却听不见声）。
        if played:
            _deliver(cascade.device_id, greeting_caption(text, len(pcm), pcm=pcm))

    task = loop.create_task(_play())
    # 强引用到底：否则 task 可能在执行前被回收，日志里只剩「Task was destroyed」。
    _greeting_tasks.add(task)
    task.add_done_callback(_greeting_tasks.discard)


# ------------------------------------------------------------------ 入口 ①：建会话


# 库内 `identity_source`（含 device）→ C5 的 source 闭集（voice / confirm / generic）。
# device 类终态在 C5 里没有对应值，对外统一按 voice 通知（真实来源只进日志）。
_SOURCE_BY_THREAD: dict[str, str] = {
    "voice": "voice",
    "confirm": "confirm",
    "device": "voice",
    "generic": "generic",
}


async def _reuse_open_thread(cascade: _Cascade, identity: Identity, session: AsyncSession) -> bool:
    """无音频首轮：库里已有「就是这位长者」的 open thread 时直接复用（不发 ask）。

    P1-4（只读审查 2026-11-07）：WS 断线重连会再次走到首轮身份判定，而这一轮没有
    音频，服务层固定 `confirm/no_audio` → 必发 `identity_ask`。长者刚点完「是我」，
    网络抖一下又要再点一次（退避 1/2/4/8/15s，最多 5 次）。库里那条 open thread
    已经是确凿答案，复用它就够了。

    语义边界（刻意这么定）：
    - **不做进程内缓存**：每次连接都回库查，服务重启 / 多副本部署行为一致；
    - `identity_source='generic'`（`elder_id IS NULL`）的 open thread **不参与复用**：
      它不是「已确认身份」，只是降级通行；复用它等于把 generic 永久焊在这台设备上；
    - open thread 的 elder 与设备当前绑定长者不一致（换人 / 设备重绑）→ 结束旧
      thread 后返回 False，交回正常级联重新确认；
    - 返回 True 表示已经下发 `identity_result` + greeting，调用方不得再判定；
    - **复用不计入确认率 / 声纹命中率**（P2-E）：这条路径下发 `identity_result` 只是
      把库内既有结论回放给新连接，既没有用户点选、也没有声纹比对。故额外打
      `identity_thread_reused{source}`；统计口径应用「result_sent − reused」算真实确认。
    """
    device_uuid = UUID(cascade.device_id)
    open_thread = await identity_service.find_open_thread(session, device_id=device_uuid)
    if open_thread is None:
        return False

    # 时效闸（2026-11-10 裁决 A）：open thread 只代表**一段**会话的身份结论。
    # 没有这道闸时，第一次确认过的身份会被永久回放：长者一登录就显示
    # 「身份 · 已确认」，而这一轮其实还没验过是谁（库里那条 open thread 从早上
    # 一直挂到晚上，`ended_at` 永远为空）。
    window_seconds = max(0, get_settings().identity_reuse_window_seconds)
    started_at = open_thread.started_at
    if started_at.tzinfo is None:  # 库里理论上恒带时区；naive 也不能让复用整条报错
        started_at = started_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - started_at
    if age > timedelta(seconds=window_seconds):
        await identity_service.end_open_thread(session, thread_id=open_thread.thread_id)
        logger.info(
            "identity_cascade_stale_thread_ended",
            extra=_log_extra(
                cascade,
                thread_id=open_thread.thread_id,
                reason="expired",
                age_seconds=int(age.total_seconds()),
                window_seconds=window_seconds,
            ),
        )
        return False

    if open_thread.elder_id is None:  # generic：只降级通行，不代表身份
        return False

    bound_elder = await identity_service.resolve_device_elder(session, device_uuid)
    if open_thread.elder_id != bound_elder:
        # 换人 / 设备重绑：旧 thread 不能再代表这台设备当前的身份
        await identity_service.end_open_thread(session, thread_id=open_thread.thread_id)
        logger.info(
            "identity_cascade_stale_thread_ended",
            extra=_log_extra(
                cascade,
                thread_id=open_thread.thread_id,
                thread_elder_id=str(open_thread.elder_id),
                bound_elder_id=str(bound_elder) if bound_elder is not None else None,
            ),
        )
        return False

    source = _SOURCE_BY_THREAD.get(open_thread.identity_source, "confirm")
    cascade.resolved = True
    cascade.source = source
    cascade.thread_id = open_thread.thread_id
    # W1 修复（2026-11-10）：复用路径必须把「是谁」写回候选身份。
    # dialogue_runtime._resolve 判身份时用的是 cascade.candidate_elder_id 而不是
    # thread_id，只设 thread_id 会让复用连接被判成「身份未定」，
    # 表现为 ASR/TTS 都通、但每一轮都打 `dialogue skipped unresolved`、长者听不到回复。
    cascade.candidate_elder_id = str(open_thread.elder_id)
    # 复用路径同样要读人设开场白（W5 · G-D9）：重连后听到的仍是这位长者的那句。
    await _resolve_greeting(cascade, open_thread.elder_id, session)
    _emit_result(cascade, source=source, elder_id=str(open_thread.elder_id))
    metrics.counter("identity_thread_reused", source=source)
    _emit_greeting(cascade, generic=False)
    logger.info(
        "identity_cascade_reused_thread",
        extra=_log_extra(
            cascade,
            thread_id=open_thread.thread_id,
            thread_source=open_thread.identity_source,
            source=source,
            elder=identity.elder_id,
        ),
    )
    return True


async def start_identity_cascade(
    conn: ConnectionContext,
    identity: Identity,
    *,
    audio_bytes: bytes | None = None,
    session: AsyncSession | None = None,
) -> _Cascade | None:
    """会话起点：校验身份并按决策下发第一条身份消息（+ 问候）。

    调用时机：`signaling.signal_endpoint` 里 `auth_ok` 入队之后。返回 None 表示
    连身份都取不到（未鉴权）—— 此时不发任何身份消息，让连接自己走断开流程。

    `audio_bytes` 为 None 时走「pad 刚上线、用户还没开口」分支（服务层给
    `confirm/no_audio`），有音频时走声纹比对。首轮真实链路上恒为 None；
    参数保留给「首帧先到音频」的后续接线（静默复检走 `recheck_session_identity`）。
    """
    device_id = conn.device_id
    if device_id is None:
        logger.warning("identity_cascade_without_device", extra={"trace_id": conn.trace_id})
        return None

    # 开场白 2 秒截止的计时起点（2026-11-11 裁决：起点＝登录进页面，总时长 2 秒）。
    # 取在 `cancel_identity_cascade` **之前**：那是本次登录的第一条逻辑，等它跑完再起算
    # 就等于把重连清理的耗时从 2 秒预算里偷偷赚回来了。end-to-end 只认这一个钟。
    greeting_clock_start = asyncio.get_running_loop().time()

    # 同设备重连：上一轮可能还挂着 30s 定时器，先清掉，避免重复开 thread。
    # 这里是**无条件**清（不带 conn）：新连接接管时，旧连接那条级联必须让位，
    # 哪怕它属于另一条 ConnectionContext（这正是「重连 supersede」的语义）。
    await cancel_identity_cascade(device_id)

    # 新一轮会话：清掉上一次的「数字人已就绪」上报，否则第二次登录会立刻具备开口条件。
    reset_avatar_ready(device_id)

    cascade = _Cascade(
        device_id=device_id,
        trace_id=conn.trace_id,
        owner=conn,
        session=session,
        greeting_clock_start=greeting_clock_start,
    )
    cascade.identity = identity
    _cascades[device_id] = cascade

    try:
        async with _session_scope(session) as sess:
            # 无音频（刚上线/重连）先看库里有没有可复用的 open thread：
            # 有就说明这次连接的「是谁」已经定过，不必再问一遍（P1-4）。
            if not audio_bytes and await _reuse_open_thread(cascade, identity, sess):
                return cascade
            if audio_bytes is None:
                # D 方案（2026-11-10 产品负责人裁决）：登录这一刻不索取确认。
                # 老人还没开口，此时问「是您吗」既打断又难看；等第一句真音频
                # 到位再跑声纹（`resolve_identity_from_audio`），命中直接确认。
                await _defer_identity(cascade, sess)
                return cascade
            decision = await identity_service.verify_session_identity(
                sess, device_id=UUID(device_id), audio_bytes=audio_bytes
            )
            if decision.needs_confirm:
                await _apply_confirm_request(cascade, identity, decision, sess)
            else:
                # 开场白取人设值（W5 · G-D9）：必须在 `_apply_terminal` **之前**读，
                # 因为 `_emit_greeting` 是同步的、`_apply_terminal` 里就要用它。
                await _resolve_greeting(cascade, decision.elder_id, sess)
                _apply_terminal(cascade, decision)
    except VoiceprintUnavailable:
        # 本进程配置错（Fernet 密钥不可用）：**不能**静默 generic。
        # 静默降级会让「声纹没配好」表现成「系统不认识您」，且用户没有任何出口。
        # 走 vp_unavailable 主动确认：问一句「是您吗」，点确认即可继续。
        logger.exception("identity_cascade_config_unavailable", extra=_log_extra(cascade))
        # 上面的 `async with` 已因异常退出（注入会话被回滚、自开会话已关），
        # 这里必须重新取一条会话；直接用 `session` 参数在生产路径上是 None。
        #
        # P1-B（审计 2026-11-07）：这一整段原先在 `except Exception` 的 try 之外，
        # 里面两个 await 一旦因为 DB 抖动抛错就会穿出 `start_identity_cascade`，
        # 结果 WS 以 1011 断开，且既没有 identity_result 也没有 greeting —— 比
        # 「静默 generic」更糟。补一层兜底：兜底自己失败，也仍要有一条 generic 结果。
        try:
            async with _session_scope(session) as fallback_sess:
                bound = await identity_service.resolve_device_elder(fallback_sess, UUID(device_id))
                if bound is None:
                    await _apply_generic_fallback(cascade)
                else:
                    await _apply_confirm_request(
                        cascade,
                        identity,
                        IdentityDecision(
                            source="confirm",
                            elder_id=bound,
                            thread_id=None,
                            reason="vp_unavailable",
                        ),
                        fallback_sess,
                    )
        except Exception:
            logger.exception("identity_cascade_config_fallback_failed", extra=_log_extra(cascade))
            await _apply_generic_fallback(cascade)
    except Exception:
        # 身份链路任何异常都不得挡住房门：降级 generic 继续对话（FR-VPR-03 口径），
        # 但要留完整栈，别让「声纹服务挂了」表现成「长者点了没反应」。
        logger.exception("identity_cascade_failed", extra=_log_extra(cascade))
        await _apply_generic_fallback(cascade)

    return cascade


async def _defer_identity(cascade: _Cascade, session: AsyncSession) -> None:
    """D 方案分支：登录这一刻**不索取确认**，等第一句真音频再判定身份。

    ## 为什么（2026-11-10 产品负责人裁决 D）

    长者刚坐下的第一件事是「听到招呼」，不是「点一个『是我』按钮」。原来
    首轮无音频直接走 `confirm/no_audio` → 弹确认层，体验上是「刚开机就被要求
    证明自己是谁」，而且开场白还要抢在确认层之前播，两条链路互相挤。

    改成：登录只播一句**中性**问候（不念人名），身份胶囊停在「待确认」；
    老人开口后 `resolve_identity_from_audio()` 用这段真音频跑声纹，
    命中就直接确认并补播个人开场白，命中不了才弹确认层。

    ## 为什么不建 thread

    thread 只在「身份已定」时落库（闸门①）。这里故意不写，把 `resolved`
    留给首句音频那一次判定，`_reuse_open_thread` 也就不会把半个身份复用出去。
    """
    bound = await identity_service.resolve_device_elder(session, UUID(cascade.device_id))
    if bound is None:
        # 设备未绑定长者：没有候选可问，与老路径一致走 generic。
        await _apply_generic_fallback(cascade)
        return
    cascade.candidate_elder_id = str(bound)
    cascade.reason = "await_wake"
    delivered = _deliver(cascade.device_id, {"type": "identity_pending", "reason": "await_wake"})
    metrics.counter("identity_deferred")
    _emit_greeting(cascade, generic=True)
    logger.info(
        "identity_deferred",
        extra=_log_extra(cascade, delivered=delivered, reason="await_wake"),
    )


async def resolve_identity_from_audio(device_id: str, audio_bytes: bytes) -> bool:
    """首句真音频到达：跑一次声纹，命中即确认；命中不了才弹主动确认。

    身份已定（`cascade.resolved`）之后这里不再无事可做：每条后续终稿进
    `_recheck_session_identity_quietly` 做一次静默复检（S1 / B2-06）。

    返回 True 表示「这次真的做了一次判定」（不论结论），False 表示无事可做
    （级联不存在 / 没有音频 / 复检按「不复检」放行）。

    调用方是媒体层（`app/audio/webrtc.py::_asr_callback._on_asr_event` 的终稿分支），
    音频来自 `MediaSession.last_utterance_pcm()` —— 只在本轮内存里存在，不落库。
    """
    cascade = _cascades.get(device_id)
    if cascade is None or not audio_bytes:
        return False
    if cascade.resolved:
        # 复检结论**自下一句生效**：调用方是 `session.spawn`，本句已经进了对话编排
        # （见模块 docstring 的「已知局限」，产品负责人 2026-11-13 裁决接受）。
        return await _recheck_session_identity_quietly(cascade, audio_bytes)
    identity = cascade.identity
    if identity is None:
        return False
    try:
        async with _session_scope(cascade.session) as sess:
            decision = await identity_service.verify_session_identity(
                sess, device_id=UUID(device_id), audio_bytes=audio_bytes
            )
            if decision.needs_confirm:
                await _apply_confirm_request(cascade, identity, decision, sess)
            else:
                await _resolve_greeting(cascade, decision.elder_id, sess)
                _apply_terminal(cascade, decision)
    except VoiceprintUnavailable:
        logger.exception("identity_first_audio_unavailable", extra=_log_extra(cascade))
        try:
            async with _session_scope(cascade.session) as fallback_sess:
                bound = await identity_service.resolve_device_elder(fallback_sess, UUID(device_id))
                if bound is None:
                    await _apply_generic_fallback(cascade)
                else:
                    await _apply_confirm_request(
                        cascade,
                        identity,
                        IdentityDecision(
                            source="confirm",
                            elder_id=bound,
                            thread_id=None,
                            reason="vp_unavailable",
                        ),
                        fallback_sess,
                    )
        except Exception:
            logger.exception("identity_first_audio_fallback_failed", extra=_log_extra(cascade))
            await _apply_generic_fallback(cascade)
    except Exception:
        logger.exception("identity_first_audio_failed", extra=_log_extra(cascade))
        await _apply_generic_fallback(cascade)
    return True


# ------------------------------------------------------------------ 会话中途静默复检


async def _recheck_session_identity_quietly(cascade: _Cascade, audio_bytes: bytes) -> bool:
    """复检一句：疑似换人 → 审计 + `identity_ask` 降级；其余一律放行。

    返回 True 仅当「判成了可疑身份并已处置」。本函数**永不抛**：复检是增强不是闸门，
    声纹容器 / 会话 / 超时任何一环坏掉都只 warn，对话照常继续。
    """
    if cascade.recheck_pending:
        # 已有一条复检弹层在等 confirm：不再重复跑声纹，也不再叠一条 ask。
        return False
    if cascade.source == "generic" or cascade.thread_id is None:
        # generic 会话没有本人身份可保护（thread 无 elder），复检没有意义。
        return False

    settings = get_settings()
    budget = settings.voiceprint_extract_timeout_seconds + _RECHECK_TIMEOUT_MARGIN_S
    try:
        async with asyncio.timeout(budget):
            async with _session_scope(cascade.session) as sess:
                result = await identity_service.recheck_session_identity(
                    sess,
                    device_id=UUID(cascade.device_id),
                    current_thread_id=cascade.thread_id,
                    audio_bytes=audio_bytes,
                )
    except Exception:
        # 超时 / 容器异常 / DB 抖动：按「不复检」放行，只留一条 warn。
        logger.warning(
            "identity_recheck_failed",
            exc_info=True,
            extra=_log_extra(cascade, thread_id=cascade.thread_id),
        )
        return False

    if result.status == "suspected_replacement":
        await _on_recheck_mismatch(cascade, result)
        return True
    if result.status == "switched":
        await _apply_recheck_switch(cascade, result)
        return True
    return False


async def _on_recheck_mismatch(
    cascade: _Cascade, result: identity_service.SessionRecheckResult
) -> None:
    """疑似换人：先写审计，再复用既有 `identity_ask` 降级（不新造话术）。

    代价刻意选「降级」而不是「静默放行」：误报的代价是这一次降级到 generic 话术、
    长者重说一遍，**不是**让陌生人冒认成本人（R-B，产品负责人 2026-11-13 裁决）。

    顺序（同一次裁决）：审计在前且永不抛 —— 降级一旦发生就必须留痕。
    """
    loop_now = asyncio.get_running_loop().time()
    last_denied = cascade.last_recheck_denied_at
    cooling = last_denied is not None and (loop_now - last_denied) < _RECHECK_DENY_COOLDOWN_S
    cascade.last_recheck_denied_at = loop_now

    try:
        await identity_service.audit_session_recheck_denied(
            thread_id=cascade.thread_id or "",
            device_id=UUID(cascade.device_id),
            elder_id=result.elder_id,
            score=result.score,
            threshold=result.threshold,
        )
    except Exception:
        # 服务层已自行吞异常；这里再兜一层，保证任何情况下都不打断对话。
        logger.warning("identity_recheck_audit_failed", exc_info=True, extra=_log_extra(cascade))
    metrics.counter("identity_recheck_denied", result="suspected_replacement")

    identity = cascade.identity
    if identity is None or cooling:
        logger.info(
            "identity_recheck_ask_suppressed",
            extra=_log_extra(cascade, reason="cooldown" if cooling else "no_identity"),
        )
        return

    cascade.recheck_pending = True
    try:
        async with _session_scope(cascade.session) as sess:
            await _apply_confirm_request(
                cascade,
                identity,
                IdentityDecision(
                    source="confirm",
                    elder_id=result.elder_id,
                    thread_id=cascade.thread_id,
                    reason="low_confidence",
                    confidence=result.score,
                ),
                sess,
            )
    except Exception:
        cascade.recheck_pending = False
        logger.warning("identity_recheck_ask_failed", exc_info=True, extra=_log_extra(cascade))
        return
    if cascade.ask_id is None or cascade.timeout_task is None:
        # 建不出候选 / 下行断开会走 terminal 或直接返回：没有 ask 在途就没什么可 pending。
        cascade.recheck_pending = False


async def _apply_recheck_switch(
    cascade: _Cascade, result: identity_service.SessionRecheckResult
) -> None:
    """复检命中另一位已绑定长者：切到服务层新开的 thread（voice）。

    与旧实现（recheck 返回 IdentityDecision 时关旧 thread、开新 thread）同一语义，
    只是搬进 media_bridge，并补一条 `identity_result` 让前端跟着换人设。
    """
    decision = result.decision
    if decision is None or decision.thread_id is None or decision.elder_id is None:
        logger.warning("identity_recheck_switch_missing_decision", extra=_log_extra(cascade))
        return
    cascade.thread_id = decision.thread_id
    cascade.source = "voice"
    cascade.candidate_elder_id = str(decision.elder_id)
    cascade.recheck_pending = False
    _emit_result(cascade, source="voice", elder_id=str(decision.elder_id))
    logger.info(
        "identity_recheck_switched",
        extra=_log_extra(cascade, thread_id=decision.thread_id, elder_id=str(decision.elder_id)),
    )


async def _apply_confirm_request(
    cascade: _Cascade,
    identity: Identity,
    decision: IdentityDecision,
    session: AsyncSession,
) -> None:
    """confirm 分支：下发 identity_ask 并起 30s 定时器（C5 裁决 ②）。"""
    if decision.elder_id is None:
        # 契约不允许「要确认却没有候选」：这种数据只可能是服务层 bug，
        # 按 generic 处理比发一条 candidates=[] 的 ask（前端无法渲染）更安全。
        logger.warning("identity_confirm_without_candidate", extra=_log_extra(cascade))
        await _apply_generic_fallback(cascade)
        return

    candidate = await _build_candidate(
        identity, decision.elder_id, UUID(cascade.device_id), session
    )
    ask_id = uuid.uuid4().hex
    reason = _ASK_REASON_BY_SERVICE_REASON.get(str(decision.reason or ""), "low_confidence")

    # 首句通用模式（P1）：先把这一句接进对话，再问「是您吗」。
    # 只在身份**尚未确定**时开 provisional thread；会话中途复检（resolved=True）不能
    # 再开第二条，否则确认后会多出一条孤儿 open thread。
    if not cascade.resolved:
        try:
            provisional = await identity_service.open_thread_as_generic_pending_confirm(
                session, device_id=UUID(cascade.device_id)
            )
        except Exception:
            logger.exception("identity_provisional_thread_failed", extra=_log_extra(cascade))
        else:
            cascade.resolved = True
            cascade.source = "generic"
            cascade.thread_id = provisional.thread_id
            cascade.provisional_thread_id = provisional.thread_id
            cascade.confirm_pending = True
            logger.info(
                "identity_provisional_generic",
                extra=_log_extra(cascade, thread_id=provisional.thread_id),
            )

    cascade.ask_id = ask_id
    cascade.candidate_elder_id = candidate["elder_id"]
    cascade.candidates = [candidate]
    cascade.reason = reason

    message = {
        "type": "identity_ask",
        "ask_id": ask_id,
        "reason": reason,
        "candidates": [candidate],
        "timeout_s": int(IDENTITY_ASK_TIMEOUT_S),
    }
    if not _deliver(cascade.device_id, message):
        # 连接已断：不必起定时器（没人能确认），状态随连接关闭一起清掉。
        # provisional thread 也要结束，否则下一次连接复用它就等于「从没问过」。
        provisional_id = cascade.provisional_thread_id
        cascade.confirm_pending = False
        cascade.provisional_thread_id = None
        if provisional_id is not None:
            cascade.resolved = False
            cascade.source = None
            cascade.thread_id = None
            try:
                await identity_service.end_open_thread(session, thread_id=provisional_id)
            except Exception:
                logger.warning(
                    "identity_provisional_thread_close_failed",
                    exc_info=True,
                    extra=_log_extra(cascade, thread_id=provisional_id),
                )
        logger.info("identity_ask_dropped", extra=_log_extra(cascade, reason=reason))
        return

    metrics.counter("identity_ask_sent", reason=reason)
    task = asyncio.create_task(
        _identity_timeout(cascade, ask_id),
        name=f"identity-timeout:{cascade.device_id}",
    )
    cascade.timeout_task = task
    # P2-C（审计 2026-11-07）：新连接可能在「旧连接写进 `_cascades`」之后、
    # 「这里装 task」之前完成 supersede —— 那一刻 `timeout_task` 还是 None，
    # cancel 直接 return，随后这条定时器就再没人能取消，30s 后会替新连接
    # 写一行 generic thread 并推一条 identity_result 给新连接。装完立刻自检归属。
    if _cascades.get(cascade.device_id) is not cascade:
        task.cancel()
        cascade.timeout_task = None
        provisional_id = cascade.provisional_thread_id
        cascade.confirm_pending = False
        cascade.provisional_thread_id = None
        if provisional_id is not None:
            # 这条 ask 永远不会有人回答了：provisional thread 必须当场结束，
            # 否则它会作为一条孤儿 generic 会话一直堆在库里。
            cascade.resolved = False
            cascade.source = None
            cascade.thread_id = None
            try:
                await identity_service.end_open_thread(session, thread_id=provisional_id)
            except Exception:
                logger.warning(
                    "identity_provisional_thread_close_failed",
                    exc_info=True,
                    extra=_log_extra(cascade, thread_id=provisional_id),
                )
        logger.info("identity_timeout_superseded", extra=_log_extra(cascade, ask_id=ask_id))
        return
    logger.info(
        "identity_ask_sent",
        extra=_log_extra(cascade, ask_id=ask_id, reason=reason, timeout_s=IDENTITY_ASK_TIMEOUT_S),
    )


async def _build_candidate(
    identity: Identity, elder_id: UUID, device_id: UUID, session: AsyncSession
) -> dict[str, str]:
    """组装 C5 candidates[0]：`{elder_id, display_name}`，姓名用服务层已脱敏的值。

    候选集来自 `beds`，而设备绑定在 `devices.elder_id` —— 两者历史不一致时
    （解绑中、调房未同步）候选里可能查不到这位长者。此时**仍然问这个人**，
    姓名换成脱敏兜底话术：宁可少一个姓名字段，也不能因为查不到名字就换个人问。
    """
    try:
        listing = await list_elder_candidates(session, identity, device_id)
    except Exception:
        logger.warning("identity_candidates_unavailable", exc_info=True)
        listing = None

    if listing is not None:
        for entry in listing.candidates[:_CANDIDATE_LIMIT]:
            if str(entry.elder_id) == str(elder_id):
                return {"elder_id": str(entry.elder_id), "display_name": entry.display_name}

    return {"elder_id": str(elder_id), "display_name": _ANONYMOUS_DISPLAY_NAME}


def _apply_terminal(
    cascade: _Cascade, decision: IdentityDecision, *, force_generic: bool = False
) -> None:
    """voice / generic 分支：下发 identity_result + 问候，级联到此结束。"""
    if force_generic or decision.elder_id is None or decision.source == "generic":
        cascade.resolved = True
        cascade.source = "generic"
        cascade.thread_id = decision.thread_id
        _emit_result(cascade, source="generic", elder_id=None)
        _emit_greeting(cascade, generic=True)
        logger.info("identity_generic", extra=_log_extra(cascade, reason=decision.reason))
        return

    # voice：声纹命中 → identity_result 带上 elder_id。C5 的 source 是闭集
    # （voice/confirm/generic）；服务层若给出 device 类终态，同样表示「身份已定」，
    # 对外统一按 voice 通知（真实来源只进日志，不再造第四个枚举值）。
    cascade.resolved = True
    cascade.source = "voice"
    cascade.thread_id = decision.thread_id
    _emit_result(cascade, source="voice", elder_id=str(decision.elder_id))
    _emit_greeting(cascade, generic=False)
    logger.info(
        "identity_voice",
        extra=_log_extra(
            cascade,
            decision_source=decision.source,
            confidence=decision.confidence,
            thread_id=decision.thread_id,
        ),
    )


async def _apply_generic_fallback(cascade: _Cascade) -> None:
    """异常兜底：没有可用身份时**先开 generic thread**，再发一次 generic（已 resolved 则返回）。

    只置 `source/thread_id` 而不建 thread 的旧写法会让 `dialogue_runtime._resolve()`
    继续返回 `None`：降级看起来发生了，实际每一轮都被 `skipped unresolved` 丢掉。
    """
    if cascade.resolved:
        return
    thread_id = cascade.thread_id or cascade.provisional_thread_id
    if not thread_id:
        try:
            async with _session_scope(cascade.session) as sess:
                decision = await identity_service.open_thread_as_generic_pending_confirm(
                    sess, device_id=UUID(cascade.device_id)
                )
            thread_id = decision.thread_id
        except Exception:
            # 写库失败也照样发 generic：话术比没有结果更重要，thread 缺失由编排层兜底。
            logger.exception("identity_generic_thread_failed", extra=_log_extra(cascade))
    cascade.resolved = True
    cascade.source = "generic"
    cascade.thread_id = thread_id
    cascade.confirm_pending = False
    cascade.provisional_thread_id = None
    _emit_result(cascade, source="generic", elder_id=None)
    _emit_greeting(cascade, generic=True)


# ------------------------------------------------------------------ 入口 ②：confirm 到达


async def handle_identity_confirm(conn: ConnectionContext, message: Mapping[str, Any]) -> None:
    """处理 C→S `identity_confirm`（C5 裁决 ③：不匹配/重复一律静默忽略）。

    **任何分支都不回 error、不关连接**：这是契约明文，不是省事。前端在 confirm
    期间仍在正常说话，把连接关掉等于把一次误触升级成一次掉线。
    """
    cascade = active_cascade(conn.device_id)
    if cascade is None:
        _ignore_confirm(conn, reason="no_cascade")
        return
    if cascade.owner is not conn:
        # 同设备重连后可能残留另一条连接的级联（正常路径下 start 已清掉，但 start 在
        # 建库会话上失败时不会走到）。这时新旧两条连接都在发 confirm，只有归属连接
        # 才有权推进它 —— 否则旧连接的误触会替新连接把身份定下来。
        _ignore_confirm(conn, reason="stale_cascade", cascade=cascade)
        return

    ask_id = message.get("ask_id")
    elder_id = message.get("elder_id")
    if not isinstance(ask_id, str) or not isinstance(elder_id, str) or not elder_id:
        _ignore_confirm(conn, reason="malformed", cascade=cascade)
        return

    # 校验与占位在同一临界区：这里没有 await，事件循环不会插进别的协程
    provisional_id: str | None = None
    async with cascade.lock:
        if cascade.resolved and not cascade.recheck_pending and not cascade.confirm_pending:
            # 首轮身份已定且没有复检/首句确认在途：重复 confirm 静默忽略（C5 裁决 ③）。
            # `recheck_pending` / `confirm_pending` 例外——两条 ask 都是 `resolved=True`
            # 之后发的，不放行这两个分支就永远按不下「是我」。
            reason = "already_resolved"
        elif cascade.ask_id is None or ask_id != cascade.ask_id:
            reason = "ask_id_mismatch"
        elif cascade.candidate_elder_id is None or elder_id != cascade.candidate_elder_id:
            # 候选集外 elder_id：按「未确认」处理 —— 不占掉这次 ask，30s 窗口继续有效，
            # 也绝不产生该 elder 的 thread（完工标准 §7.2）
            reason = "elder_id_not_candidate"
        else:
            reason = None
            provisional_id = cascade.provisional_thread_id
            cascade.resolved = True
            cascade.source = "confirm"
            cascade.recheck_pending = False
            cascade.confirm_pending = False
            cascade.provisional_thread_id = None

    if reason is not None:
        _ignore_confirm(conn, reason=reason, cascade=cascade, ask_id=ask_id, elder_id=elder_id)
        return

    metrics.counter("identity_confirm_received", result="accepted")
    await _cancel_timeout(cascade)

    try:
        async with _session_scope(cascade.session) as sess:
            decision = await identity_service.open_thread_from_confirm(
                sess,
                device_id=UUID(cascade.device_id),
                confirmed_elder_id=UUID(elder_id),
                candidate_elder_id=UUID(str(cascade.candidate_elder_id)),
                replace_thread_id=provisional_id,
            )
    except Exception:
        logger.exception("identity_confirm_thread_failed", extra=_log_extra(cascade))
        # 已承诺「确认成功但不能开 thread」比降级 generic 更糟：既没身份也没话术，
        # 前端会一直停在确认页。统一降级 generic，保证会话能继续。
        if provisional_id is not None:
            # provisional thread 还在，继续沿用；它仍是 open 的。
            cascade.source = "generic"
            cascade.thread_id = provisional_id
            _emit_result(cascade, source="generic", elder_id=None)
            _emit_greeting(cascade, generic=True)
        else:
            # 没有可沿用的 thread：必须补开一条，否则 dialogue_runtime._resolve()
            # 仍返回 None，确认失败后的会话一句也答不了。
            cascade.resolved = False
            await _apply_generic_fallback(cascade)
        return

    if decision.elder_id is None or decision.source == "generic":
        # 服务层在候选不一致时会降级 generic；我们已前置校验，走到这里说明数据在
        # 校验与写库之间变了（如设备被解绑）。按服务层结论处理，不再自作主张。
        # 服务层已经把 provisional 结束并开了新的 generic thread，必须切到新 id。
        cascade.source = "generic"
        cascade.thread_id = decision.thread_id
        _emit_result(cascade, source="generic", elder_id=None)
        _emit_greeting(cascade, generic=True)
        logger.warning("identity_confirm_degraded", extra=_log_extra(cascade))
        return

    cascade.thread_id = decision.thread_id
    # 开场白取人设值（W5 · G-D9）：先读再发，`_emit_greeting` 保持同步。
    await _resolve_greeting(cascade, decision.elder_id, sess)
    _emit_result(cascade, source="confirm", elder_id=str(decision.elder_id))
    _emit_greeting(cascade, generic=False)
    logger.info(
        "identity_confirmed",
        extra=_log_extra(cascade, thread_id=decision.thread_id, elder_id=str(decision.elder_id)),
    )


def _ignore_confirm(
    conn: ConnectionContext,
    *,
    reason: str,
    cascade: _Cascade | None = None,
    **fields: object,
) -> None:
    """忽略一次 confirm：warn 日志 + 计数器，不回执、不关连接（C5 裁决 ③）。"""
    metrics.counter("identity_confirm_received", result="ignored", reason=reason)
    logger.warning(
        "identity_confirm_ignored",
        extra={
            "trace_id": cascade.trace_id if cascade else conn.trace_id,
            "device_id": conn.device_id or "-",
            "reason": reason,
            **fields,
        },
    )


# ------------------------------------------------------------------ 入口 ③：30s 超时


async def _identity_timeout(cascade: _Cascade, ask_id: str) -> None:
    """30s 到点：落 generic thread 并通知前端（C5 裁决 ②）。"""
    try:
        await asyncio.sleep(IDENTITY_ASK_TIMEOUT_S)
    except asyncio.CancelledError:
        # 会话关闭 / 用户已确认 → 正常退出，不写库
        raise

    async with cascade.lock:
        if (
            cascade.resolved and not cascade.recheck_pending and not cascade.confirm_pending
        ) or cascade.ask_id != ask_id:
            return
        # 复检弹层超时：`resolved` 在首轮就已是 True，这里要把它落成 generic，
        # 后续句不再当本人处理（R-B：宁可降级，不冒认）。
        # 首句 provisional 超时：thread 已经在库里，直接沿用它落定，不再开第二条。
        provisional_id = cascade.provisional_thread_id
        cascade.recheck_pending = False
        cascade.confirm_pending = False
        cascade.provisional_thread_id = None
        cascade.resolved = True
        cascade.source = "generic"

    if provisional_id is not None:
        cascade.thread_id = provisional_id
        _emit_result(cascade, source="generic", elder_id=None)
        _emit_greeting(cascade, generic=True)
        logger.info(
            "identity_timeout_generic_reused",
            extra=_log_extra(cascade, ask_id=ask_id, thread_id=provisional_id),
        )
        return

    decision: IdentityDecision | None = None
    try:
        async with _session_scope(cascade.session) as sess:
            decision = await identity_service.open_thread_as_generic_on_timeout(
                sess, device_id=UUID(cascade.device_id)
            )
    except Exception:
        # 写库失败也要把 generic 发出去：否则前端永远停在「请确认是您吗」，
        # 而这台设备其实已经没人能确认了。
        logger.exception("identity_timeout_thread_failed", extra=_log_extra(cascade))

    cascade.thread_id = decision.thread_id if decision is not None else None
    _emit_result(cascade, source="generic", elder_id=None)
    _emit_greeting(cascade, generic=True)
    logger.info(
        "identity_timeout_generic",
        extra=_log_extra(cascade, ask_id=ask_id, thread_id=cascade.thread_id),
    )


async def _cancel_timeout(cascade: _Cascade) -> None:
    """取消定时器并等它退掉（幂等）。confirm 到达后立刻调，避免两路同时写 thread。

    P2-F（审计 2026-11-07）：原实现只 `task.cancel()` 不 await，与
    `cancel_identity_cascade` 的口径不一致 —— 协程可能已经越过 `sleep` 正在写库，
    这时 `handle_identity_confirm` 会并发写同一设备的 second thread。统一成
    `await gather(...)`：等它真正退出（或确认它已写完库），再继续往下写。
    用 `return_exceptions=True` 收下子任务的取消/异常，但不吞调用方自己的取消信号。
    """
    task = cascade.timeout_task
    cascade.timeout_task = None
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ------------------------------------------------------------------ 入口 ④：连接关闭


async def cancel_identity_cascade(
    device_id: str | None, *, conn: ConnectionContext | None = None
) -> None:
    """连接关闭时清理：移除状态 + 取消定时器（幂等，可在任何阶段调用）。

    **必须由 `signaling` 的 finally 调用**：定时器是会话的延伸，会话没了它就只是
    一个 30s 后必然写错库的孤儿任务。

    `conn` 是**归属守卫**（P1，审计 2026-11-07）：同一台设备重连后，`_cascades`
    里放的已经是新连接那条级联，而旧连接的 finally 稍后才跑。按 device_id 无条件
    pop 会把新连接的级联和 30s 定时器一起删掉 —— 新连接随后点「是我」落
    `no_cascade`，被静默忽略且拿不到 generic，会话卡在确认页。所以：
    - `conn` 给定时，只有 `cascade.owner is conn` 才清（连接自己的账自己销）；
    - `conn` 为 None 表示无条件清（仅供 `start_identity_cascade` 的重连 supersede）。
    """
    if device_id is None:
        return

    cascade = _cascades.get(device_id)
    if cascade is None:
        return
    if conn is not None and cascade.owner is not None and cascade.owner is not conn:
        # 新连接已接管：这里的 finally 属于旧连接，动它就是删别人的定时器
        logger.info(
            "identity_cascade_cancel_skipped",
            extra={"trace_id": cascade.trace_id, "device_id": device_id, "reason": "not_owner"},
        )
        return

    _cascades.pop(device_id, None)
    task = cascade.timeout_task
    cascade.timeout_task = None
    provisional_id = cascade.provisional_thread_id
    cascade.provisional_thread_id = None
    if task is not None and not task.done():
        task.cancel()
        # 等它真的退掉（否则测试会看到「用例结束仍在跑的任务」，生产里可能在上一条连接
        # 关闭后还写一行 thread）。用 gather(return_exceptions=True)：**子任务**的取消
        # 与异常都被收下，但外层自己被取消时照常抛出，不吞调用方的取消信号。
        await asyncio.gather(task, return_exceptions=True)

    if provisional_id is not None:
        # 没等到确认就断线：provisional thread 只服务于那一次待确认的首句，
        # 留着就会变成一条永不结束的 generic 会话（不被复用，但会一直堆积）。
        try:
            async with _session_scope(cascade.session) as sess:
                await identity_service.end_open_thread(sess, thread_id=provisional_id)
        except Exception:
            logger.warning(
                "identity_provisional_thread_close_failed",
                exc_info=True,
                extra=_log_extra(cascade, thread_id=provisional_id),
            )


__all__ = [
    "DEFAULT_GREETING_TEXT",
    "GENERIC_GREETING_TEXT",
    "IDENTITY_ASK_TIMEOUT_S",
    "active_cascade",
    "cancel_identity_cascade",
    "handle_identity_confirm",
    "start_identity_cascade",
]
