/**
 * 长者端底部「两级降级入口」的布局组件（按住说话｜打字输入）。
 *
 * 两栏不是等宽，而是与中部同轴（2026-11-16 产品负责人要求）：
 *   左栏 = 3D 舞台列，按住说话在列内居中 → 与数字人同一条垂线；
 *   右栏 = 字幕栏宽（`--pad-subtitle-w`）并留同样的右槽 → 打字框与字幕卡等宽同轴。
 * 具体分栏写在 `index.css` 的 `.pad-entry-grid`，窄屏堆叠档退回等宽两栏。
 *
 * 提示文字由调用方从 `resolveFallbackState()` 取（本组件不编文案、不判断连接态）；
 * 挂载点是 `App.tsx` 的 footer，替换原来那段 inline 的 `data-testid="fallback-entries"` 块。
 */

import { useRef, type RefObject } from "react";
import { HoldToTalk } from "./HoldToTalk";
import type { SubtitleSource } from "./BigSubtitle";
import { TextFallback } from "./TextFallback";

export interface FallbackEntriesProps {
  talkHint: string;
  token: string;
  textHint: string;
  textEnabled: boolean;
  onSubmit?: (text: string) => Promise<void> | void;
  onSubtitle: (text: string, source: SubtitleSource) => void;
  onAuthFail: () => void;
  /**
   * 额外的类（2026-11-11 新增，2026-11-16 改为语义类）：任务抽屉在窄屏打开时底栏
   * 只剩 ~384px，两栏并排会把输入框压到 108px（placeholder 折行）。调用方传
   * `pad-entry-stack` 让 T/P 档改成上下两行，D 档仍保持与中部同轴的两栏。
   */
  className?: string;
}

export function FallbackEntries({
  talkHint,
  token,
  textHint,
  textEnabled,
  onSubmit,
  onSubtitle,
  onAuthFail,
  className,
}: FallbackEntriesProps) {
  const textRef: RefObject<HTMLTextAreaElement | null> = useRef<HTMLTextAreaElement>(null);

  /** 麦克风起不来时把长者送到打字框：聚焦 + 滚进视口（foot 部在屏底，必要时才滚）。 */
  const focusText = () => {
    const field = textRef.current;
    if (!field) return;
    field.focus();
    field.scrollIntoView?.({ block: "nearest" });
  };

  return (
    <div
      data-testid="fallback-entries"
      className={`pad-entry-grid w-full items-start ${className ?? ""}`}
    >
      <div data-testid="fallback-talk" className="pad-entry-talk flex min-w-0 flex-col gap-2">
        <p data-testid="fallback-talk-hint" className="text-2xl leading-snug text-ink-muted">
          {talkHint}
        </p>
        <HoldToTalk
          className="w-full"
          token={token}
          onSubtitle={onSubtitle}
          onAuthFail={onAuthFail}
          onAskForText={focusText}
        />
      </div>

      <div data-testid="fallback-text" className="pad-entry-text flex min-w-0 flex-col gap-2">
        <p data-testid="fallback-text-hint" className="text-2xl leading-snug text-ink-muted">
          {textHint}
        </p>
        <TextFallback
          showHint={false}
          hint={textHint}
          enabled={textEnabled}
          onSubmit={onSubmit}
          inputRef={textRef}
        />
      </div>
    </div>
  );
}
