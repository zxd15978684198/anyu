"""机构知识召回接线（服务层一半）：把「一句话」翻译成「一次带相关性闸门与注入防护的检索」。

## 为什么需要这一层

`knowledge_service.search_knowledge_result` 只回答「库里哪几块最相关」；对话链路要的是
「这句话进来，能不能给模型一段**可用的**参考资料」。两者之间差三件事，而这三件事都属于
服务层，不该散在 `agents/`、`graphs/` 或 `context_build` 里各写一遍：

1. **相关性闸门**：不设阈值时，库非空就恒有 k 条命中（`ORDER BY <=> LIMIT k`），
   「库里没有的信息」永远命中，AC-47 的 `no_match` 走不到。闸门 `DEFAULT_MAX_DISTANCE`
   在这里定，作为**默认值**下发给检索（可调，见常量注释）。
2. **注入防护**：机构知识是外部录入的正文，检索出来必须过
   `app/core/knowledge_guard.guard_knowledge_hits`（与 FR-TC-07 同一族检测器），
   脏条目丢弃且**绝不进上下文**。
3. **降级收敛**：会话工厂配错、库挂了、provider 抽风，都统一变成
   `KnowledgeLookupOutcome(hits=(), reason="degraded")` 并记一条不带正文的 warning。

## 为什么做成闭包

与 `memory_recall.build_memory_recaller` 同形（结构等价的 `Callable`）：上层拿到的只是
`(str) -> Awaitable[KnowledgeLookupOutcome]`，拿不到 session、也拿不到 provider ——
会话生命周期与 provider 实例都钉在创建它的那一刻，调用方没有任何参数能把别的东西塞进来。

## 为什么不 import `app/agents/companion.py`

依赖方向单向：`agents → services → models`（AGENTS §2.2）。为了复用一行 `Callable`
别名让 services 反向依赖 agents 不值得；这里**结构等价**地重写一份 `KnowledgeLookup`，
运行期零耦合，静态检查按结构判等。

## 与 `memory_recall` 的差别：执行期**吞**异常（有意的）

`memory_recall` 的闭包把异常上抛，因为 `companion._recall_memories` 有唯一的降级收敛点；
知识这条链路没有这样一个「必定被调用的包装函数」：`context_build` 只做拼接，拿到异常
只能整轮失败。PRD FR-KNW-05 / AC-47 要的是「查不到就说没有」，**不是**「知识服务挂了
就让长者听不到话」。所以降级口径统一收敛在**这一层**：

- 执行期任何异常 → `hits=()` + `reason="degraded"` + 一条 `logger.warning`（**只带异常
  类型，不带 query 正文**，见 AGENTS §8）；
- `KnowledgeLookup` 因此签名上**永不抛**，调用方不需要、也**不允许**再包一层兜底
  （写两处 = 两个口径，日后必然分叉）；
- **构造期**（`build_knowledge_lookup` 本身）不碰 provider、不碰库，所以它不该抛；
  provider 是首次调用时才惰性构造的（见 `_embedding_provider`）。

## 日志只带计数与原因，不带正文

`knowledge_lookup_built` 只记 `max_distance` / `limit` / `provider_injected`；
`knowledge_looked_up` 只记 `reason` / `hits` / `blocked` / `query_length`；
失败只记 `error`（异常类名）。**任何一条日志都不许出现长者原话或知识正文**。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.knowledge_guard import guard_knowledge_hits
from app.services import knowledge_service
from app.services.embedding import EmbeddingProvider, build_embedding_provider

logger = logging.getLogger("app.services.knowledge_lookup")

#: 默认相关性闸门：余弦距离 ≤ 0.40（相似度 ≥ 0.60）才算「这段资料真的相关」。
#:
#: 这是**相关性闸门**，不是 rerank：不做二次打分、不重排、不重新召回，只把
#: `distance > max_distance` 的命中丢掉。0.40 是演示数据集上「同义改写仍相关」与
#: 「同一领域但换了话题」之间的分界（可调；调它只影响召回宽严，不影响 correctness）。
#: `None` = 不设闸门（回到旧行为，仅用于对照/调试，生产不该这么配）。
DEFAULT_MAX_DISTANCE: Final[float | None] = 0.40

#: 每轮最多给模型几块知识。与 `knowledge_service.DEFAULT_SEARCH_LIMIT` 同值（5），
#: 对应技术设计文档 §3.6 的 top-5 口径。
DEFAULT_LOOKUP_LIMIT: Final[int] = knowledge_service.DEFAULT_SEARCH_LIMIT

#: 召回闭包：入参是本轮原句，出参是「命中 + 原因 + 被拦条数」，**永不抛**。
#: 故意不 import `companion` 里的同名别名：结构相同即可，方向不能反（见模块 docstring）。
KnowledgeLookup = Callable[[str], Awaitable["KnowledgeLookupOutcome"]]


@dataclass(frozen=True, slots=True)
class KnowledgeLookupOutcome:
    """一次知识召回的结果。`hits` 是 `KnowledgeHit` 元组，可直接进上下文。

    Attributes:
        hits: 通过闸门且未被注入防护拦下的命中（已按 distance 升序，最多 `limit` 条）。
        reason: `ok` / `no_match` / `empty_query` / `degraded`，与
            `knowledge_service.KnowledgeSearchResult.reason` 同口径；「全被 guard 拦下」
            也归 `no_match`（这轮确实没有可用的参考资料），此时 `blocked > 0` 用于区分
            「库里没有」与「库里有但不可信」。
        blocked: 被注入防护丢弃的条数（0 表示没有脏条目）。调用方可据此打点/告警。
    """

    hits: tuple[Any, ...]
    reason: str
    blocked: int = 0


#: embedding provider 进程内缓存：Http 实现每次新建会漏连接池（与 `memory_recall`
#: 同一口径）。单测直接替换这个全局，避免打真实网络。
_EMBEDDING_PROVIDER: EmbeddingProvider | None = None


def _embedding_provider() -> EmbeddingProvider:
    """取（并缓存）embedding provider。

    **惰性**而不是在模块导入时构造：`build_embedding_provider()` 要读配置并可能建
    HTTP 客户端，而本模块会在 `main.py` 装配期被 import —— 那时不该为了「也许这一轮
    会检索」去碰网络配置。缓存是为了让第二轮以后不再读配置。
    """
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is None:
        _EMBEDDING_PROVIDER = build_embedding_provider()
    return _EMBEDDING_PROVIDER


def build_knowledge_lookup(
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    embedding_provider: EmbeddingProvider | None = None,
    max_distance: float | None = DEFAULT_MAX_DISTANCE,
    limit: int = DEFAULT_LOOKUP_LIMIT,
) -> KnowledgeLookup:
    """造一个「一句话进、命中出」的知识召回闭包。

    - `session_factory`：每次调用**新建**一个会话（`async with ... as session`）。
      不复用请求级 session：知识检索是只读的短查询，与主链路的长事务分开，才不会因为
      「查一段制度」把整轮落库推迟。**不需要 commit**（本路径不写库、不刷新访问时间）。
    - `embedding_provider`：缺省时首次调用才惰性取模块级缓存（见 `_embedding_provider`）。
    - `max_distance`：相关性闸门，默认 :data:`DEFAULT_MAX_DISTANCE`；`None` = 不设闸门。
    - `limit`：透传给 `search_knowledge_result`（1..20，越界由它抛 `KnowledgeValidationError`
      —— 那是编程错误，会在执行期被收敛成 `degraded`，不会拖垮对话）。

    返回的闭包：空白输入直接 `((), "empty_query")` 且**不连库**；命中先过
    `guard_knowledge_hits`；执行期任何异常 → `((), "degraded", 0)` + 一条不带正文的
    `logger.warning`。**永不抛**。
    """

    async def _lookup(text: str) -> KnowledgeLookupOutcome:
        """检索一句；所有失败路径都收敛成 outcome（见模块 docstring 的降级口径）。"""
        try:
            cleaned = text.strip()
            if not cleaned:
                # 空输入连库都不连：没有 query 就没有向量，connect 一次纯属浪费。
                logger.info("knowledge_lookup_skipped", extra={"reason": "empty_query"})
                return KnowledgeLookupOutcome(hits=(), reason="empty_query")

            provider = (
                embedding_provider if embedding_provider is not None else _embedding_provider()
            )
            async with session_factory() as session:
                result = await knowledge_service.search_knowledge_result(
                    session,
                    query=cleaned,
                    limit=limit,
                    embedding_provider=provider,
                    max_distance=max_distance,
                )

            verdict = guard_knowledge_hits(result.hits)
            reason = result.reason
            if result.hits and not verdict.kept:
                # 有候选但全被拦：这轮没有可用的参考资料，对外就是 no_match
                # （blocked 条数用于区分「库里没有」与「库里有但不可信」）。
                reason = "no_match"

            logger.info(
                "knowledge_looked_up",
                extra={
                    "reason": reason,
                    "hits": len(verdict.kept),
                    "blocked": verdict.blocked_count,
                    "query_length": len(cleaned),
                },
            )
            return KnowledgeLookupOutcome(
                hits=verdict.kept,
                reason=reason,
                blocked=verdict.blocked_count,
            )
        except Exception as exc:
            # 只记异常类型：query 正文与知识正文都不许进日志（AGENTS §8 / 红线 10）。
            logger.warning(
                "knowledge_lookup_failed",
                extra={"error": type(exc).__name__, "max_distance": max_distance, "limit": limit},
            )
            return KnowledgeLookupOutcome(hits=(), reason="degraded")

    logger.info(
        "knowledge_lookup_built",
        extra={
            "max_distance": max_distance,
            "limit": limit,
            "provider_injected": embedding_provider is not None,
        },
    )
    return _lookup


__all__ = [
    "DEFAULT_LOOKUP_LIMIT",
    "DEFAULT_MAX_DISTANCE",
    "KnowledgeLookup",
    "KnowledgeLookupOutcome",
    "build_knowledge_lookup",
]
