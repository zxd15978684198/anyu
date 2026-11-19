/**
 * 任务面板调度（PRD §4.5 用药提醒、§4.6 健康采集、FR-ALT-06 一键求助）。
 *
 * 为什么单开一个纯函数文件：三条任务「什么时候该弹」都是时间/数据判断，
 * 直接写在 `App.tsx` 里就只能靠肉眼看页面来验；抽成纯函数后可以拿固定时刻断言
 * （见 `frontend/tests/task-dispatch.test.ts`），也避免把状态机塞进渲染路径。
 *
 * 口径（与 PRD 对齐，不许在这里改数值）：
 * - 用药：到点即开，窗口 = `attemptsSent` 的 5 分钟递进 + 本次 60s（PRD §4.5 T0/+5/+10/+15 每次等 60s）；
 * - 健康采集：本机时间 19:00–20:00 之间开一次，当天只开一次（PRD §4.6 19:00 起、20:00 强制结束）。
 */

import type { OfflineReminder } from "./offline/types";
import type { MedicationSnapshot } from "./tasks";
import { formatClock } from "./medicationView";

/** 一次到点的用药提醒（面板要的数据 + 用于「本次会话不再重复弹」的键）。 */
export interface DueMedication {
  reminderId: string;
  snapshot: MedicationSnapshot;
}

/** 缓存里的提醒 → 面板需要的快照（`scheduledAt` 从 ISO 转成「08:00」展示口径）。 */
export function toSnapshot(reminder: OfflineReminder): MedicationSnapshot {
  return {
    drugName: reminder.drugName,
    dose: reminder.dose,
    scheduledAt: formatClock(reminder.scheduledAt),
    usage: reminder.usage,
    attemptNo: Math.max(1, reminder.attemptsSent),
    windowSeconds: reminder.windowSeconds,
    // 快照生成时间：有服务端下发时刻就用它，否则退回「计划时间」（同一份药单快照，
    // 显示哪个时刻都不改变长者的判断，卡面文案见 MedicationCard）
    snapshotAt: formatClock(reminder.announcedAt ?? reminder.scheduledAt),
  };
}

/** 该提醒的「可弹窗」截止时刻：到点 + (触达次数-1)×5min + 本轮窗口。 */
export function medicationWindowEnd(reminder: OfflineReminder): number {
  const due = Date.parse(reminder.scheduledAt);
  if (Number.isNaN(due)) return Number.NaN;
  const extra = Math.max(0, reminder.attemptsSent - 1) * 5 * 60_000;
  return due + extra + reminder.windowSeconds * 1000;
}

/**
 * 当前该弹哪一条用药提醒。
 *
 * @param reminders 当日缓存
 * @param now 判定时刻
 * @param dismissed 本次会话已经弹过（或长者已处理）的 `reminderId`
 * @returns 最早到点且仍在窗口内的那条；没有就 `null`
 */
export function resolveDueMedication(
  reminders: readonly OfflineReminder[],
  now: Date,
  dismissed: ReadonlySet<string>,
): DueMedication | null {
  const ts = now.getTime();
  let best: DueMedication | null = null;
  let bestDue = Number.POSITIVE_INFINITY;
  for (const reminder of reminders) {
    if (dismissed.has(reminder.reminderId)) continue;
    const due = Date.parse(reminder.scheduledAt);
    const end = medicationWindowEnd(reminder);
    if (Number.isNaN(due) || Number.isNaN(end)) continue;
    if (ts < due || ts > end) continue;
    if (due < bestDue) {
      bestDue = due;
      best = { reminderId: reminder.reminderId, snapshot: toSnapshot(reminder) };
    }
  }
  return best;
}

/** 第一条（按到点时刻排序）提醒，供顶栏「用药提醒」手工入口演示用。 */
export function earliestReminder(
  reminders: readonly OfflineReminder[],
  dismissed: ReadonlySet<string>,
): DueMedication | null {
  let best: DueMedication | null = null;
  let bestDue = Number.POSITIVE_INFINITY;
  for (const reminder of reminders) {
    if (dismissed.has(reminder.reminderId)) continue;
    const due = Date.parse(reminder.scheduledAt);
    if (Number.isNaN(due)) continue;
    if (due < bestDue) {
      bestDue = due;
      best = { reminderId: reminder.reminderId, snapshot: toSnapshot(reminder) };
    }
  }
  return best;
}

/** 本机时间是否落在健康采集窗口（19:00–20:00）。 */
export function isHealthWindow(now: Date): boolean {
  const hour = now.getHours();
  return hour === 19;
}

/** 「今天是否已经问过健康采集」的本地键（按本地日期，跨日自动作废）。 */
export function healthAskedKey(now: Date): string {
  const y = now.getFullYear();
  const m = `${now.getMonth() + 1}`.padStart(2, "0");
  const d = `${now.getDate()}`.padStart(2, "0");
  return `anyu.health.asked.${y}-${m}-${d}`;
}

/**
 * 是否该自动弹出健康采集：在窗口内且今天没问过。
 *
 * 日期已经在**键**里（`healthAskedKey`），所以值只是个「问过了」的标记，写 `"1"` 就够。
 * 2026-11-11：原来这里拿值去和键字符串比，而 `closeHealth()` 写的是 `"1"`，两者永不相等——
 * 长者点了「结束采集」，15 秒后照样再弹一次，整个 19 点窗口都在反复打断。
 */
export function shouldOpenHealth(now: Date, askedMarker: string | null): boolean {
  if (!isHealthWindow(now)) return false;
  return askedMarker === null || askedMarker.trim() === "";
}
