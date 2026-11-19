# 演示账号一览（本机）

> 状态：已冻结　｜　版本：v1.1　｜　日期：2026-11-16
> 关联：`scripts/seed_demo.py`、`scripts/seed_acceptance.py`（唯一写入来源）、`README.md` §5.2-A
> 上游依据：无（工程约定件，非 PRD/技术文档口径）
> 变更记录见文末

**这份文件只描述本机演示环境。** 所有口令都是公开的演示值，不是任何真实服务的凭据；生产环境不得使用（见文末「不可用于生产」）。

## 1. 访问入口

| 端 | 地址 | 说明 |
| --- | --- | --- |
| 长者端 pad | `https://localhost/` | 1920×1080 大字；证书自签，首次需在浏览器里「继续前往」 |
| 照护台 console | `https://localhost/console` | 护工 / 医生 / 管理员三角色按权限分流 |
| 家属端 family | `https://localhost/family` | 375px 移动优先 |
| 3D 自检页 | `https://localhost/diagnose.html` | 判断当前浏览器能否跑 WebGL |

局域网（同一 WiFi）把 `localhost` 换成宿主机 IP（用 `ipconfig` 查本机内网 IP）即可；手机需先把 `certs/dev.crt` 装成 **CA 证书**。

## 2. 四端账号

| 角色 | 登录入口 | 账号 | 口令 | 能看到什么 |
| --- | --- | --- | --- | --- |
| 管理员 | `/console` | `13700000000` | `Admin@2026` | 全部长者档案、用药管理（可读+代确认，**不能开药**）、每日日报、健康采集、消息中心、运维统计 |
| 护工 | `/console` | `13800000002` | `Care@2026` | 仅被分配长者的档案 / 健康 / 用药（可读+代确认）、消息中心；**看不到每日日报**（`reports_view` 只给管理员与医生） |
| 医生 | `/console` | `13600000001` | `Doc@2026` | 被分配长者的档案 / 健康 / 用药（**可开药、改药、停用**、代确认）、每日日报、消息中心 |
| 家属 | `/family` | `13900000002` | `Fam@2026` | 已绑定长者（张桂芬）的日报列表与详情、通知中心；**读不到家属档案**（A-01：`/api/elders` 一律 403） |

## 3. 长者端设备凭证

| 项 | 值 |
| --- | --- |
| 设备编号 | `11111111-1111-4111-8111-111111111106` |
| 设备密钥 | `demo-device-secret-0001` |
| 绑定长者 | 张桂芬（`11111111-1111-4111-8111-111111111102`，A-301） |
| 数字人形象 | 小满（`personas.id = 11111111-1111-4111-8111-111111111201`） |

pad 登录页有「**填入演示账号**」按钮，点一下自动填好上面两项（数据来自 `GET /api/pad/demo-login`，该端点在 `APP_ENV=production` 或未配置演示凭据时返回 404）。

> 设备密钥在库里只有 `devices.token_hash`（sha256），**无法从数据库反查明文**；明文只存在于 `scripts/seed_demo.py` 的常量与本文。丢了就重跑一次种入脚本。

## 4. 基础演示数据（`seed_demo.py`）

- 2 张药单：氨氯地平 5mg 每日 08:00、二甲双胍 0.5g 每日 12:30（均「启用中」）
- 11 条健康记录，覆盖最近 13 天但**故意缺第 4 天和第 9 天**（家属端趋势图会断线，这是刻意的）
- 4 份日报（最近 4 个自然日）
- 2 条通知（健康复核、家属日报）

上面是 `seed_demo.py` 单独跑出来的最小集；要做**全页面验收**请再跑一次验收数据包（见 §6）。

## 5. 重建 / 重置

```powershell
Set-Location <仓库根>
.\.venv\Scripts\python.exe scripts\seed_demo.py
```

脚本幂等：固定 UUID、按业务唯一键认领既有行、口令未变不重算哈希、`token_version` 不动（不会把你已登录的会话踢下线）。第二次跑输出应全为 `unchanged`。

若账号又失效，先确认是不是跑测试把开发库清了（`TRUNCATE`，见 `AGENTS.md` §9 第三条），跑测试请设：

```powershell
$env:ANYU_TEST_DATABASE_URL='postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test'
```

## 6. 验收数据包（`seed_acceptance.py`）

`seed_demo.py` 只保证「四端能登录、页面有内容」。要让**照护台每个页签、家属端每位家属、
平板每条主链路**都有可看的正反样例，再跑：

```powershell
Set-Location <仓库根>
.\.venv\Scripts\python.exe scripts\seed_acceptance.py          # 种入；末尾打印覆盖自检
.\.venv\Scripts\python.exe scripts\seed_acceptance.py --check  # 只检查覆盖，有缺口返回 1
```

它先 `await seed_demo._run()`，再复用 `scripts/seed_media.py` 与
`app/services/knowledge_seed.py` 两个既有入口，最后补场景数据。**不新增第二套账号**：
四端登录仍用 §2 的固定账号，新增长者只用于「多长者 / 多状态 / 多家属」的验收。

### 6.1 新增人物与状态

| 实体 | 关键值 | 用来看什么 |
| --- | --- | --- |
| 李秀兰 | `active`，B-302 1 号床，设备 online | 多长者隔离；家属端日报/通知；连续低自评触发「建议关注」 |
| 陈国强 | `away`，B-302 2 号床，药单已停 | 外出状态、停用药单、绑定已解绑（`unbound`） |
| 赵德海 | `transferred` | 已转出长者仍保留历史健康记录 |
| 孙玉梅 | `archived` | 归档状态筛选 |
| 周文华 | `active`，D-101，采集时刻 `20:30`，设备 `locked` | 自定义采集时段、锁定设备、复开工单 |
| 赵护工 | `care_worker` / `disabled` | 停用员工不再出现在可选受理人里 |
| 刘建国 | 家属 `13500000003` / `Fam2@2026` | 绑定李秀兰；与张女士的数据互不可见 |
| 王丽 | 家属 `13500000004` / `Fam3@2026` / `disabled` | 停用家属账号（不可登录） |

> 赵护工与王丽是**停用态样例**，不要拿来登录；能登录的新账号只有刘建国。

### 6.2 每个页面能看到什么

| 页面 | 覆盖的典型数据 |
| --- | --- |
| 照护台首页 | 6 位长者（4 状态）、5 台设备（online/offline/locked）、未结红黄工单、今日用药 3 条、红标复核、记忆切片 |
| 长者档案 | 有床/无床、四状态、采集时刻自定义；房间 available/partial/full/maintenance |
| 员工 / 家属 | 三角色 + 停用护工；两位家属、active/unbound 两种绑定 |
| 用药管理 | active/stopped、续方链（老单到期 + 新单未来生效）、7 天内到期复核、今日 waiting_confirm/confirmed（calling 只在提醒刚发出时出现，调度器会正常推进）、历史 missed/refused/notification_failed/late_taken，T0→+15 四次触达 |
| 健康采集 | green/yellow/red/missing 四档、四种来源、`revision=2` 补录、撤销留痕、趋势断线 |
| 作业看板 | 工单六态（含 reopened）、红黄分级、误报原因、24 条流转记录、六类风险事件 |
| 消息中心 | 7 类 kind × 3 档严重度 × 6 种状态，>20 条可翻页；含失败与模拟送达 |
| 每日日报 | 张桂芬 7 天、李秀兰 5 天、周文华 3 天；同一天有 v1 与补录后的新版本 |
| 长期记忆 | active/superseded/deleted、private/family、embedding 为 NULL 的降级样例 |
| 机构知识 | 4 份种子文档、1 份 retired 旧版、1 份 draft 草稿、13 个分块 |
| 操作日志 | staff/guardian/system/device 四类主体；越权拒绝、注入拦截、两种导出、今日与历史 |
| 家属端 | 张女士只看张桂芬，刘建国只看李秀兰；越界读长者/日报返回 403 |
| 长者端 | 今日待确认用药、数字人配置、健康/记忆/媒体素材均可演示 |

### 6.3 口径与边界

- **幂等**：固定 UUID + 业务键认领；同一天重跑不新增账号、不重复插工单/通知。
  日期相对「今天」的流水（健康、用药、日报、工单、通知、审计）换一天会补当天数据，
  **不删旧数据**。
- **知识向量是确定性兜底**：验收只验「链路是否走通」，不验召回质量。知识种子用
  `DeterministicEmbeddingProvider` 写入；要验证语义检索需另配真实 embedding key。
- **后台调度会继续产生通知**：backend 容器运行时，APScheduler 可能把风险事件升级成
  工单/通知，所以消息中心的条数会略多于种入值，这是服务在跑，不是种子重复。
- **不改设备凭据**：平板仍用 §3 的固定设备号与密钥；验收设备只是列表展示用，
  `token_hash` 由固定串派生，明文不可登录。

## 7. 不可用于生产

- 四份口令都是弱口令，且写在仓库里；
- `scripts/seed_demo.py` 有硬闸：`DATABASE_URL` 主机不在 `localhost` / `127.0.0.1` / `postgres` 时直接 `SystemExit`；
- `GET /api/pad/demo-login` 在 `APP_ENV=production` 时恒 404；
- 真部署请删掉本文与演示数据，改用 `scripts/create_account.py` 建强口令账号。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：汇总四端固定账号、设备凭证、演示数据清单与重建方式；与 `scripts/seed_demo.py` 对齐 |
| v1.1 | 2026-11-16 | 新增 §6 验收数据包：`scripts/seed_acceptance.py` 的覆盖范围、新增人物/状态、每页可见数据与幂等边界；原「演示数据里有什么」改名「基础演示数据」，明确它是 `seed_demo.py` 的最小集 |
