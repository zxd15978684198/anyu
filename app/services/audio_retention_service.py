"""原始音频保留期清理（72h）：纯文件系统实现，APScheduler 与 Celery worker 共用。

## 为什么从 `app/scheduler/maintenance.py` 搬到这里

技术设计文档 §1.2 第 8 条（PRD §1.5、FR-TC-07）要求原始音频最长保留 72 小时。清理动作
本身**不读库、不开 session**，只是按 `mtime` 删白名单后缀的文件——它从来就不属于
「调度」这件事，只是历史上恰好被调度器当任务体调用了。

批 3 要把它交给 Celery worker 触发，而 `app/tasks/jobs/*` import `app/scheduler/**` 会
顺带执行 `app/scheduler/__init__.py`：那里 import 了 devices / health / medication /
reports / notifications / risks / attention / memory 全部模块，等于把整个 APScheduler
装配连同 DB 引擎、models、图驱动一起拖进 worker。搬进 services 之后依赖变成
`tasks → services` 与 `scheduler → services`，两边都不横向 import 对方（AGENTS §2.1）。

## 打点为什么也在这里

`audio_cleanup_runs_total` / `audio_cleanup_files_total` / `audio_cleanup_ms` 是
「这条任务的运行结果」，不是「哪个触发者的运行结果」：两个触发路径共用同一实现，
打点跟着实现走才不会在迁移期出现「一条路径有数、另一条没有」。名称与单位沿用迁移前的
字面量，看板与既有用例不用改。

## 日志归属

`purge_expired_audio()` 收一个可选的 `log`：调用方传自己的 logger，日志的 `logger=`
归属就还是原模块（既有用例按 `app.scheduler.maintenance` 这个 logger 名捕获日志）。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import tempfile
import time
from pathlib import Path
from typing import Final

from app.core.config import get_settings
from app.core.metrics import counter, observe_ms

logger = logging.getLogger(__name__)

#: 原始音频的约定落盘目录（W4 音频层落地前的临时契约）。
#:
#: 配置里只有 `audio_debug_save`（开关）与 `audio_retention_hours`（保留时长），**没有**
#: 目录项；这里用「系统临时目录 / anyu / audio-spool」这一条约定路径，
#: 等 `app/audio/**` 接入时把同一常量换成配置项即可（`run_audio_cleanup(root=...)`
#: 已经留好注入点，测试与将来的配置都走它）。
AUDIO_SPOOL_DIR: Final[Path] = Path(tempfile.gettempdir()) / "anyu" / "audio-spool"

#: 只删这些后缀：共享临时目录里可能有别人的文件，白名单比黑名单安全。
AUDIO_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".wav", ".pcm", ".opus", ".ogg", ".webm", ".mp3", ".m4a", ".flac"}
)

#: 打点名（迁移前就在 `app/scheduler/maintenance.py` 里，字面量不变）。
AUDIO_CLEANUP_RUNS_TOTAL: Final[str] = "audio_cleanup_runs_total"
AUDIO_CLEANUP_FILES_TOTAL: Final[str] = "audio_cleanup_files_total"
AUDIO_CLEANUP_MS: Final[str] = "audio_cleanup_ms"


def as_utc(value: datetime.datetime | None) -> datetime.datetime:
    """统一成 UTC aware；`None` → 现在；naive 按 UTC 解释（存储口径，AGENTS §8）。"""
    if value is None:
        return datetime.datetime.now(datetime.UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def report_audio_dir() -> Path:
    """日报语音的落盘根：现读 `report_service.REPORT_AUDIO_ROOT`（唯一事实源）。

    这里**不复制路径、也不 `from app.services.report_service import REPORT_AUDIO_ROOT`**：
    那是可被 `monkeypatch.setattr` 换掉的模块属性（测试指到 tmp_path），import 一次就把
    旧值固化了。
    """
    from app.services import report_service

    return Path(report_service.REPORT_AUDIO_ROOT)


def cleanup_dirs(root: Path | str | None = None) -> tuple[Path, ...]:
    """本轮要扫的根。

    `root` 是测试/临时用的单目录注入点：**给了就只扫它**，不叠加默认根——否则测试
    （`root=tmp_path`）会顺带扫到仓库里真实的 `data/report_audio/`。不给才扫两个默认根
    （spool + 日报语音）。

    > 调度器那条路径故意自己算根再传进来：`app.scheduler.maintenance.AUDIO_SPOOL_DIR`
    > 是既有用例 monkeypatch 的落点，函数内不读本模块的同名常量。
    """
    if root is not None:
        return (Path(root),)
    return (AUDIO_SPOOL_DIR, report_audio_dir())


def retention_cutoff(now: datetime.datetime | None = None) -> datetime.datetime:
    """保留期分界点：`now - settings.audio_retention_hours`（默认 72h）。"""
    hours = get_settings().audio_retention_hours
    return as_utc(now) - datetime.timedelta(hours=hours)


def purge_stale_audio(directory: Path, cutoff: datetime.datetime) -> int:
    """同步清理：删 `mtime < cutoff` 的白名单音频，返回删除条数。

    同步实现 + `asyncio.to_thread` 调用：文件 I/O 会阻塞事件循环，而调度器线程还要
    继续给别人投递任务。删不掉的单个文件只 warning（权限/占用），不影响同轮其它文件。
    """
    if not directory.is_dir():
        logger.info("原始音频目录不存在，清理空转 dir=%s", directory)
        return 0

    removed = 0
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        try:
            mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.UTC)
        except OSError as exc:
            logger.warning("读不到音频文件时间戳，跳过 path=%s：%s", path, exc)
            continue
        if mtime >= cutoff:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("删除过期音频失败 path=%s：%s", path, exc)
            continue
        removed += 1
    return removed


async def purge_expired_audio(
    directories: tuple[Path, ...],
    *,
    cutoff: datetime.datetime,
    log: logging.Logger | None = None,
) -> int:
    """跑一轮清理：逐根删过期文件，返回删除总数；打点与首尾日志都在这里。

    - 某个根抛异常只影响该根，其它根继续跑——维护任务失败不许把触发方（调度器 / worker）
      带崩；
    - `counter()` / `observe_ms()` 在 `finally` 里记：目录不存在、全删不掉、甚至抛异常，
      都要留下「跑过」的样本（区分「跑过但没文件」与「没跑」）。
    """
    emit = log or logger
    dirs_text = " ".join(str(directory) for directory in directories)
    started = time.perf_counter()
    removed = 0

    emit.info("原始音频清理开始 dirs=%s cutoff=%s", dirs_text, cutoff.isoformat())
    try:
        for directory in directories:
            try:
                removed += await asyncio.to_thread(purge_stale_audio, directory, cutoff)
            except Exception:
                emit.exception("原始音频清理失败 dir=%s", directory)
    finally:
        counter(AUDIO_CLEANUP_RUNS_TOTAL)
        counter(AUDIO_CLEANUP_FILES_TOTAL, value=removed)
        observe_ms(AUDIO_CLEANUP_MS, (time.perf_counter() - started) * 1000.0)

    emit.info(
        "原始音频清理完成 dirs=%s cutoff=%s removed=%s",
        dirs_text,
        cutoff.isoformat(),
        removed,
    )
    return removed


async def run_audio_cleanup(
    *,
    now: datetime.datetime | None = None,
    root: Path | str | None = None,
    log: logging.Logger | None = None,
) -> int:
    """Celery 侧入口：自己算根在哪儿，然后跑一轮清理；返回删除条数。

    与 `app.scheduler.maintenance.run_audio_cleanup` 的区别只有「根从哪来」：那边读
    调度器模块上的可 monkeypatch 常量，这边读本模块的默认根。行为（删什么、打什么点、
    日志文案）完全一致，因为最后都落到 `purge_expired_audio()`。
    """
    return await purge_expired_audio(cleanup_dirs(root), cutoff=retention_cutoff(now), log=log)


__all__ = [
    "AUDIO_CLEANUP_FILES_TOTAL",
    "AUDIO_CLEANUP_MS",
    "AUDIO_CLEANUP_RUNS_TOTAL",
    "AUDIO_SPOOL_DIR",
    "AUDIO_SUFFIXES",
    "as_utc",
    "cleanup_dirs",
    "purge_expired_audio",
    "purge_stale_audio",
    "report_audio_dir",
    "retention_cutoff",
    "run_audio_cleanup",
]
