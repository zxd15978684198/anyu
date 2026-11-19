/**
 * 逐句字幕（方案 B）的守卫用例（2026-11-10 产品负责人实测「字幕领先语音」后加的）。
 *
 * 测两件事：
 * 1. `subtitlePlan.ts` 的取值逻辑（真代码，纯函数，Node 能直接跑）；
 * 2. **源码契约**：`realtime.ts` 必须真的按 `tts_segments` 推进、`protocol.ts` 必须收录
 *    这个下行型 —— 这条链路最容易的退化方式是「前端悄悄退回整段文本一次性上屏」，
 *    而那种退化在类型层是合法的（`tts_text` 本来就在），只能靠字面量兜住。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { pickSegmentIndex, revealedCharCount } from "../src/pad/subtitlePlan.ts";
import type { TtsSegment } from "../src/pad/protocol.ts";

const read = (name: string): string => readFileSync(new URL(`../src/pad/${name}`, import.meta.url), "utf8");

const PLAN: TtsSegment[] = [
  { text: "您好呀，", start_ms: 0, duration_ms: 1200 },
  { text: "我是小满，", start_ms: 1200, duration_ms: 1200 },
  { text: "今天感觉怎么样？", start_ms: 2400, duration_ms: 2400 },
];

test("播放未开始时显示第一句（锚点即第一句起点，不返回 -1）", () => {
  assert.equal(pickSegmentIndex(PLAN, 0), 0);
  assert.equal(pickSegmentIndex(PLAN, -50), 0);
});

test("恰好到达某句起点时切到该句（边界含等号）", () => {
  assert.equal(pickSegmentIndex(PLAN, 1199), 0);
  assert.equal(pickSegmentIndex(PLAN, 1200), 1);
  assert.equal(pickSegmentIndex(PLAN, 2399), 1);
  assert.equal(pickSegmentIndex(PLAN, 2400), 2);
});

test("播放超过最后一句结尾时停在最后一句（不消失、不回退）", () => {
  assert.equal(pickSegmentIndex(PLAN, 4800), 2);
  assert.equal(pickSegmentIndex(PLAN, 60_000), 2);
});

test("单句计划始终是 0（服务端切句只出一句的常态）", () => {
  assert.equal(pickSegmentIndex([{ text: "好的。", start_ms: 0, duration_ms: 800 }], 5000), 0);
});

test("源码契约：realtime.ts 按 tts_segments 推进字幕、换回复时复位", () => {
  const realtime = read("realtime.ts");
  assert.ok(realtime.includes('case "tts_segments":'), "缺少 tts_segments 分支：字幕退化为整段一次上屏");
  assert.ok(
    realtime.includes("this.applySubtitlePlan(msg.segments, msg.estimated !== true)"),
    "收到计划后必须交给 applySubtitlePlan（第二个参数标注「服务端是否真有音频」）",
  );
  assert.ok(realtime.includes("pickSegmentIndex(plan, elapsed)"), "推进必须走统一取值函数（时间轴口径唯一）");
  assert.ok(
    realtime.includes("const elapsed = this.playbackElapsedMs(now);"),
    "elapsed 必须取自唯一时钟 playbackElapsedMs：字幕与口型各算各的就是三线不同步的根子",
  );
  assert.ok(realtime.includes("this.resetSubtitlePlan();"), "tts_text/收窗时必须复位旧计划，否则上一条回复的字幕会串到这一条");
});

test("源码契约：protocol.ts 收录逐句字幕下行型", () => {
  const protocol = read("protocol.ts");
  assert.ok(protocol.includes('type: "tts_segments";'), "缺少 TtsSegmentsMsg 定义");
  assert.ok(protocol.includes('"tts_segments",'), "SERVER_SIGNAL_TYPES 未收录 tts_segments → 会被当成未知帧丢弃");
});

test("源码契约：播放态驱动不得依赖 3D 是否加载完成", () => {
  const realtime = read("realtime.ts");
  assert.ok(
    !realtime.includes("if (this.lipRaf || !this.avatar) return;"),
    "startLipLoop 又在依赖 avatar：3D 降级（FR-3D-02 允许）时口型/字幕/「正在说」会全停",
  );
  // 2026-11-12：放开静音改成「放开，除非媒体在播」（`speechSuppressed`）。
  // 钉的因果不变 —— ontrack 不许把已经该出声的这一段静音掉；新增的唯一例外是
  // 戏曲/电影正在响时压住数字人，那由 `setSpeechSuppressed` 显式控制、可逆。
  assert.ok(
    /this\.cb\.audioEl\.muted = this\.speechSuppressed;/.test(realtime),
    "ontrack 必须放开静音（未被压住时）：远端轨只承载 TTS，静音只会让开头几秒变哑",
  );
  assert.ok(
    !/this\.cb\.audioEl\.muted = true;/.test(
      realtime.slice(realtime.indexOf("ontrack"), realtime.indexOf("ontrack") + 4000),
    ),
    "ontrack 里出现无条件 muted = true：实测开场白前 4.21 秒会哑掉（2026-11-11）",
  );
  assert.ok(
    !realtime.includes("if (!this.ttsPlaying && !this.speakingWindow) this.cb.audioEl.muted = true;"),
    "ontrack 又按 ttsPlaying/speakingWindow 静音了：实测开场白前 4.21 秒会哑掉（2026-11-11）",
  );
});

test("源码契约：播放态按计划时长收口，不等远端轨残留噪声自然静音", () => {
  const realtime = read("realtime.ts");
  assert.ok(
    realtime.includes("const PLAYBACK_END_GRACE_MS = 500;"),
    "缺少计划时长宽限：远端轨排空后仍有 1-3s 噪声尖峰，「正在说」会被拖住",
  );
  assert.ok(
    realtime.includes("this.subtitlePlanEndMs = segments.reduce("),
    "subtitlePlanEndMs 必须由各句 start_ms+duration_ms 累加得出（服务端是唯一时长来源）",
  );
  // 2026-11-11 复审 P1-6：只发 tts_text 的服务端永远填不上 subtitlePlanEndMs，
  // 收口必须退回 planFallbackMs，否则「静音 3s」与「12s 心跳」会被残留尖峰同时废掉。
  assert.ok(
    realtime.includes(
      "const planEndMs = this.subtitlePlanEndMs > 0 ? this.subtitlePlanEndMs : this.planFallbackMs;",
    ),
    "计划时长必须带兜底：没有 tts_segments 时退回 planFallbackMs",
  );
  assert.ok(
    realtime.includes("this.playbackElapsedMs(now) > planEndMs + PLAYBACK_END_GRACE_MS"),
    "到点必须收口：否则胶囊停在「播放·正在说」（用户 m08520 报的现象）",
  );
  assert.ok(
    realtime.includes("this.planFallbackMs = REPLY_TEXT_FALLBACK_MS;"),
    "开窗时必须给本轮装上兜底时长（只发 tts_text 的轮次也要有上限）",
  );
  assert.ok(realtime.includes("this.endPlayback();"), "收口走 endPlayback()，胶囊才能回「待机」");
});

test("源码契约：说话态用平滑能量，单帧尖峰不算在说话", () => {
  const realtime = read("realtime.ts");
  assert.ok(
    realtime.includes("this.lipEnergyAvg = this.lipEnergyAvg * 0.86 + rms * 0.14;"),
    "说话态必须由平滑能量判定：逐帧 rms 会让一次抖动把静音累计清零（口型也会跟着乱跳）",
  );
  assert.ok(
    realtime.includes("const speaking = this.lipEnergyAvg > MOUTH_SPEAK_THRESHOLD;"),
    "speaking 仍按逐帧 rms 判定：孤立尖峰会把「正在说」无限续命",
  );
});

/* ------------------------------------------------------------------ *
 * 打字机「往回缩 2 个字」回归（2026-11-11 产品负责人 CDP 假麦实测 3→1→2…）
 *
 * 旧根因：`lipFrame()` 的一次性重锚分支把 `subtitleIndex` 打回 -1，等价于「从第一句第 0
 * 毫秒重放」。该分支已随「播放侧唯一时钟」整块删除。
 *
 * 2026-11-12 再改：锚点口径从「按已跑毫秒倒推 baseSamples」收成**直接取首个越门限
 * quantum**。倒推意味着「计划到达 → 真出声」的空档被算成已播时长，字幕会领先声音
 * （远端轨曾领先数秒、元素路径领先 68ms）；而按能量定锚，零点就是出声点，不需要倒推。
 * 下面三组断言分别钉住：定锚不重置句号、elapsed 从出声点单调增长、组件层 reveal 只增不减。
 * ------------------------------------------------------------------ */

test("源码契约：时间轴零点由采样计数定，定锚不重置 subtitleIndex", () => {
  const realtime = read("realtime.ts");

  assert.ok(
    !realtime.includes("SUBTITLE_REANCHOR_MS"),
    "一次性重锚又回来了：两个锚点就是「字幕越说越偏」的根子，采样时钟下它没有存在理由",
  );
  const anchorStart = realtime.indexOf("if (this.clockAnchorSamples === null) {");
  assert.ok(anchorStart >= 0, "找不到采样锚点分支：时间轴又退回墙钟了吗？");
  const anchorBlock = realtime.slice(anchorStart, anchorStart + 700);

  assert.ok(
    !anchorBlock.includes("this.subtitleIndex = -1;"),
    "定锚又把句序号打回 -1：等于从第 0 毫秒重放，字幕会往回缩（用户看到的 3→1）",
  );
  assert.ok(
    anchorBlock.includes("this.clockAnchorSamples = this.clockEnergySamples;"),
    "锚点没取首个越门限 quantum：倒推或按墙钟都会把「计划到达 → 真出声」的空档算成已播时长",
  );
  assert.ok(
    anchorBlock.includes("this.subtitleAnchorAt = now;"),
    "没有采样时钟（老内核 / worklet 加载失败）时必须退回「真出声这一帧」的墙钟零点",
  );
});

test("行为：按能量定锚后 elapsed 从出声点起单调增长，不回退", () => {
  const rate = 48_000;
  const energySamples = 1_000_000; // worklet 记下的首个越门限 quantum
  // 2026-11-12：锚点就是能量采样本身，不再按 leadMs 倒推。
  // 倒推那版的测试留在下面这条断言里——它证明「倒推会把空档算成已播」，所以不该再有倒推。
  const anchor = energySamples;

  let previous = -1;
  for (const sinceEnergyMs of [0, 20, 100, 800, 3000, 6000]) {
    const samples = energySamples + Math.round((sinceEnergyMs * rate) / 1000);
    const elapsed = ((samples - anchor) / rate) * 1000;
    assert.ok(
      Math.abs(elapsed - sinceEnergyMs) < 1,
      `出声后 ${sinceEnergyMs}ms 时 elapsed=${elapsed}：时间轴跳价`,
    );
    assert.ok(elapsed >= previous, `elapsed 从 ${previous} 掉到 ${elapsed}`);
    previous = elapsed;
  }

  // 反证：若按「计划到达时刻倒推 leadMs」，字幕会比声音早 leadMs 毫秒。
  const leadMs = 900;
  const rewoundAnchor = energySamples - Math.round((leadMs * rate) / 1000);
  const atEnergy = ((energySamples - rewoundAnchor) / rate) * 1000;
  assert.ok(
    Math.abs(atEnergy - leadMs) < 1,
    "倒推公式的偏差应为 leadMs：这正是「字幕领先声音」的量化来源，定锚不能再倒推",
  );
});

test("行为：elapsed 单调增长时句序号不减小", () => {
  let highest = -1;
  for (const elapsed of [0, 300, 1200, 2400, 3000, 4800, 6000]) {
    const index = pickSegmentIndex(PLAN, elapsed);
    assert.ok(index >= highest, `elapsed=${elapsed} 时句号从 ${highest} 掉到 ${index}`);
    highest = index;
  }
  assert.equal(highest, pickSegmentIndex(PLAN, 6000), "应正常推进到最后一句");
});

test("行为：reveal 字数在进度倒退时被钳住（Math.max 护栏的必要性）", () => {
  // 组件层用 Math.max 兜住；这里只证明「不钳就会掉」——掉 2 个字正是用户看到的现象
  const text = PLAN[2].text; // 「今天感觉怎么样？」
  const full = revealedCharCount(text, PLAN[2].duration_ms, PLAN[2].duration_ms);
  const rewound = revealedCharCount(text, 0, PLAN[2].duration_ms);
  assert.ok(full > rewound, "夹具前提不成立：整句字数应大于 0 毫秒时的字数");
  assert.equal(
    Math.max(full, rewound),
    full,
    "Math.max 钳制失效：进度倒退时字数被覆盖成更小的值（字幕往回缩）",
  );
});

test("源码契约：BigSubtitle 的 reveal 用 Math.max 只增不减", () => {
  const bigSubtitle = readFileSync(new URL("../src/pad/components/BigSubtitle.tsx", import.meta.url), "utf8");
  assert.ok(
    /Math\.max\(\s*shownRef\.current\.count \?\? 0,/.test(bigSubtitle),
    "BigSubtitle 缺少 `Math.max(shownRef.current.count ?? 0, …)` 护栏：任何进度倒退都会把已上屏的字擦掉",
  );
  assert.ok(
    /revealedCharCount\(\s*progress\.text,\s*progress\.elapsedMs,\s*progress\.durationMs,\s*progress\.charMs,?\s*\)/.test(
      bigSubtitle,
    ),
    "护栏必须包住真实的字数计算（且把逐字坐标一起传下去），不能另写一套口径",
  );
});