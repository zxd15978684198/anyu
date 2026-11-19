"""记忆提取的单元测试（纯函数，不连库、不跑网络）。

覆盖三类断言：

1. **正常解析**：合法 JSON 数组 → 字段与 `source` 正确、`importance_hint` 被 clamp；
2. **失败即空**：非 JSON / 非数组 / 空串 / 降级话术 / 调用抛异常，一律 `[]` 且**不抛**；
3. **硬过滤**：身份证 / 手机号 / 银行卡 / 完整住址 / 诊断口径 / 超长 / 重复 / 非法类别。

假 LLM 只实现 `chat(messages, *, trace_id=None) -> LLMReply` 一个方法，正好对上
`memory_extraction.SupportsChat` 这个 Protocol——用真 `LLMReply` 而不是 MagicMock，
是为了让「降级话术」这条分支走真实字段，而不是被假对象糊过去。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.services.llm_service import LLMReply
from app.services.memory_extraction import (
    DEFAULT_IMPORTANCE,
    MAX_CANDIDATES,
    MemoryCandidate,
    extract_memories,
)


class FakeLLM:
    """可控的假客户端：要么返回固定内容，要么抛指定异常。"""

    def __init__(
        self,
        *,
        text: str = "",
        degraded: bool = False,
        raises: BaseException | None = None,
    ) -> None:
        self.text = text
        self.degraded = degraded
        self.raises = raises
        self.calls: list[Sequence[Mapping[str, str]]] = []

    async def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        return LLMReply(text=self.text, degraded=self.degraded)


def _json_llm(payload: list[dict[str, Any]]) -> FakeLLM:
    return FakeLLM(text=json.dumps(payload, ensure_ascii=False))


async def test_valid_array_becomes_candidates() -> None:
    llm = _json_llm(
        [
            {
                "content": "女儿叫王芳，每周末来看我",
                "category": "family",
                "source": "explicit",
                "importance_hint": 0.9,
            },
            {
                "content": "总点越剧《梁山伯与祝英台》",
                "category": "private",
                "source": "inferred",
                "importance_hint": 1.7,
            },
        ]
    )

    candidates = await extract_memories(llm, user_text="我女儿王芳周末来，放段越剧吧")

    assert [c.content for c in candidates] == [
        "女儿叫王芳，每周末来看我",
        "总点越剧《梁山伯与祝英台》",
    ]
    assert [c.category for c in candidates] == ["family", "private"]
    assert [c.source for c in candidates] == ["explicit", "inferred"]
    # 1.7 被 clamp 到 1.0
    assert [c.importance_hint for c in candidates] == [0.9, 1.0]
    assert all(isinstance(c, MemoryCandidate) for c in candidates)
    # 一次调用，且 system prompt 与对话都在 messages 里
    assert len(llm.calls) == 1
    assert llm.calls[0][0]["role"] == "system"
    assert "女儿" in llm.calls[0][1]["content"]


async def test_code_fenced_array_is_parsed() -> None:
    payload = [{"content": "不吃香菜", "category": "private", "source": "explicit"}]
    llm = FakeLLM(text="```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")

    candidates = await extract_memories(llm, user_text="我不吃香菜")

    assert [c.content for c in candidates] == ["不吃香菜"]


async def test_plain_text_prose_around_array_is_parsed() -> None:
    payload = [{"content": "老家在绍兴", "category": "private", "source": "explicit"}]
    llm = FakeLLM(text="好的，以下是提取结果：" + json.dumps(payload, ensure_ascii=False))

    candidates = await extract_memories(llm, user_text="我老家绍兴的")

    assert [c.content for c in candidates] == ["老家在绍兴"]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"a": 1}',
        "",
        "   ",
        "[]",
        '["不是对象"]',
        "[1, 2, 3]",
    ],
)
async def test_unparseable_output_returns_empty(text: str) -> None:
    llm = FakeLLM(text=text)

    assert await extract_memories(llm, user_text="随便说点什么") == []


async def test_degraded_reply_returns_empty() -> None:
    llm = FakeLLM(text="网络不太好，我们稍后再聊。", degraded=True)

    assert await extract_memories(llm, user_text="我女儿叫王芳") == []


async def test_llm_exception_is_swallowed() -> None:
    llm = FakeLLM(raises=TimeoutError("llm timeout"))

    assert await extract_memories(llm, user_text="我女儿叫王芳") == []


async def test_empty_user_text_does_not_call_llm() -> None:
    llm = _json_llm([])

    assert await extract_memories(llm, user_text="   ") == []
    assert llm.calls == []


async def test_pii_and_diagnosis_items_are_dropped() -> None:
    llm = _json_llm(
        [
            {"content": "手机号 13812345678", "category": "private", "source": "explicit"},
            {"content": "身份证 11010119900307551X", "category": "private", "source": "explicit"},
            {"content": "银行卡 6222021234567890", "category": "private", "source": "explicit"},
            {
                "content": "住在杭州市西湖区文三路100号",
                "category": "private",
                "source": "explicit",
            },
            {"content": "您这是高血压", "category": "private", "source": "explicit"},
            {"content": "对青霉素过敏", "category": "private", "source": "explicit"},
        ]
    )

    candidates = await extract_memories(llm, user_text="记一下：对青霉素过敏")

    # 只有「过敏」这条留下：黑名单收的是诊断断言，不是本人陈述的事实
    assert [c.content for c in candidates] == ["对青霉素过敏"]


async def test_overlong_content_is_dropped() -> None:
    llm = _json_llm(
        [
            {"content": "药" * 201, "category": "private", "source": "explicit"},
            {"content": "好" * 200, "category": "private", "source": "explicit"},
        ]
    )

    candidates = await extract_memories(llm, user_text="说点长的")

    assert [len(c.content) for c in candidates] == [200]


async def test_duplicate_content_keeps_first_only() -> None:
    llm = _json_llm(
        [
            {"content": "不吃辣", "category": "private", "source": "explicit"},
            {"content": " 不吃辣 ", "category": "private", "source": "inferred"},
        ]
    )

    candidates = await extract_memories(llm, user_text="我不吃辣")

    assert [c.content for c in candidates] == ["不吃辣"]
    assert [c.source for c in candidates] == ["explicit"]


async def test_invalid_category_or_source_is_dropped() -> None:
    llm = _json_llm(
        [
            {"content": "类别不对", "category": "secret", "source": "explicit"},
            {"content": "来源不对", "category": "private", "source": "guessed"},
            {"content": "缺字段"},
            {"content": "正常一条", "category": "private", "source": "explicit"},
        ]
    )

    candidates = await extract_memories(llm, user_text="随便")

    assert [c.content for c in candidates] == ["正常一条"]


async def test_missing_importance_uses_default() -> None:
    llm = _json_llm([{"content": "喜欢听京剧", "category": "private", "source": "explicit"}])

    candidates = await extract_memories(llm, user_text="我就爱听京剧")

    assert candidates[0].importance_hint == DEFAULT_IMPORTANCE


async def test_candidates_are_capped() -> None:
    payload = [
        {"content": f"事实{i}", "category": "private", "source": "explicit"}
        for i in range(MAX_CANDIDATES + 3)
    ]
    llm = _json_llm(payload)

    candidates = await extract_memories(llm, user_text="一堆事实")

    assert len(candidates) == MAX_CANDIDATES


# ============================================================
# 硬过滤（续）：FR-MEM-01「只写长者明确表达的信息」
# ============================================================


@pytest.mark.parametrize(
    "content",
    [
        "我儿子可能失业了",
        "儿子可能被裁员了",
        "女儿估计要离婚",
        "听说楼下老王住院了",
        "据说儿媳妇辞职了",
        "邻居家的孩子考上大学了",
        "老伴好像生病了",
    ],
)
async def test_unconfirmed_claims_about_others_are_dropped(content: str) -> None:
    """推测 / 传闻 / 别人家的境况：一律不入库。

    假 LLM 是**故意**把不该记的条目吐回来的 —— 真实模型也会这么干。prompt 只能降低
    概率，闸门必须落在服务层，所以这里要假设模型不听话。
    """
    llm = _json_llm([{"content": content, "category": "family", "source": "explicit"}])

    candidates = await extract_memories(llm, user_text=content)

    assert candidates == []


async def test_prd_example_writes_nothing() -> None:
    """PRD FR-MEM-01 原例：「我儿子可能失业了」不得写入家庭关系画像。"""
    llm = _json_llm(
        [
            {
                "content": "我儿子可能失业了",
                "category": "family",
                "source": "inferred",
            }
        ]
    )

    candidates = await extract_memories(llm, user_text="我儿子可能失业了")

    assert candidates == []


async def test_inferred_family_profile_is_dropped() -> None:
    """家属的关系/情绪不得由模型**推断**写入（PRD §4.9 敏感画像条款）。

    差别不在内容，在「这句话是谁说的」：长者本人明确表达的家属信息可以记，模型的推断
    不行。inferred 的偏好（反复点同一出戏）照记。
    """
    llm = _json_llm(
        [
            {"content": "和女儿关系不太好", "category": "family", "source": "inferred"},
            {"content": "喜欢安静", "category": "private", "source": "inferred"},
        ]
    )

    candidates = await extract_memories(llm, user_text="……")

    assert [c.content for c in candidates] == ["喜欢安静"]


@pytest.mark.parametrize(
    ("content", "category", "source"),
    [
        ("女儿叫王芳，每周末来看我", "family", "explicit"),
        ("对青霉素过敏", "private", "explicit"),
        ("喜欢听越剧", "private", "explicit"),
    ],
)
async def test_stable_facts_are_kept(content: str, category: str, source: str) -> None:
    """过滤只能挡住「说别人的境况」，不能误伤长者自己对稳定事实的陈述。

    误判方向不对称：多留一条只是多一句背景，删错一条长者下次会被反复追问 —— 所以
    这里把 FR-MEM-01 正面例子的原句钉死。
    """
    llm = _json_llm([{"content": content, "category": category, "source": source}])

    candidates = await extract_memories(llm, user_text=content)

    assert [c.content for c in candidates] == [content]
