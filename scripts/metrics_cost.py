#!/usr/bin/env python3
"""从 `/metrics` 文本估算 LLM token 成本（FR-TC-08 · 成本导出）。

**单价不写死，是数据**：本脚本只做两件事——① 读 `/metrics` 里 llm token 计数器的
累计值；② 拿 model 去 `config/llm_prices.json` 查单价，乘出成本。脚本本身不知道
任何一家供应商的价钱，改价只改 JSON，不改代码。

用法：

    uv run python scripts/metrics_cost.py                        # 抓本机 8000 端口，输出 JSON
    uv run python scripts/metrics_cost.py --format csv           # 机器可读表格
    uv run python scripts/metrics_cost.py --metrics-url http://127.0.0.1:8000/metrics
    uv run python scripts/metrics_cost.py --metrics-file build/metrics.txt
    uv run python scripts/metrics_cost.py --as-of 2026-10-15     # 按指定统计日期取价目版本
    uv run python scripts/metrics_cost.py --model deepseek-v4.1-flash
                                                                # 计数器没有 model 标签时归到该模型

## 口径

**读什么**：`llm_prompt_tokens_total` / `llm_completion_tokens_total`；可选
`llm_cache_read_tokens_total`（当前 `/metrics` 还没有这一项，缺失时 cache_read_tokens
为 null、cache_cost 恒为 null，**不拿 0 冒充**）。

**为什么有 `--model`**：真实 `/metrics` 里这两个计数器**不带 model 标签**
（`app/core/_observe.py` 只把 model 写进 span 属性 `gen_ai.request.model`）。因此：

- 样本带 `model="x"` 标签 → 直接按标签分组；
- 全部无标签 → 用 `--model` 把整组归到该模型；不给 `--model` 就输出
  `cost: null, reason: "model_unattributed"`，只报 token 数，**不猜是哪个模型**。

**单价**：`config/llm_prices.json` 里同一 model 可有多条记录，按 `effective_from`
取「不晚于 `--as-of` 的最新一条」；表里没有该 model → `cost: null, reason: "price_missing"`
（只给 token 数）；有记录但都晚于统计日期 → `reason: "price_not_effective_yet"`。

**版本与不漂移**：每条成本记录带 `price_version`（价目表 version 字段，缺省用文件内容
sha256 前 12 位）+ `effective_from`，历史报告在改价后仍能复算。

**不跨币种求和**：按 model 分组；出现多个币种时 `mixed_currency: true` 且 `total_cost: null`，
只给分组明细。

容错（演示时不能因为「还没流量」就报错）：服务没起 / 端口不通 / 文件不存在 / 文本里
没有这些计数器 → 输出空明细并 `exit 0`，原因写 stderr；单行值不是数字 → 跳过该行。

仅用标准库。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_METRICS_URL = "http://127.0.0.1:8000/metrics"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_PRICES_PATH = Path(__file__).resolve().parents[1] / "config" / "llm_prices.json"

PROMPT_COUNTER = "llm_prompt_tokens_total"
COMPLETION_COUNTER = "llm_completion_tokens_total"
CACHE_READ_COUNTER = "llm_cache_read_tokens_total"
TOKEN_COUNTERS = (PROMPT_COUNTER, COMPLETION_COUNTER, CACHE_READ_COUNTER)

REASON_PRICE_MISSING = "price_missing"
REASON_PRICE_NOT_EFFECTIVE = "price_not_effective_yet"
REASON_MODEL_UNATTRIBUTED = "model_unattributed"

#: 价目表没写 version 时用内容 sha256 前 N 位兜底（改价后版本自然变，历史报告不漂移）。
PRICE_TABLE_VERSION_FALLBACK_LEN = 12

CSV_COLUMNS = (
    "model",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "currency",
    "input_per_1m",
    "output_per_1m",
    "cache_read_per_1m",
    "input_cost",
    "output_cost",
    "cache_cost",
    "cost",
    "effective_from",
    "price_version",
    "reason",
)


class PriceTableError(ValueError):
    """价目表是数据文件，坏了要说清坏在哪一行，不能静默按 0 处理。"""


@dataclass(frozen=True, slots=True)
class PriceRecord:
    """一条价目：某个 model 从某天起生效的一组单价（每 100 万 token）。"""

    model: str
    currency: str
    input_per_1m: Decimal
    output_per_1m: Decimal
    cache_read_per_1m: Decimal | None
    effective_from: date
    source: str


@dataclass(frozen=True, slots=True)
class PriceTable:
    """整张价目表 + 它的版本串（改价后版本必须变，历史报告才不漂移）。"""

    records: dict[str, tuple[PriceRecord, ...]]
    version: str
    path: Path


@dataclass(slots=True)
class TokenSample:
    """一台实例上某个 model 的累计 token（计数器是累计型，多标签按 model 汇总）。"""

    model: str | None
    prompt_tokens: float = 0.0
    completion_tokens: float = 0.0
    cache_read_tokens: float | None = None


def _parse_decimal(value: object, *, where: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PriceTableError(f"{where} 不是数字：{value!r}") from exc
    if number < 0:
        raise PriceTableError(f"{where} 不能是负数：{value!r}")
    return number


def _parse_price_record(model: str, raw: object, *, where: str) -> PriceRecord:
    if not isinstance(raw, dict):
        raise PriceTableError(f"{where} 必须是对象，实际是 {type(raw).__name__}")
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        raise PriceTableError(f"{where}.currency 必填（如 CNY / USD）")
    effective = raw.get("effective_from")
    if not isinstance(effective, str):
        raise PriceTableError(f"{where}.effective_from 必填（YYYY-MM-DD）")
    try:
        effective_from = date.fromisoformat(effective)
    except ValueError as exc:
        raise PriceTableError(f"{where}.effective_from 不是合法日期：{effective!r}") from exc
    cache_raw = raw.get("cache_read_per_1m")
    cache = (
        None if cache_raw is None else _parse_decimal(cache_raw, where=f"{where}.cache_read_per_1m")
    )
    return PriceRecord(
        model=model,
        currency=currency.strip().upper(),
        input_per_1m=_parse_decimal(raw.get("input_per_1m"), where=f"{where}.input_per_1m"),
        output_per_1m=_parse_decimal(raw.get("output_per_1m"), where=f"{where}.output_per_1m"),
        cache_read_per_1m=cache,
        effective_from=effective_from,
        source=str(raw.get("source") or ""),
    )


def load_price_table(path: Path) -> PriceTable:
    """读价目表；顶层可以是 `{"models": {...}}`，也可以直接是 `{"<model>": {...}}`。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PriceTableError(f"读不到价目表 {path}：{type(exc).__name__}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PriceTableError(f"价目表 {path} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise PriceTableError(f"价目表 {path} 顶层必须是对象")

    version = payload.get("version")
    if isinstance(version, str) and version.strip():
        price_version = version.strip()
    else:
        price_version = hashlib.sha256(text.encode("utf-8")).hexdigest()[
            :PRICE_TABLE_VERSION_FALLBACK_LEN
        ]

    raw_models = payload.get("models")
    if raw_models is None:
        raw_models = {
            key: value for key, value in payload.items() if key not in {"version", "source", "note"}
        }
    if not isinstance(raw_models, dict) or not raw_models:
        raise PriceTableError(f"价目表 {path} 里没有任何 model 条目")

    records: dict[str, tuple[PriceRecord, ...]] = {}
    for model, raw in raw_models.items():
        if not isinstance(model, str) or not model.strip():
            raise PriceTableError(f"价目表 {path} 里出现非法 model 键：{model!r}")
        entries = raw if isinstance(raw, list) else [raw]
        parsed = tuple(
            _parse_price_record(model, entry, where=f"{path}:{model}[{index}]")
            for index, entry in enumerate(entries)
        )
        records[model] = tuple(sorted(parsed, key=lambda record: record.effective_from))
    return PriceTable(records=records, version=price_version, path=path)


def select_price(
    table: PriceTable, model: str, as_of: date
) -> tuple[PriceRecord | None, str | None]:
    """取「不晚于 as_of 的最新一条」；取不到时返回原因，由调用方决定怎么呈现。"""
    entries = table.records.get(model)
    if not entries:
        return None, REASON_PRICE_MISSING
    candidates = [record for record in entries if record.effective_from <= as_of]
    if not candidates:
        return None, REASON_PRICE_NOT_EFFECTIVE
    return candidates[-1], None


def _parse_labels(labels_text: str) -> dict[str, str] | None:
    """解析 `a="x",b="y,z"`：标签值里的逗号、等号都在引号内，不能按逗号裸切。"""
    labels: dict[str, str] = {}
    text = labels_text.strip()
    index = 0
    length = len(text)
    while index < length:
        equals = text.find("=", index)
        if equals < 0:
            return None
        key = text[index:equals].strip().strip(",")
        if not key:
            return None
        cursor = equals + 1
        while cursor < length and text[cursor] == " ":
            cursor += 1
        if cursor < length and text[cursor] == '"':
            cursor += 1
            value_chars: list[str] = []
            while cursor < length:
                character = text[cursor]
                if character == "\\" and cursor + 1 < length:
                    value_chars.append(text[cursor + 1])
                    cursor += 2
                    continue
                if character == '"':
                    cursor += 1
                    break
                value_chars.append(character)
                cursor += 1
            labels[key] = "".join(value_chars)
        else:
            end = text.find(",", cursor)
            if end < 0:
                end = length
            labels[key] = text[cursor:end].strip()
            cursor = end
        index = cursor
        while index < length and text[index] in " ,":
            index += 1
    return labels


def _split_sample(line: str) -> tuple[str, dict[str, str], str] | None:
    """把 `name{labels} value` / `name value` 拆成 (name, labels, value)。"""
    if "{" in line:
        open_index = line.index("{")
        close_index = line.rfind("}")
        if close_index < open_index:
            return None
        name = line[:open_index].strip()
        labels_text = line[open_index + 1 : close_index]
        rest = line[close_index + 1 :].strip()
        labels = _parse_labels(labels_text)
        if labels is None:
            return None
        return name, labels, rest

    parts = line.split()
    if len(parts) != 2:
        return None
    return parts[0], {}, parts[1]


def parse_token_samples(text: str) -> list[TokenSample]:
    """从 Prometheus 文本汇总各 model 的 token 累计值；同名多标签样本按 model 相加。"""
    samples: dict[str | None, TokenSample] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        split = _split_sample(line)
        if split is None:
            continue
        name, labels, value_text = split
        if name not in TOKEN_COUNTERS:
            continue
        try:
            value = float(value_text.split()[0])
        except (ValueError, IndexError):
            print(f"[warn] 跳过无法解析的样本行：{line[:120]}", file=sys.stderr)
            continue
        if value != value or value in (float("inf"), float("-inf")):  # NaN / ±Inf
            continue
        model = labels.get("model") or None
        sample = samples.get(model)
        if sample is None:
            sample = TokenSample(model=model)
            samples[model] = sample
        if name == PROMPT_COUNTER:
            sample.prompt_tokens += value
        elif name == COMPLETION_COUNTER:
            sample.completion_tokens += value
        else:
            sample.cache_read_tokens = (sample.cache_read_tokens or 0.0) + value
    return list(samples.values())


def _money(value: Decimal) -> float:
    """金额统一保留 6 位小数，避免 JSON 里出现 0.30000000000000004。"""
    return float(round(value, 6))


def price_sample(
    sample: TokenSample,
    table: PriceTable,
    *,
    as_of: date,
    model_hint: str | None = None,
) -> dict[str, object]:
    """给一条 token 样本算成本；表里没有就不给价，只回 token 数。"""
    model = sample.model or model_hint
    row: dict[str, object] = {
        "model": model,
        "prompt_tokens": int(sample.prompt_tokens),
        "completion_tokens": int(sample.completion_tokens),
        "cache_read_tokens": (
            None if sample.cache_read_tokens is None else int(sample.cache_read_tokens)
        ),
        "currency": None,
        "input_per_1m": None,
        "output_per_1m": None,
        "cache_read_per_1m": None,
        "input_cost": None,
        "output_cost": None,
        "cache_cost": None,
        "cost": None,
        "effective_from": None,
        "price_version": table.version,
        "reason": None,
    }
    if model is None:
        row["reason"] = REASON_MODEL_UNATTRIBUTED
        return row

    record, reason = select_price(table, model, as_of)
    if record is None:
        row["reason"] = reason
        return row

    million = Decimal(1_000_000)
    input_cost = Decimal(int(sample.prompt_tokens)) / million * record.input_per_1m
    output_cost = Decimal(int(sample.completion_tokens)) / million * record.output_per_1m
    cache_cost: float | None = None
    if sample.cache_read_tokens is not None and record.cache_read_per_1m is not None:
        cache_cost = _money(
            Decimal(int(sample.cache_read_tokens)) / million * record.cache_read_per_1m
        )

    row.update(
        {
            "currency": record.currency,
            "input_per_1m": float(record.input_per_1m),
            "output_per_1m": float(record.output_per_1m),
            "cache_read_per_1m": (
                None if record.cache_read_per_1m is None else float(record.cache_read_per_1m)
            ),
            "input_cost": _money(input_cost),
            "output_cost": _money(output_cost),
            "cache_cost": cache_cost,
            "cost": _money(input_cost + output_cost + Decimal(str(cache_cost or 0))),
            "effective_from": record.effective_from.isoformat(),
        }
    )
    return row


def build_report(
    text: str,
    *,
    source: str,
    table: PriceTable,
    as_of: date,
    model_hint: str | None = None,
) -> dict[str, object]:
    """组装最终报告：按 model 分组的明细 + 币种检查后的总额。"""
    rows = [
        price_sample(sample, table, as_of=as_of, model_hint=model_hint)
        for sample in parse_token_samples(text)
    ]
    rows.sort(key=lambda row: str(row["model"] or ""))

    priced = [row for row in rows if row["cost"] is not None]
    currencies = sorted({str(row["currency"]) for row in priced})
    mixed_currency = len(currencies) > 1
    total_cost: float | None = None
    if priced and not mixed_currency:
        total_cost = _money(sum(Decimal(str(row["cost"])) for row in priced))

    report: dict[str, object] = {
        "source": source,
        "price_table": str(table.path),
        "price_version": table.version,
        "as_of": as_of.isoformat(),
        "currency": None if mixed_currency else (currencies[0] if currencies else None),
        "mixed_currency": mixed_currency,
        "currencies": currencies,
        "total_cost": total_cost,
        "models": rows,
    }
    if mixed_currency:
        report["note"] = "出现多个币种，按 FR-TC-08 口径不给总额，只给分组明细"
    return report


def format_csv(report: dict[str, object]) -> str:
    """把同一份报告摊平成 CSV（金额列缺省为空串，别写成 0）。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in report["models"]:  # type: ignore[union-attr]
        writer.writerow(
            {key: ("" if row.get(key) is None else row.get(key)) for key in CSV_COLUMNS}
        )
    return buffer.getvalue()


def fetch_metrics_text(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """抓 `/metrics`；失败只 warn 并按空文本处理（演示现场不能因为服务没起就崩）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - 本机端点
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[warn] 读取 {url} 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return ""


def read_metrics(
    args: argparse.Namespace, *, metrics_file: Path | None, metrics_url: str
) -> tuple[str, str]:
    """返回 (Prometheus 文本, 数据来源字符串)。"""
    if metrics_file is not None:
        try:
            return (
                metrics_file.read_text(encoding="utf-8", errors="replace"),
                str(metrics_file),
            )
        except OSError as exc:
            print(
                f"[warn] 读取 {metrics_file} 失败：{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return "", str(metrics_file)
    text = fetch_metrics_text(metrics_url)
    return text, metrics_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 /metrics 文本 × 价目表估算 LLM 成本（单价是数据，不写死在代码里）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  uv run python scripts/metrics_cost.py\n"
            "  uv run python scripts/metrics_cost.py --format csv\n"
            "  uv run python scripts/metrics_cost.py --metrics-file build/metrics.txt\n"
            "  uv run python scripts/metrics_cost.py --model deepseek-v4.1-flash --as-of 2026-11-02\n"
            "\n"
            "价目表默认 config/llm_prices.json；model 不在表里时只报 token 数、不给价。\n"
            "没有流量 / 服务没起 / 文件不存在时输出空明细并 exit 0。"
        ),
    )
    parser.add_argument(
        "--metrics-url",
        default=DEFAULT_METRICS_URL,
        help=f"抓取的 /metrics 地址（默认 {DEFAULT_METRICS_URL}）",
    )
    parser.add_argument(
        "--metrics-file",
        type=Path,
        default=None,
        help="改为读取本地保存的 Prometheus 文本（测试/离线复算用）",
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=DEFAULT_PRICES_PATH,
        help=f"价目表路径（默认 {DEFAULT_PRICES_PATH}）",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="统计日期 YYYY-MM-DD（决定取哪一版价目；默认今天）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="计数器没有 model 标签时，把整组 token 归到该模型",
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="输出格式（默认 json）",
    )
    # 旧参数名保留为别名，README 里的老命令仍能跑；单价参数已删除（单价是数据，见价目表）。
    parser.add_argument("--url", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--file", dest="legacy_file", type=Path, default=None, help=argparse.SUPPRESS
    )
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prompt-price", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--completion-price", type=float, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.prompt_price is not None or args.completion_price is not None:
        print(
            "[warn] --prompt-price / --completion-price 已废弃并被忽略："
            "单价是数据，请看 config/llm_prices.json",
            file=sys.stderr,
        )
    try:
        table = load_price_table(args.prices)
    except PriceTableError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    if args.as_of is None:
        as_of = date.today()
    else:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"[error] --as-of 不是合法日期：{args.as_of!r}", file=sys.stderr)
            return 2

    metrics_file = args.metrics_file if args.metrics_file is not None else args.legacy_file
    metrics_url = args.url or args.metrics_url
    text, source = read_metrics(args, metrics_file=metrics_file, metrics_url=metrics_url)
    report = build_report(
        text,
        source=source,
        table=table,
        as_of=as_of,
        model_hint=args.model,
    )
    if args.format == "csv":
        sys.stdout.write(format_csv(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
