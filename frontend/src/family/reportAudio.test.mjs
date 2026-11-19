/**
 * 日报语音条闸门（FR-RPT-04）的用例 —— Node 内置 test runner，零新依赖。
 *
 * 跑法（仓库根）：`node --test frontend/src/family/reportAudio.test.mjs`
 *
 * 为什么不用 vitest：本工程没有前端测试设施（`frontend/package.json` 只有
 * dev / build / preview），为一个纯函数引入测试框架属于「擅自引入新依赖」
 * （AGENTS §4.12）。Node ≥22 自带 `node:test` 与原生 TS 类型擦除，够用。
 *
 * 反向验收（计划件 §4）：`audio_url` 为空时**不渲染播放器**。把页面改成无条件渲染
 * 播放器、或绕过 `audioUrlOf` 自己判空，下面那条接线用例就会红。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const { audioUrlOf } = await import(new URL("./reportAudio.ts", import.meta.url));

test("audio_url 缺失 / null / 空串 / 全空白 → 不放行播放器", () => {
  const empty = [
    null,
    undefined,
    {},
    { audio_url: null },
    { audio_url: "" },
    { audio_url: "   " },
  ];
  for (const item of empty) {
    assert.equal(audioUrlOf(item), null, `不该为 ${JSON.stringify(item)} 渲染播放器`);
  }
});

test("audio_url 是脏数据（非字符串）时同样不放行", () => {
  assert.equal(audioUrlOf({ audio_url: 42 }), null);
  assert.equal(audioUrlOf({ audio_url: false }), null);
  assert.equal(audioUrlOf({ audio_url: [] }), null);
});

test("接口地址原样放行，首尾空白被去掉", () => {
  const url = "/api/reports/6f1d8c1e/audio?date=2026-11-09";
  assert.equal(audioUrlOf({ audio_url: url }), url);
  assert.equal(audioUrlOf({ audio_url: `  ${url}\n` }), url);
});

test("详情页确实接这根闸门（回退成裸判空即红）", async () => {
  const page = await readFile(new URL("./pages/ReportDetailPage.tsx", import.meta.url), "utf8");
  assert.match(page, /from "\.\.\/reportAudio"/, "页面必须从 reportAudio 取闸门");
  assert.match(page, /audioUrlOf\(item\)/, "闸门必须是 audioUrlOf(item) 的结果");
  assert.doesNotMatch(page, /item\?\.audio_url/, "页面不许绕过闸门自己取 audio_url");
  assert.match(page, /\{audioUrl && \(/, "播放器必须挂在 audioUrl 为真时才渲染");
});
