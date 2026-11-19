"""路由决策值对象：三级路由的唯一共享契约（迭代 6 计划 §4 W1／W4；W2 直接依赖）。

口径出处
- `layer` 取值闭集 ``'L0' | 'L1' | 'L2'``：AGENTS §6「三级升序、低层不得被高层覆盖」；
  与 迭代 6 计划 §4 W4 的迁移 ``0008``（`decision_logs.layer` CHECK 枚举；
  S-D6-M1 改号：0007 号位已由 0007_health_reports 占用）同源。
- 出名分层（W4 写 `decision_logs` 的字段名以本类为准）：`sensitive` / `is_out_of_domain`
  恒为 bool（False 显式表达「未判定为不属于」），`confidence` 可为 None
  （L0/降级层没有置信度语义）。
- 必填三个：`elder_id` / `thread_id` / `layer`。W4 落库三方都有值；而 `match_l0`
  这类纯文本入口拿不到身份，以 None 构造，由外层编排补齐——身份只从服务端
  `runtime_context` 注入（AGENTS §4.9），**绝不从 text 里发明 elder_id**。
- `route_ms` 默认 0：路由计时不归属 L0 文本匹配（迭代 6 计划 §4 W1「route 计时
  不在此层」），由 W2/W4 编排侧统一观测后覆盖该字段再落库。
- `privacy` 的真伪判定只有 `is_privacy_converged` 一个口径，且**唯一实现就在本模块**
  （2026-11-09 W4 上收）：`app/agents/companion.py`（真则跳过记忆召回）与
  `app/audio/reply_stub.py`（真则前置收敛话术）都 import 它。此前两处各有一份镜像
  实现、靠注释约定「改一处必须同改另一处」；本模块只 import stdlib，反向不依赖任何
  使用方，`audio ↔ agents` 的横向依赖（AGENTS §2.2）因此不再需要靠人工同步规避。

构建示例::

    # L0 文本匹配（纯文本入口，身份/trace 留空交给编排层）：
    RouteDecision(elder_id=None, thread_id=None, layer=LAYER_L0,
                  rule_id="safety_emergency", intent="alert", sensitive=True)
    # W2 传入 Laya 五信号时：
    RouteDecision(elder_id=ctx.elder_id, thread_id=ctx.thread_id, layer=LAYER_L1,
                  intent=signals.intent, sensitive=signals.is_sensitive,
                  confidence=signals.confidence, is_out_of_domain=signals.is_out_of_domain,
                  privacy=signals.privacy)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

LAYER_L0 = "L0"
LAYER_L1 = "L1"
LAYER_L2 = "L2"

# layer 取值闭集，迁移 0007 的 CHECK 与这里同源；新增层级属契约变更，须同步改迁移。
LAYER_VALUES: tuple[str, ...] = (LAYER_L0, LAYER_L1, LAYER_L2)


@dataclass(frozen=True, slots=True)
class SafetyHit:
    """一次安全规则命中的**证据**（迭代 8 N01；随 `RouteDecision.alert` 传递）。

    为什么要有它：`match_safety` 今天只回一个 `rule_id`+`sensitive=True`，判定结果
    到落库之间没有任何中间结构，于是「为什么判成急症」在事后无法复核——误报只能靠
    `false_alarm` 手工打标而没有依据（迭代 8 计划 §2 第②条）。本对象就是那层证据。

    字段语义
    - ``category``：三值 ``emergency`` / ``harm`` / ``request_human``，与 `rule_id`
      一一对应；告警分级与话术选择都以它为准，而不是靠解析 `rule_id` 字符串。
    - ``severity``：迭代 8 的红色事件只有一档 ``red``。留字段是为了让「以后要不要分级」
      成为数据问题，而不是改一次表结构。
    - ``matched``：命中的**片段**（正则命中的原文片段），用于人工复核与回归断言。
    - ``transcript_excerpt``：命中处前后各 8 字的上下文切片。**写库前必须过脱敏**
      （`app/core/` 的脱敏工具；AGENTS §4.10 不允许把未脱敏原话落库）——本对象只是
      在纯文本层把切片带出来，脱敏发生在落库侧，因为 `app/router/safety.py` 自述
      「纯文本、纯函数、无 I/O」，不该在这里 import 配置或日志组件。
    - ``occurred_at``：命中时刻。**由编排层填**（同上的纯函数理由：规则层不取时钟）。
    """

    rule_id: str
    category: str
    severity: str
    matched: str
    transcript_excerpt: str
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """一次路由判定的不可变值对象，可直接序列化写 ``decision_logs``（迭代 6 §4 W4）。

    字段顺序与默认值就是契约：W2/W3/W4 均按关键字构造，不要依赖位置。
    """

    elder_id: str | None
    thread_id: str | None
    layer: str
    rule_id: str | None = None
    intent: str | None = None
    sensitive: bool = False
    confidence: float | None = None
    is_out_of_domain: bool = False
    privacy: str | None = None
    route_ms: int = 0
    degraded: str | None = None
    trace_id: str | None = None
    #: 命中安全规则时的证据；非安全命中恒为 None（迭代 8 N01）。
    #: 放在末尾：既有的关键字构造与位置断言都不受影响（本类其余字段顺序即契约）。
    alert: SafetyHit | None = None


# ------------------------------------------------------------------ privacy 谓词


#: `privacy` 的「明确为假」取值集合（`RouteDecision.privacy` 的唯一判定依据，W4 上收）。
#: 契约声明是 `str | None`，但 Laya 五信号透传的是 bool、`decision_log_service` 落库前
#: 才收敛成 "true"/"false"，历史用例里还出现过 "couple"——所以两种都接：只要不是这些
#: 取值就算隐私收敛。
PRIVACY_FALSE_TAGS: Final[frozenset[str]] = frozenset(
    {"", "0", "false", "no", "none", "null", "off"}
)


def is_privacy_converged(privacy: object) -> bool:
    """本轮是否隐私收敛模式（`RouteDecision.privacy` 真伪的**唯一**判定口径）。

    两种形态都要接：Laya 五信号透传的 bool，和落库/契约里的 "true"/"false" 字符串
    （`app/services/decision_log_service.py` 就是这么收敛的），历史用例还出现过
    "couple"、"双人间" 这类标签值。因此只判「是否**明确为假**」：`False` / `None` /
    "false" / "0" / "none" 等算不收敛，其余（True、"true"、"couple"、"双人间"）都算收敛。

    为什么落在本模块：`privacy` 由 `RouteDecision` 产出，谓词跟着字段走不会反向依赖
    任何使用方。两个使用方——`app/agents/companion.py`（真则整段跳过记忆召回）与
    `app/audio/reply_stub.py`（真则前置一句收敛话术）——都 import 这一个函数。此前
    两处各留了一份**镜像**，靠注释约定「改一处必须同改另一处」，因为 `audio` 与
    `agents` 的横向 import 被 AGENTS §2.2 禁止；上收到路由层后，两条链路对「什么算
    收敛」只有一个答案，不会出现「agent 跳过记忆、嘴上却没提醒」这类分叉。

    契约外的类型（上游若漂移成 0/1）走 Python 真值兜底：宁可多说一句，不可漏收敛
    ——漏收敛是隐私事故，多说一句只是啰嗦。
    """
    if isinstance(privacy, bool):
        return privacy
    if privacy is None:
        return False
    if isinstance(privacy, str):
        # 落库侧只写小写，但客户端/评测夹具可能给 `"False"`：大小写不该决定那半句提醒出不出。
        return privacy.strip().lower() not in PRIVACY_FALSE_TAGS
    return bool(privacy)


__all__ = [
    "LAYER_L0",
    "LAYER_L1",
    "LAYER_L2",
    "LAYER_VALUES",
    "PRIVACY_FALSE_TAGS",
    "RouteDecision",
    "SafetyHit",
    "is_privacy_converged",
]
