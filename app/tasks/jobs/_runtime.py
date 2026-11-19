"""Celery worker 的同步↔异步桥：**一个进程一个事件循环**。

## 为什么不能每条任务 `asyncio.run`

`asyncio.run` 每次都新起一个 loop 并关掉它。asyncpg 的连接池把连接绑在「创建它的
loop」上，池本身是进程级单例（`app.db.session_factory`）：第一条任务在新 loop 里
建了连接，任务结束 loop 关闭；第二条任务再起一个 loop 去复用那条连接，就会炸
`AttributeError: 'NoneType' object has no attribute 'send'`（实测于 `anyu_test`）。

所以这里保留一个常驻 loop：第一次调用创建，之后所有任务都在同一个 loop 上
`run_until_complete`，连接池与 loop 的生命周期一致。

## 为什么放在下划线模块

`app/tasks/celery_app.py` 的自动发现会跳过 `_` 开头的模块（见 `app/tasks/jobs/__init__.py`），
本模块因此不会被当成 jobs 模块去要 `register` / `BEAT_ENTRIES`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """取本进程的常驻 loop；被外部关掉（如某些测试）则重建。"""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_coro[T](coro: Awaitable[T]) -> T:
    """在当前线程的常驻 loop 上跑完一个 coroutine 并返回结果。"""
    return _get_loop().run_until_complete(coro)


def sync_task[T](task_fn: Callable[..., Awaitable[T]], task_name: str) -> Callable[..., T]:
    """把 `async def task(...)` 包成 Celery 能注册的同步可调用，并包一层 `task.<短名>` span。

    为什么 span 放这里而不是每个 jobs 模块各写一份：worker 侧的可观测性（计划 §6-4 A′）
    要求「一次任务执行对应一条 trace」，而五个模块的同步适配层形状完全一样——放在
    `_runtime` 就只有一处需要维护，也保证 `task.name` 属性口径统一（FR-TC-04）。

    span 在 provider 关掉时是 no-op（`app.core.observability.span` 返回空 scope），
    因此单测里不会因为没配 Grafana 而多线程/多网络。
    """

    def _run(*args: Any, **kwargs: Any) -> T:
        from app.core.observability import task_span

        trace_id = kwargs.get("trace_id")
        with task_span(task_name, trace_id=trace_id if isinstance(trace_id, str) else None):
            return run_coro(task_fn(*args, **kwargs))

    _run.__name__ = f"sync_{getattr(task_fn, '__name__', 'task')}"
    _run.__doc__ = getattr(task_fn, "__doc__", None)
    return _run


__all__ = ["run_coro", "sync_task"]
