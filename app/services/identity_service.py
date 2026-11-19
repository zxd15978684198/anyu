"""迭代 4 T8 · 身份级联编排 — identity_service.py（闸门①落点）。

职责（沿 task-01 T8 验收矩阵）：
    1. 候选集恒为设备绑定长者 1v1（裁决 v2.3）
    2. 每会话首轮强制校验：有音频 → voiceprint_service.cosine_compare → voice 命中 / 主动确认
    3. 容器不可用 → 主动确认（reason=vp_unavailable）
    4. 无音频首轮 → 直接主动确认（reason=no_audio）
    5. conversation_threads 闸门①：身份确定后事务内 INSERT，thread_id 服务端生成
    6. 返回结构化 IdentityDecision，**不写 WS 消息发送**（A2 signaling.py 写作域）

分层硬约束（AGENTS §2）：
    - 本模块只 import voiceprint_service（比对）+ ORM（唯一写库层）
    - 不 import FastAPI（REST 层）、不 import signaling.py（A2 写作域）、不直接 httpx
    - 所有 service 异常内部消化 → 只产出结构化决策（reason 枚举 / identity_source）
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import (
    STAFF_CARD_SCOPES,
    TYP_STAFF_CARD,
    TokenExpiredError,
    TokenInvalidError,
    TokenPayloadError,
    access_ttl_seconds,
    decode_token,
    normalize_scope,
    sha256_hex,
    sign_staff_card,
)
from app.models.audit import AuditLog
from app.models.conversation import ConversationThread
from app.models.organization import Device
from app.models.staff import StaffAccount, StaffToken
from app.services import voiceprint_service as vs

logger = logging.getLogger("identity_service")

IdentitySource = Literal["device", "voice", "confirm", "generic"]
ConfirmReason = Literal[
    "low_confidence", "vp_unavailable", "no_audio", "elder_unbound", "confirm_pending"
]


# -----------------------------------------------------------
# 输出：身份决策（消费方：A2 对话主链 / REST 层）
# -----------------------------------------------------------


@dataclass
class IdentityDecision:
    """一次身份校验的结构化结果。

    **三类终态**：
      - voice/device/confirm → elder_id 已确定（会话可继续走记忆/健康/用药）
      - generic → elder_id=None，下游必须跳过所有 elder 绑定功能
    """

    source: IdentitySource
    elder_id: uuid.UUID | None
    thread_id: str | None  # 终态时必产生（闸门① INSERT 后）；需 confirm 时为 None
    reason: ConfirmReason | None = None  # source=confirm/generic 时填；终态不填
    confidence: float | None = None  # source=voice 时 cosine 值；其他终态 None
    started_at: datetime | None = None  # thread.started_at（供下游日志/trace 用）

    # 便捷属性
    @property
    def confirmed(self) -> bool:
        """闸门①已落库且 elder_id 非空（voice/device/confirm 终态）。

        用 thread_id 是否存在而非 source 判断：主动确认阶段（thread_id=None）
        虽然 source=confirm 且 elder_id 已拿到候选，但 thread 没落库前下游不得用
        elder_id 做任何写操作（AGENTS §4.9 身份只从服务端注入 + 闸门①约束）。
        """
        return self.thread_id is not None and self.elder_id is not None

    @property
    def needs_confirm(self) -> bool:
        """是否需要向前端发 identity_ask 等待 identity_confirm。

        双条件：source=confirm（处于主动确认分支）AND thread_id=None（闸门①
        未落地，还在等用户点「是我」）。open_thread_from_confirm 返回的终态 confirm
        source 也是 confirm，但 thread_id 已存在 → needs_confirm=False（不再需要 ask）。
        """
        return self.source == "confirm" and self.thread_id is None


# -----------------------------------------------------------
# 输入：候选集 + 首轮校验
# -----------------------------------------------------------


async def resolve_device_elder(session: AsyncSession, device_id: uuid.UUID) -> uuid.UUID | None:
    """候选集获取：设备 → 绑定长者（1v1，裁决 v2.3）。None 表示未绑定。"""
    row = (
        await session.execute(select(Device.elder_id).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if row is None:
        # row 可能是 None（elder_id 为 NULL）或 Device 行不存在
        # scalar_one_or_none() 返回 None 在行不存在 + 列为 NULL 两种情况 —— 但 DB 不会让
        # device_id 找不到就跑过来（voiceprint 注册路径已在 REST 层拦 device_unbound），
        # 这里兜底防行被删除的 race。
        return None
    return row  # scalar_one_or_none() 已经是 Device.elder_id 列值（UUID | None）


# -----------------------------------------------------------
# thread_id 生成 + 闸门① INSERT
# -----------------------------------------------------------


# -----------------------------------------------------------
# thread_id 形态（ADM-06）：服务端可自校验，仍保持 32 位 hex
# -----------------------------------------------------------

#: thread_id 总长度：前 16 个 hex 是随机段，后 16 个 hex 是「随机段 + 设备」的 HMAC 截断。
#:
#: **为什么不加 `vt1.` 之类的前缀**：`conversation_events.thread_id` 是
#: `uuid.UUID NOT NULL`，话音链路的 thread_id 会被 `text_turn_service.thread_id_from_text`
#: 当 UUID 解析（`uuid.UUID("vt1.…")` 直接 ValueError → 事件流整条静默丢写）；
#: `tests/test_identity_confirm_api.py:212` 也断言 `len(thread_id) == 32`。
#: 保持「32 位 hex」= 保持 `uuid.UUID(hex)` 可解析 + 长度契约不变，只把内部结构换成
#: 「随机段 + 设备绑定的 MAC」——判据从「无从判断」升级为「零 IO 可验」。
THREAD_ID_HEX_LEN: Final[int] = 16
THREAD_ID_MAC_HEX_LEN: Final[int] = 16
THREAD_ID_TOTAL_LEN: Final[int] = THREAD_ID_HEX_LEN + THREAD_ID_MAC_HEX_LEN

#: 存量形态①：打字/降级语音链路的线程号 `device:<device_uuid>`
#: （`app/services/text_turn_service.py:51` 的 `THREAD_TEMPLATE`）。它**不在**
#: `conversation_threads` 里，是纯字符串线程号，只能按字符串绑定判定。
LEGACY_DEVICE_THREAD_PREFIX: Final[str] = "device:"

#: 拒绝访问时写进 `audit_logs.action` 的取值（与 `delete_voiceprint` 等同一命名空间）。
THREAD_ACCESS_DENIED_ACTION: Final[str] = "thread_access_denied"

#: 会话中途声纹复检不通过（攻防计划 §4.6-S1 / B2-06）写进 `audit_logs.action` 的取值。
#: 与 `thread_access_denied` 同一命名空间：两者都是「身份侧拒绝留痕」，
#: 区别只在触发点（前者是 thread 归属校验，后者是逐句静默复检）。
SESSION_RECHECK_DENIED_ACTION: Final[str] = "session.identity.recheck_denied"

#: 存量 thread_id 未必是合法 UUID 串（`device:<uuid>` 形态），而 `audit_logs.resource_id`
#: 是 `uuid NOT NULL`：非 UUID 形态用 uuid5 派生一个**确定性**占位，真实字符串另存 detail。
_THREAD_AUDIT_NAMESPACE: Final[uuid.UUID] = uuid.UUID("8f14e45f-ceea-467a-9b3c-1d3f4e5a6b7c")

#: 32 位小写 hex（新旧两种形态都长这样，只能靠 MAC 是否成立来区分「本设备签发」）。
_HEX32_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{32}\Z")

ThreadShape = Literal["signed", "legacy_unverified", "legacy_device", "invalid"]


@dataclass(frozen=True, slots=True)
class ThreadToken:
    """`thread_id` 的形态解析结果（**只描述形态，不做鉴权**）。

    `signed` 与 `legacy_unverified` 的串**长得一模一样**（都是 32 位 hex）：后者是本
    改动之前落库的 uuid4 hex，没有 MAC，只能交给 DB 复核 —— 这正是「存量兼容」的落点，
    也是不能一刀切拒绝的原因（拒绝等于把现有会话全踢掉）。
    """

    shape: ThreadShape
    thread_id: str
    #: 仅 `legacy_device` 形态有值（`device:<这里>`）。
    device_hint: str | None = None


def _thread_signing_key() -> bytes:
    """派生 thread_id 的 HMAC 密钥。

    复用 `settings.jwt_secret`（服务端唯一长期密钥），**不新增配置项**：ADM-06 是闸门
    加固，不该把部署面一起扩大（`app/core/config.py` 不在本次写作用域）。

    `jwt_secret` 为空只会在本地/测试出现，此时退化成固定占位：它只影响「同一台机器上
    的 thread_id 能否被离线伪造」，不放宽任何一种跨设备/跨长者的判定（那一路由 DB 复核
    兜底，见 `validate_thread_access`）。
    """
    secret = get_settings().jwt_secret.strip()
    return secret.encode("utf-8") if secret else b"anyu-thread-token-local-only"


def _thread_mac(random_hex: str, device_id: uuid.UUID | None) -> str:
    """`random_hex + 设备` 的 HMAC 截断（16 hex = 64 bit）。

    绑定设备而不是绑定长者：调用方（复采入口）手里一定有自己的 `device_id`，
    却未必在解析这一刻就拿到 `elder_id`；长者的绑定关系由 `conversation_threads`
    那一行权威表达（跨长者 = 行里的 `elder_id` 与调用方声称的不一致）。
    """
    device_part = device_id.hex if device_id is not None else "-"
    digest = hmac.new(
        _thread_signing_key(),
        f"{random_hex}:{device_part}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest[:THREAD_ID_MAC_HEX_LEN]


def _new_thread_id(*, device_id: uuid.UUID | None = None) -> str:
    """服务端生成 thread_id：`<16 hex 随机><16 hex MAC(随机, 设备)>`。

    - **仍可当 UUID 解析**（32 位 hex），事件流/审计/日志的既有假设一个都不用改；
    - **对新号零 IO 可验**：拿到 `(thread_id, device_id)` 就能判定「是不是本设备签发的」，
      不必先查库（伪造的随机 hex 直接在这一步被挡掉）；
    - **对旧号不设门槛**：旧号 MAC 必然不成立，但走 DB 复核照常通过（存量兼容）。
    """
    random_hex = secrets.token_hex(THREAD_ID_HEX_LEN // 2)
    return f"{random_hex}{_thread_mac(random_hex, device_id)}"


def parse_thread_token(thread_id: str | None, *, device_id: uuid.UUID | None = None) -> ThreadToken:
    """把 `thread_id` 归类成四种形态之一（纯函数，无 IO）。

    传了 `device_id` 且 MAC 成立 → `signed`（本设备签发，零 IO 可证）；
    32 位 hex 但 MAC 不成立 → `legacy_unverified`（改动前落库的 uuid4 号，
    **不得因此拒绝**，交给 DB 复核）；`device:` 前缀 → `legacy_device`；其余 → `invalid`。
    """
    raw = (thread_id or "").strip()
    if not raw:
        return ThreadToken(shape="invalid", thread_id="")
    if raw.startswith(LEGACY_DEVICE_THREAD_PREFIX):
        return ThreadToken(
            shape="legacy_device",
            thread_id=raw,
            device_hint=raw[len(LEGACY_DEVICE_THREAD_PREFIX) :],
        )
    if not _HEX32_RE.match(raw):
        # 带连字符的 UUID、任意长度字符串、大写 hex：一律不认。宁可 403，也不猜。
        return ThreadToken(shape="invalid", thread_id=raw)
    if device_id is not None and thread_token_matches_device(raw, device_id=device_id):
        return ThreadToken(shape="signed", thread_id=raw)
    return ThreadToken(shape="legacy_unverified", thread_id=raw)


def thread_token_matches_device(thread_id: str | None, *, device_id: uuid.UUID) -> bool:
    """`thread_id` 的 MAC 是否绑定到这台设备（纯函数，零 IO）。

    只对**新号**成立；旧 uuid4 号恒为 `False` —— 调用方**不得**因此拒绝旧号，
    要落到 `validate_thread_access` 的 DB 复核上。
    """
    raw = (thread_id or "").strip()
    if not _HEX32_RE.match(raw):
        return False
    random_hex, mac_hex = raw[:THREAD_ID_HEX_LEN], raw[THREAD_ID_HEX_LEN:]
    return hmac.compare_digest(mac_hex, _thread_mac(random_hex, device_id))


async def _open_thread(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None,
    identity_source: IdentitySource,
    confidence: float | None,
) -> ConversationThread:
    """事务内 INSERT conversation_threads（闸门①唯一落点）。"""
    thread_id = _new_thread_id(device_id=device_id)
    row = ConversationThread(
        thread_id=thread_id,
        elder_id=elder_id,
        device_id=device_id,
        identity_source=identity_source,
        identity_confidence=(Decimal(str(confidence)) if confidence is not None else None),
    )
    session.add(row)
    await session.commit()
    logger.info(
        "identity thread opened",
        extra={
            "thread_id": thread_id,
            "device_id": str(device_id),
            "elder_id": str(elder_id) if elder_id else None,
            "source": identity_source,
            "confidence": confidence,
        },
    )
    return row


async def _close_active_thread(session: AsyncSession, *, device_id: uuid.UUID) -> None:
    """同一设备当前未结束 thread 回填 ended_at（换人/退出发起新 thread 前调）。"""

    await session.execute(
        update(ConversationThread)
        .where(
            ConversationThread.device_id == device_id,
            ConversationThread.ended_at.is_(None),
        )
        .values(ended_at=datetime.now(UTC))
    )
    await session.commit()


# -----------------------------------------------------------
# 闸门①只读查询：重连复用（P1-4）
# -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenThread:
    """未结束 thread 的最小只读视图。

    不复用 ORM 行：调用方只需要「这台设备现在是哪位长者、按什么来源确认的」，
    拿到 ORM 行迟早有人顺手改字段绕过写库层。
    """

    thread_id: str
    elder_id: uuid.UUID | None
    identity_source: IdentitySource
    # 会话起点（UTC）。调用方用它判「这条 thread 还算不算本次会话」：
    # 复用必须有有效期，否则一次确认会被永久回放成「已确认」（2026-11-10 裁决 A）。
    started_at: datetime


async def find_open_thread(session: AsyncSession, *, device_id: uuid.UUID) -> OpenThread | None:
    """取该设备最近一条未结束（`ended_at IS NULL`）的会话 thread；没有则 None。

    为什么需要它：WS 每次重连都会重跑一遍首轮身份判定，而「刚上线、还没说话」
    这一轮拿不到音频，服务层只能给 `confirm/no_audio` —— 于是断线重连（退避
    1/2/4/8/15s，最多 5 次）每次都重弹「请问是哪位在说话」，长者刚点完「是我」
    又要再点一次。库里这条 open thread 的 `elder_id` 就是答案，复用即可。

    本函数**只读**：不建 thread、不改 `ended_at`、不 commit。
    """
    row = (
        await session.execute(
            select(ConversationThread)
            .where(
                ConversationThread.device_id == device_id,
                ConversationThread.ended_at.is_(None),
            )
            .order_by(ConversationThread.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return OpenThread(
        thread_id=row.thread_id,
        elder_id=row.elder_id,
        identity_source=cast(IdentitySource, row.identity_source),
        started_at=row.started_at,
    )


async def end_open_thread(session: AsyncSession, *, thread_id: str) -> bool:
    """结束指定的 open thread（换人 / 设备重绑时由媒体层调用）。

    只认 `thread_id` 且只改 `ended_at`：调用方已经判定这段会话不再代表当前设备，
    这里不做二次判定。返回 True 表示真的改了一行（重复调用幂等返回 False）。
    """
    result = await session.execute(
        update(ConversationThread)
        .where(
            ConversationThread.thread_id == thread_id,
            ConversationThread.ended_at.is_(None),
        )
        .values(ended_at=datetime.now(UTC))
    )
    await session.commit()
    changed = getattr(result, "rowcount", 0) or 0
    if changed:
        logger.info("identity thread ended", extra={"thread_id": thread_id})
    return bool(changed)


# -----------------------------------------------------------
# 公开入口：首轮身份校验
# -----------------------------------------------------------


async def verify_session_identity(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    audio_bytes: bytes | None = None,
) -> IdentityDecision:
    """会话首轮：强制校验身份（裁决 v2.3）。

    **前置条件**：设备已从 JWT 解出 device_id；调用方（A2 对话主链）拿到 decision 后
    决定是否发 identity_ask、如何组装 initial greeting、以及 thread 闸门已否已就位。

    返回三类结果：
      - **终态**（source=voice/device）：elder_id 已定，thread_id 已生成
      - **终态**（source=generic）：elder_id=None，thread_id 已生成（下游跳过 elder 绑定功能）
      - **待确认**（source=confirm + reason）：需要发 identity_ask 等用户 identity_confirm；
        此时 thread 不 INSERT（等 confirm 到达再调 open_thread_from_confirm）
    """
    # 1. 候选集：恒 1 位（裁决 v2.3）
    elder_id = await resolve_device_elder(session, device_id)

    if elder_id is None:
        # 设备未绑长者 → generic（不挑时间点，直接降级）
        row = await _open_thread(
            session,
            device_id=device_id,
            elder_id=None,
            identity_source="generic",
            confidence=None,
        )
        return IdentityDecision(
            source="generic",
            elder_id=None,
            thread_id=row.thread_id,
            reason="elder_unbound",
            started_at=row.started_at,
        )

    # 2. 有音频 → 走声纹比对
    if audio_bytes is not None and len(audio_bytes) > 0:
        try:
            match = await vs.cosine_compare_against_elder(session, elder_id, audio_bytes)
        except vs.VoiceprintExtractUnavailable:
            # 推理容器不可用 → 主动确认（C5 reason=vp_unavailable）
            logger.warning("vp 容器不可用，降级主动确认（device=%s）", device_id)
            return IdentityDecision(
                source="confirm",
                elder_id=elder_id,
                thread_id=None,
                reason="vp_unavailable",
            )

        if match.matched:
            # 命中 → 终态 voice
            row = await _open_thread(
                session,
                device_id=device_id,
                elder_id=elder_id,
                identity_source="voice",
                confidence=match.score,
            )
            return IdentityDecision(
                source="voice",
                elder_id=elder_id,
                thread_id=row.thread_id,
                confidence=match.score,
                started_at=row.started_at,
            )
        else:
            # 低置信 → 主动确认（C5 reason=low_confidence）
            logger.info(
                "vp 低置信，请求确认（score=%.4f < %.2f, elder=%s）",
                match.score,
                get_settings().voiceprint_similarity_threshold,
                elder_id,
            )
            return IdentityDecision(
                source="confirm",
                elder_id=elder_id,
                thread_id=None,
                reason="low_confidence",
            )

    # 3. 无音频首轮（pad 刚上线、用户还没开口）→ 主动确认（reason=no_audio）
    logger.info("无音频首轮，请求确认（device=%s, elder=%s）", device_id, elder_id)
    return IdentityDecision(
        source="confirm",
        elder_id=elder_id,
        thread_id=None,
        reason="no_audio",
    )


# -----------------------------------------------------------
# 公开入口：主动确认到达后终态落 thread
# -----------------------------------------------------------


async def open_thread_from_confirm(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    confirmed_elder_id: uuid.UUID,
    candidate_elder_id: uuid.UUID,  # 候选集恒 1 位（identity_ask.candidates[0].elder_id）
    replace_thread_id: str | None = None,
) -> IdentityDecision:
    """用户点了「是我」大按钮（C5 identity_confirm 到达）→ 闸门① INSERT。

    `replace_thread_id` 是首句通用模式开出的 provisional thread：确认成功时在同一事务里
    先结束它再开正式 thread，避免同设备同时挂两条 open thread；`None` 表示没有临时线程。

    关键判定（C5 开放点裁决 3）：**confirm 的 elder_id 必须等于候选集那 1 位**，否则
    一律按 generic 处理（不入库 elder_id，也不报错）。这是前端伪造 elder_id 的防线。
    """
    if replace_thread_id is not None:
        await session.execute(
            update(ConversationThread)
            .where(ConversationThread.thread_id == replace_thread_id)
            .values(ended_at=datetime.now(UTC))
        )
        # 不单独 commit：紧接着 `_open_thread` 的 commit 会把两条一起落库。
    if str(confirmed_elder_id) != str(candidate_elder_id):
        logger.warning(
            "identity_confirm elder_id 不匹配候选集，降级 generic（confirm=%s, expected=%s）",
            confirmed_elder_id,
            candidate_elder_id,
        )
        row = await _open_thread(
            session,
            device_id=device_id,
            elder_id=None,
            identity_source="generic",
            confidence=None,
        )
        return IdentityDecision(
            source="generic",
            elder_id=None,
            thread_id=row.thread_id,
            reason="elder_unbound",
            started_at=row.started_at,
        )

    row = await _open_thread(
        session,
        device_id=device_id,
        elder_id=confirmed_elder_id,
        identity_source="confirm",
        confidence=None,
    )
    return IdentityDecision(
        source="confirm",
        elder_id=confirmed_elder_id,
        thread_id=row.thread_id,
        started_at=row.started_at,
    )


async def open_thread_as_generic_on_timeout(
    session: AsyncSession, *, device_id: uuid.UUID
) -> IdentityDecision:
    """主动确认 30s 超时 → generic（C5 开放点裁决 2）。"""
    row = await _open_thread(
        session,
        device_id=device_id,
        elder_id=None,
        identity_source="generic",
        confidence=None,
    )
    return IdentityDecision(
        source="generic",
        elder_id=None,
        thread_id=row.thread_id,
        reason="elder_unbound",
        started_at=row.started_at,
    )


async def open_thread_as_generic_pending_confirm(
    session: AsyncSession, *, device_id: uuid.UUID
) -> IdentityDecision:
    """首句尚未确认身份 → 先开一条 provisional generic thread。

    目的：PRD FR-VPR-03 要求身份未定时进入通用模式，而不是把这句话丢掉。
    这条 thread 只保证本轮能按 generic 编排（不读个人记忆、不写健康/用药），
    用户点「是我」后由 `open_thread_from_confirm(replace_thread_id=...)` 原子替换。
    """
    row = await _open_thread(
        session,
        device_id=device_id,
        elder_id=None,
        identity_source="generic",
        confidence=None,
    )
    return IdentityDecision(
        source="generic",
        elder_id=None,
        thread_id=row.thread_id,
        reason="confirm_pending",
        started_at=row.started_at,
    )


# -----------------------------------------------------------
# 公开入口：静默复检（会话内后续 utterance）
# -----------------------------------------------------------


RecheckStatus = Literal["skipped", "same_person", "suspected_replacement", "switched"]


@dataclass(frozen=True, slots=True)
class SessionRecheckResult:
    """一次会话中途声纹复检的结构化结论（消费方：media_bridge）。

    `skipped` 覆盖所有「本次不具备复检条件」的情形（thread 不存在 / generic 未绑定 /
    vp 容器不可用 / 没取到音频）。调用方对 `skipped` 与 `same_person` 一视同仁：放行。
    """

    status: RecheckStatus
    score: float | None = None
    threshold: float = 0.0
    elder_id: uuid.UUID | None = None
    decision: IdentityDecision | None = None


def classify_session_voiceprint(
    score: float,
    *,
    threshold: float | None = None,
) -> Literal["same_person", "suspected_replacement"]:
    """把一次 cosine 分数判成「同一人 / 疑似换人」。

    纯函数，便于对阈值边界做无库断言。取数口径与首轮校验、`voiceprint_service`
    完全一致：`score >= threshold` 视为命中（AGENTS §7 声纹阈值 0.55）；缺省阈值
    从 settings 读，不在本模块另立常量。
    """
    effective = get_settings().voiceprint_similarity_threshold if threshold is None else threshold
    return "same_person" if score >= effective else "suspected_replacement"


async def recheck_session_identity(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    current_thread_id: str,
    audio_bytes: bytes,
) -> SessionRecheckResult:
    """每 utterance 后复检一次（裁决 v2.3 静默复检）。

    **只在能下结论时表态**：
      - `skipped` → 没条件复检（thread 不存在 / generic / vp 容器不可用），放行
      - `same_person` → 同一人，放行
      - `suspected_replacement` → 低于阈值（分数见 `score`）：**不关 thread**，
        由调用方写审计 + 走 identity_ask 降级（攻防计划 §4.6-S1 / B2-06）
      - `switched` → 命中另一个已绑定长者，携带新 thread 的 `decision`，调用方切 thread

    与旧实现（返回 `IdentityDecision | None`）的差别：低置信不再折叠进 `None`。
    旧写法把「没复检」「同一人」「疑似换人」混成同一个返回值，调用方无从区分，
    于是审计与降级话术永远触发不了 —— 这正是本任务要修的死代码。
    """
    threshold = get_settings().voiceprint_similarity_threshold

    # 1. 找到当前 thread
    current_thread = (
        await session.execute(
            select(ConversationThread).where(
                ConversationThread.thread_id == current_thread_id,
                ConversationThread.device_id == device_id,
                ConversationThread.ended_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if current_thread is None:
        logger.warning(
            "recheck：当前 thread %s 不存在或已结束（device=%s）",
            current_thread_id,
            device_id,
        )
        return SessionRecheckResult(status="skipped", threshold=threshold)

    # 2. 比对新音频
    elder_id = await resolve_device_elder(session, device_id)
    if elder_id is None or current_thread.elder_id is None:
        return SessionRecheckResult(status="skipped", threshold=threshold)

    try:
        match = await vs.cosine_compare_against_elder(session, elder_id, audio_bytes)
    except vs.VoiceprintExtractUnavailable:
        logger.warning("recheck：vp 容器不可用，跳过静默复检")
        return SessionRecheckResult(status="skipped", threshold=threshold)

    effective_threshold = float(match.threshold or threshold)
    if not match.enrolled:
        # 没注册过声纹 ≠ 比对失败。score 恒为 0，若照常判定会每次都说「疑似换人」，
        # 确认过身份的长者每句话都要再点一次「是我」。
        logger.info("recheck：elder=%s 尚未注册声纹，跳过静默复检", elder_id)
        return SessionRecheckResult(status="skipped", threshold=effective_threshold)
    verdict = classify_session_voiceprint(float(match.score), threshold=effective_threshold)
    if verdict == "suspected_replacement":
        logger.info(
            "recheck：声纹低置信（score=%.4f < threshold=%.2f），疑似换人",
            match.score,
            effective_threshold,
        )
        return SessionRecheckResult(
            status="suspected_replacement",
            score=float(match.score),
            threshold=effective_threshold,
            elder_id=elder_id,
        )

    # 3. 命中同一人 → 不打断
    if match.elder_id is None or str(match.elder_id) == str(current_thread.elder_id):
        return SessionRecheckResult(
            status="same_person",
            score=float(match.score),
            threshold=effective_threshold,
            elder_id=match.elder_id or elder_id,
        )

    # 4. 换人 → 结束旧 thread + 开新 thread（voice）
    await _close_active_thread(session, device_id=device_id)
    logger.info(
        "recheck：身份变化，切 thread（old_elder=%s → new_elder=%s）",
        current_thread.elder_id,
        match.elder_id,
    )
    new_row = await _open_thread(
        session,
        device_id=device_id,
        elder_id=match.elder_id,
        identity_source="voice",
        confidence=match.score,
    )
    return SessionRecheckResult(
        status="switched",
        score=float(match.score),
        threshold=effective_threshold,
        elder_id=match.elder_id,
        decision=IdentityDecision(
            source="voice",
            elder_id=match.elder_id,
            thread_id=new_row.thread_id,
            confidence=match.score,
            started_at=new_row.started_at,
        ),
    )


# -----------------------------------------------------------
# 公开入口：thread 合法性校验（REST 层拦截伪造的守门函数）
# -----------------------------------------------------------


def _thread_resource_id(thread_id: str) -> uuid.UUID:
    """审计行的 `resource_id`：合法 UUID 直接用，否则 uuid5 派生确定性占位。

    `audit_logs.resource_id` 是 `uuid NOT NULL`，而 `device:<uuid>` 形态的 thread_id
    不是 UUID（`app/models/audit.py:64-66`）；真实字符串另存 `detail.thread_id`。
    """
    try:
        return uuid.UUID(thread_id)
    except (AttributeError, TypeError, ValueError):
        return uuid.uuid5(_THREAD_AUDIT_NAMESPACE, thread_id or "-")


async def _audit_thread_denial(
    *,
    thread_id: str,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None,
    reason: str,
) -> None:
    """把一次 thread 访问拒绝写进 `audit_logs`（**独立会话 + 立即提交，永不抛**）。

    为什么不用调用方的 session：拒绝之后 REST 层要抛 403，请求事务回滚，同事务写的
    审计行会跟着消失 —— 那就成了「拒了但没留痕」。独立会话把这一笔钉死。
    """
    logger.warning(
        "thread access denied",
        extra={"reason": reason, "device_id": str(device_id), "thread_id": thread_id},
    )
    try:
        from app.db import session_factory

        async with session_factory() as audit_session:
            audit_session.add(
                AuditLog(
                    actor_type="device",
                    actor_id=device_id,
                    action=THREAD_ACCESS_DENIED_ACTION,
                    resource_type="conversation_threads",
                    resource_id=_thread_resource_id(thread_id),
                    detail={
                        "reason": reason,
                        "thread_id": thread_id,
                        "elder_id": str(elder_id) if elder_id is not None else None,
                    },
                )
            )
            await audit_session.commit()
    except Exception as exc:
        # 审计写不进去也不许把 403 变成 500：拒绝已经成立，只是少了一行记录。
        logger.warning(
            "thread access audit failed",
            extra={"error_type": type(exc).__name__, "reason": reason},
        )


async def audit_session_recheck_denied(
    *,
    thread_id: str,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None,
    score: float | None,
    threshold: float,
) -> None:
    """把一次「会话中途复检判定疑似换人」写进 `audit_logs`（独立会话，**永不抛**）。

    与 `_audit_thread_denial` 同一套写法：拒绝/降级已经发生，请求事务可能随后回滚，
    所以审计必须用独立会话钉死；写不进去也只 warn，不许把降级话术变成 500。

    `action` 固定为 `SESSION_RECHECK_DENIED_ACTION` —— 攻防用例与告警按此字符串检索。
    """
    logger.warning(
        "session identity recheck denied",
        extra={
            "device_id": str(device_id),
            "thread_id": thread_id,
            "score": score,
            "threshold": threshold,
        },
    )
    try:
        from app.db import session_factory

        async with session_factory() as audit_session:
            audit_session.add(
                AuditLog(
                    actor_type="device",
                    actor_id=device_id,
                    action=SESSION_RECHECK_DENIED_ACTION,
                    resource_type="conversation_threads",
                    resource_id=_thread_resource_id(thread_id),
                    detail={
                        "reason": "voiceprint_low_confidence",
                        "thread_id": thread_id,
                        "elder_id": str(elder_id) if elder_id is not None else None,
                        "score": score,
                        "threshold": threshold,
                        "disposition": "identity_ask_degraded",
                    },
                )
            )
            await audit_session.commit()
    except Exception as exc:
        # 审计失败不改变处置结论：话术照降级，只是少一行记录。
        logger.warning(
            "session recheck audit failed",
            extra={"error_type": type(exc).__name__, "thread_id": thread_id},
        )


async def validate_thread_access(
    session: AsyncSession,
    *,
    thread_id: str | None,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None = None,
) -> bool:
    """闸门①的访问判定：这串 `thread_id` 能不能被这台设备（这位长者）用？

    三级判定，任一级不过即拒绝并写审计：

    1. **形态**（零 IO）：`device:<uuid>` 与 32 位 hex 认；带连字符的 UUID、空串、
       任意长度串一律不认（宁可 403，也不猜）。
    2. **存量兼容**（硬要求，不做门槛）：`device:` 形态与本设备严格比串（它本来就不在
       `conversation_threads` 里，见 `text_turn_service.py:51`）；旧 uuid4 hex 的 MAC
       必然不成立，**不因此拒绝**，直接落到第 3 级 —— 否则现有会话会被整批踢掉。
    3. **DB 复核**（有 IO）：`conversation_threads` 存在 (thread_id, device_id) 行；
       传了 `elder_id` 时行里的 `elder_id` 必须一致（**跨长者一律拒**）。

    返回 `False` 即调用方映射 403。拒绝会写 `audit_logs(action='thread_access_denied')`。

    实际接线点：`app/api/dialogue.py::ensure_thread_access`（在 `dialogue_text` 里于
    `thread_id` 服务端派生之后、交给 `run_text_turn` 之前调用）——本函数只判定，
    403 由 API 层映射。注意**入口本身不接收客户端传入的 `thread_id`**（服务端按设备
    派生），所以这道闸是防御性的：正常链路走「存量兼容」分支，它兜的是 thread 归属
    异常/伪造/跨设备注入这些异常态，异常时一律 **fail-closed**（拒绝 + 写审计）。
    """
    token = parse_thread_token(thread_id, device_id=device_id)

    if token.shape == "invalid":
        await _audit_thread_denial(
            thread_id=token.thread_id,
            device_id=device_id,
            elder_id=elder_id,
            reason="malformed_thread_id",
        )
        return False

    if token.shape == "legacy_device":
        if token.thread_id == f"{LEGACY_DEVICE_THREAD_PREFIX}{device_id}":
            return True
        await _audit_thread_denial(
            thread_id=token.thread_id,
            device_id=device_id,
            elder_id=elder_id,
            reason="device_thread_owner_mismatch",
        )
        return False

    # signed（MAC 已证明本设备签发）与 legacy_unverified（旧 uuid4 号）都走 DB 复核：
    # 会话可能已经 ended、或被退房清理删掉，只凭签名放行等于把死会话复活。
    row = (
        await session.execute(
            select(ConversationThread.elder_id).where(
                ConversationThread.thread_id == token.thread_id,
                ConversationThread.device_id == device_id,
            )
        )
    ).first()
    if row is None:
        await _audit_thread_denial(
            thread_id=token.thread_id,
            device_id=device_id,
            elder_id=elder_id,
            reason="unknown_thread",
        )
        return False

    if elder_id is not None and row[0] is not None and row[0] != elder_id:
        await _audit_thread_denial(
            thread_id=token.thread_id,
            device_id=device_id,
            elder_id=elder_id,
            reason="cross_elder",
        )
        return False
    return True


async def validate_thread_belongs_to_device(
    session: AsyncSession,
    *,
    thread_id: str,
    device_id: uuid.UUID,
) -> bool:
    """判断某 thread 是否属于某 device（rest 层鉴权用）。

    **签名保持不变**（既有调用方与测试不受影响），内部转调 `validate_thread_access`：
    形态 → 存量兼容 → DB 复核 → 拒绝写审计，一条链只有一处判据，不养第二套。

    **thread_id 伪造**时返回 False（调用方映射为 403）。identity_service 不抛
    403，因为它不是 HTTP 层——REST 层统一处理（task-01 T8-6 验收矩阵）。
    """
    return await validate_thread_access(session, thread_id=thread_id, device_id=device_id)


# -----------------------------------------------------------
# 工牌令牌（FR-ADM-08）：签发 / 换短期 access / 越权审计落点
#
# 为什么签发与换取落在 identity_service（名字上像是「身份级联」的地盘）：
# 本卡写作用域里唯一的 service 就是本文件 + lifecycle_service，
# `auth_service`（设备换会话那一套）不在作用域内。工牌与设备会话是同一件事的
# 两个主体（受限令牌换短期会话），放这里至少保持「凭证 → 主体 → 会话」一条链在
# 同一层，而不是让 api 层直接写 staff_tokens（AGENTS §2 禁止）。
# -----------------------------------------------------------

#: 工牌签发成功写进 `audit_logs.action` 的取值。
STAFF_CARD_ISSUED_ACTION: Final[str] = "staff_card.issue"

#: 越权拒绝写进 `audit_logs.action` 的取值（`require_scopes` 的 403 路径）。
SCOPE_DENIED_ACTION: Final[str] = "scope_denied"

#: 工牌 TTL 缺省值 = 上限（86400s）。只在调用方不传时生效；
#: 上限本身仍由 `core/security._STAFF_CARD_MAX_TTL_SECONDS` 在签发时二次把关，
#: 这里不复制那个数字 —— 复制了就会出现「改了一处、另一处还是旧值」。
DEFAULT_STAFF_CARD_TTL_SECONDS: Final[int] = 86400


class StaffCardError(RuntimeError):
    """工牌链路业务异常基类（REST 层按子类映射状态码）。"""


class StaffCardNotFoundError(StaffCardError):
    """员工或设备不存在 → 404。"""


class StaffCardConflictError(StaffCardError):
    """员工已停用、设备已锁定 → 409。"""


class StaffCardValidationError(StaffCardError):
    """scope 表外取值、ttl 非法、scope 形态非法 → 422。"""


class StaffCardCredentialError(StaffCardError):
    """工牌令牌不可用（无效 / 过期 / 已吊销）→ 401。

    `code` 与 `app/services/auth_service` 的 401 口径一致（TOKEN_INVALID /
    TOKEN_EXPIRED / TOKEN_REVOKED），前端登录态分支只写一套。
    """

    def __init__(self, message: str, *, code: str = "TOKEN_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IssuedStaffCard:
    """一次工牌签发的产物。**`token` 只在这里出现一次，不写日志、不入审计 detail。**"""

    token: str
    staff_id: uuid.UUID
    device_id: uuid.UUID
    scope: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StaffCardSession:
    """工牌换来的短期会话（**kind 仍是 staff_card**，不升格为全权员工会话）。"""

    access_token: str
    scope: tuple[str, ...]
    expires_in: int
    device_id: uuid.UUID
    elder_id: uuid.UUID | None
    expires_at: datetime


def _validated_staff_card_scope(scope: object) -> tuple[str, ...]:
    """工牌 scope 的两道闸：形态（`normalize_scope`）+ 白名单（`STAFF_CARD_SCOPES`）。

    两道都必须过。只过形态闸的话，`workorder:hadle` 这种拼写错误能签出卡来，
    故障点漂到调用端 403 —— 排障的人只会怀疑权限配置。
    """
    try:
        items = normalize_scope(cast(Sequence[str], scope))
    except TokenPayloadError as exc:
        raise StaffCardValidationError(str(exc)) from exc
    unknown = sorted(set(items) - STAFF_CARD_SCOPES)
    if unknown:
        raise StaffCardValidationError(f"scope not in staff card allowlist: {', '.join(unknown)}")
    return items


def _validated_staff_card_ttl(ttl_seconds: int | None) -> int:
    ttl = DEFAULT_STAFF_CARD_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise StaffCardValidationError("ttl_seconds must be a positive integer")
    return ttl


async def issue_staff_card(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    staff_id: uuid.UUID,
    device_id: uuid.UUID,
    scope: object,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> IssuedStaffCard:
    """管理员为某员工在某台设备上签发一张受限工牌（写 `staff_tokens` 一行）。

    **有效期缺省取上限 86400s**：PRD/TD 只规定「短时效且 ≤24h」，没有给具体值；
    缺省取上限而不是随便一个中间值，是因为管理员端不传时最可能的意图是「给一张
    今天的卡」，而 24h 上限本身仍由 `core/security` 把住 —— 想更短就显式传。

    只 `flush` 不 `commit`：事务归 `app.db.get_session`（AGENTS §2）。
    """
    from app.services.audit_service import write_audit

    items = _validated_staff_card_scope(scope)
    ttl = _validated_staff_card_ttl(ttl_seconds)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise StaffCardValidationError("now must be a timezone-aware UTC instant")

    staff_status = (
        await session.execute(select(StaffAccount.status).where(StaffAccount.staff_id == staff_id))
    ).scalar_one_or_none()
    if staff_status is None:
        raise StaffCardNotFoundError("staff account not found")
    if staff_status != "active":
        raise StaffCardConflictError("staff account is not active")

    device_status = (
        await session.execute(select(Device.status).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device_status is None:
        raise StaffCardNotFoundError("device not found")
    if device_status == "locked":
        raise StaffCardConflictError("device is locked")

    try:
        token = sign_staff_card(
            staff_id=staff_id,
            device_id=device_id,
            scope=items,
            ttl_seconds=ttl,
            now=moment,
        )
    except TokenPayloadError as exc:
        # 走到这里说明 ttl 越过了 86400 上限（形态与白名单已在上面判过）。
        raise StaffCardValidationError(str(exc)) from exc

    expires_at = moment + timedelta(seconds=ttl)
    # `scope` 列是 text（契约 §5.1 原样），逗号分隔只为可读；
    # **本列不参与鉴权**（S-22：切分口径未定，比对留白），判定只认令牌载荷。
    session.add(
        StaffToken(
            token_hash=sha256_hex(token),
            staff_id=staff_id,
            device_id=device_id,
            scope=", ".join(items),
            expires_at=expires_at,
        )
    )
    await write_audit(
        session,
        actor_type="staff",
        actor_id=actor_id,
        action=STAFF_CARD_ISSUED_ACTION,
        resource_type="staff_tokens",
        resource_id=staff_id,
        # 完整令牌绝不入审计：审计表是可导出的（audit_service.export_audits）。
        detail={
            "staff_id": str(staff_id),
            "device_id": str(device_id),
            "scope": list(items),
            "ttl_seconds": ttl,
            "expires_at": expires_at.isoformat(),
        },
    )
    await session.flush()
    return IssuedStaffCard(
        token=token,
        staff_id=staff_id,
        device_id=device_id,
        scope=items,
        expires_at=expires_at,
    )


async def exchange_staff_card(
    session: AsyncSession,
    *,
    card_token: str,
    now: datetime | None = None,
) -> StaffCardSession:
    """工牌令牌换一张**同样受限**的短期令牌（`kind` 仍是 `staff_card`）。

    为什么不升格成 `kind=staff`：工牌的全部意义是「比全权会话更小的权限面」，
    换一次就变全权等于签发时的 scope 只影响第一次请求。契约 §5.2 也没有这条升级路径。

    有效期取 `min(设备 access TTL, 卡剩余寿命)`：卡还剩 5 分钟时不发一张 2 小时的
    令牌 —— 否则「补一张卡」就能绕过到期时间（卡的失效只靠 `expires_at`，R9）。
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise StaffCardValidationError("now must be a timezone-aware UTC instant")

    try:
        claims = decode_token(card_token, expected_typ=TYP_STAFF_CARD)
    except TokenExpiredError as exc:
        raise StaffCardCredentialError("staff card has expired", code="TOKEN_EXPIRED") from exc
    except TokenInvalidError as exc:
        raise StaffCardCredentialError("staff card is invalid") from exc

    lookup_key = sha256_hex(card_token)
    row = (
        await session.execute(
            select(
                StaffToken.staff_id,
                StaffToken.device_id,
                StaffToken.expires_at,
            ).where(StaffToken.token_hash == lookup_key)
        )
    ).first()
    if row is None:
        # 库里没有这一行 = 已吊销（表只有到期与删除两种失效方式，无版本号）。
        raise StaffCardCredentialError("staff card has been revoked", code="TOKEN_REVOKED")

    staff_id, device_id, db_expires_at = row
    if db_expires_at.tzinfo is None:
        raise StaffCardCredentialError("staff card expiry must be timezone-aware")
    if db_expires_at <= moment:
        raise StaffCardCredentialError("staff card has expired", code="TOKEN_EXPIRED")

    staff_status = (
        await session.execute(select(StaffAccount.status).where(StaffAccount.staff_id == staff_id))
    ).scalar_one_or_none()
    if staff_status != "active":
        raise StaffCardCredentialError("staff account is not active", code="TOKEN_REVOKED")

    remaining = int((db_expires_at - moment).total_seconds())
    ttl = min(remaining, access_ttl_seconds())
    if ttl <= 0:
        raise StaffCardCredentialError("staff card has expired", code="TOKEN_EXPIRED")

    # scope 只认令牌载荷（S-22：`staff_tokens.scope` 是 text，切分口径未定，不比对）。
    payload_scope = cast(Sequence[str], claims.get("scope") or ())
    items = _validated_staff_card_scope(payload_scope)

    elder_id = (
        await session.execute(select(Device.elder_id).where(Device.device_id == device_id))
    ).scalar_one_or_none()

    access_token = sign_staff_card(
        staff_id=staff_id,
        device_id=device_id,
        scope=items,
        ttl_seconds=ttl,
        now=moment,
    )
    # 换出来的令牌同样要落一行 `staff_tokens`：`resolve_principal` 对 `typ=staff_card`
    # 一律按 `staff_tokens.token_hash` 回查（表里没有行 = 已吊销），只签 JWT 不落库
    # 会让这张短期令牌每次都被判 `TOKEN_REVOKED`。原卡那一行原样保留（未到期仍可用）。
    # 副作用是每次换取多一行、随 `expires_at` 自然失效，换来的是这张会话**可被吊销**：
    # 退房时按 `device_id` 硬删该设备的全部工牌行，会话跟着一起废。
    access_hash = sha256_hex(access_token)
    already = (
        await session.execute(
            select(StaffToken.token_hash).where(StaffToken.token_hash == access_hash)
        )
    ).scalar_one_or_none()
    if already is None:
        # 同一秒内对同一张卡换两次，JWT 载荷（sub/dev/scope/iat/exp）完全一致 →
        # hash 相同；那一行已经代表这张令牌，直接复用，不重复插入（PK 冲突）。
        session.add(
            StaffToken(
                token_hash=access_hash,
                staff_id=staff_id,
                device_id=device_id,
                scope=", ".join(items),
                expires_at=moment + timedelta(seconds=ttl),
            )
        )
        await session.flush()
    return StaffCardSession(
        access_token=access_token,
        scope=items,
        expires_in=ttl,
        device_id=device_id,
        elder_id=elder_id,
        expires_at=moment + timedelta(seconds=ttl),
    )


async def audit_scope_denial(
    *,
    actor_type: str,
    actor_id: uuid.UUID,
    required: Sequence[str],
    granted: Sequence[str],
    principal_kind: str,
    method: str,
    path: str,
    trace_id: str | None = None,
) -> bool:
    """把一次「scope 不足」的 403 写进 `audit_logs`（**独立会话，永不抛**）。

    为什么必须用独立会话（`write_audit_isolated`）：403 是异常路径，
    `app.db.get_session` 会 rollback 请求事务，同事务写的审计行跟着消失 ——
    那就成了「拒了但没留痕」，而 ADM-08 的验收恰恰要看这条留痕。

    返回 False 只代表审计没写上，不影响调用方继续抛 403（审计是旁路）。
    """
    from app.services.audit_service import write_audit_isolated

    return await write_audit_isolated(
        actor_type=actor_type,
        actor_id=actor_id,
        action=SCOPE_DENIED_ACTION,
        resource_type="staff_tokens",
        resource_id=actor_id,
        detail={
            "kind": principal_kind,
            "required": list(required),
            "granted": list(granted),
            "method": method,
            "path": path,
        },
        trace_id=trace_id,
    )
