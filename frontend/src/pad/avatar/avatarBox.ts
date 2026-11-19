/**
 * 数字人容器的**唯一几何口径**：真实舞台（`AvatarStage`）与 Suspense 兜底共用同一串类名。
 *
 * 为什么不再用 `min(760px, 72vh)`：
 *  - 那是**视口**尺寸，不感知父容器给了多少高度。1920×1080 下扣掉顶栏(≈80)、字幕带(≈250)、
 *    底部按钮后，中部只有 ≈600px；760 的固定方块会溢出到字幕区（2026-11-10 产品负责人实测
 *    「数字人和文字重叠」）。
 *  - 兜底当时写的是 `min(560px, 48vh)`，与真实容器不一致 → 加载完成瞬间画面从 560 跳到 760。
 *    两处共用本常量后，这个跳变从结构上消失。
 *
 * 现在的口径：高度由父容器决定（`h-full`），宽度由 `aspect-square` 推出，上下限各自封顶，
 * 任何视口都不会越界，也不会在字幕长短变化时改尺寸。
 *
 * 上限走 `--pad-avatar-max`（index.css 按断点给值）：2026-11-11 之前这里写死 760，
 * 断点里改的 `--pad-avatar-max` 根本没人消费——T/P 档的「缩小数字人」是空转的。
 */
export const AVATAR_BOX_CLASS = [
  "relative",
  "flex",
  "aspect-square",
  "h-full",
  "min-h-0",
  "max-h-[var(--pad-avatar-max)]",
  "max-w-[var(--pad-avatar-max)]",
  "items-center",
  "justify-center",
  // 产品负责人 2026-11-11：不要给模型设「框」——去掉边框/圆角/底色，整身直接站在页面上
  "overflow-visible",
].join(" ");
