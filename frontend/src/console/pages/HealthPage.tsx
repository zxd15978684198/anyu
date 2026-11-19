/**
 * 健康采集页（迭代 8 · W4）。
 *
 * 三块从上到下：**黄/红复核队列**（health_review 通知）→ **五字段代填** → **近 14 天趋势**，
 * 中间夹一张「当日记录」表，给补录（FR-HLT-04，追加版本行）留入口。
 *
 * 口径：
 * - 分级、越权、状态机全部由服务端判定，页面只做展示与「按状态显隐按钮」；
 * - 护工/医生只看负责长者（`elderScopeNote` 提示 + 服务端 403 兜底），**不靠前端隐藏数据**；
 * - 表单跳过的那一问发 `null`（服务端判 missing 黄），页面不自己算 grade。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Principal } from "../api";
import { errorText } from "../http";
import { listElders, textOrDash, type ElderListItem } from "../elders";
import { can, elderScopeNote } from "../permissions";
import { Badge, EmptyState, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { Field, FormGrid, Select } from "../ui/Form";
import { Dialog } from "../ui/Dialog";
import { Pagination } from "../ui/Pagination";
import type { Column } from "../ui/Table";
import { DataTable } from "../ui/Table";
import { TrendChart } from "../components/TrendChart";
import { formatDateTimeFull, todayInShanghai } from "../../shared/format";
import {
  HEALTH_PAGE_SIZE,
  STAFF_SOURCE_OPTIONS,
  diastolicOf,
  fetchHealthTrend,
  gradeLabel,
  gradeTone,
  heartRateOf,
  listHealthRecords,
  moodScoreOf,
  resolveHealthReview,
  revokeHealthRecord,
  sleepHoursOf,
  sourceLabel,
  submitHealthRecord,
  supersedeHealthRecord,
  systolicOf,
  type HealthRecordItem,
  type HealthTrendPoint,
  type StaffHealthSource,
} from "../health";
import {
  ackNotification,
  canDeliver,
  deliverNotification,
  listNotifications,
  payloadSummary,
  severityLabel,
  severityTone,
  statusLabel,
  statusTone,
  type NotificationItem,
} from "../notifications";

const SECTION_GAP = { marginBottom: 16 } as const;

/** 上海自然日的今天。原实现用 getTimezoneOffset() 折算，是**浏览器本地时区**，
 *  护工机器时区一变「今天」就错一天（AGENTS §8）。三页各写一份，现收敛到 shared/format。 */
function todayIso(): string {
  return todayInShanghai();
}

function formatTime(value?: string | null): string {
  return formatDateTimeFull(value);
}

interface FormState {
  systolic: string;
  diastolic: string;
  heart_rate: string;
  sleep_hours: string;
  mood_score: string;
  complaint: string;
  source: StaffHealthSource;
  note: string;
}

const EMPTY_FORM: FormState = {
  systolic: "",
  diastolic: "",
  heart_rate: "",
  sleep_hours: "",
  mood_score: "",
  complaint: "",
  source: "staff_scheduled",
  note: "",
};

type FormErrors = Partial<Record<"systolic" | "diastolic" | "heart_rate" | "sleep_hours" | "mood_score", string>>;

/** 只挡「不可能是血压/心率/睡眠/情绪」的值；分级阈值是服务端的事，前端不重复一遍。 */
function parseNumber(raw: string, kind: "int" | "float", min: number, max: number, label: string) {
  const text = raw.trim();
  if (!text) return { value: null as number | null, error: null as string | null };
  const value = Number(text);
  if (!Number.isFinite(value) || (kind === "int" && !Number.isInteger(value))) {
    return { value: null as number | null, error: `${label}要填数字` };
  }
  if (value < min || value > max) {
    return { value: null as number | null, error: `${label}超出范围（${min}–${max}）` };
  }
  return { value, error: null as string | null };
}

export function HealthPage({ principal }: { principal: Principal }) {
  const scopeNote = elderScopeNote(principal);
  const canRevoke = can(principal, "health:revoke");

  // ---- 长者列表（下拉 + 名称映射） ----
  const [elders, setElders] = useState<ElderListItem[]>([]);
  const [eldersLoading, setEldersLoading] = useState(true);
  const [eldersError, setEldersError] = useState<string | null>(null);
  const [elderId, setElderId] = useState("");

  // ---- 当日记录 ----
  const [dateFilter, setDateFilter] = useState(todayIso);
  const [records, setRecords] = useState<HealthRecordItem[]>([]);
  const [recordTotal, setRecordTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const [recordsError, setRecordsError] = useState<string | null>(null);
  const [recordsKey, setRecordsKey] = useState(0);

  // ---- 趋势 ----
  const [trend, setTrend] = useState<HealthTrendPoint[]>([]);
  const [trendLoading, setTrendLoading] = useState(false);
  const [trendError, setTrendError] = useState<string | null>(null);
  const [trendKey, setTrendKey] = useState(0);

  // ---- 复核队列 ----
  const [queue, setQueue] = useState<NotificationItem[]>([]);
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [queueKey, setQueueKey] = useState(0);
  const [queueBusy, setQueueBusy] = useState<string | null>(null);

  // ---- 代填表单 ----
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [formErrors, setFormErrors] = useState<FormErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ grade: string; reasons: string[] } | null>(null);

  // ---- 补录弹窗 ----
  const [supplement, setSupplement] = useState<HealthRecordItem | null>(null);
  const [supplementForm, setSupplementForm] = useState<FormState>(EMPTY_FORM);
  const [supplementError, setSupplementError] = useState<string | null>(null);
  const [supplementSaving, setSupplementSaving] = useState(false);

  // ---- 撤销弹窗（医生/管理员） ----
  const [revokeTarget, setRevokeTarget] = useState<HealthRecordItem | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [revokeSaving, setRevokeSaving] = useState(false);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const elderMap = (() => {
    const map: Record<string, string> = {};
    for (const elder of elders) map[elder.elder_id] = elder.name;
    return map;
  })();

  const loadElders = useCallback(async () => {
    setEldersLoading(true);
    setEldersError(null);
    try {
      const data = await listElders({ page: 1, pageSize: 100, status: "" });
      if (!mounted.current) return;
      setElders(data.items);
      setElderId((current) => current || data.items[0]?.elder_id || "");
    } catch (err) {
      if (!mounted.current) return;
      setEldersError(errorText(err));
    } finally {
      if (mounted.current) setEldersLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadElders();
  }, [loadElders]);

  const loadQueue = useCallback(async () => {
    setQueueLoading(true);
    setQueueError(null);
    try {
      const data = await listNotifications({ page: 1, pageSize: HEALTH_PAGE_SIZE, kind: "health_review" });
      if (!mounted.current) return;
      setQueue(data.items);
    } catch (err) {
      if (!mounted.current) return;
      setQueueError(errorText(err));
    } finally {
      if (mounted.current) setQueueLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadQueue();
  }, [loadQueue, queueKey]);

  const loadRecords = useCallback(async () => {
    if (!elderId) {
      setRecords([]);
      setRecordTotal(0);
      return;
    }
    setRecordsLoading(true);
    setRecordsError(null);
    try {
      const data = await listHealthRecords({
        elderId,
        date: dateFilter,
        page,
        pageSize: HEALTH_PAGE_SIZE,
      });
      if (!mounted.current) return;
      setRecords(data.items);
      setRecordTotal(data.total);
    } catch (err) {
      if (!mounted.current) return;
      setRecordsError(errorText(err));
    } finally {
      if (mounted.current) setRecordsLoading(false);
    }
  }, [elderId, dateFilter, page]);

  useEffect(() => {
    void loadRecords();
  }, [loadRecords, recordsKey]);

  const loadTrend = useCallback(async () => {
    if (!elderId) {
      setTrend([]);
      return;
    }
    setTrendLoading(true);
    setTrendError(null);
    try {
      const points = await fetchHealthTrend(elderId);
      if (!mounted.current) return;
      setTrend(points);
    } catch (err) {
      if (!mounted.current) return;
      setTrendError(errorText(err));
    } finally {
      if (mounted.current) setTrendLoading(false);
    }
  }, [elderId]);

  useEffect(() => {
    void loadTrend();
  }, [loadTrend, trendKey]);

  const refreshAll = () => {
    setRecordsKey((key) => key + 1);
    setTrendKey((key) => key + 1);
    setQueueKey((key) => key + 1);
  };

  function updateForm<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
    setFormErrors((current) => ({ ...current, [key]: undefined }));
    setResult(null);
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!elderId) {
      setFormError("请先选择长者");
      return;
    }
    const checks = {
      systolic: parseNumber(form.systolic, "int", 40, 300, "收缩压"),
      diastolic: parseNumber(form.diastolic, "int", 20, 200, "舒张压"),
      heart_rate: parseNumber(form.heart_rate, "int", 20, 250, "心率"),
      sleep_hours: parseNumber(form.sleep_hours, "float", 0, 24, "睡眠小时"),
      mood_score: parseNumber(form.mood_score, "int", 1, 5, "情绪分"),
    };
    const nextErrors: FormErrors = {};
    for (const [key, check] of Object.entries(checks)) {
      if (check.error) nextErrors[key as keyof FormErrors] = check.error;
    }
    if (Object.keys(nextErrors).length > 0) {
      setFormErrors(nextErrors);
      setFormError(null);
      return;
    }
    if (
      checks.systolic.value !== null &&
      checks.diastolic.value !== null &&
      checks.systolic.value <= checks.diastolic.value
    ) {
      setFormErrors({ systolic: "收缩压应高于舒张压" });
      return;
    }

    setSubmitting(true);
    setFormError(null);
    try {
      const response = await submitHealthRecord({
        elder_id: elderId,
        systolic: checks.systolic.value,
        diastolic: checks.diastolic.value,
        heart_rate: checks.heart_rate.value,
        sleep_hours: checks.sleep_hours.value,
        mood_score: checks.mood_score.value,
        source: form.source,
        note: form.note,
        complaint: form.complaint,
      });
      if (!mounted.current) return;
      setResult({
        grade: response.grade || "",
        reasons: (response.abnormal_reasons ?? []).slice(0, 6),
      });
      setForm((current) => ({ ...EMPTY_FORM, source: current.source }));
      setRecordsKey((key) => key + 1);
      setTrendKey((key) => key + 1);
      setQueueKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setFormError(errorText(err));
    } finally {
      if (mounted.current) setSubmitting(false);
    }
  }

  function openSupplement(record: HealthRecordItem) {
    setSupplement(record);
    setSupplementError(null);
    setSupplementForm({
      systolic: systolicOf(record) === null ? "" : String(systolicOf(record)),
      diastolic: diastolicOf(record) === null ? "" : String(diastolicOf(record)),
      heart_rate: heartRateOf(record) === null ? "" : String(heartRateOf(record)),
      sleep_hours: sleepHoursOf(record) === null ? "" : String(sleepHoursOf(record)),
      mood_score: moodScoreOf(record) === null ? "" : String(moodScoreOf(record)),
      complaint: "",
      source: (record.source as StaffHealthSource) === "staff_active" ? "staff_active" : "staff_scheduled",
      note: "",
    });
  }

  async function saveSupplement(event: React.FormEvent) {
    event.preventDefault();
    if (!supplement) return;
    const checks = {
      systolic: parseNumber(supplementForm.systolic, "int", 40, 300, "收缩压"),
      diastolic: parseNumber(supplementForm.diastolic, "int", 20, 200, "舒张压"),
      heart_rate: parseNumber(supplementForm.heart_rate, "int", 20, 250, "心率"),
      sleep_hours: parseNumber(supplementForm.sleep_hours, "float", 0, 24, "睡眠小时"),
      mood_score: parseNumber(supplementForm.mood_score, "int", 1, 5, "情绪分"),
    };
    const nextErrors: FormErrors = {};
    for (const [key, check] of Object.entries(checks)) {
      if (check.error) nextErrors[key as keyof FormErrors] = check.error;
    }
    if (Object.keys(nextErrors).length > 0) {
      setSupplementError(Object.values(nextErrors)[0] ?? "请检查填写内容");
      return;
    }
    setSupplementSaving(true);
    setSupplementError(null);
    try {
      await supersedeHealthRecord(supplement.record_id, {
        systolic: checks.systolic.value,
        diastolic: checks.diastolic.value,
        heart_rate: checks.heart_rate.value,
        sleep_hours: checks.sleep_hours.value,
        mood_score: checks.mood_score.value,
        note: supplementForm.note,
      });
      if (!mounted.current) return;
      setSupplement(null);
      setRecordsKey((key) => key + 1);
      setTrendKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setSupplementError(errorText(err));
    } finally {
      if (mounted.current) setSupplementSaving(false);
    }
  }

  function openRevoke(record: HealthRecordItem) {
    setRevokeTarget(record);
    setRevokeError(null);
  }

  async function confirmRevoke() {
    if (!revokeTarget) return;
    setRevokeSaving(true);
    setRevokeError(null);
    try {
      await revokeHealthRecord(revokeTarget.record_id);
      if (!mounted.current) return;
      setRevokeTarget(null);
      setRecordsKey((key) => key + 1);
      setTrendKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setRevokeError(errorText(err));
    } finally {
      if (mounted.current) setRevokeSaving(false);
    }
  }

  async function actOnNotification(item: NotificationItem, action: "deliver" | "ack" | "resolve") {
    setQueueBusy(item.notification_id);
    setQueueError(null);
    try {
      if (action === "deliver") await deliverNotification(item.notification_id);
      else if (action === "ack") await ackNotification(item.notification_id);
      else await resolveHealthReview(item.notification_id);
      if (!mounted.current) return;
      setQueueKey((key) => key + 1);
    } catch (err) {
      if (!mounted.current) return;
      setQueueError(errorText(err));
    } finally {
      if (mounted.current) setQueueBusy(null);
    }
  }

  const columns: Column<HealthRecordItem>[] = [
    { key: "record_date", title: "日期", width: "110px" },
    { key: "source", title: "来源", render: (row) => sourceLabel(row.source) },
    { key: "systolic", title: "收缩压", align: "right", render: (row) => textOrDash(systolicOf(row)) },
    { key: "diastolic", title: "舒张压", align: "right", render: (row) => textOrDash(diastolicOf(row)) },
    { key: "heart_rate", title: "心率", align: "right", render: (row) => textOrDash(heartRateOf(row)) },
    { key: "sleep_hours", title: "睡眠(h)", align: "right", render: (row) => textOrDash(sleepHoursOf(row)) },
    { key: "mood_score", title: "情绪(1–5)", align: "right", render: (row) => textOrDash(moodScoreOf(row)) },
    {
      key: "grade",
      title: "分级",
      render: (row) => <Badge tone={gradeTone(row.grade)}>{gradeLabel(row.grade)}</Badge>,
    },
    {
      key: "revision",
      title: "版本",
      align: "right",
      render: (row) => `v${row.revision ?? 1}${row.superseded_by ? "（已失效）" : ""}`,
    },
    {
      key: "actions",
      title: "操作",
      align: "right",
      render: (row) => {
        if (row.revoked_at) {
          return <span className="cell-muted">已撤销 · {formatTime(row.revoked_at)}</span>;
        }
        if (row.superseded_by) {
          return <span className="cell-muted">已被补录取代</span>;
        }
        return (
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
            <button type="button" className="btn-secondary btn-sm" onClick={() => openSupplement(row)}>
              补录
            </button>
            {canRevoke ? (
              <button type="button" className="btn-danger btn-sm" onClick={() => openRevoke(row)}>
                撤销
              </button>
            ) : null}
          </div>
        );
      },
    },
  ];

  const elderOptions = elders.map((elder) => ({
    value: elder.elder_id,
    label: elder.room_no ? `${elder.name}（${elder.room_no}）` : elder.name,
  }));

  const pendingQueue = queue.filter((item) => item.status !== "acknowledged" && item.status !== "failed");

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">健康采集</h1>
        <p className="page-sub">黄/红复核 · 五字段代填 · 近 14 天趋势</p>
        <div className="page-actions">
          <button type="button" className="btn-secondary btn-sm" onClick={refreshAll}>
            刷新
          </button>
        </div>
      </header>

      {scopeNote ? <Notice>{scopeNote}</Notice> : null}

      {eldersError ? (
        <ErrorBanner message={eldersError} onRetry={() => void loadElders()} />
      ) : null}

      {eldersLoading ? <LoadingState text="正在加载长者列表…" /> : null}

      {!eldersLoading && !eldersError && elders.length === 0 ? (
        <EmptyState
          title="还没有长者档案"
          description="先到「长者档案」建一位长者，健康采集才有代填对象。"
        />
      ) : null}

      {elders.length > 0 ? (
        <>
          {/* ---- 黄/红复核队列 ---- */}
          <div className="card" style={SECTION_GAP}>
            <header className="page-head" style={{ marginBottom: 8 }}>
              <h2 className="page-title" style={{ fontSize: 17 }}>
                黄/红复核队列
              </h2>
              <p className="page-sub">
                health_review 通知 · 待处理 {pendingQueue.length} 条
              </p>
            </header>
            {queueError ? (
              <ErrorBanner message={queueError} onRetry={() => void loadQueue()} />
            ) : null}
            {queueLoading ? <LoadingState text="正在加载复核队列…" /> : null}
            {!queueLoading && queue.length === 0 ? (
              <p className="page-sub">当前没有待复核的红/黄档记录。</p>
            ) : null}
            {queue.length > 0 ? (
              <ul className="queue-list">
                {queue.map((item) => {
                  const busy = queueBusy === item.notification_id;
                  return (
                    <li key={item.notification_id} className="queue-item">
                      <div className="queue-main">
                        <div className="queue-title">
                          <Badge tone={severityTone(item.severity)}>{severityLabel(item.severity)}</Badge>
                          <span className="queue-elder">
                            {item.elder_id ? elderMap[item.elder_id] ?? `长者 ${item.elder_id.slice(0, 8)}` : "系统通知"}
                          </span>
                          <Badge tone={statusTone(item.status)}>{statusLabel(item.status)}</Badge>
                        </div>
                        <p className="queue-meta">
                          {payloadSummary(item.payload)} · {formatTime(item.created_at)}
                        </p>
                      </div>
                      <div className="queue-actions">
                        {canDeliver(item.status) ? (
                          <button
                            type="button"
                            className="btn-secondary btn-sm"
                            disabled={busy}
                            onClick={() => void actOnNotification(item, "deliver")}
                          >
                            推送/重发
                          </button>
                        ) : null}
                        {item.status === "delivered" ? (
                          <button
                            type="button"
                            className="btn-secondary btn-sm"
                            disabled={busy}
                            onClick={() => void actOnNotification(item, "ack")}
                          >
                            确认已阅
                          </button>
                        ) : null}
                        {item.status !== "acknowledged" ? (
                          <button
                            type="button"
                            className="btn-primary btn-sm"
                            disabled={busy}
                            onClick={() => void actOnNotification(item, "resolve")}
                          >
                            标记已处理
                          </button>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </div>

          {/* ---- 五字段代填 ---- */}
          <div className="card" style={SECTION_GAP}>
            <header className="page-head" style={{ marginBottom: 8 }}>
              <h2 className="page-title" style={{ fontSize: 17 }}>
                代填健康数据
              </h2>
              <p className="page-sub">
                五个数值字段 + 第 5 问主诉；分级由服务端引擎判定，页面不自己算
              </p>
            </header>

            {formError ? <ErrorBanner message={formError} /> : null}

            <form onSubmit={submit} noValidate>
              <FormGrid>
                <Field label="长者" required>
                  <Select value={elderId} onChange={setElderId} options={elderOptions} disabled={submitting} />
                </Field>
                <Field label="来源" required hint="语音渠道自动写入，不由照护台代填">
                  <Select
                    value={form.source}
                    onChange={(value) => updateForm("source", value as StaffHealthSource)}
                    options={STAFF_SOURCE_OPTIONS}
                    disabled={submitting}
                  />
                </Field>
                <Field label="收缩压（mmHg）" error={formErrors.systolic} hint="留空表示本轮未采集">
                  <input
                    className="input"
                    inputMode="numeric"
                    value={form.systolic}
                    aria-invalid={formErrors.systolic ? true : undefined}
                    onChange={(event) => updateForm("systolic", event.target.value)}
                    placeholder="例如 128"
                  />
                </Field>
                <Field label="舒张压（mmHg）" error={formErrors.diastolic}>
                  <input
                    className="input"
                    inputMode="numeric"
                    value={form.diastolic}
                    aria-invalid={formErrors.diastolic ? true : undefined}
                    onChange={(event) => updateForm("diastolic", event.target.value)}
                    placeholder="例如 82"
                  />
                </Field>
                <Field label="静息心率（bpm）" error={formErrors.heart_rate}>
                  <input
                    className="input"
                    inputMode="numeric"
                    value={form.heart_rate}
                    aria-invalid={formErrors.heart_rate ? true : undefined}
                    onChange={(event) => updateForm("heart_rate", event.target.value)}
                    placeholder="例如 72"
                  />
                </Field>
                <Field label="昨夜睡眠（小时）" error={formErrors.sleep_hours}>
                  <input
                    className="input"
                    inputMode="decimal"
                    value={form.sleep_hours}
                    aria-invalid={formErrors.sleep_hours ? true : undefined}
                    onChange={(event) => updateForm("sleep_hours", event.target.value)}
                    placeholder="例如 6.5"
                  />
                </Field>
                <Field label="情绪自评（1–5）" error={formErrors.mood_score} hint="1 很低落，5 很好">
                  <input
                    className="input"
                    inputMode="numeric"
                    value={form.mood_score}
                    aria-invalid={formErrors.mood_score ? true : undefined}
                    onChange={(event) => updateForm("mood_score", event.target.value)}
                    placeholder="例如 4"
                  />
                </Field>
                <Field
                  label="主诉 / 不适（第 5 问）"
                  hint="「今天身体有什么不舒服？」填「无」等同未填；有主诉等级不低于黄"
                >
                  <input
                    className="input"
                    value={form.complaint}
                    onChange={(event) => updateForm("complaint", event.target.value)}
                    placeholder="例如 无 / 说有点头晕"
                  />
                </Field>
                <Field label="备注" hint="可留空；不写诊断结论">
                  <input
                    className="input"
                    value={form.note}
                    onChange={(event) => updateForm("note", event.target.value)}
                    placeholder="例如 家属代述，昨晚睡得少"
                  />
                </Field>
              </FormGrid>

              <div className="form-actions">
                <button type="submit" className="btn-primary" disabled={submitting}>
                  {submitting ? "提交中…" : "提交代填"}
                </button>
              </div>
            </form>

            {result ? (
              <p className="submit-result" role="status">
                已记录 · <Badge tone={gradeTone(result.grade)}>{gradeLabel(result.grade)}</Badge>
                {result.reasons.length > 0 ? (
                  <span className="submit-reasons">依据：{result.reasons.join("、")}</span>
                ) : null}
              </p>
            ) : null}
          </div>

          {/* ---- 当日记录 ---- */}
          <div className="card" style={SECTION_GAP}>
            <header className="page-head" style={{ marginBottom: 12 }}>
              <h2 className="page-title" style={{ fontSize: 17 }}>
                采集记录
              </h2>
              <p className="page-sub">补录会追加新版本行，原值保留</p>
              <div className="page-actions">
                <Field label="日期">
                  <input
                    className="input"
                    type="date"
                    value={dateFilter}
                    onChange={(event) => {
                      setDateFilter(event.target.value);
                      setPage(1);
                    }}
                  />
                </Field>
              </div>
            </header>
            <DataTable
              columns={columns}
              rows={records}
              rowKey={(row) => row.record_id}
              loading={recordsLoading}
              error={recordsError}
              onRetry={() => setRecordsKey((key) => key + 1)}
              emptyTitle="这一天还没有采集记录"
              emptyDescription="用上方表单代填一次，或等长者在语音端完成 19:00 采集。"
              caption="健康采集记录列表"
            />
            {recordTotal > HEALTH_PAGE_SIZE ? (
              <Pagination
                page={page}
                pageSize={HEALTH_PAGE_SIZE}
                total={recordTotal}
                onChange={setPage}
                disabled={recordsLoading}
              />
            ) : null}
          </div>

          {/* ---- 趋势 ---- */}
          <div className="card">
            <header className="page-head" style={{ marginBottom: 12 }}>
              <h2 className="page-title" style={{ fontSize: 17 }}>
                近 14 天趋势
              </h2>
              <p className="page-sub">收缩压 / 舒张压双线，色点为当次分级</p>
            </header>
            {trendError ? (
              <ErrorBanner message={trendError} onRetry={() => setTrendKey((key) => key + 1)} />
            ) : null}
            {trendLoading ? <LoadingState text="正在加载趋势…" /> : <TrendChart points={trend} />}
          </div>
        </>
      ) : null}

      <Dialog
        open={supplement !== null}
        title="补录（追加新版本）"
        onClose={() => setSupplement(null)}
        width={640}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setSupplement(null)}>
              取消
            </button>
            <button
              type="submit"
              form="health-supplement-form"
              className="btn-primary"
              disabled={supplementSaving}
            >
              {supplementSaving ? "保存中…" : "保存补录"}
            </button>
          </>
        }
      >
        {supplementError ? <ErrorBanner message={supplementError} /> : null}
        <p className="page-sub" style={{ marginBottom: 16 }}>
          原记录：{supplement?.record_date ?? "—"} · {sourceLabel(supplement?.source ?? "")} · 版本 v
          {supplement?.revision ?? 1}（补录不覆盖原值，旧版本会标记为已失效）
        </p>
        <form id="health-supplement-form" onSubmit={saveSupplement} noValidate>
          <FormGrid>
            <Field label="收缩压（mmHg）">
              <input
                className="input"
                inputMode="numeric"
                value={supplementForm.systolic}
                onChange={(event) => setSupplementForm((c) => ({ ...c, systolic: event.target.value }))}
              />
            </Field>
            <Field label="舒张压（mmHg）">
              <input
                className="input"
                inputMode="numeric"
                value={supplementForm.diastolic}
                onChange={(event) => setSupplementForm((c) => ({ ...c, diastolic: event.target.value }))}
              />
            </Field>
            <Field label="静息心率（bpm）">
              <input
                className="input"
                inputMode="numeric"
                value={supplementForm.heart_rate}
                onChange={(event) => setSupplementForm((c) => ({ ...c, heart_rate: event.target.value }))}
              />
            </Field>
            <Field label="昨夜睡眠（小时）">
              <input
                className="input"
                inputMode="decimal"
                value={supplementForm.sleep_hours}
                onChange={(event) => setSupplementForm((c) => ({ ...c, sleep_hours: event.target.value }))}
              />
            </Field>
            <Field label="情绪自评（1–5）">
              <input
                className="input"
                inputMode="numeric"
                value={supplementForm.mood_score}
                onChange={(event) => setSupplementForm((c) => ({ ...c, mood_score: event.target.value }))}
              />
            </Field>
            <Field label="补录说明" hint="会写入审计，请写清为什么改">
              <input
                className="input"
                value={supplementForm.note}
                onChange={(event) => setSupplementForm((c) => ({ ...c, note: event.target.value }))}
                placeholder="例如 长者口述舒张压报错，家属更正"
              />
            </Field>
          </FormGrid>
        </form>
      </Dialog>

      <Dialog
        open={revokeTarget !== null}
        title="撤销健康记录"
        onClose={() => setRevokeTarget(null)}
        width={520}
        footer={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setRevokeTarget(null)}
              disabled={revokeSaving}
            >
              取消
            </button>
            <button
              type="button"
              className="btn-danger"
              onClick={() => void confirmRevoke()}
              disabled={revokeSaving}
            >
              {revokeSaving ? "正在撤销…" : "确认撤销"}
            </button>
          </>
        }
      >
        {revokeError ? <ErrorBanner message={revokeError} /> : null}
        <p>
          将撤销 <strong>{revokeTarget?.record_date ?? "—"}</strong>
          （{sourceLabel(revokeTarget?.source ?? "")} · 版本 v{revokeTarget?.revision ?? 1}）这条记录。
        </p>
        <p className="cell-muted">
          撤销是软删：记录与审计保留，但不再进入日报与趋势，也不能在本页恢复。
          已撤销或已被补录版本顶替的记录会被服务端拒绝（409）。
        </p>
      </Dialog>
    </section>
  );
}
