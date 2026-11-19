"""三级路由编排薄壳（迭代 6 计划 §4 W4 接线）。

唯一编排入口 ``route()``：L0 纯文本匹配短路 → L1 Laya 决策（含四类失败降级）。
L2 不进 ``route()``——``app/agents/companion.py`` 的 ``run_turn`` 自含 ``layer='L2'``
决策；本层只裁决「这句话该走哪一层」并量化 ``route_ms``。

分层依据（AGENTS §6）：请求固定按 L0 规则层 → L1 System One → L2 System Two 三级
升序，**低层不得被高层覆盖**；规则层失效即 P0 缺陷，所以 L0 命中时不调 Laya、
不调 LLM、不进 L2（反向验收 ①「我胸口疼」必须短路，见计划 §6）。

``route_ms``：由本模块 perf_counter 观测后 ``dataclasses.replace`` 覆盖回决策对象
（迭代 6 计划 §4 W1「route 计时不在此层」，归编排侧统一观测——W2 的 ``decide_l1``
内部不计时）。Windows 上必须用 ``perf_counter``（QPC 亚微秒粒度）而非
``monotonic``（GetTickCount64 约 15.6ms 粒度，P50 会失真——迭代 5 E1 实测教训）。

身份（``elder_id``/``thread_id``/``trace_id``）由调用方从服务端 runtime_context
注入后透传；L0 命中的决策本身不带身份（``match_l0`` 是纯文本入口），由本层把
调用方身份补齐进决策对象再返回——**绝不从 text 里发明身份**（AGENTS §4.9）。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING, Any

from app.router.decision import RouteDecision
from app.router.rules import match_l0
from app.system_one.questions import decide_l1

if TYPE_CHECKING:
    from app.system_one.client import LayaClient

logger = logging.getLogger(__name__)

__all__ = ["route"]


async def route(
    text: str,
    *,
    laya: LayaClient,
    turn_state: dict[str, Any],
    now: float,
    elder_id: str | None = None,
    thread_id: str | None = None,
    trace_id: str | None = None,
) -> RouteDecision:
    """一句话的三级路由裁决：L0 短路优先，未命中交 L1（L1 内部失败降级到 L2）。

    参数与 ``decide_l1`` 对齐（迭代 6 计划 §4 W2/W4）：``turn_state`` 携带
    澄清状态（键 ``clarified``/``clarified_at``/``last_confidence``），``now``
    为澄清窗口判定用的当前时刻；身份三元组只透传不判定。

    返回的 ``RouteDecision`` 已含实测 ``route_ms``（整数毫秒，四舍五入）与
    补齐后的身份字段，可直接交决策日志服务落 ``decision_logs``。
    """
    started = perf_counter()
    decision = match_l0(text)
    if decision is not None:
        # L0 短路：不调 Laya/LLM。纯文本入口拿不到身份，由这里补齐（见模块 docstring）。
        decision = replace(
            decision,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )
    else:
        decision = await decide_l1(
            laya,
            text,
            turn_state=turn_state,
            now=now,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )
    route_ms = round((perf_counter() - started) * 1000.0)
    logger.debug(
        "route decided",
        extra={
            "trace_id": trace_id or "-",
            "layer": decision.layer,
            "rule_id": decision.rule_id or "-",
            "route_ms": route_ms,
        },
    )
    return replace(decision, route_ms=route_ms)
