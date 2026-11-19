# T5a spike 结果 · `vrm1_sample.vrm`

> 状态：已完成　｜　日期：2026-11-07（末次更新）　｜　关联：task-11（3D 接入代码）／task-15（自适应取景 + 状态接口）／task-24（复查修复）／plan day04 W4-T5a
> 目的只有一个：在把 three-vrm 接进 pad 之前，先证明**这份资产本身能跑**，且三个关键数字不离谱。
>
> 一~八节 = T5a（资产可行性）；**九~十一节 = T5b/task-15（正式代码的自适应取景与状态机实测）**；
> 十二节 = 多分辨率构图验证；十三节 = 懒加载拆分（他人卡）；**十四节 = task-24 四条缺陷的复查修复**。

## 一、结论（三个数字）

| 待验证 | 结论 | 数字 |
| --- | --- | --- |
| ① 能否加载 | **能**，`gltf.userData.vrm` 有值，`metaVersion = "1"`（VRM 1.0） | 加载到 VRM 对象 **306–448ms** |
| ② 首帧耗时 | **达标**（阈值 5000ms，PRD FR-3D-02） | 缓跑 **670–767ms**；首次冷跑 **2358ms**（着色器缓存为空）；稳态 **3.1–4.2ms/帧** |
| ③ 朝向是否正确 | **正确，无需额外旋转** | 判据 `leftHand.x − rightHand.x = +1.0862` → 面向镜头；`rotateVRM0` 前后**完全一致**（本资产是 VRM 1.0，`rotateVRM0` 只对 VRM 0.x 生效，故 `rotation.y` 保持 0） |

**一句话**：10.3MB 的 VRM1 资产在本机（Intel UHD 630）缓跑 0.67s 出首帧、4ms/帧、朝向天然正确，
`rotation_y_offset` 取后端默认 0 即可；5s 池子还剩 4 倍余量，但**冷启动 2.36s 说明「首次访问」不能按缓跑估**。

## 二、测量环境

| 项 | 值 |
| --- | --- |
| 资产 | `deploy/assets/avatars/vrm1_sample.vrm`，**10,776,032 字节** |
| URL | `http://127.0.0.1:5273/avatars/vrm1_sample.vrm`（与生产 nginx 同路径） |
| 网络 | 本机回环，资源传输 62–72ms |
| 渲染器 | `ANGLE (Intel, Intel(R) UHD Graphics 630, Direct3D11 vs_5_0 ps_5_0)` — 真 GPU，非 SwiftShader |
| 浏览器 | Playwright Chromium（headless） |
| 取景/光照 | 与正式代码同一套：fov 30、机位 (0,1.55,2.2)、看向 (0,1.45,0)、三灯光 |
| 计时口径 | `t0` = 调用 `loadAsync` 前；`firstFrameMs` = 首次 `renderer.render()` 返回 |

## 三、三次实测原始数据

```jsonc
// 首次访问（浏览器着色器缓存为空）
{ "loaded": true, "metaVersion": "1", "elapsedToVrmMs": 448, "firstFrameMs": 2357.8 }

// 复跑 1
{ "loaded": true, "metaVersion": "1", "elapsedToVrmMs": 306, "firstFrameMs": 669.7,
  "warmFrameMs": 4.2, "bytes": 10776332, "transferMs": 62.1,
  "dxBefore": 1.0862, "facingBefore": "toward_camera(+Z)",
  "dxAfter":  1.0862, "facingAfter":  "toward_camera(+Z)", "rotationYAfter": 0 }

// 复跑 2（带静息姿势）
{ "loaded": true, "metaVersion": "1", "elapsedToVrmMs": 363.6, "firstFrameMs": 767.3,
  "warmFrameMs": 3.1, "bytes": 10776332, "transferMs": 71.5,
  "dxBefore": 1.0862, "dxAfter": 1.0862, "rotationYAfter": 0,
  "relaxPose": { "drop": 1.222, "applied": true } }
```

首帧 2358ms → 670ms 的落差全在 MToon 着色器首次编译（约 1.9s），**与资产无关**，所以九端真机首访也可能比
复跑慢一倍以上 —— 这也是 5s 降级必须可靠的原因，不能拿复跑数字做验收依据。

## 四、朝向判据（不靠肉眼）

VRM 人形骨架里 `leftHand` 与 `rightHand` 的世界 x 之差就是脸的朝向：人物面向 **+Z**（镜头方向）时，
自己的左手在 **+X**，故 `dx = left.x − right.x > 0`。同一份数据在 `VRMUtils.rotateVRM0()` 前后各取一次：

| 时刻 | dx | 判定 |
| --- | --- | --- |
| `rotateVRM0` 之前 | +1.0862 | 面向镜头 |
| `rotateVRM0` 之后 | +1.0862 | 面向镜头（VRM 1.0 不动） |

⇒ 这份资产**不需要**任何额外 180° 补偿；后端 `rotation_y_offset` 默认 0 是对的。
⚠️ 该结论**只对 VRM 1.0 资产成立**。今后若换成 VRM 0.x 资产，`rotateVRM0` 会接管 π 旋转，
正式代码 `root.rotation.y += rotationYOffset` 已按「`rotateVRM0` 用赋值、我这边叠加」写成，无需改动。

## 五、附带修正：T-pose → 待机动画（已回填正式代码）

第一版截图暴露一个真问题：**资产没有待机动画时就是 T-pose**，双臂横伸、半身取景里两只手被裁到框外。

两条路都验了，结论是**用动画**：

| 方案 | 做法 | 结果 |
| --- | --- | --- |
| 静态静息姿势 | 上臂绕 Z 收 70°（左 −70°/右 +70°） | 可用的 A-pose，但像个立牌（不呼吸、不眨眼体感差） |
| **待机动画（采用）** | `VRMAnimationLoaderPlugin` 读 `frontend/public/avatars/animations/idle.vrma`（task-12 交付，Apache-2.0）→ `createVRMAnimationClip(vrma, vrm)` → `mixer.clipAction(clip).play()` | **重定向到本模型正常**，双臂自然下垂、有呼吸起伏 |

动画实测（`?idle=1`）：

```jsonc
{ "idleClip": { "url": "/anims/idle.vrma", "duration": 10, "tracks": 21 },
  "elapsedToVrmMs": 417.9, "firstFrameMs": 929.2, "warmFrameMs": 4.3,
  "facingAfter": "toward_camera(+Z)", "error": null }
```

- 时长 **10.000s / 21 条轨道**，与 `frontend/public/avatars/animations/manifest.json` 的 `durationSeconds: 10` 一致。
- 带上动画后首帧 929ms（比空跑多约 260ms，多为 clip 解析与骨骼绑定），仍在 5000ms 预算内。
- **动画是异步补齐的**：正式代码里它不参与首帧竞速，失败只回落到静息 A-pose，不会把整台 3D 拖进降级。
- 本 spike 用 `/anims/` 单独挂 `frontend/public/avatars/animations/`（生产路径是 `/avatars/animations/*`），
  以免往 `deploy/assets/avatars/` 塞文件（该目录只放 `.vrm` 调试样本）。

## 六、复现步骤

```bash
cd spikes/avatar
pnpm install          # three 0.180.0 / @pixiv/three-vrm 3.5.3 / …-animation 3.5.3
node serve.mjs        # → http://127.0.0.1:5273
```

打开页面即可，右侧 `<pre>` 单调打印上面那份 JSON；`window.__SPIKE__` 可被脚本直接读取。
`?idle=1` 挂待机动画，`?drop=0|70` 调静息姿势角度。

| 文件 | 作用 |
| --- | --- |
| `index.html` | 原生 import map（`three` / `three/examples/jsm/` / `@pixiv/three-vrm` / `@pixiv/three-vrm-animation`） |
| `main.js` | 加载三连 + 朝向探针 + 首帧/稳态计时 + 待机动画 + 静息姿势（`?idle=` `?drop=`） |
| `serve.mjs` | 约 30 行静态服务器，`/avatars/*` → `deploy/assets/avatars/*`（复刻生产路径）、`/anims/*` → 前端动画目录 |

## 七、坑（已定性，别再踩）

1. **pnpm 12 默认禁 build script**：`pnpm install` 装 vite 会报
   `ERR_PNPM_IGNORED_BUILDS … Ignored build scripts: esbuild@0.28.2`，`vite dev` 因此起不来。
   spike 不需要打包，**改用浏览器原生 import map + 静态服务器**，一个构建步骤都不留。
   （`pnpm` 顺手生成的 `pnpm-workspace.yaml` 已删。）
2. `performance.getEntriesByName()` 按相对路径查不到条目 —— 必须用完整 URL 或按 `includes()` 过滤。
3. 三个裸说明符缺一个就白屏报 `Failed to resolve module specifier`：`three`、`three/examples/jsm/`、
   `@pixiv/three-vrm`。

## 八、失败时的备选方案（未触发，留档）

若后续实测超 5s 或加载失败：换 ≤15MB 备选资产（plan day04 W4-T5a 的退出条件）；
资产本身 10.3MB + 回环 62ms 已足够，**当前不需要回避**。

---

# 九、T5b · 半身取景自适应（改前 / 改后）

## 9.1 为什么改

原机位是从 playground 原样搬来的常量：`position (0, 1.55, 2.2)` / `target (0, 1.45, 0)`。
它隐含「头骨离地 1.55m」这一前提，而本调试样本实测 **`headY = 1.386m`** ——
镜头按 1.55m 的个子架，画面里人就显得**又小又偏下**（头顶留一大片白，肩线掉到中部）。

## 9.2 怎么改（一个纯函数，可单测）

`frontend/src/pad/avatar/VrmAvatar.ts` 的 `computeBustFraming(headY)`：

```
scale    = clamp(headY / 1.55, 0.7, 1.4)
position = [0, headY, 2.2 * scale]
target   = [0, headY - 0.1 * scale, 0]
```

- `headY = 1.55` 时**恰好退回原常量**，所以改前/改后可以直接对照，不是两套逻辑。
- 限幅 0.7~1.4 防极端身材（儿童模型 / 带缩放的巨人体）把镜头怼到脸上或退到墙外。
- `headY` 取 `humanoid.getNormalizedBoneNode("head")` 的世界坐标；取不到、非有限值、或落在
  (0.5m, 2.6m) 之外 → 回落原常量（`source: "fixed"`）。

## 9.3 实测（同一资产、同一套光照）

| 项 | 改前（fixed） | 改后（auto） |
| --- | --- | --- |
| 机位 | `(0, 1.55, 2.2)` | `(0, **1.386**, **1.967**)` |
| 看向 | `(0, 1.45, 0)` | `(0, **1.297**, 0)` |
| 缩放系数 | 1 | **0.8942** |
| 首帧 | 753.3ms | 1377.7ms（**缓存噪声**，见下） |
| 稳态 | 5.2ms/帧 | 4.7ms/帧 |

> 首帧那两个数**不是取景的开销**：取景是 O(1) 纯数学（读一次头骨世界坐标）。
> 两次跑分别是冷/热着色器缓存，浮动量级与 T5a 里 670↔2358ms 的同源噪声一致。
> 取景真正的收益在**画面构图**：截图对比里 auto 的头顶留白收窄、双肩完整进画，
> fixed 则明显偏小偏下。

## 9.4 回归

- 朝向不受影响（`dx` 仍 +1.0862，`rotation.y` 仍为 0）。
- `idle.vrma` 仍是 10.000s / 21 tracks，重定向正常。
- 降级路径（拿不到 VRM）走的是 `<img>`，根本不进取景逻辑。

---

# 十、T5b · `setSpeaking()` / `nod()` 状态接口（正式代码实测）

被测对象**不是**spike 里另写的一份，而是 `frontend/src/pad/avatar/VrmAvatar.ts` 本体
（`spikes/avatar/prod-check.ts` 直接 `import` 它，用 esbuild 打成一次性 bundle 在真浏览器跑）。

## 10.1 结果总表

| # | 动作 | 期望 | 实测 |
| --- | --- | --- | --- |
| ① | `setSpeaking(true)` | idle 淡出 → 0，talking 淡入 → 1 | **idle 1→0 / talking 0→1**，`baseKind` 切 `talking` ✅ |
| ② | 同值重复 `setSpeaking(true)` | 幂等：不重播、不重置淡化 | 权重保持不变 ✅ |
| ③ | `setSpeaking(false)` | talking → 0，idle → 1 | **talking 0 / idle 1** ✅ |
| ④ | `nod()` | nod → 1，基线让位 | **nod 1 / idle 0**，`nodding=true` ✅ |
| ⑤ | nod 播完 | 平滑淡回基线 | 抓到中间态 `idle 0.382 / nod 0.618` → 下一采样点 **idle 1 / nod 0** ✅ |
| ⑥ | 说话中 `nod()` ×2（重入） | 不抛、重播、结束后回 talking | 无异常；结束后 **talking 1 / nod 0** ✅ |
| ⑦ | `dispose()` 后调用 `setSpeaking` / `nod` / `setMouthOpen` / 重复 `dispose` | 空操作，不抛 | 四项全部无异常，`clips: []`、权重归零 ✅ |
| ⑧ | `talking` / `nod` 动画 404（`?mode=missing`） | 静默留在 idle，不抛、不切空气 | `setSpeaking(true)` 后 `baseKind="idle"`、`idle=1`；`nod()` 后 `nodding=false` ✅ |

单次实测（`window.__PROD__`）：

```jsonc
{ "loadMs": 374, "firstFrameMs": 1786, "idleLoaded": true, "nodEnded": true, "done": true }
```

三个槽位的 clip 均解析成功：`clips: ["idle", "talking", "nod"]`。

## 10.2 动画缺失时的降级（`?mode=missing`）

把 `talkingAnimationUrl` / `nodAnimationUrl` 指向不存在的 `.vrma` 再跑一遍：

| 调用 | 结果 |
| --- | --- |
| `setSpeaking(true)` | **不抛**，`baseKind` 仍是 `idle`、`idle` 权重 1 —— 不会切到尚未存在的动画上 |
| `nod()` | **不抛**，`nodding` 始终 `false` —— 「点不了这个头」好过卡在半截姿态 |
| 30 帧后 | 权重序列不变（没有淡出后回不来的中间态） |

⇒ 动画是**纯增强**：三条 `.vrma` 全 404 时角色仍以静息 A-pose 正常站立（第五节），
与「加载失败不白屏」是同一条设计约束。

## 10.3 复现命令

```bash
# 1) 打包（用 frontend 自带的 esbuild，spike 目录没有打包器）
cd frontend
node_modules/.pnpm/@esbuild+win32-x64@0.28.2/node_modules/@esbuild/win32-x64/esbuild.exe \
  ../spikes/avatar/prod-check.ts --bundle --format=esm \
  --outfile=../spikes/avatar/prod-check.bundle.js

# 2) 起静态服务器后打开 http://127.0.0.1:5273/prod.html
cd ../spikes/avatar && node serve.mjs
```

结果写在 `window.__PROD__` 与页面右侧 `<pre>`；`document.body.dataset.ready === "1"` 表示跑完。
`prod-check.bundle.js` 是**一次性产物，不入库**（含 three，1.6MB）。

---

# 十一、坑（T5b 新增两条，务必先读）

1. **headless Chromium 空闲时会冻结 rAF —— 用 `setTimeout` 采样会得到假结论。**
   第一轮验证跑了 720ms，`requestAnimationFrame` 计数一动没动（23 → 23），
   `mixer.update()` 因此从没被执行，读到的权重全是调度瞬间的陈旧值
   （出现过 `idle 1 / talking 1` 同时为 1、nod 结束后 `nod 1 / idle 0` 这种「像 bug」的数字）。
   **改成「主动请求帧」驱动**（`await new Promise(r => requestAnimationFrame(r))`）后
   所有断言一次通过 —— 状态机本来就是对的。
   ⇒ 以后任何「动画/时间」相关的前端验证，时钟必须用 rAF 而非 `setTimeout`。

2. **`three@0.180.0` 不自带类型定义**，`--noEmit` 会报找不到声明；
   必须补 `@types/three@0.180.0`（devDependency）。已随任务落盘并经 Lead 追认。

3. （环境）`@esbuild/win32-x64` 的原生二进制不在 `node_modules/.bin` 里，
   要走 `node_modules/.pnpm/@esbuild+win32-x64@<ver>/node_modules/@esbuild/win32-x64/esbuild.exe`。

---

# 十二、多分辨率构图验证（只读验收，2026-11-06）

> 目的：确认 3D 舞台在 1920×1080（DPR1/DPR2）、1366×768、2560×1440 下构图是否一致、resize 是否重取景、降级图是否变形。
> **本章结论全部只读得出，`frontend/src/` 一行未改。**

## 12.1 环境与方法

- 页面：`frontend/dist` 构建产物，`pnpm --dir frontend exec vite preview --port 4173 --strictPort`
  （避开 tests-runner 占用的 8000/5173）；Playwright Chromium（headless，本机 Intel UHD 630 / ANGLE D3D11）。
- 无后端：`page.route` 打桩 `GET /api/pad/avatar-config`（3D 档 → `/avatars/vrm1_sample.vrm`；降级档 → `/avatars/__missing__.vrm`），
  `/avatars/vrm1_sample.vrm` 直接回真实文件字节；`sessionStorage` 注入设备会话（`kb_access_token` / `kb_expires_at` / `kb_device`）以进入 MainScreen；
  `WebSocket` 桩成「永不失败」，避免 `onAuthFail → clearSession` 把页面打回登录页（与构图无关）。
- DPR2：用 JS 覆写 `window.devicePixelRatio`，验证 `renderer.setPixelRatio(min(dpr,2))` 路径与后备缓冲尺寸；
  **截图仍是 1× 物理像素**（见 12.7 未验项）。
- 量化：Pillow 按 canvas 的 `getBoundingClientRect` 裁图（内缩 1px 跳过圆角/边框，只统计 10%~90% 高度带），
  以舞台底色 `#eeeaf1` 为背景阈值（Δ>10）提取人物轮廓。

## 12.2 机位（三档相同）

取景只取决于模型头高，与视口无关：

| 项 | 值 | 说明 |
| --- | --- | --- |
| `headY` | 1.386 | 本资产（VRM 1.0，10.3MB 调试样本）head 骨世界 Y，spike 探针实测 |
| `computeBustFraming()` → `source` | `auto` | headY 在 (0.5, 2.6) 合法区间内，未回落常量 |
| `scale` | 0.8942 | `clamp(headY / 1.55, 0.7, 1.4)` |
| 相机位置 | `(0, 1.386, 1.967)` | `[0, headY, 2.2·scale]` |
| 看向 | `(0, 1.297, 0)` | `[0, headY − 0.1·scale, 0]` |
| FOV / near / far | 30° / 0.1 / 100 | 与 playground 一致 |

## 12.3 三档实测（3D 路径）

| 视口 | DPR | 舞台容器 (CSS px) | canvas (CSS px) | 后备缓冲 | 头顶留白 | 留白占比 | 肩部左右余量 | 左右裁切 | 黑边/letterbox | 截图 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1920×1080 | 1 | 518.4 | 516.8 | 517×517 | 100 px | 19.4% | 147 / 163 px | 无 | 无（dark rows/cols = 0） | `spikes/avatar/shots/pad-1920x1080-dpr1-vrm.png` |
| 1920×1080 | 2 | 518.4 | 516.8 | 1034×1034 | 101 px | 19.6% | 148 / 164 px | 无 | 无 | `spikes/avatar/shots/pad-1920x1080-dpr2-vrm.png` |
| 1366×768 | 1 | 368.64 | 367.04 | 367×367 | 71 px | 19.4% | 112 / 116 px | 无 | 无 | `spikes/avatar/shots/pad-1366x768-vrm.png` |
| 2560×1440 | 1 | 560（封顶） | 558.4 | 558×558 | 108 px | 19.4% | 170 / 177 px | 无 | 无 | `spikes/avatar/shots/pad-2560x1440-vrm.png` |

（肩部余量 = 0.55~0.78 画布高带内人物左/右边缘到画布边的像素距离。三档在 10%~90% 高度带内**没有任何一行**出现贴边像素，`edge_rows_in_mid = 0`。）

读三条：

1. 容器尺寸由 `min(560px, 48vh)` 决定 → 1080 高给 518.4、768 高给 368.64、1440 高撞 560 封顶。
   **构图本身与视口无关**：留白占比恒 19.4%，人物水平居中（轮廓中心 = 画布宽的 0.499）；
   因为相机距离按 headY 定标、容器又恒为正方形，三档就是同一构图的三次等比缩放。
2. DPR2 只把后备缓冲从 517 提到 1034（`setPixelRatio(min(dpr,2))` 生效），CSS 尺寸与构图不变。
3. 躯干下端切在画布底边（bust 取景的预期行为），非缺陷；若要「完整上半身 + 底边留白」，需改 `CAMERA_DISTANCE` / `HEAD_TO_TARGET`，属实现变更，本次未做。

## 12.4 resize 行为：**不重取景**

1920×1080 → 1366×768 实时改窗口（不刷新页面）：

| 指标 | 实测 |
| --- | --- |
| 事件顺序 | ResizeObserver 回调**先于** `resize` 事件（实测早 12.5 ms） |
| RO → 首个新构图帧 | **17.4 ms** |
| 帧间隔 | 16.6 ms（≈60 Hz） |
| canvas 后备缓冲 | 517×517 → 367×367 |
| 头顶留白占比 | 0.1942 → 0.1918（差 0.0024，≈0.9 px @365） |
| 截图 | 改前 `spikes/avatar/shots/pad-resize-1-before-1920x1080.png`，改后 `spikes/avatar/shots/pad-resize-2-after-1366x768.png` |

结论：**只改 aspect 与渲染分辨率，不重新推导 headY**。源码口径一致——`ResizeObserver` 里只做 `renderer.setSize()` + `camera.aspect`（容器恒正方形，aspect 恒 1），
`headY` 只在模型加载完成后读一次骨骼世界坐标。留白占比的 0.24% 差异来自 idle 动画的微幅摆动，不是重取景。

## 12.5 降级路径（`object-fit`）

降级档（config 的 `avatar_url` 指向不存在的 `.vrm`）：

| 视口 | 图片框 | 源图 | `object-fit` | 实际可见源宽 | 左右各裁 | 与 cover 期望的 MAE | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1920×1080（DPR1/DPR2） | 516.8² | 1920×1080 | cover | 1080 px | 420 px | 2.20 | 中心裁切，与 cover 一致 |
| 1366×768 | 367.04² | 1920×1080 | cover | 1080 px | 420 px | 1.77 | 同上 |
| 2560×1440 | 558.4² | 1920×1080 | cover | 1080 px | 420 px | 1.08 | 同上 |

- **无拉伸、无留白、无黑边**：MAE 1.08–2.20（判据 <8），与「等比放大填满 + 居中裁切」的理论图逐像素吻合。
- 裁掉的是 1920 源图左右各 420 px；`fallback.png` 的品牌图案（图标 +「安语 / Anyu · 数字人形象加载中」）居中，三档下均完整可见
  （见 `spikes/avatar/shots/pad-1366x768-fallback.png`）。
- 字节一致性：`deploy/assets/avatars/fallback.png` 与 `frontend/public/avatars/fallback.png` 的 sha256 均为
  `0D440E9B02A02812CC12D7416E4AF9C9BD6C02EBC6EE3E4DCD109E716E6FCAD3` ✅

## 12.6 产物卫生

- `spikes/avatar/prod-check.bundle.js` **已删除**（`Test-Path` → False）。
- 排除 `node_modules` 后，`spikes/avatar/` 最大的 5 个文件全是本次截图：80,324 B / 69,821 B / 69,578 B / 67,855 B / 53,700 B，
  **无 >1 MB 产物**（`>1MB` 计数 = 0）。`node_modules/` 里的 esbuild/rollup 二进制属 pnpm 正常内容，已被 gitignore。

## 12.7 未验项

1. **真实 2× 屏幕像素**：DPR2 是 JS 覆写模拟，截图仍为 1× 物理像素；验的是后备缓冲尺寸与该渲染路径，不是真实 Retina 上屏清晰度。
2. **真实大屏/投屏硬件**：三档都在同一台机器的 headless Chromium 视口里完成，未在 2560 物理屏/投影仪上复核颜色与视角。
3. **窄屏（<1366）**：pad 只面向 1920×1080 大字平板，未测 375/768 等移动视口（非目标场景）。
4. **长时间稳态帧率**：此前 spike 的 3.1–4.3 ms/帧取自 517² 画布，大屏 558² 未单独计时，也未测长时间发热降频。
5. **真实后端 `avatar-config`**：本次全部打桩（无后端进程），未验 403 `device_not_bound` 等真实返回。

## 13 3D 区域懒加载（task-23，2026-11-07）

### 13.1 改前 → 改后（同机 `pnpm --dir frontend build`）

| 产物 | 改前（task-18 后） | 改后（task-23） | 变化 |
| --- | --- | --- | --- |
| pad 入口 `assets/pad-*.js` | `pad-CQdU5eI9.js` **780.28 kB**（gzip 203.89） | `pad-Dwn2Cigy.js` **31.94 kB**（gzip 13.07） | **−748.34 kB（−95.9%）** |
| three-vrm 懒加载 chunk | 无（与入口同包） | `assets/AvatarStage-DwuD5y0G.js` **750.02 kB**（gzip 191.75） | 新增，首屏不下载 |
| 共享 chunk | `client-BaUToD7D.js` 222.44 kB | 同文件同大小 | 未变 |
| 构建 | 57 modules | 69 modules，exit=0，3.74s | — |

验收线「pad 入口 < 80 kB（未压缩）」→ **31.94 kB，达成**；比接 3D 之前的 26.18 kB 只多 5.76 kB
（lazy 包装 + Suspense 占位组件 + 3D 接线本身）。

### 13.2 改法与证据

只动 `frontend/src/pad/App.tsx`：

1. 删静态 `import { AvatarStage } from "./avatar/AvatarStage";`，改用
   `lazy(() => import("./avatar/AvatarStage").then((m) => ({ default: m.AvatarStage })))`——
   `AvatarStage` 是**具名导出**，`lazy` 要 default，故用 `.then` 映射；`frontend/src/pad/avatar/**` 一字未动。
2. 3D 区域包 `<Suspense fallback={<AvatarStageFallback />}>`；fallback 复用 AvatarStage 内部 loading 态的容器几何
   （`min(560px, 48vh)` 正方形、`rounded-[2rem]`、`bg-brand-soft`、`border-brand-line`），内文「数字人加载中…」，**不白屏**。
3. `onDiag` 原样透传（`onReady` App 当前未用；AvatarStage 的 props 形状未变、未改其签名）。

证据：

- `frontend/dist/index.html`（pad 入口）只引 `assets/pad-Dwn2Cigy.js` + `modulepreload client-BaUToD7D.js` + `pad-*.css`，
  **没有** AvatarStage 的静态引用/preload → 真懒加载，首屏不再下 750 kB。
- `three-vrm|MToon` 逐文件命中数：`AvatarStage-DwuD5y0G.js` **22**；`pad-Dwn2Cigy.js` 0、`client-BaUToD7D.js` 0、
  `family-*.js` 0、`console-*.js` 0 → three-vrm 只在懒加载 chunk 内，三入口隔离未破。
- 构建 `exit=0`（`tsc --noEmit && vite build`，✓ 69 modules，✓ built in 3.74s）。仍有 `> 500 kB` chunk 警告，
  指向 `AvatarStage-*.js` 750.02 kB——3D 的 750 kB 分量没变，只是从**首屏**挪到**按需**。

### 13.3 注意事项与未验项

1. **几何值跨文件重复**：fallback 的 `min(560px, 48vh)`、圆角与底色是照 `AvatarStage.tsx:156-160` 抄的常量，
   改一边要同步另一边（本卡未改 `avatar/` 内任何文件）。
2. **弱网表现未测**：只证明首屏不再含 750 kB；懒 chunk 下载期间的实际观感（本机回环、无网络节流）未做弱网模拟。
3. **dev 模式**：`pnpm dev` 下 vite 按需编译，本结论只对生产构建成立。
4. **750 kB 仍是单 chunk**：要再降需按 three / three-vrm 二次拆分，超出本卡范围。

---

## 十四、task-24 复查修复（2026-11-07）

> 编号说明：本节原计划写成「十三」，但十三已被懒加载卡占用，故顺延为十四。
> 结论一句话：**四条缺陷全部修好，五个探针场景全绿**；`renderer.info.memory` 归零、dispose 后零绘制、
> 上下文丢失自动切静态图、超时迟到的 10.3 MB 资产被释放——都有探针实测数字，不是「应该」。

### 14.1 四条缺陷 → 修法 → 证据

| # | 缺陷（原状） | 修法（文件:行） | 验证场景与结果 |
| --- | --- | --- | --- |
| P1-1 | 超时后 `loadPromise` 继续解析，10.3 MB 的 VRM 场景无人释放 | `loadAvatar.ts:155-195`：`timedOut` 先立旗再 reject，迟到的 `then` 里走 `releaseLate` → `disposeAvatar()`（`:68-74`，deepDispose + 清 `meta.thumbnailImage.src`）；`no_vrm` 分支也先 `disposeScene`（`:75-78`） | `?case=timeout`：失败时刻 dispose 计数 `0/0/0` → 迟到资产到达后 `13/50/20` → **`lateReleaseObserved: true`** |
| P1-2 | 无 `webglcontextlost` 监听；`ok:true` 在首帧前就上报 | `VrmAvatar.ts:396-414` 加 `webglcontextlost/restored`（lost 时 `preventDefault` + 停 rAF，restored 时重启同一 renderer）；`AvatarStage.tsx:86,131-149` 把 `ok:true` 与 `stage=vrm` 挪到 `onFirstFrame`，丢失时 `ok:false,error:"context_lost"` | `?case=lost`：首帧后 `ok:true` → `loseContext()` 后 canvas `opacity 0` + `<img>` 回退 + diag `context_lost` → `restoreContext()` 后 canvas 回 `opacity 1`、img 消失 |
| P2-7 | `dispose()` 不还上下文，GPU 侧留痕 | `VrmAvatar.ts:430-436`：`disposeAvatar` → `renderer.dispose()` → **`renderer.forceContextLoss()`** | `?case=handle`：`getStats()` 从 `{geometries:12, textures:36, programs:8, contextLost:false}` → **`{0,0,0,contextLost:true}`** |
| P2-8 | `React.lazy` 无 ErrorBoundary，chunk 失败整页白屏 | 新文件 `avatar/AvatarBoundary.tsx`（class 边界 + 静态头像回退，自带 SVG 剪影，**不 import** 任何 three 文件，故不会把 750 kB 拖回主 chunk）；`App.tsx:11,119-123` 套在 `<Suspense>` **外面** | `?case=chunk`：模拟 `lazy` 抛 `Failed to fetch dynamically imported module` → **353ms** 出静态头像、`pageNotBlank: true`、图上 `1920x1080` |

补充证据（`?case=handle`，正式模块 `createAvatar`）：

```json
{"loaded":true,"firstFrameMs":2677,
 "statsAfterFirstFrame":{"geometries":12,"textures":36,"programs":8,"contextLost":false},
 "framesDrivenAlive":3,"drawsWhileAlive":54,
 "statsAfterDispose":{"geometries":0,"textures":0,"programs":0,"contextLost":true},
 "stateAfterDispose":{"disposed":true,"baseKind":null,"clips":[],"weights":{"idle":0,"talking":0,"nod":0}},
 "framesDrivenAfterDispose":4,"drawsAfterDispose":0,
 "rafIdleGrowthAfterDispose":0,"noRafResidue":true}
```

`drawsWhileAlive=54 / 3 帧`（≈18 次 draw/帧）对 `drawsAfterDispose=0 / 4 帧` 是最硬的证据：
**帧循环真的停了**，不是「没人 await 所以看起来停了」。

### 14.2 五个探针场景（`spikes/avatar/task24.html?case=…`）

| 场景 | 断言 | 实测 |
| --- | --- | --- |
| `bad`（404 资产） | 5s 内降级 + diag 带原因 | `elapsedMs` 两轮 56 / 2054（帧驱采样误差，见下）、`img 1920x1080`、diag `{ok:false,error:"HTTP 404"}`、`within5s true` |
| `timeout`（资产 6.5s 才到） | ≤5s 降级，且迟到资产被释放 | 超时判定 **5017ms**、`<img>` 落地 **5019ms**（MutationObserver 计时）、dispose 计数 `0→13/50/20` |
| `lost`（上下文丢失/恢复） | 丢失切图、恢复回 canvas | 见 14.1 P1-2 |
| `chunk`（懒 chunk 失败） | 静态头像 + 不白屏 | `msToStaticFallback 353`、`pageNotBlank true` |
| `handle`（控制器契约） | 内存归零、无残留帧、dispose 后调用不抛 | 见 14.1 补充证据 |

> 计时说明：headless Chromium 空闲时会冻结 rAF（任务卡 15 已踩过），所以
> 「降级发生在几毫秒」一律用 `MutationObserver` + `performance.now()` 记，不用帧驱采样；
> 帧驱采样只用来数「渲染/绘制还在不在发生」。

### 14.3 构建与三入口隔离（修复后重跑）

```
dist/assets/pad-ic1jsnA8.js           34.71 kB │ gzip:  13.99 kB   ← pad 入口，hits=0
dist/assets/AvatarStage-DceErPX7.js  751.18 kB │ gzip: 192.17 kB   ← 懒加载 chunk，hits=75
dist/assets/client-BaUToD7D.js       222.44 kB │ gzip:  69.27 kB   ← hits=0
dist/assets/console-UxqHN2Kd.js        5.99 kB │ gzip:   2.53 kB   ← hits=0
dist/assets/family-CzRoBLQ1.js         5.93 kB │ gzip:   2.52 kB   ← hits=0
✓ built in 3.21s（build exit=0）
```

关键字 `three-vrm|@pixiv|VRMUtils|MToon|VRMAnimationLoaderPlugin|WebGLRenderer|VrmAvatar` 逐文件命中数如上；
三个 HTML 的引用：`index.html → pad + client`、`console.html → console + client`、`family.html → family + client`
——**pad 入口不含 three，three 只在按需 chunk 里**（AGENTS §2 满足）。

### 14.4 复现命令

```powershell
# 0) 前置：探针从 spikes/avatar 入口打包，react 在 frontend/node_modules 里，需先建三个 junction（一次性，node_modules 不入库）
cd spikes\avatar\node_modules
New-Item -ItemType Junction -Name react      -Target ..\..\frontend\node_modules\react
New-Item -ItemType Junction -Name react-dom  -Target ..\..\frontend\node_modules\react-dom
New-Item -ItemType Junction -Name scheduler  -Target ..\..\frontend\node_modules\.pnpm\scheduler@0.28.0\node_modules\scheduler

# 1) 起探针静态服务器（127.0.0.1:5273，映射 /avatars/* → deploy/assets/avatars、/avatars/animations/* → frontend/public/avatars）
node spikes/avatar/serve.mjs

# 2) 打包探针（esbuild 不在 .bin，用原生二进制；--define 是为了让 import.meta.env.DEV 在 bundle 内可用）
#    bundle 产物 ~2.2 MB，跑完即删，不入库
cd frontend
node_modules\.pnpm\@esbuild+win32-x64@0.28.2\node_modules\@esbuild\win32-x64\esbuild.exe `
  ..\spikes\avatar\task24.ts --bundle --format=esm --jsx=automatic `
  --define:import.meta.env.DEV=false --define:process.env.NODE_ENV='"production"' `
  --outfile=..\spikes\avatar\task24.bundle.js

# 3) 打开 http://127.0.0.1:5273/task24.html?case=bad|timeout|lost|chunk|handle
#    读 window.__T24__（timeout 场景需先把 /avatars/vrm1_sample.vrm 延迟 6.5s 再放行）
```

> 探针里 `import * as THREE from "../../frontend/node_modules/three/build/three.module.js"` 是有意的：
> 若写成 `from "three"`，esbuild 会从 `spikes/avatar/node_modules` 再打一份 three（bundle 3.2 MB、两个模块实例、
> 原型 patch 全失效）。改成显式路径后 bundle 降到 2.2 MB，且与生产模块共用同一实例。

### 14.5 两条超出原卡范围的改动（需要 Lead 知晓）

1. **新增 `VrmAvatarHandle.getStats()`**（`VrmAvatar.ts:174-201,477-486`）：读 `renderer.info.memory` 的唯一办法。
   three 的 `WebGLRenderer` 把方法挂在**实例**上（构造器里 `this.render = …`），`WebGLInfo` 又不是导出的构造器，
   外部**没有任何**途径读到显存计数；探针实测 patch 原型无效（`seen.renderer` 恒 null）。
   这是**加**接口，不影响既有调用方（`setSpeaking`/`nod`/`dispose` 签名未变）。
2. **`classify()` 补三条 message 判据**（`loadAvatar.ts:199-213`）：three r180 的 `FileLoader` 改走 `fetch`，
   404 时抛的是普通 `Error`（`fetch for "…" responded with 404: Not Found`），不再是老的 `HttpError` 类；
   只按 `name` 判会把 404 归类成 `"unknown"`，`client_diag` 就丢了原因。现在 `responded with \d{3}` /
   `\b[45]\d\d\b` → `http`，`failed to fetch|networkerror|err_network|load failed` → `network`。

### 14.6 未验项

1. **真实设备**：全部实测在本机 Intel UHD 630 + ANGLE/D3D11 的 headless Chromium 上；未在 pad 真机/低端集显复测。
2. **真实上下文丢失**：只用手动 `WEBGL_lose_context.loseContext()` 模拟，未测驱动崩溃/GPU 进程重启。
3. **弱网**：懒 chunk（751 kB）在慢网下的首帧观感未做节流模拟。
4. **首帧预算**：`firstFrameMs` 实测 2547–2719ms（冷着色器缓存），未缓存机器上可能更久，未做多次冷启动统计。
5. `?mode=missing`（动画缺失降级）在 task-15 已验，本卡未复跑。

