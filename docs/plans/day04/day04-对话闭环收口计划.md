> 状态：进行中（W1–W5 已落，W6 取证到「代码级」为止，浏览器级未验）　｜　版本：v1.2　｜　日期：2026-11-07
> 关联：设计记录（day04-功能收口计划）、设计记录（day04-审计记录-20261107）、设计记录（延迟护栏与口型）、契约记录（信令与对话闭环）
> 上游依据：PRD line 270（FR-DLG-05）、line 509（AC-28）、line 520（AC-39）、line 571（迭代 4 完成定义「Agent 上下文组装」）（验收）｜技术设计文档 line 1028（迭代 4 落地清单 context.py 组装）、line 1031（换人内容不写进上一位记忆）、line 847-857（§6.3 上下文组装五层与注入原则）、line 200（统一 LLM 适配器）、line 840-842（护栏）（实现）｜契约 契约记录 §C6（line 93-112）
> 变更记录见文末

# 迭代 4 · 对话闭环收口计划（把「该说出口的那句话」接上）

---

## 0. 一页结论

**这份计划只做一件事：让长者说完一句话之后，真的听到数字人回答。**

`设计记录（day04-功能收口计划）` 已经把 迭代 4 的「3D 展示 / 声纹身份 / 前端交互 / VAD 接线」收完了（12 项缺口 G-01..G-12 中 11 项已修，剩 G-06 前端接线待复核）。但那份计划把 **迭代 4 完成定义的最后一项「Agent 上下文组装」显式划在范围外**：§4 冻结白名单里没有 `app/services/llm_service.py` / `app/services/dialogue_service.py` / `app/core/context.py`，§8 不排期项里也没有登记它 —— 它不是被排除，是**静默消失**了。

实读代码确认这不是文档问题，是**代码真的没有**：

- `app/services/` 只有 8 个文件，**没有 `llm_service.py`、没有 `dialogue_service.py`**（`app/services/persona_service.py:21` 的 docstring 还留着「`llm_service` / `dialogue_service`：这里刻意不碰」的预告）。
- 全仓 `app/` + `tests/` 搜 `llm_service|LLMService|dialogue|Dialogue|build_dialogue_messages`，**只有上面那一行 docstring 命中**。
- `app/core/context.py` 全文 47 行，只有 trace_id 工具，**没有 `build_dialogue_messages`、没有 `DIALOGUE_MAX_ROUNDS`、没有 system prompt**。
- WS 主链断在 `app/audio/signaling.py:342`：`asr_final` 发出去之后**没有任何后续处理**，没有 handler 钩子。
- `MediaSession.enqueue_tts_pcm`（`app/audio/webrtc.py:1244`）**生产代码零调用**，只有测试在调。

所以「长者说了话，屏幕有字幕，但扬声器一直是哑的」。本计划补齐的就是这一段。

**与第一份收口计划的边界**：那份管「身份、3D、前端、VAD 观测」；这份管「文字进 → 上下文组装 → LLM → 文字出 → 音频回灌」。两份**不重叠、不互相替代**，本计划的 §4 只碰第一份 §4 白名单里「明确不改」的那几个文件（`llm_service.py` / `dialogue_service.py` / `core/context.py` 是新增文件，不在其白名单内；`main.py` 与 `signaling.py` 的改动行段与本计划不冲突，见 §6 写锁）。

**本日不碰**：三级路由（`app/router`，迭代 6）、System One（迭代 6）、工具调用与 MCP（迭代 6）、长期记忆与 pgvector（迭代 9）、LLM 流式与首 token 打点（迭代 5）、前端降级标记展示与状态复位（迭代 5）。

---

## 1. 验收依据（只取 PRD，逐条可测）

| # | PRD 出处 | 验收口径（逐字或紧贴原文） | 本计划的验证方式 |
| --- | --- | --- | --- |
| A1 | line 571 迭代 4 完成定义 | 「……**Agent 上下文组装**」 | 说一句话后收到基于上下文生成的回复（含最近 5 轮记忆） |
| A2 | line 571 迭代 4 功能验收（TD line 1031 同口径展开） | 「平板打开 5 秒内立起 3D 数字人**并开口说话**」 | greeting 下发后 WebRTC 音频轨**真的有声**（现只发文本，见 G-D3） |
| A3 | line 270 FR-DLG-05（P0） | 「大模型/分类器故障时固定功能继续可用，开放聊天返回明确降级话术……**闲聊返回"暂时无法回答"**」 | 断开 LLM（清空 key / 打不可达 base_url），说话 → 听到降级话术，**不编造内容** |
| A4 | line 509 AC-28 | 「匹配 A 并加载 A 的记忆上下文……**仍不确定进通用模式不写个人数据**」 | 身份 voice/device → 该 elder 的 thread 记忆；身份 generic → 不读也不写任何个人数据 |
| A5 | line 1031 TD 功能验收（PRD 同源） | 「**换一个人说话，内容不会写进上一位的记忆**」 | 结束旧 thread 后新开 thread，上一人轮次不被写入 |
| A6 | line 520 AC-39 | 「伪造长者 B 的 thread_id……**均被拒绝**（403/越权拒绝）并记审计」 | `thread_id` 只从服务端 `cascade.thread_id` 取；客户端传入值与模型传入值**一律丢弃** |

> PRD line 571 的「Agent 上下文组装」是全篇唯一把「上下文」写进 迭代 4 完成定义的地方；TD line 1028 把它落成 `app/core/context.py` 的具体职责。本计划不再引入任何第二套「记忆」形态（AGENTS §4.8）。

---

## 2. 实现基准（只取技术设计文档 + 已冻结契约）

### 2.1 上下文组装五层（TD §6.3 line 847-857）

TD 给的是**五层**，迭代 4 只做前两层 + 第五层留空口，其余登记为不排期（§8）：

| 层 | TD 口径 | 迭代 4 落点 |
| --- | --- | --- |
| ① 固定前缀 | system prompt = 人设 personality + tone_examples + 称呼 + 当前时间 + 医疗边界；**逐字节稳定以命中 prompt cache** | `app/core/context.py` 新增：DB 取 `personas.personality` / `tone_examples`，拼 `elders.name` 与 `Asia/Shanghai` 当前时间 |
| ② 短期记忆 | 最近 **5 轮**对话原文 | Redis List，key 见 S-D7 |
| ③ 历史摘要 | 更早轮次滚动摘要 | **不排期**（§8） |
| ④ 结构化状态 | Redis 会话状态 thread_id → state dict | **不排期**（§8） |
| ⑤ 长期记忆 | pgvector top5，强制 `WHERE elder_id` | **不排期**（迭代 9） |

注入原则（TD line 857）：「什么进 system prompt（人设/称呼/时间）、什么进工具参数（`elder_id` 只经 `runtime_context`）、什么进日志（脱敏后）」。本计划三处都照办：**`elder_id` 绝不进 prompt 正文，只从 `require_identity()` 拿**。

### 2.2 LLM 适配器（TD line 200 + AGENTS §1）

「DeepSeek 或 Qwen 兼容 API……**通过统一 LLM 适配器接入**」，AGENTS §1 同口径「不直连 SDK」。所以：

- 用 `httpx`（`pyproject.toml:22` 已有 `httpx>=0.27`）POST `{llm_base_url}/chat/completions`，**不引任何 LLM SDK**（AGENTS §4.12「不擅自引入新框架」）；
- 端点/模型/key **全部从 `Settings` 读**（`app/core/config.py:78-85` 七项已齐），代码里**不出现任何供应商名字面量与密钥**（AGENTS §4.2）；
- 失败四类按 AGENTS §8：临时错误（超时/连接/429/5xx）退避重试 ≤2 次，用尽 → 降级话术；其余（4xx 参数错）不重试。

### 2.3 已冻结契约（C6，line 93-112）

| 契约点 | 原文要求 | 现状 | 本计划处置 |
| --- | --- | --- | --- |
| 消息类型 | 「回复复用 C2 `tts_text`，音频走 WebRTC track——**零新消息类型**」 | `deliver` 已支持 `tts_text` | **不加新消息类型**，只复用 |
| `degraded` 字段 | `TtsTextMsgV2.degraded: bool`，「缺省 False：True=LLM 降级模板话术」 | `media_bridge._emit_greeting` 已显式发 `degraded=False`（line 194） | 回复路径同样**显式发** `degraded` |
| greeting | 「auth_ok + 身份确定后主动下发 `tts_text{text=personas.greeting}` **+ TTS 音频**，不依赖 LLM」 | **只发了文本，没发音频**（`media_bridge.py:186` docstring 自己承认「TTS 合成归对话主链（A1）」，而 A1 没做） | G-D3，见 W5 |
| 上下文组装 | 「固定前缀 + **Redis 最近 5 轮**，迭代 4 无记忆来源」 | Redis **全仓零 import** | G-D4，见 W3 |
| 已知偏离 | 「`persona_service` 没有 greeting 读口……实现暂用 `media_bridge.py:76-78` 两个模块常量」 | 现状即偏离态 | 本计划**消项**（W5 补 `get_persona_greeting`） |
| thread_id | 「服务端生成；客户端/模型传入一律丢弃」 | `identity_service._new_thread_id()`（line 107）= `uuid4().hex` | 直接复用，**不新造生成器** |

---

## 3. 缺口清单（逐条带代码证据，全部 2026-11-07 实读）

| 编号 | 缺口 | 代码证据 | 后果 | 对应步骤 |
| --- | --- | --- | --- | --- |
| **G-D1** | LLM 适配器不存在 | `app/services/` 无 `llm_service.py`；全仓 `LLMService` 0 命中；`config.py:78-85` 七项配置**零消费方** | 无法生成回复 | W1 |
| **G-D2** | 上下文组装不存在 | `app/core/context.py` 仅 47 行 trace 工具，无 `build_dialogue_messages` / `DIALOGUE_MAX_ROUNDS` / system prompt | 无「上下文」可组装 | W2 |
| **G-D3** | greeting 只发文本不发音频 | `app/audio/media_bridge.py:194` `message = {"type": "tts_text", "text": text, "degraded": False}` —— 没有 TTS 合成，没有 `enqueue_tts_pcm` | **平板打开没声音**（违反 A2） | W5 |
| **G-D4** | Redis 全仓零 import | `pyproject.toml:29` 已装 `redis>=5.2`；`config.py:74-75` 已配 `redis_url` / `redis_key_prefix`；`app/` + `tests/` 搜 `import redis` **0 命中** | 短期记忆无处落 | W3 |
| **G-D5** | WS 主链断在 asr_final 后 | `app/audio/signaling.py:342` 发完 `asr_final` 即返回，**无 handler 钩子**；全仓无 `set_dialogue_final_handler` 一类注入点（只有 `set_media_handler`，`signaling.py:278`） | 转写文本进不了编排层 | W4 |
| **G-D6** | 下行 TTS 回灌无入口 | `webrtc.py:1244 enqueue_tts_pcm` **生产零调用**（只有 `tests/test_audio_webrtc.py`）；`MediaSessionRegistry` 虽有 per-loop 单例 `_signaling_registry()`（`webrtc.py:1781`），但**没有 device_id → session 的写轨函数** | 生成了音频也送不出去 | W4 |
| **G-D7** | 对话编排不存在 | 无 `dialogue_service.py`；`turn_id` 在 `_asr_callback(device_id, trace_id)`（`webrtc.py:1818`）的形参里拿得到，但**当前被丢弃**（只把 device_id + event 传给 `deliver_asr_event`） | 三者串不起来 | W3 |
| **G-D8** | 人设 prompt 实体缺失 | `app/prompts/personas/` 只有 `README.md`(414B)，**无任何 prompt 文件**；`app/prompts/` 只有 `__init__.py`(134B) | 固定前缀从哪来没落点 | W2（自裁 S-D11） |
| **G-D9** | greeting 取值口缺失 | `app/services/persona_service.py` 只有 `get_avatar_config`（line 142）；`__all__`（line 179-181）无 greeting 读口 | 人设化开场白取不到 | W5 |
| **G-D10** | 无对话/LLM 测试 | `tests/` 25 个文件，**无 `test_dialogue*`、无 `test_llm*`** | 改动无回归网 | W6 |
| **G-D11** | 降级话术有两套，主链该用哪套未定 | `app/api/audio.py:71-77 DEGRADE_TEMPLATES`（REST 链）与 `app/audio/signaling.py:308-313 _ERROR_TALKBACK`（WS 链）并存 | 再写一套就违反 AGENTS §4.8 | W3（自裁 S-D6） |
| **G-D12** | 前端 `ontrack` 会无条件静音，一旦开场白有音频就变哑 | `frontend/src/pad/realtime.ts:310` `audioEl.muted = true`；而 `tts_text` 分支（`:175-181`）会 `muted = false`。greeting 的 `tts_text` 先到、`ontrack` 后到 → 又静音，直到长者说第一句 | 现在看不出问题（开场白从没发过音频）；W5 一旦让它出声，这行立刻成为静音 bug 源 | W5 |
| **G-D13** | 播放入口只有「带 turn_id」一种，系统播报（greeting/提醒）进不来 | `app/audio/webrtc.py:1244 enqueue_tts_pcm(turn_id, pcm)` 必须匹配 `_playback_turn_id`；`_playback_generation`（`:745` 初始 0、`:1298` barge_in +1、`:1616` close +1）**已在自增但全仓无人读它** | 开场白、以及将来所有「服务端主动说」的场景都只能发文本 | W5 |

> 说明：G-D3 / G-D6 是同一根因果的两头 —— greeting 有文本没音频，根源就是**下行写轨入口在生产侧根本不存在**。两处必须一起修，只修一头仍然是哑的。

---

## 4. 变更范围清单（冻结）

### 4.1 新增文件（4 个）

| 文件 | 行数量级 | 职责 | 依据 |
| --- | --- | --- | --- |
| `app/services/llm_service.py` | ~190 | 统一 LLM 适配器：httpx 直连 OpenAI 兼容端点、非流式、退避 ≤2 次、失败返回降级话术并标 `degraded=True` | TD line 200；FR-DLG-05 |
| `app/services/dialogue_service.py` | ~230 | 对话编排：取最近 5 轮 → 组装 messages → 调 LLM → 写回轮次；`RedisTurnStore` 同文件内 | C6 line 110 |
| `tests/test_llm_service.py` | ~280 | 成功/超时重试/重试耗尽降级/4xx 不重试/key 为空四类 | FR-DLG-05 |
| `tests/test_dialogue_service.py` | ~300 | 组装正确性（固定前缀逐字节稳定）、5 轮截断、generic 不读写个人数据、换 thread 隔离 | AC-28、A5 |

### 4.2 修改文件（6 个）

| 文件 | 改动 | 与既有改动的冲突风险 |
| --- | --- | --- |
| `app/core/context.py` | 追加 `build_dialogue_messages()` / `DialogueIdentity` / `PersonaContext` / `DialogueTurn` / `DIALOGUE_MAX_ROUNDS`，**保留现有 trace_id 工具一字不动** | 无（现在是纯新增段） |
| `app/audio/signaling.py` | 新增 `set_dialogue_final_handler(handler)` / `_dialogue_handler` 与 `asr_final` 后的钩子调用 | 只动 `deliver_asr_event`（line 330-353）**尾部**，不碰鉴权/心跳/媒体分派 |
| `app/audio/webrtc.py` | ① 新增 `async def enqueue_tts_for_device(device_id, turn_id, pcm) -> bool`（对话回复，经 `_signaling_registry()` 取会话）；② 新增 **`async def play_system_tts_for_device(device_id, pcm) -> bool`** 与 `MediaSession.enqueue_system_tts_pcm(pcm) -> bool`（系统播报，按 `_playback_generation` 校验，不要求 turn_id） | ①②都是新增函数。**`MediaSession` 只加方法、不动 `enqueue_tts_pcm` / `PlaybackAudioTrack` / `barge_in` 现有实现**。注意：`app/audio/**` 在 AGENTS §5 高风险清单，须先出方案再落码、合并前人工逐行读 diff |
| `app/audio/media_bridge.py` | `_emit_greeting` 调用侧：文本立刻下发不变；音频改为「媒体就绪后回调补播」（见 W5） | **只动 `_emit_greeting` 及其调用点**，身份级联三分支逻辑一行不改 |
| `app/main.py` | lifespan 里建 Redis 客户端 / LLMService / TTSProvider / DialogueService，注册 `set_dialogue_final_handler`，finally 注销并 `aclose()` | 只动 `lifespan`（line 65-75）与 `create_app` 装配行 |
| `frontend/src/pad/realtime.ts` | `ontrack`（`:308-318`）里 `audioEl.muted = true` 这行按 W5 第 4 条修正（G-D12） | 只动 `ontrack` 的静音时机；`tts_text` 分支（`:175-181`）不动 |

### 4.3 明确不改（列出来防手滑）

`app/audio/media_bridge.py` 的身份级联三分支逻辑（只动 `_emit_greeting` 及其调用点）｜`app/services/identity_service.py`｜`app/services/voiceprint_service.py`｜`app/models/**`｜`alembic/versions/**`（**本计划零迁移**）｜`app/api/audio.py` 的 REST 链｜`app/api/audio.py:71-77 DEGRADE_TEMPLATES`（REST 链保留自己的措辞，见 S-D6）｜`app/audio/signaling.py:308-313 _ERROR_TALKBACK` 的**文案**（只 import 不复制，见 S-D6）｜`app/router` / `app/agents` / `app/mcp_client`（迭代 6）｜前端 `degraded` 标记的视觉展示与 `speaking → listening` 复位（迭代 5）｜工作树里 39 项「照护台前端」WIP 文件（一律不碰）。

---

## 5. 实施步骤

> 工时按单人串行估；W1/W2 可并行，W3 依赖 W1+W2，W4 依赖 W3，W5 依赖 W4（且与 W4 在 `webrtc.py` 上串行，见 §6 写锁 L5），W6 收口。**总工时 20.5h**（W1 3h + W2 2.5h + W3 4h + W4 3h + W5 6h + W6 2h）。

### W1 · LLM 适配器（3h，写作用域：`app/services/llm_service.py` + `tests/test_llm_service.py`）

1. `class LLMService(settings, *, client: httpx.AsyncClient | None = None)`；`async def chat(messages: Sequence[Mapping[str, str]]) -> LLMReply`。
2. `@dataclass(frozen=True, slots=True) class LLMReply: text: str; degraded: bool`。
3. 端点 `POST {base_url}/chat/completions`，body `{model, messages, temperature, stream: False}`；超时 `settings.llm_timeout_seconds`。
4. **重试**：只对瞬态错误（`httpx.TimeoutException` / `httpx.ConnectError` / 429 / 5xx）退避，≤ `settings.llm_max_retries`（=2）次；退避用 `asyncio.sleep(0.25 * attempt)`。
5. **降级**：重试耗尽 / 4xx / `llm_api_key` 为空 → 返回 `LLMReply(text=<降级话术>, degraded=True)`，**不抛异常**（FR-DLG-05 要求「继续可用」）。
6. 不写库、不读 Redis、不发 WS、不调 TTS（保持单职责，便于单测与后续替换）。
7. 日志：只记 `model` / `latency_ms` / `attempts` / `status` / `degraded`，**不记 messages 正文、不记 key**（AGENTS §4.3、§8）。

**验收命令**：`.venv\Scripts\python.exe -m pytest tests/test_llm_service.py -q`

### W2 · 上下文组装（2.5h，写作用域：`app/core/context.py`）

1. `DIALOGUE_MAX_ROUNDS = 5`。
2. `@dataclass(frozen=True, slots=True) class DialogueIdentity(source, elder_id, display_name)`；**generic 时强制 `elder_id=None`**（对齐 `conversation_threads` 的复合 CHECK 语义）。
3. `@dataclass(frozen=True, slots=True) class PersonaContext(name, personality, tone_examples)`。
4. `@dataclass(frozen=True, slots=True) class DialogueTurn(user, assistant)`。
5. `def build_dialogue_messages(*, identity, user_text, history: Sequence[DialogueTurn], persona: PersonaContext | None, now: datetime) -> list[dict[str,str]]`：
   - messages[0] = system，**逐字节稳定**：`personas.personality` + `tone_examples` + 称呼（`elders.name`）+ `now` 的 `Asia/Shanghai` 时间 + 医疗边界声明；
   - 顺序固定为 `[system, *history(最多 5 轮 → ≤10 条), user]`；
   - **`elder_id` 不进 prompt 任何位置**（TD line 857 注入原则）；
   - **纯函数**：不查 DB、不读 Redis、不散读环境变量（`now` 与 `persona` 由调用方传入）。
6. 固定前缀中间插入任何会变的东西（比如把时间塞在系统提示最前面）会导致 prompt cache 全失效 —— **时间只放在前缀末尾一处**，这条写进函数 docstring 当约束。

**验收命令**：`.venv\Scripts\python.exe -m pytest tests/test_dialogue_service.py -q -k context`

### W3 · 对话编排 + Redis 短期记忆（4h，写作用域：`app/services/dialogue_service.py` + `tests/test_dialogue_service.py`）

1. `@dataclass(frozen=True, slots=True) class RedisTurnStore(client, key_prefix, max_turns=DIALOGUE_MAX_ROUNDS)`：
   - `key_for(thread_id) -> f"{prefix}:dialogue:{thread_id}:turns"`（前缀取 `settings.redis_key_prefix`）；
   - `async def load_recent_turns(thread_id) -> list[DialogueTurn]` / `async def append_turn(thread_id, *, user, assistant) -> None`；
   - 写入用 `pipeline(transaction=True)`：`RPUSH` 两条（user/assistant）+ `LTRIM -2*max_turns -1`，**一次往返**；
   - 存的是 `json.dumps({"user": ..., "assistant": ...})`，读取时逐条 `json.loads` 并容错跳过坏行。
2. `class DialogueService(*, llm, store, now_provider)`；`async def respond(*, thread_id, identity, user_text, persona=None) -> DialogueReply`。
3. **generic 身份全程不碰 Redis**（A4「不写个人数据」）：`identity.source == "generic"` → `history=[]` 且**不 append**。
4. Redis 不可用（连接异常）→ 记 warn 日志，按「本轮无历史」继续，**不阻断回复**（S-D9）。
5. 降级话术**复用 `app/audio/signaling.py:_ERROR_TALKBACK["llm_timeout"]`**（S-D6），不新开第三套。
6. `thread_id` **只由调用方从 `cascade.thread_id` 传入**；本模块不生成、不透出给客户端（A6）。

**验收命令**：`.venv\Scripts\python.exe -m pytest tests/test_dialogue_service.py -q`

### W4 · 链路接线（3h，写作用域：`app/audio/signaling.py` 尾部 + `app/audio/webrtc.py` 模块级函数 + `app/main.py` lifespan）

1. `signaling.py`：新增 `_dialogue_handler: DialogueFinalHandler | None` 与 `def set_dialogue_final_handler(handler: DialogueFinalHandler | None) -> None`；在 `deliver_asr_event` 里 `FINAL` 分支**发完 `asr_final` 之后**追加：
   ```
   if event.kind is not ASREventKind.FINAL or not delivered: return delivered
   await _invoke_dialogue_handler(device_id, turn_id, trace_id, text)
   ```
   异常只记 `dialogue_final_handler_failed` 日志，**不外抛**（下行不能被编排层炸掉）。
2. **`turn_id` 要传进来**：`deliver_asr_event` 现在的签名是 `(device_id, event)`，需加 `turn_id` 形参（默认 `None` 保持向后兼容），由 `webrtc.py:1822` 的调用点把 `_on_asr_event(turn_id, ...)` 那个 `turn_id` 接上（G-D7 的正是这里）。
3. `webrtc.py`：新增模块级
   ```
   async def enqueue_tts_for_device(device_id: str, turn_id: str, pcm: bytes) -> bool
   ```
   经 `_signaling_registry().get_session(device_id)` 取会话 → `await session.enqueue_tts_pcm(turn_id, pcm)`；捕获 `MediaError` / `MediaStateError` → 记日志返 `False`，**不抛**。这是 A2→A3 的唯一写轨入口。
4. `main.py` lifespan：`redis_asyncio.from_url(settings.redis_url, decode_responses=False)` → `LLMService(settings)` → `build_tts(settings)` → `RedisTurnStore(client, key_prefix=settings.redis_key_prefix)` → `DialogueService(...)`；挂 `app.state.*`；注册 `set_dialogue_final_handler(_on_dialogue_final)`；finally 里注销 handler + `await llm_service.aclose()` + `await redis_client.aclose()`。
   - **保持「没有 PG/Redis 也能启动」**：Redis 建连失败只 warn，不 raise（现有 lifespan 注释 line 65-75 明写这条纪律）。
5. `_on_dialogue_final(device_id, turn_id, trace_id, text)` 编排放 `main.py`（装配层），不塞进 `signaling.py`（那里只管连接）。
6. **硬约束（不许绕）**：`enqueue_tts_pcm` 的 stale-turn 校验在 `webrtc.py:1251`，`turn_id` 必须是 `asr_start` 那一刻 `begin_turn()` 返回的那个；回复若晚于下一次 `barge_in` 到达，**必须被拒**（那是打断语义，不是 bug）。

**验收命令**：`.venv\Scripts\python.exe -m pytest tests/test_audio_ws_auth.py tests/test_audio_webrtc.py tests/test_identity_cascade.py -q`

### W5 · greeting 真的出声 + 人设化开场白（6h，写作用域：`app/services/persona_service.py` + `app/audio/webrtc.py` 的播放入口 + `app/audio/media_bridge.py` 的 `_emit_greeting` + `frontend/src/pad/realtime.ts` 的 `ontrack`）

> v1.1 重写。v1.0 写的「第一版允许只是文本、登记为已知偏离」是**错的**：契约 `contracts.md:110` 原文就要求「`tts_text{text=personas.greeting}` **+ TTS 音频**」。这不是可协商的降级，是欠账。本节改为真的把它做出来。

**按时序排一遍，看它到底卡在哪**（全部实读，2026-11-07）：

```
① 鉴权通过
② app/audio/signaling.py:665            下发 auth_ok
③ app/audio/signaling.py:674            start_identity_cascade → 查库判身份
④ app/audio/media_bridge.py:446/457     _emit_greeting   ← 只发文本；此刻音频轨还不存在
   ─────────────────────── 以上在服务端，下面要等浏览器 ───────────────────────
⑤ 前端收到 auth_ok → getUserMedia → createOffer → 发 offer（数百 ms ~ 1s）
⑥ app/audio/webrtc.py:1876              registry.create_session
⑦ app/audio/webrtc.py:832               PlaybackAudioTrack  ← 音频轨到这里才存在
⑧ app/audio/webrtc.py:1177              用户说话 → _playback_turn_id 才有值
```

④ 必然早于 ⑤⑥⑦，所以开场白想出声会连撞两道闸：

| 闸 | 位置 | 拒绝原因 |
| --- | --- | --- |
| 轨不存在 | `app/audio/webrtc.py:1248-1249` | `if self._playback_track is None: raise MediaStateError("peer connection is not created")` |
| turn 不存在 | `app/audio/webrtc.py:1251` → `:1260` | `if self._playback_turn_id is None or turn_id != self._playback_turn_id: raise MediaStateError("stale turn for TTS playback")` |

**而且这是竞态**：④ 要查库判身份（几十~几百 ms），⑤ 要 getUserMedia（几百 ms~1s），谁先谁后不定。同类缺陷连测十次可能只崩三次 —— 所以验收必须按次数算，不能按「跑一次过了」算。

**做法：换校验依据，不取消校验。**

`turn_id` 校验不只是拦开场白，它承担「用户一开口，上一句立刻作废」这个语义（`barge_in()` `app/audio/webrtc.py:1293-1300`：`:1298 _playback_generation += 1` → `:1299 _drain_playback_queue()` → `:1300 _playback_turn_id = None`）。直接照抄参照实现的无校验版本等于抛弃这个保护。

`_playback_generation` **已经存在**（`:745` 初始 0、`:1298` barge_in +1、`:1616` close +1），但**全仓无人读它**，只出现在日志 `:1309`。它天生就是「代际号」，直接拿来当校验依据：

```
现在：turn_id  != _playback_turn_id      → 拒绝
改成：generation != _playback_generation → 拒绝
```

1. **`persona_service.py`** 追加 `async def get_persona_greeting(session, *, elder_id) -> str | None`：`elders.persona_id → personas.greeting`，缺任一环返回 `None`。（G-D9）
2. **`app/audio/webrtc.py` 新增系统播放入口**（**AGENTS §5 高风险文件，先出方案再落码、合并前人工逐行读 diff**）：
   ```python
   async def enqueue_system_tts_pcm(self, pcm: bytes) -> bool:   # 不传 turn_id
       # 校验：generation 快照 == 当前 _playback_generation（被打断则丢弃）
       # 其余与 enqueue_tts_pcm 一致：validate_downlink_pcm → _tts_pcm_to_48k_frames → 队列
       # 轨不存在（_playback_track is None）→ 返回 False，不抛（这是「还没连上」，不是错误）
   ```
   保留 `enqueue_tts_pcm(turn_id, pcm)` 原样不动 —— 对话回复继续走它，**只有系统播报（greeting、将来的提醒播报）走新入口**。两个入口共用同一队列与同一个 `_playback_generation`。
   模块级再包一层 `async def play_system_tts_for_device(device_id, pcm) -> bool`（经 `_signaling_registry().get_session(device_id)`），与 W4 的 `enqueue_tts_for_device` 并列。
3. **`media_bridge._emit_greeting`**：① 文本仍**立刻**下发（`tts_text` 与 `degraded=False` 一字不改，保证「5 秒内先看到字」）；② 把 `(device_id, text)` 挂到级联对象上；③ 媒体就绪后（`_handle_offer` 里 `create_session` 之后）回调触发：`await tts.synthesize(text)` → `await play_system_tts_for_device(device_id, pcm)`；④ 轨还没好就**等**，等到就播，等不到（会话关闭）就丢弃并记日志。**不许静默吞掉**。
4. **`frontend/src/pad/realtime.ts:310` 修 `audioEl.muted` 时序**（G-D12）：当前 `ontrack` 里无条件 `audioEl.muted = true`，而 `tts_text` 分支（`:175-181`）会 `muted = false`；greeting 的 `tts_text` 先到 → 解静音，`ontrack` 后到 → **又静音**，直到长者说第一句才恢复。**现在看不出问题，是因为开场白从来没发过音频**；一旦 W5 让开场白出声，这行立刻变成静音 bug 源。改法：`ontrack` 只在「当前没有待播内容」时静音，或直接去掉这一行（`<audio>` 是否出声由 `tts_text`/`speaking` 状态决定）。
5. 常量 `DEFAULT_GREETING_TEXT` / `GENERIC_GREETING_TEXT`（`media_bridge.py:78-79`）在 `get_persona_greeting` 可用后**改为兜底**，不删；`contracts.md:112` 的「已知偏离」登记在本步完成后**注销**，并在契约变更记录里补一行。

**验收命令**：
```powershell
.venv\Scripts\python.exe -m pytest tests/test_identity_cascade.py tests/test_audio_media_bridge.py tests/test_audio_webrtc.py -q
```
**手工验收（竞态必须按次数）**：连续开关页面 10 次，**≥8 次**在 5 秒内听到开场白；剩余 2 次允许「只有字幕没声音」，但每次都要在日志里留下原因（`greeting_audio_dropped` + 原因码），**不许静默失败**。

### W6 · 测试与取证（2h，写作用域：`tests/` 新增 2 个文件 + `设计记录（day04-取证记录）` 追加一节）

1. 补齐 G-D10 的两份测试（已计入 W1/W2 工时，此处只做**联调级**用例）：
   - **端到端一条**：mock ASR final → 假 LLM → 断言 `tts_text` 下发且 `degraded` 字段存在 → 断言 `enqueue_tts_for_device` 收到 16k PCM；
   - **降级一条**：LLM 全超时 → 断言下发的 `text` 等于 `_ERROR_TALKBACK["llm_timeout"]` 且 `degraded=True`；
   - **隔离一条**：generic 身份跑两轮 → 断言 Redis 里**零写入**。
2. 全量门禁：`.venv\Scripts\python.exe -m pytest -q`（基线 601 passed / 1 skipped / 7 errors，7 个 error 是沙箱临时目录问题，**不算代码缺陷**，取证时如实标注）。
3. `.venv\Scripts\python.exe -m ruff check app tests` 与 `ruff format --check app tests`。
4. 取证记录写「验了什么 / 怎么验 / 结果 / **未验什么**」，未验项显式标注不外推（沿用 `day04-取证记录.md` 既有口径）。

---

## 6. 执行 DAG 与写锁

```
W1 LLM 适配器 ─┐
               ├─→ W3 编排+Redis ─→ W4 接线 ─→ W5 greeting 出声 ─→ W6 取证
W2 上下文组装 ─┘
```

| 写锁 | 文件 | 归属步骤 | 说明 |
| --- | --- | --- | --- |
| L1 | `app/core/context.py` | W2 独占 | 只有 W2 写；W3 只 import |
| L2 | `app/audio/signaling.py` | W4 独占 | 只动 `deliver_asr_event` 尾部与新增 handler 三行；与 迭代 4 收口计划已完成的 C5 段不重叠 |
| L3 | `app/main.py` | W4 独占 | 只动 `lifespan` + 装配行；`ROUTERS` 循环与 `include_router` 不动 |
| L4 | `tests/` | W6 独占 | W1–W5 各写自己的新测试文件，W6 只加联调文件 |
| **L5** | **`app/audio/webrtc.py`** | **W4 与 W5 共用，串行** | W4 只加 `enqueue_tts_for_device`（模块级函数）；W5 只加 `enqueue_system_tts_pcm`（`MediaSession` 方法）+ `play_system_tts_for_device`。**两者不得并行**，且均不得改动 `enqueue_tts_pcm` / `PlaybackAudioTrack` / `barge_in` 的现有实现 |
| **L6** | **`app/audio/media_bridge.py` + `frontend/src/pad/realtime.ts`** | **W5 独占** | 只动 `_emit_greeting` 调用侧与 `ontrack` 静音时机 |

**与既有工作树的隔离**：工作树当前脏 39 项（照护台前端 WIP，含 `frontend/src/console/*`、`tests/test_console_read_api.py`、`app/api/schemas/console.py`、`alembic/versions/0006_*.py`、`app/models/audit.py` 等）。本计划**全部写作用域与这 39 项无交集**，提交时按文件逐个 `git add`，不用 `git add -A`。

---

## 7. 完工标准

| 类别 | 标准 | 判定方式 |
| --- | --- | --- |
| 功能 | 说一句话 → 收到 LLM 回复 → **听到声音**（`enqueue_tts_for_device` 返 True） | W6 联调用例 + 手工实听 |
| 功能 | **打开页面 5 秒内听到开场白**（`play_system_tts_for_device` 返 True），不只是看到字幕 | 手工开关 10 次 ≥8 次有声（W5 验收）；剩余次数必须有 `greeting_audio_dropped` 日志 |
| 功能 | 最近 5 轮上下文生效（第 6 轮起最早一轮被 LTRIM 掉） | `test_dialogue_service.py` |
| 降级 | 断开 LLM → 闲聊返回 `_ERROR_TALKBACK["llm_timeout"]` 且 `degraded=True`，不编造 | W6 降级用例（A3 / FR-DLG-05） |
| 隔离 | generic 身份零 Redis 写入；换 thread 后上一人轮次不被写入 | W6 隔离用例（A4 / A5） |
| 安全 | `thread_id` / `elder_id` 只从服务端取；日志无转写全文、无 key | 人工读 diff（AGENTS §5 高风险清单：`app/audio/**` 与 `app/services/**`） |
| 质量 | `pytest -q` 无新增 failure；`ruff check` / `format --check` 全绿 | 命令输出 |
| 文档 | 本计划 + `docs/plans/README.md` 索引同一次提交；`day04-取证记录.md` 追加本轮一节 | `git status` 核对 |
| 事实 | 未验项（如 greeting 开口次数未达标）**必须写进取证记录的「未验什么」** | 人工核对，不许把偏离写成已实现 |
| 契约 | `契约记录` 的「已知偏离（greeting 取值口）」在本轮完成后**注销**，并按该文档规则补一行变更记录 | 人工核对 diff |

---

## 8. 不排期项（显式登记，避免再出现「静默消失」）

| 项 | 理由 | 去向 |
| --- | --- | --- |
| LLM **流式**与「首 Token ≤800ms」打点 | 迭代 4 用非流式口径；首 token 预算与三档超时属 迭代 5 延迟预算 | `设计记录（延迟护栏与口型）` W1/W2 |
| 前端 `degraded` 标记展示、`speaking → listening` 复位 | 属 迭代 5 三档降级话术收口；`frontend/src/pad/protocol.ts:108-111` 的 `TtsTextMsg` 目前也没有 `degraded` 字段 | 迭代 5 W2 |
| 历史摘要层（③）与结构化状态层（④） | TD §6.3 五层中的后两层，迭代 4 无数据来源 | 迭代 9 记忆归并时一并做 |
| 长期记忆 pgvector top5（⑤） | 依赖 embedding 与 `WHERE elder_id` 强制，属记忆子系统 | 迭代 9 |
| 人设 prompt 实体文件（`app/prompts/personas/*.jinja`） | 固定前缀先用 Python 常量 + DB 取值拼装；`personas/README.md` 的三条约定（进 Git / 不含真实数据 / 同步 seed）在有多个人设时再落文件 | 待 personas 多于 1 个时 |
| 三级路由 / System One / 工具调用 / MCP | 不在 迭代 4 完成定义内 | 迭代 6 |
| 前端身份确认页与 3D 接线的复核（G-06 尾项） | 属第一份收口计划的范围 | `day04-功能收口计划.md` |

---

## 9. 自裁与待裁决

| 编号 | 事项 | 处置 | 依据 |
| --- | --- | --- | --- |
| **S-D5** | LLM 接入方式 | `httpx` 直连 OpenAI 兼容 `/chat/completions`，**不引 SDK** | TD line 200 + AGENTS §1/§4.12 |
| **S-D6** | 主链降级话术用哪一套 | 复用 `app/audio/signaling.py:308-313 _ERROR_TALKBACK["llm_timeout"]`；`app/api/audio.py:71-77 DEGRADE_TEMPLATES` 保留给 REST 链，**不新开第三套** | AGENTS §4.8 |
| **S-D7** | Redis 键格式 | `f"{settings.redis_key_prefix}:dialogue:{thread_id}:turns"`，`RPUSH` + `LTRIM`，保留最近 5 轮 = 10 条 JSON | C6 line 110 + TD §6.3 ② |
| **S-D8** | LLM 是否流式 | **非流式**（`stream: False`）；流式与首 token 打点移交 迭代 5 | 迭代 4 完成定义未要求流式 |
| **S-D9** | Redis 不可用怎么办 | 记 warn，按「本轮无历史」继续，**不阻断回复**；启动阶段建连失败也不 raise | 沿用 `main.py:65-75` 现有纪律 |
| **S-D10** | `degraded` 前端展示 | 本轮**只保证后端下发**；前端消费移交 迭代 5 | 见 §8 |
| **S-D11** | 固定前缀载体 | 迭代 4 用 Python 常量 + DB 取值（`personas.personality` / `tone_examples`），不建 `*.jinja` 实体 | TD line 1028 只要求「组装」，未要求模板文件 |
| **A-D3** | LLM 供应商（deepseek / minimax / 其他） | 代码**不硬编码任何供应商**，全部走 `settings.llm_base_url` / `llm_model` / `llm_api_key`；`.env` 里填哪家由人填 | AGENTS §4.2；`task-02` v1.1 记 minimax，`config.py:79-82` 默认为 deepseek，**两者差异留给配置解决，不进代码** |
| **S-D12** | 系统播报（无 turn）凭什么校验打断 | **换依据不取消校验**：`generation != _playback_generation` → 丢弃。`_playback_generation` 已存在且已在自增（`webrtc.py:745/1298/1616`），只是从无人在读 | AGENTS §4.8「不开第二套形态」；对照本人既有工程 asr 的无校验实现，判定为**不可直接照抄** |
| **S-D13** | 开场白音频的补播时机 | 文本**立刻**发（先看到字）；音频等 `create_session` 后再播，轨没好就等，会话关了才丢弃并记 `greeting_audio_dropped`。**不做「预置静音 turn」**（会污染 `barge_in` 语义，且 `barge_in`/`close` 都会清 `_playback_turn_id`，等于每次重造） | `contracts.md:110` 要求文本与音频同在；PRD line 571「5 秒内立起并开口说话」按 §7 完工标准第 2 条判定 |
| **S-D14** | 验收按次数还是按一次性 | **按次数**：开关 10 次 ≥8 次有声。这是竞态缺陷（③查库 vs ⑤getUserMedia 先后不定），单次通过不构成证据 | PRD line 550 的测试纪律（固定样例、可重复、结果可追溯）同理 |
| **S-D15** | 对话编排的装配代码放哪 | **新建 `app/dialogue_runtime.py`**（`DialogueRuntime`），`app/main.py` 只留 `_build_dialogue_runtime` / `_build_tts_or_none` 两个装配函数与 `runtime.register()` 一行。**偏离 W4 第 5 条「`_on_dialogue_final` 放 `main.py`」** | AGENTS §2 分层硬约束（装配层不写业务逻辑）；`main.py` 已有 259 行且是 WIP 冲突高发区，缩小其改动面 |
| **S-D16** | 开场白等轨的期限 | `GREETING_TRACK_WAIT_SECONDS = 5.0`、`GREETING_TRACK_POLL_SECONDS = 0.05`，超期记 `identity_greeting_audio_dropped{reason:"track_not_ready"}` 并放弃（**不写轨、不重试**） | PRD line 571「5 秒内立起并开口说话」；前端 `frontend/src/pad/avatar/AvatarStage.tsx:53` 的 `LOAD_TIMEOUT_MS = 5000` 是同一预算 |
| **S-D17** | `reply_stub` 与 LLM 编排同时存在时谁说话 | **有终稿钩子时 `reply_stub` 不参与**（`_on_asr_event` 里 `dialogue_ready` 为真则直接 return），否则一句话会回两遍（一遍「我听到了，您慢慢说」，一遍真回复） | 并行会话的 `reply_stub` 是 迭代 3 垫层，语义是「编排未落地时的占位」；编排落地后它自动让位，**代码一行未删**（回退安全） |

---

## 10. 落点回填（2026-11-07 实际交付，逐条对 §5）

| 步骤 | 计划工时 | 实际落点 | 状态 |
| --- | --- | --- | --- |
| W1 LLM 适配器 | 3h | `app/services/llm_service.py`（`LLMService.chat` 从不抛；`stream: False`；退避 `0.25×attempt`）+ `tests/test_llm_service.py` **21 passed** | ✅ |
| W2 上下文组装 | 2.5h | `app/core/context.py` 47→约 300 行（`build_dialogue_messages` / `DialogueIdentity` / `PersonaContext` / `DialogueTurn` / `DIALOGUE_MAX_ROUNDS` / `MEDICAL_BOUNDARY`）；语义用例并入 W3 测试文件的 `-k context` **14 passed** | ✅ |
| W3 编排 + Redis | 4h | `app/services/dialogue_service.py`（`RedisTurnStore` + `DialogueService.respond`）+ `tests/test_dialogue_service.py` **38 passed** | ✅ |
| W4 链路接线 | 3h | `app/audio/signaling.py`（`set_dialogue_final_handler` / `has_dialogue_final_handler` / `deliver_asr_event` 加 `turn_id`+`trace_id`）、`app/audio/webrtc.py`（`enqueue_tts_for_device`）、**新增 `app/dialogue_runtime.py`**（计划的 `_on_dialogue_final` 落成独立装配模块，见 S-D15）、`app/main.py` lifespan 真装配 | ✅ |
| W5 greeting 出声 | 6h | `app/services/persona_service.py`（`get_persona_greeting`）、`app/audio/media_bridge.py`（`_Cascade.greeting` / `_resolve_greeting` / `_spawn_greeting_audio`）、`app/audio/webrtc.py`（`enqueue_system_tts_pcm` / `play_system_tts_for_device` / `playback_generation_for_device` / `playback_track_ready_for_device` / `has_playback_track`）、`frontend/src/pad/realtime.ts`（G-D12） | ✅ 代码；⚠️ 浏览器实听未验 |
| W6 测试与取证 | 2h | `tests/test_dialogue_runtime.py` **51 passed**、`tests/test_persona_greeting.py` **7 passed**；`设计记录（day04-取证记录）` §9；全量 **787 passed / 覆盖 83.89%** | ✅ 代码级；⚠️ 浏览器级未验 |

**与计划的偏差（两条，均已登记为自裁）**：

- **S-D15**：W4 第 5 条原写「`_on_dialogue_final` 放 `app/main.py`」。实际落为独立模块 `app/dialogue_runtime.py`（`DialogueRuntime`）。理由：`main.py` 只该管装配，把「ASR final → 编排 → 下发 → 写轨」写在 `main.py` 会让它同时承担路由装配与业务编排两种职责，违反 AGENTS §2「`app/api/` 与装配层不写业务逻辑」的同类纪律；且 39 项 WIP 之外再多动一次 `main.py` 也增加冲突面。`main.py` 只保留 `_build_dialogue_runtime` / `_build_tts_or_none` 两个装配函数与 `runtime.register()` 一行。
- **W5 第 2 条补充**：计划只写「等 `create_session` 后再补」，未写「轨没来要等多久」。实际落为 `GREETING_TRACK_WAIT_SECONDS = 5.0` / `GREETING_TRACK_POLL_SECONDS = 0.05`（`app/audio/media_bridge.py`），依据是 PRD line 571 的 5 秒与前端 `AvatarStage.tsx:53` 的 `LOAD_TIMEOUT_MS = 5000`。

**明确没做（对应 §8 不排期项与取证记录 §9.3）**：浏览器实听、`pnpm build`（沙箱 esbuild EPERM）、Redis/LLM 真连通、generic 零写入的真 Redis 复核、`app/audio/**` 的人工逐行 diff 复核。

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 初稿。第一份收口计划（`day04-功能收口计划.md`）把 迭代 4 完成定义中的「Agent 上下文组装」静默划出范围且未登记不排期，故单独立此第二份。全文缺口（G-D1..G-D11）均由 2026-11-07 实读代码取证，**不引用 `docs/team_tasks/day04/progress` 任何自动生成件**。给出 W1–W6 六步、4 条写锁、四类完工标准、7 项不排期登记、8 项自裁（S-D5..S-D11、A-D3）。总工时 16h |
| v1.1 | 2026-11-07 | **W5 重写：从「允许只发文本 + 登记已知偏离」改为「必须真的出声」。** 依据 `契约记录` 原文——greeting 本来就要求 `tts_text` **+ TTS 音频**，v1.0 把它当可协商降级是错的。新增缺口 G-D12（`frontend/src/pad/realtime.ts:310` 的 `audioEl.muted = true` 会让开场白音频静音）、G-D13（播放入口只有带 `turn_id` 一种，系统播报进不来）。方案为**换校验依据不取消校验**：新入口 `enqueue_system_tts_pcm(pcm)` 按 `_playback_generation` 判定（该字段已在 `app/audio/webrtc.py:745/1298/1616` 自增，仅无人读），`enqueue_tts_pcm` 原样不动。§4.2 修改文件 4→6（增 `app/audio/media_bridge.py`、`frontend/src/pad/realtime.ts`），§6 新增写锁 L5（`webrtc.py` 由 W4 与 W5 串行）与 L6，§7 新增「5 秒内听到开场白」完工标准与契约注销项，§9 新增 S-D12..S-D14。W5 工时 1.5h→6h，总工时 16h→20.5h。参照实现：本人既有工程 asr 的 ProviderTTSTrack（建链即建轨、入队不校验 turn），但**其无校验版本不可直接照抄**（会丢掉「用户一开口上一句作废」的打断语义） |
| v1.2 | 2026-11-07 | **W1–W5 全部落码、W6 取证到代码级**。新增 §10「落点回填」，逐条对 §5 列出实际文件与状态；新增自裁 S-D15（编排装配独立成 `app/dialogue_runtime.py`，偏离 W4「放 main.py」）、S-D16（等轨 5s 期限）、S-D17（有终稿钩子时 `reply_stub` 让位，避免一句话回两遍）。新增测试 4 个文件共 **117 passed**（llm 21 / dialogue 38 / runtime 51 / persona_greeting 7），全量 **787 passed / 覆盖 83.89% / ruff 全绿 / tsc exit=0**。契约 `contracts.md:112` 的 greeting 取值口偏离已注销（该文件变更记录 v1.3）。**未验**：浏览器实听、`pnpm build`（沙箱 esbuild EPERM）、Redis/LLM 真连通、generic 零写入的真 Redis 复核、`app/audio/**` 人工逐行 diff —— 全部写进 `day04-取证记录.md` §9.3，状态改「进行中（W6 取证到代码级为止）」 |
