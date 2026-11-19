"""迭代 10 · W4 · 跨层观测打点收口（路由分层 / S1 闭环 / LLM token / prompt cache）。

## 为什么要有这一层

`app/core/metrics.py` 只管「把数加上去」，`app/core/observability.py` 只管「记一个 span」，
但**打点口径**必须只有一份：哪一层算命中、什么样的 L1 决策算「直接闭环」、token 字段
叫什么名字。口径散在调用点，半年后必然出现「同一个比率有两个算法」。

调用方（`services` 层）只传业务事实，其余都在这里定。

## 与 `app/audio/_observe.py` 的分工

那份是**音频域**的日志 + 耗时收口（ASR/TTS，迭代 3 落地）；这里是**与域无关**的横切打点。
音频侧接 trace 只额外调一次 `emit_span()` 旁路，不重复实现任何口径。

## 三条自律

1. **绝不抛**：观测失败不能让用药提醒或语音回复失败。所有入口都 `try/except` 兜住，
   出问题只留一条 warning。
2. **不开数据库连接、不 await 网络**：计数是纯内存 dict 自增，导出在 tracer 的后台任务里。
3. **拿不到就不记**：不填占位值。没有样本的比率由 `app/core/metrics.py` 返回 0 或 `None`，
   由读的人去区分「真 0」与「没数据」。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Final

from app.core.metrics import (
    CACHE_HIT_COUNTER,
    CACHE_LOOKUP_COUNTER,
    COMPLETION_TOKEN_COUNTER,
    LAYER_COUNTER,
    PROMPT_TOKEN_COUNTER,
    S1_CLOSED_COUNTER,
    S1_TOTAL_COUNTER,
    counter,
)
from app.core.observability import Span, get_tracer

logger = logging.getLogger("app.observe")

#: 快决策置信度阈值：L1 置信度 ≥ 该值才允许直路由，低于阈值的只澄清一次并升级 L2
#: （AGENTS §7 / PRD §2.3）。这里用常量而不是再读一次配置：它是**业务判据**，不是开关。
S1_DIRECT_CONFIDENCE: Final = 0.85

#: 分层取值白名单。口径来自 `app/router/decision.py`（LAYER_L0/L1/L2），这里写字符串字面量
#: 是刻意的：`core` 横向 import `router` 会违反 AGENTS §2.2 的依赖方向（api/services/core 单向）。
_ROUTE_LAYERS: Final[frozenset[str]] = frozenset({"L0", "L1", "L2"})


# ======================================================================
# 一、span 旁路
# ======================================================================


def emit_span(name: str, *, duration_ms: float | None = None, **attributes: Any) -> Span | None:
    """记一个**已结束**的 span；返回 span 方便测试断言，失败返回 None。

    这是给「本来只有日志、没有计时上下文」的调用点用的（音频域就是这样）：
    它不改变任何既有行为，只多一条 trace。
    """
    try:
        return get_tracer().record(name, duration_ms=duration_ms, attributes=attributes)
    except Exception as exc:
        logger.warning(
            "emit_span failed", extra={"span_name": name, "error_type": type(exc).__name__}
        )
        return None


# ======================================================================
# 二、路由分层与 S1 闭环
# ======================================================================


def record_route(
    layer: str,
    *,
    confidence: float | None = None,
    escalated: bool | None = None,
    intent: str | None = None,
    rule_id: str | None = None,
    sensitive: bool = False,
    route_ms: int | float | None = None,
    degraded: str | None = None,
) -> None:
    """记录一次路由决策：分层计数（L0/L1/L2）+ S1 闭环计数 + `route.decide` span。

    - `layer` 不在 L0/L1/L2 里：只记 span，不计数（宁可缺一条样本，也不把脏值塞进分母）；
    - `escalated` 有值就以调用方为准（路由层知道真实结果）；没有就按置信度阈值推。
    """
    try:
        if layer in _ROUTE_LAYERS:
            counter(LAYER_COUNTER, layer=layer)
            if layer == "L1":
                counter(S1_TOTAL_COUNTER)
                if _s1_closed(confidence=confidence, escalated=escalated):
                    counter(S1_CLOSED_COUNTER)
        attributes: dict[str, Any] = {"route.layer": layer, "route.sensitive": bool(sensitive)}
        if confidence is not None:
            attributes["route.confidence"] = confidence
        if intent:
            attributes["route.intent"] = intent
        if rule_id:
            attributes["route.rule_id"] = rule_id
        if degraded:
            attributes["route.degraded"] = degraded
        if escalated is not None:
            attributes["route.escalated"] = bool(escalated)
        get_tracer().record("route.decide", duration_ms=_as_millis(route_ms), attributes=attributes)
    except Exception as exc:
        logger.warning("record_route failed", extra={"error_type": type(exc).__name__})


def record_decision(decision: object) -> None:
    """`RouteDecision` → `record_route()` 的适配器。

    用 `getattr` 而不是 `from app.router.decision import RouteDecision`：本模块是横切层，
    反向依赖路由层会把 `core` 绑死在某个具体的决策对象上（AGENTS §2.2 依赖方向单向）。
    字段缺失时按「没拿到」处理，不抛 AttributeError。
    """
    try:
        record_route(
            str(getattr(decision, "layer", "") or ""),
            confidence=_as_float(getattr(decision, "confidence", None)),
            intent=_as_str(getattr(decision, "intent", None)),
            rule_id=_as_str(getattr(decision, "rule_id", None)),
            sensitive=bool(getattr(decision, "sensitive", False)),
            route_ms=_as_float(getattr(decision, "route_ms", None)),
            degraded=_as_str(getattr(decision, "degraded", None)),
        )
    except Exception as exc:
        logger.warning("record_decision failed", extra={"error_type": type(exc).__name__})


def _s1_closed(*, confidence: float | None, escalated: bool | None) -> bool:
    """L1 是否**直接闭环**（未升级到 L2）。

    - `escalated` 明确给了值：以它为准（路由层最清楚真实结果）；
    - 否则按阈值推：L1 置信度 ≥ 0.85 才允许直路由，低于阈值的决策一定会澄清或升层；
    - 置信度缺失按「不闭环」算 —— 宁可把闭环率算低，也不要虚报质量。
    """
    if escalated is not None:
        return not escalated
    if confidence is None:
        return False
    return confidence >= S1_DIRECT_CONFIDENCE


# ======================================================================
# 三、LLM 成本与 prompt cache
# ======================================================================


def record_llm_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: str | None = None,
    latency_ms: float | None = None,
) -> None:
    """记录一次 LLM 调用的 token 用量 + `llm.chat` span。

    span 属性用 OTel GenAI 语义约定的名字（`gen_ai.request.model`、
    `gen_ai.usage.input_tokens` / `output_tokens`），将来真要接 OTel 时不用改名。

    三项都是「没拿到」时不记 span：凭空记一条 0 token 的调用会把成本均值拉偏。
    """
    try:
        prompt = _non_negative_int(prompt_tokens)
        completion = _non_negative_int(completion_tokens)
        if prompt == 0 and completion == 0 and latency_ms is None:
            return
        token_labels: dict[str, object] = {"model": model} if model else {}
        if prompt:
            counter(PROMPT_TOKEN_COUNTER, value=prompt, **token_labels)
        if completion:
            counter(COMPLETION_TOKEN_COUNTER, value=completion, **token_labels)
        attributes: dict[str, Any] = {
            "gen_ai.usage.input_tokens": prompt,
            "gen_ai.usage.output_tokens": completion,
        }
        if model:
            attributes["gen_ai.request.model"] = model
        get_tracer().record("llm.chat", duration_ms=_as_millis(latency_ms), attributes=attributes)
    except Exception as exc:
        logger.warning("record_llm_usage failed", extra={"error_type": type(exc).__name__})


def record_prompt_cache(*, hit: bool) -> None:
    """记录一次 provider 侧 prompt cache 查询结果。

    只在 provider **真的回了 cached 相关字段**时调用：拿不到就不调，`prompt_cache_hit_rate()`
    因此会返回 `None`（没数据）而不是 0（一次没命中）—— 后者是告警级结论，不能靠猜。
    """
    try:
        counter(CACHE_LOOKUP_COUNTER)
        if hit:
            counter(CACHE_HIT_COUNTER)
        get_tracer().record("llm.prompt_cache", attributes={"gen_ai.prompt_cache.hit": bool(hit)})
    except Exception as exc:
        logger.warning("record_prompt_cache failed", extra={"error_type": type(exc).__name__})


# ======================================================================
# 四、小工具（全部「拿不到返回 None/0」，不抛）
# ======================================================================


def _as_millis(value: Any) -> float | None:
    """转毫秒；None / 非数值 / 非有限值一律 None（由 `Tracer.record` 兜底成 0 时长）。"""
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return None
    return millis if math.isfinite(millis) else None


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _as_float(value: Any) -> float | None:
    return _as_millis(value)


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


__all__ = [
    "S1_DIRECT_CONFIDENCE",
    "emit_span",
    "record_decision",
    "record_llm_usage",
    "record_prompt_cache",
    "record_route",
]
