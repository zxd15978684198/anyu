/**
 * 顶栏 / 底栏的**实测高度**回写 CSS 变量（B6-L3）。
 *
 * 为什么必须实测：`AvatarLayer` 是 fixed 层，靠 `--pad-header-h` /
 * `--pad-footer-h-real` 把自己夹在顶栏和底栏之间。这两个数原先是手填的常量，
 * 而顶栏在窄屏会换行、底栏会被提示语和输入框撑高——2026-11-11 CDP 实测：
 * T 档变量写 88、顶栏实际 64（数字人整体下坠 24px）；P 档变量写 64、顶栏实际 114
 * （数字人头顶被顶栏盖住 50px），底栏变量写 0、实际 156（脚底被底栏盖住）。
 * 手填的预算永远追不上真实排版，所以改成「排版说了算」。
 *
 * 不会形成反馈环：顶栏高度由 `--pad-header-pt` 与内容决定，底栏由 `--pad-footer-h`
 * （下限）与内容决定，两者都**不读**这里写回的两个变量。
 */

export const HEADER_BAND_VAR = "--pad-header-h";
export const FOOTER_BAND_VAR = "--pad-footer-h-real";

/** 量一个元素的占高；拿不到（未挂载 / 隐藏 / 0 高）返回 null，调用方保持原值。 */
export function bandHeight(el: Element | null | undefined): number | null {
  if (!el) return null;
  const height = el.getBoundingClientRect().height;
  if (!Number.isFinite(height) || height <= 0) return null;
  return Math.round(height);
}

/**
 * 盯住顶栏与底栏，把实测高度写进 `target` 的内联样式。
 * 返回清理函数：卸载时**移除**内联值，让样式表里的断点默认值重新生效
 * （否则退出登录回到登录页后，画布还按主界面的高度定位）。
 */
export function observeBands(
  header: Element | null,
  footer: Element | null,
  target: HTMLElement,
): () => void {
  const write = () => {
    const h = bandHeight(header);
    const f = bandHeight(footer);
    if (h !== null) target.style.setProperty(HEADER_BAND_VAR, `${h}px`);
    if (f !== null) target.style.setProperty(FOOTER_BAND_VAR, `${f}px`);
  };

  write();

  if (typeof ResizeObserver === "undefined") return () => clearBands(target);
  const observer = new ResizeObserver(write);
  if (header) observer.observe(header);
  if (footer) observer.observe(footer);
  return () => {
    observer.disconnect();
    clearBands(target);
  };
}

/** 撤掉内联覆盖，交还给样式表的断点默认值。 */
export function clearBands(target: HTMLElement): void {
  target.style.removeProperty(HEADER_BAND_VAR);
  target.style.removeProperty(FOOTER_BAND_VAR);
}