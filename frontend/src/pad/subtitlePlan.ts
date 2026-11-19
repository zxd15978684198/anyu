/**
 * 逐句字幕（方案 B）的**取值逻辑**，拆成纯函数以便单测。
 *
 * ## 为什么需要它
 *
 * 2026-11-10 产品负责人实测：口令「数字人和文字不同步」。根因不是渲染，
 * 而是服务端**逐句**合成音频（一句合成完就入队播一句），字幕却是一条**整段**文本
 * 一次性上屏 —— 字幕天然领先整段音频的时长，越长的回复偏得越多。
 *
 * ## 时间轴怎么对齐
 *
 * 服务端按**播放流落位**换算 `start_ms`（下行固定 20ms/帧）。落位 = 已出帧数（**含静音**）
 * + 队列里排在前面的帧数，所以句与句之间「上一句播完了、下一句还没合成好」的那段静音
 * 也在时间轴上——2026-11-11 之前只数「已入队帧数」，静音不在轴上，句子越多偏得越远。
 * 前端的零点取「第一段音频真的出声」那一帧（与口型同一个能量判定点，由 AudioWorklet
 * 的采样计数提供），于是两端零点一致，不需要额外握手、也不必猜合成耗时。
 *
 * 这里回答两个问题：**已播放 `elapsedMs` 毫秒时，该显示第几句、第几个字**。
 */

import type { TtsSegment } from "./protocol";

/**
 * 取当前应显示的下标：`start_ms ≤ elapsedMs` 的最后一句。
 *
 * - `elapsedMs` 小于第一句起点（理论上不会发生，锚点就是第一句起点）→ 返回 0，
 *   宁可显示第一句也不要空白；
 * - 超过最后一句的结尾 → 返回最后一句的 text 继续留在屏幕上（大字幕不该突然消失，
 *   长者反应慢，留白等于「刚才那句没听见也没看见」）；
 * - 空计划由调用方拦截，这里不返回 -1（调用方拿它直接索引）。
 */
export function pickSegmentIndex(plan: readonly TtsSegment[], elapsedMs: number): number {
  let index = 0;
  for (let i = 0; i < plan.length; i += 1) {
    if (elapsedMs >= plan[i].start_ms) index = i;
    else break;
  }
  return index;
}

/**
 * 当前句之上留几句历史。
 *
 * 2026-11-11 版面重排：字幕从「底部三行横带」改成右栏整列的对话记录，高度不再被
 * 三行封顶，所以可以留两句上文。留两句而不是全部：长回复会把当前句推到滚动区外，
 * 长者要的是「正在说的这句」，历史只是让他能回看刚才漏听的部分。
 */
export const SUBTITLE_HISTORY_LINES = 2;

/** 当前句之前的最多 `SUBTITLE_HISTORY_LINES` 句，**旧的在前**；第一句给空数组。 */
export function historyTexts(plan: readonly TtsSegment[], index: number): string[] {
  const end = Math.max(0, index);
  return plan.slice(Math.max(0, end - SUBTITLE_HISTORY_LINES), end).map((s) => s.text);
}

/**
 * 当前句的播放进度：**已播毫秒**与**该句时长**，外加最多两句上文。
 *
 * 为什么进度要在这里算、而不是组件自己拿时钟推：零点（第一段出声那一帧）只有
 * `RealtimeClient` 知道，它同时是口型的能量判定点。组件自己起一个时钟，就等于
 * 和音频各走各的表，句间停顿、卡顿、后台标签页一恢复就会错位。所以时间只由
 * realtime 提供，这里只做「减法 + 夹取」。
 */
export interface SubtitleProgress {
  /** 当前句在计划里的下标（0 起） */
  index: number;
  /** 当前句全文，与 `onSubtitle(text, "reply")` 的 text 一致（组件据此核对是不是同一句） */
  text: string;
  /** 当前句之前的最多两句，旧的在前；第一句为空数组 */
  previousTexts: readonly string[];
  /** 当前句已播毫秒，已夹在 `[0, durationMs]` */
  elapsedMs: number;
  /** 当前句时长（服务端 `tts_segments` 的 duration_ms）；0 表示服务端没给可信值 */
  durationMs: number;
  /**
   * 句内逐字坐标（服务端 `tts_segments` 的 `char_ms`）：每个字的**结束偏移**，相对句首。
   * 缺省 / 长度与句字数不符时按「没有坐标」处理，回落到按时长比例揭示。
   */
  charMs?: readonly number[];
}

/**
 * 已播 `elapsedMs` 毫秒时，当前句的进度；空计划返回 null（调用方据此回落到整段渲染）。
 *
 * `index` 可传可不传：`RealtimeClient` 那边先调 `pickSegmentIndex` 再传进来，
 * 保证「取第几句」全仓只有一处口径；单测/组件侧不关心下标时直接传 elapsed 即可。
 *
 * `duration_ms` 缺失或 ≤0 时不再夹取：宁可按「没有时长」处理（整句显示），
 * 也不要把进度永远钉在 0、让大字幕停在空白上——长者会以为机器没反应。
 */
export function segmentProgressAt(
  plan: readonly TtsSegment[],
  elapsedMs: number,
  index?: number,
): SubtitleProgress | null {
  if (plan.length === 0) return null;
  const current = index ?? pickSegmentIndex(plan, elapsedMs);
  const segment = plan[current];
  const durationMs =
    Number.isFinite(segment.duration_ms) && segment.duration_ms > 0 ? segment.duration_ms : 0;
  const raw = elapsedMs - segment.start_ms;
  let elapsed = 0;
  if (Number.isFinite(raw) && raw > 0) elapsed = durationMs > 0 ? Math.min(raw, durationMs) : raw;
  return {
    index: current,
    text: segment.text,
    previousTexts: historyTexts(plan, current),
    elapsedMs: elapsed,
    durationMs,
    charMs: segment.char_ms,
  };
}

/**
 * 逐字揭示：已播 `elapsedMs` 毫秒时该显示几个字。
 *
 * - **按字符不按字节**：中文一个字是一个信息单位，用 `Array.from` 按码点切，
 *   既不会把「你」按 UTF-8 字节切碎，也不会把 emoji 的代理对切成半个乱码。
 * - **有 `charMs` 就按坐标走**：服务端把这一句的 PCM 切成 20ms 帧、按能量挑出「有声帧」，
 *   再把有声时长分摊到每个字上，给出每个字的**结束偏移**。于是句中的停顿、拖长的尾音、
 *   念得慢的药名都落在坐标里——比例法在这些地方必然偏（它假设一句里字速恒定）。
 *   坐标是「结束偏移」，所以 `elapsed >= charMs[i]` 才算第 i 个字念完、可以上屏。
 * - 没有坐标时才用比例：`已播 / 该句时长`。**不引入「每秒 N 字」的固定速率**，
 *   固定速率一定会出现「字先说完、声音还在」或反过来。
 * - 出声即亮第一个字：比例法 `floor` 在小比例下得 0、坐标法在第一个字念完前也是 0，
 *   开头几十毫秒大字幕会空白，长者会以为没反应；所以 `elapsedMs > 0` 时至少 1 个字。
 * - 没有可信时长（`durationMs <= 0`）且没有坐标 → 整句显示，不逐字：没有节奏依据时
 *   宁可一次性给全，也不要卡在 0 个字。
 */
export function revealedCharCount(
  text: string,
  elapsedMs: number,
  durationMs: number,
  charMs?: readonly number[],
): number {
  const chars = Array.from(text);
  if (chars.length === 0) return 0;
  if (charMs && charMs.length === chars.length) {
    if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return 0;
    let count = 0;
    while (count < chars.length && elapsedMs >= charMs[count]) count += 1;
    return Math.min(chars.length, Math.max(1, count));
  }
  if (!Number.isFinite(durationMs) || durationMs <= 0) return chars.length;
  if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return 0;
  const ratio = Math.min(1, elapsedMs / durationMs);
  const count = Math.floor(ratio * chars.length);
  return Math.min(chars.length, Math.max(1, count));
}

/* ------------------------------------------------------------------ *
 * 进度通道：realtime 每帧写入，BigSubtitle 每帧读取
 *
 * 为什么用模块级变量而不是 React 状态 / 新增回调参数：
 * 1. `App.tsx` 这次不归我改（有并行 writer 在同文件），所以进度不能靠 props 往下传；
 * 2. 60fps 的 `setState` 会让整棵 pad 树每帧重渲染（含 3D 视图），代价远大于收益；
 * 3. 写入只是给一个变量赋值，读取在组件自己的 rAF 里做、只在「字数变了」时才 setState，
 *    重渲染最多每秒十几次、且只落在字幕这一个叶子上。
 * ------------------------------------------------------------------ */

let currentProgress: SubtitleProgress | null = null;

/** 由 `RealtimeClient` 每帧调用；传 null 表示「当前没有在逐句播放」（待机/打断/收窗）。 */
export function publishSubtitleProgress(progress: SubtitleProgress | null): void {
  currentProgress = progress;
}

/** 由字幕组件在 rAF 里读取；没有进度时返回 null（调用方回落到整段渲染）。 */
export function readSubtitleProgress(): SubtitleProgress | null {
  return currentProgress;
}

/* ------------------------------------------------------------------
 * 当前这句「是不是有播放计划」
 *
 * 为什么要单独一个位：字幕组件第一帧就到、但进度通道要等首个音频能量帧才写值。
 * 如果第一帧按「整段显示」渲染，长者会先看到整句、下一帧再缩回去从头打
 * （实测字符数 21→22→3→9→10）。有计划的句子必须从 0 个字开始。
 *
 * 没有计划时仍整段显示（旧服务端 / TTS 全挂的兜底文本），否则那类句子会永远空屏。
 * ------------------------------------------------------------------ */

let planPresent = false;

/** 由 `RealtimeClient` 在「收到/清掉本句播放计划」时调用。 */
export function publishSubtitlePlanPresence(present: boolean): void {
  planPresent = present;
}

/** 当前这句是否已有播放计划（决定字幕组件首帧是从 0 字打还是整段显示）。 */
export function isSubtitlePlanPresent(): boolean {
  return planPresent;
}
