"""task-8 · 安全层语境四分类固定集（PRD FR-DLG-07，P0）。

固定集 `eval/fixtures/safety_negation.jsonl` 按 PRD §5.2「安全边界表达 20 条」
拆成四类，每类 ≥5 条：否定 / 历史 / 他人主体 / 模糊。每条都跑**真**判定
`app.router.safety.classify_safety`，不做任何 mock。

为什么每条还要额外断言 `raw_match`：光断言「否定 → 不告警」是不够的——如果词族
压根没匹配上，测试也会绿，而真正的缺陷（「我没有胸痛」被派红色工单）依然在。
所以否定类样本必须同时满足「词族命中」+「语境判为否定」，放行才是**判定**出来的，
不是**漏掉**出来的。

期望值的三态（`expect`）：
- `severity=null` + `context=negated`：不告警，交 L1/L2 正常对话；
- `severity=yellow`：黄色复核（历史叙述 / 他人主体 / 模糊主诉），**仍然 `sensitive`**，
  绝不等于放行；
- `severity=red`：当下、本人、明确 → 红色急症派人。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.router.safety import (
    CONTEXTS,
    RULE_COUNT,
    SAFETY_RULES,
    classify_safety,
    match_safety,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "safety_negation.jsonl"

#: PRD §5.2 的四类安全边界表达（不含「当下确认」这一档）。
KINDS = ("negation", "historical", "third_party", "vague")

#: `alert_service.SAFETY_CATEGORIES` 的闭集：本模块不许造新类别。
ALERT_CATEGORIES = frozenset({"emergency", "harm", "request_human"})


def _load() -> list[dict[str, Any]]:
    """读固定集。**不用 tmp_path**：用例只读仓库里的固定文件（沙箱写不了临时目录）。"""
    lines = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


RECORDS: list[dict[str, Any]] = _load()
RECORD_IDS: list[str] = [str(record["id"]) for record in RECORDS]


def _raw_match(text: str) -> bool:
    """只跑词族、不跑语境：证明「这个词是的确命中了词表」。"""
    return any(spec.pattern.search(text) is not None for spec in SAFETY_RULES)


# --------------------------------------------------------------------------- 逐条判定


@pytest.mark.parametrize("record", RECORDS, ids=RECORD_IDS)
def test_fixture_record(record: dict[str, Any]) -> None:
    """固定集逐条：语境、严重度、rule_id、以及适配器同源。"""
    text = str(record["text"])
    expect = record["expect"]
    assessment = classify_safety(text)

    if expect["severity"] is None:
        # 否定：词族命中，但语境判为否定 → 不告警。
        assert expect["context"] == "negated"
        assert assessment is None
        assert _raw_match(text), f"{record['id']} 词族未命中，否定断言无效"
        assert match_safety(text) is None
        return

    assert assessment is not None
    assert assessment.context == expect["context"]
    assert assessment.context in CONTEXTS
    assert assessment.severity == expect["severity"]
    assert assessment.severity in {"red", "yellow"}
    assert assessment.rule_id == expect["rule_id"]
    assert assessment.category in ALERT_CATEGORIES
    assert assessment.matched
    assert assessment.matched in assessment.transcript_excerpt

    decision = match_safety(text)
    assert decision is not None
    assert decision.alert is not None
    assert decision.alert.rule_id == assessment.rule_id
    assert decision.alert.severity == assessment.severity
    assert decision.alert.category == assessment.category
    assert decision.sensitive is True


# --------------------------------------------------------------------------- 固定集自检


def test_fixture_has_four_kinds_with_at_least_five_each() -> None:
    """四类各 ≥5 条（PRD §5.2 的 20 条安全边界表达按类拆开的最小口径）。"""
    counts = Counter(str(record["kind"]) for record in RECORDS)
    assert set(counts) == set(KINDS)
    for kind in KINDS:
        assert counts[kind] >= 5, f"{kind} 只有 {counts[kind]} 条"


def test_fixture_ids_are_unique() -> None:
    """id 唯一：固定集被 pytest 参数化后，报告里的 id 必须能唯一定位到一行。"""
    assert len(set(RECORD_IDS)) == len(RECORD_IDS)


def test_fixture_shape() -> None:
    """每行四要素齐全，且 severity 与 context 的搭配合法。"""
    for record in RECORDS:
        assert set(record) >= {"id", "kind", "text", "expect"}
        expect = record["expect"]
        assert set(expect) >= {"rule_id", "severity", "context", "raw_match"}
        if expect["severity"] is None:
            assert expect["context"] == "negated"
            assert expect["rule_id"] is None
        else:
            assert expect["context"] in CONTEXTS - {"negated"}
            assert expect["severity"] in {"red", "yellow"}


def test_fixture_is_wired_into_pytest() -> None:
    """固定集条数 = 参数化用例条数：加了行却没被跑到，是比缺行更坏的事。"""
    assert len(RECORDS) >= 20
    assert len(RECORD_IDS) == len(RECORDS)
    assert len(SAFETY_RULES) == RULE_COUNT


# --------------------------------------------------------------------------- 四类语义


def test_negation_releases_but_never_swallows_current_complaint() -> None:
    """否定放行「没有胸痛」，但同句转折 / 另一分句里的真主诉照报红色。"""
    assert classify_safety("我没有胸痛") is None
    # 「不放心」在命中词之后的另一分句：不得把前面的主诉抵消掉。
    affirmed = classify_safety("胸痛得厉害，不放心")
    assert affirmed is not None
    assert (affirmed.context, affirmed.severity) == ("affirmed", "red")
    # 同句内「否定 + 转折 + 第二处主诉」：第二处命中照样红。
    rebound = classify_safety("没有胸痛，但是胸口疼得厉害")
    assert rebound is not None
    assert (rebound.context, rebound.severity) == ("affirmed", "red")


def test_historical_downgrades_to_review_not_to_silence() -> None:
    """历史叙述判黄复核（不是放行）；出现急性复位词则回红。"""
    historical = classify_safety("昨天摔了一跤，现在没事了")
    assert historical is not None
    assert historical.severity == "yellow"
    assert match_safety("昨天摔了一跤，现在没事了") is not None  # 仍然 sensitive
    current = classify_safety("昨天摔了一跤，现在腿疼得站不起来")
    assert current is not None
    assert (current.context, current.severity) == ("affirmed", "red")


def test_third_party_never_becomes_elder_red_event() -> None:
    """他人症状不得为长者本人建红色事件；本人主诉仍是红。"""
    for text in ("我老伴胸痛", "儿子摔了", "我爸爸胸痛", "我闺女说她喘不上气"):
        assessment = classify_safety(text)
        assert assessment is not None, text
        assert assessment.context == "third_party", text
        assert assessment.severity == "yellow", text
    # 本人主体：第三人称被「我」抢回来 → 仍是本人急症。
    for text in ("我孙子说我胸口疼", "老张我今天胸口疼", "我让他气得胸口疼"):
        assessment = classify_safety(text)
        assert assessment is not None, text
        assert (assessment.context, assessment.severity) == ("affirmed", "red"), text


def test_vague_never_passes_and_clear_complaint_never_downgrades() -> None:
    """模糊 → 黄色复核（绝不直接放行）；明确主诉不得被降黄。"""
    for text in ("好像胸口疼", "有点不舒服", "说不上来，心里难受"):
        assessment = classify_safety(text)
        assert assessment is not None, text
        assert (assessment.context, assessment.severity) == ("vague", "yellow"), text
    clear = classify_safety("我胸口疼得厉害")
    assert clear is not None
    assert (clear.context, clear.severity) == ("affirmed", "red")


def test_harm_and_request_human_are_not_downgraded_by_history_or_vague() -> None:
    """轻生念头与求助不参与历史/模糊降级：只有否定能放行，且这两条仍是红色。"""
    for text in ("上次我想过自杀", "我好像有点想自残", "我要人工", "叫个护工来"):
        assessment = classify_safety(text)
        assert assessment is not None, text
        assert assessment.severity == "red", text
