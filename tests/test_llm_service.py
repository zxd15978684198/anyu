"""W1 · 统一 LLM 适配器（`app/services/llm_service.py`）的回归集。

对应 `docs/plans/day04/day04-对话闭环收口计划.md` §5-W1 的验收命令，逐条覆盖：

| 用例 | 覆盖的验收口径 |
| --- | --- |
| `test_chat_returns_model_text` | 正常返回，`degraded=False` |
| `test_missing_api_key_degrades_without_network` | key 为空 → **一次网络都不打**，直接降级 |
| `test_timeout_retries_then_succeeds` | 瞬态错误重试 ≤ `llm_max_retries` 次后成功 |
| `test_retries_exhausted_degrades` | 重试耗尽 → 降级话术，`degraded=True` |
| `test_4xx_is_not_retried` | 4xx 不重试（只打一次网络） |
| `test_429_and_5xx_are_retried` | 429/5xx 属临时错误，要重试 |
| `test_bad_payload_degrades` | 200 但结构不对（网关 HTML / 形状变了）→ 降级 |
| `test_never_raises_on_unexpected_error` | 未知异常不外抛（FR-DLG-05「继续可用」） |
| `test_request_body_is_openai_compatible` | 请求体字段与非流式口径 |
| `test_no_vendor_literal_in_source` | 代码里不出现任何供应商域名（AGENTS §4.2） |
| `test_no_key_or_message_body_in_logs` | 日志不含 key、不含消息正文（AGENTS §4.3） |

**为什么全部走 `httpx.MockTransport`**：真打网络会让 CI 依赖外网与余额，
而且「超时→重试→降级」这条链根本没法用真接口稳定复现。`MockTransport` 是 httpx 官方
提供的注入点，走的是**与生产完全相同**的请求构造与响应解析路径。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.degrade_speech import SPOKEN_DEGRADE
from app.services import llm_service
from app.services.llm_service import DEGRADED_REPLY_TEXT, LLMService

# --------------------------------------------------------------- 测试替身 / 夹具

#: 假 key。写成常量是为了让 grep 「sk-」 之类的前缀在测试里也没有真实形态。
FAKE_KEY = "test-key-not-a-real-secret"


def _settings(**overrides: Any) -> Settings:
    """构造一份不读 `.env` 的配置。

    `_env_file=None` 是必须的：否则本机 `.env` 里的真 key 会漏进来，
    让「key 为空」那条用例在他机器上静默变成「真打了网络」。
    """
    base: dict[str, Any] = {
        "llm_base_url": "https://llm.invalid/v1",
        "llm_api_key": FAKE_KEY,
        "llm_model": "test-model",
        "llm_timeout_seconds": 5,
        "llm_max_retries": 2,
        "llm_temperature": 0.3,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _ok_body(text: str = "您好呀，今天天气不错。") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }


def _service(handler: Callable[[httpx.Request], httpx.Response], **overrides: Any) -> LLMService:
    """把 handler 包进 MockTransport，注入给 LLMService（生产路径不变）。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMService(_settings(**overrides), client=client)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """退避不真睡：记录 sleep 入参即可断言「重试之间确实退避了」。

    不 patch 掉的话，重试耗尽的用例要真等 0.75s，一批用例下来十几秒。
    """
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(llm_service, "_sleep", _fake_sleep)
    return slept


_MESSAGES = [
    {"role": "system", "content": "你是小满。"},
    {"role": "user", "content": "今天天气怎么样"},
]


# --------------------------------------------------------------- 主路径


async def test_chat_returns_model_text() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok_body("外面晴着呢。"))

    service = _service(handler)
    reply = await service.chat(_MESSAGES, trace_id="trace-1")

    assert reply.text == "外面晴着呢。"
    assert reply.degraded is False
    assert len(captured) == 1


async def test_request_body_is_openai_compatible() -> None:
    """请求体形状：`model` / `messages` / `temperature` / `stream=False`。

    `stream` 必须是 False：迭代 4 的解析假设 JSON 响应体（计划 S-D8）。
    有人把它改成 True 而没同步改解析，这里先红。
    """
    captured: list[dict[str, Any]] = []
    headers: list[httpx.Headers] = []
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        headers.append(request.headers)
        urls.append(str(request.url))
        return httpx.Response(200, json=_ok_body())

    await _service(handler).chat(_MESSAGES)

    assert urls == ["https://llm.invalid/v1/chat/completions"]
    body = captured[0]
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.3
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "你是小满。"},
        {"role": "user", "content": "今天天气怎么样"},
    ]
    assert headers[0]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert headers[0]["Content-Type"] == "application/json"


async def test_extra_headers_are_sent_but_cannot_override_auth() -> None:
    """`llm_extra_headers` 要随请求发出，但**不许**覆盖认证与 Content-Type。

    真实场景：opencode 网关要求 `x-opencode-session`，缺了直接 400；同时不能让人
    在配置里写 `Authorization` 把请求变成匿名（那会静默 401，比报错更难查）。
    """
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json=_ok_body())

    service = _service(
        handler,
        llm_extra_headers={
            "x-opencode-session": "anyu-app",
            "Authorization": "Bearer overridden-by-config",
        },
    )
    reply = await service.chat(_MESSAGES)

    assert reply.degraded is False
    assert seen[0]["x-opencode-session"] == "anyu-app"
    assert seen[0]["Authorization"] == f"Bearer {FAKE_KEY}"


async def test_extra_headers_default_is_empty() -> None:
    """默认不额外带头：不配就没有，别把某个网关的头塞给所有部署。"""
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json=_ok_body())

    await _service(handler).chat(_MESSAGES)

    assert "x-opencode-session" not in seen[0]


async def test_empty_messages_degrades_without_network() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调
        calls.append(request)
        return httpx.Response(200, json=_ok_body())

    reply = await _service(handler).chat([], trace_id="trace-1")

    assert reply.degraded is True
    assert reply.text == DEGRADED_REPLY_TEXT
    assert calls == []


# --------------------------------------------------------------- 降级


async def test_missing_api_key_degrades_without_network() -> None:
    """key 为空：本地常态，不该先打三次网络再失败。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调
        calls.append(request)
        return httpx.Response(200, json=_ok_body())

    reply = await _service(handler, llm_api_key="").chat(_MESSAGES)

    assert calls == []
    assert reply.degraded is True
    assert reply.text == DEGRADED_REPLY_TEXT
    # 话术必须来自唯一源，不是本模块另写一句
    assert reply.text == SPOKEN_DEGRADE["llm_timeout"]


async def test_retries_exhausted_degrades() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("boom", request=request)

    reply = await _service(handler, llm_max_retries=2).chat(_MESSAGES, trace_id="trace-1")

    # llm_max_retries=2 → 总请求 1 + 2 = 3 次
    assert len(attempts) == 3
    assert reply.degraded is True
    assert reply.text == DEGRADED_REPLY_TEXT


async def test_4xx_is_not_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("Authorization") is not None)
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    reply = await _service(handler, llm_max_retries=2).chat(_MESSAGES)

    assert len(attempts) == 1, "401 属权限错，重试只是浪费两次超时窗口"
    assert reply.degraded is True


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_429_and_5xx_are_retried(status: int) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(status)
        return httpx.Response(status, json={"error": {"message": "busy"}})

    reply = await _service(handler, llm_max_retries=1).chat(_MESSAGES)

    assert len(attempts) == 2, f"{status} 属临时错误，应重试 1 次"
    assert reply.degraded is True


async def test_bad_payload_degrades() -> None:
    """200 但结构不对（网关 HTML / 换了不兼容 API）：当「没有可用回复」→ 降级。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login required</html>")

    reply = await _service(handler).chat(_MESSAGES)

    assert reply.degraded is True
    # 绝不能把 HTML 当回复念给长者听
    assert "<html>" not in reply.text


async def test_empty_choices_degrades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    reply = await _service(handler).chat(_MESSAGES)

    assert reply.degraded is True
    assert reply.text == DEGRADED_REPLY_TEXT


async def test_null_content_degrades() -> None:
    """模型只出了 tool_calls 时 `content` 是 None：同样按「没有可用回复」处理。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    reply = await _service(handler).chat(_MESSAGES)

    assert reply.degraded is True


async def test_never_raises_on_unexpected_error() -> None:
    """未知异常必须被吃掉：一次 WS 连接不能因为上游实现 bug 就断（FR-DLG-05）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected internal failure")

    reply = await _service(handler).chat(_MESSAGES)

    assert reply.degraded is True
    assert reply.text == DEGRADED_REPLY_TEXT


# --------------------------------------------------------------- 重试语义


async def test_timeout_retries_then_succeeds(_no_real_sleep: list[float]) -> None:
    """第一次超时、第二次成功：返回模型正文，且确实退避过一次。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=_ok_body("我来了。"))

    reply = await _service(handler, llm_max_retries=2).chat(_MESSAGES, trace_id="trace-1")

    assert reply.text == "我来了。"
    assert reply.degraded is False
    assert len(calls) == 2
    # 退避基数 0.25 × 第 1 次 = 0.25
    assert _no_real_sleep == [0.25]


async def test_backoff_grows_with_attempt(_no_real_sleep: list[float]) -> None:
    """两次重试的退避是 0.25 / 0.5，不是固定值（线性增长）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    await _service(handler, llm_max_retries=2).chat(_MESSAGES)

    assert _no_real_sleep == [0.25, 0.5]


async def test_zero_retries_means_single_attempt() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("down", request=request)

    reply = await _service(handler, llm_max_retries=0).chat(_MESSAGES)

    assert len(calls) == 1
    assert reply.degraded is True


# --------------------------------------------------------------- 卫生检查


def test_no_vendor_literal_in_source() -> None:
    """代码里不得出现任何供应商域名：换供应商只改 `.env`（AGENTS §4.2 / 计划 A-D3）。"""
    source = llm_service.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for literal in ("api.deepseek.com", "dashscope", "aliyuncs", "minimax", "openai.com"):
        assert literal not in text, f"发现供应商字面量 {literal!r}，应走 settings"


async def test_no_key_or_message_body_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """日志只记 model / latency / attempts / status / degraded，不记 key 与消息正文。"""
    secret_user_text = "我今天血压有点高"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("知道了。"))

    caplog.set_level("DEBUG")
    await _service(handler).chat(
        [
            {"role": "system", "content": "你是小满。"},
            {"role": "user", "content": secret_user_text},
        ],
        trace_id="trace-log",
    )

    joined = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert FAKE_KEY not in joined, "日志里不得出现 API key（AGENTS §4.3）"
    assert secret_user_text not in joined, "日志里不得出现长者原话（AGENTS §8 脱敏）"
    # 但 trace_id 必须出现（AGENTS §8 每行带 trace_id）
    assert "trace-log" in joined


# --------------------------------------------------------------- 生命周期


async def test_aclose_closes_owned_client_only() -> None:
    """自建 client 由本类关；注入的 client 由注入方关（不关别人的东西）。"""
    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    service = LLMService(_settings(), client=injected)
    await service.aclose()
    assert not injected.is_closed, "注入的 client 不该被本类关闭"
    await injected.aclose()


async def test_aclose_is_idempotent() -> None:
    service = LLMService(_settings())
    await service.aclose()
    await service.aclose()  # 不抛


async def test_base_url_with_request_path_does_not_double_it() -> None:
    """`.env` 里误贴整条接口地址时，不能拼成 `.../chat/completions/chat/completions`。

    真实事故：base 被写成 `https://host/v1/chat/completions`，而本模块再拼一次
    `_REQUEST_PATH` → 404 → 每一轮都静默降级成「我稍微想一想」。这里钉住两件事：
    URL 只出现一次该路径，且**必须**留下 warning（配置该改，不能被悄悄原谅）。
    """

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_ok_body())

    service = _service(handler, llm_base_url="https://llm.invalid/v1/chat/completions")
    reply = await service.chat([{"role": "user", "content": "你好"}])
    await service.aclose()

    assert reply.text == "您好呀，今天天气不错。"
    assert seen == ["https://llm.invalid/v1/chat/completions"]


def test_base_url_duplicate_path_warns_once_per_build(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """截断不是静默行为：构造时就要把「配置写错了」说出来。"""
    caplog.set_level(logging.WARNING, logger="app.services.llm_service")

    service = LLMService(_settings(llm_base_url="https://llm.invalid/v1/chat/completions"))

    assert service._endpoint == "https://llm.invalid/v1/chat/completions"
    assert any(
        "base_url contains request path" in record.getMessage() for record in caplog.records
    ), "配置里带请求路径时必须留警告，否则真实 404 会被当成网络抖动"
