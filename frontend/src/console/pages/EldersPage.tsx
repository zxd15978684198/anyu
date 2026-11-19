/**
 * 长者档案页（W2）：列表 / 状态筛选 / 详情 / 建档 / 改状态。
 *
 * 分工：本文件只做编排与界面状态；端点形状、分页参数、空值收口都在 `../elders.ts`，
 * 权限判定只在 `can()` / `elderScopeNote()`（全站唯一角色判断处），本页不另造规则。
 *
 * 四条值得留意的实现约束：
 * 1. 列表与详情的异步结果各带 `alive` 标记——切筛选、关弹窗后到达的旧响应直接丢弃，
 *    避免慢请求把新页面状态盖回去。
 * 2. 写按钮只在 `can(principal, "elder:write")` 时**渲染**（不是禁用）；服务端仍会 403，
 *    那时按 `errorText` 显示，不把权限判断下沉成前端守卫。
 * 3. 建档失败保留已填内容，只在顶部/字段下方追加错误文案。
 * 4. 两个 Dialog 都只挂一个 `open` 布尔，关闭时清空对应状态，重复打开不会残留上一次的报错。
 */
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import type { Principal } from "../api";
import {
  CHECKOUT_REASON_OPTIONS,
  ELDER_STATUS_OPTIONS,
  PAGE_SIZE,
  checkoutElder,
  createElder,
  getElder,
  isElderStatus,
  listElders,
  patchElder,
  textOrDash,
  type ElderCheckoutReceipt,
  type ElderDetail,
  type ElderListItem,
  type ElderStatus,
} from "../elders";
import { errorText, apiSend } from "../http";
import { can, elderScopeNote } from "../permissions";
import { listPersonaAssets, type PersonaAsset } from "../personas";
import { DataTable, type Column } from "../ui/Table";
import { Pagination } from "../ui/Pagination";
import { Dialog } from "../ui/Dialog";
import { Field, FormGrid, Select } from "../ui/Form";
import { ElderStatusBadge, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { formatDateTimeFull } from "../../shared/format";
import { useListResource } from "../../shared/useListResource";

interface CreateForm {
  name: string;
  birth_date: string;
  gender: string;
  persona_id: string;
}

const EMPTY_FORM: CreateForm = { name: "", birth_date: "", gender: "", persona_id: "" };

/**
 * 形象选择（W1 · FR-3D-03）：一处人设 = 一张数字人脸，`personas.avatar_url` 指向一个 VRM。
 * 下拉与角色页共用 `listPersonaAssets()`，本页只取预览需要的三列。
 */
type PersonaOption = Pick<PersonaAsset, "persona_id" | "name" | "avatar_url">;

/** `frontend/public/avatars/catalog.json` 里一条模型（只取预览要用的字段）。 */
interface CatalogModel {
  id: string;
  display_name: string;
  gender: string;
  file: string;
  url: string;
  bytes: number;
  character?: string;
}

const NO_PERSONA_LABEL = "未指定（小满兜底）";

/** 「全部」+ 闭集四态；筛选项与详情改状态共用同一份 label。 */
const STATUS_FILTER_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "全部" },
  ...ELDER_STATUS_OPTIONS,
];

const COLUMNS: Column<ElderListItem>[] = [
  { key: "name", title: "姓名" },
  { key: "room_no", title: "房间", render: (row) => textOrDash(row.room_no) },
  { key: "bed_no", title: "床位", render: (row) => textOrDash(row.bed_no) },
  { key: "status", title: "状态", render: (row) => <ElderStatusBadge status={row.status} /> },
  {
    key: "elder_id",
    title: "档案编号",
    render: (row) => (
      <span className="cell-muted" title={row.elder_id}>
        {row.elder_id.slice(0, 8)}
      </span>
    ),
  },
];

/** 详情字段：标签小字 + 值；空值由调用方先过 `textOrDash`。 */
function DetailItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <p className="field-hint">{label}</p>
      <p style={{ margin: "2px 0 0" }}>{value}</p>
    </div>
  );
}

/** 后端存 UTC，展示转本地：能解析就本地化，解析不了就原样显示，不抛错。 */
function formatTime(value: string): string {
  return formatDateTimeFull(value);
}

export function EldersPage({ principal }: { principal: Principal }) {
  const canWrite = can(principal, "elder:write");
  const scopeNote = elderScopeNote(principal);

  // ---- 列表 ----
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<ElderStatus | "">("");
  // 列表骨架收敛到 shared/useListResource（W4）：竞态取消 / 失败清空 / reload 语义
  // 原先每页各写一遍。本页渲染用的是 listLoading/listError 两个派生量，保持调用点不变。
  const { rows, total, state: listState, reload } = useListResource<ElderListItem>(
    () => listElders({ page, pageSize: PAGE_SIZE, status: statusFilter }),
    [page, statusFilter],
    { errorText },
  );
  const listLoading = listState.kind === "loading";
  const listError = listState.kind === "error" ? listState.message : null;

  // ---- 详情 ----
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ElderDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailReloadKey, setDetailReloadKey] = useState(0);
  const [statusDraft, setStatusDraft] = useState<ElderStatus>("active");
  const [statusSaving, setStatusSaving] = useState(false);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [statusSaved, setStatusSaved] = useState(false);

  // 形象（W1）：候选人设 + catalog 元信息只拉一次；写入口是 `PATCH /{elder_id}/persona`
  const [personas, setPersonas] = useState<PersonaOption[]>([]);
  const [personasError, setPersonasError] = useState<string | null>(null);
  const [catalogModels, setCatalogModels] = useState<CatalogModel[]>([]);
  const [personaDraft, setPersonaDraft] = useState("");
  const [personaSaving, setPersonaSaving] = useState(false);
  const [personaError, setPersonaError] = useState<string | null>(null);
  const [personaSaved, setPersonaSaved] = useState(false);

  // ---- 退房（FR-ADM-09，仅 admin） ----
  const canCheckout = can(principal, "elder:checkout");
  const [checkoutOpen, setCheckoutOpen] = useState(false);
  const [checkoutReason, setCheckoutReason] = useState("checkout");
  const [checkoutArchive, setCheckoutArchive] = useState(false);
  const [checkoutConfirmed, setCheckoutConfirmed] = useState(false);
  const [checkoutSaving, setCheckoutSaving] = useState(false);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const [checkoutReceipt, setCheckoutReceipt] = useState<ElderCheckoutReceipt | null>(null);

  // ---- 建档 ----
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState<CreateForm>(EMPTY_FORM);
  const [nameError, setNameError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 详情：每次打开/重试都重新取，避免用列表里的旧字段拼详情
  useEffect(() => {
    if (!detailId) return;
    let alive = true;
    setDetailLoading(true);
    setDetailError(null);
    setDetail(null);
    setStatusError(null);
    setStatusSaved(false);
    getElder(detailId)
      .then((data) => {
        if (!alive) return;
        setDetail(data);
        setStatusDraft(isElderStatus(data.status) ? data.status : "active");
        // 形象草稿跟着详情走：换一次长者就重置，避免把上一位的选择带过来
        setPersonaDraft(data.persona_id ?? "");
        setPersonaError(null);
        setPersonaSaved(false);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setDetailError(errorText(err));
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [detailId, detailReloadKey]);

  // 形象候选 + catalog 元信息：只给有写权限的人拉（care_worker 拿这个端点会 403，
  // 没必要让只读角色吃一条无意义的报错）。catalog 用原生 fetch 读静态 JSON——
  // console **绝不能** import pad 的 avatar 模块，否则 three-vrm 会被打进照护台产物（AGENTS §2.4）。
  useEffect(() => {
    if (!canWrite) return;
    let alive = true;
    listPersonaAssets()
      .then((items) => {
        if (!alive) return;
        setPersonas(items);
        setPersonasError(null);
      })
      .catch((err: unknown) => {
        if (alive) setPersonasError(errorText(err));
      });
    fetchCatalogModels().then((items) => {
      if (alive) setCatalogModels(items);
    });
    return () => {
      alive = false;
    };
  }, [canWrite]);

  function openDetail(elderId: string) {
    setDetail(null);
    setDetailError(null);
    setDetailId(elderId);
  }

  function closeDetail() {
    setDetailId(null);
    setDetail(null);
    setDetailError(null);
    setStatusError(null);
    setPersonaError(null);
    setPersonaSaved(false);
    closeCheckout();
  }

  function openCreate() {
    setForm(EMPTY_FORM);
    setNameError(null);
    setFormError(null);
    setCreateOpen(true);
  }

  function closeCreate() {
    if (submitting) return;
    setCreateOpen(false);
    setFormError(null);
  }

  function updateField(key: keyof CreateForm, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }));
    if (key === "name" && nameError) setNameError(null);
  }

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    const name = form.name.trim();
    if (!name) {
      setNameError("请填写长者姓名");
      return;
    }
    setNameError(null);
    setFormError(null);
    setSubmitting(true);
    try {
      await createElder({
        name,
        birth_date: form.birth_date,
        gender: form.gender,
        persona_id: form.persona_id,
      });
      setCreateOpen(false);
      setForm(EMPTY_FORM);
      setPage(1);
      reload();
    } catch (err) {
      // 失败不清空已填内容，错误显示在表单顶部
      setFormError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function saveStatus() {
    if (!detail || !canWrite || statusSaving) return;
    if (statusDraft === detail.status) return;
    setStatusSaving(true);
    setStatusError(null);
    setStatusSaved(false);
    try {
      await patchElder(detail.elder_id, { status: statusDraft });
      setDetail({ ...detail, status: statusDraft });
      setStatusSaved(true);
      reload();
    } catch (err) {
      setStatusError(errorText(err));
    } finally {
      setStatusSaving(false);
    }
  }

  async function savePersona() {
    if (!detail || !canWrite || personaSaving) return;
    const next = personaDraft ? personaDraft : null;
    if (next === detail.persona_id) return;
    setPersonaSaving(true);
    setPersonaError(null);
    setPersonaSaved(false);
    try {
      // 空串 = 解除分配（请求体里显式 `persona_id: null`），pad 侧回到四段回落链的兜底形象
      await apiSend<void>(
        `/api/elders/${encodeURIComponent(detail.elder_id)}/persona`,
        "PATCH",
        { persona_id: next },
      );
      setDetail({ ...detail, persona_id: next });
      setPersonaSaved(true);
      reload();
    } catch (err) {
      setPersonaError(errorText(err));
    } finally {
      setPersonaSaving(false);
    }
  }

  function openCheckout() {
    setCheckoutReason("checkout");
    setCheckoutArchive(false);
    setCheckoutConfirmed(false);
    setCheckoutError(null);
    setCheckoutReceipt(null);
    setCheckoutOpen(true);
  }

  function closeCheckout() {
    if (checkoutSaving) return;
    setCheckoutOpen(false);
    setCheckoutError(null);
    setCheckoutReceipt(null);
    setCheckoutConfirmed(false);
  }

  async function submitCheckout() {
    if (!detail || !canCheckout || checkoutSaving || !checkoutConfirmed) return;
    setCheckoutSaving(true);
    setCheckoutError(null);
    try {
      const receipt = await checkoutElder(detail.elder_id, {
        archive: checkoutArchive,
        reason: checkoutReason,
      });
      setCheckoutReceipt(receipt);
      setDetail({ ...detail, status: receipt.elder_status ?? detail.status });
      reload();
    } catch (err) {
      setCheckoutError(errorText(err));
    } finally {
      setCheckoutSaving(false);
    }
  }

  function finishCheckout() {
    setCheckoutSaving(false);
    setCheckoutOpen(false);
    setCheckoutReceipt(null);
    setCheckoutConfirmed(false);
    closeDetail();
  }

  /** 人设展示名：候选里有就用名字，否则回退原始 UUID（只读角色拿不到人设列表） */
  function personaLabel(personaId: string | null | undefined): string {
    if (!personaId) return NO_PERSONA_LABEL;
    const found = personas.find((item) => item.persona_id === personaId);
    return found ? `${found.name}${modelSuffix(found.avatar_url)}` : personaId;
  }

  /** 人设后面缀上它绑定的模型名，让「小满 · 丽莎」一眼看出换的是哪张脸 */
  function modelSuffix(avatarUrl: string | null): string {
    if (!avatarUrl) return "";
    const model = catalogModels.find((item) => item.url === avatarUrl);
    return model ? ` · ${model.display_name}` : "";
  }

  const personaOptions = [
    { value: "", label: NO_PERSONA_LABEL },
    ...personas.map((item) => ({
      value: item.persona_id,
      label: `${item.name}${modelSuffix(item.avatar_url)}`,
    })),
  ];
  const selectedPersona = personas.find((item) => item.persona_id === personaDraft) ?? null;

  const statusChanged = detail ? statusDraft !== detail.status : false;
  const personaChanged = detail ? (personaDraft || null) !== detail.persona_id : false;

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">长者档案</h1>
        <p className="page-sub">{listLoading ? "正在加载…" : `共 ${total} 位长者`}</p>
        {canWrite ? (
          <div className="page-actions">
            <button type="button" className="btn-primary" onClick={openCreate}>
              新建长者
            </button>
          </div>
        ) : null}
      </header>

      {scopeNote ? <Notice>{scopeNote}</Notice> : null}

      <div className="toolbar">
        <Field label="状态" htmlFor="elder-status-filter">
          <Select
            id="elder-status-filter"
            value={statusFilter}
            onChange={(value) => {
              setStatusFilter(value as ElderStatus | "");
              setPage(1);
            }}
            options={STATUS_FILTER_OPTIONS}
          />
        </Field>
      </div>

      <DataTable<ElderListItem>
        columns={COLUMNS}
        rows={rows}
        rowKey={(row) => row.elder_id}
        loading={listLoading}
        error={listError}
        onRetry={reload}
        onRowClick={(row) => openDetail(row.elder_id)}
        emptyTitle="还没有长者档案"
        emptyDescription={
          statusFilter
            ? "该状态下暂无档案，换个状态试试。"
            : canWrite
              ? "建档后就可以在这里维护房间、状态与人设。"
              : "您负责范围内暂无长者档案。"
        }
        emptyAction={
          canWrite ? (
            <button type="button" className="btn-primary" onClick={openCreate}>
              新建第一位
            </button>
          ) : undefined
        }
        caption="长者档案列表"
      />

      {!listLoading && !listError && total > 0 ? (
        <Pagination
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onChange={setPage}
          disabled={listLoading}
        />
      ) : null}

      <Dialog
        open={detailId !== null}
        title={detail ? `长者详情 · ${detail.name}` : "长者详情"}
        onClose={closeDetail}
        width={640}
        footer={
          <>
            {canCheckout && detail && detail.status !== "archived" ? (
              <button
                type="button"
                className="btn-danger"
                onClick={openCheckout}
                disabled={detailLoading}
              >
                办理退房
              </button>
            ) : null}
            <button type="button" className="btn-secondary" onClick={closeDetail}>
              关闭
            </button>
          </>
        }
      >
        {detailLoading ? (
          <LoadingState text="正在加载档案…" />
        ) : detailError ? (
          <ErrorBanner
            message={detailError}
            onRetry={() => setDetailReloadKey((key) => key + 1)}
          />
        ) : detail ? (
          <>
            <div className="form-grid">
              <DetailItem label="姓名" value={detail.name} />
              <DetailItem label="状态" value={<ElderStatusBadge status={detail.status} />} />
              <DetailItem label="房间" value={textOrDash(detail.room_no)} />
              <DetailItem label="床位" value={textOrDash(detail.bed_no)} />
              <DetailItem label="出生日期" value={textOrDash(detail.birth_date)} />
              <DetailItem label="性别" value={textOrDash(detail.gender)} />
              <DetailItem label="人设" value={personaLabel(detail.persona_id)} />
              <DetailItem label="会话序号" value={textOrDash(detail.session_seq)} />
              <DetailItem
                label="绑定设备"
                value={detail.device_count === undefined ? "—" : `${detail.device_count} 台`}
              />
              <DetailItem
                label="家属"
                value={detail.guardian_count === undefined ? "—" : `${detail.guardian_count} 位`}
              />
              <DetailItem label="建档时间" value={formatTime(detail.created_at)} />
              <DetailItem label="档案编号" value={<span className="cell-muted">{detail.elder_id}</span>} />
            </div>

            {canWrite ? (
              <>
                <div className="toolbar" style={{ marginTop: 20, marginBottom: 0 }}>
                  <Field label="修改状态" htmlFor="elder-status-edit">
                    <Select
                      id="elder-status-edit"
                      value={statusDraft}
                      onChange={(value) => {
                        if (isElderStatus(value)) setStatusDraft(value);
                      }}
                      options={ELDER_STATUS_OPTIONS}
                      disabled={statusSaving}
                    />
                  </Field>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => {
                      void saveStatus();
                    }}
                    disabled={statusSaving || !statusChanged}
                  >
                    {statusSaving ? "保存中…" : "保存状态"}
                  </button>
                </div>
                {statusError ? (
                  <p className="helper-error" role="alert">
                    {statusError}
                  </p>
                ) : null}
                {statusSaved && !statusError ? <p className="field-hint">状态已更新</p> : null}

                <div className="toolbar" style={{ marginTop: 20, marginBottom: 0 }}>
                  <Field
                    label="形象"
                    htmlFor="elder-persona-edit"
                    hint="人设与形象一一对应；保存后长者端下次加载即换人。"
                  >
                    <Select
                      id="elder-persona-edit"
                      value={personaDraft}
                      onChange={setPersonaDraft}
                      options={personaOptions}
                      disabled={personaSaving || personas.length === 0}
                    />
                  </Field>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => {
                      void savePersona();
                    }}
                    disabled={personaSaving || !personaChanged}
                  >
                    {personaSaving ? "保存中…" : "保存形象"}
                  </button>
                </div>
                {personasError ? (
                  <p className="helper-error" role="alert">
                    形象列表加载失败：{personasError}
                  </p>
                ) : null}
                {personaError ? (
                  <p className="helper-error" role="alert">
                    {personaError}
                  </p>
                ) : null}
                {personaSaved && !personaError ? <p className="field-hint">形象已更新</p> : null}
                <PersonaPreview persona={selectedPersona} catalog={catalogModels} />
              </>
            ) : null}
          </>
        ) : null}
      </Dialog>

      <Dialog
        open={checkoutOpen}
        title={checkoutReceipt ? "退房回执" : `办理退房${detail ? ` · ${detail.name}` : ""}`}
        onClose={closeCheckout}
        width={560}
        footer={
          checkoutReceipt ? (
            <button type="button" className="btn-primary" onClick={finishCheckout}>
              完成
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn-secondary"
                onClick={closeCheckout}
                disabled={checkoutSaving}
              >
                取消
              </button>
              <button
                type="button"
                className="btn-danger"
                onClick={() => {
                  void submitCheckout();
                }}
                disabled={checkoutSaving || !checkoutConfirmed}
              >
                {checkoutSaving ? "正在退房…" : "确认退房"}
              </button>
            </>
          )
        }
      >
        {checkoutReceipt ? (
          <>
            <Notice role="status">
              退房流水线已完成。下方计数是本次实际变更；重复办理时计数全 0、不产生新审计。
            </Notice>
            <div className="form-grid">
              <DetailItem
                label="房态结果"
                value={checkoutReceipt.archived ? "已归档（archived）" : "外出（away）"}
              />
              <DetailItem label="清理记忆" value={`${checkoutReceipt.memories_purged} 条`} />
              <DetailItem label="清除声纹" value={`${checkoutReceipt.voiceprints_removed} 条`} />
              <DetailItem label="解绑设备" value={`${checkoutReceipt.devices_unbound} 台`} />
              <DetailItem label="吊销工牌" value={`${checkoutReceipt.staff_cards_revoked} 张`} />
              <DetailItem label="关闭会话" value={`${checkoutReceipt.sessions_closed} 个`} />
              <DetailItem label="解除关系" value={`${checkoutReceipt.relations_removed} 条`} />
              <DetailItem
                label="审计编号"
                value={
                  checkoutReceipt.audit_log_id ? (
                    <span className="cell-muted" title={checkoutReceipt.audit_log_id}>
                      {checkoutReceipt.audit_log_id}
                    </span>
                  ) : (
                    "本次未产生新变更"
                  )
                }
              />
            </div>
          </>
        ) : (
          <>
            {checkoutError ? <ErrorBanner message={checkoutError} /> : null}
            {detail ? (
              <p className="checkout-summary">
                {detail.name} · {textOrDash(detail.room_no)} · {textOrDash(detail.bed_no)}
              </p>
            ) : null}
            <div className="checkout-warning">
              <strong>此操作不可撤销。</strong>提交后该长者的设备、会话、记忆与声纹会被立即清理，
              家属与护工关系解除；审计记录保留。
            </div>
            <ul className="checkout-impact">
              <li>解绑名下设备，设备旧令牌立即失效；</li>
              <li>吊销已签发的工牌会话；</li>
              <li>软删长期记忆、清除声纹样本；</li>
              <li>结束进行中的会话；</li>
              <li>解除家属与护工关系（保留审计痕迹）。</li>
            </ul>
            <Field label="退房原因" htmlFor="elder-checkout-reason">
              <Select
                id="elder-checkout-reason"
                value={checkoutReason}
                onChange={setCheckoutReason}
                options={CHECKOUT_REASON_OPTIONS}
                disabled={checkoutSaving}
              />
            </Field>
            <label className="checkout-check">
              <input
                type="checkbox"
                checked={checkoutArchive}
                onChange={(event) => setCheckoutArchive(event.target.checked)}
                disabled={checkoutSaving}
              />
              同时归档档案（不再出现在在住列表）
            </label>
            <label className="checkout-check">
              <input
                type="checkbox"
                checked={checkoutConfirmed}
                onChange={(event) => setCheckoutConfirmed(event.target.checked)}
                disabled={checkoutSaving}
              />
              我已核对长者身份与离院事由，确认执行退房
            </label>
          </>
        )}
      </Dialog>

      <Dialog
        open={createOpen}
        title="新建长者档案"
        onClose={closeCreate}
        width={640}
        footer={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={closeCreate}
              disabled={submitting}
            >
              取消
            </button>
            <button
              type="submit"
              form="elder-create-form"
              className="btn-primary"
              disabled={submitting}
            >
              {submitting ? "保存中…" : "创建"}
            </button>
          </>
        }
      >
        <form id="elder-create-form" onSubmit={submitCreate} noValidate>
          <FormGrid>
            <Field label="姓名" htmlFor="elder-name" required error={nameError}>
              <input
                id="elder-name"
                className="input"
                value={form.name}
                aria-invalid={nameError ? true : undefined}
                onChange={(event) => updateField("name", event.target.value)}
                placeholder="如：王秀兰"
                autoComplete="off"
              />
            </Field>
            <Field label="出生日期" htmlFor="elder-birth">
              <input
                id="elder-birth"
                className="input"
                type="date"
                value={form.birth_date}
                onChange={(event) => updateField("birth_date", event.target.value)}
              />
            </Field>
            <Field label="性别" htmlFor="elder-gender">
              <input
                id="elder-gender"
                className="input"
                value={form.gender}
                onChange={(event) => updateField("gender", event.target.value)}
                placeholder="如：女"
                autoComplete="off"
              />
            </Field>
            <Field label="形象" htmlFor="elder-persona" hint="可选，留空则由小满兜底形象上阵；建档后还能在详情里换。">
              <Select
                id="elder-persona"
                value={form.persona_id}
                onChange={(value) => updateField("persona_id", value)}
                options={personaOptions}
                disabled={personas.length === 0}
              />
            </Field>
          </FormGrid>
          {formError ? (
            <p className="helper-error" role="alert" style={{ marginTop: 16 }}>
              {formError}
            </p>
          ) : null}
        </form>
      </Dialog>
    </section>
  );
}

/**
 * 形象预览（W1 · FR-3D-03）：只读 `catalog.json` 的元信息，**不渲染 3D**。
 *
 * 为什么不在照护台里画 VRM：three-vrm 只能进 pad 构建包（AGENTS §2 第 4 条），
 * console 引一次就会把整个 three 依赖拖进照护台产物。这里回答「换的是哪张脸、
 * 多大、性别」三个问题足够；立体效果到长者端看。
 */
function PersonaPreview({
  persona,
  catalog,
}: {
  persona: PersonaOption | null;
  catalog: CatalogModel[];
}) {
  if (!persona) {
    return (
      <div className="card" style={{ marginTop: 12 }}>
        <p className="field-hint" style={{ margin: 0 }}>
          未指定人设：长者端使用小满兜底形象（/avatars/vrm1_sample.vrm）。
        </p>
      </div>
    );
  }
  const model = persona.avatar_url
    ? catalog.find((item) => item.url === persona.avatar_url) ?? null
    : null;
  const genderText = model ? (model.gender === "male" ? "男" : "女") : null;
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div className="form-grid">
        <div className="field">
          <label>人设</label>
          <p style={{ margin: 0 }}>{persona.name}</p>
        </div>
        <div className="field">
          <label>形象</label>
          <p style={{ margin: 0 }}>
            {model
              ? `${model.display_name}（${genderText} · ${formatBytes(model.bytes)}）`
              : persona.avatar_url
                ? "未收录在 catalog.json"
                : "跟随小满兜底形象"}
          </p>
        </div>
      </div>
      <p className="field-hint" style={{ marginBottom: 0 }}>
        {model
          ? `${model.file}${model.character ? ` · ${model.character}` : ""} · 3D 效果在长者端呈现`
          : "预览仅展示模型元信息；3D 效果在长者端呈现。"}
      </p>
    </div>
  );
}

/**
 * 读静态 catalog 拿模型元信息；目录缺失/解析失败/非 200 一律返回空数组——
 * 预览退化成「未收录」，绝不能因为目录问题让档案页报错。
 */
async function fetchCatalogModels(): Promise<CatalogModel[]> {
  try {
    const res = await fetch("/avatars/catalog.json", { cache: "force-cache" });
    if (!res.ok) return [];
    const data = (await res.json()) as { models?: unknown };
    if (!Array.isArray(data.models)) return [];
    return data.models.filter((item): item is CatalogModel => {
      const row = item as Partial<CatalogModel> | null;
      return Boolean(row && typeof row.url === "string" && typeof row.display_name === "string");
    });
  } catch {
    return [];
  }
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
