"""健康采集子图与分级引擎（PRD §4.6 / FR-HLT-01/02/03/04，迭代 8 W2）。

本文件不连数据库：子图走 `MemorySaver` 单跑，分级是纯函数。
「写库/通知」在 `tests/test_health_api.py` 验，两处不混在一起 —— 图挂了先看图，
库挂了先看 service，排障时不会两头怀疑。
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graphs import health_checkup_graph as graph_module
from app.graphs.health_checkup_graph import (
    MAX_ATTEMPTS,
    QUESTION_ORDER,
    build_health_graph,
    health_thread_id,
    initial_state,
    parse_answer,
    pending_prompt,
    state_outcome,
)
from app.services.health_service import grade_vitals

ELDER = uuid.UUID("11111111-1111-1111-1111-111111111111")
DAY = datetime.date(2026, 11, 7)


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


async def _run_replies(graph, thread_id: str, replies: list[object]):
    """跑一轮采集：从首问开始，按给定回答逐条 resume，返回最后一次结果。"""
    state = initial_state(ELDER, record_date=DAY, thread_id=thread_id)
    config = _config(thread_id)
    result = await graph.ainvoke(state, config)
    for reply in replies:
        prompt = pending_prompt(result)
        if prompt is None or prompt.get("type") != "question":
            break
        result = await graph.ainvoke(Command(resume=reply), config)
    return result


def _keys(result) -> list[str]:
    return list(result.get("asked", []))


# ------------------------------------------------------------------ 分级引擎（12 例）


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # 绿
        (dict(systolic=128, diastolic=78, heart_rate=72, sleep_hours=7, mood_score=4), "green"),
        # 红：收缩压单分量
        (dict(systolic=180, diastolic=70), "red"),
        # 红：舒张压单分量（另一分量缺失不阻断 —— AC-21）
        (dict(systolic=None, diastolic=120), "red"),
        # 红：两项同时超限，等级不因缺项降级
        (dict(systolic=190, diastolic=130, sleep_hours=3, mood_score=1), "red"),
        # 黄：收缩压 140–179
        (dict(systolic=150, diastolic=80, heart_rate=72, sleep_hours=7, mood_score=4), "yellow"),
        # 黄：舒张压 90–119
        (dict(systolic=128, diastolic=95, heart_rate=72, sleep_hours=7, mood_score=4), "yellow"),
        # 黄：收缩压 <90（PRD 低值项）
        (dict(systolic=85, diastolic=70, heart_rate=72, sleep_hours=7, mood_score=4), "yellow"),
        # 黄：舒张压 <60
        (dict(systolic=128, diastolic=55, heart_rate=72, sleep_hours=7, mood_score=4), "yellow"),
        # 黄：睡眠 <5h
        (dict(systolic=128, diastolic=78, heart_rate=72, sleep_hours=4.5, mood_score=4), "yellow"),
        # 黄：情绪 ≤2
        (dict(systolic=128, diastolic=78, heart_rate=72, sleep_hours=7, mood_score=2), "yellow"),
        # 黄：任一采集项缺失
        (dict(systolic=None, diastolic=78, heart_rate=72, sleep_hours=7, mood_score=4), "yellow"),
        # 黄：心率异常（<50 / >110，属「其他异常」，非 PRD 五问字段）
        (dict(systolic=128, diastolic=78, heart_rate=45, sleep_hours=7, mood_score=4), "yellow"),
        # 绿：心率缺失不判黄（心率为基座额外列，避免把 PRD 绿档拖黄）
        (dict(systolic=128, diastolic=78, heart_rate=None, sleep_hours=7, mood_score=4), "green"),
    ],
)
def test_grade_vitals_table(values: dict[str, object], expected: str) -> None:
    assert grade_vitals(**values).grade == expected


def test_grade_reasons_are_specific() -> None:
    """原因码要能直接给复核台看：红档保留缺项说明，黄档逐项列出。"""
    red = grade_vitals(systolic=185, diastolic=None)
    assert red.grade == "red"
    assert red.reasons[0] == "systolic_red"
    assert "diastolic_missing" in red.reasons  # 单分量判红不丢「另一项没测」的信息

    yellow = grade_vitals(systolic=150, diastolic=95, heart_rate=130, sleep_hours=3, mood_score=1)
    assert yellow.grade == "yellow"
    assert {
        "systolic_high",
        "diastolic_high",
        "heart_rate_abnormal",
        "sleep_low",
        "mood_low",
    } <= set(yellow.reasons)
    assert yellow.needs_review is True


# ------------------------------------------------------------------ 子图主流程


async def test_five_questions_in_fixed_order_then_green() -> None:
    graph = build_health_graph(MemorySaver())
    thread_id = health_thread_id(ELDER, DAY)

    result = await _run_replies(graph, thread_id, ["128", "78", "72", "7", 4])

    assert _keys(result) == list(QUESTION_ORDER)
    assert result["grade"] == "green"
    assert result["reasons"] == []
    assert result["needs_review"] is False
    assert result["finished"] is True
    assert pending_prompt(result) is None
    # 解析后的类型：血压/心率为整数，睡眠为浮点
    assert result["answers"]["systolic"] == 128
    assert isinstance(result["answers"]["sleep_hours"], float)


async def test_invalid_answer_asked_twice_then_marked_missing() -> None:
    graph = build_health_graph(MemorySaver())
    thread_id = health_thread_id(ELDER, DAY)

    result = await _run_replies(graph, thread_id, ["不知道", "还是不知道", "78", "72", "7", 4])

    systolic_prompts = [item for item in result["prompts"] if "收缩压" in item]
    assert len(systolic_prompts) == MAX_ATTEMPTS  # 重问一次，不多不少
    assert result["asked"].count("systolic") == MAX_ATTEMPTS
    assert result["missing"] == ["systolic"]
    assert result["grade"] == "yellow"  # 缺项判黄，但不阻断整场采集
    # 黄 → 进 HITL 复核闸门，而不是直接收尾
    assert pending_prompt(result)["type"] == "review"
    assert result["finished"] is False


async def test_retry_once_then_valid_answer_is_recorded() -> None:
    graph = build_health_graph(MemorySaver())
    thread_id = health_thread_id(ELDER, DAY)

    result = await _run_replies(graph, thread_id, ["不知道", "128", "78", "72", "7", 4])

    assert result["missing"] == []
    assert result["answers"]["systolic"] == 128
    assert result["asked"].count("systolic") == MAX_ATTEMPTS


async def test_diastolic_not_above_systolic_is_rejected() -> None:
    """收缩压 ≤ 舒张压按无效处理：重问一次仍无效则标缺失（PRD §4.6 数据校验）。"""
    graph = build_health_graph(MemorySaver())
    thread_id = health_thread_id(ELDER, DAY)

    result = await _run_replies(graph, thread_id, ["120", "130", "130", "72", "7", 4])

    assert "diastolic" in result["missing"]
    assert "diastolic:not_below_systolic" in result["warnings"]
    assert result["grade"] == "yellow"


async def test_red_single_component_goes_to_review_interrupt() -> None:
    graph = build_health_graph(MemorySaver())
    thread_id = health_thread_id(ELDER, DAY)

    result = await _run_replies(graph, thread_id, ["190", "80", "72", "7", 4])

    assert result["grade"] == "red"
    assert result["needs_review"] is True
    assert result["finished"] is False  # 停在 HITL 闸门
    review = pending_prompt(result)
    assert review is not None and review["type"] == "review"
    assert review["grade"] == "red"


async def test_yellow_review_resume_records_decision() -> None:
    graph = build_health_graph(MemorySaver())
    thread_id = health_thread_id(ELDER, DAY)
    config = _config(thread_id)

    result = await _run_replies(graph, thread_id, ["150", "95", "72", "7", 4])
    assert pending_prompt(result)["type"] == "review"

    resumed = await graph.ainvoke(
        Command(resume={"decision": "escalated", "note": "已电话联系"}), config
    )

    assert resumed["reviewed"] is True
    assert resumed["review_decision"] == "escalated"
    assert resumed["finished"] is True
    assert resumed["grade"] == "yellow"  # 复核不改分级


async def test_restart_does_not_reask_answered_questions() -> None:
    """kill 后重连：同一 thread_id 的 checkpoint 里已有三问的答案，继续时只问剩下的。"""
    saver = MemorySaver()
    thread_id = health_thread_id(ELDER, DAY)
    config = _config(thread_id)

    first = build_health_graph(saver)
    result = await _run_replies(first, thread_id, ["128", "78", "72"])
    assert _keys(result) == ["systolic", "diastolic", "heart_rate"]
    assert pending_prompt(result)["key"] == "sleep_hours"

    # 模拟进程重启：同一个 checkpointer、全新的编译图
    second = build_health_graph(saver)
    snapshot = await second.aget_state(config)
    assert snapshot.values["asked"] == ["systolic", "diastolic", "heart_rate"]
    assert snapshot.values["answers"] == {"systolic": 128, "diastolic": 78, "heart_rate": 72}

    resumed = await second.ainvoke(Command(resume="7"), config)
    assert pending_prompt(resumed)["key"] == "mood_score"
    resumed = await second.ainvoke(Command(resume=4), config)
    assert resumed["finished"] is True
    assert _keys(resumed) == list(QUESTION_ORDER)


def test_health_thread_id_is_stable_per_elder_and_day() -> None:
    assert health_thread_id(ELDER, DAY) == f"health:{ELDER}:2026-11-07"
    assert health_thread_id(ELDER, "2026-11-08") == f"health:{ELDER}:2026-11-08"
    # 不传日期用北京时间当天
    assert health_thread_id(ELDER).startswith(f"health:{ELDER}:")


@pytest.mark.parametrize(
    ("key", "reply", "expected"),
    [
        ("systolic", "128", 128),
        ("systolic", 128, 128),
        ("systolic", "  128 ", 128),
        ("systolic", "128.0", 128),
        ("systolic", "不知道", None),
        ("systolic", "", None),
        ("systolic", None, None),
        ("systolic", -5, None),
        ("systolic", 400, None),
        ("systolic", True, None),  # bool 不是血压
        ("mood_score", "4.5", None),
        ("mood_score", 9, None),
        ("sleep_hours", "7.5", 7.5),
    ],
)
def test_parse_answer(key: str, reply: object, expected: object) -> None:
    assert parse_answer(key, reply) == expected


async def test_state_outcome_carries_persist_payload() -> None:
    graph = build_health_graph(MemorySaver())
    thread_id = health_thread_id(ELDER, DAY)

    result = await _run_replies(graph, thread_id, ["128", "78", "72", "7", 4])
    outcome = state_outcome(result)

    assert outcome["elder_id"] == str(ELDER)
    assert outcome["thread_id"] == thread_id
    assert outcome["record_date"] == "2026-11-07"
    assert outcome["source"] == "voice_scheduled"
    assert outcome["grade"] == "green"
    assert outcome["answers"]["mood_score"] == 4
    assert outcome["reviewed"] is False


def test_graph_singleton_reset_allows_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """单例按 checkpointer 建成后不能串到别的测试：`reset_health_graph()` 必须真的清掉。"""
    graph_module.reset_health_graph()
    assert graph_module._GRAPH is None
    graph_module.reset_health_graph()
