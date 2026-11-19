/**
 * 今日用药面板的纯视图逻辑（PRD:338 FR-MED-08 / TD:1111）。
 *
 * 单独放在 `.ts` 里而不是内联进 `.tsx`：`scripts/node-ts-hook.mjs` 只认 `.ts`，
 * `.tsx` 进不了 node:test；把「时刻怎么显示」「最多铺几行」这些能被写错的判断挪出来，
 * 才能在门禁里用真函数断言，而不是拿正则去猜渲染结果。
 */

import type { OfflineReminder } from "./offline/types";
import { medicationWindowEnd } from "./taskDispatch";

/** 角标文案：验收原文要求 `离线数据`，不做同义替换（要能被肉眼与用例同时搜到）。 */
export const OFFLINE_BADGE = "离线数据";

/**
 * 缓存刷新完成后的通知事件（`initOfflineCache().refresh()` 之后由 pad 根组件派发）。
 *
 * 面板只读 IndexedDB，它自己不知道「刚有人重新拉过一遍药单」；没有这个事件，
 * 登录后当场看屏要等下一次 60s 轮询才出现药单，演示时会被当成「今天没有药」。
 */
export const REMINDERS_UPDATED_EVENT = "sm:reminders-updated";

/** 面板一次最多铺几行：1920×1080 下超过 6 行就会压到数字人。 */
export const MAX_ROWS = 6;

/**
 * `2026-11-10T00:30:00+00:00` → `08:30`。
 *
 * 长者只看本地墙钟时刻，所以显式固定 24 小时制（`hour12: false`）：
 * 默认 en-US 会出 `8:30 AM`，`zh-CN` 若不指定也可能带 AM/PM 前缀。
 * 解析失败返回 `--:--`，绝不返回 `Invalid Date` 这种会被当成正文念出来的串。
 */
export function formatClock(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "--:--";
  return parsed.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/**
 * 取要铺的行 + 折叠条数。
 *
 * 先按到点时刻升序**再**切前 N 条：`readReminders` 的排序保证只对整表成立，
 * 面板自己再排一次，才不会因为上层换了数据源顺序就把 08:00 的药排到 12:00 后面。
 */
export function visibleRows(
  reminders: readonly OfflineReminder[],
  max: number = MAX_ROWS,
): { shown: OfflineReminder[]; rest: number } {
  const sorted = [...reminders].sort(
    (a, b) => Date.parse(a.scheduledAt) - Date.parse(b.scheduledAt),
  );
  const shown = sorted.slice(0, Math.max(0, max));
  return { shown, rest: sorted.length - shown.length };
}

/* ---------------------------------------------------------------------------
 * 药单真源收敛（task-32，2026-11-13）：状态判定与「今天还剩什么」只有这一份
 * ------------------------------------------------------------------------ */

/**
 * 参与药单判定的最小行形状。
 *
 * `OfflineReminder` 是它的超集，缓存行可以直接传进来；服务端若有确认时刻，
 * 补一个 `confirmedAt` 就能让该行判成「已吃」，不必改本文件。
 */
export interface ReminderRow {
  reminderId: string;
  drugName: string;
  dose: string;
  /** 计划到点时刻（ISO8601）；非法值按「待吃」处理，绝不崩 */
  scheduledAt: string;
  /** 本次触达的确认窗口秒数（PRD §4.5：每次触达等 60s） */
  windowSeconds: number;
  /** 已触达次数（服务端告知；决定窗口末尾，见 `taskDispatch.medicationWindowEnd`） */
  attemptsSent: number;
  /** 已确认服药的时刻（ISO）。缓存里通常没有——「已吃」不能从缓存推，见下方说明。 */
  confirmedAt?: string | null;
}

/** 一条药单此刻的三态。`taken` 只由确认信息推出，绝不拿「播报过」冒充。 */
export type MedicationStatus = "taken" | "pending" | "missed";

/**
 * `medicationAgenda` 的「到点后仍算下一次」宽容窗口（60s）。
 *
 * 与 PRD §4.5「每次触达等 60s」同口径：长者晚几秒看屏时，那条刚到点的药
 * 仍该显示成「今日下一次」，而不是立刻被算成「已经过去了」。
 */
export const AGENDA_GRACE_MS = 60_000;

/**
 * 一条药单此刻的状态。
 *
 * 判据只有两处，都是既有口径，本函数不新造阈值：
 * 1. 已确认（`row.confirmedAt` 或调用方给的 `taken` 集合）→ `taken`；
 * 2. 否则看提醒窗口是否已过（`medicationWindowEnd`，来自 `taskDispatch`）：
 *    过了 → `missed`，没过 → `pending`。
 *
 * 窗口函数住在 `taskDispatch` 而不是这里，是为了**只有一处**实现 PRD §4.5 的
 * 「到点 + (触达次数-1)×5min + 60s」。两个模块互相引用（`taskDispatch` 反过来
 * 用本文件的 `formatClock`），但两侧都只在函数体内使用、没有顶层求值依赖，
 * ESM 的函数声明提升足以保证安全；将来若要拆环，把窗口函数整段搬进本文件、
 * `taskDispatch` 改成 re-export 即可。
 *
 * @param taken 调用方掌握的「本次会话已确认」集合；不传就只看窗口与 `confirmedAt`
 */
export function medicationStatus(
  row: ReminderRow,
  now: Date | number,
  taken: ReadonlySet<string> | null = null,
): MedicationStatus {
  const nowMs = typeof now === "number" ? now : now.getTime();
  if (row.confirmedAt) return "taken";
  if (taken !== null && taken.has(row.reminderId)) return "taken";
  // 只读 row 的三个字段（scheduledAt / attemptsSent / windowSeconds），所以把最小形状
  // 交给只认 `OfflineReminder` 的窗口函数是安全的；多出来的 planId/usage 它根本不碰。
  const end = medicationWindowEnd(row as OfflineReminder);
  if (Number.isNaN(end)) return "pending";
  return nowMs > end ? "missed" : "pending";
}

/** 「下一个该吃的药」：只带摘要行需要的四列，够摘要与抽屉直接用。 */
export interface MedicationAgendaItem {
  reminderId: string;
  drugName: string;
  dose: string;
  scheduledAt: string;
}

/** 今日药单的收敛视图：下一刻该吃什么 + 有几条已经没确认。 */
export interface MedicationAgenda {
  next: MedicationAgendaItem | null;
  missedCount: number;
}

/**
 * 把当日缓存收敛成「今天还剩什么」。
 *
 * 为什么要有它：摘要是常驻的一行，抽屉是按需展开的全量视图，两者若各算一遍，
 * 就会出现「摘要说今天没有待提醒的药、抽屉却列出两条已过期」——这不是渲染错，
 * 是两份口径在漂。收敛成一个纯函数后，两处只可能同时对或同时错。
 *
 * 规则（来自任务卡，不新造）：
 * - `next`：`scheduledAt ∈ [now-60s, 窗口结束]` 里**最早**一条，且尚未确认；
 * - `missedCount`：窗口已过、又没有确认信息的条数（即 `medicationStatus === "missed"`）。
 *
 * @param rows 当日缓存（`OfflineReminder[]` 可直接传）
 * @param now 判定时刻；`Date` 或毫秒数都收，便于用例钉死时刻
 */
export function medicationAgenda(
  rows: readonly ReminderRow[],
  now: Date | number,
): MedicationAgenda {
  const nowMs = typeof now === "number" ? now : now.getTime();
  if (Number.isNaN(nowMs)) return { next: null, missedCount: 0 };

  let missedCount = 0;
  let next: MedicationAgendaItem | null = null;
  let nextDue = Number.POSITIVE_INFINITY;

  for (const row of rows) {
    const status = medicationStatus(row, nowMs);
    if (status === "missed") {
      missedCount += 1;
      continue;
    }
    // 已吃的不算「没确认」，也不再是「下一次」——长者已经处理过这条了。
    if (status === "taken") continue;

    const due = Date.parse(row.scheduledAt);
    if (Number.isNaN(due)) continue;
    // 卡里写的下界：到点超过 60s 就不再算「下一次」（它多半是上一枪没确认的那条，
    // 会随窗口结束自然进入 missedCount，而不是一直霸占摘要）。
    if (due < nowMs - AGENDA_GRACE_MS) continue;
    if (due < nextDue) {
      nextDue = due;
      next = {
        reminderId: row.reminderId,
        drugName: row.drugName,
        dose: row.dose,
        scheduledAt: row.scheduledAt,
      };
    }
  }

  return { next, missedCount };
}
