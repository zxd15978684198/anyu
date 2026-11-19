"""Embedding 供应商：协议 + OpenAI 兼容 HTTP + 确定性兜底（迭代 7 · 计划 §P1）。

## 为什么单独一个文件

`memory_chunks.embedding vector(N)`（技术设计文档 §3.4）需要一个「把文本变成 N 维
向量」的能力（N 当前为 1024，见 `settings.embedding_dimension`），但仓库此前**没有任何
embedding 客户端**（计划 §P1 的最大阻塞）。本模块只做
这一件事，且让 `memory_service` **只依赖 `EmbeddingProvider` 协议** —— 换供应商、换维度、
换降级策略都只改这里，五个记忆函数一行不动（AGENTS §4.8 一能力一形态）。

## 三种形态与边界

| 形态 | 何时用 | 能不能当召回质量证据 |
| --- | --- | --- |
| `HttpEmbeddingProvider` | 配了 `embedding_base_url` + `embedding_model` + `embedding_api_key` | 能（真实语义向量） |
| `DeterministicEmbeddingProvider` | 未配 key 的本地开发 / 联调 / 断点恢复演示 | **不能**，见下 |
| `build_embedding_provider()` | 唯一的构造入口，二选一 | — |

## S-D7-1 · 确定性兜底的红线（写进代码，防止后来者误读）

`DeterministicEmbeddingProvider` 用 SHA-256 把文本散列成 N 维单位向量：**同一个句子
永远得到同一条向量，不同句子几乎正交**。它能让「写入 → 存向量 → 召回 → 更新
`last_accessed`」整条链路跑通，但它**没有任何语义**——
「我喜欢越剧」和「我讨厌越剧」在它眼里毫不相干。

因此：**联调兜底、非语义检索、不能作为召回质量证据**。
不得拿它的召回结果写「记忆召回准」「相似度 0.93 说明模型懂老人」这类结论；
真实的召回质量评估必须先把 key 配好、走 `HttpEmbeddingProvider`。

## 四类失败统一成 `EmbeddingUnavailable(reason)`

reason ∈ `timeout | network | bad_status | bad_payload | not_configured`
（`bad_payload` 也覆盖「返回维度 ≠ 期望维度」，因为对上游来说那是同一件事：这次响应不可用）。
不抛 httpx 原生异常：调用方（`memory_service`）只需要知道「这次能不能拿到向量」，
不需要区分 `ReadTimeout` 与 `ConnectTimeout`——区分了也没有不同的处置。
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Final, Literal, Protocol, runtime_checkable

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger("app.services.embedding")

#: 默认向量维度，与 `settings.embedding_dimension` 同源（技术设计文档 §3.4）。
#: 真实值由 `build_embedding_provider` 从配置读取后构造器注入；这里只是「没配置时的兜底」。
#: 供应商返回别的维度一律按 `bad_payload` 处理，**不做截断/补零**——那会让向量悄悄失真。
EMBEDDING_DIMENSION: Final[int] = 1024

#: `/embeddings` 是 OpenAI 兼容口径的固定路径，只拼在构造器注入的 base_url 后面。
EMBEDDING_PATH: Final[str] = "/embeddings"

#: 构造器默认超时（毫秒）。真实值由 `settings.embedding_timeout_ms` 注入，这里只是兜底。
DEFAULT_TIMEOUT_MS: Final[int] = 5000

EmbeddingFailureReason = Literal[
    "timeout",
    "network",
    "bad_status",
    "bad_payload",
    "not_configured",
]


class EmbeddingUnavailable(RuntimeError):
    """一次 embedding 调用失败。`reason` 是给打点与日志用的**封闭枚举**，不是自由文本。"""

    def __init__(self, reason: EmbeddingFailureReason, detail: str | None = None) -> None:
        self.reason: EmbeddingFailureReason = reason
        self.detail = detail
        super().__init__(f"embedding unavailable: {reason}" + (f" ({detail})" if detail else ""))


@runtime_checkable
class EmbeddingProvider(Protocol):
    """`memory_service` 唯一认得的 embedding 能力（结构类型，不绑实现）。"""

    @property
    def dimension(self) -> int:
        """向量维度；`memory_chunks.embedding` 是 `vector(N)`。"""
        ...

    @property
    def available(self) -> bool:
        """当前进程**此刻**能不能真的产出向量（未配 key / 容器没起时为 False）。"""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量取向量，顺序与 `texts` 严格一致；失败抛 `EmbeddingUnavailable`。"""
        ...


# ------------------------------------------------------------------ HTTP 实现


class HttpEmbeddingProvider:
    """OpenAI 兼容 `POST {base_url}/embeddings`（DeepSeek / Qwen / 本地 vLLM 同一套）。

    base_url / model / api_key **全部构造器注入**：本文件不读 `.env`、不出现任何供应商
    域名字面量（AGENTS §4.2 密钥红线、§4.12 不擅自升级依赖）。测试注入
    `httpx.MockTransport` 就能覆盖四类失败，不起真实网络。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        dimension: int = EMBEDDING_DIMENSION,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._base_url = base_url.strip().rstrip("/")
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._timeout_ms = int(timeout_ms)
        self._dimension = int(dimension)
        self._client = client
        self._owned_client = False

    @property
    def dimension(self) -> int:
        """期望维度（来自配置）；与 `memory_chunks.embedding` 的 `vector(N)` 必须相等。"""
        return self._dimension

    @property
    def available(self) -> bool:
        """三项配置缺一即不可用：缺 base_url 不知打哪、缺 model 不知用谁、缺 key 打不开门。"""
        return bool(self._base_url and self._model and self._api_key)

    async def aclose(self) -> None:
        """只关自己建的连接池；外部注入的 client 由注入方负责（同 `llm_service` 口径）。"""
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owned_client = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量取向量；空入参直接返回空列表（不发请求）。"""
        if not texts:
            return []
        if not self.available:
            raise EmbeddingUnavailable("not_configured")

        client = self._ensure_client()
        payload = {"model": self._model, "input": list(texts)}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{self._base_url}{EMBEDDING_PATH}"

        try:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout_ms / 1000.0,
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingUnavailable("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            # 连接被拒 / DNS 失败 / 协议错 / 中途断流：对调用方都是「网络不可用」。
            raise EmbeddingUnavailable("network", type(exc).__name__) from exc

        if response.status_code != 200:
            raise EmbeddingUnavailable("bad_status", str(response.status_code))

        return self._parse(response, expected=len(texts))

    def _ensure_client(self) -> httpx.AsyncClient:
        """惰性建池：没人调 embedding 时不该占着连接与文件描述符。"""
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owned_client = True
        return self._client

    def _parse(self, response: httpx.Response, *, expected: int) -> list[list[float]]:
        """解析 OpenAI 兼容响应体；任何一处不对都归 `bad_payload`，绝不猜。"""
        try:
            data = response.json()
        except Exception as exc:  # 非 JSON / 截断的 body 都算坏载荷
            raise EmbeddingUnavailable("bad_payload", "not_json") from exc

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != expected:
            raise EmbeddingUnavailable("bad_payload", "bad_items")

        vectors: list[list[float] | None] = [None] * expected
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                raise EmbeddingUnavailable("bad_payload", "bad_item")
            raw = item.get("embedding")
            if not isinstance(raw, list) or len(raw) != self.dimension:
                # detail 保持 `bad_dimension` 字面量（打点/测试按它聚合），实际维度只进日志：
                # 「配置 1536、模型回 1024」这种事故，唯一能定位的就是这一行。
                logger.warning(
                    "embedding_dimension_mismatch",
                    extra={
                        "expected": self.dimension,
                        "got": len(raw) if isinstance(raw, list) else None,
                        "model": self._model,
                    },
                )
                raise EmbeddingUnavailable("bad_payload", "bad_dimension")
            # 供应商明确回了 index 就按它排；没回则按数组顺序（两种都要支持，
            # 否则一个「省略 index」的兼容实现会让所有向量错位）。
            index = item.get("index", position)
            if not isinstance(index, int) or not 0 <= index < expected:
                raise EmbeddingUnavailable("bad_payload", "bad_index")
            if vectors[index] is not None:
                raise EmbeddingUnavailable("bad_payload", "duplicate_index")
            vectors[index] = self._as_finite_vector(raw)

        if any(vector is None for vector in vectors):
            raise EmbeddingUnavailable("bad_payload", "missing_index")
        return [vector for vector in vectors if vector is not None]

    def _as_finite_vector(self, raw: list[object]) -> list[float]:
        """元素必须是有限实数：NaN/inf 会让 pgvector 的余弦距离整列失真。"""
        values: list[float] = []
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise EmbeddingUnavailable("bad_payload", "non_numeric")
            value = float(item)
            if value != value or value in (float("inf"), float("-inf")):
                raise EmbeddingUnavailable("bad_payload", "non_finite")
            values.append(value)
        return values


# ------------------------------------------------------------------ 确定性兜底


class DeterministicEmbeddingProvider:
    """SHA-256 派生 N 维**单位**向量 —— **联调兜底、非语义检索、不能作为召回质量证据**。

    用途只有三个：没配 key 时让写库/召回链路能跑、给断点恢复演示一个稳定向量、给
    纯逻辑测试一个不依赖网络的 provider。它的语义能力是**零**：
    - 同一句话 → 同一条向量（这是它唯一「正确」的性质，用来对齐联调结果）；
    - 不同句子 → 近似正交（SHA-256 输出噪声，没有词义结构）；
    - 「喜欢越剧」与「讨厌越剧」的相似度与任意两句话没有区别。

    因此**不得**用它产出任何「召回准确率/相似度阈值有效」的结论（计划 S-D7-1 红线）。
    """

    def __init__(self, *, dimension: int = EMBEDDING_DIMENSION, salt: str = "anyu") -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._dimension = int(dimension)
        self._salt = salt

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def available(self) -> bool:
        """本地纯计算，永不「不可用」——所以任何降级分支都不会因为它而触发。"""
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._unit_vector(text) for text in texts]

    def _unit_vector(self, text: str) -> list[float]:
        """取 SHA-256 摘要流的前 N 字节当分量，映射到 [-1, 1] 后做 L2 归一化。"""
        # 每次摘要出 32 字节；1024 维需要 32 次摘要（最后一块可能只用一部分）。
        raw = bytearray()
        block = 0
        while len(raw) < self._dimension:
            digest = hashlib.sha256(f"{self._salt}:{block}:{text}".encode()).digest()
            raw.extend(digest)
            block += 1

        values = [(byte / 255.0) * 2.0 - 1.0 for byte in raw[: self._dimension]]
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:  # pragma: no cover - N 个字节恰好全等于 127.5 才有此可能
            raise AssertionError("deterministic embedding produced a zero vector")
        return [value / norm for value in values]


# ------------------------------------------------------------------ 构造入口


def build_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """按配置二选一：四项配齐 → Http；否则 Deterministic + warning。

    四项指 `embedding_base_url` / `embedding_model` / `embedding_api_key` /
    `embedding_timeout_ms`，外加 `embedding_dimension`（2026-11-11 起可配，默认 1024）。
    本节用 `getattr` 兜底，字段一落地即生效。**为什么不回落 `llm_api_key`**：LLM 供应商
    通常不提供 `/embeddings`，拿聊天 key 打 embedding 只会得到 404 与一串误导性告警。

    降级时记一条 `embedding_provider_degraded` warning（键名固定，运维按它做告警规则）：
    降级**不阻断**记忆写入，但写入的向量没有语义，召回质量必须另外评估。
    """
    config = settings if settings is not None else get_settings()
    base_url = str(getattr(config, "embedding_base_url", "") or "").strip()
    model = str(getattr(config, "embedding_model", "") or "").strip()
    api_key = str(getattr(config, "embedding_api_key", "") or "").strip()
    timeout_ms = int(
        getattr(config, "embedding_timeout_ms", DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS
    )
    dimension = int(
        getattr(config, "embedding_dimension", EMBEDDING_DIMENSION) or EMBEDDING_DIMENSION
    )
    if dimension <= 0:
        raise ValueError("embedding_dimension must be positive")

    if base_url and model and api_key:
        return HttpEmbeddingProvider(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_ms=timeout_ms,
            dimension=dimension,
        )

    missing = [
        name
        for name, value in (
            ("embedding_base_url", base_url),
            ("embedding_model", model),
            ("embedding_api_key", api_key),
        )
        if not value
    ]
    logger.warning(
        "embedding_provider_degraded",
        extra={"missing": ",".join(missing), "provider": "deterministic", "dimension": dimension},
    )
    return DeterministicEmbeddingProvider(dimension=dimension)


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_PATH",
    "DeterministicEmbeddingProvider",
    "EmbeddingFailureReason",
    "EmbeddingProvider",
    "EmbeddingUnavailable",
    "HttpEmbeddingProvider",
    "build_embedding_provider",
]
