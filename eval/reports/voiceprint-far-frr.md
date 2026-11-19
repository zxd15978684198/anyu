# 迭代 9 声纹 FAR/FRR 缺口报告（未评测；附 迭代 4 阈值可分性实测线索）

> 状态：已完成（缺口说明；**FAR/FRR 评测未执行**）｜ 版本：v1.0 ｜ 日期：2026-11-08
> 关联：`设计记录（交付与观测）`（必备文件含本报告）、`:181`（交付七件）；`设计记录（权限与安全）` §6.1；`eval/reports/security.md` §9
> 上游依据：PRD §3.2（line 187 阈值 cosine ≥0.85）、line 205（报告必填七项）、line 286（FR-VPR-04，**P1**）、line 51（生产级声纹列为非目标）、line 195（降级：长者 ID + 主动确认） ｜ 技术设计文档 line 268（`docs/IDENTITY_RULES.md` 定口径）、line 1094（要求误识率/拒识率表）
> 变更记录见文末

## 0. 结论（先说人话）

1. **FAR/FRR 未评测。本报告不提供任何 FAR/FRR 数值、不提供任何曲线。** 三条硬缺口：无标注数据集、无评测脚本、无口径文档（`docs/IDENTITY_RULES.md` 不存在）。
2. 因此 **FR-VPR-04（P1）未闭合**，PRD line 205 要求的七项必填数据一项都没有。
3. 但「没测所以什么都不知道」是**不准确**的：迭代 4 的阈值可分性 spike 用 4 个真实语音样本实测过，结论是 **CAM++ 在同人样本上的 cosine 只有 0.5679，远低于 0.85**，0.85 工作点在 CAM++ 上不可行（详见 §3）。这是一条**阈值风险**，不是 FAR/FRR 结论——两者不能互相替代。
4. 该 spike 的待裁决项（阈值调回 0.55 / 换模型 / 找 0.85 依据）至今**未见闭环记录**：`app/core/config.py:196` 仍是 `0.85`，PRD line 187 仍是 0.85。本轮 W7b 不改配置、不改基线文档，只如实登记。

## 1. 能力现状（代码已实现，评测未做）

| 环节 | 落点 | 规模 |
| --- | --- | --- |
| 注册样本（本人现场录 3 段，PRD line 64/163） | `app/services/voiceprint_service.py:189 register_sample()`，`SampleIndex = Literal[1, 2, 3]`（`:35`） | 374 行 |
| 比对（cosine，阈值取配置） | `voiceprint_service.py:327 cosine_compare_against_elder()`，`threshold = settings.voiceprint_similarity_threshold`（`:337`） | 同上 |
| 阈值配置 | `app/core/config.py:196 voiceprint_similarity_threshold: float = 0.85`；`config.py:200 voiceprint_base_url="http://localhost:7003"`；`config.py:202 voiceprint_embedding_dimension=192` | — |
| 注册 / 删除接口 | `app/api/voiceprints.py` | 360 行 |
| 表模型 | `app/models/voiceprint.py`（embedding 加密存储） | 67 行 |
| 会话首轮身份校验 + 降级 | `app/services/identity_service.py:5`（每会话首轮 → cosine 比对 → 语音确认 / 主动确认） | — |
| 退房时清理声纹 | `app/services/lifecycle_service.py:8`（硬删），用例 `tests/test_lifecycle_service.py:286` | 257 行 |

**能力是「能用」的，缺的是「准不准」的证据**：阈值走配置、比对走 CAM++ 192 维、不可用有降级链，但 0.85 这个数从未被标注数据校准过。

## 2. 为什么 FAR/FRR 没测（三条硬缺口，逐条给证据）

### 2.1 无标注数据集

盘点命令与结果（`Get-ChildItem eval -Recurse -File`）：

| 位置 | 内容 | 能否用于 FAR/FRR |
| --- | --- | --- |
| `eval/fixtures/` | `routing.jsonl`(110 行)、`injection.jsonl`(20 行)、`routing_badcase.jsonl`(1 行) —— 全是**文本**夹具 | ❌ 无音频 |
| `eval/harness/` | `degrade_demo.py`、`latency_baseline.py`、`routing_eval.py` | ❌ 无声纹脚本 |
| `eval/reports/` | day05/06/07/08/09 文本报告 | ❌ 无样本 |
| `spikes/voiceprint/` | `README.md`、`spike_extract.py`、`spike_resemblyzer.py`、`verify_container.py` | ⚠️ 只够做可分性冒烟（§3），不够算 FAR/FRR |

全仓音频文件（排除 `.venv` / `node_modules` / `.git`，`Get-ChildItem -Recurse -File -Include *.wav,*.mp3,...`）：

| 文件 | 字节 | 性质 | 能否用作说话人样本 |
| --- | --- | --- | --- |
| `spikes/reference/asr/examples/speaker1_a_cn_16k.wav` | 118932 | 参考项目公开样例（说话人 1，第 a 段） | ⚠️ 可当 1 条同人样本 |
| `spikes/reference/asr/examples/speaker1_b_cn_16k.wav` | 157058 | 同说话人第 b 段 | ⚠️ 同上，只构成 **1 对**同人正样本 |
| `spikes/reference/asr/examples/speaker2_a_cn_16k.wav` | 170028 | 说话人 2 | ⚠️ 异人负样本，数量个位数 |
| `spikes/reference/asr/examples/asr_example_zh.wav` | 177572 | ASR 示例，**无说话人标注** | ❌ 标签未知 |
| `spikes/edge_tts/out/out.wav`、`static/mock/tts_*.wav`（36524B/个） | — | **TTS 合成音**，非目标人群 | ❌ 合成音不能代表长者声纹分布 |
| `pytest-of-<用户名>/.../sine.mp3`（4641B/个） | — | 单音正弦，测试临时产物 | ❌ 非语音 |

**判定**：现有可用样本是「2 个说话人、每人 1–2 段、零划分、零注册/测试隔离」。FAR 的估计分辨率是 `1/N` 量级（N=负样本对数），个位数负样本只能给出 0 或 1 这种粗糙值——**算出来的任何 FAR/FRR 都会被误读成结论**，所以不写。

口径上也对不上：`voiceprints` 表存的是**本人现场录制的 3 段加密 embedding**（PRD line 64/163），公开样例与 TTS 音都不属于「长者本人」这一目标分布。

### 2.2 无评测脚本

`eval/harness/` 只有三个脚本（`degrade_demo.py`、`latency_baseline.py`、`routing_eval.py`），没有 `voiceprint_eval.py`；`spikes/voiceprint/spike_extract.py` 是**单次冒烟**（打印两组 cosine + 阈值扫描），不是批量评测器：没有数据集加载、没有正负样本对枚举、没有阈值曲线与 EER、没有置信区间。要出 FAR/FRR 必须新写脚本（§5.2）。

### 2.3 无口径文档（`docs/IDENTITY_RULES.md` 不存在）

技术设计文档 line 268 明确「FAR/FRR 口径由 `docs/IDENTITY_RULES.md` 定义」。`Get-ChildItem docs -File -Filter *.md` 实际只有 9 个：工作流选型说明、接口契约_身份与权限、迭代排期、技术设计文档、PRD、前端页面方案、demo-script、ER_数据模型设计、SECURITY —— **没有 `IDENTITY_RULES.md`**。

所以「FAR 算到多少算过」没有文字依据：PRD 给了阈值 0.85，但没给 FAR/FRR 的验收上限；技术文档 line 1094 只说「要出误识率/拒识率表」，没说表长什么样。**这个上限不允许我自己定**——它属于 PRD 修订记录级别的事（AGENTS §4.7）。

## 3. 已有的唯一实测线索：迭代 4 阈值可分性 spike（**不是** FAR/FRR）

来源：`spikes/voiceprint/README.md`（迭代 4 A1 · 2026-11-06，73 行）。用的是 §2.1 里那 4 个样例语音。

| 度量 | 实测值 | 对照 | README 行 |
| --- | --- | --- | --- |
| 同人 cosine（`speaker1_a` ↔ `speaker1_b`） | **0.5679** | 要求 ≥ 0.85 | `:21` |
| 异人 cosine max | **-0.0146** | 要求 < 0.85 | `:22` |
| gap（同人 − 异人 max） | **0.5824** | — 可分性本身够大 | `:23` |
| embedding 维度 | **192 维**（固定），模型 27 MB，sherpa-onnx 纯 CPU 可加载 | — | `:12-15` |

阈值扫描（README `:27-32`，同一样本集）：

| 阈值 | 同人 ≥ T | 异人 < T | 结论 |
| --- | --- | --- | --- |
| 0.50 | ✅ | ✅ | 可行 |
| 0.55 | ✅ | ✅ | 可行（**CAM++ 原生口径**，README `:41`） |
| 0.60 | ❌ | ✅ | 不可行 |
| 0.85 | ❌ | ✅ | **完全不可行**（同人 cosine 0.5679 < 0.85） |

**边界声明（必须一起读）**：这组数字是 **1 对同人正样本 + 个位数异人负样本** 的冒烟结果，衡量的是「可分性/阈值可达性」，**不是** FAR/FRR。它不能替代 §5 的评测，也**不能被引用成误识率/拒识率**。它能说明的只有一件事：

> 若照 0.85 上线，CAM++ 把**本人**判成「不是本人」的概率在这个样本上是 100%（1/1），也就是说同人比对会系统性地落到「主动确认」兜底。

**未闭环的裁决项**（README `:50-68`，四条；本轮无任何闭环记录）：

1. 阈值调回 0.55？——需要产品负责人裁决，因为 0.85 写死在 AGENTS §7 与 PRD line 187。
2. 换更大的 CAM++ / ECAPA-TDNN？——需额外 spike，且 sherpa-onnx 生态里没有更大的中文 speaker verification 模型。
3. 找 0.85 的出处？——PRD 写死但没标是哪个模型的经验值。
4. 方案 C：先按 0.55 写服务、阈值放 Settings 运行时可调（现状即如此，`config.py:196` 仍是 0.85）。

现状核对：`app/core/config.py:196` = **0.85**（未改），PRD line 187 = 0.85（未改）。**代码与 spike 结论之间存在一个没写进任何修订记录的矛盾**——这正是本报告要交给评审裁决的点。

## 4. 影响

| 影响面 | 具体后果 | 严重度 |
| --- | --- | --- |
| 验收 | FR-VPR-04（P1）未闭合；PRD line 205 七项必填数据全缺 | 中（P1，且 PRD line 51 已把生产级声纹列为非目标，不阻塞演示） |
| 功能正确性 | 阈值未校准 + spike 实测同人 cosine 0.5679 → 大概率每次会话都走「主动确认」，声纹等于没生效 | 高（体验层） |
| 文档一致性 | 0.85 同时出现在 PRD line 187、AGENTS §7、`config.py:196`；spike 结论 0.55 只存在于 `spikes/voiceprint/README.md` 与代码注释类说明里 | 高（评审会被问到） |
| 数据隐私 | 无评测数据 ⇒ 无真实长者音频入仓；这反而是当前唯一「合规」的部分 | — |

**不能做的事**：不能因为 PRD line 51 写了「生产级声纹为非目标」就把 FR-VPR-04 当免测。line 286 把 FR-VPR-04 标成 **P1** 且要求 迭代 8.4 产出报告，line 205 给了七项必填——「演示级也要有报告」，只是报告里该写「演示级」「样本量小」。

## 5. 要做什么才能测（补齐方案，**尚未执行**）

### 5.1 数据集规格：`eval/fixtures/voiceprint/`

- **说话人**：≥ 10 人，覆盖不同性别/年龄/口音（至少含 3 位模拟长者：语速慢、音量低）。
- **每人样本**：注册 3 段（对齐 `SampleIndex = Literal[1, 2, 3]`）+ 测试 5 段，**注册与测试不重叠**；16 kHz 单声道 wav；段长 ≥ 3 s（最短注册时长最终由 `IDENTITY_RULES.md` 定，PRD line 205 要求把它写进报告）。
- **样本对规模（按上述规格的算术推演，是方案规模、不是实测结果）**：
  - 正样本对（同人）= 10 人 × 3 段注册 × 5 段测试 = **150 对**；
  - 负样本对（异人）= 30 段注册 × 50 段测试 − 150 = **1350 对**。
  - 这个量级下 FAR 的最小可分辨步长是 1/1350 ≈ 0.0007，报 95% Wilson 区间才有意义。
- **manifest.jsonl**：`{speaker_id, sample_index, split: "enroll"|"test", path, duration_s, device, source_license}`。
- **合规**：不得用真实长者音频（AGENTS §4.3/§4.10）；用志愿者录音或已授权公开语料（如 AISHELL 系列，需先确认许可），入仓前脱敏、可删除。

### 5.2 评测脚本：`eval/harness/voiceprint_eval.py`（需新写）

1. 读 manifest → 逐段抽 embedding（走 `app/services/voiceprint_service.py` 的同一路径，或直连 7003 的 CAM++ 容器；**两者必须同源**，否则测的不是线上模型）。
2. 枚举全部正/负样本对，算 cosine，落 CSV（`pair_type, speaker_a, speaker_b, cosine`）。
3. 阈值扫描 0.50 → 0.99 步长 0.01：
   - `FAR = 负样本对判同数 / 负样本对总数`
   - `FRR = 正样本对判异数 / 正样本对总数`
   - 取 `FAR = FRR` 的插值交点作 **EER**；同时报 **0.85 工作点**与 **0.55 参考工作点**。
4. 每个工作点报 95% Wilson 置信区间（样本量小，不能只报点估计）。
5. 记录**模型版本**：容器镜像 digest + `voiceprint_embedding_dimension=192`（`config.py:202`）；同一输入跑两遍确认 cosine 完全一致（防非确定性）。
6. 退出码：`0`=评测成功产出报告、`1`=数据集/模型缺失、`2`=参数错（对齐 `tests/eval_gate.py:59-60` 与 `scripts/scan_logs.py:9` 的惯例）。

### 5.3 报告必填字段（对齐 PRD line 205）

| 字段 | 取值方式 | 当前 |
| --- | --- | --- |
| 注册最短语音时长 | manifest 里 `split="enroll"` 的 `duration_s` 最小值 | 缺 |
| 声纹模型版本 | 容器镜像 digest + 模型文件名 | 缺（spike 只记了 27 MB / 192 维） |
| 注册样本数 | 注册段总数（方案 30） | 缺 |
| 测试说话人数 | `speaker_id` 去重数（方案 10） | 缺 |
| FAR | §5.2 输出 | 缺 |
| FRR | §5.2 输出 | 缺 |
| 主动确认兜底记录 | `identity_source='confirm'` 的会话计数（来自身份/decision 日志） | 缺 |

### 5.4 验收口径（须先裁决，不得自定）

1. 结论**只能标「演示级」**：PRD line 51 把生产级 FAR/FRR 承诺列为非目标。
2. FAR/FRR 的通过上限 PRD 没给数——必须由产品负责人裁决并写进 `docs/IDENTITY_RULES.md`（技术文档 line 268）+ PRD 修订记录（AGENTS §4.7）。**没有上限就没有 pass/fail**，脚本只能产出数据。
3. 若采纳 0.55（spike 结论 1）：`config.py:196`、AGENTS §7、PRD line 187 **三处必须同一次提交改**并补修订记录；若维持 0.85，则必须写清「声纹在生产上等同兜底」这一事实，不许在交付说明里暗示可用。
4. 失败也不阻塞 迭代 10 演示（降级链 PRD line 195 存在），但 README / 交付说明必须保留本报告链接。

### 5.5 依赖与阻断项

| 依赖 | 状态 | 阻断的是 |
| --- | --- | --- |
| `docs/IDENTITY_RULES.md` | **不存在**，需新建 | FAR/FRR 通过上限、注册最短时长、主动确认触发条件 |
| 标注数据集 | 不存在（§2.1） | 一切 |
| 评测脚本 | 不存在（§2.2） | 一切 |
| 7003 声纹容器 | 代码指向 `http://localhost:7003`；spike 验证过纯 CPU 可部署 | 端到端复跑 |
| 阈值裁决 | 未闭环（§3） | 报告结论能否写「阈值合理」 |

## 6. 未验证项

| # | 未验证项 | 原因 |
| --- | --- | --- |
| 1 | FAR/FRR（任何数值、任何曲线） | 本报告主题：缺数据集/脚本/口径 |
| 2 | 注册 → 比对 → 主动确认 端到端（真容器 + 真录音） | 需 7003 容器与现场录音，本轮未起 |
| 3 | `spikes/voiceprint/` 的可分性结论**复跑** | 只读 README，未重跑 spike 脚本（重跑要拉模型/依赖，不在 W7b 范围） |
| 4 | 多说话人、多设备、噪声条件下的稳定性 | 无数据 |
| 5 | 注册 3 段是否够（PRD line 163 的 3 段是产品口径，非实验结论） | 无数据，属待测项 |

## 7. 复跑与证据链

```powershell
# 1) 数据集与脚本都不存在（应输出 3 个 fixtures + 3 个 harness 脚本 + 报告）
Get-ChildItem eval -Recurse -File | Select-Object FullName, Length

# 2) 现有唯一声纹实测线索（4 个样例、1 对同人、个位数异人）
Get-Content spikes/voiceprint/README.md

# 3) 现有音频盘点（全是样例/TTS/单音，无标注集）
Get-ChildItem -Recurse -File -Include *.wav,*.mp3,*.flac,*.m4a,*.ogg |
  Where-Object { $_.FullName -notmatch '\\(node_modules|\.venv|\.git)\\' } |
  Select-Object FullName, Length

# 4) 阈值现状（代码 0.85 vs spike 0.55）
Select-String -Path app/core/config.py -Pattern 'voiceprint'
Select-String -Path docs/安语3D数字人_PRD.md -Pattern '0\.85|FAR|FRR|FR-VPR-04'

# 5) 口径文档是否存在（应无输出）
Get-ChildItem docs -File -Filter IDENTITY_RULES.md
```


## 8. 裁决闭合（2026-11-09 追加，**不改上文任何已测数字**）

产品负责人 2026-11-09 裁决：**声纹判定阈值由 0.85 下调为 0.55**（依据本文 §3 的 spike 实测与 CAM++ 原生口径）。本节只登记「裁决后事实」，上文第 1–7 节的测量值、推断与「未评测」结论**保持原样**，作为历史基线可追溯。

| 项 | 裁决前 | 裁决后 | 落点 |
| --- | --- | --- | --- |
| 判定阈值 | 0.85 | **0.55** | `app/core/config.py:201`（`voiceprint_similarity_threshold`）、`.env.example:143` |
| 需求侧口径 | PRD §3.2 line 187 / FR-VPR-01 / AC-28 / §6.1 迭代 4 行写 0.85 | 同上四处改 0.55 | PRD **v2.8** 修订记录 |
| 实现侧口径 | TD line 54/416/712/1028 写 0.85 | 同上四处改 0.55 | TD **v1.8** 修订记录 |
| 协作规则 | AGENTS §6/§7 写 0.85 | 改 0.55（§7 注明 2026-11-09 裁决与 PRD §3.2 v2.8） | `AGENTS.md` |
| 相邻文档 | `IDENTITY_RULES.md:203`、`SECURITY.md:244`、`接口契约_身份与权限.md:44` | 改 0.55，契约升 **v1.10** | 各自变更记录 |
| 测试桩 | `tests/test_identity_cascade.py` 桩 `threshold=0.85` | 改 0.55（与真实默认值一致） | 同文件 220/223/230/233 行区域 |

**仍未闭合（不因阈值裁决而消失）**：FAR/FRR 依旧**没有**标注数据集与 harness（本文 §1/§2 的证据仍然成立），因此「0.55 工作点到底 FAR/FRR 是多少」仍无实测答案；降阈值只把「同人恒被判非本人」这个确定性缺陷消除，**不等于**已获得误识率证据。PRD line 286（FR-VPR-04，P1）与 line 205 的报告七项要求仍待补数据后重做。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版（W7b）：如实登记 FAR/FRR **未评测**（无标注数据集 / 无脚本 / 无 `IDENTITY_RULES.md`），列出 PRD line 187/195/205/286/51 与技术上依据；附 迭代 4 spike 的 4 样本实测（同人 cosine 0.5679 < 0.85、异人 max -0.0146、阈值扫描 0.60 起不可行、CAM++ 原生 0.55）并标注其**不是** FAR/FRR；给出数据集规格、脚本口径、报告必填七项与需先裁决的验收上限；未编造任何 FAR/FRR 数字或曲线 |
| v1.1 | 2026-11-09 | 追加 §8「裁决闭合」：产品负责人裁决阈值 0.85 → 0.55 已落地到 `config.py:201` / PRD v2.8 / TD v1.8 / AGENTS §6§7 / IDENTITY_RULES / SECURITY / 接口契约 v1.10 / 测试桩。**第 1–7 节测量值与「未评测」结论一字未改**；同时明确「阈值下调≠FAR/FRR 已评测」，避免把配置变更误读成证据补齐。 |
