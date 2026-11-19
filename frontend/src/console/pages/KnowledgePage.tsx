/**
 * 机构知识库管理页（K3，FR-KNW-02 / AC-44）。
 *
 * 四件事：
 * 1. **列表**：标题 / 类别 / 版本 / 权威级别 / 生效区间 / 状态（含 draft、retired 全量）；
 * 2. **新建**：文档级版本 + 生效区间 + 权威级别 + 正文（后端切块，不由前端决定切法）；
 * 3. **换代**：新版生效、旧版置 `retired` 并把生效终点收到新版起点——旧版立刻不可检索；
 * 4. **停用**：把 `active` 置 `retired`，检索不再命中，但正文仍留在库里可追溯。
 *
 * 三条硬口径：
 * 1. **只有管理员能进**（服务端 `WRITE_ROLES` 与 `GET /documents` 都只认 admin）；
 *    非管理员看到的是 403 文案而不是空白页（卡里第 2 条验收）。
 * 2. **状态只有三值**（draft / active / retired），页面不发明「审核中」「已发布」这类词；
 *    中文标签只在 `knowledge.ts` 的 `STATUS_LABEL` 一处映射。
 * 3. **「停用」不是删除**：正文与向量都还在，只是不再被检索。页面上要说清这一点，
 *    否则下一个人会以为可以靠停用来「清理错数据」。
 *
 * 分层：本文件只做渲染与表单状态，取数/映射/校验全在 `../knowledge`（可被 node:test 直接覆盖）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { HttpError, errorText } from "../http";
import type { Principal } from "../api";
import { isAdmin } from "../permissions";
import { Badge, EmptyState, ErrorBanner } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { Dialog } from "../ui/Dialog";
import { Field, FormGrid, Select } from "../ui/Form";
import { DataTable, type Column } from "../ui/Table";
import {
  ADMIN_ONLY_DESCRIPTION,
  ADMIN_ONLY_TITLE,
  AUTHORITY_LABEL,
  BODY_HINT,
  CATEGORY_LABEL,
  CATEGORY_ORDER,
  INSERT_HINT,
  STATUS_LABEL,
  STATUS_ORDER,
  SUPERSEDE_HINT,
  authorityLabel,
  canSubmitForm,
  categoryLabel,
  createDocument,
  effectiveWindowText,
  emptyIngestForm,
  listDocuments,
  retireDocument,
  statusLabel,
  statusTone,
  supersedeDocument,
  toApiIso,
  validateForm,
  type FormErrors,
  type IngestForm,
  type KnowledgeDocumentSummary,
} from "../knowledge";

const SECTION_GAP = { marginBottom: 16 } as const;
const PAGE_SIZE = 50;

const STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  ...STATUS_ORDER.map((value) => ({ value, label: STATUS_LABEL[value] })),
];

const CATEGORY_OPTIONS = [
  { value: "", label: "全部类别" },
  ...CATEGORY_ORDER.map((value) => ({ value, label: CATEGORY_LABEL[value] })),
];

const AUTHORITY_OPTIONS = Object.entries(AUTHORITY_LABEL).map(([value, label]) => ({ value, label }));
const CATEGORY_FORM_OPTIONS = CATEGORY_ORDER.map((value) => ({ value, label: CATEGORY_LABEL[value] }));

/** 服务端错误 → 页面能读的中文（不解析 detail，按 code 分支）。 */
function describeError(err: unknown, fallback: string): string {
  if (err instanceof HttpError) {
    if (err.code === "CONFLICT") return "同名同版本已存在：请提高版本号新建，或对原条目做「换代」。";
    if (err.code === "SERVICE_NOT_READY") return "向量服务未就绪，本次没有写入（库里的条目不受影响）。";
    if (err.code === "VALIDATION_ERROR") return "填写内容不合法：请检查类别、生效区间与正文长度。";
    if (err.code === "ROLE_FORBIDDEN") return "当前账号不是管理员，无法维护机构知识。";
    if (err.code === "SCOPE_FORBIDDEN") return "只有员工账号可以维护机构知识。";
  }
  return errorText(err) || fallback;
}

export function KnowledgePage({ principal }: { principal: Principal }) {
  const admin = isAdmin(principal);

  const [docs, setDocs] = useState<KnowledgeDocumentSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [keyword, setKeyword] = useState("");
  const [appliedKeyword, setAppliedKeyword] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState<IngestForm>(emptyIngestForm);
  const [formErrors, setFormErrors] = useState<FormErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const [editTarget, setEditTarget] = useState<KnowledgeDocumentSummary | null>(null);
  const [editBody, setEditBody] = useState("");
  const [editVersion, setEditVersion] = useState("");
  const [editFrom, setEditFrom] = useState("");
  const [editUntil, setEditUntil] = useState("");
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const [retireTarget, setRetireTarget] = useState<KnowledgeDocumentSummary | null>(null);
  const [retireBusy, setRetireBusy] = useState(false);
  const [retireError, setRetireError] = useState<string | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const loadDocuments = useCallback(async () => {
    if (!admin) return;
    setLoading(true);
    setListError(null);
    try {
      const data = await listDocuments({
        status: statusFilter,
        category: categoryFilter,
        q: appliedKeyword,
        limit: PAGE_SIZE,
        offset: 0,
      });
      if (!mounted.current) return;
      setDocs(data.items);
      setTotal(data.total);
    } catch (err) {
      if (!mounted.current) return;
      setDocs([]);
      setTotal(0);
      setListError(describeError(err, "加载知识库失败"));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [admin, statusFilter, categoryFilter, appliedKeyword]);

  useEffect(() => {
    void loadDocuments();
  }, [loadDocuments, reloadKey]);

  const openCreate = () => {
    setForm(emptyIngestForm());
    setFormErrors({});
    setSubmitError(null);
    setCreateOpen(true);
  };

  const closeCreate = () => {
    if (submitting) return;
    setCreateOpen(false);
  };

  const submitCreate = async () => {
    const errors = validateForm(form);
    setFormErrors(errors);
    if (Object.keys(errors).length > 0) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await createDocument({
        title: form.title.trim(),
        body: form.body.trim(),
        category: form.category,
        version: Number(form.version),
        authority_level: form.authority_level,
        effective_from: toApiIso(form.effective_from),
        effective_until: toApiIso(form.effective_until),
      });
      if (!mounted.current) return;
      setCreateOpen(false);
      setNotice(
        `已录入《${created.title}》v${created.version}（${created.chunks} 个分块，状态 ${statusLabel(created.status)}）。` +
          (created.semantic_embedding
            ? ""
            : "注意：本次写入的向量来自确定性降级实现，只作占位，检索不会走语义匹配。"),
      );
      setReloadKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setSubmitError(describeError(err, "录入失败"));
    } finally {
      if (mounted.current) setSubmitting(false);
    }
  };

  const openSupersede = (doc: KnowledgeDocumentSummary) => {
    setEditTarget(doc);
    setEditBody("");
    setEditVersion(String(doc.version + 1));
    setEditFrom("");
    setEditUntil("");
    setEditError(null);
  };

  const closeSupersede = () => {
    if (editBusy) return;
    setEditTarget(null);
  };

  const submitSupersede = async () => {
    if (!editTarget) return;
    if (!editBody.trim()) {
      setEditError("换代必须带新正文：正文是版本之间的实质差异，空正文等于把旧口径换成空话。");
      return;
    }
    setEditBusy(true);
    setEditError(null);
    try {
      const next = await supersedeDocument(editTarget.document_id, {
        body: editBody.trim(),
        version: /^[1-9]\d*$/.test(editVersion.trim()) ? Number(editVersion) : null,
        effective_from: toApiIso(editFrom),
        effective_until: toApiIso(editUntil),
      });
      if (!mounted.current) return;
      setEditTarget(null);
      setNotice(
        `《${next.title}》已换代为 v${next.version}；旧版状态置「已退役」，检索不再命中它（正文仍留在库里可追溯）。`,
      );
      setReloadKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setEditError(describeError(err, "换代失败"));
    } finally {
      if (mounted.current) setEditBusy(false);
    }
  };

  const openRetire = (doc: KnowledgeDocumentSummary) => {
    setRetireTarget(doc);
    setRetireError(null);
  };

  const closeRetire = () => {
    if (retireBusy) return;
    setRetireTarget(null);
  };

  const submitRetire = async () => {
    if (!retireTarget) return;
    setRetireBusy(true);
    setRetireError(null);
    try {
      const next = await retireDocument(retireTarget.document_id);
      if (!mounted.current) return;
      setRetireTarget(null);
      setNotice(
        next.already_retired
          ? `《${next.title}》v${next.version} 本来就是停用状态：本次没有重复写审计，也没有改动封口时刻。`
          : `《${next.title}》v${next.version} 已停用（状态 ${statusLabel(next.status)}）：检索不再命中它，正文仍保留。`,
      );
      setReloadKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setRetireError(describeError(err, "停用失败"));
    } finally {
      if (mounted.current) setRetireBusy(false);
    }
  };

  // ---- 非管理员：403 文案，不白屏（卡里第 2 条验收）
  if (!admin) {
    return (
      <section>
        <header className="page-head">
          <h1 className="page-title">机构知识库</h1>
          <p className="page-sub">录入与版本切换 · 仅管理员（FR-KNW-02）</p>
        </header>
        <div className="card">
          <EmptyState title={ADMIN_ONLY_TITLE} description={ADMIN_ONLY_DESCRIPTION} />
        </div>
      </section>
    );
  }

  const columns: Column<KnowledgeDocumentSummary>[] = [
    {
      key: "title",
      title: "标题",
      render: (row) => <div>{row.title}</div>,
    },
    {
      key: "category",
      title: "类别",
      width: "104px",
      render: (row) => <Badge tone="info">{categoryLabel(row.category)}</Badge>,
    },
    {
      key: "version",
      title: "版本",
      width: "72px",
      align: "right",
      render: (row) => `v${row.version}`,
    },
    {
      key: "authority_level",
      title: "权威级别",
      width: "132px",
      render: (row) => (
        <Badge tone={row.authority_level === "institution" ? "success" : "neutral"}>
          {authorityLabel(row.authority_level)}
        </Badge>
      ),
    },
    {
      key: "effective",
      title: "生效区间",
      width: "260px",
      render: (row) => (
        <span className="cell-muted">{effectiveWindowText(row.effective_from, row.effective_until)}</span>
      ),
    },
    {
      key: "status",
      title: "状态",
      width: "96px",
      render: (row) => <Badge tone={statusTone(row.status)}>{statusLabel(row.status)}</Badge>,
    },
    {
      key: "chunks",
      title: "分块",
      width: "72px",
      align: "right",
      render: (row) => row.chunks,
    },
    {
      key: "actions",
      title: "操作",
      width: "150px",
      render: (row) => (
        <div className="cell-actions">
          <button type="button" className="btn-secondary btn-sm" onClick={() => openSupersede(row)}>
            换代
          </button>
          <button
            type="button"
            className="btn-danger btn-sm"
            onClick={() => openRetire(row)}
            disabled={row.status === "retired"}
          >
            停用
          </button>
        </div>
      ),
    },
  ];

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">机构知识库</h1>
        <p className="page-sub">
          文档级版本 + 生效区间 + 权威级别 · 仅管理员可写 · 只有生效版本能被检索（FR-KNW-02 / AC-44）
        </p>
        <div className="page-actions">
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => setReloadKey((key) => key + 1)}
            disabled={loading}
          >
            刷新
          </button>
          <button type="button" className="btn-primary" onClick={openCreate}>
            新建文档
          </button>
        </div>
      </header>

      <Notice style={SECTION_GAP}>
        这里维护的是<strong>全院共享</strong>的机构口径（作息、餐饮、活动、制度、设施、服务流程），不是某位长者的记忆：
        不进长者记忆、不参与衰减，退房清理数据也不会删除它。
      </Notice>

      {notice ? (
        <Notice role="status" style={SECTION_GAP}>
          {notice}
        </Notice>
      ) : null}

      <div className="toolbar" style={SECTION_GAP}>
        <Field label="状态" htmlFor="knowledge-status">
          <Select id="knowledge-status" value={statusFilter} onChange={setStatusFilter} options={STATUS_OPTIONS} />
        </Field>
        <Field label="类别" htmlFor="knowledge-category">
          <Select
            id="knowledge-category"
            value={categoryFilter}
            onChange={setCategoryFilter}
            options={CATEGORY_OPTIONS}
          />
        </Field>
        <div style={{ minWidth: 240, flex: 1 }}>
          <Field label="标题关键字" htmlFor="knowledge-keyword">
            <input
              id="knowledge-keyword"
              className="input"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") setAppliedKeyword(keyword.trim());
              }}
              placeholder="例如：午餐（按标题模糊过滤）"
            />
          </Field>
        </div>
        <div className="toolbar-actions">
          <button type="button" className="btn-secondary" onClick={() => setAppliedKeyword(keyword.trim())}>
            查询
          </button>
        </div>
      </div>

      <h2 className="page-sub">
        知识文档（{total} 条{statusFilter ? `，状态 ${statusLabel(statusFilter)}` : ""}
        {categoryFilter ? `，类别 ${categoryLabel(categoryFilter)}` : ""}）
      </h2>
      <DataTable<KnowledgeDocumentSummary>
        columns={columns}
        rows={docs}
        rowKey={(row) => row.document_id}
        loading={loading}
        error={listError}
        onRetry={() => setReloadKey((key) => key + 1)}
        emptyTitle="知识库还没有符合条件的文档"
        emptyDescription="用右上角「新建文档」录入第一条机构口径；同一标题的修改请用列表里的「换代」，不要新建一个同版本号碰运气。"
        caption="机构知识文档列表"
      />

      <Dialog
        open={createOpen}
        title="新建知识文档"
        onClose={closeCreate}
        width={720}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={closeCreate} disabled={submitting}>
              取消
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void submitCreate()}
              disabled={submitting || !canSubmitForm(form)}
            >
              {submitting ? "正在写入…" : "录入并生效"}
            </button>
          </>
        }
      >
        <p className="field-hint" style={SECTION_GAP}>
          {INSERT_HINT}
          正文会由后端切块并写入向量；库里的条目按「文档级版本」留痕，改口径请用「换代」而不是覆盖。
        </p>
        <FormGrid>
          <Field label="标题" htmlFor="knowledge-title" required error={formErrors.title} hint="例如：午餐时间">
            <input
              id="knowledge-title"
              className="input"
              value={form.title}
              onChange={(event) => setForm({ ...form, title: event.target.value })}
              placeholder="午餐时间"
            />
          </Field>
          <Field label="类别" htmlFor="knowledge-form-category" required>
            <Select
              id="knowledge-form-category"
              value={form.category}
              onChange={(value) => setForm({ ...form, category: value as IngestForm["category"] })}
              options={CATEGORY_FORM_OPTIONS}
            />
          </Field>
          <Field label="版本号" htmlFor="knowledge-version" required error={formErrors.version} hint="正整数；同名同版本会被拒收（409）">
            <input
              id="knowledge-version"
              className="input"
              inputMode="numeric"
              value={form.version}
              onChange={(event) => setForm({ ...form, version: event.target.value })}
            />
          </Field>
          <Field label="权威级别" htmlFor="knowledge-authority" hint="机构规定优先于长者自述转录">
            <Select
              id="knowledge-authority"
              value={form.authority_level}
              onChange={(value) => setForm({ ...form, authority_level: value as IngestForm["authority_level"] })}
              options={AUTHORITY_OPTIONS}
            />
          </Field>
          <Field label="生效起点" htmlFor="knowledge-from" hint="留空 = 当前时刻立即生效">
            <input
              id="knowledge-from"
              className="input"
              type="datetime-local"
              value={form.effective_from}
              onChange={(event) => setForm({ ...form, effective_from: event.target.value })}
            />
          </Field>
          <Field label="生效终点" htmlFor="knowledge-until" error={formErrors.effective_until} hint="留空 = 长期有效">
            <input
              id="knowledge-until"
              className="input"
              type="datetime-local"
              value={form.effective_until}
              onChange={(event) => setForm({ ...form, effective_until: event.target.value })}
            />
          </Field>
        </FormGrid>
        <Field label="正文" htmlFor="knowledge-body" required error={formErrors.body} hint={BODY_HINT}>
          <textarea
            id="knowledge-body"
            className="input"
            rows={8}
            value={form.body}
            onChange={(event) => setForm({ ...form, body: event.target.value })}
            placeholder={"例如：午餐时间为每日 11:30，地点在一层餐厅；行动不便的长者可致电护士站送餐到房。"}
          />
        </Field>
        {submitError ? <ErrorBanner message={submitError} /> : null}
      </Dialog>

      <Dialog
        open={editTarget !== null}
        title={editTarget ? `换代：${editTarget.title}` : "换代"}
        onClose={closeSupersede}
        width={680}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={closeSupersede} disabled={editBusy}>
              取消
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void submitSupersede()}
              disabled={editBusy || !editBody.trim()}
            >
              {editBusy ? "正在换代…" : "提交新版"}
            </button>
          </>
        }
      >
        {editTarget ? (
          <>
            <p style={SECTION_GAP}>
              当前生效版本：<strong>v{editTarget.version}</strong>（{categoryLabel(editTarget.category)} ·{" "}
              {authorityLabel(editTarget.authority_level)}），生效区间{" "}
              {effectiveWindowText(editTarget.effective_from, editTarget.effective_until)}。
            </p>
            <p className="field-hint" style={SECTION_GAP}>
              {SUPERSEDE_HINT}
            </p>
            <FormGrid>
              <Field label="新版本号" htmlFor="knowledge-edit-version" hint="留空由后端按旧版本 +1">
                <input
                  id="knowledge-edit-version"
                  className="input"
                  inputMode="numeric"
                  value={editVersion}
                  onChange={(event) => setEditVersion(event.target.value)}
                />
              </Field>
              <Field label="新版本生效起点" htmlFor="knowledge-edit-from" hint="留空 = 当前时刻">
                <input
                  id="knowledge-edit-from"
                  className="input"
                  type="datetime-local"
                  value={editFrom}
                  onChange={(event) => setEditFrom(event.target.value)}
                />
              </Field>
              <Field label="新版本生效终点" htmlFor="knowledge-edit-until" hint="留空 = 长期有效">
                <input
                  id="knowledge-edit-until"
                  className="input"
                  type="datetime-local"
                  value={editUntil}
                  onChange={(event) => setEditUntil(event.target.value)}
                />
              </Field>
            </FormGrid>
            <Field label="新版本正文" htmlFor="knowledge-edit-body" required hint="旧版正文不会被改写，只改状态与生效终点">
              <textarea
                id="knowledge-edit-body"
                className="input"
                rows={7}
                value={editBody}
                onChange={(event) => setEditBody(event.target.value)}
                placeholder="把新的机构口径完整写一遍（不要写「同上」）。"
              />
            </Field>
            {editError ? <ErrorBanner message={editError} /> : null}
          </>
        ) : null}
      </Dialog>

      <Dialog
        open={retireTarget !== null}
        title={retireTarget ? `停用：${retireTarget.title} v${retireTarget.version}` : "停用"}
        onClose={closeRetire}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={closeRetire} disabled={retireBusy}>
              取消
            </button>
            <button type="button" className="btn-danger" onClick={() => void submitRetire()} disabled={retireBusy}>
              {retireBusy ? "正在停用…" : "确认停用"}
            </button>
          </>
        }
      >
        <p>
          停用会把该版本状态置为 <strong>已退役（retired）</strong>，并把生效终点收到当前时刻：
          检索立刻不再命中它，长者也不会再被这样回答。
        </p>
        <p className="field-hint">
          这不是删除——正文与向量仍留在库里可追溯，随时能看出「当时机构是怎么规定的」。重复停用不会报错
          （幂等），也不会改出第二个版本。
        </p>
        {retireError ? <ErrorBanner message={retireError} /> : null}
      </Dialog>
    </section>
  );
}
