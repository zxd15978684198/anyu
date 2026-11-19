# owner=W1 · 依据 TD line 200 / PRD FR-DLG-05 · 统一 LLM 适配器（不直连 SDK）
"""System Two · 统一 LLM 适配器（迭代 4 收口 W1 · G-D1）。

**只做一件事**：把一段 OpenAI 兼容的 `messages` 发给 `settings.llm_base_url`，
拿回一句话。不编排对话、不读写 Redis、不发 WebSocket、不调 TTS —— 那四件事分别属于
`dialogue_service` / `signaling` / `webrtc` / `tts`（AGENTS §2 依赖单向、AGENTS §4.8 一能力一形态）。

## 为什么用 httpx 直连而不是官方 SDK

技术设计文档 line 200 的口径是「通过**统一 LLM 适配器**接入」，AGENTS §1 更明确「不直连 SDK」。
DeepSeek / Qwen / MiniMax 都提供 OpenAI 兼容的 `/chat/completions`，用 httpx 发一个 POST
就能同时覆盖三家；换成任何一家只改 `.env`（`LLM_BASE_URL` / `LLM_MODEL`），**代码里不出现
任何供应商名字面量**（AGENTS §4.2 密钥红线、§4.12 不擅自引入新框架）。

## 失败分类（AGENTS §8 四分类的 LLM 侧映射）

| 情形 | 处置 | 为什么 |
| --- | --- | --- |
| `httpx.TimeoutException` / `ConnectError` / 429 / 5xx | 退避重试 ≤ `llm_max_retries` 次 | 临时错误，退避后大概率成功 |
| 4xx（400/401/403/404 等） | **不重试**，直接降级 | 参数错/权限错，重试只是浪费 2 次超时窗口 |
| `llm_api_key` 为空 | **不请求**，直接降级 | 本地开发常态；不该先打 3 次网络再失败 |
| 重试耗尽 | 降级 | FR-DLG-05 要求「继续可用」 |

**关键口径**：任何失败都返回 `LLMReply(degraded=True)`，**从不抛异常**。PRD line 270 的
验收是「断开 LLM 后闲聊返回"暂时无法回答"」——如果这里抛异常，调用方要么漏处理（用户
听不到任何回应），要么自己再写一套降级（AGENTS §4.8 一能力一形态）。

## 降级话术用哪一套

复用 `app/core/degrade_speech.py::SPOKEN_DEGRADE["llm_timeout"]`（迭代 5 §4.3 已把两套
硬编码话术收到这一处）。本模块**不持有任何话术字面量**，只 import 取用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Final

import httpx

from app.core._observe import record_llm_usage, record_prompt_cache
from app.core.config import Settings, get_settings
from app.core.degrade_speech import SPOKEN_DEGRADE
from app.core.metrics import counter, observe_ms

logger = logging.getLogger("app.services.llm_service")

#: 请求路径。只拼在 `settings.llm_base_url` 后面，不在这里写死任何供应商域名。
_REQUEST_PATH: Final[str] = "/chat/completions"


def _endpoint_url(base_url: str) -> str:
    """把 `llm_base_url` 归一成完整端点：配置里误贴整条 URL 也不会拼出双路径。

    `.env` 里把 base 写成 `https://host/v1/chat/completions` 是很常见的失误（复制粘贴
    接口地址），而这里会再拼一次 `_REQUEST_PATH`，结果是 `.../v1/chat/completions/chat/
    completions` → 404 → 每一轮都静默降级成「我稍微想一想」。与其让人对着 404 猜，不如
    截掉重复段并打一条 warning —— 但**不吞掉问题**：配置仍然应该只写 base。
    """
    trimmed = base_url.strip()
    while trimmed.endswith(_REQUEST_PATH):
        logger.warning(
            "llm base_url contains request path",
            extra={"reason": "base_url_has_request_path"},
        )
        trimmed = trimmed[: -len(_REQUEST_PATH)]
    return trimmed.rstrip("/") + _REQUEST_PATH


#: 降级话术（取降级话术唯一源，不在本文件另写一句）。
DEGRADED_REPLY_TEXT: Final[str] = SPOKEN_DEGRADE["llm_timeout"]

#: 退避基数（秒）。第 n 次重试前睡 `0.25 * n`：2 次重试累计 0.75s，
#: 远小于 `llm_timeout_seconds`，不会把「重试」本身变成延迟主要来源。
RETRY_BACKOFF_SECONDS: Final[float] = 0.25


@dataclass(frozen=True, slots=True)
class LLMReply:
    """一次 LLM 调用的结果。

    `degraded=True` 表示这句话**不是模型生成的**，是固定降级话术；调用方据此决定
    是否在 `tts_text.degraded` 里标记（契约 C6 `TtsTextMsgV2.degraded`），
    以及是否把它计入「有效回复」统计。
    """

    text: str
    degraded: bool


@dataclass(frozen=True, slots=True)
class _Attempt:
    """一次网络尝试的结果；只在本模块内部流转，不外露。"""

    text: str | None
    status: int | None
    retryable: bool
    error_type: str | None


class LLMService:
    """OpenAI 兼容 `chat/completions` 的非流式客户端。

    生命周期与 HTTP 连接池绑定：生产在 `app/main.py::lifespan` 里建一个，
    进程退出时 `await service.aclose()`；测试可直接注入 `httpx.AsyncClient`，
    或注入 `httpx.MockTransport` 所在的 client（见 `tests/test_llm_service.py`）。

    **为什么非流式**：迭代 4 的验收是「收到回复」，不是「首 token 多快」。流式与
    首 token 打点属 迭代 5 延迟预算（计划 §8 已登记），届时只改本文件的多一次
    `stream=True` 分支与 `observe_ms("llm_first_token_ms", ...)` 的位置，
    `chat()` 的签名与 `LLMReply` 形状不变，调用方零改动。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._owned_client = client is None
        self._endpoint = _endpoint_url(self._settings.llm_base_url)

    # ---------------------------------------------------------------- 生命周期

    @property
    def settings(self) -> Settings:
        """只读配置快照；调用方（如主装配）需要读 model 名打日志时用。"""
        return self._settings

    async def aclose(self) -> None:
        """关闭自建连接池。注入的 client 由注入方负责关闭（不在这里关别人的东西）。"""
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- 对外接口

    async def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        trace_id: str | None = None,
    ) -> LLMReply:
        """发一轮对话，返回一句话（失败时是降级话术，**从不抛**）。

        `trace_id` 只用于日志串联，不进请求体（AGENTS §8「每行日志都带 trace_id」）。
        """
        started = _perf_counter()
        normalized = _normalize_messages(messages)
        if not normalized:
            # 空 messages 属调用方编程错误，但对长者而言等价于「这轮没话说」，
            # 按降级处理而不是抛 ValueError —— 一次 WS 连接不能因为上游拼错就断。
            logger.warning(
                "llm empty messages",
                extra={"trace_id": trace_id or "-", "reason": "empty_messages"},
            )
            return self._degraded(trace_id, reason="empty_messages")

        if not self._settings.llm_api_key.strip():
            # 本地常态：不打网络、不重试、不伪装成「网络超时」。
            logger.warning(
                "llm api key missing",
                extra={"trace_id": trace_id or "-", "reason": "missing_api_key"},
            )
            return self._degraded(trace_id, reason="missing_api_key")

        attempts_allowed = max(0, int(self._settings.llm_max_retries)) + 1
        attempt = 0
        last_error: str | None = None
        status: int | None = None

        while attempt < attempts_allowed:
            attempt += 1
            result = await self._post_once(normalized, trace_id=trace_id)
            status = result.status
            if result.text is not None:
                elapsed = (_perf_counter() - started) * 1000.0
                observe_ms("llm_chat_ms", elapsed)
                counter("llm_chat_total", outcome="ok")
                logger.info(
                    "llm chat ok",
                    extra={
                        "trace_id": trace_id or "-",
                        "model": self._settings.llm_model,
                        "attempts": attempt,
                        "status": status,
                        "latency_ms": round(elapsed, 3),
                        "reply_len": len(result.text),
                        "degraded": False,
                    },
                )
                return LLMReply(text=result.text, degraded=False)

            last_error = result.error_type
            if not result.retryable:
                # 4xx：重试只是重复同一个错，浪费两次超时窗口。
                break
            if attempt < attempts_allowed:
                await _sleep(RETRY_BACKOFF_SECONDS * attempt)

        elapsed = (_perf_counter() - started) * 1000.0
        observe_ms("llm_chat_ms", elapsed)
        counter("llm_chat_total", outcome="degraded")
        logger.warning(
            "llm chat degraded",
            extra={
                "trace_id": trace_id or "-",
                "model": self._settings.llm_model,
                "attempts": attempt,
                "status": status,
                "latency_ms": round(elapsed, 3),
                "error_type": last_error or "-",
                "degraded": True,
            },
        )
        return self._degraded(trace_id, reason=last_error or "llm_unavailable")

    # ---------------------------------------------------------------- 内部实现

    async def _post_once(
        self,
        messages: list[dict[str, str]],
        *,
        trace_id: str | None,
    ) -> _Attempt:
        """一次网络往返。**只在本协程内把异常翻成 retryable 标记**，不外抛。"""
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
            # 非流式：迭代 4 口径（计划 S-D8）。开成 True 会让响应体变成 SSE，
            # `_extract_text` 的 JSON 假设立刻失效 —— 改流式时这里与解析同时改。
            "stream": False,
        }
        url = self._endpoint
        # 额外头先铺，再写本模块的两条 —— 认证与 Content-Type 不允许被配置覆盖
        # （配置里误写 Authorization 不至于把请求发成匿名）。
        headers: dict[str, str] = dict(self._settings.llm_extra_headers)
        headers["Authorization"] = f"Bearer {self._settings.llm_api_key}"
        headers["Content-Type"] = "application/json"
        timeout = httpx.Timeout(float(self._settings.llm_timeout_seconds))

        try:
            client = await self._ensure_client()
            response = await client.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            return _Attempt(text=None, status=None, retryable=True, error_type="timeout")
        except httpx.ConnectError:
            return _Attempt(text=None, status=None, retryable=True, error_type="connect_error")
        except httpx.HTTPError as exc:
            # 其余 httpx 层异常（协议错、读超时之外的传输错）：可重试一次看是否是抖动。
            return _Attempt(text=None, status=None, retryable=True, error_type=type(exc).__name__)
        except Exception as exc:  # 兜底：绝不把未知异常漏给调用方（FR-DLG-05「继续可用」）
            logger.exception(
                "llm unexpected error",
                extra={"trace_id": trace_id or "-", "error_type": type(exc).__name__},
            )
            return _Attempt(text=None, status=None, retryable=False, error_type=type(exc).__name__)

        status = response.status_code
        if status == 429 or status >= 500:
            return _Attempt(text=None, status=status, retryable=True, error_type=f"http_{status}")
        if status >= 400:
            # 401/403（key 错）/ 400（参数错）/ 404（endpoint 错）：重试没有意义。
            # 只记状态码，**不记响应体** —— 供应商的 4xx 响应体可能回显部分 key。
            logger.warning(
                "llm http error",
                extra={"trace_id": trace_id or "-", "status": status, "retryable": False},
            )
            return _Attempt(text=None, status=status, retryable=False, error_type=f"http_{status}")

        text = _extract_text(response)
        if text is None:
            # 200 但结构不对（被网关拦截返回 HTML、或换了不兼容的 API）：不可重试。
            return _Attempt(text=None, status=status, retryable=False, error_type="bad_payload")
        # 迭代 10 · W4：token 与 prompt cache 打点。只在响应真带 usage 时才记（见
        # `_extract_usage` 的「不拿 0 冒充」口径），打点函数自身永不抛。
        usage = _extract_usage(response)
        if usage is not None:
            prompt_tokens, completion_tokens, cached_tokens = usage
            record_llm_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model=self._settings.llm_model,
            )
            if prompt_tokens > 0:
                record_prompt_cache(hit=cached_tokens > 0)
        return _Attempt(text=text, status=status, retryable=False, error_type=None)

    async def _ensure_client(self) -> httpx.AsyncClient:
        """惰性建池：没人调用 LLM 时不该占着连接与文件描述符（同 tts 的惰性口径）。"""
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owned_client = True
        return self._client

    def _degraded(self, trace_id: str | None, *, reason: str) -> LLMReply:
        """降级出口：话术固定、`degraded=True`；不抛、不重试、不写库。"""
        logger.info(
            "llm degraded reply",
            extra={"trace_id": trace_id or "-", "reason": reason, "degraded": True},
        )
        return LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)


# -------------------------------------------------------------------- 纯函数


def _normalize_messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    """收敛成 `[{"role": ..., "content": ...}]`，丢掉空 content 的条目。

    为什么要清洗：调用方（编排层）会从 Redis 读历史，坏行/空行已被跳过，
    但 `system` 与当前 `user` 是现拼的，任何一个空 content 都会让部分供应商
    返回 400；与其到网络上才发现，不如在这里收敛（且不会让系统提示被静默丢掉 ——
    空 content 的 system 本来就没有信息量）。
    """
    normalized: list[dict[str, str]] = []
    for item in messages:
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if not role or not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def _extract_usage(response: httpx.Response) -> tuple[int, int, int] | None:
    """从 OpenAI 兼容响应里取 `(prompt_tokens, completion_tokens, cached_tokens)`。

    迭代 10 · W4：`/metrics` 的 `token_usage` 与 `prompt_cache_hit_rate` 靠这里供数。
    **取不到就返回 None，绝不用 0 冒充**——把「没测到」记成「0 token」会让成本曲线
    看起来比真实漂亮，比缺样本更危险。三种供应商口径都认：
    - `usage.prompt_tokens_details.cached_tokens`（OpenAI / DeepSeek）
    - `usage.prompt_cache_hit_tokens`（DeepSeek 直出头）
    - `usage.cached_tokens`（部分兼容层的扁平写法）
    """
    try:
        data: Any = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = _as_token_count(usage.get("prompt_tokens"))
    completion = _as_token_count(usage.get("completion_tokens"))
    if prompt is None and completion is None:
        return None
    details = usage.get("prompt_tokens_details")
    cached = None
    if isinstance(details, dict):
        cached = _as_token_count(details.get("cached_tokens"))
    if cached is None:
        cached = _as_token_count(usage.get("prompt_cache_hit_tokens"))
    if cached is None:
        cached = _as_token_count(usage.get("cached_tokens"))
    return (prompt or 0, completion or 0, cached or 0)


def _as_token_count(value: Any) -> int | None:
    """token 计数只接受非负整数；`True` 是 `int` 子类，必须排除。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _extract_text(response: httpx.Response) -> str | None:
    """从 OpenAI 兼容响应里取第一段 message.content；结构不符返回 None。

    三种真实会遇到的「200 但不是我们要的」：网关返回 HTML 登录页、供应商把
    `choices` 改成别的形状、`content` 是 `None`（模型只出了 tool_calls）。
    三种都当「没有可用回复」→ 降级，而不是把 HTML 当回复念给长者听。
    """
    try:
        data: Any = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    return text or None


def _perf_counter() -> float:
    """计时源。

    用 `perf_counter` 而不是 `time.monotonic`：Windows 上 `monotonic` 走
    `GetTickCount64`，粒度约 15.6ms，一次 6ms 的调用会一半记成 0、一半记成 16ms，
    P50 直接失真（同 `app/audio/reply_stub.py` 的实测结论）。`perf_counter` 是 QPC。
    """
    return perf_counter()


async def _sleep(seconds: float) -> None:
    """退避等待。抽成函数是为了单测能 monkeypatch 掉，不必真的等 0.75s。"""
    await asyncio.sleep(seconds)


def build_llm(settings: Settings | None = None) -> LLMService:
    """装配入口（与 `build_tts` / `build_asr` 同风格），供 `app/main.py` 调用。"""
    return LLMService(settings)


__all__ = [
    "DEGRADED_REPLY_TEXT",
    "RETRY_BACKOFF_SECONDS",
    "LLMReply",
    "LLMService",
    "build_llm",
]
