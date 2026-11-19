"""进程内指标登记（迭代 4 T2 · 契约 C5 裁决 ⑤；迭代 5 S3 起双写 OTel Meter）。

对外仍是「打点 + 可读快照」两件事，但 S3 之后每次打点会**同时**写进
OTel `MeterProvider`（Prometheus reader），`/metrics` 因此能出标准 Prometheus 文本，
而 `metrics_snapshot()` / `derived_metrics()` 这些既有读法保留——它们读的是同一批
打点的**进程内镜像**，只服务于本仓的测试与 `/metrics` 的 JSON 兼容期，不参与抓取。

三条口径（照 C5 裁决 ⑤，别自己加）：

1. 打点只有三处：`identity_ask` 下发、`identity_confirm` 收到、`identity_result` 下发；
2. `settings.metrics_enabled=False` 时 **全部 no-op**，且**不建 key** ——
   关掉开关后快照必须是空的，否则「关掉还留一堆零计数」会被误读成有流量；
3. `metrics_snapshot()` 返回**只读副本**：调用方（未来的 `/metrics` 端点）能随手序列化
   或丢弃，改它不会污染内部状态。

为什么不用 Lock：计数器只被事件循环里的协程读写，`dict[name] = n + 1` 在单线程里
不会被别的协程插进来（没有 await 点），GIL 之外也不需要额外互斥。将来若真的有线程
写入，再补 Lock 也不迟 —— 现在加只会让人以为这里有多线程语义。

标签（label）顺序固定为**调用方传参顺序**，key 形如
`identity_result_sent{source=voice}`。同一 name 不同标签组合是不同 key，这是有意的：
排查时要能一眼看出 voice / confirm / generic 各走了多少次。

### S3 的双写分工（2026-11-13）

| 通道 | 谁读 | 用途 |
| --- | --- | --- |
| OTel Meter → Prometheus reader | `render_metrics()`、Grafana Cloud OTLP | 正式抓取面（文本 + 远端推送） |
| 进程内镜像（`_counters` / `_timings`） | `metrics_snapshot()`、`derived_metrics()` | 测试断言与兼容期读法；**打点仍只有一处** |

两条通道由同一次 `counter()` / `observe_ms()` 调用写入，不存在第二个打点入口。
`reset_metrics()` 会连同 meter 一起重建，避免用例之间残留（详情见该函数 docstring）。
"""

from __future__ import annotations

import logging
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import unquote

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from prometheus_client import CollectorRegistry, generate_latest

from app.core.config import get_settings

logger = logging.getLogger("app.metrics")

# 计数器：key = "name{k=v,k=v}"（无标签时就是 name），value = 累计次数
_counters: dict[str, int] = {}

# 耗时聚合：key = name，value = {count, sum_ms, max_ms}
# 平均值由 sum/count 现算；分位数需要样本，另存一份**有界**环形缓冲（见下）。
_timings: dict[str, dict[str, float]] = {}

# 分位数样本：key = name，value = 最近 N 个耗时（毫秒）。
# 为什么有界：单进程长跑时样本无界增长就是内存泄漏；2048 个 float 约 16KB/指标，
# 对 P50/P95 已经足够稳（迭代 5 §3.3 的最小改动口径），再大只是更贵不是更准。
_TIMING_SAMPLE_MAX: Final = 2048
_timing_samples: dict[str, deque[float]] = {}

_METRICS_NAME_MAX: Final = 64
_METRICS_LABEL_MAX: Final = 8

#: HTTP 请求量与耗时（PRD:457 FR-TC-08「请求量」的落点）。
#:
#: 为什么单独给一对函数而不是让调用方直接 `counter()`：请求量的标签是
#: `method/path/status` 三元组，路径必须用**路由模板**（`/elders/{elder_id}`）而不是
#: 真实路径，否则一个长者一个 key，标签基数会把内存吃光。模板只有路由层知道，
#: 所以写入点固定在 `TraceIdMiddleware` 收尾处（路由已匹配完），这里只提供读写口径。
HTTP_REQUESTS_COUNTER: Final = "http_requests_total"
HTTP_LATENCY: Final = "http_request_ms"

_HTTP_LABEL_RE: Final = re.compile(r"^http_requests_total\{(?P<labels>.*)\}$")


def _enabled() -> bool:
    """每次调用都现读配置：运维改完 `.env` 重启即生效，不需要重新 import 本模块。

    `get_settings()` 自带 lru_cache，这里不会每次都解析一遍环境变量。
    """
    return bool(get_settings().metrics_enabled)


def _format_key(name: str, labels: Mapping[str, object]) -> str:
    """name + 标签 → 稳定 key。标签按传入顺序，值统一 `str()`。

    为空标签时返回裸 name（不写 `name{}`）：日志与快照里少一层噪音。
    """
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in labels.items())
    return f"{name}{{{parts}}}"


def counter(name: str, *, value: int = 1, **labels: object) -> None:
    """累加一个计数点。`metrics_enabled=False` 时什么都不做。

    value 允许为 0 或负数（例如「净变化」类指标），但**负数累计结果不允许变成
    看似正常的正数** —— 这里不做 clamp，交给读的人判断：悄悄夹到 0 才是最难查的。
    """
    if not _enabled():
        return
    key = _format_key(name, labels)
    _counters[key] = _counters.get(key, 0) + int(value)
    _otel_add_counter(name, int(value), labels)


# ======================================================================
# OTel 双写（迭代 5 · S3）
# ======================================================================
#
# 打点仍只有 `counter()` / `observe_ms()` 两个入口；这里只是同一笔打点的第二个落点：
# 写进 OTel Meter，由 Prometheus reader 生成文本（`render_metrics()`），或由 OTLP
# 推到 Grafana Cloud。进程内 dict 继续服务 `metrics_snapshot()`（测试与兼容读法）。
#
# 为什么不把 dict 删掉改成「从 registry 反查」：
# 全仓约 20 个测试文件断言的是精确字典（`{"outbound_calls_total{result=ok}": 1}`、
# `timings == {}`、nearest-rank 的 p50_ms/p95_ms），registry 只能给近似分位与另一种
# 名字形状，改读法等于把 20 个文件一起重写，而收益只是少一个 dict。

#: 仪表缓存：`(kind, name)` → instrument。同一 name 的标签组合由 `add()` 的 attributes 承担。
_instruments: dict[tuple[str, str], Any] = {}

#: 进程内 Prometheus registry：`/metrics` 的文本就来自它，`reset_metrics()` 会整只换新的。
_prom_registry: CollectorRegistry | None = None
_meter_provider: MeterProvider | None = None
_meter: Meter | None = None

#: OTLP/HTTP 的指标信号路径。trace 走 `/v1/traces`（见 `observability._traces_url`），
#: 这里是同一个网关下的 metrics 端点，鉴权头也共用，不另配一套凭据。
_METRICS_PATH: Final = "/v1/metrics"
#: 推送周期。60s 与 demo 流量匹配：太密会烧 Grafana Cloud 的 series 额度，
#: 太稀则 5 分钟一次的用药扫描故障最晚要 10 分钟才在曲线上显形。
_METRIC_EXPORT_INTERVAL_MS: Final = 60_000
_METRIC_EXPORT_TIMEOUT_MS: Final = 10_000


def _metrics_url(endpoint: str) -> str:
    """`OTEL_EXPORTER_OTLP_ENDPOINT`（base）→ metrics 端点；已带路径就原样用。"""
    base = (endpoint or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith(_METRICS_PATH) else f"{base}{_METRICS_PATH}"


def _otlp_headers(raw: str) -> dict[str, str]:
    """解析 `key=value,key2=value2`，按 OTel 规范 percent-decode（Grafana 会带 `Basic%20`）。"""
    headers: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key, value = unquote(key.strip()), unquote(value.strip())
        if key and value:
            headers[key] = value
    return headers


def _otlp_metric_reader() -> PeriodicExportingMetricReader | None:
    """把指标**主动推**给 OTLP 后端（Grafana Cloud）。

    为什么非要有这一条：`PrometheusMetricReader` 只把累计值渲染成 `/metrics` 文本，
    没有人来抓就一条都出不去。云端要看到指标，只能由进程 push；本地 `/metrics` 与
    远端推送并存，两者读的是同一批 instrument，不存在两套打点口径。

    任何异常都返回 `None`（退化为「只有本地文本」），不让打点路径把业务带崩。
    """
    try:
        settings = get_settings()
        if not bool(getattr(settings, "otel_enabled", False)):
            return None
        # 测试环境不挂远端 reader：provider.shutdown() 会 flush，无网环境下每个用例都要
        # 白等 10s 超时；推送本身属于部署环境的事，集成环境验，不在单测里连外网。
        if str(getattr(settings, "app_env", "") or "") == "test":
            return None
        endpoint = _metrics_url(str(getattr(settings, "otel_exporter_otlp_endpoint", "") or ""))
        if not endpoint:
            return None
        headers = _otlp_headers(str(getattr(settings, "otel_exporter_otlp_headers", "") or ""))
        return PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=endpoint,
                headers=headers or None,
                timeout=_METRIC_EXPORT_TIMEOUT_MS / 1000,
            ),
            export_interval_millis=_METRIC_EXPORT_INTERVAL_MS,
        )
    except Exception as exc:  # pragma: no cover - 只在依赖损坏/配置异常时发生
        logger.warning(
            "otlp metric exporter unavailable; cloud metrics disabled",
            extra={"error_type": type(exc).__name__},
        )
        return None


def _ensure_meter() -> Meter | None:
    """懒建 Meter（仅一次）；任何异常都退化为 `None`，绝不让打点抛给业务。"""
    global _prom_registry, _meter_provider, _meter
    if _meter is not None:
        return _meter
    try:
        registry = CollectorRegistry()
        # 本地文本 reader 恒定在；远端推送按配置追加。缺 OTLP endpoint 或 otel_enabled=false
        # 时只剩本地 `/metrics`，与改动前的行为完全一致。
        readers: list[Any] = [PrometheusMetricReader(registry=registry)]
        otlp_reader = _otlp_metric_reader()
        if otlp_reader is not None:
            readers.append(otlp_reader)
        provider = MeterProvider(
            metric_readers=readers,
            resource=Resource.create(
                {
                    "service.name": str(
                        getattr(get_settings(), "otel_service_name", "") or "anyu-backend"
                    )
                }
            ),
        )
        _prom_registry = registry
        _meter_provider = provider
        _meter = provider.get_meter("anyu")
    except Exception as exc:  # pragma: no cover - 只可能在依赖损坏时发生
        logger.warning(
            "metrics meter unavailable; text export disabled",
            extra={"error_type": type(exc).__name__},
        )
        _meter = None
    return _meter


def _otel_attributes(labels: Mapping[str, object]) -> dict[str, str] | None:
    """OTel 属性只能是 str；空标签传 None（导出时就是不带标签的样本）。"""
    if not labels:
        return None
    return {str(k): str(v) for k, v in labels.items()}


def _otel_add_counter(name: str, value: int, labels: Mapping[str, object]) -> None:
    meter = _ensure_meter()
    if meter is None:
        return
    try:
        instrument = _instruments.get(("counter", name))
        if instrument is None:
            instrument = meter.create_counter(name)
            _instruments[("counter", name)] = instrument
        instrument.add(value, _otel_attributes(labels))
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "otel counter failed", extra={"metric_name": name, "error_type": type(exc).__name__}
        )


def _otel_record_ms(
    name: str, elapsed_ms: float, labels: Mapping[str, object] | None = None
) -> None:
    meter = _ensure_meter()
    if meter is None:
        return
    try:
        instrument = _instruments.get(("histogram", name))
        if instrument is None:
            # 不设 unit：设成 ms 会被 Prometheus exporter 叠成 `xxx_ms_milliseconds`。
            instrument = meter.create_histogram(name)
            _instruments[("histogram", name)] = instrument
        instrument.record(elapsed_ms, attributes=_otel_attributes(labels or {}))
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "otel histogram failed", extra={"metric_name": name, "error_type": type(exc).__name__}
        )


def gauge(name: str, *, value: float, **labels: object) -> None:
    """设置一个瞬时值（连接数、队列深度这类「此刻多少」）。

    用 OTel 的 `UpDownCounter` 落：Prometheus exporter 会把它导出成 gauge 语义的文本，
    而 `add()` 的口径与 `counter()` 一致（增减都记），不需要再引第三套 instrument。
    """
    if not _enabled():
        return
    meter = _ensure_meter()
    if meter is None:
        return
    try:
        instrument = _instruments.get(("gauge", name))
        if instrument is None:
            instrument = meter.create_up_down_counter(name)
            _instruments[("gauge", name)] = instrument
        instrument.add(float(value), _otel_attributes(labels))
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "otel gauge failed", extra={"metric_name": name, "error_type": type(exc).__name__}
        )


def render_metrics() -> str:
    """把进程内 registry 渲染成 Prometheus 文本（`/metrics` 的响应体）。

    开关关掉或 meter 建不起来时返回空串：Prometheus 抓一次空响应不会报错，只会
    「没有样本」，比 500 更容易看出是配置问题。
    """
    if not _enabled():
        return ""
    _ensure_meter()
    registry = _prom_registry
    if registry is None:
        return ""
    try:
        return generate_latest(registry).decode("utf-8")
    except Exception as exc:  # pragma: no cover
        logger.warning("metrics render failed", extra={"error_type": type(exc).__name__})
        return ""


def observe_ms(name: str, elapsed_ms: float, **labels: object) -> None:
    """记录一次耗时（毫秒），聚合为 count / sum / max。

    口径同 `counter`：关掉开关即 no-op；非有限值直接丢弃并 warn ——
    把 inf/NaN 混进 sum 会让整条曲线失效，且事后无法从结果里认出是哪一个样本。

    注意 `extra` 的键只能叫 `metric_name`：`name` 是 `LogRecord` 的保留属性，
    `extra={"name": ...}` 会让 logging 自己抛 `KeyError: "Attempt to overwrite 'name' in LogRecord"`，
    于是「丢弃坏样本」的兜底路径反过来把异常抛给调用方（迭代 5 A2 报的既有 bug）。

    `**labels` 只影响 OTel 直方图的属性（`agent_run_ms{layer="L2"}` 这类）；进程内镜像
    仍按**名字**聚合，键不带标签——20 个既有测试断言的是 `timings[name]`，加标签会
    把它们全改一遍，而分位口径本来就该按名字算。
    """
    if not _enabled():
        return
    try:
        ms = float(elapsed_ms)
    except (TypeError, ValueError):
        logger.warning("observe_ms ignored non-numeric value", extra={"metric_name": name})
        return
    if ms != ms or ms in (float("inf"), float("-inf")):
        logger.warning("observe_ms ignored non-finite value", extra={"metric_name": name})
        return
    slot = _timings.setdefault(name, {"count": 0.0, "sum_ms": 0.0, "max_ms": 0.0})
    slot["count"] += 1.0
    slot["sum_ms"] += ms
    slot["max_ms"] = max(slot["max_ms"], ms)
    samples = _timing_samples.get(name)
    if samples is None:
        samples = deque(maxlen=_TIMING_SAMPLE_MAX)
        _timing_samples[name] = samples
    samples.append(ms)
    _otel_record_ms(name, ms, labels)


def record_http_request(*, method: str, path: str, status: int | str, elapsed_ms: float) -> None:
    """记一次已完成的 HTTP 请求：请求量 + 耗时各一笔。

    `path` 必须是**路由模板**（`request.scope["route"].path`），不是真实路径：
    真实路径带 `elder_id` / `device_id`，每条都能长出新的计数器 key，长跑必爆内存。
    未匹配到路由（404）时调用方传 `unmatched`，同样是为了压住基数。
    """
    counter(HTTP_REQUESTS_COUNTER, method=method, path=path, status=str(status))
    observe_ms(HTTP_LATENCY, elapsed_ms)


def http_request_stats() -> dict[str, object]:
    """把 `http_requests_total{method=,path=,status=}` 汇成可读的三张分布表。

    `/metrics` 的响应体直接带这张表：抓的人不必自己拆 key，也能一眼看出
    「总量 / 状态码分布 / 热点路由」。开关关掉（或一次请求都没发生过）时
    `total=0` 且三张表为空 —— 与 counters 的「空即没发生」口径一致。

    实现上从 `_counters` 反解标签而不是另存一份聚合：单一数据源，
    免得两份计数在并发写入下慢慢错开（那类 bug 不报错，只会永远差一点）。
    """
    total = 0
    by_status: dict[str, int] = {}
    by_path: dict[str, int] = {}
    by_method: dict[str, int] = {}
    for key, count in _counters.items():
        matched = _HTTP_LABEL_RE.match(key)
        if matched is None:
            continue
        labels = {
            part.split("=", 1)[0]: part.split("=", 1)[1]
            for part in matched.group("labels").split(",")
            if "=" in part
        }
        total += count
        status = labels.get("status", "?")
        path = labels.get("path", "?")
        method = labels.get("method", "?")
        by_status[status] = by_status.get(status, 0) + count
        by_path[path] = by_path.get(path, 0) + count
        by_method[method] = by_method.get(method, 0) + count
    return {"total": total, "by_status": by_status, "by_path": by_path, "by_method": by_method}


def _nearest_rank(samples: Sequence[float], percentile: float) -> float | None:
    """nearest-rank 分位数：升序样本里取第 ceil(p/100 × n) 个（1-based）。

    选它而不是线性插值：样本量小（20 次实测）时插值会造出一个从没发生过的耗时，
    汇报「P95 = 1.47s」比「第 19 个样本 1.5s」更难解释。样本为空返回 None。
    """
    if not samples:
        return None
    rank = math.ceil(percentile / 100.0 * len(samples))
    rank = min(max(rank, 1), len(samples))
    return samples[rank - 1]


# ======================================================================
# 派生指标（迭代 10 · W4）
# ======================================================================
#
# 数据来源：**只读本模块的内存计数器**（`_counters`），不查 `decision_logs`。
#
# 为什么不查库：
# 1. `/metrics` 是运维高频抓取端点（Prometheus 15s 一次），一次抓取顺手打一条
#    `GROUP BY layer` 到 PostgreSQL，等于把监控变成数据库的隐性负载；
# 2. 本模块 163 行至今没有一行数据库依赖（没有 session / engine / asyncpg 的 import），
#    为了一个百分比破掉这个边界不值得 —— 想让 metrics 读库，得先给它一个连接池，
#    那就不再是「进程内计数」了；
# 3. 两个数据源的**窗口不同**：计数器是「本进程启动以来」，`decision_logs` 是「历史全量」。
#    混着看会得到对不上的两个数（比如重启后比率归零）。要历史口径去查
#    `app/services/decision_log_service.py`，本文件只回答「这个进程自己看到了什么」。
#
# 读写同一套 key：写入方是 `app/core/_observe.py`，读取方复用 `_format_key()`，
# 避免两边各手拼一遍字符串后悄悄错位（这类 bug 不报错，只会永远读到 0）。

#: 分层命中计数：`route_layer_total{layer=L0|L1|L2}`，一次路由决策加一次。
LAYER_COUNTER: Final = "route_layer_total"
#: L1 决策总数 / 其中直接闭环（未升 L2）的数量。
S1_TOTAL_COUNTER: Final = "route_s1_total"
S1_CLOSED_COUNTER: Final = "route_s1_closed_total"
#: LLM token 累计（DeepSeek / Qwen 的 usage 字段，拿不到就不加）。
PROMPT_TOKEN_COUNTER: Final = "llm_prompt_tokens_total"  # noqa: S105 - 指标名，不是口令
COMPLETION_TOKEN_COUNTER: Final = "llm_completion_tokens_total"  # noqa: S105 - 指标名，不是口令
#: prompt cache：分母是「provider 真回了 cached 字段的调用次数」，不是「所有 LLM 调用」。
CACHE_HIT_COUNTER: Final = "llm_prompt_cache_hit_total"
CACHE_LOOKUP_COUNTER: Final = "llm_prompt_cache_lookup_total"

#: 分层取值（顺序即输出顺序，便于人肉比对）。
LAYERS: Final[tuple[str, ...]] = ("L0", "L1", "L2")


def _read_counter(name: str, labels: Mapping[str, object] | None = None) -> int:
    """读一个计数器当前值；key 的拼法与 `counter()` 完全一致。"""
    return _counters.get(_format_key(name, labels or {}), 0)


def _sum_counter(name: str) -> int:
    """读一个计数器**跨标签**的合计（裸 key 与所有 `name{...}` 一起相加）。

    为什么需要单独一个函数：token 计数按 `model` 打标签后（FR-TC-08 要求
    成本能按模型换算），只看裸 key 的 `_read_counter` 会永远读到 0，
    从而把「有 token 消耗」误报成「一次 LLM 都没调过」。
    """
    prefix = f"{name}{{"
    total = _counters.get(name, 0)
    for key, value in _counters.items():
        if key.startswith(prefix):
            total += value
    return total


def _ratio(part: int, total: int) -> float:
    """占比：0..1 小数（保留 4 位）；分母为 0 时返回 0.0。

    这里返回 0.0 而不是 None 是有意的：**比率旁边永远配一个 `*_samples`**，
    要区分「真 0」（有样本但一次没命中）和「没数据」（样本为 0）看 samples，
    不看比率本身。唯一的例外是 `prompt_cache_hit_rate()`，它的 `None` 是对外契约
    （见该函数 docstring）。
    """
    if total <= 0:
        return 0.0
    return round(part / total, 4)


def layer_hit_rates() -> dict[str, float]:
    """L0 / L1 / L2 三层各占全部路由决策的比例（0..1，三者相加为 1.0）。

    分子来自内存计数器 `route_layer_total{layer=…}`，由 `app/core/_observe.record_route()`
    在每次决策时累加；分母就是三层之和。一条路由都没发生过时三层都是 0.0。
    """
    counts = {layer: _read_counter(LAYER_COUNTER, {"layer": layer}) for layer in LAYERS}
    total = sum(counts.values())
    return {layer: _ratio(count, total) for layer, count in counts.items()}


def layer_samples() -> int:
    """分层指标的样本量（= 已计入的路由决策条数），用来区分「真 0」与「没数据」。"""
    return sum(_read_counter(LAYER_COUNTER, {"layer": layer}) for layer in LAYERS)


def s1_closure_rate() -> float:
    """L1 **直接闭环**（未升级 L2）占 L1 决策的比例（0..1）。

    判据在 `app/core/_observe._s1_closed()`：置信度 ≥ 0.85 视为直路由闭环
    （AGENTS §7 / PRD §2.3），调用方也能显式传 `escalated` 覆盖。分母为 0 时返回 0.0。
    """
    return _ratio(_read_counter(S1_CLOSED_COUNTER), _read_counter(S1_TOTAL_COUNTER))


def s1_samples() -> int:
    """S1 闭环率的分母（L1 决策条数）。"""
    return _read_counter(S1_TOTAL_COUNTER)


def token_usage() -> dict[str, int]:
    """累计 LLM token 用量；**一次 LLM 都没调过时三项都是 0**（不估、不造）。"""
    prompt = _sum_counter(PROMPT_TOKEN_COUNTER)
    completion = _sum_counter(COMPLETION_TOKEN_COUNTER)
    return {"prompt": prompt, "completion": completion, "total": prompt + completion}


def prompt_cache_hit_rate() -> float | None:
    """prompt cache 命中率；**一次 cache 查询都没有时返回 None**。

    为什么不返回 0.0：0.0 会被读成「cache 一次都没命中」，那是个要立刻排查的告警级结论；
    而实际情况是「provider 没回 cached 字段 / 还没发过请求」。两者混成一个 0，会让人对着
    一条假故障曲线查半天。`/metrics` 的消费方必须显式处理 null。
    """
    total = _read_counter(CACHE_LOOKUP_COUNTER)
    if total <= 0:
        return None
    return round(_read_counter(CACHE_HIT_COUNTER) / total, 4)


def prompt_cache_samples() -> int:
    """prompt cache 命中率的分母（provider 真回了 cached 字段的调用次数）。"""
    return _read_counter(CACHE_LOOKUP_COUNTER)


def derived_metrics() -> dict[str, object]:
    """`metrics_snapshot()` 的 `derived` 节：四类派生指标 + 各自的样本量。"""
    return {
        "layer_hit_rates": layer_hit_rates(),
        "layer_samples": layer_samples(),
        "s1_closure_rate": s1_closure_rate(),
        "s1_samples": s1_samples(),
        "token_usage": token_usage(),
        "prompt_cache_hit_rate": prompt_cache_hit_rate(),
        "prompt_cache_samples": prompt_cache_samples(),
    }


def metrics_snapshot() -> dict[str, object]:
    """当前进程的指标只读快照。

    结构固定为 `{"enabled": bool, "counters": {...}, "timings": {...}, "derived": {...}}`，
    counters / timings / derived 都是**新对象**（深拷贝）；enabled 取的是读快照这一刻的配置，
    便于调用方区分「开关关掉了」和「一件事都没发生」。

    `derived` 是 迭代 10 · W4 新增的第四节：**既有两节（counters / timings）的键名一个不动**，
    迭代 5/6 写下的断言（`set(timings) == {count,sum_ms,max_ms,…}` 之类）继续成立。
    `/metrics` 的响应体是 pydantic 模型，多出来的节要等它补字段才会出现在 HTTP 响应里
    （`app/api/metrics.py` 属 Lead 独占，本包不碰）。
    """
    return {
        "enabled": _enabled(),
        "counters": dict(_counters),
        "timings": {
            name: {
                **dict(slot),
                "p50_ms": _nearest_rank(sorted(_timing_samples.get(name, ())), 50),
                "p95_ms": _nearest_rank(sorted(_timing_samples.get(name, ())), 95),
            }
            for name, slot in _timings.items()
        },
        "derived": derived_metrics(),
    }


def reset_metrics() -> None:
    """清空全部指标（仅测试与本地调试用）。

    生产代码不要调：进程里唯一的一份计数被清掉后，监控曲线会凭空出现一个回零。

    S3 之后连带重建 OTel 侧：`MeterProvider` 里的累计值是只增不减的，不换 provider
    就会出现「快照已清零、`/metrics` 还有旧数」的两套口径（用例之间尤其明显）。
    """
    global _prom_registry, _meter_provider, _meter
    _counters.clear()
    _timings.clear()
    _timing_samples.clear()
    _instruments.clear()
    provider, _meter_provider = _meter_provider, None
    _meter = None
    _prom_registry = None
    if provider is not None:
        try:
            provider.shutdown()
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "metrics provider shutdown failed", extra={"error_type": type(exc).__name__}
            )


__all__ = [
    "CACHE_HIT_COUNTER",
    "CACHE_LOOKUP_COUNTER",
    "COMPLETION_TOKEN_COUNTER",
    "HTTP_LATENCY",
    "HTTP_REQUESTS_COUNTER",
    "LAYERS",
    "LAYER_COUNTER",
    "PROMPT_TOKEN_COUNTER",
    "S1_CLOSED_COUNTER",
    "S1_TOTAL_COUNTER",
    "counter",
    "derived_metrics",
    "gauge",
    "http_request_stats",
    "layer_hit_rates",
    "layer_samples",
    "metrics_snapshot",
    "observe_ms",
    "prompt_cache_hit_rate",
    "prompt_cache_samples",
    "record_http_request",
    "render_metrics",
    "reset_metrics",
    "s1_closure_rate",
    "s1_samples",
    "token_usage",
]
