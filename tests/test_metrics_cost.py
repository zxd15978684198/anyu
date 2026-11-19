"""`scripts/metrics_cost.py` 的验收：读数 × 查价，**单价来自价目表，不写死在代码里**。

只喂文本与 JSON，不连服务：脚本的价值全在「把 /metrics 文本按 model 归并、再去查价目录
算出钱」这一段。网络读取失败按空文本处理的分支单独用「文件不存在」用例覆盖。

用例对齐 FR-TC-08 的交付口径：
1. 表内 model 算对（手算数字写在断言注释里）；
2. 表外 model → `cost: null` + `reason: "price_missing"`，token 数照常给；
3. 同一 model 两条价目 → 按 `effective_from` 取「不晚于统计日期的最新一条」；
4. 跨币种不求和（`mixed_currency: true`、`total_cost: null`）；
5. `/metrics` 里没有某个 model 就不出现在明细里，不报错；
6. 缓存单价为 null → `cache_cost: null`（不拿 0 冒充）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from scripts.metrics_cost import (
    CACHE_READ_COUNTER,
    COMPLETION_COUNTER,
    PRICE_TABLE_VERSION_FALLBACK_LEN,
    PROMPT_COUNTER,
    REASON_MODEL_UNATTRIBUTED,
    REASON_PRICE_MISSING,
    REASON_PRICE_NOT_EFFECTIVE,
    PriceTableError,
    build_report,
    format_csv,
    load_price_table,
    main,
    parse_token_samples,
    select_price,
)


@pytest.fixture()
def workdir() -> Iterator[Path]:
    """临时目录：`pytest` 的 `tmp_path` 用 `mkdir(mode=0o700)`，本机沙箱会 PermissionError，
    这里改用「系统 temp 下默认权限的独立目录」，CI 与本机都能跑。"""
    root = Path(tempfile.gettempdir()) / f"metrics-cost-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


TABLE = {
    "version": "2026-11-13.1",
    "models": {
        "deepseek-v4.1-flash": [
            {
                "currency": "CNY",
                "input_per_1m": 1.0,
                "output_per_1m": 4.0,
                "cache_read_per_1m": 0.1,
                "effective_from": "2026-06-01",
                "source": "旧价",
            },
            {
                "currency": "CNY",
                "input_per_1m": 2.0,
                "output_per_1m": 8.0,
                "cache_read_per_1m": 0.2,
                "effective_from": "2026-11-01",
                "source": "现价",
            },
        ],
        "qwen-plus": {
            "currency": "CNY",
            "input_per_1m": 0.8,
            "output_per_1m": 2.0,
            "cache_read_per_1m": None,
            "effective_from": "2026-03-01",
            "source": "单一价目",
        },
    },
}

SAMPLE = f"""\
# HELP {PROMPT_COUNTER} LLM prompt tokens
# TYPE {PROMPT_COUNTER} counter
{PROMPT_COUNTER}{{model="deepseek-v4.1-flash",layer="L2"}} 1000000.0
{PROMPT_COUNTER}{{model="deepseek-v4.1-flash",layer="L0"}} 500000.0
{PROMPT_COUNTER}{{model="qwen-plus"}} 2000000.0
{PROMPT_COUNTER}_created 1731000000.0
# TYPE {COMPLETION_COUNTER} counter
{COMPLETION_COUNTER}{{model="deepseek-v4.1-flash"}} 250000.0
"""


def _write_table(workdir: Path, payload: object = TABLE) -> Path:
    path = workdir / "llm_prices.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _rows(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(row["model"]): row for row in report["models"]}  # type: ignore[union-attr]


def test_priced_model_cost_matches_hand_calculation(workdir: Path) -> None:
    """手算（deepseek-v4.1-flash，2026-11-01 起价：输入 2 元/百万、输出 8 元/百万）：

    prompt 1,000,000 + 500,000 = 1,500,000 token × 2 / 1,000,000 = **3.0 元**
    completion 250,000 token × 8 / 1,000,000                     = **2.0 元**
    合计                                                          = **5.0 元**
    """
    table = load_price_table(_write_table(workdir))
    report = build_report(SAMPLE, source="metrics.txt", table=table, as_of=date(2026, 11, 13))
    row = _rows(report)["deepseek-v4.1-flash"]

    assert row["prompt_tokens"] == 1_500_000
    assert row["completion_tokens"] == 250_000
    assert row["input_cost"] == pytest.approx(3.0)
    assert row["output_cost"] == pytest.approx(2.0)
    assert row["cost"] == pytest.approx(5.0)
    assert row["currency"] == "CNY"
    assert report["total_cost"] == pytest.approx(6.6)  # 5.0 + qwen 2M×0.8/1M = 1.6


def test_model_missing_from_price_table_reports_tokens_only(workdir: Path) -> None:
    """表里没有的 model 只报 token 数，**绝不猜价**（FR-TC-08 明文）。"""
    text = f'{PROMPT_COUNTER}{{model="mystery-model"}} 1000000.0\n'
    table = load_price_table(_write_table(workdir))
    report = build_report(text, source="metrics.txt", table=table, as_of=date(2026, 11, 13))
    row = _rows(report)["mystery-model"]

    assert row["prompt_tokens"] == 1_000_000
    assert row["cost"] is None
    assert row["reason"] == REASON_PRICE_MISSING
    assert row["currency"] is None
    assert report["total_cost"] is None


def test_price_selection_follows_effective_from(workdir: Path) -> None:
    """同一 model 两条价目：统计日期落在哪一段就用哪一段，且带出 effective_from 与版本。"""
    table = load_price_table(_write_table(workdir))

    old, old_reason = select_price(table, "deepseek-v4.1-flash", date(2026, 10, 31))
    new, new_reason = select_price(table, "deepseek-v4.1-flash", date(2026, 11, 1))

    assert old is not None and old.input_per_1m == 1.0 and old.effective_from == date(2026, 6, 1)
    assert old_reason is None
    assert new is not None and new.input_per_1m == 2.0 and new.effective_from == date(2026, 11, 1)
    assert new_reason is None
    assert table.version == "2026-11-13.1"


def test_report_uses_price_effective_at_as_of(workdir: Path) -> None:
    """同一份 `/metrics`，换统计日期就换价目版本——历史报告复算不漂移。"""
    table = load_price_table(_write_table(workdir))
    text = f'{PROMPT_COUNTER}{{model="deepseek-v4.1-flash"}} 1000000.0\n'

    before = _rows(build_report(text, source="m", table=table, as_of=date(2026, 10, 31)))[
        "deepseek-v4.1-flash"
    ]
    after = _rows(build_report(text, source="m", table=table, as_of=date(2026, 11, 2)))[
        "deepseek-v4.1-flash"
    ]

    assert before["effective_from"] == "2026-06-01"
    assert before["cost"] == pytest.approx(1.0)
    assert after["effective_from"] == "2026-11-01"
    assert after["cost"] == pytest.approx(2.0)
    assert before["price_version"] == after["price_version"] == "2026-11-13.1"


def test_price_not_effective_yet_is_distinct_from_missing(workdir: Path) -> None:
    """表里有该 model 但所有记录都晚于统计日期 → 单独原因，便于排查「是不是忘改日期」。"""
    table = load_price_table(_write_table(workdir))
    text = f'{PROMPT_COUNTER}{{model="deepseek-v4.1-flash"}} 1000.0\n'

    row = _rows(build_report(text, source="m", table=table, as_of=date(2026, 1, 1)))[
        "deepseek-v4.1-flash"
    ]

    assert row["cost"] is None
    assert row["reason"] == REASON_PRICE_NOT_EFFECTIVE


def test_mixed_currency_never_sums_across_currencies(workdir: Path) -> None:
    """两个 model 两种币种：给明细、给 mixed_currency，**不给总额**。"""
    payload = {
        "version": "t-mixed",
        "models": {
            "cny-model": {
                "currency": "CNY",
                "input_per_1m": 2.0,
                "output_per_1m": 8.0,
                "effective_from": "2026-01-01",
            },
            "usd-model": {
                "currency": "USD",
                "input_per_1m": 0.5,
                "output_per_1m": 1.5,
                "effective_from": "2026-01-01",
            },
        },
    }
    table = load_price_table(_write_table(workdir, payload))
    text = (
        f'{PROMPT_COUNTER}{{model="cny-model"}} 1000000.0\n'
        f'{PROMPT_COUNTER}{{model="usd-model"}} 1000000.0\n'
    )

    report = build_report(text, source="m", table=table, as_of=date(2026, 11, 13))

    assert report["mixed_currency"] is True
    assert report["total_cost"] is None
    assert report["currency"] is None
    assert report["currencies"] == ["CNY", "USD"]
    rows = _rows(report)
    assert rows["cny-model"]["cost"] == pytest.approx(2.0)
    assert rows["usd-model"]["cost"] == pytest.approx(0.5)


def test_model_absent_from_metrics_is_simply_omitted(workdir: Path) -> None:
    """`/metrics` 里没有的 model 不出现、也不报错（还没调用过就不该出现在成本表里）。"""
    table = load_price_table(_write_table(workdir))
    text = f'{PROMPT_COUNTER}{{model="qwen-plus"}} 1000.0\n'

    report = build_report(text, source="m", table=table, as_of=date(2026, 11, 13))

    assert set(_rows(report)) == {"qwen-plus"}
    assert report["total_cost"] == pytest.approx(0.0008)


def test_cache_cost_is_null_when_price_has_no_cache_rate(workdir: Path) -> None:
    """价目没给缓存单价（qwen-plus）时 cache_cost 为 null，**不拿 0 冒充已计价**。"""
    table = load_price_table(_write_table(workdir))
    text = (
        f'{PROMPT_COUNTER}{{model="qwen-plus"}} 1000000.0\n'
        f'{CACHE_READ_COUNTER}{{model="qwen-plus"}} 500000.0\n'
    )

    row = _rows(build_report(text, source="m", table=table, as_of=date(2026, 11, 13)))["qwen-plus"]

    assert row["cache_read_tokens"] == 500_000
    assert row["cache_read_per_1m"] is None
    assert row["cache_cost"] is None
    assert row["cost"] == pytest.approx(0.8)  # 只有输入成本


def test_cache_cost_uses_cache_rate_when_present(workdir: Path) -> None:
    """价目有缓存单价时按缓存单价算：500,000 × 0.2 / 1,000,000 = 0.1 元。"""
    table = load_price_table(_write_table(workdir))
    text = (
        f'{PROMPT_COUNTER}{{model="deepseek-v4.1-flash"}} 1000000.0\n'
        f'{CACHE_READ_COUNTER}{{model="deepseek-v4.1-flash"}} 500000.0\n'
    )

    row = _rows(build_report(text, source="m", table=table, as_of=date(2026, 11, 13)))[
        "deepseek-v4.1-flash"
    ]

    assert row["cache_cost"] == pytest.approx(0.1)
    assert row["cost"] == pytest.approx(2.1)


def test_unlabelled_samples_are_unattributed_without_model_hint(workdir: Path) -> None:
    """真实 `/metrics` 的 token 计数器没有 model 标签：不猜模型，只报 token 数。"""
    table = load_price_table(_write_table(workdir))
    text = f"{PROMPT_COUNTER} 1234.0\n{COMPLETION_COUNTER} 56.0\n"

    report = build_report(text, source="m", table=table, as_of=date(2026, 11, 13))
    row = _rows(report)["None"]

    assert row["prompt_tokens"] == 1234
    assert row["cost"] is None
    assert row["reason"] == REASON_MODEL_UNATTRIBUTED


def test_unlabelled_samples_can_be_attributed_by_model_hint(workdir: Path) -> None:
    """给了 `--model deepseek-v4.1-flash` 就把无标签样本归到它，成本才算得出来。"""
    table = load_price_table(_write_table(workdir))
    text = f"{PROMPT_COUNTER} 1000000.0\n"

    report = build_report(
        text,
        source="m",
        table=table,
        as_of=date(2026, 11, 13),
        model_hint="deepseek-v4.1-flash",
    )

    assert _rows(report)["deepseek-v4.1-flash"]["cost"] == pytest.approx(2.0)


def test_parse_handles_labels_with_commas_and_ignores_created_lines() -> None:
    """标签值里带逗号、以及 `_created` 行都不能把解析带偏。"""
    text = (
        f'{PROMPT_COUNTER}{{note="a,b",model="qwen-plus"}} 10.0\n'
        f"{PROMPT_COUNTER}_created 1731000000.0\n"
        f"{PROMPT_COUNTER} 5\n"
    )

    samples = parse_token_samples(text)
    by_model = {sample.model: sample for sample in samples}

    assert by_model["qwen-plus"].prompt_tokens == pytest.approx(10.0)
    assert by_model[None].prompt_tokens == pytest.approx(5.0)


def test_empty_metrics_text_yields_empty_report(workdir: Path) -> None:
    """没有流量（空文本）不是错误：明细为空、没有总额、exit 0。"""
    table = load_price_table(_write_table(workdir))

    report = build_report("", source="m", table=table, as_of=date(2026, 11, 13))

    assert report["models"] == []
    assert report["total_cost"] is None
    assert report["mixed_currency"] is False


def test_version_falls_back_to_content_hash_without_version_field(workdir: Path) -> None:
    """价目表没写 version 时用内容 hash 兜底，改价后版本自然变。"""
    payload = {"models": {"qwen-plus": dict(TABLE["models"]["qwen-plus"])}}
    table = load_price_table(_write_table(workdir, payload))

    assert len(table.version) == PRICE_TABLE_VERSION_FALLBACK_LEN
    assert all(character in "0123456789abcdef" for character in table.version)


def test_broken_price_table_fails_loudly(workdir: Path) -> None:
    """价目表坏了要报错退出，不能静默按 0 计价。"""
    bad = workdir / "bad.json"
    bad.write_text('{"models": {"x": {"currency": "CNY"}}}', encoding="utf-8")

    with pytest.raises(PriceTableError, match="effective_from"):
        load_price_table(bad)


def test_real_repo_price_table_is_usable() -> None:
    """仓库里那份价目表本身要能读，且至少覆盖两个模型（交付口径）。"""
    table = load_price_table(Path(__file__).resolve().parents[1] / "config" / "llm_prices.json")

    assert len(table.records) >= 2
    assert "deepseek-v4.1-flash" in table.records
    assert len(table.records["deepseek-v4.1-flash"]) >= 2  # 至少两条不同生效日


def test_main_reads_file_and_prints_json(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    metrics = workdir / "metrics.txt"
    metrics.write_text(SAMPLE, encoding="utf-8")

    assert main(["--metrics-file", str(metrics), "--prices", str(_write_table(workdir))]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["price_version"] == "2026-11-13.1"
    assert payload["total_cost"] == pytest.approx(6.6)


def test_main_csv_output_has_one_row_per_model(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    metrics = workdir / "metrics.txt"
    metrics.write_text(SAMPLE, encoding="utf-8")

    assert (
        main(
            [
                "--metrics-file",
                str(metrics),
                "--prices",
                str(_write_table(workdir)),
                "--format",
                "csv",
            ]
        )
        == 0
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0].split(",")[0] == "model"
    assert len(lines) == 3  # 表头 + 两个 model
    assert any(line.startswith("deepseek-v4.1-flash,") for line in lines[1:])


def test_main_missing_metrics_file_exits_zero_with_warning(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """演示时文件没准备好不能报错：输出空明细、exit 0，原因写 stderr。"""
    missing = workdir / "absent.txt"

    assert main(["--metrics-file", str(missing), "--prices", str(_write_table(workdir))]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["models"] == []
    assert "[warn]" in captured.err


def test_main_unreachable_url_exits_zero(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """端口不通走同一条容错分支（127.0.0.1:1 上不会有服务）。"""
    assert (
        main(
            ["--metrics-url", "http://127.0.0.1:1/metrics", "--prices", str(_write_table(workdir))]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out)["models"] == []
    assert "[warn]" in captured.err


def test_main_rejects_broken_price_table(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = workdir / "bad.json"
    bad.write_text('{"models": {}}', encoding="utf-8")

    assert main(["--prices", str(bad)]) == 2

    assert "[error]" in capsys.readouterr().err


def test_csv_escapes_none_as_empty_cell(workdir: Path) -> None:
    """CSV 里缺省值写空串，不能写 None / 0（否则下游会把「没价」当「零成本」）。"""
    table = load_price_table(_write_table(workdir))
    text = f'{PROMPT_COUNTER}{{model="mystery-model"}} 10.0\n'

    csv_text = format_csv(build_report(text, source="m", table=table, as_of=date(2026, 11, 13)))

    row = csv_text.splitlines()[1].split(",")
    column = dict(zip(csv_text.splitlines()[0].split(","), row, strict=True))
    assert column["cost"] == ""
    assert column["currency"] == ""
    assert column["reason"] == REASON_PRICE_MISSING
