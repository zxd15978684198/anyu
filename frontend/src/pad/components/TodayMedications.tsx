/**
 * 今日用药面板（PRD:338 FR-MED-08 第①层 / TD:1111「当日药单写入 IndexedDB，
 * 断网可读，标『离线数据』」）。
 *
 * 为什么要有它：IndexedDB + Service Worker 只做到「断网能读」，验收还要求
 * 「仍能看到当日药单并标注『离线数据』」——这样一来必须有一块**看得见的药单**，
 * 而不是只在到点弹一次卡。断网时角标显式写「离线数据」，联网时撤掉角标，
 * 让人一眼知道当前看到的是服务器的最新值还是本地缓存。
 *
 * 数据来源只有 IndexedDB（`offline/db.ts` 的 `readReminders`），不直连网络：
 * 断网那一刻网络请求必失败，面板要显示的是「上次同步到本地的药单」，而不是空白。
 *
 * 尺寸口径（2026-11-11 版面重排）：卡片原先 520px 宽、`absolute` 贴在中部左栏左下角，
 * 会压住数字人左腿；收到 330px 后仍然压着，只是压得少一点。现在整块**移进右栏**
 * （字幕之上），改成文档流里的 `shrink-0` 块——左栏整块留给整身数字人，
 * 「读的信息」（药单 + 对话）集中在同一栏，窄屏堆叠时也跟着一起走。
 * 行内仍是「时刻 + 药名」一行、「剂量」一行，宽度不够时截断药名而不是挤压字号。
 */

import { useCallback, useEffect, useState } from "react";

import { formatClock, OFFLINE_BADGE, REMINDERS_UPDATED_EVENT, visibleRows } from "../medicationView";
import { readReminders } from "../offline/db";
import type { OfflineReminder } from "../offline/types";

/** 面板最多铺几行：贴底摆放，多出来的折成「还有 N 条」，既不压数字人也不遮字幕。
 *  多端适配（2026-11-11）：行数随断点收（D 4 / T 2 / P 1），见 `usePadTier()`。 */
const PANEL_ROWS = 4;

/**
 * 当前版面档位。
 *
 * 为什么不读 `window.innerWidth` 一次就完：平板横竖切换、浏览器缩放、桌面窗口拖拽
 * 都会让同一块屏在不同档之间来回跳；只读一次会让面板停在错的形态，直到整页刷新。
 * 用 `matchMedia` 的 change 事件，与 index.css 的断点完全同口径（含 `max-height:520px`
 * 这条守卫——否则「很矮但很宽」的窗口里 CSS 已按 P 档排版，而组件还以为自己是宽屏）。
 */
type PadTier = "wide" | "tablet" | "phone";

const PHONE_QUERY = "(max-width: 1023px), (max-height: 520px)";
const TABLET_QUERY = "(max-width: 1439px)";

function readPadTier(): PadTier {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return "wide";
  if (window.matchMedia(PHONE_QUERY).matches) return "phone";
  if (window.matchMedia(TABLET_QUERY).matches) return "tablet";
  return "wide";
}

function usePadTier(): PadTier {
  const [tier, setTier] = useState<PadTier>(readPadTier);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const queries = [window.matchMedia(PHONE_QUERY), window.matchMedia(TABLET_QUERY)];
    const update = () => setTier(readPadTier());
    queries.forEach((query) => query.addEventListener("change", update));
    return () => queries.forEach((query) => query.removeEventListener("change", update));
  }, []);
  return tier;
}

/** P 档行数：只留最近一条，其余折成「还有 N 条」——71px 的中部放不下一张卡 */
const TIER_ROWS: Record<PadTier, number> = { wide: PANEL_ROWS, tablet: 2, phone: 1 };

export function TodayMedications() {
  const [reminders, setReminders] = useState<OfflineReminder[]>([]);
  const [offline, setOffline] = useState<boolean>(() => !navigator.onLine);
  // 必须在早退 return 之前调用：否则「先没药单、后同步到药单」时 hook 数量会变
  const tier = usePadTier();

  const refresh = useCallback(async () => {
    try {
      setReminders(await readReminders());
    } catch {
      // 读缓存失败不是「没有药单」，但面板没有别的数据源可退；保持上一次渲染，
      // 不要清空成空白（空白会被误读成「今天不用吃药」）。
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onOnline = () => {
      setOffline(false);
      // 联网后缓存会被 initOfflineCache 重新拉一遍，稍后再读一次即可对齐
      void refresh();
    };
    const onOffline = () => setOffline(true);
    // 登录/刷新缓存之后立刻重读：否则「刚登录、当场看面板」要等到下一次 60s 轮询
    // 才见到药单，演示时看起来像「没有药单」。
    const onRefreshed = () => void refresh();
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    window.addEventListener(REMINDERS_UPDATED_EVENT, onRefreshed);
    const timer = window.setInterval(() => void refresh(), 60_000);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
      window.removeEventListener(REMINDERS_UPDATED_EVENT, onRefreshed);
      window.clearInterval(timer);
    };
  }, [refresh]);

  if (reminders.length === 0) return null;

  const { shown, rest } = visibleRows(reminders, TIER_ROWS[tier]);

  // P 档（手机横屏 667×375）中部只剩约 71px，整张卡会把数字人和字幕一起挤掉：
  // 折成一条 chip（只留最近一条）。`aria-label` / `data-testid` / `OFFLINE_BADGE`
  // 文案三处锚点必须原样保留——离线兜底 E2E（N54）和读屏都靠它们认这块面板。
  if (tier === "phone") {
    const first = shown[0];
    if (first === undefined) return null;
    return (
      <section
        aria-label="今日用药"
        data-testid="today-medications"
        data-compact="true"
        className="flex w-full shrink-0 items-center gap-2 rounded-xl border border-line bg-surface px-3 py-2 shadow-sm"
      >
        {offline && (
          <span className="shrink-0 rounded-full bg-amber-500/15 px-2 py-0.5 text-[length:var(--pad-med-text)] font-bold text-amber-700">
            {OFFLINE_BADGE}
          </span>
        )}
        <span className="shrink-0 text-[length:var(--pad-med-text)] font-bold tabular-nums text-ink">
          {formatClock(first.scheduledAt)}
        </span>
        <span className="min-w-0 truncate text-[length:var(--pad-med-text)] text-ink">{first.drugName}</span>
        {rest > 0 && (
          <span className="shrink-0 text-[length:var(--pad-med-text)] text-ink-muted">还有 {rest} 条</span>
        )}
      </section>
    );
  }

  return (
    <section
      aria-label="今日用药"
      data-testid="today-medications"
      className="w-[var(--pad-med-w)] shrink-0 rounded-2xl border border-line bg-surface px-5 py-4 shadow-sm"
    >
      <header className="flex items-center justify-between gap-3">
        <h2
          className="font-bold leading-tight text-ink"
          style={{ fontSize: "var(--pad-med-title)" }}
        >
          今日用药
        </h2>
        {offline ? (
          <span className="shrink-0 rounded-full bg-amber-500/15 px-3 py-1 text-xl font-bold text-amber-700">
            {OFFLINE_BADGE}
          </span>
        ) : (
          rest > 0 && (
            <span className="shrink-0 text-xl text-ink-muted">还有 {rest} 条</span>
          )
        )}
      </header>
      <ul className="mt-2 space-y-1.5">
        {shown.map((row) => (
          <li key={row.reminderId} className="flex flex-col leading-tight">
            <span className="flex items-baseline gap-2">
              <span className="w-[var(--pad-med-time-w)] shrink-0 text-[length:var(--pad-med-text)] font-bold tabular-nums text-ink">
                {formatClock(row.scheduledAt)}
              </span>
              <span className="min-w-0 flex-1 truncate text-[length:var(--pad-med-text)] text-ink">{row.drugName}</span>
            </span>
            <span className="text-xl text-ink-muted">{row.dose}</span>
          </li>
        ))}
      </ul>
      {rest > 0 && <p className="mt-1 text-xl text-ink-muted">还有 {rest} 条</p>}
    </section>
  );
}
