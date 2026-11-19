# 安语（Anyu）回滚演练手册

> 版本：v1.0　｜　日期：2026-11-08　｜　维护：迭代 10 · W6
> 上游依据：`设计记录（交付与观测）` §4-W6、§6.1-5、§6.4-3（**验收口径**）｜`docs/安语3D数字人_技术设计文档.md` §8 迭代 10「改一行 → 故意坏 → 回滚到上一 tag」与风险条「**迁移回滚（`alembic downgrade`）与代码回滚是两件事，要分别演练**」（**实现口径**）｜`README.md` §5.2 / §6.2（起服务命令与端口）。
> 实现对照：`scripts/rollback.sh`，本文每一步都能在脚本里找到同名函数。
> 读数约定：所有「实际」列只填 **dry-run** 结果；真跑 `docker compose` 的那几次留给 Lead 在集成验收时回填（本日 `up/down` 归 Lead 独占）。

---

## 1. 两种回滚不是一件事

| | 代码回滚 | 迁移回滚 |
| --- | --- | --- |
| 对象 | 工作树 + 分支指针（`git reset --hard`） | 数据库 schema 版本（`alembic downgrade`） |
| 载体 | git 对象库 | 具名卷 `postgres_data` 里的 `alembic_version` 表 |
| 能否一条命令撤回 | 能，指针退回去就行 | 只有该迁移写了可逆的 `downgrade()` 才行；**`drop_column` 类迁移退不回数据** |
| 失败后果 | 丢未提交改动（可重来） | 丢列/丢表/丢行（**不可逆**） |

技术设计文档把「迁移回滚与代码回滚要分别演练」写成风险条，根因就在这里：**代码回滚是幂等的，迁移回滚不是**。两件事捆在一起做，迁移那步一失败，你已经回不到「代码新、库也新」的状态，只剩硬修库一条路。

### 1.1 本手册采用的次序（脚本第 3–6 步）

**次序**：① 停服务 `docker down` → ② 回代码 `git reset` → ③ 回迁移 `alembic` → ④ 起服务 `compose up` → ⑤ 探活 `curl /health`。四条理由，缺一条次序就不成立：

1. **先停，避免「旧进程写新 schema」**。`0009_memory_medication` 之后才有记忆/用药的列；旧进程对着新 schema 继续 `INSERT`，写进半张表比报错更难收拾。
2. **代码先于迁移，因为迁移文件跟着代码走**。`alembic heads` 读的是**当前工作树**的 `alembic/versions/`；`git reset --hard` 之后读到的才是目标版本，反过来只能凭记忆猜 revision。
3. **迁移退到「目标代码的 head」，不是退一格**。多跳回滚时 `alembic downgrade -1` 只退一格，退完仍与代码不匹配——脚本因此先比对 `alembic current` 与 `alembic heads` 再决定（§2.2 的 A/B/C/D）。
4. **最后探活，因为「起来了」不等于「回滚成功」**。`curl -fsS /health` 是唯一客观证据；不健康就打印 `docker compose logs --tail=50 backend` 并 exit 1，**不许口头宣布成功**。

> 反向次序（先迁移后代码）只在「停服窗口内库还要供别的消费者读」时才考虑；本项目只有 backend 一个写库方（`app/services/` 是唯一写库层，AGENTS §2），没有第二个消费者，故不采用。

### 1.2 次序陷阱：迁移要连库，而库刚被 `down` 停掉

`docker compose down` 会把 `postgres` 一起停，紧接着 `alembic downgrade` 必然连不上。脚本第 5 步因此先单独拉起数据层：`docker compose up -d ${ROLLBACK_DB_SERVICES:-postgres redis}`。

**为什么不改成「在容器里跑 alembic」**：`docker compose run backend alembic` 用的是**镜像里烘焙的** `alembic/versions`（构建那一刻的代码），不是刚 `git reset` 出来的目标代码——等于拿**错版本的迁移文件**判断 head，比连不上库更危险。迁移一律在宿主机上跑。

---

## 2. `scripts/rollback.sh`：参数、八步、三条闸

### 2.1 参数

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--yes` | 关 | 跳过交互确认。**非交互环境必须显式给**，否则 exit 1，不允许无人值守默认执行 |
| `--dry-run` | 关 | 只打印命令，一条不执行；**恒退出 0**（用法错误除外） |
| `--ref <tag\|commit>` | `git describe --tags --abbrev=0 HEAD` | 目标版本 |
| `--no-migrate` | 关 | 只回代码，不动库（代码回滚单练） |
| `--force-dirty` | 关 | 工作树脏也继续（**必须同时给 `--yes`**，否则 exit 2） |
| `--force-ref` | 关 | 目标不在 HEAD 祖先链上也继续（跳转而非回退） |
| `-h/--help` | — | 用法 |

环境变量（**脚本不读 `.env`**，`.env` 由 docker compose 与 Pydantic Settings 各自读）：`BACKEND_HEALTH_URL`（默认 `http://127.0.0.1:${BACKEND_HOST_PORT:-8000}/health`）、`ROLLBACK_HEALTH_RETRIES` / `ROLLBACK_HEALTH_INTERVAL`（默认 30 / 2，合计 60s）、`ROLLBACK_DB_SERVICES`（默认 `postgres redis`）、`ALEMBIC_CMD`（默认自动探测）。

### 2.2 八步、失败动作与 A/B/C/D

| 步 | 命令 | 失败/边界时 |
| --- | --- | --- |
| 1 前置检查 | `git status --porcelain`、`git describe --tags`、`git merge-base --is-ancestor`、`command -v` | 任一 blocker → 真实执行**在动任何东西之前** exit 1；dry-run 只记账继续打计划 |
| 2 打印现场 | `git log --oneline -3` + 目标 ref + 跨度提交数 | — |
| 3 停服务 | `docker compose down` | **绝不允许 `-v`**（§5 禁忌 1） |
| 4 回代码 | `git reset --hard <目标 SHA>` | 目标未解析 → 只打印形状 |
| 5 回迁移 | `alembic current` + `alembic heads` → 走 A/B/C/D | 库 10×3s 起不来 → exit 1；两条链分叉 → exit 1 交人工 |
| 6 起服务 | `docker compose up -d --build` | 构建失败 → `set -e` 中止 |
| 7 探活 | `curl -fsS --max-time 5 $HEALTH_URL`，30 次 × 2s | 失败 → `docker compose logs --tail=50 backend` 并 exit 1 |
| 8 摘要 | from → to、迁移号变化、健康结果、反向命令 | — |

第 5 步判定（只有真实执行才连库）：

| 分支 | 条件 | 动作 |
| --- | --- | --- |
| A | 库内版本 == 目标 head | 跳过 |
| B | 库内比目标新（其后代） | `alembic downgrade <目标 head>` |
| C | 库内比目标旧（其祖先） | `alembic upgrade head` |
| D | 号相同但 id 不同（链已分叉） | **中止**，交人工，不猜 |

### 2.3 三条安全闸

1. **脏工作树闸**：`git status --porcelain` 非空即中止；只有 `--yes --force-dirty` 同时给才放行，放行时打印将被丢弃的前 10 个文件。
2. **ref 与祖先链闸**：`--ref` 显式指定却解析不到 → **用法错误 exit 2**（dry-run 也一样）；默认解析不到 tag → blocker。不在祖先链上 → 中止，需 `--force-ref`。
3. **破坏性参数闸**：执行前扫 `down` 的 argv，出现 `-v` / `--volumes` / `--rmi` 立即 `die`——不靠注释，靠代码拦。

### 2.4 退出码与两个实现细节

`0` 成功（dry-run 也是 0）｜`1` 前置检查或执行失败｜`2` 用法错误。**dry-run 恒 0 的道理**：它的产物是「计划」不是「结果」，必须能在**仓库脏、没有 tag、alembic 没装**的机器上照样打出完整计划（那正是最需要预演的时候）；真正会挡住的原因在结尾 `[SUMMARY]` 逐条列出。

- **自复制重入**：第 4 步会把 `scripts/rollback.sh` 本身也改回旧版本。bash 边读边执行，源文件中途被换会读到半截；故真实执行前先把脚本 `cp` 到 `mktemp` 目录再 `exec bash <tmp>`，退出时 `trap` 清理。dry-run 不动工作树，无需这层。
- **`GIT_OPTIONAL_LOCKS=0`**：`git status`/`git log` 默认会刷新 `.git/index` 的 stat 缓存（写盘）。关掉后 dry-run 对仓库真正零写入，验证方式见 §4.1。

---

## 3. 演练脚本（可直接复制粘贴）

```bash
# 3.1 只预演（零风险，随时可跑）
bash -n scripts/rollback.sh            # 语法自检
bash scripts/rollback.sh --dry-run     # 八步全打印，一条不执行，exit 0

# 3.2 代码回滚单练（只动代码，不动库）
bash scripts/rollback.sh --dry-run --no-migrate
bash scripts/rollback.sh --no-migrate  # 交互确认后真跑（y）

# 3.3 迁移回滚单练（只动库，不动代码）——必须单独来一次
uv run --locked alembic current        # 记下当前 revision
uv run --locked alembic downgrade -1   # 退一格
uv run --locked alembic current        # 期望 0008_decision_logs
uv run --locked alembic upgrade head   # 前进回来
uv run --locked alembic current        # 期望 0009_memory_medication (head)

# 3.4 全栈回滚（当前 tag → 上一个 tag）
git tag v0.9-drill                     # 演练 tag；真仓库只在 W8 冻结点打 v1.0-demo / final
bash scripts/rollback.sh --dry-run     # 默认 --ref = 最近 tag，先确认解析对不对
bash scripts/rollback.sh               # 真跑：down → reset → alembic → up → 探活
```

3.1 预期：八步全打印、`reset`/`alembic` 只出现命令形状、结尾 `[SUMMARY] DRY-RUN：一条命令都没有执行`。
3.4 预期摘要：

```
  from        : <当前短 SHA>（v1.0-demo）<提交标题>
  to          : <目标短 SHA>（v0.9-drill）<提交标题>
  migration   : 0009_memory_medication → 0008_decision_logs（downgrade）
  health      : ok（第 3 次，约 4s 后恢复）
  数据卷      : 保留（down 未加 -v）
  反向操作    : git reset --hard <原 SHA>
```

3.5 临时 clone 里的小回滚演练（W6 实际做过，可重复；**真仓库绝不打测试 tag**）：

```bash
TMP="$(mktemp -d)"; git clone <repo> "$TMP/clone"; cd "$TMP/clone"
mkdir -p scripts && cp <repo>/scripts/rollback.sh scripts/   # 脚本还没入库时手动带上
git add -A && git commit -qm "drill: add rollback.sh"
git tag t-test && git commit -q --allow-empty -m tmp
bash scripts/rollback.sh --ref t-test --yes --no-migrate --dry-run
bash scripts/rollback.sh --dry-run     # 不带 --ref：验证「最近一个 tag」解析
```

---

## 4. 三次演练记录

### 4.1 代码回滚

| 命令 | 预期 | 实际（dry-run / clone） |
| --- | --- | --- |
| `bash -n scripts/rollback.sh` | 无输出、exit 0 | ✅ exit 0，无输出 |
| `bash scripts/rollback.sh --dry-run` | 八步全打印、零副作用、exit 0 | ✅ exit 0；HEAD `52ab926` 前=后；`.git/index` MD5 前=后（`6B3270AF…`）；`git status --porcelain` 前后差集 **0 条** |
| `--ref t-test --yes --no-migrate --dry-run`（clone） | 解析到基线、1 个提交退回 | ✅ `目标 ref（--ref 指定）：t-test → def56de`、`跨度：8a5cc3c → def56de，1 个提交将被退回`、exit 0 |
| 不带 `--ref`（clone，已有 tag） | 默认取 `t-test` | ✅ `目标 ref（最近 tag）：t-test → def56de` |
| 真跑 `--no-migrate` | `/health` 恢复、业务可继续 | ⬜ 待 Lead（本机 `up/down` 归 Lead） |

### 4.2 迁移回滚

| 命令 | 预期 | 实际 |
| --- | --- | --- |
| `alembic heads` | `0009_memory_medication (head)` | ✅ 实测该行（Windows 侧 `.venv\Scripts\alembic.exe`） |
| 脚本第 5 步静态预判 | `当前工作树最新迁移号：0009` | ✅ `[INFO] 当前工作树最新迁移号：0009` |
| 脚本 A/B/C/D 判定 | 库内 0009、目标 0008 → 走 B | ⬜ 待 Lead（dry-run 不连库，只打印规则） |
| `downgrade -1` → `upgrade head` | `0009 → 0008 → 0009` | ⬜ 待 Lead（需真实库，且**必须单独演练**） |

### 4.3 全栈回滚

| 命令 | 预期 | 实际 |
| --- | --- | --- |
| `bash scripts/rollback.sh --dry-run`（无 tag 的真仓库） | 被 tag 拦下并说清怎么办 | ✅ `[BLOCK] 仓库里没有任何 tag…请用 --ref <tag\|commit> 指定，或先打 tag`，exit 0 |
| `bash scripts/rollback.sh --ref no-such-ref --dry-run` | 用法错误 exit 2 | ✅ `[用法错误] 找不到 ref：no-such-ref`，exit 2 |
| `bash scripts/rollback.sh --force-dirty` | 用法错误 exit 2 | ✅ `--force-dirty 会丢弃未提交改动，必须同时给 --yes`，exit 2 |
| 真跑 down → reset → alembic → up → 探活 | 摘要四行齐、`/health` ok | ⬜ 待 Lead；**前置：先打 tag**（本仓库当前 0 个 tag，默认 `--ref` 必被拦） |

> `README.md` §7 的演练登记由 W8 在冻结时引用本表（`README.md` 属 W8 写锁）。

---

## 5. 禁忌清单

1. **禁止 `docker compose down -v` / `--volumes` / `down --rmi all`**。`postgres_data` 是具名卷，`-v` 会把**整个数据库连表带数据**删掉：`git reset` 回不来、`alembic upgrade` 也回不来（迁移只建 schema，不建行）。回滚的目的是「保住数据退回旧版」，删卷等于把演练变成数据销毁事故。脚本 `guard_no_destructive_flags()` 会扫 argv 并 `die`。
2. **禁止在工作树有未提交改动时 `reset --hard`**。会吞掉别人的改动（本日 W1–W8 八个包并行写同一工作树，这条尤其致命）。脚本默认拦；`--force-dirty` 只给「确认这些改动该丢」的人。
3. **禁止在迁移未回退时把代码回到旧 schema 版本**。旧代码 + 新列 = 写入 `UndefinedColumn`；旧的 `INSERT` 还可能把新列写成 NULL，之后再升级就留下「列在但值为空」的脏数据。
4. **禁止把 `alembic downgrade -1` 当「回到上一版」的默认动作**。tag 隔了 3 个迁移时它只退一格，退完仍与代码不匹配；要退到**目标代码的 head**。
5. **禁止在真仓库打演练 tag / 建演练分支**。演练一律在 `mktemp -d` + `git clone` 里做；真仓库的 tag 只在 W8 冻结点打 `v1.0-demo` / `final`。
6. **禁止 `reset` 完不探活就宣布完成**。`up` 返回 0 只代表容器起来了，不代表 `/health` 200，更不代表业务能跑。
7. **禁止在回滚脚本里连生产库 / 打生产地址**。只认 `127.0.0.1` 与 compose 内的服务名（AGENTS §4.1）。
8. **禁止改 `.env` 来「修」回滚失败**。原因在代码/迁移/镜像里，改 `.env` 只会把问题藏起来，且 `.env` 不入库、别人复现不了。

---

## 6. 故障注入：怎么「故意坏」才安全

目标：证明「改坏一行 → 回滚 → 好了」这条链真的通（TD §8 迭代 10 技术验收），且**不污染任何人的分支**。

**场地**：优先「临时 clone + 临时 tag」（零风险，删掉即净）；需要真实容器与真实库时用临时分支 `drill/inject-<日期>`（**不许 push**）；**不要直接改工作树**——会和 W1–W8 的并行改动打架。

**改哪一行**：选必然 500 但**不写库、不改 schema**的地方（如某个只读 GET 端点里写错一个常量名）。三条硬约束：

1. 不动 `app/models/**` 与 `alembic/versions/**`——那是 schema，坏在这里就不是「代码回滚」而是「迁移回滚」，两种演练混在一起就白做了（AGENTS §5）。
2. 不动 `.env` 与 `docker-compose.yml`——W1/W2 写锁，且改配置会把「代码坏」伪装成「配置坏」。
3. 一次只改一处——两处同时坏，回滚后仍坏，你会误判脚本失效。

**四步走（每步留证据）**：

```bash
git switch -c drill/inject-$(date +%m%d)              # ① 演练分支
# ② 改一行 → 本地提交（不 push）
git commit -am "drill: 故意改坏 <文件>:<行>"
git tag drill-broken
docker compose up -d --build && sleep 20
curl -i http://127.0.0.1:8000/health                  # ③ 抓到「坏」：非 200 / 异常体
docker compose logs --tail=30 backend                 #    留一段 traceback
bash scripts/rollback.sh --dry-run                    # ④ 回滚
bash scripts/rollback.sh --yes
curl -i http://127.0.0.1:8000/health                  #    抓到「好」：200 + {"status":"ok"}
```

判定：**第 ③ 步必须真的红、第 ④ 步必须真的绿**。只做绿的那一半等于没演练（与计划 §6.5 反向验收同一口径）。

**收尾**：`git switch -` → `git branch -D drill/inject-<日期>` → `git tag -d drill-broken` → `docker compose up -d --build`（镜像里的坏代码不会自己消失，不重建下次 `up` 起的还是坏镜像）。

---

## 7. 本机实测到的五条限制（2026-11-08）

| 现象 | 影响 | 处理 |
| --- | --- | --- |
| **CC Safety Net 会拦 `bash scripts/rollback.sh` 任何调用**：它静态读脚本正文，命中 `git reset --hard`（第 423 行）即拒，`--dry-run` 也拦 | agent 会话内无法再跑本脚本（含只读预演）；§4 的 dry-run 结果来自拦截生效前，后续复跑须在**人**的终端里 | 这是脚本要执行破坏性命令的固有代价，**不要为绕闸去改脚本或复制改路径**；拦截生效后一律由 Lead 在本地终端跑，或给该路径加白名单 |
| **仓库当前 0 个 tag**（`git describe --tags` → `No names found`） | 默认 `--ref` 必被拦；§4.3 真跑做不了 | 打 tag 是 W8 冻结点的活；之前用 `--ref <commit>` 或 clone 里 `git tag` |
| WSL 无 `uv`（AGENTS §3.1 已登记），也无系统 `alembic` | `uv run alembic` 在 WSL 落空 | 脚本按 `uv → alembic → .venv/bin/alembic → .venv/Scripts/alembic.exe` 探测，本机命中最后一项 |
| 本机 `anyu-postgres`/`anyu-redis` 正跑（占 5432/6379） | `down` 会停掉它们，影响正在联调的人 | W6 全程只跑 `--dry-run`，真跑由 Lead 统一做 |
| `shellcheck` 未安装 | 静态检查缺一环 | 已用 `bash -n` 兜底；脚本按 shellcheck 口径写（引号全包、`local` 与赋值分开、刻意处标 `# shellcheck disable=SC2086/SC2064`），装了就 `shellcheck scripts/rollback.sh` |

---

## 8. 与其它工作包的边界

| 包 | 边界 |
| --- | --- |
| W1 镜像 | 换 base 后第 6 步构建时间会变；脚本不关心镜像细节 |
| W2 编排 | 当前 `docker-compose.yml` 有 10 个 service（七服务 + funasr/voiceprint 等既有件）。**脚本不写死服务清单**：只硬编码数据层 `postgres redis`（alembic 需要），且可用 `ROLLBACK_DB_SERVICES` 覆盖；`down`/`up` 不带服务名，由 compose 自己枚举 |
| W5 门禁 | 回滚后若 `py-run`/`ci.py` 因代码变旧而红，属**预期**（旧版本过不了新门禁），不要为此改门禁 |
| W8 冻结 | 打 tag 之后默认 `--ref` 才可用；`README.md` §7 的演练登记归 W8，本文 §4 是其数据来源 |
| Lead | `app/main.py` 与 `docker compose up/down` 归 Lead；本文所有「待 Lead」行由集成验收时回填 |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-08 | 首版：按 迭代 10 计划 §4-W6 落地 `scripts/rollback.sh`（八步 + 三闸 + 自复制 + `GIT_OPTIONAL_LOCKS=0`）；确立「停服务 → 回代码 → 回迁移 → 起服务」次序及四条理由、A/B/C/D 迁移判定、八条禁忌、故障注入四步走；记录 `bash -n` / dry-run / clone 演练的实际结果与本机四条限制 |
