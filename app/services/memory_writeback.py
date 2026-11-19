"""记忆写入回填（N27b 服务层一半）：把「提取到的一批候选」落成真记忆。

## 与 `memory_service.remember` 的分工

- `remember()` 回答「**这一条**怎么落」：来源系数、supersede 冲突、embedding 降级；
- 本模块回答「**这一批**哪些该落」：内容级幂等、`inferred` 降权、容量收口、绝不抛。

之所以不把批量逻辑塞进 `remember()`：单条写入是 API `POST /api/memory/chunks` 的
落库路径，它需要「冲突就 supersede」；批量回填是**对话旁路**，它需要「同一句不必重复
记」。两种语义混在一个函数里，迟早会有人把旁路的幂等键当成主路的冲突判定。

## 幂等键 = 内容哈希，**不是** supersede

`_supersede_conflicts`（`memory_service.py:327`）的判据是「同长者 + 同类别 + active +
有向量」的最近一条**余弦相似度 > 0.9**，且旧行只是置 `superseded`、物理保留。它在
deterministic embedding 降级时只有字面完全相同的句子才会命中，配了真模型后近义改写也
会命中 —— 这适合表达「长者改口了，新的取代旧的」，**不适合**当幂等键：重放一次同一句
会得到「又 supersede 一次」而不是「本来就有、跳过」。

所以本模块在调用 `remember()` **之前**按 `(elder_id, sha256(归一化 content))` 查一次
「是否已有 active 行」：命中即 `skipped_duplicate += 1`，既不建行、也不 supersede。
顺序不能反 —— 先 `remember()` 再查重，库里会先多一行，后面的检查只来得及把它标掉。

## 并发窗口（本卡不修，先写清楚）

两个请求同时带同一句进来，都可能查到「还没有」然后各写一行。唯一的根治是数据库唯一
约束（如 `(elder_id, content_hash)` 部分唯一索引），那需要新增列 + 迁移，属于另一条线，
本卡**不加**。因此本模块的幂等是「顺序重放安全」，不是「并发线性一致」；调用方（对话
回填）是单轮串行触发的，实际撞车概率低，但不为零。

## 失败面：绝不向上抛

这是对话旁路：提取失败的正确行为是「这轮不记」，不是「这轮对话挂掉」。所以整批准备
失败（elder_id 非法、provider 构建失败、查重查询失败）返回全零结果；单条失败只跳过该
条。结果对象把两类「没写进去」分开计数：`skipped_filtered` 是**内容不合格**（归一化后
为空 / 超 200 字），`failed` 是**内容合格但库写不进**（约束、连接等）—— 混成一个数就
分不清该查 prompt 还是查存储。进入逐条处理后，不变量是每个候选恰好落进一个桶：
`len(candidates) == len(written) + skipped_duplicate + skipped_filtered + failed`
（整批准备失败时早退、返回全零，此时不变量不适用）。单条写入额外套一层 SAVEPOINT：DB 级错误（约束、连接）若直接撞在外层事务上会让整
个事务进入 aborted 状态，后面的条目即使合法也会全红 —— 用 `begin_nested()` 把失败隔离
在保存点里，其余候选照常尝试。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemoryChunk
from app.services import memory_service
from app.services.embedding import EmbeddingProvider, build_embedding_provider
from app.services.memory_extraction import MemoryCandidate

logger = logging.getLogger("app.services.memory_writeback")

#: 推断出来的记忆最高权重。模型从行为里「猜」的偏好，不该压过长者亲口说的
#: （讲义 §8.3）。注意这是**来源系数之前**的上限：`remember()` 还会再乘 0.6，
#: 所以 inferred 的落库上限实际是 0.5 × 0.6 = 0.30。
INFERRED_IMPORTANCE_CAP: Final[float] = 0.5

#: 归一化只做两件不改变语义的事：去首尾空白、把连续空白折成一个空格。
#: **不做 lower()**：中文没有大小写，英文人名/药名的大小写是有信息量的，改了就丢。
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: embedding provider 进程内单例：Http 实现每次新建会漏连接池（同 `api/memory.py`
#: 的口径）；单测直接替换这个全局，避免打真实网络。
_EMBEDDING_PROVIDER: EmbeddingProvider | None = None


@dataclass(frozen=True, slots=True)
class WritebackResult:
    """一批候选的落库结果。计数而不是明细：调用方只需知道「写了几条、跳了几条」。

    **不变量**（进入逐条处理后）：`len(candidates) == len(written) + skipped_duplicate
    + skipped_filtered + failed` —— 每个候选恰好落进一个桶，没有「悄悄消失」的条目。
    整批准备失败（elder_id 非法 / provider 构建失败 / 查重查询失败）是**早退**：一条都
    没处理，返回全零结果，此时不变量不适用 —— 这种情况看返回值全零 + 一条 warn 日志。
    四桶分开的理由是排障时要能区分三种完全不同的坏法：

    - `skipped_duplicate`：内容没问题，只是这句已经记过（幂等命中，不是故障）；
    - `skipped_filtered`：**内容不合格**（归一化后为空、超 200 字）—— 上游提取给坏了，
      该去查 prompt / `memory_extraction`；
    - `failed`：内容合格但**库写不进去**（约束、连接、provider 之外的写入异常）——
      该去查存储，与内容无关。

    混成一个数就分不清「模型抽风」和「库坏了」，所以拆分是排障口径而不是洁癖。

    `capacity_evicted` 用 `int | None` 而不是 `int`：`None` 表示**本轮没跑容量收口**
    （一条都没写进去时不值得多查一次库），`0` 表示跑了但没有条目被淘汰 —— 两者
    对排查「容量逻辑到底有没有生效」意义完全不同。
    """

    written: tuple[uuid.UUID, ...] = ()
    skipped_duplicate: int = 0
    skipped_filtered: int = 0
    failed: int = 0
    capacity_evicted: int | None = None


_EMPTY_RESULT: Final[WritebackResult] = WritebackResult()


def _embedding_provider() -> EmbeddingProvider:
    """取（并缓存）embedding provider；单测用它替换成 deterministic。"""
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is None:
        _EMBEDDING_PROVIDER = build_embedding_provider()
    return _EMBEDDING_PROVIDER


def normalise_content(content: str) -> str:
    """去首尾空白 + 折叠连续空白；供去重键与落库正文共用同一个口径。"""
    return _WHITESPACE_RE.sub(" ", (content or "").strip())


def content_key(content: str) -> str:
    """内容幂等键：归一化正文的 SHA-256（十六进制）。

    只取哈希不存原文：日志与结果对象里可以放心带键值，不会把记忆正文漏出去。
    """
    return hashlib.sha256(normalise_content(content).encode("utf-8")).hexdigest()


def _clamp_importance(value: object) -> float:
    """把候选里的 `importance_hint` 夹到 [0,1]；非数值不丢事实、回落到 1.0。

    回落 1.0 而不是 0：`importance_hint` 缺失/写坏是模型的噪声，不该因此让一条
    「长者亲口说的过敏」变成最容易被容量淘汰的那条。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    return min(1.0, max(0.0, float(value)))


async def _active_content_keys(session: AsyncSession, elder_id: uuid.UUID) -> set[str]:
    """一条查询取回该长者全部 active 正文并算哈希。

    为什么不按哈希查：`memory_chunks` 没有 hash 列，加列要迁移，本卡不做（见模块
    docstring 的并发窗口）。代价可控 —— 容量上限 500 条、每条 ≤200 字，一次
    `SELECT content` 的传输量在几十 KB 量级；比「逐条 exists 查询」少几十次往返。
    """
    rows = await session.execute(
        select(MemoryChunk.content).where(
            MemoryChunk.elder_id == elder_id,
            MemoryChunk.status == "active",
        )
    )
    return {content_key(content) for (content,) in rows.all()}


async def writeback_memories(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID | str,
    candidates: Sequence[MemoryCandidate],
    source_thread_id: str | None = None,
) -> WritebackResult:
    """把一批候选落库；**任何失败都不外抛**，最差返回全零结果。

    - `elder_id`：`UUID` 或字符串。非法字符串直接返回全零（没有合法长者就无处可写）。
      不做「从正文猜长者」这类推断 —— `elder_id` 只能由服务端上下文注入（红线 9）。
    - `candidates`：来自 `memory_extraction.extract_memories`；空序列不碰库、不建 provider。
    - `source_thread_id`：透传给 `remember`，用于回溯「这句话是哪一轮记下的」。

    写入顺序 = 候选顺序：先出现的先落，批内重复只留第一条（与提取层的口径一致）。
    事务边界由调用方掌握（本函数只 flush，不 commit）。
    """
    if not candidates:
        return _EMPTY_RESULT

    if isinstance(elder_id, uuid.UUID):
        elder_uuid = elder_id
    else:
        try:
            elder_uuid = uuid.UUID(str(elder_id))
        except (TypeError, ValueError, AttributeError):
            logger.warning("memory_writeback_bad_elder", extra={"reason": "invalid_uuid"})
            return _EMPTY_RESULT

    try:
        provider = _embedding_provider()
    except Exception as exc:  # provider 构建失败 = 一条也写不了，整批收敛成空结果
        logger.warning(
            "memory_writeback_unavailable",
            extra={"reason": "provider_init", "detail": type(exc).__name__},
        )
        return _EMPTY_RESULT

    try:
        existing = await _active_content_keys(session, elder_uuid)
    except Exception as exc:
        logger.warning(
            "memory_writeback_unavailable",
            extra={"reason": "dedupe_query", "detail": type(exc).__name__},
        )
        return _EMPTY_RESULT

    written: list[uuid.UUID] = []
    skipped_duplicate = 0
    skipped_filtered = 0
    failed = 0

    for candidate in candidates:
        text = normalise_content(candidate.content)
        # 归一化后为空 / 超长：这是**内容不合格**（上游提取给坏了），在进库前就挡下，
        # 记 `skipped_filtered`；与「内容没问题但库写不进」的 `failed` 分开统计。
        if not text or len(text) > memory_service.MAX_CONTENT_LENGTH:
            skipped_filtered += 1
            continue

        key = content_key(text)
        if key in existing:
            skipped_duplicate += 1
            continue

        importance = _clamp_importance(candidate.importance_hint)
        if candidate.source == "inferred":
            importance = min(importance, INFERRED_IMPORTANCE_CAP)

        try:
            # SAVEPOINT：单条 DB 级失败不会毒化整批（见模块 docstring「失败面」）。
            async with session.begin_nested():
                row = await memory_service.remember(
                    session,
                    elder_id=elder_uuid,
                    content=text,
                    embedding_provider=provider,
                    category=candidate.category,
                    importance=importance,
                    source=candidate.source,
                    source_thread_id=source_thread_id,
                )
        except Exception as exc:
            # 内容合格但写不进去（约束、连接等）：进 `failed` 而不是 `skipped_filtered`，
            # 否则排障时会把「库坏了」误当成「模型抽风」。只记类型不记正文（红线 10）。
            failed += 1
            logger.warning(
                "memory_writeback_item_failed",
                extra={
                    "elder_id": str(elder_uuid),
                    "reason": type(exc).__name__,
                    "source": candidate.source,
                },
            )
            continue

        written.append(row.chunk_id)
        # 批内去重：同批后续的同内容候选直接判重，不必回库再查一次。
        existing.add(key)

    capacity_evicted: int | None = None
    if written:
        try:
            capacity_evicted = await memory_service.enforce_capacity(session, elder_id=elder_uuid)
        except Exception as exc:
            # 容量收口失败不该让「已经写进去的记忆」变成失败：本批仍然是成功的，
            # 只是这一轮没做淘汰 —— 记为 None 让调用方能区分「没跑」与「跑了没淘汰」。
            logger.warning(
                "memory_writeback_capacity_failed",
                extra={"elder_id": str(elder_uuid), "detail": type(exc).__name__},
            )

    result = WritebackResult(
        written=tuple(written),
        skipped_duplicate=skipped_duplicate,
        skipped_filtered=skipped_filtered,
        failed=failed,
        capacity_evicted=capacity_evicted,
    )
    logger.info(
        "memory_writeback",
        extra={
            "elder_id": str(elder_uuid),
            "candidates": len(candidates),
            "written": len(written),
            "skipped_duplicate": skipped_duplicate,
            "skipped_filtered": skipped_filtered,
            "failed": failed,
            "capacity_evicted": capacity_evicted,
        },
    )
    return result


__all__ = [
    "INFERRED_IMPORTANCE_CAP",
    "WritebackResult",
    "content_key",
    "normalise_content",
    "writeback_memories",
]
