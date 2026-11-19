# 迭代 10 机构知识进主链路 · 端到端验收报告（FR-KNW-01~05 / AC-43~47）

> 状态：已完成 ｜ 版本：v1.0 ｜ 日期：2026-11-12
> 关联：团队任务板 task-3（owner kb-eval）；用例 `tests/test_knowledge_e2e.py`；固定集 `eval/fixtures/knowledge.jsonl`
> 上游依据：PRD §4.12 机构知识（FR-KNW-01~05）、§5.1 验收 AC-43~47；实现口径见技术设计文档知识库章节
> 实测环境：Windows / Python 3.12.13 / pytest 9.2.0 / ruff 0.17.3 / SQLAlchemy 2.1.8 / 本地 PostgreSQL `anyu_test`（127.0.0.1:5432，已是 alembic head；本轮**未** upgrade / create / drop）
> 变更记录见文末

## 0. 结论（先说人话）

| 项 | 实测结论 |
| --- | --- |
| 新用例 | `tests/test_knowledge_e2e.py` **13 passed / 0 failed**（5 个场景 + 8 行固定集逐行参数化，2.14s） |
| 知识相关五个文件合计 | **88 passed / 0 failed**（6.88s，含本文件 13 例） |
| 链路是否真的通了 | 是：真库 `ingest_document` → `build_knowledge_lookup` 召回闭包 → `guard_knowledge_hits` → `build_agent_context` 渲染 → `companion.run_turn` **发给模型的真实 messages**，断言对象是后者 |
| AC-43 / 44 / 46 / 47 | 有真库端到端证据（见 §1） |
| AC-45 | **只有提示词规则，没有代码判定** —— 用例只钉住已实现的那一半，未覆盖项 U1 如实登记 |
| 固定集 | 8 行（AC-43×2 / AC-44×1 / AC-45×1 / AC-46×2 / AC-47×2），且**由用例逐行执行**，不是没人跑的心愿单 |

**一句话**：机构知识「能检索到」和「能进到模型嘴里」这两件事，本轮用真库 + 真闭包 + 真 `run_turn` 串起来验过了；**做不到的地方（冲突判定、真 embedding、真语音、召回质量）没有装作做到**，全部列在 §5。

## 1. AC ↔ 用例映射

追踪关系（PRD §5.4）：§4.12 机构知识 → AC-43 / AC-44 / AC-45 / AC-46 / AC-47，下表逐条落到用例。

| AC | PRD 要求（§4.12 / §5.1） | 对应用例（`tests/test_knowledge_e2e.py`） | 固定集行 |
| --- | --- | --- | --- |
| AC-43 | 检索回答必须带**来源**与**生效时间**，不得编造 | `test_hit_reaches_prompt_with_title_date_and_footer`：`reason="ok"`；system 里出现 `[参考资料 1｜来源: 《社区餐厅午餐时段规定》…`、`生效: 2026-11-08`、正文 `11:30`/`餐厅二层`、`CONTEXT_FOOTER`；**不含**未命中提示 | `knw-43-01`、`knw-43-02` |
| AC-44 | 知识按文档版本化，**旧版不得再检索** | `test_superseded_version_replaces_old_in_prompt`：`supersede_document` 换版后只命中 v2（`生效: 2026-11-01`），旧版正文 `11:00` 与旧生效日**一个字都不进 prompt**；旧版仍在库中 `status="retired"` 可追溯 | `knw-44-01` |
| AC-45 | 与业务表（药单/健康/工单）冲突时**以业务表为准**并提示「这条信息最近有更新」 | `test_business_conflict_is_prompt_rule_only`：断言 `AGENT_SYSTEM_PROMPT` 第 7 条存在（`app/agents/companion.py:198-199`），且知识一律渲染成「参考资料」而非权威事实 | `knw-45-01` |
| AC-46 | 知识正文**注入防护 P0**：指令不执行、有脱敏拦截日志 | `test_injected_body_is_blocked_and_never_enters_prompt`：`blocked == 1`、`reason="no_match"`、`hits == ()`；注入句 `忽略以上指令…他人病历` **不进 messages**；拦截日志只记条数与规则 id，**不落正文** | `knw-46-01`、`knw-46-02` |
| AC-47 | 无命中必须说「我查不到」并给替代路径，**不得补造** | `test_unrelated_question_gets_miss_note_without_sources`：`reason="no_match"`、上下文含「未检索到机构知识」、**没有任何** `[参考资料`/`｜来源:`/文档标题 | `knw-47-01`、`knw-47-02` |
| AC-43~47 | 固定集逐行对照 | `test_fixture_row_matches_implementation`（参数化 8 例） | 全部 8 行 |

> 「仅管理员可录入」（FR-KNW-02 的另一半）与「非管理员写入被审计」由 `tests/test_knowledge_api.py::test_non_admin_staff_cannot_write_and_is_audited`（line 412）覆盖，不在本文件范围。

## 2. 跑过的命令与结果（原样）

先设测试库（AGENTS §9 第三条：不设会 TRUNCATE 开发库）：

```powershell
$env:ANYU_TEST_DATABASE_URL="postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test"
```

### 2.1 静态门禁

```powershell
.venv\Scripts\ruff.exe check tests/test_knowledge_e2e.py
# All checks passed!

.venv\Scripts\ruff.exe format --check tests/test_knowledge_e2e.py
# 1 file already formatted
```

### 2.2 本文件用例

```powershell
.venv\Scripts\python.exe -m pytest tests/test_knowledge_e2e.py -q -p no:cacheprovider
# .............  [100%]
# 13 passed in 2.14s
```

### 2.3 知识相关五个文件（确认没把别人的用例带红）

```powershell
.venv\Scripts\python.exe -m pytest tests/test_knowledge.py tests/test_knowledge_context.py `
  tests/test_knowledge_lookup.py tests/test_agent_context_knowledge.py tests/test_knowledge_e2e.py `
  -q -p no:cacheprovider
# 88 passed in 6.88s
```

没跑全量 pytest（任务卡明确禁止）；上面是知识链路的五个文件，其余模块本轮未触碰。

## 3. 用例怎么造出来的（关键设计与踩过的坑）

### 3.1 距离可控的假 embedding（不打网络）

`KeywordAxisProvider`（`tests/test_knowledge_e2e.py` 内）把文本映射到 1024 维单位向量的某一轴：

| 文本含 | 轴 | 与查询同轴时的余弦距离 |
| --- | --- | --- |
| 午餐 / 开饭 / 用餐 / 开餐 / 餐厅 / 吃饭 | 0 | 0.0（命中） |
| 熄灯 / 关灯 / 作息 / 夜里 / 夜间 / 睡觉 | 1 | 0.0（命中） |
| 其它 | 1023 | 1.0（**> `DEFAULT_MAX_DISTANCE=0.40`，被闸门丢弃**） |

于是「命中」「无命中」「命中但被 guard 拦下」三条路径都能稳定复现，且不依赖任何外部服务。
契约与真 provider 一致（`dimension` / `available` / `async embed`），所以能从 `ingest_document` 一路走到 `search_knowledge_result`，**不需要 monkeypatch 检索层**。

### 3.2 召回闭包必须看得见「本用例未提交的行」

夹具每例开一个外层事务、结束回滚（沿用 `tests/test_knowledge.py` 口径），而 `build_knowledge_lookup` 会**新建会话**。若工厂指向新连接，就看不见未提交的 ingest 行，用例会假绿/假红。所以 `session_factory` 夹具绑在**同一个 `AsyncConnection`** 上：

```python
async_sessionmaker(bind=db_conn, expire_on_commit=False)
```

### 3.3 钉住「现在」

检索层用 `knowledge_service._utc_now()` 取当下。autouse fixture 把它钉在 `2026-11-09 04:00Z`（上海 12:00），否则「生效 / 未生效」的断言会随机器时钟漂移。

### 3.4 断言口径的坑：固定前缀自己就写着「参考资料」

`AGENT_SYSTEM_PROMPT` 的规则段含「参考资料」，few-shot 例子里还含一个机构规定名。所以：

- 命中证据只用**块首标记** `[参考资料 1｜来源: `，不用「参考资料」四个字（否则恒真）；
- 判定「上下文里有没有来源」时只看**除固定前缀之外**的 system 文本（`_context_text`）；
- 本轮因此把用例文档标题定为《**社区餐厅午餐时段规定**》而不是《午餐时间规定》——后者出现在 few-shot 例子里，撞上会让断言失去区分度。

### 3.5 AC-47 要有「对照组」，否则证明不了是闸门拦的

`no_match` 可能是「库里没东西」也可能是「距离超阈值被丢」。用例补了一层对照：

```python
ungated = await knowledge_service.search_knowledge_result(..., max_distance=None)
assert ungated.hits and min(h.distance for h in ungated.hits) == pytest.approx(1.0)
assert DEFAULT_MAX_DISTANCE < 1.0
```

关掉闸门能看到候选、打开闸门就没有 —— 这才叫「闸门生效」。

### 3.6 AC-46 连日志一起验

guard 命中会打 `knowledge_injection_blocked`（`logger.warning`，`extra` 里只有条数与规则 id）。用例断言：记录存在、**`他人病历` / `药单` / `22:00` 不在 `caplog.text` 里** —— 拦截日志本身不能成为二次泄露源。

## 4. 固定集 `eval/fixtures/knowledge.jsonl`

每行字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 行 id（`knw-43-01` 这类，前缀即 AC） |
| `ac` | 归属验收条款 |
| `query` | 长者问句（**原句**，不做归一化） |
| `expect_reason` | 期望 `KnowledgeSearchReason`：本轮只出现 `ok` / `no_match` |
| `expect_markers` | 必须在**知识层文本**里出现的字符串 |
| `forbid_markers` | 必须**不**出现的字符串（旧版正文、注入句、来源块标记等） |
| `note` | 这条钉的是什么、对照哪个用例 |

**判定面**：`expect_markers` / `forbid_markers` 都在「命中渲染 + 未命中提示」拼起来的知识层文本上判定，**不含**固定 system 前缀（理由同 §3.4）。

**行数分布**：AC-43×2、AC-44×1、AC-45×1、AC-46×2、AC-47×2 = 8 行，全部由 `test_fixture_row_matches_implementation` 逐行执行（参数化 id 就是行 id，失败一眼能指到行）。

**没有写进固定集的能力**（PRD 没要求，不编）：PDF/Word 解析、rerank、混合检索（BM25+向量）、独立 RAG 平台、多轮追问式检索、知识图谱。

## 5. 未覆盖项（如实登记，不许当已通过）

| # | 未覆盖 | 为什么没覆盖 | 影响 / 建议 |
| --- | --- | --- | --- |
| **U1** | **AC-45「与业务表冲突以业务表为准」只有提示词规则，没有代码判定** | `app/` 里没有「业务表 vs 知识」的冲突仲裁实现；唯一的约束是 `AGENT_SYSTEM_PROMPT` 第 7 条（`app/agents/companion.py:198-199`：以业务数据为准 + 提示「这条信息最近有更新」）。用例只能钉住「提示词在、知识被标成参考资料」这半边 | 模型不听话时代码兜不住；建议后续在 `app/services/` 加显式冲突判定并补一条真业务表对照用例（本卡不允许改 app/，故未做） |
| **U2** | **真机语音未验** | 本轮是文本入、文本出（`run_turn` 直接吃字符串）；ASR / TTS / WebRTC 完全不参与 | 「长者说一句、数字人念出来」这条只验到文本层；语音链路另需真机验证 |
| **U3** | **召回质量未评测** | 假 provider 只证明**链路通**，不证明**检索准**；没有 recall@k / MRR / 相关性人工标注 | 「该查到的查不到」这类问题本轮测不出来；建议单独做一次真 embedding 的召回评测 |
| **U4** | **真实 embedding provider 未接** | 用例全程假 provider；真 key 未配、未跑 | 线上若没配 key，行为是 `degraded`（该路径有 `tests/test_knowledge_api.py::test_degraded_embeddings_are_visible_not_silently_empty` 覆盖），但**真模型跑通与否本轮未知** |
| **U5** | **种子文档未直接跑** | 用例走 `ingest_document`（与种子 CLI 同一写入口），但没有解析 `docs/knowledge_seed/*.md` 的真 front-matter | 种子文件的 front-matter 解析由 `tests/test_knowledge_seed.py` 覆盖，不在本文件；「种子 → 真库 → 对话」这条完整链路仍未一次跑通 |
| **U6** | 录入权限 / 审计 | 属 API 层，`tests/test_knowledge_api.py` 已覆盖（line 412、437、567） | 本文件不重复 |

## 6. 复跑

```powershell
$env:ANYU_TEST_DATABASE_URL="postgresql+asyncpg://anyu:anyu@127.0.0.1:5432/anyu_test"
.venv\Scripts\ruff.exe check tests/test_knowledge_e2e.py
.venv\Scripts\ruff.exe format --check tests/test_knowledge_e2e.py
.venv\Scripts\python.exe -m pytest tests/test_knowledge_e2e.py -q -p no:cacheprovider
```

前置：`anyu_test` 库已存在且是 alembic head（本轮未建库、未迁移）。用例自建文档、自造向量，不依赖种子文件、不依赖任何真实 API。

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-12 | 首版：AC-43~47 端到端用例 13 例（含固定集逐行 8 例）、固定集 `eval/fixtures/knowledge.jsonl` 8 行、未覆盖项 U1~U6 |
