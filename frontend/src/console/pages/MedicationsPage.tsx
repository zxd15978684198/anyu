/**
 * 用药管理页（迭代 11 补：把医生开药单的入口从占位页变成可用页）。
 *
 * 为什么这一页现在才做：迭代 7 交付了用药后端（药单 / 事件 / 调度 / 子图 / 代确认），
 * 迭代 8 只放了一张只读占位页（裁决 S-D8-3「用药模块属 迭代 7 交付」），此后没人回来把它接上，
 * 于是「医生唯一专属写权限」在后端可用、在界面上没有入口。本页就是补这段接线。
 *
 * 从上到下：**建议关注**（FR-HLT-10）→ **多药合并卡片** + **药单**（列表 + 医生新建/改/停）
 * → **用药事件**（列表 + 状态筛选 + 代确认）；页头另有**批量导入**入口（FR-MED-07，两段式）。
 *
 * 口径：
 * - 角色只决定「按钮显不显示」，服务端每个写端点仍独立校验（`permissions.ts` 铁律）；
 * - 药单可「改」可「停」，但只有医生看得到这两个按钮：改复用同一个 Dialog 的编辑模式
 *   （预填 + 带 `expected_version`），停用走二次确认；服务端 `WRITING_ROLES = {doctor}`
 *   独立校验，前端显隐只是体贴，不是权限；
 * - 批量导入**强制两段式**：先「预览」（`dry_run=true`，只预检不写库）看逐行失败清单与
 *   将写入条数，预览通过后才允许「确认导入」（`dry_run=false`）；没有一步直写的按钮；
 * - 请求层在 `console/medications.ts`（`previewImport` / `applyImport` / `fetchMergedCards`），
 *   本页只负责选文件、展示逐行结果与触发下载，不在页面里另拼一套请求；
 * - 代确认只在事件还没有终态时给按钮；服务端对已终态返回 409，页面按错误码显示原文；
 * - 护工/医生只看负责长者，范围由服务端 403 兜底，页面只用 `elderScopeNote` 提示。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Principal } from "../api";
import { listElders, type ElderListItem } from "../elders";
import { HttpError, apiGet, apiSend, errorText, query } from "../http";
import { can, elderScopeNote } from "../permissions";
import { Badge, EmptyState, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { EventConfirmDialog } from "../medications/EventConfirmDialog";
import { PlanStopDialog } from "../medications/PlanStopDialog";
import { ImportWizard } from "../medications/ImportWizard";
import { PlanFormDialog } from "../medications/PlanFormDialog";
import {
  type AttentionReport,
  type CreateErrors,
  type CreateForm,
  REVIEW_WINDOW_DAYS,
  RENEW_DURATION_DAYS,
  daysUntil,
  emptyCreateForm,
  todayIso,
  validateCreate,
} from "../medications/model";
import { Field, Select } from "../ui/Form";
import type { Column } from "../ui/Table";
import { DataTable } from "../ui/Table";
import {
  EVENT_STATUS_OPTIONS,
  IMPORT_TEMPLATE_FILENAME,
  VERSION_CONFLICT_TEXT,
  applyImport,
  canConfirm,
  channelLabel,
  confirmEvent,
  createPlan,
  downloadImportTemplate,
  eventStatusLabel,
  eventStatusTone,
  fetchMergedCards,
  isAlreadyStopped,
  listEvents,
  listPlans,
  planStatusLabel,
  planStatusTone,
  previewImport,
  scheduleText,
  stopPlan,
  terminalStateLabel,
  terminalStateTone,
  timeText,
  updatePlan,
  windowText,
  type MedicationEventItem,
  type MedicationImportResult,
  type MedicationMergedCards,
  type MedicationPlanItem,
} from "../medications";

const SECTION_GAP = { marginBottom: 16 } as const;


export function MedicationsPage({ principal }: { principal: Principal }) {
  const scopeNote = elderScopeNote(principal);
  // 服务端 `WRITING_ROLES = {doctor}`：管理员即便有 `medication:create` 能力位，写端点
  // 仍 403，因此写入口按**角色**显隐（`permissions.ts` 的 ADMIN_CAPS 含该能力位）。
  const canCreate = can(principal, "medication:create") && principal.role === "doctor";
  const canDoConfirm = can(principal, "medication:confirm");

  const [elders, setElders] = useState<ElderListItem[]>([]);
  const [eldersError, setEldersError] = useState<string | null>(null);
  const [elderId, setElderId] = useState("");

  const [plans, setPlans] = useState<MedicationPlanItem[]>([]);
  const [plansLoading, setPlansLoading] = useState(false);
  const [plansError, setPlansError] = useState<string | null>(null);

  const [events, setEvents] = useState<MedicationEventItem[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [eventStatus, setEventStatus] = useState("");

  const [formOpen, setFormOpen] = useState(false);
  //: 非空 = 编辑模式（同一 Dialog 复用）；空 = 新建。提交时用它带 `expected_version`。
  const [editing, setEditing] = useState<MedicationPlanItem | null>(null);
  const [form, setForm] = useState<CreateForm>(emptyCreateForm);
  const [formErrors, setFormErrors] = useState<CreateErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [stopTarget, setStopTarget] = useState<MedicationPlanItem | null>(null);
  const [stopReason, setStopReason] = useState("");
  const [stopBusy, setStopBusy] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);

  //: 续方（FR-MED-09）：按下就新开一条药单，旧的原地自然到期；只给医生。
  const [renewBusyId, setRenewBusyId] = useState<string | null>(null);
  const [renewError, setRenewError] = useState<string | null>(null);

  const [confirmTarget, setConfirmTarget] = useState<MedicationEventItem | null>(null);
  const [confirmNote, setConfirmNote] = useState("");
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  // 批量导入（FR-MED-07）：两段式——先预检（`dry_run=true`）看逐行失败清单与将写入条数，
  // 预览通过后再确认导入（`dry_run=false`）；页面不提供「一步直写」的按钮。
  const [importOpen, setImportOpen] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  /** 第一段结果（`dry_run=true`）：有它且 `accepted` 才允许点「确认导入」。 */
  const [importPreview, setImportPreview] = useState<MedicationImportResult | null>(null);
  /** 第二段结果（`dry_run=false`）：非空表示这批已经落库，不再给「确认导入」。 */
  const [importApplied, setImportApplied] = useState<MedicationImportResult | null>(null);
  const importInputRef = useRef<HTMLInputElement | null>(null);

  // 多药合并卡片（FR-MED-07 后半）与聚合关注信号（FR-HLT-10）。
  const [merged, setMerged] = useState<MedicationMergedCards | null>(null);
  const [mergedLoading, setMergedLoading] = useState(false);
  const [mergedError, setMergedError] = useState<string | null>(null);

  const [attention, setAttention] = useState<AttentionReport | null>(null);
  const [attentionLoading, setAttentionLoading] = useState(false);
  const [attentionError, setAttentionError] = useState<string | null>(null);

  /** 导入弹窗当前要展示的那一份结果：应用过就展示应用结果，否则展示预览结果。 */
  const importResult = importApplied ?? importPreview;

  // 到期复核（FR-MED-09）：到期是**时间事实**，不是新状态——药单只有 active/stopped 两种，
  // 所以「7 天内到期 / 已过期未续」按 end_date 与今天（Asia/Shanghai）现算，不回写数据库；
  // 长期医嘱（end_date 为空）与已停用药单不进任何一组。
  const today = todayIso();
  const { dueSoonPlans, expiredPlans } = useMemo(() => {
    const dueSoon: MedicationPlanItem[] = [];
    const expired: MedicationPlanItem[] = [];
    for (const plan of plans) {
      if (plan.status !== "active" || !plan.end_date) continue;
      const days = daysUntil(plan.end_date, today);
      if (days === null) continue;
      if (days < 0) expired.push(plan);
      else if (days <= REVIEW_WINDOW_DAYS) dueSoon.push(plan);
    }
    const byEndDate = (a: MedicationPlanItem, b: MedicationPlanItem) =>
      (a.end_date ?? "").localeCompare(b.end_date ?? "");
    return { dueSoonPlans: [...dueSoon].sort(byEndDate), expiredPlans: [...expired].sort(byEndDate) };
  }, [plans, today]);

  // 长者列表：护工/医生拿到的本来就是「负责范围」，管理员拿全量，页面不做二次过滤。
  const loadElders = useCallback(async () => {
    setEldersError(null);
    try {
      const data = await listElders({ page: 1, pageSize: 100, status: "" });
      setElders(data.items);
      setElderId((current) => current || data.items[0]?.elder_id || "");
    } catch (err) {
      setEldersError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void loadElders();
  }, [loadElders]);

  const loadPlans = useCallback(async () => {
    if (!elderId) return;
    setPlansLoading(true);
    setPlansError(null);
    try {
      const data = await listPlans(elderId);
      setPlans(data.items);
    } catch (err) {
      setPlansError(errorText(err));
    } finally {
      setPlansLoading(false);
    }
  }, [elderId]);

  const loadEvents = useCallback(async () => {
    if (!elderId) return;
    setEventsLoading(true);
    setEventsError(null);
    try {
      const data = await listEvents(elderId, eventStatus);
      setEvents(data.items);
    } catch (err) {
      setEventsError(errorText(err));
    } finally {
      setEventsLoading(false);
    }
  }, [elderId, eventStatus]);

  useEffect(() => {
    void loadPlans();
  }, [loadPlans]);

  useEffect(() => {
    void loadEvents();
  }, [loadEvents]);

  const loadMerged = useCallback(async () => {
    if (!elderId) {
      setMerged(null);
      return;
    }
    setMergedLoading(true);
    setMergedError(null);
    try {
      setMerged(await fetchMergedCards(elderId));
    } catch (err) {
      setMergedError(errorText(err));
    } finally {
      setMergedLoading(false);
    }
  }, [elderId]);

  const loadAttention = useCallback(async () => {
    if (!elderId) {
      setAttention(null);
      return;
    }
    setAttentionLoading(true);
    setAttentionError(null);
    try {
      setAttention(await apiGet<AttentionReport>(`/api/medication/attention${query({ elder_id: elderId })}`));
    } catch (err) {
      setAttentionError(errorText(err));
    } finally {
      setAttentionLoading(false);
    }
  }, [elderId]);

  useEffect(() => {
    void loadMerged();
  }, [loadMerged]);

  useEffect(() => {
    void loadAttention();
  }, [loadAttention]);

  const elderOptions = useMemo(
    () => elders.map((elder) => ({ value: elder.elder_id, label: `${elder.name}（${elder.room_no ?? "—"}）` })),
    [elders],
  );

  function openCreate() {
    setEditing(null);
    setForm(emptyCreateForm());
    setFormErrors({});
    setSubmitError(null);
    setNotice(null);
    setFormOpen(true);
  }

  /** 编辑模式：同一个 Dialog，预填当前值，提交时带 `expected_version` 做乐观锁。 */
  function openEdit(plan: MedicationPlanItem) {
    setEditing(plan);
    setForm({
      drug_name: plan.drug_name,
      dose: plan.dose,
      schedule_cron: plan.schedule_cron,
      start_date: plan.start_date,
      end_date: plan.end_date ?? "",
    });
    setFormErrors({});
    setSubmitError(null);
    setNotice(null);
    setFormOpen(true);
  }

  function closeForm() {
    setFormOpen(false);
    setEditing(null);
  }

  async function handleSubmit() {
    const errors = validateCreate(form);
    setFormErrors(errors);
    if (Object.keys(errors).length > 0) return;
    setSubmitting(true);
    setSubmitError(null);
    const payload = {
      drug_name: form.drug_name.trim(),
      dose: form.dose.trim(),
      schedule_cron: form.schedule_cron,
      start_date: form.start_date,
      end_date: form.end_date ? form.end_date : null,
    };
    try {
      if (editing) {
        await updatePlan(editing.plan_id, { ...payload, expected_version: editing.version });
        setNotice("药单已修改，版本号已递增；历史提醒事件仍保留当时的药名与剂量快照。");
      } else {
        await createPlan({ elder_id: elderId, ...payload });
        setNotice("药单已创建。调度器会在每日到点触发提醒。");
      }
      closeForm();
      await loadPlans();
    } catch (err) {
      setSubmitError(
        err instanceof HttpError && err.code === "VERSION_CONFLICT"
          ? VERSION_CONFLICT_TEXT
          : errorText(err),
      );
    } finally {
      setSubmitting(false);
    }
  }

  function openConfirm(event: MedicationEventItem) {
    setConfirmTarget(event);
    setConfirmNote("");
    setConfirmError(null);
  }

  async function handleConfirm() {
    if (!confirmTarget) return;
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      const result = await confirmEvent(confirmTarget.reminder_id, confirmNote.trim() || null);
      setConfirmTarget(null);
      setNotice(
        result.terminal_state === "taken_reported"
          ? "已代确认服药（渠道记为照护台按钮，可回查）。"
          : `已代确认，终态：${terminalStateLabel(result.terminal_state)}。`,
      );
      await loadEvents();
    } catch (err) {
      setConfirmError(errorText(err));
    } finally {
      setConfirmBusy(false);
    }
  }

  function openStop(plan: MedicationPlanItem) {
    setStopTarget(plan);
    setStopReason("");
    setStopError(null);
  }

  async function handleStop() {
    if (!stopTarget) return;
    setStopBusy(true);
    setStopError(null);
    try {
      const result = await stopPlan(stopTarget.plan_id, stopReason.trim() || null);
      setNotice(
        isAlreadyStopped(result)
          ? "该药单已是停用状态，本次未重复改动。"
          : "药单已停用：不会再生成新的服药提醒，历史事件与审计保留。",
      );
      setStopTarget(null);
      await loadPlans();
    } catch (err) {
      setStopError(errorText(err));
    } finally {
      setStopBusy(false);
    }
  }

  async function handleRenew(plan: MedicationPlanItem) {
    setRenewBusyId(plan.plan_id);
    setRenewError(null);
    try {
      // 续方请求直连服务端点：服务端负责日期推算（旧到期日次日 → +30 天）、
      // 幂等闸门与审计；页面只把结果说清楚，不自己算结束日期。
      await apiSend<MedicationPlanItem>(`/api/medication/plans/${plan.plan_id}/renew`, "POST");
      setNotice(
        `已为「${plan.drug_name}」续方：新药单从 ${plan.end_date ?? "旧到期日"} 次日开始再开 ${RENEW_DURATION_DAYS} 天，旧药单保持启用、按自己的到期日自然结束。`,
      );
      await Promise.all([loadPlans(), loadMerged()]);
    } catch (err) {
      setRenewError(errorText(err));
    } finally {
      setRenewBusyId(null);
    }
  }

  /** 复核组里的一行：药名 + 剂量 + 每日时刻 + 区间，医生给「续方」按钮，其他人只读。 */
  function renderReviewList(rows: MedicationPlanItem[]) {
    return (
      <ul style={{ listStyle: "none", padding: 0, margin: "6px 0 0", display: "grid", gap: 6 }}>
        {rows.map((row) => (
          <li
            key={row.plan_id}
            style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}
          >
            <strong>{row.drug_name}</strong>
            <span className="cell-muted">{row.dose}</span>
            <span className="cell-muted">{scheduleText(row.schedule_cron)}</span>
            <span className="cell-muted">{windowText(row.start_date, row.end_date)}</span>
            {canCreate ? (
              <button
                type="button"
                className="btn-secondary btn-sm"
                disabled={renewBusyId === row.plan_id}
                onClick={() => void handleRenew(row)}
              >
                {renewBusyId === row.plan_id ? "续方中…" : "续方"}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    );
  }

  function openImport() {
    setImportFile(null);
    setImportPreview(null);
    setImportApplied(null);
    setImportError(null);
    setImportOpen(true);
  }

  function closeImport() {
    if (importBusy) return;
    setImportOpen(false);
  }

  /** 换文件即作废旧结论：预览只对「那次选中的文件」有效，不能拿来确认另一份。 */
  function pickImportFile(file: File | null) {
    setImportFile(file);
    setImportPreview(null);
    setImportApplied(null);
    setImportError(null);
  }

  /**
   * 第一段 · 预检（`?dry_run=true`）：一个字都不写库。
   * 失败行原样留在 `importPreview.errors`（行号 + 字段 + 原因），不做「导入失败」的笼统提示。
   */
  async function handlePreviewImport() {
    if (!importFile) {
      setImportError("请先选择 CSV 或 XLSX 文件");
      return;
    }
    setImportBusy(true);
    setImportError(null);
    setImportApplied(null);
    try {
      setImportPreview(await previewImport(importFile, elderId));
    } catch (err) {
      setImportPreview(null);
      setImportError(errorText(err));
    } finally {
      setImportBusy(false);
    }
  }

  /**
   * 第二段 · 应用（`?dry_run=false`）：真的落库。
   * 入口只在「预览已通过」时放开；服务端仍会再跑一遍预检，不过就整批不写。
   */
  async function handleApplyImport() {
    if (!importFile || !importPreview?.accepted) return;
    setImportBusy(true);
    setImportError(null);
    try {
      const result = await applyImport(importFile, elderId);
      setImportApplied(result);
      if (result.accepted) {
        setNotice(
          `导入完成：新建 ${result.created.length} 条药单` +
            (result.skipped.length > 0 ? `，跳过重复 ${result.skipped.length} 条。` : "。"),
        );
        // 就地刷新：药单列表与今日合并卡片都会受这批新药单影响。
        await Promise.all([loadPlans(), loadMerged()]);
      }
    } catch (err) {
      setImportError(errorText(err));
    } finally {
      setImportBusy(false);
    }
  }

  /** 模板走鉴权请求拿字节流，再交给浏览器存盘（不新开窗口、也不把 token 拼进 URL）。 */
  async function handleDownloadTemplate() {
    setImportError(null);
    try {
      const blob = await downloadImportTemplate();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = IMPORT_TEMPLATE_FILENAME;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setImportError(errorText(err));
    }
  }

  const basePlanColumns: Column<MedicationPlanItem>[] = [
    { key: "drug_name", title: "药名", width: "20%" },
    { key: "dose", title: "剂量", width: "12%" },
    {
      key: "schedule_cron",
      title: "每日时刻",
      width: "12%",
      render: (row) => <span className="cell-muted">{scheduleText(row.schedule_cron)}</span>,
    },
    {
      key: "window",
      title: "服药区间",
      render: (row) => <span className="cell-muted">{windowText(row.start_date, row.end_date)}</span>,
    },
    {
      key: "status",
      title: "状态",
      width: "10%",
      render: (row) => <Badge tone={planStatusTone(row.status)}>{planStatusLabel(row.status)}</Badge>,
    },
    { key: "version", title: "版本", width: "8%", render: (row) => `v${row.version}` },
  ];

  /** 「改」「停」只给医生：护工与管理员是只读视图（服务端仍独立校验角色）。 */
  const planActionColumn: Column<MedicationPlanItem> = {
    key: "actions",
    title: "操作",
    width: "16%",
    align: "right",
    render: (row) =>
      row.status === "active" ? (
        <span className="cell-actions">
          <button type="button" className="btn-secondary btn-sm" onClick={() => openEdit(row)}>
            改
          </button>
          <button type="button" className="btn-danger btn-sm" onClick={() => openStop(row)}>
            停
          </button>
        </span>
      ) : (
        <span className="cell-muted">—</span>
      ),
  };

  const planColumns: Column<MedicationPlanItem>[] = canCreate
    ? [...basePlanColumns, planActionColumn]
    : basePlanColumns;

  const eventColumns: Column<MedicationEventItem>[] = [
    {
      key: "scheduled_at",
      title: "计划时刻",
      width: "18%",
      render: (row) => <span className="cell-muted">{timeText(row.scheduled_at)}</span>,
    },
    {
      key: "status",
      title: "流转态",
      width: "12%",
      render: (row) => <Badge tone={eventStatusTone(row.status)}>{eventStatusLabel(row.status)}</Badge>,
    },
    {
      key: "terminal_state",
      title: "终态",
      width: "16%",
      render: (row) => (
        <Badge tone={terminalStateTone(row.terminal_state)}>
          {terminalStateLabel(row.terminal_state)}
        </Badge>
      ),
    },
    {
      key: "retry_count",
      title: "触达次数",
      width: "10%",
      render: (row) => `${row.retry_count}`,
    },
    {
      key: "confirm",
      title: "确认",
      render: (row) =>
        row.confirmed_at ? (
          <span className="cell-muted">
            {timeText(row.confirmed_at)} · {channelLabel(row.confirm_channel)}
          </span>
        ) : row.late_taken ? (
          <span className="cell-muted">补服</span>
        ) : (
          <span className="cell-muted">—</span>
        ),
    },
    {
      key: "actions",
      title: "操作",
      width: "14%",
      align: "right",
      render: (row) =>
        canDoConfirm && canConfirm(row) ? (
          <span className="cell-actions">
            <button type="button" className="btn-secondary btn-sm" onClick={() => openConfirm(row)}>
              代确认
            </button>
          </span>
        ) : (
          <span className="cell-muted">—</span>
        ),
    },
  ];

  return (
    <section>
      <header className="page-head">
        <div>
          <h1 className="page-title">用药管理</h1>
          <p className="page-sub">
            药单由医生开具；护工与管理员可查、可代确认；服药提醒按每日时刻由调度器触达。
          </p>
        </div>
        <div className="page-actions">
          {canCreate ? (
            <>
              <button type="button" className="btn-secondary" onClick={openImport} disabled={!elderId}>
                批量导入
              </button>
              <button type="button" className="btn-primary" onClick={openCreate} disabled={!elderId}>
                新建药单
              </button>
            </>
          ) : null}
        </div>
      </header>

      {scopeNote ? <Notice>{scopeNote}</Notice> : null}
      {!canCreate ? (
        <Notice>
          药单只能由医生开具，本页对您是可读视图；确需改动请让医生在照护台操作。
        </Notice>
      ) : null}

      {eldersError ? <ErrorBanner message={eldersError} onRetry={() => void loadElders()} /> : null}

      <div style={SECTION_GAP}>
        <Field label="长者" htmlFor="med-elder">
          <Select
            id="med-elder"
            value={elderId}
            onChange={setElderId}
            options={elderOptions.length > 0 ? elderOptions : [{ value: "", label: "暂无可选长者" }]}
            disabled={elderOptions.length === 0}
          />
        </Field>
      </div>

      {notice ? (
        <Notice role="status">
          {notice}
        </Notice>
      ) : null}

      {elderId ? (
        <div style={SECTION_GAP}>
          <h2 className="page-title" style={{ fontSize: 18, marginBottom: 8 }}>
            建议关注
          </h2>
          {attentionError ? (
            <ErrorBanner message={attentionError} onRetry={() => void loadAttention()} />
          ) : attentionLoading ? (
            <LoadingState text="正在汇总关注信号…" />
          ) : attention ? (
            <>
              {attention.signals.length === 0 ? (
                <Notice>近 {attention.window_days} 天没有聚合关注信号。</Notice>
              ) : (
                <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 8 }}>
                  {attention.signals.map((signal) => (
                    <li key={signal.code} className="note-banner" style={{ margin: 0 }}>
                      <Badge tone="warning">{signal.title}</Badge>
                      <div style={{ marginTop: 4 }}>{signal.detail}</div>
                      <div className="cell-muted">{signal.suggestion}</div>
                    </li>
                  ))}
                </ul>
              )}
              <p className="field-hint">{attention.disclaimer}</p>
            </>
          ) : null}
        </div>
      ) : null}

      {elderId ? (
        <div style={SECTION_GAP}>
          <h2 className="page-title" style={{ fontSize: 18, marginBottom: 8 }}>
            多药合并卡片
          </h2>
          {mergedError ? (
            <ErrorBanner message={mergedError} onRetry={() => void loadMerged()} />
          ) : mergedLoading ? (
            <LoadingState text="正在汇总今日服药时刻…" />
          ) : merged && merged.slots.length > 0 ? (
            <>
              <Notice>
                {merged.on_date} 共 {merged.total_slots} 个服药时刻；同一时刻的多味药合并成一次播报。
              </Notice>
              <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 8 }}>
                {merged.slots.map((slot) => (
                  <li key={slot.schedule_cron} className="note-banner" style={{ margin: 0 }}>
                    <strong>{slot.schedule_cron}</strong> · 共 {slot.drug_count} 种药
                    <div className="cell-muted">{slot.summary}</div>
                    {/* 一张卡 = 一个时刻：卡内逐味列出药名与剂量，护工看一眼就知道「这几味一起吃」。 */}
                    <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                      {slot.drugs.map((drug) => (
                        <li key={drug.plan_id}>
                          {drug.drug_name}
                          <span className="cell-muted">　{drug.dose}</span>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
              <p className="field-hint">
                只统计启用中且服药区间覆盖 {merged.on_date} 的药单，按每日时刻升序；本区只读，不出现任何写操作。
              </p>
            </>
          ) : (
            <Notice>今天没有在服用的药单。</Notice>
          )}
        </div>
      ) : null}

      {elderId ? (
        <div style={SECTION_GAP}>
          <h2 className="page-title" style={{ fontSize: 18, marginBottom: 8 }}>
            到期复核
          </h2>
          {renewError ? (
            <Notice style={{ marginBottom: 8 }}>
              {renewError}
            </Notice>
          ) : null}
          {dueSoonPlans.length === 0 && expiredPlans.length === 0 ? (
            <Notice>
              近 {REVIEW_WINDOW_DAYS} 天没有到期的药单，也没有过期未续的药单。
            </Notice>
          ) : (
            <div style={{ display: "grid", gap: 8 }}>
              {expiredPlans.length > 0 ? (
                <div className="note-banner" style={{ margin: 0 }}>
                  <Badge tone="danger">已过期未续 · {expiredPlans.length} 条</Badge>
                  <div className="cell-muted" style={{ marginTop: 4 }}>
                    到期日已过、仍在启用的药单不再生成新的服药提醒；续方会从旧到期日次日接着开。
                  </div>
                  {renderReviewList(expiredPlans)}
                </div>
              ) : null}
              {dueSoonPlans.length > 0 ? (
                <div className="note-banner" style={{ margin: 0 }}>
                  <Badge tone="warning">
                    {REVIEW_WINDOW_DAYS} 天内到期 · {dueSoonPlans.length} 条
                  </Badge>
                  <div className="cell-muted" style={{ marginTop: 4 }}>
                    到期不等于停药：确认还要继续吃的，提前续方；停用请走「停」。
                  </div>
                  {renderReviewList(dueSoonPlans)}
                </div>
              ) : null}
            </div>
          )}
          <p className="field-hint">
            续方只复制药名、剂量与每日时刻：新药单从旧到期日次日起再开 {RENEW_DURATION_DAYS} 天，
            旧药单保持启用、按自己的到期日自然结束，历史提醒与审计都留在旧药单上。
          </p>
        </div>
      ) : null}

      <h2 className="page-title" style={{ fontSize: 18, marginBottom: 8 }}>
        药单
      </h2>
      <DataTable<MedicationPlanItem>
        columns={planColumns}
        rows={plans}
        rowKey={(row) => row.plan_id}
        loading={plansLoading}
        error={plansError}
        onRetry={() => void loadPlans()}
        caption="药单列表"
        emptyTitle={elderId ? "该长者暂无药单" : "请先选择长者"}
        emptyDescription={
          elderId
            ? canCreate
              ? "点右上角「新建药单」开始开具。"
              : "医生开具后即可在此查看。"
            : undefined
        }
      />

      <h2 className="page-title" style={{ fontSize: 18, margin: "24px 0 8px" }}>
        用药事件
      </h2>
      <div style={{ ...SECTION_GAP, maxWidth: 240 }}>
        <Field label="按状态筛选" htmlFor="med-event-status">
          <Select
            id="med-event-status"
            value={eventStatus}
            onChange={setEventStatus}
            options={EVENT_STATUS_OPTIONS}
          />
        </Field>
      </div>
      <DataTable<MedicationEventItem>
        columns={eventColumns}
        rows={events}
        rowKey={(row) => row.reminder_id}
        loading={eventsLoading}
        error={eventsError}
        onRetry={() => void loadEvents()}
        caption="用药事件列表"
        emptyTitle="还没有用药事件"
        emptyDescription="调度器按药单的每日时刻生成事件；到点后这里会出现触达与确认记录。"
      />

      <PlanFormDialog
        open={formOpen}
        editing={editing}
        form={form}
        errors={formErrors}
        submitting={submitting}
        submitError={submitError}
        onChange={setForm}
        onClose={closeForm}
        onSubmit={() => void handleSubmit()}
      />

      <EventConfirmDialog
        target={confirmTarget}
        note={confirmNote}
        busy={confirmBusy}
        error={confirmError}
        onNoteChange={setConfirmNote}
        onClose={() => setConfirmTarget(null)}
        onSubmit={() => void handleConfirm()}
      />

      <PlanStopDialog
        target={stopTarget}
        reason={stopReason}
        busy={stopBusy}
        error={stopError}
        onReasonChange={setStopReason}
        onClose={() => setStopTarget(null)}
        onSubmit={() => void handleStop()}
      />

      <ImportWizard
        open={importOpen}
        busy={importBusy}
        error={importError}
        file={importFile}
        result={importResult}
        applied={importApplied !== null}
        inputRef={importInputRef}
        onClose={closeImport}
        onPickFile={pickImportFile}
        onPreview={() => void handlePreviewImport()}
        onApply={() => void handleApplyImport()}
        onDownloadTemplate={() => void handleDownloadTemplate()}
      />

      {elderId && plans.length === 0 && !plansLoading && !plansError && !canCreate ? (
        <EmptyState
          title="没有可显示的药单"
          description="若确认该长者应有药单，请让医生在自己的账号下核对。"
        />
      ) : null}
    </section>
  );
}
