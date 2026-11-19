"""FR-KNW-01 / FR-KNW-05 / FR-KNW-03 输出层校验闸（第四轮 W3，task-15）。

覆盖计划的 8 条（+挂载点回归）：
1. 有来源通过；2. 无来源降级；3. 空检索固定话术；4. 冲突取权威级；
5. 同权威级取新生效；6. 关开关零变化；7. 不误伤正常闲聊；8. 文案不越医疗红线；
9. companion 答案出口确实挂了这道闸（只改播报文本、非 speak 不动、闸门自身崩了不抛）。

> 文件名刻意叫 `test_knowledge_output_guard.py`：`tests/test_knowledge_guard.py` 早已存在，
> 测的是**另一个**模块 `app/core/knowledge_guard.py`（FR-KNW-04 知识正文注入防护，迭代 9 W3），
> 两者不是一回事，不能合并、更不能互相覆盖。

这些都是纯函数用例，不连库；`_guard_knowledge_output` 只读 `get_settings()`（默认开关为真）。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import pytest

from app.agents.companion import CompanionTurnResult, _guard_knowledge_output
from app.services.alert_aggregation import assert_attention_wording
from app.services.knowledge_output_guard import (
    MISSING_ANSWER_SPEECH,
    STALE_SOURCE_SPEECH,
    arbitrate,
    arbitration_note,
    arbitration_speech,
    assert_source_cited,
    contains_concrete_fact,
    guard_answer,
    is_missing_answer,
)


@dataclass(frozen=True, slots=True)
class FakeHit:
    """`KnowledgeHit` 的最小替身：本模块只按属性读，不需要真去查库。"""

    title: str
    authority_level: str
    effective_from: datetime.datetime | None
    document_id: str = "doc-1"
    version: int = 1
    content: str = ""


def _hit(
    title: str,
    *,
    authority: str = "institution",
    effective: datetime.datetime | None = None,
    document_id: str | None = None,
    no_date: bool = False,
) -> FakeHit:
    moment: datetime.datetime | None
    if no_date:
        moment = None
    else:
        moment = effective or datetime.datetime(2026, 3, 1, 1, 0, tzinfo=datetime.UTC)
    return FakeHit(
        title=title,
        authority_level=authority,
        effective_from=moment,
        document_id=document_id or f"doc-{title}",
        content=f"{title} 的内容",
    )


MEAL = _hit(
    "探视与用餐管理规定", effective=datetime.datetime(2026, 3, 1, 1, 0, tzinfo=datetime.UTC)
)
MEAL_OLD = _hit(
    "探视与用餐管理规定", effective=datetime.datetime(2024, 1, 8, 1, 0, tzinfo=datetime.UTC)
)
MEAL_2026 = _hit(
    "探视与用餐管理规定", effective=datetime.datetime(2026, 9, 1, 1, 0, tzinfo=datetime.UTC)
)
MEMORY = _hit("长者自己说过的作息偏好", authority="elder_memory")


# ---------------------------------------------------------------- 1. 有来源通过


def test_answer_with_source_and_date_passes() -> None:
    answer = "按《探视与用餐管理规定》（2026-03-01 生效），午饭是 11:30 开饭。"
    assert assert_source_cited(answer, [MEAL]) is True
    outcome = guard_answer(answer, query="几点开饭", citations=[MEAL])
    assert outcome.outcome == "ok"
    assert outcome.changed is False
    assert outcome.spoken_text == answer  # 通过时不改写一个字


def test_source_without_date_is_not_counted_as_cited() -> None:
    """只有标题没有生效日期 ≠ 点了来源（AC-43 要的是「同时」）。"""
    answer = "按《探视与用餐管理规定》，午饭 11:30 开饭。"
    assert assert_source_cited(answer, [MEAL]) is False


@pytest.mark.parametrize(
    "date_text",
    ["2026-03-01", "2026年3月1日", "2026.03.01", "2026/03/01", "2026/3/1"],
)
def test_relaxed_date_formats_count_as_cited(date_text: str) -> None:
    """日期放宽：ISO / 年月日 / 点分 / 斜杠 四种写法都认（归一化后比）。"""
    answer = f"按《探视与用餐管理规定》（{date_text} 生效），午饭 11:30 开饭。"
    assert assert_source_cited(answer, [MEAL]) is True
    assert guard_answer(answer, query="几点开饭", citations=[MEAL]).outcome == "ok"


def test_title_without_book_marks_counts_as_cited() -> None:
    """标题放宽：不要求逐字带《》，归一化包含即算命中。"""
    answer = "探视与用餐管理规定 2026-03-01 起生效，午饭 11:30 开饭。"
    assert assert_source_cited(answer, [MEAL]) is True


def test_chinese_numeral_date_is_not_parsed_on_purpose() -> None:
    """中文数字日期（「三月一日」）有意不解析：歧义大，宁可保守降级。"""
    answer = "按《探视与用餐管理规定》，三月一日起午饭 11:30 开饭。"
    assert assert_source_cited(answer, [MEAL]) is False
    assert guard_answer(answer, query="几点开饭", citations=[MEAL]).outcome == "downgraded"


def test_title_with_trailing_note_still_matches() -> None:
    """标题尾巴的括注不算核心片段：「入住须知（二）」→「入住须知」也能命中。"""
    hit = _hit("入住须知（二）")
    answer = "按入住须知，2026-03-01 起晚上 21:00 统一关灯。"
    assert assert_source_cited(answer, [hit]) is True


def test_undated_pick_never_falls_back_to_today() -> None:
    """仲裁胜出条目没标生效日期 → 显式说「没标」，绝不把今天当生效日期念出来。"""
    undated = _hit("夜间门禁规定", no_date=True)
    pick = arbitrate([undated])
    assert pick is not None
    assert pick.effective_from is None
    assert pick.has_effective_date is False
    assert pick.effective_date == ""

    speech = arbitration_speech(pick)
    assert "没标生效日期" in speech and "问前台" in speech
    note = arbitration_note(pick)
    assert "未标注" in note

    today = datetime.datetime.now(datetime.UTC).astimezone(
        datetime.timezone(datetime.timedelta(hours=8))
    )
    today_text = today.date().isoformat()
    assert today_text not in speech
    assert today_text not in note
    assert contains_concrete_fact(speech) is False

    # 冲突场景下走同一分支：胜出条目没标日期，输出必须是「没标」而不是今天。
    outcome = guard_answer(
        "晚上十点以后不能出门。", query="夜间能出门吗", citations=[MEMORY, undated]
    )
    assert outcome.outcome == "arbitrated"
    assert "没标生效日期" in outcome.spoken_text
    assert today_text not in outcome.spoken_text


# ---------------------------------------------------------------- 2. 无来源降级


def test_answer_without_source_is_downgraded() -> None:
    answer = "午饭是 11:30，晚饭 17:30，家属每天下午都能来。"
    outcome = guard_answer(answer, query="几点开饭", citations=[MEAL])
    assert outcome.outcome == "downgraded"
    assert outcome.spoken_text == STALE_SOURCE_SPEECH
    assert "可能过期" in outcome.spoken_text and "问前台" in outcome.spoken_text


# ---------------------------------------------------------------- 3. 空检索话术


def test_empty_retrieval_replaces_invented_numbers() -> None:
    outcome = guard_answer(
        "中午 11:30 开饭，晚上 5 点半有加餐。",
        query="咱们这儿午餐几点开饭？",
        citations=(),
        missing=True,
    )
    assert outcome.outcome == "missing"
    assert outcome.spoken_text == MISSING_ANSWER_SPEECH
    # 固定话术里不许再出现具体时间/数字：等号右边是在「编规定」。
    assert contains_concrete_fact(outcome.spoken_text) is False
    assert is_missing_answer(outcome.spoken_text) is True


def test_empty_retrieval_without_numbers_is_left_alone() -> None:
    """检索没命中、但模型本来就在说「查不到」——不必再改一遍。"""
    answer = "这个我这儿查不到，我帮您问一下前台。"
    outcome = guard_answer(answer, query="探视要预约吗", citations=(), missing=True)
    assert outcome.outcome == "ok"
    assert outcome.spoken_text == answer


# ---------------------------------------------------------------- 4. 冲突取权威级


def test_conflict_prefers_institution_over_elder_memory() -> None:
    pick = arbitrate([MEMORY, MEAL])
    assert pick is not None
    assert pick.title == "探视与用餐管理规定"
    assert pick.authority_level == "institution"
    assert "权威级 institution 优先" in pick.reason

    # 缺来源 + 冲突 → 用仲裁结果覆盖，并且把来源名与生效日期一起点出来。
    outcome = guard_answer("下午三点到五点可以探视。", query="探视时间", citations=[MEMORY, MEAL])
    assert outcome.outcome == "arbitrated"
    assert "《探视与用餐管理规定》" in outcome.spoken_text
    assert "2026-03-01" in outcome.spoken_text
    assert assert_source_cited(outcome.spoken_text, [MEAL]) is True


def test_conflict_with_citation_only_appends_basis() -> None:
    """已点明来源时不覆盖正文，只追加一行依据（避免把正常回答整段吃掉）。"""
    answer = "按《探视与用餐管理规定》（2026-03-01 生效），下午三点到五点可以探视。"
    outcome = guard_answer(answer, query="探视时间", citations=[MEAL, MEMORY])
    assert outcome.outcome == "arbitrated"
    assert outcome.spoken_text.startswith(answer)
    assert outcome.spoken_text.endswith("）")


# ---------------------------------------------------------------- 5. 同权威级取新生效


def test_same_authority_prefers_newer_effective_date() -> None:
    pick = arbitrate([MEAL_OLD, MEAL_2026])
    assert pick is not None
    assert pick.effective_date == "2026-09-01"
    assert "生效日期更新者" in pick.reason


def test_same_authority_ignores_lower_authority_newer_date() -> None:
    """权威级优先于生效日期：elder_memory 再新也压不过 institution。"""
    newer_memory = _hit(
        "长者说自己可以随时下楼",
        authority="elder_memory",
        effective=datetime.datetime(2026, 10, 1, 1, 0, tzinfo=datetime.UTC),
    )
    pick = arbitrate([newer_memory, MEAL])
    assert pick is not None
    assert pick.authority_level == "institution"


# ---------------------------------------------------------------- 6. 关开关零变化


def test_disabled_guard_is_byte_identical() -> None:
    answer = "午饭是 11:30，可以带家属一起吃。"
    outcome = guard_answer(answer, query="几点开饭", citations=[MEAL], enabled=False)
    assert outcome.outcome == "disabled"
    assert outcome.changed is False
    assert outcome.spoken_text == answer


def test_disabled_guard_keeps_missing_retrieval_speech() -> None:
    answer = "中午 11:30 开饭。"
    outcome = guard_answer(answer, query="几点开饭", citations=(), missing=True, enabled=False)
    assert outcome.spoken_text == answer


# ---------------------------------------------------------------- 7. 不误伤闲聊


def test_small_talk_with_numbers_is_not_touched() -> None:
    answer = "我今年 88 岁啦，早上 6 点就醒了，您也是吗？"
    outcome = guard_answer(answer, query="您今年多大岁数呀", citations=(), missing=False)
    assert outcome.outcome == "ok"
    assert outcome.spoken_text == answer


def test_non_institutional_query_with_missing_flag_is_not_touched() -> None:
    """`missing` 为真只说明检索没命中，不代表这是机构事务问题。"""
    answer = "我 1958 年出生的。"
    outcome = guard_answer(answer, query="您是哪年生的", citations=(), missing=True)
    assert outcome.outcome == "ok"
    assert outcome.spoken_text == answer


# ---------------------------------------------------------------- 8. 文案不越医疗红线


@pytest.mark.parametrize(
    "speech",
    [
        MISSING_ANSWER_SPEECH,
        STALE_SOURCE_SPEECH,
        arbitration_speech(arbitrate([MEAL])),  # type: ignore[arg-type]
    ],
)
def test_speeches_are_free_of_medical_wording(speech: str) -> None:
    assert assert_attention_wording(speech) == speech
    for forbidden in ("高血压", "糖尿病", "诊断", "确诊", "处方"):
        assert forbidden not in speech


# ---------------------------------------------------------------- 9. 挂载点回归


def _turn_result(text: str, *, finish_reason: str = "speak") -> CompanionTurnResult:
    return CompanionTurnResult(
        spoken_text=text,
        route_decision=object(),
        finish_reason=finish_reason,
        degrade_reason=None,
        falsified_identity=[],
        tool_calls=[],
        steps=[],
    )


def test_companion_hook_rewrites_spoken_text_only() -> None:
    result = _turn_result("午饭是 11:30，可以带家属一起吃。")
    guarded = _guard_knowledge_output(
        result,
        query="几点开饭",
        citations=[MEAL],
        missing=False,
        trace_id="t-1",
    )
    assert guarded.spoken_text == STALE_SOURCE_SPEECH
    assert guarded.finish_reason == result.finish_reason
    assert guarded.route_decision is result.route_decision
    assert guarded.tool_calls == result.tool_calls
    assert guarded.steps == result.steps


def test_companion_hook_leaves_non_speak_alone() -> None:
    """降级/熔断收口话术属于失败分类口径，不该被拼来源或被换成「查不到」。"""
    result = _turn_result("这会儿有点忙，您稍等一下。", finish_reason="llm_unavailable")
    guarded = _guard_knowledge_output(
        result,
        query="几点开饭",
        citations=[],
        missing=True,
        trace_id="t-2",
    )
    assert guarded is result


def test_companion_hook_survives_guard_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门自身故障只告警，不能把一轮对话带崩。"""
    import app.agents.companion as companion_module

    def _boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("guard 炸了")

    monkeypatch.setattr(companion_module, "guard_answer", _boom)
    result = _turn_result("随便说点啥")
    guarded = _guard_knowledge_output(
        result,
        query="随便",
        citations=[MEAL],
        missing=False,
        trace_id="t-3",
    )
    assert guarded is result
