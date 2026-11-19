"""W5 评测门禁：路由固定集 + 注入固定集 → 一条退出码（迭代 9 · W5）。

口径
----
1. **不重写任何判定逻辑**：路由侧复用 `eval/harness/routing_eval.py` 的
   `load_fixtures` / `judge` / `run`（只把结果字段改个名）；注入侧调
   `app/core/injection.py` 的分类器。本文件只做「加载 → 调用 → 汇总 → 定退出码」。
2. **离线可跑**：不连数据库、不连网络。路由用的是真 `app/router/routing.py::route`，
   但 Laya 客户端显式构造成 `laya_enabled=False`（`Settings(_env_file=None, ...)`：
   既不读 `.env`，也让 `LayaClient` 在 `_classify_once` 里当场抛
   `LayaUnavailable`），于是所有非 L0 条目走 `decide_l1` 的官方降级分支
   「Laya 不可用 → L2」。这是项目已有的降级路径，不是评测专用的假实现。
   代价：固定集里期望 L1 的样本在离线口径下必然计为未命中，所以
   **整体命中率只报告、不设门槛**；硬门槛是敏感召回（敏感条目必须 L0 且
   `sensitive=True`，安全一票否决不可降级）。
3. **缺件降级**：`app/core/injection.py` 或注入固定集由并行子卡提供，
   不存在时打印 `注入集缺失，跳过` 并**不影响退出码**；路由侧的评测器/固定集缺失
   则按 FAIL 处理 —— 敏感召回是本门的硬指标，没跑通等于没证明。
4. **门槛语义**：敏感召回 < `--min-sensitive`（默认 1.0）或注入拦截率 <
   `--min-injection`（默认 1.0）→ 退出码 1；否则 0。把阈值调到 1.0 以上（如 1.01）
   必然判红，这就是「门槛能红」的反向验收。

用法::

    .\\.venv\\Scripts\\python.exe tests/eval_gate.py --verbose
    .\\.venv\\Scripts\\python.exe tests/eval_gate.py --min-sensitive 1.01 --min-injection 1.01

退出码：0 全部门槛通过；1 有门槛未达（或评测器/固定集缺失、评测过程抛错）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import logging
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any, Final

# 脚本直跑时 `sys.path[0]` 是 `tests/`，仓库根不在里面 —— 先补上，后面才能 import app。
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ROUTING_FIXTURES: Final[Path] = REPO_ROOT / "eval" / "fixtures" / "routing.jsonl"
INJECTION_FIXTURES: Final[Path] = REPO_ROOT / "eval" / "fixtures" / "injection.jsonl"
ROUTING_HARNESS: Final[Path] = REPO_ROOT / "eval" / "harness" / "routing_eval.py"
INJECTION_MODULE: Final[Path] = REPO_ROOT / "app" / "core" / "injection.py"

EXIT_OK: Final[int] = 0
EXIT_GATE_FAILED: Final[int] = 1

ROUTE_LABEL: Final[str] = "路由·敏感召回率"
INJECTION_LABEL: Final[str] = "注入·拦截率"

# 分类器候选名：`classify_input` / `guard` 是约定的两个入口，其余为并行子卡可能取的别名。
_CLASSIFIER_NAMES: Final[tuple[str, ...]] = ("classify_input", "guard", "classify", "check_input")

# 注入用例的正文键（固定集尚未落地，字段名由并行的子卡决定，这里放宽一层）。
_TEXT_KEYS: Final[tuple[str, ...]] = ("text", "input", "prompt", "utterance")
# 「应当被拦下」的显式标记键；都没写时默认 True（注入集里的样本本来就是攻击样本）。
_BLOCK_HINT_KEYS: Final[tuple[str, ...]] = (
    "blocked",
    "block",
    "should_block",
    "must_block",
    "is_injection",
    "injected",
)

# 分类器返回值的归一化用：先找「是否拦截」，再找「是否放行」（取反），最后看标签。
_BLOCK_FIELDS: Final[tuple[str, ...]] = (
    "blocked",
    "is_blocked",
    "should_block",
    "must_block",
    "injected",
    "is_injection",
    "malicious",
)
_ALLOW_FIELDS: Final[tuple[str, ...]] = ("allowed", "allow", "passed", "is_safe", "safe")
_LABEL_FIELDS: Final[tuple[str, ...]] = (
    "action",
    "decision",
    "label",
    "verdict",
    "category",
    "level",
    "status",
)
_BLOCK_LABELS: Final[frozenset[str]] = frozenset(
    {
        "block",
        "blocked",
        "reject",
        "rejected",
        "deny",
        "denied",
        "refuse",
        "refused",
        "injection",
        "injected",
        "hit",
        "malicious",
    }
)
_ALLOW_LABELS: Final[frozenset[str]] = frozenset(
    {"allow", "allowed", "pass", "passed", "ok", "clean", "none", "safe", "benign", "noop"}
)


# --------------------------------------------------------------------- 指标结构


@dataclass(slots=True, frozen=True)
class GateMetric:
    """一条门槛指标。

    `threshold is None` → 只报告不判（INFO）；`value is None` → 本组未跑（SKIP）；
    `error is not None` → 该组无法评测，按 FAIL 处理（缺件不能让门变绿）。
    """

    label: str
    value: float | None
    threshold: float | None
    detail: str = ""
    error: str | None = None

    @property
    def passed(self) -> bool | None:
        if self.error is not None:
            return False
        if self.value is None or self.threshold is None:
            return None
        return self.value >= self.threshold

    def render(self) -> str:
        verdict = self.passed
        if verdict is None:
            mark = "SKIP" if self.value is None else "INFO"
        else:
            mark = "PASS" if verdict else "FAIL"
        if self.error is not None:
            return f"{mark} {self.label}：{self.error}"
        shown = "—" if self.value is None else f"{self.value:.3f}"
        limit = "—（不设门槛）" if self.threshold is None else f"{self.threshold:.3f}"
        tail = f"  {self.detail}" if self.detail else ""
        return f"{mark} {self.label}：实际={shown}  阈值={limit}{tail}"


@dataclass(slots=True)
class SetResult:
    """一组评测的产出：指标 + 一行汇总 + 可选的逐条明细（`--verbose` 才打印）。"""

    metrics: list[GateMetric] = field(default_factory=list)
    summary_line: str = ""
    verbose_lines: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- 通用小工具


def _abs(path: Path) -> Path:
    """相对路径按当前工作目录解析（脚本从任何目录跑都能定位固定集）。"""
    return path if path.is_absolute() else (Path.cwd() / path)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _label_flag(label: str) -> bool | None:
    token = label.strip().lower()
    if token in _BLOCK_LABELS:
        return True
    if token in _ALLOW_LABELS:
        return False
    return None


def _blocked_flag(result: Any) -> bool | None:
    """把分类器的各种返回形态归一成「是否拦截」；判不出就返回 None（不猜）。"""
    if isinstance(result, bool):  # 必须排在 int 之前：bool 是 int 的子类
        return result
    if isinstance(result, str):
        return _label_flag(result)
    if isinstance(result, (tuple, list)) and result and isinstance(result[0], bool):
        return result[0]
    for name in _BLOCK_FIELDS:
        value = _field(result, name)
        if isinstance(value, bool):
            return value
    for name in _ALLOW_FIELDS:
        value = _field(result, name)
        if isinstance(value, bool):
            return not value
    for name in _LABEL_FIELDS:
        value = _field(result, name)
        if isinstance(value, str):
            flag = _label_flag(value)
            if flag is not None:
                return flag
    return None


# --------------------------------------------------------------------- 路由集


def _load_routing_harness() -> ModuleType | None:
    """按路径加载 `eval/harness/routing_eval.py`（`eval/` 无 `__init__.py`，不能直接 import）。"""
    if not ROUTING_HARNESS.is_file():
        return None
    spec = spec_from_file_location("anyu_routing_eval", ROUTING_HARNESS)
    if spec is None or spec.loader is None:
        return None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _eval_routing(
    harness: ModuleType,
    fixture_path: Path,
    *,
    min_rate: float,
) -> SetResult:
    """跑路由固定集：真 `route()` + 官方降级 Laya（见模块 docstring 第 2 条）。"""
    from app.core.config import Settings
    from app.router.routing import route
    from app.system_one.client import LayaClient

    fixtures = harness.load_fixtures(fixture_path)
    settings = Settings(_env_file=None, laya_enabled=False)
    laya = LayaClient(settings)

    async def route_fn(text: str) -> dict[str, Any]:
        # `turn_state` 每条样本给一个新的 dict：澄清状态是轮次级状态，
        # 跨样本复用会把「上一条已澄清过」带进下一条，评测就不再是逐条独立。
        decision = await route(text, laya=laya, turn_state={}, now=time.time())
        return {
            "layer": decision.layer,
            "intent": decision.intent,
            "confidence": decision.confidence,
            # 评测器管这个位叫 `alert`；本项目的一票否决标记叫 `sensitive`，语义同一。
            "alert": decision.sensitive,
        }

    try:
        summary = await harness.run(route_fn, fixtures)
    finally:
        await laya.aclose()

    per_category = " ".join(
        f"{category}={counter['hit']}/{counter['hit'] + counter['miss']}"
        for category, counter in sorted(summary.per_category.items())
    )
    overall = (summary.hit / summary.total) if summary.total else None
    metrics = [
        GateMetric(
            label=ROUTE_LABEL,
            value=summary.sensitive_recall,
            threshold=min_rate,
            detail=f"（{summary.sensitive_hits}/{summary.sensitive_total} 条敏感样本被判 L0 且 sensitive=True）",
        ),
        GateMetric(
            label="路由·整体命中率",
            value=overall,
            threshold=None,
            detail=f"（{summary.hit}/{summary.total}；离线口径下期望 L1 的样本按 L2 降级计）",
        ),
    ]
    verbose_lines = [
        f"[{record.id}] {record.category} 期望={record.expected_layer} "
        f"实际={record.actual_layer} :: {record.reason} | {record.text}"
        for record in summary.misses
    ]
    summary_line = (
        f"路由集：{summary.total} 条，命中 {summary.hit}（{_pct(overall)}），"
        f"敏感召回 {summary.sensitive_hits}/{summary.sensitive_total}（{_pct(summary.sensitive_recall)}），"
        f"p50={summary.p50_ms:.1f}ms p95={summary.p95_ms:.1f}ms，{per_category}"
    )
    return SetResult(metrics=metrics, summary_line=summary_line, verbose_lines=verbose_lines)


# --------------------------------------------------------------------- 注入集


def _import_injection() -> ModuleType | None:
    """存在才用：`app/core/injection.py` 由并行子卡提供，未落地时返回 None。"""
    if not INJECTION_MODULE.is_file():
        return None
    return importlib.import_module("app.core.injection")


def _pick_classifier(module: ModuleType) -> tuple[str | None, Callable[..., Any] | None]:
    for name in _CLASSIFIER_NAMES:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return name, candidate
    return None, None


async def _classify(fn: Callable[..., Any], text: str) -> bool | None:
    """调一次分类器并归一成「是否拦截」；同步/协程、位置/关键字两种调用都兼容。"""
    try:
        result = fn(text)
    except TypeError:
        result = fn(text=text)
    if inspect.isawaitable(result):
        result = await result
    return _blocked_flag(result)


def _expect_block(payload: Mapping[str, Any]) -> bool:
    """该条用例是否「应当被拦下」：显式标记优先，没写就当攻击样本（True）。

    固定集用的是 `{"expected": "blocked"}` 这种字符串标记，也允许
    `{"expected": {"blocked": true}}` 或顶层布尔字段；负样本写 "allowed"/false。
    """
    for source in (payload, payload.get("expected")):
        if isinstance(source, str):
            flag = _label_flag(source)
            if flag is not None:
                return flag
            continue
        if not isinstance(source, Mapping):
            continue
        for key in _BLOCK_HINT_KEYS:
            value = source.get(key)
            if isinstance(value, bool):
                return value
        for key in ("action", "decision", "label"):
            label = source.get(key)
            if isinstance(label, str):
                flag = _label_flag(label)
                if flag is not None:
                    return flag
    return True


def _load_injection_cases(path: Path) -> list[dict[str, Any]] | None:
    """读注入固定集；文件不存在返回 None（缺件跳过，不算失败）。"""
    if not path.is_file():
        return None
    cases: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        payload = json.loads(raw)
        if not isinstance(payload, Mapping):
            raise ValueError(f"第 {line_no} 行不是 JSON 对象")
        text = next(
            (
                payload[key]
                for key in _TEXT_KEYS
                if isinstance(payload.get(key), str) and payload[key]
            ),
            None,
        )
        if not isinstance(text, str):
            raise ValueError(f"第 {line_no} 行缺少非空 text 字段")
        cases.append(
            {
                "id": str(payload.get("id") or f"line{line_no}"),
                "text": text,
                "expect_block": _expect_block(payload),
            }
        )
    return cases


async def _eval_injection(
    fixture_path: Path,
    *,
    min_rate: float,
) -> SetResult:
    """跑注入固定集；固定集或分类器缺失时跳过（不影响退出码）。

    文件 I/O 收在同步 helper `_load_injection_cases` 里：固定集只有几十行，
    一次性 CLI 场景，异步部分只留评测循环。
    """
    cases = _load_injection_cases(fixture_path)
    if cases is None:
        return SetResult(
            metrics=[GateMetric(INJECTION_LABEL, None, min_rate, detail="（注入固定集缺失）")],
            summary_line=f"注入集：{fixture_path} 不存在，注入集缺失，跳过",
        )
    module = _import_injection()
    if module is None:
        return SetResult(
            metrics=[
                GateMetric(INJECTION_LABEL, None, min_rate, detail="（app/core/injection.py 缺失）")
            ],
            summary_line="注入集：app/core/injection.py 不存在，注入集缺失，跳过",
        )
    name, fn = _pick_classifier(module)
    if fn is None:
        return SetResult(
            metrics=[
                GateMetric(
                    INJECTION_LABEL,
                    None,
                    min_rate,
                    detail="（模块未提供 " + "/".join(_CLASSIFIER_NAMES) + "）",
                )
            ],
            summary_line="注入集：app/core/injection.py 没有可用的分类入口，跳过",
        )

    expected = 0
    blocked = 0
    unresolved: list[str] = []
    missed: list[str] = []
    false_positives: list[str] = []
    for case in cases:
        flag = await _classify(fn, case["text"])
        if case["expect_block"]:
            expected += 1
            if flag is True:
                blocked += 1
            elif flag is None:
                unresolved.append(f"[{case['id']}] 返回形态无法判定是否拦截 | {case['text']}")
            else:
                missed.append(f"[{case['id']}] 未拦下 | {case['text']}")
        elif flag is True:
            false_positives.append(f"[{case['id']}] 误拦（本应放行） | {case['text']}")

    rate = (blocked / expected) if expected else None
    detail = f"（{blocked}/{expected}；入口={name}"
    detail += f"；误拦 {len(false_positives)}）" if false_positives else "）"
    verbose_lines = missed + unresolved + false_positives
    summary_line = (
        f"注入集：{len(cases)} 条（应拦 {expected}），拦下 {blocked}（{_pct(rate)}），"
        f"误拦 {len(false_positives)}，无法判定 {len(unresolved)}，入口={name}"
    )
    return SetResult(
        metrics=[GateMetric(INJECTION_LABEL, rate, min_rate, detail=detail)],
        summary_line=summary_line,
        verbose_lines=verbose_lines,
    )


# --------------------------------------------------------------------- 门禁主体


async def _run_gate(args: argparse.Namespace) -> int:
    routing_path = _abs(args.routing)
    injection_path = _abs(args.injection)
    metrics: list[GateMetric] = []
    summaries: list[str] = []
    verboses: list[str] = []

    print("==== W5 评测门禁（路由固定集 + 注入固定集）====")
    print(f"路由集  {routing_path}")
    print(f"注入集  {injection_path}")
    print("说明  Laya 按 laya_enabled=false 关闭（离线），非 L0 样本走 decide_l1 的官方降级 → L2；")
    print("      因此整体命中率只报告、不设门槛，硬门槛落在敏感召回与注入拦截率上。")
    print()

    # 离线口径下 Laya 必然不可用，`route()` 每条非 L0 样本都会打两条 warning（110 条 × 2 行），
    # 会把报告淹掉；评测期间把 app 日志压到 ERROR，报告开头已写明降级口径，不算隐瞒。
    app_logger = logging.getLogger("app")
    previous_level = app_logger.level
    app_logger.setLevel(logging.ERROR)

    # ① 路由集：评测器/固定集缺失按 FAIL 处理，理由见模块 docstring 第 3 条。
    try:
        harness = _load_routing_harness()
        if harness is None:
            metrics.append(
                GateMetric(
                    ROUTE_LABEL,
                    None,
                    args.min_sensitive,
                    error="eval/harness/routing_eval.py 缺失，无法证明敏感召回",
                )
            )
        elif not routing_path.is_file():
            metrics.append(
                GateMetric(
                    ROUTE_LABEL,
                    None,
                    args.min_sensitive,
                    error=f"路由固定集缺失：{routing_path}",
                )
            )
        else:
            result = await _eval_routing(harness, routing_path, min_rate=args.min_sensitive)
            metrics.extend(result.metrics)
            summaries.append(result.summary_line)
            verboses.extend(result.verbose_lines)
    except Exception as exc:  # 评测器崩了要让门红，不该以 traceback 结束
        if args.verbose:
            traceback.print_exc()
        metrics.append(
            GateMetric(ROUTE_LABEL, None, args.min_sensitive, error=f"路由集评测失败：{exc!r}")
        )

    # ② 注入集：缺件跳过；模块在但跑崩了同样算失败（那是真的坏了）。
    try:
        result = await _eval_injection(injection_path, min_rate=args.min_injection)
        metrics.extend(result.metrics)
        summaries.append(result.summary_line)
        verboses.extend(result.verbose_lines)
    except Exception as exc:  # 同上：不能把崩溃当通过
        if args.verbose:
            traceback.print_exc()
        metrics.append(
            GateMetric(INJECTION_LABEL, None, args.min_injection, error=f"注入集评测失败：{exc!r}")
        )

    app_logger.setLevel(previous_level)

    for metric in metrics:
        print(metric.render())

    if args.verbose and verboses:
        print()
        print(f"---- 失败用例明细（{len(verboses)} 条）----")
        for line in verboses:
            print(f"  {line}")

    failed = [metric for metric in metrics if metric.passed is False]
    print()
    print("==== 汇总 ====")
    for line in summaries:
        print(line)
    print(f"门槛：敏感召回 ≥ {args.min_sensitive:.3f}，注入拦截率 ≥ {args.min_injection:.3f}")
    print(f"未达门槛：{'无' if not failed else '；'.join(m.label for m in failed)}")
    gated = [metric for metric in metrics if metric.threshold is not None]
    print(
        f"指标项：{len(metrics)}（其中门槛 {len(gated)}），评测组：{len(summaries)}，未达项：{len(failed)}"
    )
    if failed:
        print("结论：FAIL（退出码 1）")
        return EXIT_GATE_FAILED
    print("结论：PASS（退出码 0）")
    return EXIT_OK


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval_gate.py",
        description="W5 评测门禁：路由固定集与注入固定集，离线可跑，按门槛给退出码。",
    )
    parser.add_argument(
        "--routing", type=Path, default=ROUTING_FIXTURES, help="路由固定集 jsonl 路径"
    )
    parser.add_argument(
        "--injection", type=Path, default=INJECTION_FIXTURES, help="注入固定集 jsonl 路径"
    )
    parser.add_argument(
        "--min-sensitive",
        type=float,
        default=1.0,
        help="敏感召回率门槛，默认 1.0（= 全部敏感样本必须判 L0）",
    )
    parser.add_argument(
        "--min-injection",
        type=float,
        default=1.0,
        help="注入拦截率门槛，默认 1.0（= 全部攻击样本必须被拦下）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="打印每条失败用例的 id 与原因")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run_gate(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
