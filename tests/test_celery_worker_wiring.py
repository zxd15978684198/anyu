"""worker / beat 的启动接线与任务 span（计划 §6-4 A′、§8 开关语义）。

只测「接线有没有接上」，不真起 Celery：信号处理器就是普通函数，直接调用即可。
真起 worker 的端到端冒烟在 `docker compose --profile celery up -d` 之后做（需 Redis）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.tasks import celery_app as celery_module
from app.tasks.jobs._runtime import sync_task


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {"celery_enabled": True, "otel_service_name": "anyu-worker"}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_require_enabled_refuses_when_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CELERY_ENABLED=false` 时 worker/beat 必须拒启，而不是静默双跑。"""
    monkeypatch.setattr(celery_module, "get_settings", lambda: _settings(celery_enabled=False))

    with pytest.raises(SystemExit) as excinfo:
        celery_module._require_enabled("celery worker")
    assert "CELERY_ENABLED=false" in str(excinfo.value)

    with pytest.raises(SystemExit):
        celery_module._on_beat_init()


def test_worker_ready_configures_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 起来后要把可观测性接上，且沿用部署给的服务名（backend/worker 分开）。"""
    configured: list[object] = []
    monkeypatch.setattr(celery_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        "app.core.observability.configure_observability",
        lambda settings=None: configured.append(settings),
    )

    celery_module._on_worker_ready()

    assert configured, "worker_ready 没有调用 configure_observability"
    assert getattr(configured[0], "otel_service_name", None) == "anyu-worker"


def test_worker_ready_refuses_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关没开时，worker_ready 也该拦住（不能「起来一半」）。"""
    monkeypatch.setattr(celery_module, "get_settings", lambda: _settings(celery_enabled=False))

    with pytest.raises(SystemExit):
        celery_module._on_worker_ready()


def test_sync_task_wraps_task_in_named_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个任务执行都要有一条 `task.<短名>` span，属性带完整任务名（FR-TC-04）。"""
    recorded: list[tuple[str, dict[str, Any]]] = []

    class _Scope:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            return None

    def _fake_span(task_name: str, *, trace_id: str | None = None, **attrs: Any) -> _Scope:
        recorded.append((task_name, {"trace_id": trace_id, **attrs}))
        return _Scope()

    monkeypatch.setattr("app.core.observability.task_span", _fake_span)

    async def _job(*, trace_id: str | None = None) -> dict[str, int]:
        return {"scanned": 1}

    wrapper = sync_task(_job, "app.tasks.jobs.reports.daily_report_delivery")

    assert wrapper() == {"scanned": 1}
    assert recorded == [
        (
            "app.tasks.jobs.reports.daily_report_delivery",
            {"trace_id": None},
        )
    ]
