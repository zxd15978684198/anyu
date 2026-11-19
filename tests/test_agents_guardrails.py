"""迭代 5 · D5-C1：`app/agents/guardrails.py` 三规则纯逻辑护栏测试。

钉住 迭代 5 计划 §4.4 / §4.6 的四件事：

1. **预算**：每轮第 9 次拒绝（`max_tool_calls_per_turn=8`，PRD FR-DLG-10）；
2. **熔断**：同 Tool 60s 内第 3 次 → 打开；fake clock 推进 61s + 下一 turn 才恢复；
3. **重复短路**：同工具同参命中返回缓存结果，异参不短路；
4. **判定顺序**：预算 → 熔断 → short_circuit；两闸同时满足拒绝条件时 `reject_budget` 生效。

全部为纯逻辑用例：不连库、不起服务、不碰 `mcp_client`；时间源一律用假钟注入，
因此没有任何 `sleep`，整份文件毫秒级跑完。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.agents.guardrails import (
    Guardrail,
    GuardrailDecision,
    RepeatCache,
    ToolBreaker,
    TurnBudget,
    canonical_args,
)
from app.core.config import get_settings
from app.core.degrade_speech import spoken_speech

_BREAKER_SPEECH = spoken_speech("tool_breaker")


class FakeClock:
    """可推进的假时钟，替代 `time.monotonic`（护栏构造参数 `clock=` 注入）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _guardrail(clock: FakeClock, **overrides: Any) -> Guardrail:
    """默认阈值（8 / 60s / 3）接受配置，只把时钟换成假钟。"""
    return Guardrail(clock=clock, **overrides)


# ---------------------------------------------------------------------------
# 1. 预算：第 9 次拒绝
# ---------------------------------------------------------------------------


def test_budget_allows_eight_then_rejects_ninth(clock: FakeClock) -> None:
    """前 8 次 allow，第 9 次 reject_budget；被拒的那次不消耗计数。

    9 次调用刻意用 9 个不同 Tool，保证熔断（同 Tool 第 3 次）不会先触发，
    这条用例只测预算一个变量。
    """
    guard = _guardrail(clock)

    for index in range(8):
        decision = guard.check(f"tool_{index}", {"i": index})
        assert decision.action == "allow", f"第 {index + 1} 次应当放行"
    assert guard.budget.used == 8

    ninth = guard.check("tool_8", {"i": 8})
    assert ninth.action == "reject_budget"
    assert guard.budget.used == 8, "被预算拒绝的调用不得计数"


def test_budget_is_per_turn(clock: FakeClock) -> None:
    """`reset_turn()` 后重新获得 8 次预算。"""
    guard = _guardrail(clock)
    for index in range(8):
        assert guard.check(f"tool_{index}").action == "allow"
    assert guard.check("tool_8").action == "reject_budget"

    guard.reset_turn()
    assert guard.budget.used == 0
    assert guard.check("tool_0").action == "allow"


def test_budget_defaults_come_from_settings() -> None:
    """默认阈值只从 `get_settings()` 读，不在模块里另写一份数字。"""
    settings = get_settings()
    assert TurnBudget().max_calls == settings.max_tool_calls_per_turn == 8


def test_turn_budget_try_consume_contract() -> None:
    """`try_consume()` 成功才自增；`used` 是只读视图。"""
    budget = TurnBudget(max_calls=2)
    assert budget.would_allow() is True
    assert budget.try_consume() is True
    assert budget.try_consume() is True
    assert budget.try_consume() is False
    assert budget.used == 2
    budget.reset()
    assert budget.used == 0


# ---------------------------------------------------------------------------
# 2. 熔断：60s 内第 3 次 → open；61s + 下一 turn 恢复
# ---------------------------------------------------------------------------


def test_breaker_trips_on_third_call_within_window(clock: FakeClock) -> None:
    breaker = ToolBreaker(clock=clock)

    assert breaker.allow("weather") is True
    clock.advance(10)
    assert breaker.allow("weather") is True
    clock.advance(10)
    assert breaker.allow("weather") is False, "60s 内第 3 次必须熔断"
    assert breaker.is_tripped("weather") is True


def test_breaker_recovers_when_window_slides_out(clock: FakeClock) -> None:
    """熔断是「1 分钟窗口」语义，不是「本轮」语义：窗口空了就恢复，同一 turn 也一样。"""
    breaker = ToolBreaker(clock=clock)
    breaker.allow("weather")
    breaker.allow("weather")
    assert breaker.allow("weather") is False

    clock.advance(120)  # 三次调用全部滑出 60s 窗口
    assert breaker.allow("weather") is True, "时间窗滑空后必须恢复（迭代 5 审计 P1-5）"
    assert breaker.is_tripped("weather") is False


def test_breaker_stays_tripped_across_turns_within_window(clock: FakeClock) -> None:
    """跨轮不自动恢复：turn 1 熔断，turn 2（几秒后）仍拒绝。"""
    guard = _guardrail(clock)
    assert guard.check("weather").action == "allow"
    clock.advance(5)
    assert guard.check("weather").action == "allow"
    clock.advance(5)
    assert guard.check("weather").action == "reject_breaker"

    guard.reset_turn()  # 下一 turn，但只过了 10s
    clock.advance(5)
    assert guard.check("weather").action == "reject_breaker"
    assert guard.breaker.is_tripped("weather") is True


def test_breaker_recovers_after_61s_and_next_turn(clock: FakeClock) -> None:
    """fake clock 推进 61s（最早两次滑出）且进入下一 turn 后恢复放行。"""
    guard = _guardrail(clock)

    assert guard.check("weather").action == "allow"
    clock.advance(5)
    assert guard.check("weather").action == "allow"
    clock.advance(5)
    assert guard.check("weather").action == "reject_breaker"

    guard.reset_turn()  # 下一 turn
    clock.advance(51)  # 距首次调用共 61s，t=0 那条滑出窗口
    assert guard.check("weather").action == "allow"


def test_breaker_window_slides_independently_of_reset(clock: FakeClock) -> None:
    """窗口是半开区间 `(now-60, now]`：只有滑出窗口的旧调用被丢弃。"""
    breaker = ToolBreaker(clock=clock)
    assert breaker.allow("weather") is True  # t=0
    clock.advance(20)
    assert breaker.allow("weather") is True  # t=20
    clock.advance(41)  # t=61：cutoff=1，t=0 滑出，t=20 保留

    assert breaker.allow("weather") is True, "窗口只剩 1 条旧记录，不应熔断"
    assert breaker.is_tripped("weather") is False
    assert breaker.timestamps("weather") == (1020.0, 1061.0)


def test_breaker_defaults_come_from_settings() -> None:
    settings = get_settings()
    breaker = ToolBreaker()
    assert breaker.window_seconds == settings.tool_repeat_window_seconds == 60
    assert breaker.threshold == settings.tool_repeat_break_threshold == 3


# ---------------------------------------------------------------------------
# 3. 重复短路：同参命中、异参不命中
# ---------------------------------------------------------------------------


def test_short_circuit_returns_cached_result(clock: FakeClock) -> None:
    guard = _guardrail(clock)
    args = {"city": "杭州", "date": "2026-11-07"}
    cached = {"text": "杭州今天多云"}

    assert guard.check("weather", args).action == "allow"
    guard.remember("weather", args, cached)

    hit = guard.check("weather", args)
    assert hit.action == "short_circuit"
    assert hit.cached_result is cached, "必须原样返回上次结果，不做拷贝/改写"
    assert hit.allowed is True, "short_circuit 属于放行（只是不真的执行工具）"


def test_different_args_do_not_short_circuit(clock: FakeClock) -> None:
    guard = _guardrail(clock)
    guard.check("weather", {"city": "杭州"})
    guard.remember("weather", {"city": "杭州"}, "杭州多云")

    assert guard.check("weather", {"city": "苏州"}).action == "allow"


def test_short_circuit_is_key_order_insensitive(clock: FakeClock) -> None:
    """`canonical_args` 用 `sort_keys`：键序不同的同一组参数算同一缓存键。"""
    guard = _guardrail(clock)
    guard.check("tool", {"a": 1, "b": 2})
    guard.remember("tool", {"a": 1, "b": 2}, "result")

    assert guard.check("tool", {"b": 2, "a": 1}).action == "short_circuit"


def test_canonical_args_is_json_sorted_and_str_fallback() -> None:
    """规范键：键按字典序、中文不转义、不可序列化对象走 `str`。"""
    elder_id = UUID("12345678-1234-5678-1234-567812345678")
    key = canonical_args({"b": 2, "a": elder_id, "name": "张桂芬"})

    assert key == ('{"a": "12345678-1234-5678-1234-567812345678", "b": 2, "name": "张桂芬"}')
    assert canonical_args(None) == "{}"


def test_repeat_cache_expires_after_ttl(clock: FakeClock) -> None:
    """TTL 到期即失效并删除条目（60s 默认，这里用 5s 便于断言）。"""
    cache = RepeatCache(ttl_seconds=5, clock=clock)
    cache.put("weather", {"city": "杭州"}, "多云")
    assert cache.get("weather", {"city": "杭州"}) is not None

    clock.advance(5)
    assert cache.get("weather", {"city": "杭州"}) is None, "TTL 到点必须失效"
    assert len(cache) == 0, "过期条目应被顺手删除"


def test_repeat_cache_evicts_oldest_beyond_capacity(clock: FakeClock) -> None:
    """条目上限防止长会话无界增长：超出后按写入顺序淘汰最旧的。"""
    cache = RepeatCache(ttl_seconds=60, clock=clock, max_entries=2)
    cache.put("tool", {"i": 1}, "r1")
    cache.put("tool", {"i": 2}, "r2")
    cache.put("tool", {"i": 3}, "r3")

    assert cache.get("tool", {"i": 1}) is None
    assert cache.get("tool", {"i": 3}) is not None


# ---------------------------------------------------------------------------
# 4. 判定优先级（§4.6：预算与熔断是并联闸门，short_circuit 在两者之后）
# ---------------------------------------------------------------------------


def test_budget_wins_over_breaker_when_both_trigger(clock: FakeClock) -> None:
    """第 3 次同 Tool 调用同时满足「预算用满」与「熔断打开」→ 返回 reject_budget。

    附带证明：被预算拦下的调用**没有**进入熔断窗口（`is_tripped` 仍为 False）。
    """
    guard = Guardrail(budget=TurnBudget(max_calls=2), clock=clock)

    assert guard.check("weather").action == "allow"
    assert guard.check("weather").action == "allow"

    tie = guard.check("weather")
    assert tie.action == "reject_budget"
    assert guard.breaker.is_tripped("weather") is False, "预算闸门先返回，不得污染熔断窗口"
    assert len(guard.breaker.timestamps("weather")) == 2


def test_breaker_wins_over_short_circuit(clock: FakeClock) -> None:
    """命中缓存的第 3 次同 Tool 调用仍先熔断：short_circuit 排在两道闸门之后。"""
    guard = _guardrail(clock)
    args = {"city": "杭州"}

    assert guard.check("weather", args).action == "allow"
    guard.remember("weather", args, "多云")
    assert guard.check("weather", args).action == "short_circuit"

    third = guard.check("weather", args)
    assert third.action == "reject_breaker"
    assert third.cached_result is None


def test_rejected_call_does_not_consume_budget(clock: FakeClock) -> None:
    """熔断拒绝的调用未执行，因此不占预算（`used` 只增两次）。"""
    guard = _guardrail(clock)
    guard.check("weather")
    guard.check("weather")
    assert guard.check("weather").action == "reject_breaker"

    assert guard.budget.used == 2


# ---------------------------------------------------------------------------
# 5. 话术：拒绝取 `tool_breaker` 键，不新写第三套
# ---------------------------------------------------------------------------


def test_reject_decisions_use_their_own_speech(clock: FakeClock) -> None:
    """预算耗尽与同 Tool 熔断是两个机制、两句话术（不得共用 `tool_breaker`）。"""
    # 两条拒绝路径各用一个门面：预算用 max_calls=1 逼出第 2 次拒绝，
    # 熔断用默认阈值（1 次预算撑不到同 Tool 第 3 次）。
    tight = Guardrail(budget=TurnBudget(max_calls=1), clock=clock)
    assert tight.check("tool_a").action == "allow"
    budget_reject = tight.check("tool_b")
    assert budget_reject.action == "reject_budget"

    guard = _guardrail(clock)
    guard.check("weather")
    guard.check("weather")
    breaker_reject = guard.check("weather")
    assert breaker_reject.action == "reject_breaker"

    assert budget_reject.speech == spoken_speech("tool_budget")
    assert budget_reject.speech != spoken_speech("tool_breaker")
    assert breaker_reject.speech == _BREAKER_SPEECH
    for decision in (budget_reject, breaker_reject):
        assert decision.speech, "拒绝必须给得出对长者说的话"
        assert decision.rejected is True


def test_allow_decisions_have_no_speech(clock: FakeClock) -> None:
    """放行与短路都不该凭空塞话术（语音由正常回答链路产生）。"""
    guard = _guardrail(clock)
    assert guard.check("tool").speech == ""

    guard.remember("tool", {}, "r")
    short = guard.check("tool", {})
    assert short.action == "short_circuit"
    assert short.speech == ""


def test_guardrail_decision_allowed_flags() -> None:
    """`allowed` / `rejected` 是 action 的派生视图，四态一一对应。"""
    allow = GuardrailDecision(action="allow", tool_name="t")
    short = GuardrailDecision(action="short_circuit", tool_name="t", cached_result=1)
    budget = GuardrailDecision(action="reject_budget", tool_name="t", speech="x")
    breaker = GuardrailDecision(action="reject_breaker", tool_name="t", speech="y")

    assert (allow.allowed, short.allowed) == (True, True)
    assert (budget.rejected, breaker.rejected) == (True, True)
    assert budget.cached_result is None


def test_guardrail_reset_turn_keeps_cache(clock: FakeClock) -> None:
    """下一 turn：预算与熔断复位，但缓存保留（跨 turn 短路是这个缓存的价值）。"""
    guard = _guardrail(clock)
    guard.check("weather", {"city": "杭州"})
    guard.remember("weather", {"city": "杭州"}, "多云")

    guard.reset_turn()

    assert guard.check("weather", {"city": "杭州"}).action == "short_circuit"
