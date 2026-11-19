"""进 prompt 的上下文组装：五层拼装 + 预算裁剪（DAG N23 · 技术设计文档 §6.3 line 902-910）。

## 一、和 `app/core/context.py` 的分工（别把两件事搅在一起）

`context.py::build_dialogue_messages` 解决的是「**前缀长什么样**」：人设、医疗边界、称呼、
时间怎么拼，以及 `[system, *history, user]` 的顺序。它不关心 token。
本模块解决的是「**给不给得下**」：调用方把已经拼好的 `prefix` 传进来，本模块只做预算与裁剪。
所以这里**不生成人设文本、不碰 persona 表**——生成逻辑只有一处，重复生成就等于两套前缀，
prompt cache 从第一天起就命中不了。

## 二、五层与顺序（TD §6.3）

| 层 | 内容 | 裁剪优先级 |
| --- | --- | --- |
| ① 固定前缀 | system（人设 + 规则 + 工具说明），逐字节稳定 | **零裁剪**（只观测溢出） |
| ② 历史摘要 | 更早轮次的滚动摘要 | 第三顺位被丢 |
| ③ 最近轮 | 最近 5 轮原文（`DIALOGUE_MAX_ROUNDS` 与 `context.py` 共用同一个常量） | 最后才动，最新一轮永不丢 |
| ④ 结构化状态 | `state_store.StateRecord.value`（当前任务/槽位/进行中子图） | 第四顺位（比摘要晚丢，见下） |
| ⑤ 知识命中 | `knowledge_service.KnowledgeHit` | **第一个被裁** |

消息顺序固定为「前缀 → 摘要 → 最近轮 → 状态 → 知识」：状态与知识离本轮提问最近，
模型读完参考资料就接当前这句；调用方在本函数返回的 messages **末尾**追加本轮用户话。

## 三、四条硬约束（每一条都有代价，不要「优化」）

1. **前缀绝不截断**。超了 `prefix_reserve` 只记 `dropped["prefix_overflow"]`（溢出 token 数），
   正文一个字节都不动。截断前缀会让供应商的 prompt cache 全废：4096 token 里 1200 的前缀
   每轮重算，省下的 token 远不够付多出来的钱——**超预算是小事，破缓存才是大事**。
   半年后想在这里加 `prefix[:n]` 的人，请先算一遍缓存命中率的账。
2. **裁剪顺序固定：知识 → 摘要 → 状态 → 最近轮，从最旧的轮开始丢**。
   反过来的灾难是「长者刚说完的那句被裁掉、三分钟前的还在」；所以**最新一轮与固定前缀
   一样是硬保留**：哪怕它自己就超出剩余额度，也照原样进（`used_tokens` 如实超标，
   不静默改用户原话）。
3. **token 估算可注入**。默认 `ceil(len/1.6)` 只是工程近似（中文约 1.5–2 字/token），
   测试一律注入「每字符 1 token」的确定性实现，断言精确数字——**不要用 `len/1.6` 去反推断言**，
   那是拿近似值验证近似值。
4. **空层不产出占位**。知识为空就不写「知识：无」，状态为空就不写「状态：{}」——
   模型会把占位当事实读，这是幻觉最常见的入口。同理被裁掉的层**整段消失**，
   不留空标题。

## 四、`dropped` 的口径（可观测）

`{"prefix_overflow": 溢出 token 数, "summary": 0/1, "state": 0/1, "recent": 丢掉轮数,
"knowledge": 丢掉条数}` —— 五个键**恒定存在**（没丢就是 0），便于直接进 metrics。
注意 `prefix_overflow` 的单位是 token，其余是「条/轮/段」，不是同一种计数。

## 五、纯函数

不连库、不读 Redis、不调 LLM、不打日志。唯一的模块外依赖是知识段落渲染器
`knowledge_service.format_knowledge_for_context`（先过滤后排序、带 `CONTEXT_FOOTER`），
在函数内 import：`app/core/` 是底座层，模块级 import `app/services/` 会把依赖方向拉反
（AGENTS §2），也容易在装配期形成环。渲染器本身是纯函数，复用它可以避免第二种「参考资料」
格式——两套格式意味着两条注入防线。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.core.context import DIALOGUE_MAX_ROUNDS

#: 默认 token 估算：中文约 1.5–2 字/token，取 1.6 作折中；向上取整保证「宁可多算」。
#: 只是兜底近似，精确断言请注入确定性实现（见模块 docstring 第三节）。
_CHARS_PER_TOKEN: Final[float] = 1.6

#: 摘要段标题。措辞固定：它属于 prompt 正文，改字等于改行为，要连同用例一起改。
SUMMARY_HEADER: Final[str] = (
    "更早对话的摘要（用来延续上下文；不要逐字复述，也不要把它当成本轮刚说过的话）："
)

#: 状态段标题。明确「服务端维护」，压住模型自行编造槽位与进度的倾向。
STATE_HEADER: Final[str] = (
    "当前会话状态（服务端维护的 JSON，只作参考；不要编造字段，也不要据它推断还没发生的操作）："
)

#: `dropped` 的固定键集合（全部预置 0，便于直接上报指标）。
DROPPED_KEYS: Final[tuple[str, ...]] = (
    "prefix_overflow",
    "recent",
    "summary",
    "state",
    "knowledge",
)


def default_estimate_tokens(text: str) -> int:
    """`ceil(len(text) / 1.6)`；空串记 0。**只作兜底**，正式链路应注入与模型一致的分词器。"""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """各层额度（token）。前缀/最近轮/摘要是**上限**，剩余自动给知识层。

    四个 reserve 合计 3300 < 4096，差额 796 是知识层的默认额度；前缀溢出直接吃这份差额。
    """

    total_tokens: int = 4096
    #: 固定前缀的“额度”，超了只记 `prefix_overflow`，绝不截断（见模块 docstring 第三节）。
    prefix_reserve: int = 1200
    #: 最近轮上限；它比摘要与知识优先，7 轮只会留最新 5 轮（`DIALOGUE_MAX_ROUNDS`）。
    recent_reserve: int = 1200
    #: 摘要上限；整段超了宁可整段丢，也不切半句（半截摘要比没有摘要更危险）。
    summary_reserve: int = 600
    #: 结构化状态上限；同样整段丢，不发半截 JSON。
    state_reserve: int = 300


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """组装结果。`messages` 不含本轮用户话——调用方在末尾自行追加一句 `user`。"""

    prefix: str
    messages: tuple[dict[str, str], ...]
    #: 每层被裁掉多少（口径见模块 docstring 第四节；键恒定存在）。
    dropped: dict[str, int]
    #: 实际写入的 token 数 = 各层之和。前缀/最新轮硬保留时可能 > `budget.total_tokens`。
    used_tokens: int


def build_context(
    *,
    prefix: str,
    recent_turns: Sequence[tuple[str, str]],
    recent_messages: Sequence[Mapping[str, Any]] | None = None,
    summary: str | None,
    state: Mapping[str, Any] | None,
    knowledge_hits: Sequence[Any],
    budget: ContextBudget = ContextBudget(),
    estimate_tokens: Callable[[str], int] | None = None,
) -> BuiltContext:
    """把五层上下文按预算拼成 OpenAI 兼容 `messages`。

    参数：

    - `prefix`：已拼好的固定前缀（`context.py::_build_system_prompt` 的产物），**原样保留**；
    - `recent_turns`：`(user, assistant)` 轮次序列，旧 → 新；超过 5 轮先按窗口裁，再从最旧丢；
    - `recent_messages`：与 `recent_turns` 互斥的另一种输入形状——历史本来就是消息
      （带 `tool` 结果、裁剪摘要等，角色不能重新配对成轮次）时用它，角色原样透传；
      同样从最旧丢、末条硬保留；
    - `summary`：更早轮次的滚动摘要，`None`/空串表示没有这一层；
    - `state`：`StateRecord.value` 之类的结构化状态，`None`/空 dict 表示没有；
    - `knowledge_hits`：已按相关度排好序的命中（`KnowledgeHit` 或同形对象，本模块只读
      `title/effective_from/authority_level/content`，具体渲染交给知识服务）；
    - `budget`：各层额度；`estimate_tokens`：注入的估算器，测试传「每字符 1 token」。

    返回的 `messages` 顺序为「前缀 → 摘要 → 最近轮 → 状态 → 知识」，空层不出现。
    """
    estimate = estimate_tokens or default_estimate_tokens
    dropped: dict[str, int] = dict.fromkeys(DROPPED_KEYS, 0)

    # ① 固定前缀：整段进，只记录溢出（理由见模块 docstring 第三节第 1 条）。
    prefix_tokens = estimate(prefix)
    if prefix_tokens > budget.prefix_reserve:
        dropped["prefix_overflow"] = prefix_tokens - budget.prefix_reserve
    remaining = max(0, budget.total_tokens - prefix_tokens)

    # ③ 最近轮：先按 5 轮窗口裁，再在额度内从最旧丢；最新一轮硬保留。
    #    `recent_messages` 是给「历史本来就是消息」的场景（含 tool 结果与裁剪摘要，
    #    角色不能重新配对），与 `recent_turns` 互斥——实现同一个层，两种输入形状。
    if recent_messages is not None and recent_turns:
        raise ValueError("recent_turns 与 recent_messages 只能给一个（同一层两种输入形状）")
    recent_cap = min(budget.recent_reserve, remaining)
    kept_messages: list[tuple[str, str]] = []
    kept_turns: list[tuple[str, str]] = []
    kept_recent_tokens = 0
    if recent_messages is not None:
        kept_messages, kept_recent_tokens, dropped_messages = _fit_recent_messages(
            recent_messages, recent_cap, estimate
        )
        dropped["recent"] += dropped_messages
    else:
        rounds = [
            (user, assistant)
            for user, assistant in recent_turns
            if user.strip() or assistant.strip()
        ]
        if len(rounds) > DIALOGUE_MAX_ROUNDS:
            dropped["recent"] += len(rounds) - DIALOGUE_MAX_ROUNDS
            rounds = rounds[-DIALOGUE_MAX_ROUNDS:]
        for turn in reversed(rounds):
            cost = _turn_tokens(turn, estimate)
            # `kept_turns` 非空才允许因超预算中断：最新一轮无论如何都要在（docstring 第三节第 2 条）。
            if kept_turns and kept_recent_tokens + cost > recent_cap:
                break
            kept_turns.insert(0, turn)
            kept_recent_tokens += cost
        dropped["recent"] += len(rounds) - len(kept_turns)
    remaining = max(0, remaining - kept_recent_tokens)

    # ④ 结构化状态：排在摘要之前抢额度——它是「进行到哪一步」的锚点，丢了下轮会重复问；
    #    摘要只是更早轮次的压缩件，丢了顶多上下文短一点。
    state_text = _render_state(state)
    state_tokens = 0
    if state_text:
        state_tokens = estimate(state_text)
        if state_tokens <= min(budget.state_reserve, remaining):
            remaining -= state_tokens
        else:
            state_text, state_tokens = "", 0
            dropped["state"] = 1

    # ② 历史摘要：整段判定，超了就整段丢（不切半句：断句会被模型当完整事实续写）。
    #    计价按**实际发出去的那段字节**（含标题），否则 used_tokens 会系统性少算一个标题。
    summary_text = (summary or "").strip()
    summary_message = f"{SUMMARY_HEADER}\n{summary_text}" if summary_text else ""
    summary_tokens = 0
    if summary_message:
        summary_tokens = estimate(summary_message)
        if summary_tokens <= min(budget.summary_reserve, remaining):
            remaining -= summary_tokens
        else:
            summary_message, summary_tokens = "", 0
            dropped["summary"] = 1

    # ⑤ 知识：最后一个吃余量，也是第一个被裁的层；按相关度顺序取，不跳条。
    knowledge_text, knowledge_tokens, kept_hits = _fit_knowledge(
        list(knowledge_hits), remaining, estimate
    )
    dropped["knowledge"] = len(knowledge_hits) - kept_hits

    messages: list[dict[str, str]] = []
    if prefix:
        messages.append({"role": "system", "content": prefix})
    if summary_message:
        messages.append({"role": "system", "content": summary_message})
    for role, text in kept_messages:
        messages.append({"role": role, "content": text})
    for user, assistant in kept_turns:
        if user.strip():
            messages.append({"role": "user", "content": user.strip()})
        if assistant.strip():
            messages.append({"role": "assistant", "content": assistant.strip()})
    if state_text:
        messages.append({"role": "system", "content": state_text})
    if knowledge_text:
        messages.append({"role": "system", "content": knowledge_text})

    used = prefix_tokens + kept_recent_tokens + state_tokens + summary_tokens + knowledge_tokens
    return BuiltContext(
        prefix=prefix,
        messages=tuple(messages),
        dropped=dropped,
        used_tokens=used,
    )


def _turn_tokens(turn: tuple[str, str], estimate: Callable[[str], int]) -> int:
    """一轮的 token = 两句各自估算之和（空句不产出消息，也不计价）。"""
    user, assistant = turn
    return estimate(user.strip()) + estimate(assistant.strip())


def _fit_recent_messages(
    messages: Sequence[Mapping[str, Any]],
    budget_tokens: int,
    estimate: Callable[[str], int],
) -> tuple[list[tuple[str, str]], int, int]:
    """消息形态的最近层：从最旧丢，末条硬保留；返回 `(保留的(role,text), token, 丢掉条数)`。

    角色**原样透传**（`tool` 结果、裁剪摘要这类 system 消息都不许被重新配对成轮次），
    空 content 先剔除——它既占不了 token 也没法表达，留着只会让供应商 400。
    """
    normalized: list[tuple[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user").strip() or "user"
        text = _message_text(message.get("content")).strip()
        if not text:
            continue
        normalized.append((role, text))

    kept: list[tuple[str, str]] = []
    used = 0
    for role, text in reversed(normalized):
        cost = estimate(text)
        # `kept` 非空才允许因超预算中断：最后一条无论如何都要在（同轮次的硬保留规则）。
        if kept and used + cost > budget_tokens:
            break
        kept.insert(0, (role, text))
        used += cost
    return kept, used, len(normalized) - len(kept)


def _message_text(content: Any) -> str:
    """把一条消息的 content 归一成文本（兼容 str、OpenAI 多段 content、None）。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(str(block["text"]))
        return "\n".join(parts)
    return str(content)


def _render_state(state: Mapping[str, Any] | None) -> str:
    """结构化状态渲染成 `标题 + 排序后的 JSON`。

    `sort_keys=True` 是刻意的：同一份状态每轮必须逐字节相同，否则状态段自己就成了
    不稳定内容。`default=str` 兜住 datetime 之类的非 JSON 值——宁可显示成字符串，
    也不能因为一个字段让整轮对话 500。
    """
    if not state:
        return ""
    payload = json.dumps(dict(state), ensure_ascii=False, sort_keys=True, indent=2, default=str)
    return f"{STATE_HEADER}\n{payload}"


def _fit_knowledge(
    hits: list[Any],
    budget_tokens: int,
    estimate: Callable[[str], int],
) -> tuple[str, int, int]:
    """按顺序把命中塞进知识层，返回 `(渲染文本, token 数, 放入条数)`。

    每加一条都用**真实渲染结果**重算 token（命中数 ≤ `MAX_CONTEXT_HITS`，O(n²) 无所谓）：
    这样标题、来源、`CONTEXT_FOOTER` 的字节都算进预算，不会出现「按正文估完、结果 footer
    把额度撑爆」。任一条塞不下就停——不跳过它去拿后面更短但更不相关的命中。
    """
    if not hits or budget_tokens <= 0:
        return "", 0, 0

    from app.services.knowledge_service import format_knowledge_for_context

    accepted = 0
    text = ""
    tokens = 0
    for position in range(1, len(hits) + 1):
        candidate = format_knowledge_for_context(hits[:position])
        candidate_tokens = estimate(candidate)
        if candidate_tokens > budget_tokens:
            break
        accepted = position
        text = candidate
        tokens = candidate_tokens
    return (text, tokens, accepted) if accepted else ("", 0, 0)


def build_agent_context(
    *,
    history: Sequence[Mapping[str, Any]] = (),
    prefix: str,
    privacy_notice: str,
    privacy_converged: bool,
    memory_block: str | None = None,
    knowledge_hits: Sequence[Any] = (),
    knowledge_note: str | None = None,
    state: Mapping[str, Any] | None = None,
    summary: str | None = None,
    budget: ContextBudget = ContextBudget(),
    estimate_tokens: Callable[[str], int] | None = None,
) -> list[dict[str, str]]:
    """Agent 侧适配：`build_context` 的五层 + 两个**不受预算约束**的控制位。

    产出与 `companion._build_context_messages` 同形（`list[{"role","content"}]`），
    调用方在末尾自行追加本轮用户话。

    ## 控制位（隐私说明 / 记忆块）为什么单独拿出来

    它们是**本轮的指令**，不是历史：模型看不见隐私说明句就不会守（那是隐私收敛
    唯一的生效方式），而记忆块一旦被裁，长者就得把同一件事再说一遍。
    所以两位都不走 `build_context` 的预算，直接插在前缀之后——
    **再长的会话也不许把它们裁出去**。这也意味着它们不计入 `used_tokens`
    （`build_context` 只管四层可裁内容，预算没有「给控制位留位」这件事）。

    ## 隐私收敛

    `privacy_converged=True` 时**只插说明句、记忆层整层不注入**——即使调用方把
    `memory_block` 传了进来也不注（「有没有记忆」都不给模型看的机会）。
    两句同插会让模型在「不要说旁人细节」与若干他人事实之间自相矛盾。

    ## 参数

    - `prefix`：**逐字节原样**进第一条 message（人设 prompt）；这里不做任何拼接，
      拼了就等于换前缀，prompt cache 从第一轮起失效。
    - `history`：历史消息（`{"role","content"}`，含 `tool` 与裁剪摘要），角色原样透传；
      预算触发时从**最旧**丢，最后一条硬保留。
    - `privacy_notice`：隐私收敛时插入的固定说明句（由调用方给，本模块不复制文案）。
    - `memory_block`：**已渲染**的记忆块（`companion._memory_block` 的产物）。渲染留给
      调用方是为了不出现第二套记忆格式（条数/截断口径在那边，重复一份必然漂移）；
      `None`/空串表示本轮没有记忆，**不产出占位段落**。
    - `knowledge_hits` / `state` / `summary` / `budget` / `estimate_tokens`：同 `build_context`。
    - `knowledge_note`：**已给好的**一行短提示（例如「本轮未检索到机构知识……」），
      非空时作为**最后一条** system 消息追加在知识层之后，`None`/空白表示不产出。
      它和两个控制位一样**不走预算**：提示句的价值恰恰在于「知识层没命中/被裁」时
      告诉模型别用常识补造，让预算把它裁掉等于白写。渲染留给调用方（文案在
      `companion`，本模块不复制第二份）。**只提示，不造来源**——没有任何
      `[参考资料 …]` 块时，模型才没有可援引的假标题。

    顺序：前缀 → 控制位 → 摘要 → 历史 → 状态 → 知识 → 未命中提示。
    """
    built = build_context(
        prefix=prefix,
        recent_turns=(),
        recent_messages=history,
        summary=summary,
        state=state,
        knowledge_hits=knowledge_hits,
        budget=budget,
        estimate_tokens=estimate_tokens,
    )
    messages = list(built.messages)
    # 隐私收敛优先于记忆：收敛时连「有记忆」这件事都不给模型看（见 docstring）。
    control = privacy_notice.strip() if privacy_converged else (memory_block or "").strip()
    if control:
        index = 1 if prefix and messages and messages[0]["content"] == prefix else 0
        messages.insert(index, {"role": "system", "content": control})
    # 未命中提示固定压在最后（知识层之后）：它描述的是「本轮知识面为空的后果」，
    # 排在参考资料前面会让模型先读到「查不到」再读到资料，自相矛盾。
    note = (knowledge_note or "").strip()
    if note:
        messages.append({"role": "system", "content": note})
    return messages


__all__ = [
    "DROPPED_KEYS",
    "STATE_HEADER",
    "SUMMARY_HEADER",
    "BuiltContext",
    "ContextBudget",
    "build_agent_context",
    "build_context",
    "default_estimate_tokens",
]
