# anyu 向量服务（bge-m3 int8 量化）

OpenAI 兼容的本地 embedding 服务：`POST /v1/embeddings` + `GET /health`。
后端 `HttpEmbeddingProvider` 直接就能连它，**代码零改动**。

## 为什么有这个服务

| 现状 | 问题 |
| --- | --- |
| 云端 embedding 不可用 | 代码只在启动时二选一，运行期没有重试、没有主备切换 |
| 兜底走 `DeterministicEmbeddingProvider` | SHA-256 哈希噪声，**没有语义**，只能证明链路通 |
| 唯一超上界 `EMBEDDING_TIMEOUT_MS=5000` | 语音链路端到端预算 P95 ≤1.5s，一次卡住就爆 |
| bge-m3 fp32 权重 2.27 GB、常驻约 3 GB | 本机 Docker VM 只有 3.7 GiB，常驻必被内核 OOM killer 随机杀进程（不报错） |

所以这里加载 **int8 动态量化 ONNX**（约 570 MB，常驻约 1 GB），并且**默认不常驻**。

## 三步起服务

```bash
python deploy/embedding/fetch_model.py              # 拉权重 → ./embedding-model
docker compose --profile embedding up -d embedding  # 起容器（127.0.0.1:7004）
curl -s localhost:7004/health                       # ready=true 才算好
```

然后 `.env` 里改成：

```bash
EMBEDDING_BASE_URL=http://embedding:7004/v1
EMBEDDING_MODEL=bge-m3
EMBEDDING_API_KEY=local      # 必须非空，否则后端仍走确定性兜底
```

验证一次真实调用：

```bash
curl -s localhost:7004/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":["我想听越剧"]}' \
  | python -c "import json,sys; d=json.load(sys.stdin); print(len(d['data'][0]['embedding']))"
# 期望输出：1024
```

用完关掉：`docker compose --profile embedding stop embedding`。

## 文件

| 文件 | 作用 |
| --- | --- |
| `server.py` | 服务本体：onnxruntime 推理 + CLS 池化 + L2 归一化 |
| `fetch_model.py` | 从 HF（默认镜像站）拉 `tokenizer.json` 与 `onnx/model_quantized.onnx` |
| `Dockerfile` | `python:3.12-slim` + onnxruntime，非 root，单 worker |
| `requirements.txt` | 只装在容器里，不进项目 `pyproject` |

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EMBEDDING_MODEL_DIR` | `/models/bge-m3` | 权重目录（compose 挂载点） |
| `EMBEDDING_ONNX_FILE` | `onnx/model_quantized.onnx` | 换 fp32/fp16 就改这里 |
| `EMBEDDING_TOKENIZER_FILE` | `tokenizer.json` | Rust 版 tokenizers 的唯一输入 |
| `EMBEDDING_POOLING` | `cls` | `cls`（BGE-M3 官方口径）或 `mean` |
| `EMBEDDING_MAX_TOKENS` | `512` | 截断长度 |
| `EMBEDDING_MAX_BATCH` | `32` | 单请求条数上限，超出 400 |
| `EMBEDDING_THREADS` | `2` | ONNX intra-op 线程；再多只跟后端抢核 |
| `EMBEDDING_API_KEY` | 空 | 非空则校验 `Authorization: Bearer <key>` |

## 三条不能动的口径

1. **维度 1024 写死**：与 `memory_chunks.embedding vector(1024)` 一致。模型维度变了要同时改
   数据库列、后端 `EMBEDDING_DIMENSION` 与迁移，不是改这个文件能解决的。
2. **CLS 池化 + L2 归一化**：BGE-M3 的 dense 表征就是 `last_hidden_state[:, 0]` 归一化。
   用 mean pooling 能得到向量，但**和云端算的不是一个空间**，召回会静默劣化。
3. **不常驻**：和 `funasr` / `voiceprint` 抢那 3.7 GiB；起它之前先 `docker compose stop funasr voiceprint`。

## 换模型 / 换精度

- 只要换成同维度（1024）的 bge-m3 变体：改 `EMBEDDING_ONNX_FILE` 即可（fp16 约 1.1 GB，
  本机不建议常驻）。
- 换成别的模型：先确认维度。**维度相同也不等于可以混用**——向量空间不同的两个模型算出的
  向量做余弦没有意义，而代码不会报错。
