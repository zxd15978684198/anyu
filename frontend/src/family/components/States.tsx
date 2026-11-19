/**
 * 家属端通用状态块（W4）。
 *
 * 验收口径：**任何异步都不能留空白页** ——
 * 加载中 → 骨架屏 + 读屏可播报的「加载中…」；
 * 失败 → 网络错一律渲染成整块按钮「网络不太好，点这里重试」（可直接点），
 *        权限错显示「没有权限查看」，其余按状态码给固定文案；
 * 空 → 标题 + 一句解释，说明「为什么这里没东西」。
 */
import { errorText, isNetworkError } from "../http";
import { DocIcon } from "./Icons";

const NETWORK_RETRY_TEXT = "网络不太好，点这里重试";

export function FullScreenLoading({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="state-center" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <p className="state-text">{label}</p>
    </div>
  );
}

/** 骨架屏：形状贴近日报卡片，避免加载完成时页面跳动。 */
export function ListSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="skeleton-list" role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">加载中…</span>
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton-card" key={index} aria-hidden="true">
          <span className="skeleton-bar skeleton-bar-sm" />
          <span className="skeleton-bar skeleton-bar-lg" />
          <span className="skeleton-bar skeleton-bar-md" />
        </div>
      ))}
    </div>
  );
}

interface ErrorBlockProps {
  error: unknown;
  /** 提供则渲染成可点的整块按钮；不提供则退化成纯提示。 */
  onRetry?: () => void;
}

export function ErrorBlock({ error, onRetry }: ErrorBlockProps) {
  const network = isNetworkError(error);
  const text = network ? NETWORK_RETRY_TEXT : errorText(error);

  if (onRetry) {
    return (
      <button type="button" className="retry-block" onClick={onRetry}>
        <span className="retry-text">{text}</span>
        {!network && <span className="retry-hint">点一下重新获取</span>}
      </button>
    );
  }

  return (
    <div className="alert-danger" role="alert">
      {text}
    </div>
  );
}

interface InlineEmptyProps {
  title: string;
  hint?: string;
}

export function InlineEmpty({ title, hint }: InlineEmptyProps) {
  return (
    <div className="empty-box">
      <span className="empty-icon" aria-hidden="true">
        <DocIcon />
      </span>
      <p className="empty-title">{title}</p>
      {hint && <p className="empty-hint">{hint}</p>}
    </div>
  );
}
