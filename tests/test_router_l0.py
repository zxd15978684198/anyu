"""迭代 6 W1 · L0 规则层单测（app/router/safety.py + rules.py + decision.py）。

覆盖口径（迭代 6 计划 §4 W1）：
- 每条规则三类断言：正例（命中本规则且 intent 正确）、反例（明显不相关 → 不命中本规则）、
  近似不命中（只差一点的近似词 → 不得命中，落到 None 或别的规则）；
- 两文件合计规则条数 ≤ 10 的元测试（AGENTS §7）；
- 无命中 → None；身份字段不经 L0（elder_id/thread_id 恒 None，AGENTS §4.9）；
- route 计时不在此层：`RouteDecision.route_ms` 恒为 0，由 W2/W4 编排层观测。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from app.router import ROUTER_RULE_COUNT, match_l0
from app.router.decision import LAYER_L0, RouteDecision
from app.router.rules import DIRECT_RULES, match_rules
from app.router.safety import SAFETY_RULES

# ---------------------------------------------------------------------------
# decision.py 契约（W2 直接依赖，字段名/顺序/可空性一次写对）
# ---------------------------------------------------------------------------

_EXPECTED_FIELD_NAMES = (
    "elder_id",
    "thread_id",
    "layer",
    "rule_id",
    "intent",
    "sensitive",
    "confidence",
    "is_out_of_domain",
    "privacy",
    "route_ms",
    "degraded",
    "trace_id",
    # N01（迭代 8）：安全命中证据。放在末尾而不是 rule_id 旁边，是因为它是
    # 「这次命中带来了什么后果」的载体，而不是路由标签的一部分——顺序即语义，
    # 新增字段一律追加到尾部，避免调用方按位置解包时被悄悄换位。
    "alert",
)


def test_route_decision_contract_field_names_and_order() -> None:
    names = tuple(f.name for f in fields(RouteDecision))
    assert names == _EXPECTED_FIELD_NAMES


def test_route_decision_contract_defaults_and_immutability() -> None:
    decision = RouteDecision(elder_id=None, thread_id=None, layer=LAYER_L0)
    # 出名分层：sensitive/is_out_of_domain 恒 bool，confidence 可 None
    assert decision.sensitive is False
    assert decision.is_out_of_domain is False
    assert decision.confidence is None
    assert decision.route_ms == 0  # 计时不归 L0：默认 0
    assert decision.rule_id is None and decision.intent is None
    with pytest.raises(FrozenInstanceError):
        decision.layer = "L1"  # type: ignore[misc]
    assert not hasattr(decision, "__dict__")  # slots=True


# ---------------------------------------------------------------------------
# 元测试：两文件合计 ≤10 条
# ---------------------------------------------------------------------------


def test_rule_count_meta_two_files_total_at_most_ten() -> None:
    total = len(SAFETY_RULES) + len(DIRECT_RULES)
    assert 0 < total <= 10
    assert total == ROUTER_RULE_COUNT
    # 模块顶部注释的条数与表长度一致（防注释漂移）
    assert len(SAFETY_RULES) == 3
    assert len(DIRECT_RULES) == 7
    # rule_id 全局唯一（用于 decision_logs 归因）
    ids = [r.rule_id for r in SAFETY_RULES] + [r.rule_id for r in DIRECT_RULES]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# safety.py：每条 {正例 / 反例 / 近似不命中}
# 命中断言：layer=L0、intent=alert、sensitive=True、rule_id 对号
# ---------------------------------------------------------------------------

_SAFETY_POSITIVE_CASES: list[tuple[str, str]] = [
    ("我胸口疼得厉害", "safety_emergency"),
    ("躺下就喘不上气", "safety_emergency"),
    ("我在卫生间摔倒了", "safety_emergency"),
    ("手指头一直流血", "safety_emergency"),
    ("我不想活了", "safety_harm"),
    ("我有点想自残", "safety_harm"),
    ("自己伤害自己是不对的", "safety_harm"),
    ("有人打我", "safety_harm"),
    ("他动不动就打人", "safety_harm"),
    ("我要人工", "safety_request_human"),
    ("需要人工服务", "safety_request_human"),
    ("请你帮我叫医生来", "safety_request_human"),
    ("给我叫个护工来", "safety_request_human"),
]

_SAFETY_NEGATIVE_CASES: list[tuple[str, str]] = [
    # 反例：语义无关整句，不得命中对应规则
    ("昨晚睡得挺好的", "safety_emergency"),
    ("测了个血压没什么事", "safety_emergency"),
    ("这个枕头有点软", "safety_emergency"),
    ("邻居家孙子满月了", "safety_harm"),
    ("帮我把窗帘拉上", "safety_harm"),
    ("今天的戏唱得真好", "safety_harm"),
    ("花园里的花开了", "safety_request_human"),
    ("今天日照挺好的", "safety_request_human"),
]

_SAFETY_NEAR_MISS_CASES: list[tuple[str, str]] = [
    # 近似不命中：差一点就踩词，必须不命中
    ("不想活动了，想躺着", "safety_harm"),  # 不想活(?!动) 排除
    ("血压计摔地上坏了", "safety_emergency"),  # 摔了/摔倒 都不是
    ("手机摔管理条例不明白", "safety_emergency"),
    ("有人敲门，看看是谁", "safety_harm"),  # 有人打(?!门) 排除
    ("你打我电话说下情况", "safety_harm"),  # 打我(?!电话) 排除
    ("公园有个人工湖", "safety_request_human"),
    ("这药是人工合成的", "safety_request_human"),
    ("医生说了没什么大事", "safety_request_human"),
    ("换季了注意保暖", "safety_request_human"),
    ("叫出租车去趟医院复诊", "safety_request_human"),
]


@pytest.mark.parametrize(("text", "rule_id"), _SAFETY_POSITIVE_CASES)
def test_safety_positive(text: str, rule_id: str) -> None:
    decision = match_l0(text)
    assert decision is not None
    assert decision.layer == LAYER_L0
    assert decision.rule_id == rule_id
    assert decision.intent == "alert"
    assert decision.sensitive is True


@pytest.mark.parametrize(("text", "rule_id"), _SAFETY_NEGATIVE_CASES)
def test_safety_negative(text: str, rule_id: str) -> None:
    decision = match_l0(text)
    assert decision is None or decision.rule_id != rule_id


@pytest.mark.parametrize(("text", "rule_id"), _SAFETY_NEAR_MISS_CASES)
def test_safety_near_miss_misses(text: str, rule_id: str) -> None:
    decision = match_l0(text)
    assert decision is None or decision.rule_id != rule_id


# ---------------------------------------------------------------------------
# rules.py：每条 {正例 / 反例 / 近似不命中}
# 正例断言：layer=L0、全句锚定命中的 rule_id 与 intent 对号
# 反例/近似：不得命中本规则（真实语义留给 L1/L2，不在 L0 扩义）
# ---------------------------------------------------------------------------

_DIRECT_CASES: dict[str, dict[str, list[tuple[str, str | None]]]] = {
    "play_control": {
        "positive": [
            ("换一段", "play_next"),
            ("再换一段吧", "play_next"),
            ("给我换一段", "play_next"),
            ("停", "stop"),
            ("停下", "stop"),
            ("停下来", "stop"),
            ("先停一下呀", "stop"),
            ("快停", "stop"),
        ],
        "negative": [("换个话题", None), ("停车熄火哦", None), ("换个台", None)],
        "near_miss": [("停下来了吗", None), ("换一段京剧吧", None)],
    },
    "volume_control": {
        "positive": [
            ("大声点", "volume_up"),
            ("大点声", "volume_up"),
            ("声音大一点", "volume_up"),
            ("小声点吧", "volume_down"),
            ("声音小点", "volume_down"),
        ],
        "negative": [("声音", None), ("音量", None)],
        "near_miss": [("大声点说话", None), ("小声嘀咕", None)],
    },
    "opera": {
        "positive": [
            ("越剧", "play_yueju"),
            ("来一段越剧", "play_yueju"),
            ("给我放一段越剧", "play_yueju"),
            ("京剧", "play_jingju"),
            ("来一出京剧吧", "play_jingju"),
            ("我要听京剧呀", "play_jingju"),
        ],
        "negative": [("戏曲", None), ("黄梅戏来一段", None)],
        "near_miss": [
            ("我妈以前是唱京剧的", None),
            ("越剧票还有吗", None),
            ("越剧频道怎么调", None),
        ],
    },
    "info_query": {
        "positive": [
            ("几点了", "ask_time"),
            ("现在几点", "ask_time"),
            ("现在几点了呀", "ask_time"),
            ("请问几点钟了", "ask_time"),
            ("帮我看看几点", "ask_time"),
            ("今天天气", "weather_query"),
            ("今天天气怎么样", "weather_query"),
            ("今天天气怎么样啊", "weather_query"),
            ("今天天气好不好", "weather_query"),
            ("今天天气如何", "weather_query"),
        ],
        "negative": [("时间", None), ("日期", None), ("天气", None), ("明天下雨吗", None)],
        "near_miss": [
            ("你几点睡觉呀", None),
            ("十一点了", None),
            ("几点吃饭呢", None),
            ("今天天气真不错", None),
            ("今天天气预报说有雨", None),
        ],
    },
    "medication": {
        "positive": [
            ("我要吃药", "medication_take"),
            ("我想吃药", "medication_take"),
            ("该吃药了", "medication_take"),
            ("该我吃药了", "medication_take"),
            ("现在吃药", "medication_take"),
            ("吃药时间到了", "medication_take"),
        ],
        "negative": [("吃药麻烦的", None), ("忘了吃药时间", None)],
        "near_miss": [
            ("我吃过了", None),
            ("药吃完了", None),
            ("我吃点苹果吧", None),
            ("把药倒了吧", None),
        ],
    },
    "farewell": {
        "positive": [
            ("再见", "farewell"),
            ("再见啦", "farewell"),
            ("拜拜", "farewell"),
            ("下次再见呀", "farewell"),
        ],
        "negative": [("见", None), ("欢迎再来", None)],
        "near_miss": [("再见面聊聊", None), ("回头见吧", None)],
    },
    "repeat": {
        "positive": [
            ("再说一遍", "repeat"),
            ("你再讲一遍吧", "repeat"),
            ("重复一遍", "repeat"),
            ("再重复一遍", "repeat"),
            ("再放一遍", "repeat"),
            ("再来一遍", "repeat"),
            ("帮我重复一下", "repeat"),
        ],
        "negative": [("再说", None), ("重新算一遍", None)],
        "near_miss": [("重复那个故事", None), ("明天再来一趟", None)],
    },
}


@pytest.mark.parametrize(
    ("rule_id", "text", "intent"),
    [
        (rule_id, text, intent)
        for rule_id, groups in _DIRECT_CASES.items()
        for text, intent in groups["positive"]
    ],
    ids=lambda v: str(v),
)
def test_direct_rule_positive(rule_id: str, text: str, intent: str) -> None:
    decision = match_rules(text)
    assert decision is not None
    assert decision.layer == LAYER_L0
    assert decision.rule_id == rule_id
    assert decision.intent == intent
    assert decision.sensitive is False
    assert decision.route_ms == 0  # 计时不归 L0


@pytest.mark.parametrize(
    ("rule_id", "text"),
    [
        (rule_id, text)
        for rule_id, groups in _DIRECT_CASES.items()
        for (text, _) in groups["negative"]
    ],
    ids=lambda v: str(v),
)
def test_direct_rule_negative(rule_id: str, text: str) -> None:
    decision = match_rules(text)
    assert decision is None or decision.rule_id != rule_id


@pytest.mark.parametrize(
    ("rule_id", "text"),
    [
        (rule_id, text)
        for rule_id, groups in _DIRECT_CASES.items()
        for (text, expected) in groups["near_miss"]
        if expected is None
    ],
    ids=lambda v: str(v),
)
def test_direct_rule_near_miss_falls_through(rule_id: str, text: str) -> None:
    decision = match_rules(text)
    assert decision is None or decision.rule_id != rule_id


# ---------------------------------------------------------------------------
# 主入口 match_l0：无命中 → None；身份不进 L0；safety 优先于 rules（短路）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "今天心情不错",
        "给我讲个故事听",
        "我们下盘象棋吧",
        "嗯",
        "   ",
        "",
    ],
)
def test_match_l0_no_hit_returns_none(text: str) -> None:
    assert match_l0(text) is None


def test_match_l0_leaves_identity_to_runtime_context() -> None:
    decision = match_l0("几点了")
    assert decision is not None
    assert decision.elder_id is None and decision.thread_id is None
    assert decision.trace_id is None
    assert decision.degraded is None
    assert decision.confidence is None


@pytest.mark.parametrize(
    ("text", "expected_rule_id"),
    [
        ("小声点，我不想活了", "safety_harm"),
        ("放段京剧好了，我胸口疼", "safety_emergency"),
        ("再见了，我要转人工", "safety_request_human"),
    ],
)
def test_safety_scan_precedes_direct_rules(text: str, expected_rule_id: str) -> None:
    """safety 一票否决先于 rules 扫描：即便句中混入固定意图，也只回 alert 决策。"""
    decision = match_l0(text)
    assert decision is not None
    assert decision.intent == "alert"
    assert decision.sensitive is True
    assert decision.rule_id == expected_rule_id
