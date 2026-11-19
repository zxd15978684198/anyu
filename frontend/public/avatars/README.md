# 安语（Anyu）· 数字人静态资产

> 状态：已冻结　｜　版本：v1.0　｜　日期：2026-11-06
> 关联：无（迭代 5 的静态资产准备任务卡随过程记录移出仓库）
> 上游依据：`AGENTS.md` §4.11（不提交 `*.vrm`/`*.glb`）、§7「3D 加载降级 = 失败或 >5s 切静态头像」（PRD FR-3D-02）
> 本目录**只放静态资产**，不含任何代码；前端加载逻辑不在此处。

---

## 1. 目录内容

| 路径 | 说明 | 字节数 |
| --- | --- | --- |
| `animations/idle.vrma` | 自然待机（10s，循环） | 193,244 |
| `animations/idle-talking.vrma` | 站立说话（7.1s，循环） | 268,464 |
| `animations/nod.vrma` | 点头（1.767s，单次） | 137,860 |
| `animations/manifest.json` | 三个动画的清单（来源 URL / 许可 / sha256 / 时长） | 893 |
| `animations/LICENSE-Apache-2.0.txt` | Apache-2.0 许可正文，原样复制 | 11,358 |
| `fallback.png` | 1920×1080 静态头像兜底图（`scripts/gen_fallback_avatar.py` 生成） | 28,246 |

部署副本：`deploy/assets/avatars/fallback.png` —— 与 `frontend/public/avatars/fallback.png` 为**同一文件**（sha256 `db1df580c4bd652f327ce2c113b0ccd817f099d67ea5d17ce2b28646a64e28a0`），nginx 直接从该目录托管。

## 2. 来源与许可

- **上游**：[Undi95/Hanami](https://github.com/Undi95/Hanami/tree/main/vrma) 的 `vrma/` 动作库。
- **中间副本**：开发机本地的 vrm-playground 工作副本（其 `public/assets/vrm/animations/common/` 自带 `manifest.json`、`README.md`、`UPSTREAM-NOTICE.md`、`LICENSE-Apache-2.0.txt`）。
- **许可**：Apache License 2.0。这三个 `.vrma` 由 **Overte** 项目的头像动画转换而来（重定向到 VRM 1.0 humanoid 骨架、裁剪、重采样 30fps），属衍生作品，按 Apache-2.0 分发；**再分发时必须随附本目录的 `LICENSE-Apache-2.0.txt`**。
- **文件未做任何修改**：字节与上游一致，可用下列 sha256 复核。

| 文件 | 中文名 | 时长 | sha256 |
| --- | --- | --- | --- |
| `idle.vrma` | 自然待机 | 10s | `9563c582418bd5b51da473ea85b41722cb5304f84128935c3b570c72316c444a` |
| `idle-talking.vrma` | 站立说话 | 7.1s | `74f50c1b63bb8c2b19276dd7db26c9e8096da72acb521d4e635b059730e78bd8` |
| `nod.vrma` | 点头 | 1.767s | `c5f715092eacacc827b52c984ebd5c8b8d991b2e34102fbc03235b01c6bcbbe8` |

复核方式（Windows PowerShell）：

```powershell
certutil -hashfile frontend\public\avatars\animations\idle.vrma SHA256
certutil -hashfile frontend\public\avatars\animations\idle-talking.vrma SHA256
certutil -hashfile frontend\public\avatars\animations\nod.vrma SHA256
```

> 说明：源目录 README 记录这些动作已用 `VRMAnimationLoaderPlugin` 逐个解析，确认存在有效动画轨道与正数时长。本次交付只做了**字节级校验**（大小 + sha256）与 `manifest.json` 可解析校验，**未重复做动画内容解析**，也**未在具体角色上目视检查**——不同模型比例会影响手脚位置。

## 3. 三个动画的用途

| 文件 | 场景 | 播放方式 | 触发 |
| --- | --- | --- | --- |
| `idle.vrma` | **待机**：无对话、等待唤醒时的默认姿态 | 循环 | 页面进入稳定态、TTS 播报结束、会话超时回落到空闲 |
| `idle-talking.vrma` | **说话**：数字人播报 / 对话输出中 | 循环 | TTS 开始播放 → 切到本动画；播报结束 → 切回 `idle` |
| `nod.vrma` | **应答**：长者说完话、系统确认收到时的短促反馈 | 单次（播完即回 `idle`） | 语音识别结束、意图确认、用药提醒被应答 |

- 动作文件**不含家具与角色**，只有骨骼动画；坐姿类动作（如 `world-sit-*`）需要自行配合椅子与角色位置，本交付未包含。
- 同一时刻只应有一个"基础循环"（`idle` / `idle-talking`）在跑，`nod` 这类单次动作叠加播放后必须回到基础循环。

## 4. `fallback.png` 的定位

1920×1080 静态兜底图，用于 3D 形象不可用时**不白屏**：

- three-vrm / WebGL 初始化失败、模型文件 404、或加载**超过 5 秒**（PRD FR-3D-02）→ 展示本图。
- 纯本地生成（Python + Pillow：渐变底 + 人形图形 + 「安语」字样），**无外部素材、无联网下载**，不存在第三方版权问题。
- 与 `pad` 端 1920×1080 大字版式对齐，可直接铺满。

## 5. 红线（违反即返工）

1. **角色 `.vrm` 一律不入库。** 现有 15 个角色模型是《原神》《崩坏》系列的**二创**，原版权不在本项目，**不得复制进仓库、不得随构建产物分发**。仓库 `.gitignore` 已用 `*.vrm` 拦截（`.gitignore` 第 74 行），本目录与 `deploy/assets/avatars/` 均**不得**新增任何 `.vrm` / `.glb`。正式人设只能走**自建（自研或委托制作）**或**取得书面授权**两条路。
2. **Apache-2.0 许可文件不得删除或改写。** `animations/LICENSE-Apache-2.0.txt` 与 `manifest.json` 里的 `source` / `license` / `sha256` 是再分发的合规凭据，随动画文件一起走，不许"精简"掉。
3. **不替换、不重编码已有动画文件。** 需要新动作请**新增**文件并同步更新 `manifest.json`（含重算 sha256），不要覆盖这三个已校验的文件。
4. **本目录不放代码。** 加载器、降级逻辑、TTS 驱动口型等实现一律在 `frontend/src/` 内，本目录只提供静态文件。

> 备注（非本交付内容）：`deploy/assets/avatars/vrm1_sample.vrm` 是**任务开始前就存在**的本地调试样本，已被 `.gitignore` 的 `*.vrm` 拦截、不会入库，本次任务未复制、未修改、未删除它。

## 6. 形象目录 `catalog.json` 与同步脚本（v1.1 起）

W1（FR-3D-03：8 个形象 + 6 条动作）后，本目录多了一份**机器可读清单**，前端不再硬编码文件名：

| 路径 | 说明 | 是否入库 |
| --- | --- | --- |
| `catalog.json` | 形象与动作清单：`models[]`（id / 显示名 / 性别 / 文件 / url / 字节 / 权重档 / sha256）+ `animations[]`（id / 场景 / 文件 / url / 许可 / sha256） | ✅ 入库 |
| `NOTICE.md` | 生成物：版权与分发红线、本次同步结果、未同步条目 | ✅ 入库 |
| `models/*.vrm` | 8 个本地演示形象（原神 / 星穹铁道角色模型） | ❌ 由 `.gitignore` 的 `*.vrm` 拦截，**只在开发机本地** |
| `animations/{neutral,idle-2,idle-talking-6,world-sit-talking,VRMA_01,ダンス28}.vrma` | 6 条新增动作（Apache-2.0，来自 vrm-playground `common/` 与 animations 根目录，`catalog.json` 里 `bundled: false`） | ❌ 由 `.gitignore` 的 `*.vrma` 拦截，**只在开发机本地**（口径与 `.vrm` 一致：第三方二进制不进 Git 历史） |
| `animations/{idle,idle-talking,nod}.vrma` | 3 条内置动作（`bundled: true`），历史已 tracked、前端 `DEFAULT_*_ANIMATION_URL` 契约依赖它们 | ✅ 在册 |

生成与更新：

```powershell
# 只预览不落盘
pwsh -NoProfile -File scripts\sync_avatar_assets.ps1 -DryRun
# 首次同步 / 源目录新增文件后重跑（已存在且 sha256 相同的文件不会重复拷贝）
pwsh -NoProfile -File scripts\sync_avatar_assets.ps1
```

脚本对源目录缺失、单个文件缺失都是**容错**的：缺什么就记进 `catalog.json` 的 `skipped[]` 与 `NOTICE.md` §2.3，仍然以退出码 0 结束；动作按场景写进 `animations[].scene`，前端用 `buildSceneAnimationUrls()` 取用，缺场景就退回本 README §3 的内置三条。

> **换机器 / 新 clone**：本目录只有 `catalog.json`、`NOTICE.md`、3 条内置动作和静态兜底图；8 个形象与 6 条新增动作都在 `.gitignore` 里，需要在本机重跑 `scripts/sync_avatar_assets.ps1`。没跑也不白屏——`catalog.json` 里登记但文件缺失的场景会被 `ensureSceneAction` 静默吞掉，退回内置三条（`animations/manifest.json` 里这 6 条同样是登记项，克隆后对应文件不存在）。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初版：三个 Apache-2.0 动画 + 许可 + 清单 + 1920×1080 兜底图；明确 `.vrm` 不入库红线 |
| v1.1 | 2026-11-12 | W1（FR-3D-03）：新增 `catalog.json` / `NOTICE.md` / `scripts/sync_avatar_assets.ps1`；6 条新动作按场景进清单并同步 `animations/manifest.json`（`*.vrma` 已被 `.gitignore` 拦截，文件只留本机，内置 3 条仍在册）；8 个本地形象只落开发机不进 Git；前端改为目录驱动、硬编码降级为兜底 |
| v1.2 | 2026-11-18 | 兜底图改为 `scripts/gen_fallback_avatar.py` 可复现生成（原图印有旧品牌字样，非本仓库产物）；同步更新字节数与 sha256；`catalog.json` 的 `source` 与 `NOTICE.md` 的源目录只记源目录名、不落本机绝对路径 |
