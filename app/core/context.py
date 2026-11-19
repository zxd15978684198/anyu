"""请求级上下文：`trace_id` 的载体 + 对话上下文组装（迭代 4 · Agent 上下文组装）。

## 一、trace_id（迭代 1 起）

为什么用 `contextvars` 而不是参数透传：日志格式化器与中间件不在同一条调用链上，
只有 contextvar 能让「任何一行日志」自动拿到当前请求的 trace_id
（技术文档 §1.2：全链路统一 trace_id）。

## 二、对话上下文组装（迭代 4 收口 W2 · G-D2）

技术设计文档 line 1028 把「Agent 上下文组装」落在这里，line 847-857 给出**五层**结构：

| 层 | 内容 | 本文件负责 |
| --- | --- | --- |
| ① 固定前缀 | system = 人设 personality + tone_examples + 称呼 + 当前时间 + 医疗边界 | ✅ `build_dialogue_messages` |
| ② 短期记忆 | 最近 5 轮原文 | ✅ 由 `history` 形参传入（存储读写在 `services/dialogue_service.py`） |
| ③ 历史摘要 | 更早轮次滚动摘要 | ❌ 不排期（计划 §8） |
| ④ 结构化状态 | Redis thread_id → state dict | ❌ 不排期（计划 §8） |
| ⑤ 长期记忆 | pgvector top5，强制 `WHERE elder_id` | ❌ 不排期（迭代 9） |

本模块**只做参数组装**：不查 DB、不读 Redis、不发 HTTP、不散读环境变量
（`now` 与 `persona` 全部由调用方传入）。这样它可以在任何时间、任何进程里被单测，
也不会因为「顺手查一下库」把纯函数变成 I/O 节点。

## 三、prompt cache 的硬约束（TD line 847）

固定前缀必须**逐字节稳定**才能命中供应商的 prompt cache。三条纪律：

1. 会变的东西（当前时间）**只放在前缀最末尾一处**，不放在开头；
2. `tone_examples` 的顺序即稳定顺序，不做排序/去重（那会让两次调用产生不同字节）；
3. `elder_id` **绝不进 prompt**（TD line 857 注入原则：`elder_id` 只经 `runtime_context`
   进工具参数，不进 prompt 正文）—— 称呼用 `display_name`，那本来就是给人看的。

违反第 1 条最贵：把时间挪到 system 开头，缓存命中率会从「几乎全中」掉到 0。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

# 对外约定：请求头与响应头同名
TRACE_HEADER = "X-Trace-Id"

# 只接受 8~64 位安全字符，防止把换行/引号塞进日志（日志注入）
_TRACE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    """生成新 trace_id（32 位十六进制，与 OTel trace_id 长度同量级）。"""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id_ctx.get()


def set_trace_id(trace_id: str) -> Token[str | None]:
    return _trace_id_ctx.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _trace_id_ctx.reset(token)


def is_valid_trace_id(value: str | None) -> bool:
    """上游透传的 trace_id 是否可信；不可信一律重新生成，不报错。"""
    # 显式判空而非 bool(value) 短路：mypy 无法借布尔短路收窄 value，原写法报 union-attr
    if not value:
        return False
    return bool(_TRACE_PATTERN.match(value.strip()))


_thread_id_ctx: ContextVar[str | None] = ContextVar("thread_id", default=None)
_turn_id_ctx: ContextVar[str | None] = ContextVar("turn_id", default=None)


def get_thread_id() -> str | None:
    """当前协程的 `thread_id`（LangGraph 线程，只由服务端注入，模型/客户端传的不用）。"""
    return _thread_id_ctx.get()


def set_thread_id(thread_id: str | None) -> Token[str | None]:
    return _thread_id_ctx.set(thread_id)


def reset_thread_id(token: Token[str | None]) -> None:
    _thread_id_ctx.reset(token)


def get_turn_id() -> str | None:
    """当前协程的 `turn_id`（一轮对话的编号；一轮 = 一次终稿 + 一条回复）。"""
    return _turn_id_ctx.get()


def set_turn_id(turn_id: str | None) -> Token[str | None]:
    return _turn_id_ctx.set(turn_id)


def reset_turn_id(token: Token[str | None]) -> None:
    _turn_id_ctx.reset(token)


# ======================================================================
# 二、对话上下文组装（迭代 4 收口 W2 · 技术设计文档 line 1028 / §6.3 line 847-857）
# ======================================================================

#: 短期记忆窗口：最近 5 轮（TD §6.3 ②）。一轮 = user + assistant 两条消息。
DIALOGUE_MAX_ROUNDS: Final[int] = 5

#: 展示与 prompt 内的时间统一用 `Asia/Shanghai`（AGENTS §8：存储 UTC、展示本地）。
_DISPLAY_TIMEZONE: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")

#: 医疗边界声明（PRD §4 红线：不做诊断、不替代 SOS）。
#: 写成常量而不是拼进每个分支，是为了让它在 prompt 里逐字节稳定 —— 这段是固定前缀的一部分。
MEDICAL_BOUNDARY: Final[str] = (
    "你只做陪伴与提醒，不做医疗诊断。长者说到身体不适时，先关心，"
    "再建议「问一下医生或护工」，不要说「您这是××病」。"
    "遇到紧急情况（胸口疼、说不出话、摔倒）时，提醒长者立刻按呼叫铃或喊人，"
    "并说明你会同时通知护工。"
)

#: generic（未识别身份）时的称呼兜底。**不含任何长者个人信息**（AC-28：通用模式不写个人数据）。
GENERIC_ADDRESS: Final[str] = "您好"

_SYSTEM_PREFIX_HEAD: Final[str] = "你是一位名叫「{name}」的数字人陪伴助手，正在陪长者聊天。"


@dataclass(frozen=True, slots=True)
class DialogueIdentity:
    """本轮说话人身份（服务端判定结果的**只读投影**）。

    `source` 的四值对齐 `conversation_threads.identity_source` 的 CHECK 约束与
    `app/services/identity_service.py::IdentitySource`：`device` / `voice` / `confirm` / `generic`。

    **generic 时强制 `elder_id=None`**：AC-28 要求「仍不确定进通用模式**不写个人数据**」，
    而库里 `conversation_threads` 的复合 CHECK 也是这个语义
    （`(generic AND elder_id IS NULL) OR (非generic AND elder_id IS NOT NULL)`）。
    这里在构造时就把不合法的组合打回去，比等到 INSERT 时才 500 好。
    """

    source: str
    elder_id: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.source not in ("device", "voice", "confirm", "generic"):
            raise ValueError(f"unknown identity source: {self.source!r}")
        if self.source == "generic":
            # generic 的身份在**任何**场景下都不能带出长者身份；这里覆盖掉调用方的误传，
            # 而不是 raise —— 上游判定为 generic 就该照 generic 走，不应让一轮对话失败。
            object.__setattr__(self, "elder_id", None)
            object.__setattr__(self, "display_name", None)
        elif not self.elder_id:
            raise ValueError(f"identity source {self.source!r} requires elder_id")

    @property
    def is_generic(self) -> bool:
        return self.source == "generic"


@dataclass(frozen=True, slots=True)
class PersonaContext:
    """人设（来自 `personas` 表：`name` / `personality` / `tone_examples` / `voice_id`）。

    `tone_examples` 保持传入顺序，**不排序、不去重**：固定前缀要逐字节稳定，
    任何「为了好看」的重排都会让 prompt cache 失效（TD line 847）。`voice_id` 不拼进
    prompt，只供 TTS 选音色；默认 `None` 保持旧调用点不用改。
    """

    name: str
    personality: str
    tone_examples: Sequence[str] = field(default_factory=tuple)
    voice_id: str | None = None


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    """一轮对话（user 一句 + assistant 一句），供短期记忆使用。"""

    user: str
    assistant: str


def build_dialogue_messages(
    *,
    identity: DialogueIdentity,
    user_text: str,
    history: Sequence[DialogueTurn] = (),
    persona: PersonaContext | None = None,
    now: datetime,
) -> list[dict[str, str]]:
    """组装 OpenAI 兼容的 `messages`：`[system, *history, user]`。

    三条硬约束（写在签名里而不是靠调用方自觉）：

    1. **纯函数**：`now` / `persona` / `history` 全部由调用方传入，本函数不碰 I/O；
    2. **固定前缀逐字节稳定**：`system` 的第一段（人设 + 称呼 + 医疗边界）与传入的
       `now`、`history` 无关；时间只出现在 `system` 的**最后一行**；
    3. **`elder_id` 不进 prompt**：只使用 `identity.display_name` 作称呼。

    `history` 超过 `DIALOGUE_MAX_ROUNDS` 轮时**从最旧的开始截断**（保留最近的）。
    调用方可能传全量历史，截断放在这里可以保证「窗口口径只有一处」。
    """
    system_text = _build_system_prompt(identity=identity, persona=persona, now=now)

    messages: list[dict[str, str]] = [{"role": "system", "content": system_text}]
    for turn in _window(history):
        # 空串轮次整轮跳过：任何一条空 content 都会让部分供应商直接 400。
        # 只跳过空的半边，不强行配平 —— 配平会凭空造一句用户没说过的话。
        if turn.user.strip():
            messages.append({"role": "user", "content": turn.user.strip()})
        if turn.assistant.strip():
            messages.append({"role": "assistant", "content": turn.assistant.strip()})

    if user_text.strip():
        messages.append({"role": "user", "content": user_text.strip()})
    return messages


def _window(history: Sequence[DialogueTurn]) -> Sequence[DialogueTurn]:
    """只保留最近 `DIALOGUE_MAX_ROUNDS` 轮；更早的丢弃（TD §6.3 ②「最近 5 轮」）。"""
    if len(history) <= DIALOGUE_MAX_ROUNDS:
        return history
    return history[-DIALOGUE_MAX_ROUNDS:]


def _build_system_prompt(
    *,
    identity: DialogueIdentity,
    persona: PersonaContext | None,
    now: datetime,
) -> str:
    """拼固定前缀。**每一段的先后顺序都是契约**（见模块 docstring 三条纪律）。

    段序：人设名 / 性格 / 口吻示例 / 医疗边界 / 称呼 / 当前时间。
    最后两段会随调用变化（称呼按人、时间按分钟），但它们**在末尾**，
    前面的字节稳定 → 供应商的 prefix cache 仍能命中前面那一大段。
    """
    name = (persona.name if persona is not None else "") or "小满"
    lines: list[str] = [_SYSTEM_PREFIX_HEAD.format(name=name)]

    personality = (persona.personality if persona is not None else "").strip()
    if personality:
        lines.append(f"你的性格：{personality}")

    examples = [item.strip() for item in (persona.tone_examples if persona is not None else ())]
    examples = [item for item in examples if item]
    if examples:
        # 固定顺序、固定标点：同一个人设每次拼出来的字节完全相同。
        lines.append("说话示例：" + "；".join(f"「{item}」" for item in examples))

    lines.append(MEDICAL_BOUNDARY)
    lines.append(_address_line(identity))
    lines.append(_time_line(now))
    return "\n".join(lines)


def _address_line(identity: DialogueIdentity) -> str:
    """称呼。generic 用通用称呼，**不带任何长者信息**（AC-28）。"""
    if identity.is_generic:
        return f"当前对话对象：{GENERIC_ADDRESS}（尚未确认身份，不要提及任何个人信息）。"
    display = (identity.display_name or "").strip()
    if not display:
        # 有 elder_id 但没传 display_name：用中性称呼，绝不回退成把 id 写进 prompt
        # （TD line 857：elder_id 不进 prompt；32 位 hex 出现在 system 里既是隐私也是噪音）。
        return "当前对话对象：长者（用「您」称呼）。"
    return f"当前对话对象：{display}（用「您」称呼）。"


def _time_line(now: datetime) -> str:
    """当前时间。**固定放在 system 的最后一行** —— 这是 prompt cache 的边界线。"""
    local = (
        now.astimezone(_DISPLAY_TIMEZONE) if now.tzinfo else now.replace(tzinfo=_DISPLAY_TIMEZONE)
    )
    weekday = "周" + "一二三四五六日"[local.weekday()]
    return f"现在时间：{local:%Y-%m-%d %H:%M}（{weekday}）。"


__all__ = [
    "DIALOGUE_MAX_ROUNDS",
    "GENERIC_ADDRESS",
    "MEDICAL_BOUNDARY",
    "DialogueIdentity",
    "DialogueTurn",
    "PersonaContext",
    "build_dialogue_messages",
    "get_thread_id",
    "get_trace_id",
    "get_turn_id",
    "is_valid_trace_id",
    "new_trace_id",
    "reset_thread_id",
    "reset_trace_id",
    "reset_turn_id",
    "set_thread_id",
    "set_trace_id",
    "set_turn_id",
]
