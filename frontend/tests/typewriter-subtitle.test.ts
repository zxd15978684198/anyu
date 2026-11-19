/**
 * 大字幕逐字揭示（task-68 · FR-UI-02）的可打红用例。
 *
 * ## 为什么把「字数」抽成纯函数测
 *
 * 逐字揭示真正会错的地方不是 CSS，而是**取值**：比例怎么算、时长缺失怎么办、中文按什么切。
 * 这几条只有纯函数能穷举；组件里的 rAF 只负责「每帧读一次进度、字数变了才重渲染」。
 *
 * ## 为什么还要源码契约用例
 *
 * `BigSubtitle.tsx` 是 `.tsx`，node --test 的类型擦除不处理 JSX，没法直接 import 渲染。
 * 但这次改动有三条「看不见的硬约束」必须钉住，只能读源码断言：
 *   1. 卸载/换句必须 `cancelAnimationFrame`，且**不许**用 `setInterval`（定时器泄漏）；
 *   2. 当前句**不截断**（旧的 line-clamp 会把正在说的后半句裁掉），改为在定高字幕带里
 *      滚动 + 贴底，上文另起 2 行；
 *   3. `App.tsx` 的调用形态不变（`<BigSubtitle text={subtitle.text} source={...} />`）——
 *      进度走模块级通道，不靠 props 往下传，否则就得改 App（这次不在作用域）。
 *
 * 复跑：
 *   node scripts/run-frontend-tests.mjs
 *   node --import ./scripts/node-ts-hook.mjs --test frontend/tests/typewriter-subtitle.test.ts
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  pickSegmentIndex,
  publishSubtitleProgress,
  readSubtitleProgress,
  revealedCharCount,
  segmentProgressAt,
  type SubtitleProgress,
} from "../src/pad/subtitlePlan";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");

const BIG_SUBTITLE = read("../src/pad/components/BigSubtitle.tsx");
const REALTIME = read("../src/pad/realtime.ts");
const APP = read("../src/pad/App.tsx");

/* ------------------------------------------------------------------ *
 * 1. 揭示字符数：比例、边界、中文切分
 * ------------------------------------------------------------------ */

test("空串不揭示：0 字也不抛", () => {
  assert.equal(revealedCharCount("", 0, 1000), 0);
  assert.equal(revealedCharCount("", 500, 1000), 0);
});

test("还没出声（elapsedMs=0）不上屏任何字", () => {
  assert.equal(revealedCharCount("小伴你好", 0, 1000), 0);
});

test("中段按比例揭示：一半时长一半字", () => {
  assert.equal(revealedCharCount("小伴你好", 500, 1000), 2);
  assert.equal(revealedCharCount("小伴你好", 250, 1000), 1);
  assert.equal(revealedCharCount("小伴你好", 750, 1000), 3);
});

test("超过句时长：整句揭示，不越界、不返 0", () => {
  assert.equal(revealedCharCount("小伴你好", 1000, 1000), 4);
  assert.equal(revealedCharCount("小伴你好", 99999, 1000), 4);
});

test("没有可信时长：整句显示（宁可不上动效，也不能停在全空白）", () => {
  assert.equal(revealedCharCount("小伴你好", 100, 0), 4);
  assert.equal(revealedCharCount("小伴你好", 100, -1), 4);
  assert.equal(revealedCharCount("小伴你好", 100, Number.NaN), 4);
  assert.equal(revealedCharCount("小伴你好", Number.NaN, 1000), 0);
});

test("刚出声就亮第一个字：不出现「已经在说、字幕还是空」的几十毫秒", () => {
  assert.equal(revealedCharCount("小伴你好", 1, 10000), 1);
});

test("中文按字符切，不按字节：4 个汉字就是 4 个信息单位", () => {
  const text = "小伴你好";
  assert.equal(Array.from(text).length, 4);
  assert.equal(Buffer.byteLength(text, "utf8"), 12); // 字节层面是 12，按字节切会切碎汉字
  assert.equal(revealedCharCount(text, 1000, 1000), 4);
  assert.equal(revealedCharCount(text, 500, 1000), 2);
});

test("代理对按码点计一次：emoji 不会被切成半个乱码", () => {
  const text = "😀好";
  assert.equal(text.length, 3); // UTF-16 单元是 3
  assert.equal(Array.from(text).length, 2); // 码点才是 2
  assert.equal(revealedCharCount(text, 1000, 1000), 2);
});

test("揭示字数随播放时间单调不减（不许出现「越说越少」）", () => {
  const text = "这是一句用来检查单调性的台词";
  let previous = 0;
  for (let elapsed = 0; elapsed <= 2000; elapsed += 40) {
    const count = revealedCharCount(text, elapsed, 2000);
    assert.ok(count >= previous, `elapsed=${elapsed} 时字数回退：${count} < ${previous}`);
    previous = count;
  }
  assert.equal(previous, Array.from(text).length);
});

/* ------------------------------------------------------------------ *
 * 1b. 句内逐字坐标（char_ms）：停顿不吃字、尾音不抢跑
 *
 * 坐标由服务端按这一句 PCM 的能量分布算出（`app/audio/tts_sentences.py::char_offsets_ms`），
 * 语义是每个字的**结束偏移**。下面这条夹具是「记得吃药」的真实形态：
 * 前 400ms 念「记得」→ 中间 800ms 停顿 → 后 400ms 念「吃药」。
 * ------------------------------------------------------------------ */

const PAUSE_TEXT = "记得吃药";
const PAUSE_CHAR_MS = [200, 400, 1400, 1600];
const PAUSE_DURATION = 1600;

test("有坐标就按坐标走：句中停顿不推进字数", () => {
  assert.equal(revealedCharCount(PAUSE_TEXT, 600, PAUSE_DURATION, PAUSE_CHAR_MS), 2);
  assert.equal(revealedCharCount(PAUSE_TEXT, 1200, PAUSE_DURATION, PAUSE_CHAR_MS), 2);
  // 比例法在同一时刻给 1 和 3：一个落后、一个抢跑，正是「字和声音各说各的」的来源
  assert.equal(revealedCharCount(PAUSE_TEXT, 600, PAUSE_DURATION), 1);
  assert.equal(revealedCharCount(PAUSE_TEXT, 1200, PAUSE_DURATION), 3);
});

test("坐标是「结束偏移」：这个字念完那一刻才上屏", () => {
  assert.equal(revealedCharCount(PAUSE_TEXT, 399, PAUSE_DURATION, PAUSE_CHAR_MS), 1);
  assert.equal(revealedCharCount(PAUSE_TEXT, 400, PAUSE_DURATION, PAUSE_CHAR_MS), 2);
  assert.equal(revealedCharCount(PAUSE_TEXT, 1399, PAUSE_DURATION, PAUSE_CHAR_MS), 2);
  assert.equal(revealedCharCount(PAUSE_TEXT, 1400, PAUSE_DURATION, PAUSE_CHAR_MS), 3);
  assert.equal(revealedCharCount(PAUSE_TEXT, 1600, PAUSE_DURATION, PAUSE_CHAR_MS), 4);
});

test("坐标缺失或长度不符：当作没有坐标，回落比例（不许把字幕钉死）", () => {
  assert.equal(revealedCharCount(PAUSE_TEXT, 800, PAUSE_DURATION, undefined), 2);
  assert.equal(revealedCharCount(PAUSE_TEXT, 800, PAUSE_DURATION, []), 2);
  assert.equal(revealedCharCount(PAUSE_TEXT, 800, PAUSE_DURATION, [200, 400]), 2);
});

test("坐标法同样单调不减，末字在句尾上齐", () => {
  let previous = 0;
  for (let elapsed = 0; elapsed <= PAUSE_DURATION + 200; elapsed += 20) {
    const count = revealedCharCount(PAUSE_TEXT, elapsed, PAUSE_DURATION, PAUSE_CHAR_MS);
    assert.ok(count >= previous, `elapsed=${elapsed} 时字数回退：${count} < ${previous}`);
    previous = count;
  }
  assert.equal(previous, Array.from(PAUSE_TEXT).length);
});

test("时长缺失但有坐标：仍按坐标逐字（坐标是比时长更强的依据）", () => {
  assert.equal(revealedCharCount(PAUSE_TEXT, 600, 0, PAUSE_CHAR_MS), 2);
  assert.equal(revealedCharCount(PAUSE_TEXT, 0, 0, PAUSE_CHAR_MS), 0);
});

test("坐标长度按码点校验：emoji 不会被误判成「长度不符」而回落比例", () => {
  const text = "😀好啊";
  assert.equal(text.length, 4); // UTF-16 单元
  assert.equal(Array.from(text).length, 3); // 码点
  assert.equal(revealedCharCount(text, 500, 900, [100, 200, 900]), 2);
  assert.equal(revealedCharCount(text, 500, 900), 1); // 按 length 校验就会错落到这一档
});

/* ------------------------------------------------------------------ *
 * 2. 句内进度：夹取、上文、空计划
 * ------------------------------------------------------------------ */

const PLAN = [
  { text: "第一句", start_ms: 0, duration_ms: 1000 },
  { text: "第二句", start_ms: 1000, duration_ms: 2000 },
  { text: "第三句", start_ms: 3000, duration_ms: 500 },
];

test("空计划：没有进度（组件据此回落到整段渲染）", () => {
  assert.equal(segmentProgressAt([], 100), null);
});

test("第一句内：进度从 0 起、没有上文", () => {
  const first = segmentProgressAt(PLAN, 0);
  assert.ok(first);
  assert.equal(first.index, 0);
  assert.equal(first.text, "第一句");
  assert.deepEqual(first.previousTexts, []);
  assert.equal(first.elapsedMs, 0);
  assert.equal(first.durationMs, 1000);

  const midway = segmentProgressAt(PLAN, 400);
  assert.ok(midway);
  assert.equal(midway.index, 0);
  assert.equal(midway.elapsedMs, 400);
});

test("换句：index 走路、上文正好是上一句、进度自动减去起点", () => {
  const second = segmentProgressAt(PLAN, 1500);
  assert.ok(second);
  assert.equal(second.index, 1);
  assert.equal(second.text, "第二句");
  assert.deepEqual(second.previousTexts, ["第一句"]);
  assert.equal(second.elapsedMs, 500); // 1500 - start_ms(1000)
  assert.equal(second.durationMs, 2000);
});

test("上文最多两句、旧的在前：长者能回看刚才两句，不会被整段回复灌满", () => {
  const third = segmentProgressAt(PLAN, 3200);
  assert.ok(third);
  assert.deepEqual(third.previousTexts, ["第一句", "第二句"]);

  const long = Array.from({ length: 6 }, (_, i) => ({
    text: `第${i + 1}句`,
    start_ms: i * 1000,
    duration_ms: 1000,
  }));
  const last = segmentProgressAt(long, 5500);
  assert.ok(last);
  assert.equal(last.index, 5);
  assert.deepEqual(last.previousTexts, ["第4句", "第5句"]);
});

test("句内进度被夹在 [0, durationMs]：不越过本句时长，也不为负", () => {
  // 恰好落在第一句之后、第二句起点上当帧：第二句已播 0
  const atBoundary = segmentProgressAt(PLAN, 1000);
  assert.ok(atBoundary);
  assert.equal(atBoundary.index, 1);
  assert.equal(atBoundary.elapsedMs, 0);

  // 播过最后一句的结尾：仍停在最后一句，且进度封顶在它的时长
  const overrun = segmentProgressAt(PLAN, 99999);
  assert.ok(overrun);
  assert.equal(overrun.index, 2);
  assert.equal(overrun.text, "第三句");
  assert.equal(overrun.elapsedMs, 500);
});

test("没有时长的句：durationMs 归 0，不夹取（交给纯函数整句显示）", () => {
  const broken = [{ text: "没有时长", start_ms: 0, duration_ms: 0 }];
  const progress = segmentProgressAt(broken, 700);
  assert.ok(progress);
  assert.equal(progress.durationMs, 0);
  assert.equal(revealedCharCount(progress.text, progress.elapsedMs, progress.durationMs), 4);
});

test("进度与 pickSegmentIndex 同源：不会出现「显示第二句、进度算成第一句」", () => {
  for (const elapsed of [0, 999, 1000, 2500, 3000, 4000]) {
    const progress = segmentProgressAt(PLAN, elapsed);
    assert.ok(progress);
    assert.equal(progress.index, pickSegmentIndex(PLAN, elapsed));
    assert.equal(progress.text, PLAN[progress.index].text);
  }
});

test("句内进度透传 char_ms：组件拿到进度就能逐字，不必回查计划", () => {
  const plan = [
    {
      text: PAUSE_TEXT,
      start_ms: 1000,
      duration_ms: PAUSE_DURATION,
      char_ms: PAUSE_CHAR_MS,
    },
  ];
  const progress = segmentProgressAt(plan, 1600);
  assert.ok(progress);
  assert.equal(progress.elapsedMs, 600); // 句内已播 600ms，正落在停顿里
  assert.deepEqual(progress.charMs, PAUSE_CHAR_MS);
  assert.equal(
    revealedCharCount(progress.text, progress.elapsedMs, progress.durationMs, progress.charMs),
    2,
  );
});

test("旧服务端没有 char_ms：进度里是 undefined，揭示自动回落比例", () => {
  const progress = segmentProgressAt(PLAN, 500);
  assert.ok(progress);
  assert.equal(progress.charMs, undefined);
});

/* ------------------------------------------------------------------ *
 * 3. 进度通道：realtime 每帧写、组件每帧读
 * ------------------------------------------------------------------ */

test("进度通道可写可读，null 表示「当前没有在逐句播放」", () => {
  const sample: SubtitleProgress = {
    index: 0,
    text: "小伴在呢",
    previousTexts: [],
    elapsedMs: 300,
    durationMs: 900,
  };
  publishSubtitleProgress(sample);
  assert.deepEqual(readSubtitleProgress(), sample);

  publishSubtitleProgress(null);
  assert.equal(readSubtitleProgress(), null);
});

/* ------------------------------------------------------------------ *
 * 4. 源码契约（.tsx 不能 import，只能读源码钉约束）
 * ------------------------------------------------------------------ */

test("字幕组件：rAF 驱动 + 卸载取消，不许 setInterval", () => {
  assert.match(BIG_SUBTITLE, /requestAnimationFrame\(tick\)/);
  assert.match(BIG_SUBTITLE, /cancelAnimationFrame\(raf\)/);
  assert.ok(
    !/setInterval\s*\(/.test(BIG_SUBTITLE),
    "字幕不许用 setInterval（定时器会在换句/卸载后泄漏）",
  );
});

test("字幕组件：reply 与 notice 逐字，其余来源整段显示", () => {
  // 2026-11-11：打字轮命中安全规则时回复走 notice（告警话术），那句也要逐字，
  // 所以判定从「只有 reply」改成「reply || notice」；ASR 稿仍是整段。
  assert.match(BIG_SUBTITLE, /const typewriterSource = source === "reply" \|\| source === "notice";/);
  assert.match(BIG_SUBTITLE, /readSubtitleProgress\(\)/);
  assert.match(BIG_SUBTITLE, /revealedCharCount\(/);
});

test("字幕组件：当前句不截断、在定高区里滚动并贴底（长者不会读到半句话）", () => {
  const current = BIG_SUBTITLE.slice(BIG_SUBTITLE.indexOf('data-testid="subtitle-scroll"'));
  assert.match(current, /overflow-y-auto/, "当前句区没有滚动：长回复的后半句会溢出到版面外");
  assert.ok(
    !/line-clamp-\d/.test(current),
    "当前句又被 line-clamp 截断：正在说的后半句会被裁掉，长者永远读到半句话",
  );
  assert.match(BIG_SUBTITLE, /el\.scrollTop = el\.scrollHeight/, "没有自动贴底：字会在可视区外继续打");
  assert.match(BIG_SUBTITLE, /useLayoutEffect/, "贴底不在 layout effect 里：会先画一帧再跳");
});

test("字幕组件：上文单独一块、每句最多 2 行，不跟当前句抢高度", () => {
  const history = BIG_SUBTITLE.slice(
    BIG_SUBTITLE.indexOf('data-testid="subtitle-history"'),
    BIG_SUBTITLE.indexOf('data-testid="subtitle-scroll"'),
  );
  assert.match(history, /line-clamp-2/, "上文没封顶 2 行：两句长上文会把当前句挤没");
  assert.match(history, /shrink-0/, "上文块会被压缩：字号会被挤成小字");
  assert.match(history, /--pad-subtitle-prev/, "上文字号没走令牌：多端适配时改不动");
  assert.match(history, /reveal\.previous\.map/, "上文没按句渲染：多句历史会被拼成一坨");
});

test("realtime：进度在「句序号去重 return」之前发布（否则句内永远没有新进度）", () => {
  const body = REALTIME.slice(REALTIME.indexOf("private advanceSubtitle"));
  const publishAt = body.indexOf("publishSubtitleProgress(progress)");
  const dedupeAt = body.indexOf("progress.index === this.subtitleIndex");
  assert.ok(publishAt >= 0, "advanceSubtitle 里没有发布进度");
  assert.ok(dedupeAt > publishAt, "进度必须比去重分支更早发布，否则同句内进度不更新");
});

test("realtime：重置/空计划时清空进度（待机不再打字）", () => {
  const reset = REALTIME.slice(REALTIME.indexOf("private resetSubtitlePlan"));
  assert.ok(
    reset.slice(0, reset.indexOf("\n  }")).includes("publishSubtitleProgress(null)"),
    "resetSubtitlePlan 必须清空进度通道",
  );
  assert.ok(
    /if \(!segments\.length\) \{\s*this\.subtitlePlan = null;\s*(?:this\.subtitlePlanEndMs = 0;\s*)?publishSubtitleProgress\(null\);/.test(REALTIME),
    "空计划必须清空进度通道",
  );
});

test("App 的调用形态不变：进度不靠 props 下传（否则要改 App，不在本卡作用域）", () => {
  // 只钉「仍然用 text/source 两个 props 渲染」，不钉空白与属性顺序——
  // App.tsx 有并行 writer，过严的正则会让别人的无关改动把我的门禁打红。
  assert.ok(APP.includes("<BigSubtitle"), "App 不再渲染大字幕组件");
  assert.ok(APP.includes("text={subtitle.text}"), "App 传的正文 prop 变了");
  assert.ok(APP.includes("source={subtitle.source}"), "App 传的来源 prop 变了");
});
