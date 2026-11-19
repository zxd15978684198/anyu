"""spike: S1 组 A —— OTel SDK 接入前的四项本地验证。

背景：设计记录（可观测性接入） v1.3 §6 的 S1。
本脚本只回答四个问题，**不联网、不读 .env、不碰 app/**：

    A1 依赖   sdk / otlp-proto-http / prometheus exporter 能否同时 import，版本是否自洽。
    A2 零开销 otel_enabled=False 时不建 provider 就没有任何后台线程（这是「关闭零开销」的验收底）。
    A3 父子链 start_as_current_span 嵌套能推出 parentSpanId，且出栈后 current 为空。
    A4 指标文本 prometheus_client 能产文本；装了 otel Prometheus reader 则额外验指标名归一化。

    C1 Langfuse 真连  用 .env 里的 LANGFUSE_PUBLIC_KEY/SECRET_KEY 经 OTLP/HTTP 发一条 span。
    C2 Grafana 真连   用 GRAFANA_OTLP_ENDPOINT/INSTANCE_ID/TOKEN（S12 前走环境变量）发一次指标。

跑法（仓库根目录）：
    uv run python spikes/otel_smoke.py                     # 全部
    uv run python spikes/otel_smoke.py --group a2,a3       # 只跑指定项
    uv run --with opentelemetry-exporter-prometheus python spikes/otel_smoke.py

退出码：0 = 全绿（SKIP 不算失败）；1 = 有 FAIL。
产物：无（只打印）。不写文件，避免把临时输出带进提交。
"""

from __future__ import annotations

import argparse
import base64
import importlib.metadata as md
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

# 脚本直接跑时 sys.path[0] 是 spikes/，把仓库根补进来才能 import app.*
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_TOTAL_TIMEOUT_S = 30.0


@dataclass
class Result:
    cid: str
    status: str
    detail: str
    extra: dict = field(default_factory=dict)


def _ver(dist: str) -> str:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return "-"


def _thread_names() -> set[str]:
    return {t.name for t in threading.enumerate()}


def _has_batch_thread(names: set[str]) -> bool:
    return any("batch" in n.lower() or "spanprocessor" in n.lower() for n in names)


def _wait_thread_gone(names_before: set[str], timeout: float = 5.0) -> set[str]:
    """等后台线程退出，返回残留线程名。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        leftover = _thread_names() - names_before
        if not _has_batch_thread(leftover):
            return set()
        time.sleep(0.05)
    return {n for n in _thread_names() - names_before if _has_batch_thread({n})}


# --------------------------------------------------------------------- A1


def check_a1() -> Result:
    mods = [
        "opentelemetry",
        "opentelemetry.sdk",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.metrics",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.exporter.prometheus",
        "prometheus_client",
    ]
    import importlib.util

    missing = [m for m in mods if importlib.util.find_spec(m) is None]
    vers = {
        "sdk": _ver("opentelemetry-sdk"),
        "otlp-http": _ver("opentelemetry-exporter-otlp-proto-http"),
        "otlp-grpc": _ver("opentelemetry-exporter-otlp-proto-grpc"),
        "prom-reader": _ver("opentelemetry-exporter-prometheus"),
        "prometheus-client": _ver("prometheus-client"),
    }
    extra = {"versions": vers, "missing": missing}
    if missing:
        # prometheus exporter 允许缺：组 A 的 A4 会退化成只验 prometheus_client
        hard = [m for m in missing if m != "opentelemetry.exporter.prometheus"]
        status = FAIL if hard else SKIP
        return Result("A1", status, f"缺模块: {', '.join(missing)}", extra)
    return Result("A1", PASS, "全部可 import；" + json.dumps(vers, ensure_ascii=False), extra)


# ---------------------------------------------------------------- A2 零开销


def check_a2() -> Result:
    """不建 provider / 建但无 processor / 有 processor 三种形态的线程数对比。"""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    base = _thread_names()

    # 形态一：不建 provider（otel_enabled=False 的真实形态）
    p_none = TracerProvider()
    p_none.shutdown()
    after_none = _thread_names() - base

    # 形态二：只建 provider，不挂 exporter
    p_idle = TracerProvider()
    after_idle = _thread_names() - base
    p_idle.shutdown()

    # 形态三：挂 SimpleSpanProcessor（同步导出，无后台线程）
    p_sync = TracerProvider()
    p_sync.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    after_sync = _thread_names() - base
    p_sync.shutdown()

    leftover = _wait_thread_gone(base)
    shapes = {"none": after_none, "idle": after_idle, "sync": after_sync}
    bad = {k: sorted(v) for k, v in shapes.items() if v}
    extra = {"new_threads": {k: sorted(v) for k, v in shapes.items()}, "leftover": sorted(leftover)}
    if bad or leftover:
        return Result("A2", FAIL, f"出现线程: {json.dumps(bad, ensure_ascii=False)} leftover={sorted(leftover)}", extra)
    return Result("A2", PASS, "不建 provider / 不挂 exporter / 同步 processor 均零后台线程", extra)


# ---------------------------------------------------------------- A3 父子链


def check_a3() -> Result:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    tracer = provider.get_tracer("spike.otel_smoke")

    with tracer.start_as_current_span("turn") as turn:
        turn_ctx = turn.get_span_context()
        with tracer.start_as_current_span("agent") as agent:
            agent_ctx = agent.get_span_context()
            assert otel_trace.get_current_span() is agent, "current span 不是 agent"
    assert otel_trace.get_current_span() is otel_trace.INVALID_SPAN, "出栈后 current 未复位"

    spans = {s.name: s for s in exporter.get_finished_spans()}
    provider.shutdown()
    if set(spans) != {"turn", "agent"}:
        return Result("A3", FAIL, f"span 集合异常: {sorted(spans)}")
    agent, turn = spans["agent"], spans["turn"]
    ok = agent.parent is not None and agent.parent.span_id == turn.context.span_id
    same_trace = agent.context.trace_id == turn.context.trace_id == turn_ctx.trace_id
    extra = {"trace_id": format(turn_ctx.trace_id, "032x"), "turn_span_id": format(turn_ctx.span_id, "016x"), "agent_span_id": format(agent_ctx.span_id, "016x"), "parent_ok": ok, "same_trace": same_trace}
    if not ok or not same_trace:
        return Result("A3", FAIL, "父子关系或 trace_id 不一致", extra)
    return Result("A3", PASS, "agent.parentSpanId == turn.spanId，trace_id 一致，出栈复位", extra)


# ---------------------------------------------------------------- A4 指标文本


def check_a4() -> Result:
    from prometheus_client import CollectorRegistry, Counter, generate_latest

    reg = CollectorRegistry()
    Counter("route_s1_closed_total", "S1 closure", registry=reg).inc(3)
    text = generate_latest(reg).decode("utf-8", "replace")
    has_line = "route_s1_closed_total" in text

    extra: dict = {"prometheus_client_text": has_line, "sample": [ln for ln in text.splitlines() if "route_s1_closed" in ln][:3]}

    # 若装了 otel 的 Prometheus reader，再验一遍归一化命名
    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import MeterProvider

        reader = PrometheusMetricReader()
        mp = MeterProvider(metric_readers=[reader])
        meter = mp.get_meter("spike.otel_smoke")
        meter.create_counter("route_s1_closed", description="closure").add(2)
        meter.create_histogram("agent_run_ms", unit="ms").record(12.5)
        otel_text = generate_latest().decode("utf-8", "replace")
        names = sorted({ln.split("{")[0].split(" ")[0] for ln in otel_text.splitlines() if ln and not ln.startswith("#")})
        mp.shutdown()
        extra["otel_names"] = names[:20]
        extra["otel_has_s1"] = "route_s1_closed_total" in otel_text
        extra["otel_has_ms"] = "agent_run_ms_milliseconds" in otel_text
        if not extra["otel_has_s1"]:
            return Result("A4", FAIL, "OTel 导出后找不到 route_s1_closed_total（命名映射需确认）", extra)
    except Exception as exc:  # 缺 reader 属预期
        extra["otel_reader"] = f"SKIP: {type(exc).__name__}: {exc}"

    if not has_line:
        return Result("A4", FAIL, "prometheus_client 文本里没有指标行", extra)
    return Result("A4", PASS, "prometheus_client 文本可产；" + ("OTel reader 命名已验" if "otel_names" in extra else "OTel reader 未装，仅验客户端文本"), extra)


# ---------------------------------------------------------------- C1 Langfuse


def _basic_auth(user: str, secret: str) -> dict[str, str]:
    raw = f"{user}:{secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def check_c1() -> Result:
    from app.core.config import Settings

    s = Settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return Result("C1", SKIP, "缺 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY", {})

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider

    url = s.langfuse_host.rstrip("/") + "/api/public/otel/v1/traces"
    exporter = OTLPSpanExporter(endpoint=url, headers=_basic_auth(s.langfuse_public_key, s.langfuse_secret_key), timeout=15)
    provider = TracerProvider()
    tracer = provider.get_tracer("spike.langfuse_check")
    span = tracer.start_span("spike.langfuse_check")
    span.set_attribute("spike.group", "c1")
    span.end()
    try:
        res = exporter.export([span])
    finally:
        provider.shutdown()
    extra = {"endpoint": url, "result": res.name}
    if res.name != "SUCCESS":
        return Result("C1", FAIL, f"Langfuse 导出失败: {res.name}", extra)
    return Result("C1", PASS, "Langfuse OTLP/HTTP 收到一条 span", extra)


# ---------------------------------------------------------------- C2 Grafana


def check_c2() -> Result:
    """Grafana Cloud：优先用官方 tile 给的标准环境变量，兼容旧的 GRAFANA_* 三项。"""
    ep = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or os.environ.get("GRAFANA_OTLP_ENDPOINT", "").strip()
    headers: dict[str, str] = {}
    raw_headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if raw_headers:
        for part in raw_headers.split(","):
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            # 官方脚本里 Python 场景把空格写成 %20，这里统一解回来
            headers[key.strip()] = urllib.parse.unquote(val.strip())
    else:
        iid = os.environ.get("GRAFANA_OTLP_INSTANCE_ID", "").strip()
        tok = os.environ.get("GRAFANA_OTLP_TOKEN", "").strip()
        if iid and tok:
            headers = _basic_auth(iid, tok)
    if not (ep and headers):
        return Result(
            "C2",
            SKIP,
            "缺 OTEL_EXPORTER_OTLP_ENDPOINT + OTEL_EXPORTER_OTLP_HEADERS（Grafana 的 OpenTelemetry tile 会直接给这两行）；S12 前用环境变量",
            {},
        )

    import logging

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    errors: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = record.getMessage()
            if record.levelno >= logging.WARNING and ("export" in msg.lower() or "otlp" in msg.lower()):
                errors.append(msg)

    handler = _Collect()
    otel_log = logging.getLogger("opentelemetry")
    otel_log.addHandler(handler)
    otel_log.setLevel(logging.WARNING)

    url = ep.rstrip("/") + "/v1/metrics"
    exporter = OTLPMetricExporter(endpoint=url, headers=headers, timeout=15)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
    mp = MeterProvider(metric_readers=[reader])
    mp.get_meter("spike.grafana_check").create_counter("spike_checks_total").add(1)
    try:
        mp.force_flush(timeout_millis=20_000)
    finally:
        mp.shutdown()
        otel_log.removeHandler(handler)

    extra = {"endpoint": url, "errors": errors[:3]}
    if errors:
        return Result("C2", FAIL, "Grafana 导出报错（见 extra）", extra)
    return Result("C2", PASS, "Grafana OTLP/HTTP 未报错（以日志无 FAILED 为准）", extra)


CHECKS = {"a1": check_a1, "a2": check_a2, "a3": check_a3, "a4": check_a4, "c1": check_c1, "c2": check_c2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S1 组 A：OTel 本地 smoke")
    parser.add_argument("--group", default="a1,a2,a3,a4", help="逗号分隔，如 a1,a3")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    wanted = [g.strip().lower() for g in args.group.split(",") if g.strip()]
    unknown = [g for g in wanted if g not in CHECKS]
    if unknown:
        print(f"未知项: {unknown}，可选 {sorted(CHECKS)}", file=sys.stderr)
        return 2

    started = time.monotonic()
    results: list[Result] = []
    for gid in wanted:
        try:
            results.append(CHECKS[gid]())
        except Exception as exc:  # spike 要把异常留在结果里
            results.append(Result(gid.upper(), FAIL, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"))

    elapsed = time.monotonic() - started
    if args.json:
        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            print(f"[{r.status}] {r.cid}  {r.detail}")
            if r.extra and r.status != PASS:
                print(json.dumps(r.extra, ensure_ascii=False, indent=2, default=str))
        print(f"-- {len(results)} 项，耗时 {elapsed:.2f}s")

    failed = [r for r in results if r.status == FAIL]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
