"""通知幂等键（FR-ALT-04）的回归：机制 + 三个生产者的接线。

背景（2026-11-13 真机库）：`family_report` 5 行、`safety_alert` 3 行、`medication_escalation`
2 行重复，`dedupe_key` 全为 NULL——不是去重失效，是生产者压根没传键。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import Elder, Notification
from app.services import notification_service

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    await require_db_connection(engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_dedupe_key_yields_a_single_row(db_session: AsyncSession) -> None:
    # 通知有 elder_id 外键：先造一位用完回滚的长者，避免依赖库里的演示数据。
    elder = Elder(name="幂等用例长者", status="active")
    db_session.add(elder)
    await db_session.flush()
    elder_id = elder.elder_id
    key = f"daily_report:{elder_id}:{uuid.uuid4()}:1"
    first = await notification_service.create_notification(
        db_session,
        kind="family_report",
        severity="green",
        elder_id=elder_id,
        payload={"title": "日报"},
        dedupe_key=key,
    )
    second = await notification_service.create_notification(
        db_session,
        kind="family_report",
        severity="green",
        elder_id=elder_id,
        payload={"title": "日报"},
        dedupe_key=key,
    )
    assert first.notification_id == second.notification_id
    rows = (
        (await db_session.execute(select(Notification).where(Notification.elder_id == elder_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].dedupe_key == key


def test_producers_pass_a_dedupe_key() -> None:
    """三个通知生产者必须显式传键——漏传就会像真机库那样每轮插一行。"""

    expected = {
        "app/services/risk_event_service.py": "risk_event_escalation:{event.event_id}",
        "app/services/alert_service.py": "safety_alert:risk_event:{event.event_id}",
        "app/services/notification_service.py": "daily_report:{elder_id}:{report_id}:{version}",
    }
    for rel, pattern in expected.items():
        source = (_REPO / rel).read_text(encoding="utf-8")
        assert "dedupe_key=" in source, f"{rel} 没有任何 dedupe_key"
        assert pattern in source, f"{rel} 的 dedupe_key 前缀不对：缺 {pattern}"
