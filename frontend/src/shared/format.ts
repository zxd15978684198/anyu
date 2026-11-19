/**
 * 三端共享的日期 / 时间格式化 —— **唯一真源**（前端计划 W2，Q-1 已裁决放开共享）。
 *
 * 口径（AGENTS §8）：存储一律 UTC，展示一律 `Asia/Shanghai`。
 *
 * 为什么必须收敛成一份：收敛前 console 有 **12 份**各自实现，其中多数走
 * `toLocaleString("zh-CN", { hour12: false })` 或 `date.getHours()`，即**浏览器本地时区**，
 * 只有 `knowledge.ts` 显式指定了上海。同一份数据在护工机器上随系统时区漂移，
 * 而家属端固定上海 —— 这不是风格差异，是 AGENTS §8 违规。
 *
 * 两条刻意保留的语义差异（**不是冲突，是两端契约不同**，各自有用例钉住）：
 * - console：空值 → `—`，解析不了 → **原样返回**（`knowledge-page.test.ts:124`
 *   「解析不了就原样给人看，不吞掉」）；
 * - family：空值与解析不了都 → `—`。
 * 所以这里提供 `invalid: "raw" | "dash"` 策略参数，而不是强行统一。
 *
 * 为什么不 `new Date(str).toLocaleString()` 一把梭：
 * - `report_date` 是**自然日字符串**（YYYY-MM-DD），`new Date("2026-11-09")` 按 UTC 零点解析，
 *   在负时区会掉一天；所以日期串只做纯 UTC 算术，不经过 Date 的本地时区；
 * - `created_at` 是带时区的时间戳，个别序列化不带后缀时补 `Z`，否则被当本地时间，整整差 8 小时。
 *
 * 本模块是**叶子**：不得 import pad / console / family 任何文件，也不得 import three。
 */

const SHANGHAI = "Asia/Shanghai";
const WEEKDAY = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"] as const;
const DASH = "—";

export interface DateParts {
  y: number;
  m: number;
  d: number;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** 解析带时区的瞬间；无后缀时补 `Z`（后端个别序列化不带后缀，不补会差 8 小时）。 */
export function parseInstant(value: string): Date | null {
  if (!value) return null;
  const text = /([zZ]|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const stamp = new Date(text);
  return Number.isNaN(stamp.getTime()) ? null : stamp;
}

export interface InstantFormatOptions {
  /** 是否带年份。console 表格带年，家属端 375px 窄列不带。 */
  withYear?: boolean;
  /** 解析不了时：`raw` 原样返回（console 契约）/ `dash` 返回 —（family 契约）。 */
  invalid?: "raw" | "dash";
}

/**
 * 瞬间 → 上海时间字符串。带年 `2026-11-10 08:30`，不带年 `11-10 08:30`。
 * 用 `formatToParts` 而不是 `format`：`zh-CN` 的 `format` 输出分隔符随 ICU 版本变
 * （`2026/11/10` vs `2026-11-10`），逐段取才能把形状钉死。
 */
export function formatInstant(value: string, options: InstantFormatOptions = {}): string {
  const { withYear = true, invalid = "dash" } = options;
  const stamp = parseInstant(value);
  if (stamp === null) return invalid === "raw" ? value : DASH;
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI,
    ...(withYear ? { year: "numeric" } : {}),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(stamp);
  const pick = (type: string): string => parts.find((part) => part.type === type)?.value ?? "";
  const date = withYear
    ? `${pick("year")}-${pick("month")}-${pick("day")}`
    : `${pick("month")}-${pick("day")}`;
  return `${date} ${pick("hour")}:${pick("minute")}`;
}

/** console 契约：空 → `—`，解析不了 → 原样返回。带年。 */
export function formatDateTimeFull(value?: string | null): string {
  if (!value) return DASH;
  return formatInstant(value, { withYear: true, invalid: "raw" });
}

/** family 契约：空或解析不了都 → `—`。不带年（375px 窄列）。 */
export function formatDateTimeShort(value?: string | null): string {
  if (!value) return DASH;
  return formatInstant(value, { withYear: false, invalid: "dash" });
}

/** 只要日期部分（上海自然日），`2026-11-10`。 */
export function formatDateOnly(value?: string | null): string {
  if (!value) return DASH;
  const stamp = parseInstant(value);
  if (stamp === null) return DASH;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: SHANGHAI,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(stamp);
  const pick = (type: string): string => parts.find((part) => part.type === type)?.value ?? "";
  return `${pick("year")}-${pick("month")}-${pick("day")}`;
}

// ------------------------------------------------------------------ 自然日串
// 下面这组只处理 `YYYY-MM-DD`，**不经过 Date 的本地时区**，避免负时区掉一天。

/** 解析 YYYY-MM-DD（容忍 `2026-11-09T00:00:00` 这类带时间的串，只取前 10 位）。 */
export function parseDateOnly(value: string): DateParts {
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) {
    const today = todayInShanghai();
    const [ty, tm, td] = today.split("-").map(Number);
    return { y: ty, m: tm, d: td };
  }
  return { y, m, d };
}

/** 「11 月 9 日 周日」——列表卡片与详情标题共用。 */
export function formatDateLabel(value: string): string {
  const { y, m, d } = parseDateOnly(value);
  const weekday = WEEKDAY[new Date(Date.UTC(y, m - 1, d)).getUTCDay()] ?? "";
  return `${String(m)} 月 ${String(d)} 日 ${weekday}`;
}

/** 「11-09」——趋势图横轴与 `<title>` 里用，短且不会把 375px 撑开。 */
export function formatShortDate(value: string): string {
  const { m, d } = parseDateOnly(value);
  return `${pad(m)}-${pad(d)}`;
}

/** 日期加减（纯 UTC 算术，规避夏令时与本地时区）。 */
export function shiftDate(value: string, deltaDays: number): string {
  const { y, m, d } = parseDateOnly(value);
  const stamp = Date.UTC(y, m - 1, d) + deltaDays * 86_400_000;
  const shifted = new Date(stamp);
  return `${String(shifted.getUTCFullYear())}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`;
}

/** 上海自然日的今天（YYYY-MM-DD）。后端默认 `date` 也是这个口径。 */
export function todayInShanghai(): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: SHANGHAI,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const pick = (type: string): string => parts.find((part) => part.type === type)?.value ?? "01";
  return `${pick("year")}-${pick("month")}-${pick("day")}`;
}

/** 报告日期是不是今天 —— 决定摘要用「今日」还是「当日」。 */
export function isToday(value: string): boolean {
  return value.slice(0, 10) === todayInShanghai();
}

/** 主键太长，表格里只留前 8 位；完整值放 `title`，悬停可看。 */
export function shortId(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value;
}