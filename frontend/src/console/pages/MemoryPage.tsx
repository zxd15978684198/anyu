/**
 * 照护台「长者记忆」页（task-40 / FR-MEM-02 查看 / 纠正 / 删除）。
 *
 * 契约唯一依据：`docs/接口契约_记忆.md` v1.1 §5（M1/M2/M3，**admin only**）。
 * 长者本人视图（M4–M6 `/api/memory/me/chunks`）只认设备主体，是 pad 侧能力，本页不用。
 *
 * 本页的三条硬口径（写错了就是缺陷，不是文案问题）：
 * 1. **`total` 是当前页条数**，不是全量。所以这里**不用** `ui/Pagination`（它按 `total/pageSize`
 *    算页数并渲染「共 N 条」），翻页只靠 `offset` 与 `mayHaveNextPage(本页条数, limit)`。
 * 2. **`DELETE` 是长者级软删除、非幂等操作**：第二次调用仍 200 但 `deleted_chunks=0` 且再写一条审计。
 *    页面用「二次确认 → 乐观清空 → 失败回滚」的写法，并把 0 与 N 显示成两句不同的话。
 * 3. **5xx / 403 / 404 不许画成「暂无记忆」**：错误只进 `error` 分支（`DataTable` 的 `error` 优先于
 *    empty），`emptyTitle` 只在真的拿到空数组时才出现。
 *
 * 不新增 fetch 封装：三个端点都在 `../memory`，走 `../http` 的 `apiGet` / `apiSend`。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Principal } from "../api";
import { listElders } from "../elders";
import type { ElderListItem } from "../elders";
import { elderScopeNote, isAdmin } from "../permissions";
import {
  CATEGORY_OPTIONS,
  MEMORY_CONTENT_MAX,
  MEMORY_DELETE_REASON,
  MEMORY_PAGE_SIZE,
  PAGE_TOTAL_HINT,
  SOURCE_HINT,
  SOURCE_OPTIONS,
  STATUS_OPTIONS,
  categoryLabel,
  contentPreview,
  createMemoryChunk,
  deleteElderMemory,
  deleteOutcomeText,
  formatTimestamp,
  importanceText,
  listMemoryChunks,
  listStateOf,
  mayHaveNextPage,
  memoryErrorText,
  pageNote,
  pageRangeText,
  previewImportance,
  sourceLabel,
  statusLabel,
  statusTone,
  validateChunkDraft,
} from "../memory";
import type { MemoryCategory, MemoryChunk, MemorySource, MemoryStatus } from "../memory";
import { Badge, EmptyState, ErrorBanner } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { FormActions, FormGrid, Field, Select } from "../ui/Form";
import { Dialog } from "../ui/Dialog";
import { DataTable } from "../ui/Table";
import type { Column } from "../ui/Table";

/** 记住上一次写入服务的响应，用来显示「落库重要度 / 向量来源」。 */
interface WriteReceipt {
  importance: number;
  provider: string;
  semantic: boolean;
  source: MemorySource;
}

export function MemoryPage({ principal }: { principal: Principal }) {
  const admin = isAdmin(principal);
  const scopeNote = elderScopeNote(principal);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // 长者选择
  const [elders, setElders] = useState<ElderListItem[]>([]);
  const [eldersLoading, setEldersLoading] = useState(true);
  const [eldersError, setEldersError] = useState<string | null>(null);
  const [elderId, setElderId] = useState("");

  // 列表
  const [status, setStatus] = useState<MemoryStatus | "">("active");
  const [offset, setOffset] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [chunks, setChunks] = useState<MemoryChunk[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  // 写入表单
  const [draftContent, setDraftContent] = useState("");
  const [draftCategory, setDraftCategory] = useState<MemoryCategory>("private");
  const [draftSource, setDraftSource] = useState<MemorySource>("explicit");
  const [draftImportance, setDraftImportance] = useState("1");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [receipt, setReceipt] = useState<WriteReceipt | null>(null);

  // 删除
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteOutcome, setDeleteOutcome] = useState<number | null>(null);

  // 长者下拉：一次拉 100 条够用（照护台的长者数远小于这个量级）
  useEffect(() => {
    if (!admin) {
      setEldersLoading(false);
      return;
    }
    let active = true;
    setEldersLoading(true);
    setEldersError(null);
    void (async () => {
      try {
        const data = await listElders({ page: 1, pageSize: 100, status: "" });
        if (!active || !mounted.current) return;
        setElders(data.items);
        setElderId((current) => current || data.items[0]?.elder_id || "");
      } catch (err) {
        if (!active || !mounted.current) return;
        setEldersError(memoryErrorText(err));
      } finally {
        if (active && mounted.current) setEldersLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [admin]);

  // 列表：elderId / status / offset / reloadKey 任一变就重拉（切筛选一律回到第一页）
  const loadChunks = useCallback(async () => {
    if (!admin || !elderId) {
      setChunks([]);
      setListError(null);
      setListLoading(false);
      return;
    }
    setListLoading(true);
    setListError(null);
    try {
      const data = await listMemoryChunks({ elderId, status, limit: MEMORY_PAGE_SIZE, offset });
      if (!mounted.current) return;
      setChunks(data.items);
    } catch (err) {
      if (!mounted.current) return;
      // 失败时清空行：旧数据留在表里会让人以为「这次也读到了」
      setChunks([]);
      setListError(memoryErrorText(err));
    } finally {
      if (mounted.current) setListLoading(false);
    }
  }, [admin, elderId, offset, status]);

  useEffect(() => {
    void loadChunks();
  }, [loadChunks, reloadKey]);

  const onSubmit = useCallback(async () => {
    if (!elderId) {
      setDraftError("请先选择长者。");
      return;
    }
    const content = draftContent.trim();
    const invalid = validateChunkDraft({ content, importance: draftImportance });
    if (invalid) {
      setDraftError(invalid);
      return;
    }
    setDraftError(null);
    setSaving(true);
    setReceipt(null);
    try {
      const created = await createMemoryChunk({
        elderId,
        content,
        category: draftCategory,
        importance: Number(draftImportance),
        source: draftSource,
      });
      if (!mounted.current) return;
      setDraftContent("");
      setReceipt({
        importance: created.importance,
        provider: created.embedding_provider,
        semantic: created.semantic_embedding,
        source: draftSource,
      });
      setOffset(0);
      setReloadKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setDraftError(memoryErrorText(err));
    } finally {
      if (mounted.current) setSaving(false);
    }
  }, [draftCategory, draftContent, draftImportance, draftSource, elderId]);

  /**
   * 软删除该长者的生效记忆（长者级，不是按行）。
   * 乐观更新：先把表格清空，失败再把快照放回去——「删了」与「没删掉」在界面上必须能看出来。
   */
  const onConfirmDelete = useCallback(async () => {
    if (!elderId) return;
    const snapshot = chunks;
    setConfirmOpen(false);
    setDeleteError(null);
    setDeleteOutcome(null);
    setDeleting(true);
    setChunks([]);
    try {
      const result = await deleteElderMemory({ elderId, reason: MEMORY_DELETE_REASON });
      if (!mounted.current) return;
      setDeleteOutcome(result.deleted_chunks);
      setOffset(0);
      setReloadKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setChunks(snapshot);
      setDeleteError(memoryErrorText(err));
    } finally {
      if (mounted.current) setDeleting(false);
    }
  }, [chunks, elderId]);

  if (!admin) {
    return (
      <section>
        <header className="page-head">
          <h1 className="page-title">长者记忆</h1>
          <p className="page-sub">查看 / 纠正 / 删除（FR-MEM-02）</p>
        </header>
        <div className="card">
          <EmptyState
            title="当前角色无权查看长者记忆"
            description="记忆的查看、纠正与删除仅对管理员开放（接口契约 M1–M3：admin only）。请使用管理员账号登录，或联系管理员处理。"
          />
        </div>
      </section>
    );
  }

  const elderOptions = elders.map((elder) => ({
    value: elder.elder_id,
    label: elder.room_no ? `${elder.name}（${elder.room_no}）` : elder.name,
  }));

  const columns: Column<MemoryChunk>[] = [
    {
      key: "content",
      title: "记忆内容",
      render: (row) => <span title={row.content}>{contentPreview(row.content)}</span>,
    },
    {
      key: "category",
      title: "分类",
      width: "96px",
      render: (row) => (
        <Badge tone={row.category === "family" ? "info" : "neutral"}>{categoryLabel(row.category)}</Badge>
      ),
    },
    {
      key: "importance",
      title: "重要度",
      width: "96px",
      align: "right",
      render: (row) => importanceText(row.importance),
    },
    {
      key: "status",
      title: "状态",
      width: "110px",
      render: (row) => <Badge tone={statusTone(row.status)}>{statusLabel(row.status)}</Badge>,
    },
    {
      key: "created_at",
      title: "写入时间",
      width: "184px",
      render: (row) => <span className="cell-muted">{formatTimestamp(row.created_at)}</span>,
    },
    {
      key: "last_accessed",
      title: "最近召回",
      width: "184px",
      render: (row) => <span className="cell-muted">{formatTimestamp(row.last_accessed)}</span>,
    },
  ];

  const hasNext = mayHaveNextPage(chunks.length, MEMORY_PAGE_SIZE);
  // 失败或加载中不渲染翻页条：让「错误」只出现在错误分支，不要混进「本页 N 条」的措辞里
  const listState = listStateOf({ loading: listLoading, error: listError, count: chunks.length });

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">长者记忆</h1>
        <p className="page-sub">查看 / 纠正 / 删除 · 仅管理员 · 软删除可审计（FR-MEM-02）</p>
        <div className="page-actions">
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => setReloadKey((key) => key + 1)}
            disabled={!elderId || listLoading}
          >
            刷新
          </button>
          <button
            type="button"
            className="btn-danger btn-sm"
            onClick={() => {
              setDeleteError(null);
              setConfirmOpen(true);
            }}
            disabled={!elderId || deleting}
          >
            清空该长者的生效记忆
          </button>
        </div>
      </header>

      {scopeNote ? <Notice>{scopeNote}</Notice> : null}
      {eldersError ? <ErrorBanner message={eldersError} /> : null}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="toolbar">
          <Field label="长者" htmlFor="memory-elder">
            <Select
              id="memory-elder"
              value={elderId}
              disabled={eldersLoading || elderOptions.length === 0}
              onChange={(value) => {
                setElderId(value);
                setOffset(0);
              }}
              options={elderOptions}
            />
          </Field>
          <Field label="状态" htmlFor="memory-status">
            <Select
              id="memory-status"
              value={status}
              onChange={(value) => {
                setStatus(value as MemoryStatus | "");
                setOffset(0);
              }}
              options={STATUS_OPTIONS}
            />
          </Field>
        </div>
        <p className="field-hint">{PAGE_TOTAL_HINT}</p>
      </div>

      {deleteError ? <ErrorBanner message={deleteError} onRetry={() => setReloadKey((key) => key + 1)} /> : null}
      {deleteOutcome !== null ? (
        <Notice role="status">
          {deleteOutcomeText(deleteOutcome)}
        </Notice>
      ) : null}

      <DataTable
        columns={columns}
        rows={chunks}
        rowKey={(row) => row.chunk_id}
        loading={listLoading}
        error={listError}
        onRetry={() => setReloadKey((key) => key + 1)}
        emptyTitle={elderId ? "该长者名下没有符合条件的记忆" : "还没有可管理的长者"}
        emptyDescription={
          elderId
            ? "换一个状态筛选（例如「已软删除」）能看到历史记录；也可以在下方向该长者写入一条新记忆。"
            : "长者档案为空时无法写入记忆，请先在「长者档案」页建立档案。"
        }
        caption="长者记忆列表"
      />

      {listState === "loading" || listState === "error" ? null : (
        <div className="toolbar" style={{ justifyContent: "space-between" }}>
          <span className="cell-muted">{pageRangeText(offset, chunks.length)}</span>
          <span className="cell-muted">{pageNote(chunks.length, MEMORY_PAGE_SIZE)}</span>
          <div className="cell-actions">
            <button
              type="button"
              className="btn-secondary btn-sm"
              onClick={() => setOffset(Math.max(0, offset - MEMORY_PAGE_SIZE))}
              disabled={offset === 0}
            >
              上一页
            </button>
            <button
              type="button"
              className="btn-secondary btn-sm"
              onClick={() => setOffset(offset + MEMORY_PAGE_SIZE)}
              disabled={!hasNext}
            >
              下一页
            </button>
          </div>
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="page-sub">新增 / 纠正一条记忆</h2>
        <p className="field-hint">
          纠正的做法是写入一条新说法（旧记录仍留在列表里，可用状态区分）；写入即落库并生成向量，
          正文超过 {MEMORY_CONTENT_MAX} 字会被拒收，不会替你截断。
        </p>
        <FormGrid>
          <Field
            label="记忆内容"
            htmlFor="memory-content"
            required
            hint={`1–${MEMORY_CONTENT_MAX} 字`}
            error={draftError ?? undefined}
          >
            <textarea
              id="memory-content"
              className="input"
              rows={3}
              value={draftContent}
              onChange={(event) => setDraftContent(event.target.value)}
              placeholder="例如：张奶奶喜欢越剧《梁山伯与祝英台》，下午三点常在活动室听戏。"
            />
          </Field>
          <Field label="分类" htmlFor="memory-category">
            <Select
              id="memory-category"
              value={draftCategory}
              onChange={(value) => setDraftCategory(value as MemoryCategory)}
              options={CATEGORY_OPTIONS}
            />
          </Field>
          <Field label="来源" htmlFor="memory-source" hint={SOURCE_HINT}>
            <Select
              id="memory-source"
              value={draftSource}
              onChange={(value) => setDraftSource(value as MemorySource)}
              options={SOURCE_OPTIONS}
            />
          </Field>
          <Field
            label="基础重要度"
            htmlFor="memory-importance"
            hint={`折算后落库约 ${previewImportance(Number(draftImportance), draftSource)}`}
          >
            <input
              id="memory-importance"
              className="input"
              inputMode="decimal"
              value={draftImportance}
              onChange={(event) => setDraftImportance(event.target.value)}
            />
          </Field>
        </FormGrid>
        <FormActions>
          <button
            type="button"
            className="btn-primary"
            onClick={() => void onSubmit()}
            disabled={saving || !elderId}
          >
            {saving ? "正在写入…" : "写入记忆"}
          </button>
        </FormActions>
        {receipt ? (
          <p className="submit-result" role="status">
            已写入 1 条记忆，落库重要度 {importanceText(receipt.importance)}（来源：{sourceLabel(receipt.source)}）；
            向量来源：{receipt.provider}
            {receipt.semantic ? "（语义向量）" : "（确定性噪声向量，仅用于占位，不用于召回评估）"}。
          </p>
        ) : null}
      </div>

      <Dialog
        open={confirmOpen}
        title="清空该长者的生效记忆？"
        onClose={() => setConfirmOpen(false)}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setConfirmOpen(false)}>
              取消
            </button>
            <button type="button" className="btn-danger" onClick={() => void onConfirmDelete()}>
              确认软删除
            </button>
          </>
        }
      >
        <p>
          只会把该长者 <strong>生效中（active）</strong> 的记忆标记为已软删除，物理记录保留并写一条审计日志；
          已被覆盖（superseded）与已删除的记录不受影响。
        </p>
        <p className="field-hint">
          再次执行不会删掉更多东西——第二次调用返回 deleted_chunks=0 并再写一条审计，这不是错误。
        </p>
      </Dialog>
    </section>
  );
}
