"""三条纯逻辑护栏：每轮工具调用预算、同 Tool 熔断、同参重复短路（迭代 5 §4.4 / §4.6）。

口径出处
- 每轮工具调用预算 `MAX_TOOL_CALLS_PER_TURN = 8`（第 9 次拒绝并告知用户）：PRD FR-DLG-10。
- 同一 Tool 1 分钟内第 3 次调用 → 熔断：PRD FR-DLG-10、技术设计文档 line 841。
- 相同工具 + 相同参数 → 短路返回上次结果：技术设计文档 line 842。
- 「熔断打开后本 turn 内对该 Tool 后续调用一律直接拒绝（不重置），下一 turn 重新计」：
  迭代 5 计划 §4.6。三条阈值的默认值只从 `get_settings()` 读，构造参数可覆盖（测试用）。

判定顺序（**写死，测试钉住**）
1) 预算闸门：本 turn 已用满 `max_calls`（默认 8）→ 直接 `reject_budget`，**不再**触碰熔断状态
   （被预算拦掉的调用根本不会执行，不能污染 60s 熔断窗口）。
2) 熔断闸门：同 Tool 在 `window_seconds`（默认 60）内第 `threshold`（默认 3）次 → `reject_breaker`；
   这一次**不消耗**预算（调用未执行），但**计入**熔断窗口；此后本 turn 内该 Tool 一律拒绝（latch）。
3) 重复短路：两道闸门都放行后，才查 `RepeatCache`；命中未过期缓存 → `short_circuit`，
   直接返回上次结果、不执行工具（但**已消耗预算**：模型确实发出了一次工具调用，预算要拦的是这种循环）。
4) 三道都过 → `allow`。

预算与熔断是**并联闸门**，先触发者生效：二者可同时满足拒绝条件（例如第 3 次同参调用时
预算刚好用满），此时按上面的顺序返回 `reject_budget`；`short_circuit` 永远排在两者之后。

拒绝话术只从 `app/core/degrade_speech.py::spoken_speech("tool_breaker")` 取，
本模块**不新写第三套话术**（AGENTS §4.8、迭代 5 §4.3）。本模块只做纯逻辑，
不 import `mcp_client`、不建连接、不执行任何工具；迭代 6 接线时把它当装饰器包在调用外侧。
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.core.config import get_settings
from app.core.degrade_speech import spoken_speech

# 四态动作闭集。新增动作属契约变更，要同步 迭代 6 的调用外侧接线与 tests/。
GuardrailAction = Literal["allow", "reject_budget", "reject_breaker", "short_circuit"]

# 拒绝话术按机制分开：预算耗尽 ≠ 同一 Tool 熔断（PRD FR-DLG-10 两个机制、两句话术）。
_BUDGET_SPEECH_KEY = "tool_budget"
_BREAKER_SPEECH_KEY = "tool_breaker"


def canonical_args(args: Mapping[str, Any] | None) -> str:
    """参数规范化：`sort_keys` 保证键序无关，`default=str` 让 UUID/Decimal/日期不炸。

    `ensure_ascii=False` 保留中文原样，便于日志里直接读懂是哪个城市的天气。
    """
    return json.dumps(
        dict(args or {}),
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


@dataclass(frozen=True, slots=True)
class GuardrailDecision:
    """一次工具调用闸门的判定结果。`check()` 的返回值，调用方按 `action` 分支处理。"""

    action: GuardrailAction
    tool_name: str
    speech: str = ""  # 仅 reject_* 非空：对长者说的口语降级话术
    cached_result: Any = None  # 仅 short_circuit 有意义：上次结果原样返回

    @property
    def allowed(self) -> bool:
        """`allow` 与 `short_circuit` 都算放行（后者只是不真的执行工具）。"""
        return self.action in ("allow", "short_circuit")

    @property
    def rejected(self) -> bool:
        return not self.allowed


class TurnBudget:
    """每 turn 的工具调用计数。第 `max_calls + 1` 次起不再放行。

    `try_consume()` 成功才自增，被拒时不消耗计数（避免拒绝次数把已用量顶到更高处）。
    """

    def __init__(self, max_calls: int | None = None) -> None:
        # 默认值只从配置读（config.py:166），不在模块里写死 8，改阈值走配置与 §7 评审。
        self.max_calls: int = (
            int(max_calls) if max_calls is not None else get_settings().max_tool_calls_per_turn
        )
        self._used = 0

    @property
    def used(self) -> int:
        """本 turn 已执行（=已消耗预算）的工具调用次数。"""
        return self._used

    def would_allow(self) -> bool:
        """只看状态、不自增：用于「预算先判」时判断是否会被拦（peek）。"""
        return self._used < self.max_calls

    def try_consume(self) -> bool:
        """占一次预算：返回 True 表示已记账；False 表示本轮预算已满。"""
        if not self.would_allow():
            return False
        self._used += 1
        return True

    def reset(self) -> None:
        """进入下一 turn：计数归零。"""
        self._used = 0


class ToolBreaker:
    """同 Tool 熔断：`window_seconds` 内第 `threshold` 次调用即打开，窗口滑出后自动恢复。

    时间源可注入（默认 `time.monotonic`，单调且不受系统时钟校正影响）；
    窗口按**半开区间** `(now - window, now]` 计算，恰好落在边界上的旧调用视为滑出窗口。

    熔断是 **latch（自锁）**：一旦打开，该 Tool 的后续调用一律拒绝，且被拒的调用不记入窗口；
    恢复只有一条路——时间滑过窗口，`reset_turn()` 与 `allow()` 都会先淘汰过期戳，
    当窗口内不足 `threshold - 1` 次时 latch 自动解除。

    判定口径来自 PRD FR-DLG-10 / TD §6.2「**1 分钟内**第 3 次调用触发熔断」：
    它是**跨轮**的时间窗，不是「本轮内」计数；按 turn 清零会让「跨轮连点同一工具」
    永远触发不了熔断（迭代 5 审计 P1-5）。
    """

    def __init__(
        self,
        *,
        window_seconds: float | None = None,
        threshold: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        settings = get_settings()
        self.window_seconds: float = float(
            window_seconds if window_seconds is not None else settings.tool_repeat_window_seconds
        )
        self.threshold: int = int(
            threshold if threshold is not None else settings.tool_repeat_break_threshold
        )
        self._clock: Callable[[], float] = clock or time.monotonic
        # tool_name -> 窗口内的时间戳（deque 左端最旧，便于 popleft 滑窗）
        self._calls: dict[str, deque[float]] = {}
        # 窗口内已打开的 Tool（latch，窗口滑出后由 reset_turn/allow 解除）
        self._tripped: set[str] = set()

    def is_tripped(self, tool_name: str) -> bool:
        """该 Tool 在**当前窗口**内是否已熔断。"""
        return tool_name in self._tripped

    def timestamps(self, tool_name: str) -> tuple[float, ...]:
        """当前窗口内的调用时间戳快照（只读，排障/断言用）。"""
        return tuple(self._calls.get(tool_name, ()))

    def allow(self, tool_name: str) -> bool:
        """记一次调用并判断是否放行：窗口内第 `threshold` 次触发熔断并返回 False。"""
        now = self._clock()
        stamps = self._calls.setdefault(tool_name, deque())
        self._prune(stamps, now)
        if len(stamps) + 1 >= self.threshold:
            # 触发熔断的那一次调用**不执行、也不记入窗口**：否则每次重试都会把窗口
            # 重新填满，latch 就永远等不到「滑出」的那一刻。
            self._tripped.add(tool_name)
            return False
        self._tripped.discard(tool_name)
        stamps.append(now)
        return True

    def reset_turn(self) -> None:
        """进入下一 turn：淘汰滑出窗口的时间戳，latch 随之自然解除（见类 docstring）。

        预算按轮清零，熔断按 1 分钟窗口累计——两者时间尺度不同，不能一起 reset。
        """
        now = self._clock()
        for stamps in self._calls.values():
            self._prune(stamps, now)
        self._tripped = {
            tool for tool, stamps in self._calls.items() if len(stamps) + 1 >= self.threshold
        }

    def _prune(self, stamps: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """缓存条目：结果 + 绝对过期时刻（按注入时钟的刻度）。"""

    value: Any
    expires_at: float


class RepeatCache:
    """同工具同参数的短期结果缓存（TTL 到期即失效）。

    键 = `(tool_name, canonical_args(args))`；TTL 默认复用配置里的
    `tool_repeat_window_seconds`（60s）——不新增配置字段，语义也一致：
    「一分钟内重复的同一个调用」既是要熔断的现象，也是可以安全短路的时间窗。

    条目上限 `max_entries`（默认 256）防止长会话无界增长：满了之后按写入顺序淘汰最旧的，
    被重新 put 的键会移到队尾（近似 LRU 的廉价版本，够用且无额外依赖）。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
        max_entries: int = 256,
    ) -> None:
        self.ttl_seconds: float = float(
            ttl_seconds if ttl_seconds is not None else get_settings().tool_repeat_window_seconds
        )
        self._clock: Callable[[], float] = clock or time.monotonic
        self.max_entries = int(max_entries)
        self._entries: dict[tuple[str, str], CacheEntry] = {}

    def get(self, tool_name: str, args: Mapping[str, Any] | None = None) -> CacheEntry | None:
        """命中未过期条目返回它；不存在或已过期返回 None（过期条目顺手删除）。"""
        key = (tool_name, canonical_args(args))
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        return entry

    def put(self, tool_name: str, args: Mapping[str, Any] | None, value: Any) -> None:
        """写入/覆盖一次调用结果，过期时刻 = now + ttl。"""
        key = (tool_name, canonical_args(args))
        self._entries.pop(key, None)  # 覆盖时移到队尾，保持淘汰顺序
        self._entries[key] = CacheEntry(value=value, expires_at=self._clock() + self.ttl_seconds)
        self._evict()

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def _evict(self) -> None:
        while len(self._entries) > self.max_entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]


class Guardrail:
    """门面：把预算 / 熔断 / 重复缓存三件事合成 `check()` 一次判定。

    典型用法（迭代 6 接 mcp_client 时包在调用外侧）：:

        decision = guardrail.check(tool_name, args)
        if decision.action == "short_circuit":
            return decision.cached_result
        if decision.rejected:
            await speak(decision.speech)
            return None
        result = await call_tool(tool_name, args)
        guardrail.remember(tool_name, args, result)
        return result

    下一 turn 开始时调用 `reset_turn()`：预算归零；熔断**不归零**，只淘汰滑出
    1 分钟窗口的旧时间戳（PRD FR-DLG-10 的口径是时间窗，不是 turn）；**缓存不清**
    （跨 turn 的重复调用同样可以短路，这正是它的价值）。
    """

    def __init__(
        self,
        *,
        budget: TurnBudget | None = None,
        breaker: ToolBreaker | None = None,
        cache: RepeatCache | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        # 未显式传入的子件都共用同一个时钟，避免「熔断用假钟、缓存用真钟」的测试陷阱。
        self.budget = budget if budget is not None else TurnBudget()
        self.breaker = breaker if breaker is not None else ToolBreaker(clock=self._clock)
        self.cache = cache if cache is not None else RepeatCache(clock=self._clock)

    def check(self, tool_name: str, args: Mapping[str, Any] | None = None) -> GuardrailDecision:
        """按 §4.6 的顺序判定：预算 → 熔断 → 重复短路 → 放行。"""
        # 1) 预算闸门：先判且拒绝即返回，不触碰熔断状态。
        if not self.budget.would_allow():
            return GuardrailDecision(
                action="reject_budget",
                tool_name=tool_name,
                speech=spoken_speech(_BUDGET_SPEECH_KEY),
            )
        # 2) 熔断闸门：计入窗口；被拒时调用不执行，因此不消耗预算。
        if not self.breaker.allow(tool_name):
            return GuardrailDecision(
                action="reject_breaker",
                tool_name=tool_name,
                speech=spoken_speech(_BREAKER_SPEECH_KEY),
            )
        # 3) 两道闸门都过 → 记账，再看能否短路。
        self.budget.try_consume()
        entry = self.cache.get(tool_name, args)
        if entry is not None:
            return GuardrailDecision(
                action="short_circuit",
                tool_name=tool_name,
                cached_result=entry.value,
            )
        return GuardrailDecision(action="allow", tool_name=tool_name)

    def remember(self, tool_name: str, args: Mapping[str, Any] | None, result: Any) -> None:
        """工具真实执行成功后登记结果，后续同参调用可被短路。"""
        self.cache.put(tool_name, args, result)

    def reset_turn(self) -> None:
        """进入下一 turn：预算归零 + 熔断按窗口淘汰（缓存保留，见类 docstring）。"""
        self.budget.reset()
        self.breaker.reset_turn()


__all__ = [
    "CacheEntry",
    "Guardrail",
    "GuardrailAction",
    "GuardrailDecision",
    "RepeatCache",
    "ToolBreaker",
    "TurnBudget",
    "canonical_args",
]
