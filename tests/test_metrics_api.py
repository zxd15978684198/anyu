"""`GET /metrics` 的接口级测试（task-20 · 迭代 5 S3 改写）。

S3 之后 `/metrics` 是 **Prometheus 文本抓取面**，不再是鉴权的 JSON 快照。本文件因此
覆盖新的三件事：

1. **文本形状**：`text/plain; version=0.0.4`，内容是 `# HELP` / `# TYPE` + 样本行；
   `counter()` 与 `observe_ms()` 打的点都能在文本里找到（`_total` 后缀、`_bucket` 系列）；
2. **鉴权口径变化**：无 token 也返回 200（抓取端不带凭证）；非回环地址 403；
3. **开关与只读性**：`metrics_enabled=False` 返回空文本 200；连续拉两次文本不会让计数变多。

不再需要数据库：鉴权分支已去掉，`ContractAPIRoute` 的错误体仍由 `TraceIdMiddleware`
补 `trace_id`。计数器是进程级全局，故有 autouse 夹具在每用例前后各 `reset_metrics()`
（S3 起它也会重建 OTel meter，否则上一个用例的累计值会留在 registry 里）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import metrics as metrics_api
from app.core import metrics as metrics_core
from app.core.config import get_settings
from app.core.trace import TraceIdMiddleware

# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def fresh_metrics() -> Iterator[None]:
    """隔离进程级计数器与 OTel registry。"""
    metrics_core.reset_metrics()
    yield
    metrics_core.reset_metrics()


@pytest.fixture
def app() -> FastAPI:
    """最小应用：只挂中间件与 `/metrics`，不接 DB / 鉴权。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(metrics_api.router)
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------- 文本形状


async def test_returns_prometheus_text_without_token(client: AsyncClient) -> None:
    """无 Authorization 也 200：Prometheus 抓取端不会带员工 JWT。"""
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in response.headers["content-type"]
    # 正文可能为空：一次打点都没有时 registry 里没有样本（不算失败）。


async def test_counter_and_histogram_both_render(client: AsyncClient) -> None:
    """打点后文本里能同时看到 counter（`_total`）与直方图（`_bucket`/`_count`/`_sum`）。"""
    metrics_core.counter("outbound_calls_total", result="ok")
    metrics_core.observe_ms("agent_run_ms", 12.5)

    text = (await client.get("/metrics")).text

    assert "outbound_calls_total{" in text
    assert 'result="ok"' in text
    assert "agent_run_ms_bucket" in text
    assert "agent_run_ms_count" in text
    assert "agent_run_ms_sum" in text


async def test_counter_without_total_suffix_gets_one(client: AsyncClient) -> None:
    """历史 5 个不带 `_total` 的 counter 由 exporter 补后缀（口径写进计划 S1 映射表）。"""
    metrics_core.counter("identity_ask_sent")

    text = (await client.get("/metrics")).text

    assert "identity_ask_sent_total" in text


async def test_histogram_has_no_duplicated_milliseconds_suffix(client: AsyncClient) -> None:
    """`observe_ms` 的 instrument 不设 unit，否则会叠成 `agent_run_ms_milliseconds_*`。"""
    metrics_core.observe_ms("agent_run_ms", 5.0)

    text = (await client.get("/metrics")).text

    assert "agent_run_ms_milliseconds" not in text


# ---------------------------------------------------------------- 鉴权 / 开关 / 只读


async def test_non_loopback_is_forbidden() -> None:
    """非回环地址直接拒绝：`/metrics` 的防护从 JWT 换成回环限制。"""
    request = SimpleNamespace(client=SimpleNamespace(host="10.1.2.3"))

    with pytest.raises(HTTPException) as caught:
        await metrics_api.read_metrics(request)  # type: ignore[arg-type]

    assert caught.value.status_code == 403


async def test_loopback_is_allowed() -> None:
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    response = await metrics_api.read_metrics(request)  # type: ignore[arg-type]

    assert response.status_code == 200


async def test_disabled_returns_empty_text(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关掉时仍 200，但正文为空（Prometheus 只会记「没有样本」）。"""
    metrics_core.counter("outbound_calls_total")
    monkeypatch.setenv("METRICS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        response = await client.get("/metrics")
    finally:
        monkeypatch.delenv("METRICS_ENABLED", raising=False)
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.text == ""


async def test_reading_twice_does_not_increase_counts(client: AsyncClient) -> None:
    """端点本身只读：两次拉取之间计数不变。"""
    metrics_core.counter("identity_ask_sent")

    first = (await client.get("/metrics")).text
    second = (await client.get("/metrics")).text

    assert first == second
    # 样本行带 `otel_scope_*` 标签，故只断言「名字出现且值为 1」，不锁整行形状。
    assert "identity_ask_sent_total{" in first
    assert first.rstrip().endswith(" 1.0")


async def test_render_metrics_is_stable_without_instruments(client: AsyncClient) -> None:
    """一次打点都没有时也能渲染（可能只有 `target_info` 之类的自动样本）。"""
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert isinstance(response.text, str)


def test_token_usage_sums_across_model_labels() -> None:
    """token 计数按 `model` 打标签后，合计仍要读到。

    否则 FR-TC-08 的成本换算会看到 `token_usage()` 恒为 0，把「有 token 消耗」
    误报成「一次 LLM 都没调过」。同时验证无 `model` 的样本也计入合计。
    """
    from app.core._observe import record_llm_usage

    record_llm_usage(
        prompt_tokens=1000, completion_tokens=500, latency_ms=100, model="deepseek-v4.1-flash"
    )
    record_llm_usage(prompt_tokens=300, completion_tokens=0, latency_ms=90)

    assert metrics_core.token_usage() == {"prompt": 1300, "completion": 500, "total": 1800}
    rendered = metrics_core.render_metrics()
    assert 'llm_prompt_tokens_total{model="deepseek-v4.1-flash"' in rendered
