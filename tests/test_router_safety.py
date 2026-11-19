"""task-8 · 安全层单测（app/router/safety.py）：词族 × 语境 × 下游契约。

分四段：
1. 规则条数/元数据：三条不变，两文件合计 ≤10（AGENTS §7），rule_id 唯一；
2. 词族：FR-DLG-01 的七类触发词全部落在 `safety_emergency` 一条里，其中
   「疑似中风」「严重副作用」是本次补的两类；每条近似不命中（FP 护栏）也各有一条；
3. 语境：否定 / 历史 / 他人主体 / 模糊 的判定只看命中词所在分句（逐条正反例）；
4. 契约：严重度只落 `red`/`yellow` 两值、类别只落三个闭集值、`match_safety` 与
   `classify_safety` 同源、`RouteDecision` 的身份字段恒 None、命中切片 ±8 字。

固定集逐条断言在 `tests/test_router_safety_negation.py`，本文件只放可读的单点规则。
"""

from __future__ import annotations

import pytest

from app.router.decision import LAYER_L0
from app.router.rules import DIRECT_RULES
from app.router.safety import (
    ALERT_INTENT,
    CONTEXT_AFFIRMED,
    CONTEXT_HISTORICAL,
    CONTEXT_NEGATED,
    CONTEXT_THIRD_PARTY,
    CONTEXT_VAGUE,
    RULE_COUNT,
    SAFETY_RULES,
    SEVERITY_RED,
    SEVERITY_YELLOW,
    SafetyRuleSpec,
    classify_safety,
    match_safety,
)

#: `alert_service.SAFETY_CATEGORIES` 的闭集：本模块不许造新类别。
ALERT_CATEGORIES = frozenset({"emergency", "harm", "request_human"})


def _rule(rule_id: str) -> SafetyRuleSpec:
    for spec in SAFETY_RULES:
        if spec.rule_id == rule_id:
            return spec
    raise AssertionError(f"没有这条规则: {rule_id}")


# --------------------------------------------------------------------------- 1. 规模与元数据


def test_rule_count_is_three_and_total_at_most_ten() -> None:
    """新增中风/副作用词族**不新增规则条**：条数仍是 3，合计仍 ≤10。"""
    assert RULE_COUNT == 3
    assert len(SAFETY_RULES) == RULE_COUNT
    assert len(SAFETY_RULES) + len(DIRECT_RULES) <= 10
    ids = [spec.rule_id for spec in SAFETY_RULES]
    assert len(set(ids)) == len(ids) == 3


def test_rule_metadata_is_contract_ready() -> None:
    """每条规则都带类别（下游按类别建工单）与两值严重度（下游 CHECK 只有两档）。"""
    by_id = {spec.rule_id: spec for spec in SAFETY_RULES}
    assert set(by_id) == {"safety_emergency", "safety_harm", "safety_request_human"}
    assert by_id["safety_emergency"].category == "emergency"
    assert by_id["safety_harm"].category == "harm"
    assert by_id["safety_request_human"].category == "request_human"
    for spec in SAFETY_RULES:
        assert spec.category in ALERT_CATEGORIES
        assert spec.severity == SEVERITY_RED


def test_context_checks_are_scoped_per_rule() -> None:
    """语境判定的开关按规则收敛：轻生/求助不参与历史与模糊降级。"""
    emergency = _rule("safety_emergency")
    harm = _rule("safety_harm")
    request_human = _rule("safety_request_human")
    assert emergency.context_checks == {
        "negation",
        "historical",
        "third_party",
        "vague",
    }
    assert harm.context_checks == {"negation", "third_party"}
    assert request_human.context_checks == {"negation"}
    assert harm.subject_self_harm_only is True
    assert emergency.subject_self_harm_only is False


# --------------------------------------------------------------------------- 2. 词族


#: FR-DLG-01 七类触发词 → 至少两种口语写法各自命中红色急症。
_FAMILY_SAMPLES: tuple[tuple[str, str, str], ...] = (
    ("胸痛", "safety_emergency", "我胸口疼得厉害"),
    ("胸痛", "safety_emergency", "左胸口一阵一阵地疼"),
    ("呼吸困难", "safety_emergency", "我憋得慌，一口气快上不来了"),
    ("跌倒", "safety_emergency", "半夜坠床，腿疼得不敢碰"),
    ("出血", "safety_emergency", "手指头一直流血"),
    ("吞咽/呛咳", "safety_emergency", "喝水都呛，咽东西费劲"),
    ("认知急性变化", "safety_emergency", "我忽然认不得屋里的人了"),
    # 本次补的第 2 类：疑似中风（嘴歪 / 说话不清 / 半身无力）
    ("疑似中风", "safety_emergency", "我嘴歪了，说话不清"),
    ("疑似中风", "safety_emergency", "我半边身子没劲"),
    ("疑似中风", "safety_emergency", "我突然半身不遂"),
    ("疑似中风", "safety_emergency", "我中风了"),
    # 本次补的第 6 类：严重副作用（皮疹 / 风团 / 全身瘙痒 / 口唇肿胀）
    ("严重副作用", "safety_emergency", "我吃了药起疹子了"),
    ("严重副作用", "safety_emergency", "身上起了红疹"),
    ("严重副作用", "safety_emergency", "浑身发痒得厉害"),
    ("严重副作用", "safety_emergency", "我嘴唇肿了"),
    ("轻生", "safety_harm", "我不想活了"),
    ("轻生", "safety_harm", "我有点想自残"),
    ("要求人工", "safety_request_human", "我要人工"),
    ("要求人工", "safety_request_human", "请你帮我叫医生来"),
)


@pytest.mark.parametrize(("family", "rule_id", "text"), _FAMILY_SAMPLES)
def test_family_samples_hit_red(family: str, rule_id: str, text: str) -> None:
    """七类词族（含新增中风/副作用）逐条命中红色急症，并落在正确的 rule_id 上。"""
    assessment = classify_safety(text)
    assert assessment is not None, f"{family} 漏召回: {text}"
    assert assessment.rule_id == rule_id, f"{family} 命中了 {assessment.rule_id}: {text}"
    assert assessment.severity == SEVERITY_RED, f"{family} 必须是红档: {text}"
    assert assessment.category in ALERT_CATEGORIES


#: 近似不命中（FP 护栏）：这些句子必须一条都不告警。
_NEAR_MISS_SAMPLES: tuple[str, ...] = (
    "风大，把窗户关一下",  # 「风」/「风团」的近似
    "楼下有人敲锣打鼓",
    "这药说明书写着忌酒",
    "我孙子在学医",
    "血压计摔地上坏了",
    "公园有个人工湖",
    "这药是人工合成的",
    "医生说了没什么大事",
    "换季了注意保暖",
    "叫出租车去趟医院复诊",
    "不想活动了，想躺着",
    "你打我电话说下情况",
    "有人敲门，看看是谁",
    # 模糊的**情绪**表达不是安全事件（PRD §5.2 的「模糊普通意图」另成一类）
    "我心里有点乱",
    "说不清，就是心里不得劲",
    "哎，烦得很",
    # 历史叙述里的词族不该被误当当下事件（这里连词族都没命中）
    "去年冬天摔过一跤，躺了仨月",
    "上回量血压有点高，让复查",
)


@pytest.mark.parametrize("text", _NEAR_MISS_SAMPLES)
def test_near_miss_samples_do_not_alert(text: str) -> None:
    """近似语料不告警：宁可少报一个噪音，也别把日常闲聊变成红色工单。"""
    assert classify_safety(text) is None
    assert match_safety(text) is None


def test_bare_discomfort_without_hedge_is_left_to_l1() -> None:
    """裸的「我很难受」不归 L0：它是情绪表达，交 L1/L2 澄清。

    只有被模糊词修饰的躯体不适（「有点不舒服」）才走黄档——这条边界是对
    「模糊普通意图 20 条」固定集的让步，写下来免得后来者顺手放开。
    """
    assert classify_safety("我很难受") is None
    assert classify_safety("我心里难受") is None
    vague = classify_safety("有点不舒服")
    assert vague is not None
    assert (vague.context, vague.severity) == (CONTEXT_VAGUE, SEVERITY_YELLOW)


# --------------------------------------------------------------------------- 3. 语境


def test_negation_only_counts_before_the_match_in_the_same_clause() -> None:
    """否定窗口 = 命中词之前的同一分句；命中词之后的「不」不参与判定。"""
    assert classify_safety("我没有胸痛") is None
    assert classify_safety("我不觉得喘不上气") is None
    assert classify_safety("没出血") is None
    # 「不放心」在命中之后 / 另一分句 → 前面的主诉照报
    after = classify_safety("胸痛得厉害，不放心")
    assert after is not None
    assert after.severity == SEVERITY_RED


def test_negation_does_not_swallow_bu_dong_verbs() -> None:
    """「动不动」这类「不+动词」不是否定：他伤词族必须照报红色。"""
    assessment = classify_safety("他动不动就打人")
    assert assessment is not None
    assert (assessment.rule_id, assessment.context) == ("safety_harm", CONTEXT_AFFIRMED)


def test_historical_marks_yellow_and_acute_wake_marks_red() -> None:
    """历史 → 黄复核；历史 + 急性复位词 → 回红（主诉当前不适仍走安全侧）。"""
    historical = classify_safety("上次胸痛，后来没事了")
    assert historical is not None
    assert (historical.context, historical.severity) == (
        CONTEXT_HISTORICAL,
        SEVERITY_YELLOW,
    )
    current = classify_safety("上次胸痛，现在又疼得厉害了")
    assert current is not None
    assert (current.context, current.severity) == (CONTEXT_AFFIRMED, SEVERITY_RED)


def test_third_party_requires_subject_position() -> None:
    """人称词要站在分句主语位才算他人主体；被「我」抢回主语就是本人。"""
    third = classify_safety("我爸爸胸痛")
    assert third is not None
    assert (third.context, third.severity) == (CONTEXT_THIRD_PARTY, SEVERITY_YELLOW)
    mine = classify_safety("我孙子说我胸口疼")
    assert mine is not None
    assert (mine.context, mine.severity) == (CONTEXT_AFFIRMED, SEVERITY_RED)


def test_vague_hedged_symptom_is_yellow() -> None:
    """模糊主诉 → 黄复核，绝不直接放行。"""
    for text in ("好像胸口疼", "有点不舒服", "说不上来，心里难受"):
        assessment = classify_safety(text)
        assert assessment is not None, text
        assert (assessment.context, assessment.severity) == (
            CONTEXT_VAGUE,
            SEVERITY_YELLOW,
        ), text


# --------------------------------------------------------------------------- 4. 下游契约


def test_severity_and_context_pairing_is_closed() -> None:
    """red 只配 affirmed；yellow 只配历史/他人/模糊；否定永不产出 assessment。"""
    samples = [text for _, _, text in _FAMILY_SAMPLES] + [
        "我上次摔跤，膝盖到现在还酸",
        "我老伴胸痛",
        "好像胸口疼",
    ]
    for text in samples:
        assessment = classify_safety(text)
        assert assessment is not None, text
        assert assessment.severity in {SEVERITY_RED, SEVERITY_YELLOW}
        if assessment.severity == SEVERITY_RED:
            assert assessment.context == CONTEXT_AFFIRMED, text
        else:
            assert assessment.context in {
                CONTEXT_HISTORICAL,
                CONTEXT_THIRD_PARTY,
                CONTEXT_VAGUE,
            }, text
        assert assessment.context != CONTEXT_NEGATED


@pytest.mark.parametrize("text", ["我胸口疼", "我老伴胸痛", "好像胸口疼", "上次胸痛，后来没事了"])
def test_match_safety_is_a_thin_adapter_over_classify_safety(text: str) -> None:
    """`match_safety` 只做适配：同一句话两条入口的结论必须逐字段一致。"""
    assessment = classify_safety(text)
    decision = match_safety(text)
    assert assessment is not None
    assert decision is not None
    assert decision.alert is not None
    assert decision.layer == LAYER_L0
    assert decision.rule_id == assessment.rule_id
    assert decision.intent == ALERT_INTENT
    assert decision.sensitive is True
    assert decision.elder_id is None
    assert decision.thread_id is None
    assert decision.route_ms == 0
    assert decision.alert.severity == assessment.severity
    assert decision.alert.category == assessment.category
    assert decision.alert.matched == assessment.matched
    assert decision.alert.transcript_excerpt == assessment.transcript_excerpt


def test_excerpt_is_plus_minus_eight_chars() -> None:
    """命中切片 ±8 字：够复核、又不足以还原整句（隐私口径 AGENTS §4.10）。"""
    text = "一二三四五六七八九十我胸口疼甲乙丙丁戊己庚辛壬癸"
    assessment = classify_safety(text)
    assert assessment is not None
    assert len(assessment.transcript_excerpt) == len(assessment.matched) + 16
    assert assessment.matched in assessment.transcript_excerpt
    assert assessment.transcript_excerpt.startswith("四五六七八九十")


def test_empty_input_is_safe() -> None:
    """空串/纯空白 → None（`match_l0` 的第一道守卫依赖它）。"""
    assert classify_safety("") is None
    assert classify_safety("   ") is None
    assert match_safety("") is None
