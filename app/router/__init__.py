"""三级路由的 L0：`safety.py` 安全一票否决 + `rules.py` 高频固定意图（迭代 6）。

规则层数量契约：safety **3** 条 + rules **7** 条 = 合计 **10** 条 ≤ 10 条上限
（AGENTS §7、迭代 6 计划 §4 W1）。外部推荐入口 ``match_l0(text)``：扫描顺序
**safety 一票否决 → rules 固定意图 → None**；safety 命中返回 ``intent='alert'``
且 ``sensitive=True``，调用方据此短路进人工告警分支，不进 L1/L2。

阈值与词表全部写死在代码里（不进 prompt、不进配置）——规则层失效即 P0 缺陷，
本包内部保持零依赖（只 import stdlib 与自身，不读 Settings）。
"""

from __future__ import annotations

from app.router.decision import (
    LAYER_L0,
    LAYER_L1,
    LAYER_L2,
    LAYER_VALUES,
    RouteDecision,
)
from app.router.rules import (
    DIRECT_RULES,
    DirectBranch,
    DirectRule,
    match_l0,
    match_rules,
)
from app.router.rules import (
    RULE_COUNT as DIRECT_RULE_COUNT,
)
from app.router.safety import (
    ALERT_INTENT,
    SAFETY_RULES,
    SafetyRuleSpec,
    match_safety,
)
from app.router.safety import (
    RULE_COUNT as SAFETY_RULE_COUNT,
)

# 全 router 规则总数（=10），供元测试与看板口径使用；超出 ≤10 上限即契约违规。
ROUTER_RULE_COUNT = SAFETY_RULE_COUNT + DIRECT_RULE_COUNT

__all__ = [
    "ALERT_INTENT",
    "DIRECT_RULES",
    "DIRECT_RULE_COUNT",
    "LAYER_L0",
    "LAYER_L1",
    "LAYER_L2",
    "LAYER_VALUES",
    "ROUTER_RULE_COUNT",
    "SAFETY_RULES",
    "SAFETY_RULE_COUNT",
    "DirectBranch",
    "DirectRule",
    "RouteDecision",
    "SafetyRuleSpec",
    "match_l0",
    "match_rules",
    "match_safety",
]
