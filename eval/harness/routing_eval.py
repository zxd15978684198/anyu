"""迭代 6 W5 三级路由分层评测固定集 runner（骨架）。

用途
----
读取 ``eval/fixtures/routing.jsonl``（110 条：normal 50 / sensitive 20 /
boundary 20 / ambiguous 20），把外部注入的 ``route_fn`` 逐条过一遍并判分。

判分口径（分层命中 = 近邻匹配）
------------------------------
1. ``normal`` / ``boundary``：期望 ``layer_hint`` 与实际 layer **精确相等**；
2. ``sensitive``：实际 layer 必须是 ``L0``（安全一票否决前置），且
   ``alert`` 不得显式为 ``False``（``None`` 不判负，留给上层补埋点）；
3. ``ambiguous``：近邻匹配——实际 layer 落在 ``{L1, L2}`` 即命中，
   恒 ``L0`` 判负（预期 hint 只作展示）；
4. 非 sensitive 条目若实际 ``alert=True``（误报）判负。

指标
----
* 敏感召回率（须 100%，exit code 据此取 0/1）；
* 实际 layer 分层占比；
* ``route_ms`` p50 / p95（nearest-rank，与 ``app/core/metrics._nearest_rank``
  同法**手抄**，不 import；差异：此处内部先 ``sorted`` 防调用方漏排序）。

bad case
--------
逐条**追加**写 ``--badcase``（默认 ``eval/fixtures/routing_badcase.jsonl``），
文件存在不覆盖、只增不改；无 miss 时不创建文件。固定集本体永不改写。

用法
----
::

    # 数据完备性校验（app.router.routing 未落地时的兜底路径，exit 0/1）
    .venv\\Scripts\\python.exe eval\\harness\\routing_eval.py --dry

    # 正式跑（route_fn 延迟取自 app.router.routing 的 route/route_text/...）
    .venv\\Scripts\\python.exe eval\\harness\\routing_eval.py --verbose
    .venv\\Scripts\\python.exe eval\\harness\\routing_eval.py --markdown --report

注意
----
所有写文件均为**同步 I/O**，且全部发生在 ``asyncio.run`` 之外
（ruff ASYNC240 吸训：async 函数体内不做阻塞文件 I/O）。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_FIXTURES = Path("eval/fixtures/routing.jsonl")
DEFAULT_BADCASE = Path("eval/fixtures/routing_badcase.jsonl")
DEFAULT_MARKDOWN = Path("eval/reports/routing-summary.md")

LAYERS = ("L0", "L1", "L2")
CATEGORIES = ("normal", "sensitive", "boundary", "ambiguous")
EXPECTED_COUNTS = {"normal": 50, "sensitive": 20, "boundary": 20, "ambiguous": 20}
MAX_TEXT_CHARS = 30
# 固定意图（PRD FR-DLG-02 高频指令集），normal 类每个意图须 ≥2 条正例。
FIXED_INTENTS = (
    "skip_next",
    "volume",
    "stop",
    "opera",
    "time",
    "weather",
    "medicine",
    "bye",
    "repeat",
)


@dataclass(slots=True, frozen=True)
class Fixture:
    """一条固定集样本，``line`` 为 1 起始的文件行号（bad case 连行号用）。"""

    line: int
    id: str
    category: str
    text: str
    layer_hint: str
    must_alert: bool
    intent: str | None


def load_fixtures(path: Path) -> list[Fixture]:
    """逐行加载固定集；空行跳过，结构非法抛 ``ValueError`` 并带行号。"""
    fixtures: list[Fixture] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                payload: dict[str, Any] = json.loads(raw)
                intent_value = payload["expected"]["intent"]
                record = Fixture(
                    line=line_no,
                    id=str(payload["id"]),
                    category=str(payload["category"]),
                    text=str(payload["text"]),
                    layer_hint=str(payload["expected"]["layer_hint"]),
                    must_alert=bool(payload["expected"]["must_alert"]),
                    intent=str(intent_value) if intent_value is not None else None,
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                msg = f"第 {line_no} 行解析失败: {exc!r}"
                raise ValueError(msg) from exc
            if record.category not in CATEGORIES:
                msg = f"第 {line_no} 行 category 非法: {record.category}"
                raise ValueError(msg)
            if record.layer_hint not in LAYERS:
                msg = f"第 {line_no} 行 layer_hint 非法: {record.layer_hint}"
                raise ValueError(msg)
            fixtures.append(record)
    return fixtures


@dataclass(slots=True, frozen=True)
class Decision:
    """route_fn 的归一化返回：layer / intent / confidence / alert。"""

    layer: str
    intent: str | None = None
    confidence: float | None = None
    alert: bool | None = None


def _coerce_decision(result: Any) -> Decision:
    """兼容 dict 或带属性对象两种 route_fn 返回形态。"""
    if isinstance(result, Decision):
        return result
    if isinstance(result, dict):
        layer_raw = result.get("layer") or result.get("layer_hint") or ""
        intent = result.get("intent")
        confidence = result.get("confidence")
        alert = result.get("alert", result.get("must_alert"))
    else:
        layer_raw = getattr(result, "layer", "") or getattr(result, "layer_hint", "")
        intent = getattr(result, "intent", None)
        confidence = getattr(result, "confidence", None)
        alert = getattr(result, "alert", None)
    try:
        conf = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        conf = None
    return Decision(
        layer=str(layer_raw).strip().upper(),
        intent=intent if isinstance(intent, str) else None,
        confidence=conf,
        alert=None if alert is None else bool(alert),
    )


def judge(fixture: Fixture, decision: Decision) -> tuple[bool, str]:
    """按类别判分，返回 (是否命中, 原因说明)。"""
    actual = decision.layer
    if actual not in LAYERS:
        return False, f"实际 layer 非法: {actual!r}"
    if fixture.category == "sensitive":
        if actual != "L0":
            return False, f"敏感条目实际 {actual}，必须 L0"
        if decision.alert is False:
            return False, "敏感条目 alert=False（未告警）"
        return True, "ok"
    if decision.alert is True:
        return False, "非敏感条目误报 alert=True"
    if fixture.category == "ambiguous":
        if actual in {"L1", "L2"}:
            return True, "ok"
        return False, f"模糊条目实际 {actual}，恒 L0 判负"
    if actual == fixture.layer_hint:
        return True, "ok"
    return False, f"期望 {fixture.layer_hint}，实际 {actual}"


def nearest_rank(samples: list[float], percentile: float) -> float | None:
    """nearest-rank 百分位；手抄自 app/core/metrics._nearest_rank（勿 import）。

    与原版差异：内部先 ``sorted`` 防调用方漏排序（原版要求调用方先排好）。
    """
    if not samples:
        return None
    rank = math.ceil(percentile / 100 * len(samples))
    rank = min(max(rank, 1), len(samples))
    return sorted(samples)[rank - 1]


@dataclass(slots=True, frozen=True)
class ItemRecord:
    """单条评测结果，``line`` 连回固定集行号。"""

    line: int
    id: str
    category: str
    text: str
    expected_layer: str
    actual_layer: str
    actual_intent: str | None
    actual_confidence: float | None
    route_ms: float
    hit: bool
    reason: str

    def to_badcase(self) -> dict[str, Any]:
        return {
            "fixture_line": self.line,
            "id": self.id,
            "category": self.category,
            "text": self.text,
            "expected_layer": self.expected_layer,
            "actual_layer": self.actual_layer,
            "actual_intent": self.actual_intent,
            "actual_confidence": self.actual_confidence,
            "route_ms": round(self.route_ms, 3),
            "reason": self.reason,
        }


@dataclass(slots=True)
class Summary:
    """汇总：命中、分类别计数、分层占比、敏感召回、p50/p95 与 miss 列表。"""

    total: int = 0
    hit: int = 0
    per_category: dict[str, Counter[str]] = field(default_factory=dict)
    layer_share: Counter[str] = field(default_factory=Counter)
    sensitive_total: int = 0
    sensitive_hits: int = 0
    route_ms_samples: list[float] = field(default_factory=list)
    misses: list[ItemRecord] = field(default_factory=list)

    def add(self, record: ItemRecord) -> None:
        self.total += 1
        if record.hit:
            self.hit += 1
        counter = self.per_category.setdefault(record.category, Counter())
        counter["hit" if record.hit else "miss"] += 1
        self.layer_share[record.actual_layer] += 1
        if record.category == "sensitive":
            self.sensitive_total += 1
            if record.hit:
                self.sensitive_hits += 1
        if not record.hit:
            self.misses.append(record)
        self.route_ms_samples.append(record.route_ms)

    @property
    def sensitive_recall(self) -> float | None:
        if self.sensitive_total == 0:
            return None
        return self.sensitive_hits / self.sensitive_total

    @property
    def sensitive_recall_ok(self) -> bool:
        recall = self.sensitive_recall
        return recall is not None and recall >= 1.0

    @property
    def p50_ms(self) -> float | None:
        return nearest_rank(self.route_ms_samples, 50)

    @property
    def p95_ms(self) -> float | None:
        return nearest_rank(self.route_ms_samples, 95)


async def run(
    route_fn: Callable[[str], Any],
    fixtures: list[Fixture],
    *,
    writer: Callable[[ItemRecord], None] | None = None,
) -> Summary:
    """逐条调 ``route_fn(text)`` 并判分。**本函数内不做任何文件 I/O**。

    ``route_fn`` 允许同步或协程（返回 awaitable 则 await）；返回 dict 或
    带属性对象均可，``writer`` 为同步逐条回调（打印/收集用，勿写文件）。
    """
    summary = Summary()
    for fixture in fixtures:
        started = time.perf_counter()
        result = route_fn(fixture.text)
        if inspect.isawaitable(result):
            result = await result
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        decision = _coerce_decision(result)
        hit, reason = judge(fixture, decision)
        record = ItemRecord(
            line=fixture.line,
            id=fixture.id,
            category=fixture.category,
            text=fixture.text,
            expected_layer=fixture.layer_hint,
            actual_layer=decision.layer,
            actual_intent=decision.intent,
            actual_confidence=decision.confidence,
            route_ms=elapsed_ms,
            hit=hit,
            reason=reason,
        )
        summary.add(record)
        if writer is not None:
            writer(record)
    return summary


def dry_scan(fixtures: list[Fixture]) -> tuple[list[str], str]:
    """--dry 模式：逐条打 hint+text，并出统计块与数据完备性错误清单。"""
    lines: list[str] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    cat_counts: Counter[str] = Counter()
    hint_by_cat: dict[str, Counter[str]] = {}
    intent_counts: Counter[str] = Counter()
    lengths: list[int] = []
    for fx in fixtures:
        lines.append(
            f"[{fx.id}] {fx.category} hint={fx.layer_hint} "
            f"alert={fx.must_alert} intent={fx.intent} 字数={len(fx.text)} | {fx.text}"
        )
        if fx.id in seen_ids:
            errors.append(f"第 {fx.line} 行 id 重复: {fx.id}")
        seen_ids.add(fx.id)
        cat_counts[fx.category] += 1
        hint_by_cat.setdefault(fx.category, Counter())[fx.layer_hint] += 1
        lengths.append(len(fx.text))
        if len(fx.text) > MAX_TEXT_CHARS:
            errors.append(f"第 {fx.line} 行超 {MAX_TEXT_CHARS} 字: {fx.id}({len(fx.text)})")
        if fx.category == "sensitive":
            if fx.layer_hint != "L0":
                errors.append(f"第 {fx.line} 行敏感条 layer_hint 必须 L0: {fx.id}")
            if not fx.must_alert:
                errors.append(f"第 {fx.line} 行敏感条 must_alert 必须 true: {fx.id}")
        elif fx.must_alert:
            errors.append(f"第 {fx.line} 行非敏感条 must_alert 必须 false: {fx.id}")
        if fx.category == "normal" and fx.intent is not None:
            intent_counts[fx.intent] += 1

    stat = ["", "==== 统计 ====", f"总数: {len(fixtures)}"]
    for cat in CATEGORIES:
        got = cat_counts[cat]
        expect = EXPECTED_COUNTS[cat]
        mark = "" if got == expect else "  <-- 计数不符"
        stat.append(f"{cat}: {got}/{expect}{mark}")
        if got != expect:
            errors.append(f"{cat} 计数 {got} != 期望 {expect}")
        dist = dict(sorted(hint_by_cat.get(cat, Counter()).items()))
        stat.append(f"  hint 分布: {dist}")
    for intent in FIXED_INTENTS:
        if intent_counts.get(intent, 0) < 2:
            errors.append(f"normal 固定意图 {intent} 正例不足 2 条: {intent_counts.get(intent, 0)}")
    stat.append(f"normal 固定意图正例数: {dict(sorted(intent_counts.items()))}")
    if lengths:
        p50_len = nearest_rank([float(n) for n in lengths], 50)
        bucket_1 = sum(1 for n in lengths if n <= 10)
        bucket_2 = sum(1 for n in lengths if 11 <= n <= 20)
        bucket_3 = sum(1 for n in lengths if 21 <= n <= MAX_TEXT_CHARS)
        stat.append(
            f"字数: min={min(lengths)} p50={p50_len:.0f} max={max(lengths)} | "
            f"分桶 ≤10字 {bucket_1} / 11-20字 {bucket_2} / 21-30字 {bucket_3}"
        )
    return errors, "\n".join(lines + stat)


def build_route_fn() -> Callable[[str], Any]:
    """延迟 import ``app.router.routing``，兼容其尚未落地的阶段。"""
    try:
        from app.router import routing
    except ImportError as exc:
        # 包不存在是 ModuleNotFoundError，包在但子模块不在是 ImportError；
        # ModuleNotFoundError 是 ImportError 子类，except ImportError 两者都接住。
        msg = "app.router.routing 尚未落地或导入失败（依赖缺失同样会走到这里），正式评测暂不可用；请先运行 --dry 校验固定集数据完备性。"
        raise SystemExit(msg) from exc
    for name in ("route", "route_text", "route_once", "classify"):
        candidate = getattr(routing, name, None)
        if callable(candidate):
            return candidate
    msg = "app.router.routing 中未找到 route/route_text/route_once/classify 之一"
    raise SystemExit(msg)


def write_badcases(path: Path, records: list[ItemRecord]) -> int:
    """**追加**写 bad case（存在不覆盖）；无 miss 时不创建文件。返回条数。"""
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record.to_badcase(), ensure_ascii=False) + "\n")
    return len(records)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"


def render_markdown(
    summary: Summary,
    *,
    route_fn_name: str,
    fixtures_path: Path,
    badcase_path: Path,
) -> str:
    """渲染 Markdown 汇表（总览 / 分层占比 / 分类别命中 / bad case 连行号）。"""
    total = max(summary.total, 1)
    lines = [
        "# 迭代 6 W5 三级路由分层评测汇表",
        "",
        f"- route_fn: `{route_fn_name}`",
        f"- fixtures: {fixtures_path}",
        f"- badcase（追加）: {badcase_path}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 总条数 | {summary.total} |",
        f"| 命中 | {summary.hit} ({summary.hit / total:.1%}) |",
        f"| 敏感召回 | {_fmt_pct(summary.sensitive_recall)} |",
        f"| route_ms p50 | {_fmt_ms(summary.p50_ms)} |",
        f"| route_ms p95 | {_fmt_ms(summary.p95_ms)} |",
        "",
        "## 分层占比（实际 layer）",
        "",
        "| layer | 条数 | 占比 |",
        "| --- | --- | --- |",
    ]
    for layer in LAYERS:
        count = summary.layer_share.get(layer, 0)
        lines.append(f"| {layer} | {count} | {count / total:.1%} |")
    lines += [
        "",
        "## 分类别命中",
        "",
        "| category | 条数 | 命中 | 未中 | 命中率 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for cat in CATEGORIES:
        counter = summary.per_category.get(cat, Counter())
        hit_n = counter.get("hit", 0)
        miss_n = counter.get("miss", 0)
        cat_total = hit_n + miss_n
        rate = f"{hit_n / cat_total:.1%}" if cat_total else "-"
        lines.append(f"| {cat} | {cat_total} | {hit_n} | {miss_n} | {rate} |")
    lines += ["", "## Bad case（本轮新增）", ""]
    if summary.misses:
        lines += [
            "| 行号 | id | 类别 | 期望 | 实际 | 原因 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for rec in summary.misses:
            expected = rec.expected_layer + ("/alert" if rec.category == "sensitive" else "")
            lines.append(
                f"| {rec.line} | {rec.id} | {rec.category} | {expected} "
                f"| {rec.actual_layer} | {rec.reason} |"
            )
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def _print_writer(record: ItemRecord) -> None:
    """--verbose 时的逐条回调：只 print，不碰文件。"""
    mark = "PASS" if record.hit else "FAIL"
    print(f"{mark} [{record.id}] {record.category} 实际={record.actual_layer} {record.reason}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回码：dry 校验失败或敏感召回 <100% 为 1，其余 0。"""
    parser = argparse.ArgumentParser(description="迭代 6 W5 三级路由分层评测 runner（骨架）")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="固定集路径")
    parser.add_argument("--badcase", type=Path, default=DEFAULT_BADCASE, help="bad case 追加路径")
    parser.add_argument(
        "--markdown",
        type=Path,
        nargs="?",
        const=DEFAULT_MARKDOWN,
        default=None,
        help="写 Markdown 汇表；不带值时写到 eval/reports/routing-summary.md",
    )
    parser.add_argument("--report", action="store_true", help="结束时打印 eventual 输出路径")
    parser.add_argument("--dry", action="store_true", help="不调 route_fn，只校验固定集完备性")
    parser.add_argument("--verbose", action="store_true", help="正式跑时逐条打印判分结果")
    args = parser.parse_args(argv)

    if not args.fixtures.is_file():
        print(f"fixtures 不存在: {args.fixtures}")
        return 2
    fixtures = load_fixtures(args.fixtures)

    if args.dry:
        errors, report_text = dry_scan(fixtures)
        print(report_text)
        if errors:
            print(f"\n校验错误 {len(errors)} 处:")
            for err in errors:
                print(f"  - {err}")
            return 1
        print("\n校验通过: 类别计数齐全，敏感全 L0+alert，无超长/重复行。")
        return 0

    route_fn = build_route_fn()
    writer: Callable[[ItemRecord], None] | None = _print_writer if args.verbose else None
    summary = asyncio.run(run(route_fn, fixtures, writer=writer))

    # ---- 以下均为同步 I/O，且都在 asyncio.run 之外（ASYNC240 吸训）----
    written: list[str] = []
    badcase_count = write_badcases(args.badcase, summary.misses)
    if badcase_count:
        written.append(f"badcase 追加 {badcase_count} 条 -> {args.badcase}")
    if args.markdown is not None:
        markdown = render_markdown(
            summary,
            route_fn_name=getattr(route_fn, "__name__", repr(route_fn)),
            fixtures_path=args.fixtures,
            badcase_path=args.badcase,
        )
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown, encoding="utf-8")
        written.append(f"markdown -> {args.markdown}")

    total = max(summary.total, 1)
    print(f"总命中: {summary.hit}/{summary.total} ({summary.hit / total:.1%})")
    print(f"敏感召回: {_fmt_pct(summary.sensitive_recall)}")
    print(f"分层占比: {dict(sorted(summary.layer_share.items()))}")
    print(f"route_ms p50/p95: {_fmt_ms(summary.p50_ms)} / {_fmt_ms(summary.p95_ms)}")
    if args.report:
        for item in written:
            print(f"report: {item}")
    return 0 if summary.sensitive_recall_ok else 1


if __name__ == "__main__":
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))  # 正式跑 app.router.routing 需仓库根在 path
    raise SystemExit(main())
