# 安语（Anyu）· 3D 数字人康养陪伴系统

> 一间长者卧室里的「会说话的屏」：浏览器页面上的 VRM 数字人，听得懂方言语音，会陪着聊天、提醒吃药、每晚问一遍身体状况、放一段戏曲，遇到急症立刻叫人。

> **最近核对：2026-11-19**　后端 31 条路由 · `public` schema 30 张业务表 · 后端用例 3481 passed / 1 skipped · 前端用例 738（60 文件）。
> 数字只保证该日成立，复验命令见 [§7.2](#72-复现与门禁命令)。
>
> 快速跳转：[快速开始](#5-快速开始) · [架构](#3-架构总览) · [质量门](#7-质量门与验收) · [文档](#10-文档)

---

## 1. 背景与目标

CCRC 康养社区的长者卧室普遍只有一块平板。安语把这块平板变成一位**有形象、有声音、有记忆的陪伴者**，同时把照护过程中最容易漏掉的三件事（用药确认、健康采集、急症预警）变成可追溯的数据。

系统同时服务三类使用者：

| 端 | 使用者 | 界面口径 |
| --- | --- | --- |
| `pad` 长者端 | 长者本人 | 1920×1080 全屏、大字大按钮、字幕正文 ≥20px、触控区 ≥48×48px |
| `console` 照护台 | 护工 / 医生 / 管理员 | 按角色分流：护工看提醒与工单，医生维护药单与趋势图，管理员管人员与设备 |
| `family` 家属端 | 长者家属 | 375px 移动优先、无横向滚动、正文 ≥16px |

**工程目标**：端到端语音往返 P95 ≤ 1.5s；≥60% 的请求在快决策层闭环；方言语音仍可用。

> ⚠️ **无真实平板硬件**：全部前端都是 HTML 页面，用浏览器全屏模拟平板界面。这是本项目的既定约束，不是临时方案。

---

## 2. 业务边界（11 条非目标，逐条硬性约束）

产品能力的扩张以不越过下面 11 条为界。**任何越过边界的实现都视为缺陷，不是功能。**

| # | 不做 | 边界说明 |
| --- | --- | --- |
| 1 | **不做医疗诊断** | 绝不说「您这是高血压 / 糖尿病」。异常值只说「这个数值偏高，建议问问医生」，同时触发人工告警 |
| 2 | **不替代 SOS 紧急呼叫** | 急症走「提醒护工」通道，不承诺替代 120 或社区应急系统 |
| 3 | **不做高精度 3D 建模 / 动捕** | 用现成 VRM 形象 + 音频能量近似口型，不追求影视级驱动 |
| 4 | **不做 AI 歌曲生成** | 早期方案里的 yue2 已删除，只做点播 |
| 5 | **不依赖商业 Jev API** | System One 用本地开源权重（Laya），不付费调商业意图分类服务 |
| 6 | **不做盗版曲库 / 影库** | 仅使用 20–30 条授权样例，不爬取、不抓取、不内置来源不明内容 |
| 7 | **不接支付 / 采购 / 门禁** | 与社区商业系统无集成 |
| 8 | **不存完整病历与原始录音** | 只存结构化摘要 + 脱敏文本；原始音频最长 72 小时，由定时任务自动删除 |
| 9 | **不做移动端原生 App** | 家属端是移动优先的 Web 页面 |
| 10 | **不做多租户 SaaS** | 单社区部署，不做 SaaS 化改造 |
| 11 | **不做完整 diarization** | 只做「当前说话人是否本人」的声纹判定，不做多人分离转写 |

补充（来自 PRD）：不接真实 HIS / 医保 / 医院 / 急救 / 微信；不做生产级声纹认证与视觉识别；长期记忆**仅本人可见**，不做公私共享。

---

## 3. 架构总览

### 3.1 一次对话的链路

```
浏览器（pad / console / family，HTML 模拟平板）
   │  语音帧 ▲ PCM 音频轨
   ▼        │
FastAPI（WebSocket 信令 SDP/ICE + aiortc 媒体）
   │
   ├─► 流式 ASR ──► 文本 + 置信度
   │
   ├─► ① 规则层（safety 一票否决 + ≤10 条固定意图）        零延迟，命中即返回
   │        └─ 未命中 ▼
   ├─► ② System One：Laya 本地 CPU 分类器                    单句 ≤200ms
   │        ├─ confidence ≥ 0.85 → 直接路由到能力
   │        └─ < 0.85 → 仅澄清一次 → 仍模糊则下沉
   │
   └─► ③ System Two：主控 Agent（ReAct + Tool Calling）      开放域兜底
            ├─ LangGraph 子图：用药确认 / 健康采集（带 HITL 与断点恢复）
            ├─ MCP Tool：get_weather / create_work_order / send_family_notification
            └─ 预算 MAX_TOOL_CALLS=8，同 Tool 1 分钟 3 次熔断
   │
   ▼
流式 TTS ──► WebRTC 音频轨回放 + 页面大字幕（字幕必须与语音内容一致）
```

### 3.2 分层与技术栈

| 层 | 内容 | 技术 |
| --- | --- | --- |
| 交互层 | 三端 HTML 页面、3D 形象、字幕、提醒卡片 | React + Vite（MPA）、Tailwind（pad）/ 手写 CSS + tokens（console、family，不引组件库）、three.js + three-vrm |
| 语音层 | WebRTC 全双工、反向提醒外呼、降级上传 | aiortc、coturn、webrtcvad、第三方流式 ASR/TTS |
| 决策层 | 规则层 → System One → System Two | 纯代码规则、Laya（CPU）、DeepSeek / Qwen 兼容 API |
| 编排层 | 主控 Agent、两个业务子图、MCP 工具 | LangGraph + PostgreSQL Checkpointer、MCP |
| 数据层 | 业务表、记忆向量、checkpoint、缓存 | PostgreSQL 15 + pgvector、Redis |
| 运维层 | 容器、观测、评测门禁 | Docker Compose、Langfuse + OpenTelemetry + `/metrics`、pytest |

> 选型理由与逐项口径见 [`docs/安语3D数字人_技术设计文档.md`](docs/安语3D数字人_技术设计文档.md) §1.4；工程约束与禁令见 [`AGENTS.md`](AGENTS.md)。

---

## 4. 目录结构

```
.
├── app/              # 后端唯一代码根：core / api / audio / router / system_one / agents / graphs / services / models / mcp_client / scheduler / prompts
├── mcp-servers/      # weather（真实 API + 缓存）、mock-operations（工单 / 家属通知落库）
├── frontend/         # MPA 四入口：index.html / console.html / family.html / portal.html + src/{pad,console,family,portal,shared}
├── alembic/versions/ # 分批迁移，已合入的主迁移不再改
├── eval/             # fixtures（固定测试集）+ reports（基线与 bad case 回流）
├── tests/            # 后端用例 + eval_gate.py / test_injection.py / test_gates.py
├── scripts/          # 门禁、迁移辅助、资产同步、账号初始化
├── spikes/           # 新依赖最小验证，跑通才合并
└── docs/             # PRD / 技术设计 / ER / 契约 / 交付文档（索引见 §10）
```

完整目录语义与**分层硬约束**（`services` 是唯一写库层、模块间不得横向 import、three-vrm 不得进 console/family 产物）见 [`AGENTS.md`](AGENTS.md) §2。

---

## 5. 快速开始

### 5.1 前置工具

| 工具 | 用途 | 备注 |
| --- | --- | --- |
| Python 3.12（要求 3.12+） | 后端运行时 | `.python-version` / `requires-python` / ruff / mypy 四处统一锁 3.12 |
| [uv](https://docs.astral.sh/uv/) | 唯一的 Python 包管理器 | 不要混用 pip / conda，本仓库 `.venv` 由 uv 管理 |
| Docker + Docker Compose | 起 PG / Redis 等依赖 | 一键全栈见 §5.2-A |
| Node.js + pnpm | 前端构建 | 四入口共用一套 Vite 配置（pad / console / family / portal） |
| mkcert（可选） | 本地 HTTPS 证书 | 麦克风在非 localhost 下必须 HTTPS |

> **Windows 不需要装 Microsoft C++ Build Tools**。VAD 依赖用的是 `webrtcvad-wheels`（上游 `webrtcvad` 的同源 fork，import 名与 API 完全不变），自带 cp310–cp314 预编译 wheel，`uv sync` 直接取二进制。若改回上游 `webrtcvad`，Windows 上会因缺少 C++ 编译器而 `uv sync` 失败。

### 5.2 A · 一键全栈（`https://localhost`，含 HTTPS 与健康检查）

```bash
uv sync                                     # 本机跑门禁/迁移需要 .venv
cp .env.example .env                        # 至少填 LLM_API_KEY、ASR_*/TTS_*、WEATHER_API_KEY、TURN_CREDENTIAL
pnpm --dir frontend install
pnpm --dir frontend build                   # nginx 直出的是构建产物，必须先 build 一次
uv run python scripts/gen_dev_cert.py       # 生成 certs/dev.{crt,key}（自签、不入库，只需一次）
docker compose up -d --build                # 七个必需服务 + funasr/voiceprint（实起 9 个容器），全部带 healthcheck
docker compose ps                           # 等 backend 变 healthy
uv run alembic upgrade head                 # 建表（容器起好后在宿主机跑：容器内跑会用到镜像烘焙的旧版本）
```

> **HTTPS 是默认**：`NGINX_CONF_PATH` 缺省指向 `frontend/nginx.https.conf`。缺 `certs/` 时 frontend 容器会启动失败——这是刻意的，不允许「端口绿了但其实是 HTTP」；要临时退回 HTTP 用 `NGINX_CONF_PATH=./frontend/nginx.conf docker compose up -d`。

演示数据与账号：

```bash
uv run python scripts/seed_demo.py          # 演示长者 / 设备 / 员工 / 家属账号
uv run python scripts/seed_acceptance.py    # 验收场景数据
```

| 入口 | 角色 | 账号 | 密码 |
| --- | --- | --- | --- |
| `/console.html` | 管理员 | 13700000000 | `Admin@2026` |
| `/console.html` | 护工 | 13800000002 | `Care@2026` |
| `/console.html` | 医生 | 13600000001 | `Doc@2026` |
| `/family.html` | 家属 | 13900000002 | `Fam@2026` |
| `/`（pad） | 长者端 | 设备令牌（由后端下发） | — |

> 以上是**公开演示账号**，仅存在于本地演示库；不是任何真实凭据。对照表见 [`docs/demo-accounts.md`](docs/demo-accounts.md)。

### 5.3 B · 本地开发（基础组件走 Docker，前后端各一个终端）

当前 `docker-compose.yml` 编排的是**完整七服务**，默认另有 `funasr` / `voiceprint` 随栈起（共 9 个容器）；`celery-worker` / `celery-beat` 走 `--profile celery`、`laya` 走 `--profile laya`，缺省 `up` 不起。本地开发只起基础组件即可，前后端各起一个终端，改代码即时生效。

**终端 A · 后端（FastAPI，:8000）**

```bash
uv sync
cp .env.example .env          # 已有 .env 可跳过；至少填 LLM_API_KEY、ASR_*/TTS_*、WEATHER_API_KEY

docker compose up -d postgres redis
uv run alembic upgrade head

uv run uvicorn app.main:app --reload --port 8000
```

```bash
curl -s localhost:8000/health   # {"status":"ok", ...}，响应头带 X-Trace-Id
```

接口文档在 `http://localhost:8000/docs`。`/health` 刻意不探库：没有 `.env`、没起 PG/Redis 也能 200。

**终端 B · 前端（Vite，:5173）**

```bash
pnpm --dir frontend install     # 首次，或 package.json / pnpm-lock.yaml 变更后
pnpm --dir frontend dev         # 开发服务器，strictPort: true，端口被占会直接报错退出
pnpm --dir frontend build       # tsc --noEmit + MPA 构建，产物 frontend/dist/
pnpm --dir frontend preview     # 本地预览构建产物
```

| 项 | 说明 |
| --- | --- |
| 页面入口 | MPA 四入口（以 `frontend/vite.config.ts` 的 `rollupOptions.input` 为准）：`pad` = `/`（长者端，设备语义，不占位给其他端）、`console` = `/console.html`、`family` = `/family.html`、`portal` = `/portal.html`（演示门户，PRD 外的工程辅助页） |
| 接口代理 | `vite.config.ts` 把 `/api` → `http://localhost:8000`、`/ws` → `ws://localhost:8000`；**先起后端再起前端**，页面里不用配 API 地址，也不触发 CORS |
| HTTPS | 本机 `localhost:5173` 可直接使用麦克风；跨机联调需 mkcert 本地证书，否则 `getUserMedia` 不可用 |
| 构建期变量 | **无**：前端不读任何 `VITE_*`，接口地址固定同源（dev 走上面代理、生产走 nginx），改 `.env` 里的前端地址不会生效 |

> 没有 `uv` 的机器（容器兜底）、本机环境坑、nginx 片段自检：见 [`docs/环境与排障.md`](docs/环境与排障.md)。

---

## 6. 配置

### 6.1 环境变量

所有密钥只走环境变量，由 Pydantic Settings 从 `.env` 加载；`.env.example` 只有占位符。**三份基线文档没有枚举任何环境变量名与端口**，下表是本项目自定义并回填文档的约定。

| 分组 | 变量 | 说明 |
| --- | --- | --- |
| 应用 | `APP_ENV` `DEBUG` `API_HOST` `API_PORT` `PUBLIC_BASE_URL` `FRONTEND_ORIGIN` | 运行环境、监听地址、对外域名 |
| 鉴权 | `JWT_SECRET` `JWT_ALGORITHM` `ACCESS_TOKEN_EXPIRE_MINUTES` `REFRESH_TOKEN_EXPIRE_DAYS` `ELDER_SESSION_EXPIRE_HOURS` | 人（2h/7d）、设备（24h）、工牌三套体系 |
| 数据库 | `DATABASE_URL` `LANGGRAPH_CHECKPOINT_DATABASE_URL` `LANGGRAPH_CHECKPOINT_SCHEMA` | checkpoint 必须落独立 schema `langgraph` 与独立连接池 |
| 缓存 | `REDIS_URL` | 短期会话、限流、重复调用检测 |
| System Two | `LLM_BASE_URL` `LLM_API_KEY` `LLM_MODEL` `LLM_TIMEOUT_SECONDS` | DeepSeek / Qwen 兼容端点，经统一适配器 |
| System One | `LAYA_ENABLED` `LAYA_BASE_URL` `LAYA_API_KEY` `LAYA_TIMEOUT_MS` `LAYA_CONFIDENCE_THRESHOLD` | 本地 CPU 分类器，阈值默认 0.85 |
| 语音 | `ASR_*` `TTS_*` | 第三方流式服务，统一发 key |
| 实时通话 | `TURN_URL` `TURN_USERNAME` `TURN_CREDENTIAL` `STUN_URL` `WEBRTC_ICE_TIMEOUT_SECONDS` | ICE 超时触发降级 |
| 天气 | `WEATHER_API_PROVIDER` `WEATHER_API_KEY` `WEATHER_API_BASE_URL` `WEATHER_CACHE_TTL_SECONDS` | 全系统唯一真实外部接口 |
| 通知 | `NOTIFY_CHANNEL` `WEBPUSH_VAPID_PUBLIC_KEY` `WEBPUSH_VAPID_PRIVATE_KEY` | 仅 `inapp` / `webpush`，**无微信、无邮件** |
| MCP | `MCP_WEATHER_SERVER_URL` `MCP_OPERATIONS_SERVER_URL` `MCP_CALL_TIMEOUT_SECONDS` | 工具发现与超时 |
| 观测 | `LANGFUSE_*` `OTEL_*` `METRICS_ENABLED` | Trace 上传前脱敏 |
| 数据保留 | `AUDIO_RETENTION_HOURS` `SCHEDULER_TIMEZONE` | 原始音频默认 72h 自动清理 |
| 前端 | **无构建期变量** | 前端同源访问 `/api`、`/ws`、`/media`，代码零 `VITE_*` 读取 |
| 3D 资产 | `DEFAULT_AVATAR_URL` `DEFAULT_FALLBACK_AVATAR_URL` `AVATAR_ROTATION_Y_OFFSET` `AVATAR_CACHE_VERSION_SALT` | 形象取值链**第二层**兜底（见 §6.2） |

**本地 ASR 基线（FunASR）**：**不新增地址类变量**，复用现有 `ASR_*` 字段——`ASR_PROVIDER=funasr`、`ASR_BASE_URL=wss://localhost:7002`、`ASR_INSECURE=true`（宿主端口见 §6.3 的 `funasr` 行，容器内 FunASR runtime 监听 10095）。

⚠️ 该服务默认开 TLS 且用镜像内置自签证书（启动日志固定打印 `SSL is opened!`）。因此：

- 用 `ws://` 连会被直接掐断，报 `did not receive a valid HTTP response`——**必须用 `wss://`**；
- 自签证书需要 `ASR_INSECURE=true` 才能握手；生产环境换真证书并把该开关置回 `false`；
- 部分环境下 Docker 的端口发布（宿主 `7002` → 容器 `10095`）从宿主侧不可达，此时让调用方加入同一 compose 网络、直接用容器名访问 `wss://anyu-funasr:10095`。

云 API 仍是主实现，FunASR 只作本地基线与降级备份（口径见 [`docs/接口契约_音频与信令.md`](docs/接口契约_音频与信令.md)）。

### 6.2 3D 形象资产与取值链

平板渲染数字人所需的四个值由 `GET /api/pad/avatar-config` 给出（契约 C7，设备 JWT 鉴权）：

```json
{"avatar_url": "https://localhost/avatars/vrm1_sample.vrm",
 "fallback_image_url": null, "rotation_y_offset": 0.0, "version": 1686351657}
```

`avatar_url` 的取值顺序是**三层**，前一层为空才落到下一层：

| 层 | 来源 | 改它的方式 |
| --- | --- | --- |
| 1 | `personas.avatar_url`（库里存**相对路径**，响应时拼 `PUBLIC_BASE_URL`） | 改 persona 行即可换形象，**零代码、零重启**；`version` 随之变化，前端缓存自动失效 |
| 2 | `DEFAULT_AVATAR_URL` | 改 `.env` 后重启后端 |
| 3 | 前端内置兜底（`fallback_image_url` 为空时显示仓库自带静态图 / 剪影） | 改 persona 的 `fallback_image_url` 或 `DEFAULT_FALLBACK_AVATAR_URL` |

`version` 是四键内容的 crc32 指纹（不是自增版本号），只承担「缓存失效」职责，**不代表形象可切换的档位**（FR-3D-03「更多动作与形象」是 P2，不排期）。

**静态托管**：`.vrm` 由 nginx 直出，不经后端转发（PRD §2.1）。片段在 `deploy/nginx.conf` 的 `location /avatars/`，要点两条：

- `.vrm` 必须回 `Content-Type: model/gltf-binary`，否则 three-vrm 拒绝解析（配置里用 `types { model/gltf-binary vrm; }` 显式声明，不依赖系统 mime.types）；
- 目录映射 `deploy/assets/avatars/` ↔ URL `/avatars/`，文件体积上限 **15MB**（超过会挤掉 FR-3D-02 的 5s 降级窗口）。

**资产不入库**：`*.vrm` 与 `*.glb` 已被 `.gitignore` 忽略（单个 10MB 量级）。新克隆仓库后按 [`deploy/assets/README.md`](deploy/assets/README.md) 重新下载：

```bash
curl -L -o deploy/assets/avatars/vrm1_sample.vrm \
  https://pixiv.github.io/three-vrm/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm
```

兜底图 `fallback.png` 由 [`scripts/gen_fallback_avatar.py`](scripts/gen_fallback_avatar.py) 生成（Pillow，输出确定性可复现），改字或改配色后重跑即可。

### 6.3 服务与端口

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| postgres + pgvector | 5432 | 业务表 + 独立 schema `langgraph` |
| redis | 6379 | 缓存与限流 |
| backend（FastAPI + aiortc） | 8000 | `/health` 健康检查；WebSocket 信令 |
| frontend（nginx） | 80 / 443 | 静态资源 + HTTPS 入口 + 媒体直连 |
| laya | 7001 | 本地 System One 推理服务（`--profile laya`） |
| funasr | 7002 | 本地 ASR 基线（容器内 10095） |
| weather-mcp | 7101 | `get_weather` |
| mock-operations-mcp | 7102 | 工单 / 家属通知 |
| coturn | 3478 / 5349 | TURN/STUN，跨网段才需要 |
| vite dev server | 5173 | 仅本地开发 |

### 6.4 账号与首启

账号不由使用者自助注册：员工与家属账号一律由 `admin` 创建，家属建号后还要再绑定到长者（两步）。

| 能力 | 端点 | 说明 |
| --- | --- | --- |
| 建员工 | `POST /api/staff` | `{name, role, phone, password, elder_ids?}`；`role ∈ admin / care_worker / doctor`；`elder_ids` 同时写负责范围（doctor→`doctor`、care_worker→`primary_care`，给 admin 分配一律 403） |
| 改员工 | `PATCH /api/staff/{staff_id}` | `{role?, status?}`，任一变更 `token_version+1`，旧 Token 立即失效（AC-24） |
| 建家属 | `POST /api/guardians` | `{name, phone, password}` → 返回 `guardian_id` |
| 绑家属 | `POST /api/elders/{elder_id}/guardians` | `{guardian_id, relation}`；重复绑定 409；同一长者最多 2 位在册监护人（服务层校验） |

错误码：`401 TOKEN_INVALID` / `403 ROLE_FORBIDDEN` / `404` / `409 CONFLICT`（手机号跨员工与家属两表查重）/ `422`。响应只回 `phone_masked`，不回传完整手机号与口令哈希。

**空库先建第一个 admin**——没有任何 admin 时上面四个接口都进不去，用 CLI 引导：

```bash
uv run python scripts/create_account.py --role admin --name 王管理 \
  --phone 13700000000 --password 'Admin@2026'
```

CLI 幂等（手机号已存在则 `kept`）、只存 bcrypt 哈希、支持 `--dry-run` 与 `--status`。契约见 [`docs/接口契约_身份与权限.md`](docs/接口契约_身份与权限.md) E12/E13/E15。

### 6.5 身份与语音能力

| 能力 | 对外入口 | 落点 | 证据 |
| --- | --- | --- | --- |
| 声纹注册与比对（REST） | `POST /api/voiceprints/samples`（注册）｜`GET /api/voiceprints/status`（查状态）｜`DELETE /api/voiceprints`（删除，204） | `app/api/voiceprints.py` | `tests/test_voiceprints_api.py` **11 passed** |
| 会话身份级联（契约 C5） | WS 四类消息：`identity_ask` / `identity_confirm` / `identity_result` / `client_diag` | `app/audio/signaling.py` 上行分派；`app/audio/media_bridge.py` 调 `verify_session_identity`、`start_identity_cascade` | `tests/test_identity_cascade.py` **17 passed** |
| 服务端 VAD 兜底 | 无对外入口（进程内） | `app/audio/webrtc.py` 的 `_build_vad_session`；webrtcvad 不可用自动 no-op，**不改轮次语义** | `tests/test_audio_wav_vad.py::TestServerVadWiring` **3 例** |
| 3D 数字人（pad 专属） | `GET /api/pad/avatar-config` 四键（取值链见 §6.2） | `frontend/src/pad/avatar/{VrmAvatar,loadAvatar,AvatarStage}` | `pnpm --dir frontend build` **exit=0**；`console`/`family` 产物搜 `three-vrm` **零命中** |

三条实现口径（细节在 [`docs/IDENTITY_RULES.md`](docs/IDENTITY_RULES.md) 与 [`docs/HARNESS_DESIGN.md`](docs/HARNESS_DESIGN.md)）：

- 级联三态：声纹 `voice` → 直接对话；低置信 `confirm` → `identity_ask` 让长者点选（`timeout_s: 30`）；30s 内未确立 → `generic` 通用模式。
- **通用模式不是降级聊天**：不读记忆、不写健康数据、不确认用药（PRD §3.2）。
- 3D 加载失败或超 5s 一律切静态头像，**不白屏**；pad 的 three-vrm 依赖不得进入 `console`/`family` 构建包（AGENTS §2 硬约束）。

---

## 7. 质量门与验收

### 7.1 验收标准

| 项 | 标准 |
| --- | --- |
| 核心 Tool 与权限场景 | 用例覆盖 **100%** |
| 接口测试覆盖率 | **≥ 70%** |
| 三道隔离闸门 | 会话隔离 / 记忆隔离（强制 `WHERE elder_id`）/ 工具授权，**100% 覆盖** |
| 提示注入测试集 | ≥10 条，**100% 拦截**；日志脱敏 grep 零命中 |
| 评测门禁 | `eval_gate.py` 作为 pre-push 钩子，固定集不达标阻断提交，bad case 回流 `eval/fixtures` |
| 端到端延迟 | P95 ≤ 1.5s；规则层路由 ≤200ms |
| 一键部署 | 全部服务健康检查通过 + 长者端 HTTPS 可访问 + 可回滚 |

硬性数值（`MAX_TOOL_CALLS=8`、0.85 阈值、用药四次触达、19:00–20:00 采集窗口、72h 音频保留等）在 [`AGENTS.md`](AGENTS.md) §7 有完整清单，**改动需走评审**。

### 7.2 复现与门禁命令

| 用途 | 命令 |
| --- | --- |
| 全量门禁（8 阶段，与 pre-push 同款） | `uv run --locked python scripts/ci.py` |
| 续跑某一段 / 只看清单 | `... --from test` · `... --stage lint` · `... --list` |
| 评测门禁（路由敏感召回 + 注入拦截，门槛均 100%） | `uv run python tests/eval_gate.py` |
| 后端用例 | `uv run pytest -q`（覆盖率：加 `--cov=app --cov-report=term-missing`） |
| 前端用例 | `node scripts/run-frontend-tests.mjs` |
| 文档 `file:line` 引用校验 | `uv run python scripts/check_doc_refs.py`（DRIFT / MISSING 必须为 0） |
| 部署静态自检 | `python scripts/check_compose.py` · `python scripts/check_image_secrets.py` |
| CI | [`.github/workflows/anyu.yml`](.github/workflows/anyu.yml)，与本地 8 阶段同 argv |

八个默认阶段与顺序：`lock`（`uv lock --check`）→ `sync`（`uv sync --locked --all-groups`）→ `lint`（`ruff check .` + `ruff format --check .`）→ `type`（`mypy app mcp-servers`）→ `manifest`（版本清单校验）→ `test`（`pytest --cov --cov-report=term-missing`）→ `security`（`scripts/secret_scan.py`）→ `build`（清 `dist/` + 导出冻结依赖 + `import app.main` 冒烟）。

- **门禁只在 push 前跑**（2026-11-12 裁决）：`git commit` 不触发任何代码门禁；`.git/hooks` 由 `core.hooksPath` 指向 `.githooks`，那里只有 `pre-push`。新 clone 后执行一次 `git config core.hooksPath .githooks`。
- **两处刻意 override**：`build` 阶段——本项目是应用不是可分发包（`[tool.uv] package = false`），`uv build` 必然报 `Multiple top-level packages discovered`，阶段语义改述为「交付物能从已提交源码物化」＝导出冻结依赖 + 装配冒烟；`security` 阶段——`detect-secrets` 走独立 `security` 依赖组进锁，`.secrets.baseline` 271 条已逐条人工复核（全部 `is_secret: false`）。

### 7.3 成本与容量口径（`/metrics` 为 Prometheus 文本）

> `GET /metrics` 返回 `text/plain; version=0.0.4` 的 Prometheus exposition 文本。抓取**不要求 JWT**，但只允许回环地址（`127.0.0.1` / `::1`），非回环 403。进程内的 `metrics_snapshot()` 仍保留，只给测试与兼容读法用。

| 指标 | 口径 | 门槛 / 当前值 |
| --- | --- | --- |
| 端到端语音响应 | `reply_source_total`、`tts_first_packet_ms`、`tts_total_ms`；分位由 Grafana/PromQL 算 | P95 ≤ 1500ms（受控链路实测 P50 5.79ms / P95 6.47ms，含 stub TTS；`/metrics` 分位为桶近似） |
| 分层命中率 | `sum by (layer) (route_layer_total)` | L0/L1/L2 占比；规则层直路由越多越省 |
| S1 闭环率 | `route_s1_closed_total / route_s1_total`（分母只算 L1；`confidence ≥ 0.85` 视为直路由闭环，调用方也可显式传 `escalated`） | **无验收阈值**，只作省算力的观察项（AGENTS 前置·铁律 1：排期叙述里的数值不作事实依据）；无样本时 PromQL 返回空而不是 0 |
| 单轮工具预算 | `MAX_TOOL_CALLS_PER_TURN` | 8（PRD FR-DLG-10）；同 Tool 60s 内第 3 次熔断 |
| token 消耗 | `llm_prompt_tokens_total` / `llm_completion_tokens_total`（标签只有 `model`） | 供应商不回 usage 时**不计入**（不记 0 冒充样本） |
| 单会话成本 | `uv run python scripts/metrics_cost.py`：读 `/metrics` 文本 × 价目表换算；`--prices` 默认 `config/llm_prices.json`，`--format json\|csv` | 演示四场景 ≈ 数十次 LLM 调用量级；价目表当前是**示意占位值**，上线前按供应商价目替换并同时改 `version` |

### 7.4 交付清单逐项自检

PRD §6.1 列的交付物逐项对应到「在哪」与「怎么验」，任何一项不成立都算本轮没交付：

| 交付物 | 位置 | 怎么验 |
| --- | --- | --- |
| 一键启动 + HTTPS + 健康检查 | `docker-compose.yml` / `frontend/nginx.https.conf` / `scripts/gen_dev_cert.py` | `docker compose ps` 全 `healthy`；`curl -k -s https://localhost/health` 200 且带 `X-Trace-Id`；`python scripts/check_compose.py` 五项判据 PASS |
| 容器编排 | `docker-compose.yml`（默认 9 个容器：7 必需 + funasr/voiceprint；`laya` 走 `--profile laya`，`celery-*` 走 `--profile celery`） | `docker compose config --services`；`scripts/check_compose.py` 的 C1–C5 |
| 镜像不含密钥 | `Dockerfile` / `.dockerignore` | `python scripts/check_image_secrets.py` → `[OK]`；反向删 `.dockerignore` 的 `.env` 必 `[FAIL]` |
| 完整 Trace | `app/core/observability.py` + `app/core/_observe.py` | 上报走 **OTLP**（Langfuse：`{LANGFUSE_HOST}/api/public/otel/v1/traces`，Basic Auth；Grafana Cloud：`{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces`）。已实测把一条 6 span 树写进云实例（`trace_id=9f19c80ae37c774502f9629b82864857`）；配好 `LANGFUSE_*` 后按 `trace_id` 可搜到同一条链路 |
| 成本与质量指标 | `GET /metrics`（Prometheus 文本，仅回环可读） | `curl -s 127.0.0.1:8000/metrics` 能读到 `route_layer_total` / `route_s1_closed_total` / `llm_*_tokens_total`；成本换算用 `scripts/metrics_cost.py --format json` |
| 回滚 | `scripts/rollback.sh` + [`docs/rollback.md`](docs/rollback.md) | `bash scripts/rollback.sh --dry-run` 打印八步计划后 exit 0；真跑需在普通终端（`git reset --hard` 会被安全网静态拦截） |
| 门禁 | `scripts/ci.py` + [`.github/workflows/anyu.yml`](.github/workflows/anyu.yml) + `.githooks/*` | `uv run --locked python scripts/ci.py` 八阶段全 PASS（见 §7.2） |
| 评测报告 | [`eval/reports/`](eval/reports/) | 六份 md：延迟基线、路由、记忆用药、健康日报、安全、声纹 FAR/FRR（后者如实标注「未闭合」） |
| 安全报告 | [`docs/SECURITY.md`](docs/SECURITY.md) | 五类威胁逐条给出 `file:line`；三道闸门 ASCII 图 |
| 交付文档四件 | [`docs/HARNESS_DESIGN.md`](docs/HARNESS_DESIGN.md) / [`docs/IDENTITY_RULES.md`](docs/IDENTITY_RULES.md) / [`docs/AI_USAGE.md`](docs/AI_USAGE.md) / [`docs/INTERVIEW_QA.md`](docs/INTERVIEW_QA.md) | 每份的 `file:line` 引用抽检 10 处能对上正文 |
| 5 分钟演示 | 分镜脚本 | 仓库**不含录像**；脚本见 [`docs/plans/day05/adhoc-20261107-iter2演示脚本.md`](docs/plans/day05/adhoc-20261107-iter2演示脚本.md) |
| 无 3D 能力设备不白屏 | `frontend/src/pad/avatar/webgl.ts` + `AvatarBoundary.tsx` | 挂载前探测 WebGL2/WebGL：拿不到直接显示 `/avatars/static-portrait.png` 并说明「当前设备未启用 3D」；禁用 GPU 的 Playwright 回归 = canvas 0、静态图可见、不下载 10.7MB 模型 |

---

## 8. 已知限制与降级矩阵

| 故障 | 表现 | 降级行为 |
| --- | --- | --- |
| 3D 模型/动画加载失败或超 5s | 3D 区域不渲染 | 切静态头像 + 字幕，**不白屏**（`loadAvatar` 5s 竞速兜底） |
| WebRTC 建连超 5s / ICE 失败 / TURN 不可达 | 双向语音不可用 | 转 HTTP 按住说话整段上传 |
| ASR 不可用 | 语音无法转文本 | 转页面输入框 |
| Laya 加载失败 | System One 缺失 | 全部请求走 System Two，并打降级标记 |
| System Two 超时/限流 | 开放域回复失败 | 重试 2 次后返回「L1 结果 + 模板话术」并提示人工 |
| 天气 API 失败 | 天气查询空回 | 返回缓存值并标注数据时间；无缓存则如实告知 |
| 网络中断 | 页面不可用 | 前端 IndexedDB 兜底（P1） |
| 声纹置信不足 | 无法确认身份 | 主动询问「请问是哪位在说话？」；仍无法确认进通用模式（不读记忆、不写健康、不确认用药） |
| 声纹服务不可用 | 无法比对 | 同样下发 `identity_ask`（`reason=low_confidence`）；30s 未确认转通用模式 |

**其他限制**：无真实平板硬件；天气是唯一真实外部系统，其余全 Mock；声纹向量维度已定 **CAM++ 192 维**（解密后维度不符即抛 `VoiceprintError`）；唤醒词端侧检测（PRD FR-UI-11）已实现（sherpa-onnx KWS），但**真机麦克风**下的命中率未测，只验过受控输入（同一份合成 WAV 走 Chrome 假设备 20/20 命中）；单机 Docker 验收负载为 1 路活跃语音 + ≤10 后台用户。

---

## 9. 版权与合规

- 戏曲、影视、音乐素材**只使用 20–30 条授权样例**，不爬取、不内置来源不明内容。
- 长者声纹样本由本人现场录制 3 段，录制前告知用途，**可随时要求删除**。
- 原始音频默认不保存（仅调试开关临时开启，最长 72 小时自动删除），**不得用于任何模型训练**。
- 日志与 Trace 上传前必须脱敏：无身份证、手机号、完整住址、密钥。
- 第三方 3D 形象、动画与唤醒词资源的版权声明见 [`frontend/public/avatars/NOTICE.md`](frontend/public/avatars/NOTICE.md)、[`animations/LICENSE-Apache-2.0.txt`](frontend/public/avatars/animations/LICENSE-Apache-2.0.txt)、[`frontend/public/kws-assets/NOTICE.md`](frontend/public/kws-assets/NOTICE.md)。
- 本项目为演示用途，**不构成医疗器械或医疗服务**。

---

## 10. 文档

### 10.1 索引

| 文档 | 用途 |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | **工程规则**：技术栈、目录约定、常用命令、禁区、高风险文件清单、模型路由表、硬性数值 |
| [`docs/安语3D数字人_PRD.md`](docs/安语3D数字人_PRD.md) | 需求与验收基线（**v3.2**）。**版本号以文末修订记录末行为准，本行只作索引** |
| [`docs/安语3D数字人_技术设计文档.md`](docs/安语3D数字人_技术设计文档.md) | 架构、表清单与字段/约束、Agent 闭环、子图设计（**v1.20**） |
| [`docs/ER_数据模型设计.md`](docs/ER_数据模型设计.md) | **数据模型唯一实现口径**（v0.7）：逐字段、约束与索引、迁移分批、AC 取证矩阵 |
| [`docs/接口契约_身份与权限.md`](docs/接口契约_身份与权限.md) | **契约 · 身份与权限**（v1.14）：三套凭证、端点字段口径、权限矩阵 |
| [`docs/接口契约_记忆.md`](docs/接口契约_记忆.md) | **契约 · 记忆**（v1.5）：FR-MEM-01..07 端点口径、写入链、召回隔离 |
| [`docs/接口契约_音频与信令.md`](docs/接口契约_音频与信令.md) | **契约 · 音频与信令**（v1.1）：`GET /api/audio/ice-config`、STUN/TURN 字段、降级矩阵 |
| [`docs/HARNESS_DESIGN.md`](docs/HARNESS_DESIGN.md) | **交付文档 · 模型路由与门槛**：三级路由判定表、预算与降级矩阵、记忆隔离、工具四类失败与熔断 |
| [`docs/IDENTITY_RULES.md`](docs/IDENTITY_RULES.md) | **交付文档 · 身份规则**：三套凭证、`runtime_context` 注入、模型自报身份作废、双人间隐私收敛 |
| [`docs/SECURITY.md`](docs/SECURITY.md) | **安全报告**：五类威胁模型、三道闸门位置图、脱敏规则、密钥与轮换、审计与降级矩阵 |
| [`docs/AI_USAGE.md`](docs/AI_USAGE.md) | **交付文档 · AI 使用说明**：哪些环节用模型、prompt 与 persona 版本管理、成本与延迟口径、人工兜底点 |
| [`docs/INTERVIEW_QA.md`](docs/INTERVIEW_QA.md) | 演示与评审问答（加分项，非交付硬门）：20 问，每题指向代码或文档位置 |
| [`docs/rollback.md`](docs/rollback.md) | 回滚演练手册：`scripts/rollback.sh` 八步、迁移 A/B/C/D 判定、退出码与演练记录 |
| [`docs/demo-accounts.md`](docs/demo-accounts.md) | 演示账号与床位/设备/状态对照（v1.1） |
| [`docs/环境与排障.md`](docs/环境与排障.md) | 本机环境坑、无 `uv` 机器的容器兜底、nginx 片段自检 |
| [`docs/DOC_DRIFT.md`](docs/DOC_DRIFT.md) | 实现与文档漂移台账（轴二） |
| [`docs/deploy/公网部署.md`](docs/deploy/公网部署.md) | 公网部署说明 |
| [`docs/plans/`](docs/plans/README.md) | 迭代计划与验收脚本（过程记录：终态件留在原地，靠文件头的 `状态` 区分） |
| [`eval/reports/`](eval/reports/) | 评测报告：延迟基线、路由（110 条）、记忆与用药、健康日报、安全与声纹 FAR/FRR |

**门禁与自检脚本**：[`tests/eval_gate.py`](tests/eval_gate.py)（路由敏感召回 + 注入拦截，pre-push 第一段）、`scripts/ci.py`（八阶段）、`scripts/secret_scan.py`（仓库与日志密钥扫描）、`scripts/check_compose.py` 与 `scripts/check_image_secrets.py`（部署静态自检）、`scripts/rollback.sh`、`scripts/check_doc_refs.py`（文档 `file:line` 引用校验）。

### 10.2 已知文档冲突（轴一：早期方案 vs 现行文档）

| 冲突点 | 旧口径 | 现行口径 |
| --- | --- | --- |
| 药单修改权限 | 早期方案：护工可改 | **PRD §4.5：医生增改停查，护工与管理员只读** |
| Checkpointer 后端 | 早期方案：SQLite | **PostgreSQL 独立 schema `langgraph`** |
| 表数量与表名 | 早期方案 11 张（`workorders`/`conversations`） | **30 张业务表（`public` schema）+ 4 张 checkpointer 表（`langgraph` schema）；表名 `work_orders`/`conversation_threads`** |
| 老电影点播 | 早期方案列为功能验收 | **PRD 降为 P2** |
| 通知渠道 | 旧稿含微信 / 邮件 | **仅站内 `inapp` + Web Push** |
| 前端 UI 库 | 技术文档 §1.4 概括为 Tailwind | **2026-11-06 裁决：pad 用 Tailwind；console/family 手写 CSS + tokens，不引组件库** |

### 10.3 实现与文档漂移台账（轴二：以实现为准）

**轴一与轴二规则相反，不要混**：轴一（上表）以 PRD / 技术设计文档 / ER 为准；轴二以**代码实现**为准，差异登记进 [`docs/DOC_DRIFT.md`](docs/DOC_DRIFT.md)。

分流判据（完整版与台账见该文件）：

| 类型 | 差的是什么 | 动作 |
| --- | --- | --- |
| **细节漂移** | 数值、字段名、时点、枚举写法、页面写法 | 以实现为准，不改代码；登记台账 |
| **范围漂移** | 能力有无、业务边界、角色权限、状态机、幂等口径 | 以实现为准判验收，**必须回写 PRD** 并同一次提交补修订记录 |
| **红线漂移** | 与 AGENTS §7 硬性数值表不一致 | 不许自动吸收：要么改代码对齐 §7，要么先改 §7 写明理由再改代码 |

---

## 11. 许可证

本项目代码以 MIT 许可发布，详见 [`LICENSE`](LICENSE)。`media/` 下的素材为 CC0-1.0；第三方 3D 形象与动画的授权红线见 [`frontend/public/avatars/NOTICE.md`](frontend/public/avatars/NOTICE.md)。
