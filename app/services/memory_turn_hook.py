"""N27b：把「一轮对话」接成一条长期记忆。

`memory_extraction` 是纯函数（不认识数据库），`memory_writeback` 是存储编排
（不认识一轮对话长什么样）。这两件事要串起来、又要能在**回复已经发出之后**
悄悄跑完，就需要一个中间层 —— 本模块只做这一件事。

职责边界（三条，都很容易被后来的人改坏）：

1. **从不抛**。记忆写回失败只该少一条记忆，绝不能让长者听见回复的路径受影响。
   调用方（`DialogueRuntime`）还在事件循环里跑，这里漏出的异常会变成
   「asyncio task exception was never retrieved」——主链路看起来没事，
   但每次都被静默吞掉，排障时没有任何痕迹。所以这里收敛成「打一条日志 + 返回 None」。
2. **自己管事务**。`writeback_memories` 只 flush 不 commit（它的契约就是「事务归调用方」），
   而本模块跑在对话主链路之外，没有别人的事务可以借，所以 `async with session` 出来自己提交。
3. **不在提取阶段就写库**。等价于「模型说不清就不记」：`extract_memories` 返回空列表时
   直接返回，连 session 都不开 —— 省一次连接，也让「没记」这件事和「记失败」区分得开。

刻意不做的事：不在这里做去重、不做降权、不做容量淘汰。那三件事都在
`memory_writeback` 里（各自有测试），这里再做一遍就是第二套口径。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅为类型检查与阅读
    from app.services.memory_extraction import MemoryCandidate
    from app.services.memory_writeback import WritebackResult

logger = logging.getLogger(__name__)


class MemoryTurnHook:
    """一轮对话的长期记忆写回器，供 `DialogueRuntime` 在后台调用。

    `session_factory` 用可调用对象注入（生产是 `app.db.session_factory`），
    这样本模块既不 import 全局引擎、也能在测试里换成内存替身。
    """

    def __init__(
        self,
        *,
        llm: Any,
        session_factory: Any,
        extract_memories: Any = None,
        writeback_memories: Any = None,
    ) -> None:
        self._llm = llm
        self._session_factory = session_factory
        # 两个纯函数允许注入：测试要能构造「提取成功/失败」「写回成功/失败」的四种组合，
        # 而不必真的起一个 LLM 或连库。缺省走真实实现，见 `_load_defaults()`。
        self._extract_memories = extract_memories
        self._writeback_memories = writeback_memories

    # ---------------------------------------------------------------- 默认实现
    def _load_defaults(self) -> None:
        """惰性导入真实实现。放在这里而不是模块顶层，是为了让「只想用替身」的
        测试不必因为 `memory_extraction` 的 import 成本（它会拉 LLM 提示词）
        付出代价，也让本模块在上层依赖坏掉时仍可被导入。"""
        if self._extract_memories is None:
            from app.services.memory_extraction import extract_memories

            self._extract_memories = extract_memories
        if self._writeback_memories is None:
            from app.services.memory_writeback import writeback_memories

            self._writeback_memories = writeback_memories

    # ---------------------------------------------------------------- 入口
    async def __call__(
        self,
        *,
        elder_id: str,
        thread_id: str,
        user_text: str,
        assistant_text: str | None = None,
    ) -> WritebackResult | None:
        """提取并写回一轮的长期记忆。成功返回写回统计，任何失败返回 `None`。"""
        text = (user_text or "").strip()
        if not text:
            # 空转写不代表长者说了什么，提取只会把模型的想象力放进来。
            return None

        try:
            self._load_defaults()
            candidates: list[MemoryCandidate] = await self._extract_memories(
                self._llm, user_text=text, assistant_text=assistant_text
            )
            if not candidates:
                # 模型判断「这一轮没有值得长期记住的事」是正常结果，不是失败。
                logger.debug(
                    "memory extraction empty",
                    extra={"thread_id": thread_id, "elder_id": elder_id},
                )
                return None

            async with self._session_factory() as session:
                try:
                    result: WritebackResult = await self._writeback_memories(
                        session,
                        elder_id=elder_id,
                        candidates=candidates,
                        source_thread_id=thread_id,
                    )
                except BaseException:
                    # 写回半途失败：先把事务退干净再往上交，否则连接会带着
                    # 「事务已被中止」的状态回池，下一次用它的请求直接炸。
                    await session.rollback()
                    raise
                await session.commit()

        except Exception as exc:  # 契约就是「从不抛」，见模块 docstring ①
            logger.warning(
                "memory writeback failed",
                extra={
                    "thread_id": thread_id,
                    "elder_id": elder_id,
                    "error_type": type(exc).__name__,
                },
            )
            return None

        logger.info(
            "memory writeback done",
            extra={
                "thread_id": thread_id,
                "elder_id": elder_id,
                "candidates": len(candidates),
                "written": result.written,
                "skipped_duplicate": result.skipped_duplicate,
                "skipped_filtered": result.skipped_filtered,
                "capacity_evicted": result.capacity_evicted,
            },
        )
        return result
