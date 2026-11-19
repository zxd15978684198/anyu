/**
 * 大字幕（FR-UI-02）：数字人回复逐字揭示，节奏跟着**这一句的播放进度**走。
 *
 * ## 为什么要逐字
 *
 * 2026-11-10 实测：整句瞬间上屏，长者眼睛先看完了、耳朵还没听到，读起来像「字幕和声音
 * 各说各的」。改成跟着播放进度揭示后，看到的进度与听到的进度是同一个量：有服务端
 * 逐字坐标（`char_ms`，按这一句 PCM 的能量分布算）就按坐标走，没有才退回 `elapsed/duration`
 * 的比例——比例假设一句里字速恒定，句中停顿和拖长的尾音必然偏，坐标里则本来就有。
 *
 * ## 时间从哪来（为什么组件不自己起时钟）
 *
 * 单个零点只有 `RealtimeClient` 知道：第一段音频**真的出声**那一帧（与口型同一个能量判定点），
 * 服务端按入队帧数累计 `start_ms`，两端零点一致。所以进度由 realtime 每帧写进
 * `subtitlePlan.ts` 的模块级通道，本组件在自己的 rAF 里读——组件自起时钟就会和音频各走各的表，
 * 句间停顿、卡顿、后台标签页恢复后必然错位。
 *
 * ## 句切换口径（改这里前先读完）
 *
 * - **保留上文 2 句**（浅色、各封顶 2 行）作为上下文，当前句从 0 开始打字。
 * - **当前句不截断**：2026-11-11 版面重排后字幕是右栏一整列，高度由版面给，
 *   长回复在区内滚动并自动贴底。旧的 `line-clamp` 三行封顶会把正在说的后半句裁掉，
 *   长者读到的永远是半句话——这是「同步」被看见的最后一环，不能裁。
 * - **非 reply 不逐字**：ASR 中间稿/终稿、系统提示是「别人说的话 / 系统的话」，逐字既不像对话，
 *   又会让已经听到的内容看起来在卡顿。
 * - **没有进度 = 整段显示**：旧服务端没有 `tts_segments`、或计划未到时，回落到原来的整句渲染，
 *   宁可不上动效也不能空屏。
 * - **重置必须发生在 paint 之前**（`useLayoutEffect`）：用 `useEffect` 时「整句」已经在上一帧
 *   画到屏幕上，长者先看到 21~22 个字、下一帧再缩回 0 重打（2026-11-10 CDP 采样到的
 *   `21,22,12,13,…` 就是这一帧）。
 * - 卸载 / 换句时取消 rAF（不用 setInterval，不会在后台泄漏定时器）。
 */

import { useLayoutEffect, useRef, useState } from "react";

import { isSubtitlePlanPresent, readSubtitleProgress, revealedCharCount } from "../subtitlePlan";

export type SubtitleSource = "partial" | "final" | "reply" | "notice";

interface BigSubtitleProps {
  /** partial=ASR 中间结果（浅灰、斜体）；final=ASR 终稿（深字）；reply=数字人回复（主色）；notice=系统提示（次级灰）
      大字幕是全页最大色块，按约定不承载信号色，避免鲜色大面积铺开 */
  text: string;
  source: SubtitleSource;
}

const SOURCE_STYLE: Record<SubtitleSource, string> = {
  partial: "text-ink-subtle italic",
  final: "text-ink",
  reply: "text-brand",
  notice: "text-ink-muted",
};

interface RevealState {
  /** 当前句揭示到第几个字；null = 不逐字（整段显示） */
  count: number | null;
  /** 上文（最多两句，旧的在前）；没有上文时是空数组 */
  previous: readonly string[];
}

const FULL_TEXT: RevealState = { count: null, previous: [] };

/** 两次上文是不是同一批：rAF 里每帧都比一次，不能拿数组引用当判据。 */
function sameHistory(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((line, i) => line === b[i]);
}

/**
 * 逐字揭示的当前字数；`count === null` = 不逐字（整段显示）。
 *
 * 只在「字数真的变了」时 setState：60fps 的 rAF 里如果每帧都设一次，整棵 pad 树会跟着
 * 每帧重渲染（含 3D 视图）。字数最多每秒变十几次，代价可以接受。
 */
function useTypewriterReveal(text: string, source: SubtitleSource): RevealState {
  const [reveal, setReveal] = useState<RevealState>(FULL_TEXT);
  const shownRef = useRef<RevealState>(FULL_TEXT);

  // useLayoutEffect：重置到 0 字必须赶在浏览器 paint 之前，否则「整句先闪一帧」。
  useLayoutEffect(() => {
    // 逐字只给「数字人说的话」：语音轮是 reply，打字轮可能是 reply（正常回复）
    // 也可能是 notice（命中安全规则时的告警话术，2026-11-11 起也逐字）。
    // ASR 中间稿/终稿是别人说的话，逐字既不像对话、又会让已听到的内容看起来在卡顿。
    const typewriterSource = source === "reply" || source === "notice";
    if (!typewriterSource || !text) {
      shownRef.current = FULL_TEXT;
      setReveal(FULL_TEXT);
      return;
    }

    // 首帧：这一句有播放计划就从 0 个字开始打。若按「整段显示」渲染首帧，
    // 长者会先看到整句、下一帧再缩回去从头打（实测字符数 21→22→13→14）。
    // 没有计划的句子（旧服务端 / TTS 兜底文本）仍整段显示，不能空屏。
    if (isSubtitlePlanPresent()) {
      const start: RevealState = { count: 0, previous: [] };
      shownRef.current = start;
      setReveal(start);
    }

    let raf = 0;
    let alive = true;

    const tick = () => {
      if (!alive) return;
      const progress = readSubtitleProgress();
      // 进度里的 text 必须与当前句一致：换句的那一帧两者可能差一拍，
      // 拿旧句的进度去切新句会显示错位的文字。
      //
      // 有计划但**还没有进度**（音频尚未出声、锚点未定）时不能退回整段显示：
      // 那会让这一句先整句亮一帧、等首帧能量到了再缩回去重打（实测 `21,22,12…`）。
      // 这段空窗期保持当前字数不动（首帧已被重置成 0，所以就是「空着等」）。
      const next: RevealState =
        progress && progress.text === text
          ? {
              // 护栏（2026-11-11）：reveal 只许增不许减。重锚、跳帧、进度晚到都可能让
              // 算出来的字数比已上屏的少，直接照抄就会「往回缩 2 个字」。这里取大值：
              // 出现倒退时原地停住，等进度追上来再继续（宁可停一瞬，也不许倒着打）。
              count: Math.max(
                shownRef.current.count ?? 0,
                revealedCharCount(
                  progress.text,
                  progress.elapsedMs,
                  progress.durationMs,
                  progress.charMs,
                ),
              ),
              previous: progress.previousTexts,
            }
          : isSubtitlePlanPresent()
            ? { count: shownRef.current.count ?? 0, previous: shownRef.current.previous }
            : FULL_TEXT;
      if (
        next.count !== shownRef.current.count ||
        !sameHistory(next.previous, shownRef.current.previous)
      ) {
        shownRef.current = next;
        setReveal(next);
      }
      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
    };
  }, [text, source]);

  return reveal;
}

/** 大字幕：FR-UI-02「语音回答同步显示大字幕」；正文字号 ≥20px，此处为大字号长者可读规格 */
export function BigSubtitle({ text, source }: BigSubtitleProps) {
  const reveal = useTypewriterReveal(text, source);
  const scrollRef = useRef<HTMLDivElement>(null);

  const shown =
    reveal.count === null ? text : Array.from(text).slice(0, reveal.count).join("");
  const charCount = Array.from(text).length;

  // 正在说的字必须始终看得见：长句在滚动区里往下长，不贴底就会「字在屏幕外继续打」。
  // 用 layout effect 赶在 paint 前贴底，否则长者会看到一帧跳动。
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [shown]);

  if (!text) return null;

  return (
    <div className="flex min-h-0 w-full max-w-[var(--pad-subtitle-max)] flex-1 flex-col">
      {reveal.previous.length > 0 && (
        <div
          data-testid="subtitle-history"
          className="shrink-0 space-y-2 border-b border-line pb-4 text-left"
        >
          {reveal.previous.map((line, i) => (
            <p
              key={`${i}-${line}`}
              className="line-clamp-2 font-normal text-ink-subtle"
              style={{ fontSize: "var(--pad-subtitle-prev)" }}
            >
              {line}
            </p>
          ))}
        </div>
      )}
      {/* 当前句**不截断**（2026-11-11 版面重排）：字幕从底部三行横带改成右栏整列，
          高度由版面给而不是由行数给，长回复改成在区内滚动 + 自动贴底。
          旧的 line-clamp 会把正在说的后半句直接裁掉——长者读到的永远是半句话。
          `my-auto`：句子短时垂直居中，长到超出时自动 margin 归零、正常滚动
          （用 justify-center 会在溢出时把开头裁到滚不到的地方）。

          字号仍按长度分三档：短句用最大字，长回复降一档，保证 2 米外可读。 */}
      <div
        ref={scrollRef}
        data-testid="subtitle-scroll"
        className="pad-transcript pointer-events-auto flex min-h-0 flex-1 flex-col overflow-y-auto"
      >
        <p
          data-testid="subtitle-text"
          className={`my-auto font-bold leading-snug ${SOURCE_STYLE[source]}`}
          style={{
            fontSize:
              charCount > 48
                ? "var(--pad-subtitle-sm)"
                : charCount > 20
                  ? "var(--pad-subtitle-md)"
                  : "var(--pad-subtitle-lg)",
          }}
        >
          {shown}
        </p>
      </div>
    </div>
  );
}
