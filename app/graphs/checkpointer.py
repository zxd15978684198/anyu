"""LangGraph checkpointer：PostgreSQL 独立 schema `langgraph`（S-D8-5 / TD §7.2）。

## 为什么单独一层

技术设计文档要求「业务表与 LangGraph checkpoint **物理隔离**：独立 schema、独立连接池」。
业务表在 `public`，checkpoint 表落 `langgraph` —— 由 `langgraph-checkpoint-postgres`
的 `setup()` 建（`checkpoints` / `checkpoint_blobs` / `checkpoint_writes`），
与 26 张业务表同实例不同 schema，业务迁移完全不感知它。

## 为什么是「同步 saver + to_thread」而不是 AsyncPostgresSaver

`langgraph-checkpoint-postgres 3.2` 同时提供同步 `PostgresSaver` 与 `AsyncPostgresSaver`，
迭代 8 计划 S-D8-5 明确选了**同步驱动 psycopg + `asyncio.to_thread`**：一条连接、
一把 `threading.Lock`，读写都挪到线程池，事件循环不被 psycopg 的阻塞等待占住。
代价是 checkpoint 写串行化 —— 它们是毫秒级小事务，演示规模下远比引入第二条异步连接池划算。

## 为什么锁是 `threading.Lock` 而不是 `asyncio.Lock`

`asyncio.Lock` 绑定创建它的事件循环；本项目测试逐用例新建事件循环，
模块级 asyncio 锁会在第二个用例里炸「bound to a different event loop」。
对「保护一条同步连接」这件事，`threading.Lock` 才是语义正确的那把锁。

## 生命周期

- `init_checkpointer()` / `shutdown_checkpointer()` 由 `app/main.py` 的 lifespan 接线
  （启动预热、退出关连接）；**导入本模块不建连接**，与 `app/db.py` 同一口径。
- 连不上库时 `get_checkpointer()` 抛 `CheckpointerUnavailableError`，
  `health_checkup_graph.get_health_graph()` 会记 warning 并退回 `MemorySaver`
  （能力降级为「进程内可恢复」，不阻断接口）。

## 为什么 setup 不能留在请求路径里（2026-11-12 · task-11，P0）

`PostgresSaver.setup()` 里的 `CREATE INDEX CONCURRENTLY` 必须等**所有更早开始的事务**结束。
它在请求路径里跑时，请求自己的业务事务还没提交、又在等 setup 返回 —— 两边互等，
`pg_stat_activity` 上是 `wait_event=virtualxid` + `pg_blocking_pids` 指向**自己**，
请求永不返回（新库上「第一次点已服用」就是永久转圈）。

修法分两层，缺一不可：

1. **正常路径**：DDL 只在 lifespan 的 `init_checkpointer()` 里做。启动期没有业务事务，
   `CREATE INDEX CONCURRENTLY` 拿得到快照，几毫秒完事；请求路径只读已建好的表。
2. **兜底路径**（没跑 lifespan 的 TestClient / 启动时库不可用 / 多进程同起）：
   运行时**绝不允许无限等** —— 见 `_setup_saver_sync`：
   - 建表权用 `pg_try_advisory_lock` 抢，抢不到立刻放弃（不排队、不等待）；
   - 建表连接带 `statement_timeout`（5s），DDL 被别的长事务挡住时 5 秒内被数据库取消；
   - 两种情况都抛 `CheckpointerUnavailableError`（调用方已有降级分支），
     并把失败翻译成 `setup_skipped` / `setup_timeout` 两行结构化日志 ——
     「5 秒内明确失败」远好于「请求挂到天荒地老」。
   - 失败**不缓存半成品**：`_saver` 仍是 None，下一次调用可以重试。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from typing import Any, Final, TypeVar

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql

from app.core.config import get_settings

logger = logging.getLogger("app.graphs.checkpointer")

#: checkpoint 专属 schema；建表由 `PostgresSaver.setup()` 完成。
LANGGRAPH_SCHEMA = "langgraph"

#: 建表权的 advisory lock 键（ASCII "LGCP"，固定常量，跨进程一致）。
#: 只有拿到它的进程/请求才跑 DDL，避免多个 worker 同时 `setup()` 互相锁等待。
CHECKPOINTER_SETUP_LOCK_KEY: Final[int] = 0x4C474350

#: 建表连接上的 `statement_timeout`（毫秒）：DDL 必须「要么很快成功，要么很快失败」。
#: 5s 是 task-11 的验收上限（测试按 <6s 断言，留 1s 余量给连接与线程切换）。
SETUP_STATEMENT_TIMEOUT_MS: Final[int] = 5_000

#: 建连超时（秒）：库不可达时要立刻失败，不许把请求挂在 TCP 握手上。
SETUP_CONNECT_TIMEOUT_S: Final[int] = 5

#: asyncpg / psycopg 的 SQLAlchemy 方言前缀，统一还原成 libpq 认的 `postgresql://`。
_DRIVER_PREFIXES: tuple[str, ...] = (
    "postgresql+asyncpg://",
    "postgresql+psycopg://",
    "postgresql+psycopg_async://",
)

_T = TypeVar("_T")


class CheckpointerUnavailableError(RuntimeError):
    """checkpoint 后端不可用（连不上库 / `setup()` 失败）。

    只表达「这一层没起来」，不携带口令等连接细节（AGENTS §8 日志脱敏）。
    """


def sync_dsn(database_url: str | None = None) -> str:
    """把 `app.db` 用的异步连接串还原成 psycopg 认的同步连接串。

    与 `alembic/env.py` 同一思路（那里只替换 asyncpg，因为迁移只认 psycopg），
    这里把三种 SQLAlchemy 前缀都覆盖掉：测试可能用 `postgresql+psycopg://` 直连。
    """
    raw = (database_url or get_settings().database_url).strip()
    for prefix in _DRIVER_PREFIXES:
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def _ensure_schema_sync(dsn: str) -> None:
    """`CREATE SCHEMA IF NOT EXISTS langgraph`。

    没有它，带 `search_path=langgraph` 的连接连 `setup()` 的建表都会失败。
    用 `psycopg.sql` 拼标识符（不用 f-string 拼 SQL，S608）。
    """
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(LANGGRAPH_SCHEMA))
        )


def _connect_sync(dsn: str) -> psycopg.Connection[Any]:
    """建一条 autocommit 连接，search_path 固定指向 checkpoint schema。

    autocommit 是 `PostgresSaver.setup()` 的硬要求（它要跑 CREATE TABLE / 索引；
    `CREATE INDEX CONCURRENTLY` 更是压根不许在事务块里）。

    `statement_timeout` 也在这里钉死：建表期间任何 DDL 只要被别的会话/长事务挡住，
    5 秒内一定被数据库取消（task-11），不会重演「首次确认永久挂起」。建表成功后
    `_setup_saver_sync` 会把它改回 0，运行期读写不受这 5 秒限制。
    """
    return psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=SETUP_CONNECT_TIMEOUT_S,
        options=(
            f"-csearch_path={LANGGRAPH_SCHEMA} -cstatement_timeout={SETUP_STATEMENT_TIMEOUT_MS}"
        ),
    )


class ThreadedPostgresSaver(BaseCheckpointSaver[str]):
    """把同步 `PostgresSaver` 包成异步 checkpointer（每次调用 `asyncio.to_thread`）。

    `BaseCheckpointSaver` 的 `aget_tuple/alist/aput/aput_writes` 默认直接
    `raise NotImplementedError` —— 它**不会**自动把同步实现搬进线程池，所以必须手写。
    同步接口（`get_tuple/put/...`）不覆写：本项目只有 async 图在用它，
    留默认的 NotImplementedError 反而能在接线写错时立刻炸出来。
    """

    def __init__(self, saver: PostgresSaver) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver
        # 一条 psycopg 连接不是线程安全的：所有 to_thread 调用串行进出。
        self._lock = threading.Lock()

    def _call(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        with self._lock:
            return fn(*args, **kwargs)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self._call, self._saver.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        # 生成器必须在工作线程里**消费完**：psycopg 游标不能跨线程懒加载。
        rows = await asyncio.to_thread(
            lambda: list(self._saver.list(config, filter=filter, before=before, limit=limit))
        )
        for row in rows:
            yield row

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self._call, self._saver.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._call, self._saver.put_writes, config, writes, task_id, task_path
        )

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._saver.get_next_version(current, channel)

    async def adelete_thread(self, thread_id: str) -> None:
        """测试与「撤销采集」用：删掉某 thread 的全部 checkpoint（thread 隔离）。"""
        await asyncio.to_thread(self._call, self._saver.delete_thread, thread_id)

    async def aclose(self) -> None:
        """关闭底层连接（进程退出 / 测试收尾）。重复调用是安全的。"""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            conn = getattr(self._saver, "conn", None)
            if conn is not None and not conn.closed:
                conn.close()


_saver: ThreadedPostgresSaver | None = None
# 建连 + 建表是同步动作，可能被多个请求同时触发；用线程锁保证只建一次。
_init_lock = threading.Lock()


def _is_setup_timeout(exc: BaseException) -> bool:
    """这次失败是不是「被数据库按 `statement_timeout` 取消」这一类。

    只有这一类（以及下面抢不到锁）才翻译成 `CheckpointerUnavailableError`；
    其它异常（权限、库不存在、SQL 写错）一律原样上抛，不吞成「降级」。
    """
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return True
    text = str(exc).lower()
    return "statement timeout" in text or "canceling statement" in text


def _try_acquire_setup_lock(dsn: str) -> psycopg.Connection[Any] | None:
    """抢建表权（`pg_try_advisory_lock`）：抢不到返回 `None`，**绝不排队等待**。

    会话级 advisory lock 跟着连接走：持锁进程崩了锁自动释放，不会留成死锁。
    等待它就是在等一个正在跑 DDL 的会话 —— 而那个会话可能正等本请求的事务，
    这正是 task-11 的自互等。所以这里只 try，不 wait。
    """
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=SETUP_CONNECT_TIMEOUT_S)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (CHECKPOINTER_SETUP_LOCK_KEY,))
            row = cur.fetchone()
    except Exception:
        with suppress(Exception):
            conn.close()
        raise
    if row is None or not row[0]:
        with suppress(Exception):
            conn.close()
        return None
    return conn


def _release_setup_lock(conn: psycopg.Connection[Any]) -> None:
    """释放建表权并关掉那条持锁连接（关连接本身也会释放会话锁）。"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (CHECKPOINTER_SETUP_LOCK_KEY,))
    except Exception as exc:  # 解锁失败只记日志：连接一关，会话锁照样没
        logger.warning("checkpointer_setup_unlock_failed", extra={"error_type": type(exc).__name__})
    finally:
        with suppress(Exception):
            conn.close()


def _drop_invalid_indexes_sync(conn: psycopg.Connection[Any]) -> None:
    """清掉上一次 setup 被超时取消后留下的 invalid 索引（`indisvalid = false`）。

    `PostgresSaver.setup()` 用的是 `CREATE INDEX CONCURRENTLY IF NOT EXISTS`：
    invalid 索引仍然占着名字，`IF NOT EXISTS` 会直接跳过它 —— 残骸就此永久留着
    （W7 在 `anyu_test_w7` 上就见到了一个）。建表前顺手清掉；清不掉只记日志，
    不拦 setup（它本身有 statement_timeout 兜底）。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indexrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema() AND NOT i.indisvalid
                 ORDER BY c.relname
                """
            )
            names = [row[0] for row in cur.fetchall()]
        for name in names:
            conn.execute(
                sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(name))
            )
        if names:
            logger.warning("checkpointer_invalid_indexes_dropped", extra={"count": len(names)})
    except Exception as exc:
        logger.warning(
            "checkpointer_invalid_index_cleanup_failed",
            extra={"error_type": type(exc).__name__},
        )


def _setup_saver_sync(dsn: str) -> ThreadedPostgresSaver:
    """建 schema / 建表 / 建连接，返回新 saver。失败抛异常，**不碰全局单例**。

    三条硬约束（task-11）：
    1. 抢不到 `pg_try_advisory_lock` 立刻放弃（`setup_skipped`），不排队等 DDL；
    2. 建表连接带 `statement_timeout`，被挡住 5 秒内被取消（`setup_timeout`）；
    3. 任何失败都不留下半成品单例 —— 调用方下次可以重试。
    """
    lock_conn = _try_acquire_setup_lock(dsn)
    if lock_conn is None:
        logger.warning("checkpointer_setup_skipped", extra={"reason": "setup_lock_held"})
        raise CheckpointerUnavailableError("langgraph checkpointer setup is in progress elsewhere")
    try:
        _ensure_schema_sync(dsn)
        conn = _connect_sync(dsn)
        try:
            _drop_invalid_indexes_sync(conn)
            inner = PostgresSaver(conn)
            inner.setup()
            # 建表期已过：运行期读写不受那 5 秒限制（慢查询不该被这里截断）。
            conn.execute("SET statement_timeout = 0")
        except Exception as exc:
            with suppress(Exception):
                conn.close()
            if _is_setup_timeout(exc):
                logger.warning(
                    "checkpointer_setup_timeout",
                    extra={"timeout_ms": SETUP_STATEMENT_TIMEOUT_MS},
                )
                raise CheckpointerUnavailableError(
                    "langgraph checkpointer setup timed out"
                ) from exc
            raise
        return ThreadedPostgresSaver(inner)
    finally:
        _release_setup_lock(lock_conn)


def _get_or_create_sync() -> ThreadedPostgresSaver:
    global _saver
    with _init_lock:
        if _saver is not None:
            if not _saver._saver.conn.closed:
                return _saver
            # 连接已断：丢掉旧的，重建 —— 绝不能返回一个「一用就炸」的缓存对象。
            stale, _saver = _saver, None
            with suppress(Exception):
                stale._saver.conn.close()
        fresh = _setup_saver_sync(sync_dsn())
        _saver = fresh
        logger.info("checkpointer_ready", extra={"schema": LANGGRAPH_SCHEMA})
    return fresh


async def get_checkpointer() -> ThreadedPostgresSaver:
    """取进程级 checkpointer 单例（首次调用时建 schema、建表、建连接）。

    正常路径上 lifespan 已经预热过，这里只是一次「开关判断」，**不做任何 DDL**。
    没预热过时也绝不无限等：见 `_setup_saver_sync`（抢锁 + statement_timeout）。
    失败抛 `CheckpointerUnavailableError`：调用方要么降级（`get_health_graph`），
    要么让请求红 —— 不允许静默拿到一个「什么也不存」的假 checkpointer。
    """
    try:
        return await asyncio.to_thread(_get_or_create_sync)
    except CheckpointerUnavailableError:
        raise
    except Exception as exc:  # psycopg 的连接/权限/建表异常统一归口
        logger.warning("checkpointer_unavailable", extra={"error_type": type(exc).__name__})
        raise CheckpointerUnavailableError("langgraph checkpointer is unavailable") from exc


async def close_checkpointer() -> None:
    """关闭并丢弃单例；下次 `get_checkpointer()` 重新建。"""
    global _saver
    saver, _saver = _saver, None
    if saver is not None:
        await saver.aclose()


async def init_checkpointer() -> None:
    """lifespan 启动预热：把 schema / 表 / 索引在**没有业务事务**的启动期建好。

    这是 DDL 唯一该发生的地方（task-11）：请求路径只读已建好的表，
    因此不会再出现「`CREATE INDEX CONCURRENTLY` 等请求自己那条未提交事务」的自互等。
    失败只 warn —— checkpoint 是「断点恢复」的增强能力，连不上不该让后端起不来
    （与 `app/db.py` 的 fail-fast 不同：业务库连不上，整个 API 都没有意义）。
    预热失败后运行时仍有兜底：`_setup_saver_sync` 抢锁 + 5 秒 statement_timeout。
    """
    try:
        await get_checkpointer()
    except CheckpointerUnavailableError:
        logger.warning("checkpointer_init_failed_degraded")


async def shutdown_checkpointer() -> None:
    """lifespan 注册位：进程退出时关连接。"""
    await close_checkpointer()


__all__ = [
    "CHECKPOINTER_SETUP_LOCK_KEY",
    "LANGGRAPH_SCHEMA",
    "SETUP_STATEMENT_TIMEOUT_MS",
    "CheckpointerUnavailableError",
    "ThreadedPostgresSaver",
    "close_checkpointer",
    "get_checkpointer",
    "init_checkpointer",
    "shutdown_checkpointer",
    "sync_dsn",
]
