"""Celery 应用的装配点（批 0 地基，见 `设计记录（celery定时任务边界）` §4/§5）。

## 只搬离线批处理

留在 APScheduler 的是「长者侧到点必须发生」的任务（用药触达、19:00 健康采集），它们
依赖进程内状态与秒级精度；搬过来的是日报生成/推送重试、通知升级、离线扫描、音频清理
这类「结果对、晚几十秒无所谓」的批处理。为什么这么切见计划 §2。

## 四条可靠性配置（§5，是配置不是注意事项）

- `task_acks_late=True`：先执行后签收，worker 半路被杀时消息回队重跑（重跑靠服务层
  幂等闸门消化）；
- `worker_prefetch_multiplier=1`：一次只预取一条，长任务不把队列攥在一个 worker 手里；
- `task_ignore_result=True` 且**不配 `result_backend`**：这批任务没有返回值语义，开了
  backend 只会在 Redis 里堆 `celery-task-meta-*`；
- broker 单独用 `settings.celery_broker_url`（默认 `/1`），**不复用 `redis_url`**——后者
  默认 `/0`，业务短期记忆也在用。

## beat_schedule 自动发现，而不是一个巨大的静态字典

`app/tasks/jobs/*.py` 每个模块自己注册任务、自己声明 `BEAT_ENTRIES`，本模块只负责遍历
并合并。这样批 1–4 各写各的模块，不需要所有人改同一个文件（避免每天在这个文件上撞 diff）。
代价是约定必须硬：模块缺 `register` / `BEAT_ENTRIES`、或者条目重名，一律在 import 时抛
`RuntimeError` —— 定时任务静默漏跑比启动失败难查得多。

jobs 模块**不许** `from app.tasks.celery_app import app`：那会形成
`celery_app → jobs → celery_app` 的循环导入。实例由 `register(app)` 的参数传进来。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any, Final

from celery import Celery
from celery.signals import beat_init, worker_ready, worker_shutdown

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: jobs 模块所在的目录。**用 __file__ 推导而不是 `from app.tasks import jobs`**：
#: `app/tasks/__init__.py` 会 import 本模块，先 import 父包就成了循环导入。
_JOBS_DIR: Final[Path] = Path(__file__).resolve().parent / "jobs"

#: jobs 模块的点分前缀（`importlib.import_module` 用）。
_JOBS_PACKAGE: Final[str] = "app.tasks.jobs"


def _discover_jobs(application: Celery) -> dict[str, Any]:
    """遍历 `app/tasks/jobs/*.py`：调各自的 `register(app)`，合并各自 `BEAT_ENTRIES`。

    约定（写进 `app/tasks/jobs/__init__.py`）：每个模块必须暴露

    - `register(app: Celery) -> None`：把本模块的 task 注册到传入实例上；
    - `BEAT_ENTRIES: dict[str, Any]`：喂给 `beat_schedule` 的条目，没有周期任务也要写 `{}`。

    缺一个就抛 `RuntimeError`：静默跳过等于「任务永远不跑」，而它不会在任何地方报错。
    """
    schedule: dict[str, Any] = {}
    for module_info in pkgutil.iter_modules([str(_JOBS_DIR)]):
        # 下划线开头的是私有模块（约定里不参与发现），`__init__` 本身不会出现在这里。
        if module_info.name.startswith("_"):
            continue
        dotted = f"{_JOBS_PACKAGE}.{module_info.name}"
        module = importlib.import_module(dotted)

        register = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(
                f"jobs 模块缺少 register(app) 函数：{dotted}（约定见 app/tasks/jobs/__init__.py）"
            )
        register(application)

        entries = getattr(module, "BEAT_ENTRIES", None)
        if not isinstance(entries, dict):
            raise RuntimeError(
                f"jobs 模块的 BEAT_ENTRIES 必须是 dict，没有周期条目也要写 {{}}：{dotted}"
            )
        duplicated = sorted(schedule.keys() & entries.keys())
        if duplicated:
            raise RuntimeError(f"beat 条目重名 {duplicated}：{dotted} 与前面的 jobs 模块冲突")
        schedule.update(entries)

    return schedule


settings = get_settings()

app = Celery("anyu")
app.conf.update(
    broker_url=settings.celery_broker_url,
    # 业务时间一律 Asia/Shanghai 展示、UTC 存储（AGENTS §8）；beat 的 crontab 按前者解释。
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # 故意不设 result_backend：见模块文档第三条。
)

app.conf.beat_schedule = _discover_jobs(app)


# ======================================================================
# worker / beat 的启动接线（计划 §6-4 A′、§8 开关语义）
# ======================================================================


def _require_enabled(role: str) -> None:
    """`CELERY_ENABLED=false` 时拒绝启动 worker/beat。

    为什么在这里硬拦：开关的另一半在 backend 的 APScheduler（`CELERY_OWNED_JOBS`
    摘除 6 条）。只起 profile `celery`、却没把 `CELERY_ENABLED` 打开时，两边都不跑或
    两边都跑——前者静默停掉 6 条任务，后者靠服务层幂等兜住但属「靠下游兜」，都不是
    计划承诺的「开关二选一」。把战争放到启动期，比让人从日志里数「今天跑了几次」便宜。
    """
    if not get_settings().celery_enabled:
        raise SystemExit(
            f"CELERY_ENABLED=false，{role} 拒绝启动：backend 的 APScheduler 仍持有这 6 条任务，"
            "同时启动会双跑。先在 .env 里设 CELERY_ENABLED=true 再起 worker/beat。"
        )


@worker_ready.connect  # type: ignore[untyped-decorator]
def _on_worker_ready(sender: Any = None, **_kwargs: Any) -> None:
    """worker 起来了：先验开关，再把 worker 侧可观测性接上。

    服务名由部署环境给（compose 里 `OTEL_SERVICE_NAME: anyu-worker`）：
    不覆盖的话会沿用 `anyu-backend`，两条车道的指标与 trace 在 Grafana 里分不开。
    """
    _require_enabled("celery worker")
    from app.core.observability import configure_observability

    configure_observability(get_settings())
    logger.info(
        "celery worker observability configured",
        extra={"service_name": get_settings().otel_service_name},
    )


@beat_init.connect  # type: ignore[untyped-decorator]
def _on_beat_init(sender: Any = None, **_kwargs: Any) -> None:
    """beat 起来了：只验开关（beat 不产生 trace，不需要 provider）。"""
    _require_enabled("celery beat")
    logger.info("celery beat started", extra={"beat_entries": sorted(app.conf.beat_schedule)})


@worker_shutdown.connect  # type: ignore[untyped-decorator]
def _on_worker_shutdown(sender: Any = None, **_kwargs: Any) -> None:
    """worker 退出前把 trace 队列排空，避免最后一批 span 丢在 BatchSpanProcessor 里。"""
    from app.core.observability import flush
    from app.tasks.jobs._runtime import run_coro

    try:
        run_coro(flush())
    except Exception as exc:  # 收尾失败不该让进程带着异常退出
        logger.warning(
            "observability flush on worker shutdown failed",
            extra={"error_type": type(exc).__name__},
        )


__all__ = ["app"]
