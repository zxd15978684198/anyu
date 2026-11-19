import type { ReactNode } from "react";

const DEFAULT_MAX_HEIGHT = "min(92vh, calc(100dvh - 48px), var(--pad-drawer-maxh))";

/**
 * 任务面板通用外壳：主对话屏中部右侧的任务区。
 *
 * 布局口径：任务来时不整屏切页，顶栏状态条与底部大字幕保持在场
 * （S3「语音 + 字幕 + 按钮同时呈现」、FR-UI-02 字幕与音频一致）。
 * 高度固定，内容超长时只让正文区滚动，标题与底部动作永远可见。
 *
 * `bodyFit`（2026-11-11 产品负责人实测后用）：用药卡片打开时同时出现了外层滚动与内层滚动
 * 两条滚动条。根因是正文区默认 `overflow-y-auto`，而用药卡片的内容比可视区高出约 40px；
 * 且它一屏本就放得下，只是间距太松。用药卡片传 `bodyFit` 后正文区改成 `overflow-hidden`，
 * 配合内部收紧间距，把「两屏才看得见『已服用 / 这次不吃』」变成一屏。
 *
 * 高度口径（2026-11-11 浮层内容显示不全）：标题与底部动作区都在正文区之外（`shrink-0`），
 * 只有正文区滚动 —— 所以正文再长也滚不跑按钮。外壳本身封到
 * `min(92vh, calc(100dvh - 48px), var(--pad-drawer-maxh))`：
 * 前两项是矮屏适配的口径（`dvh` 跟移动端地址栏收放走，`-48px` 留出上下呼吸位），
 * 第三项保住大屏上的原尺寸（D 档仍是 660px，不因为这次修矮屏把桌面撑满）。
 */
interface TaskShellProps {
  eyebrow: string;
  title: string;
  /** 信号色的唯一入口：StatusBadge，不铺面 */
  badge?: ReactNode;
  progress?: ReactNode;
  exitLabel?: string;
  onExit?: () => void;
  /** 正文区是否改为「一屏放下、不滚」（默认仍允许滚动，其它任务不受影响） */
  bodyFit?: boolean;
  /**
   * 高度口径覆盖（默认仍走上面的三档 min()）。
   * 只给视口级浮层用：声纹录入层不在 `main` 里，可用高度是整屏而不是中部区，
   * 在 1536×703（1920 屏 125% 缩放）这类矮屏上需要比抽屉的 600px 多要一点，
   * 否则正文区会被标题/进度/页脚压到 0。
   */
  maxHeight?: string;
  children: ReactNode;
  footer?: ReactNode;
}

export function TaskShell({
  eyebrow,
  title,
  badge,
  progress,
  exitLabel = "退出任务",
  onExit,
  bodyFit = false,
  maxHeight = DEFAULT_MAX_HEIGHT,
  children,
  footer,
}: TaskShellProps) {
  return (
    <section
      data-testid="task-shell"
      style={{ maxHeight }}
      className="flex h-full w-[var(--pad-drawer-w)] max-w-[var(--pad-drawer-maxw)] shrink-0 flex-col overflow-hidden rounded-2xl border border-line bg-surface p-[var(--pad-drawer-pad)]"
    >
      <header className="flex shrink-0 items-start justify-between gap-8">
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-4">
            <span className="text-xl font-semibold text-ink-muted">{eyebrow}</span>
            {badge}
          </div>
          <h2
            className="font-bold leading-tight text-ink"
            style={{ fontSize: "var(--pad-drawer-title)" }}
          >
            {title}
          </h2>
        </div>
        {onExit && (
          <button
            type="button"
            onClick={onExit}
            className="shrink-0 rounded-2xl border border-line bg-surface px-6 py-4 text-xl text-ink-muted active:bg-surface-sunken"
          >
            {exitLabel}
          </button>
        )}
      </header>

      {progress && <div className="mt-6 shrink-0">{progress}</div>}

      <div
        data-testid="task-shell-body"
        data-fit={bodyFit ? "true" : "false"}
        className={`mt-6 flex min-h-0 flex-1 flex-col ${bodyFit ? "overflow-hidden" : "overflow-y-auto"}`}
      >
        {children}
      </div>

      {footer && (
        <div
          data-testid="task-shell-footer"
          className="mt-6 shrink-0 border-t border-line pt-6"
        >
          {footer}
        </div>
      )}
    </section>
  );
}
