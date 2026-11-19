# ============================================================================
# 安语（Anyu）· 后端镜像（迭代 10 · W1）
# ----------------------------------------------------------------------------
# 目标：`app/` 这套 FastAPI 服务在任意装了 Docker 的机器上起得来，且起得来之后
# 镜像本身不携带任何密钥。三条硬约束（计划 §3.1 / §6.2-3）：
#   ① 多阶段：builder 只装依赖 → runtime 只带虚拟环境与运行所需的源码目录；
#   ② 非 root：运行期进程身份是 `app`（uid/gid 1001），不是 root；
#   ③ 构建期不 COPY `.env`：上下文里由 `.dockerignore` 挡掉，镜像里由本文件「按目录
#      COPY」挡住——两条独立的防线，任何一条失效都还有另一条（不需要 `COPY . .`）。
#
# 构建：docker build -t anyu-backend .
# 注意：compose 编排（端口/网络/依赖服务）在 docker-compose.yml，属 W2 写锁，本文件不碰。
# ============================================================================

# ---------------------------------------------------------------------------
# builder：装依赖，产出一份可以直接搬到下一阶段的虚拟环境
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# uv 用官方镜像里的静态二进制，**不用 `pip install uv`**，理由三条：
#   ① 二进制由 uv 官方发布，版本直接钉死在 tag 上（0.12.24 = 本仓库本机实测版本，
#      见 AGENTS §3.1）；pip 路线要先进 pip 引导层，还多一次「pip 装出来的 uv 是不是
#      同一个东西」的不确定性。Gatekeeper 的 `pip-install` 规则同口径：构建期不跑 pip。
#   ② 不依赖 PyPI 可达性即可装工具链（镜像层自带）。
#   ③ `uv sync --locked` 会用这份 uv 校验锁文件，工具版本漂移会立刻表现为构建失败。
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /uvx /bin/

# UV_LINK_MODE=copy：容器里跨层做硬链接没有意义（/opt/venv 与缓存不同文件系统），
#   copy 让行为确定，不会因为在同一 overlay 上而变成可变状态。
# UV_COMPILE_BYTECODE=1：构建期预编译 .pyc，运行期少一次磁盘写（配 PYTHONDONTWRITEBYTECODE）。
# UV_PYTHON_DOWNLOADS=never：只用基础镜像自带的 3.12，不偷偷下载解释器。
# UV_PROJECT_ENVIRONMENT：把虚拟环境放到 /opt/venv，方便整目录 COPY 进 runtime。
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# 先只拷依赖声明：源码天天改、依赖很少改，这一步让 `uv sync` 那一层进缓存，
# 改 `app/` 不会重装 155 个包。uv.lock 必须一起进——`--locked` 靠它校验。
COPY pyproject.toml uv.lock ./

# --locked      锁文件与 pyproject 不一致直接失败（不许静默重解析出一份新解析结果）
# --no-dev      运行期不需要 pytest/ruff/mypy（[dependency-groups].dev 整组跳过）
#               uv 0.12.24 已实测支持（`uv sync --help` 里 --no-dev 与 --no-default-groups 都在）
# --no-install-project  本项目 [tool.uv] package = false，不把自身装成包
RUN uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------------------
# runtime：只带虚拟环境 + 运行所需的四个路径
# ---------------------------------------------------------------------------
# 两个阶段必须用同一个基础镜像 tag：uv 建的 venv 里 `bin/python` 是指向
# /usr/local/bin/python3.12 的符号链接；换基础镜像（哪怕换个发行版变体）会让整份 venv
# 变成「指向不存在的解释器」，且这种坏法只有在运行期 import 时才炸。
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED=1：日志立刻进容器日志，不攒在缓冲区里（结构化日志是排障前提）。
# PYTHONDONTWRITEBYTECODE=1：运行期不往源码目录写 .pyc（/app 只挂载不写）。
#   注意别把它和 builder 的 UV_COMPILE_BYTECODE 混为一谈：那一项预编译的是**依赖**（/opt/venv
#   里的 site-packages），`app/` 自己的 .pyc 从来不生成——运行期解释源码，代价可忽略。
# PATH 前置 venv：`uvicorn` / `alembic` 这些 console script 直接从 /opt/venv/bin 解析。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/home/app \
    PATH="/opt/venv/bin:$PATH"

# 非 root 的实现：固定 uid/gid 1001，用户名 app，不动 root 的任何东西。
#   --no-log-init 防止 useradd 在层里写 /var/log/btmp、/var/log/lastlog（可达几十 MB 的稀疏文件）；
#   --create-home 给一个可写 HOME（缓存/临时文件有地方落，不必借 root 的目录）。
#
# 为什么还要 apt 装 tzdata：`app/core/context.py:90` 在**模块导入期**就执行
# `ZoneInfo("Asia/Shanghai")`，而 uv.lock 里的 tzdata 带 `sys_platform == 'win32'` 标记
# （uv.lock:2837）—— Linux 容器里根本不会装它。基础镜像若没有 /usr/share/zoneinfo，
# 这个 import 会抛 ZoneInfoNotFoundError，表现为「每个请求都 500」。宁可多一层 1.5MB。
# `--no-install-recommends` + 删 apt 列表：不带 Recommends 的 locale 包，也不留元数据。
# ffmpeg：Edge TTS 走 `edge-tts` 出 mp3 → `app/audio/tts.py:141 mp3_to_pcm()` 用 ffmpeg 子进程
# 转 16k/16bit/单声道 PCM（FFMPEG_BIN="ffmpeg" 直接查 PATH）。原来只装 tzdata，导致容器里
# TTS_PROVIDER=edge 时会以 `TTSUnavailable: ffmpeg not found` 早失败、整条语音回复没有声音
# （2026-11-10 实测）。它只在 TTS 这条支路上用，不装也能跑，但演示要出声就必须有。
# espeak-ng（2026-11-11 加）：`TTS_PROVIDER=edge` 时 edge-tts 走公网，而**容器出网到
# `speech.platform.bing.com:443` 的 TLS 握手可能被拦**（实测宿主机可通、容器 100% 失败）。
# 失败后 `app/audio/tts.py::FallbackTTSProvider` 会退到 espeak-ng 的普通话音色 `cmn`：
# 音色是机器人声，但**有声音就有口型**，不至于整条语音体验断掉。约 6MB。
# 想彻底拿掉这一层，把 `FallbackTTSProvider` 换回 `EdgeTTSProvider` 并删本行即可。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ffmpeg espeak-ng \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1001 app \
 && useradd --uid 1001 --gid 1001 --no-log-init --create-home --shell /usr/sbin/nologin app

# 整份 venv 搬过来并改归属：运行期用 app 身份读 /opt/venv，不需要任何 root 权限。
COPY --from=builder --chown=app:app /opt/venv /opt/venv

WORKDIR /app

# 按目录逐个 COPY，**没有 `COPY . .`**——这是「密钥不进镜像」的第二道防线：
# 即便哪天 .dockerignore 被改坏、.env 混进了上下文，它也不会被这条 COPY 命中。
# 只带运行必需的四项：应用代码、MCP server、迁移脚本、alembic 配置。
COPY --chown=app:app app/ ./app/
COPY --chown=app:app mcp-servers/ ./mcp-servers/
COPY --chown=app:app alembic/ ./alembic/
COPY --chown=app:app alembic.ini ./

# 身份切换放在所有 COPY 之后：COPY --chown 需要 root 才能改归属。
USER app

EXPOSE 8000

# 健康检查用 python 而不是 curl：python:3.12-slim 不带 curl，为了探活 apt 装一层 curl
# 既拖慢构建又扩大攻击面；urllib 在标准库里，语义完全够（HTTP 200 = healthy）。
# /health 刻意不探 PG/Redis（app/api/health.py 模块文档），所以这个探针证明的是
# 「进程活着且能接 HTTP」，依赖就绪由 compose 的 depends_on 负责。
#
# 用 shell 形式而非 JSON 形式，只为带一个 `2>/dev/null`：服务没起时 urlopen 抛
# URLError，退出码本来就是 1（语义正确），但会往 health 日志里糊一段 traceback，
# 看起来像崩了而不是「还没就绪」。exec 形式没有重定向能力，所以这里退一步用 sh -c；
# 探针是一次性短进程，没有信号转发问题。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)" 2>/dev/null

# exec 形式 + sh -c 只为了拿 `API_HOST` / `API_PORT` 的默认值（.env.example:17-18 的变量名），
# exec 保证 uvicorn 成为 PID 1 的直接子进程，SIGTERM 能直达（容器停不干净的经典原因）。
# 端口固定 8000 与 EXPOSE、AGENTS §3 端口约定一致。
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${API_HOST:-0.0.0.0}\" --port \"${API_PORT:-8000}\""]
