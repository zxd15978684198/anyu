"""健康采集子图（PRD §4.6 / FR-HLT-01/03/07，迭代 8 W2）。

## 为什么是「纯状态机 + interrupt」

采集要能暂停、落盘、重启继续（FR-HLT-03）：每问用 `interrupt()` 挂起，
调用方把长者回答用 `Command(resume=...)` 喂回来。这样「问 → 等 → 收」的
三段时间全在 checkpointer 里，WebSocket 断了也能从同一 `thread_id` 接着问，
不会从头再问一遍已经答过的项（验收项：kill 后 get_state 不重问已答）。

**子图只算状态，不碰数据库**：分级调 `health_service.grade_vitals()`，
落库由调用方在终态调 `health_service.persist_checkup_state()`。理由是本图用
同步 `PostgresSaver` 之外还要能在测试里换 `MemorySaver` 单跑，节点里一旦
写库就把两条路径的时序绑死，也违反「graph 节点只做状态转换」的可测性。

## 五问顺序（固定，不可由模型决定）

收缩压 → 舒张压 → 心率 → 睡眠时长 → 情绪评分。每问等一次回答；
无效答（非数字/越界/收缩压≤舒张压）**重问一次**，仍无效就记进 `missing`
继续下一问，绝不阻断整场采集（FR-HLT-01）。

### 与 PRD 第 5 问的口径差异（HLT-06，按 AGENTS 前置铁律 7 以实现为准）

PRD §4.6 的五问是「收缩压 / 舒张压 / 睡眠 / 情绪 / 主诉」，代码这一套是
「收缩压 / 舒张压 / 心率 / 睡眠 / 情绪」：`heart_rate` 是基座
`health_records.heart_rate_bpm` 列（迭代 8 任务书的第 3 问），PRD 五问里没有它；
PRD 的第 5 问（主诉 `complaint`）由**独立链路**承载 —— 长者自述不舒服时由
`start_complaint_checkup()` 带着 `complaint` 原文起同一张图，终态经
`persist_checkup_state()` 落 `health_records.complaint` 列并按「非急症主诉判黄」。
两套问法共用同一个分级引擎与同一条落库链路，所以是**同一能力的一种形态**，
不新增第二个采集子图；`QUESTION_ORDER` 仍固定五项（改成六项会破坏
`tests/test_health_graph.py` 与调度侧的超时/缺失口径，且 `tests/` 不在本波次
写作用域内），差异已登记进任务报告。

## 黄/红走 HITL

`grade` 节点判出 yellow/red 时进入 `review` 节点 `interrupt`，等人工复核裁决
再落终态；green 直接 `finish`。
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, TypedDict
from zoneinfo import ZoneInfo

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.observability import graph_span
from app.models.health import HealthCheckupSession
from app.services import health_service
from app.services.health_service import (
    MEASUREMENTS,
    GradeResult,
    coerce_measurement,
    grade_vitals,
    grade_with_complaint,
    today_in_shanghai,
)

logger = logging.getLogger(__name__)

#: 五个采集字段的固定提问顺序（数值部分按 PRD §4.6 的顺序：血压两分量 → 睡眠 → 情绪；
#: `heart_rate` 是基座列/任务书第 3 问，插在血压之后）。
#: PRD 的第 5 问「主诉」不走这里 —— 见模块 docstring「与 PRD 第 5 问的口径差异」。
QUESTION_ORDER: tuple[str, ...] = (
    "systolic",
    "diastolic",
    "heart_rate",
    "sleep_hours",
    "mood_score",
)

#: 每问最多问两次：第一次无效重问一次，第二次仍无效标缺失（FR-HLT-01）。
MAX_ATTEMPTS = 2

QUESTION_PROMPTS: dict[str, str] = {
    "systolic": "今天量血压了吗？收缩压（高压）是多少呀？",
    "diastolic": "那低压（舒张压）是多少呢？",
    "heart_rate": "心率是多少呀？就是心跳每分钟多少下。",
    "sleep_hours": "昨晚睡了几个小时呀？",
    "mood_score": "今天心情怎么样？从 1 到 5 打个分，1 是很差，5 是很好。",
}

RETRY_HINT: dict[str, str] = {
    "systolic": "说数字就行，比如 128。",
    "diastolic": "说数字就行，比如 78。",
    "heart_rate": "说数字就行，比如 72。",
    "sleep_hours": "说小时数就行，比如 7 或者 7.5。",
    "mood_score": "说 1 到 5 之间的一个整数。",
}


class HealthCheckupState(TypedDict, total=False):
    """子图状态（全部可选：checkpoint 恢复时字段可能还没写入）。

    字段按用途分段：身份/上下文、问答进度、终态结论、复核结果。
    """

    # 身份与上下文（由调用方注入，模型不可自报 —— AGENTS §4.9）
    thread_id: str
    elder_id: str
    source: str
    record_date: str
    # N33：非采集时段的主诉原文（「我头晕」）。只做上下文留痕，不参与分级——
    # 分级只看五问的数值；主诉要不要另建复核任务由调用方决定，别在这里写库。
    complaint: str

    # 问答进度
    answers: dict[str, float | int]
    asked: list[str]
    missing: list[str]
    attempts: dict[str, int]
    current_q: str | None
    prompts: list[str]
    warnings: list[str]

    # 分级结论（grade 节点产出）
    grade: str
    reasons: list[str]
    needs_review: bool

    # 复核（review 节点）
    reviewed: bool
    review_decision: str | None

    finished: bool


def health_thread_id(elder_id: object, record_date: datetime.date | str | None = None) -> str:
    """HITL 线程键：`health:{elder_id}:{YYYY-MM-DD}`（同一长者同一天只有一条采集线程）。

    同一天重复进入会命中同一个 checkpoint —— 已答的项不会重问，
    这也让「WebSocket 断开重连」天然幂等。
    """
    if isinstance(record_date, datetime.date):
        day = record_date.isoformat()
    elif record_date:
        day = str(record_date)
    else:
        day = today_in_shanghai().isoformat()
    return f"health:{elder_id}:{day}"


def initial_state(
    elder_id: object,
    *,
    source: str = "voice_scheduled",
    record_date: datetime.date | str | None = None,
    thread_id: str | None = None,
) -> HealthCheckupState:
    """构造首轮状态；`thread_id` 不传则按规则生成（调用方也可以显式指定）。"""
    day = (
        record_date
        if isinstance(record_date, datetime.date)
        else (datetime.date.fromisoformat(record_date) if record_date else today_in_shanghai())
    )
    return HealthCheckupState(
        thread_id=thread_id or health_thread_id(elder_id, day),
        elder_id=str(elder_id),
        source=source,
        record_date=day.isoformat(),
        answers={},
        asked=[],
        missing=[],
        attempts={},
        current_q=None,
        prompts=[],
        warnings=[],
        reviewed=False,
        review_decision=None,
        finished=False,
    )


def _next_question(state: Mapping[str, Any]) -> str | None:
    """下一问 = 顺序表里第一个「既没答对、也没被标缺失」的字段。"""
    answers = state.get("answers") or {}
    missing = set(state.get("missing") or ())
    for key in QUESTION_ORDER:
        if key not in answers and key not in missing:
            return key
    return None


def parse_answer(key: str, reply: object) -> float | int | None:
    """解析长者回答；非法返回 None 交给重问逻辑（异常只用于「值越界」这类可归因错误）。

    「不知道」「没测」这类口头回答解析失败 → None，不当作 0，
    否则会把没测的人判成低血压黄档。
    """
    try:
        return coerce_measurement(key, reply)
    except Exception:
        return None


def _retry_prompt(key: str) -> str:
    return f"我没听清，您再说一遍{MEASUREMENTS[key].label}好吗？{RETRY_HINT[key]}"


def _ask_prompt(key: str, attempt: int) -> str:
    return QUESTION_PROMPTS[key] if attempt == 0 else _retry_prompt(key)


def ask_node(state: HealthCheckupState) -> dict[str, Any]:
    """问一个问题并（可能）重问一次；每问一次都通过 `interrupt()` 等回答。

    返回值只包含发生变化的键：LangGraph 默认整键覆盖，所以 `answers` 这类
    字典必须整体回传，不能依赖隐式 merge。
    """
    answers = dict(state.get("answers") or {})
    asked = list(state.get("asked") or [])
    missing = list(state.get("missing") or [])
    attempts = dict(state.get("attempts") or {})
    prompts = list(state.get("prompts") or [])
    warnings = list(state.get("warnings") or [])

    key = _next_question(state)
    if key is None:  # 理论上不会发生：路由已在调用前判过
        return {"current_q": None}

    for attempt in range(MAX_ATTEMPTS):
        prompt = _ask_prompt(key, attempt)
        prompts.append(prompt)
        asked.append(key)
        attempts[key] = attempt + 1
        reply = interrupt(
            {
                "type": "question",
                "key": key,
                "label": MEASUREMENTS[key].label,
                "prompt": prompt,
                "attempt": attempt + 1,
                "max_attempts": MAX_ATTEMPTS,
            }
        )
        value = parse_answer(key, reply)
        if value is not None:
            answers[key] = value
            break
        warnings.append(f"{key}:invalid_answer:{attempt + 1}")
        if attempt + 1 < MAX_ATTEMPTS:
            continue
        missing.append(key)

    # 舒张压与收缩压的交叉校验：收缩压≤舒张压时，若两值都已有则作废低压并重问一次。
    systolic = answers.get("systolic")
    diastolic = answers.get("diastolic")
    if (
        key == "diastolic"
        and systolic is not None
        and diastolic is not None
        and float(diastolic) >= float(systolic)
    ):
        answers.pop("diastolic", None)
        missing.append("diastolic")
        warnings.append("diastolic:not_below_systolic")

    return {
        "answers": answers,
        "asked": asked,
        "missing": missing,
        "attempts": attempts,
        "prompts": prompts,
        "warnings": warnings,
        "current_q": key,
    }


def grade_node(state: HealthCheckupState) -> dict[str, Any]:
    """调服务层分级引擎；阈值与原因码全部来自 `health_service`（不在本文件重复）。

    HLT-06：这一场如果是主诉起的（`state["complaint"]` 有真实内容），等级不低于黄，
    与落库侧 `health_service.persist_checkup_state()` 同一口径 —— 否则五项全绿的
    主诉场会跳过 HITL 直接 finish，落库时又被抬成黄档，出现「黄档但没人复核」。
    """
    answers = state.get("answers") or {}
    result: GradeResult = grade_with_complaint(
        grade_vitals(
            systolic=answers.get("systolic"),
            diastolic=answers.get("diastolic"),
            heart_rate=answers.get("heart_rate"),
            sleep_hours=answers.get("sleep_hours"),
            mood_score=answers.get("mood_score"),
        ),
        state.get("complaint"),
    )
    return {
        "grade": result.grade,
        "reasons": list(result.reasons),
        "needs_review": result.needs_review,
        "current_q": None,
    }


def review_node(state: HealthCheckupState) -> dict[str, Any]:
    """黄/红档人工复核闸门（HITL）：`interrupt` 等复核裁决再继续。

    裁决值只影响 `review_decision`（落进终态供通知 payload 记录），
    `grade` 本身不因复核而改——复核改写分级会让审计链对不上。
    """
    decision = interrupt(
        {
            "type": "review",
            "thread_id": state.get("thread_id"),
            "elder_id": state.get("elder_id"),
            "grade": state.get("grade"),
            "reasons": list(state.get("reasons") or ()),
            "prompt": "本次采集判定需要人工复核，请确认已联系长者或家属。",
        }
    )
    payload: Mapping[str, Any] = decision if isinstance(decision, Mapping) else {}
    return {
        "reviewed": True,
        "review_decision": str(payload.get("decision") or "acknowledged"),
    }


def finish_node(state: HealthCheckupState) -> dict[str, Any]:
    """终态：调用方据此落库（`persist_checkup_state`）并按 grade 决定后续话术。"""
    return {"finished": True, "current_q": None}


def _route_after_ask(state: HealthCheckupState) -> str:
    return "ask" if _next_question(state) is not None else "grade"


def _route_after_grade(state: HealthCheckupState) -> str:
    return "review" if state.get("needs_review") else "finish"


@contextlib.contextmanager
def _node_span(thread_id: str | None) -> Iterator[None]:
    """节点的 `graph.health_checkup` span（S5）。

    `interrupt()` 抛的 `GraphInterrupt` 是 HITL 的**暂停信号**（等长者回答、等复核裁决），
    属正常流程；若让它穿过 `with`，`_SpanScope` 会把 span 标成 ERROR，trace 上看起来
    每次采集都失败了。这里接住它、先按「正常结束」收口 span 再原样抛出，交给 LangGraph
    的循环去暂停；真正的异常仍照常标 ERROR。
    """
    scope = graph_span("health_checkup", thread_id=thread_id)
    scope.__enter__()
    try:
        yield
    except GraphInterrupt:
        scope.__exit__(None, None, None)
        raise
    except BaseException as exc:
        scope.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        scope.__exit__(None, None, None)


def build_health_graph(
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> Any:
    """装配并编译子图；传 `MemorySaver()` 可在测试里单跑，不落 PG。

    返回值类型故意用 `Any`：`CompiledStateGraph` 的泛型参数在 langgraph 各小版本
    间变动过，写死会在升级时报「不兼容」而不是真正的错误。
    """

    # `StateGraph` 泛型只填状态类型：ContextT/InputT/OutputT 在 langgraph 侧已带默认值
    # （None / StateT / StateT），返回值继续保持 `Any`（理由见上一段）。
    #
    # 四个节点各包一层 `graph.health_checkup` span（S5）：节点本身是纯函数、内部没有
    # 打点，这一层是「采集走到哪一问 / 复核还是结束」的唯一观测点；`thread_id` 取状态里
    # 的服务端键（AGENTS §4.9），没注入时少一个属性，不影响采集。
    def _ask(state: HealthCheckupState) -> dict[str, Any]:
        with _node_span(state.get("thread_id")):
            return ask_node(state)

    def _grade(state: HealthCheckupState) -> dict[str, Any]:
        with _node_span(state.get("thread_id")):
            return grade_node(state)

    def _review(state: HealthCheckupState) -> dict[str, Any]:
        with _node_span(state.get("thread_id")):
            return review_node(state)

    def _finish(state: HealthCheckupState) -> dict[str, Any]:
        with _node_span(state.get("thread_id")):
            return finish_node(state)

    builder: StateGraph[HealthCheckupState] = StateGraph(HealthCheckupState)
    builder.add_node("ask", _ask)
    builder.add_node("grade", _grade)
    builder.add_node("review", _review)
    builder.add_node("finish", _finish)

    builder.add_edge(START, "ask")
    builder.add_conditional_edges("ask", _route_after_ask, {"ask": "ask", "grade": "grade"})
    builder.add_conditional_edges(
        "grade", _route_after_grade, {"review": "review", "finish": "finish"}
    )
    builder.add_edge("review", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


_GRAPH: Any | None = None


async def get_health_graph() -> Any:
    """进程内单例子图；checkpointer 不可用时降级成 `MemorySaver` 并打警告。

    降级不阻断采集（采集是长者日常功能，不能因为 checkpoint 库连不上就整场不可用），
    但会丢「断线后不重问」能力 —— 日志里必须留下 `health_graph_degraded`。
    """
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH

    checkpointer: BaseCheckpointSaver[str] | None
    try:
        from app.graphs.checkpointer import get_checkpointer

        checkpointer = await get_checkpointer()
    except Exception as exc:
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning(
            "health_graph_degraded",
            extra={"reason": "checkpointer_unavailable", "error": type(exc).__name__},
        )
        checkpointer = MemorySaver()

    _GRAPH = build_health_graph(checkpointer)
    return _GRAPH


def reset_health_graph() -> None:
    """清掉单例（测试用：换 checkpointer / 换事件循环后重建）。"""
    global _GRAPH
    _GRAPH = None


def pending_prompt(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """从 `ainvoke` 结果里取出当前 suspend 的提示（没有则 None，表示已到终态）。"""
    interrupts = result.get("__interrupt__") if isinstance(result, Mapping) else None
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return dict(value) if isinstance(value, Mapping) else {"value": value}


def state_outcome(state: Mapping[str, Any]) -> dict[str, Any]:
    """把终态整理成调用方落库需要的载荷（服务层 `persist_checkup_state` 直接消费）。"""
    return {
        "thread_id": state.get("thread_id"),
        "elder_id": state.get("elder_id"),
        "source": state.get("source") or "voice_scheduled",
        "record_date": state.get("record_date"),
        "answers": dict(state.get("answers") or {}),
        "missing": list(state.get("missing") or ()),
        "grade": state.get("grade"),
        "reasons": list(state.get("reasons") or ()),
        "reviewed": bool(state.get("reviewed")),
        "review_decision": state.get("review_decision"),
        # HLT-07：主诉原文必须跟着终态走到落库处，否则「头晕」只存在于会话日志里。
        "complaint": state.get("complaint"),
    }


# ---------------------------------------------------------------------------
# N33：非采集时段的情感陪伴主诉 → 健康采集入口
# ---------------------------------------------------------------------------

COMPLAINT_NOTICE: Final[str] = (
    "我记下了，先给您做一遍今天的健康采集，问几个小问题就好。要是还一直晕，建议问医生。"
)
"""主诉触发采集时的固定话术。

**不许**出现诊断口径（AGENTS §4.4）：这里只说「采集」与「建议问医生」，
不解释头晕的原因，也不给任何结论。
"""


@dataclass(frozen=True, slots=True)
class ComplaintCheckupOutcome:
    """主诉入口的返回值：`action` 决定调用方下一步做什么。"""

    action: str
    """`started`（已起采集，等回答）、`in_window_use_rest`（窗口内，走 REST 路径）
    或 `existing`（当天已有会话行，原样沿用，不重开）。"""

    elder_id: str
    complaint: str
    thread_id: str
    prompt: dict[str, Any] | None
    notice: str
    session: health_service.CheckupSessionView | None = None
    """带 `session` 起图时写回后的会话快照；纯模式（只算状态）为 None。"""

    record: health_service.HealthRecordView | None = None
    """若一句话内直接走到终态才落库，这里带落库结果；首发提问恒为 None。"""


def _clock_minutes(clock: str) -> int:
    """把 `"19:00"` 解析成当日分钟数；解析不了直接抛（配置错误要早炸）。"""
    hour_text, _, minute_text = clock.partition(":")
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid clock value {clock!r}, expect HH:MM")
    return hour * 60 + minute


def inside_collection_window(now: datetime.datetime | None = None) -> bool:
    """当前是否落在采集窗口 `[start, end)` 内（默认 19:00–20:00，按 `scheduler_timezone`）。

    注入 `now` 是为了让用例把时钟拨到窗口两侧各跑一次，而不是靠执行时刻撞运气。
    naive 时间按 UTC 解释（AGENTS §8：存储口径 UTC，只有展示层转上海）。
    """
    settings = get_settings()
    moment = now if now is not None else datetime.datetime.now(datetime.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    local = moment.astimezone(ZoneInfo(settings.scheduler_timezone))
    current = local.hour * 60 + local.minute
    return (
        _clock_minutes(settings.health_checkup_start)
        <= current
        < _clock_minutes(settings.health_checkup_end)
    )


async def start_complaint_checkup(
    *,
    elder_id: uuid.UUID | str,
    complaint: str,
    now: datetime.datetime | None = None,
    record_date: datetime.date | None = None,
    graph: Any | None = None,
    session: Any | None = None,
) -> ComplaintCheckupOutcome:
    """非采集时段的主诉（「我头晕」）→ 起一场采集；窗口内交回 REST 路径。

    ## 两种模式

    - **纯模式**（`session=None`，调度器/REST 侧与既有用例使用）：只算状态、不碰库。
      19:00–20:00 是 PRD 的固定采集窗口，那段时间的入口只有 `app/api/health.py` 的
      REST 路径（到点提醒 + 照护台代填）；从这里再起一条 `voice_active` 采集会与当日
      唯一性打架，所以窗口内**不碰图**，只把判决回给调用方
      （`action="in_window_use_rest"`），由它继续走原来的路。
    - **会话模式**（传入 `session`，`app/turn_wiring.py` 的语音主诉链路使用）：
      调用方已经确认当天**没有**开着的会话行，这里就真的起一场 —— 建会话行（
      `source="voice_active"`）→ 图跑到第一问 → 用 `_advance` 写回会话行。
      没有这一步，主诉进来的那一问只活在 checkpoint 里：下一句会被当成「当天没有
      会话」重新 `start_checkup`，用全新 `initial_state` 把 checkpoint 冲掉，长者
      刚答的答案与主诉原文一起丢（HLT-07 的「死代码」根因就在这里）。
      窗口判断交给调用方：只有「当天无会话 + 窗口内」时才开始，避免与 19:00 任务
      重复起场。

    ## 不写业务表

    本函数只建/推进**采集会话**，健康记录仍由终态落库（`persist_checkup_state`）写，
    主诉原文放在 `state["complaint"]` 里跟到那一步，最终落到 `health_records.complaint`。
    """
    text = complaint.strip()
    if not text:
        raise ValueError("complaint must not be empty")

    day = record_date if record_date is not None else today_in_shanghai()

    if session is None:
        thread_id = health_thread_id(elder_id, day)
        if inside_collection_window(now):
            return ComplaintCheckupOutcome(
                action="in_window_use_rest",
                elder_id=str(elder_id),
                complaint=text,
                thread_id=thread_id,
                prompt=None,
                notice=COMPLAINT_NOTICE,
            )
        state = initial_state(elder_id, source="voice_active", record_date=day)
        state["complaint"] = text
        active_graph = graph if graph is not None else await get_health_graph()
        result = await active_graph.ainvoke(
            state,
            {"configurable": {"thread_id": state["thread_id"]}},
        )
        return ComplaintCheckupOutcome(
            action="started",
            elder_id=str(elder_id),
            complaint=text,
            thread_id=state["thread_id"],
            prompt=pending_prompt(result),
            notice=COMPLAINT_NOTICE,
        )

    # 会话模式：先建/复用当天会话行，再让主诉原文随状态进 checkpoint。
    eid = elder_id if isinstance(elder_id, uuid.UUID) else uuid.UUID(str(elder_id))
    state = initial_state(eid, source="voice_active", record_date=day)
    state["complaint"] = text
    row, created = await health_service.open_checkup_session(
        session,
        elder_id=eid,
        record_date=day,
        thread_id=state["thread_id"],
        source="voice_active",
    )
    if not created:
        # 当天已有会话行（并发 / 已在 answering 的重复主诉）：沿用，不重开。
        logger.info(
            "health_complaint_reused_session",
            extra={"session_id": str(row.session_id), "status": row.status},
        )
        return ComplaintCheckupOutcome(
            action="existing",
            elder_id=str(eid),
            complaint=text,
            thread_id=row.thread_id,
            prompt=dict(row.prompt) if row.prompt else None,
            notice=COMPLAINT_NOTICE,
            session=health_service.checkup_session_view(row),
        )

    moment = _moment(now)
    active_graph = graph if graph is not None else await get_health_graph()
    try:
        result = await active_graph.ainvoke(state, _thread_config(row.thread_id))
    except Exception:
        # 与 `start_checkup` 同一收口：图起不来就把会话关成 ended，别留一张
        # 没有 expires_at 的 asking 行给超时扫描。
        await health_service.claim_checkup_session(
            session,
            session_id=row.session_id,
            status="ended",
            expected="asking",
            now=moment,
            close=True,
        )
        raise

    advanced = await _advance(
        session, row, _result_state(result), pending_prompt(result), now=moment
    )
    return ComplaintCheckupOutcome(
        action="started",
        elder_id=str(eid),
        complaint=text,
        thread_id=row.thread_id,
        prompt=advanced.prompt,
        notice=COMPLAINT_NOTICE,
        session=advanced.session,
        record=advanced.record,
    )


# ---------------------------------------------------------------------------
# 采集会话编排（FR-HLT-01：19:00 驱动 → 每题 60 秒 → 主动结束 → 终态落库）
# ---------------------------------------------------------------------------
#
# 为什么编排放在图模块、而不是服务层或调度器：
# 只有这里同时握有「图实例」与「会话行」两件东西。放服务层会让 services → graphs
# 反向依赖（AGENTS §2.1 禁止），放调度器则要把图实例、Command、超时推进全抄一遍。
# 本层的职责就一条：**把「一次问答推进」翻译成「一行会话 + 一次落库」**，
# 业务口径（阈值、分级、通知）全部留在 `health_service`。
#
# 调用方：`app/scheduler/health.py` 不 import 本模块，只在 19:00 / 每 20 秒通过注入的
# `app.scheduler.health.HealthCheckupDriver`（实现由 `app/main.py` 用本模块拼出）调
# `start_checkup` / `advance_due_checkups`；`app/api/health.py` 调 `submit_checkup_answer`。
#
# 投递载体：**会话行 + `GET /api/health/checkup/{elder_id}`**。pad 轮到这一题时拉一次，
# 拿到题面、第几次、截止时间。服务端不做 WS 主动推送（`app/scheduler` 不许 import
# `app/audio`，且 pad 侧目前还没接后端）——主动推送与 pad 接后端留给前端卡，不是漏做。
#
# 「一题都没答」的取舍：终态不落 `health_records`。全缺项交给 `grade_vitals` 会判黄，
# 等于凭空造一条异常记录（还会让 20:00 的漏采扫描以为「当天已采」）；如实留空，
# 由 `run_missed_scan` 报漏采才是诚实的兜底。

#: 长者主动结束采集的说法（命中即收尾，已答项照常落库）。
#: 只做子串命中：方言/口语很多，枚举不完；宁可多识别一句「不测了」也不要死缠。
END_PHRASES: Final[tuple[str, ...]] = (
    "不测了",
    "不想测",
    "不测",
    "别问了",
    "不要问了",
    "结束",
    "算了",
    "以后再说",
    "跳过",
)

#: 超时推进时喂给 `interrupt()` 的「无回答」值。
#:
#: 语义上本应是 `Command(resume=None)`（漏答就是没有回答），但仓库钉的 langgraph 版本里
#: `resume is None` 会走进一条未初始化变量的分支（`UnboundLocalError: cannot access local
#: variable 'resume_is_map'`），跑不通。空串是「没有说任何字」的等价表达：`parse_answer("")`
#: 解析失败返回 None，与 `resume=None` 在图上走的是同一条路径（判无效 → 重问 / 标缺失）。
#: 换 langgraph 版本后可以改回 None，届时删掉本常量即可。
TIMEOUT_RESUME: Final[str] = ""


def is_end_reply(reply: object) -> bool:
    """是否是「主动结束」的回答（非字符串/空串一律不算，避免把 None 当结束）。"""

    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    return any(phrase in text for phrase in END_PHRASES)


@dataclass(frozen=True, slots=True)
class CheckupTurnOutcome:
    """一次推进的结果：会话现况 + 下一问（None 表示已到终态）+ 本次落的库行。"""

    session: health_service.CheckupSessionView
    prompt: dict[str, Any] | None
    record: health_service.HealthRecordView | None = None
    created: bool = False
    """是否本次新建了会话（调度器据此计数，重跑不算）。"""


def _moment(now: datetime.datetime | None) -> datetime.datetime:
    """会话时刻统一 UTC；naive 按 UTC 解释（与 `inside_collection_window` 同口径）。"""
    moment = now if now is not None else datetime.datetime.now(datetime.UTC)
    return moment.replace(tzinfo=datetime.UTC) if moment.tzinfo is None else moment


def _thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _result_state(result: Mapping[str, Any]) -> dict[str, Any]:
    """`ainvoke` 的返回值去掉 `__interrupt__` 后就是最新状态（LangGraph 语义）。"""
    return {key: value for key, value in result.items() if key != "__interrupt__"}


async def _persist_if_any(
    session: AsyncSession,
    row: HealthCheckupSession,
    state: Mapping[str, Any],
) -> health_service.HealthRecordView | None:
    """把已答项落库并分级；一个数都没采到就不落（留给 20:00 漏采扫描如实报缺失）。

    为什么「零答案不落库」：`grade_vitals` 对全缺项会给 yellow，于是每场「长者没接电话」
    的采集都会凭空生成一条黄色记录与一条复核任务，20:00 的 `run_missed_scan` 也会因为
    「当天已有记录」而不再报漏采——那等于把漏采洗成了已采集。宁可当场不落，
    让缺失如实暴露在下游。
    """
    answers = dict(state.get("answers") or {})
    if not answers:
        logger.info(
            "health_checkup_persist_skipped",
            extra={"session_id": str(row.session_id), "reason": "no_answers"},
        )
        return None
    payload: dict[str, Any] = dict(state_outcome(state))
    payload["answers"] = answers
    payload.setdefault("source", row.source)
    payload["record_date"] = row.record_date.isoformat()
    payload["thread_id"] = row.thread_id
    return await health_service.persist_checkup_state(
        session,
        elder_id=row.elder_id,
        state=payload,
    )


async def _advance(
    session: AsyncSession,
    row: HealthCheckupSession,
    state: Mapping[str, Any],
    prompt: Mapping[str, Any] | None,
    *,
    now: datetime.datetime,
    timeouts: int | None = None,
) -> CheckupTurnOutcome:
    """把一次 resume 的结果写成会话行：下一问 / 等复核 / 终态三种走向。"""
    answers = dict(state.get("answers") or {})
    missing = list(state.get("missing") or ())

    if prompt is None:
        claimed = await health_service.claim_checkup_session(
            session,
            session_id=row.session_id,
            status="completed",
            expected="asking",
            now=now,
            close=True,
        )
        record = await _persist_if_any(session, row, state) if claimed else None
        if claimed:
            # 终态也要把最后一问的答案写回会话行：`claim_checkup_session` 只动状态与截止时间，
            # 不收尾就会被 pad 看到「completed 但少最后一问」的残缺快照。
            await health_service.update_checkup_prompt(
                session,
                row,
                prompt=None,
                current_key=None,
                attempt_no=row.attempt_no,
                answers=answers,
                missing=missing,
                expires_at=None,
            )
        await session.refresh(row)
        return CheckupTurnOutcome(
            session=health_service.checkup_session_view(row), prompt=None, record=record
        )

    if str(prompt.get("type") or "") == "review":
        if not answers:
            # 一个数都没采到：既不能落库（全缺项会被 `grade_vitals` 判黄，等于凭空造一条
            # 异常记录，20:00 的漏采扫描也会因为「当天已有记录」而不再报缺失），
            # 也不该挂复核闸门（复核台没有记录可复核，只会空转）。
            # 直接把会话关成 completed，缺失如实留给 `run_missed_scan` 报漏采。
            claimed = await health_service.claim_checkup_session(
                session,
                session_id=row.session_id,
                status="completed",
                expected="asking",
                now=now,
                close=True,
            )
            if claimed:
                await health_service.update_checkup_prompt(
                    session,
                    row,
                    prompt=None,
                    current_key=None,
                    attempt_no=row.attempt_no,
                    answers=answers,
                    missing=missing,
                    expires_at=None,
                )
                logger.info(
                    "health_checkup_closed_without_data",
                    extra={"session_id": str(row.session_id), "missing_count": len(missing)},
                )
            await session.refresh(row)
            return CheckupTurnOutcome(
                session=health_service.checkup_session_view(row), prompt=None, record=None
            )

        # 五问已答完、分级已出，才进复核闸门：先落库（黄/红复核任务由服务层顺带建），
        # 再把会话挂到 awaiting_review。复核裁决回来时只剩收尾，不再碰记录。
        claimed = await health_service.claim_checkup_session(
            session,
            session_id=row.session_id,
            status="awaiting_review",
            expected="asking",
            now=now,
        )
        record = await _persist_if_any(session, row, state) if claimed else None
        if claimed:
            await health_service.update_checkup_prompt(
                session,
                row,
                prompt=dict(prompt),
                current_key=None,
                attempt_no=int(state.get("attempts", {}).get("mood_score") or row.attempt_no),
                answers=answers,
                missing=missing,
                expires_at=None,
            )
        await session.refresh(row)
        return CheckupTurnOutcome(
            session=health_service.checkup_session_view(row), prompt=dict(prompt), record=record
        )

    await health_service.update_checkup_prompt(
        session,
        row,
        prompt=dict(prompt),
        current_key=str(prompt.get("key") or "") or None,
        attempt_no=int(prompt.get("attempt") or 1),
        answers=answers,
        missing=missing,
        expires_at=now + datetime.timedelta(seconds=health_service.CHECKUP_ANSWER_TIMEOUT_SECONDS),
        timeouts=timeouts,
    )
    await session.refresh(row)
    return CheckupTurnOutcome(session=health_service.checkup_session_view(row), prompt=dict(prompt))


async def start_checkup(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    record_date: datetime.date | None = None,
    source: str = "voice_scheduled",
    now: datetime.datetime | None = None,
    graph: Any | None = None,
) -> CheckupTurnOutcome | None:
    """起一场采集并问到第一题（19:00 任务对每位无记录长者各调一次）。

    返回 `None` 表示当天这场采集已经走完（终态），调用方不必再计数；
    已经开着的会话原样返回当前问句，不重新问一遍——长者的回答在 checkpoint 里，
    重开会把进度冲掉。
    """
    moment = _moment(now)
    day = record_date if record_date is not None else today_in_shanghai()
    row, created = await health_service.open_checkup_session(
        session,
        elder_id=elder_id,
        record_date=day,
        thread_id=health_thread_id(elder_id, day),
        source=source,
    )
    if not created:
        if row.status not in health_service.CHECKUP_OPEN_STATUSES:
            logger.info(
                "health_checkup_start_skipped",
                extra={"reason": "session_closed", "status": row.status},
            )
            return None
        return CheckupTurnOutcome(
            session=health_service.checkup_session_view(row),
            prompt=dict(row.prompt) if row.prompt else None,
        )

    active_graph = graph if graph is not None else await get_health_graph()
    try:
        result = await active_graph.ainvoke(
            initial_state(elder_id, source=source, record_date=day),
            _thread_config(row.thread_id),
        )
    except Exception:
        # 图起不来（checkpoint 库刚崩/图装配失败）：把会话关成 ended 再抛，
        # 否则这一行会停在 asking 且没有 expires_at，永远不会被超时扫描捡起来。
        await health_service.claim_checkup_session(
            session,
            session_id=row.session_id,
            status="ended",
            expected="asking",
            now=moment,
            close=True,
        )
        raise

    state = _result_state(result)
    prompt = pending_prompt(result)
    outcome = await _advance(session, row, state, prompt, now=moment)
    return CheckupTurnOutcome(
        session=outcome.session,
        prompt=outcome.prompt,
        record=outcome.record,
        created=True,
    )


async def submit_checkup_answer(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    text: str,
    record_date: datetime.date | None = None,
    now: datetime.datetime | None = None,
    graph: Any | None = None,
) -> CheckupTurnOutcome:
    """长者（或照护台代填）回答当前一问，返回下一问或终态。

    幂等：会话已到终态时不再 resume 图，原样返回现况；重复回答同一题也不会重复落库
    （终态落库只发生在「条件 UPDATE 抢到状态迁移」的那一次）。
    """
    moment = _moment(now)
    day = record_date if record_date is not None else today_in_shanghai()
    row = await health_service.load_checkup_session(session, elder_id=elder_id, record_date=day)
    if row is None:
        raise health_service.HealthNotFoundError("no health checkup session for today")
    if row.status not in health_service.CHECKUP_OPEN_STATUSES:
        return CheckupTurnOutcome(session=health_service.checkup_session_view(row), prompt=None)
    if row.status == "awaiting_review":
        # 已进人工复核闸门：回答不再喂图（复核 resume 走 `app/api/health.py` 的旧路径）。
        return CheckupTurnOutcome(
            session=health_service.checkup_session_view(row),
            prompt=dict(row.prompt) if row.prompt else None,
        )

    active_graph = graph if graph is not None else await get_health_graph()
    if is_end_reply(text):
        return await _end_checkup(session, row, graph=active_graph, now=moment)

    # 先抢状态：同一场会话被「回答」与「超时扫描」同时推的时候，只有抢到的一方 resume，
    # 另一方直接返回现况——两次 resume 会让同一题被消费两次（回答错位到下一题）。
    claimed = await health_service.claim_checkup_session(
        session,
        session_id=row.session_id,
        status="asking",
        expected="asking",
        now=moment,
    )
    if not claimed:
        await session.refresh(row)
        return CheckupTurnOutcome(
            session=health_service.checkup_session_view(row),
            prompt=dict(row.prompt) if row.prompt else None,
        )

    result = await active_graph.ainvoke(
        Command(resume=text),
        _thread_config(row.thread_id),
    )
    state = _result_state(result)
    prompt = pending_prompt(result)
    return await _advance(session, row, state, prompt, now=moment)


async def _end_checkup(
    session: AsyncSession,
    row: HealthCheckupSession,
    *,
    graph: Any,
    now: datetime.datetime,
    state_override: Mapping[str, Any] | None = None,
) -> CheckupTurnOutcome:
    """长者主动结束：置 ended，已答项照常落库分级（不得丢弃已采集数据）。

    不 resume 图：resume 会把「不测了」当成一次无效回答继续追问，而长者的意思是收工。
    已经答过的项在 checkpoint 的 state 里，直接读出来落库即可。
    """
    claimed = await health_service.claim_checkup_session(
        session,
        session_id=row.session_id,
        status="ended",
        expected=row.status,
        now=now,
        close=True,
    )
    record: health_service.HealthRecordView | None = None
    if claimed:
        if state_override is not None:
            state = dict(state_override)
        else:
            snapshot = await graph.aget_state(_thread_config(row.thread_id))
            state = dict(getattr(snapshot, "values", None) or {})
        record = await _persist_if_any(session, row, state)
        # 收工之后不该再有「在等谁回答」的题挂着：清掉 prompt / current_key，
        # 否则 pad 拉会话仍会显示一个已经没人等的问句（还会以为 60 秒后要超时）。
        await health_service.update_checkup_prompt(
            session,
            row,
            prompt=None,
            current_key=None,
            attempt_no=row.attempt_no,
            answers=dict(state.get("answers") or {}),
            missing=list(state.get("missing") or []),
            expires_at=None,
        )
    await session.refresh(row)
    return CheckupTurnOutcome(
        session=health_service.checkup_session_view(row), prompt=None, record=record
    )


async def end_checkup(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    record_date: datetime.date | None = None,
    now: datetime.datetime | None = None,
    graph: Any | None = None,
) -> CheckupTurnOutcome:
    """长者一次性结束当天采集：未答项在这里统一补成「缺失」，再收口。

    为什么不让端侧自己逐题标缺失：pad 原来要先发 N 次「标缺失」再发一次「结束」，
    最多 6 次串行往返（实测按钮要转 2–5 秒），中途任何一次失败还会留下半个缺失集；
    而且「哪些算未答」本来就只能由图里的 state 判定，端侧的 `current_key` 只是它的投影。
    缺失判定与 `ask_node` 同口径：既不在 `answers` 里、也不在 `missing` 里。
    """
    moment = _moment(now)
    day = record_date if record_date is not None else today_in_shanghai()
    row = await health_service.load_checkup_session(session, elder_id=elder_id, record_date=day)
    if row is None:
        raise health_service.HealthNotFoundError("no health checkup session for today")
    if row.status not in health_service.CHECKUP_OPEN_STATUSES:
        # 已经结束/复核中：幂等返回现况，不重复落库（与 `submit_checkup_answer` 同口径）
        return CheckupTurnOutcome(session=health_service.checkup_session_view(row), prompt=None)
    active_graph = graph if graph is not None else await get_health_graph()
    snapshot = await active_graph.aget_state(_thread_config(row.thread_id))
    state = dict(getattr(snapshot, "values", None) or {})
    answers = dict(state.get("answers") or {})
    missing = list(state.get("missing") or ())
    for key in QUESTION_ORDER:
        if key not in answers and key not in missing:
            missing.append(key)
    state["missing"] = missing
    return await _end_checkup(session, row, graph=active_graph, now=moment, state_override=state)


async def advance_due_checkups(
    session: AsyncSession,
    *,
    now: datetime.datetime | None = None,
    limit: int = health_service.CHECKUP_TIMEOUT_BATCH_LIMIT,
    graph: Any | None = None,
) -> int:
    """把到期未答的会话用 `Command(resume=TIMEOUT_RESUME)` 推进一轮，返回推进了几场。

    为什么是「漏答值」而不是自己算「重问」：`parse_answer(TIMEOUT_RESUME)` 判无效，
    图的 `MAX_ATTEMPTS=2` 循环第一次落到重问、第二次记 `missing` 再问下一题——
    「一次超时=重问，两次超时=标缺失继续」正是 FR-HLT-01 要的语义，
    在这里重写一遍就等于把同一条规则实现两遍（AGENTS §4.8）。
    """
    moment = _moment(now)
    rows = await health_service.due_checkup_sessions(session, now=moment, limit=limit)
    if not rows:
        return 0

    active_graph = graph if graph is not None else await get_health_graph()
    advanced = 0
    for row in rows:
        claimed = await health_service.claim_checkup_session(
            session,
            session_id=row.session_id,
            status="asking",
            expected="asking",
            now=moment,
        )
        if not claimed:
            continue
        try:
            result = await active_graph.ainvoke(
                Command(resume=TIMEOUT_RESUME),
                _thread_config(row.thread_id),
            )
        except Exception as exc:  # 单场失败不能拖垮整批，逐场记账后继续
            # 抢到状态后 ainvoke 才失败：expires_at 已被清空，若不补回，这一场就永远不会
            # 再被扫到（长者的采集会静默停在半路）。补一个到期时间，下一轮重试。
            row.expires_at = moment + datetime.timedelta(
                seconds=health_service.CHECKUP_ANSWER_TIMEOUT_SECONDS
            )
            await session.flush()
            logger.warning(
                "health checkup timeout advance failed",
                extra={"session_id": str(row.session_id), "error_type": type(exc).__name__},
            )
            continue
        state = _result_state(result)
        prompt = pending_prompt(result)
        await _advance(
            session,
            row,
            state,
            prompt,
            now=moment,
            timeouts=row.timeouts + 1,
        )
        advanced += 1
    return advanced


__all__ = [
    "COMPLAINT_NOTICE",
    "END_PHRASES",
    "MAX_ATTEMPTS",
    "QUESTION_ORDER",
    "QUESTION_PROMPTS",
    "CheckupTurnOutcome",
    "ComplaintCheckupOutcome",
    "HealthCheckupState",
    "advance_due_checkups",
    "ask_node",
    "build_health_graph",
    "end_checkup",
    "finish_node",
    "get_health_graph",
    "grade_node",
    "health_thread_id",
    "initial_state",
    "inside_collection_window",
    "is_end_reply",
    "parse_answer",
    "pending_prompt",
    "reset_health_graph",
    "review_node",
    "start_checkup",
    "start_complaint_checkup",
    "state_outcome",
    "submit_checkup_answer",
]
