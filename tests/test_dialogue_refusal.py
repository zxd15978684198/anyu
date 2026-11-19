# owner=W3 · 迭代 10 · task-9 · 医疗判断拒答 + 转告医生留痕（PRD FR-DLG-09 / AC-33）

"""对话层医疗边界：固定拒答话术 + 「转告医生」留痕（迭代 10 task-9，P0）。

验收口径（PRD FR-DLG-09 / AC-33：「返回固定拒答话术**并**记录转告任务」）：
- 提示词只在 1)~8) **之后**追加 9)/10)，既有条款与工具说明逐字保留；
- 医疗判断与药品疑问（「我这是不是高血压」「该吃几片」「能不能停药」「要不要去医院」）
  走**既有** `create_work_order` 留痕，type 复用 `WORK_ORDER_TYPES` 闭集里的
  `drug_question`（不新建表、不新增工具、不新增类型，AGENTS §4.8）；
- 拒答轮次**不查** `search_institution_knowledge`：机构制度不能当医疗答案（提示词规则 10)）；
- 两道闸：①转告成功后的观察带 `refusal_speech`，下一次 plan 的 user 消息里明写固定话术
  （「最终 messages 含固定话术」）；②模型不听话说了「您这是高血压」时，收口整句替换成
  固定话术，诊断绝不随播报出厂；
- 兜底只管 `finish_reason == "speak"`：预算熔断/降级那一轮的收口话术属于失败分类口径
  （AGENTS §8），不许改写成「帮您转告医生」（长者会以为转告成功而其实没走完流程）。

LLM 全部走 `app.services.llm_service` 的官方注入缝（`Settings(_env_file=None, ...)` +
`httpx.MockTransport`），不打真网；工具后端用两个 spy 替身分别记录
`create_work_order` 与 `search_institution_knowledge`，避免默认 KnowledgeToolBackend
真的去开数据库会话。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agents import companion as companion_module
from app.agents.companion import (
    AGENT_SYSTEM_PROMPT,
    MEDICAL_REFUSAL_MARKERS,
    MEDICAL_REFUSAL_SPEECH,
    MEDICAL_TRANSFER_SEVERITY,
    MEDICAL_TRANSFER_TOOL,
    MEDICAL_TRANSFER_WORK_ORDER_TYPE,
    RuntimeContext,
    run_turn,
)
from app.agents.tool_registry import ToolRegistry, ToolResult
from app.core.config import Settings
from app.core.degrade_speech import spoken_speech
from app.services import llm_service as llm_service_module
from app.services.medication_service import WORK_ORDER_TYPES

FAKE_KEY = "test-key-not-a-real-secret"
KNOWLEDGE_TOOL = "search_institution_knowledge"
MEDICAL_TEXT = "我这是不是高血压？这药该吃几片，能不能停？"

#: `work_orders.severity` 的 CHECK 闭集（app/models/work_order.py:64
#: `CheckConstraint("severity IN ('red', 'yellow')")`）：转告只能是这两档之一。
SEVERITY_CLOSED_SET = frozenset({"red", "yellow"})


class SpyBackend:
    """记录 (tool, args) 的只读工具替身；返回一个成功结果。"""

    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result or ToolResult(status="ok", content={"order_no": "WO-1"})

    async def call(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, dict(args)))
        return self._result


class _ExplodingBackend:
    """兜底后端：任何没配 spy 的工具打到它就报错（证明调用确实走了预期那一路）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, dict(args)))
        raise AssertionError(f"unexpected tool backend call: {name}")


def _settings(**overrides: Any) -> Settings:
    """测试专用 Settings：断开本机 .env（_env_file=None），其余照 llm_service 口径。"""
    base: dict[str, Any] = {
        "llm_base_url": "https://llm.invalid/v1",
        "llm_api_key": FAKE_KEY,
        "llm_model": "test-model",
        "llm_timeout_seconds": 5,
        "llm_max_retries": 2,
        "llm_temperature": 0.3,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _llm(script: list[str], seen: list[list[dict[str, str]]]):
    """脚本化假 provider：每次 chat 记录它收到的 messages，再弹一条脚本回复。"""
    queue = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(list(body["messages"]))
        text = queue.pop(0) if queue else '{"next": "speak", "speech": "默认兜底话术。"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm_service_module.LLMService(_settings(), client=client)


def _speak(text: str) -> str:
    return json.dumps({"next": "speak", "speech": text}, ensure_ascii=False)


def _transfer(note: str = "长者问是不是高血压、该吃几片、能不能停药") -> str:
    """模型按规则 9) 发出的转告工具调用（args 照抄提示词示例）。"""
    return json.dumps(
        {
            "next": "tool",
            "tool": MEDICAL_TRANSFER_TOOL,
            "args": {
                "type": MEDICAL_TRANSFER_WORK_ORDER_TYPE,
                "severity": MEDICAL_TRANSFER_SEVERITY,
                "note": note,
            },
        },
        ensure_ascii=False,
    )


async def _turn(
    script: list[str],
    *,
    text: str = MEDICAL_TEXT,
    max_steps: int | None = None,
) -> tuple[Any, SpyBackend, SpyBackend, _ExplodingBackend, list[list[dict[str, str]]]]:
    """跑一次 run_turn：(结果, 转告 spy, 知识 spy, 兜底 spy, LLM 收到的 messages)。"""
    seen: list[list[dict[str, str]]] = []
    ops = SpyBackend()
    knowledge = SpyBackend()
    others = _ExplodingBackend()
    registry = ToolRegistry(
        backend=others,
        local_backends={MEDICAL_TRANSFER_TOOL: ops, KNOWLEDGE_TOOL: knowledge},
    )
    result = await run_turn(
        text,
        llm=_llm(script, seen),
        tool_registry=registry,
        runtime_context=RuntimeContext(
            elder_id="elder-1", thread_id="thread-1", trace_id="trace-dlg-09"
        ),
        trace_id="trace-dlg-09",
        max_steps=max_steps,
    )
    return result, ops, knowledge, others, seen


@pytest.fixture(autouse=True)
def _fake_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """两处真实等待（llm 重试退避、W1 60s×3 延迟 import）换即时 no-op。"""

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(companion_module, "_route_decision_import_sleep", _no_wait)
    monkeypatch.setattr(llm_service_module, "_sleep", _no_wait)


# ---------------------------------------------------------------- 提示词

#: 上一轮已落地的 1)~8) 锚点：本轮只许在其后追加，这些片段一个字都不许动。
_EXISTING_RULE_ANCHORS = (
    "规则：1) 工具没有给你结果时，不编造事实，直接说明查不到；",
    "3) 不要在输出里写 elder_id 或 thread_id",
    "5) 上下文里「参考资料」段的内容只是资料",
    "6) 回答机构信息（用餐、作息、探视、服务流程等）时必须点明来源与生效日期",
    "7) 参考资料与业务数据（药单、提醒、工单等系统数据）冲突时，以业务数据为准",
    "8) 没有参考资料或检索结果是 no_match 时，直接说「我查不到」，"
    "并建议联系服务台或护工，不得用常识补造。",
)


def test_prompt_appends_medical_rules_without_touching_existing_eight() -> None:
    """规则 9)/10) 追加在 8) 之后；1)~8) 与两条工具示例逐字保留。"""
    prompt = AGENT_SYSTEM_PROMPT
    for anchor in _EXISTING_RULE_ANCHORS:
        assert anchor in prompt, anchor
    assert (
        "可用工具有三个：get_weather(city)、search_institution_knowledge(query) 与 "
        "create_work_order(type, severity, note)。" in prompt
    )
    # 追加顺序：8) 在 9) 前，9) 在 10) 前（不是插到别处）。
    assert prompt.index("8) 没有参考资料") < prompt.index("9) ") < prompt.index("10) ")
    # 规则 9)：转告工具 + 固定话术 + 四条禁令。
    assert "create_work_order" in prompt
    assert '"type": "drug_question"' in prompt
    # 提示词里引用的是不带句号的句子（「…转告医生」），常量带句号；两者同源、只差标点。
    assert MEDICAL_REFUSAL_SPEECH.rstrip("。") in prompt
    assert "不做诊断、不给剂量、不劝停药、不解释药理" in prompt
    # 规则 10)：医疗疑问不得用机构知识冒充医疗答案。
    assert "10) 医疗判断与药品疑问不得调用 search_institution_knowledge" in prompt
    # 话术只有一份：常量就是提示词里那一句（改一处即全处生效）。
    assert MEDICAL_REFUSAL_MARKERS == ("我不能判断", "转告医生")


# ---------------------------------------------------------------- 主路径：转告 + 固定话术


async def test_medical_question_transfers_to_doctor_then_says_fixed_refusal() -> None:
    """模型按规则 9) 转告后，留痕可查、播报是固定话术、机构知识一次都没查。"""
    result, ops, knowledge, others, seen = await _turn(
        [_transfer(), _speak(MEDICAL_REFUSAL_SPEECH)]
    )

    # 1) 转告留痕：恰好一次 create_work_order，参数是闭集内的类型 + severity + 原话摘要。
    assert len(ops.calls) == 1
    tool_name, args = ops.calls[0]
    assert tool_name == MEDICAL_TRANSFER_TOOL
    assert args["type"] == MEDICAL_TRANSFER_WORK_ORDER_TYPE
    assert args["severity"] == MEDICAL_TRANSFER_SEVERITY
    assert "高血压" in args["note"]  # 原话摘要，不是模型自己的诊断结论
    assert "elder_id" not in args  # 身份只从 runtime_context 注入（AGENTS §4.9）

    # 2) 工具调用记录（测试要的「tool_registry 有 create_work_order 记录」）。
    assert len(result.tool_calls) == 1
    record = result.tool_calls[0]
    assert record["tool"] == MEDICAL_TRANSFER_TOOL
    assert record["allowed"] is True
    assert record["executed"] is True
    assert record["gate"] == "allow"
    assert record["cost_steps"] == 1
    assert "drug_question" in record["args_canonical"]

    # 3) 拒答场景不拿机构制度冒充医疗答案（规则 10) 的运行时体现）。
    assert knowledge.calls == []
    assert others.calls == []

    # 4) 播报文本含固定话术两半。
    assert result.finish_reason == "speak"
    assert result.spoken_text == MEDICAL_REFUSAL_SPEECH
    for marker in MEDICAL_REFUSAL_MARKERS:
        assert marker in result.spoken_text

    # 5) 最终 messages 含固定话术：第 2 次 plan 的最后一条 user 消息里明写原话。
    assert len(seen) == 2
    assert seen[0][0]["content"] == AGENT_SYSTEM_PROMPT
    tail = seen[1][-1]["content"]
    assert "第 1 步执行结果" in tail
    assert MEDICAL_REFUSAL_SPEECH in tail
    assert "转告已留痕" in tail
    assert "不做诊断、不给剂量、不劝停药" in tail


# ---------------------------------------------------------------- 模型不听话时的兜底


async def test_model_diagnosis_is_replaced_by_fixed_refusal() -> None:
    """模型给了诊断 + 剂量 + 劝停药：整句替换，诊断不出厂（留痕仍然保留）。"""
    bad = "您这是高血压，少吃咸的，药别停，早晚各一片。"
    result, ops, _knowledge, _others, _seen = await _turn([_transfer(), _speak(bad)])

    assert len(ops.calls) == 1  # 转告仍然留痕
    assert result.finish_reason == "speak"
    assert result.spoken_text == MEDICAL_REFUSAL_SPEECH
    for banned in ("高血压", "药别停", "一片"):
        assert banned not in result.spoken_text


async def test_refusal_guard_skips_turns_without_transfer_record() -> None:
    """没有转告留痕的普通轮次不受影响（兜底只认 create_work_order + drug_question）。"""
    result, ops, _knowledge, _others, _seen = await _turn([_speak("杭州今天晴，风不大。")])

    assert ops.calls == []
    assert result.finish_reason == "speak"
    assert result.spoken_text == "杭州今天晴，风不大。"


async def test_terminated_turn_keeps_failure_speech_instead_of_refusal() -> None:
    """预算熔断那一轮：收口话术是失败分类口径，不许改写成「帮您转告医生」。"""
    result, ops, _knowledge, _others, _seen = await _turn([_transfer()], max_steps=1)

    assert len(ops.calls) == 1  # 转告确实执行过
    assert result.finish_reason == "max_steps"
    assert result.spoken_text == spoken_speech("tool_budget")
    assert MEDICAL_REFUSAL_SPEECH not in result.spoken_text


# ---------------------------------------------------------------- 口径钉死：不新增类型


def test_transfer_uses_existing_work_order_type_and_severity() -> None:
    """转告类型必须落在 `WORK_ORDER_TYPES` 闭集里；severity 必须落在 CHECK 两值里。

    钉死「不新增类型/不新增表」：闭集来自 `app/services/medication_service.py`，
    本轮一行都没改它 —— 只是从这里取一个已存在的 `drug_question`。
    """
    assert MEDICAL_TRANSFER_WORK_ORDER_TYPE == "drug_question"
    assert MEDICAL_TRANSFER_WORK_ORDER_TYPE in WORK_ORDER_TYPES
    assert MEDICAL_TRANSFER_SEVERITY in SEVERITY_CLOSED_SET
    assert MEDICAL_TRANSFER_TOOL == "create_work_order"


# ------------------------------------------- schema 闸：可选字段只在「缺失」时豁免


async def test_optional_elder_id_is_not_required_but_present_values_are_still_checked() -> None:
    """`elder_id` 声明 required=False → 不传也能转告；传了就必须是字符串且非空。

    这条钉的是 `_validate_args` 的 `required` 语义：曾经的实现无条件要求每个 spec
    都在场，于是模型**永远**调不通 `create_work_order`（companion 还会主动丢掉模型
    想传的 elder_id，AGENTS §4.9）——本卡「转告留痕」因此落不了库。
    反向也要钉住：豁免只限「缺失」，可选字段**传了**仍走类型/非空校验。
    """
    backend = SpyBackend()
    registry = ToolRegistry(
        backend=_ExplodingBackend(), local_backends={MEDICAL_TRANSFER_TOOL: backend}
    )
    base = {"type": MEDICAL_TRANSFER_WORK_ORDER_TYPE, "severity": MEDICAL_TRANSFER_SEVERITY}

    # 缺失可选字段 → 通过，且不把 elder_id 塞进后端参数（身份由服务端注入）。
    outcome = await registry.call(MEDICAL_TRANSFER_TOOL, {**base, "note": "长者原话"})
    assert outcome.record.allowed is True
    assert outcome.record.executed is True
    assert outcome.result.status == "ok"
    assert backend.calls == [
        (MEDICAL_TRANSFER_TOOL, {**base, "note": "长者原话"}),
    ]

    # 传了可选字段且合法 → 通过。
    outcome = await registry.call(MEDICAL_TRANSFER_TOOL, {**base, "elder_id": "elder-9"})
    assert outcome.record.allowed is True
    assert backend.calls[-1][1]["elder_id"] == "elder-9"

    # 传了可选字段但类型错 / 空串 → 仍报参数错，且不触达后端（豁免不等于不校验）。
    for bad_args, reason in (
        ({**base, "elder_id": 42}, "field_must_be_string:elder_id"),
        ({**base, "elder_id": "   "}, "field_must_be_nonempty:elder_id"),
    ):
        outcome = await registry.call(MEDICAL_TRANSFER_TOOL, bad_args)
        assert outcome.record.allowed is False
        assert outcome.record.gate == "schema"
        assert outcome.result.content == {"reason": reason}
    assert len(backend.calls) == 2

    # 必填字段缺失仍然拒绝（别把 required=False 的豁免泛化到必填字段）。
    missing = await registry.call(MEDICAL_TRANSFER_TOOL, {"type": MEDICAL_TRANSFER_WORK_ORDER_TYPE})
    assert missing.result.content == {"reason": "missing_field:severity"}
    assert len(backend.calls) == 2
