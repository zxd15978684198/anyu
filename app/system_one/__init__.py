"""System One：Laya 本地分类器客户端 + 五信号到路由决策（迭代 6 W2）。

五信号口径：intent / sensitive / confidence / privacy / is_out_of_domain
（计划 S-D6-1；AGENTS §6 的「四信号」清单待产品负责人裁决回填）。
`RouteDecision` 由 W1 的 `app.router.decision` 产出，`questions.decide_l1`
在函数体内延迟绑定它（并行波次开工时可能尚未落盘）。
"""

from app.system_one.client import (
    FiveSignal,
    LayaBadPayload,
    LayaBadStatus,
    LayaClient,
    LayaError,
    LayaTimeout,
    LayaUnavailable,
)
from app.system_one.questions import (
    DEGRADED_LAYA_UNAVAILABLE,
    DEGRADED_LOW_CONFIDENCE_CLARIFY,
    DEGRADED_LOW_CONFIDENCE_TWICE,
    decide_l1,
)

__all__ = [
    "DEGRADED_LAYA_UNAVAILABLE",
    "DEGRADED_LOW_CONFIDENCE_CLARIFY",
    "DEGRADED_LOW_CONFIDENCE_TWICE",
    "FiveSignal",
    "LayaBadPayload",
    "LayaBadStatus",
    "LayaClient",
    "LayaError",
    "LayaTimeout",
    "LayaUnavailable",
    "decide_l1",
]
