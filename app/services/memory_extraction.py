"""记忆提取（纯函数）：从一轮对话里挑出「值得长期记住」的候选。

**本模块只提取、不写库、不 import session**（AGENTS §2「`app/services/` 是唯一写库层」
约束的是写库动作；这里连 session 都不需要，落库由调用方把候选交给
`memory_service.remember` 完成）。理由：提取是**可失败、可丢弃**的旁路，写入是**有幂等
键、有幂等约束**的主路，两者混在一起会让「模型抽风」变成「对话主链路崩溃」。

## 为什么先做提取这一段

生产调用方是 `memory_writeback.writeback_memories`（它逐条调 `memory_service.remember`），
而它由 `memory_turn_hook.MemoryTurnHook` 在一轮对话结束后调用，钩子再挂到
`DialogueRuntime._schedule_memory` 的后台任务上。**生产端已接通，不再是空缺。**

`memory_chunks` 出现 0 行不等于链路断了：本模块对「这一轮没有值得长期记住的事」
返回空列表，钩子拿到空列表就**不开 session、不写库**（见 `memory_turn_hook` 契约③），
所以聊天内容本身不含长期事实时，0 行是**正确结果**而不是故障。判定方法是用一句
明确含事实的话（如「我对青霉素过敏」）跑同一套装配——能落库即链路正常。
本模块是「提取 → 写入」两段里的前一段。

## 判定口径（写进 prompt，也写在这里，防止 prompt 漂移后没人知道原意）

- `explicit`：长者**明确陈述**的长期事实——亲人称谓与姓名、家乡、职业、口味禁忌、
  过敏、固定习惯、忌讳话题。
- `inferred`：从行为/偏好**推断**出来的——反复点某类戏曲、反复拒绝某类话题。
- 只记「跨会话仍然成立」的信息；**一次性的即时需求**（今天想听戏、现在几点了）不记。

## 硬过滤（返回前做，不依赖模型自觉）

模型会不会照规则输出是概率问题，敏感信息一旦落进长期记忆就是**合规事故**，所以
下面每条都是代码级过滤，命中即丢弃该条：

1. 身份证号（17 位 + 数字/X）、手机号（`1[3-9]` 打头 11 位）、银行卡号（16–19 位）——
   正则命中即丢；
2. 完整住址——「省/市/区/县 … 路/街/道/巷 … 号/栋/单元/室/楼」三段式启发式命中即丢
   （只到「杭州西湖区」这种行政区级别**不丢**：那不是门牌地址）；
3. 诊断性表述——复用 `alert_aggregation.FORBIDDEN_WORDING`（「您这是/确诊/诊断为/
   是高血压…」，AGENTS §4.4 红线），命中即丢。注意「对青霉素过敏」不命中黑名单，
   属可记事實；
4. 长度不在 1–200 字（按 `len` 计，中文一个字算 1）；
5. 同批按 `content.strip()` 去重，保留先出现的一条；总数硬上限 5 条（与 prompt 一致）；
6. **推测语气**（「可能/大概/估计/好像/说不定」…）——长者说的是猜测，不是事实。
   PRD FR-MEM-01 的原例「我儿子可能失业了」正是这一类：拿它写「家庭关系画像」，等于把
   猜测落成事实，此后每一轮都会被复述成「您儿子失业了」（模型会把记忆当事实用）；
7. **传闻/转述**（「听说/据说/别人说/大家都说」…）——别人说的，不是长者本人明确表达的；
8. **第三方主体 × 状态事件**（「儿子/女儿/老伴/邻居…」×「失业/生病/住院/离婚/去世/辞职…」）
   ——家属的就业、健康、婚姻、经济状况属 PRD §4.9 的「敏感画像」，**不得由模型推断写入**。
   长者明确说来访习惯（「女儿每周末来看我」）不含状态事件，照旧可记；
9. `source="inferred"` 且 `category="family"` 一律丢弃 —— 同上一条的红线：敏感家庭画像
   只能来自长者的明确陈述，不能来自「反复提到」这类行为推断。

8/9 两条的边界值得写清楚：**「我女儿叫王芳，每周末来看我」要记**（亲属关系 + 探视习惯，
长者亲口、跨会话稳定）；**「我女儿可能被裁员了」不记**（推测 + 家属就业状况）。
两者都在说女儿，差别在「是不是事实陈述」与「是不是敏感境况」。

## 失败即空

`LLMService.chat` 本身就**从不抛**（失败时返回 `degraded=True` 的固定话术），所以这里
的失败面有三层，全部收敛成 `[]`：调用抛异常（假实现/未来换成别的客户端）、降级话术、
不是合法 JSON 数组。**绝不向上抛**：记忆提取失败的正确行为是「这轮不记」，不是
「这轮对话挂掉」。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, cast

from app.services.alert_aggregation import FORBIDDEN_WORDING
from app.services.llm_service import LLMReply

MemoryCategory = Literal["private", "family"]
MemorySource = Literal["explicit", "inferred"]

#: 与 `memory_chunks.category` 的 CHECK 取值对齐（private 默认 / family 家属相关）。
MEMORY_CATEGORIES: Final[tuple[str, ...]] = ("private", "family")
#: 提取来源：明确陈述 / 行为推断。注意 `memory_chunks` 表**没有**这一列，
#: 它只用于调用方决定 `importance` 与是否入库（inferred 写低权重或直接丢弃）。
MEMORY_SOURCES: Final[tuple[str, ...]] = ("explicit", "inferred")

MIN_CONTENT_LENGTH: Final[int] = 1
MAX_CONTENT_LENGTH: Final[int] = 200
#: 单轮最多提取条数；与 prompt 里的「最多 5 条」一致。
MAX_CANDIDATES: Final[int] = 5
#: 模型没给/给错 `importance_hint` 时的缺省权重（偏保守，低于 remember 默认的 1.00）。
DEFAULT_IMPORTANCE: Final[float] = 0.5

_ID_CARD_RE: Final[re.Pattern[str]] = re.compile(r"\d{17}[\dXx]")
_MOBILE_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_BANK_CARD_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
#: 三级式住址启发式：行政区 → 路/街/道/巷 → 门牌/楼栋。只命中行政区级别不算地址。
_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(
    r"[省市区县][^，。；,;]{0,12}?[路街道巷][^，。；,;]{0,12}?[号栋单元室楼]"
)

#: 推测语气（FR-MEM-01「只写长者明确表达的信息」）。这一层不能只靠 prompt：
#: 模型把「我儿子可能失业了」原样输出是**合理**行为（那句话确实说了儿子），
#: 错的是把它当事实写进库里 —— 所以判据放在代码里。
_SPECULATION_RE: Final[re.Pattern[str]] = re.compile(
    r"可能|也许|大概|估计|好像|似乎|说不定|兴许|没准|恐怕|怕是|搞不好|应该会"
    r"|会不会|听上去|看样子|推测|猜测|不确定|说不准"
)

#: 传闻/转述：消息的发出者不是长者本人。与「推测」分开判，因为两者的证据不同：
#: 一个是语气（可能），一个是来源（听说）——日志与测试要能分辨是哪一条挡掉的。
_HEARSAY_RE: Final[re.Pattern[str]] = re.compile(
    r"听说|据说|传闻|传说|听人说|别人说|人家说|大家都说|外面说|小区里说|他们说"
)

#: 第三方主体：家属与熟人。「别人家的事」不在长者本人画像范围内（PRD §4.9）。
_THIRD_PARTY_RE: Final[re.Pattern[str]] = re.compile(
    r"儿子|女儿|老伴|丈夫|妻子|爱人|儿媳|女婿|媳妇|孙子|孙女|外孙|外孙女"
    r"|哥哥|姐姐|弟弟|妹妹|亲戚|邻居|保姆|护工|老同事|老同学"
)

#: 家属的状态事件：就业、健康、婚姻、司法、经济这类**他人的**敏感境况。
#: 与 `_THIRD_PARTY_RE` 必须同时命中才丢 —— 单命中主体是「我女儿叫王芳」（要记），
#: 单命中事件是「我住院了」（长者本人的健康，属结构化健康记录，不由记忆承载）。
_FAMILY_EVENT_RE: Final[re.Pattern[str]] = re.compile(
    r"失业|下岗|病重|生病|住院|手术|确诊|癌症|化疗|离婚|离异|去世|过世"
    r"|欠债|赌博|坐牢|判刑|吵架|闹矛盾|辞职|换工作|被裁|裁员|考上|落榜"
    r"|不容易|可怜|命苦"
)

SYSTEM_PROMPT: Final[
    str
] = """你是「长期记忆提取器」，只做一件事：从一段对话里挑出**跨会话仍然成立**的关于长者的信息，输出 JSON 数组。

可以记（source="explicit"，长者明确陈述）：
- 亲人称谓与姓名（「我女儿叫王芳」「老伴姓李」）——只记**关系与姓名**，不记家属的境况；
- 家乡、籍贯、职业、退休前的单位；
- 口味与饮食禁忌（「不吃辣」「不吃香菜」）；
- 过敏（「对青霉素过敏」）；
- 固定习惯（「每天早上要喝杯热水」）；
- 忌讳话题（「别提我老伴」）。

可以记（source="inferred"，从反复行为推断）：
- 反复点的戏曲剧种/曲目（「总点越剧」）；
- 反复拒绝或回避的话题。

不要记：
- 一次性即时需求（「今天想听戏」「现在几点了」「帮我打电话」）；
- 客套话、寒暄、对助手的评价；
- 手机号、身份证号、银行卡号、完整住址（出现也一律不要输出）；
- 医疗诊断结论或病名断言（「您这是高血压」绝不记；长者自己说「对青霉素过敏」可以记）；
- 没有依据的猜测；
- **带推测语气的**（「可能」「大概」「估计」「好像」「说不定」）——是猜测，不是事实；
- **听来的**（「听说」「据说」「别人说」「大家都说」）——不是长者本人说的；
- **别人家的事**：家属/邻居的失业、生病、住院、离婚、欠债、去世等境况。例如长者说
  「我儿子可能失业了」，**不要输出任何条目**（既不记「可能失业」，也不记「儿子失业」）；
- 对家属的负面评价与情绪猜测（「他不孝顺」这类）。

输出格式（只输出 JSON 数组，不要 markdown 代码块、不要解释文字）：
[{"content": "一句话", "category": "private", "source": "explicit", "importance_hint": 0.8}]

字段要求：
- content：中文一句话，尽量 20 字以内，最长 200 字；
- category：private = 长者本人；family = 家属相关（称谓、姓名、探视习惯；家属的境况一律不要输出）；
- source：explicit = 明确陈述；inferred = 行为推断；
- importance_hint：0 到 1 的小数，越长期稳定越高；
- 最多 5 条；没有可记的内容就输出 []。"""


class SupportsChat(Protocol):
    """只用到 `LLMService.chat` 这一个方法；用 Protocol 便于测试注入假实现。

    刻意不 import `LLMService` 本体做类型：那会把「提取」绑在具体客户端上，
    而本模块只需要「给 messages、拿回一段文本」。
    """

    async def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply: ...


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """一条待落库的记忆候选。`frozen` + `slots`：候选只读，改是调用方的事。"""

    content: str
    category: MemoryCategory
    source: MemorySource
    importance_hint: float


def _build_messages(user_text: str, assistant_text: str | None) -> list[dict[str, str]]:
    """拼一轮提取请求；助手回复作为上下文帮助判定「是否只是寒暄」。"""
    conversation = f"长者：{user_text.strip()}"
    if assistant_text and assistant_text.strip():
        conversation += f"\n助手：{assistant_text.strip()}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": conversation},
    ]


def _strip_code_fence(raw: str) -> str:
    """模型常把 JSON 包在 ```json … ``` 里；剥掉围栏比「正则找数组」更少误伤。"""
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_items(raw: str) -> list[Any]:
    """把模型输出解析成列表；不是 JSON、不是数组、元素不是对象都收敛成空列表。"""
    text = _strip_code_fence(raw)
    if not text:
        return []
    parsed: Any
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # 兜底：模型爱在数组前后加「好的，以下是结果：」这类前缀，切出最外层数组再试一次。
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            parsed = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return []
    if not isinstance(parsed, list):
        return []
    return parsed


def _drop_reason(
    content: str,
    *,
    category: str | None = None,
    source: str | None = None,
) -> str | None:
    """返回丢弃原因（`None` 表示保留）。原因字符串只给测试与日志用，不外露给长者。

    顺序即优先级：身份号/联系方式排最前（出现了就一定不能落库，哪怕同时还有别的问题），
    之后才是语义类的过滤。`category` / `source` 是可选的，因为本函数的既有调用点
    （`test_memory_e2e` 的语义自检）只关心正文层；两者都给时才会判第 9 条
    「推断出来的家庭画像」。
    """
    if _ID_CARD_RE.search(content):
        return "id_card"
    if _MOBILE_RE.search(content):
        return "phone"
    if _BANK_CARD_RE.search(content):
        return "bank_card"
    if _ADDRESS_RE.search(content):
        return "address"
    for wording in FORBIDDEN_WORDING:
        if wording in content:
            return "diagnosis"
    if _SPECULATION_RE.search(content):
        return "speculation"
    if _HEARSAY_RE.search(content):
        return "hearsay"
    if _THIRD_PARTY_RE.search(content) and _FAMILY_EVENT_RE.search(content):
        return "third_party_event"
    if source == "inferred" and category == "family":
        return "inferred_family"
    if not MIN_CONTENT_LENGTH <= len(content) <= MAX_CONTENT_LENGTH:
        return "length"
    return None


def _as_importance(value: Any) -> float:
    """clamp 到 [0,1]；非数字/布尔/None 一律回缺省权重（不因为一个权重字段丢掉整条事实）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_IMPORTANCE
    return min(1.0, max(0.0, float(value)))


def _coerce(item: Any) -> MemoryCandidate | None:
    """把一条 JSON 元素收敛成候选；字段不合规就丢（不做「猜一个默认类别」）。"""
    if not isinstance(item, Mapping):
        return None
    raw_content = item.get("content")
    if not isinstance(raw_content, str):
        return None
    content = raw_content.strip()
    category = item.get("category")
    source = item.get("source")
    if category not in MEMORY_CATEGORIES or source not in MEMORY_SOURCES:
        return None
    if _drop_reason(content, category=category, source=source) is not None:
        return None
    return MemoryCandidate(
        content=content,
        category=cast("MemoryCategory", category),
        source=cast("MemorySource", source),
        importance_hint=_as_importance(item.get("importance_hint")),
    )


async def extract_memories(
    llm: SupportsChat,
    *,
    user_text: str,
    assistant_text: str | None = None,
) -> list[MemoryCandidate]:
    """从一轮对话提取记忆候选；**任何失败都返回 `[]`，绝不抛**。

    - `llm`：只需实现 `chat(messages, trace_id=...)`，生产传 `LLMService`；
    - `user_text`：长者这一轮说的话（空/纯空白直接返回 `[]`，不打网络）；
    - `assistant_text`：助手回复，参与判定「是不是只在寒暄」，可空。

    返回值顺序 = 模型输出顺序（已去重、已过滤、已截断到 `MAX_CANDIDATES`）。
    本函数**不打日志**：调用方持有 trace_id，记日志与落库都是调用方的事。
    """
    if not isinstance(user_text, str) or not user_text.strip():
        return []

    try:
        reply = await llm.chat(_build_messages(user_text, assistant_text))
    except Exception:  # 提取是旁路，任何异常都不能带回对话主链路
        return []

    text = getattr(reply, "text", "")
    if getattr(reply, "degraded", False):
        # 降级话术是固定中文句子，不可能是 JSON；提前返回省一次解析。
        return []
    if not isinstance(text, str) or not text.strip():
        return []

    candidates: list[MemoryCandidate] = []
    seen: set[str] = set()
    for item in _parse_items(text):
        candidate = _coerce(item)
        if candidate is None:
            continue
        key = candidate.content.strip()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


__all__ = [
    "DEFAULT_IMPORTANCE",
    "MAX_CANDIDATES",
    "MAX_CONTENT_LENGTH",
    "MEMORY_CATEGORIES",
    "MEMORY_SOURCES",
    "MIN_CONTENT_LENGTH",
    "SYSTEM_PROMPT",
    "MemoryCandidate",
    "SupportsChat",
    "extract_memories",
]
