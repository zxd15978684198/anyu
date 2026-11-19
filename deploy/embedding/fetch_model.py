"""把 bge-m3 的 int8 ONNX 权重与 tokenizer 下到 ``./embedding-model``。

容器只挂这一个目录（``EMBEDDING_MODEL_DIR:/models/bge-m3:ro``），所以**先跑本脚本、再起容器**；
容器内不联网、不做下载，避免每次重启都在等 HuggingFace。

    python deploy/embedding/fetch_model.py            # 默认走 hf-mirror.com
    python deploy/embedding/fetch_model.py --endpoint https://huggingface.co
    python deploy/embedding/fetch_model.py --dir D:/models/bge-m3

只拉两个文件：

- ``onnx/model_quantized.onnx``：int8 动态量化，约 570 MB（fp32 是 2.27 GB，本机塞不下）
- ``tokenizer.json``：Rust 版 tokenizers 的唯一输入，不需要整条 transformers 依赖
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

DEFAULT_REPO = "Xenova/bge-m3"
#: 官方站直连在大陆常见 45s 级超时，镜像通常 0.2s 内响应；要换回来用 --endpoint 覆盖。
DEFAULT_ENDPOINT = "https://hf-mirror.com"
FILES: tuple[str, ...] = ("tokenizer.json", "onnx/model_quantized.onnx")
#: 粗略下限，用来识别「下了一半就断」的残缺文件（真实 570 MB，留 5% 余量）。
MIN_ONNX_BYTES = 500 * 1024 * 1024


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 bge-m3 int8 ONNX 与 tokenizer")
    parser.add_argument(
        "--dir", default="embedding-model", help="落地目录（默认 ./embedding-model）"
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"HF 仓库（默认 {DEFAULT_REPO}）")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT),
        help=f"HF 端点（默认 {DEFAULT_ENDPOINT}）",
    )
    args = parser.parse_args(argv)

    target = Path(args.dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] repo     = {args.repo}")
    print(f"[fetch] endpoint = {args.endpoint}")
    print(f"[fetch] target   = {target}")

    for name in FILES:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and destination.stat().st_size > 0:
            print(f"[skip] {name} 已存在（{_human(destination.stat().st_size)}）")
            continue
        print(f"[get ] {name} …")
        cached = hf_hub_download(
            repo_id=args.repo,
            filename=name,
            endpoint=args.endpoint,
            local_dir=str(target),
        )
        size = Path(cached).stat().st_size
        print(f"[ok  ] {name} → {_human(size)}")
        if name.endswith(".onnx") and size < MIN_ONNX_BYTES:
            print(
                f"[warn] {name} 只有 {_human(size)}，疑似未下完（应为 ~570 MB）；删掉后重跑本脚本",
                file=sys.stderr,
            )
            return 2
    print("[done] 可以起容器了：docker compose --profile embedding up -d embedding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
