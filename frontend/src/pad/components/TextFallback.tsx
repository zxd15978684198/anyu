/**
 * 长者端第二级降级：页面打字输入（AGENTS §6「按住说话 → 再降级页面输入框」）。
 *
 * 只用一次用户手势就完成一次提交，不做多步表单：输入框 + 发送两件东西，字号按长者端口径 28px。
 *
 * 关于「提交去哪」——`onSubmit` 是**注入**的，不是这里自己拼 URL：
 * 后端已补 `POST /api/dialogue/text`，由 App 传 `submitTextTurn` 进来；本组件不认地址，
 * 将来换端点也只改 App。
 *
 * 交互口径（2026-11-11 产品负责人实测后重做）：
 *  - 输入框是 `<textarea>`：Enter 发送、Shift+Enter 换行（长者打错字要学会换行，而不是被吞）；
 *    中文输入法组合中的 Enter 不算发送（`isComposing`），否则选字一按就把半句话发出去。
 *  - 提交成功后**清空并留住焦点**：长者接着打第二句时不该再点一次输入框。
 *  - 空输入时发送按钮不是「灰掉就算」，而是换成虚线描边的空态：大字幕场景下低对比度
 *    半透明会被当成「按钮坏了」。
 */

import { useRef, useState, type RefObject } from "react";
import { MAX_TEXT_LENGTH, TEXT_ENTRY_LABEL } from "./fallbackState";

export interface TextFallbackProps {
  /** 「什么时候用它」：由 `resolveFallbackState().text.hint` 给，组件不自己编文案。 */
  hint: string;
  /** 后端纯文本入口是否已接通。false 时只显示入口与原因，不假装能发。 */
  enabled: boolean;
  /** 提交函数（接后端文本入口后由 App 注入）；缺省视为未接通。 */
  onSubmit?: (text: string) => Promise<void> | void;
  /** 外层自己渲染提示文字时传 false（两栏对齐用，避免同一句提示出现两次） */
  showHint?: boolean;
  /** 外部需要「把光标送进来」时传（例如按住说话不可用，引导长者改打字） */
  inputRef?: RefObject<HTMLTextAreaElement | null>;
}

export function TextFallback({
  hint,
  enabled,
  onSubmit,
  showHint = true,
  inputRef,
}: TextFallbackProps) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const localRef = useRef<HTMLTextAreaElement>(null);
  const fieldRef = inputRef ?? localRef;

  const wired = typeof onSubmit === "function";
  const usable = enabled && wired;
  const trimmed = draft.trim();
  const canSend = usable && !busy && trimmed.length > 0 && trimmed.length <= MAX_TEXT_LENGTH;

  async function handleSend(): Promise<void> {
    if (!canSend || !onSubmit) return;
    setBusy(true);
    setProblem(null);
    try {
      await onSubmit(trimmed);
      setDraft("");
    } catch {
      setProblem("没发出去，请再按一次");
    } finally {
      setBusy(false);
      // 无论成败都把光标放回输入框：接着改字/重发都不用再点一次
      fieldRef.current?.focus();
    }
  }

  return (
    <form
      data-testid="text-fallback"
      data-usable={usable ? "true" : "false"}
      className="w-full"
      onSubmit={(event) => {
        event.preventDefault();
        void handleSend();
      }}
    >
      {showHint && (
        <p data-testid="text-fallback-hint" className="text-2xl text-ink-muted">
          {hint}
        </p>
      )}
      <div className={`${showHint ? "mt-2" : ""} flex items-stretch gap-3`}>
        <textarea
          ref={fieldRef}
          data-testid="text-fallback-input"
          aria-label={TEXT_ENTRY_LABEL}
          value={draft}
          rows={2}
          onChange={(event) => {
            setDraft(event.target.value);
            setProblem(null);
          }}
          onKeyDown={(event) => {
            // 中文输入法组合中：Enter 是选字，不是发送
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void handleSend();
            }
          }}
          maxLength={MAX_TEXT_LENGTH}
          placeholder="在这里打字…"
          disabled={!usable || busy}
          // 定高 80px 而不是让 rows=2 自己撑（2026-11-11 底栏压高）：
          // 28px 字 + py-3 + leading-snug 会撑到 105px，整条底栏被顶到 206。
          // 26px / leading-tight / py-1.5 下两行仍是 77px，装得下，长者仍看得见两行。
          className="h-20 min-h-14 min-w-0 flex-1 resize-none rounded-2xl border-2 border-line bg-surface px-5 py-1.5 text-[26px] leading-tight text-ink placeholder:text-ink-subtle disabled:bg-neutral-soft disabled:text-ink-subtle"
        />
        <button
          type="submit"
          data-testid="text-fallback-send"
          data-sendable={canSend ? "true" : "false"}
          disabled={!canSend}
          className={`pad-send-btn min-h-14 min-w-32 rounded-2xl px-8 py-3 text-3xl font-bold transition-colors duration-150 ${
            canSend
              ? "bg-brand text-ink-inverse shadow-lg active:bg-brand-strong"
              : "border-2 border-dashed border-line bg-surface text-ink-subtle"
          }`}
        >
          {busy ? "发送中…" : "发送"}
        </button>
      </div>
      {/* 错误行常驻占位（不报错时是空的）：不留位的话报错瞬间整条底栏会长高、
          数字人层跟着跳。高度从 36 收到 28，仍放得下 20px 的错误文案。 */}
      <p role="status" className="mt-1 h-6 text-xl leading-none text-danger-ink">
        {problem ?? ""}
      </p>
    </form>
  );
}
