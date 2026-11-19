/**
 * 长者端浮层显隐的派生层（场景化改造 · 批零）。
 *
 * 为什么单独抽出来：`App.tsx` 里 `openOverlays` 拼装 + `pickVisible` 仲裁 +
 * 六个 `show*` 派生布尔一直挤在 1200 多行的组件体中，批一要把它换成场景状态机
 * 时没有地方「只换判据、不动渲染」。先原样搬家，行为零变化。
 *
 * 铁律（与 `overlayPriority.ts` 一致）：
 * 1. **`pickVisible` 仍是唯一可见性出口**——被压制的层根本不进 DOM，不靠 z-index 盖。
 * 2. **state 一律保留**——关掉上层后下层自然回来，不记「谁盖了谁」。
 * 3. **顺序只在 `OVERLAY_PRIORITY` 里改**，本文件不许另立优先级。
 *
 * 纯函数 `buildOpenOverlays` / `deriveOverlays` 可脱离 React 直接测；
 * `usePadOverlays` 只是把它们接到组件上，**每次渲染现算**（不做 memo，
 * 与搬家前逐行等价，避免缓存键漏项导致的显隐漂移）。
 */
import { pickVisible, type OverlayId } from "./overlayPriority";

/** 参与仲裁的开关 + 当前任务。
 *  2026-11-16 补入 incoming / privacy / guide 三项：它们原先绕过仲裁各写各的
 *  z-index，导致隐私说明（z-40 全屏）盖住按仲裁渲染的用药卡（z-20），
 *  长者看不见也点不到一条用药提醒。 */
export interface PadOverlayInputs<T extends { kind: OverlayId }> {
  activeTask: T | null;
  identityAsk: boolean;
  mediaOpen: boolean;
  memoryOpen: boolean;
  enrolling: boolean;
  wakeSettingsOpen: boolean;
  settingsOpen: boolean;
  /** 护工来电（全屏遮罩，仅次于 SOS）。 */
  incomingCall: boolean;
  /** 隐私说明（信息层，最低）。 */
  privacyOpen: boolean;
  /** 首启引导（信息层，最低）。 */
  guideOpen: boolean;
}

export interface PadOverlayView<T extends { kind: OverlayId }> {
  /** 当前打开着的浮层（未排序，交给 `pickVisible` 定唯一可见） */
  openOverlays: OverlayId[];
  /** 仲裁结果：唯一该渲染的那一个，`null` 表示一个都不该亮 */
  visibleOverlay: OverlayId | null;
  /** 任务抽屉：只有它自己是可见浮层时才渲染（被身份确认压住时连 DOM 都不进） */
  shownTask: T | null;
  showIdentity: boolean;
  showMedia: boolean;
  showMemory: boolean;
  showEnroll: boolean;
  showWake: boolean;
  showSettings: boolean;
  showIncoming: boolean;
  showPrivacy: boolean;
  showGuide: boolean;
}

/** 把「长者按过的开关 + 当前任务」翻译成「打开着的浮层列表」。 */
export function buildOpenOverlays<T extends { kind: OverlayId }>(
  inputs: PadOverlayInputs<T>,
): OverlayId[] {
  const open: OverlayId[] = [];
  if (inputs.activeTask) open.push(inputs.activeTask.kind);
  if (inputs.identityAsk) open.push("identity");
  // 戏曲播放器（2026-11-12）：长者自己按开的消遣浮层
  if (inputs.mediaOpen) open.push("media");
  // 长者记忆面板（FR-MEM-02）：与 media 同档，长者主动打开的自助查看
  if (inputs.memoryOpen) open.push("memory");
  if (inputs.enrolling) open.push("enroll");
  if (inputs.wakeSettingsOpen) open.push("wakeWord");
  if (inputs.settingsOpen) open.push("settings");
  if (inputs.incomingCall) open.push("incoming");
  if (inputs.privacyOpen) open.push("privacy");
  if (inputs.guideOpen) open.push("guide");
  return open;
}

/** 纯派生：仲裁一次，算出所有 `show*` 布尔。 */
export function deriveOverlays<T extends { kind: OverlayId }>(
  inputs: PadOverlayInputs<T>,
): PadOverlayView<T> {
  const openOverlays = buildOpenOverlays(inputs);
  const visibleOverlay = pickVisible(openOverlays);
  return {
    openOverlays,
    visibleOverlay,
    shownTask:
      inputs.activeTask !== null && visibleOverlay === inputs.activeTask.kind
        ? inputs.activeTask
        : null,
    showIdentity: visibleOverlay === "identity",
    showMedia: visibleOverlay === "media",
    showMemory: visibleOverlay === "memory",
    showEnroll: visibleOverlay === "enroll",
    showWake: visibleOverlay === "wakeWord",
    showSettings: visibleOverlay === "settings",
    showIncoming: visibleOverlay === "incoming",
    showPrivacy: visibleOverlay === "privacy",
    showGuide: visibleOverlay === "guide",
  };
}

/** 组件侧入口：与批零之前的逐行计算等价（不做 memo）。 */
export function usePadOverlays<T extends { kind: OverlayId }>(
  inputs: PadOverlayInputs<T>,
): PadOverlayView<T> {
  return deriveOverlays(inputs);
}
