"""`deploy/embedding/server.py` 的纯函数契约（不需要真实模型文件）。

`onnxruntime` 只装在 `deploy/embedding` 镜像里，本机 `.venv` 没有 → 本模块整体 skip。
要真跑：

    docker compose --profile embedding run --rm embedding \\
        python -m pytest -q /app/tests_placeholder

（镜像里没有 tests/，实际跑法是把本文件挂进去，见 `deploy/embedding/README.md`。）

守的是三条口径，不是覆盖率：
- 池化取 [CLS]（BGE-M3 官方 dense 表征），不是 mean
- 归一化后每个向量长度为 1（pgvector 余弦才等价于点积）
- 维度 1024 与 `memory_chunks.embedding vector(1024)` 一致
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

pytest.importorskip("onnxruntime", reason="向量服务依赖只装在 deploy/embedding 镜像里")

if TYPE_CHECKING:  # 只为类型检查器认识 `server`，运行期由下面的 sys.path 注入
    import server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "embedding"))
import server


def test_dimension_matches_vector_column() -> None:
    """维度写死 1024：改了必须同时改数据库列与后端配置。"""
    assert server.DIMENSION == 1024


def test_cls_pooling_takes_first_token() -> None:
    hidden = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    mask = np.ones((2, 3), dtype=np.int64)

    pooled = server._pool(hidden, mask)

    assert pooled.shape == (2, 4)
    np.testing.assert_array_equal(pooled, hidden[:, 0, :])


def test_mean_pooling_respects_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """padding 位置不能算进均值——否则同一句话会因批次里别人更长而变向量。"""
    monkeypatch.setattr(server, "POOLING", "mean")
    hidden = np.array(
        [
            [[1.0, 0.0], [3.0, 0.0], [100.0, 0.0]],
            [[2.0, 2.0], [4.0, 4.0], [6.0, 6.0]],
        ],
        dtype=np.float32,
    )
    mask = np.array([[1, 1, 0], [1, 1, 1]], dtype=np.int64)

    pooled = server._pool(hidden, mask)

    np.testing.assert_allclose(pooled, [[2.0, 0.0], [4.0, 4.0]])


def test_normalize_produces_unit_vectors() -> None:
    vectors = np.array([[3.0, 4.0], [0.0, 5.0]], dtype=np.float32)

    normalized = server._normalize(vectors)

    np.testing.assert_allclose(np.linalg.norm(normalized, axis=1), [1.0, 1.0], rtol=1e-6)


def test_normalize_survives_zero_vector() -> None:
    """全零向量（理论上不该出现）不能变成 NaN——pgvector 的余弦会整列失真。"""
    normalized = server._normalize(np.zeros((1, 4), dtype=np.float32))

    assert np.isfinite(normalized).all()


def test_request_accepts_str_and_list() -> None:
    assert server.EmbeddingRequest(input="一句话").input == "一句话"
    assert server.EmbeddingRequest(input=["a", "b"]).input == ["a", "b"]


def test_request_model_is_optional() -> None:
    assert server.EmbeddingRequest(input="x").model is None
