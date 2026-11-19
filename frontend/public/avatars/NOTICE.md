# 3D 形象与动作资产声明（NOTICE）

> 本文件由 `scripts/sync_avatar_assets.ps1` 自动生成，请勿手改；要更新清单就重跑脚本。

生成时间（UTC）：2026-11-12T10:33:42Z　｜　源目录：vrm-playground（本机绝对路径不入库）

## 1. 版权与分发红线

`models/` 下的 8 个 VRM 形象分别是 **原神（Genshin Impact）** 与 **星穹铁道（Honkai: Star Rail）** 的角色模型，
角色形象与模型版权归 **miHoYo / HoYoverse** 所有，模型由社区作者制作并经 vrm-playground 收集。

- **仅限本机演示与验收**，不得随仓库提交、不得对外分发、不得用于任何商业用途；
- 仓库 `.gitignore` 已忽略 `*.vrm` / `*.glb` / `*.vrma`，`frontend/public/avatars/models/` 与新增动作文件都只存在于开发机本地；
- **动作文件同模型一样不入库**：换机器、换 clone 后需重跑 `scripts/sync_avatar_assets.ps1` 重新同步，前端只认在册的 `catalog.json`；
- 若要对外演示或开源，必须换成可再分发的模型（如 VRoid Studio 自制 / CC0），
  届时只需重跑本脚本更换源目录，前端读的是 `catalog.json`，无需改代码。

`animations/` 下有两类动作，入库口径不同：

- **6 条新增动作**（`bundled: false`）：来自 vrm-playground 的 `common/` 与 animations 根目录，
  按 **Apache License 2.0** 引入（许可证全文见 `animations/LICENSE-Apache-2.0.txt`），但文件本身**不入库**，只留本机；
- **3 条既有动作**（`bundled: true`：`idle.vrma` / `idle-talking.vrma` / `nod.vrma`）：历史已 tracked、
  沿用原 README 的授权口径，本脚本只登记不覆盖，删掉会破前端 `DEFAULT_*_ANIMATION_URL` 契约。

## 2. 本次同步结果

已就位：模型 **8/8**，动作 **9** 条（新增 6 条 + 内置 3 条）。

**入库口径**：入库模型 **0/8**（全部本地）；入库动作 **3/9**（内置的 3 条在册，新增的 6 条本地未入库）。

### 2.1 形象（`models/`）

| id | 显示名 | 性别 | 文件 | 体积 | 权重档 | sha256（前 12 位） |
| --- | --- | --- | --- | --- | --- | --- |
| `lisa` | 丽莎 | female | `Lisa.vrm` | 15.73 MB | normal | `c3188a17acfa` |
| `shenhe` | 申鹤 | female | `Shenhe.vrm` | 12.2 MB | normal | `4172b9deb7d4` |
| `firefly` | 流萤 | female | `Firefly.vrm` | 13.88 MB | normal | `9b6c3c1d133f` |
| `sparkle` | 花火 | female | `Sparkle.vrm` | 11.74 MB | normal | `7fc3e74722cf` |
| `ningguang` | 凝光 | female | `Ningguang.vrm` | 10.53 MB | normal | `b8688f813ad9` |
| `jingyuan` | 景元 | male | `Jingyuan.vrm` | 13.14 MB | normal | `2ead5201068b` |
| `danheng` | 丹恒 | male | `Danheng.vrm` | 7.76 MB | light | `b31ffce747d4` |
| `neuvillette` | 那维莱特 | male | `Neuvillette.vrm` | 6.28 MB | light | `845fcb6a569a` |

### 2.2 动作（`animations/`）

| id | 场景 | 显示名 | 文件 | 体积 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `neutral` | idle | 静息 | `neutral.vrma` | 232 KB | playground common/ |
| `idle-2` | listen | 倾听变体 | `idle-2.vrma` | 433 KB | playground common/ |
| `idle-talking-6` | talking | 说话 | `idle-talking-6.vrma` | 547 KB | playground common/ |
| `world-sit-talking` | talking_sit | 坐姿说话 | `world-sit-talking.vrma` | 269 KB | playground common/ |
| `vrma-01` | show | 展示动作 | `VRMA_01.vrma` | 1317 KB | playground common/ |
| `dance-28` | talent | 才艺舞蹈 | `ダンス28.vrma` | 1886 KB | playground common/ |
| `bundled-idle` | idle | 内置静息 | `idle.vrma` | 189 KB | 仓库内置 |
| `bundled-talking` | talking | 内置说话 | `idle-talking.vrma` | 262 KB | 仓库内置 |
| `bundled-nod` | nod | 点头 | `nod.vrma` | 135 KB | 仓库内置 |

### 2.3 未同步条目

无。全部清单条目均已就位。

## 3. 前端怎么用

前端只读 `catalog.json`：`frontend/src/pad/avatar/loadAvatar.ts` 拉取并校验它，
按 `scene` 字段挑动作、按 `weight` 字段挑低配优先的形象；catalog 缺失或解析失败时
回落到代码里的硬编码默认值（`DEFAULT_AVATAR_URL` 等），不会白屏。

动作同理：`catalog.json` 里登记了、但本机没有对应文件的场景（新 clone / 没跑同步脚本），
`loadVrmAnimation` 会失败并被 `ensureSceneAction` 静默吞掉，角色退回内置的
`idle.vrma` / `idle-talking.vrma` / `nod.vrma` 三条，不会卡住渲染循环、也不触发 5s 降级。

