"""会话事件流的**事务适配器**：一次事件一个短事务，永不抛。

## 为什么需要一个适配器

`app/services/conversation_event_service.py` 是唯一写入口，但它**不 commit**（事务边界
归调用方，见该模块 docstring）。对话链路的调用方与 API 不同：

- `DialogueRuntime.on_asr_final` 是从 WS 的 ASR 终稿回调里进来的，**没有请求作用域**，
  拿不到 `get_session` 依赖注入的那条会话；
- 这条链路对失败的容忍度与 API 相反：**一条事件写不进去，绝不能让这一轮对话不成立**。
  长者说话、模型回话、音频播放是主链路；事件流是回放与审计的底座，属旁路。

所以这里把「开事务 → 调服务 → 提交 → 异常收敛」封成一次调用，主链路拿到的是
`bool`，永远不是异常。

## 口径

- **一条事件一个事务**：不复用长事务，避免与主链路的读取互相拖住；事件之间没有原子性
  要求（回放按 `seq` 排序，缺一条是缺一条，不会被读成错序）。
- **失败只记日志不重试**：重试会在主链路上叠加延迟；`seq` 冲突由服务层内部重试一次兜住，
  再失败说明是结构性问题，重试也没用。
- **`thread_id` / `elder_id` 由调用方从服务端上下文取**：本模块不接受客户端传值，
  也不做任何身份推导（AGENTS §4-9）。

## 与 `append_events` 的分工

同一轮对话要写多条时，调用方可以自己开一个会话调 `append_events` 拿一次取号区间；
本模块的 `record_many` 是给「已经拿到一批事件、但不想自己管事务」的旁路用的。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import conversation_event_service
from app.services.conversation_event_service import NewEvent

logger = logging.getLogger(__name__)

#: 会话工厂的签名：`async_sessionmaker[AsyncSession]` 的 `__call__`。
SessionFactory = Callable[[], AsyncSession]


class ConversationEventRecorder:
    """把事件写入包成「短事务 + 失败收敛」的一次调用。

    构造时注入会话工厂（生产是 `app.db.session_factory`，测试可传假的）；
    不持有连接，每次 `record` 自己开一条，用完就关。
    """

    def __init__(self, session_factory: SessionFactory | async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        thread_id: uuid.UUID | str,
        elder_id: uuid.UUID | str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> bool:
        """写一条事件。成功返回 True，任何异常返回 False（只记日志）。"""
        try:
            async with self._session_factory() as session:
                await conversation_event_service.append_event(
                    session,
                    thread_id=thread_id,
                    elder_id=elder_id,
                    event_type=event_type,
                    payload=payload,
                    trace_id=trace_id,
                )
                await session.commit()
        except Exception as exc:
            # 事件流写不进去不是 P0：主链路照常，但必须留痕，否则「回放缺段」无法定位。
            logger.warning(
                "conversation_event_write_failed",
                extra={
                    "event_type": event_type,
                    "thread_id": str(thread_id),
                    "elder_id": str(elder_id),
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                },
            )
            return False
        return True

    async def record_many(
        self,
        *,
        thread_id: uuid.UUID | str,
        elder_id: uuid.UUID | str,
        events: Sequence[NewEvent],
        trace_id: str | None = None,
    ) -> bool:
        """一批事件写一个事务（只取一次号）。空序列直接返回 True。"""
        if not events:
            return True
        try:
            async with self._session_factory() as session:
                await conversation_event_service.append_events(
                    session,
                    thread_id=thread_id,
                    elder_id=elder_id,
                    events=events,
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "conversation_events_write_failed",
                extra={
                    "count": len(events),
                    "thread_id": str(thread_id),
                    "elder_id": str(elder_id),
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                },
            )
            return False
        return True


__all__ = ["ConversationEventRecorder", "SessionFactory"]
