/**
 * 药单一行摘要（S3-批二 / task-27）。
 *
 * 为什么要有它：长者端右栏的 `TodayMedications` 是一整块面板，铺满今天所有提醒，
 * 会跟「对话记录」抢高度；而长者真正关心的是**下一次该吃什么**。这一行只回答那一个问题，
 * 一句话 + 一个「查看药单」按钮，点开才进完整面板。
 *
 * 为什么是**受控纯展示**组件：数据由调用方给（`plan`），开关也由调用方给（`onOpen`）。
 * 组件自己不 fetch、不读 IndexedDB、不持状态——药单的真源仍是服务端同步到 `offline/db.ts`
 * 的那份，谁取数据谁负责，这样它在登录前后、断网时行为都不会变，也不必为它造第二套数据通路。
 *
 * 为什么时间和格式不收进 `medicationView.ts`：那里的 `formatClock` 是给缓存面板用的，
 * 这里要在组件内联出现（`toLocaleTimeString`），源码级用例才能守住「非法日期不许显示
 * `Invalid Date`」这条——两种写法都保持 24 小时制，长者看到的是同一个 `08:30`。
 */

/**
 * 一行摘要要的数据（只取展示需要的几个字段，不绑死任一上层类型）。
 *
 * 为什么剂量有两个字段名：`medicationAgenda().next` 给的是 `dose`（与
 * `OfflineReminder` / 服务端 `PadReminderOut` 同口径），早期摘要结构用的是 `dosage`。
 * 两个都收、渲染时取先有的那个，收敛期就能让 App.tsx 直接传 `agenda.next`，
 * 也不必为改名做一次全仓搜索替换。
 */
export interface MedicationPlanSummary {
  /** 计划 id（可选）：调用方要拿它做 `key` 或埋点就传，组件本身不解释它 */
  id?: string;
  /** 药品名，如「苯磺酸氨氯地平」 */
  drugName: string;
  /** 单次剂量（新口径），如「5mg」「1 片」 */
  dose?: string;
  /** 单次剂量（旧口径，兼容保留）；与 `dose` 同时给时以 `dose` 为准 */
  dosage?: string;
  /** 计划到点时刻（ISO8601，带时区；UTC 存储、展示转 Asia/Shanghai） */
  scheduledAt: string;
}

export interface MedicationSummaryProps {
  /** 今日下一次要吃的药；`null` = 今天没有待提醒的药 */
  plan: MedicationPlanSummary | null;
  /**
   * 今天已经过窗口、又没确认的条数（`medicationAgenda().missedCount`）。
   *
   * 有它才能说出「今天有 2 条药没确认」，而不是在 `plan === null` 时一律说
   * 「今天没有待提醒的药」——后者会和抽屉里列出的已过期条目对不上口径。
   */
  missedCount?: number;
  /** 点「查看药单」：由调用方决定打开面板还是抽屉 */
  onOpen: () => void;
}

/** 空态文案：用「没有待提醒」而不是「没有药」——别让人以为今天不用吃药。 */
export const EMPTY_TEXT = "今天没有待提醒的药";

/**
 * 有漏确认时的文案；`{n}` 由调用方替换成条数。
 *
 * 只说「没确认」，**不说**「漏服 / 该补吃」：本系统不做服药依从性判断，
 * 更不提供补服建议（AGENTS §4-4 医疗口径红线）。
 */
export const MISSED_TEXT_TEMPLATE = "今天有 {n} 条药没确认，点开看看";

/** 把条数填进 `MISSED_TEXT_TEMPLATE`。 */
export function missedText(count: number): string {
  return MISSED_TEXT_TEMPLATE.replace("{n}", String(count));
}


/** 按钮文案：固定「查看药单」，不做同义替换（要能被肉眼与用例同时搜到）。 */
export const OPEN_LABEL = "查看药单";

/** 摘要前缀：告诉长者这是「下一次」而不是「今天全部」。 */
export const SUMMARY_PREFIX = "今日下一次：";

/** 解析不出时刻时的占位：绝不把 `Invalid Date` 当正文显示（会被读屏念出来）。 */
export const UNKNOWN_CLOCK = "--:--";

/** `2026-11-13T00:30:00+00:00` → `08:30`；非法或空值 → `--:--`。 */
export function formatScheduledClock(scheduledAt: string): string {
  const parsed = new Date(scheduledAt);
  if (Number.isNaN(parsed.getTime())) return UNKNOWN_CLOCK;
  return parsed.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

/**
 * 渲染一行摘要 + 右侧「查看药单」。
 *
 * 版面：`flex` 两端对齐、`min-h-[64px]`——长者端可点下限是 48px（坐得远、手在抖），
 * 这里连带行高留到 64px，按钮本身再撑满行高，整行都是一个可点目标。
 * 样式只用既有类名与 token（`bg-surface` / `border-line` / `text-ink-muted` / `bg-brand`
 * 等 Tailwind 语义类，配 `--pad-*` 变量做内边距），不新增全局 CSS、不改 `tokens.css`。
 */
export function MedicationSummary({ plan, missedCount = 0, onOpen }: MedicationSummaryProps) {
  // 三分支只在这里判一次：有下一次 → 说下一次；没有但今天有漏确认 → 说漏确认；
  // 都没有 → 空态。三处文案互斥，避免「没有待提醒」与抽屉里的已过期条目打架。
  const text =
    plan === null
      ? missedCount > 0
        ? missedText(missedCount)
        : EMPTY_TEXT
      : `${SUMMARY_PREFIX}${plan.drugName} ${plan.dose ?? plan.dosage ?? ""} · ${formatScheduledClock(plan.scheduledAt)}`;

  return (
    <section
      aria-label="今日用药摘要"
      className="flex min-h-[64px] shrink-0 items-center gap-4 rounded-[1.75rem] border border-line bg-surface py-3 pr-3 pl-[var(--pad-gutter-x)]"
    >
      {plan === null ? (
        <p className="pad-med-summary min-w-0 flex-1 text-2xl text-ink-muted">{text}</p>
      ) : (
        <p className="pad-med-summary min-w-0 flex-1 text-2xl text-ink">
          {SUMMARY_PREFIX}
          <span className="font-bold">{plan.drugName}</span>
          <span className="text-ink-muted">
            {" "}
            {plan.dose ?? plan.dosage ?? ""} ·{" "}
          </span>
          <span className="tabular-nums">{formatScheduledClock(plan.scheduledAt)}</span>
        </p>
      )}
      <button
        type="button"
        onClick={onOpen}
        className="min-h-[48px] shrink-0 rounded-2xl bg-brand px-6 text-xl font-bold text-ink-inverse active:bg-brand-strong"
      >
        {OPEN_LABEL}
      </button>
    </section>
  );
}
