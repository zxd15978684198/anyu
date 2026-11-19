"""W2 + W3 的回归集：上下文组装（`app/core/context.py`）与对话编排
（`app/services/dialogue_service.py`）。

对应 `docs/plans/day04/day04-对话闭环收口计划.md` §5-W2 / §5-W3 的验收命令（该计划仍在推进）。
两者放在同一个文件里是刻意的：**组装正确性只有在被编排真正调用时才有意义**，
把 `build_dialogue_messages` 单测到 100% 覆盖、但编排层传错参数，
线上照样是错的话；所以「编排把什么喂给了组装」也在本文件里断言。

## 覆盖的验收口径

| 分组 | 用例 | 验收条款 |
| --- | --- | --- |
| 组装 | system 段序固定、时间在最后一行 | TD line 847（固定前缀逐字节稳定） |
| 组装 | 同一输入两次调用字节相同 | 同上（prompt cache 命中前提） |
| 组装 | `elder_id` 不出现在任何一条 message 里 | TD line 857（注入原则） |
| 组装 | 5 轮截断从最旧的开始丢 | TD §6.3 ②（最近 5 轮） |
| 组装 | 空串半轮跳过且不配平 | —— |
| 身份 | generic 强制 `elder_id=None`、称呼不含个人信息 | PRD AC-28 |
| 编排 | 正常一轮：messages 形状 + 写回一轮 | C6 line 110 |
| 编排 | **generic 全程不读写 Redis** | PRD AC-28（不写个人数据） |
| 编排 | 降级话术**不入记忆** | FR-DLG-05 的可观测后果 |
| 编排 | Redis 读/写抛异常 → 照常回复 + warn | 计划 S-D9 |
| 编排 | `thread_id` 非法 → 拒绝（不静默改写） | PRD AC-39 |
| 存储 | key 形态、坏行跳过、一轮一元素、LTRIM 同事务 | 计划 S-D7 / S-D15 |

## 为什么 Redis 用假的而不是真起一个

`RedisTurnStore` 只用四个命令（`lrange` / `pipeline` / `rpush` / `ltrim`），
用 30 行的假客户端就能把「窗口裁剪」「坏行容错」「事务包裹」三条断言写实，
而真起 Redis 会让这批用例在没装 Redis 的机器上变红（本项目已有
`ANYU_ALLOW_DB_SKIP` 的前车之鉴：依赖外部服务的用例最容易被环境吃掉）。
真正的端到端验证在 W6 的联调取证里做。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.context import (
    DIALOGUE_MAX_ROUNDS,
    DialogueIdentity,
    DialogueTurn,
    PersonaContext,
    build_dialogue_messages,
)
from app.services.dialogue_service import (
    DialogueService,
    RedisTurnStore,
    validate_thread_id,
)
from app.services.llm_service import LLMReply

# --------------------------------------------------------------- 测试替身

FIXED_NOW = datetime(2026, 11, 7, 4, 30, tzinfo=UTC)  # = 12:30 Asia/Shanghai

PERSONA = PersonaContext(
    name="小满",
    personality="温和耐心的陪伴者，语速慢、句子短，先共情再回应",
    tone_examples=("慢点说，我在呢", "先别急，咱们一句一句来"),
)


class FakeLLM:
    """按脚本返回的假 LLM；记录每次收到的 messages 便于断言。"""

    def __init__(self, *, text: str = "今天晴，适合晒晒太阳。", degraded: bool = False) -> None:
        self.text = text
        self.degraded = degraded
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self, messages: Sequence[dict[str, str]], *, trace_id: str | None = None
    ) -> LLMReply:
        self.calls.append(list(messages))
        return LLMReply(text=self.text, degraded=self.degraded)


class FakeStore:
    """内存版 TurnStore；可注入异常以验证「存储故障不炸主链路」。"""

    def __init__(
        self,
        turns: list[DialogueTurn] | None = None,
        *,
        fail_load: bool = False,
        fail_append: bool = False,
    ) -> None:
        self.turns = list(turns or [])
        self.fail_load = fail_load
        self.fail_append = fail_append
        self.appended: list[tuple[str, str, str]] = []

    async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
        if self.fail_load:
            raise ConnectionError("redis down (load)")
        return list(self.turns)

    async def append_turn(self, thread_id: str, *, user: str, assistant: str) -> None:
        if self.fail_append:
            raise ConnectionError("redis down (append)")
        self.appended.append((thread_id, user, assistant))


class FakeRedis:
    """够 `RedisTurnStore` 用的最小假客户端（LIST + pipeline）。"""

    def __init__(self, *, fail_on_execute: bool = False) -> None:
        self.data: dict[str, list[str]] = {}
        self.fail_on_execute = fail_on_execute
        self.pipeline_calls: list[list[tuple[str, tuple[Any, ...]]]] = []

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.data.get(key, [])
        # Redis 语义：负数下标从尾部数；end=-1 表示最后一个
        n = len(items)
        i = max(0, n + start if start < 0 else start)
        j = n + end + 1 if end < 0 else end + 1
        return items[i:j]

    def pipeline(self, *, transaction: bool = False) -> FakePipeline:
        assert transaction is True, "必须用事务管道：RPUSH + LTRIM 不能分两次落"
        return FakePipeline(self)

    async def rpush(self, key: str, value: str) -> int:
        self.data.setdefault(key, []).append(value)
        return len(self.data[key])

    async def ltrim(self, key: str, start: int, end: int) -> None:
        items = self.data.get(key, [])
        n = len(items)
        i = max(0, n + start if start < 0 else start)
        j = n + end + 1 if end < 0 else end + 1
        self.data[key] = items[i:j]


class FakePipeline:
    def __init__(self, parent: FakeRedis) -> None:
        self._parent = parent
        self._queue: list[tuple[str, tuple[Any, ...]]] = []

    def rpush(self, key: str, value: str) -> FakePipeline:
        self._queue.append(("rpush", (key, value)))
        return self

    def ltrim(self, key: str, start: int, end: int) -> FakePipeline:
        self._queue.append(("ltrim", (key, start, end)))
        return self

    async def execute(self) -> list[Any]:
        if self._parent.fail_on_execute:
            raise ConnectionError("redis down (execute)")
        self._parent.pipeline_calls.append(list(self._queue))
        results = []
        for name, args in self._queue:
            results.append(await getattr(self._parent, name)(*args))
        return results


def _identity(**overrides: Any) -> DialogueIdentity:
    base: dict[str, Any] = {
        "source": "voice",
        "elder_id": "11111111-1111-4111-8111-111111111102",
        "display_name": "张桂芬",
    }
    base.update(overrides)
    return DialogueIdentity(**base)


def _system(messages: list[dict[str, str]]) -> str:
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


# ======================================================================
# 一、上下文组装（W2）
# ======================================================================


class TestContext:
    """`build_dialogue_messages` —— 固定前缀的稳定性与身份投影。"""

    def test_message_shape_is_system_history_user(self) -> None:
        messages = build_dialogue_messages(
            identity=_identity(),
            user_text="今天天气怎么样",
            history=[DialogueTurn(user="你好", assistant="您好呀")],
            persona=PERSONA,
            now=FIXED_NOW,
        )
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
        assert messages[-1] == {"role": "user", "content": "今天天气怎么样"}

    def test_system_prefix_is_byte_stable_across_calls(self) -> None:
        """同输入两次调用必须完全相等 —— prompt cache 命中的唯一前提。"""
        kwargs: dict[str, Any] = {
            "identity": _identity(),
            "user_text": "今天天气怎么样",
            "persona": PERSONA,
            "now": FIXED_NOW,
        }
        first = build_dialogue_messages(**kwargs)
        second = build_dialogue_messages(**kwargs)
        assert first == second

    def test_time_is_the_last_line_of_system(self) -> None:
        """时间必须留在前缀末尾：挪到开头等于让 cache 每轮全失效。"""
        messages = build_dialogue_messages(
            identity=_identity(), user_text="你好", persona=PERSONA, now=FIXED_NOW
        )
        lines = _system(messages).splitlines()
        assert lines[-1].startswith("现在时间：")
        assert "2026-11-07 12:30" in lines[-1]  # UTC 04:30 → Asia/Shanghai 12:30
        assert all("现在时间" not in line for line in lines[:-1])

    def test_system_section_order(self) -> None:
        """段序是人设名 → 性格 → 说话示例 → 医疗边界 → 称呼 → 时间。"""
        system = _system(
            build_dialogue_messages(
                identity=_identity(), user_text="你好", persona=PERSONA, now=FIXED_NOW
            )
        )
        assert system.index("小满") < system.index("性格")
        assert system.index("性格") < system.index("说话示例")
        assert system.index("说话示例") < system.index("医疗诊断")
        assert system.index("医疗诊断") < system.index("当前对话对象")
        assert system.index("当前对话对象") < system.index("现在时间")

    def test_elder_id_never_reaches_prompt(self) -> None:
        """TD line 857：elder_id 只经 runtime_context 进工具参数，不进 prompt。"""
        identity = _identity()
        messages = build_dialogue_messages(
            identity=identity, user_text="你好", persona=PERSONA, now=FIXED_NOW
        )
        blob = "\n".join(m["content"] for m in messages)
        assert identity.elder_id is not None
        assert identity.elder_id not in blob

    def test_missing_display_name_falls_back_to_neutral_address(self) -> None:
        """没传 display_name 时用「长者」，**绝不**把 elder_id 当称呼。"""
        system = _system(
            build_dialogue_messages(
                identity=_identity(display_name=None),
                user_text="你好",
                persona=PERSONA,
                now=FIXED_NOW,
            )
        )
        assert "当前对话对象：长者" in system

    def test_generic_identity_strips_personal_info(self) -> None:
        """AC-28：通用模式不写个人数据 —— 构造时就把 elder_id/称呼抹掉。"""
        identity = DialogueIdentity(
            source="generic",
            elder_id="11111111-1111-4111-8111-111111111102",
            display_name="张桂芬",
        )
        assert identity.elder_id is None
        assert identity.display_name is None
        system = _system(
            build_dialogue_messages(
                identity=identity, user_text="你好", persona=PERSONA, now=FIXED_NOW
            )
        )
        assert "张桂芬" not in system
        assert "11111111" not in system
        assert "尚未确认身份" in system

    def test_non_generic_requires_elder_id(self) -> None:
        with pytest.raises(ValueError, match="requires elder_id"):
            DialogueIdentity(source="voice")

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown identity source"):
            DialogueIdentity(source="robot", elder_id="x")

    def test_history_is_truncated_from_the_oldest(self) -> None:
        history = [DialogueTurn(user=f"问题{i}", assistant=f"回答{i}") for i in range(8)]
        messages = build_dialogue_messages(
            identity=_identity(), user_text="最后一问", history=history, now=FIXED_NOW
        )
        # system + 5 轮 * 2 + user = 12
        assert len(messages) == 1 + DIALOGUE_MAX_ROUNDS * 2 + 1
        content = "\n".join(m["content"] for m in messages)
        assert "问题3" in content  # 第 4 条起保留（8-5=3）
        assert "问题2" not in content

    def test_empty_half_turn_is_skipped_not_padded(self) -> None:
        """空 assistant 只跳过它自己，不凭空造一句「数字人说过的话」。"""
        messages = build_dialogue_messages(
            identity=_identity(),
            user_text="你好",
            history=[DialogueTurn(user="上一问", assistant="   ")],
            now=FIXED_NOW,
        )
        assert [m["role"] for m in messages] == ["system", "user", "user"]
        assert all(m["content"].strip() for m in messages)

    def test_empty_user_text_produces_no_trailing_user(self) -> None:
        messages = build_dialogue_messages(identity=_identity(), user_text="   ", now=FIXED_NOW)
        assert [m["role"] for m in messages] == ["system"]

    def test_persona_optional(self) -> None:
        messages = build_dialogue_messages(
            identity=_identity(), user_text="你好", persona=None, now=FIXED_NOW
        )
        system = _system(messages)
        assert "小满" in system  # 无人设时给默认名，不抛
        assert "性格：" not in system

    def test_naive_datetime_is_treated_as_local(self) -> None:
        """传无时区的 datetime 时按 Asia/Shanghai 解释，不炸也不悄悄改成 UTC。"""
        messages = build_dialogue_messages(
            identity=_identity(), user_text="你好", now=datetime(2026, 11, 7, 12, 30)
        )
        assert "2026-11-07 12:30" in _system(messages)


# ======================================================================
# 二、对话编排（W3）
# ======================================================================


class TestDialogueService:
    """`DialogueService.respond` —— 取历史、组装、问模型、写回。"""

    async def test_happy_path_feeds_history_and_user_to_llm(self) -> None:
        store = FakeStore([DialogueTurn(user="你好", assistant="您好呀")])
        llm = FakeLLM()
        service = DialogueService(llm=llm, store=store, now_provider=lambda: FIXED_NOW)

        reply = await service.respond(
            thread_id="t-1", identity=_identity(), user_text="今天天气怎么样", persona=PERSONA
        )

        assert reply.text == "今天晴，适合晒晒太阳。"
        assert reply.degraded is False
        assert len(llm.calls) == 1
        roles = [m["role"] for m in llm.calls[0]]
        assert roles == ["system", "user", "assistant", "user"]
        assert llm.calls[0][-1]["content"] == "今天天气怎么样"

    async def test_successful_turn_is_appended_once(self) -> None:
        store = FakeStore()
        service = DialogueService(llm=FakeLLM(), store=store, now_provider=lambda: FIXED_NOW)

        await service.respond(thread_id="t-1", identity=_identity(), user_text="今天天气")

        assert store.appended == [("t-1", "今天天气", "今天晴，适合晒晒太阳。")]

    async def test_generic_never_touches_store(self) -> None:
        """AC-28：通用模式既不读也不写 —— 读写两条路都不许碰。"""
        store = FakeStore([DialogueTurn(user="上一位老人的私事", assistant="我记下了")])
        llm = FakeLLM()
        service = DialogueService(llm=llm, store=store, now_provider=lambda: FIXED_NOW)

        await service.respond(
            thread_id="t-1", identity=DialogueIdentity(source="generic"), user_text="你好"
        )

        assert store.appended == []
        blob = "\n".join(m["content"] for m in llm.calls[0])
        assert "上一位老人的私事" not in blob  # 历史没被读进 prompt

    async def test_degraded_reply_is_not_persisted(self) -> None:
        """降级话术不是「数字人说过的话」，不许进记忆（否则下次把它当真答话）。"""
        store = FakeStore()
        llm = FakeLLM(text="我稍微想一想，请等我一下下。", degraded=True)
        service = DialogueService(llm=llm, store=store, now_provider=lambda: FIXED_NOW)

        reply = await service.respond(thread_id="t-1", identity=_identity(), user_text="今天天气")

        assert reply.degraded is True
        assert store.appended == []

    async def test_store_load_failure_does_not_break_reply(self) -> None:
        """S-D9：存储是增强能力，读失败按「本轮无历史」继续。"""
        store = FakeStore(fail_load=True)
        llm = FakeLLM()
        service = DialogueService(llm=llm, store=store, now_provider=lambda: FIXED_NOW)

        reply = await service.respond(thread_id="t-1", identity=_identity(), user_text="你好")

        assert reply.degraded is False
        assert [m["role"] for m in llm.calls[0]] == ["system", "user"]

    async def test_store_append_failure_does_not_break_reply(self) -> None:
        store = FakeStore(fail_append=True)
        service = DialogueService(llm=FakeLLM(), store=store, now_provider=lambda: FIXED_NOW)

        reply = await service.respond(thread_id="t-1", identity=_identity(), user_text="你好")

        assert reply.degraded is False
        assert reply.text == "今天晴，适合晒晒太阳。"

    async def test_no_store_still_answers(self) -> None:
        """没配 Redis（例如单进程演示）时仍能回复，只是没有记忆。"""
        llm = FakeLLM()
        service = DialogueService(llm=llm, store=None, now_provider=lambda: FIXED_NOW)

        reply = await service.respond(thread_id="t-1", identity=_identity(), user_text="你好")

        assert reply.degraded is False
        assert [m["role"] for m in llm.calls[0]] == ["system", "user"]

    @pytest.mark.parametrize("bad", ["", "has space", "a" * 129, "new\nline", "semi;colon"])
    async def test_invalid_thread_id_is_rejected(self, bad: str) -> None:
        """AC-39：拼进 Redis key 的串必须过准入校验，不静默改写。"""
        service = DialogueService(llm=FakeLLM(), store=FakeStore())
        with pytest.raises(ValueError, match="invalid thread_id"):
            await service.respond(thread_id=bad, identity=_identity(), user_text="你好")

    async def test_threads_are_isolated(self) -> None:
        """换 thread 就换记忆（闸门①：换人 → 旧 thread 落 ended_at + 新 thread）。"""
        store = FakeStore()
        llm = FakeLLM()
        service = DialogueService(llm=llm, store=store, now_provider=lambda: FIXED_NOW)

        await service.respond(thread_id="t-a", identity=_identity(), user_text="A 的问题")
        await service.respond(thread_id="t-b", identity=_identity(), user_text="B 的问题")

        assert [item[0] for item in store.appended] == ["t-a", "t-b"]
        assert "A 的问题" not in "\n".join(m["content"] for m in llm.calls[1])


# ======================================================================
# 三、Redis 短期记忆（W3 的存储半边）
# ======================================================================


class TestRedisTurnStore:
    """`RedisTurnStore` —— key 形态、一轮一元素、窗口裁剪、坏行容错。"""

    def _store(self, client: FakeRedis, *, max_turns: int = DIALOGUE_MAX_ROUNDS) -> RedisTurnStore:
        return RedisTurnStore(client, key_prefix="anyu", max_turns=max_turns)

    def test_key_shape(self) -> None:
        store = self._store(FakeRedis())
        assert store.key_for("abc123") == "anyu:dialogue:abc123:turns"

    def test_key_rejects_invalid_thread_id(self) -> None:
        store = self._store(FakeRedis())
        with pytest.raises(ValueError, match="invalid thread_id"):
            store.key_for("bad id")

    async def test_append_then_load_roundtrip(self) -> None:
        client = FakeRedis()
        store = self._store(client)

        await store.append_turn("t-1", user="今天天气", assistant="今天晴")
        turns = await store.load_recent_turns("t-1")

        assert turns == [DialogueTurn(user="今天天气", assistant="今天晴")]

    async def test_one_element_per_turn(self) -> None:
        """一轮一个元素：句级存储会在中断后留下孤儿，配对时整段错位（S-D15）。"""
        client = FakeRedis()
        store = self._store(client)

        await store.append_turn("t-1", user="问一", assistant="答一")
        await store.append_turn("t-1", user="问二", assistant="答二")

        assert len(client.data["anyu:dialogue:t-1:turns"]) == 2

    async def test_pipeline_is_transactional_and_trims_in_the_same_round_trip(self) -> None:
        """RPUSH + LTRIM 必须同事务：分开落会留下永不裁剪的列表。"""
        client = FakeRedis()
        store = self._store(client)

        await store.append_turn("t-1", user="问", assistant="答")

        assert len(client.pipeline_calls) == 1
        names = [name for name, _ in client.pipeline_calls[0]]
        assert names == ["rpush", "ltrim"]

    async def test_window_keeps_only_latest_turns(self) -> None:
        client = FakeRedis()
        store = self._store(client, max_turns=5)

        for i in range(8):
            await store.append_turn("t-1", user=f"问{i}", assistant=f"答{i}")

        turns = await store.load_recent_turns("t-1")
        assert len(turns) == 5
        assert turns[0].user == "问3"
        assert turns[-1].user == "问7"

    async def test_corrupt_rows_are_skipped_not_raised(self) -> None:
        """手工改过 Redis、旧格式、写到一半的 JSON —— 都跳过，不许炸整轮。"""
        client = FakeRedis()
        store = self._store(client)
        key = store.key_for("t-1")
        client.data[key] = [
            "not-json-at-all",
            json.dumps({"user": "好的一轮", "assistant": "答话"}, ensure_ascii=False),
            json.dumps({"user": 123, "assistant": "数字不是字符串"}),
            json.dumps(["列表不是对象"]),
            json.dumps({"user": "缺 assistant"}),
            json.dumps({"user": "  ", "assistant": "   "}),
        ]

        turns = await store.load_recent_turns("t-1")

        assert turns == [DialogueTurn(user="好的一轮", assistant="答话")]

    async def test_bytes_payload_is_decoded(self) -> None:
        """`decode_responses=False` 下 redis-py 给 bytes，读取端要认。"""
        client = FakeRedis()
        store = self._store(client)
        client.data[store.key_for("t-1")] = [
            json.dumps({"user": "你好", "assistant": "您好呀"}, ensure_ascii=False).encode("utf-8")
        ]

        turns = await store.load_recent_turns("t-1")

        assert turns == [DialogueTurn(user="你好", assistant="您好呀")]

    async def test_survives_utf8_roundtrip(self) -> None:
        client = FakeRedis()
        store = self._store(client)

        await store.append_turn("t-1", user="我想听越剧", assistant="好，给您放一段《梁祝》。")

        turns = await store.load_recent_turns("t-1")
        assert turns[0].assistant == "好，给您放一段《梁祝》。"


# ======================================================================
# 四、模块级不变量
# ======================================================================


def test_validate_thread_id_accepts_real_uuid_hex() -> None:
    """`identity_service._new_thread_id()` 产出 32 位 hex，必须过准入。"""
    validate_thread_id("a" * 32)


def test_dialogue_service_has_no_io_imports() -> None:
    """编排层不直接 import redis / httpx / sqlalchemy —— 依赖倒置的护栏。

    这条用例的价值在于「改坏时会红」：一旦有人图省事在编排层里 `import redis.asyncio`
    直连，存储故障的边界就没了（S-D9 会失效）。
    """
    from pathlib import Path

    import app.services.dialogue_service as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("import redis", "import httpx", "from sqlalchemy"):
        assert forbidden not in source, f"编排层不该直接依赖 {forbidden}"
