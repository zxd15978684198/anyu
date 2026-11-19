"""聚合预警四信号（FR-HLT-10，PRD:374）。

验收口径（对应 task-4）：

1. **四信号各一例命中**，且**边界值**都要断言：情绪恰好连续 2 次 ≤2（1 次不报、中间插
   一次好分数就断）、沉默恰好 48h（47h59m 不报）、深夜恰好 3 次（2 次不报）、同一意图
   恰好 3 次（2 次不报、播放控制类意图不算）。
2. **口径红线**：所有信号只有 `level="attention"` 一档，文案里不许出现任何诊断表述，
   报告自带免责声明（AGENTS §4.4）。

隔离方式与 `tests/test_medication_plan_edit.py` 同款：真库 + `NullPool` 单连接 + 外层
事务，测试结束整体回滚。
"""

from __future__ import annotations

import datetime
import uuid
import zoneinfo
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.conftest import require_db_connection

from app.core.config import get_settings
from app.models import DecisionLog, Elder, HealthRecord
from app.services.alert_aggregation import (
    ATTENTION_WINDOW_DAYS,
    DISCLAIMER,
    FORBIDDEN_WORDING,
    LEVEL_ATTENTION,
    MOOD_LOW_STREAK_REQUIRED,
    NIGHT_MIN_OCCURRENCES,
    REPEAT_MIN_OCCURRENCES,
    RULE_VERSION,
    SIGNAL_ABNORMAL_SILENCE,
    SIGNAL_CODES,
    SIGNAL_MOOD_LOW_STREAK,
    SIGNAL_NIGHT_INTERACTION,
    SIGNAL_REPEATED_QUESTION,
    SILENCE_THRESHOLD_HOURS,
    AlertAggregationError,
    AttentionReport,
    AttentionWordingError,
    assert_attention_wording,
    evaluate_attention_signals,
)

SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")

#: 固定锚点：所有用例都相对它造数据，边界差 1 分钟也必须能断言。
NOW = datetime.datetime(2026, 11, 9, 12, 0, tzinfo=SHANGHAI)


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    connection = await require_db_connection(db_engine)
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture
async def elder_id(db: AsyncSession) -> uuid.UUID:
    """被测长者：姓名带随机后缀，避免与开发库里的演示长者撞名。"""
    new_id = uuid.uuid4()
    await db.execute(
        Elder.__table__.insert(),
        [{"elder_id": new_id, "name": f"测试长者{uuid.uuid4().hex[:6]}", "status": "active"}],
    )
    await db.flush()
    return new_id


# ------------------------------------------------------------------ 造数据


async def _mood(
    db: AsyncSession, elder_id: uuid.UUID, scores: list[int], *, last_day: datetime.date
) -> None:
    """按「最后一天 = last_day」倒排写入若干条情绪自评（每天一条）。"""
    for offset, score in enumerate(scores):
        day = last_day - datetime.timedelta(days=len(scores) - 1 - offset)
        db.add(
            HealthRecord(
                record_id=uuid.uuid4(),
                elder_id=elder_id,
                record_date=day,
                source="voice_scheduled",
                mood_score=score,
                grade="green",
            )
        )
    await db.flush()


def _log(
    db: AsyncSession,
    elder_id: uuid.UUID,
    *,
    at: datetime.datetime,
    intent: str | None = None,
) -> None:
    """一条路由决策 = 一次交互（`created_at` 是本地时区感知时间）。"""
    db.add(
        DecisionLog(
            id=uuid.uuid4(),
            elder_id=str(elder_id),
            layer="L1",
            intent=intent,
            route_ms=120,
            created_at=at,
        )
    )


async def _report(
    db: AsyncSession, elder_id: uuid.UUID, *, now: datetime.datetime = NOW
) -> AttentionReport:
    await db.flush()
    return await evaluate_attention_signals(db, elder_id=elder_id, now=now)


# ------------------------------------------------------------------ ① 情绪连续低位


async def test_mood_low_streak_fires_on_exactly_two(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """边界：恰好连续 2 次 ≤2 → 报；只要中间插一次 3 分就断，1 次也不报。"""
    await _mood(db, elder_id, [1, 2], last_day=datetime.date(2026, 11, 8))
    report = await _report(db, elder_id)

    assert SIGNAL_MOOD_LOW_STREAK in report.codes
    signal = next(item for item in report.signals if item.code == SIGNAL_MOOD_LOW_STREAK)
    assert signal.level == LEVEL_ATTENTION
    assert signal.evidence["streak"] == MOOD_LOW_STREAK_REQUIRED == 2
    assert signal.evidence["threshold"] == 2
    assert signal.evidence["scores"] == [1, 2]
    assert "连续不高于 2 分" in signal.detail


async def test_mood_low_single_sample_is_not_enough(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """边界：只有 1 次低位 → 不报（「连续 ≥2 次」是硬口径）。"""
    await _mood(db, elder_id, [2], last_day=datetime.date(2026, 11, 8))
    report = await _report(db, elder_id)
    assert SIGNAL_MOOD_LOW_STREAK not in report.codes


async def test_mood_streak_breaks_on_good_score(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """1、1、3、1：从最新往前数只到第一个 3 分就停 → streak=1，不报。"""
    await _mood(db, elder_id, [1, 1, 3, 1], last_day=datetime.date(2026, 11, 8))
    report = await _report(db, elder_id)
    assert SIGNAL_MOOD_LOW_STREAK not in report.codes


async def test_mood_superseded_records_are_ignored(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """被后续修订替代（`superseded_by` 非空）的旧记录不参与连续判定。"""
    keeper_id = uuid.uuid4()
    old_id = uuid.uuid4()
    db.add(
        HealthRecord(
            record_id=old_id,
            elder_id=elder_id,
            record_date=datetime.date(2026, 11, 7),
            source="voice_scheduled",
            mood_score=1,
            grade="green",
        )
    )
    db.add(
        HealthRecord(
            record_id=keeper_id,
            elder_id=elder_id,
            record_date=datetime.date(2026, 11, 7),
            source="voice_scheduled",
            mood_score=4,
            grade="green",
            revision=2,
        )
    )
    db.add(
        HealthRecord(
            record_id=uuid.uuid4(),
            elder_id=elder_id,
            record_date=datetime.date(2026, 11, 8),
            source="voice_scheduled",
            mood_score=2,
            grade="green",
        )
    )
    await db.flush()
    # 旧的那条在库里没有 superseded_by 时会被算进去；这里显式把它标成被替代。
    record = await db.get(HealthRecord, old_id)
    assert record is not None
    record.superseded_by = keeper_id
    await db.flush()

    report = await _report(db, elder_id)
    assert SIGNAL_MOOD_LOW_STREAK not in report.codes


# ------------------------------------------------------------------ ② 异常沉默


async def test_silence_fires_on_exactly_48_hours(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """边界：恰好 48.0h → 报（阈值含等号）；47h59m → 不报。"""
    _log(db, elder_id, at=NOW - datetime.timedelta(hours=SILENCE_THRESHOLD_HOURS))
    report = await _report(db, elder_id)

    assert SIGNAL_ABNORMAL_SILENCE in report.codes
    signal = next(item for item in report.signals if item.code == SIGNAL_ABNORMAL_SILENCE)
    assert signal.evidence["silent_hours"] == 48.0
    assert signal.evidence["threshold_hours"] == SILENCE_THRESHOLD_HOURS
    assert "48 小时" in signal.detail


async def test_silence_one_minute_short_does_not_fire(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """边界：47h59m 差 1 分钟 → 不报，避免把「一天半没说话」误当异常沉默。"""
    _log(db, elder_id, at=NOW - datetime.timedelta(hours=47, minutes=59))
    report = await _report(db, elder_id)
    assert SIGNAL_ABNORMAL_SILENCE not in report.codes


async def test_silence_ignores_elder_without_any_interaction(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """一次交互都没有 → 不报「沉默」（那是设备/接入问题，不该由这条信号下结论）。"""
    report = await _report(db, elder_id)
    assert SIGNAL_ABNORMAL_SILENCE not in report.codes


# ------------------------------------------------------------------ ③ 深夜频繁交互


async def test_night_interaction_fires_on_exactly_three(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """边界：近 7 天本地 23:00–05:00 恰好 3 次 → 报。"""
    for day in (6, 7, 8):
        _log(db, elder_id, at=datetime.datetime(2026, 11, day, 23, 30, tzinfo=SHANGHAI))
    report = await _report(db, elder_id)

    assert SIGNAL_NIGHT_INTERACTION in report.codes
    signal = next(item for item in report.signals if item.code == SIGNAL_NIGHT_INTERACTION)
    assert signal.evidence["occurrences"] == NIGHT_MIN_OCCURRENCES == 3
    assert signal.evidence["night_window"] == "23:00-05:00"


async def test_night_interaction_two_times_does_not_fire(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """边界：2 次 → 不报；凌晨 04:30 也算深夜窗口内（05:00 前）。"""
    _log(db, elder_id, at=datetime.datetime(2026, 11, 8, 4, 30, tzinfo=SHANGHAI))
    _log(db, elder_id, at=datetime.datetime(2026, 11, 8, 23, 10, tzinfo=SHANGHAI))
    report = await _report(db, elder_id)
    assert SIGNAL_NIGHT_INTERACTION not in report.codes


async def test_daytime_interactions_are_not_night(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """白天 3 次交互不算深夜（窗口边界外）。"""
    for hour in (9, 14, 20):
        _log(db, elder_id, at=datetime.datetime(2026, 11, 8, hour, 0, tzinfo=SHANGHAI))
    report = await _report(db, elder_id)
    assert SIGNAL_NIGHT_INTERACTION not in report.codes


# ------------------------------------------------------------------ ④ 重复问同一类问题


async def test_repeated_question_fires_on_exactly_three(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """边界：近 24h 同一 `intent` 恰好 3 次 → 报（认知退化信号的结构化代理）。"""
    for minute in (0, 20, 40):
        _log(
            db,
            elder_id,
            at=datetime.datetime(2026, 11, 9, 10, minute, tzinfo=SHANGHAI),
            intent="ask_time",
        )
    report = await _report(db, elder_id)

    assert SIGNAL_REPEATED_QUESTION in report.codes
    signal = next(item for item in report.signals if item.code == SIGNAL_REPEATED_QUESTION)
    assert signal.evidence["intent"] == "ask_time"
    assert signal.evidence["occurrences"] == REPEAT_MIN_OCCURRENCES == 3
    assert signal.evidence["breakdown"] == {"ask_time": 3}
    assert "问时间" in signal.detail


async def test_repeated_question_two_times_does_not_fire(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """边界：2 次 → 不报。"""
    for minute in (0, 20):
        _log(
            db,
            elder_id,
            at=datetime.datetime(2026, 11, 9, 10, minute, tzinfo=SHANGHAI),
            intent="ask_time",
        )
    report = await _report(db, elder_id)
    assert SIGNAL_REPEATED_QUESTION not in report.codes


async def test_playback_intents_are_not_counted_as_repetition(
    db: AsyncSession, elder_id: uuid.UUID
) -> None:
    """连说 5 次「下一段」是正常操作，不是认知退化：播放控制类意图排除在外。"""
    for minute in (0, 10, 20, 30, 40):
        _log(
            db,
            elder_id,
            at=datetime.datetime(2026, 11, 9, 10, minute, tzinfo=SHANGHAI),
            intent="stop",
        )
    report = await _report(db, elder_id)
    assert SIGNAL_REPEATED_QUESTION not in report.codes


# ------------------------------------------------------------------ 单信号故障隔离


async def test_one_signal_failure_does_not_hide_others(
    db: AsyncSession, elder_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """某一条信号查询抛错 → 只跳过它自己，其余三条仍要出现在报告里。

    反向验证：把 `evaluate_attention_signals` 里 `except Exception: … continue` 改成
    `raise`，本用例立刻变红（整份报告抛错，而不是少一条信号继续算）。
    """
    await _mood(db, elder_id, [1, 2], last_day=datetime.date(2026, 11, 8))
    for minute in (0, 20, 40):
        _log(
            db,
            elder_id,
            at=datetime.datetime(2026, 11, 9, 10, minute, tzinfo=SHANGHAI),
            intent="ask_time",
        )
    for day in (6, 7, 8):
        _log(db, elder_id, at=datetime.datetime(2026, 11, day, 23, 30, tzinfo=SHANGHAI))

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("交互时间查询炸了")

    monkeypatch.setattr("app.services.alert_aggregation._latest_interaction", boom)

    report = await _report(db, elder_id)
    assert len(report.signals) == 3
    assert SIGNAL_ABNORMAL_SILENCE not in report.codes
    assert set(report.codes) == {
        SIGNAL_MOOD_LOW_STREAK,
        SIGNAL_NIGHT_INTERACTION,
        SIGNAL_REPEATED_QUESTION,
    }


# ------------------------------------------------------------------ 口径红线


async def test_report_carries_no_diagnosis_wording(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """四信号全亮时逐条检查文案：不许出现任何诊断口径，且只有 attention 一档。"""
    await _mood(db, elder_id, [1, 2], last_day=datetime.date(2026, 11, 8))
    _log(
        db,
        elder_id,
        at=NOW - datetime.timedelta(hours=SILENCE_THRESHOLD_HOURS),
        intent="weather_query",
    )
    for minute in (0, 20, 40):
        _log(
            db,
            elder_id,
            at=datetime.datetime(2026, 11, 9, 10, minute, tzinfo=SHANGHAI),
            intent="ask_time",
        )
    for day in (6, 7, 8):
        _log(db, elder_id, at=datetime.datetime(2026, 11, day, 23, 30, tzinfo=SHANGHAI))

    report = await _report(db, elder_id)
    assert set(report.codes) <= set(SIGNAL_CODES)
    assert report.has_attention is True
    assert report.disclaimer == DISCLAIMER
    assert "不构成任何医疗诊断" in report.disclaimer
    assert report.rule_version == RULE_VERSION
    assert report.window_days == ATTENTION_WINDOW_DAYS

    for signal in report.signals:
        assert signal.level == LEVEL_ATTENTION
        assert signal.code in SIGNAL_CODES
        for text in (signal.title, signal.detail, signal.suggestion):
            # 仍走同一条守卫：禁词出现即抛，不是靠人肉 review
            assert_attention_wording(text)
            for phrase in FORBIDDEN_WORDING:
                assert phrase not in text


def test_wording_guard_rejects_diagnosis_phrases() -> None:
    """守卫函数本身：典型诊断句必须被拒，普通「建议关注」句放行。"""
    for bad in ("您这是高血压，需要吃降压药", "你这是抑郁症早期", "建议确诊后再说"):
        with pytest.raises(AttentionWordingError) as excinfo:
            assert_attention_wording(bad)
        assert "只允许「建议关注」" in str(excinfo.value)

    good = "建议护工近期多关心情绪与睡眠，是否需要就医由医生判断。"
    assert assert_attention_wording(good) == good


def test_wording_guard_rejects_non_text() -> None:
    """非字符串入参直接报错，避免把 None 当文案发出去。"""
    with pytest.raises(AlertAggregationError):
        assert_attention_wording(None)  # type: ignore[arg-type]


async def test_evaluate_rejects_non_uuid_elder() -> None:
    """`elder_id` 必须是 UUID：字符串或 None 一律拒绝（身份只从服务端注入）。"""
    with pytest.raises(AlertAggregationError):
        await evaluate_attention_signals(None, elder_id="not-a-uuid", now=NOW)  # type: ignore[arg-type]


async def test_quiet_elder_gets_empty_report(db: AsyncSession, elder_id: uuid.UUID) -> None:
    """没有任何数据 → 四信号全不报，但仍返回完整报告结构（前端不用担心字段缺失）。"""
    report = await _report(db, elder_id)
    assert report.signals == ()
    assert report.codes == ()
    assert report.has_attention is False
    assert report.disclaimer == DISCLAIMER
