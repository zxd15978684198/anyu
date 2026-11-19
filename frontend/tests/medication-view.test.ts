/**
 * 药单真源收敛的用例（task-32）。
 *
 * 这一份是**真跑函数**的用例，不是源码契约：`medicationView` 是纯 TS（不碰 React、不碰
 * IndexedDB），Node 的类型擦除就能直接 import。之所以值得为它单开一份，是因为「今天还剩
 * 什么药」现在只有一个答案——摘要行与抽屉都读它。它一旦算错，两个界面会一起错，而且错法
 * 很隐蔽：摘要说「今天没有待提醒的药」、抽屉里却躺着两条已过期，长者只会觉得系统在糊弄人。
 *
 * 三态口径（窗口 = 到点 + (触达次数-1)×5min + 60s，PRD §4.5，实现在 `taskDispatch`）：
 * - `taken`：有确认信息（`confirmedAt` 或调用方给的 `taken` 集合）；
 * - `missed`：窗口已过且没确认；
 * - `pending`：其余（含还没到点、时刻解析不出）。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AGENDA_GRACE_MS,
  medicationAgenda,
  medicationStatus,
  type ReminderRow,
} from "../src/pad/medicationView.ts";

/** 2026-11-13 08:00 +08:00 的毫秒数：用例里所有时刻都以它为「到点」。 */
const DUE = Date.parse("2026-11-13T00:00:00+00:00");

/** 造一行药单；窗口默认 60s、只触达过一次（窗口末尾 = 到点 + 60s）。 */
function row(overrides: Partial<ReminderRow> = {}): ReminderRow {
  return {
    reminderId: "r-1",
    drugName: "苯磺酸氨氯地平片",
    dose: "5mg",
    scheduledAt: new Date(DUE).toISOString(),
    windowSeconds: 60,
    attemptsSent: 1,
    ...overrides,
  };
}

test("还没到点、窗口内都是待吃，过了窗口才算已过期", () => {
  const target = row();
  assert.equal(medicationStatus(target, DUE - 5 * 60_000), "pending", "提前看屏不该显示成过期");
  assert.equal(medicationStatus(target, DUE + 30_000), "pending", "窗口内仍是待吃");
  assert.equal(medicationStatus(target, DUE + 60_001), "missed", "窗口一过就是已过期");
});

test("窗口末尾随触达次数后移：第 3 枪的窗口到点 + 10min + 60s", () => {
  const target = row({ attemptsSent: 3 });
  assert.equal(
    medicationStatus(target, DUE + 10 * 60_000 + 30_000),
    "pending",
    "第 3 次触达还在等确认，不能判成过期",
  );
  assert.equal(
    medicationStatus(target, DUE + 10 * 60_000 + 60_001),
    "missed",
    "第 3 次触达的窗口末尾同样按 60s 收口",
  );
});

test("已吃只认确认信息：confirmedAt 或调用方给的 taken 集合", () => {
  assert.equal(
    medicationStatus(row({ confirmedAt: "2026-11-13T00:00:30+00:00" }), DUE + 5 * 60_000),
    "taken",
    "有确认时刻就是已吃，哪怕已经过了窗口",
  );
  assert.equal(
    medicationStatus(row(), DUE + 30_000, new Set(["r-1"])),
    "taken",
    "本次会话点过「已服」的也要判成已吃",
  );
  assert.equal(
    medicationStatus(row(), DUE + 30_000, new Set(["other"])),
    "pending",
    "别人的确认不能串到这条上",
  );
});

test("时刻解析不出时按待吃兜底，绝不崩", () => {
  assert.equal(medicationStatus(row({ scheduledAt: "not-a-date" }), DUE), "pending");
});

test("下一次 = 窗口未过的最早一条，已吃的跳过", () => {
  const rows = [
    row({ reminderId: "r-late", scheduledAt: new Date(DUE + 20 * 60_000).toISOString() }),
    row({ reminderId: "r-early" }),
  ];
  const agenda = medicationAgenda(rows, DUE);
  assert.equal(agenda.next?.reminderId, "r-early", "要按到点排序取最早，不能按缓存顺序");
  assert.equal(agenda.missedCount, 0);

  const takenFirst = medicationAgenda(
    [row({ reminderId: "r-early", confirmedAt: "2026-11-13T00:00:10+00:00" }), rows[0]],
    DUE,
  );
  assert.equal(takenFirst.next?.reminderId, "r-late", "吃过的那条不能再占「今日下一次」");
});

test("到点超过 60s 的待吃不再算下一次，但还没过期时不进 missedCount", () => {
  // 触达 3 次 → 窗口到点 +10min +60s；此刻已到点 2 分钟，超过宽限但仍未过期。
  const target = row({ attemptsSent: 3 });
  const now = DUE + 2 * 60_000;
  assert.ok(now - DUE > AGENDA_GRACE_MS, "用例前提：已超出「下一次」的宽容下界");
  const agenda = medicationAgenda([target], now);
  assert.equal(agenda.next, null, "超出宽限就不再是「下一次」");
  assert.equal(agenda.missedCount, 0, "窗口没过就不算漏确认");
});

test("漏确认只数已过期且没有确认信息的条数", () => {
  const rows = [
    row({ reminderId: "r-missed-1" }),
    row({ reminderId: "r-missed-2" }),
    row({ reminderId: "r-taken", confirmedAt: "2026-11-13T00:00:10+00:00" }),
    row({ reminderId: "r-next", scheduledAt: new Date(DUE + 60 * 60_000).toISOString() }),
  ];
  const agenda = medicationAgenda(rows, DUE + 5 * 60_000);
  assert.equal(agenda.missedCount, 2, "两条过窗未确认；已吃与未来那条都不算");
  assert.equal(agenda.next?.reminderId, "r-next");
});

test("空列表与非法 now 都退成「今天没有、也没漏」", () => {
  assert.deepEqual(medicationAgenda([], DUE), { next: null, missedCount: 0 });
  assert.deepEqual(medicationAgenda([row()], Number.NaN), { next: null, missedCount: 0 });
});

test("scheduledAt 解析不出的行不参与 next 与计数", () => {
  const agenda = medicationAgenda([row({ scheduledAt: "??" })], DUE);
  assert.equal(agenda.next, null);
  assert.equal(agenda.missedCount, 0, "解析不出的行不能被当成漏确认去吓人");
});
