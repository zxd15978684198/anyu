"""脱敏底座：手机号 / 身份证 / 地址 / 密钥的掩码与递归脱敏（迭代 9 · W1）。

## 一、为什么规则只写这一份

PRD FR-TC-07 与 AC-36 要求「日志脱敏零命中」，技术设计文档 §1.2 第 8 条要求「不落未经脱敏的
个人敏感信息」。落地时最怕的是「一份规则、三处实现」：日志脱了、审计导出没脱、验收脚本用的是
第三套正则 —— 于是 grep 零命中，泄露照旧。所以本模块同时是**唯一规则表**：

| 使用方 | 走哪个入口 |
| --- | --- |
| `app/core/logging.py`（每行日志落盘前） | `redact_text`（message）、`redact_mapping`（`extra`） |
| 审计导出 / 审计脱敏视图（迭代 9 · W4） | `redact_mapping` |
| `scripts/scan_logs.py`（验收脚本） | `REDACTION_RULES` 里的 `pattern` 与 `masker` |

## 二、三条设计纪律

1. **fail closed（识别不了就整体打码）**：写错格式的手机号、认不出层级的地址，一律整体
   打码，不原样透出。宁可日志难看，也不让一个畸形手机号漏出去。
2. **幂等**：任何 `mask_*` 遇到已经带 `*` 的值原样返回，因此 `redact_mapping` / `redact_text`
   可以重复套用（审计导出后再写日志，不会把掩码再打一次）。
3. **纯函数、无 I/O**：不读环境变量、不连库、不写文件，任何进程里都能直接单测。

## 三、`redact_mapping` 与 `redact_text` 的分工

- `redact_mapping` **按键名**识别：`{"user_phone": "N/A"}` 这种「值不像手机号」的字段也会被打码；
- `redact_text` **按内容**识别：message 里嵌了一句「他的号码是 13800138000」也能抓到。

两者互补，缺一不可：只看键名漏自由文本，只看内容漏结构化字段（比如口令）。

## 四、已知边界（写进 docs/SECURITY.md 的「已知限制」）

1. 只认中国大陆 11 位手机号（`1[3-9]` 开头）；带 `+86`、带空格或横线的写法走「整体打码」，
   不到「覆盖面不全」的程度，但不会保留前 3 后 4。
2. 身份证只认 15 位老证与 18 位新证（含末位 `X`），且**做出生日期合法性校验**：校验不过的
   18 位数字串不会被当成身份证（避免把时间戳、流水号误报进日志扫描结果）。
3. 地址只保留到「区 / 县 / 旗」层级；没有该层级的地址整体打码（连街道一起丢）。
4. 中文以外的地址（如英文门牌）识别不到，整体打码。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: 掩码字符。判「已打过码」也靠它，所以集中成一常量，不在各处写死。
MASK_CHAR: Final[str] = "*"

#: 地址尾部打码后的固定占位（不用等长星号：门牌号长度本身也是信息）。
MASK_ADDRESS_TAIL: Final[str] = MASK_CHAR * 3

#: 认不出 scheme 的长 base64 串保留的前缀长度（够定位「哪一条 key」，不够还原）。
SECRET_PREFIX_KEEP: Final[int] = 4

#: 长 base64 串的最小长度（短串既可能是正常词，也可能是被截断的密钥，不猜）。
BASE64_MIN_LEN: Final[int] = 32

#: 中国大陆手机号：11 位、`1` 开头、第二位 3-9。
_PHONE_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

#: 18 位新证：前 6 位地区码 + 8 位出生日期（做合法性校验）+ 3 位顺序码 + 校验位（数字或 X）。
_ID_CARD_18: Final[str] = (
    r"\d{6}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"
)

#: 15 位老证：前 6 位地区码 + 6 位出生日期（YYMMDD）+ 3 位顺序码，无校验位。
_ID_CARD_15: Final[str] = r"\d{6}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}"

#: 身份证文本模式。边界吃掉相邻的数字与 X，避免从一个 19 位数字串里「截」出 18 位。
_ID_CARD_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?<![0-9Xx])(?:{_ID_CARD_18}|{_ID_CARD_15})(?![0-9Xx])"
)

#: 密钥文本模式，四类写在同一张分支里（顺序会影响结果，见模块 docstring 与本表注释）：
#: ① `Authorization: Bearer xxx`；② OpenAI 风格 `sk-...`；③ GitHub `ghp_...` / `github_pat_...`；
#: ④ 长度 ≥32 且同时含大写、小写、数字的 base64 系串（纯小写 hex 的 trace_id 不会被误伤）。
_SECRET_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}"
    r"|(?<![A-Za-z0-9_])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{8,}"
    r"|(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}"
    r"|(?<![A-Za-z0-9+/=_-])"
    r"(?=[A-Za-z0-9+/=_-]{32,})"
    r"(?=[A-Za-z0-9+/=_-]*[a-z])(?=[A-Za-z0-9+/=_-]*[A-Z])(?=[A-Za-z0-9+/=_-]*\d)"
    r"[A-Za-z0-9+/=_-]{32,}"
    r"(?![A-Za-z0-9+/=_-])"
)

#: 完整地址：必须有「区/县/旗」且后面跟着门牌号（`N号`/`N栋`/`N单元`/`N室`…）。
#: 只用于**扫描**（`in_text=False`），不进 `redact_text`：自由文本里地址颗粒度太差，
#: 逐条正则替换容易把正常中文改花；日志侧由 `redact_mapping` 按键名兜住。
_ADDRESS_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[\u4e00-\u9fa5]{2,10}(?:省|自治区|特别行政区|市))?"
    r"[\u4e00-\u9fa5]{2,10}(?:区|县|旗)"
    r"(?:[\u4e00-\u9fa5]{1,10}(?:街道|路|街|巷|胡同|大道))?"
    r"[\u4e00-\u9fa5A-Za-z]{0,10}?\d+(?:号|栋|幢|单元|楼|室)"
)

#: 地址「保留到哪一级」的层级字：取最后一次出现的位置当切点。
_ADDRESS_LEVEL_CHARS: Final[tuple[str, ...]] = ("区", "县", "旗")

#: 地址尾部允许被丢掉而无需打码的内容（纯空白与标点）。
_ADDRESS_TAIL_SEPARATORS: Final[str] = " \t，,。.;；、"

#: 常见姓氏（单字），用于「姓 + 称谓」与「叫/患者 + 姓名」两类姓名识别。
_SURNAME_CHARS: Final[str] = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏"
    "潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮"
    "卞齐康伍余元卜顾孟平黄和穆萧尹姚邵汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫房裘解"
    "应宗丁宣邓郁单杭洪包诸左石崔吉龚程邢裴陆荣翁荀羊惠甄封储靳段富巫乌焦巴牧山谷车侯全班"
    "仰秋仲伊宫宁仇栾甘厉祖武符刘景詹束龙叶幸司黎薄印宿白怀蒲索咸赖卓蔺屠蒙池乔双闻党翟谭"
    "劳姬申扶冉宰雍桑桂通边燕冀浦尚农温庄晏柴瞿阎慕连茹习艾鱼容向古易慎戈廖庾终衡步都耿满"
    "弘匡国文寇广禄阙隆师巩聂晁融冷辛简饶曾沙养鞠丰巢关蒯查后荆红游权盖益桓公"
)

#: 「姓 + 称谓」：王奶奶 / 李爷爷 / 赵阿姨 / 王大爷。称谓是闭合集，误伤面极小。
_NAME_HONORIFIC_RE: Final[re.Pattern[str]] = re.compile(
    rf"[{_SURNAME_CHARS}][\u4e00-\u9fff]?(?:奶奶|爷爷|阿姨|叔叔|伯伯|大爷|大妈|姥姥|姥爷)"
)

#: 明确写死的人名（与 `app.core.injection` 的 `_NAME_LITERAL_PATTERN` 同一口径）。
_NAME_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r"张三|李四|王五")

#: 「叫 / 名叫 / 患者 / 病友 + 姓 + 1~2 字名」这类上下文里的真实姓名。
_NAME_CONTEXT_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:(?:名字)?叫|名叫|患者|病友)([{_SURNAME_CHARS}][\u4e00-\u9fff]{{1,2}})"
)

#: 已识别的密钥 scheme → 掩码结果。**按顺序匹配**，长的写前面。
_SCHEME_MASKS: Final[tuple[tuple[str, str], ...]] = (
    ("github_pat_", "github_pat_***"),
    ("ghp_", "ghp_***"),
    ("gho_", "gho_***"),
    ("ghu_", "ghu_***"),
    ("ghs_", "ghs_***"),
    ("ghr_", "ghr_***"),
    ("sk-", "sk-***"),
)

#: `Authorization` 方案名（大小写不敏感，输出归一化为 `Bearer`）。
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"bearer\b", re.IGNORECASE)

#: base64 系串允许的字符集（含 URL-safe 变体与填充符）。
_BASE64_CHARS: Final[str] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-"


# ======================================================================
# 一、各类型的掩码函数（纯字符串进出，不抛异常）
# ======================================================================


def _is_masked(value: str) -> bool:
    """值里已经带 `*` 就认为「已经脱过敏」，原样返回 —— 这是幂等的唯一判据。"""
    return MASK_CHAR in value


def mask_phone(value: str) -> str:
    """手机号：保留前 3 后 4，中间 4 个星号（`13700000000` → `137****0000`）。

    - 空串原样返回（空值不是隐私，也不需要制造噪音）；
    - 已经带 `*` 的值原样返回（幂等）；
    - 形如 11 位 `1[3-9]` 开头 → 掩码；**其余一律整体打码**（长度都不改动，
      保持「有个值但看不到内容」的信息量，同时不泄露真实长度以外的字符）。
    """
    if not value or _is_masked(value):
        return value
    if _PHONE_TEXT_RE.fullmatch(value):
        return f"{value[:3]}{MASK_CHAR * 4}{value[7:]}"
    return MASK_CHAR * len(value)


def mask_id_card(value: str) -> str:
    """身份证：18 位保留前 6 后 4（8 个星号），15 位老证保留前 6 后 4（5 个星号）。

    `110101199003071234` → `110101********1234`；`110101900307123` → `110101*****7123`。
    非 15/18 位（或出生日期非法）→ 整体打码；空串与已打码的值原样返回。
    """
    if not value or _is_masked(value):
        return value
    if not _ID_CARD_TEXT_RE.fullmatch(value):
        return MASK_CHAR * len(value)
    hidden = len(value) - 6 - 4
    return f"{value[:6]}{MASK_CHAR * hidden}{value[-4:]}"


def mask_address(value: str) -> str:
    """地址：保留到「区 / 县 / 旗」，后面的门牌号换成 `***`。

    `北京市朝阳区建国路88号院3栋2单元501室` → `北京市朝阳区***`。

    识别不到区/县层级（例如只写了「建国路88号」或英文门牌）→ **整体打码**；
    区/县级后面没有实质内容（如只写「朝阳区」）→ 原样返回（本来就没有门牌号）。
    """
    if not value or _is_masked(value):
        return value

    head_end = max(value.rfind(char) for char in _ADDRESS_LEVEL_CHARS)
    if head_end < 0:
        return MASK_CHAR * len(value)

    head, tail = value[: head_end + 1], value[head_end + 1 :]
    if not tail.strip(_ADDRESS_TAIL_SEPARATORS):
        return head
    return f"{head}{MASK_ADDRESS_TAIL}"


def mask_name(value: str) -> str:
    """姓名：保留首字（姓），其余换星号。`王奶奶` → `王**`，`张三` → `张*`。

    只掩一个字是没用的（姓氏 + 称谓组合仍可识别），所以除首字外全部打掉；
    空串与已打码的值原样返回（幂等）。
    """
    if not value or _is_masked(value):
        return value
    return f"{value[0]}{MASK_CHAR * (len(value) - 1)}"


def _is_long_mixed_base64(value: str) -> bool:
    """长度 ≥32、字符集像 base64、且同时含大写 / 小写 / 数字（「混合」三条件缺一不可）。"""
    if len(value) < BASE64_MIN_LEN:
        return False
    if any(char not in _BASE64_CHARS for char in value):
        return False
    return (
        any(char.islower() for char in value)
        and any(char.isupper() for char in value)
        and any(char.isdigit() for char in value)
    )


def mask_secret(value: str) -> str:
    """密钥：只留前缀 + `***`，**取不到前缀就整体打码**。

    | 输入前缀 | 输出 |
    | --- | --- |
    | `Bearer ` | `Bearer ***` |
    | `sk-` | `sk-***` |
    | `ghp_` / `github_pat_` 等 | `ghp_***` / `github_pat_***` |
    | 长 base64（≥32，大小写数字混合） | 前 4 字符 + `***` |
    | 其余（**含普通口令** `123456`） | `***` |

    最后一行是**故意 fail closed** 的：`password` 一类键名下的值即便不像密钥，也不能原样落盘。
    空串（以及纯空白）原样返回，已打码的值原样返回（幂等）。
    """
    if not value or not value.strip() or _is_masked(value):
        return value
    if _BEARER_RE.match(value):
        return f"Bearer {MASK_ADDRESS_TAIL}"
    for scheme, masked in _SCHEME_MASKS:
        if value.startswith(scheme):
            return masked
    if _is_long_mixed_base64(value):
        return f"{value[:SECRET_PREFIX_KEEP]}{MASK_ADDRESS_TAIL}"
    return MASK_ADDRESS_TAIL


# ======================================================================
# 二、规则表（docs/SECURITY.md 的脱敏规则表就从这里抄）
# ======================================================================


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """一条脱敏规则：类型名 + 匹配正则 + 掩码函数，供文档、日志与验收脚本共用。

    `in_text=True` 表示该规则也参与 `redact_text`（自由文本替换）；
    `in_text=False` 表示只用于扫描（地址就是这一类）。
    """

    kind: str
    pattern: re.Pattern[str]
    masker: Callable[[str], str]
    description: str
    in_text: bool = True


REDACTION_RULES: Final[tuple[RedactionRule, ...]] = (
    RedactionRule(
        kind="secret",
        pattern=_SECRET_TEXT_RE,
        masker=mask_secret,
        description="Bearer 令牌 / sk-* / ghp_* / 长度≥32 的大小写数字混合 base64 串",
    ),
    RedactionRule(
        kind="id_card",
        pattern=_ID_CARD_TEXT_RE,
        masker=mask_id_card,
        description="18 位新证（出生日期合法）与 15 位老证",
    ),
    RedactionRule(
        kind="phone",
        pattern=_PHONE_TEXT_RE,
        masker=mask_phone,
        description="中国大陆 11 位手机号（1[3-9] 开头）",
    ),
    RedactionRule(
        kind="address",
        pattern=_ADDRESS_TEXT_RE,
        masker=mask_address,
        description="含区/县/旗且带门牌号的完整地址（只扫描，不进 redact_text）",
        in_text=False,
    ),
)

#: 真正参与自由文本替换的子集（顺序即替换顺序：先密钥，避免长串被后一条规则切碎）。
_TEXT_RULES: Final[tuple[RedactionRule, ...]] = tuple(
    rule for rule in REDACTION_RULES if rule.in_text
)


@dataclass(frozen=True, slots=True)
class SensitiveKeyRule:
    """按键名识别敏感字段。

    `exact=True` 时只匹配**整个键名等于 token**（用于 `identity` 这种既有「身份证」
    含义、又是其他键名前缀的词：`identity_source` 是设备/声纹/确认这类**非隐私**枚举，
    不能被一起打码）。
    """

    token: str
    masker: Callable[[str], str]
    exact: bool = False


#: 键名规则表（大小写不敏感、**子串匹配**，按顺序命中即止）。
#: 顺序即优先级：`identity_card` 这类具体词排在 `identity`（精确匹配）之前。
SENSITIVE_KEY_RULES: Final[tuple[SensitiveKeyRule, ...]] = (
    SensitiveKeyRule("password", mask_secret),
    SensitiveKeyRule("passwd", mask_secret),
    SensitiveKeyRule("pwd", mask_secret),
    SensitiveKeyRule("secret", mask_secret),
    SensitiveKeyRule("token", mask_secret),
    SensitiveKeyRule("api_key", mask_secret),
    SensitiveKeyRule("apikey", mask_secret),
    SensitiveKeyRule("authorization", mask_secret),
    SensitiveKeyRule("credential", mask_secret),
    SensitiveKeyRule("cookie", mask_secret),
    SensitiveKeyRule("sdp", mask_secret),
    SensitiveKeyRule("id_card", mask_id_card),
    SensitiveKeyRule("idcard", mask_id_card),
    SensitiveKeyRule("identity_card", mask_id_card),
    SensitiveKeyRule("identity_no", mask_id_card),
    SensitiveKeyRule("identity_number", mask_id_card),
    SensitiveKeyRule("id_number", mask_id_card),
    SensitiveKeyRule("id_no", mask_id_card),
    SensitiveKeyRule("cert_no", mask_id_card),
    SensitiveKeyRule("identity", mask_id_card, exact=True),
    SensitiveKeyRule("phone", mask_phone),
    SensitiveKeyRule("mobile", mask_phone),
    SensitiveKeyRule("msisdn", mask_phone),
    SensitiveKeyRule("addr", mask_address),
)


# ======================================================================
# 三、对外入口
# ======================================================================


def _apply_masker(masker: Callable[[str], str], value: Any) -> Any:
    """对敏感键下的值套掩码；容器逐项下钻，非字符串标量原样保留。"""
    if isinstance(value, str):
        return masker(value)
    if isinstance(value, list):
        return [_apply_masker(masker, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_apply_masker(masker, item) for item in value)
    if isinstance(value, Mapping):
        # 敏感键下面是字典时，子键更具体，交给按键名递归去判。
        return redact_mapping(value)
    return value


def _masker_for_key(key: Any) -> Callable[[str], str] | None:
    """按键名找掩码函数；非字符串键、未命中一律返回 `None`。"""
    if not isinstance(key, str):
        return None
    lowered = key.lower()
    for rule in SENSITIVE_KEY_RULES:
        hit = lowered == rule.token if rule.exact else rule.token in lowered
        if hit:
            return rule.masker
    return None


def _redact_value(value: Any) -> Any:
    """对**非敏感键**的值递归下钻（dict / list / tuple），其余原样返回。"""
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """递归脱敏一个映射，返回**新** dict（不改入参）。

    规则：键名命中 `SENSITIVE_KEY_RULES`（大小写不敏感、子串匹配）→ 值套对应 mask；
    字典 / 列表 / 元组一律下钻；非字符串值（数字、布尔、`None`）原样保留
    —— `redact_mapping` 只做「不该以原文落盘的东西」，不负责类型改写。
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        masker = _masker_for_key(key)
        result[key] = _apply_masker(masker, value) if masker is not None else _redact_value(value)
    return result


def _rule_substitution(rule: RedactionRule) -> Callable[[re.Match[str]], str]:
    """把一条文本规则收成一个 `re.sub` 替换回调（每个 `pattern.sub` 只拿这一份）。

    为什么不像原来那样写 `lambda match, mask=rule: ...`：默认参数能把 `rule` 绑死
    （避开闭包在循环里捕获最后一个变量，ruff B023），但**带默认值的 lambda** 让 mypy
    无从从调用上下文推断参数类型（`Cannot infer type of lambda`）。具名函数据此显式
    标出 `re.Match[str]`，既保留防 B023 的绑定语义，又不再需要 `# type: ignore`。
    """

    def substitute(match: re.Match[str]) -> str:
        return rule.masker(match.group())

    return substitute


def redact_text(text: str) -> str:
    """对自由文本做正则替换（手机号、身份证、密钥、**长者称呼与姓名**）。

    地址不在这里处理：自由文本里地址颗粒度太差，逐条替换会把正常中文改花；
    结构化字段的地址由 `redact_mapping` 按键名兜住，日志验收由 `scan_logs.py` 负责。

    姓名（迭代 9 · L2 攻防检验 B4-03）：日志里「王奶奶今天……」能直接把人和事
    对上，属于准标识信息。识别三类：**姓 + 称谓**（王奶奶/李爷爷/赵阿姨）、
    写死的示例人名（张三/李四/王五）、**叫/患者 + 姓 + 名** 的上下文；
    只保留姓氏，其余打星号。宁可直接改花也不留全名 —— 日志是给人查的，
    不是给攻击者拼人画像的。
    """
    if not text:
        return text
    result = text
    for rule in _TEXT_RULES:
        result = rule.pattern.sub(_rule_substitution(rule), result)
    result = _NAME_HONORIFIC_RE.sub(lambda m: mask_name(m.group(0)), result)
    result = _NAME_LITERAL_RE.sub(lambda m: mask_name(m.group(0)), result)
    result = _NAME_CONTEXT_RE.sub(
        lambda m: m.group(0).replace(m.group(1), mask_name(m.group(1)), 1), result
    )
    return result


def detect_sensitive(text: str) -> Sequence[tuple[str, str]]:
    """扫描一段文本，返回 `(类型, 打码后的命中片段)` 列表（供脚本与测试断言用）。

    与 `redact_text` 用**同一张** `REDACTION_RULES` 表（含只扫描的地址规则），
    保证「验的是什么」和「脱的是什么」不会分叉。
    """
    if not text:
        return ()
    hits: list[tuple[str, str]] = []
    for rule in REDACTION_RULES:
        hits.extend(
            (rule.kind, rule.masker(match.group())) for match in rule.pattern.finditer(text)
        )
    return hits


__all__ = [
    "BASE64_MIN_LEN",
    "MASK_ADDRESS_TAIL",
    "MASK_CHAR",
    "REDACTION_RULES",
    "SECRET_PREFIX_KEEP",
    "SENSITIVE_KEY_RULES",
    "RedactionRule",
    "SensitiveKeyRule",
    "detect_sensitive",
    "mask_address",
    "mask_id_card",
    "mask_name",
    "mask_phone",
    "mask_secret",
    "redact_mapping",
    "redact_text",
]
