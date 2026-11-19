"""Celery 周期任务模块（`BEAT_ENTRIES` 约定）。

`app/tasks/celery_app.py` 在 import 时用 `pkgutil.iter_modules` 遍历本目录的 `*.py`
（**下划线开头的文件跳过**，如 `_shared.py`），对每个模块依次做两件事：

1. 调 `register(app: Celery) -> None`，把本模块的 task 注册到该实例上；
2. 把本模块的 `BEAT_ENTRIES: dict[str, dict]` 合并进 `app.conf.beat_schedule`。

所以每个模块**必须**同时暴露这两个名字，缺一个或条目重名都会在 import 时抛
`RuntimeError`——定时任务静默漏跑不会在任何地方报错，只能在启动时拦住。

## 模板（新建模块照抄）

```python
from __future__ import annotations

from typing import Any, Final

from celery import Celery
from celery.schedules import crontab

TASK_NAME: Final[str] = "anyu.reports.daily_generate"


def _run(*, trace_id: str | None = None) -> None:
    ...  # 任务体：返回 None，无返回值语义（task_ignore_result=True）


def register(app: Celery) -> None:
    app.task(name=TASK_NAME, soft_time_limit=30, time_limit=60)(_run)


BEAT_ENTRIES: dict[str, Any] = {
    "daily-report-generate": {
        "task": TASK_NAME,
        "schedule": crontab(hour=7, minute=0),  # int/float = 秒
    },
}
```

## 三条硬约束

- **不要** `from app.tasks.celery_app import app`：那会形成
  `celery_app → jobs → celery_app` 的循环导入。实例由 `register(app)` 的参数传进来。
- **不要**在任务体里 `await`/`sleep` 十几秒：等待靠时间戳推进（见 `app/scheduler/medication.py`
  的 `next_retry_at`），占着 worker 会把队列堵死。
- 一上 worker 就会重跑（`task_acks_late=True`）：幂等由服务层既有闸门保证，模块里不要
  再自己加「内存里记一下」的去重。
"""
