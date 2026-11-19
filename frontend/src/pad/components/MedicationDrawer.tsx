/**
 * 「今日药单」侧边抽屉（场景化改造 · 批二 / S5）。
 *
 * 为什么要有它：`TodayMedications` 是**常驻右栏**的一块面板，只能铺 4 行（P 档只剩 1 行），
 * 长者想知道「今天到底还有几次药」就必须有个能展开看全的地方。抽屉就是那个「全量视图」，
 * 与 `MediaPanel` / `MemoryPanel` 同位置、同层级——右缘 fixed 抽屉，打开时不占版面、
 * 不挤压字幕与数字人。
 *
 * ## 受控、只读、不通网
 *
 * - **受控**：开关由调用方给（`open` / `onClose`），组件自己不持 `open` 状态，
 *   这样「场景机说该开」和「用户点关闭」只有一条真源，不会出现两个布尔打架。
 * - **只读**：数据唯一来源是 IndexedDB 里的当日缓存（`readReminders`），**不发请求**。
 *   断网时抽屉必须照样能开——这正是 FR-MED-08 第①层要的效果；联网刷新由
 *   `initOfflineCache()` 负责，它写完后派发 `REMINDERS_UPDATED_EVENT`，这里订阅后重读。
 * - **状态口径复用既有函数**：三态由 `medicationView.medicationStatus()` 判定
 *   （窗口截止 = 到点 + (触达次数-1)×5min + 本轮 60s，PRD §4.5，实现在 `taskDispatch`），
 *   时刻格式化取 `medicationView.formatClock()`。本文件**不新造**任何阈值或时间算法。
 *
 * ## 「已吃」为什么由调用方给
 *
 * 缓存行（`OfflineReminder`）里没有终态字段——服务端只把「待提醒」的定位键同步到平板，
 * 「已服自报」是点卡片当场上报的，落库在服务端。所以「已吃」不能从缓存推，必须由
 * 调用方把「本次会话已确认过」的 `reminderId` 集合传进来（`takenIds`，可选）。
 * 不传就只会有「待吃 / 已过期」两态——宁可少显示一态，也不能凭 `announcedAt`（只是
 * 「播报过」）谎报长者已经吃了。
 */
import { useCallback, useEffect, useState } from "react";

import {
  formatClock,
  medicationStatus,
  REMINDERS_UPDATED_EVENT,
  visibleRows,
} from "../medicationView";
import type { MedicationStatus } from "../medicationView";
import { readReminders } from "../offline/db";
import type { OfflineReminder } from "../offline/types";

/** 抽屉标题：一旦改动，源码级用例与验收脚本都得同步（不要同义替换）。 */
export const TITLE = "今天的药";

/** 空态：写「没有要吃的药」而不是「今天没有药」——别让长者以为今天不用吃药。 */
export const EMPTY_TEXT = "今天没有要吃的药";

/** 关闭按钮文案。 */
export const CLOSE_LABEL = "关闭";

/** 三态：已吃 / 待吃 / 已过期（就是 `medicationStatus` 的返回值类型，不再各写一份）。 */
export type ReminderStatus = MedicationStatus;

/** 状态文案（长者面：一眼能认）。 */
export const STATUS_LABEL: Record<ReminderStatus, string> = {
  taken: "已吃",
  pending: "待吃",
  missed: "已过期",
};

/** 状态徽标配色：红=过期、绿=已吃、中性=待吃；只用既有 token，不新增 CSS。 */
export const STATUS_CLASS: Record<ReminderStatus, string> = {
  taken: "bg-emerald-500/15 text-emerald-700",
  pending: "bg-amber-500/15 text-amber-700",
  missed: "bg-red-500/10 text-red-700",
};

/** 一行药单：药名 + 剂量 + 时间 + 状态（渲染只用这几个字段）。 */
export interface MedicationRow {
  reminder: OfflineReminder;
  clock: string;
  status: ReminderStatus;
  label: string;
}

/**
 * 一条缓存提醒此刻的状态。
 *
 * @param taken 调用方给的「本次会话已确认」集合；不传视为空集（只有待吃/已过期）
 *
 * 判据**不在这里**：窗口末尾的口径（PRD §4.5）与三态语义收在 `medicationStatus`，
 * 抽屉、摘要、将来的状态栏都调那一个函数。这里只保留抽屉自己的入参形状，
 * 免得「摘要说已过期、抽屉说待吃」这种两份口径漂移。
 */
export function reminderStatus(
  reminder: OfflineReminder,
  now: Date,
  taken: ReadonlySet<string> = new Set<string>(),
): ReminderStatus {
  return medicationStatus(reminder, now, taken);
}

/** 把缓存行摊成渲染行（排序复用 `visibleRows`，不在这里写第二套排序）。 */
export function toRows(
  reminders: readonly OfflineReminder[],
  now: Date,
  taken: ReadonlySet<string> = new Set<string>(),
): MedicationRow[] {
  const { shown } = visibleRows(reminders, reminders.length);
  return shown.map((reminder) => {
    const status = reminderStatus(reminder, now, taken);
    return {
      reminder,
      clock: formatClock(reminder.scheduledAt),
      status,
      label: STATUS_LABEL[status],
    };
  });
}

export interface MedicationDrawerProps {
  /** 是否打开：由调用方（场景机接线后是 App）控制。 */
  open: boolean;
  /** 关闭：点右上角关闭按钮时回调。 */
  onClose: () => void;
  /** 本次会话已确认服药（或已处理）的 `reminderId`；不传则「已吃」不出现。 */
  takenIds?: ReadonlySet<string>;
}

export function MedicationDrawer({ open, onClose, takenIds }: MedicationDrawerProps) {
  const [reminders, setReminders] = useState<OfflineReminder[]>([]);
  // 判定时刻单独存：60s 重读一次顺便推进 now，跨过窗口末尾时徽标会自己从「待吃」翻成「已过期」。
  const [now, setNow] = useState<Date>(() => new Date());

  const refresh = useCallback(async () => {
    try {
      const rows = await readReminders();
      setReminders(rows);
      setNow(new Date());
    } catch {
      // 读缓存失败不是「没有药」：保持上一次渲染，绝不清空成空白（空白会被读成「今天不用吃药」）。
    }
  }, []);

  // hooks 必须在早退之前调用，否则「先关后开」会让 hook 数量变化并崩掉渲染树。
  useEffect(() => {
    if (!open) return;
    void refresh();
    const onRefreshed = () => void refresh();
    window.addEventListener(REMINDERS_UPDATED_EVENT, onRefreshed);
    const timer = window.setInterval(() => void refresh(), 60_000);
    return () => {
      window.removeEventListener(REMINDERS_UPDATED_EVENT, onRefreshed);
      window.clearInterval(timer);
    };
  }, [open, refresh]);

  if (!open) return null;

  const rows = toRows(reminders, now, takenIds);

  return (
    <div className="pointer-events-none fixed inset-y-0 right-0 z-20 flex items-center pr-[var(--pad-drawer-gap)]">
      <div
        aria-label={TITLE}
        className="pointer-events-auto max-h-full w-[min(30rem,92vw)] overflow-y-auto rounded-[1.75rem] border border-line bg-surface p-6 shadow-lg"
        role="dialog"
      >
        <header className="flex items-center justify-between gap-3">
          <h2 className="text-[length:var(--pad-med-title)] font-bold leading-tight text-ink">
            {TITLE}
          </h2>
          <button
            aria-label={`${CLOSE_LABEL}${TITLE}`}
            className="min-h-[48px] shrink-0 rounded-xl border border-line px-4 text-2xl text-ink"
            onClick={onClose}
            type="button"
          >
            {CLOSE_LABEL}
          </button>
        </header>
        {rows.length === 0 ? (
          <p className="mt-4 text-2xl text-ink-muted">{EMPTY_TEXT}</p>
        ) : (
          <ul className="mt-3 space-y-2">
            {rows.map((row) => (
              <li
                key={row.reminder.reminderId}
                className="flex flex-col gap-1 border-b border-line/60 pb-2 last:border-b-0"
              >
                <span className="flex items-baseline gap-2">
                  <span className="w-[var(--pad-med-time-w)] shrink-0 text-[length:var(--pad-med-text)] font-bold tabular-nums text-ink">
                    {row.clock}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[length:var(--pad-med-text)] text-ink">
                    {row.reminder.drugName}
                  </span>
                  <span
                    className={`shrink-0 rounded-full px-3 py-0.5 text-xl font-bold ${STATUS_CLASS[row.status]}`}
                  >
                    {row.label}
                  </span>
                </span>
                <span className="text-xl text-ink-muted">{row.reminder.dose}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
