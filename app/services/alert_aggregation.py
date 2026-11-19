"""聚合预警信号：只出「建议关注」，不出诊断（PRD:374 FR-HLT-10）。

分层位置：`api → services → models`（AGENTS §2.1）。本模块**只读**：情绪曲线读
`health_records`，交互痕迹读 `decision_logs` / `conversation_threads`。范围过滤
（`_require_elder_scope`）留在 API 层，与 `medication_service` 同口径——服务层只认
`elder_id`，不做身份解析，也不决定谁能看。

## 四信号与阈值

| 信号码 | 判据 | 阈值来源 |
| --- | --- | --- |
| `mood_low_streak` | `mood_score` 尾部连续 ≤2 分 | PRD:374「连续 ≥2 次 ≤2」 |
| `abnormal_silence` | 距最近一次交互 ≥48h | PRD:374「异常沉默 ≥48h」 |
| `night_frequent_interaction` | 近 7 天 23:00–05:00 交互 ≥3 次 | PRD 只写「频繁」，次数为自裁 |
| `repeated_same_question` | 近 24h 同一 `intent` ≥3 次 | PRD 只写「重复」，口径为自裁 |

阈值全部落在模块常量里（配置项 `app/core/config.py` 不在本卡写作用域），改口径只改
一处；每条信号独立求值、独立成败——一条查崩了不影响另外三条出结论。

## 红线（AGENTS §4.4）

四条信号只输出「建议关注」+ 一句建议动作，**不得出现任何诊断结论**。生成时每条文本
都过 `assert_attention_wording()`：命中「您这是 / 您患有 / 确诊 / 诊断为 / 得了…」
一类断言型措辞就抛 `AttentionWordingError`，把口径钉在代码里，而不是靠 review 记得。

## 两处口径偏差（已登记，见回报）

1. 「重复问同一问题」没有原句可读：`decision_logs` 不存 `input_text`（TD §3.5 明确
   不在范围内），只能按 `intent` 指纹聚类。同一 `intent` 反复出现**不等于**同一句话，
   但它是库内唯一可聚合的结构化痕迹；播放控制类 intent（换一段 / 音量）属正常操作，
   已排除在指纹之外。
2. 「异常沉默」的交互时刻取 `max(decision_logs.created_at, conversation_threads.started_at)`：
   前者是每次路由决策，后者是一次会话开始，任一有记录即视为有交互。**两者都没有记录
   时不报沉默**——「从没接入过」不是「突然不说话」，报出来只是噪音。

## 合规

本模块不写库、不发通知、不建工单：信号交给 console 展示，由护工/医生决定下一步；
派单与告警仍走既有 `notification_service` / `work_orders` 链路，不在这里开第二条。
"""

from __future__ import annotations

import datetime
import logging
import uuid
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConversationThread, DecisionLog, HealthRecord

logger = logging.getLogger(__name__)

SHANGHAI: Final[zoneinfo.ZoneInfo] = zoneinfo.ZoneInfo("Asia/Shanghai")
"""展示用本地时区（存储一律 UTC，AGENTS §8「时间」）。"""

RULE_VERSION: Final[str] = "fr-hlt-10-v1"
"""信号口径版本：阈值一改就得换版本号，让前端/日报能区分「哪一版规则报的」。"""

# ------------------------------------------------------------------ 信号码
SIGNAL_MOOD_LOW_STREAK: Final[str] = "mood_low_streak"
SIGNAL_ABNORMAL_SILENCE: Final[str] = "abnormal_silence"
SIGNAL_NIGHT_INTERACTION: Final[str] = "night_frequent_interaction"
SIGNAL_REPEATED_QUESTION: Final[str] = "repeated_same_question"
SIGNAL_CODES: Final[tuple[str, ...]] = (
    SIGNAL_MOOD_LOW_STREAK,
    SIGNAL_ABNORMAL_SILENCE,
    SIGNAL_NIGHT_INTERACTION,
    SIGNAL_REPEATED_QUESTION,
)
"""四信号码闭集（PRD:374 四项，一一对应，不多不少）。"""

LEVEL_ATTENTION: Final[str] = "attention"
"""唯一的等级：「建议关注」。这里刻意没有 info/warning/danger 分级——
分级一出现，读的人就会把它当病情严重程度（那是诊断），而 PRD 只要人去看一眼。"""

# ------------------------------------------------------------------ 阈值
MOOD_LOW_MAX: Final[int] = 2
"""≤2 分算低位。与 `app/services/health_service.py` 的 `MOOD_LOW_MAX` 同口径。"""

MOOD_LOW_STREAK_REQUIRED: Final[int] = 2
"""PRD:374「连续 ≥2 次」——恰好 2 次就命中，不需要更多。"""

MOOD_LOOKBACK_DAYS: Final[int] = 14
MOOD_MAX_SAMPLES: Final[int] = 60
"""回看 14 天、最多 60 条，足以算尾部连续段，又不至于把一年记录全捞进内存。"""

SILENCE_THRESHOLD_HOURS: Final[float] = 48.0
"""PRD:374「异常沉默 ≥48h」：恰好 48 小时即命中（边界含等号）。"""

SILENCE_LOOKBACK_DAYS: Final[int] = 30
"""只回看 30 天内的交互：更早的记录说明不了「最近是否沉默」。"""

NIGHT_START_HOUR: Final[int] = 23
NIGHT_END_HOUR: Final[int] = 5
"""深夜窗口 = 本地 23:00–次日 05:00（左闭右开）。"""

NIGHT_WINDOW_DAYS: Final[int] = 7
NIGHT_MIN_OCCURRENCES: Final[int] = 3
NIGHT_MAX_SAMPLES: Final[int] = 2000
"""PRD 只写「频繁」：7 天内 ≥3 次即算频繁（自裁，写在这里便于一处调整）。"""

REPEAT_WINDOW_HOURS: Final[int] = 24
REPEAT_MIN_OCCURRENCES: Final[int] = 3
REPEAT_MAX_SAMPLES: Final[int] = 2000
"""PRD 只写「重复问同一问题」：24 小时内同一 intent ≥3 次（自裁）。"""

REPEAT_EXCLUDED_INTENTS: Final[frozenset[str]] = frozenset(
    {"play_next", "stop", "volume_up", "volume_down", "repeat", "farewell"}
)
"""播放控制与告别类 intent：连点音量、跳过戏曲是正常操作，重复出现不构成信号。"""

INTENT_LABELS: Final[dict[str, str]] = {
    "medication_take": "用药提醒",
    "ask_time": "问时间",
    "weather_query": "问天气",
    "play_yueju": "越剧点播",
    "play_jingju": "京剧点播",
    "alert": "求助",
}
"""`decision_logs.intent` 的中文标签；表里没有的取值直接回显原值（不猜）。"""

ATTENTION_WINDOW_DAYS: Final[int] = MOOD_LOOKBACK_DAYS
"""报告上标注的最长回看窗口（情绪信号的回看天数）。"""

DISCLAIMER: Final[str] = (
    "以上仅为「建议关注」提示，用于提醒护工或医生主动关心；"
    "不构成任何医疗诊断或结论，是否需要就医由医生判断。"
)

FORBIDDEN_WORDING: Final[tuple[str, ...]] = (
    "您这是",
    "你这是",
    "您患有",
    "你患有",
    "您得了",
    "你得了",
    "确诊",
    "诊断为",
    "是抑郁症",
    "是高血压",
    "是糖尿病",
    "疑似",
)
"""断言型/诊断型措辞黑名单（AGENTS §4.4 红线）。命中即抛错，不许写进输出。"""


class AlertAggregationError(Exception):
    """聚合预警服务错误基类（与 `medication_service` 等既有服务同口径）。"""


class AttentionWordingError(AlertAggregationError, ValueError):
    """输出文本出现诊断口径。这是红线违规，不是「措辞不好」——必须抛错。"""


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True, slots=True)
class AttentionSignal:
    """一条「建议关注」信号：说清「看到了什么」+「建议做什么」。

    `evidence` 只放结构化事实（次数、时长、分值），便于前端渲染与排查，**不放**任何
    推论性结论——推论就是诊断的入口。
    """

    code: str
    title: str
    detail: str
    suggestion: str
    level: str = LEVEL_ATTENTION
    evidence: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime.datetime | None = None
    """信号指向的时刻（UTC）：沉默=最近一次交互、情绪=最近一次记录，其余为 None。"""


@dataclass(frozen=True, slots=True)
class AttentionReport:
    """某长者一次聚合评估的结果（可能是空列表：没有信号也是一种结论）。"""

    elder_id: uuid.UUID
    generated_at: datetime.datetime
    window_days: int
    rule_version: str
    signals: tuple[AttentionSignal, ...]
    disclaimer: str = DISCLAIMER

    @property
    def has_attention(self) -> bool:
        return bool(self.signals)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(signal.code for signal in self.signals)


# ------------------------------------------------------------------ 措辞闸门


def assert_attention_wording(text: str, *, field_name: str = "text") -> str:
    """诊断口径闸门：命中黑名单就抛 `AttentionWordingError`，否则原样返回。

    放在生成路径上（而不是测试里）是有意的：测试只保护已知用例，闸门保护所有未来
    新增的信号——谁再写「您这是高血压」，调用就 500，而不是把一句话送到护工眼前。
    """
    if not isinstance(text, str):
        raise AlertAggregationError(f"{field_name} 必须是字符串，收到 {type(text).__name__}")
    for phrase in FORBIDDEN_WORDING:
        if phrase in text:
            raise AttentionWordingError(
                f"{field_name} 含诊断口径「{phrase}」：FR-HLT-10 只允许「建议关注」（AGENTS §4.4）"
            )
    return text


def _signal(
    *,
    code: str,
    title: str,
    detail: str,
    suggestion: str,
    evidence: dict[str, Any],
    observed_at: datetime.datetime | None = None,
) -> AttentionSignal:
    """构造信号，先过措辞闸门（每条文本逐条校验，不放过任何一条）。"""
    return AttentionSignal(
        code=code,
        title=assert_attention_wording(title, field_name="title"),
        detail=assert_attention_wording(detail, field_name="detail"),
        suggestion=assert_attention_wording(suggestion, field_name="suggestion"),
        evidence=evidence,
        observed_at=observed_at,
    )


# ------------------------------------------------------------------ 内部工具


def _now(now: datetime.datetime | None) -> datetime.datetime:
    """评估基准时刻（UTC）。显式注入便于用例钉死边界，生产走系统时钟。"""
    if now is None:
        return datetime.datetime.now(datetime.UTC)
    if not isinstance(now, datetime.datetime):
        raise AlertAggregationError(f"now 必须是 datetime，收到 {type(now).__name__}")
    if now.tzinfo is None:
        raise AlertAggregationError("now 必须带时区（天真时间算不出 48h 差）")
    return now.astimezone(datetime.UTC)


def _local(value: datetime.datetime) -> datetime.datetime:
    """UTC → Asia/Shanghai（展示用；比较与存储仍走 UTC）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(SHANGHAI)


def _local_text(value: datetime.datetime) -> str:
    return _local(value).strftime("%Y-%m-%d %H:%M")


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


async def _latest_interaction(
    session: AsyncSession, elder_id: uuid.UUID, *, since: datetime.datetime
) -> datetime.datetime | None:
    """最近一次交互时刻（UTC）：路由决策与会话开始取较晚者；都没有则 None。"""
    last_decision = await session.scalar(
        select(func.max(DecisionLog.created_at)).where(
            DecisionLog.elder_id == str(elder_id),
            DecisionLog.created_at >= since,
        )
    )
    last_thread = await session.scalar(
        select(func.max(ConversationThread.started_at)).where(
            ConversationThread.elder_id == elder_id,
            ConversationThread.started_at >= since,
        )
    )
    candidates = [_as_utc(value) for value in (last_decision, last_thread) if value is not None]
    return max(candidates) if candidates else None


# ------------------------------------------------------------------ 四信号


async def _mood_low_streak_signal(
    session: AsyncSession, elder_id: uuid.UUID, *, now: datetime.datetime
) -> AttentionSignal | None:
    """① `mood_score` 尾部连续 ≤2 分，连续 ≥2 次即命中（PRD:374）。"""
    since_date = _local(now).date() - datetime.timedelta(days=MOOD_LOOKBACK_DAYS)
    rows = (
        await session.execute(
            select(HealthRecord.record_date, HealthRecord.mood_score)
            .where(
                HealthRecord.elder_id == elder_id,
                HealthRecord.superseded_by.is_(None),
                HealthRecord.mood_score.is_not(None),
                HealthRecord.record_date >= since_date,
            )
            .order_by(HealthRecord.record_date.asc(), HealthRecord.revision.asc())
            .limit(MOOD_MAX_SAMPLES)
        )
    ).all()
    if not rows:
        return None

    # 从最新一条往前数：一旦遇到 > MOOD_LOW_MAX 就停——中间插了一次好心情，
    # 就不该把更早的低位也串进来（「连续」是紧邻的，不跳号）。
    streak: list[tuple[datetime.date, int]] = []
    for record_date, score in reversed(list(rows)):
        if score is None or score > MOOD_LOW_MAX:
            break
        streak.append((record_date, int(score)))
    if len(streak) < MOOD_LOW_STREAK_REQUIRED:
        return None

    streak.reverse()
    recent = streak[-6:]
    scored = "、".join(f"{day.isoformat()} {score} 分" for day, score in recent)
    last_date = streak[-1][0]
    return _signal(
        code=SIGNAL_MOOD_LOW_STREAK,
        title="情绪评分连续偏低",
        detail=(
            f"最近 {len(streak)} 次情绪自评连续不高于 {MOOD_LOW_MAX} 分（{scored}），"
            f"最近一次记录于 {last_date.isoformat()}。"
        ),
        suggestion="建议护工或医生近期多问候、关心情绪与睡眠；是否需要就医由医生判断。",
        evidence={
            "streak": len(streak),
            "required": MOOD_LOW_STREAK_REQUIRED,
            "threshold": MOOD_LOW_MAX,
            "scores": [score for _day, score in streak],
            "dates": [day.isoformat() for day, _score in streak],
        },
        observed_at=_as_utc(now.replace(hour=0, minute=0, second=0, microsecond=0))
        if last_date == _local(now).date()
        else None,
    )


async def _abnormal_silence_signal(
    session: AsyncSession, elder_id: uuid.UUID, *, now: datetime.datetime
) -> AttentionSignal | None:
    """② 距最近一次交互 ≥48h（PRD:374）；边界含等号。"""
    since = now - datetime.timedelta(days=SILENCE_LOOKBACK_DAYS)
    last_at = await _latest_interaction(session, elder_id, since=since)
    if last_at is None:
        # 回看窗口内一次交互都没有：可能是从没接入，也可能是设备停了——
        # 这不是「沉默」信号能表达的结论，交给设备告警，这里如实不报。
        return None
    elapsed = now - last_at
    hours = elapsed.total_seconds() / 3600.0
    if hours < SILENCE_THRESHOLD_HOURS:
        return None
    return _signal(
        code=SIGNAL_ABNORMAL_SILENCE,
        title="长时间没有互动",
        detail=(
            f"最近一次交互停在 {_local_text(last_at)}，至今已 {hours:.1f} 小时没有新的互动"
            f"（阈值 {SILENCE_THRESHOLD_HOURS:.0f} 小时）。"
        ),
        suggestion="建议护工主动上门或电话问候，确认生活与身体状态；本提示不代替人工判断。",
        evidence={
            "last_interaction_at": last_at.isoformat(),
            "silent_hours": round(hours, 1),
            "threshold_hours": SILENCE_THRESHOLD_HOURS,
        },
        observed_at=last_at,
    )


async def _night_interaction_signal(
    session: AsyncSession, elder_id: uuid.UUID, *, now: datetime.datetime
) -> AttentionSignal | None:
    """③ 近 7 天深夜（本地 23:00–05:00）交互 ≥3 次。"""
    since = now - datetime.timedelta(days=NIGHT_WINDOW_DAYS)
    stamps = list(
        (
            await session.scalars(
                select(DecisionLog.created_at)
                .where(
                    DecisionLog.elder_id == str(elder_id),
                    DecisionLog.created_at >= since,
                )
                .order_by(DecisionLog.created_at.desc())
                .limit(NIGHT_MAX_SAMPLES)
            )
        ).all()
    )
    if not stamps:
        return None
    night_stamps = [_as_utc(stamp) for stamp in stamps if _is_night_hour(_local(stamp).hour)]
    if len(night_stamps) < NIGHT_MIN_OCCURRENCES:
        return None
    return _signal(
        code=SIGNAL_NIGHT_INTERACTION,
        title="深夜频繁交互",
        detail=(
            f"近 {NIGHT_WINDOW_DAYS} 天有 {len(night_stamps)} 次交互发生在本地 "
            f"{NIGHT_START_HOUR:02d}:00–{NIGHT_END_HOUR:02d}:00 之间"
            f"（阈值 {NIGHT_MIN_OCCURRENCES} 次）。"
        ),
        suggestion="建议护工了解深夜醒着的原因（疼痛、夜尿、环境噪声等），必要时请医生评估。",
        evidence={
            "occurrences": len(night_stamps),
            "required": NIGHT_MIN_OCCURRENCES,
            "window_days": NIGHT_WINDOW_DAYS,
            "night_window": f"{NIGHT_START_HOUR:02d}:00-{NIGHT_END_HOUR:02d}:00",
            "occurred_at": [stamp.isoformat() for stamp in night_stamps[:10]],
        },
        observed_at=night_stamps[0],
    )


def _is_night_hour(hour: int) -> bool:
    """本地小时是否落在深夜窗口（23 点起、5 点前）。"""
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


async def _repeated_question_signal(
    session: AsyncSession, elder_id: uuid.UUID, *, now: datetime.datetime
) -> AttentionSignal | None:
    """④ 近 24h 同一 `intent` ≥3 次（认知退化信号的结构化代理，见模块头偏离 1）。"""
    since = now - datetime.timedelta(hours=REPEAT_WINDOW_HOURS)
    rows = (
        await session.execute(
            select(DecisionLog.intent, func.count())
            .where(
                DecisionLog.elder_id == str(elder_id),
                DecisionLog.created_at >= since,
                DecisionLog.intent.is_not(None),
            )
            .group_by(DecisionLog.intent)
            .order_by(func.count().desc())
            .limit(REPEAT_MAX_SAMPLES)
        )
    ).all()
    counts = {
        str(intent): int(total)
        for intent, total in rows
        if intent and str(intent) not in REPEAT_EXCLUDED_INTENTS
    }
    if not counts:
        return None
    top_intent, top_count = max(counts.items(), key=lambda item: item[1])
    if top_count < REPEAT_MIN_OCCURRENCES:
        return None
    label = INTENT_LABELS.get(top_intent, top_intent)
    return _signal(
        code=SIGNAL_REPEATED_QUESTION,
        title="同一类问题反复出现",
        detail=(
            f"近 {REPEAT_WINDOW_HOURS} 小时「{label}」类请求出现 {top_count} 次"
            f"（阈值 {REPEAT_MIN_OCCURRENCES} 次，按意图指纹聚合）。"
        ),
        suggestion=(
            "建议护工当面聊一聊：是没听清、想反复确认，还是有别的需求没被满足；必要时请医生评估。"
        ),
        evidence={
            "intent": top_intent,
            "label": label,
            "occurrences": top_count,
            "required": REPEAT_MIN_OCCURRENCES,
            "window_hours": REPEAT_WINDOW_HOURS,
            "breakdown": counts,
        },
    )


_SIGNAL_EVALUATORS = (
    (_mood_low_streak_signal, SIGNAL_MOOD_LOW_STREAK),
    (_abnormal_silence_signal, SIGNAL_ABNORMAL_SILENCE),
    (_night_interaction_signal, SIGNAL_NIGHT_INTERACTION),
    (_repeated_question_signal, SIGNAL_REPEATED_QUESTION),
)
"""四信号与信号码：顺序即报告里的展示顺序（情绪→沉默→深夜→重复）。"""


async def evaluate_attention_signals(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> AttentionReport:
    """评估某长者的四条聚合信号，返回「建议关注」报告（可能为空）。

    每条信号独立求值、独立兜底：某一条查询抛错只记 warning 并跳过，**不**让整个
    报告 500——预警页面拿不到全部四条，也好过一条都拿不到。
    """
    if not isinstance(elder_id, uuid.UUID):
        raise AlertAggregationError(f"elder_id 必须是 UUID，收到 {type(elder_id).__name__}")
    anchor = _now(now)
    signals: list[AttentionSignal] = []
    for evaluator, code in _SIGNAL_EVALUATORS:
        try:
            signal = await evaluator(session, elder_id, now=anchor)
        except AlertAggregationError:
            raise
        except Exception:  # 单信号失败不得拖垮整份报告
            logger.warning(
                "attention_signal_failed",
                extra={"signal": code, "elder_id": str(elder_id)},
                exc_info=True,
            )
            continue
        if signal is not None:
            signals.append(signal)

    logger.info(
        "attention_signals_evaluated",
        extra={
            "elder_id": str(elder_id),
            "signals": [signal.code for signal in signals],
            "rule_version": RULE_VERSION,
        },
    )
    return AttentionReport(
        elder_id=elder_id,
        generated_at=anchor,
        window_days=ATTENTION_WINDOW_DAYS,
        rule_version=RULE_VERSION,
        signals=tuple(signals),
    )


__all__ = [
    "ATTENTION_WINDOW_DAYS",
    "DISCLAIMER",
    "FORBIDDEN_WORDING",
    "LEVEL_ATTENTION",
    "MOOD_LOW_MAX",
    "MOOD_LOW_STREAK_REQUIRED",
    "NIGHT_MIN_OCCURRENCES",
    "REPEAT_MIN_OCCURRENCES",
    "REPEAT_WINDOW_HOURS",
    "RULE_VERSION",
    "SIGNAL_ABNORMAL_SILENCE",
    "SIGNAL_CODES",
    "SIGNAL_MOOD_LOW_STREAK",
    "SIGNAL_NIGHT_INTERACTION",
    "SIGNAL_REPEATED_QUESTION",
    "SILENCE_THRESHOLD_HOURS",
    "AlertAggregationError",
    "AttentionReport",
    "AttentionSignal",
    "AttentionWordingError",
    "assert_attention_wording",
    "evaluate_attention_signals",
]
