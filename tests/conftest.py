"""共享测试夹具（task-3）。

数据库用例的隔离策略：每个测试独立事务、结束回滚，主库零残留
（不建独立 anyu_test 库——演示规模下 rollback 隔离够用，决策见
设计记录（库与模型基座））。

**连不上库时的口径（P0-1）**：`require_db_connection()` **默认 `pytest.fail`**，不是 skip。
原因：仓库没有 CI，`skip` 会让整批数据库用例在退出码 0 下消失（审计实测：无库时
110/476 用例被静默跳过），迭代 4 的功能缺陷因此没人发现。确实没有库的环境必须**显式**
设置 `ANYU_ALLOW_DB_SKIP=1`，才会退回「带原因的 skip」。

用法：需要数据库的用例声明 `db_conn` 参数即可；`/health` 等不依赖库的用例不受影响。
自建 NullPool 引擎的文件（`test_*_api.py` 等）一律调用 `require_db_connection(engine)`，
不许再各写一份 `except Exception: pytest.skip(...)`——两套逻辑迟早分叉。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import NoReturn

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import get_settings

#: 把 `localhost` 换成回环 IPv4 的开关（默认开）。设成 `0` 可关掉，用于复现慢路径。
_FAST_LOCALHOST_ENV = "ANYU_TEST_NO_LOCALHOST_FASTPATH"


def _force_ipv4_loopback() -> None:
    """把测试进程里的 `localhost` 换成 `127.0.0.1`，并说明为什么。

    Windows 上 `localhost` 会先解析出 `::1`；PostgreSQL 只监听 IPv4 时这条会**白等
    ~2 秒**才回落到 IPv4（实测：`asyncpg.connect("localhost")` 2057ms vs
    `127.0.0.1` 26ms）。每个用例都要新建 NullPool 引擎 → 一次连接，1794 个用例
    光连库就多花约一小时，八阶段门禁的 `test` 阶段因此从 3 分钟涨到 24 分钟。

    只影响**测试进程**：改写的是环境变量，随后清掉 settings 缓存让 `app.db` 重新读；
    生产/本机服务不受影响，`.env` 也不必改（`docker compose` 里连的是服务名 `postgres`）。
    """
    if os.environ.get(_FAST_LOCALHOST_ENV, "").strip() == "0":
        return
    settings = get_settings()
    database_url = settings.database_url
    checkpoint_url = settings.langgraph_checkpoint_database_url
    if "localhost" not in database_url and "localhost" not in checkpoint_url:
        return
    os.environ["DATABASE_URL"] = database_url.replace("localhost", "127.0.0.1")
    os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = checkpoint_url.replace(
        "localhost", "127.0.0.1"
    )
    get_settings.cache_clear()


_force_ipv4_loopback()

#: 独立测试库开关。**强烈建议每次跑全量前都设上** —— 有四个夹具会 `TRUNCATE` 名义上的
#: "测试数据"，但本仓默认连的是开发库 `anyu`：不隔离时一次全量 pytest 就会把本机
#: 演示数据（长者/设备/账号/药单/日报）整片抹掉，2026-11-09 已因此连丢三次。
#: 用法：`ANYU_TEST_DATABASE_URL=postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test`  # pragma: allowlist secret
_TEST_DB_ENV = "ANYU_TEST_DATABASE_URL"


def _use_test_database() -> None:
    """把测试进程指向独立测试库（`ANYU_TEST_DATABASE_URL`）。

    只在**显式**给变量时生效：猜一个库名出来比连开发库更危险（连错库会把别的环境清空）。
    给了变量却连不上，由各用例的 `require_db_connection()` 报 fail —— 不在这里静默兜底。
    同步驱动的 checkpointer 走 `LANGGRAPH_CHECKPOINT_DATABASE_URL`，同样按测试库改写。
    """
    test_url = os.environ.get(_TEST_DB_ENV, "").strip()
    if not test_url:
        return
    os.environ["DATABASE_URL"] = test_url
    # 同一个库、换同步驱动（`PostgresSaver` 用 psycopg 而非 asyncpg）。
    os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = test_url.replace("+asyncpg", "+psycopg")
    get_settings.cache_clear()


#: 测试进程用的固定 JWT 密钥。48 字节，满足 `app/core/security.py:311` 的 ≥32 字节下限。
_TEST_JWT_SECRET_ENV = "ANYU_TEST_JWT_SECRET"
_TEST_JWT_SECRET = "anyu-test-jwt-secret-0123456789abcdef"  # pragma: allowlist secret


def _use_test_jwt_secret() -> None:
    """给测试进程注入固定 `JWT_SECRET`，不依赖开发者本机 `.env`。

    为什么必须这么做：`.env.example` 自带的占位串 `replace-with-random-48-bytes` 只有
    **28 个字符**，而代码要求 ≥32 字节，于是「clone 下来直接跑测试」会有 30+ 条鉴权用例
    红在 `JwtSecretNotConfiguredError: jwt_secret is too short` 上——这是**测试依赖外部
    配置**的缺陷，不是被测代码的问题。测试用的密钥就该由测试自己钉死，这样「同一条用例
    在任何机器上结果一致」才成立；真实的 `.env` 由部署侧负责，不由测试替它兜底。

    `setdefault` 而非赋值：CI 想用别的密钥时，外面的环境变量优先。
    """
    os.environ.setdefault("JWT_SECRET", os.environ.get(_TEST_JWT_SECRET_ENV) or _TEST_JWT_SECRET)
    get_settings.cache_clear()


_use_test_database()
_use_test_jwt_secret()


def _isolate_external_services() -> None:
    """把测试进程里**外部 HTTP 依赖**一律关掉，让用例只跑本地代码。

    为什么必须这么做（2026-11-10 事故）：开发机 `.env` 一旦配上
    `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY`（知识库接
    向量网关时就会配），`build_embedding_provider()` 会走 HTTP 分支，于是
    `tests/test_knowledge_api.py` 变成**联网用例**：网关一改状态码，
    `test_admin_can_create_document` 就红在 `503 embedding unavailable: bad_status`。
    这不是被测代码的问题，是测试依赖了本机配置。

    做法：把四项 embedding 配置清空 → 工厂回落 `DeterministicEmbeddingProvider`
    （纯计算、零网络）。想验 HTTP provider 的用例本来就直接注入 `Settings(...)`，
    不读环境变量，因此不受影响。

    用**赋值**而非 `setdefault`：本机 `.env` 有真实 key 时，`setdefault` 不会生效。
    `""` 而非 `del`：`get_settings` 在 Pydantic Settings 里对空串的处理是「字符串保持
    空」，正好等于「没配」，不必担心回落成默认值。
    """
    for name in (
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_TIMEOUT_MS",
    ):
        os.environ[name] = ""
    get_settings.cache_clear()


_isolate_external_services()


def _silence_observability() -> None:
    """把测试进程里的 OTLP 导出关掉，让用例不联网、不起后台导出线程。

    为什么必须这么做：`.env` 一旦配了真实的 `OTEL_EXPORTER_OTLP_ENDPOINT`
    （S2 起本机就是配好的），每个用例只要构造出 app 就会起一个 `BatchSpanProcessor`，
    跑千条用例时既慢又会在 CI/无网机器上反复失败。测试要验的是「门面行为」，
    不是「网络是否通」——真连 Grafana 由 `spikes/otel_smoke.py` 的 C1/C2 负责。

    用**赋值**而非 `setdefault`：本机 `.env` 有真端点时 `setdefault` 不生效。
    """
    os.environ["OTEL_ENABLED"] = "false"
    os.environ["LANGFUSE_ENABLED"] = "false"
    get_settings.cache_clear()


_silence_observability()

# `app.db` 必须在上面两步**之后**才导入：它在模块级（`app/db.py:24-33`）就按当时的
# settings 建好了 `engine` 与 `session_factory`。先导入再改环境变量，只会改到 settings
# 的缓存，已建好的对象仍指向旧库 —— 实测过：设了 `ANYU_TEST_DATABASE_URL`，
# `app.db.engine` 与 `session_factory.bind` 仍打在开发库 `anyu` 上。
# 后果不是「跑得慢」，而是 `write_audit_isolated` 这类**借 session_factory 的独立会话**
# 把测试数据写进开发库（N42 的撤销审计就落错过一条）。
from app.db import engine as app_engine  # noqa: E402

#: 显式逃生开关：只有把它设成 `1` 才允许「无库 → skip」。默认必须 fail。
ALLOW_DB_SKIP_ENV = "ANYU_ALLOW_DB_SKIP"

#: 固定失败话术：出现这条红就说明是「没库」，不是被测代码坏了。
DB_UNAVAILABLE_MESSAGE = "PostgreSQL 不可达：本项目禁止静默跳过（会掩盖 迭代 4 功能缺陷）"


def db_skip_allowed() -> bool:
    """逃生开关是否打开（只有字面量 `1` 算开，`true`/`yes` 不算，避免误开）。"""
    return os.environ.get(ALLOW_DB_SKIP_ENV, "").strip() == "1"


def _raise_db_unavailable(exc: BaseException) -> NoReturn:
    """连不上库时的**唯一**处置点：默认 fail，只有显式逃生开关才 skip。"""
    if db_skip_allowed():
        pytest.skip(f"{DB_UNAVAILABLE_MESSAGE}（已显式设置 {ALLOW_DB_SKIP_ENV}=1）：{exc!r}")
    pytest.fail(f"{DB_UNAVAILABLE_MESSAGE}：{exc!r}", pytrace=False)


async def require_db_connection(db_engine: AsyncEngine) -> AsyncConnection:
    """打开一条真实连接；连不上就按统一策略处置（**默认 fail**，见模块 docstring）。

    返回的连接由调用方负责关闭（通常是包在事务夹具里，finally 中 rollback + close）。

    为什么默认 fail：`pytest.skip` 的退出码仍是 0，等于「没有数据库 = 全绿」；而本项目的
    数据库用例覆盖身份、鉴权、用药提醒等 迭代 4 主链路，缺库时必须让流水线红。
    确实要跑无库环境（例如只验证 `/health`）时，显式 `export ANYU_ALLOW_DB_SKIP=1`。
    """
    try:
        return await db_engine.connect()
    except Exception as exc:  # 任何连接失败（超时/拒绝/口令错/库不存在）都算「无库」
        _raise_db_unavailable(exc)
        raise  # 仅用于让类型检查器知道这里必然不返回（_raise_db_unavailable 必抛）


async def require_db_reachable(db_engine: AsyncEngine) -> None:
    """只探活：连上即关。连不上时与 `require_db_connection` 走**同一**处置策略。"""
    conn = await require_db_connection(db_engine)
    await conn.close()


@pytest.fixture
async def db_conn() -> AsyncIterator[AsyncConnection]:
    """每测试一个事务，yield 后回滚；连不上库时 fail（`ANYU_ALLOW_DB_SKIP=1` 才 skip）。"""
    conn = await require_db_connection(app_engine)

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture(autouse=True)
def _isolate_session_guardrails() -> Iterator[None]:
    """每个用例前后清空会话级护栏缓存（迭代 9 · B3-04 修复的副作用）。

    `run_turn(guardrail=None)` 现在按 `(elder_id, thread_id)` 复用同一个 `Guardrail`，
    「同一 Tool 1 分钟内第 3 次」的熔断时间窗才真的跨轮生效；代价是这份状态活在
    进程里、会跨用例残留——同一个测试文件里多个用例共用 `thread-1` 时，前一条
    打出的熔断会让后一条莫名被拒（在本文件中实测复现过 6 例）。
    这里统一在用例边界清空，让"跨轮"只在**单个用例内部**成立。
    """
    from app.agents.companion import reset_guardrails  # 局部导入：避免 conftest 顶部拉起 Agent 依赖

    reset_guardrails()
    try:
        yield
    finally:
        reset_guardrails()
