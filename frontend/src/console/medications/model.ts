/**
 * 用药管理页的**纯逻辑层**（前端计划 W4：MedicationsPage 拆分第一步）。
 *
 * 从 1291 行、39 个 useState 的页面里抽出「与 React 无关」的部分：
 * 类型、表单初值与校验、到期复核的常量与日期算术。
 * 抽出来的都是纯函数，可以直接单测，不必渲染整页。
 *
 * 时区口径（AGENTS §8）：展示一律 Asia/Shanghai。
 * 原页面里有 `todayIso()` 与 `shanghaiToday()` **两个同义函数**，
 * 且前者一度用浏览器本地时区算「今天」；现统一走 `shared/format.todayInShanghai`。
 */
import { todayInShanghai } from "../../shared/format";

// ---------------------------------------------------------------- FR-HLT-10 聚合关注

export interface AttentionSignal {
  code: string;
  title: string;
  detail: string;
  suggestion: string;
  level: string;
  evidence: Record<string, unknown>;
  observed_at?: string | null;
}

export interface AttentionReport {
  elder_id: string;
  generated_at: string;
  window_days: number;
  rule_version: string;
  has_attention: boolean;
  codes: string[];
  disclaimer: string;
  signals: AttentionSignal[];
}

// ---------------------------------------------------------------- 建单 / 改档表单

export interface CreateForm {
  drug_name: string;
  dose: string;
  schedule_cron: string;
  start_date: string;
  end_date: string;
}

export type CreateErrors = Partial<Record<keyof CreateForm, string>>;

/** 上海自然日的今天（原页面 todayIso / shanghaiToday 两个同义函数收敛到此）。 */
export function todayIso(): string {
  return todayInShanghai();
}

export function emptyCreateForm(): CreateForm {
  return {
    drug_name: "",
    dose: "",
    schedule_cron: "08:00",
    start_date: todayIso(),
    end_date: "",
  };
}

/** 只挡「明显不可能是药名/剂量/时间」的输入；业务校验（格式、窗口先后）仍由服务端 422 兜底。 */
export function validateCreate(form: CreateForm): CreateErrors {
  const errors: CreateErrors = {};
  const drug = form.drug_name.trim();
  if (!drug) errors.drug_name = "请填药名";
  else if (drug.length > 100) errors.drug_name = "药名最多 100 字";

  const dose = form.dose.trim();
  if (!dose) errors.dose = "请填剂量";
  else if (dose.length > 50) errors.dose = "剂量最多 50 字";

  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(form.schedule_cron)) {
    errors.schedule_cron = "每日服药时刻只支持 HH:MM";
  }
  if (!form.start_date) errors.start_date = "请选择起始日";
  if (form.end_date && form.start_date && form.end_date < form.start_date) {
    errors.end_date = "停用日不能早于起始日";
  }
  return errors;
}

// ---------------------------------------------------------------- 到期复核

/** 与后端 `medication_service.REVIEW_WINDOW_DAYS` 对齐：到期前 7 天进入复核视野。 */
export const REVIEW_WINDOW_DAYS = 7;

/** 与后端 `medication_service.RENEW_DURATION_DAYS` 对齐：一次续方开 30 天。 */
export const RENEW_DURATION_DAYS = 30;

/** `date` 距 `today` 的自然日数（负数=已过期）；日期不合法返回 null，调用方跳过该行。 */
export function daysUntil(date: string, today: string): number | null {
  const target = Date.parse(`${date}T00:00:00Z`);
  const base = Date.parse(`${today}T00:00:00Z`);
  if (Number.isNaN(target) || Number.isNaN(base)) return null;
  return Math.round((target - base) / 86_400_000);
}

/** 续方后的新停用日：从今天起开 RENEW_DURATION_DAYS 天（纯 UTC 算术，不受本地时区影响）。 */
export function renewedEndDate(today: string): string {
  const base = Date.parse(`${today}T00:00:00Z`);
  if (Number.isNaN(base)) return today;
  const next = new Date(base + RENEW_DURATION_DAYS * 86_400_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-${pad(next.getUTCDate())}`;
}

/** 是否落在复核视野内：已过期，或距停用日 ≤ REVIEW_WINDOW_DAYS。 */
export function needsRenewReview(endDate: string | null | undefined, today: string): boolean {
  if (!endDate) return false;
  const days = daysUntil(endDate, today);
  return days !== null && days <= REVIEW_WINDOW_DAYS;
}