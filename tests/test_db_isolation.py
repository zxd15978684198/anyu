"""测试库重定向的守卫（由 N42 的审计发现补建）。

**背景**：`tests/conftest.py` 原先在 `_use_test_database()` **之前**就
`from app.db import engine as app_engine`，而 `app/db.py:24-33` 在模块级按当时的
settings 建好了 `engine` 与 `session_factory`。于是即便设了
`ANYU_TEST_DATABASE_URL`，测试进程里的 `app.db.engine` 与
`session_factory.bind` 仍指开发库 `anyu`：

- `db_conn` 夹具借的是 conftest 自己那份 `app_engine`（恰好是旧的，所以看着没问题）；
- 但 `write_audit_isolated` 这类**借 `app.db.session_factory` 的独立会话**直接连开发库，
  把测试数据写了进去（N42 的撤销审计就落错过一条，已按 audit_id 精确删除）。

**为什么这条要单独存在**：这不是功能缺陷，是「测试以为自己隔离了、其实没有」——它不会让
任何业务用例变红，只会悄悄污染开发库。守卫必须断言**对象本身**指向测试库，
而不是断言「用例跑绿了」。

只在设置了 `ANYU_TEST_DATABASE_URL` 时有意义：未设置时进程本就该连开发库，
此时 skip（不 fail），否则本地零配置跑单测会全红。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import URL, make_url

from app.core.config import get_settings
from app.db import engine, session_factory

#: 与其他测试同一命名，避免两处各写一个字符串常量。
_TEST_DB_ENV = "ANYU_TEST_DATABASE_URL"


def _test_url() -> str | None:
    value = os.environ.get(_TEST_DB_ENV, "").strip()
    return value or None


def _identity(url: URL) -> tuple[str, str | None, int | None, str]:
    """取「连的是哪个库」的身份四元组。

    不比整串：`str(engine.url)` 会把口令渲染成 `***`，与原始环境变量永远不相等，
    那样的断言会「永远红」，等于没测。库名 + 主机 + 端口才是这条守卫关心的东西。
    """
    return (url.drivername, url.host, url.port, url.database)


pytestmark = pytest.mark.skipif(
    _test_url() is None,
    reason=f"未设置 {_TEST_DB_ENV}：本守卫用来证明「设了变量之后真的连的是测试库」",
)


def test_settings_honours_test_database_url() -> None:
    """settings 层是第一个容易骗人的地方：它读环境变量，改了就会变。"""
    assert get_settings().database_url == _test_url()


def test_engine_binds_to_test_database() -> None:
    """`app.db.engine` 是模块级对象——改了环境变量不会让它重建，这里正是会失守的点。"""
    expected = make_url(_test_url() or "")
    assert _identity(engine.url) == _identity(expected), (
        "app.db.engine 仍指向非测试库：conftest 的导入顺序被打乱了"
        "（`app.db` 必须在 `_use_test_database()` 之后导入）"
    )


def test_session_factory_binds_to_test_database() -> None:
    """`session_factory` 被 `write_audit_isolated` 借用，它失守就是往开发库写数据。"""
    bind = getattr(session_factory, "bind", None) or session_factory.kw["bind"]
    expected = make_url(_test_url() or "")
    assert _identity(bind.url) == _identity(expected), (
        "session_factory 仍绑定非测试库：独立的审计会话会写进开发库"
    )
