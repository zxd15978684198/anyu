# owner=W2 · 依据 设计记录（三级路由） §1-P4 / §4-W2 · 五信号 → 路由决策
"""System One · 五信号 → 路由决策（迭代 6 W2）。

**只做一件事**：拿 `LayaClient.classify` 的五个信号，产出一条 W1 的
`RouteDecision`。不写库（`decision_logs` 归 W4）、不生成澄清话术
（话术归调用方，本文件只给 `degraded` 标记与决策）、不做人工分支的执行
（`sensitive=True` 只在决策里标记 `intent="alert"`，执行在调用方）。

## 决策矩阵（计划 §4-W2，顺序即优先级——安全先于一切，AGENTS §6）

| 顺序 | 条件 | 决策 |
| --- | --- | --- |
| 1 | `sensitive=True` | `layer=L1` + `intent=alert`（人工分支由调用方执行；**先于置信度判断**） |
| 2 | `is_out_of_domain=True` | `layer=L2` + `is_out_of_domain=True`（域外问题不澄清、不查记忆，迭代 7 只消费该标记） |
| 3 | `confidence ≥ laya_confidence_threshold`（默认 0.85） | `layer=L1` 直接路由 |
| 4 | `confidence < 阈值` 且同 turn 未澄清过 | `layer=L1` + `degraded=low_confidence_clarify`；在 `turn_state` 记 `clarified=True` |
| 5 | `confidence < 阈值` 且已澄清过（二次仍模糊） | `layer=L2` + `degraded=low_confidence_twice` |
| 6 | `LayaError`（超时/不可用/非200/坏 payload 任意一类） | `layer=L2` + `degraded=laya_unavailable`（`laya_fallback_to_llm` 语义，**不再向下抛**） |

`turn_state` 由调用方按 **turn 粒度**传入（同一对话轮传同一 dict）：
「同一 turn_id 只澄清一次」= 第一次低置信时在 dict 里落 `clarified=True`，
第二次（同一 dict）再看就升级 L2。澄清话术不属于本文件（计划 §4-W2）。

## 与 W1 的共享契约

`RouteDecision` 与 layer 取值闭集常量来自同波 W1（`app/router/decision.py`）：
构造按关键字传**前三位必填** `elder_id` / `thread_id` / `layer` + 五信号字段包，
与 decision.py 模块 docstring 的「W2 传入 Laya 五信号时」示例一一对应。
身份与 trace 只从调用方（编排层）的 `runtime_context` 注入后透传——**降级行
（矩阵 6）也带齐身份**，W4 写 `decision_logs` 时直接序列化本对象；**绝不从
text 里发明 elder_id**（AGENTS §4.9）。`privacy` 按同一示例原样透传。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Final

from app.router.decision import LAYER_L1, LAYER_L2, RouteDecision
from app.system_one.client import FiveSignal, LayaClient, LayaError

logger = logging.getLogger("app.system_one.questions")

#: 七类 intent 的**唯一出处**：键 = Laya 输出的 `intent` 取值（刻意对齐
#: `app/core/injection.py::INTENT_WHITELIST` 的既有生产词），值 = `AGENTS.md` §6 /
#: `docs/HARNESS_DESIGN.md:97` 的中文类名，顺序即类目顺序。
#:
#: 为什么必须在这一处：N30 spike 之前「七类」只以中文散落在 AGENTS / HARNESS，
#: 服务壳、评测、路由各写一份就会漂移；`other` 是第七类「其他」。
#: **任何地方要七类，一律 import 本常量，不许再抄一遍**（服务壳
#: `deploy/laya/server.py` 直接拿它当 choice 的 criteria）。
INTENT_CLASSES: Final[dict[str, str]] = {
    "chitchat": "闲聊",
    "medicine": "用药查询",
    "opera": "戏曲点播",
    "movie": "电影点播",
    "weather": "天气查询",
    "health": "健康采集",
    "other": "其他",
}

#: 降级标记三常量：全部写在这里，调用方按 `degraded` 值译话术（澄清话术不在此文件）。
DEGRADED_LAYA_UNAVAILABLE: Final[str] = "laya_unavailable"
DEGRADED_LOW_CONFIDENCE_CLARIFY: Final[str] = "low_confidence_clarify"
DEGRADED_LOW_CONFIDENCE_TWICE: Final[str] = "low_confidence_twice"

#: `turn_state` 里「本 turn 已澄清过一次」的落盘键名。
_CLARIFIED_KEY: Final[str] = "clarified"
_CLARIFIED_AT_KEY: Final[str] = "clarified_at"
_LAST_CONFIDENCE_KEY: Final[str] = "last_confidence"


def _signals_from(signal: FiveSignal) -> dict[str, Any]:
    """五个信号 → RouteDecision 的公共字段包（五个字段全部透传，供 W4 落 `decision_logs`）。"""
    return {
        "intent": signal.intent,
        "sensitive": signal.sensitive,
        "confidence": signal.confidence,
        "privacy": signal.privacy,
        "is_out_of_domain": signal.is_out_of_domain,
    }


def _decision(
    layer: str,
    fields: dict[str, Any],
    *,
    degraded: str | None,
    elder_id: str | None,
    thread_id: str | None,
    trace_id: str | None,
) -> RouteDecision:
    """按 W1 契约统一构造 `RouteDecision`（所有返回点走同一形态，不依赖位置参数）。

    前三位 `elder_id` / `thread_id` / `layer` 是 W1 dataclass 的必填项；
    降级行（`fields` 为空包、无五信号）同样带齐身份与 trace，
    `route_ms` 保持默认 0——按 W1 口径由编排侧统一观测后覆盖再落库。
    """
    return RouteDecision(
        elder_id=elder_id,
        thread_id=thread_id,
        layer=layer,
        degraded=degraded,
        trace_id=trace_id,
        **fields,
    )


def _safety_decision_on_laya_failure(
    text: str,
    *,
    elder_id: str | None,
    thread_id: str | None,
    trace_id: str | None,
) -> RouteDecision | None:
    """Laya 不可用时的兜底敏感判定：直接跑 L0 的安全规则（纯函数、零 I/O）。

    DLG-05 的缺口：`except LayaError` 原先一律返 L2 降级行，而「敏感优先于一切」
    的判定（`signal.sensitive` → `alert`）在**降级之后**才轮到，于是 Laya 挂掉的
    那一刻，「胸口疼得厉害」就只是一句待回复的闲聊——安全一票否决被降级顺手覆盖了。

    规则层 `app/router/safety.py` 不依赖 Laya（纯正则 + 语境判定，AGENTS §6 的 L0），
    是这条链上唯一还能救回命中的判定。这里复用它的适配器 `match_safety`（不重写
    任何正则），只把身份、trace 与降级标记补上：

    - `layer` 仍是 `L0`：这次命中是规则层判出来的，不是 Laya 的五信号；
    - `degraded` 照留 `laya_unavailable`：复盘时要能把它与 L1 正常判出的敏感区分开；
    - `SafetyHit.occurred_at` 仍留空——取时钟属 I/O，由编排层补
      （`text_turn_service._stamp_hit_clock` / `risk_event_service.record_safety_alert`）。

    未命中返回 ``None``，调用方据此维持原来的 L2 降级行为。
    """
    from app.router.safety import match_safety  # 局部 import：本模块不常年依赖规则层

    base = match_safety(text)
    if base is None:
        return None
    return replace(
        base,
        elder_id=elder_id,
        thread_id=thread_id,
        trace_id=trace_id,
        degraded=DEGRADED_LAYA_UNAVAILABLE,
    )


async def decide_l1(
    client: LayaClient,
    text: str,
    *,
    turn_state: dict[str, Any],
    now: float,
    elder_id: str | None = None,
    thread_id: str | None = None,
    trace_id: str | None = None,
) -> RouteDecision:
    """把一句话交给 Laya，按「决策矩阵」产出 `RouteDecision`。

    `text` 只影响 Laya 调用，不进任何日志；`elder_id`/`thread_id`/`trace_id`
    由调用方从服务端 `runtime_context` 注入后透传进决策对象（含降级行），
    本函数不做身份判定（AGENTS §4.9）。
    """
    try:
        signal = await client.classify(text)
    except LayaError as exc:
        # 四类失败同权降级：不往下抛（断 Laya 不许 500 / 卡死，计划 §6 完工标准 4）。
        logger.warning(
            "laya classify failed, degrade to L2",
            extra={
                "trace_id": trace_id or "-",
                "error_type": type(exc).__name__,
                "thread_id": thread_id or "-",
            },
        )
        # DLG-05：降级之前先把安全规则跑一遍——Laya 不在时，「敏感优先于一切」不能
        # 跟着一起消失（「胸口疼得厉害」在上面的 warning 之后就是一句待回复的闲聊了）。
        safety = _safety_decision_on_laya_failure(
            text, elder_id=elder_id, thread_id=thread_id, trace_id=trace_id
        )
        if safety is not None:
            logger.warning(
                "laya unavailable but safety rule hit, keep alert path",
                extra={
                    "trace_id": trace_id or "-",
                    "rule_id": safety.rule_id,
                    "thread_id": thread_id or "-",
                },
            )
            return safety
        return _decision(
            LAYER_L2,
            {},
            degraded=DEGRADED_LAYA_UNAVAILABLE,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )

    # 阈值取注入 client 自带的配置快照：测试可以注入 `_env_file=None` 的 Settings，
    # 避免经由 get_settings() 读取本机 `.env`（任务红线：W2 全程禁读 .env）。
    threshold = client.settings.laya_confidence_threshold

    # 1) 敏感优先于置信度：低置信也必须有安全通道（AGENTS §6 规则优先级）。
    if signal.sensitive:
        fields = _signals_from(signal)
        fields["intent"] = "alert"  # 覆盖透传的 intent：敏感一律送人工告警分支。
        logger.info(
            "laya signal sensitive -> alert handled by caller",
            extra={"trace_id": trace_id or "-", "confidence": signal.confidence},
        )
        return _decision(
            LAYER_L1,
            fields,
            degraded=None,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )

    # 2) 域外问题：不澄清、不查记忆，直接 L2（计划 §1-P4 分支）。
    if signal.is_out_of_domain:
        logger.info(
            "laya signal out-of-domain -> L2",
            extra={"trace_id": trace_id or "-", "intent": signal.intent},
        )
        return _decision(
            LAYER_L2,
            _signals_from(signal),
            degraded=None,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )

    # 3) 高置信：直接路由 L1（阈值为 PRD §2.3 的 0.85，含边界）。
    if signal.confidence >= threshold:
        return _decision(
            LAYER_L1,
            _signals_from(signal),
            degraded=None,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )

    # 5) 同 turn 已澄清过、二次仍模糊：升级 L2，不再纠缠。
    if turn_state.get(_CLARIFIED_KEY):
        logger.info(
            "low confidence twice -> L2",
            extra={"trace_id": trace_id or "-", "confidence": signal.confidence},
        )
        return _decision(
            LAYER_L2,
            _signals_from(signal),
            degraded=DEGRADED_LOW_CONFIDENCE_TWICE,
            elder_id=elder_id,
            thread_id=thread_id,
            trace_id=trace_id,
        )

    # 4) 低置信首次：本层给澄清标记，话术由调用方生成（计划 §4-W2）。
    turn_state[_CLARIFIED_KEY] = True
    turn_state[_CLARIFIED_AT_KEY] = now
    turn_state[_LAST_CONFIDENCE_KEY] = signal.confidence
    return _decision(
        LAYER_L1,
        _signals_from(signal),
        degraded=DEGRADED_LOW_CONFIDENCE_CLARIFY,
        elder_id=elder_id,
        thread_id=thread_id,
        trace_id=trace_id,
    )
