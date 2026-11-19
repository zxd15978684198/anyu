"""日报语音 WAV 纳入 72h 清理（攻防检验 §4.6-S4 / B4-07）。

被验证的事实：`data/report_audio/<elder_id>/<date>.wav` 是**原始音频**，同样受技术设计
文档 §1.2 第 8 条的 72h 保留期约束；但在本次改动前 `run_audio_cleanup` 只扫 spool 目录，
这批文件永远不删。这里用 `tmp_path` + `monkeypatch` 把两个根都指到临时目录，断言：

1. 默认（不传 `root`）确实扫两个根，且日志能看出扫了哪些根；
2. 73h 前的报文 WAV 被删、71h 的保留；
3. 目录不存在时静默空转、不抛；
4. 显式传 `root` 只扫那一个目录（否则既有测试会误删仓库里真实的 `data/report_audio/`）。
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.metrics import metrics_snapshot, reset_metrics
from app.scheduler import maintenance as maintenance_jobs
from app.services import report_service

_NOW = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.UTC)


def _age(path: Path, hours: float) -> None:
    """把文件 mtime 拨到 `hours` 小时前（清理只看 mtime）。"""
    stamp = (_NOW - datetime.timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


def _report_wav(root: Path, elder_id: str, day: str, *, hours: float) -> Path:
    target = root / elder_id / f"{day}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"RIFF....")
    _age(target, hours)
    return target


@pytest.fixture
def two_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """把 spool 与日报语音两个默认根都指到 tmp_path 下，测试不碰真实目录。"""
    spool = tmp_path / "spool"
    report_audio = tmp_path / "report_audio"
    spool.mkdir()
    report_audio.mkdir()
    monkeypatch.setattr(maintenance_jobs, "AUDIO_SPOOL_DIR", spool)
    monkeypatch.setattr(report_service, "REPORT_AUDIO_ROOT", report_audio)
    return spool, report_audio


def _metrics_enabled() -> bool:
    return bool(get_settings().metrics_enabled)


async def test_default_run_scans_both_roots(
    two_roots: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """默认（不传 root）两个根都扫：各删一份过期 WAV，各留一份新鲜的。"""
    spool, report_audio = two_roots
    spool_stale = spool / "turn-1.wav"
    spool_stale.write_bytes(b"RIFF....")
    _age(spool_stale, 73)
    spool_fresh = spool / "turn-2.wav"
    spool_fresh.write_bytes(b"RIFF....")
    _age(spool_fresh, 5)

    report_stale = _report_wav(report_audio, "elder-a", "2026-11-05", hours=73)
    report_fresh = _report_wav(report_audio, "elder-b", "2026-11-08", hours=2)

    with caplog.at_level(logging.INFO, logger=maintenance_jobs.__name__):
        removed = await maintenance_jobs.run_audio_cleanup(now=_NOW)

    assert removed == 2
    assert not spool_stale.exists()
    assert not report_stale.exists()
    assert spool_fresh.exists()
    assert report_fresh.exists()
    # 护栏「日志能看出扫了哪些根」：两个根的绝对路径都要出现在开跑日志里。
    started = [r for r in caplog.records if "原始音频清理开始" in r.getMessage()]
    assert len(started) == 1
    line = started[0].getMessage()
    assert str(spool) in line
    assert str(report_audio) in line


async def test_expired_report_wav_removed(two_roots: tuple[Path, Path]) -> None:
    """攻防验收口径：造一个 73h 前的 report_audio/<elder>/<date>.wav → 跑清理 → 被删。"""
    _, report_audio = two_roots
    stale = _report_wav(report_audio, "elder-a", "2026-11-05", hours=73)

    removed = await maintenance_jobs.run_audio_cleanup(now=_NOW)

    assert removed == 1
    assert not stale.exists()


async def test_fresh_report_wav_kept(two_roots: tuple[Path, Path]) -> None:
    """72h 内（这里 71h）的日报 WAV 不删，空目录与子目录结构也不动。"""
    _, report_audio = two_roots
    fresh = _report_wav(report_audio, "elder-a", "2026-11-08", hours=71)

    removed = await maintenance_jobs.run_audio_cleanup(now=_NOW)

    assert removed == 0
    assert fresh.exists()
    assert fresh.parent.is_dir()


async def test_missing_report_audio_dir_is_silent(
    two_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日报语音目录不存在（首次运行常见）→ 静默空转，不抛、不影响另一个根。"""
    spool, _ = two_roots
    stale = spool / "turn-1.wav"
    stale.write_bytes(b"RIFF....")
    _age(stale, 73)
    monkeypatch.setattr(
        report_service, "REPORT_AUDIO_ROOT", spool / "never-created" / "report_audio"
    )

    removed = await maintenance_jobs.run_audio_cleanup(now=_NOW)

    assert removed == 1
    assert not stale.exists()


async def test_explicit_root_scans_only_that_dir(two_roots: tuple[Path, Path]) -> None:
    """显式 `root` 是单目录注入点：不叠加默认根，避免测试误删仓库真实文件。"""
    spool, report_audio = two_roots
    report_stale = _report_wav(report_audio, "elder-a", "2026-11-05", hours=73)

    removed = await maintenance_jobs.run_audio_cleanup(now=_NOW, root=spool)

    assert removed == 0
    assert report_stale.exists()


@pytest.mark.skipif(not _metrics_enabled(), reason="metrics_enabled=False 时打点是 no-op")
async def test_both_roots_counted_in_one_run(two_roots: tuple[Path, Path]) -> None:
    """两个根合计算一次 run、files_total 累加（不按目录拆成两轮）。"""
    spool, report_audio = two_roots
    for name in ("a.wav", "b.mp3"):
        path = spool / name
        path.write_bytes(b"RIFF....")
        _age(path, 96)
    _report_wav(report_audio, "elder-a", "2026-11-01", hours=100)

    reset_metrics()
    removed = await maintenance_jobs.run_audio_cleanup(now=_NOW)

    counters = metrics_snapshot()["counters"]
    assert removed == 3
    assert counters["audio_cleanup_runs_total"] == 1
    assert counters["audio_cleanup_files_total"] == 3
