"""退房生命周期（`app/services/lifecycle_service.py`）的真库用例（迭代 9 · W4b②）。

## 这张卡最容易出错的地方，也是本文件逐条钉住的地方

1. **原子性**：四步 + 审计在同一 savepoint 里，任一步失败整体回滚。用 monkeypatch 让
   ②（声纹）和 ⑤（审计）分别炸一次，断言「前面已经执行过的删除也消失了」—— 只测返回值的
   用例看不出半截状态，而半截状态正是退房最危险的失败模式（记忆没了、声纹还在）。
2. **幂等**：重复退房返回全 0 的报告且不重复审计，不是抛错。
3. **两种「解除」**：`elder_guardians` 有 status → 打 `unbound` 保痕（AC-24）；
   `elder_staff_assignments` 没有 status 列 → 删行。两条断言故意分开写，
   将来谁把 staff 表加上 status 列，这里会立刻红。
4. **只动目标长者**：退房是数据面最重的一次删除，跨长者串号不可接受。

库与事务范式同 `tests/test_staff_service.py`（`NullPool` + 外层事务 + savepoint session），
用例结束后整体 rollback。
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import (
    AuditLog,
    ConversationThread,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    MemoryChunk,
    Room,
    StaffAccount,
    StaffToken,
    Voiceprint,
)
from app.models.organization import Device
from app.services import lifecycle_service
from app.services.lifecycle_service import CheckoutReport, checkout_elder
from conftest import require_db_connection

ELDER_ID = uuid.UUID("cccc3333-3333-4333-8333-333333333101")
OTHER_ELDER_ID = uuid.UUID("cccc3333-3333-4333-8333-333333333102")
ABSENT_ELDER_ID = uuid.UUID("cccc3333-3333-4333-8333-3333333331ff")
GUARDIAN_ID = uuid.UUID("cccc3333-3333-4333-8333-333333333201")
STAFF_ID = uuid.UUID("cccc3333-3333-4333-8333-333333333301")
ACTOR_ID = uuid.UUID("cccc3333-3333-4333-8333-333333333401")
ROOM_ID = uuid.UUID("cccc3333-3333-4333-8333-333333333501")
DEVICE_ID = uuid.UUID("cccc3333-3333-4333-8333-333333333601")

TRACE_ID = "trace-w4b-checkout"
PASSWORD_HASH = "$2b$12$placeholderplaceholderplaceholderplaceholder"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    conn: AsyncConnection = await require_db_connection(engine)
    trans = await conn.begin()
    sess = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        await sess.close()
        await trans.rollback()
        await conn.close()


# ------------------------------------------------------------------ 造数


async def _seed_elders(session: AsyncSession) -> None:
    session.add_all(
        [
            Elder(elder_id=ELDER_ID, name="长者甲", status="active"),
            Elder(elder_id=OTHER_ELDER_ID, name="长者乙", status="active"),
        ]
    )
    await session.flush()


async def _seed_device(session: AsyncSession) -> None:
    """一台未绑定长者的设备（`devices.elder_id` 可空），给会话表当 FK 兜底。"""
    session.add(Room(room_id=ROOM_ID, room_no="A-301", status="partial"))
    await session.flush()
    session.add(
        Device(
            device_id=DEVICE_ID, room_id=ROOM_ID, elder_id=None, token_hash="hash", status="offline"
        )
    )
    await session.flush()


async def _seed_memories(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> None:
    session.add_all(
        [
            MemoryChunk(elder_id=elder_id, content="活动记忆", status="active"),
            MemoryChunk(elder_id=elder_id, content="被归并", status="superseded"),
            MemoryChunk(elder_id=elder_id, content="已软删", status="deleted"),
        ]
    )
    await session.flush()


async def _seed_voiceprints(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> None:
    session.add_all(
        [
            Voiceprint(
                elder_id=elder_id,
                version=1,
                sample_index=1,
                embedding=b"\x01" * 16,
                status="active",
            ),
            Voiceprint(
                elder_id=elder_id,
                version=1,
                sample_index=2,
                embedding=b"\x02" * 16,
                status="retired",
            ),
        ]
    )
    await session.flush()


async def _seed_threads(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> None:
    """两条未结束 + 一条已结束（长者的），外加一条 `generic` 会话（`elder_id` 必须为 NULL）。"""
    session.add_all(
        [
            ConversationThread(
                thread_id=f"{elder_id}-open-1",
                elder_id=elder_id,
                device_id=DEVICE_ID,
                identity_source="voice",
                identity_confidence=Decimal("0.910"),
            ),
            ConversationThread(
                thread_id=f"{elder_id}-open-2",
                elder_id=elder_id,
                device_id=DEVICE_ID,
                identity_source="device",
            ),
            ConversationThread(
                thread_id=f"{elder_id}-closed",
                elder_id=elder_id,
                device_id=DEVICE_ID,
                identity_source="confirm",
                ended_at=datetime.datetime(2026, 11, 1, 9, 30, tzinfo=datetime.UTC),
            ),
        ]
    )
    await session.flush()


async def _seed_relations(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> None:
    session.add_all(
        [
            GuardianAccount(
                guardian_id=GUARDIAN_ID,
                name="长子",
                phone="13900000001",
                password_hash=PASSWORD_HASH,
                token_version=0,
                status="active",
            ),
            StaffAccount(
                staff_id=STAFF_ID,
                name="王护工",
                role="care_worker",
                phone="13800000001",
                password_hash=PASSWORD_HASH,
                token_version=0,
                status="active",
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            ElderGuardian(
                elder_id=elder_id, guardian_id=GUARDIAN_ID, relation="长子", status="active"
            ),
            ElderStaffAssignment(elder_id=elder_id, staff_id=STAFF_ID, relation="primary_care"),
        ]
    )
    await session.flush()


async def _seed_full_elder(session: AsyncSession) -> None:
    """一位「什么都有」的长者：记忆 + 声纹 + 会话 + 家属/护工关系。"""
    await _seed_elders(session)
    await _seed_device(session)
    await _seed_memories(session)
    await _seed_voiceprints(session)
    await _seed_threads(session)
    await _seed_relations(session)


# ------------------------------------------------------------------ 查询helpers


async def _count(session: AsyncSession, model: Any, elder_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(model).where(model.elder_id == elder_id)
    return int((await session.execute(stmt)).scalar_one())


async def _checkout_audits(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog)
        .where(
            AuditLog.resource_id == elder_id,
            AuditLog.action == lifecycle_service.AUDIT_ACTION_CHECKOUT,
        )
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _guardian_status(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> str | None:
    rows = await session.execute(
        select(ElderGuardian.status)
        .where(ElderGuardian.elder_id == elder_id)
        .execution_options(populate_existing=True)
    )
    return rows.scalar_one_or_none()


# ------------------------------------------------------------------ 1. 四步计数


async def test_checkout_counts_all_four_steps(session: AsyncSession) -> None:
    """一份报告里四个计数各就各位：3 条记忆 / 2 个声纹 / 2 条未结束会话 / 2 条关系。"""
    await _seed_full_elder(session)

    report = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert report.elder_id == ELDER_ID
    assert report.memories_purged == 3
    assert report.voiceprints_removed == 2
    assert report.sessions_closed == 2  # 第 3 条本来就已结束
    assert report.relations_removed == 2  # 1 家属打标记 + 1 护工删行
    assert report.audit_log_id is not None


async def test_checkout_hard_deletes_memories(session: AsyncSession) -> None:
    """退房是**硬删**：不是把 status 改成 deleted，而是行都没了。"""
    await _seed_full_elder(session)

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _count(session, MemoryChunk, ELDER_ID) == 0
    rows = await session.execute(
        select(func.count())
        .select_from(MemoryChunk)
        .where(MemoryChunk.elder_id == ELDER_ID, MemoryChunk.status == "deleted")
    )
    # 若哪天有人把退房改成"软删"，这条会红：软删会留下 status='deleted' 的行
    assert int(rows.scalar_one()) == 0


async def test_checkout_removes_retired_voiceprints_too(session: AsyncSession) -> None:
    """声纹不分 active/retired：生物特征不能留，哪怕它已经退役。"""
    await _seed_full_elder(session)

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _count(session, Voiceprint, ELDER_ID) == 0


# ------------------------------------------------------------------ 2. 会话收口


async def test_checkout_closes_only_open_sessions_and_keeps_old_end_time(
    session: AsyncSession,
) -> None:
    """只回填 `ended_at IS NULL` 的会话；已结束会话的结束时间必须原样保留。"""
    await _seed_full_elder(session)

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    rows = await session.execute(
        select(ConversationThread.thread_id, ConversationThread.ended_at)
        .where(ConversationThread.elder_id == ELDER_ID)
        .order_by(ConversationThread.thread_id.asc())
        .execution_options(populate_existing=True)
    )
    ended_at = dict(rows.all())
    assert ended_at[f"{ELDER_ID}-open-1"] is not None
    assert ended_at[f"{ELDER_ID}-open-2"] is not None
    # 已结束的那条不能被改写成"退房时刻"，否则「上次说话是什么时候」就丢了
    assert ended_at[f"{ELDER_ID}-closed"] == datetime.datetime(
        2026, 11, 1, 9, 30, tzinfo=datetime.UTC
    )


async def test_checkout_ignores_generic_threads(session: AsyncSession) -> None:
    """`identity_source='generic'` 的会话 `elder_id` 为 NULL，不属于任何长者，退房不动它。"""
    await _seed_full_elder(session)
    session.add(
        ConversationThread(
            thread_id="generic-1", elder_id=None, device_id=DEVICE_ID, identity_source="generic"
        )
    )
    await session.flush()

    report = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert report.sessions_closed == 2
    generic = await session.execute(
        select(ConversationThread.ended_at)
        .where(ConversationThread.thread_id == "generic-1")
        .execution_options(populate_existing=True)
    )
    assert generic.scalar_one() is None


# ------------------------------------------------------------------ 3. 关系解除


async def test_checkout_unbinds_guardian_but_keeps_the_row(session: AsyncSession) -> None:
    """家属关系**打标记不删行**（AC-24 解绑保痕：家属端要看得到「曾经绑过谁」）。"""
    await _seed_full_elder(session)

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _count(session, ElderGuardian, ELDER_ID) == 1
    assert await _guardian_status(session) == "unbound"


async def test_checkout_deletes_staff_assignment(session: AsyncSession) -> None:
    """护工关系没有 status 列（表结构决定），只能删行。"""
    await _seed_full_elder(session)

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _count(session, ElderStaffAssignment, ELDER_ID) == 0


# ------------------------------------------------------------------ 4. 摘要审计


async def test_checkout_writes_one_summary_audit(session: AsyncSession) -> None:
    """恰好一条 `elder.checkout`，detail 带各步计数（+ 两种解除方式的明细）。"""
    await _seed_full_elder(session)

    report = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID, trace_id=TRACE_ID)

    audits = await _checkout_audits(session)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.audit_id == report.audit_log_id
    assert audit.actor_type == "staff"
    assert audit.actor_id == ACTOR_ID
    assert audit.resource_type == "elders"
    assert audit.resource_id == ELDER_ID
    assert audit.detail == {
        "reason": "checkout",
        "memories_purged": 3,
        "voiceprints_removed": 2,
        "sessions_closed": 2,
        "relations_removed": 2,
        "relations_unbound": 1,
        "relations_deleted": 1,
        # 迭代 10（FR-ADM-09）：房态/设备面。本夹具的设备没绑长者，故两个计数为 0，
        # 但 `elder_status` 与 `status_changed` 证明「置 away」这一步真的发生了。
        "devices_unbound": 0,
        "staff_cards_revoked": 0,
        "elder_status": "away",
        "status_changed": True,
        "archived_at": None,
        "trace_id": TRACE_ID,
    }


async def test_checkout_records_reason(session: AsyncSession) -> None:
    """退房/转院/身故共用入口，靠 `reason` 区分（本函数不枚举白名单）。"""
    await _seed_full_elder(session)

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID, reason="transfer")

    audit = (await _checkout_audits(session))[0]
    assert audit.detail["reason"] == "transfer"


# ------------------------------------------------------------------ 5. 幂等


async def test_checkout_is_idempotent(session: AsyncSession) -> None:
    """第二次退房：计数全 0、不写第二条审计、不抛错。"""
    await _seed_full_elder(session)
    first = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    second = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert first.audit_log_id is not None
    assert second.memories_purged == 0
    assert second.voiceprints_removed == 0
    assert second.sessions_closed == 0
    assert second.relations_removed == 0
    # 什么都没做，就不该再留一条 0 计数审计
    assert second.audit_log_id is None
    assert len(await _checkout_audits(session)) == 1


async def test_checkout_of_absent_elder_returns_zero_report(session: AsyncSession) -> None:
    """库里没有这位长者：返回全 0 报告而不是抛错（幂等口径的延伸）。"""
    report = await checkout_elder(session, elder_id=ABSENT_ELDER_ID, actor_id=ACTOR_ID)

    assert report == CheckoutReport(
        elder_id=ABSENT_ELDER_ID,
        memories_purged=0,
        voiceprints_removed=0,
        sessions_closed=0,
        relations_removed=0,
        audit_log_id=None,
    )
    assert await _checkout_audits(session, ABSENT_ELDER_ID) == []


# ------------------------------------------------------------------ 6. 隔离


async def test_checkout_leaves_other_elders_untouched(session: AsyncSession) -> None:
    """跨长者隔离：别人一条都不能少。"""
    await _seed_full_elder(session)
    await _seed_memories(session, OTHER_ELDER_ID)
    await _seed_voiceprints(session, OTHER_ELDER_ID)
    await _seed_threads(session, OTHER_ELDER_ID)
    # 另一位长者绑同一位家属（一人可绑多位长者，关系随绑定变化）
    session.add(ElderGuardian(elder_id=OTHER_ELDER_ID, guardian_id=GUARDIAN_ID, relation="长子"))
    await session.flush()

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _count(session, MemoryChunk, OTHER_ELDER_ID) == 3
    assert await _count(session, Voiceprint, OTHER_ELDER_ID) == 2
    assert await _count(session, ElderGuardian, OTHER_ELDER_ID) == 1
    assert await _guardian_status(session, OTHER_ELDER_ID) == "active"
    open_other = await session.execute(
        select(func.count())
        .select_from(ConversationThread)
        .where(ConversationThread.elder_id == OTHER_ELDER_ID, ConversationThread.ended_at.is_(None))
    )
    assert int(open_other.scalar_one()) == 2


# ------------------------------------------------------------------ 7. 回滚


async def test_checkout_rolls_back_when_voiceprint_step_fails(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """② 炸了 → ① 已经执行过的记忆硬删必须一起消失（不许半途）。"""
    await _seed_full_elder(session)

    async def _boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("voiceprint step exploded")

    monkeypatch.setattr(lifecycle_service, "_remove_voiceprints", _boom)

    with pytest.raises(RuntimeError, match="voiceprint step exploded"):
        await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    # ① 在 ② 之前执行过；savepoint 回滚后这 3 条必须还在
    assert await _count(session, MemoryChunk, ELDER_ID) == 3
    assert await _checkout_audits(session) == []


async def test_checkout_rolls_back_when_audit_step_fails(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⑤ 炸了（最后一步）→ 前面四步全部回滚：退房"做了但没留痕"必须整体不发生。"""
    await _seed_full_elder(session)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("audit step exploded")

    monkeypatch.setattr(lifecycle_service, "write_audit", _boom)

    with pytest.raises(RuntimeError, match="audit step exploded"):
        await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _count(session, MemoryChunk, ELDER_ID) == 3
    assert await _count(session, Voiceprint, ELDER_ID) == 2
    assert await _count(session, ElderStaffAssignment, ELDER_ID) == 1
    assert await _guardian_status(session) == "active"
    open_threads = await session.execute(
        select(func.count())
        .select_from(ConversationThread)
        .where(ConversationThread.elder_id == ELDER_ID, ConversationThread.ended_at.is_(None))
    )
    assert int(open_threads.scalar_one()) == 2


async def test_checkout_rejects_bad_actor_type_before_touching_data(
    session: AsyncSession,
) -> None:
    """非法 `actor_type` 在**一条语句都没执行**时抛，不留下"删了一半才报错"。"""
    await _seed_full_elder(session)

    with pytest.raises(ValueError, match="actor_type must be one of"):
        await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID, actor_type="elder")

    assert await _count(session, MemoryChunk, ELDER_ID) == 3
    assert await _count(session, Voiceprint, ELDER_ID) == 2


# ------------------------------------------------------------------ 8. 房态与设备面（FR-ADM-09）


async def _bind_device(session: AsyncSession, *, elder_id: uuid.UUID) -> None:
    """把 `DEVICE_ID` 绑到该长者（夹具默认让它不绑人，这里显式绑）。"""
    device = await session.get(Device, DEVICE_ID)
    assert device is not None
    device.elder_id = elder_id
    device.status = "online"
    await session.flush()


async def _seed_card_on_device(session: AsyncSession) -> None:
    """在 `DEVICE_ID` 上签一张工牌（`staff_tokens` 一行），给「退房要吊销」当靶子。"""
    session.add(
        StaffToken(
            token_hash="card-hash-before-checkout",
            staff_id=STAFF_ID,
            device_id=DEVICE_ID,
            scope="workorder:handle",
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=1),
        )
    )
    await session.flush()


async def _device_row(session: AsyncSession) -> Device:
    rows = await session.execute(
        select(Device)
        .where(Device.device_id == DEVICE_ID)
        .execution_options(populate_existing=True)
    )
    return rows.scalar_one()


async def _elder_status(session: AsyncSession, elder_id: uuid.UUID = ELDER_ID) -> str | None:
    rows = await session.execute(
        select(Elder.status)
        .where(Elder.elder_id == elder_id)
        .execution_options(populate_existing=True)
    )
    return rows.scalar_one_or_none()


async def test_checkout_unbinds_devices_and_revokes_staff_cards(session: AsyncSession) -> None:
    """退房后：设备解绑 + 置 offline + 轮换令牌哈希 + 该设备上的工牌被删。"""
    await _seed_full_elder(session)
    await _bind_device(session, elder_id=ELDER_ID)
    await _seed_card_on_device(session)

    report = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    device = await _device_row(session)
    assert device.elder_id is None
    assert device.status == "offline"
    assert device.token_hash != "hash"  # 轮换过：旧设备令牌立即失效
    assert report.devices_unbound == 1
    assert report.staff_cards_revoked == 1
    cards = await session.execute(
        select(func.count()).select_from(StaffToken).where(StaffToken.device_id == DEVICE_ID)
    )
    assert cards.scalar_one() == 0


async def test_checkout_sets_elder_away_and_keeps_the_row(session: AsyncSession) -> None:
    """房态收口是**状态迁移**而不是删人：行还在，status='away'（调度的唯一在场判据）。"""
    await _seed_full_elder(session)

    report = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _elder_status(session) == "away"
    assert report.elder_status == "away"
    assert report.archived is False


async def test_checkout_archive_sets_archived_and_records_time(session: AsyncSession) -> None:
    """`archive=True` → `archived`，归档时刻进审计（`elders` 表没有该列）。"""
    await _seed_full_elder(session)

    report = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID, archive=True)

    assert await _elder_status(session) == "archived"
    assert report.elder_status == "archived"
    assert report.archived is True
    detail = (await _checkout_audits(session))[0].detail
    assert detail["elder_status"] == "archived"
    assert detail["archived_at"] is not None


async def test_second_checkout_does_not_resurrect_archived_elder(session: AsyncSession) -> None:
    """已 archived 的长者再走普通退房：状态不变、不写第二条审计（终态不可被回退）。"""
    await _seed_full_elder(session)
    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID, archive=True)

    second = await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    assert await _elder_status(session) == "archived"
    assert second.audit_log_id is None
    assert len(await _checkout_audits(session)) == 1


async def test_checkout_leaves_other_elders_device_and_status_untouched(
    session: AsyncSession,
) -> None:
    """只动这一位长者：别人的设备仍绑着、状态仍是 active。"""
    await _seed_full_elder(session)
    await _bind_device(session, elder_id=ELDER_ID)

    other_room = uuid.UUID("cccc3333-3333-4333-8333-333333333502")
    other_device = uuid.UUID("cccc3333-3333-4333-8333-333333333602")
    session.add(Room(room_id=other_room, room_no="A-302", status="partial"))
    await session.flush()
    session.add(
        Device(
            device_id=other_device,
            room_id=other_room,
            elder_id=OTHER_ELDER_ID,
            token_hash="other-hash",
            status="online",
        )
    )
    await session.flush()

    await checkout_elder(session, elder_id=ELDER_ID, actor_id=ACTOR_ID)

    rows = await session.execute(
        select(Device.elder_id)
        .where(Device.device_id == other_device)
        .execution_options(populate_existing=True)
    )
    assert rows.scalar_one() == OTHER_ELDER_ID
    assert await _elder_status(session, OTHER_ELDER_ID) == "active"


# ------------------------------------------------------------------ 9. 报告对象本身


async def test_checkout_report_is_frozen_and_slotted() -> None:
    """回执不可被调用方改写；`slots` 保证不会有调用方塞进来的野字段。"""
    report = CheckoutReport(
        elder_id=ELDER_ID,
        memories_purged=0,
        voiceprints_removed=0,
        sessions_closed=0,
        relations_removed=0,
        audit_log_id=None,
    )

    assert [field.name for field in dataclasses.fields(report)] == [
        "elder_id",
        "memories_purged",
        "voiceprints_removed",
        "sessions_closed",
        "relations_removed",
        "audit_log_id",
        # 迭代 10 新增（FR-ADM-09）：房态/设备面。顺序锁死 = 谁再插字段这里会红。
        "devices_unbound",
        "staff_cards_revoked",
        "elder_status",
        "archived",
    ]
    assert not hasattr(report, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.memories_purged = 1  # type: ignore[misc]
