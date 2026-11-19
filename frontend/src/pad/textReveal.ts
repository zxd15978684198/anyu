/**
 * 打字框（聊天框）回复的逐字揭示（2026-11-11 新增）。
 *
 * ## 为什么单独一条通道
 *
 * 语音回复的进度来自「远端音频真的出声」那一帧（`RealtimeClient` 写 `publishSubtitleProgress`）。
 * 打字回复**没有音频**：`POST /api/dialogue/text` 一次返回整段文本，`audio_url` 固定为 null，
 * 所以此前它是 `setSubtitle({ text: reply, source: "reply" })` 直接整段上屏——同一块大字幕，
 * 语音那条逐字、打字这条瞬出，长者会觉得「打字的像系统提示」。
 *
 * 这里给打字回复造一条**合成时间轴**：一条 segment、时长按字数估，零点就是现在。
 * 走的是与语音完全相同的进度通道（`subtitlePlan.ts` 的模块级变量），字幕组件不需要知道
 * 这段文字是听来的还是打出来的。
 *
 * ## 速率为什么按字数估
 *
 * 语音那条不猜速率（进度=真实音频），打字这条没有可依据的物理量，只能估：
 * 中文朗读约 4 字/秒，但长者读字幕慢一些、且这里要和「正在说」的动效同感，
 * 取 110ms/字（≈9 字/秒）接近真人快读；下限 900ms 保证短句（「好的」）也有可见的过程，
 * 不至于一闪而过看起来像没动。
 */

import { publishSubtitlePlanPresence, publishSubtitleProgress } from "./subtitlePlan";

/** 每字揭示耗时（毫秒）。改这个值要同步 `frontend/tests/text-reveal.test.ts` 的断言。 */
export const TEXT_REVEAL_MS_PER_CHAR = 110;

/** 最短揭示时长：短句也要看得见「在打字」。 */
export const TEXT_REVEAL_MIN_MS = 900;

/** 按字数估这一段该打多久（`Array.from` 按码点计数，emoji 不会被算成两个）。 */
export function estimateRevealDuration(text: string): number {
  const chars = Array.from(text).length;
  if (chars === 0) return 0;
  return Math.max(TEXT_REVEAL_MIN_MS, chars * TEXT_REVEAL_MS_PER_CHAR);
}

export interface TextRevealOptions {
  /**
   * 真实语音时长（ms）。语音补到之后用它**重锚**：字幕节奏跟着音频走，而不是按字数估。
   * 不传则用 `estimateRevealDuration()`（纯文本，无音频时的口径）。
   */
  durationMs?: number;
  /**
   * 重锚时的起点（ms）。语音后到时，旧揭示可能已经打了几秒——
   * 传「已打时长」进来，新时间轴从同一位置接着走，长者看不到字符回退。
   */
  startElapsedMs?: number;
}

export interface TextRevealHandle {
  /** 中止揭示并清掉进度（换句、退出登录、新一轮提交时调用）。 */
  cancel: () => void;
  /** 是否已打完。 */
  done: () => boolean;
  /** 已经揭示多久（ms）——重锚时用它当 `startElapsedMs`。 */
  elapsed: () => number;
}

type RafFn = (cb: (timestamp: number) => void) => number;
type CancelFn = (handle: number) => void;

function raf(): RafFn {
  const impl = globalThis.requestAnimationFrame;
  if (typeof impl === "function") return (cb) => impl(cb);
  // 单测/降级环境没有 rAF：用 setTimeout 顶替，保证「打完」这件事仍会发生。
  return (cb) => globalThis.setTimeout(() => cb(performance.now()), 16) as unknown as number;
}

function cancelRaf(): CancelFn {
  const impl = globalThis.cancelAnimationFrame;
  if (typeof impl === "function") return (handle) => impl(handle);
  return (handle) => globalThis.clearTimeout(handle);
}

let activeHandle: { cancel: () => void } | null = null;

/**
 * 开始逐字揭示 `text`。**必须在 `setSubtitle({ text, source: "reply" })` 之前调用**：
 * 字幕组件第一帧会看 `isSubtitlePlanPresent()` 决定「从 0 字打」还是「整段显示」，
 * 顺序反了就会先整句亮一帧再缩回去。
 */
export function startTextReveal(
  text: string,
  onDone?: () => void,
  options?: { durationMs?: number; startElapsedMs?: number },
): TextRevealHandle {
  // 同一时刻只允许一条：上一句没打完就来了新的（连发两条），直接顶掉旧的。
  activeHandle?.cancel();

  const realDuration = options?.durationMs;
  const durationMs =
    typeof realDuration === "number" && realDuration > 0
      ? realDuration
      : estimateRevealDuration(text);
  // 重锚：把时间轴零点往前挪「已打时长」，新时长下同一位置的字符数略有变化，
  // 但 BigSubtitle 侧有单调护栏（只进不退），画面不会回跳。
  const startElapsed =
    typeof options?.startElapsedMs === "number" && options.startElapsedMs > 0
      ? Math.min(options.startElapsedMs, durationMs)
      : 0;
  const startedAt = performance.now() - startElapsed;
  const schedule = raf();
  const unschedule = cancelRaf();

  publishSubtitlePlanPresence(true);
  publishSubtitleProgress({ index: 0, text, previousTexts: [], elapsedMs: 0, durationMs });

  let cancelled = false;
  let finished = false;
  let handle = 0;

  const stop = () => {
    cancelled = true;
    unschedule(handle);
    publishSubtitleProgress(null);
    publishSubtitlePlanPresence(false);
    if (activeHandle === self) activeHandle = null;
  };

  const tick = () => {
    if (cancelled) return;
    const elapsed = performance.now() - startedAt;
    if (elapsed >= durationMs) {
      // 收尾：把进度推到满格让最后一帧一定是整句，然后撤掉「有计划」这个位——
      // 保留 null 会让字幕退回整段显示，那本来就是最终状态。
      publishSubtitleProgress({ index: 0, text, previousTexts: [], elapsedMs: durationMs, durationMs });
      unschedule(handle);
      publishSubtitleProgress(null);
      publishSubtitlePlanPresence(false);
      finished = true;
      if (activeHandle === self) activeHandle = null;
      onDone?.();
      return;
    }
    publishSubtitleProgress({ index: 0, text, previousTexts: [], elapsedMs: elapsed, durationMs });
    handle = schedule(tick);
  };

  const self: TextRevealHandle & { cancel: () => void } = {
    cancel: stop,
    done: () => finished,
    elapsed: () => Math.min(performance.now() - startedAt, durationMs),
  };
  activeHandle = self;
  handle = schedule(tick);
  return self;
}

/** 中止当前（若有）揭示，例如退出登录/切屏。 */
export function stopTextReveal(): void {
  activeHandle?.cancel();
  activeHandle = null;
}
