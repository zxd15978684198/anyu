"""Celery 定时任务应用的对外唯一入口：`from app.tasks import app`。

本包只做转发，不写业务、不碰 broker：装配全在 `app/tasks/celery_app.py`，任务体在
`app/tasks/jobs/` 下的模块里各自注册（约定见 `app/tasks/jobs/__init__.py`）。

为什么在这里 re-export：worker / beat 的启动命令统一是
`celery -A app.tasks.celery_app worker`（Windows 联调加 `--pool=solo`），而应用代码
需要的是同一个实例；两处指向同一对象才不会出现「两份 conf、两套 beat_schedule」。
"""

from __future__ import annotations

from app.tasks.celery_app import app

__all__ = ["app"]
