"""L0 · 急症/敏感一票否决（AGENTS §5 高风险 / §6 安全规则优先；PRD FR-DLG-01 / FR-DLG-07）。

``app/router/safety.py`` 规则条数：**3**；加上 ``rules.py`` 固定意图 **7** 条，
两文件合计 **10** 条 ≤ 10 条硬上限（AGENTS §7；只做减法，新能力一律并进现有三条，
不许长出第 11 条）。三条对应急症/敏感入口：急症躯体症状、自伤/他伤、明确要求人工。

语义（S-D6-2：急症不进 LLM，直接人工分支；调用方对 intent='alert' 一律短路，
不进 L1/L2——AGENTS §6「低层不得被高层覆盖」）：

===============  ========================  ==========================================
rule_id          类型（子串正则）          示例（正例 → 命中；近似 → 不得命中）
===============  ========================  ==========================================
safety_emergency  急症躯体症状关键词        胸口疼得厉害 ✓｜坠床 ✓｜血止不住 ✓｜
                  （十类词族合并进这一条）  认不得人 ✓｜嘴歪说话不清 ✓｜起疹子 ✓｜
                                            头晕站不稳 ✓｜眼前发黑 ✓｜
                                            近似：昨晚睡得挺好 ✗
safety_harm       自伤/他伤关键词           不想活了 ✓｜有人打我 ✓｜近似：不想活动了 ✗
safety_request_   明确要求人工/护工/医护    我要人工 ✓｜叫个护工来 ✓｜
human                                       近似：公园有个人工湖 ✗
===============  ========================  ==========================================

工程口径
- 词表为**写死在代码**的关键词/可编译正则（不进 prompt、不进配置；AGENTS §5）。
  严重度阈值同理：红/黄由本文件常量决定，不由模型或配置决定。
- 高召回优先：任一关键词在全文任意位置命中即告警（非全句锚定）。宁可误报给
  人工确认，也不放过真急症——评测门「敏感召回率 100%」。
- **语境四分类（FR-DLG-07，本次 P0）**：命中之后先看命中处**所在分句**的语境，
  再决定严重度。此前「任何否定一律照报」的口径是错的：它让「我没有胸痛」直接
  派红色急症工单。四类口径与出口：

  ==========  ====================================  ==============================
  语境        识别（只看命中词所在分句的前缀）      出口
  ==========  ====================================  ==============================
  否定        没有/没/不觉得/并不…紧贴或在前 10 字   不告警（交 L1/L2 正常对话）
  历史        上次/上回/昨天/去年/前年…            黄色复核（severity=yellow）
  他人主体    老伴/儿子/孙子/他/她…作**分句主语**   黄色复核（不当成本人急症）
  模糊        好像/有点/可能/说不上来…             黄色复核
  其余        当下、本人、明确                      红色急症（severity=red）
  ==========  ====================================  ==============================

  三个关键取舍（都是踩过的坑，别改回去）：
  ① 否定窗口**只取命中词之前的同一分句**：这样「胸痛得厉害，不放心」后面的
     「不」不会把前面的主诉抵消掉——否定词在命中之后属于另一分句，不参与判定。
  ② 单字「不」只在**紧贴命中词**时才算否定（「不觉得喘不上气」），且不把
     「动不动」这类「不+动词」当否定（否则「他动不动就打人」会被误吞）。
  ③ 历史语境只要出现「现在/越来越/疼得厉害/起不来」这类**急性复位词**就升红：
     「昨天摔了一跤，现在腿疼得站不起来」仍是当下急症（主诉当前不适走安全侧）。
- 少量极窄的负向排除继续保留（``不想活动了``、``打我电话``、``人工湖``）。
- 本模块**纯文本、纯函数、无 I/O、无配置依赖**：不读 Settings、不记日志、
  不打 trace；`route_ms` 归编排层观测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.router.decision import LAYER_L0, RouteDecision, SafetyHit

# 本文件规则条数：3（全 router 合计 10，≤10 上限 AGENTS §7）
RULE_COUNT = 3

# 一票否决统一 intent：调用方据此短路进人工告警分支。
ALERT_INTENT = "alert"

#: 严重度两值闭集，与 `work_orders.severity` 的 CHECK、`alert_service._resolve_severity`
#: 严格同口径。红=急症派人，黄=复核（历史叙述/他人主体/模糊主诉），绝不新增第三档。
SEVERITY_RED: Final[str] = "red"
SEVERITY_YELLOW: Final[str] = "yellow"

#: 语境分类（`SafetyAssessment.context`）。除 `affirmed` 外都是「不足以判红」的语境，
#: 其中 `negated` 直接放行，其余三类落黄色复核。
CONTEXT_AFFIRMED: Final[str] = "affirmed"
CONTEXT_NEGATED: Final[str] = "negated"
CONTEXT_HISTORICAL: Final[str] = "historical"
CONTEXT_THIRD_PARTY: Final[str] = "third_party"
CONTEXT_VAGUE: Final[str] = "vague"
CONTEXTS: Final[frozenset[str]] = frozenset(
    {
        CONTEXT_AFFIRMED,
        CONTEXT_NEGATED,
        CONTEXT_HISTORICAL,
        CONTEXT_THIRD_PARTY,
        CONTEXT_VAGUE,
    }
)

# 语境判定开关名（写进 `SafetyRuleSpec.context_checks`，按规则关掉不该管的判定）
_CHECK_NEGATION: Final[str] = "negation"
_CHECK_HISTORICAL: Final[str] = "historical"
_CHECK_THIRD_PARTY: Final[str] = "third_party"
_CHECK_VAGUE: Final[str] = "vague"
_CHECKS_ALL: Final[frozenset[str]] = frozenset(
    {_CHECK_NEGATION, _CHECK_HISTORICAL, _CHECK_THIRD_PARTY, _CHECK_VAGUE}
)

# 急症词族：胸痛 / 呼吸窘迫 / 跌倒 / 出血 / 吞咽呛咳 / 认知急性变化 / 疑似中风 /
# 严重副作用 / 模糊躯体不适 / 头晕-晕厥前兆。**十族共用一条正则**——FR-DLG-01
# 要求的七类触发词全部落在 safety_emergency 这一条里，规则条数因此仍是 3。
_EMERGENCY_PATTERN = (
    # ① 胸痛族
    r"胸痛|胸口疼|胸口痛|胸疼|心口疼"
    r"|(?:左|右)?胸口[^，。!！?？]{0,8}(?:疼|痛)"
    # ② 呼吸窘迫族
    r"|喘不上气|喘不过气|上不来气|快上不来"
    r"|出气费劲|透不上来|憋得慌|气不够用"
    # ③ 跌倒族
    r"|摔倒|摔跤|摔了|摔下来|从床上摔|坠床"
    # ④ 出血族
    r"|出血|流血|血止不住|口子"
    # ⑤ 吞咽/呛咳族
    r"|咽不下|咽东西费劲"
    r"|(?:喝水|吃饭|进食|喂)[^，。!！?？]{0,2}呛"
    r"|呛到|呛着|被呛"
    # ⑥ 认知急性变化族
    r"|认不得|糊涂"
    # ⑦ 疑似中风族（FR-DLG-01 七类之一，原先缺）：口角歪斜/言语不清/单侧无力
    r"|中风|脑梗|脑出血|脑溢血"
    r"|嘴歪|嘴斜|口角歪|口水止不住"
    r"|说话不清|说话含糊|吐字不清|口齿不清|言语不清|说不成话"
    r"|半身不遂|半身无力|半边身子|半个身子|一边身子"
    r"|手脚不听使唤|胳膊抬不起来|手臂抬不起来|腿抬不起来|一侧无力|单侧无力"
    # ⑧ 严重副作用族（FR-DLG-01 七类之一，原先缺）：皮疹/风团/全身瘙痒/口唇肿胀
    r"|皮疹|疹子|红疹|风团|起红点|浑身发痒|全身发痒|痒得难受"
    r"|脸肿|嘴唇肿|嘴肿|喉咙肿"
    # ⑨ 模糊躯体不适族：**只在被模糊词修饰时**才算命中（「有点不舒服」「说不上来
    # 心里难受」）。裸的「我很难受」不进 L0——那是情绪表达，交 L1/L2 澄清后再说，
    # 否则每天的抱怨都会变成黄色工单（噪音会把复核队列淹掉）。
    r"|(?:好像|有点像|有点|有点儿|说不上来|说不清|不太确定|不确定|似乎|大概|隐隐|怕是)"
    r"[^。!！?？\n]{0,6}(?:不舒服|难受|发虚|没劲儿|没劲|使不上劲)"
    # ⑩ 头晕/晕厥前兆族（FR-HLT-03；2026-11-13 task-43）：长者说「我有点头晕」此前
    # 落到 L2 主诉路径，当天采集已结束时只回一句「这次就问到这儿」——既无「建议问
    # 医生」也无人工告警。头晕/眩晕/头昏是跌倒与晕厥的先兆，按安全侧收；「眼前发黑」
    # 「站不稳」「站不住」是晕厥前兆；「心慌/心悸/胸口闷」是心血管侧常见主诉。
    # 裸「闷」「晕」不进表（「心里闷得慌」「我晕车」这类单独走会误伤），只认复合词。
    r"|头晕|眩晕|头昏|发晕"
    r"|眼前发黑|眼前一黑|两眼发黑"
    r"|站不稳|站不住"
    r"|心慌|心悸"
    r"|胸口闷|心口闷|胸闷"
)

# 自伤/他伤词族。括号里是唯一的三处窄负向排除：
#   不想活(?!动) —— 排除「不想活动了」；打我(?!电话|手机) —— 排除「打我电话叫我来」；
#   有人打(?!门|电话) —— 排除「有人敲门/打电话」。
_HARM_PATTERN = (
    r"自杀|自残|自伤|自[我己]伤害?"
    r"|不想活着?(?!动)(?!跟)"
    r"|活不下去了?"
    r"|伤害自己|害自己"
    r"|打我(?!电话|手机)"
    r"|掐死我|掐我|咬我|拧我"
    r"|有人打(?!门|电话)|打了我"
    r"|打人|打死人|杀了我|杀了他|砍了他|勒死"
    r"|拿拐杖打|要打他|就动手|要动手"
)

# 明确要求人工词族：转人工/要人工等固定短语，以及「叫/找/要/换/请/喊 + 护工/医生/护士/工作人员」。
# 「要人工」前置排除 「不」；「人工湖」类噪音靠动词前缀约束压掉。
_REQUEST_HUMAN_PATTERN = (
    r"(?<!不)要人工|需要人工|找人工|转人工|人工服务"
    r"|(?:叫|找|要|换|请|喊)(?:个|一?位|来|一下)?(?:护工|医生|护士|工作人员)"
    r"|叫(?:一?个)?人(?:来|过来)?"
    r"|来(?:个|一?位)?人"
)

# --------------------------------------------------------------------------- 语境判定
# 分句切分：否定/历史/他人主体/模糊都**不跨分句**判定。「我没有胸痛，但是胸口疼」
# 里前一个否定不能把后一个主诉一起吞掉，靠的就是这条。
_CLAUSE_SEP = re.compile(r"[，,。；;！!？?\n]+")

# 紧贴命中词的否定（含「不觉得/不太/不怎么」这类感知否定）。
# 单字「不」只在此处生效：`不$` 要求它**紧贴**命中词，「动不动就打人」里
# 「不+动」不在命中词前一个字符，因此不会被误判成否定。
_NEGATION_ADJACENT = re.compile(r"(?:不觉得|不感觉|不觉着|不太|不怎么|并不|没有|没|无|不)$")
# 强否定：可隔几个字（「我没有明显的胸痛」）。刻意不含单字「无/未」——「未来」
# 「无数」这类词会让真主诉被静默吞掉，代价远大于收益。
_NEGATION_STRONG = re.compile(r"(?:并没有|没有|从来没|从没|不曾|并不|并未|并没)")
# 转折词：否定之后又转折 = 主诉复位，不按否定处理。
_ADVERSATIVE = re.compile(r"(?:但|可是|不过|然而|却)")

# 历史语境标记（同分句、命中词之前）。刻意**不含**「刚才/刚」：那是当下事件。
_HISTORICAL = re.compile(
    r"(?:上次|上回|上一回|上一次|那回|那次|那时候|当时|上个月|上个礼拜|上周|上星期"
    r"|昨天|昨晚|前天|前些天|当年|去年|前年|大前年|头年|多年前|几年前"
    r"|之前|以前|从前|过去|曾经|原来|小时候|年轻时|早年)"
)
# 急性复位词：历史叙述里一旦出现「现在…疼得厉害/起不来」就说明是当下急症，
# 升回红色（FR-DLG-07 后半句「主诉当前不适仍走安全侧」）。
_CURRENT_WAKE = re.compile(
    r"(?:现在|这会儿|目前|正在|还在|又|越来越|忽然|突然|一下子|刚)"
    r"[^，,。；;！!？?\n]{0,6}"
    r"(?:疼得|痛得|厉害|加重|越来越|止不住|不停|受不了|不行了|起不来|站不起来|动不了)"
)

# 他人主体（FR-DLG-07：他人症状不得为长者本人建红色事件）。
# 两重约束才判他人：① 人称词必须是**分句主语**（之前只允许「我/我家/的」这类限定语，
# 「我让他气得胸口疼」不满足 → 仍按本人处理）；② 人称词之后到命中词之间不得再出现
# 「我/咱」把人称拉回本人（「我孙子说我胸口疼」→ 本人）。
_PERSON = re.compile(
    r"(?:老伴儿?|老头子?|老?[爸妈]|爹|娘|儿子|闺女|女儿|儿媳妇?|女婿|孙子|孙女"
    r"|外孙|外孙女|哥哥|弟弟|姐姐|妹妹|亲戚|邻居|护工|保姆|王姐|老张|老王|老李|朋友"
    r"|(?<!其)他|她|别人|人家)"
)
_SUBJECT_PREFIX = re.compile(r"^(?:我|俺|咱|我们|咱们)?(?:家|们)?(?:的)?$")
_FIRST_PERSON = re.compile(r"[我咱俺]")

# 模糊语境标记（命中词之前，或命中词自身以它开头）。
# 刻意不含「感觉/觉着」——「我感觉胸口疼」是直接主诉，不是模糊（降黄会漏掉红档）。
_VAGUE = re.compile(
    r"(?:好像|有点像|有点|有点儿|说不上来|说不清|不确定|不太确定|似乎|大概|隐隐"
    r"|会不会|怕是|可能)"
)


@dataclass(frozen=True, slots=True)
class SafetyRuleSpec:
    """一条安全一票否决规则：命中即 alert，`rule_id` 随决策原样上抛给告警链路。"""

    rule_id: str
    description: str
    pattern: re.Pattern[str]
    #: 告警类别（迭代 8 N01）：`emergency` / `harm` / `request_human`。下游
    #: （alert_service 的工单类型、告警分级）读它，而不是去解析 `rule_id` 字符串。
    category: str
    #: 命中即红色时的严重度；`affirmed` 之外的三类语境由 `_context_severity` 落黄。
    severity: str = SEVERITY_RED
    #: 本规则参与哪些语境判定。没列出的判定一律不生效（见下方三条规则的注释）。
    context_checks: frozenset[str] = _CHECKS_ALL
    #: 他人主体判定是否只对「自伤词族」生效：他伤/被打的表述（「有人打我」
    #: 「他动不动就打人」）里出现「他」恰恰是**本人受害**的证据，不能判成他人。
    subject_self_harm_only: bool = False


SAFETY_RULES: tuple[SafetyRuleSpec, ...] = (
    SafetyRuleSpec(
        rule_id="safety_emergency",
        description=("急症躯体症状：胸痛/呼吸窘迫/跌倒/出血/吞咽/认知急性变化/疑似中风/严重副作用"),
        pattern=re.compile(_EMERGENCY_PATTERN),
        category="emergency",
        context_checks=_CHECKS_ALL,
    ),
    SafetyRuleSpec(
        rule_id="safety_harm",
        description="自伤/他伤：自伤意图词族 + 伤害他人/被他人伤害词族",
        pattern=re.compile(_HARM_PATTERN),
        category="harm",
        # 不做历史/模糊降级：轻生念头的任何表述都必须按急症处理，「上次想过」
        # 「好像有点想」也不例外；只保留否定（「我没有想自杀」）与他人主体。
        context_checks=frozenset({_CHECK_NEGATION, _CHECK_THIRD_PARTY}),
        subject_self_harm_only=True,
    ),
    SafetyRuleSpec(
        rule_id="safety_request_human",
        description="明确要求人工：转人工/要人工/叫护工/找医生等",
        pattern=re.compile(_REQUEST_HUMAN_PATTERN),
        category="request_human",
        # 只做否定排除（「我没有要人工」）。要求人工本身是长者主动求助，
        # 不分历史/他人语境——替老伴叫人也是正当求助。
        context_checks=frozenset({_CHECK_NEGATION}),
    ),
)

assert len(SAFETY_RULES) == RULE_COUNT, "safety.py 规则条数与顶部注释必须一致"

#: 命中片段上下文半径：命中处**前后各取 8 字**。
#: 刻意不取整句：这段切片会随 `SafetyHit` 进 `risk_events.detail`，多留字数就是
#: 多留隐私（AGENTS §4.10）；8 字足够人工复核「是不是这句」，又不足以还原整句话。
_EXCERPT_RADIUS = 8


def _excerpt(text: str, match: re.Match[str]) -> str:
    """截命中处前后各 `_EXCERPT_RADIUS` 字；脱敏在落库侧做，不在这层做。

    为什么脱敏不在这里：本模块自述「纯函数、无 I/O、无配置依赖」，脱敏工具住在
    `app/core/`，引进来就把纯文本层接到了配置/日志组件上。这里只产出**可能是敏感**
    的短切片，写库前必须过 `app/core/` 的脱敏（`alert_service` 的职责）。
    """
    start = max(0, match.start() - _EXCERPT_RADIUS)
    end = min(len(text), match.end() + _EXCERPT_RADIUS)
    return text[start:end]


def _clause_start(text: str, index: int) -> int:
    """返回 `index` 所在分句的起点（`index` 落在分隔符后面时即其本身）。"""
    begin = 0
    for sep in _CLAUSE_SEP.finditer(text):
        if sep.start() >= index:
            break
        begin = sep.end()
    return begin


def _negated(prefix: str) -> bool:
    """命中词之前（同一分句）是否有否定词，且其后没有被转折词复位。"""
    tail = prefix.rstrip()
    if _NEGATION_ADJACENT.search(tail):
        return True
    window = tail[-10:]
    strong = _NEGATION_STRONG.search(window)
    if strong is None:
        return False
    return not _ADVERSATIVE.search(window[strong.end() :])


def _third_party(prefix: str, matched: str, spec: SafetyRuleSpec) -> bool:
    """人称词是否充当**本分句主语**、且命中词说的是那个人而不是长者本人。

    逐个候选试而不是只看最后一个：「我闺女说她喘不上气」里最后一个候选是「她」，
    但它前面还有「我闺女说」，只有「闺女」站在主语位——按最后一个判会把真·他人
    主体误判成本人急症。
    """
    if spec.subject_self_harm_only and "自" not in matched:
        return False
    for person in _PERSON.finditer(prefix):
        if not _SUBJECT_PREFIX.match(prefix[: person.start()].strip()):
            continue
        if _FIRST_PERSON.search(prefix[person.end() :]) is None:
            return True
    return False


def _context_of(text: str, match: re.Match[str], spec: SafetyRuleSpec) -> str:
    """判定这次命中属于哪一种语境（FR-DLG-07 四分类 + 当下确认）。"""
    begin = _clause_start(text, match.start())
    prefix = text[begin : match.start()]
    matched = match.group(0)
    checks = spec.context_checks

    if _CHECK_NEGATION in checks and _negated(prefix):
        return CONTEXT_NEGATED
    if (
        _CHECK_HISTORICAL in checks
        and _HISTORICAL.search(prefix)
        # 历史叙述 + 急性复位词 → 落回「当下确认」（红色），不降黄。
        and _CURRENT_WAKE.search(text) is None
    ):
        return CONTEXT_HISTORICAL
    if _CHECK_THIRD_PARTY in checks and _third_party(prefix, matched, spec):
        return CONTEXT_THIRD_PARTY
    if _CHECK_VAGUE in checks and (_VAGUE.search(prefix) or _VAGUE.search(matched)):
        return CONTEXT_VAGUE
    return CONTEXT_AFFIRMED


_CONTEXT_SEVERITY: Final[dict[str, str]] = {
    CONTEXT_AFFIRMED: SEVERITY_RED,
    CONTEXT_HISTORICAL: SEVERITY_YELLOW,
    CONTEXT_THIRD_PARTY: SEVERITY_YELLOW,
    CONTEXT_VAGUE: SEVERITY_YELLOW,
}


@dataclass(frozen=True, slots=True)
class SafetyAssessment:
    """一次安全命中的判定结果：谁命中、多严重、为什么、命中在哪。

    `RouterDecision` 只装得下 `SafetyHit` 的五个字段，`context` 是三分类的直接依据，
    因此单独成一个值对象；`match_safety` 负责把它翻译成 `RouteDecision`。
    """

    rule_id: str
    category: str
    severity: str
    context: str
    matched: str
    transcript_excerpt: str


def classify_safety(text: str) -> SafetyAssessment | None:
    """安全判定的**唯一实现**：按规则优先级扫描，返回首个「不足以放行」的命中。

    与 `match_safety` 的分工：这里是纯判定（含四类语境），`match_safety` 只做
    `SafetyAssessment → RouteDecision` 的适配，绝不另写一套正则。

    - 否定语境 → 当次命中**丢弃**，继续在同一句里找下一个命中（「没有胸痛，
      但是胸口疼得厉害」第二处命中照样告警）；
    - 历史/他人主体/模糊 → 黄色复核；其余 → 红色急症；
    - 全部命中都被否掉 → None，交 L1/L2 正常对话处理。
    """
    if not text:
        return None
    for spec in SAFETY_RULES:
        for match in spec.pattern.finditer(text):
            context = _context_of(text, match, spec)
            if context == CONTEXT_NEGATED:
                continue
            return SafetyAssessment(
                rule_id=spec.rule_id,
                category=spec.category,
                severity=_CONTEXT_SEVERITY[context],
                context=context,
                matched=match.group(0),
                transcript_excerpt=_excerpt(text, match),
            )
    return None


def match_safety(text: str) -> RouteDecision | None:
    """安全一票否决扫描（`match_l0` 的第一步，先于 rules.py 一切固定意图）。

    命中返回 ``layer='L0'`` + ``intent='alert'`` + ``sensitive=True`` 的决策，
    `rule_id` 标明告警类别，``alert`` 带上这次命中的**证据**（`SafetyHit`：
    类别、严重度、命中片段、±8 字上下文切片）；调用方据此**短路**，不进 L1/L2。
    严重度可能是 `red`（急症派人）或 `yellow`（复核），两者都已在下游
    `alert_service` 的闭集内——`intent` 仍是 `alert`，因为 `INTENT_WHITELIST`
    没有「复核」这个值，且两者都必须短路，不能落到 L1/L2。
    未命中返回 None（交由 rules.py 继续）。

    ``SafetyHit.occurred_at`` 留空：本模块自述「纯文本、纯函数、无 I/O」，
    取时钟属 I/O，由编排层（`dialogue_runtime`）补。
    """
    assessment = classify_safety(text)
    if assessment is None:
        return None
    return RouteDecision(
        elder_id=None,
        thread_id=None,
        layer=LAYER_L0,
        rule_id=assessment.rule_id,
        intent=ALERT_INTENT,
        sensitive=True,
        alert=SafetyHit(
            rule_id=assessment.rule_id,
            category=assessment.category,
            severity=assessment.severity,
            matched=assessment.matched,
            transcript_excerpt=assessment.transcript_excerpt,
        ),
    )


__all__ = [
    "ALERT_INTENT",
    "CONTEXTS",
    "CONTEXT_AFFIRMED",
    "CONTEXT_HISTORICAL",
    "CONTEXT_NEGATED",
    "CONTEXT_THIRD_PARTY",
    "CONTEXT_VAGUE",
    "RULE_COUNT",
    "SAFETY_RULES",
    "SEVERITY_RED",
    "SEVERITY_YELLOW",
    "SafetyAssessment",
    "SafetyRuleSpec",
    "classify_safety",
    "match_safety",
]
