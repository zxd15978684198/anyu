"""一次性回填：把历史日报的「用药」块从占位改成真实四类统计。

用法::

    # 先看（默认 dry-run，不写库）
    uv run python scripts/backfill_report_medication.py

    # 确认没问题再落库
    uv run python scripts/backfill_report_medication.py --apply

**为什么需要它**：迭代 8 生成日报时 `medication_events` 还不存在，用药块只能写
`{"module": "medication", "state": "pending_day7", "summary": "用药模块属 迭代 7 交付，暂未启用"}`
（迭代 8 计划裁决 S-D8-3：表缺席时不许编造统计）。迭代 7 的表与服务落地后，新日报
已按真实终态计数输出；但**已落库的历史行**里仍是那句占位说明——不补，家属翻旧日报
会看到两套口径。

## 口径（产品负责人 2026-11-09 裁决：历史也补）

- 只改 `payload.medication` 与 `payload.sources` 两个键，**健康块一个字不动**；
- 只处理 `state == "pending_day7"` 的行，已回填的行完全不碰 → 重复执行安全（幂等）；
- 不回填「生成时确实没有数据」这一事实之外的任何东西：某天库里没有到点事件，
  回填结果就是 0（`state` 可能是 `not_configured`），不会凭空造数。
- 不需要迁移：`payload` 是 JSONB，形状由 `report_service` 承诺（不改表结构）。

退出码：0 成功（含「没有需要回填的行」）；1 数据库/环境错误。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.db import session_factory  # noqa: E402
from app.models import DailyReport  # noqa: E402
from app.services import report_service  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把历史日报的用药占位回填成真实四类统计（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：uv run python scripts/backfill_report_medication.py --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加则只统计将要改动多少行（dry-run）",
    )
    return parser


async def _pending_reports(session) -> list[DailyReport]:
    """还没回填的日报行（`payload.medication.state == 'pending_day7'`）。"""
    stmt = (
        select(DailyReport)
        .where(
            DailyReport.payload["medication"]["state"].as_string()
            == report_service.MEDICATION_LEGACY_STATE
        )
        .order_by(DailyReport.report_date.desc(), DailyReport.elder_id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _run(apply: bool) -> int:
    async with session_factory() as session:
        rows = await _pending_reports(session)
        if not rows:
            print("没有需要回填的日报（全部已是真实统计口径）。")
            return 0

        print(f"待回填日报：{len(rows)} 条")
        changed = 0
        for report in rows:
            before = (report.payload or {}).get("medication") or {}
            updated = await report_service.refresh_medication_block(session, report)
            if not updated:  # pragma: no cover - 查询已按 pending_day7 过滤
                continue
            changed += 1
            after = report.payload["medication"]
            counts = after["counts"]
            print(
                f"  {report.report_date.isoformat()}  {report.elder_id}  "
                f"{before.get('state')} → {after['state']}  "
                f"已服 {counts['taken_reported']} / 拒服 {counts['refused']} / "
                f"未确认 {counts['missed_unconfirmed']} / 通知失败 {counts['notification_failed']} / "
                f"进行中 {counts['pending']}  （合计 {after['total']}）"
            )

        if not apply:
            await session.rollback()
            print(f"\nDRY-RUN：将改动 {changed} 条，未写库。确认无误后加 --apply 重跑。")
            return 0

        await session.commit()
        print(f"\n已提交：{changed} 条日报的用药块回填为真实统计。")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(apply=args.apply))
    except SQLAlchemyError as exc:  # 连不上库 / 查询失败：不吞，给出可读原因
        print(f"数据库错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
