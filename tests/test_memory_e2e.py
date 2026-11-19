"""迭代 7 验收第 5 条 · 记忆端到端（真库、真服务链路）。

## 这里到底验了什么

一句话从长者嘴里说出来，到下一场会话被提起，中间经过五步；本文件把五步串起来跑，
只有**两个外部边界**是替身：

    [模拟] 长者说「我有糖尿病」
      → [真实] `memory_extraction.extract_memories`：真解析、真过滤（身份证/手机/诊断措辞/长度）
      → [真实] `MemoryTurnHook`：真开 session、真 commit（它自己管事务，见该模块 docstring ②）
      → [真实] `memory_service.remember` + `enforce_capacity`：真写 `memory_chunks`
      → [真实] `memory_service.recall`：真 SQL（`WHERE elder_id AND status='active' AND ...`）+ 真余弦排序
      → [真实] `companion._memory_block` + `context_build.build_agent_context`：真上下文拼装

两个替身都在**进程外边界**上，且是本仓既有的可替换实现：

1. `llm.chat`：固定返回一段 JSON 数组（真实链路里这里打 DeepSeek/Qwen）。网络不可复现，
   用例不该因为模型今天心情不好而红；
2. `embedding provider`：`DeterministicEmbeddingProvider`（写回走 `memory_writeback` 的
   进程内单例替身，召回显式传入同一个类）—— 不让用例去烧真模型的额度。

## 明确未验的层（不许含糊成「已验」）

**WebSocket / 会话层未验。** 本文件不建 ws 连接、不跑 `DialogueRuntime`、不进 Agent 图。
「新会话」在这里用「新连接 + 新 thread_id」表示，理由不是偷懒：`memory_service.recall`
的签名里根本没有 `thread_id` —— 记忆的归属键是 `elder_id`，不是会话。所以「隔天新会话
还记得」在存储层可验的部分就是「换一条连接、换一个 thread_id 再查，仍然查得到」；
「图上真的把这条塞进了这一轮 prompt」验到 `build_agent_context` 的返回值为止。

## 隔离与清理

`MemoryTurnHook` 的契约就是**自己 commit**（不借别人的事务），所以本文件的写入是真实
落库的，不能靠外层事务回滚。每个用例用随机 UUID 造两位长者，结束时按 UUID 精确删除
自己造的行——不 TRUNCATE、不碰任何既有数据。跑之前必须设
`ANYU_TEST_DATABASE_URL` 指向 `anyu_test`（AGENTS §9：裸跑会 TRUNCATE 开发库）。

## 反向证据（人工做过一次，不在用例里）

把 `memory_service.recall` 里 `MemoryChunk.elder_id == elder_id` 这一行删掉，
`test_isolation_is_enforced_by_the_sql_filter_not_the_application` 必红
（B 的召回会拿到 A 的行）——这条负向验证是本文件「隔离不是应用层筛完再断言空」的凭据。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_reachable

from app.agents.companion import AGENT_SYSTEM_PROMPT, PRIVACY_NOTICE_TEXT, _memory_block
from app.core.config import get_settings
from app.core.context_build import build_agent_context
from app.models import Elder, MemoryChunk
from app.services import memory_service, memory_writeback
from app.services.embedding import DeterministicEmbeddingProvider
from app.services.llm_service import LLMReply
from app.services.memory_turn_hook import MemoryTurnHook
from app.services.memory_writeback import WritebackResult

#: 长者原话（任务卡指定）。注意**不能**把提取出的正文写成「是糖尿病」——那会命中
#: `alert_aggregation.FORBIDDEN_WORDING` 被代码级过滤丢掉；「自述有」是对事实的记录，
#: 不是诊断断言。
UTTERANCE = "我有糖尿病"
MEMORY_TEXT = "长者自述有糖尿病"
B_MEMORY_TEXT = "李长顺喜欢听京剧《空城计》。"
ASSISTANT_TEXT = "记住了，我帮您留意着。"


# ---------------------------------------------------------------- 替身（只在进程外边界）


class _FakeExtractionLLM:
    """只实现 `chat`：把「模型该回的 JSON」写死，供 `extract_memories` 真解析。"""

    def __init__(self, *contents: str) -> None:
        self._payload = json.dumps(
            [
                {
                    "content": content,
                    "category": "private",
                    "source": "explicit",
                    "importance_hint": 0.9,
                }
                for content in contents
            ],
            ensure_ascii=False,
        )
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        self.calls.append([dict(message) for message in messages])
        return LLMReply(text=self._payload, degraded=False)


# ---------------------------------------------------------------- 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """真引擎（`ANYU_TEST_DATABASE_URL` 已由 `tests/conftest.py` 改写进 DATABASE_URL）。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    await require_db_reachable(engine)  # 没库就 fail，不 skip（conftest 的口径）
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """生产同款：每次调用开一个 session；`MemoryTurnHook` 要的就是这个形状。"""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def deterministic_provider(monkeypatch: pytest.MonkeyPatch) -> DeterministicEmbeddingProvider:
    """写回路径走进程内单例，换成 deterministic，避免打真 embedding API。"""
    provider = DeterministicEmbeddingProvider()
    monkeypatch.setattr(memory_writeback, "_EMBEDDING_PROVIDER", provider)
    return provider


@pytest.fixture
async def residents(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """王奶奶 + 李爷爷；用例结束按 UUID 精确删除（写入是真 commit，回滚救不了）。"""
    elder_a, elder_b = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as session:
        await session.execute(
            Elder.__table__.insert(),
            [
                {"elder_id": elder_a, "name": "王桂英", "status": "active"},
                {"elder_id": elder_b, "name": "李长顺", "status": "active"},
            ],
        )
        await session.commit()

    try:
        yield elder_a, elder_b
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(MemoryChunk).where(MemoryChunk.elder_id.in_([elder_a, elder_b]))
            )
            await session.execute(delete(Elder).where(Elder.elder_id.in_([elder_a, elder_b])))
            await session.commit()


# ---------------------------------------------------------------- 小工具


async def _say(
    session_factory: async_sessionmaker[AsyncSession],
    elder_id: uuid.UUID,
    *,
    thread_id: str,
    memory_text: str,
    utterance: str = UTTERANCE,
) -> WritebackResult:
    """跑一轮真实的「提取 → 写回」；`None` 说明链路内部把异常吞了（日志里会有痕迹）。"""
    hook = MemoryTurnHook(
        llm=_FakeExtractionLLM(memory_text),
        session_factory=session_factory,
    )
    result = await hook(
        elder_id=str(elder_id),
        thread_id=thread_id,
        user_text=utterance,
        assistant_text=ASSISTANT_TEXT,
    )
    assert result is not None, "MemoryTurnHook 吞掉了异常（看 memory writeback failed 日志）"
    return result


class _SameTopicProvider(DeterministicEmbeddingProvider):
    """把任意文本都编码成 `topic` 的向量：显式造出「同一个话题」的几何关系。

    deterministic provider 的向量是 SHA-256 噪声（跨文本余弦 ±0.05），召回加上相关性
    下限后，「用『糖尿病』查『长者自述有糖尿病』」会被挡掉。真实 provider 下这两句确实
    同话题，所以这里只替换「相似度怎么来的」，链路本身（写库 → 新会话 → 注入上下文）
    一步不少；闸门数值另有 `tests/test_memory_service.py` 的用例钉住。
    """

    def __init__(self, topic: str = MEMORY_TEXT) -> None:
        super().__init__()
        self.topic = topic

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await super().embed([self.topic for _ in texts])


async def _rows(session: AsyncSession, elder_id: uuid.UUID) -> list[MemoryChunk]:
    scalars = await session.scalars(
        select(MemoryChunk).where(MemoryChunk.elder_id == elder_id).order_by(MemoryChunk.created_at)
    )
    return list(scalars)


async def _recall(
    session_factory: async_sessionmaker[AsyncSession],
    elder_id: uuid.UUID,
    *,
    query: str = "糖尿病",
    topic: str = MEMORY_TEXT,
) -> list[MemoryChunk]:
    """换一条**新连接**做召回，模拟「下一场会话」：不共享任何会话内缓存。"""
    async with session_factory() as session:
        hits = await memory_service.recall(
            session,
            elder_id=elder_id,
            query=query,
            embedding_provider=_SameTopicProvider(topic),
        )
        await session.commit()  # recall 会把 last_accessed 刷成现在（真写入）
    return list(hits)


# ---------------------------------------------------------------- ① 真写库


async def test_utterance_really_lands_in_the_database(
    session_factory: async_sessionmaker[AsyncSession],
    residents: tuple[uuid.UUID, uuid.UUID],
) -> None:
    elder_a, _ = residents

    result = await _say(
        session_factory, elder_a, thread_id="thread-a-night-1", memory_text=MEMORY_TEXT
    )

    assert len(result.written) == 1, "提取到的候选没写进去（看 skipped_duplicate / failed）"
    assert result.failed == 0

    # 换一条连接读：证明不是「看到自己事务里那条」，而是真的 commit 进了库
    async with session_factory() as session:
        rows = await _rows(session, elder_a)

    assert len(rows) == 1
    row = rows[0]
    assert row.content == MEMORY_TEXT
    assert row.status == "active"
    assert row.source_thread_id == "thread-a-night-1"  # 可回溯到哪一轮
    assert row.embedding is not None  # 召回要有向量，这是硬前提


# ---------------------------------------------------------------- ② 新会话被提起


async def test_new_session_recalls_it_and_it_reaches_the_agent_context(
    session_factory: async_sessionmaker[AsyncSession],
    residents: tuple[uuid.UUID, uuid.UUID],
) -> None:
    elder_a, _ = residents
    await _say(session_factory, elder_a, thread_id="thread-a-night-1", memory_text=MEMORY_TEXT)

    # 「隔天」= 新连接 + 新 thread_id；记忆归属 elder_id，不归属 thread_id（recall 不收 thread_id）
    hits = await _recall(session_factory, elder_a)
    assert [hit.content for hit in hits] == [MEMORY_TEXT]

    # 召回结果要真的能进这一轮的模型上下文，否则「召回了」只是库里的自娱自乐
    block = _memory_block([hit.content for hit in hits])
    assert MEMORY_TEXT in block
    messages = build_agent_context(
        history=(),
        prefix=AGENT_SYSTEM_PROMPT,
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        memory_block=block,
    )
    assert messages[0] == {"role": "system", "content": AGENT_SYSTEM_PROMPT}
    assert any(MEMORY_TEXT in message["content"] for message in messages)


# ---------------------------------------------------------------- ③ 他人查不到（SQL 层）


async def test_isolation_is_enforced_by_the_sql_filter_not_the_application(
    session_factory: async_sessionmaker[AsyncSession],
    residents: tuple[uuid.UUID, uuid.UUID],
) -> None:
    elder_a, elder_b = residents
    await _say(session_factory, elder_a, thread_id="thread-a-night-1", memory_text=MEMORY_TEXT)

    # 李爷爷的会话里查不到：先证明「库里根本没有他的行」，再证明召回真返回空
    async with session_factory() as session:
        assert await _rows(session, elder_b) == []
        # 同一条正文在库里只有王奶奶一个属主 —— 所以 B 拿到空结果**只能**是 WHERE elder_id 挡的，
        # 不可能是「应用层筛完再断言空」：应用层拿到的候选集本来就只有 A 的行。
        owners = set(
            (
                await session.execute(
                    select(MemoryChunk.elder_id).where(
                        MemoryChunk.content == MEMORY_TEXT,
                        MemoryChunk.status == "active",
                        MemoryChunk.category.in_(memory_service.RECALL_CATEGORIES),
                        MemoryChunk.embedding.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert owners == {elder_a}

    hits = await _recall(session_factory, elder_b)
    assert hits == [], "李爷爷召回到了王奶奶的记忆：elder_id 过滤失效（红线 9）"


# ---------------------------------------------------------------- ④ 双向：各看各的


async def test_both_elders_only_ever_recall_their_own_memory(
    session_factory: async_sessionmaker[AsyncSession],
    residents: tuple[uuid.UUID, uuid.UUID],
) -> None:
    elder_a, elder_b = residents
    await _say(session_factory, elder_a, thread_id="thread-a-night-1", memory_text=MEMORY_TEXT)
    await _say(
        session_factory,
        elder_b,
        thread_id="thread-b-day-2",
        memory_text=B_MEMORY_TEXT,
    )

    hits_a = await _recall(session_factory, elder_a, query="糖尿病", topic=MEMORY_TEXT)
    hits_b = await _recall(session_factory, elder_b, query="京剧", topic=B_MEMORY_TEXT)

    assert [hit.content for hit in hits_a] == [MEMORY_TEXT]
    assert [hit.content for hit in hits_b] == [B_MEMORY_TEXT]


def test_memory_text_does_not_trip_the_code_level_diagnosis_filter() -> None:
    """守住用例自身的语义：正文若命中 `FORBIDDEN_WORDING`，会被提取层静默丢掉，用例变成假绿。"""
    from app.services.memory_extraction import _drop_reason

    assert _drop_reason(MEMORY_TEXT) is None
    assert _drop_reason("您这是糖尿病") == "diagnosis"
