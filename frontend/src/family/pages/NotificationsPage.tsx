/**
 * 通知中心（W4 §4）。
 *
 * 家属对通知**只读**：`POST /{id}/deliver`、`/{id}/ack` 对家属是 403，
 * 所以这里没有「确认」「已读」按钮 —— 做了就是死按钮。
 *
 * 服务端支持 `kind` 与 `severity` 过滤，**严重度筛选下推到服务端**：
 * 早先在客户端只筛已加载的那一页，红档落在后续页时会显示成「暂时没有需要您关注的事项」，
 * 而「加载更多」还亮着——把告警说成不存在。下推后 `total` 即筛选后总数，判据自然正确。
 * 摘要是「取服务端写好的那句 summary」，取不到就退回类型名，不自己下医学结论。
 */
import { useEffect, useState } from "react";
import {
  SEVERITY_FILTERS,
  SEVERITY_LABEL,
  kindLabel,
  listNotifications,
  payloadSummary,
  severityOf,
  timeOf,
  type NotificationItem,
  type SeverityFilter,
} from "../notifications";
import { SeverityDot } from "../components/SeverityDot";
import { ErrorBlock, InlineEmpty, ListSkeleton } from "../components/States";

const PAGE_SIZE = 20;

export function NotificationsPage() {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const [filter, setFilter] = useState<SeverityFilter>("all");

  useEffect(() => {
    let alive = true;
    const first = page === 1;
    if (first) {
      setLoading(true);
      setError(null);
    }
    listNotifications({
      page,
      pageSize: PAGE_SIZE,
      severity: filter === "all" ? "" : filter,
    })
      .then((result) => {
        if (!alive) return;
        setError(null);
        setItems((prev) => {
          // 翻页时按 id 去重：期间若有新通知插到前面，简单追加会出现重复行
          const base = first ? [] : prev;
          const seen = new Set(base.map((entry) => entry.notification_id));
          return [...base, ...result.items.filter((entry) => !seen.has(entry.notification_id))];
        });
        setTotal(result.total);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err);
        if (first) setItems([]);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [page, attempt, filter]);

  // total 已是「筛选后」的总数（服务端在分页前过滤），所以这两个判据不需要再考虑筛选。
  const hasMore = !loading && !error && items.length < total;

  function changeFilter(next: SeverityFilter) {
    if (next === filter) return;
    setFilter(next);
    setPage(1);
  }

  function retry() {
    setPage(1);
    setAttempt((value) => value + 1);
  }

  return (
    <div className="page">
      <div className="filter-row" role="group" aria-label="按严重程度筛选">
        {SEVERITY_FILTERS.map((option) => {
          const selected = option.value === filter;
          return (
            <button
              key={option.value}
              type="button"
              className={selected ? "chip is-active" : "chip"}
              aria-pressed={selected}
              onClick={() => changeFilter(option.value)}
            >
              {option.label}
            </button>
          );
        })}
      </div>

      {loading && items.length === 0 ? (
        <ListSkeleton rows={4} />
      ) : error && items.length === 0 ? (
        <ErrorBlock error={error} onRetry={retry} />
      ) : items.length === 0 ? (
        <InlineEmpty
          title="暂时没有需要您关注的事项"
          hint={filter === "all" ? "家里有新的提醒时，会在这里通知您。" : "可以切换到「全部」看看其它提醒。"}
        />
      ) : (
        <ul className="notif-list">
          {items.map((entry) => {
            const severity = severityOf(entry);
            const kind = kindLabel(entry.kind);
            const summary = payloadSummary(entry);
            return (
              <li key={entry.notification_id} className="notif-item">
                <div className="notif-top">
                  <SeverityDot tone={severity} label={SEVERITY_LABEL[severity]} />
                  <span className="notif-time">{timeOf(entry)}</span>
                </div>
                <p className="notif-kind">{kind}</p>
                {/* payload 里没有可读摘要时 payloadSummary 会退回类型名 —— 那就别再重复一遍 */}
                {summary !== kind && <p className="notif-summary">{summary}</p>}
              </li>
            );
          })}
        </ul>
      )}

      {/* 翻页失败：列表还在，所以不能整块替换掉，只在底部提示并允许重试当前页 */}
      {error != null && items.length > 0 && (
        <ErrorBlock error={error} onRetry={() => setAttempt((value) => value + 1)} />
      )}

      {hasMore && (
        <button type="button" className="btn-secondary load-more" onClick={() => setPage((value) => value + 1)}>
          加载更多
        </button>
      )}

      {loading && items.length > 0 && <p className="muted-note">加载中…</p>}
    </div>
  );
}
