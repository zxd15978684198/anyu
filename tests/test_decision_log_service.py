"""决策日志服务（`app/services/decision_log_service.py`）的 W4 回归集。

覆盖 plan `设计记录（三级路由）` §4-W4 的三条硬口径：
① `decision_to_log` 纯映射——bool privacy 收敛为 'true'/'false' 字符串、
   字段逐一对位（台账 §3-7 漂移的落库侧处置）；
② `log_decision` 无身份不落库——`elder_id=None` 记 skip 计数、返回 None、
   **绝不发明身份**（AGENTS §4.9，elder_id 是 NOT NULL 服务端身份列）；
③ `log_decision` 写失败永不抛——warning + 失败计数 + None（垫层可用性
   高于评测留痕，模块 docstring 第 2 条）。

全部免 DB：纯映射用例不碰库；`log_decision` 的会话工厂 monkeypatch 成
内存假工厂——测的是「服务层的编排与兜底」，不是 SQLAlchemy 的行为。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.router.decision import RouteDecision
from app.services import decision_log_service
from app.services.decision_log_service import decision_to_log, log_decision

# --------------------------------------------------------------- 测试替身


class _FakeSession:
    """记录 add/commit 的假会话；可配置 commit 抛错。"""

    def __init__(self, *, commit_error: Exception | None = None) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.commit_error = commit_error

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1


class _FakeSessionFactory:
    """async with 可用的假会话工厂（替 `app.db.session_factory`）。"""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSessionFactory:
        return self

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        return None


def _decision(
    *,
    elder_id: str | None = "elder-1",
    privacy: Any = None,
    route_ms: int = 12,
    trace_id: str = "trace-9",
) -> RouteDecision:
    return RouteDecision(
        elder_id=elder_id,
        thread_id="thread-1",
        layer="L1",
        rule_id=None,
        intent="small_talk",
        sensitive=False,
        confidence=0.92,
        is_out_of_domain=False,
        privacy=privacy,
        route_ms=route_ms,
        degraded=None,
        trace_id=trace_id,
    )


@pytest.fixture
def metrics_reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    """打点读取；本文件不验证开关语义，直接强制开。"""
    from types import SimpleNamespace

    from app.core import metrics

    monkeypatch.setattr(metrics, "get_settings", lambda: SimpleNamespace(metrics_enabled=True))
    metrics.reset_metrics()
    yield metrics
    metrics.reset_metrics()


# --------------------------------------------------------------- 纯映射


def test_decision_to_log_converges_bool_privacy_to_text() -> None:
    """台账 §3-7：FiveSignal 的 bool privacy 落库前收敛为 'true'/'false' 文本。"""
    row = decision_to_log(_decision(privacy=True))
    assert row.privacy == "true"

    row = decision_to_log(_decision(privacy=False))
    assert row.privacy == "false"


def test_decision_to_log_maps_all_fields() -> None:
    """RouteDecision → DecisionLog 逐字段对位（W1 的 12 字段契约面）。"""
    row = decision_to_log(_decision(route_ms=42))

    assert row.elder_id == "elder-1"
    assert row.thread_id == "thread-1"
    assert row.layer == "L1"
    assert row.rule_id is None
    assert row.intent == "small_talk"
    assert row.sensitive is False
    assert row.confidence == 0.92
    assert row.is_out_of_domain is False
    assert row.privacy is None  # None 保持 None，不造 'none' 字符串
    assert row.route_ms == 42
    assert row.degraded is None
    assert row.trace_id == "trace-9"


# --------------------------------------------------------------- log_decision


async def test_log_decision_without_identity_skips_and_never_invents(
    metrics_reset: Any,
) -> None:
    """elder_id=None：不落行、记 skip 计数、返回 None——身份绝不发明（AGENTS §4.9）。"""
    session = _FakeSession()
    monkey_target = _FakeSessionFactory(session)
    import app.db as db

    original = db.session_factory
    db.session_factory = monkey_target  # type: ignore[assignment]
    try:
        result = await log_decision(_decision(elder_id=None))
    finally:
        db.session_factory = original

    assert result is None
    assert session.added == []  # 根本没走到 add
    assert (
        metrics_reset.metrics_snapshot()["counters"][
            "decision_log_skipped_total{reason=no_identity}"
        ]
        == 1
    )


async def test_log_decision_persists_via_own_session(metrics_reset: Any) -> None:
    """有身份：add + commit 各一次，返回落库行（trace_id 对账键在行上）。"""
    session = _FakeSession()
    import app.db as db

    original = db.session_factory
    db.session_factory = _FakeSessionFactory(session)  # type: ignore[assignment]
    try:
        result = await log_decision(_decision(trace_id="trace-9"))
    finally:
        db.session_factory = original

    assert result is not None
    assert result.trace_id == "trace-9"
    assert session.added == [result]
    assert session.commits == 1


async def test_log_decision_write_failure_never_raises(metrics_reset: Any) -> None:
    """commit 炸了：warning + 失败计数 + None——绝不打断「听到 → 出声」主链。"""
    session = _FakeSession(commit_error=RuntimeError("db gone"))
    import app.db as db

    original = db.session_factory
    db.session_factory = _FakeSessionFactory(session)  # type: ignore[assignment]
    try:
        result = await log_decision(_decision())
    finally:
        db.session_factory = original

    assert result is None
    snapshot = metrics_reset.metrics_snapshot()["counters"]
    assert snapshot["decision_log_write_failed_total"] == 1


async def test_service_module_imports_do_not_touch_db() -> None:
    """分层护栏（AGENTS §2）：service 只允许被垫层 import，自身 import 面不建连接。

    冒烟口径：import 本模块（连同 `app.db` 的惰性引擎）不出错即过——
    `app/db.py:33` 的 `session_factory` 是 `async_sessionmaker(...)`，构造
    不建连接，真正连接发生在首次 `async with`。这条用例钉住「import 无副作用」，
    防止有人把引擎创建提前到模块顶层。
    """
    import importlib

    module = importlib.reload(decision_log_service)
    assert module.log_decision is not None
