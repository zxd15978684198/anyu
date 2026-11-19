"""按「客户端 IP + 路由桶」的内存滑动窗口限流（攻防计划 §4.6 · S3）。

## 为什么是纯内存，不是 Redis

`rate_limit_per_minute` 早在 `app/core/config.py` 就有定义，但全仓没有读取点，
结果是口令可在线爆破、匿名可刷接口。接上它本可以用现成的 Redis，但**限流不该成为
可用性依赖**：Redis 一抖，要么 fail-closed 全站 429，要么 fail-open 等于没限流。
纯内存窗口换来的是零外部依赖、O(1) 判定、进程内可测；代价写在「风险」里——
多 worker / 多副本各算各的，真实总量约为 `worker 数 × limit`。

## 三条硬性质

1. **关掉零副作用**：`rate_limit_enabled=False`，或 `app_env` 属 `{"test", "local"}`
   且未显式 `rate_limit_enforce_in_dev=True` 时，中间件直接透传——不建窗口、不读头，
   现有 1794 例门禁与本地开发完全不受影响。
2. **只管 http**：WebSocket 是长连接，没有「每分钟请求数」的语义，`scope["type"] != "http"`
   一律放行（长连接按消息数限流是另一件事，不在本条）。
3. **绝不因为限流本身把请求打成 500**：内部任何异常 fail-open，只留一行 warning。

## 键为什么是「IP + 路由桶」而不是整条 path 或整站

- 用整条 path：`/api/elders/<uuid>` 每个 id 一个额度，换个 id 就绕过，等于没限流；
- 用整站一个桶：爆破 `/api/auth/login` 打满 60 次，正常业务跟着一起 429。

桶取路径前两段（`/api/auth/login` → `/api/auth`），既保留「按接口面」的粒度，
又不会因为资源 id 不同而拆分额度。`/health`、`/metrics` 整条豁免：探针和抓取
器被限流等于把自己打成不可用。

## 429 的响应体

沿用契约 §7 的 `{"detail": str, "code": str, "trace_id": str}`，**不走异常处理器**：
`app/main.py` 的 `StarletteHTTPException` 映射表没有 429，走异常链路会漏成 `HTTP_429`。
中间件挂在内层（trace_id 已就位），所以这里能直接取到 `get_trace_id()`；
`X-Trace-Id` 响应头由外层 `TraceIdMiddleware` 补上。
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from typing import TYPE_CHECKING, Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import get_trace_id

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger("app.http.rate_limit")

# 窗口固定 60 秒：配置项就叫 `rate_limit_per_minute`，语义按分钟走，不自创可调窗口。
WINDOW_SECONDS: Final = 60.0
# 窗口字典的硬上限：超过就扫一遍过期键。纯内存方案必须有这层兜底，否则
# 「伪造海量来源 IP」能把进程内存顶满（限流器自己变成 DoS 面）。
MAX_TRACKED_KEYS: Final = 4096
# 这两类路径整条豁免：容器探针与 Prometheus 抓取器被限流 = 自己报死。
EXEMPT_PATHS: Final = frozenset({"/health", "/metrics"})
# 默认放宽的环境：单测（test）与本地开发（local）不限流，避免 1794 例门禁集体 429。
RELAXED_ENVS: Final = frozenset({"test", "local"})

RATE_LIMIT_CODE: Final = "RATE_LIMIT_EXCEEDED"
RATE_LIMIT_DETAIL: Final = "请求过于频繁，请稍后重试"
# 被拒日志的降噪窗口：同一 key 每个窗口只记一行，防止攻击者用 429 刷日志放大。
_REJECT_LOG_INTERVAL: Final = WINDOW_SECONDS


def _now() -> float:
    """单调时钟（可被测试 monkeypatch 成假时钟，用来快进窗口）。"""
    return time.monotonic()


def limiter_enabled(settings: Settings) -> bool:
    """当前配置下是否真的限流。

    `app_env` 属 test/local 时默认放宽：`tests/` 里成百上千个用例共用同一个
    `testclient` IP，一开限流就是集体 429（AGENTS §9 的「测试打穿环境」同类事故）。
    需要在这两个环境里验证限流本身时，显式置 `rate_limit_enforce_in_dev=True`。
    """
    if not getattr(settings, "rate_limit_enabled", False):
        return False
    if getattr(settings, "rate_limit_enforce_in_dev", False):
        return True
    return str(getattr(settings, "app_env", "")).strip().lower() not in RELAXED_ENVS


def is_exempt(path: str) -> bool:
    """`/health`、`/metrics`（含其子路径）豁免限流。"""
    cleaned = path.rstrip("/") or "/"
    if cleaned in EXEMPT_PATHS:
        return True
    return any(cleaned.startswith(f"{prefix}/") for prefix in EXEMPT_PATHS)


def route_bucket(path: str) -> str:
    """路径 → 限流桶：`/api/<资源>` 或首段（`/ws`、`/openapi.json` 等）。"""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "root"
    if len(parts) > 1 and parts[0] == "api":
        return f"/api/{parts[1]}"
    return f"/{parts[0]}"


def client_ip(
    scope: Scope,
    *,
    trust_forwarded_for: bool = False,
    trusted_proxies: frozenset[str] = frozenset(),
) -> str:
    """取客户端 IP。

    默认只信 `request.client.host`（直连对端，可伪造不了）。`X-Forwarded-For` 是
    **客户端可任意填写**的，只有在确认前面有一层自家反代时才允许采信：开关默认关；
    开了之后若配了 `rate_limit_trusted_proxies`，还要求直连对端就在白名单里，
    否则仍按直连 IP 计（否则攻击者自己塞一个 XFF 就能无限换身份绕过限流）。

    取 XFF 的**最右一跳**：那是最近一层反代追加的地址；更左边的值由客户端提供。
    """
    client = scope.get("client")
    peer = str(client[0]) if client else "unknown"
    if not trust_forwarded_for:
        return peer
    if trusted_proxies and peer not in trusted_proxies:
        return peer
    raw = Headers(scope=scope).get("x-forwarded-for")
    if not raw:
        return peer
    hops = [hop.strip() for hop in raw.split(",") if hop.strip()]
    return hops[-1] if hops else peer


class SlidingWindowLimiter:
    """单个窗口的滑动计数（`deque` 里只留窗口内的命中时间戳）。

    为什么是滑动窗口而不是固定窗口：固定窗口在边界处允许 2×limit（59 秒打满 60 次，
    下一秒再打满 60 次），爆破成本直接减半。

    线程模型：dict + deque 无锁，依赖「一个事件循环一个实例」——uvicorn 每个 worker
    是独立进程，实例不共享，也不会被多线程同时写。`TestClient` 的 portal 线程同样
    只有一个 loop，故测试里也成立。
    """

    def __init__(self, limit: int, window: float = WINDOW_SECONDS) -> None:
        self.limit = max(1, int(limit))
        self.window = window
        self._hits: dict[str, deque[float]] = {}

    def acquire(self, key: str, now: float) -> float | None:
        """放行返回 None；超限返回建议的 `Retry-After` 秒数（至少 0）。"""
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= MAX_TRACKED_KEYS:
                self._sweep(now)
            hits = deque(maxlen=self.limit)
            self._hits[key] = hits
        else:
            self._drop_expired(hits, now)

        if len(hits) >= self.limit:
            # 最早那次命中滑出窗口的那一刻起才有额度
            return max(0.0, self.window - (now - hits[0]))
        hits.append(now)
        return None

    def reset(self) -> None:
        self._hits.clear()

    def _drop_expired(self, hits: deque[float], now: float) -> None:
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def _sweep(self, now: float) -> None:
        for key in list(self._hits):
            hits = self._hits[key]
            self._drop_expired(hits, now)
            if not hits:
                del self._hits[key]


class RateLimitMiddleware:
    """纯 ASGI 限流中间件（不用 `BaseHTTPMiddleware`：见 `app/core/trace.py` 同款理由）。

    挂在 `TraceIdMiddleware` **内层**：外层先注入 trace_id → 429 的响应体与日志都能
    带上同一条 trace_id，且 `X-Trace-Id` 响应头由外层统一补齐（失败响应也要可追溯）。
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.enabled = limiter_enabled(settings)
        self._limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)
        self._trust_forwarded_for = bool(settings.rate_limit_trust_forwarded_for)
        self._trusted_proxies = frozenset(
            proxy.strip().lower() for proxy in settings.rate_limit_trusted_proxies
        )
        self._logged_at: dict[str, float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if is_exempt(path):
            await self.app(scope, receive, send)
            return

        try:
            now = _now()
            key = (
                f"{client_ip(scope, trust_forwarded_for=self._trust_forwarded_for, trusted_proxies=self._trusted_proxies)}"
                f"|{route_bucket(path)}"
            )
            retry_after = self._limiter.acquire(key, now)
        except Exception:  # 限流是增强件：自身故障不得升级成请求故障
            logger.warning("rate_limit_failed_open", exc_info=True)
            await self.app(scope, receive, send)
            return

        if retry_after is None:
            await self.app(scope, receive, send)
            return
        self._log_rejection(key, path, retry_after, now)
        await self._reject(send, retry_after)

    async def _reject(self, send: Send, retry_after: float) -> None:
        """直接写 429，不走异常处理器（契约 §7 要求三个键，异常链路会给 `HTTP_429`）。"""
        # 向上取整，至少 1 秒：给 0 会让客户端立刻重试，把限流变成忙等
        seconds = max(1, math.ceil(retry_after))
        body = json.dumps(
            {
                "detail": RATE_LIMIT_DETAIL,
                "code": RATE_LIMIT_CODE,
                "trace_id": get_trace_id() or "-",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"retry-after", str(seconds).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        start: Message = {"type": "http.response.start", "status": 429, "headers": headers}
        await send(start)
        await send({"type": "http.response.body", "body": body})

    def _log_rejection(self, key: str, path: str, retry_after: float, now: float) -> None:
        """同一 key 每个窗口只记一行：429 本身是攻击者可控的高频事件，不能放大成日志洪水。"""
        last = self._logged_at.get(key)
        if last is not None and now - last < _REJECT_LOG_INTERVAL:
            return
        if len(self._logged_at) >= MAX_TRACKED_KEYS:
            self._logged_at.clear()
        self._logged_at[key] = now
        logger.warning(
            "rate_limit_exceeded",
            extra={
                "rate_limit.key": key,
                "url.path": path,
                "rate_limit.retry_after": round(retry_after, 3),
                "rate_limit.limit": self._limiter.limit,
            },
        )
