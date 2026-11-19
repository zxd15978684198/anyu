"""迭代 4 · W3：双人间隐私收敛（话术 + 审计）。

唯一依据：`设计记录（身份闭环缺口补齐）` §3-W3（口径 S-D4-3）。
PRD line 571 要求「设备绑定 1v1 声纹校验与**隐私收敛**」；`RouteDecision.privacy`
（`app/router/decision.py:55`）此前只被 `decision_log_service` 落库、没有任何消费点，
本文件钉住新接上的消费链路（`app/audio/reply_stub.py`）：

1. **privacy 为真** → 本轮第一句就是 `PRIVACY_NOTICE_TEXT`（先短句、后正文），且
   `audit_logs` 多一行 `action="privacy_converged"`（真库断言）；
2. **privacy 为假 / None / 缺失** → 不加话术、不写这条审计；
3. **privacy 为真但命中注入** → 只出 `FIXED_REFUSAL`（句子完全相等）、不写隐私审计，
   即「注入拦截优先级高于隐私话术」。

「privacy 真/假」的口径不在本卡新发明：`app/agents/companion.py:362` 的
`is_privacy_converged` 是 W5 定下的**唯一**判定口径（bool 原样、`None` 假、字符串查
`_PRIVACY_FALSE_TAGS` 集合、其余取真值）。`reply_stub._privacy_is_true` 是它的镜像
（`audio` 与 `agents` 之间禁止横向 import），所以本文件拿 `"true"/True/"couple"` 与
`False/"false"/None/""/"0"/"none"` 两侧各钉一遍：两条链路对「什么算收敛」必须同一个答案。

## 为什么计数用「基线增量」而不是全表总数

`audit_logs` 是只增表，库里本来就有历史行（`tests/test_stats_api.py` 模块 docstring:17-22
记过这个坑）。所以断言写成「调用前基线 + 1」：既精确到个位，又在有历史行的库上不假红。

## 审计写在独立会话里，怎么查得到

`write_audit_isolated` 自己开一条会话（读 `audit_service.session_factory`）。测试把它钉到
本用例的外层事务上（`join_transaction_mode="create_savepoint"`）：那行既落在同一事务里能
查到，又随用例整体回滚、不污染库——与 `tests/test_stats_api.py` 的 `isolated_session_factory`
同款做法，不另造一套夹具体系。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.audio import reply_stub
from app.audio.reply_stub import (
    PRIVACY_AUDIT_ACTION,
    PRIVACY_AUDIT_RESOURCE_TYPE,
    PRIVACY_NOTICE_TEXT,
    REPLY_TEXT,
    ReplyOutcome,
    ReplyStub,
)
from app.core.config import get_settings
from app.core.injection import FIXED_REFUSAL
from app.models import AuditLog
from app.router.decision import RouteDecision
from app.services import audit_service

#: 假身份的固定 UUID：`decision.elder_id` 按生产口径存字符串（`wiring.wire_route` 会
#: 把 `resolve_identity` 的 UUID 转成规范文本），审计落库时再反解回 UUID 列。
_ELDER_ID = uuid.UUID("6f1a1d2c-0000-4000-8000-00000000a001")


# --------------------------------------------------------------- 测试替身


class _RecordingTTS:
    """只记录喂进来的句子；返回一段合法的下行静音 PCM（偶数长度、无 RIFF 头）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return b"\x00\x00" * 320


class _RecordingSession:
    """只实现垫层真正用到的那一个方法：`enqueue_tts_pcm(turn_id, pcm)`。"""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, bytes]] = []

    async def enqueue_tts_pcm(self, turn_id: str, pcm: bytes) -> None:
        self.enqueued.append((turn_id, pcm))


# --------------------------------------------------------------- 真库夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """NullPool：pytest-asyncio 每个用例一个新事件循环，池内连接跨循环复用会
    `Event loop is closed`（`tests/test_stats_api.py:85-93` 同款）。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """外层事务：本文件的写入最后整体回滚。"""
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


@pytest.fixture(autouse=True)
def isolated_session_factory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把 `write_audit_isolated` 的「独立会话」钉在同一条测试连接上，否则查不到那行。"""
    bind = db.bind
    assert bind is not None
    factory = async_sessionmaker(
        bind=bind,  # type: ignore[arg-type]
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(audit_service, "session_factory", factory)
    return factory


# --------------------------------------------------------------- 小工具


def _decision(*, privacy: object, intent: str = "chitchat", trace_id: str) -> RouteDecision:
    """一条 L1 决策：privacy 取业务上的各种形态（"true"/True/"couple"/False/None/…）。"""
    return RouteDecision(
        elder_id=str(_ELDER_ID),
        thread_id="thread-privacy",
        layer="L1",
        rule_id="privacy_double_room",
        intent=intent,
        sensitive=False,
        confidence=0.93,
        privacy=privacy,  # type: ignore[arg-type]  # 故意喂契约外形态，测收敛口径
        route_ms=4,
        trace_id=trace_id,
    )


def _stub_returning(
    decision: RouteDecision, *, tts: _RecordingTTS, monkeypatch: pytest.MonkeyPatch
) -> ReplyStub:
    """按生产接线形态（`app/audio/webrtc.py:2435`）拼 stub：router + identity + trace_id。"""

    async def fake_router(text: str, **kwargs: Any) -> RouteDecision:
        return decision

    async def fake_identity() -> tuple[uuid.UUID | None, str | None]:
        return (_ELDER_ID, decision.thread_id)

    async def fake_log(_decision: RouteDecision) -> None:
        # 决策落库不是本文件的被测点（`tests/test_audio_reply_stub.py` 已钉住）：
        # 换成空实现，免得每条用例都往测试库的 decision_logs 真写一行。
        return None

    monkeypatch.setattr(reply_stub, "log_decision", fake_log)
    return ReplyStub(
        tts=tts,
        router=fake_router,
        identity=fake_identity,
        trace_id=decision.trace_id,
    )


async def _privacy_audit_count(db: AsyncSession) -> int:
    """`audit_logs` 里 `action="privacy_converged"` 的**当前总数**（只当基线用）。"""
    total = await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == PRIVACY_AUDIT_ACTION)
    )
    return int(total or 0)


async def _privacy_audits_for_trace(db: AsyncSession, trace_id: str) -> list[AuditLog]:
    """本轮 trace 写下的隐私收敛审计行（按 `detail.trace_id` 精确取，不依赖行序）。"""
    result = await db.execute(select(AuditLog).where(AuditLog.action == PRIVACY_AUDIT_ACTION))
    return [row for row in result.scalars().all() if (row.detail or {}).get("trace_id") == trace_id]


# --------------------------------------------- ① privacy 为真：话术 + 审计


@pytest.mark.parametrize(
    ("privacy", "turn_id", "trace_id"),
    [
        # 库里的 Text 列写 `"true"`，Laya 五信号透传的是 bool，历史用例还有 `"couple"`
        # 这类标签值 —— 三种形态都必须收敛（口径照 `companion.is_privacy_converged`）。
        pytest.param("true", "turn-privacy-str", "trace-privacy-str", id="string-true"),
        pytest.param(True, "turn-privacy-bool", "trace-privacy-bool", id="bool-true"),
        pytest.param("couple", "turn-privacy-tag", "trace-privacy-tag", id="tag-couple"),
    ],
)
async def test_privacy_true_prepends_notice_and_writes_audit(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    privacy: object,
    turn_id: str,
    trace_id: str,
) -> None:
    """双人间命中：先短句后正文，且台账多一条 `privacy_converged`（真库断言）。"""
    before = await _privacy_audit_count(db)
    tts = _RecordingTTS()
    stub = _stub_returning(
        _decision(privacy=privacy, trace_id=trace_id), tts=tts, monkeypatch=monkeypatch
    )

    outcome = await stub.reply(
        turn_id=turn_id, session=_RecordingSession(), asr_text="今天屋里怎么这么热闹"
    )

    assert outcome is ReplyOutcome.ENQUEUED
    # 首句就是收敛话术；两句拼起来逐字等于「话术 + 正文」（没插在中间、没改写正文）
    assert tts.calls[0].startswith(PRIVACY_NOTICE_TEXT)
    assert len(tts.calls) == 2
    assert "".join(tts.calls) == PRIVACY_NOTICE_TEXT + REPLY_TEXT

    # 基线增量：只多一行，且就是本轮这一行
    assert await _privacy_audit_count(db) == before + 1
    rows = await _privacy_audits_for_trace(db, trace_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == PRIVACY_AUDIT_ACTION
    assert row.resource_type == PRIVACY_AUDIT_RESOURCE_TYPE == "dialogue_turn"
    assert row.actor_type == "system"
    # 身份取 decision.elder_id（UUID 列反解成功），没有另调一次 identity
    assert row.actor_id == _ELDER_ID
    assert row.resource_id == _ELDER_ID
    # detail 只有「怎么判出来的」，不存转写原文/回复正文
    assert row.detail == {
        "trace_id": trace_id,
        "rule_id": "privacy_double_room",
        "intent": "chitchat",
    }


# --------------------------------- ② privacy 为假/None/缺失：不加话术、不写审计


@pytest.mark.parametrize(
    ("privacy", "turn_id", "trace_id"),
    [
        pytest.param(False, "turn-privacy-off-bool", "trace-off-bool", id="bool-false"),
        pytest.param("false", "turn-privacy-off-str", "trace-off-str", id="string-false"),
        pytest.param(None, "turn-privacy-off-none", "trace-off-none", id="none"),
        pytest.param("", "turn-privacy-off-empty", "trace-off-empty", id="empty-string"),
        # 下面两个取值来自 `companion._PRIVACY_FALSE_TAGS`：两条链路必须对「什么算假」
        # 给同一个答案，否则会出现 agent 跳过记忆、嘴上却没提醒的分叉。
        pytest.param("0", "turn-privacy-off-zero", "trace-off-zero", id="string-zero"),
        pytest.param("none", "turn-privacy-off-null", "trace-off-null", id="string-none"),
    ],
)
async def test_privacy_false_or_missing_leaves_reply_untouched(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    privacy: object,
    turn_id: str,
    trace_id: str,
) -> None:
    """单人间（或信号缺失）：话术原样、台账零新增——误伤面必须为零。"""
    before = await _privacy_audit_count(db)
    tts = _RecordingTTS()
    stub = _stub_returning(
        _decision(privacy=privacy, trace_id=trace_id), tts=tts, monkeypatch=monkeypatch
    )

    outcome = await stub.reply(
        turn_id=turn_id, session=_RecordingSession(), asr_text="今天屋里怎么这么热闹"
    )

    assert outcome is ReplyOutcome.ENQUEUED
    assert tts.calls == [REPLY_TEXT]
    assert await _privacy_audit_count(db) == before
    assert await _privacy_audits_for_trace(db, trace_id) == []


# ------------------------------- ③ privacy 为真但命中注入：只出拒绝话术


async def test_privacy_true_but_injection_blocked_speaks_only_refusal(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入拦截优先级高于隐私收敛：只出 `FIXED_REFUSAL`，不叠提醒、不留收敛审计。

    命中点选在**意图层**（输入层命中时根本没路由、也就没有 `privacy`，测不到优先级）：
    文本本身无害，但路由给出白名单外 intent —— 与
    `tests/test_audio_reply_stub.py::test_intent_layer_block_after_routing_speaks_fixed_refusal`
    同一个命中形态，只是多给了一个 `privacy="true"`。
    """
    trace_id = "trace-privacy-injected"
    before = await _privacy_audit_count(db)
    tts = _RecordingTTS()
    stub = _stub_returning(
        _decision(privacy="true", intent="batch_create_work_orders", trace_id=trace_id),
        tts=tts,
        monkeypatch=monkeypatch,
    )

    outcome = await stub.reply(
        turn_id="turn-privacy-injected", session=_RecordingSession(), asr_text="随便说点什么"
    )

    assert outcome is ReplyOutcome.ENQUEUED
    # 句子完全等于拒绝话术：既没有隐私前缀，也没有被切成两句
    assert tts.calls == [FIXED_REFUSAL]
    assert PRIVACY_NOTICE_TEXT not in "".join(tts.calls)
    # 被拒的轮次不是「收敛了的轮次」：不写这条审计（写了就是假台账）
    assert await _privacy_audit_count(db) == before
    assert await _privacy_audits_for_trace(db, trace_id) == []
