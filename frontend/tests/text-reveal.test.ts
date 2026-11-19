/**
 * 打字框回复的逐字揭示（2026-11-11：产品负责人要求「聊天框关联的消息也要实现流式输出打字机效果」）。
 *
 * 测三件事：
 * 1. `textReveal.ts` 的估算与开关行为（真跑代码，用 setTimeout 顶替 rAF）；
 * 2. 进度通道的写入顺序（先 `planPresent=true` 再写 progress，否则字幕首帧会整句闪一下）；
 * 3. **源码契约**：`App.tsx` 必须先 `startTextReveal` 再 `setSubtitle`，且语音轮到达时要让位。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  estimateRevealDuration,
  startTextReveal,
  stopTextReveal,
  TEXT_REVEAL_MIN_MS,
  TEXT_REVEAL_MS_PER_CHAR,
} from "../src/pad/textReveal.ts";
import { isSubtitlePlanPresent, readSubtitleProgress } from "../src/pad/subtitlePlan.ts";

const read = (rel: string): string => readFileSync(new URL(`../src/pad/${rel}`, import.meta.url), "utf8");

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

test("估算时长：短句有下限、长句按字数、空串为 0", () => {
  assert.equal(estimateRevealDuration(""), 0);
  assert.equal(estimateRevealDuration("好的"), TEXT_REVEAL_MIN_MS, "短句必须给足最小可见时长");
  const long = "今天天气不错，要不要出去走走？";
  assert.equal(
    estimateRevealDuration(long),
    Array.from(long).length * TEXT_REVEAL_MS_PER_CHAR,
    "长句按字数估（中文按码点计，不按字节）",
  );
});

test("开始揭示立刻写入计划位与零点进度（字幕首帧才能从 0 字打）", () => {
  const handle = startTextReveal("我在呢，您说。");
  try {
    assert.equal(isSubtitlePlanPresent(), true, "startTextReveal 没打开「有计划」这一位");
    const progress = readSubtitleProgress();
    assert.ok(progress, "没有写进度，字幕会退回整段显示");
    assert.equal(progress.text, "我在呢，您说。");
    assert.equal(progress.elapsedMs, 0);
    assert.ok(progress.durationMs > 0);
  } finally {
    handle.cancel();
  }
  assert.equal(isSubtitlePlanPresent(), false, "取消后必须清掉计划位");
  assert.equal(readSubtitleProgress(), null);
});

test("揭示走完会自己收口：进度清空、计划位撤销、done() 变真", async () => {
  const text = "好";
  const handle = startTextReveal(text);
  assert.equal(handle.done(), false);
  await sleep(TEXT_REVEAL_MIN_MS + 220);
  assert.equal(handle.done(), true, "到时长后没有收口，字幕会停在半句");
  assert.equal(isSubtitlePlanPresent(), false);
  assert.equal(readSubtitleProgress(), null);
});

test("新的一句顶掉旧的一句（连发两条不许两条一起打）", () => {
  const first = startTextReveal("第一条比较长的回复内容");
  const second = startTextReveal("第二条");
  try {
    assert.equal(readSubtitleProgress()?.text, "第二条");
    assert.equal(first.done(), false, "被顶掉的旧句不该被标记为完成");
  } finally {
    second.cancel();
    stopTextReveal();
  }
});

test("源码契约：App.tsx 先启动揭示再 setSubtitle，且语音轮到达时让位", () => {
  const app = read("App.tsx");
  const startAt = app.indexOf("startTextReveal(outcome.reply)");
  const setAt = app.indexOf('setSubtitle({ text: outcome.reply');
  assert.ok(startAt > 0, "App.tsx 的打字回复没有走 startTextReveal（打字轮的流式效果丢了）");
  assert.ok(setAt > 0, "App.tsx 没有把回复写进字幕");
  assert.ok(startAt < setAt, "必须先启动揭示再 setSubtitle：顺序反了字幕会先整句闪一下");
  assert.ok(
    app.includes("stopTextReveal();\n        setSubtitle({ text, source });"),
    "语音轮到达时没有让位：合成时间轴会和真实音频进度互相顶",
  );
});

test("源码契约：notice（安全告警话术）也允许逐字，ASR 稿仍然整段", () => {
  const subtitle = read("components/BigSubtitle.tsx");
  assert.ok(
    subtitle.includes('const typewriterSource = source === "reply" || source === "notice";'),
    "BigSubtitle 只对 reply 逐字：打字轮命中安全规则时那句会整段蹦出",
  );
});

test("源码契约：兜底已上屏后，迟到的音频只补声音，不许重置字幕", () => {
  // 2026-11-17 实测：/text 58ms 返回、/tts 3924ms；2s 兜底先整段上屏，
  // 音频到达后若照常 applySubtitlePlan，会把字幕句序重置到第 0 句，
  // 长者看到同一句从第一个字重打一遍（第一遍无音频、不动嘴）。
  const realtime = read("realtime.ts");
  assert.match(
    realtime,
    /if \(segments\.length > 0 && !options\.textAlreadyShown\)/,
    "playReplyAudio 丢了「文本已上屏」判断：迟到音频会重播字幕",
  );
  assert.match(
    realtime,
    /options: \{ textAlreadyShown\?: boolean \} = \{\}/,
    "playReplyAudio 没有 textAlreadyShown 选项，App 无从告知「字已经上屏」",
  );
  assert.match(
    realtime,
    /this\.lastSpeech = \{ url, durationMs, segments \};/,
    "跳过字幕计划时仍要把 segments 存进 lastSpeech，否则「再说一遍」会丢逐句时间轴",
  );

  const app = read("App.tsx");
  assert.match(
    app,
    /playReplyAudio\([\s\S]{0,500}?textAlreadyShown:\s*textShown/,
    "App 的 /tts 回调没有把「兜底是否已上屏」交给 playReplyAudio",
  );
});
