"""记忆召回接线（服务层一半）：把「本轮身份」翻译成「一个绑定 elder_id 的召回闭包」。

## 为什么需要这一层

长期记忆的**写**早已接通（`memory_turn_hook` → `memory_writeback` → `memory_service.remember`），
**读**却断在中间：`runtime_factory.build_agent_runner` 在 startup 建的是进程级单例，
`AgentRunner.run_turn(runtime_context=None)` 有意只收一段文本，而
`memory_service.recall` 的签名强制 `elder_id: uuid.UUID` —— 服务端判定的合法身份到不了
召回层。本模块补的就是这一段：入参是 `DialogueIdentity`，出参是一个只认「这一句话」的
`Recall`（或 `None`）。

## 为什么做成闭包，而不是把 elder_id 加进 `run_turn`

红线 9：`elder_id` 只能由服务端上下文注入，客户端与模型传入的值一律丢弃。闭包把
`elder_id` **钉在创建它的那一刻**；之后上层拿到的只是 `(str) -> Awaitable[Sequence[str]]`，
没有任何一个参数能把别人家的 id 塞进来。若改成「每轮把 elder_id 传进去」，就等于在
`AgentRunner` 上开了一个「谁能填这个参数」的口子 —— 而那正是要堵的那条缝。

## 为什么不 import `app/agents/companion.py` 的 `MemoryRecaller`

依赖方向单向：`agents → services → models`（AGENTS §2.2 硬约束）。为了复用一行
`Callable` 别名让 services 反向依赖 agents，不值得。这里**结构等价**地重写一份 `Recall`
（与 `MemoryRecaller = Callable[[str], Awaitable[Sequence[str]]]` 同形），运行期没有任何
耦合：两边可以互相传参，静态检查按结构判等。

## 返回 `None` 与「返回一个总是空列表的闭包」不是一回事

`companion._recall_memories` 的调用点是 `if memory_recall is not None and not converged`。
`None` 表示「这一轮根本没有召回能力」（generic 身份 / 身份不可信），连记忆块都不拼；
`[]` 表示「召回了、但没命中」。把 generic 做成空闭包会让上层以为「查过了，没有记忆」，
日志与埋点都会失真；更要命的是，AC-28 的「通用模式不读个人数据」会从「没读」退化成
「读了但没读到」—— 前者能静态审计，后者不能。

## 异常一律上抛（本层不吞）

会话工厂配错、库挂了，这一轮召回就该失败，**执行期**的降级语义只有一处收敛点：
`companion._recall_memories`（AGENTS §6：「召回失败 → 本轮不加载记忆，不得臆造」）。
在这一层再 catch 一次，等于制造第二个口径可能不同的降级策略，还会把「session_factory
接错了」这种启动期就该暴露的问题藏到运行期。唯一被吞掉的 `EmbeddingUnavailable` 在
`memory_service.recall` 内部（`_embed_text` 把它降级成 `[]` 并打点），不在本模块。

**构造期**（本函数本身）不碰 provider、不碰库，所以它不该抛；调用方若为了「工厂构造
失败也要让这一轮照跑」而另加兜底，那是调用方的降级策略（与执行期的口径分开记账），
不改变本模块「执行期不吞」这一条。

## 日志只带身份与计数，不带正文

记忆正文是最敏感的一类个人数据（红线 10）：`memory_recall_built` 只记
`elder_id` / `source` / `top_k`，跳过只记 `reason`。**任何一条日志都不许出现记忆内容**，
否则「谁被想起过」会随日志外泄。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import DialogueIdentity
from app.services import memory_service
from app.services.embedding import EmbeddingProvider, build_embedding_provider

logger = logging.getLogger("app.services.memory_recall")

#: 每轮召回的条数上限。与 `memory_service.recall` 的默认值同源，
#: 也对应技术设计文档 §6.3 ⑤ 的「pgvector top5」。
RECALL_TOP_K: Final[int] = 5

#: 召回闭包：入参是本轮原句，出参是**已脱敏**的记忆正文（可直接拼进 system）。
#: 故意不 import `companion.MemoryRecaller`：结构相同即可，方向不能反（见模块 docstring）。
Recall = Callable[[str], Awaitable[Sequence[str]]]

#: 每轮召回器的工厂：入参是本轮身份，出参是一个只认「这一句话」的 recaller（或 None）。
RecallFactory = Callable[[DialogueIdentity], Recall | None]

#: embedding provider 进程内缓存：Http 实现每次新建会漏连接池（同 `memory_writeback`
#: 与 `api/memory.py` 的口径）。单测直接替换这个全局，避免打真实网络。
_EMBEDDING_PROVIDER: EmbeddingProvider | None = None


def _embedding_provider() -> EmbeddingProvider:
    """取（并缓存）embedding provider；单测用它替换成 deterministic。

    **惰性**而不是在模块导入时构造：`build_embedding_provider()` 要读配置并可能建
    HTTP 客户端，而本模块会在 `main.py` 装配期被 import —— 那时不该为了「也许这一轮
    会召回」去碰网络配置。缓存是为了让第二轮以后不再读配置。
    """
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is None:
        _EMBEDDING_PROVIDER = build_embedding_provider()
    return _EMBEDDING_PROVIDER


def build_memory_recaller(
    identity: DialogueIdentity,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    embedding_provider: EmbeddingProvider | None = None,
    top_k: int = RECALL_TOP_K,
) -> Recall | None:
    """按本轮身份造一个绑定 `elder_id` 的召回闭包；身份不可用时返回 `None`。

    三个返回 `None` 的口子都**不抛**：身份不可用是正常业务分支（未识别身份 /
    上游塞了坏值），不该让这一轮对话挂掉 —— 上层拿到 `None` 就整轮不加载记忆，
    与 AGENTS §6 的降级口径一致。

    - `session_factory`：每次召回**新建**一个会话（`async with ... as session`）。
      不共享请求级 session：召回在 turn 内只读一段、写完 `last_accessed` 就提交，
      与主链路的长事务分开，才不会因为召回卡住而推迟整轮落库。
    - `embedding_provider`：缺省时惰性取模块级缓存（见 `_embedding_provider`）。
    - `top_k`：透传给 `memory_service.recall`；非正值由 `recall` 抛 `ValueError`
      （那属于编程错误，不该被静默夹成 0 条）。

    日志 `memory_recall_built` 带上 `elder_id` / `source` / `top_k`，**不带**任何正文。
    """
    if identity.is_generic or not identity.elder_id:
        # AC-28：通用模式不读个人数据。这里连 SQL 都不发（不是「查了返回空」）。
        logger.info(
            "memory_recall_skipped",
            extra={"reason": "generic_identity", "source": identity.source},
        )
        return None

    try:
        elder_uuid = uuid.UUID(str(identity.elder_id))
    except (TypeError, ValueError, AttributeError):
        # 坏 id 只跳过、不抛：它来自上游判定链路，接口坏值的正确处置是「这轮不召回」。
        # 不把原值写进日志：那是未经校验的字符串（可能含换行，日志注入面）。
        logger.warning(
            "memory_recall_skipped",
            extra={"reason": "invalid_elder_id", "source": identity.source},
        )
        return None

    async def _recall(text: str) -> list[str]:
        """取该长者与本轮原句最相关的 top-k 条正文（已剔除空白条目）。

        异常**不在此处收敛**：把 `EmbeddingUnavailable` / 数据库错误留到
        `companion._recall_memories` 统一定义「本轮无记忆」，避免两处口径分叉。
        """
        provider = embedding_provider if embedding_provider is not None else _embedding_provider()
        async with session_factory() as session:
            rows = await memory_service.recall(
                session,
                elder_id=elder_uuid,
                query=text,
                embedding_provider=provider,
                top_k=top_k,
            )
            # `recall` 只刷新 `last_accessed`、不提交（事务边界归调用方）；闭包自己开
            # 的会话就必须自己提交，否则「这次被想起过」会在 close 时丢掉，衰减与容量
            # 淘汰会继续把刚召回的条目当冷数据。
            await session.commit()
        return [chunk.content.strip() for chunk in rows if chunk.content and chunk.content.strip()]

    logger.info(
        "memory_recall_built",
        extra={"elder_id": str(elder_uuid), "source": identity.source, "top_k": top_k},
    )
    return _recall


__all__ = [
    "RECALL_TOP_K",
    "Recall",
    "RecallFactory",
    "build_memory_recaller",
]
