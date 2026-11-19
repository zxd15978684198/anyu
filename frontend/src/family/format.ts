/**
 * 家属端日期 / 时间格式化 —— **薄再导出层**。
 *
 * 实现已收敛到 `shared/format.ts`（前端计划 W2，Q-1 已裁决放开共享）。
 * 本文件只保留家属端的对外 API 与契约，不再自己实现：
 * - `formatDateTime` 走**不带年**的短形状（375px 窄列放不下年份）；
 * - 空值与解析不了都返回 `—`（与 console 的「原样返回」刻意不同，两端各有用例钉住）。
 *
 * 口径（AGENTS §8）：存储一律 UTC，展示一律 `Asia/Shanghai`。
 */
export {
  type DateParts,
  formatDateLabel,
  formatShortDate,
  isToday,
  parseDateOnly,
  shiftDate,
  todayInShanghai,
} from "../shared/format";

import { formatDateTimeShort } from "../shared/format";

/** 「11-09 14:23」——通知中心与日报列表的时间列。 */
export function formatDateTime(value: string): string {
  return formatDateTimeShort(value);
}