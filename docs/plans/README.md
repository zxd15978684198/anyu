# 计划索引（docs/plans）

> 规则见 `AGENTS.md` §10。**新增计划必须同一次提交里加进下表**，否则等于没写。
>
> 本表只登记**仍在推进**的计划。已终结的件（`已完成` / `已冻结` / `已废弃`）留在各自目录原地，
> 状态以文件头的 `> 状态：` 为准，从本表删行即可——不搬运、不改正文。
>
> **本目录现有 3 份未完成项**（下表）。

## 落盘与命名

- **按迭代落盘（强制）**：归属某个迭代的**计划**放 `docs/plans/day<NN>/`；目录名两位补零，不写 `day8` / `day-8`。
- 只有**跨迭代、不归属某一天**的一次性方案才留在 `docs/plans/` 根下；绑定某天的 `adhoc` 进当天目录。
- 文件名统一 `<类别>-<编号>-<主题>.md`：`day` / `task` / `adhoc` 三类，编号补零两位。
- 终态件不删：文件头改状态、从索引表删行，作为沿革记录可查。

## 索引（仅未完成项）

| 文件 | 一句话场景 | 状态 | 最后更新 |
| --- | --- | --- | --- |
| [day04/day04-对话闭环收口计划.md](./day04/day04-对话闭环收口计划.md) | 迭代 4 第二份收口计划（补齐「Agent 上下文组装」这一项被第一份收口计划静默划出范围的活）：以 PRD line 270 FR-DLG-05 / line 509 AC-28 / line 520 AC-39 / line 571 迭代 4 完成定义为验收，TD line 1028·847-857·200 与契约 C6 为实现基准；逐条列 G-D1..G-D13 十三项缺口（LLM 适配器/上下文组装/greeting 只发文本无音频/Redis 零 import/asr_final 后无钩子/`enqueue_tts_pcm` 生产零调用/编排层缺失/prompt 实体缺失/greeting 读口缺失/无测试/两套降级话术/**`realtime.ts:310` 无条件静音**/**播放入口只有带 turn_id 一种**）；W1 LLM 适配器（httpx 直连不引 SDK）→ W2 `context.py` 组装五层的前两层 → W3 编排 + Redis 最近 5 轮 → W4 接线（`set_dialogue_final_handler` + `enqueue_tts_for_device`）→ **W5 greeting 真的出声（v1.1 重写：换校验依据为 `_playback_generation`，新入口 `enqueue_system_tts_pcm`，参照本人既有工程 asr 的 ProviderTTSTrack 实现，其无校验版本不可照抄）** → W6 联调取证；6 条写锁（L5 = `webrtc.py` 由 W4/W5 串行）、7 项不排期登记、S-D5..S-D14 十一项自裁；总工时 20.5h | 进行中（W1–W5 全落、W6 取证到代码级，浏览器实听未验） | 2026-11-07 |
| [day10/adhoc-20261113-语音三态真机验收清单.md](./day10/adhoc-20261113-语音三态真机验收清单.md) | 语音三态真机验收清单（v1.0 进行中）：给「正常音量 / 拒绝麦克风权限 / 拔麦」三态各一节，每节都按同一骨架——判定什么（WS 连接状态 · 说完有没有回话 · 降级提示文案）、怎么判（看状态条与字幕、看 Console 的 `[vad]`/`[ice]`/`[pad]` 前缀、看 WS `/ws/signal` 帧与 `/api/audio/utterance` 响应）、15 分钟内可照做步骤、证据留存（截图命名 `A-/B-/C-`、console 日志、WS Frames 与 utterance JSON）。三态顺序固定：先跑 A 基线再跑 B/C；文案以实现为准、先按代码实际输出记录（话术出处 `frontend/src/pad/audio/capture.ts:9-14`、`components/HoldToTalk.tsx:50-55`、`app/core/degrade_speech.py:26-33`）。登记两处已知缺口：拔麦后前端无自动提示（`onDeviceChange` 定义于 `capture.ts:47-51` 但全仓零调用点、无 track `ended` 监听）、权限被拒但 WS 已连时状态条仍显示「正在听」（`frontend/src/pad/App.tsx:905` 只有两档），本轮只登记不修 | 进行中 | 2026-11-13 |
| [day05/adhoc-20261107-iter2演示脚本.md](./day05/adhoc-20261107-iter2演示脚本.md) | 迭代 5 Iter2 验收演示脚本（5 分钟走查） | 进行中 | 2026-11-17 |
