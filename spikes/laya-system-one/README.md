# Laya System One · CPU spike（DAG 节点 N30）

> 状态：已完成（2026-11-10）　｜　只回答三个数：中文七类准确率 / CPU 单句延迟 / 进程 RSS
> 作用域：**只落本目录**。未改 `app/`、未改 Dockerfile / docker-compose、未跑 pytest、未 commit。
> 一句话结论：**模型能加载、五字段能拼出来、七类约 70%，但 CPU 延迟过不了 200ms 预算 —— 建议保持降级，不要为了"跑通 L1"调大 timeout。**

---

## 0. 结论速览（三个数，均为本机实测）

| 指标 | 实测 | 口径 / 对照 |
| --- | --- | --- |
| **中文七类准确率** | **14/20 = 70.0%** | 20 条自造中文长者句，七类 top-1；样本量小，**量级参考，不是验收结论** |
| **CPU 单句延迟（四问一次调用）** | **p50 = 1098 ms，p95 = 1569 ms**（min 813 / max 1789） | 一句长者话 → intent+sensitive+privacy+is_out_of_domain 一次 `system_one` |
| **CPU 单句延迟（只问 intent）** | **p50 = 366 ms，p95 = 822 ms**（min 207 / max 954） | 对齐官方「一次前向 193–464ms」的口径 |
| **进程 RSS** | **加载后 989 MB，峰值 1032 MB**（导入 torch 后 226 MB） | psutil `memory_info().rss`，含 torch arena，是上界 |
| 模型加载耗时 | 86.8 s | 冷加载（构建随机权重再灌 state_dict）；官方 README 声称约 2–22 s，本机因内存饥饿慢一个量级 |

**判读（这是本 spike 最该被看到的一句）**：按任务卡的门槛「P95 > 800ms 就停下如实回报」——
**只问 intent 的 p95 = 822 ms 已经越线**，四问一次调用 p95 = 1569 ms 更是远超。
`laya_timeout_ms` 默认 200 ms 在当前机器上**必然超时**，L1 每轮都会降级 L2。
**不要**通过调大 timeout / 缩小 `max_len` 把数字做进 200ms；`max_len` 全程用模型自带配置
（`multilingual/rl_agent_config.json` 的 `max_len=1024`、`head_max_len=256`），一次没改。

---

## 1. 权重来源、许可与路径

| 项 | 值 |
| --- | --- |
| 上游仓库 | [`convaiinnovations/laya`](https://huggingface.co/convaiinnovations/laya)（本快照含三个 checkpoint） |
| 本 spike 用的子面 | `convaiinnovations/laya-multilingual` —— 即权重包里的 `multilingual/` 子目录 |
| 骨干 | `jhu-clsp/mmBERT-base`（322M 参数，1024 context，100+ 语言） |
| 为什么不能用根目录 / `typed-decisions/` | 那两个是 ModernBERT-large（421M）英文面，**中文上接近随机**，不是本次评测对象 |
| 本地路径 | `<权重根目录>\multilingual`（整个快照 2.21 GB；本子目录 646 MB） |
| 许可 | **Apache-2.0**（权重包 `README.md` frontmatter `license: apache-2.0`，tags 含 `commercial-use`） |
| 推理代码 | 权重包自带的 `<权重根目录>\rl_agent_api.py` + `rl_common.py`（扁平导入，需把该目录放上 `sys.path`） |

### SHA256（已计算）

```
multilingual/model.safetensors          9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204
multilingual/rl_agent_config.json       25061739243b617ad88d1219ba6f8a9c86c5881ca28df024fa2d9b3b2fcc30c6
```

其余文件（`tokenizer/tokenizer.json`、`encoder/config.json`）**未逐个计算**：本次只校验了
「真正装权重」的 `model.safetensors` 与「决定温度/长度」的 `rl_agent_config.json`；
再算一遍 32.8 MB 分词器不影响任何结论，故省去。要复算：

```powershell
Get-FileHash '<权重根目录>\multilingual\model.safetensors' -Algorithm SHA256
```

---

## 2. 复现命令（独立 venv，绝不碰项目 `.venv`）

```powershell
# 0) 先把缓存与临时目录指到 E: 盘，避免 200MB 轮子写满 C 盘
$env:UV_CACHE_DIR    = '<仓库根>\.tmp\uv-cache'
$env:TMP             = '<仓库根>\.tmp\tmp'
$env:TEMP            = '<仓库根>\.tmp\tmp'
$env:PIP_CACHE_DIR   = '<仓库根>\.tmp\pip-cache'

# 1) 独立 venv（Python 3.12，与项目 .python-version 对齐，但完全隔离）
uv venv <仓库根>\.tmp\laya-venv --python 3.12

# 2) 只装进这个 venv：torch 走官方 CPU 索引（不带 CUDA，轮子 120.8MB）
uv pip install --python <仓库根>\.tmp\laya-venv\Scripts\python.exe `
    torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python <仓库根>\.tmp\laya-venv\Scripts\python.exe `
    transformers safetensors numpy psutil

# 3) 跑 probe（--json 可选，落机器可读结果）
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; $env:TOKENIZERS_PARALLELISM='false'
<仓库根>\.tmp\laya-venv\Scripts\python.exe `
    <仓库根>\spikes\laya-system-one\probe.py `
    --json <仓库根>\spikes\laya-system-one\results.json
```

实测装出来的版本：`torch 2.15.0+cpu`、`transformers 5.22.2`、`tokenizers 0.23.3`、
`safetensors 0.8.0`、`numpy 2.5.4`、`psutil 7.2.2`；venv 总占用 **0.62 GB**（在 E: 盘 `.tmp/laya-venv`）。

> 为什么必须 `transformers>=5`：权重包 `encoder/config.json` 写的是
> `"transformers_version": "5.0.0"`，RoPE 参数在新版里是 `rope_parameters: {rope_theta: 160000}`；
> 4.x 不认这个键、会退回默认 `rope_theta=10000`，**不报错但算错**。本次装到 5.22.2。

---

## 3. 实测输出（原样粘贴，未改一个字）

```
==============================================================================
Laya System One · CPU spike（N30）
==============================================================================
model_dir   <权重根目录>\multilingual
model_root  <权重根目录>
torch       2.15.0+cpu
transformers 5.22.2
threads     4  device=cpu
加载完成    86835 ms

------------------------------------------------------------------------------
id   期望    预测        conf  sens  priv   ood      ms  句子
------------------------------------------------------------------------------
c1   闲聊    other     0.43  0.02  0.56  0.70  1288.2  我孙子昨天来看我了，还带了点水果
c2   闲聊    weather   0.66  0.01  0.67  0.66  1146.6  一个人在家有点闷，你陪我聊会儿天吧
c3   闲聊    chitchat  0.51  0.02  0.62  0.94  1105.3  你说人老了是不是就爱回想以前的事
m1   用药查询  medicine  0.76  0.02  0.37  0.79  1487.2  我那个降压药是饭前吃还是饭后吃
m2   用药查询  medicine  0.69  0.01  0.35  0.84  1005.4  今天的药吃了没有，我记不清了
m3   用药查询  other     0.33  0.04  0.59  0.73   864.3  阿司匹林一天吃几片
o1   戏曲点播  opera     0.98  0.02  0.25  0.56  1330.5  给我放一段越剧梁山伯与祝英台
o2   戏曲点播  opera     0.60  0.18  0.53  0.97  1184.8  我想听京剧，来一段贵妃醉酒
o3   戏曲点播  opera     0.91  0.04  0.47  0.66   983.7  放个戏曲听听，要黄梅戏
f1   电影点播  movie     0.79  0.08  0.57  0.76   812.8  有没有老电影，地道战那种
f2   电影点播  movie     0.99  0.02  0.20  0.74   964.5  我想看电影，放个喜剧片
f3   电影点播  movie     1.00  0.03  0.49  0.80  1090.3  上次那个电影没看完，接着放
w1   天气查询  weather   0.97  0.01  0.75  0.89   881.4  今天外面冷不冷，要不要加件衣服
w2   天气查询  weather   0.99  0.01  0.14  0.85  1056.8  明天会不会下雨
h1   健康采集  health    0.69  0.01  0.82  0.94  1340.0  我今天量了血压，高压一百五
h2   健康采集  health    0.33  0.05  0.78  0.91   867.0  我这两天头有点晕，浑身没力气
h3   健康采集  health    0.82  0.01  0.70  0.73  1127.3  帮我把今天的血糖记一下
t1   其他    chitchat  0.24  0.01  0.65  0.69  1789.1  帮我查查股票涨了多少
t2   其他    chitchat  0.70  0.04  0.63  0.95  1556.9  你能帮我写一段代码吗
t3   其他    medicine  0.67  0.06  0.83  0.57   972.2  我那个房产证的事该怎么打官司

==============================================================================
结果
==============================================================================
七类准确率      14/20 = 70.0%（20 条自造样本，量级参考，非验收）
  分类召回      闲聊=1/3  用药查询=2/3  戏曲点播=3/3  电影点播=3/3  天气查询=2/2  健康采集=3/3  其他=0/3
置信度         中位=0.699  min=0.240 max=1.000  >=0.85 的 6/20 条
单句延迟(四问) p50=1098ms  p95=1569ms  min=813ms max=1789ms
单句延迟(仅 intent) p50=366ms  p95=822ms  min=207ms max=954ms
模型加载        86835 ms
五字段齐全      20/20（探针拼装口径，见模块 docstring）
进程 RSS        导入后=226MB  加载后=989MB  预热后=1032MB  跑完=612MB  峰值=1032MB

未命中样例
  c1  期望=闲聊  预测=other  conf=0.43  我孙子昨天来看我了，还带了点水果
  c2  期望=闲聊  预测=weather  conf=0.66  一个人在家有点闷，你陪我聊会儿天吧
  m3  期望=用药查询  预测=other  conf=0.33  阿司匹林一天吃几片
  t1  期望=其他  预测=chitchat  conf=0.24  帮我查查股票涨了多少
  t2  期望=其他  预测=chitchat  conf=0.70  你能帮我写一段代码吗
  t3  期望=其他  预测=medicine  conf=0.67  我那个房产证的事该怎么打官司
```

机器可读版：`results.json`（同一份数字，含逐条五字段概率）。

---

## 4. 三个数怎么读（能得出的与不能得出的）

### 4.1 准确率 70%：别当成"七类可用"

- **「其他」= 0/3**：三条域外句（股票 / 写代码 / 打官司）分别被错判成
  `chitchat / chitchat / medicine`。也就是说**七类 choice 问法分不出"域外"**。
- 但同一批样本的 `is_out_of_domain` noul 分支给的概率是 **0.69 / 0.95 / 0.57**（都 ≥0.5），
  说明"域外"这个判断**换个问法（noul）就基本有效**。→ 若后续接服务壳，
  **不要把域外硬塞进七类 choice**，用独立的 noul 问更靠谱。
- `闲聊` 只有 1/3：c1 被判 `other`（0.43）、c2 被判 `weather`（0.66）。c2 里出现"在家""闷"，
  与天气类训练分布可能有牵连；也可能是 20 条样本太偏。
- **置信度中位 0.699，≥0.85 只有 6/20**：即使预测对了，也大多进不了 `confidence ≥ 0.85`
  的直接路由带 —— 按 `questions.decide_l1` 矩阵，多半会落到「澄清一次」甚至 L2。
  这不是 bug，是**七类 choice 的归一化熵本来就高**（7 类下 conf 上限被熵摊薄）。
- **样本量 20 条，四舍五入误差就是 5 个百分点**。70% 只配说"明显高于随机（1/7≈14%）、
  明显不到可用"，**不够当验收数字**。

### 4.2 延迟：CPU 上过不了 200ms，这是结论不是故障

- 只问 intent：p50 **366 ms**、p95 **822 ms**。官方给 CPU 193–464 ms，我们的 p50 落在区间内，
  p95 约为区间上沿的 1.8 倍。
- 四问一次调用：p50 **1098 ms**、p95 **1569 ms**。≈ 3 倍于单问，因为四问在一次前向里
  变成 batch=4 的四条序列。
- **PRD / AGENTS §6 的「L1 单句 ≤200ms」在纯 CPU 上不可能达成**。200 ms 更像是 GPU 或
  蒸馏小模型的口径。要上 L1，只有三条路（都需要产品负责人裁决，本 spike 不做）：
  ①换 GPU；②把七类换成更小的专用分类头；③把 L1 的预算按 CPU 实测重定（例如 p95 ≤ 900ms），
  并同步改 `AGENTS.md` §6 与 PRD 的延迟表。
- **不要**用「把 `laya_timeout_ms` 调到 800/1600」来宣布 L1 跑通：调到 1600ms 只是把
  「每轮超时降级」换成「每轮卡 1.1 秒再降级」，端到端 P95 ≤1.5s 的总预算会被吃掉大半。
  按任务卡口径：**spike 不过就保持降级**。

### 4.3 RSS：1.0 GB 峰值，没 OOM

- 峰值 **1032 MB**（加载后 989 MB + 预热），进程退出后释放。
- 宿主当时 **可用物理内存 0 MB、提交量 25.5 GB / 上限 25.1 GB**（内存被榨干在换页），
  加载因此从官方声称的 2–22 s 恶化到 **86.8 s**。**没有 OOM、没有 kill、没有停任何容器。**
- 这意味着**延迟数字偏保守（上界）**：内存健康的机器上单句应该更快。但即便按官网
  CPU 193–464 ms 的最好值，也仍然过不了 200 ms —— 结论方向不变。

---

## 5. 环境与红线（本次实际发生了什么）

| 项 | 实际情况 |
| --- | --- |
| venv | `.tmp/laya-venv`（Python 3.12.13），**0.62 GB**，与项目 `.venv` 完全隔离；项目 `.venv` / `pyproject.toml` **未被改动一个字节** |
| 依赖 | torch 2.15.0+cpu、transformers 5.22.2、safetensors、numpy、psutil —— 全部只装进临时 venv |
| 缓存落点 | `UV_CACHE_DIR` / `TMP` / `TEMP` / `PIP_CACHE_DIR` 全部指到 `<仓库根>\.tmp\`；C 盘未新增依赖缓存 |
| **funasr 容器** | **全程没有停**（`docker ps` 复核：`anyu-funasr Up 3 hours (healthy)`）。没有为腾内存动过任何容器 |
| OOM | **无**。峰值 RSS 1032 MB，进程正常退出（exit 0） |
| 改动的仓库文件 | 只新增 `spikes/laya-system-one/`（`probe.py`、`README.md`、`results.json`）与 `.tmp/`（未跟踪）。未 `git add` / 未 commit |
| 未碰 | `app/`、Dockerfile、`docker-compose.yml`、`docs/`、`tests/`；未跑 pytest |

---

## 6. 已知不一致（必须让产品负责人看到，不许悄悄圆过去）

1. **任务卡说「七类 enum 去 `app/system_one/questions.py` 抄」，但那个文件里没有七类枚举。**
   `questions.py` 只有 `alert` 一个覆盖值 + 五信号透传；七类只存在于 `AGENTS.md` §6 的**中文描述**
   （闲聊/用药查询/戏曲点播/电影点播/天气查询/健康采集/其他）与 `docs/HARNESS_DESIGN.md` §97 的同句。
   本 spike 的处理：按 `app/core/injection.py::INTENT_WHITELIST` 的**既有生产词**取键
   （`chitchat / medicine / opera / movie / weather / health`）+ 第七类 `other`，
   **没有自创新词，但确实不是从 questions.py 抄的**。建议后续在 `questions.py` 补一个
   `INTENT_CLASSES` 常量当唯一出处，否则服务壳、评测、路由会各写一份。
2. **「五字段齐全率 20/20」是探针自己拼的，不是模型的输出。**
   真实模型一次返回的是 `{answers: {qid: {...}}}`；五字段是 probe 把
   1 个 choice + 3 个 noul 拼装的结果。**它证明的只是"能稳定拼出来"，不能证明
   `app/system_one/client.py` 的契约已被满足** —— 契约级验证属于服务壳（另一张卡）。
3. **`sensitive` 在中文健康主诉上不可靠**：h2「我这两天头有点晕，浑身没力气」的
   sensitive 只有 **0.05**，h1/h3 也都 ≤0.01。这与项目既有设计一致（安全一票否决在
   L0 规则层，不靠 L1），但**不能**把 `sensitive` 当成兜底安全网来用。
4. **官方文档称 multilingual 快 ~2.2x、单次前向约 33ms** —— 那是 GPU 口径或短文本口径，
   与本机 CPU 实测（p50 366ms）不是一回事，引用时需要注明。

---

## 7. 给下一手的建议（不在本卡执行）

1. **保持降级**：`laya_enabled=false` 或 `LAYA_FALLBACK_TO_LLM=true` 不动，
   直到有人裁决 §4.2 的三条路。**不要把 `laya_timeout_ms` 调大当成果**。
2. **别自己写服务壳**：权重包 README 提到官方已提供 `pip install "laya[serve]"` → `laya-serve`
   （Jev-compatible HTTP server）。若后续要做 L1 容器，先看这个能不能直接满足
   `POST /v1/classify` 五字段契约，比自己拿 FastAPI 包一层更省事、也更少一个自研面。
   同时 `pip install laya` 还能用 `laya.load()` 把加载从 87s 降到 ~2s 量级。
3. **七类问法要改**：域外不要塞进 choice（见 §4.1），`intent` 用 choice、`is_out_of_domain`
   用 noul；`sensitive` 只作辅助、安全仍靠 L0。
4. **要可信的验收数字**：扩到 ≥110 条（直接用 `eval/fixtures/routing.jsonl` 的中文样本 + 人工标注
   七类），并在内存健康的机器上复跑。20 条只够定方向。
