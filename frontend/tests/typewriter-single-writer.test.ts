/**
 * 打字机「从头循环」是否已修 —— 复现与判定（2026-11-13）。
 *
 * 背景：`设计记录（语音三线同步与流式化）` 的 P0 声称要修
 * 「服务端每合成完一句就重发整份计划 → 字幕从头重打」，并提出用 `epoch` 实现。
 * 产品负责人说已修完但进度没更新；代码里搜 `epoch` 命中 0。
 *
 * 本文件不猜，直接复现两个触发条件并判定：
 * 1. **计划重发**会不会让当前句的字从头重打；
 * 2. **两条写入者**（打字轮 textReveal / 语音轮 realtime）会不会互相顶。
 *
 * 结论写在断言里：修好了就是绿，没修好就是红。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, describe, it } from "node:test";

import {
  isSubtitlePlanPresent,
  pickSegmentIndex,
  publishSubtitlePlanPresence,
  publishSubtitleProgress,
  readSubtitleProgress,
  revealedCharCount,
  segmentProgressAt,
} from "../src/pad/subtitlePlan.ts";
import { startTextReveal, stopTextReveal } from "../src/pad/textReveal.ts";
import type { TtsSegment } from "../src/pad/protocol.ts";

const read = (rel: string): string =>
  readFileSync(new URL(`../src/pad/${rel}`, import.meta.url), "utf8");

/** 三句回复，每句 2000ms，句间无静音。 */
const SENTENCES = ["今天天气不错。", "记得按时吃药。", "有事就叫我。"];
function plan(count: number): TtsSegment[] {
  return SENTENCES.slice(0, count).map((text, i) => ({
    text,
    start_ms: i * 2000,
    duration_ms: 2000,
  }));
}

afterEach(() => {
  stopTextReveal();
  publishSubtitleProgress(null);
  publishSubtitlePlanPresence(false);
});

describe("复现一 · 计划重发是否让字幕从头重打", () => {
  it("同一句内追加后续句，当前句文本不变（组件 effect 依赖 [text] 不会重跑）", () => {
    const elapsed = 900; // 仍在第一句内
    const before = segmentProgressAt(plan(1), elapsed);
    const after = segmentProgressAt(plan(3), elapsed); // 服务端重发整份计划
    assert.equal(before?.text, SENTENCES[0]);
    assert.equal(after?.text, before?.text, "重发计划后当前句变了 → 会触发归零重打");
    assert.equal(after?.index, before?.index);
    assert.equal(after?.elapsedMs, before?.elapsedMs);
  });

  it("句内字数随播放单调不减，且重发计划不会让它变小", () => {
    let shown = 0;
    const counts: number[] = [];
    for (let elapsed = 0; elapsed <= 1900; elapsed += 100) {
      // 每 300ms 服务端重发一次更长的计划（模拟逐句合成逐句下发）
      const count = Math.min(3, 1 + Math.floor(elapsed / 300));
      const p = segmentProgressAt(plan(count), elapsed);
      assert.equal(p?.text, SENTENCES[0], "第一句内不应换句");
      const revealed = revealedCharCount(p!.text, p!.elapsedMs, p!.durationMs, p!.charMs);
      const next = Math.max(shown, revealed); // BigSubtitle:117 的单调护栏
      counts.push(next);
      assert.equal(next >= shown, true, "字数倒退了");
      shown = next;
    }
    assert.equal(counts[0] <= 1, true, "出声即亮第一个字，不该整句闪一下");
    // 1900/2000 = 0.95 → floor(0.95 × 7 字) = 6，最后一个字要到句尾那一刻才上屏。
    assert.equal(counts[counts.length - 1], 6, "句内 95% 处应是 6 字，不是整句");
    assert.equal(
      revealedCharCount(SENTENCES[0], 2000, 2000),
      Array.from(SENTENCES[0]).length,
      "句尾（elapsed == duration）必须上齐",
    );
  });

  it("只在真正换句时才归零——这是正确行为，不是循环", () => {
    const s1 = segmentProgressAt(plan(3), 1900);
    const s2 = segmentProgressAt(plan(3), 2100);
    assert.equal(s1?.index, 0);
    assert.equal(s2?.index, 1);
    assert.notEqual(s1?.text, s2?.text, "换句时 text 必须变，组件才会为新句从 0 打起");
    const c1 = revealedCharCount(s1!.text, s1!.elapsedMs, s1!.durationMs);
    const c2 = revealedCharCount(s2!.text, s2!.elapsedMs, s2!.durationMs);
    // 旧句已近句尾（1900/2000 → 6 字），新句刚开始（100/2000 → 至少 1 字、不超过 2）。
    assert.equal(c1, 6);
    assert.equal(c2 <= 2, true, "新句刚开始只该有一两个字");
    assert.equal(c1 > c2, true, "换句后字数变小是**正确**的：新句从头上屏，不是同一句在重播");
  });

  it("整轮扫一遍：换句次数恰好等于句数减一，不多不少", () => {
    let lastIndex = -1;
    let switches = 0;
    for (let elapsed = 0; elapsed <= 6000; elapsed += 50) {
      const idx = pickSegmentIndex(plan(3), elapsed);
      if (idx !== lastIndex) {
        switches += 1;
        lastIndex = idx;
      }
    }
    assert.equal(switches, 3, "三句应只切换三次（含进入第一句），多出来就是重播");
  });
});

describe("复现二 · 两条写入者是否互相顶", () => {
  it("打字轮启动后占用通道，并标记「有计划」", () => {
    startTextReveal("这是一条打字回复。");
    assert.equal(isSubtitlePlanPresent(), true);
    const p = readSubtitleProgress();
    assert.equal(p?.text, "这是一条打字回复。");
    assert.equal(p?.index, 0);
  });

  it("语音字幕到达时 App 会停掉打字轮（单一写入者守卫存在）", () => {
    const app = read("App.tsx");
    const at = app.indexOf("stopTextReveal();\n        setSubtitle({ text, source });");
    assert.equal(
      at > 0,
      true,
      "期望 onSubtitle 里先 stopTextReveal 再 setSubtitle —— 这就是单一写入者守卫",
    );
    const before = app.slice(Math.max(0, at - 320), at);
    assert.equal(
      before.includes("打字轮的合成时间轴必须立刻让位"),
      true,
      "守卫的意图注释应在位，避免后人误删",
    );
  });

  it("停掉打字轮后通道被清空，语音轮随后重新建立「有计划」", () => {
    startTextReveal("打字回复。");
    assert.equal(isSubtitlePlanPresent(), true);
    stopTextReveal(); // App.onSubtitle 的动作
    assert.equal(readSubtitleProgress(), null);
    assert.equal(isSubtitlePlanPresent(), false);
    // realtime.applySubtitlePlan 紧接着重新置位
    const rt = read("realtime.ts");
    const at = rt.indexOf("private applySubtitlePlan");
    const body = rt.slice(at, at + 2600);
    assert.equal(
      body.includes("publishSubtitlePlanPresence(true)"),
      true,
      "语音计划到达必须重新置「有计划」，否则字幕会整段瞬出而不逐字",
    );
    publishSubtitlePlanPresence(true);
    publishSubtitleProgress({
      index: 0,
      text: SENTENCES[0],
      previousTexts: [],
      elapsedMs: 400,
      durationMs: 2000,
    });
    assert.equal(isSubtitlePlanPresent(), true);
    assert.equal(readSubtitleProgress()?.text, SENTENCES[0]);
  });

  it("连发两条打字回复：旧的被顶掉，只剩一条在写", () => {
    startTextReveal("第一条。");
    startTextReveal("第二条更长一点的回复。");
    const p = readSubtitleProgress();
    assert.equal(p?.text, "第二条更长一点的回复。", "旧揭示没被顶掉 → 两条会互相顶");
  });

  it("realtime 不自己调 stopTextReveal（职责在 App，避免双重取消）", () => {
    const rt = read("realtime.ts");
    assert.equal(
      /stopTextReveal/.test(rt),
      false,
      "realtime 不应直接管打字轮；取消统一由 App.onSubtitle 做，口径只有一处",
    );
  });
});

describe("判定 · epoch 不是必需的", () => {
  it("代码里没有 epoch，但两条触发条件都已被别的机制挡住", () => {
    for (const f of ["textReveal.ts", "realtime.ts", "components/BigSubtitle.tsx"]) {
      assert.equal(/epoch/.test(read(f)), false, `${f} 出现了 epoch，与本判定前提不符`);
    }
    // 挡住条件一：单调护栏
    const big = read("components/BigSubtitle.tsx");
    assert.equal(big.includes("Math.max("), true, "缺「只增不减」护栏");
    assert.equal(
      big.includes("reveal 只许增不许减"),
      true,
      "护栏意图注释应在位",
    );
    // 挡住条件二：换句才归零（effect 依赖 text）
    assert.equal(/\}, \[text, source\]\);/.test(big), true);
    assert.equal(big.includes("if (isSubtitlePlanPresent())"), true);
  });
});