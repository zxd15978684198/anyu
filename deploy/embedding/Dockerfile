# anyu 向量服务 · bge-m3 int8 量化（OpenAI 兼容 /v1/embeddings）
#
# 构建上下文是 deploy/embedding 本身（不需要仓库其他代码）：
#   docker compose --profile embedding build embedding
# 或不走 compose：
#   docker build -t anyu-embedding:local deploy/embedding
#
# 权重**不打进镜像**：compose 把宿主机的 ./embedding-model 只读挂到 /models/bge-m3。
# 理由：570 MB 的二进制进镜像层后，每次改一行 server.py 都要重推 570 MB。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    EMBEDDING_MODEL_DIR=/models/bge-m3 \
    EMBEDDING_ONNX_FILE=onnx/model_quantized.onnx \
    EMBEDDING_POOLING=cls \
    EMBEDDING_THREADS=2

WORKDIR /app

COPY requirements.txt ./requirements.txt

# 依赖源：与 deploy/laya/Dockerfile 同一套理由——PyPI 索引能通不代表 wheel 的 CDN 能下，
# 留一个 build-arg 换镜像源，镜像内容不变。
#   docker build -t anyu-embedding:local --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple deploy/embedding
ARG PIP_INDEX_URL=""
RUN pip install --no-cache-dir --timeout 60 --retries 3 \
    ${PIP_INDEX_URL:+--index-url ${PIP_INDEX_URL}} -r requirements.txt

COPY server.py fetch_model.py /app/

# 非 root 运行（AGENTS §5 部署审查项）。/models 由 compose 以只读方式挂载，
# 这里的 chown 只对镜像里那个空目录生效，不改变宿主机文件权限。
RUN useradd --create-home --uid 10001 embedding \
    && mkdir -p /models/bge-m3 \
    && chown -R embedding:embedding /app /models

USER embedding

EXPOSE 7004

# 加载期 /health 返回 503（status=loading/error），urlopen 抛 HTTPError → 非零退出 → unhealthy。
# 本机实测 int8 加载 5~15s；冷容器 + 换页压力按 2 分钟留足。
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=20 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7004/health', timeout=4).status == 200 else 1)"

# workers 固定 1：一次批量前向已经用满 2 线程，多 worker 只会在 3.7GiB 的 VM 上抢内存。
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7004", "--no-access-log"]
