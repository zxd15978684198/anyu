/**
 * 家属日报查阅页（迭代 8 · W4）。
 *
 * 只读：长者 + 日期 → 服务端已生成的 `daily_reports.payload`。
 * 三个区块按服务端给的结构渲染，**不在前端重算**：
 * 1. 健康块：当日绿/黄/红计数 + 来源计数 + 最新一条五字段；
 * 2. 用药块：迭代 7 前固定占位（灰底禁用感），只显示服务端 summary；
 * 3. 近 30 天列表：给一个换日期的入口，免得靠记忆输日期。
 *
 * 权限：admin / doctor（care_worker 403 由服务端兜底，导航层已按 `reports_view` 隐藏）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Principal } from "../api";
import { HttpError, errorText } from "../http";
import { listElders, textOrDash, type ElderListItem } from "../elders";
import { elderScopeNote } from "../permissions";
import { Badge, EmptyState, ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { Field, Select } from "../ui/Form";
import type { Column } from "../ui/Table";
import { DataTable } from "../ui/Table";
import { gradeLabel, gradeTone, sourceLabel, type HealthGrade } from "../health";
import { formatDateTimeFull, todayInShanghai } from "../../shared/format";
import {
  MEDICATION_PLACEHOLDER_TEXT,
  countOf,
  getReport,
  healthBlockOf,
  listReports,
  medicationBlockOf,
  medicationCountRows,
  sourceRows,
  type DailyReport,
} from "../reports";

const SECTION_GAP = { marginBottom: 16 } as const;
const GRADE_ORDER: HealthGrade[] = ["green", "yellow", "red", "missing"];

/** 上海自然日的今天。原实现用 getTimezoneOffset() 折算，是**浏览器本地时区**，
 *  护工机器时区一变「今天」就错一天（AGENTS §8：展示一律 Asia/Shanghai）。 */
function todayIso(): string {
  return todayInShanghai();
}

function formatTime(value?: string | null): string {
  return formatDateTimeFull(value);
}

export function ReportsPage({ principal }: { principal: Principal }) {
  const scopeNote = elderScopeNote(principal);
  const [elders, setElders] = useState<ElderListItem[]>([]);
  const [elderId, setElderId] = useState("");
  const [date, setDate] = useState(todayIso);
  const [eldersLoading, setEldersLoading] = useState(true);
  const [eldersError, setEldersError] = useState<string | null>(null);

  const [report, setReport] = useState<DailyReport | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [reportKey, setReportKey] = useState(0);

  const [recent, setRecent] = useState<DailyReport[]>([]);
  const [recentLoading, setRecentLoading] = useState(false);
  const [recentError, setRecentError] = useState<string | null>(null);
  const [recentKey, setRecentKey] = useState(0);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

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

  const loadReport = useCallback(async () => {
    if (!elderId || !date) {
      setReport(null);
      return;
    }
    setReportLoading(true);
    setReportError(null);
    setMissing(false);
    try {
      const data = await getReport(elderId, date);
      if (!mounted.current) return;
      setReport(data);
    } catch (err) {
      if (!mounted.current) return;
      setReport(null);
      if (err instanceof HttpError && err.code === "NOT_FOUND") {
        setMissing(true);
      } else {
        setReportError(errorText(err));
      }
    } finally {
      if (mounted.current) setReportLoading(false);
    }
  }, [elderId, date]);

  useEffect(() => {
    void loadReport();
  }, [loadReport, reportKey]);

  const loadRecent = useCallback(async () => {
    if (!elderId) {
      setRecent([]);
      return;
    }
    setRecentLoading(true);
    setRecentError(null);
    try {
      const data = await listReports(elderId, 30);
      if (!mounted.current) return;
      setRecent(data.items);
    } catch (err) {
      if (!mounted.current) return;
      setRecentError(errorText(err));
    } finally {
      if (mounted.current) setRecentLoading(false);
    }
  }, [elderId]);

  useEffect(() => {
    void loadRecent();
  }, [loadRecent, recentKey]);

  const health = healthBlockOf(report?.payload);
  const medication = medicationBlockOf(report?.payload);
  const medicationRows = medicationCountRows(medication);
  const latest = health?.latest ?? null;

  const elderOptions = elders.map((elder) => ({
    value: elder.elder_id,
    label: elder.room_no ? `${elder.name}（${elder.room_no}）` : elder.name,
  }));

  const recentColumns: Column<DailyReport>[] = [
    { key: "report_date", title: "日报日期", width: "120px" },
    { key: "generated_at", title: "生成时间", render: (row) => formatTime(row.generated_at) },
    {
      key: "counts",
      title: "健康档位",
      render: (row) => {
        const block = healthBlockOf(row.payload);
        if (!block) return "—";
        if (block.state === "missing") return <span className="cell-muted">当日未采集</span>;
        return GRADE_ORDER.map((grade) =>
          countOf(block, grade) > 0 ? (
            <Badge key={grade} tone={gradeTone(grade)}>
              {gradeLabel(grade)} {countOf(block, grade)}
            </Badge>
          ) : null,
        );
      },
    },
    {
      key: "actions",
      title: "操作",
      align: "right",
      render: (row) => (
        <button
          type="button"
          className="btn-secondary btn-sm"
          onClick={() => {
            setDate(row.report_date);
            setReport(row);
            setMissing(false);
            setReportError(null);
          }}
        >
          查看
        </button>
      ),
    },
  ];

  return (
    <section>
      <header className="page-head">
        <h1 className="page-title">每日日报</h1>
        <p className="page-sub">按长者 + 日期查阅已生成日报 · 用药块 迭代 7 前为占位</p>
        <div className="page-actions">
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => {
              setReportKey((key) => key + 1);
              setRecentKey((key) => key + 1);
            }}
          >
            刷新
          </button>
        </div>
      </header>

      {scopeNote ? <Notice>{scopeNote}</Notice> : null}

      {eldersError ? <ErrorBanner message={eldersError} onRetry={() => void loadElders()} /> : null}
      {eldersLoading ? <LoadingState text="正在加载长者列表…" /> : null}
      {!eldersLoading && !eldersError && elders.length === 0 ? (
        <EmptyState title="还没有长者档案" description="先到「长者档案」建一位长者，日报才有查询对象。" />
      ) : null}

      {elders.length > 0 ? (
        <>
          <div className="card" style={SECTION_GAP}>
            <div className="toolbar">
              <Field label="长者" htmlFor="report-elder">
                <Select id="report-elder" value={elderId} onChange={setElderId} options={elderOptions} />
              </Field>
              <Field label="日期" htmlFor="report-date">
                <input
                  id="report-date"
                  className="input"
                  type="date"
                  value={date}
                  onChange={(event) => setDate(event.target.value)}
                />
              </Field>
              <button type="button" className="btn-secondary btn-sm" onClick={() => setReportKey((k) => k + 1)}>
                查询
              </button>
            </div>

            {reportError ? (
              <ErrorBanner message={reportError} onRetry={() => setReportKey((k) => k + 1)} />
            ) : null}
            {reportLoading ? <LoadingState text="正在加载日报…" /> : null}
            {!reportLoading && missing ? (
              <EmptyState
                title="该日期还没有日报"
                description="日报由每日 7:00 调度生成；也可以等调度跑完后点「刷新」。"
              />
            ) : null}
          </div>

          {!reportLoading && report ? (
            <>
              <div className="card" style={SECTION_GAP}>
                <header className="page-head" style={{ marginBottom: 12 }}>
                  <h2 className="page-title" style={{ fontSize: 17 }}>
                    健康块 · {report.report_date}
                  </h2>
                  <p className="page-sub">生成时间 {formatTime(report.generated_at)}</p>
                </header>

                <div className="metric-row">
                  {GRADE_ORDER.map((grade) => (
                    <div key={grade} className="metric-item">
                      <span className="metric-value">{countOf(health, grade)}</span>
                      <Badge tone={gradeTone(grade)}>{gradeLabel(grade)}</Badge>
                    </div>
                  ))}
                  <div className="metric-item">
                    <span className="metric-value">{health?.total ?? 0}</span>
                    <span className="cell-muted">条记录</span>
                  </div>
                </div>

                {health?.state === "missing" ? (
                  <Notice style={{ marginTop: 12 }}>
                    当日一条采集记录都没有（缺失按黄档处理，由调度 20:00 派单补采）。
                  </Notice>
                ) : null}

                <div className="report-grid">
                  <div>
                    <h3 className="report-subtitle">来源标注</h3>
                    {sourceRows(health).length === 0 ? (
                      <p className="cell-muted">暂无来源计数</p>
                    ) : (
                      <ul className="plain-list">
                        {sourceRows(health).map((row) => (
                          <li key={row.source}>
                            {sourceLabel(row.source)} <strong>{row.count}</strong> 条
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>

                  <div>
                    <h3 className="report-subtitle">最新一条五字段</h3>
                    {latest ? (
                      <table className="data-table table-compact">
                        <tbody>
                          <tr>
                            <th scope="row">收缩压</th>
                            <td>{textOrDash(latest.systolic_mmhg)}</td>
                            <th scope="row">舒张压</th>
                            <td>{textOrDash(latest.diastolic_mmhg)}</td>
                          </tr>
                          <tr>
                            <th scope="row">心率</th>
                            <td>{textOrDash(latest.heart_rate_bpm)}</td>
                            <th scope="row">睡眠(h)</th>
                            <td>{textOrDash(latest.sleep_hours)}</td>
                          </tr>
                          <tr>
                            <th scope="row">情绪(1–5)</th>
                            <td>{textOrDash(latest.mood_score)}</td>
                            <th scope="row">分级</th>
                            <td>
                              <Badge tone={gradeTone(latest.grade)}>{gradeLabel(latest.grade)}</Badge>
                            </td>
                          </tr>
                          <tr>
                            <th scope="row">来源</th>
                            <td>{sourceLabel(latest.source)}</td>
                            <th scope="row">采集时间</th>
                            <td>{formatTime(latest.collected_at)}</td>
                          </tr>
                        </tbody>
                      </table>
                    ) : (
                      <p className="cell-muted">当日没有生效版本的记录</p>
                    )}
                  </div>
                </div>
              </div>

              <div className="card" style={SECTION_GAP}>
                <header className="page-head" style={{ marginBottom: 12 }}>
                  <h2 className="page-title" style={{ fontSize: 17 }}>
                    用药块
                  </h2>
                  <p className="page-sub">四类终态分列，不合并为「实际漏服」</p>
                </header>
                {medicationRows.length > 0 ? (
                  <>
                    <p className="cell-muted">
                      当日到点事件 {medication?.total ?? 0} 条 · 在用长期药单{" "}
                      {medication?.plans_active ?? 0} 张
                    </p>
                    <table className="data-table table-compact">
                      <tbody>
                        {medicationRows.map((row) => (
                          <tr key={row.key}>
                            <th scope="row">{row.label}</th>
                            <td>{row.value} 次</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="cell-muted" style={{ marginTop: 8 }}>
                      「已送达未确认」是提醒已推送、长者没有回应，不代表没吃；「尚未结束」是当天仍有
                      到点流程在走。
                    </p>
                  </>
                ) : (
                  <div className="placeholder-block">
                    <p className="placeholder-title">
                      {medication?.state === "not_configured"
                        ? "这位长者当天没有用药计划，也没有到点提醒"
                        : (medication?.summary ?? MEDICATION_PLACEHOLDER_TEXT)}
                    </p>
                    <p className="placeholder-desc">
                      回填前的历史日报只带一句占位说明；新日报按四类终态分别计数。
                    </p>
                  </div>
                )}
              </div>
            </>
          ) : null}

          <div className="card">
            <header className="page-head" style={{ marginBottom: 12 }}>
              <h2 className="page-title" style={{ fontSize: 17 }}>
                近 30 天日报
              </h2>
              <p className="page-sub">点「查看」直接切到该日</p>
            </header>
            {recentError ? (
              <ErrorBanner message={recentError} onRetry={() => setRecentKey((k) => k + 1)} />
            ) : null}
            <DataTable
              columns={recentColumns}
              rows={recent}
              rowKey={(row) => row.report_id}
              loading={recentLoading}
              emptyTitle="这 30 天还没有日报"
              emptyDescription="日报由每日 7:00 调度生成。"
              caption="近 30 天日报列表"
            />
          </div>
        </>
      ) : null}
    </section>
  );
}
