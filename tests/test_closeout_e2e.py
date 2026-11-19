"""迭代 10 收口三卡的**端到端证据**：记忆治理 / 监护人通知 / 录音上限（task-14）。

## 这份文件回答什么

不是把三张卡的零件单测再抄一遍（那些在 `test_memory_service.py` /
`test_memory_extraction.py` / `test_alert_service.py` / `test_risk_event_service.py`
/ `test_audio_utterance.py` 里），而是把每条验收口径**从服务入口驱动到库里的可观测事实**：

| # | 用例 | 验收口径 |
| --- | --- | --- |
| ① | `test_contradiction_supersedes_but_similar_preference_coexists` | 矛盾→旧 superseded/新 active 且两条都在；高相似不矛盾→都 active |
| ② | `test_recall_hides_superseded_and_below_floor` | recall 不返回 superseded，也不返回低于相关性下限的条目 |
| ③ | `test_speculative_family_event_writes_nothing` | 「我儿子可能失业了」经抽取管线零写入；对照组能写 |
| ④ | `test_red_alert_notifies_guardian_once_and_is_readable` | 红色事件恰 1 条 guardian 通知、`payload.recipients` 含绑定监护人、重放不增、家属可读 |
| ⑤ | `test_overdue_red_event_escalates_to_admin_only_once` | 拨时间驱动升级→事件 escalated、管理员恰一条、全程仍 1 事件 + 1 工单 |
| ⑥ | `test_utterance_accepts_120s_and_rejects_121s` | 120 秒通过、121 秒 413（WAV 头声明时长，不搬大文件） |

## 与既有用例的分工（避免重复计功）

- ④ 的幂等键本身、⑤ 的 payload 细节在 `test_alert_service.py` / `test_risk_event_service.py`
  已有单测；这里只断言「同一条真链路跑两次，库里只多一条」。
- ⑥ 的 119/120/121 秒端点级边界、真实字节 120 秒在 `test_audio_utterance.py` 已有；
  这里从**函数入口**直调一次，断言时长闸放行 120 秒、抬到 121 秒才 413，并核对
  体积闸由时长闸派生（改上限只动一个数字）。

## 夹具口径（与 `test_medication_chain_e2e.py` 同款）

真库（`ANYU_TEST_DATABASE_URL` → `anyu_test`）；NullPool 引擎 + 一条真连接 +
外层事务，session 用 `join_transaction_mode="create_savepoint"`，所以被测服务即使内部
`commit()` 也只是释放 savepoint，用例结束整条外事务回滚。**不**启动 APScheduler、
**不**起 aiortc、**不**用 `tmp_path`（本沙箱建不了目录）、**不** `sleep`。
"""

from __future__ import annotations

import datetime
import io
import struct
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from starlette.requests import Request
from tests.conftest import require_db_connection

from app.api import audio as audio_api
from app.audio.asr import MockASRProvider
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.models import (
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    MemoryChunk,
    Notification,
    RiskEvent,
    StaffAccount,
    WorkOrder,
)
from app.router.decision import RouteDecision, SafetyHit
from app.services import alert_service, memory_extraction, memory_service, notification_service
from app.services.dialogue_service import DialogueService
from app.services.embedding import EMBEDDING_DIMENSION
from app.services.llm_service import LLMReply
from app.services.risk_event_service import escalate_overdue_red_events

UTC = datetime.UTC
#: 固定锚点（UTC 直写，用例不随机器时区漂）；告警幂等键按分钟取桶。
OCCURRED_AT = datetime.datetime(2026, 11, 12, 4, 30, 0, tzinfo=UTC)
THREAD_ID = "thread-day10-closeout-1"
TRACE_ID = "trace-day10-closeout-1"


# ===========================================================================
# 夹具：NullPool + 外层事务 + savepoint（与用药 e2e 同一套手法）
# ===========================================================================


@pytest.fixture()
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture()
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    connection = await require_db_connection(db_engine)
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


# ===========================================================================
# 种子：一个长者 + 主责护工 + 管理员 + 绑定监护人
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Seed:
    """一套可复用的人与人关系；ID 在用例内唯一。"""

    elder_id: uuid.UUID
    care_worker_id: uuid.UUID
    admin_id: uuid.UUID
    guardian_id: uuid.UUID
    device_id: uuid.UUID


async def _seed(db: AsyncSession) -> Seed:
    """写一套最小关系网（只 flush，不 commit）。

    管理员是 ⑤ 的收件人；主责护工用于证明「管理员那条不是派给护工的」；
    监护人绑定走 `elder_guardians.status='active'`，是 ④ 可见范围的唯一依据。
    """
    elder_id = uuid.uuid4()
    care_worker_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    guardian_id = uuid.uuid4()
    device_id = uuid.uuid4()

    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": care_worker_id,
                "name": "李护工",
                "role": "care_worker",
                "password_hash": "x",
            },
            {"staff_id": admin_id, "name": "赵管理员", "role": "admin", "password_hash": "x"},
        ],
    )
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": elder_id, "name": f"测试长者{uuid.uuid4().hex[:6]}", "status": "active"}],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert(),
        [{"elder_id": elder_id, "staff_id": care_worker_id, "relation": "primary_care"}],
    )
    await db.execute(
        GuardianAccount.__table__.insert(),
        [{"guardian_id": guardian_id, "name": "王儿子", "password_hash": "x"}],
    )
    await db.execute(
        ElderGuardian.__table__.insert(),
        [
            {
                "elder_id": elder_id,
                "guardian_id": guardian_id,
                "relation": "子",
                "status": "active",
            }
        ],
    )
    await db.flush()
    return Seed(
        elder_id=elder_id,
        care_worker_id=care_worker_id,
        admin_id=admin_id,
        guardian_id=guardian_id,
        device_id=device_id,
    )


async def _count(db: AsyncSession, model: Any, *where: Any) -> int:
    result = await db.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


# ===========================================================================
# 记忆：可控向量 provider（按文本查表，余弦由用例显式给定）
# ===========================================================================


def _axis(index: int) -> list[float]:
    """第 `index` 个正交基向量：不同轴 = 余弦 0。"""
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[index] = 1.0
    return vector


class _FixedProvider:
    """按文本返回预设向量的 embedding provider（**非语义**，只把相似度变成显式参数）。"""

    def __init__(self, vectors: Mapping[str, list[float]]) -> None:
        self._vectors = dict(vectors)

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    @property
    def available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vectors[text]) for text in texts]


class _ScriptedLLM:
    """按脚本返回一段 JSON 文本的假模型（只实现提取器用到的 `chat`）。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        return LLMReply(text=self._payload, degraded=False)


# ===========================================================================
# ① 矛盾取代 + 高相似不矛盾并存
# ===========================================================================


async def test_contradiction_supersedes_but_similar_preference_coexists(
    db: AsyncSession,
) -> None:
    """同一长者四条记忆：一次改口（取代）+ 一份高相似偏好（并存）。

    口径（PRD FR-MEM-03 / 计划 §W1）：取代 = 够像 **且** 语义矛盾；只够像不矛盾
    必须并存。任何一条被误取代都等于抹掉长者亲口说过的话，所以这里断言的是
    库里四条行的最终 status，而不是某个函数的返回值。
    """
    seed = await _seed(db)
    apple = "我喜欢吃苹果"
    apple_negated = "我不喜欢吃苹果"
    tea = "我每天早上喝一杯热水"
    tea_similar = "我每天早上喝一杯温水"

    # 前两句向量完全相同（余弦 1.0 > 0.9），后两句也相同；两组之间正交（余弦 0）。
    provider = _FixedProvider(
        {
            apple: _axis(0),
            apple_negated: _axis(0),
            tea: _axis(1),
            tea_similar: _axis(1),
        }
    )

    await memory_service.remember(
        db, elder_id=seed.elder_id, content=apple, embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=seed.elder_id, content=apple_negated, embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=seed.elder_id, content=tea, embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=seed.elder_id, content=tea_similar, embedding_provider=provider
    )

    rows = (
        await db.execute(
            select(MemoryChunk.content, MemoryChunk.status).where(
                MemoryChunk.elder_id == seed.elder_id
            )
        )
    ).all()
    status_by_content = {content: status for content, status in rows}

    # 四条都在：取代是软失效，不是删数据。
    assert len(status_by_content) == 4
    # 改口：旧条 superseded，新条 active。
    assert status_by_content[apple] == "superseded"
    assert status_by_content[apple_negated] == "active"
    # 高相似但不矛盾：两条都 active（「热水/温水」不是一句话的两种说法）。
    assert status_by_content[tea] == "active"
    assert status_by_content[tea_similar] == "active"


# ===========================================================================
# ② 召回二次过滤：superseded 与低于下限的都不出现
# ===========================================================================


async def test_recall_hides_superseded_and_below_floor(db: AsyncSession) -> None:
    """recall 只回 active 且相关性 >= RECALL_MIN_SIMILARITY 的条目。

    两个断言各挡一类错误：
    - 旧条（superseded）不能召回 —— 否则长者会听到自己已经改口的旧偏好；
    - 不相干的条目不能靠 top_k 兜底混进来 —— 相关性下限失效等于「每问必答，
      答的全是别人的事」。
    """
    seed = await _seed(db)
    old = "我喜欢吃苹果"
    new = "我不喜欢吃苹果"
    unrelated_query = "今天天气怎么样"

    provider = _FixedProvider(
        {
            old: _axis(0),
            new: _axis(0),
            # 与 axis(0) 余弦 0 → 距离 1.0 > 1 - 0.35 = 0.65，必须被 SQL 下限挡掉。
            unrelated_query: _axis(7),
        }
    )
    await memory_service.remember(
        db, elder_id=seed.elder_id, content=old, embedding_provider=provider
    )
    await memory_service.remember(
        db, elder_id=seed.elder_id, content=new, embedding_provider=provider
    )

    hit_provider = _FixedProvider(
        {
            "我想吃苹果吗": _axis(0),
            unrelated_query: _axis(7),
        }
    )
    recalled = await memory_service.recall(
        db,
        elder_id=seed.elder_id,
        query="我想吃苹果吗",
        embedding_provider=hit_provider,
        top_k=5,
    )
    assert [chunk.content for chunk in recalled] == [new]

    empty = await memory_service.recall(
        db,
        elder_id=seed.elder_id,
        query=unrelated_query,
        embedding_provider=hit_provider,
        top_k=5,
    )
    assert empty == []


# ===========================================================================
# ③ 抽取限定：「我儿子可能失业了」零写入（对照组能写）
# ===========================================================================


async def test_speculative_family_event_writes_nothing(db: AsyncSession) -> None:
    """PRD FR-MEM-01 的原句经「抽取 → 写库」两段后，库里 0 行。

    对照组用同一套管线跑「我对青霉素过敏」：它能抽出候选并落库 —— 证明 0 行是
    **过滤判据**起作用，而不是链路本身断了（这正是 `memory_extraction` docstring
    里说的「0 行不等于链路断了」的判别方法）。
    """
    prd_elder = await _seed(db)
    control_elder = await _seed(db)
    provider = _FixedProvider(
        {
            "我儿子可能失业了": _axis(2),
            "我对青霉素过敏": _axis(3),
        }
    )

    speculative_llm = _ScriptedLLM(
        '[{"content": "我儿子可能失业了", "category": "family", '
        '"source": "explicit", "importance_hint": 0.5}]'
    )
    speculative = await memory_extraction.extract_memories(
        speculative_llm, user_text="我儿子可能失业了", assistant_text="别太担心"
    )
    assert speculative == []
    # 生产写回就是「逐条 remember」：没有候选 → 不写一行。
    for candidate in speculative:
        await memory_service.remember(
            db,
            elder_id=prd_elder.elder_id,
            content=candidate.content,
            embedding_provider=provider,
            category=candidate.category,
            importance=candidate.importance_hint,
            source=candidate.source,
        )
    assert (await _count(db, MemoryChunk, MemoryChunk.elder_id == prd_elder.elder_id)) == 0

    control_llm = _ScriptedLLM(
        '[{"content": "我对青霉素过敏", "category": "private", '
        '"source": "explicit", "importance_hint": 0.9}]'
    )
    control = await memory_extraction.extract_memories(
        control_llm, user_text="我对青霉素过敏", assistant_text="我记下了"
    )
    assert [candidate.content for candidate in control] == ["我对青霉素过敏"]
    for candidate in control:
        await memory_service.remember(
            db,
            elder_id=control_elder.elder_id,
            content=candidate.content,
            embedding_provider=provider,
            category=candidate.category,
            importance=candidate.importance_hint,
            source=candidate.source,
        )
    assert (await _count(db, MemoryChunk, MemoryChunk.elder_id == control_elder.elder_id)) == 1


# ===========================================================================
# ④ 红色事件通知监护人：恰一条、重放不增、绑定家属可读
# ===========================================================================


def _hit(
    *,
    rule_id: str = "safety_emergency",
    category: str = "emergency",
    severity: str = "red",
    excerpt: str = "我胸口疼得厉害，快叫医生",
    occurred_at: datetime.datetime | None = OCCURRED_AT,
) -> SafetyHit:
    return SafetyHit(
        rule_id=rule_id,
        category=category,
        severity=severity,
        matched="胸口疼",
        transcript_excerpt=excerpt,
        occurred_at=occurred_at,
    )


def _decision(hit: SafetyHit | None) -> RouteDecision:
    return RouteDecision(
        elder_id=None,
        thread_id=None,
        layer="L0",
        rule_id="safety_emergency",
        intent="alert",
        sensitive=True,
        alert=hit,
    )


async def _guardian_notifications(
    db: AsyncSession, *, elder_id: uuid.UUID, risk_event_id: uuid.UUID
) -> Sequence[Notification]:
    rows = await db.execute(
        select(Notification).where(
            Notification.kind == "safety_alert",
            Notification.elder_id == elder_id,
            Notification.payload["audience"].astext == "guardian",
            Notification.payload["risk_event_id"].astext == str(risk_event_id),
        )
    )
    return rows.scalars().all()


async def test_red_alert_notifies_guardian_once_and_is_readable(
    db: AsyncSession,
) -> None:
    """一次红色告警 → 家属侧恰一条通知；同事件重放不产生第二条；绑定家属真能读到。

    断言全在库的可观测事实上：条数、`payload.recipients`、以及走
    `notification_service.list_for_staff` 的 guardian 可见范围分支。
    """
    seed = await _seed(db)

    outcome = await alert_service.handle_safety_alert(
        db,
        decision=_decision(_hit()),
        elder_id=seed.elder_id,
        thread_id=THREAD_ID,
        trace_id=TRACE_ID,
    )
    assert outcome is not None
    assert outcome.created is True
    assert outcome.risk_event_id is not None
    assert outcome.guardian_notification_id is not None

    guardian_rows = await _guardian_notifications(
        db, elder_id=seed.elder_id, risk_event_id=outcome.risk_event_id
    )
    assert len(guardian_rows) == 1
    guardian_row = guardian_rows[0]
    assert guardian_row.notification_id == outcome.guardian_notification_id
    payload = guardian_row.payload or {}
    assert payload.get("audience") == "guardian"
    assert payload.get("recipients") == [str(seed.guardian_id)]
    assert payload.get("risk_event_id") == str(outcome.risk_event_id)
    assert payload.get("category") == "emergency"
    # 只有在册的绑定关系才进收件人列表（空绑定不建行、不复制 N 条）。
    assert str(seed.guardian_id) in payload["recipients"]

    # 同分钟同线程同规则重放 → 幂等命中，不建第二条（护工/家属两条都只有一条）。
    replay = await alert_service.handle_safety_alert(
        db,
        decision=_decision(_hit()),
        elder_id=seed.elder_id,
        thread_id=THREAD_ID,
        trace_id=TRACE_ID,
    )
    assert replay is not None
    assert replay.duplicate is True
    assert replay.risk_event_id == outcome.risk_event_id
    assert (
        len(
            await _guardian_notifications(
                db, elder_id=seed.elder_id, risk_event_id=outcome.risk_event_id
            )
        )
        == 1
    )
    assert (
        await _count(
            db,
            Notification,
            Notification.kind == "safety_alert",
            Notification.elder_id == seed.elder_id,
            Notification.payload["risk_event_id"].astext == str(outcome.risk_event_id),
        )
        == 2
    )  # 护工一条 + 家属一条，重放没有加行

    # 绑定家属能通过通知读取路径看到它（guardian 分支按 elder_guardians 放行）。
    guardian_identity = Identity(kind="guardian", guardian_id=str(seed.guardian_id))
    page = await notification_service.list_for_staff(db, guardian_identity, page=1, page_size=20)
    assert any(item.notification_id == guardian_row.notification_id for item in page.items)


# ===========================================================================
# ⑤ 超时升级通知管理员：拨时间驱动、恰一条、全程仍 1 事件 + 1 工单
# ===========================================================================


async def test_overdue_red_event_escalates_to_admin_only_once(db: AsyncSession) -> None:
    """120 秒没人确认 → 事件 escalated + 管理员恰一条通知；再驱动不重复。

    时间靠 `now=` 注入（不 sleep、不起调度器）；「仍只有 1 个 risk_event 与 1 张
    工单」按本事件 ID 计数，因为升级只改状态、不建第二套。
    """
    seed = await _seed(db)
    outcome = await alert_service.handle_safety_alert(
        db,
        decision=_decision(_hit()),
        elder_id=seed.elder_id,
        thread_id=THREAD_ID,
        trace_id=TRACE_ID,
    )
    assert outcome is not None and outcome.risk_event_id is not None
    risk_event_id = outcome.risk_event_id

    escalated = await escalate_overdue_red_events(
        db, now=OCCURRED_AT + datetime.timedelta(seconds=121)
    )
    assert escalated == [risk_event_id]

    event = await db.get(RiskEvent, risk_event_id)
    assert event is not None
    assert event.status == "escalated"
    assert (event.detail or {}).get("escalation_reason") == "unacknowledged_120s"

    admin_filters = (
        Notification.kind == "safety_alert",
        Notification.payload["module"].astext == "risk_event_escalation",
        Notification.payload["risk_event_id"].astext == str(risk_event_id),
        Notification.assignee_staff_id == seed.admin_id,
    )
    assert (await _count(db, Notification, *admin_filters)) == 1

    # 再驱动几轮（时间继续往前拨）：状态已是 escalated，候选条件不再命中。
    assert (
        await escalate_overdue_red_events(db, now=OCCURRED_AT + datetime.timedelta(minutes=10))
        == []
    )
    assert (await _count(db, Notification, *admin_filters)) == 1

    # 全程仍只有 1 个 risk_event 与 1 张工单（升级不建第二套）。
    assert (await _count(db, RiskEvent, RiskEvent.elder_id == seed.elder_id)) == 1
    assert (await _count(db, WorkOrder, WorkOrder.event_id == risk_event_id)) == 1


# ===========================================================================
# ⑥ 录音上限：120 秒通过、121 秒 413
# ===========================================================================


def _wav_header_only(duration_ms: int, sample_rate: int = 16_000) -> bytes:
    """只声明时长、不携带采样的 WAV 头（44 B）。

    时长闸在 ASR 之前就按 WAV 头算完（`wave.getnframes()/framerate`），所以边界判定
    不需要为每个用例真的搬几 MB 静音 —— 这也是「假时长字段，不传大文件」的落法。
    """
    num_frames = sample_rate * duration_ms // 1000
    data_size = num_frames * 2  # 单声道 16bit
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    head = struct.pack("<4sI", b"RIFF", 4 + 8 + len(fmt) + 8 + data_size) + b"WAVE"
    return (
        head
        + struct.pack("<4sI", b"fmt ", len(fmt))
        + fmt
        + struct.pack("<4sI", b"data", data_size)
    )


class _EchoLLM:
    """回显假模型：不碰任何外部服务，只保证对话链给出非空回复。"""

    async def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        last = messages[-1]["content"] if messages else ""
        return LLMReply(text=f"我在听呢。您刚说的是：{last}", degraded=False)


class _FailingTTS:
    """合成必失败的 TTS：走端点已登记的降级分支（200 + audio_url=null），不落静态文件。"""

    async def synthesize(self, text: str) -> bytes:
        raise RuntimeError("tts unavailable in closeout e2e")


async def _no_route_decision(*_args: Any, **_kwargs: Any) -> None:
    """假路由：一律不裁决，让本轮直接落到模型分支（不碰 Laya / Redis）。"""
    return None


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/audio/utterance",
            "headers": [],
            "query_string": b"",
            "http_version": "1.1",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


def _upload(duration_ms: int, *, filename: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(_wav_header_only(duration_ms)),
        filename=filename,
    )


async def test_utterance_accepts_120s_and_rejects_121s(db: AsyncSession) -> None:
    """120 秒收得下、121 秒 413，且 413 文案引用的是真源上限。

    直接调端点函数（依赖全部显式传入，不走 HTTP 鉴权）；时间/文件都不伪造真实数据：
    时长由 WAV 头给出，服务器照自己的解析结果判，客户端 `duration_ms` 只是日志。
    """
    seed = await _seed(db)
    identity = Identity(kind="device", device_id=str(seed.device_id), elder_id=str(seed.elder_id))
    service = DialogueService(llm=_EchoLLM())

    accepted = await audio_api.utterance(
        request=_request(),
        identity=identity,
        asr=MockASRProvider(),
        tts=_FailingTTS(),
        session=db,
        service=service,
        route_text=_no_route_decision,
        audio=_upload(120_000, filename="limit.wav"),
        duration_ms=120_000,
    )
    assert accepted["text"].strip() != "", "ASR 终稿为空：链路没走到对话链"
    assert accepted["reply"]["text"].strip() != ""

    with pytest.raises(HTTPException) as excinfo:
        await audio_api.utterance(
            request=_request(),
            identity=identity,
            asr=MockASRProvider(),
            tts=_FailingTTS(),
            session=db,
            service=service,
            route_text=_no_route_decision,
            audio=_upload(121_000, filename="long.wav"),
            duration_ms=121_000,
        )
    assert excinfo.value.status_code == 413
    assert f"{audio_api.MAX_AUDIO_SECONDS}s" in str(excinfo.value.detail)

    # 体积闸由时长闸派生：改上限只动 MAX_AUDIO_SECONDS，不各写一个数（PRD FR-UI-01）。
    assert audio_api.MAX_AUDIO_SECONDS == 120
    assert audio_api.MAX_AUDIO_BYTES == (
        audio_api.MAX_AUDIO_SECONDS * 16_000 * 2 + audio_api.WAV_CONTAINER_OVERHEAD_BYTES
    )
