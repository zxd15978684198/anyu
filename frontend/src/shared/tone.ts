/**
 * 徽标语义色的**单一真源**（前端计划 W4）。
 *
 * 收敛前同一个联合类型在照护台里写了 **5 份**：
 * `medications.ts:37` / `notifications.ts:39` / `health.ts:21` 的 `BadgeTone`、
 * `knowledge.ts:69` 的 `KnowledgeTone`、`ui/Feedback.tsx:50` 的 `Tone`。
 * 五份字面量完全相同，改一处忘四处就会让某个页面的徽标色掉出联合类型。
 *
 * 为什么放在 `shared/` 而不是 `ui/Feedback`：数据模块（medications/health/…）
 * 也要用这个类型，让它们 import ui 组件会把「数据层 → 视图层」的依赖方向倒过来。
 * 这里是个**叶子模块**，只导出类型与一个色值映射，谁都能依赖它。
 *
 * 注意：这是**颜色语义**的收敛，不是业务用语的收敛。
 * 按产品负责人 Q-3 裁决，各端 grade/severity 的**中文说法刻意不统一**
 * （家属「需关注」/ 护工「需复核」），本模块不碰那些标签。
 */

/** 五档语义色，与 tokens.css 的 neutral/info/success/warning/danger 一一对应。 */
export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

/** 语义色 → token。禁止硬编码色值，全部走 tokens.css。 */
export const TONE_INK: Record<Tone, string> = {
  neutral: "var(--color-neutral-ink)",
  info: "var(--color-info-ink)",
  success: "var(--color-success-ink)",
  warning: "var(--color-warning-ink)",
  danger: "var(--color-danger-ink)",
};

export const TONE_SOFT: Record<Tone, string> = {
  neutral: "var(--color-neutral-soft)",
  info: "var(--color-info-soft)",
  success: "var(--color-success-soft)",
  warning: "var(--color-warning-soft)",
  danger: "var(--color-danger-soft)",
};

/** 未知值退到 neutral，不让徽标渲染成无样式裸文本。 */
export function asTone(value: string | null | undefined): Tone {
  return value === "info" || value === "success" || value === "warning" || value === "danger"
    ? value
    : "neutral";
}