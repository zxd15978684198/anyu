"""BGE-M3 int8 量化向量服务（部署件，不进项目 pyproject）。

只对外两件事：

- ``POST /v1/embeddings``：OpenAI 兼容形状，供后端 ``HttpEmbeddingProvider`` 直连
- ``GET /health``：就绪探针（加载中 503，就绪 200）

## 为什么自建而不是直接跑现成镜像

本机 Docker VM 只有 3.7 GB，bge-m3 的 fp32 权重常驻约 3 GB，**常驻必被 OOM killer
随机杀进程且不报错**；这里加载 int8 动态量化 ONNX（约 570 MB），实测常驻约 1 GB，
塞得进去。现成镜像（TEI / Ollama）默认都是 fp32 或 f16，且 Ollama 还要额外的模型仓库。

## 三条口径，改动前先读

1. **维度固定 1024**，与 ``memory_chunks.embedding vector(1024)`` 一致。模型换了维度就变，
   后端会以 ``bad_payload / bad_dimension`` 拒收——**不要在这里截断或补零**，那会让向量
   悄悄失真且永不报错。
2. **池化默认 CLS**（``EMBEDDING_POOLING=cls``）：BGE-M3 官方 dense 表征就是
   ``last_hidden_state[:, 0]`` 再归一化。改成 ``mean`` 会与云端向量不可比，召回静默劣化。
3. **归一化成单位向量**：归一化后点积即余弦，检索少一次开方。

## 为什么同步 ``def`` 而不是 ``async def``

一次前向是纯 CPU 计算，没有 await 点。写成 ``async def`` 会把整个事件循环卡住，健康
探针和其他请求一起排队；写成同步 ``def`` 时 FastAPI 自动丢进线程池。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Final

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from tokenizers import Tokenizer

logger = logging.getLogger("anyu.embedding")
logging.basicConfig(
    level=os.environ.get("EMBEDDING_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

#: 与后端 ``settings.embedding_dimension`` 及 ``vector(1024)`` 同源，不接受环境覆盖。
#: 换模型（维度变了）必须同时改数据库列与后端配置，不是这个文件能决定的事。
DIMENSION: Final[int] = 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("env_not_int", extra={"name": name, "value": raw})
        return default


MODEL_DIR: Final[str] = os.environ.get("EMBEDDING_MODEL_DIR", "/models/bge-m3")
ONNX_FILE: Final[str] = os.environ.get("EMBEDDING_ONNX_FILE", "onnx/model_quantized.onnx")
TOKENIZER_FILE: Final[str] = os.environ.get("EMBEDDING_TOKENIZER_FILE", "tokenizer.json")
POOLING: Final[str] = os.environ.get("EMBEDDING_POOLING", "cls").strip().lower()
MAX_TOKENS: Final[int] = _env_int("EMBEDDING_MAX_TOKENS", 512)
MAX_BATCH: Final[int] = _env_int("EMBEDDING_MAX_BATCH", 32)
THREADS: Final[int] = _env_int("EMBEDDING_THREADS", 2)
#: 非空时校验 ``Authorization: Bearer <key>``；留空 = 不鉴权（容器只绑 127.0.0.1）。
API_KEY: Final[str] = os.environ.get("EMBEDDING_API_KEY", "").strip()


class _State:
    """加载状态。全部字段由 ``_lock`` 保护，读侧也是（线程池里并发调用）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.session: ort.InferenceSession | None = None
        self.tokenizer: Tokenizer | None = None
        self.input_names: tuple[str, ...] = ()
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return self.session is not None and self.tokenizer is not None


STATE = _State()


def _load() -> None:
    """在线程里加载，避免把 uvicorn 的启动期拖成「连接被拒」。"""
    onnx_path = os.path.join(MODEL_DIR, ONNX_FILE)
    tok_path = os.path.join(MODEL_DIR, TOKENIZER_FILE)
    try:
        logger.info("embedding_loading", extra={"onnx": onnx_path, "tokenizer": tok_path})
        if not os.path.isfile(onnx_path):
            raise FileNotFoundError(
                f"ONNX 未找到：{onnx_path}（先跑 deploy/embedding/fetch_model.py）"
            )
        if not os.path.isfile(tok_path):
            raise FileNotFoundError(f"tokenizer 未找到：{tok_path}")

        options = ort.SessionOptions()
        options.intra_op_num_threads = THREADS
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            onnx_path, sess_options=options, providers=["CPUExecutionProvider"]
        )

        tokenizer = Tokenizer.from_file(tok_path)
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        # S106 是误报：这是 tokenizer 的 padding 占位串，不是任何口令。
        tokenizer.enable_padding(pad_id=1, pad_token="<pad>")  # noqa: S106

        names = tuple(item.name for item in session.get_inputs())
        outputs = tuple(item.name for item in session.get_outputs())

        with STATE.lock:
            STATE.session = session
            STATE.tokenizer = tokenizer
            STATE.input_names = names
            STATE.error = None
        logger.info(
            "embedding_ready",
            extra={"inputs": names, "outputs": outputs, "pooling": POOLING, "threads": THREADS},
        )
    except Exception as exc:  # 加载失败要让 /health 一直 503，而不是让进程假活
        with STATE.lock:
            STATE.error = f"{type(exc).__name__}: {exc}"
        logger.exception("embedding_load_failed")


class EmbeddingRequest(BaseModel):
    """OpenAI 兼容入参。``model`` 只做记录——本服务只加载一个模型。"""

    model: str | None = None
    input: str | list[str]


def _pool(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """把 ``[B, T, H]`` 收成 ``[B, H]``。默认 CLS，与 BGE-M3 官方 dense 表征一致。"""
    if POOLING == "mean":
        weights = mask.astype(np.float32)[..., None]
        summed = (hidden * weights).sum(axis=1)
        counts = np.clip(weights.sum(axis=1), 1e-6, None)
        return summed / counts
    return hidden[:, 0, :]


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


def encode(texts: list[str]) -> list[list[float]]:
    """文本 → 单位向量。纯函数（除读 STATE 外无副作用），便于单测替换 session。"""
    with STATE.lock:
        session = STATE.session
        tokenizer = STATE.tokenizer
        input_names = STATE.input_names
    if session is None or tokenizer is None:
        raise RuntimeError("model not ready")

    encoded = tokenizer.encode_batch(texts)
    input_ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
    attention = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)

    feeds: dict[str, np.ndarray] = {}
    if "input_ids" in input_names:
        feeds["input_ids"] = input_ids
    if "attention_mask" in input_names:
        feeds["attention_mask"] = attention
    if "token_type_ids" in input_names:
        feeds["token_type_ids"] = np.zeros_like(input_ids)

    hidden = np.asarray(session.run(None, feeds)[0])
    if hidden.ndim != 3:
        raise RuntimeError(f"unexpected output shape: {hidden.shape}")
    if hidden.shape[-1] != DIMENSION:
        raise RuntimeError(f"model outputs {hidden.shape[-1]} dims, expected {DIMENSION}")

    vectors = _normalize(_pool(hidden, attention)).astype(np.float64)
    return [row.tolist() for row in vectors]


app = FastAPI(title="anyu-embedding", version="1.0.0")
_thread = threading.Thread(target=_load, name="embedding-load", daemon=True)
_thread.start()


@app.get("/health")
def health() -> dict[str, object]:
    """就绪探针：加载期返回 503，Docker healthcheck 据此判 unhealthy。"""
    with STATE.lock:
        ready = STATE.ready
        error = STATE.error
    payload: dict[str, object] = {
        "status": "ok" if ready else "loading",
        "ready": ready,
        "model_dir": MODEL_DIR,
        "onnx": ONNX_FILE,
        "dimension": DIMENSION,
        "pooling": POOLING,
    }
    if error:
        payload["status"] = "error"
        payload["error"] = error
    return payload


@app.post("/v1/embeddings")
def embeddings(body: EmbeddingRequest) -> dict[str, object]:
    """OpenAI 兼容 ``POST /v1/embeddings``。"""
    if STATE.error:
        raise HTTPException(status_code=503, detail=STATE.error)
    if not STATE.ready:
        raise HTTPException(status_code=503, detail="model loading")

    raw = body.input
    texts = [raw] if isinstance(raw, str) else list(raw)
    if not texts:
        raise HTTPException(status_code=400, detail="input must not be empty")
    if len(texts) > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"input size > {MAX_BATCH}")
    if any(not isinstance(item, str) for item in texts):
        raise HTTPException(status_code=400, detail="input must be str or list[str]")

    try:
        vectors = encode(texts)
    except Exception as exc:  # 计算失败是服务端问题，不当成客户端错误
        logger.exception("embedding_encode_failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    tokens = sum(len(item) for item in texts)
    logger.info("embedding_served", extra={"batch": len(texts), "chars": tokens})
    return {
        "object": "list",
        "model": body.model or os.environ.get("EMBEDDING_MODEL_NAME", "bge-m3"),
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
