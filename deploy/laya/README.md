# Laya System One 服务壳（N31）

> 状态：已冻结 ｜ 版本：v1.0 ｜ 日期：2026-11-10
> 关联：`设计记录（全量缺口补齐）`（N31）｜上游依据：技术设计文档 §6（三级路由）、PRD §2.3 / §5.3
> 变更记录见文末

把七类 intent + 三个 noul 问法包成 **`POST /v1/classify`**，响应**且仅且是**五字段。
契约唯一来源是 `app/system_one/client.py`；七类 intent 唯一来源是
`app/system_one/questions.py::INTENT_CLASSES`（本目录不另写类目表）。

```
deploy/laya/
├── server.py          # FastAPI 薄壳：/v1/classify + /health + 非法响应注入
├── contract_check.py  # N31 验收脚本：用真实 LayaClient 打本壳（16 项断言）
├── requirements.txt   # 镜像依赖（torch 只在容器，不进项目 .venv）
├── Dockerfile         # python:3.12-slim + 非 root + healthcheck
└── README.md
```

---

## 1. 为什么不复用 `laya-serve`

PyPI 的 `laya-serve`（0.2.1）只提供 Jev 协议端点：

| 它给的 | 我们要的 |
| --- | --- |
| `POST /v1/systemone`、`/v1/systemone/batch`、`GET /health` | `POST /v1/classify` |
| Jev TypeSafe 响应体 | 五字段扁平 JSON |
| 固定的问题模板 | 我们自己的七类 choice + 三 noul |

它**没有可插拔的字段映射钩子**：要把 Jev 响应改成五字段，只能在前面再套一层代理
（多一跳 + 多一次序列化），而那层代理本身就是我们要写的壳。故直接用 SDK
`laya.load()` 写薄壳，把「传输层」省掉。

## 2. 加载器选型（SDK vs 权重包自带 `rl_agent_api`）

**同一批 20 条样本、同一套四问措辞**（对照组脚本 `.tmp/laya-sdk-compare.py`，
直接 `import probe` 复用样本，不复制粘贴）：

| 指标 | 权重包自带 `rl_agent_api`（N30） | `laya` SDK 0.4.30 | 结论 |
| --- | --- | --- | --- |
| 冷加载 | 86835 ms | **15052 ms** | SDK 快 5.8× |
| 七类准确率 | 14/20 = 70.0% | 14/20 = 70.0% | 一致 |
| 逐条判定差异 | — | **0 条** | 逐条相同，不是「差不多」 |
| 四问一次 p50 / p95 | 1097.8 / 1568.5 ms | **850.8 / 1042.7 ms** | SDK 更快 |
| 仅 intent p50 / p95 | 366.5 / 822.0 ms | **253.1 / 451.3 ms** | SDK 更快 |

结论：用 SDK。加载从 87s 降到 15s，且答案逐条不变——不存在「为了快而牺牲准确率」。

## 3. 四信号怎么映射（N30 实测，不是猜的）

| 五字段 | 来源 | 阈值/口径 |
| --- | --- | --- |
| `intent` | choice 类问题的选中项 | 取值 ∈ `INTENT_CLASSES` 七类 |
| `confidence` | 该 choice 的 `confidence` = 1 − 归一化熵 | 与 N30 口径一致；**不用** `answer_confidence`（那是选中项概率） |
| `sensitive` | noul 问题的 P(true) | ≥ `LAYA_NOUL_THRESHOLD`（默认 0.5） |
| `privacy` | 同上 | 同上 |
| `is_out_of_domain` | 同上 | 同上 |

**`is_out_of_domain` 必须走独立 noul，不能塞进七类 choice。** N30 实测：域外三条
（查股票 / 写代码 / 打官司）塞进 choice 命中 **0/3**，独立 noul 却给
**0.688 / 0.953 / 0.574**。这是决定服务壳形状的证据。

**`sensitive` 不是安全兜底网。** N30 实测中文健康主诉概率极低
（「头有点晕浑身没力气」= 0.05），所以安全一票否决**只在 L0 规则层**
（`app/router/safety.py`）；L1 的 `sensitive` 仅作参考信号，任何路径不得把它当安全门。
这条是硬口径，改之前先改 PRD。

## 4. 配置（`LAYA_` 前缀，与后端共用命名）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LAYA_MODEL_ROOT` | `/models/laya` | 权重根目录（容器里挂载） |
| `LAYA_SUBFOLDER` | `multilingual` | **中文必须用这个**（mmBERT-base）；根目录是英文面 ModernBERT-large |
| `LAYA_DEVICE` | `cpu` | |
| `LAYA_THREADS` | `4` | torch 线程数；8 核机上 4 线程最优，再多只跟后端抢核 |
| `LAYA_API_KEY` | 空 | 非空则要求 `authorization: Bearer <key>`，与 `client.py` 的 `laya_api_key` 对称 |
| `LAYA_FAULT_INJECT` | `0` | 非法响应注入总开关，**生产必须为 0** |
| `LAYA_NOUL_THRESHOLD` | `0.5` | 三个 noul 转 bool 的阈值 |
| `LAYA_MAX_TEXT_CHARS` | `512` | 超长截断（防一次前向被打长文本拖死） |

`extra="ignore"`：后端也用 `LAYA_` 前缀（`LAYA_BASE_URL` / `LAYA_TIMEOUT_MS` 等），
同一份 compose env 混着两边的键，多出来的不许炸。

## 5. 本地跑（不碰项目 `.venv`）

```powershell
# 1) 隔离 venv（N30 已建，torch CPU + transformers 5.x + laya）
uv venv .tmp\laya-venv --python 3.12
uv pip install --python .tmp\laya-venv\Scripts\python.exe torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .tmp\laya-venv\Scripts\python.exe transformers laya fastapi "uvicorn[standard]" httpx pydantic-settings

# 2) 起壳（中文用 multilingual）
$env:PYTHONPATH='<仓库根>'
$env:LAYA_MODEL_ROOT='<权重根目录>'
.tmp\laya-venv\Scripts\python.exe -m uvicorn server:app --app-dir deploy\laya --port 7001

# 3) 打一发
curl -s -X POST localhost:7001/v1/classify -H 'content-type: application/json' -d '{"text":"给我放一段越剧"}'
```

## 6. 契约级验收（N31 的验收，不是可选项）

```powershell
$env:PYTHONPATH='<仓库根>'
.tmp\laya-venv\Scripts\python.exe deploy\laya\contract_check.py
```

为什么不是 pytest：契约验收必须跑真权重，而项目 `.venv` 按 AGENTS §4-12 不许装 torch，
写成 pytest 只能 skip —— 等于没验。故做成独立脚本。

**实测输出（2026-11-10，exit 0，16/16 通过，总耗时 37.9s）**：

```
[A] ASGI 进程内（注入开启）
  PASS  health ready — 19.3s
  PASS  INTENT_CLASSES 七类单一出处 — ['chitchat', 'medicine', 'opera', 'movie', 'weather', 'health', 'other']
  PASS  五字段齐全且类型合规 — {'intent': 'opera', 'sensitive': False, 'confidence': 0.9519, 'privacy': False, 'is_out_of_domain': False}
  PASS  intent 落在七类内 — opera
  PASS  confidence ∈ [0,1] — 0.9519
  PASS  缺字段 => LayaBadPayload — laya payload missing fields: privacy,is_out_of_domain
  PASS  confidence 越界 => LayaBadPayload — confidence out of [0,1]: 1.5
  PASS  intent 非字符串 => LayaBadPayload — intent must be string, got int
  PASS  布尔位非 bool => LayaBadPayload — sensitive must be bool, got str
  PASS  非 JSON 正文 => LayaBadPayload — laya payload is not valid json
  PASS  HTTP 503 => LayaBadStatus — laya returned http 503

[B] 真 uvicorn（注入开启 + Bearer 鉴权）
  PASS  uvicorn 实际监听并就绪 — http://127.0.0.1:52450
  PASS  真实 HTTP 五字段 — intent=opera conf=0.952 ood=False | 单轮 841ms (min 725 / max 2302)
  PASS  200ms 客户端打慢服务 => LayaTimeout — laya timeout after 200ms
  PASS  漏带 Bearer => LayaBadStatus(401) — laya returned http 401
  PASS  注入关闭时 ?fault= => 403 — 403
```

**「每轮多付的等待时间」= 约 750–850ms**（真实 HTTP 四问一次，min 725 / max 2302，
末位是 GC 毛刺）。这是 L1 相对「直接走 L2」多付的等待，不许藏。

### 非法响应注入

只在 `LAYA_FAULT_INJECT=1` 时生效，两条通道（都受同一开关控）：

- query：`POST /v1/classify?fault=<kind>`
- 文本前缀：`{"text": "__fault:<kind>"}` —— `client.py` 不发 query 参数，
  契约测试又必须走**未改动的**真实客户端，所以补这条通道。

`kind` ∈ `missing_field` / `bad_confidence` / `bad_intent` / `bad_bool` /
`not_json` / `bad_status` / `slow`。开关关闭时一律 `403`，不会吐假信号。

## 7. 实现偏差（必须随 PRD 一起看）

1. **CPU 纯软推理达不到 PRD §5.3 的 200ms。** 实测仅 intent 一问 p50 253 / p95 451ms，
   四问 p50 851 / p95 1043ms；代码默认值 `laya_timeout_ms=200` **保持不动**
   （那是目标口径，不静默改）。演示档在 compose 设 `LAYA_TIMEOUT_MS=1500`（Lead 裁决，§8.6）
   换取链路可取数；生产三选一：GPU / 更小分类头 / 按实测重定 §6 延迟表 —— 不由本文件拍板。
2. **`confidence` 是 1 − 归一化熵，不是选中项概率。** 20 条样本里 ≥0.85 只有 6 条，
   所以 L1 命中率天然偏低、大量走「澄清一次 → L2」。这是真实表现，不是 bug；
   若产品要更高的 L1 覆盖率，得改阈值口径（PRD §2.3）而不是改代码。
3. **`other` 类目 0/3**（查股票 / 写代码 / 打官司都被判成闲聊或用药）。
   这类句子的正确处理路径是 `is_out_of_domain=True` → L2，而 noul 那一路是准的。
   保留 double 通道（choice 判 intent + noul 判域外），不要指望 choice 自己认出「其他」。

## 8. 容器化（N32）与 L1 生效取证（N33）

### 8.1 构建：pip 必须换源，否则卡在托管 CDN

容器内实测（`python:3.12-slim` 里打各站）：

| 目标 | 结果 |
| --- | --- |
| `pypi.org/simple/transformers/` | 200，但要 **30.6s** |
| `files.pythonhosted.org`（真正托管 wheel 的 CDN） | **45s 超时，失败** |
| `mirrors.aliyun.com/pypi/simple/` | 200，**0.2s** |
| `pypi.tuna.tsinghua.edu.cn/simple/` | 200，**0.2s** |
| `download.pytorch.org/whl/cpu/` | 200，0.6s |

所以「索引通」不等于「能下包」。`Dockerfile` 因此留了 `ARG PIP_INDEX_URL`：

```bash
docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  -f deploy/laya/Dockerfile -t anyu-laya:local .
```

实测 pip 层 521s（torch 209.5MB 走 pytorch 源，其余走镜像源），镜像 **1.83GB**；
改动 `server.py` / `app/` 后复跑 pip 层全缓存，约 20s。`docker compose --profile laya up`
会自动带上 compose 里的 `build.args`（`LAYA_PIP_INDEX_URL` 可覆盖）。

### 8.2 内存：容器里真实常驻 1.44GiB，不是 1.03GB

| 口径 | 数值 |
| --- | --- |
| N30 宿主 `psutil` RSS 峰值 | 1031.8MB（**偏低**：当时宿主 AvailableMB=0、进程被换页） |
| 容器 cgroup `memory.stat` **anon** | **1.44–1.69GiB**（无 swap，不换页不缩水） |
| 容器 `file`（权重页缓存） | 67–80MB（与 anon 分开算，别混为一谈） |

因此 compose 里 `mem_limit: 2048m`（≈40% 余量）。Docker VM 只有 3.728GiB，
当前实测：laya 1.69GiB + backend 210.9MiB + funasr 263.2MiB + 其余 <100MiB。
**cap 的意义是失败局部化**：Laya 膨胀时容器重启、主栈继续走 L2 降级，而不是把 funasr 拖进 OOM。

### 8.3 `/health` 语义与启动时序

`/health` 只在 `state == "ready"` 时返回 200；`loading` / `failed` 一律 **503**
（N31 原实现 loading 返回 200，会让 compose 在模型没就绪时就报 healthy——**假阳性**，已改）。
实测 `recreate → healthy` 约 83s，`load_ms=55043`（宿主 venv 里 15s，容器冷页缓存更慢）。
start_period 给到 180s；日志里能看到 `laya shell starting` / `laya model loaded`（uvicorn
默认不给模块 logger 装 handler，本壳自装了 `_configure_logging()`）。

### 8.4 权重挂载与 `env_file` 的两处坑

- `volumes` 必须用**长语法**：`X:\models\laya:/models/laya:ro` 里的盘符冒号会被短语法
  当成 `host:container:mode` 分隔符切开。现写法：
  `{type: bind, source: ${LAYA_MODEL_DIR:-./laya-model}, target: /models/laya, read_only: true}`。
- laya 服务**不挂 `env_file: .env`**：后端那份 `.env` 里有 `JWT_SECRET` / `LLM_API_KEY` /
  `TURN_CREDENTIAL`，而本壳只用 `LAYA_` 前缀，没理由把三家密钥抄送进推理容器。
  （后端 `LAYA_BASE_URL` / `LAYA_TIMEOUT_MS` 仍按原样注入 backend。）

### 8.5 N33 取证：`decision_logs` 里真有 `layer='L1'` 的行

路径是**生产链路**，不是壳自己 curl：backend 容器内 `resolve_identity(DEMO_DEVICE_ID)`
→ `app.router.wiring.wire_route(...)` → `decision_log_service.log_decision(...)`，
与 `app/dialogue_runtime.py:365 _route` 的两步完全一致（音频轮不可用，理由见脚本 docstring）。

- 容器网络面自证：`docker exec anyu-backend python -c ...` 打到 `http://laya:7001/v1/classify`
  → `resolve: 172.25.0.10`、`http 200`、773.2ms、五字段齐全（证明不是只有宿主回环通）。
- 落库行 `id=dd680964-22e0-4294-a242-3487226a9dd1`：
  `layer=L1 intent=opera sensitive=f confidence=0.9552 privacy=true is_out_of_domain=f route_ms=628 degraded=∅ trace_id=n33-1794286366`。

### 8.6 演示档 1500ms：代价与实测（不许粉饰）

同一句「给我放个戏曲听听」：

- `LAYA_TIMEOUT_MS=800` → `LayaTimeout: laya timeout after 800ms`（服务端实测 803ms）；
- 放宽到 3000ms → 712ms 返回五字段，`wire_route` 往返 636.7ms，落 L1。

800ms 档 12 次连打：成功 10 次，p50 **663ms**，最大 **801.7ms**，2 次恰好压在超时线上——
四问一次的开销（≈660–830ms）与 800ms 是同一量级，必然间歇性降级。

**裁决（Lead，2026-11-10）：演示档提到 `LAYA_TIMEOUT_MS=1500`。** 不是 1200——1200 只留 400ms
余量，一次 GC 毛刺（实测到过 2302ms 的四问样本）就又会降级。**代码默认 200ms 一个字没动。**

1500ms 档 12 连打，两轮紧接（`docker compose up -d backend` 重建后立刻跑）：

| 轮次 | 结果 | p50 | p95 | max |
| --- | --- | --- | --- | --- |
| 第一轮（空闲后首轮） | **7/12 超时**（实测 1507–1532ms） | 569.2（仅 5 个成功样本） | 649.1 | 657.8 |
| 第二轮（紧接） | **0/12 超时** | 500.0 | 821.0 | 863.4 |

服务端真耗时（把客户端超时放到 5000ms 再量）：6 次 413.6–603.7ms，p50 **473.8ms**。

**为什么第一轮会连片超时**：壳内部是串行（`threading.Lock`），而客户端超时**不会取消服务端
在途计算**。第 1 次一旦超过 1500ms，后面的请求就排在它身后继续吃满 1500ms，形成排队级联；
积压排空后恢复正常。同期证据：容器日志显示 12 次全部完成、`restarts=0 oom=false`（不是崩溃）。

**结论**：1500ms 在**热**状态下够用（p95 821ms，留 ~680ms 余量）；但**空闲后的第一轮仍可能
连片降级**，这是「首轮毛刺 + 服务端不取消」的复合效应，不是 L1 判定逻辑的缺陷。演示时先
打一次暖机请求再开始讲故事，比把超时继续调大更诚实。

### 8.7 L1 覆盖面窄的主因是 `is_out_of_domain` 过度触发

12 条候选句里有 **11 条**被判 `is_out_of_domain=True`（含「今天几号了」0.93、「明天天气如何」
0.99、「我想看个老电影」0.99）。按决策矩阵第 2 条，域外直接 L2 **且不带降级标记**——
于是现场看起来「L1 没生效」，其实是判定顺序先撞了域外。这属于阈值/问法口径问题
（PRD 侧），**本轮没有动 `noul_threshold` 去凑数字**。

### 8.8 开 L1 的对照跑（同一份 `eval/fixtures/routing.jsonl`，110 条）

| 口径 | 命中 | 敏感召回 | p50 / p95 | layer 分布 |
| --- | --- | --- | --- | --- |
| L1 关（离线门禁 `laya_enabled=False`） | 88/110（80.0%） | 20/20 | 0.1 / 0.2ms | L0=52、L2=58 |
| L1 开（真壳，超时 2000ms） | **83/110（75.5%）** | 20/20 | 378.0 / 807.2ms | L0=52、**L1=5**、L2=53 |

新增 5 条错误**全部是「期望 L2、实际 L1」**，且都是开放域闲聊：n040「年轻时候在纺织厂，
三班倒可苦了」、n042「我孙女考上大学了，高兴」、n044「外星人到底存不存在？」、
n047「你说说李白和杜甫谁的学问大」、n049「邻座王姐要去海南过冬，你说好不好」。

这是**固定集标注口径与 PRD §6 的冲突**（§6 把「闲聊」列为 L1 的七类 intent 之一，固定集把
开放域闲聊标成 L2），不是代码回归——L1 关时那 5 条本来也对不了。门禁阈值**未改**，
需要的是裁决：要么承认 L1 口径下这 5 条算对，要么在固定集里把「开放域闲聊」单列 category。

### 8.9 并发测量（只读，不改代码）：3 并发 / 5 并发

方法：`.tmp/n33-conc.py` 在 backend 容器内用**真实 `LayaClient`** 发 N 个并发请求；宿主每
350ms 采样一次 `memory.stat` / `memory.current`。不 warm up、不改代码、不改 `mem_limit`。

| 并发 | 结果 | 单次 p50 | anon 峰值 | `memory.current` 峰值 | oom_kill | restarts |
| --- | --- | --- | --- | --- | --- | --- |
| 3（2 轮） | 每轮 2 成功 + 1 超时 | 1159.5 / 1385.2ms | 1623.2 MiB（基线 1612.9，**+10**） | 1763.6 / 2048 MiB | 0 | 0 |
| 5（2 轮） | 第 1 轮 2 成功 3 超时；第 2 轮 5 全超时 | 1519.2 / 1532.3ms | 1626.8 MiB（**+14**） | 1776.1 / 2048 MiB | 0 | 0 |

`memory.events` 全程 `oom 0 oom_kill 0 oom_group_kill 0`，`pgmajfault` 1370 → 1406。

**这组数字能证明什么、不能证明什么，必须分开写：**

- 能证明：现状下并发请求**不会**把常驻推过 2048m cap（+10~14 MiB 属于噪声），不 OOM、不重启。
- **不能**证明：noul 三问真并行后的内存增量——因为锁把并发压成了排队，本轮量到的是「排队」，
  不是「并行前向」。要回答那个问题必须改代码跑一次 spike，本轮明确禁止。
- 顺便证明了另一件事：**串行推理就是并发下的瓶颈**。3 并发时第 3 个请求必然排队吃满 1500ms，
  5 并发时几乎全超时。当时猜「三问并行能救」——**这个猜法已被 §8.11 的 spike 证伪**，
  不要再照着卡面去写一版更慢的代码。

### 8.10 性能拐点：线程数不是越多越好（S1 spike，`task-49`）

> 本节与 §8.11 来自一次**只读 spike**，代码只存在于 `<临时目录>\laya-spike\`（`probe.py` / `compose.yaml` /
> `results.json` / `threads-curve.md` / `REPORT.md`），**仓库内零改动**。
> 卡片原写「新增 §8.7 / §8.8」，与已有的 `is_out_of_domain`、对照跑两节撞号，故落在 8.10 / 8.11。

同一句「给我放个戏曲听听」、同一套四问、同容器同镜像，只改 `torch.set_num_threads()`
（即 compose 的 `LAYA_THREADS`）：

| 线程数 | p50 | p95 | max | 超 1500ms | `cpu/wall` | anon 峰 |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | 633.1ms | 735.0ms | 735.0ms | 0/6 | 1.98 | 1607.6 MiB |
| **4（当前）** | **536.7ms** | 694.9ms | 694.9ms | 0/8 | 3.88 | 1609.6 MiB |
| 8 | 1405.8ms | 2103.7ms | 2103.7ms | 3/6 | 6.44 | 1611.2 MiB |

8 线程确实用到了 6.4 个核（`cpu/wall=6.44`），却比 4 线程慢 2.6×——是核间争抢 + 内存带宽饱和
（WSL2 8 vCPU、无 swap）。

**结论：`LAYA_THREADS=4` 已是拐点。不要调大，也不要为了「利用多核」调到 8。**

### 8.11 不要拆问题并行（S1 spike）

读 SDK 源码（`laya==0.4.30`）：`Agent.system_one` → `predict_batch([state], …)`；`_encode_state`
（`agent.py:1210-1261`）给**每个问题造一行**序列，`predict_batch` 再把 `len(questions)` 行 collate 成
**一次前向**（`agent.py:1606-1616`）。也就是说：**代码里不存在「三问串行」可拆**——壳里那把
`threading.Lock` 串的是**整个请求**，问题之间早就是一次批前向。拆成线程 = 4 次单行前向，
FLOPs 一个都不少，只多付 4 次分词。

四个用例（同进程、同一模型、同一句，N=8/6）：

| case | 含义 | p50 | p95 | max | 超 1500 | `cpu/wall` | anon 峰 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | 4 问一次前向（现状） | 536.7ms | 694.9ms | 694.9ms | 0/8 | 3.88 | 1609.6 MiB |
| B | 4 问拆 4 线程 | 523.0ms | 589.2ms | 589.2ms | 0/8 | 5.20 | 1639.3 MiB |
| C | 3 请求去锁并行 | 1159.8ms | 1405.6ms | 1405.6ms | 0/6 | 5.53 | 1744.4 MiB |
| D | C+B（12 线程） | 1645.7ms | 1861.5ms | 1861.5ms | **6/6** | 5.82 | 1805.7 MiB |

- B 只快 **2.5%**（536.7 → 523.0ms）；p95 的 −15% 属批形状差异，不是并行本身带来的。
- C：单请求慢 **2.16×**，总吞吐 2.59 vs 1.85 req/s（+1.39×），`cpu/wall=5.53`；max 1405.6ms
  距 1500ms 只剩 **94ms** 余量。
- D：12 线程抢 8 核，**6/6 超 1500ms**，比现状更差。

**结论：并行化把排队从「锁」变成了「抢 CPU」，没有消失。** 内存不是否掉它的理由
（anon 1609.6 → 1805.7 MiB，`memory.events` 全程 `oom 0 / oom_kill 0`，cap 2048 MiB 未破），**核才是**。

**边界（别删，也别外推）：**

- 单次采样，N=8/6，模型只加载一次，未做重复实验；
- WSL2 8 vCPU、无 swap，与其余 8 个容器共存，线程扩展曲线受宿主争抢影响；
- 只测了一句「给我放个戏曲听听」（`state_tokens=8`、`input_tokens=305`，耗时几乎全在问题行的模型开销上）；
- 并发超时率是按**完成时间**统计，不是真 `LayaClient` 抓超时（那需要在壳里做取消）；
- 线程安全只验到答案逐字段与 A 相同（B 8/8、D 6/6），未做 stress，**SDK 无官方并发保证**。

## 9. 已知边界 / 后续

- 单进程串行推理（`threading.Lock`）：并发请求排队，峰值内存不随并发涨（见 §8.9 实测），但也不提吞吐。
- 20 条样本只够定方向，不够做验收数字（要 ≥110 条，见 `spikes/laya-system-one/README.md`）。
- 音频轮的真实 L1 取证仍缺：本机无可用麦克风，浏览器假麦 + WebRTC 需要证书链路；
  N33 用 REST/代码路径取证，音频轮留人手演示。
- `laya` 容器目前不在默认 profile 里（`profiles: ["laya"]`），演示时须显式
  `docker compose --profile laya up -d`。


## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：N31 服务壳、契约验收 16 项、SDK 选型对照、偏差登记三条 |
| v1.1 | 2026-11-10 | N32：容器化（镜像 1.83GB、换源构建、内存真相 1.44GiB、`/health` 503 语义、长语法挂载、去 `env_file`）；N33：`decision_logs.layer='L1'` 首个真实行、800ms 档抖动实测、`is_out_of_domain` 过度触发、开 L1 对照跑 88→83 |
| v1.2 | 2026-11-10 | 演示档 800ms → **1500ms**（Lead 裁决）+ 两轮 12 连打实测（首轮 7/12 超时、热轮 0/12，p50 500 / max 863.4）；新增 §8.9 只读并发测量（3/5 并发 anon +10~14 MiB、`oom_kill 0`、串行即瓶颈）；§9 补并发边界 |
| v1.3 | 2026-11-10 | S1 spike（`task-49`，仓库零改动）：新增 §8.10 线程数拐点（2 / 4 / 8 → 633.1 / 536.7 / 1405.8ms，8 线程 3/6 超时、`cpu/wall 6.44`，结论 `LAYA_THREADS=4` 不要调大）与 §8.11 不要拆问题并行（SDK 本就是一次批前向；B −2.5%、C 1.39× 吞吐但 +2.16× 延迟、D 6/6 超时）；§8.9 原「三问并行能救」的猜测标注为已证伪 |
