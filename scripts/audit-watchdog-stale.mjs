/**
 * 播放卡死兜底链的**回归门禁**（P1-6 / 2026-11-11）。
 *
 * 跑法（仓库根）：
 *   node --import ./scripts/node-ts-hook.mjs scripts/audit-watchdog-stale.mjs
 *
 * 它真跑 `frontend/src/pad/realtime.ts` 的 `lipFrame`（不是读正则）：假 analyser 按帧喂
 * RMS、`performance.now()` 可控、`requestAnimationFrame` 手动驱动。
 *
 * ## 为什么需要这个门禁
 *
 * 2026-11-11 的 bug：远端轨只剩周期性短促噪声时，三条收口分支同时失效——
 * 「等音频 8s」要求 `lipAwaitingEnergy`、「静音 3s」要求静音**连续**累计、
 * 「12s watchdog」要求心跳 12s 没刷新；而每 300ms 一次、单帧 rms≥0.43 的尖峰恰好
 * 把 EMA 顶过说话门限（清静音）又刷新心跳（续 watchdog），于是胶囊永久停在「正在说」。
 * 修法两条（lead 2026-11-11）：
 *   ① `openSpeakingWindow()` 装 `planFallbackMs = REPLY_TEXT_FALLBACK_MS`，收口用
 *      `subtitlePlanEndMs > 0 ? subtitlePlanEndMs : planFallbackMs`；
 *   ② 心跳改成连续帧判据 `LIP_PROGRESS_STREAK_REQUIRED = 3`，孤立尖峰不再续命。
 * 本脚本的每个 regime 都对应其中一条；R2c / R7b 是「关掉兜底」的对照，用来证明 ② 真的在起作用。
 *
 * 时刻口径：除 R4 外均为**从开窗起算的绝对秒数**（含 0.5s 预填出声），所以兜底口径
 * 落在 ~8.5s = 0.5s 预填 + 8s 兜底 + 0.5s 宽限，而不是 8.0s。
 *
 * 退出码：0 = 全部落在期望窗口；1 = 有 regime 偏窗（说明修复没接上）。
 */

import { RealtimeClient } from "../frontend/src/pad/realtime.ts";

/** 可控时钟 + 手动 rAF：不真的等 30 秒。 */
let NOW = 0;
globalThis.performance = { now: () => NOW };
globalThis.requestAnimationFrame = (cb) => {
  cb.__pending = true;
  return 1;
};
globalThis.cancelAnimationFrame = () => {};

const FRAME_MS = 1000 / 60;
const FALLBACK_MS = 8000; // 与 realtime.ts 的 REPLY_TEXT_FALLBACK_MS 对齐（下面有源码自检）

function makeClient() {
  const events = [];
  const cb = {
    audioEl: { muted: true, play: async () => {}, addEventListener() {}, volume: 1 },
    onStatus: (s) => events.push(["status", s]),
    onSubtitle: () => {},
    onTrackEnergyReady: () => {},
    onPlaybackStarted: () => {},
    onTiming: () => {},
    onDiag: () => {},
    onIdentityAsk: () => {},
    onIdentityResult: () => {},
    onOutboundCall: () => {},
    onOutboundCallEnded: () => {},
    onIncomingCall: () => {},
    onIncomingCallEnded: () => {},
    onNeedManualRetry: () => {},
  };
  const client = new RealtimeClient(cb, {});
  client.lipBuf = new Uint8Array(1024);
  client.analyser = {
    getByteTimeDomainData: (buf) => buf.fill(128 + Math.round(client.__rms * 127)),
  };
  client.avatar = { setMouthOpen: () => {}, setSpeaking: () => {} };
  // 两个收口出口都要记：静音累计走 closeSpeakingWindow，watchdog/计划时长走 endPlayback。
  const origClose = client.closeSpeakingWindow.bind(client);
  client.closeSpeakingWindow = () => {
    if (!events.some((e) => e[0] === "close")) {
      events.push(["close", NOW, client.__exitCause ?? "silence"]);
    }
    return origClose();
  };
  const origEnd = client.endPlayback.bind(client);
  client.endPlayback = () => {
    events.push(["endPlayback", NOW, { silence: client.lipSilenceTime, progressAgo: NOW - client.lipLastProgressAt }]);
    return origEnd();
  };
  return { client, events };
}

/**
 * 等价于一次 `openSpeakingWindow()`：真实入口在 `:1296-1298` 把 `lipProgressStreak` 清零、
 * 把 `planFallbackMs` 装成 `REPLY_TEXT_FALLBACK_MS`。这里显式复刻，因为脚本直接驱动 `lipFrame`。
 */
function openWindow(client, { planFallbackMs = FALLBACK_MS, planEndMs = 0, prefillSeconds = 0.5 } = {}) {
  NOW = 0;
  client.__rms = 0.09;
  client.lipEnergyAvg = 0.09;
  client.lipLastProgressAt = NOW;
  client.lipProgressStreak = 0;
  client.lipLastAt = NOW;
  client.speakingWindow = true;
  client.lipAwaitingEnergy = false;
  client.subtitlePlanEndMs = planEndMs;
  client.planFallbackMs = planFallbackMs;
  // 先真的「出声」半秒：定字幕锚点 + 让心跳/连续帧计数进入稳态
  const frames = Math.round(prefillSeconds / (FRAME_MS / 1000));
  for (let i = 0; i < frames; i += 1) {
    NOW += FRAME_MS;
    client.stepLipSync();
  }
  return NOW;
}

function frame(client, rms) {
  NOW += FRAME_MS;
  client.__rms = rms;
  client.stepLipSync();
}

/** 跑到收口或超时；返回首次收口类型/时刻（秒，绝对）与心电图快照。 */
function runUntilClose(client, events, feed, maxSeconds) {
  const limit = Math.round((maxSeconds * 1000) / FRAME_MS);
  let frames = 0;
  while (client.speakingWindow && frames < limit) {
    feed(client, frames);
    frames += 1;
  }
  const close = events.find((e) => e[0] === "close");
  const end = events.find((e) => e[0] === "endPlayback");
  return {
    // endPlayback 表示走到了「计划时长 / 12s watchdog」这条主动收口出口；
    // 只有 closeSpeakingWindow 则是「静音连续 3s」那条。
    kind: close ? (end ? "plan/watchdog" : "silence") : null,
    closedAt: close ? +(close[1] / 1000).toFixed(2) : null,
    viaEndPlayback: Boolean(end),
    silence: end ? +(end[2].silence ?? 0).toFixed(2) : null,
    progressAgoMs: end ? Math.round(end[2].progressAgo) : null,
    stillOpen: client.speakingWindow,
  };
}

const rows = [];
function check(name, observed, [lo, hi], note = "") {
  const ok = observed.closedAt !== null && observed.closedAt >= lo && observed.closedAt <= hi;
  rows.push({ name, observed, range: `${lo}–${hi}s`, ok, note });
}

// 常量漂移自检：脚本的 FALLBACK_MS 必须与源码一致，否则整张期望表都在验错东西。
{
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../frontend/src/pad/realtime.ts", import.meta.url), "utf8");
  const m = /const REPLY_TEXT_FALLBACK_MS = (\d+);/.exec(src);
  const s = /const LIP_PROGRESS_STREAK_REQUIRED = (\d+);/.exec(src);
  if (!m || Number(m[1]) !== FALLBACK_MS) {
    console.error(`[FATAL] 脚本 FALLBACK_MS=${FALLBACK_MS} 与源码 ${m ? m[1] : "未找到"} 不一致`);
    process.exit(1);
  }
  if (!s || Number(s[1]) !== 3) {
    console.error(`[FATAL] LIP_PROGRESS_STREAK_REQUIRED 期望 3，源码是 ${s ? s[1] : "未找到"}`);
    process.exit(1);
  }
  console.log(`[自检] REPLY_TEXT_FALLBACK_MS=${FALLBACK_MS} / LIP_PROGRESS_STREAK_REQUIRED=3 与源码一致`);
}

// R1：说完就静音 —— 静音连续 3s 收口（0.5s 预填 + 3s）
{
  const { client, events } = makeClient();
  openWindow(client);
  const o = runUntilClose(client, events, (c, i) => frame(c, i === 0 ? 0.09 : 0), 10);
  check("R1 说完就静音（rms=0）", o, [3.3, 3.9], "静音连续 3s 收口（不依赖兜底）");
}

// R2b：周期尖峰 + 装兜底（lead 改法①）—— 原 bug 的最小复现
{
  const { client, events } = makeClient();
  openWindow(client);
  const o = runUntilClose(client, events, (c, i) => frame(c, i % 18 === 0 ? 0.5 : 0), 20);
  check("R2b 尖峰 300ms/0.5 + 兜底 8s", o, [8.2, 8.9], "计划时长兜底收口（bug 已关）");
}

// R2c：同样的尖峰，但**关掉兜底** —— 只剩 ②（连续帧心跳）能救
{
  const { client, events } = makeClient();
  openWindow(client, { planFallbackMs: 0 });
  const o = runUntilClose(client, events, (c, i) => frame(c, i % 18 === 0 ? 0.5 : 0), 20);
  check("R2c 尖峰 + 无兜底（只靠连续帧心跳）", o, [12.2, 12.9], "12s watchdog 收口（0.5s 预填 + 12s）");
}

// R3：真实分段计划（tts_segments 到了）—— 仍按真实计划收口
{
  const { client, events } = makeClient();
  openWindow(client, { planEndMs: 8000 });
  const o = runUntilClose(client, events, (c, i) => frame(c, i % 18 === 0 ? 0.5 : 0), 20);
  check("R3 真实计划 8s（tts_segments）", o, [8.2, 8.9], "planEndMs 优先于兜底");
}

// R4：rAF 暂停 15s 后恢复 —— 无计划出口时由 watchdog 在恢复首帧收口（相对恢复时刻）
{
  const { client, events } = makeClient();
  openWindow(client, { planFallbackMs: 0 });
  const before = NOW;
  NOW += 15_000;
  frame(client, 0);
  const end = events.find((e) => e[0] === "endPlayback");
  const o = {
    kind: "watchdog",
    closedAt: end ? +((end[1] - before) / 1000).toFixed(2) : null,
    viaEndPlayback: Boolean(end),
    silence: end ? +(end[2].silence ?? 0).toFixed(2) : null,
    progressAgoMs: end ? Math.round(end[2].progressAgo) : null,
    stillOpen: client.speakingWindow,
  };
  check("R4 rAF 暂停 15s 后恢复首帧", o, [14.9, 15.7], "silence=0 → 走的是 watchdog（时间跳变兜底）");
}

// R7：幅度扫描（装兜底）—— 0.43/0.50 也必须落在 ~8.5s，而不是 12s/40s
const ampRows = [];
for (const amp of [0.05, 0.1, 0.2, 0.3, 0.4, 0.43, 0.5]) {
  const { client, events } = makeClient();
  openWindow(client);
  const o = runUntilClose(client, events, (c, i) => frame(c, i % 18 === 0 ? amp : 0), 20);
  const expect = amp >= 0.43 ? [8.2, 8.9] : [3.0, 4.2];
  const ok = o.closedAt !== null && o.closedAt >= expect[0] && o.closedAt <= expect[1];
  ampRows.push({ amp, at: o.closedAt, kind: o.kind, expect: `${expect[0]}–${expect[1]}s`, ok });
  rows.push({ name: `R7 幅度 ${amp}`, observed: o, range: `${expect[0]}–${expect[1]}s`, ok, note: "装兜底后" });
}

// R7b：同样幅度、关掉兜底 —— ≥0.43 的应由 ② 在 ~12.5s 收口（证明连续帧判据生效）
for (const amp of [0.43, 0.5]) {
  const { client, events } = makeClient();
  openWindow(client, { planFallbackMs: 0 });
  const o = runUntilClose(client, events, (c, i) => frame(c, i % 18 === 0 ? amp : 0), 20);
  check(`R7b 幅度 ${amp} + 无兜底`, o, [12.2, 12.9], "连续帧心跳 → watchdog 12s 收口");
}

// ---- 报表 ----
console.log("\n| regime | 收口方式 | 实测收口（绝对秒） | 期望窗口 | 判定 |");
console.log("| --- | --- | --- | --- | --- |");
let bad = 0;
for (const r of rows) {
  if (!r.ok) bad += 1;
  const shown = r.observed.closedAt === null ? "未收口" : `${r.observed.closedAt}s`;
  const kind = r.observed.kind ?? (r.observed.stillOpen ? "—" : "close");
  console.log(`| ${r.name} | ${kind} | ${shown} | ${r.range} | ${r.ok ? "✅" : "❌"} ${r.note} |`);
}
console.log("\n幅度扫描明细（装兜底）：");
for (const r of ampRows) {
  console.log(
    `  rms=${r.amp.toFixed(2)} -> ${r.at === null ? "未收口" : `${r.at}s via ${r.kind}`}（期望 ${r.expect}）${r.ok ? "" : " ❌"}`,
  );
}
console.log(`\n结论：${bad === 0 ? "全绿（P1-6 两条修复均已接上）" : `${bad} 个 regime 偏窗 → 修复没接上，见上表`}`);
process.exitCode = bad === 0 ? 0 : 1;
