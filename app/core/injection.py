"""提示注入三层拦截（迭代 9 计划 §4 W3）。

本模块只做**纯函数、无 I/O、无模型调用**的判定，不依赖 Laya / LLM，也不写库；
接线由编排层负责：

1. 拿到长者输入后先 ``guard(text, intent=...)``；``blocked`` 时输出
   :data:`FIXED_REFUSAL`，并写 ``audit_logs(action="injection_blocked",
   detail={"pattern_id": ..., "layer": ..., "trace_id": ...})``。
2. 模型输出播报/返回前调用 ``check_output(reply, elder_name=...)``；
   ``blocked`` 时同样替换成 :data:`FIXED_REFUSAL`。
3. 本模块无状态、无随机性，单测可 100% 覆盖。

三层职责：

=========  ==================================================  ==========
层         负责什么                                            入口
=========  ==================================================  ==========
input      五类注入（越权取数/角色劫持/工具滥用/指令覆盖/编码绕过）  :func:`classify_input`
intent     意图白名单；写意图叠加批量/他人关键词                  :func:`check_intent`
output     他人姓名、病历术语、身份证、手机号泄露                 :func:`check_output`
=========  ==================================================  ==========

编码绕过对策（本模块的核心难点）：

* **空格插字**：全角/半角空白、零宽字符、软连字符先做 NFKC 归一再去噪，
  然后对归一文本**重跑一遍**规则。``"忽 略 之 前 的 指 令"`` 因此可被拦截。
* **base64**：识别长度 ≥16 的 base64 片段，解码后重跑规则或查关键词
  （``忽略``/``指令``/``管理员``/``system``/``admin`` 等）。
* **拼音**：仅当整段文本**不含中文**时才启用拼音规则，避免把
  ``"hu"`` / ``"zhi"`` 这类正常英文误判。

真实意图枚举的出处（逐字读取，未凭空发明）：
``app/router/rules.py`` 的 7 条规则产出 11 个 L0 意图；
``eval/fixtures/routing.jsonl`` 的 ``expected.intent`` 另有 chitchat/opera/movie/
time/weather/volume/medicine/skip_next/bye/health；
``app/router/safety.py`` 产出 ``alert``。
``tests/test_decision_log_service.py`` 里的 ``small_talk`` 只是单测构造值，
不是生产枚举，故**未**收入白名单。
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

__all__ = [
    "FIXED_REFUSAL",
    "INJECTION_PATTERNS",
    "INTENT_WHITELIST",
    "LAYER_INPUT",
    "LAYER_INTENT",
    "LAYER_NONE",
    "LAYER_OUTPUT",
    "WRITE_INTENTS",
    "InjectionResult",
    "check_intent",
    "check_output",
    "classify_input",
    "guard",
]

# 拦截层标识：写进 InjectionResult.layer，供编排层按层统计与审计分桶。
LAYER_INPUT: Final[str] = "input"
LAYER_INTENT: Final[str] = "intent"
LAYER_OUTPUT: Final[str] = "output"
LAYER_NONE: Final[str] = "none"

# 拦截后统一输出的固定话术（不解释规则细节，避免二次提示注入）。
FIXED_REFUSAL: Final[str] = "这个我不能帮您处理，我们聊点别的吧。"


@dataclass(frozen=True, slots=True)
class InjectionResult:
    """一次拦截判定的结果。

    Attributes:
        blocked: 是否拦截。
        layer: 命中的层，取 :data:`LAYER_INPUT` / :data:`LAYER_INTENT` /
            :data:`LAYER_OUTPUT` / :data:`LAYER_NONE`。
        pattern_id: 命中的规则 id；放行时为 ``None``。
        reason: 面向审计的中文原因；不得包含完整手机号/身份证/他人姓名。
    """

    blocked: bool
    layer: str
    pattern_id: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# input 层：五类注入的规则集
# ---------------------------------------------------------------------------

# 越权取数：他人/某某/具体人名。代词只在「他的/她家/他本人」这种领属结构里算目标，
# 避免把「他昨天来了」这类普通叙述误判；`(?<!其)他人` 避免命中「其他人」。
_OTHER_TARGET: Final[str] = (
    r"(?<!其)他人|别人|旁人|别的?(?:长者|老人|奶奶|爷爷)"
    r"|其他(?:长者|老人)"
    r"|某某|某位(?:长者|老人|奶奶|爷爷)?"
    r"|张三|李四|王五"
    r"|李奶奶|王奶奶|张奶奶|赵爷爷|王大爷|李大爷|赵阿姨|李阿姨|王阿姨"
    r"|(?<!其)他(?:的|家|本人)"
    r"|她(?:的|家|本人)"
)

# 敏感字段：病历、诊断、身份证、联系方式、住址等。
_SENSITIVE_FIELD: Final[str] = (
    r"病历|病例|诊断(?:书|结果)?|病史"
    r"|身份证(?:号|号码)?|手机号(?:码)?|电话号码|号码|电话"
    r"|住址|家庭住址|住院记录|体检报告|用药记录|处方|药单"
)

# 目标在前、字段在后，最多跨 12 个非句读字符：「张三的病历」「李奶奶的电话」。
_EXFIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?:{_OTHER_TARGET})[^，。！？!?；;、\n]{{0,12}}(?:{_SENSITIVE_FIELD})"
)

# 角色劫持：你(现在/以后/...)是/变成/当 + 管理员/医生/root 等。
# 句尾疑问语气（吗/么/嘛/吧/？）不算劫持，避免误伤「你是医生吗？」。
_ROLE_WORD: Final[str] = (
    r"管理员|超级管理员|超级用户|超级|root|admin|administrator"
    r"|医生|大夫|开发者|主人|系统"
)
_ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"你(?:现在|以后|从现在起|从现在开始|今后)?(?:就)?(?:是|变成|当)"
    rf"[^，。！？!?；;、\n]{{0,6}}(?:{_ROLE_WORD})"
    rf"(?!\s*[吗么嘛吧？?])"
    rf"|扮演(?:一?个|一?名|一?位)?.{{0,6}}(?:{_ROLE_WORD})"
    rf"|\byou\s+are\s+(?:now\s+)?(?:an?\s+)?"
    rf"(?:admin|administrator|root|doctor|superuser)\b"
    rf"|\bact\s+as\s+(?:an?\s+)?(?:admin|administrator|root|doctor)\b",
    re.IGNORECASE,
)

# 工具滥用：批量调用 create_work_order / send_family_notification / 通用「工具」。
_TOOL_NAME: Final[str] = (
    r"(?:create_work_order|send_family_notification|get_weather|工具|接口|函数|mcp)"
)
_BULK: Final[str] = r"(?:所有|全部|批量|全体|每个人|每位|大家)"
_TOOL_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?:调用|执行|使用|去调|帮我调).{{0,8}}{_TOOL_NAME}"
    rf"[^，。！？!?；;、\n]{{0,12}}{_BULK}"
    rf"|{_BULK}[^，。！？!?；;、\n]{{0,6}}(?:工单|通知|任务|记录)"
    rf"[^，。！？!?；;、\n]{{0,6}}(?:关闭|关掉|取消|删掉|删除|清空|作废)"
    rf"|(?:给|帮|替).{{0,4}}{_BULK}.{{0,6}}(?:建|创建|开|发|派).{{0,4}}(?:工单|通知|单子)"
    rf"|(?:关闭|删除|取消|清空|作废).{{0,8}}{_BULK}"
    rf"[^，。！？!?；;、\n]{{0,6}}(?:工单|订单|任务|记录)",
    re.IGNORECASE,
)

# 指令覆盖：忽略/忘掉既有指令、覆盖安全策略、英文 ignore previous instructions。
_OVERRIDE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"忽略[^，。！？!?；;、\n]{0,8}(?:指令|要求|规则|指示|限制|设定|约束|提示词|人设)"
    r"|(?:忘掉|忘记)[^，。！？!?；;、\n]{0,6}(?:设定|人设|指令|规则|限制|要求)"
    r"|(?:覆盖|绕过|跳过|解除|重置)[^，。！？!?；;、\n]{0,6}"
    r"(?:安全|限制|规则|校验|白名单|提示词)"
    r"|(?:ignore|disregard|forget)\s+(?:all\s+)?"
    r"(?:previous|prior|above|earlier|your)\s+"
    r"(?:instructions?|prompts?|rules?|limits?)",
    re.IGNORECASE,
)

# 伪造 system 提示边界：半角/全角冒号都算，ChatML 特殊 token 一并识别。
_SYSTEM_PROMPT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:system|系统)\s*[:：]|<\|im_start\|>|<\|system\|>",
    re.IGNORECASE,
)

# 自称获得写权限：你现在可以改/删/关闭……
_PRIVILEGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"你现在可以[^，。！？!?；;、\n]{0,6}(?:改|删|关闭|修改|删除|操作|管理|读写)"
    r"|你现在(?:拥有|获得|被授予)[^，。！？!?；;、\n]{0,6}(?:权限|管理权|最高权限)"
    r"|from\s+now\s+on\s+you\s+(?:can|are\s+allowed\s+to)\s+"
    r"(?:modify|delete|admin)",
    re.IGNORECASE,
)

# 拼音绕过：仅当整段文本没有中文时才启用（\A 锚定 + 全串否定环视）。
# 去掉声调后 "hu lüe" -> "hu lue"，故字符类同时接受 u/ü/v。
_PINYIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\A(?![\s\S]*[\u4e00-\u9fff])[^A-Za-z0-9]{0,4}"
    r"(?:hu\s*l[uüv]\s*e|zhi\s*ling|wang\s*diao|qian\s*de\s*zhi)",
    re.IGNORECASE,
)

# 规则表：(pattern_id, layer, compiled regex)。顺序即优先级，先命中先返回。
INJECTION_PATTERNS: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = (
    ("exfil_other_sensitive", LAYER_INPUT, _EXFIL_PATTERN),
    ("role_hijack_identity", LAYER_INPUT, _ROLE_PATTERN),
    ("tool_abuse_bulk", LAYER_INPUT, _TOOL_PATTERN),
    ("override_ignore_prior", LAYER_INPUT, _OVERRIDE_PATTERN),
    ("override_system_prompt", LAYER_INPUT, _SYSTEM_PROMPT_PATTERN),
    ("override_privilege_grant", LAYER_INPUT, _PRIVILEGE_PATTERN),
    ("encoding_pinyin", LAYER_INPUT, _PINYIN_PATTERN),
)

_PATTERN_REASONS: Final[dict[str, str]] = {
    "exfil_other_sensitive": "请求他人病历/电话/身份证等敏感信息，属越权取数",
    "role_hijack_identity": "试图把助手改写成管理员/医生等更高权限角色",
    "tool_abuse_bulk": "试图批量调用工单/通知等写工具",
    "override_ignore_prior": "要求忽略既有指令或安全约束",
    "override_system_prompt": "伪造 system 提示或系统指令边界",
    "override_privilege_grant": "自称获得改/删/关闭等写权限",
    "encoding_pinyin": "无中文的拼音注入片段，疑似编码绕过",
}

# 归一化时一并去掉的噪声：空白、零宽字符、软连字符、双向控制符、间隔点。
_NOISE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[\s\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff·•・]+"
)

# base64 片段：至少 16 个字符，允许 URL-safe 变体与结尾补位。
_BASE64_CANDIDATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")

# 解码后直接查这些词（覆盖规则表可能漏掉的短句）。
_BASE64_KEYWORDS: Final[tuple[str, ...]] = (
    "忽略",
    "指令",
    "管理员",
    "病历",
    "身份证",
    "手机号",
    "ignore",
    "instruction",
    "system",
    "admin",
    "root",
    "password",
    "delete",
)

# ---------------------------------------------------------------------------
# intent 层：真实意图白名单
# ---------------------------------------------------------------------------

# 24 个生产意图，逐字来自 rules.py（13 个 L0，含 CON-03 的 pause/resume）、
# routing.jsonl 的 expected.intent 与 safety.py 的 alert；不含仅测试使用的
# small_talk，也不含 "chat"/"clarify" 这类代码里不存在的名字。
# 新增 L0 意图必须同步这里：`tests/test_injection.py::test_whitelist_covers_l0_rule_intents`
# 会断言「规则层识别出的意图是白名单子集」，漏一步就是注入面被悄悄放大。
INTENT_WHITELIST: Final[frozenset[str]] = frozenset(
    {
        "alert",
        "ask_time",
        "bye",
        "chitchat",
        "farewell",
        "health",
        "medicine",
        "medication_take",
        "movie",
        "opera",
        "pause",
        "play_jingju",
        "play_next",
        "play_yueju",
        "repeat",
        "resume",
        "skip_next",
        "stop",
        "time",
        "volume",
        "volume_down",
        "volume_up",
        "weather",
        "weather_query",
    }
)

# 会真正写业务状态的意图。当前只有用药确认（写用药事件/触达记录），
# 播放/音量/天气/闲聊都是设备侧或只读动作；未来新增写意图必须补进这里。
WRITE_INTENTS: Final[frozenset[str]] = frozenset({"medication_take"})

# 写意图叠加「批量/替他人办」时视为越权。`(?<!其)他` 避免命中「其他」。
_WRITE_SCOPE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"所有|全部|批量|全体|每个人|每位|大家|他人|别人|旁人|某某|某位"
    r"|张三|李四|王五"
    r"|李奶奶|王奶奶|张奶奶|赵爷爷|王大爷|李大爷|赵阿姨|李阿姨|王阿姨"
    r"|(?<!其)他|她"
)

# ---------------------------------------------------------------------------
# output 层：泄露与越界输出
# ---------------------------------------------------------------------------

# 中国大陆手机号与身份证号；前后不能紧贴数字，避免误伤更长编号。
_OUTPUT_PHONE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_OUTPUT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)\d{17}[\dXx](?!\d)|(?<!\d)\d{15}(?!\d)"
)
_OUTPUT_SENSITIVE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"病历|病例|诊断(?:书|结果)?|病史"
    r"|身份证(?:号|号码)?|手机号(?:码)?"
    r"|住院记录|体检报告|用药记录|处方|家庭住址|住址"
)

# 医疗诊断口径（AGENTS §4.4 红线 4、PRD「不做医疗诊断」业务边界）：
# 只拦「下结论」的句式，不拦「建议问医生」。病名单独出现**不算** —— 长者问
# 「我血压高是不是高血压」时，回复里复述病名是允许的；但「您这是高血压」
# 「医院确诊为糖尿病」是给出结论，必须拦。
_DIAGNOSIS_DISEASES: Final[str] = (
    r"高血压|糖尿病|中风|脑梗|心梗|冠心病|房颤|肺炎|骨折"
    r"|白内障|骨质疏松|老年痴呆|阿尔茨海默"
)
_OUTPUT_DIAGNOSIS_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?:您|你)(?:这是|得了|患了|患有|就是|属于)(?:{_DIAGNOSIS_DISEASES})"
    rf"|确诊(?:为|是)\s*(?:{_DIAGNOSIS_DISEASES})"
)

# 明确的人名/匿名称呼。命中后还要用 elder_name 判断是不是长者本人。
_NAME_LITERAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"张三|李四|王五"
    r"|李奶奶|王奶奶|张奶奶|赵爷爷|王大爷|李大爷|赵阿姨|李阿姨|王阿姨"
    r"|某某|某位(?:长者|老人)"
)

# 「患者叫王秀兰」这类上下文里的姓名；捕获组 1 是姓+名（1~2 字）。
_NAME_CONTEXT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:叫|名叫|名字叫|患者|病友|邻居|那位|这位)"
    r"([张王李赵刘陈杨黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段钱汤尹黎易常武乔贺赖龚文]"
    r"[\u4e00-\u9fff]{1,2})"
)


def _allowed(layer: str) -> InjectionResult:
    """构造放行结果。"""
    return InjectionResult(blocked=False, layer=layer)


def _blocked(layer: str, pattern_id: str, reason: str) -> InjectionResult:
    """构造拦截结果。"""
    return InjectionResult(blocked=True, layer=layer, pattern_id=pattern_id, reason=reason)


def _match_patterns(text: str) -> str | None:
    """按优先级跑规则表，返回第一个命中的 pattern_id。"""
    for pattern_id, _layer, pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return pattern_id
    return None


def _strip_diacritics(text: str) -> str:
    """去掉声调/重音组合字符，让拼音与全角文本归一到 ASCII 形态。"""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _deobfuscate(text: str) -> str:
    """NFKC + 去重音 + 去空白/零宽/控制符，用于对抗插字绕过。"""
    normalized = unicodedata.normalize("NFKC", text)
    return _NOISE_PATTERN.sub("", _strip_diacritics(normalized))


def _decode_base64(candidate: str) -> str | None:
    """尝试解码一个 base64 片段；不是合法 base64 时返回 ``None``。"""
    normalized = candidate.replace("-", "+").replace("_", "/").rstrip("=")
    if len(normalized) < 16:
        return None
    padded = normalized + "=" * (-len(normalized) % 4)
    try:
        raw = base64.b64decode(padded, validate=False)
    except ValueError:  # binascii.Error 是 ValueError 的子类，一并覆盖
        return None
    return raw.decode("utf-8", errors="ignore")


def _base64_bypass(text: str) -> str | None:
    """扫描 base64 片段；命中则返回审计原因，否则 ``None``。"""
    for match in _BASE64_CANDIDATE_PATTERN.finditer(text):
        decoded = _decode_base64(match.group(0))
        if not decoded:
            continue
        pattern_id = _match_patterns(decoded)
        if pattern_id is not None:
            return f"base64 片段解码后命中规则 {pattern_id}"
        folded = decoded.casefold()
        for keyword in _BASE64_KEYWORDS:
            if keyword in folded:
                return f"base64 片段解码后含敏感词「{keyword}」"
    return None


def classify_input(text: str) -> InjectionResult:
    """input 层：识别五类提示注入与编码绕过。

    先跑规则表，再扫 base64；若文本经过 NFKC/去空白/去重音后发生变化，
    对归一文本重跑一遍规则，覆盖「忽 略」「零宽字符」「全角 system」等插字绕过。

    Args:
        text: 长者输入原文。

    Returns:
        ``blocked=True`` 且带 ``pattern_id``/``reason`` 的判定；否则放行。
    """
    if not text or not text.strip():
        return _allowed(LAYER_INPUT)

    pattern_id = _match_patterns(text)
    if pattern_id is not None:
        return _blocked(
            LAYER_INPUT,
            pattern_id,
            _PATTERN_REASONS[pattern_id],
        )

    base64_reason = _base64_bypass(text)
    if base64_reason is not None:
        return _blocked(LAYER_INPUT, "encoding_base64", base64_reason)

    normalized = _deobfuscate(text)
    if normalized and normalized != text:
        pattern_id = _match_patterns(normalized)
        if pattern_id is not None:
            return _blocked(
                LAYER_INPUT,
                pattern_id,
                f"去除空白/零宽字符并做 Unicode 归一后命中规则 {pattern_id}",
            )
        base64_reason = _base64_bypass(normalized)
        if base64_reason is not None:
            return _blocked(LAYER_INPUT, "encoding_base64", base64_reason)

    return _allowed(LAYER_INPUT)


def check_intent(text: str, intent: str | None) -> InjectionResult:
    """intent 层：意图白名单 + 写意图的越权范围校验。

    Args:
        text: 长者输入原文（用于识别批量/他人关键词）。
        intent: 路由层给出的意图；``None``/空串表示尚未分类，直接放行，
            由 input 层与工具白名单兜底。

    Returns:
        意图不在白名单时为 ``intent_not_whitelisted``；
        写意图叠加批量/他人关键词时为 ``intent_write_scope``。
    """
    normalized = (intent or "").strip().casefold()
    if not normalized:
        return _allowed(LAYER_INTENT)
    if normalized not in INTENT_WHITELIST:
        return _blocked(
            LAYER_INTENT,
            "intent_not_whitelisted",
            f"意图「{intent}」不在白名单内",
        )
    if normalized in WRITE_INTENTS and _WRITE_SCOPE_PATTERN.search(text):
        return _blocked(
            LAYER_INTENT,
            "intent_write_scope",
            f"写操作意图「{intent}」叠加批量/他人关键词，超出长者本人范围",
        )
    return _allowed(LAYER_INTENT)


def _mask_number(value: str) -> str:
    """审计日志用的号码脱敏：只留前 3 位与后 4 位。"""
    if len(value) <= 7:
        return "***"
    return f"{value[:3]}****{value[-4:]}"


def _mask_name(name: str) -> str:
    """审计日志用的姓名脱敏：只留姓氏。"""
    return f"{name[0]}**"


def _other_name(reply: str, elder_name: str | None) -> str | None:
    """返回回复中出现的非本人姓名；没有则 ``None``。"""
    elder = elder_name.strip() if elder_name else None
    for literal in _NAME_LITERAL_PATTERN.finditer(reply):
        name = literal.group(0)
        if elder is not None and name == elder:
            continue
        return name
    for contextual in _NAME_CONTEXT_PATTERN.finditer(reply):
        name = contextual.group(1)
        if elder is not None and name == elder:
            continue
        return name
    return None


def check_output(reply: str, *, elder_name: str | None = None) -> InjectionResult:
    """output 层：拦截回复里的手机号、身份证、病历术语、诊断结论与他人姓名。

    Args:
        reply: 模型输出/播报文本。
        elder_name: 当前长者姓名；命中姓名与它完全一致时视为本人，放行。

    Returns:
        命中时 ``pattern_id`` 为 ``output_phone`` / ``output_id_card`` /
        ``output_sensitive_term`` / ``output_diagnosis`` / ``output_other_name``
        之一。
    """
    if not reply or not reply.strip():
        return _allowed(LAYER_OUTPUT)

    phone = _OUTPUT_PHONE_PATTERN.search(reply)
    if phone is not None:
        return _blocked(
            LAYER_OUTPUT,
            "output_phone",
            f"回复泄露手机号 {_mask_number(phone.group(0))}",
        )

    id_card = _OUTPUT_ID_PATTERN.search(reply)
    if id_card is not None:
        return _blocked(
            LAYER_OUTPUT,
            "output_id_card",
            f"回复泄露身份证号 {_mask_number(id_card.group(0))}",
        )

    sensitive = _OUTPUT_SENSITIVE_PATTERN.search(reply)
    if sensitive is not None:
        return _blocked(
            LAYER_OUTPUT,
            "output_sensitive_term",
            f"回复出现病历术语「{sensitive.group(0)}」",
        )

    diagnosis = _OUTPUT_DIAGNOSIS_PATTERN.search(reply)
    if diagnosis is not None:
        return _blocked(
            LAYER_OUTPUT,
            "output_diagnosis",
            f"回复给出医疗诊断结论「{diagnosis.group(0)}」",
        )

    name = _other_name(reply, elder_name)
    if name is not None:
        return _blocked(
            LAYER_OUTPUT,
            "output_other_name",
            f"回复出现他人姓名（{_mask_name(name)}）",
        )

    return _allowed(LAYER_OUTPUT)


def guard(text: str, *, intent: str | None = None) -> InjectionResult:
    """input + intent 两层的统一入口；output 层由调用方单独调用。

    Args:
        text: 长者输入原文。
        intent: 可选的路由意图；未分类时传 ``None``。

    Returns:
        第一个拦截层的结果；全部放行时 ``layer`` 为最后一层（intent）。
    """
    verdict = classify_input(text)
    if verdict.blocked:
        return verdict
    return check_intent(text, intent)
