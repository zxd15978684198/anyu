# N31 Laya System One 服务壳 · 只做 POST /v1/classify（五字段）
#
# 构建上下文必须是**仓库根**（要 COPY app/ 拿七类唯一出处）：
#   docker build -f deploy/laya/Dockerfile -t anyu-laya:local .
#
# 本镜像不装 funasr/后端任何依赖，也不进项目的 pyproject——
# torch 只在容器里，AGENTS §4-12。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LAYA_MODEL_ROOT=/models/laya \
    LAYA_SUBFOLDER=multilingual \
    LAYA_DEVICE=cpu \
    LAYA_THREADS=4

WORKDIR /app

COPY deploy/laya/requirements.txt ./requirements.txt

# 依赖源：默认走 PyPI，但**索引通不等于能下包**——2026-11-10 本机实测容器内
# `pypi.org/simple` 要 30.6s 才回、`files.pythonhosted.org`（真正托管 wheel 的 CDN）
# 直接 45s 超时，构建会卡在这一层；同一台机器上阿里云/清华镜像 0.2s 就回。
# 所以留一个 build-arg 换源，镜像内容不变（torch 仍从 download.pytorch.org 取，
# 由 requirements.txt 里的 --extra-index-url 指定）：
#   docker build -f deploy/laya/Dockerfile -t anyu-laya:local \
#     --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple .
ARG PIP_INDEX_URL=""
RUN pip install --no-cache-dir --timeout 60 --retries 3 \
    ${PIP_INDEX_URL:+--index-url ${PIP_INDEX_URL}} -r requirements.txt

# 七类 intent 的唯一出处住在仓库 app 包里：整包拷进来，服务壳 import 的就是同一份，
# 不在镜像里另抄一张类目表（抄一份就多一处漂移点）。
COPY app/ /app/app/
COPY deploy/laya/server.py /app/server.py

# 权重由 compose 在运行时挂到 /models/laya（2.21GB，不入镜像、不入 Git）。
RUN mkdir -p /models/laya \
    && useradd --create-home --uid 10001 laya \
    && chown -R laya:laya /app

# 非 root 运行（AGENTS §5 部署审查项）；/models 由 compose 以只读方式挂载。
USER laya

EXPOSE 7001

# 加载期 /health 返回 503（status=loading），所以 start-period 给到 3 分钟：
# 本机实测加载 15s，冷容器/换页压力下按 N30 的 87s 留余量。
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=20 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7001/health', timeout=4).status == 200 else 1)"

# workers 固定 1：一次前向已经吃满 4 线程 + 1GB RSS，多 worker 只会在 3.7GiB 的 VM 上抢内存。
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7001", "--no-access-log"]
