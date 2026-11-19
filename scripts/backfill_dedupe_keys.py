"""给历史通知补 `dedupe_key`（幂等；默认 dry-run）。

背景（2026-11-13 审计）：12 条通知里 9 条 `dedupe_key` 为 NULL。它们不是重复行，
但 NULL 行不受 `uq_notifications_dedupe_key` 约束——将来同键写入时压不住历史行。

    uv run python scripts/backfill_dedupe_keys.py            # 只打印将要改什么
    uv run python scripts/backfill_dedupe_keys.py --apply    # 真正写库

推导规则（只用 payload 里已有的业务字段，不猜）：
- `family_report`  → `daily_report:{elder_id}:{report_id}:{version}`
- `safety_alert`   → `safety_alert:risk_event:{risk_event_id}`
- `health_review`  → `health_review:checkup:{elder_id}:{business_date}`

payload 缺字段的行**跳过**并打印原因（宁可不补，也不编一个假键）。脚本可重复运行。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# `scripts/` 是 sys.path[0]，仓库根不在其中，直接 `import app...` 会 ModuleNotFoundError。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select  # noqa: E402  （sys.path 修好之后才能 import app）

from app.db import session_factory  # noqa: E402
from app.models import Notification  # noqa: E402


def derive_key(row: Notification) -> str | None:
    payload: dict[str, Any] = row.payload or {}
    kind = row.kind
    if kind == "family_report":
        report_id = payload.get("report_id")
        version = payload.get("version")
        if not report_id or version is None:
            return None
        return f"daily_report:{row.elder_id}:{report_id}:{version}"
    if kind == "safety_alert":
        event_id = payload.get("risk_event_id")
        if not event_id:
            return None
        return f"safety_alert:risk_event:{event_id}"
    if kind == "health_review":
        business_date = payload.get("business_date")
        if not business_date or row.elder_id is None:
            return None
        return f"health_review:checkup:{row.elder_id}:{business_date}"
    return None


async def run(*, apply: bool) -> int:
    planned = 0
    skipped = 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Notification)
                    .where(Notification.dedupe_key.is_(None))
                    .order_by(Notification.kind, Notification.created_at)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            key = derive_key(row)
            if key is None:
                skipped += 1
                print(
                    f"[skip] {row.kind} {row.notification_id} payload={sorted((row.payload or {}).keys())}"
                )
                continue
            planned += 1
            print(f"[plan] {row.kind} {row.notification_id} -> {key}")
            if apply:
                row.dedupe_key = key
        if apply and planned:
            await session.commit()
        elif apply:
            await session.rollback()
    print(f"\nplanned={planned} skipped={skipped} apply={apply}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="补历史通知的 dedupe_key（幂等）")
    parser.add_argument("--apply", action="store_true", help="真正写库；不加只 dry-run")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
