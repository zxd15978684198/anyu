"""checkpointer 建表路径的「绝不挂住」契约（task-11 · P0 回归）。

## 为什么要这组用例

2026-11-12 W7 服务级回归抓到一次**永久挂起**：`POST /api/pad/medications/{id}/confirm`
在本进程第一次用到用药确认图时卡死，客户端 180s 超时，服务端连 access log 都没有。
`pg_stat_activity` 现场是：

- `pid 26565`：`active` / `wait_event=virtualxid` / 语句
  `CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoints_thread_id_idx …`
- `pg_blocking_pids = {25131}`，而 25131 正是**这条 confirm 请求自己的会话**
  （`idle in transaction`，最后一条是这条请求的业务查询）

根因：`PostgresSaver.setup()` 的 `CREATE INDEX CONCURRENTLY` 要等所有更早开始的事务结束，
请求自己的业务事务还没提交、又在等 setup 返回 —— 自互等，永不返回。

## 这组用例守的是什么

1. **正常路径**：建表只在 lifespan 预热里做，预热之后调用不再跑任何 DDL（用例 3、7）。
2. **兜底路径**：没预热过时也**不许无限等** —— 抢不到建表权立刻失败；DDL 被长事务挡住时
   由 `statement_timeout` 在 5 秒内取消。两条都用**计时断言**证明（用例 1、2），
   而不是「看起来很快」。
3. **失败不留半成品**：失败后 `_saver` 仍是 `None`，下次可以重试（用例 1、2、5、6）。

用例 2 是那条 P0 的**最小复现**：一个「开着事务不发提交」的会话就足以让
`CREATE INDEX CONCURRENTLY` 停在 `virtualxid` 上，这正是生产上那条请求会话扮演的角色。
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql as pgsql

from app.graphs import checkpointer as checkpointer_mod

#: 兜底失败的时间预算（秒）。实现里 statement_timeout 是 5s，连接与线程切换留余量。
_FAIL_BUDGET_S = 9.0
#: 计时断言的下界：低于它说明根本没等到 statement_timeout，用例没测到该测的东西。
_TIMEOUT_FLOOR_S = 3.0


def _connect(*, autocommit: bool = True) -> psycopg.Connection[object]:
    """连测试库；连不上时按项目口径**直接 fail**（禁止静默 skip）。"""
    try:
        return psycopg.connect(
            checkpointer_mod.sync_dsn(), autocommit=autocommit, connect_timeout=5
        )
    except Exception as exc:  # 任何连接失败都算「没有库」
        pytest.fail(f"PostgreSQL 不可达（本项目禁止静默跳过）：{exc!r}", pytrace=False)


@pytest.fixture
async def fresh_saver() -> object:
    """用例前后都清掉进程级单例：不把半成品/别人的连接留给下一个用例。"""
    await checkpointer_mod.close_checkpointer()
    try:
        yield None
    finally:
        await checkpointer_mod.close_checkpointer()


@pytest.fixture
def probe_schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """给需要「空 schema」的用例一个一次性 schema，用完 CASCADE 删掉。

    不碰默认的 `langgraph`：那里有别的用例（test_medication_graph）正在用的表。
    schema 名在调用时才读模块常量，所以 monkeypatch 能生效。
    """
    name = f"langgraph_probe_{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(checkpointer_mod, "LANGGRAPH_SCHEMA", name)
    yield name
    conn = _connect()
    try:
        conn.execute(pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pgsql.Identifier(name)))
    finally:
        conn.close()


async def test_get_checkpointer_fails_fast_when_setup_lock_is_held(
    fresh_saver: object, probe_schema: str
) -> None:
    """建表权被别的会话占着时：**立刻**失败，不排队等它的 DDL。

    排队等就是在重演 P0 —— 等 DDL 的那一方可能正等本请求的事务，
    所以这里只允许 `pg_try_advisory_lock` 的「拿不到就放弃」。
    """

    holder = _connect()
    try:
        holder.execute(
            "SELECT pg_advisory_lock(%s)", (checkpointer_mod.CHECKPOINTER_SETUP_LOCK_KEY,)
        )

        started = time.perf_counter()
        with pytest.raises(checkpointer_mod.CheckpointerUnavailableError):
            await asyncio.wait_for(checkpointer_mod.get_checkpointer(), timeout=_FAIL_BUDGET_S)
        elapsed = time.perf_counter() - started
    finally:
        holder.close()

    assert elapsed < 2.0, f"抢锁失败必须立刻返回，实际用了 {elapsed:.2f}s"
    # 失败不留半成品：下次调用还能重试（不会被一个「空壳 saver」骗过去）。
    assert checkpointer_mod._saver is None


async def test_runtime_setup_is_bounded_when_an_open_transaction_blocks_ddl(
    fresh_saver: object, probe_schema: str
) -> None:
    """P0 最小复现：有事务没提交时，建表必须在预算内失败，而不是无限等。

    这条用例在没有 lifespan 的路径上直接调 `get_checkpointer()`，让它在**请求路径**里
    重跑 `CREATE INDEX CONCURRENTLY`；修复前它会一直卡在等对方事务的锁/快照上。

    为什么 blocker 用 `LOCK TABLE … IN SHARE MODE` 而不是「开着不提交的 SELECT」：
    实测（`.tmp/cic_blocker_probe.py`）只读事务、甚至写**别的表/临时表**的未提交事务
    都不会让 CIC 等住（0.03s 就过）；只有和**被建索引的那张表**挂着的事务才会。
    现场那条 confirm 会话正是「读/写了 checkpoints 这一族表还没提交」，
    这里用同表的 SHARE 锁复刻同一类冲突，且确定性更好。
    """

    # 先让 setup 把表建出来，再撤掉三条并发索引并清空版本表，
    # 这样下一次 setup 才会真的重跑 CREATE INDEX CONCURRENTLY（这就是要测的那一步）。
    await checkpointer_mod.get_checkpointer()
    await checkpointer_mod.close_checkpointer()

    admin = _connect()
    try:
        for index in (
            "checkpoints_thread_id_idx",
            "checkpoint_blobs_thread_id_idx",
            "checkpoint_writes_thread_id_idx",
        ):
            admin.execute(
                pgsql.SQL("DROP INDEX {}.{}").format(
                    pgsql.Identifier(probe_schema), pgsql.Identifier(index)
                )
            )
        admin.execute(
            pgsql.SQL("DELETE FROM {}.checkpoint_migrations").format(pgsql.Identifier(probe_schema))
        )
    finally:
        admin.close()

    blocker = _connect(autocommit=False)
    try:
        with blocker.cursor() as cur:
            cur.execute(
                pgsql.SQL("LOCK TABLE {}.checkpoints IN SHARE MODE").format(
                    pgsql.Identifier(probe_schema)
                )
            )

        started = time.perf_counter()
        with pytest.raises(checkpointer_mod.CheckpointerUnavailableError):
            await asyncio.wait_for(checkpointer_mod.get_checkpointer(), timeout=15.0)
        elapsed = time.perf_counter() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert elapsed < _FAIL_BUDGET_S, f"建表必须在 {_FAIL_BUDGET_S}s 内失败，实际 {elapsed:.2f}s"
    assert elapsed >= _TIMEOUT_FLOOR_S, (
        f"只用了 {elapsed:.2f}s：说明没真的等到 statement_timeout，用例没测到目标路径"
    )
    assert checkpointer_mod._saver is None, "失败不许缓存半成品 checkpointer"


async def test_warm_checkpointer_never_runs_setup_again(
    fresh_saver: object, probe_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预热（lifespan 等同动作）之后，取 checkpointer 不再跑任何建表 DDL。"""

    saver = await checkpointer_mod.get_checkpointer()

    calls: list[int] = []

    def _must_not_run() -> None:
        calls.append(1)
        raise AssertionError("预热之后不许再跑 setup（请求路径里做 DDL 就是 P0）")

    monkeypatch.setattr(checkpointer_mod, "_setup_saver_sync", _must_not_run)

    again = await checkpointer_mod.get_checkpointer()

    assert again is saver
    assert calls == []


async def test_setup_lock_is_released_after_success(fresh_saver: object, probe_schema: str) -> None:
    """建表成功后必须把 advisory lock 放开，否则下一个进程永远建不了表。"""

    await checkpointer_mod.get_checkpointer()

    probe = _connect()
    try:
        with probe.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)", (checkpointer_mod.CHECKPOINTER_SETUP_LOCK_KEY,)
            )
            row = cur.fetchone()
            assert row is not None and row[0] is True, "建表锁没有释放"
            cur.execute(
                "SELECT pg_advisory_unlock(%s)", (checkpointer_mod.CHECKPOINTER_SETUP_LOCK_KEY,)
            )
    finally:
        probe.close()


async def test_unreachable_database_raises_unavailable_within_budget(
    fresh_saver: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库不可达时：立刻抛 `CheckpointerUnavailableError`，请求不会被 TCP 握手挂住。"""

    # 假 DSN：端口 1 必然不可达，用来断言「库连不上时快速失败」。
    # 它不是任何环境的真实凭据，故行尾标 allowlist secret 放行密钥门禁。
    monkeypatch.setattr(
        checkpointer_mod,
        "sync_dsn",
        lambda *_args, **_kwargs: (
            "postgresql://anyu:anyu@127.0.0.1:1/anyu"
        ),  # pragma: allowlist secret
    )

    started = time.perf_counter()
    with pytest.raises(checkpointer_mod.CheckpointerUnavailableError):
        await asyncio.wait_for(checkpointer_mod.get_checkpointer(), timeout=_FAIL_BUDGET_S)
    elapsed = time.perf_counter() - started

    assert elapsed < _FAIL_BUDGET_S
    assert checkpointer_mod._saver is None


async def test_init_checkpointer_degrades_instead_of_raising(
    fresh_saver: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lifespan 预热失败只降级（warn），不许把后端起不来。"""

    # 同上：端口 1 必然不可达，只为让 lifespan 预热走到「连不上 → 降级 warn」分支。
    monkeypatch.setattr(
        checkpointer_mod,
        "sync_dsn",
        lambda *_args, **_kwargs: (
            "postgresql://anyu:anyu@127.0.0.1:1/anyu"
        ),  # pragma: allowlist secret
    )

    await checkpointer_mod.init_checkpointer()  # 不抛就算过

    assert checkpointer_mod._saver is None


def test_setup_timeout_classifier_only_matches_timeouts() -> None:
    """只有「被 statement_timeout 取消」才归到不可用；权限类错误必须原样上抛。"""

    timeout_exc = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    privilege_exc = psycopg.errors.InsufficientPrivilege("permission denied")

    assert checkpointer_mod._is_setup_timeout(timeout_exc) is True
    assert checkpointer_mod._is_setup_timeout(privilege_exc) is False


def test_lifespan_wires_checkpointer_lifecycle() -> None:
    """lifespan 必须同时接启动预热与退出关闭（task-11 的接线是 P0 的一部分）。"""

    from app.main import lifespan

    source = inspect.getsource(lifespan)
    assert "init_checkpointer" in source, "lifespan 启动期没有预热 checkpointer"
    assert "shutdown_checkpointer" in source, "lifespan 退出期没有关闭 checkpointer"
