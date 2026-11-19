/**
 * 任务面板调度与求助上报的守卫用例。
 *
 * 背景：`MedicationCard` / `HealthCheckup` / `SosResult` 这三个面板此前是死代码
 * （实现完整、零入口），PRD FR-MED-08（到点提醒）、§4.6（19:00 健康采集）、
 * FR-ALT-06（常显求助按钮）因此全部验不过。本轮把它们挂到 `App.tsx`。
 *
 * 为什么这么测：
 * - 「什么时候该弹」是纯时间判断，抽进 `taskDispatch.ts` 后用固定时刻直接断言，
 *   比渲染组件稳定得多（Node `--test` 只擦类型，不认 TSX，仓库也不许引 jsdom）；
 * - 面板与按钮的接线用源码契约兜住三处容易悄悄回退的地方：
 *   ①求助按钮被挪回「只在断线时出现」②面板不读缓存而自己拼端点 ③健康采集不记「今天问过」。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const dispatch = await import("../src/pad/taskDispatch");
const local = (y: number, m: number, d: number, hh: number, mm: number): Date =>
  new Date(y, m - 1, d, hh, mm, 0, 0);

const reminder = (over: Record<string, unknown> = {}) => ({
  reminderId: "r-1",
  planId: "p-1",
  drugName: "二甲双胍缓释片",
  dose: "0.5g",
  usage: "口服，温水送服",
  scheduledAt: local(2026, 11, 11, 8, 0).toISOString(),
  windowSeconds: 60,
  attemptsSent: 1,
  ...over,
});

test("快照用「08:00」展示口径，且触达次数兜到 1", () => {
  const snap = dispatch.toSnapshot(reminder({ attemptsSent: 0 }));
  assert.equal(snap.scheduledAt, "08:00");
  assert.equal(snap.drugName, "二甲双胍缓释片");
  assert.equal(snap.attemptNo, 1);
  assert.equal(snap.windowSeconds, 60);
});

test("到点且窗口内 → 弹；早于到点不弹", () => {
  const rows = [reminder()];
  const before = dispatch.resolveDueMedication(rows, local(2026, 11, 11, 7, 59), new Set());
  assert.equal(before, null);
  const at8 = dispatch.resolveDueMedication(rows, local(2026, 11, 11, 8, 0), new Set());
  assert.equal(at8?.reminderId, "r-1");
});

test("窗口按「到点 + (触达次数-1)×5min + 本轮窗口」计算", () => {
  const rows = [reminder({ attemptsSent: 1 })];
  // 第 1 次触达：只有 60 秒
  assert.equal(dispatch.resolveDueMedication(rows, local(2026, 11, 11, 8, 1), new Set())?.reminderId, "r-1");
  assert.equal(dispatch.resolveDueMedication(rows, local(2026, 11, 11, 8, 2), new Set()), null);
  // 第 3 次触达：8:00 + 10min + 60s 内都还算「本次窗口」
  const late = [reminder({ attemptsSent: 3 })];
  assert.equal(dispatch.resolveDueMedication(late, local(2026, 11, 11, 8, 10), new Set())?.reminderId, "r-1");
  assert.equal(dispatch.resolveDueMedication(late, local(2026, 11, 11, 8, 12), new Set()), null);
});

test("已记过的不再弹；同时到点取最早那条", () => {
  const rows = [
    reminder({ reminderId: "r-late", scheduledAt: local(2026, 11, 11, 8, 0).toISOString() }),
    reminder({ reminderId: "r-early", scheduledAt: local(2026, 11, 11, 7, 30).toISOString(), attemptsSent: 8 }),
  ];
  const due = dispatch.resolveDueMedication(rows, local(2026, 11, 11, 8, 0), new Set());
  assert.equal(due?.reminderId, "r-early");
  const dismissed = new Set(["r-early"]);
  assert.equal(dispatch.resolveDueMedication(rows, local(2026, 11, 11, 8, 0), dismissed)?.reminderId, "r-late");
});

test("坏时间戳不参与判定（不抛、不误弹）", () => {
  const rows = [reminder({ scheduledAt: "not-a-date" })];
  assert.equal(dispatch.resolveDueMedication(rows, local(2026, 11, 11, 8, 0), new Set()), null);
  assert.equal(dispatch.earliestReminder(rows, new Set()), null);
});

test("健康采集：19 点窗口内当天只弹一次，跨日重新可弹", () => {
  const at1905 = local(2026, 11, 11, 19, 5);
  const key = dispatch.healthAskedKey(at1905);
  assert.equal(key, "anyu.health.asked.2026-11-11");
  assert.equal(dispatch.isHealthWindow(at1905), true);
  assert.equal(dispatch.isHealthWindow(local(2026, 11, 11, 20, 0)), false);
  assert.equal(dispatch.isHealthWindow(local(2026, 11, 11, 18, 59)), false);
  assert.equal(dispatch.shouldOpenHealth(at1905, null), true);
  // 日期在键里，值只是标记：closeHealth() 写的就是 "1"，必须被认成「今天问过了」
  assert.equal(dispatch.shouldOpenHealth(at1905, "1"), false);
  assert.equal(dispatch.shouldOpenHealth(at1905, ""), true);
  // 跨日作废靠换键，不靠比对值
  assert.notEqual(dispatch.healthAskedKey(local(2026, 11, 12, 19, 5)), key);
});

test("源码契约：健康采集「写标记」与「读标记」必须是同一套口径", () => {
  // 断言随结构更新（W5）：自动弹窗轮询已抽到 useTaskPolling.ts，
  // 「读标记」在那边、「写标记」仍在 App.tsx。契约是**两侧用同一个当日键**，
  // 所以合起来看，并额外钉住 hook 真的被 App 调用（不是抽出去就没人用）。
  const app = readApp();
  const polling = readSource("useTaskPolling.ts");
  const written = /localStorage\.setItem\(healthAskedKey\(new Date\(\)\),\s*"([^"]*)"\)/.exec(app);
  assert.ok(written, "退出健康采集必须落「今天已问过」标记");
  const read = /localStorage\.getItem\(healthAskedKey\(new Date\(\)\)\)/.exec(polling);
  assert.ok(read, "自动弹窗判定必须读同一个当日键");
  assert.ok(app.includes("useTaskPolling({"), "App 必须真的调用轮询 hook");
  // 反向守卫：写进去的值必须真的能让判定不再弹（2026-11-11 之前写 "1"、读却比键字符串，永不相等）
  assert.equal(
    dispatch.shouldOpenHealth(local(2026, 11, 11, 19, 5), written[1]),
    false,
    `closeHealth() 写的 "${written[1]}" 没被 shouldOpenHealth 认成已问过：19 点窗口内会每 15s 重弹`,
  );
});

test("源码契约：求助按钮常显，不随连接态消失", () => {
  const app = readApp();
  assert.ok(
    app.includes("<SosButton onClick={handleSos}"),
    "求助按钮必须常驻顶层，不能塞进 status === \"disconnected\" 分支",
  );
  // 反向守卫：旧写法（把求助/降级入口门在断线分支里）不得复现
  assert.equal(
    /status === "disconnected" && \(\s*<SosButton/.test(app),
    false,
    "求助入口不许再被断线分支门住（FR-ALT-06 要求常显）",
  );
});

test("源码契约：求助走 apiSos，面板里不拼端点", () => {
  const sos = readSource("apiSos.ts");
  assert.ok(sos.includes('fetch("/api/sos"'), "求助端点只允许出现在 apiSos.ts");
  assert.ok(sos.includes("Authorization"), "求助必须带设备令牌");
  const app = readApp();
  assert.equal(app.includes('"/api/sos"'), false, "App.tsx 不许自己拼求助端点");
});

test("源码契约：三块面板都读缓存、都从 taskDispatch 拿判定", () => {
  // 判定逻辑随轮询抽到 useTaskPolling.ts（W5），渲染仍在 App.tsx；合起来看。
  const app = readApp();
  const polling = readSource("useTaskPolling.ts");
  const logic = app + polling;
  assert.ok(logic.includes("resolveDueMedication("), "到点用药提醒必须用 taskDispatch 判定");
  assert.ok(logic.includes("shouldOpenHealth("), "健康采集必须用 taskDispatch 判定");
  assert.ok(app.includes("<MedicationCard"), "用药面板必须真的被渲染");
  assert.ok(app.includes("<HealthCheckup"), "健康采集面板必须真的被渲染");
  assert.ok(app.includes("<SosResult"), "求助结果面板必须真的被渲染");
  assert.ok(logic.includes("readReminders()"), "任务数据必须来自离线缓存");
  assert.ok(polling.includes('from "./taskDispatch"'), "判定必须来自 taskDispatch，不自造第二套");
});

test("源码契约：健康采集退出时记「今天问过」", () => {
  const app = readApp();
  assert.ok(
    /localStorage\.setItem\(healthAskedKey\(new Date\(\)\), "1"\)/.test(app),
    "退出健康采集必须落「今天已问过」，否则 19 点窗口内每 15s 重弹",
  );
});

function readApp(): string {
  return readFileSync(fileURLToPath(new URL("../src/pad/App.tsx", import.meta.url)), "utf8");
}

function readSource(name: string): string {
  return readFileSync(fileURLToPath(new URL(`../src/pad/${name}`, import.meta.url)), "utf8");
}

/** 2026-11-11 多端适配：字幕三档字号改由 `--pad-subtitle-*` 令牌给值（D 档像素不变）。 */
const CSS_SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/index.css", import.meta.url)),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

function tokenDefaultPx(name: string): number | null {
  const match = new RegExp(`--${name}:\\s*([0-9.]+)(rem|px);`).exec(CSS_SRC);
  if (!match) return null;
  const value = Number(match[1]);
  return match[2] === "rem" ? value * 16 : value;
}

test("源码契约：长回复降字号，三行不顶高数字人区", () => {
  const subtitle = readSource("components/BigSubtitle.tsx");
  // 三档字号必须都在（短句仍是最醒目的 60px；长回复降到 36px，否则 3 行 text-6xl 会把 footer
  // 顶到 330px 以上，CDP 实测字幕上沿比 3D 盒下沿还高 20px）。
  // 2026-11-11 多端适配：判定逻辑一字未改，字号类换成语义令牌，默认值仍与 4xl/5xl/6xl 等价。
  assert.ok(subtitle.includes("charCount > 48"), "长/中/短的三档判定被删了：长回复又回到大字");
  assert.ok(subtitle.includes("var(--pad-subtitle-sm)"), "长回复没走 --pad-subtitle-sm（D 档 36px = 原 text-4xl）");
  assert.ok(subtitle.includes("charCount > 20"), "中档判定被删了");
  assert.ok(subtitle.includes("var(--pad-subtitle-md)"), "中档没走 --pad-subtitle-md（D 档 48px = 原 text-5xl）");
  assert.ok(subtitle.includes("var(--pad-subtitle-lg)"), "短句没走 --pad-subtitle-lg（D 档 60px = 原 text-6xl）");
  assert.equal(tokenDefaultPx("pad-subtitle-sm"), 36, "长回复默认字号不是 36px，3 行会把数字人区顶掉");
  assert.equal(tokenDefaultPx("pad-subtitle-md"), 48, "中档默认字号不是 48px");
  assert.equal(tokenDefaultPx("pad-subtitle-lg"), 60, "短句默认字号不是 60px");
  assert.ok(subtitle.includes("const charCount = Array.from(text).length;"), "字数按码点算（emoji/生僻字不劈开）");
});

test("源码契约：轮询 hook 保住四条不变量（W5 抽出后逐条钉住）", () => {
  const h = readSource("useTaskPolling.ts");
  // ① 同一时刻只允许一件任务占面板
  assert.ok(
    /activeTaskRef\.current !== null\) return/.test(h),
    "已有任务时必须跳过，两件并排会把数字人挤掉",
  );
  // ② 已忽略的提醒不再弹
  assert.ok(h.includes("dismissedTaskRef.current"), "必须读已忽略集合");
  assert.ok(h.includes("dismissedTaskRef.current.add(due.reminderId)"), "弹出后要记进已忽略集合");
  // ③ IndexedDB 不可用不阻断语音主链路
  assert.ok(/catch\s*\{[\s\S]{0,120}不阻断语音主链路/.test(h), "缓存不可用必须静默跳过而不是抛");
  // ④ 卸载清理：定时器与监听都要摘
  assert.ok(h.includes("window.clearInterval(timer)"), "必须清定时器");
  assert.ok(
    h.includes("window.removeEventListener(REMINDERS_UPDATED_EVENT, tick)"),
    "必须摘事件监听，否则卸载后仍在跑",
  );
  // 只注册一次（靠 ref 读最新值），不得每次渲染重起定时器
  assert.ok(/\}, \[\]\);/.test(h), "effect 依赖必须为空数组");
  // 默认间隔与原实现一致
  assert.ok(/intervalMs = 15_000/.test(h), "默认 15s 不得改");
  // 场景机 dispatch 仍与 setActiveTask 成对
  assert.equal((h.match(/dispatch\(\{ type: "task\.open"/g) ?? []).length, 2);
  assert.equal((h.match(/setActiveTask\(/g) ?? []).length, 2);
});
