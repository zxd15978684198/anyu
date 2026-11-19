/**
 * 操作日志页（FR-ADM-05，2026-11-12）。
 *
 * ## 这个页面解决什么
 * 服务端从 迭代 5 起就把关键动作写进 `audit_logs`，但一直只有 API 没有界面：
 * 管理员要看「谁在什么时候改了哪个长者的什么」，只能自己 curl。这里把它做成
 * 一张可筛选、可翻页、可导出的表。
 *
 * ## 权限口径
 * 两个端点都是 **admin only**（`app/api/audit_export.py:73`）。前端这一层不影响权限：
 * 非管理员即使手动改 hash 进来，服务端也会 403，而且**这次越权访问本身会被写进审计**
 * （`_require_admin` 先留痕再抛错）。所以 403 不当作错误藏起来，而是如实说明
 * 「审计日志仅管理员可读」，并把这条事实告诉人。
 *
 * ## 为什么不自动轮询
 * 审计是事后追查用的，不是实时看板。自动刷新只会在人正在读某一行时把表格换掉，
 * 反而找不到刚才那行。要新数据就点「查询」。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Principal } from "../api";
import {
  AUDIT_ACTOR_TYPES,
  AUDIT_DEFAULT_LIMIT,
  AUDIT_MAX_EXPORT_ROWS,
  AUDIT_MAX_LIMIT,
  actorTypeLabel,
  detailSummary,
  emptyFilters,
  exportAuditLogs,
  formatAuditTime,
  listAuditLogs,
  saveAuditExport,
  shortId,
  type AuditFilters,
  type AuditLogItem,
} from "../audit";
import { HttpError, errorText } from "../http";
import { isAdmin } from "../permissions";
import { EmptyState, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { Field, FormActions, FormGrid, Select } from "../ui/Form";

const PAGE_SIZES = [AUDIT_DEFAULT_LIMIT, 100, AUDIT_MAX_LIMIT].map((size) => ({
  value: String(size),
  label: `${size} 条/页`,
}));

const FORBIDDEN_TEXT =
  "审计日志只对管理员开放。已拦截本次访问，并把它作为一次越权尝试记进审计。";

export function AuditPage({ principal }: { principal: Principal }) {
  const [draft, setDraft] = useState<AuditFilters>(emptyFilters);
  const [applied, setApplied] = useState<AuditFilters>(emptyFilters);
  const [limit, setLimit] = useState(AUDIT_DEFAULT_LIMIT);
  const [offset, setOffset] = useState(0);

  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ text: string; forbidden: boolean } | null>(null);

  const [exporting, setExporting] = useState(false);
  const [exportNote, setExportNote] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async (filters: AuditFilters, pageLimit: number, pageOffset: number) => {
    setLoading(true);
    setError(null);
    try {
      const data = await listAuditLogs(filters, { limit: pageLimit, offset: pageOffset });
      if (!mounted.current) return;
      setItems(data.items);
      setTotal(data.total);
    } catch (err) {
      if (!mounted.current) return;
      setItems([]);
      setTotal(0);
      const forbidden = err instanceof HttpError && err.status === 403;
      setError({ text: forbidden ? FORBIDDEN_TEXT : errorText(err), forbidden });
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  // 每次「生效条件」变化都重新取一页；首次进来也会跑一次。
  useEffect(() => {
    void load(applied, limit, offset);
  }, [applied, limit, offset, load]);

  function onQuery() {
    setOffset(0);
    setApplied({ ...draft });
  }

  function onReset() {
    setDraft(emptyFilters());
    setOffset(0);
    setApplied(emptyFilters());
  }

  async function onExport() {
    setExporting(true);
    setExportError(null);
    setExportNote(null);
    try {
      const file = await exportAuditLogs(applied);
      saveAuditExport(file);
      setExportNote(
        `已导出 ${file.filename}（最多 ${AUDIT_MAX_EXPORT_ROWS} 行，超过的部分不在文件里）。`,
      );
    } catch (err) {
      setExportError(errorText(err));
    } finally {
      if (mounted.current) setExporting(false);
    }
  }

  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + items.length, total);
  const hasPrev = offset > 0;
  const hasNext = offset + limit < total;
  const canExport = isAdmin(principal) && !exporting;

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1 className="page-title">操作日志</h1>
          <p className="page-sub">
            谁在什么时候改了什么，都能在这里查到。按主体、动作、操作者与时间段筛选；
            导出为 NDJSON，一行一条记录。
          </p>
        </div>
        <div className="page-actions">
          <button type="button" className="btn-secondary" onClick={onExport} disabled={!canExport}>
            {exporting ? "正在导出…" : "导出当前筛选结果"}
          </button>
        </div>
      </header>

      <div className="card">
        <FormGrid>
          <Field label="主体种类" hint="员工 / 家属 / 系统 / 设备">
            <Select
              value={draft.actorType}
              onChange={(value) => setDraft((cur) => ({ ...cur, actorType: value }))}
              options={AUDIT_ACTOR_TYPES.map((item) => ({ value: item.value, label: item.label }))}
            />
          </Field>
          <Field label="动作" hint="精确匹配，如 elder.update、work_order.transition、audit.export">
            <input
              className="input"
              value={draft.action}
              onChange={(event) => {
                const value = event.currentTarget.value;
                setDraft((cur) => ({ ...cur, action: value }));
              }}
              placeholder="audit.export"
            />
          </Field>
          <Field label="操作者主键" hint="UUID；留空表示不限操作者">
            <input
              className="input"
              value={draft.actorId}
              onChange={(event) => {
                const value = event.currentTarget.value;
                setDraft((cur) => ({ ...cur, actorId: value }));
              }}
              placeholder="00000000-0000-0000-0000-000000000000"
            />
          </Field>
          <Field label="起始时间" hint="含该时刻；按本机时区换算成绝对时间提交">
            <input
              className="input"
              type="datetime-local"
              value={draft.start}
              onChange={(event) => {
                const value = event.currentTarget.value;
                setDraft((cur) => ({ ...cur, start: value }));
              }}
            />
          </Field>
          <Field label="截止时间" hint="含该时刻">
            <input
              className="input"
              type="datetime-local"
              value={draft.end}
              onChange={(event) => {
                const value = event.currentTarget.value;
                setDraft((cur) => ({ ...cur, end: value }));
              }}
            />
          </Field>
          <Field label="每页条数" hint={`服务端上限 ${AUDIT_MAX_LIMIT} 条`}>
            <Select
              value={String(limit)}
              onChange={(value) => {
                setLimit(Number(value));
                setOffset(0);
              }}
              options={PAGE_SIZES}
            />
          </Field>
        </FormGrid>
        <FormActions>
          <button type="button" className="btn-secondary" onClick={onQuery} disabled={loading}>
            {loading ? "正在查询…" : "查询"}
          </button>
          <button type="button" className="btn-secondary" onClick={onReset}>
            清空条件
          </button>
        </FormActions>
        <p className="field-hint">
          导出最多 {AUDIT_MAX_EXPORT_ROWS} 行，超出部分不会进文件；手机号等字段在导出前已脱敏。
          每次导出都会留一条<code>audit.export</code> 审计（含本次筛选条件与行数）。
        </p>
      </div>

      {exportError ? <ErrorBanner message={exportError} /> : null}
      {exportNote ? <Notice>{exportNote}</Notice> : null}

      {error ? <ErrorBanner message={error.text} onRetry={() => void load(applied, limit, offset)} /> : null}

      {loading && items.length === 0 ? <LoadingState text="正在读取审计日志…" /> : null}

      {!loading && !error && items.length === 0 ? (
        <EmptyState
          title="没有符合条件记录"
          description="换个动作名或放宽时间段试试；也可以先清空条件看全量。"
        />
      ) : null}

      {items.length > 0 ? (
        <div className="card">
          <div className="toolbar">
            <span>
              共 <strong>{total}</strong> 条，本页第 {pageStart}–{pageEnd} 条
            </span>
            <div className="page-actions">
              <button
                type="button"
                className="btn-secondary"
                disabled={!hasPrev || loading}
                onClick={() => setOffset(Math.max(0, offset - limit))}
              >
                上一页
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={!hasNext || loading}
                onClick={() => setOffset(offset + limit)}
              >
                下一页
              </button>
            </div>
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>主体</th>
                  <th>操作者</th>
                  <th>动作</th>
                  <th>对象</th>
                  <th>摘要</th>
                  <th>trace_id</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.audit_id}>
                    <td>{formatAuditTime(item.created_at)}</td>
                    <td>{actorTypeLabel(item.actor_type)}</td>
                    <td title={item.actor_id}>{shortId(item.actor_id)}</td>
                    <td>
                      <code>{item.action}</code>
                    </td>
                    <td>
                      {item.resource_type}
                      <span className="cell-muted"> · </span>
                      <span title={item.resource_id}>{shortId(item.resource_id)}</span>
                    </td>
                    <td className="cell-muted" title={item.detail ? JSON.stringify(item.detail) : ""}>
                      {detailSummary(item.detail)}
                    </td>
                    <td className="cell-muted" title={item.trace_id ?? ""}>
                      {item.trace_id ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </section>
  );
}
