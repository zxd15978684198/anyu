/**
 * 断网大字弹窗（PRD:338 FR-MED-08 第①层：「本地播报 + 大字弹窗」）。
 *
 * 为什么不用 Tailwind / React 渲染：这条链路要在**页面可能已经半死**的时候兜底
 * （主包加载失败、React 树崩了、路由还没挂上），所以只用裸 DOM + 内联样式，
 * 任何 CSS 构建产物缺失都不影响它显示。字号按长者端 1920×1080 的既有口径
 * （pad 页面方案：标题 ≥ 48px、正文 ≥ 32px）取更大值，保证 3 米外可读。
 *
 * 弹窗**不做确认写库**：断网时任何「已服/拒服」都无法回传，落一个本地假回执会污染
 * PRD:324 的回执口径（`no_answer` / `denied` / `offline` 三者的区分正是照护台要的）。
 * 按钮只有一个「知道了」，关掉只是收起界面。
 */
import { stopSpeaking } from "./tts";
import type { OfflineReminder } from "./types";

const ROOT_ID = "anyu-offline-overlay";

/** 时刻展示：长者端只认本地时间，统一按平板时区 `HH:MM`（与 pad 现有卡片一致）。 */
function clock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** 大字弹窗文案：只念药名/剂量/服用方式，**不出现任何诊断口径**（AGENTS §4.4）。 */
export function overlayText(reminder: OfflineReminder): string {
  return `该吃药了。${reminder.drugName}，${reminder.dose}。${reminder.usage}`;
}

function styleOf(element: HTMLElement, css: Partial<CSSStyleDeclaration>): void {
  Object.assign(element.style, css);
}

export interface OverlayOptions {
  /** 挂载点，默认 `document.body` */
  mountRoot?: HTMLElement;
  /** 收起时回调（弹窗被「知道了」或外部关闭都会调） */
  onDismiss?: () => void;
}

/**
 * 显示断网大字弹窗（单例：重复调用只更新内容，不会叠出两层）。
 * 返回收起函数。
 */
export function showMedicationOverlay(
  reminder: OfflineReminder,
  options: OverlayOptions = {},
): () => void {
  if (typeof document === "undefined") return () => undefined;
  const mount = options.mountRoot ?? document.body;

  document.getElementById(ROOT_ID)?.remove();

  const root = document.createElement("div");
  root.id = ROOT_ID;
  root.setAttribute("role", "alertdialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "用药提醒（断网本地播报）");
  root.setAttribute("lang", "zh-CN");
  styleOf(root, {
    position: "fixed",
    inset: "0",
    zIndex: "2147483000",
    boxSizing: "border-box",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: "40px",
    padding: "48px 64px",
    // 不透明：长者端最怕「字叠字」。半透明背景在登录页/白底页会透出底下的正文，
    // 弹窗就成了两块字压在一起，反而更看不清。
    background: "#0b0f16",
    color: "#ffffff",
    textAlign: "center",
    // 小屏（调试窗口、竖屏平板）内容放不下时允许滚动，而不是把标题裁掉
    overflowY: "auto",
    fontFamily:
      '"Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif',
  });

  const title = document.createElement("div");
  title.textContent = "该吃药了";
  styleOf(title, { fontSize: "112px", fontWeight: "800", lineHeight: "1.15", letterSpacing: "0.04em" });

  const drug = document.createElement("div");
  drug.textContent = `${reminder.drugName}　${reminder.dose}`;
  styleOf(drug, { fontSize: "88px", fontWeight: "700", lineHeight: "1.25" });

  const usage = document.createElement("div");
  usage.textContent = reminder.usage;
  styleOf(usage, { fontSize: "52px", lineHeight: "1.3", opacity: "0.92" });

  const timing = document.createElement("div");
  timing.textContent = `计划时间 ${clock(reminder.scheduledAt)}`;
  styleOf(timing, { fontSize: "40px", opacity: "0.78" });

  const hint = document.createElement("div");
  hint.textContent = "网络已断开：本条提醒由平板本地播报，恢复后会自动向护工补报";
  styleOf(hint, { fontSize: "34px", lineHeight: "1.35", opacity: "0.72", maxWidth: "1200px" });

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "知道了";
  button.setAttribute("aria-label", "知道了，关闭提醒");
  styleOf(button, {
    marginTop: "16px",
    minWidth: "420px",
    minHeight: "128px",
    padding: "0 56px",
    fontSize: "56px",
    fontWeight: "700",
    color: "#0b1220",
    background: "#ffd84d",
    border: "4px solid #ffd84d",
    borderRadius: "24px",
    cursor: "pointer",
  });

  let closed = false;
  const close = (): void => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKeyDown);
    stopSpeaking();
    root.remove();
    options.onDismiss?.();
  };
  const onKeyDown = (event: KeyboardEvent): void => {
    if (event.key === "Escape" || event.key === "Enter" || event.key === " ") close();
  };

  button.addEventListener("click", close);
  document.addEventListener("keydown", onKeyDown);

  root.append(title, drug, usage, timing, hint, button);
  mount.appendChild(root);
  // 焦点给按钮：外接键盘/遥控的平板能直接按回车关掉
  button.focus();
  return close;
}

/** 强制收起（切换提醒、断网恢复后调用）。 */
export function dismissOverlay(): void {
  if (typeof document === "undefined") return;
  document.getElementById(ROOT_ID)?.remove();
  stopSpeaking();
}
