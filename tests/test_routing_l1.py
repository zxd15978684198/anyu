"""L1 三层路由的回归防线（N70 审计缺口 · task-50）。

## 为什么有这个文件

N70 反向审计（task-48）实测：把 `app/router/routing.py` 的 `decide_l1` 调用
删掉，`tests/` 里 **213 条用例全绿**；把 L1 的结果静默改写成
`layer="L2" + degraded="laya_unavailable"`，pytest 与 `tests/eval_gate.py`
**双双 PASS**。也就是说「`route()` 到底有没有把非 L0 文本交给 L1」此前只有
容器里 `decision_logs.layer='L1'` 的行能证明，没有任何可回归的观察点。

本文件补的就是这四条观察点，每条都钉一个**具体值**，被下列真实缺陷打破时
一定变红（不用 `in ("L1", "L2")` 这种恒真式）：

| 用例 | 挡的缺陷 |
| --- | --- |
| `test_l1_five_signals_pass_through_field_by_field` | L1 被静默降级成 L2；五信号在 `route()` 里被丢字段 |
| `test_low_confidence_clarifies_once_then_escalates_to_l2` | 澄清无限次（二次仍留 L1）；升级口径丢失 |
| `test_laya_unavailable_degrades_to_l2_with_marker` | 降级键丢失/改名，L2 落库后分不清「域外」与「Laya 挂了」 |
| `test_l0_hit_never_calls_decide_l1` | L0 被跳过低层直上 L1（AGENTS §6「低层不得被高层覆盖」） |

## 口径

- Laya 一律用替身（`_FakeLaya`）：不连 7001、不起容器，`classify` 只回放
  预设信号或抛预设异常。
- `Settings` 不参与：替身自带 `settings.laya_confidence_threshold` 快照，
  与 `decide_l1` 取阈值的口径一致（它读的就是 `client.settings`）。
- `route()` 的 L0 短路在 `app/router/rules.py`，这里只断言「命中 L0 时
  `decide_l1` 一次都不许被调」——即三级升序的硬约束。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.router import routing
from app.router.decision import LAYER_L0, LAYER_L1, LAYER_L2
from app.router.rules import match_l0
from app.system_one.client import FiveSignal, LayaUnavailable
from app.system_one.questions import (
    DEGRADED_LAYA_UNAVAILABLE,
    DEGRADED_LOW_CONFIDENCE_CLARIFY,
    DEGRADED_LOW_CONFIDENCE_TWICE,
)

_ELDER_ID = "11111111-1111-4111-8111-111111111111"
_THREAD_ID = "thread-l1-regression"
_TRACE_ID = "trace-l1-regression"
_NOW = 1_781_000_000.0

#: 非 L0 文本：`match_l0` 对它返回 None。用例里会再断言一次——规则表一旦
#: 把这句话收进 L0，前置断言先红，而不是让后面四条用例一起变成假绿。
_PLAIN_TEXT = "给我讲讲你小时候的事"


class _FakeLaya:
    """替身 Laya：只实现 `route()` 真正碰到的两个口子（`settings` / `classify`）。"""

    def __init__(
        self,
        *,
        signal: FiveSignal | None = None,
        error: Exception | None = None,
        threshold: float = 0.85,
    ) -> None:
        self.signal = signal
        self.error = error
        self.settings = SimpleNamespace(laya_confidence_threshold=threshold)
        self.calls: list[str] = []

    async def classify(self, text: str) -> FiveSignal:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        assert self.signal is not None
        return self.signal


async def _route(
    laya: _FakeLaya,
    *,
    text: str = _PLAIN_TEXT,
    turn_state: dict[str, Any] | None = None,
) -> Any:
    """走真正的 `app.router.routing.route()`，只把 Laya 换成替身。"""
    return await routing.route(
        text,
        laya=laya,  # type: ignore[arg-type]
        turn_state={} if turn_state is None else turn_state,
        now=_NOW,
        elder_id=_ELDER_ID,
        thread_id=_THREAD_ID,
        trace_id=_TRACE_ID,
    )


def test_plain_text_is_outside_l0() -> None:
    """前置：本文件选的文本确实不被 L0 命中（否则下面四条都被短路，全成假绿）。"""
    assert match_l0(_PLAIN_TEXT) is None


@pytest.mark.asyncio
async def test_l1_five_signals_pass_through_field_by_field() -> None:
    """高置信 → L1，且五信号逐字段等于替身返回值（专挡「静默降级成 L2」）。"""
    assert match_l0(_PLAIN_TEXT) is None
    laya = _FakeLaya(
        signal=FiveSignal(
            intent="opera",
            sensitive=False,
            confidence=0.91,
            privacy=True,
            is_out_of_domain=False,
        )
    )

    decision = await _route(laya)

    assert laya.calls == [_PLAIN_TEXT]
    assert decision.layer == LAYER_L1
    assert decision.intent == "opera"
    assert decision.confidence == 0.91
    assert decision.sensitive is False
    assert decision.privacy is True
    assert decision.is_out_of_domain is False
    assert decision.degraded is None
    assert (
        decision.elder_id,
        decision.thread_id,
        decision.trace_id,
    ) == (_ELDER_ID, _THREAD_ID, _TRACE_ID)


@pytest.mark.asyncio
async def test_low_confidence_clarifies_once_then_escalates_to_l2() -> None:
    """低置信首轮 L1 + 澄清；同一 turn 第二轮 L2 + 升级口径（挡「澄清无限次」）。"""
    turn_state: dict[str, Any] = {}
    laya = _FakeLaya(
        signal=FiveSignal(
            intent="chitchat",
            sensitive=False,
            confidence=0.42,
            privacy=False,
            is_out_of_domain=False,
        )
    )

    first = await _route(laya, turn_state=turn_state)
    second = await _route(laya, turn_state=turn_state)

    assert first.layer == LAYER_L1
    assert first.degraded == DEGRADED_LOW_CONFIDENCE_CLARIFY == "low_confidence_clarify"
    assert first.confidence == 0.42
    assert turn_state["clarified"] is True
    assert second.layer == LAYER_L2
    assert second.degraded == DEGRADED_LOW_CONFIDENCE_TWICE == "low_confidence_twice"
    assert second.intent == "chitchat"
    assert len(laya.calls) == 2


@pytest.mark.asyncio
async def test_laya_unavailable_degrades_to_l2_with_marker() -> None:
    """Laya 不可用 → L2，且 `degraded` 必须还是 `laya_unavailable`（挡降级键丢失）。"""
    laya = _FakeLaya(error=LayaUnavailable("laya disabled by settings (laya_enabled=false)"))

    decision = await _route(laya)

    assert decision.layer == LAYER_L2
    assert decision.degraded == DEGRADED_LAYA_UNAVAILABLE == "laya_unavailable"
    assert decision.intent is None
    assert decision.confidence is None
    assert (
        decision.elder_id,
        decision.thread_id,
        decision.trace_id,
    ) == (_ELDER_ID, _THREAD_ID, _TRACE_ID)


@pytest.mark.asyncio
async def test_l0_hit_never_calls_decide_l1(monkeypatch: pytest.MonkeyPatch) -> None:
    """L0 命中必须短路：`decide_l1` 调用次数 0、Laya 也一次都不许问（AGENTS §6）。"""
    calls: list[str] = []

    async def _forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("decide_l1")
        raise AssertionError("L0 命中时不许调 decide_l1：三级路由是升序，低层不得被覆盖")

    monkeypatch.setattr(routing, "decide_l1", _forbidden)
    laya = _FakeLaya(
        signal=FiveSignal(
            intent="other",
            sensitive=False,
            confidence=0.99,
            privacy=False,
            is_out_of_domain=False,
        )
    )

    decision = await _route(laya, text="我胸口疼")

    assert calls == []
    assert laya.calls == []
    assert decision.layer == LAYER_L0
    assert decision.rule_id == "safety_emergency"
    assert decision.intent == "alert"
    assert decision.sensitive is True
    assert (
        decision.elder_id,
        decision.thread_id,
        decision.trace_id,
    ) == (_ELDER_ID, _THREAD_ID, _TRACE_ID)
