"""`app/core/context_build.py` 的用例：预算裁剪的边界，不是「函数能跑」。

全部注入 `estimate_tokens=len`（每字符 1 token）——确定性估算才能断言精确数字。
默认的 `len/1.6` 只是兜底近似，拿它去反推断言等于用近似值验证近似值（模块 docstring 第三节第 3 条）。

覆盖的四条硬约束：
1. 前缀超预算**不被截断**，只记 `prefix_overflow`；
2. 裁剪顺序 知识 → 摘要 → 状态 → 最近轮，从最旧丢起，**最新一轮永不丢**；
3. `used_tokens` = 各层之和（含摘要/状态/知识段落的标题字节）；
4. 空层不产出占位段落，且 `dropped` 键恒定存在。

适配层（`build_agent_context`）另有三条**反向证据**——三处断言各对着一次「顺手优化」写成，
故意让它们互相咬住：

- `test_agent_prefix_is_the_caller_string_byte_for_byte`：前缀只要被拼接/改写一个字节就会红
  （拼变量 = 换前缀 = prompt cache 全废）；
- `test_agent_privacy_convergence_hides_the_whole_memory_layer`：收敛时若还照注记忆就红
  （同时漏了隐私与记忆两条语义）；
- `test_agent_memory_and_knowledge_are_distinct_layers`：知识层若被关掉渲染（或记忆被并进知识层）就红。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass

import pytest

# 真实常量（不是本地复制）：适配层的「前缀逐字节稳定」必须对着生产那串断言，
# 自己在测试里再造一个同形字符串就只能证明「我等于我」。
from app.agents.companion import AGENT_SYSTEM_PROMPT, PRIVACY_NOTICE_TEXT
from app.core.context import DIALOGUE_MAX_ROUNDS
from app.core.context_build import (
    DROPPED_KEYS,
    STATE_HEADER,
    SUMMARY_HEADER,
    ContextBudget,
    build_agent_context,
    build_context,
    default_estimate_tokens,
)
from app.services.knowledge_service import CONTEXT_FOOTER

PREFIX = "你是小满，陪长者聊天。"

#: 记忆块的渲染归 `companion._memory_block`（条数/截断口径在那边），这里只当「已渲染的块」用。
MEMORY_BLOCK = (
    "关于这位长者的已知记忆（仅供参考，不要逐条复述）：\n- 长者喜欢越剧《梁山伯与祝英台》。"
)


@dataclass(frozen=True)
class _Hit:
    """够 `format_knowledge_for_context` 用的最小命中形态（本模块只读这四个字段）。"""

    title: str
    effective_from: datetime.datetime
    authority_level: str
    content: str


def _hit(content: str, *, title: str = "午餐时间", position: int = 0) -> _Hit:
    return _Hit(
        title=title,
        effective_from=datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC),
        authority_level="institution",
        content=content,
    )


def _zero_dropped(**overrides: int) -> dict[str, int]:
    dropped = dict.fromkeys(DROPPED_KEYS, 0)
    dropped.update(overrides)
    return dropped


def _contents(ctx) -> list[str]:
    return [message["content"] for message in ctx.messages]


# ---------------------------------------------------------------- ① 前缀零裁剪


def test_prefix_over_reserve_is_never_truncated() -> None:
    prefix = "P" * 40
    budget = ContextBudget(total_tokens=50, prefix_reserve=10, recent_reserve=0, summary_reserve=0)

    ctx = build_context(
        prefix=prefix,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=(),
        budget=budget,
        estimate_tokens=len,
    )

    # 正文逐字节原样，一个字符都没少
    assert ctx.messages == ({"role": "system", "content": prefix},)
    # 只观测：40 - 10 = 30 token 溢出
    assert ctx.dropped == _zero_dropped(prefix_overflow=30)
    assert ctx.used_tokens == 40


@pytest.mark.parametrize(("length", "overflow"), [(10, 0), (9, 0), (11, 1)])
def test_prefix_overflow_counts_only_the_excess(length: int, overflow: int) -> None:
    budget = ContextBudget(total_tokens=4096, prefix_reserve=10)

    ctx = build_context(
        prefix="P" * length,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=(),
        budget=budget,
        estimate_tokens=len,
    )

    assert ctx.dropped["prefix_overflow"] == overflow


# ---------------------------------------------------------------- ② 最近轮


def test_recent_turns_are_windowed_to_max_rounds() -> None:
    turns = [(f"u{i}", f"a{i}") for i in range(DIALOGUE_MAX_ROUNDS + 2)]

    ctx = build_context(
        prefix=PREFIX,
        recent_turns=turns,
        summary=None,
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    assert ctx.dropped["recent"] == 2
    assert _contents(ctx)[1:] == [
        f"{role}{i}" for i in range(2, DIALOGUE_MAX_ROUNDS + 2) for role in ("u", "a")
    ]
    # 最新一轮一定在，且在最后
    assert ctx.messages[-2]["content"] == f"u{DIALOGUE_MAX_ROUNDS + 1}"
    assert ctx.messages[-1]["content"] == f"a{DIALOGUE_MAX_ROUNDS + 1}"


def test_oldest_rounds_are_dropped_first_when_budget_is_short() -> None:
    # 每轮 20 token（u 10 + a 10），额度 45 → 只留最新 2 轮（40）
    turns = [("u" * 10, "a" * 10) for _ in range(5)]
    budget = ContextBudget(total_tokens=100, prefix_reserve=0, recent_reserve=45)

    ctx = build_context(
        prefix="",
        recent_turns=turns,
        summary=None,
        state=None,
        knowledge_hits=(),
        budget=budget,
        estimate_tokens=len,
    )

    assert ctx.dropped["recent"] == 3
    assert _contents(ctx) == ["u" * 10, "a" * 10, "u" * 10, "a" * 10]
    assert ctx.used_tokens == 40


def test_newest_round_survives_even_if_it_alone_exceeds_budget() -> None:
    budget = ContextBudget(total_tokens=10, prefix_reserve=0, recent_reserve=1200)

    ctx = build_context(
        prefix="",
        recent_turns=[("u" * 50, "a" * 50)],
        summary=None,
        state=None,
        knowledge_hits=(),
        budget=budget,
        estimate_tokens=len,
    )

    # 最新一轮是硬保留：宁可 used_tokens 超标，也不静默改长者的原话
    assert ctx.messages[-2]["content"] == "u" * 50
    assert ctx.messages[-1]["content"] == "a" * 50
    assert ctx.dropped["recent"] == 0
    assert ctx.used_tokens == 100


# ---------------------------------------------------------------- ③ 知识先被裁


def test_knowledge_is_cut_before_recent_turns() -> None:
    # 最近一轮占 20，剩 190 给知识；每条渲染后 81，两条 + 页脚 181，只放得下两条
    budget = ContextBudget(total_tokens=210, prefix_reserve=0, recent_reserve=1200)
    hits = [_hit("x" * 30, position=i) for i in range(3)]

    ctx = build_context(
        prefix="",
        recent_turns=[("u" * 10, "a" * 10)],
        summary=None,
        state=None,
        knowledge_hits=hits,
        budget=budget,
        estimate_tokens=len,
    )

    assert ctx.messages[0]["content"] == "u" * 10  # 最近轮毫发无伤
    assert ctx.messages[-1]["role"] == "system"
    assert ctx.messages[-1]["content"].count("[参考资料 ") == 2
    assert ctx.dropped["knowledge"] == 1


def test_knowledge_never_skips_a_hit_to_grab_a_shorter_one() -> None:
    # 第一条就超预算 → 一条都不放（不跳过它去拿后面更短但更不相关的命中）
    hits = [_hit("x" * 200), _hit("短")]

    ctx = build_context(
        prefix="",
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=hits,
        budget=ContextBudget(total_tokens=100),
        estimate_tokens=len,
    )

    assert ctx.dropped["knowledge"] == 2
    assert not any("[参考资料 " in content for content in _contents(ctx))


def test_knowledge_block_ends_with_the_single_shared_footer() -> None:
    ctx = build_context(
        prefix="",
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=[_hit("午餐 11:30 开餐")],
        estimate_tokens=len,
    )

    knowledge = ctx.messages[-1]["content"]
    assert knowledge.startswith("[参考资料 1｜来源: 午餐时间")
    assert knowledge.endswith(CONTEXT_FOOTER)


# ---------------------------------------------------------------- ④ 空层不产出占位


@pytest.mark.parametrize(
    ("summary", "state"),
    [(None, None), ("", {}), ("   ", None)],
    ids=["none", "empty", "blank"],
)
def test_empty_summary_and_state_produce_no_paragraph(summary, state) -> None:
    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=summary,
        state=state,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    assert ctx.messages == ({"role": "system", "content": PREFIX},)
    assert ctx.dropped["summary"] == 0
    assert ctx.dropped["state"] == 0


def test_empty_knowledge_produces_no_paragraph() -> None:
    ctx = build_context(
        prefix=PREFIX,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    assert not any("[参考资料" in content for content in _contents(ctx))
    assert ctx.dropped["knowledge"] == 0


def test_all_empty_returns_prefix_only() -> None:
    ctx = build_context(
        prefix="P" * 7,
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    assert ctx.messages == ({"role": "system", "content": "P" * 7},)
    assert ctx.dropped == _zero_dropped()
    assert ctx.used_tokens == 7


def test_empty_prefix_emits_no_empty_system_message() -> None:
    ctx = build_context(
        prefix="",
        recent_turns=(),
        summary=None,
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    assert ctx.messages == ()
    assert ctx.used_tokens == 0


# ---------------------------------------------------------------- 顺序与计价


def test_message_order_is_prefix_summary_turns_state_knowledge() -> None:
    state = {"turn": "medication", "slots": {"plan_id": "PL-1"}}

    ctx = build_context(
        prefix="P",
        recent_turns=[("u1", "a1")],
        summary="更早聊过戏曲",
        state=state,
        knowledge_hits=[_hit("午餐 11:30")],
        estimate_tokens=len,
    )

    assert _contents(ctx)[0] == "P"
    assert _contents(ctx)[1] == f"{SUMMARY_HEADER}\n更早聊过戏曲"
    assert _contents(ctx)[2] == "u1"
    assert _contents(ctx)[3] == "a1"
    assert _contents(ctx)[4].startswith(STATE_HEADER)
    assert _contents(ctx)[5].startswith("[参考资料 1｜来源: ")

    # 状态 JSON 排序后再落盘：同一份状态每轮必须逐字节相同
    payload = json.loads(_contents(ctx)[4][len(STATE_HEADER) + 1 :])
    assert payload == state
    assert list(payload) == ["slots", "turn"]


def test_used_tokens_equals_the_sum_of_emitted_layer_bytes() -> None:
    ctx = build_context(
        prefix=PREFIX,
        recent_turns=[("u" * 10, "a" * 20)],
        summary="摘要正文",
        state={"turn": "medication"},
        knowledge_hits=[_hit("午餐 11:30 开餐")],
        estimate_tokens=len,
    )

    # 注入「每字符 1 token」后，这个等式就是预算账本本身
    assert ctx.used_tokens == sum(len(content) for content in _contents(ctx))


def test_used_tokens_includes_the_summary_header() -> None:
    summary_message = f"{SUMMARY_HEADER}\n摘要"
    ctx = build_context(
        prefix="P" * 6,
        recent_turns=[("u" * 4, "a" * 5)],
        summary="摘要",
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    # 6（前缀）+ 9（一轮）+ 摘要段落实际字节（含标题）——标题也占 prompt，不能漏计
    assert ctx.used_tokens == 6 + 9 + len(summary_message)


# ---------------------------------------------------------------- 整段丢弃（不切半句）


def test_summary_over_reserve_is_dropped_whole() -> None:
    budget = ContextBudget(total_tokens=1000, summary_reserve=10)

    ctx = build_context(
        prefix="",
        recent_turns=(),
        summary="s" * 50,
        state=None,
        knowledge_hits=(),
        budget=budget,
        estimate_tokens=len,
    )

    assert ctx.dropped["summary"] == 1
    assert not any(SUMMARY_HEADER in content for content in _contents(ctx))
    assert ctx.used_tokens == 0


def test_state_over_reserve_is_dropped_whole() -> None:
    budget = ContextBudget(total_tokens=1000, state_reserve=10)

    ctx = build_context(
        prefix="",
        recent_turns=(),
        summary=None,
        state={"a": "b" * 50},
        knowledge_hits=(),
        budget=budget,
        estimate_tokens=len,
    )

    # 不发半截 JSON：模型会把残缺结构当完整状态读
    assert ctx.dropped["state"] == 1
    assert ctx.messages == ()
    assert ctx.used_tokens == 0


# ---------------------------------------------------------------- 估算器


def test_default_estimator_is_chars_over_1_6_rounded_up() -> None:
    assert default_estimate_tokens("") == 0
    assert default_estimate_tokens("abcde") == 4  # 5 / 1.6 = 3.125
    assert default_estimate_tokens("a" * 16) == 10  # 16 / 1.6 恰好 10
    assert default_estimate_tokens("中文三字") == 3  # 4 / 1.6 = 2.5 → 3


def test_blank_halves_are_skipped_without_counting_as_dropped() -> None:
    ctx = build_context(
        prefix="P",
        recent_turns=[("", "a"), (" ", " ")],
        summary=None,
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    assert _contents(ctx) == ["P", "a"]
    assert ctx.dropped["recent"] == 0


# ---------------------------------------------------------------- 消息形态的最近层
#
# 生产的历史是消息（角色 + tool 结果 + 裁剪摘要），不是配对好的轮次；
# 这一层的用例保证 `recent_messages` 与原轮次路径等价：同样从最旧丢、末条硬保留。


def _msg(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def test_recent_messages_keep_roles_and_order() -> None:
    history = [_msg("user", "u1"), _msg("tool", "结果"), _msg("assistant", "a1")]

    ctx = build_context(
        prefix="P",
        recent_turns=(),
        recent_messages=history,
        summary=None,
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    # tool 角色不许被重新配对成 user/assistant：供应商协议里它带独立的工具调用语义
    assert [(m["role"], m["content"]) for m in ctx.messages] == [
        ("system", "P"),
        ("user", "u1"),
        ("tool", "结果"),
        ("assistant", "a1"),
    ]
    assert ctx.dropped["recent"] == 0
    assert ctx.used_tokens == 1 + 2 + 2 + 2


def test_recent_messages_drop_oldest_but_never_the_newest() -> None:
    history = [_msg("user", "u" * 10), _msg("assistant", "a" * 10), _msg("user", "u" * 100)]
    budget = ContextBudget(total_tokens=100, prefix_reserve=0, recent_reserve=15)

    ctx = build_context(
        prefix="",
        recent_turns=(),
        recent_messages=history,
        summary=None,
        state=None,
        knowledge_hits=(),
        budget=budget,
        estimate_tokens=len,
    )

    # 末条 100 token 硬保留（长者刚说的话不许被裁）；前两条各 10 token 放不进 15 → 全丢
    assert _contents(ctx) == ["u" * 100]
    assert ctx.dropped["recent"] == 2


def test_recent_messages_skip_blank_without_counting_it_as_dropped() -> None:
    history = [
        _msg("user", ""),
        _msg("system", "（已省略 21 条更早的对话）"),
        _msg("assistant", "a"),
    ]

    ctx = build_context(
        prefix="",
        recent_turns=(),
        recent_messages=history,
        summary=None,
        state=None,
        knowledge_hits=(),
        estimate_tokens=len,
    )

    # 空 content 正常上不到模型（供应商 400），剔除它不算「裁掉了历史」
    assert _contents(ctx) == ["（已省略 21 条更早的对话）", "a"]
    assert ctx.dropped["recent"] == 0


def test_recent_turns_and_recent_messages_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="只能给一个"):
        build_context(
            prefix="P",
            recent_turns=[("u", "a")],
            recent_messages=[_msg("user", "u")],
            summary=None,
            state=None,
            knowledge_hits=(),
        )


# ---------------------------------------------------------------- Agent 适配层
#
# 下面三处断言是「反向证据」：改动适配层里看似更优雅的实现，必须先在测试里红。


def _agent(messages: list[dict[str, str]]) -> list[str]:
    return [m["content"] for m in messages]


def test_agent_prefix_is_the_caller_string_byte_for_byte() -> None:
    messages = build_agent_context(
        history=[_msg("user", "你好")],
        prefix=AGENT_SYSTEM_PROMPT,
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        estimate_tokens=len,
    )

    # 变异③：一旦有人把 prefix 改成 f"...{身份}" 这类拼接，这行就红。
    # 前缀是 prompt cache 的键，多一个变量就等于每轮都 miss。
    assert messages[0] == {"role": "system", "content": AGENT_SYSTEM_PROMPT}
    assert _agent(messages).count(AGENT_SYSTEM_PROMPT) == 1
    assert messages[1] == {"role": "user", "content": "你好"}


def test_agent_privacy_convergence_hides_the_whole_memory_layer() -> None:
    messages = build_agent_context(
        history=(),
        prefix=AGENT_SYSTEM_PROMPT,
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=True,
        # 变异②：调用方传了记忆却仍被整层吞掉——收敛时连「有记忆」都不给模型看
        memory_block=MEMORY_BLOCK,
        estimate_tokens=len,
    )

    assert messages == [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "system", "content": PRIVACY_NOTICE_TEXT},
    ]
    assert not any("梁山伯" in content for content in _agent(messages))


def test_agent_memory_block_is_its_own_layer_before_history() -> None:
    messages = build_agent_context(
        history=[_msg("user", "刚才说到哪了")],
        prefix=AGENT_SYSTEM_PROMPT,
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        memory_block=MEMORY_BLOCK,
        estimate_tokens=len,
    )

    assert messages[1] == {"role": "system", "content": MEMORY_BLOCK}
    assert messages[2] == {"role": "user", "content": "刚才说到哪了"}
    assert not any(PRIVACY_NOTICE_TEXT in content for content in _agent(messages))


def test_agent_memory_and_knowledge_are_distinct_layers() -> None:
    messages = build_agent_context(
        history=(),
        prefix=AGENT_SYSTEM_PROMPT,
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        memory_block=MEMORY_BLOCK,
        knowledge_hits=[_hit("午餐 11:00 开饭。")],
        estimate_tokens=len,
    )
    contents = _agent(messages)

    memory_at = contents.index(MEMORY_BLOCK)
    # 变异①：关掉知识层渲染（例如 hits 被忽略）→ 这里 `next()` 直接 StopIteration。
    knowledge_at = next(i for i, content in enumerate(contents) if "[参考资料 1" in content)

    # 记忆是「这位长者」，知识是「这家机构怎么规定」：两层相邻但不合并
    assert memory_at < knowledge_at
    assert "梁山伯" not in contents[knowledge_at]
    assert "午餐 11:00" not in contents[memory_at]
    # 知识是最后一层：它最可裁，也最不该压住长者刚说的话
    assert knowledge_at == len(contents) - 1


def test_agent_control_layers_are_never_trimmed_by_budget() -> None:
    # 预算只剩前缀那 1 个 token，四层可裁内容全被压掉
    budget = ContextBudget(
        total_tokens=1, prefix_reserve=1, recent_reserve=0, summary_reserve=0, state_reserve=0
    )

    converged = build_agent_context(
        history=[_msg("user", "u" * 500)],
        prefix="P",
        privacy_notice="本轮为隐私收敛模式。",
        privacy_converged=True,
        budget=budget,
        estimate_tokens=len,
    )
    assert _agent(converged)[:2] == ["P", "本轮为隐私收敛模式。"]

    plain = build_agent_context(
        history=(),
        prefix="P",
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        memory_block=MEMORY_BLOCK,
        budget=budget,
        estimate_tokens=len,
    )
    # 隐私说明句与记忆块不是历史：再长的会话也不许被裁（裁了等于本轮没有指令）
    assert _agent(plain) == ["P", MEMORY_BLOCK]


def test_agent_no_memory_means_no_placeholder() -> None:
    for memory_block in (None, "", "   "):
        messages = build_agent_context(
            history=(),
            prefix="P",
            privacy_notice=PRIVACY_NOTICE_TEXT,
            privacy_converged=False,
            memory_block=memory_block,
            estimate_tokens=len,
        )

        # 「记忆：无」会被模型当事实读；没有就不产段落
        assert _agent(messages) == ["P"]


def test_agent_state_sits_after_history_and_before_knowledge() -> None:
    messages = build_agent_context(
        history=[_msg("user", "我要吃药了吗")],
        prefix="P",
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        state={"medication": {"pending": True}},
        knowledge_hits=[_hit("药单以机构为准。")],
        estimate_tokens=len,
    )
    contents = _agent(messages)

    state_at = next(i for i, content in enumerate(contents) if content.startswith(STATE_HEADER))
    knowledge_at = next(i for i, content in enumerate(contents) if "[参考资料 " in content)
    assert contents.index("我要吃药了吗") < state_at < knowledge_at


def test_agent_summary_precedes_history() -> None:
    messages = build_agent_context(
        history=[_msg("user", "继续")],
        prefix="P",
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=False,
        summary="更早聊过越剧",
        estimate_tokens=len,
    )
    contents = _agent(messages)

    assert contents[1].startswith(SUMMARY_HEADER)
    assert contents.index("继续") == 2


def test_agent_returns_plain_messages_in_the_caller_shape() -> None:
    messages = build_agent_context(
        history=[_msg("user", "你好")],
        prefix=AGENT_SYSTEM_PROMPT,
        privacy_notice=PRIVACY_NOTICE_TEXT,
        privacy_converged=True,
        estimate_tokens=len,
    )

    # 与 companion._build_context_messages 同形：list[dict{"role","content"}]
    assert isinstance(messages, list)
    assert all(isinstance(m, dict) and set(m) == {"role", "content"} for m in messages)
    assert all(isinstance(m["content"], str) for m in messages)
