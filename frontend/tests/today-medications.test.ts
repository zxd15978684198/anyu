/**
 * 「今日用药」面板门禁用例（迭代 7 验收第 3 条 / FR-MED-08 / TD:1111）。
 *
 * 验收原文是「拔掉网线后仍能看到当日药单并**标注『离线数据』**」。这句话有两个可错的
 * 方向，所以分两层钉：
 *   1. 纯逻辑层（`medicationView.ts`）：时刻怎么显示、最多铺几行、折叠条数；
 *   2. 源码契约层（`.tsx` / `App.tsx`）：面板真的读 IndexedDB、真的在断网时挂角标、
 *      真的被挂到平板上。`.tsx` 进不了 node:test（`node-ts-hook.mjs` 只认 `.ts`），
 *      所以这一层读源码断言——与 `wake-word.test.ts` 同一套做法。
 *
 * 删掉什么会变红（反向验收口径）：
 * 1. `OF​FLINE_BADGE` 改成「脱机数据」之类 → 文案用例红；
 * 2. 去掉 `!navigator.onLine` 初始值或 `offline` 事件监听 → 源码契约用例红（角标永不出现）；
 * 3. `visibleRows` 不排序 / 不 slice → 排序与折叠用例红；
 * 4. `App.tsx` 里删掉 `<TodayMedications />` → 挂载用例红。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  formatClock,
  MAX_ROWS,
  OFFLINE_BADGE,
  visibleRows,
} from "../src/pad/medicationView.ts";
import type { OfflineReminder } from "../src/pad/offline/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");

function readRepo(relative: string): string {
  return readFileSync(resolve(REPO, relative), "utf8");
}

/** 造一条最小可用的药单行；未给的字段用固定值，避免用例里出现魔法字面量。 */
function row(scheduledAt: string, drugName: string): OfflineReminder {
  return {
    reminderId: `${drugName}-${scheduledAt}`,
    planId: "plan-1",
    drugName,
    dose: "5mg × 1 片",
    usage: "口服",
    scheduledAt,
    windowSeconds: 900,
    attemptsSent: 0,
    snapshotAt: "2026-11-10T00:00:00+00:00",
  };
}

test("formatClock 把 UTC 时刻换成 24 小时制的本地墙钟", () => {
  // 用本地时间构造再序列化，避免用例依赖跑测机器的时区
  const local = new Date(2026, 10, 10, 8, 30, 0);
  assert.equal(formatClock(local.toISOString()), "08:30");
});

test("formatClock 对坏输入返回占位符，不返回 Invalid Date", () => {
  assert.equal(formatClock("not-a-time"), "--:--");
});

test("visibleRows 先按到点时刻升序再切前 N 条", () => {
  const { shown, rest } = visibleRows(
    [row("2026-11-10T04:00:00+00:00", "中午药"), row("2026-11-10T00:00:00+00:00", "早上药")],
    MAX_ROWS,
  );
  assert.deepEqual(
    shown.map((item) => item.drugName),
    ["早上药", "中午药"],
  );
  assert.equal(rest, 0);
});

test("visibleRows 超出上限时折叠成「还有 N 条」的计数", () => {
  const many = Array.from({ length: MAX_ROWS + 2 }, (_, index) =>
    row(`2026-11-10T0${index % 10}:00:00+00:00`, `药${index}`),
  );
  const { shown, rest } = visibleRows(many, MAX_ROWS);
  assert.equal(shown.length, MAX_ROWS);
  assert.equal(rest, 2);
});

test("角标文案就是验收原文「离线数据」", () => {
  assert.equal(OFFLINE_BADGE, "离线数据");
});

test("面板源码：读 IndexedDB、断网挂角标、监听 online/offline", () => {
  const src = readRepo("src/pad/components/TodayMedications.tsx");
  assert.ok(src.includes("readReminders"), "必须从 IndexedDB 缓存读，而不是再打一次网络");
  assert.ok(src.includes("navigator.onLine"), "首帧的离线判定必须来自 navigator.onLine");
  assert.ok(src.includes('"online"') && src.includes('"offline"'), "必须听两个网络事件");
  assert.ok(src.includes("OFFLINE_BADGE"), "角标文案必须来自单一出处，不许页面里再写死一份");
});

test("App.tsx 把今日用药面板挂进平板主区", () => {
  const src = readRepo("src/pad/App.tsx");
  // 批二（2026-11-13 场景化改造）：常驻整块面板 → 「右栏一行摘要 + 全量抽屉」。
  // 判据仍是「主区看得见今天的药」，只是换了形态；只删面板不留入口才会红。
  assert.ok(src.includes("MedicationSummary"), "App.tsx 必须 import 一行药单摘要");
  assert.ok(src.includes("<MedicationSummary"), "只 import 不渲染等于没接线");
  assert.ok(src.includes("<MedicationDrawer"), "全量药单抽屉必须挂载，否则摘要没有展开入口");
});
