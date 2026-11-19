"""`CELERY_ENABLED` 开关的 `app/scheduler` 侧语义：摘下已交给 Celery 的 job。

三条断言对应三个风险：
1. 开：只摘 `CELERY_OWNED_JOBS` 里的那些，一条不多一条不少；
2. 关：一条都不摘（默认路径零行为变化，旧测试因此不需要改）；
3. 进程内任务（用药/采集/记忆/关注/**采集超时扫描**）**永远不摘**——它们是 §2.1 明确留下的；
   采集超时扫描的驱动是 lifespan 内存对象，worker 拿不到，迁过去只会退化成一个每 20 秒空转的假任务。
"""

from __future__ import annotations

from app.scheduler import CELERY_OWNED_JOBS, _drop_celery_owned_jobs, build_scheduler

_IN_PROCESS = {
    "medication-scan",
    "medication-due-scan",
    "medication-review-scan",
    "health-checkup-due",
    "health-missed-scan",
    "memory-decay",
    "memory-merge",
    "memory-capacity",
    "attention-signal-scan",
    "health-checkup-timeout",
}


def test_drop_removes_exactly_the_owned_jobs() -> None:
    scheduler = build_scheduler()
    present = {job.name for job in scheduler.get_jobs()}
    expected = present & CELERY_OWNED_JOBS

    removed = _drop_celery_owned_jobs(scheduler, enabled=True)

    assert set(removed) == expected
    remaining = {job.name for job in scheduler.get_jobs()}
    assert remaining.isdisjoint(CELERY_OWNED_JOBS)


def test_drop_is_noop_when_disabled() -> None:
    scheduler = build_scheduler()
    before = {job.name for job in scheduler.get_jobs()}

    assert _drop_celery_owned_jobs(scheduler, enabled=False) == []
    assert {job.name for job in scheduler.get_jobs()} == before


def test_in_process_jobs_are_never_dropped() -> None:
    scheduler = build_scheduler()

    _drop_celery_owned_jobs(scheduler, enabled=True)

    remaining = {job.name for job in scheduler.get_jobs()}
    assert remaining >= _IN_PROCESS
    assert _IN_PROCESS.isdisjoint(CELERY_OWNED_JOBS)
