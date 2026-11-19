"""机构知识**答案出口**的输出校验闸（FR-KNW-01 / FR-KNW-03 / FR-KNW-05，第四轮 W3）。

> 与 `app/core/knowledge_guard.py` 的区别，一句话：
> **那个管输入（检索到的正文里混了注入就整条丢弃），这个管输出（答案没点来源 / 空检索编了
> 具体时间 / 多篇命中冲突就改写或降级）。** 两者不是同一个能力，也不许合并成一套。

## 为什么要有这一层

`app/agents/companion.py` 的 system prompt 第 6 条已经要求「回答机构信息时必须点明来源与
生效日期」，`tests/test_knowledge_e2e.py` 里那条用例名也自认
`test_business_conflict_is_prompt_rule_only`——也就是说：**规则只写在提示词里，输出层零校验**。
模型不听话就没有第二道闸。本模块补的就是这道闸。

## 口径（写死，别在这里做业务裁决）

1. **只做校验与改写，不做检索**：本文件**不碰数据库、不发网络请求**，纯函数可单测；
   检索与版本逻辑仍归 `app/services/knowledge_service.py`，这里不复制第二套。
2. **缺来源 → 降级**（不删答案里的判断，但明确告诉长者「可能过期，问前台确认」）。
3. **检索为空（`reason == "no_match"`）→ 固定话术**：模型不许在「库里确实没有」时编具体
   几点开饭、多少钱这类时间与数字。
4. **多条命中的仲裁顺序：权威级 > 生效日期**。权威级的排列直接沿用
   `app.models.knowledge.KNOWLEDGE_AUTHORITY_LEVELS`（与 `knowledge_service` 排序用的
   是同一个元组，避免两套口径）；同权威级时取 `effective_from` 更新的那条。
5. **不误伤闲聊**：闲聊里出现「我今年 88 岁」这类数字是正常的，只有**本轮确实做过知识检索**
   且**问题本身在问机构事务**时才可能触发「查不到」话术（见 `looks_institutional`）。
6. 文案不得出现任何诊断/分级口径（AGENTS §4.4）；`tests/test_knowledge_output_guard.py` 用
   `app.services.alert_aggregation.assert_attention_wording` 反向验证本模块的三段固定话术。

## 与提示词的关系

提示词里的规则**保留**，只是不再**只**靠它（计划 §3.2）。开关
`settings.knowledge_output_guard_enabled`（环境变量 `KNOWLEDGE_OUTPUT_GUARD_ENABLED`）
默认 `True`；关掉后行为与加这层之前**完全一致**，用于对比与回滚。

## 已知限制（可直接引用进验收报告）

1. **「来源名」= 文档 `title`**：`KnowledgeHit` 没有独立的 source 字段；标题本身含糊时
   （如「入住须知（二）」）点出来对长者的辨识度有限，要更准得给 knowledge 表加字段
   （迁移范围，本轮不做）。
2. **空检索话术依赖 `_INSTITUTION_KEYWORDS`**：不用它会把闲聊里的数字（「我今年 88 岁」）
   当成编规定；用了则词表覆盖不到的机构问法会漏判。取舍是有意的——**宁可漏判（退回提示词
   兜底），不可误伤**。
3. **冲突判定按「多篇/多版本同时命中」粗判**：同一文档的不同 chunk 不算冲突，但两篇互补
   文档（探视规定 + 用餐规定）会被判为冲突并多追加一句依据行；只加说明、不删正文。
4. **缺生效日期显式说「没标」，不用「今天」顶替**：`KnowledgePick.effective_from` 用 `None`
   表达未标注，`arbitration_speech` / `arbitration_note` 走未标注模板（「但没标生效日期，
   我建议您问前台确认」）；把今天当生效日期念给长者属于编造事实，比降级更坏。
5. **中文数字日期（「三月一日」）不解析**，会被判「未点来源」而降级：自然语言日期有歧义
   （「三月一日」也可能是「三月，一日」），误判成引用会把没点来源的回答放过去；**保守降级
   是有意的方向**——宁可多说一句「可能过期，建议问前台」，不可漏掉 AC-43 的拦截。要覆盖
   这种写法得先定一份日期语料与消歧规则，不在本轮范围。
"""

from __future__ import annotations

import datetime
import re
import zoneinfo
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.models.knowledge import KNOWLEDGE_AUTHORITY_LEVELS

#: 检索为空时的固定话术：**只说「查不到」+ 去问前台**，不给任何时间/数字/金额。
MISSING_ANSWER_SPEECH: Final[str] = (
    "这个规定我这儿没查到准话，您别急，我帮您记下来问前台，问清楚再告诉您。"
)

#: 有检索结果但答案没点明来源与生效日期时的降级话术（计划 §3.2 原文口径）。
STALE_SOURCE_SPEECH: Final[str] = "我查到的是机构信息，可能过期，建议问前台确认。"

#: 冲突时按仲裁结果覆盖的话术：必须同时带出**来源名**与**生效日期**（AC-43）。
ARBITRATION_SPEECH_TEMPLATE: Final[str] = (
    "这条以《{title}》为准（{authority}，{effective} 生效）；"
    "机构信息可能有调整，您要是拿不准，我再帮您问前台。"
)

#: 仲裁胜出的那条**没标生效日期**时用这句：宁可说要问前台，也绝不拿「今天」顶生效日期。
ARBITRATION_UNDATED_SPEECH_TEMPLATE: Final[str] = (
    "这条以《{title}》为准（{authority}），但没标生效日期，我建议您问前台确认。"
)

#: 有来源、但本轮多篇命中之间存在版本差异时追加的依据行（保留模型正文，不整段覆盖）。
ARBITRATION_NOTE_TEMPLATE: Final[str] = "（依据：《{title}》，{authority}，{effective} 生效）"

#: 同上，但胜出条目没标生效日期时的依据行。
ARBITRATION_UNDATED_NOTE_TEMPLATE: Final[str] = "（依据：《{title}》，{authority}，生效日期未标注）"

#: 模型自己承认查不到的说法。命中这些就认为它已经在「如实说不知道」。
MISSING_ANSWER_MARKERS: Final[tuple[str, ...]] = (
    "查不到",
    "没查到",
    "没有查到",
    "查不到准话",
    "不清楚",
    "没有这条",
    "没有相关",
    "不知道",
    "没法确认",
    "无法确认",
)

#: 「具体时间 / 数字」的判据：ASCII 数字、时刻写法、时段词、星期词。
_CONCRETE_FACT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\d"),
    re.compile(r"[上下中晚早]\s*午"),
    re.compile(r"(?:早上|上午|中午|下午|傍晚|晚上|夜里|凌晨)"),
    re.compile(r"周[一二三四五六日天]|星期[一二三四五六日天]"),
    re.compile(r"[一二三四五六七八九十]+点(?:半|钟)?"),
)

#: 问题里出现这些词，才算「在问机构事务」——闲聊里的数字不触发「查不到」话术。
_INSTITUTION_KEYWORDS: Final[tuple[str, ...]] = (
    "规定",
    "制度",
    "探视",
    "探望",
    "探访",
    "用餐",
    "开饭",
    "吃饭",
    "包餐",
    "伙食",
    "活动",
    "收费",
    "费用",
    "价格",
    "多少钱",
    "流程",
    "预约",
    "登记",
    "入住",
    "退房",
    "接送",
    "作息",
    "时间表",
    "洗澡",
    "理发",
    "家属",
    "几点",
    "什么时候",
)

_SHANGHAI: Final[zoneinfo.ZoneInfo] = zoneinfo.ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class KnowledgePick:
    """仲裁结果：选了哪一条、为什么是它（`reason` 要能直接念给人听）。

    `effective_from` 为 `None` 表示**这条文档没标生效日期**——此时一律显式说「没标」，
    不许拿「今天」顶上去（把今天当生效日期念给长者，比降级更坏：是编出来的事实）。
    """

    citation: Any
    title: str
    authority_level: str
    effective_from: datetime.datetime | None
    reason: str

    @property
    def effective_date(self) -> str:
        """生效日期（`YYYY-MM-DD`，按 Asia/Shanghai 展示；存储仍是 UTC）；未标注返回空串。"""
        return format_effective_date(self.effective_from)

    @property
    def has_effective_date(self) -> bool:
        return self.effective_from is not None


@dataclass(frozen=True, slots=True)
class KnowledgeGuardOutcome:
    """输出层闸门的结论：`ok` / `disabled` / `downgraded` / `missing` / `arbitrated`。"""

    spoken_text: str
    outcome: str
    detail: str = ""

    @property
    def changed(self) -> bool:
        return self.outcome in {"downgraded", "missing", "arbitrated"}


def format_effective_date(moment: datetime.datetime | None) -> str:
    """生效日期展示口径：UTC 存储 → `Asia/Shanghai` 日期（AGENTS §8「时间」）。"""
    if not isinstance(moment, datetime.datetime):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return moment.astimezone(_SHANGHAI).date().isoformat()


def _title_of(citation: Any) -> str:
    return str(getattr(citation, "title", "") or "").strip()


def _authority_of(citation: Any) -> str:
    return str(getattr(citation, "authority_level", "") or "").strip()


def _effective_of(citation: Any) -> datetime.datetime | None:
    value = getattr(citation, "effective_from", None)
    return value if isinstance(value, datetime.datetime) else None


def _document_key(citation: Any) -> tuple[str, str]:
    """同一篇文档的哪一版：优先用 `document_id`，退化时用标题 + 版本号。"""
    document_id = getattr(citation, "document_id", None)
    version = str(getattr(citation, "version", "") or "")
    if document_id is not None:
        return (str(document_id), version)
    return (_title_of(citation), version)


def _authority_rank(authority_level: str) -> int:
    """权威级越小越优先，顺序与 `knowledge_service` 的排序同源。"""
    try:
        return KNOWLEDGE_AUTHORITY_LEVELS.index(authority_level)
    except ValueError:
        return len(KNOWLEDGE_AUTHORITY_LEVELS)


def _effective_epoch(citation: Any) -> float:
    moment = _effective_of(citation)
    if moment is None:
        return float("-inf")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return moment.timestamp()


#: 生效日期在答案里的三种合法写法（都归一化成 `(年, 月, 日)` 再比）。
_DATE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(?<!\d)(\d{4})\.(\d{1,2})\.(\d{1,2})(?!\d)"),
    # 斜杠写法（2026/03/01、2026/3/1）：中文语境里并不比点号少见，少这一种就是多一批误降级。
    re.compile(r"(?<!\d)(\d{4})/(\d{1,2})/(\d{1,2})(?!\d)"),
)

#: 归一化标题时丢掉的装饰：书名号、全半角括号、引号、标点与空白。
_TITLE_NOISE: Final[re.Pattern[str]] = re.compile(
    r"[《》〈〉「」『』“”‘’\"'（）()【】\[\]〔〕、，,。.；;：:！!？?·…—\-\s]+"
)

#: 标题尾巴上的括注（「（二）」「(试行)」）：做包含判定时去掉再比。
_TITLE_TRAILING_NOTE: Final[re.Pattern[str]] = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]\s*$")


def _date_tokens(text: str) -> set[tuple[int, int, int]]:
    """把文本里出现的日期（三种写法）统一成 `(年, 月, 日)` 集合。"""
    tokens: set[tuple[int, int, int]] = set()
    for pattern in _DATE_PATTERNS:
        for year, month, day in pattern.findall(text):
            tokens.add((int(year), int(month), int(day)))
    return tokens


def _normalize_title(title: str) -> str:
    return _TITLE_NOISE.sub("", title or "")


def _title_variants(title: str) -> tuple[str, ...]:
    """标题的两种形态：原样归一化 + 去掉尾部括注后归一化（「入住须知（二）」→「入住须知」）。"""
    raw = (title or "").strip()
    stripped = _TITLE_TRAILING_NOTE.sub("", raw).strip()
    variants = {_normalize_title(raw), _normalize_title(stripped)}
    return tuple(variant for variant in variants if len(variant) >= 2)


def assert_source_cited(text: str, citations: Sequence[Any]) -> bool:
    """答案里是否**同时**出现了来源名与生效日期（任意一条命中即可）。

    判定用放宽后的口径，避免把「其实答对了」的回答误降级成废话：

    - **日期四种写法都认**：`YYYY-MM-DD` / `YYYY年M月D日` / `YYYY.MM.DD` / `YYYY/MM/DD`，
      统一归一化成 `(年, 月, 日)` 再与文档的生效日期比；
    - **标题按归一化包含判定**：先把书名号、全半角括号、引号、标点与空白都去掉，
      再去掉标题尾巴上的括注（「入住须知（二）」→「入住须知」），答案里出现该片段即算命中
      ——不要求逐字带《》；
    - `citations` 为空 → 返回 `True`：本轮没有检索结果，就没有「引用」可核，
      该走 `is_missing_answer` 那条路，不能在这里判失败；
    - 只有标题没日期（或反之）→ `False`，这正是 AC-43 要挡的那种回答。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    text_dates = _date_tokens(text)
    if not text_dates:
        return False
    haystack = _normalize_title(text)
    for citation in citations or ():
        effective = format_effective_date(_effective_of(citation))
        if not effective:
            continue
        year, month, day = (int(part) for part in effective.split("-"))
        if (year, month, day) not in text_dates:
            continue
        if any(variant in haystack for variant in _title_variants(_title_of(citation))):
            return True
    return False


def is_missing_answer(text: str) -> bool:
    """答案是否已经在「如实说查不到」（命中 `MISSING_ANSWER_MARKERS` 之一）。"""
    if not isinstance(text, str):
        return False
    return any(marker in text for marker in MISSING_ANSWER_MARKERS)


def contains_concrete_fact(text: str) -> bool:
    """答案里有没有具体时间/数字。空检索时出现这些就等于在编规定。"""
    if not isinstance(text, str):
        return False
    return any(pattern.search(text) for pattern in _CONCRETE_FACT_PATTERNS)


def looks_institutional(query: str) -> bool:
    """这个问题是不是在问机构事务（决定「查不到」话术要不要触发）。"""
    if not isinstance(query, str):
        return False
    return any(keyword in query for keyword in _INSTITUTION_KEYWORDS)


def has_citation_conflict(citations: Sequence[Any]) -> bool:
    """本轮命中是否跨了多篇/多版本（跨了就需要仲裁，单一来源不算冲突）。"""
    keys = {_document_key(citation) for citation in citations or ()}
    distinct_ids = {key[0] for key in keys}
    if len(distinct_ids) > 1:
        return True
    return len(keys) > 1  # 同一篇文档命中两个版本 —— 也按冲突处理。


def arbitrate(candidates: Sequence[Any]) -> KnowledgePick | None:
    """冲突仲裁：**权威级 > 生效日期**（同权威级取生效日期更新者）。

    返回 `KnowledgePick`（带 `reason` 说明「按哪条、为什么」），空输入返回 `None`。
    """
    usable = [c for c in (candidates or ()) if _title_of(c)]
    if not usable:
        return None
    ordered = sorted(
        usable, key=lambda c: (_authority_rank(_authority_of(c)), -_effective_epoch(c))
    )
    winner = ordered[0]
    authority = _authority_of(winner) or "institution"
    effective = format_effective_date(_effective_of(winner))
    reason = f"权威级 {authority} 优先"
    if len(ordered) > 1:
        runner_up = ordered[1]
        if _authority_rank(_authority_of(runner_up)) == _authority_rank(authority):
            reason += f"；同权威级取生效日期更新者（{effective} 晚于 {format_effective_date(_effective_of(runner_up)) or '未标注'}）"
        else:
            reason += (
                f"；低于《{_title_of(runner_up)}》（{_authority_of(runner_up) or 'institution'}）"
            )
    return KnowledgePick(
        citation=winner,
        title=_title_of(winner),
        authority_level=authority,
        # 没有生效日期就是没有：绝不拿「今天」顶替（那是把假信息念给长者）。
        effective_from=_effective_of(winner),
        reason=reason,
    )


def arbitration_speech(pick: KnowledgePick) -> str:
    """把仲裁结果渲染成一句可播报的话（含来源名与生效日期；没标日期就明说没标）。"""
    authority = {
        "institution": "机构规定",
        "elder_memory": "您之前提过的偏好",
    }.get(pick.authority_level, pick.authority_level)
    if not pick.has_effective_date:
        return ARBITRATION_UNDATED_SPEECH_TEMPLATE.format(title=pick.title, authority=authority)
    return ARBITRATION_SPEECH_TEMPLATE.format(
        title=pick.title,
        authority=authority,
        effective=pick.effective_date,
    )


def arbitration_note(pick: KnowledgePick) -> str:
    """有来源时的依据行（追加在模型原文后面，不覆盖正文；没标日期就写「未标注」）。"""
    authority = "机构规定" if pick.authority_level == "institution" else pick.authority_level
    if not pick.has_effective_date:
        return ARBITRATION_UNDATED_NOTE_TEMPLATE.format(title=pick.title, authority=authority)
    return ARBITRATION_NOTE_TEMPLATE.format(
        title=pick.title,
        authority=authority,
        effective=pick.effective_date,
    )


def guard_answer(
    answer: str,
    *,
    query: str = "",
    citations: Sequence[Any] = (),
    missing: bool = False,
    enabled: bool = True,
) -> KnowledgeGuardOutcome:
    """答案出口的闸门：**只改写/降级，不改变路由与工具调用**。

    判定顺序（写死，别在调用方再排一遍）：
    1. 开关关闭 → 原文返回（`disabled`），行为与加这层之前完全一致；
    2. 本轮没有命中（`citations` 为空）：
       - `missing` 为真（上游 `reason == "no_match"`）、问题在问机构事务、答案里却出现
         具体时间/数字 → 换成 `MISSING_ANSWER_SPEECH`（`missing`）；
       - 其余情况原样放过（闲聊里的数字不算编规定）；
    3. 有命中：
       - 有冲突 → 仲裁；未点明来源与日期时用仲裁话术**覆盖**（`arbitrated`）；
       - 有冲突且已点明来源 → 原文 + 依据行（`arbitrated`）；
       - 无冲突但缺来源/日期 → 降级为 `STALE_SOURCE_SPEECH`（`downgraded`）；
       - 通过 → 原文（`ok`）。
    """
    text = answer if isinstance(answer, str) else str(answer or "")
    if not enabled:
        return KnowledgeGuardOutcome(spoken_text=text, outcome="disabled", detail="开关关闭")
    if not text.strip():
        return KnowledgeGuardOutcome(spoken_text=text, outcome="ok", detail="空文本")

    hits = tuple(citations or ())
    if not hits:
        if missing and looks_institutional(query) and contains_concrete_fact(text):
            return KnowledgeGuardOutcome(
                spoken_text=MISSING_ANSWER_SPEECH,
                outcome="missing",
                detail="检索 no_match 但答案给了具体时间/数字",
            )
        return KnowledgeGuardOutcome(spoken_text=text, outcome="ok", detail="本轮未命中知识")

    conflict = has_citation_conflict(hits)
    cited = assert_source_cited(text, hits)
    if conflict:
        pick = arbitrate(hits)
        if pick is None:
            return KnowledgeGuardOutcome(spoken_text=text, outcome="ok", detail="无可仲裁候选")
        if cited:
            return KnowledgeGuardOutcome(
                spoken_text=f"{text}{arbitration_note(pick)}",
                outcome="arbitrated",
                detail=f"{pick.reason}；原文已点明来源，只追加依据",
            )
        return KnowledgeGuardOutcome(
            spoken_text=arbitration_speech(pick),
            outcome="arbitrated",
            detail=pick.reason,
        )
    if not cited:
        return KnowledgeGuardOutcome(
            spoken_text=STALE_SOURCE_SPEECH,
            outcome="downgraded",
            detail="答案未同时给出来源名与生效日期",
        )
    return KnowledgeGuardOutcome(spoken_text=text, outcome="ok", detail="已点明来源与生效日期")


__all__ = [
    "ARBITRATION_NOTE_TEMPLATE",
    "ARBITRATION_SPEECH_TEMPLATE",
    "ARBITRATION_UNDATED_NOTE_TEMPLATE",
    "ARBITRATION_UNDATED_SPEECH_TEMPLATE",
    "MISSING_ANSWER_MARKERS",
    "MISSING_ANSWER_SPEECH",
    "STALE_SOURCE_SPEECH",
    "KnowledgeGuardOutcome",
    "KnowledgePick",
    "arbitrate",
    "arbitration_note",
    "arbitration_speech",
    "assert_source_cited",
    "contains_concrete_fact",
    "format_effective_date",
    "guard_answer",
    "has_citation_conflict",
    "is_missing_answer",
    "looks_institutional",
]
