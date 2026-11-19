/** 空态 / 加载 / 错误条 / 状态徽标（W1-4）。四件套在所有页面复用，文案必须可读、不暴露内部细节。 */
import type { ReactNode } from "react";

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state" role="status">
      <p className="empty-title">{title}</p>
      {description ? <p className="empty-desc">{description}</p> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function LoadingState({ text = "正在加载…" }: { text?: string }) {
  return (
    <div className="loading-state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{text}</span>
    </div>
  );
}

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="alert-danger banner" role="alert">
      <span>{message}</span>
      {onRetry ? (
        <button type="button" className="btn-link" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

/** 语义色单一真源在 shared/tone（W4）；此处再导出保持既有 import 路径不变。 */
import type { Tone as Tone } from "../../shared/tone";
export type { Tone };

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/** 长者状态 → 徽标（E8 的 ElderStatus 闭集，别处不要另造文案）。 */
const ELDER_STATUS: Record<string, { tone: Tone; text: string }> = {
  active: { tone: "success", text: "在住" },
  away: { tone: "warning", text: "外出" },
  transferred: { tone: "info", text: "转出" },
  archived: { tone: "neutral", text: "归档" },
};

export function ElderStatusBadge({ status }: { status: string }) {
  const item = ELDER_STATUS[status] ?? { tone: "neutral" as Tone, text: status };
  return <Badge tone={item.tone}>{item.text}</Badge>;
}

/** 账号状态（员工/家属共用 active / disabled）。 */
export function AccountStatusBadge({ status }: { status: string }) {
  return (
    <Badge tone={status === "active" ? "success" : "neutral"}>
      {status === "active" ? "启用" : status === "disabled" ? "停用" : status}
    </Badge>
  );
}
