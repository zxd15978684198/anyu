/**
 * 家属端首页 = 日报列表（W4 §1）。
 *
 * 布局：长者切换（仅多于一位时出现）→ 近 30 天日报卡片列表。
 * 每张卡片：日期（11 月 9 日 周日）+ 健康等级色点 + 一句摘要 + 「查看详情」。
 * 整张卡片就是一个按钮，所以 375px 下没有小到点不中的「详情」文字链接。
 *
 * 空态：服务端**不给未生成的日期补空行**，所以「列表为空」= 这 30 天还没有日报，
 * 文案写「这份日报还没生成」，而不是留一片白。
 */
import { formatDateLabel, isToday } from "../format";
import {
  GRADE_LABEL,
  gradeOf,
  reportDateOf,
  summaryOf,
  type DailyReportItem,
} from "../reports";
import { ChevronRightIcon } from "../components/Icons";
import { ElderSwitcher } from "../components/ElderSwitcher";
import { SeverityDot } from "../components/SeverityDot";
import { ErrorBlock, InlineEmpty, ListSkeleton } from "../components/States";

interface ReportListPageProps {
  elderIds: string[];
  elderNames: Record<string, string>;
  activeElderId: string | null;
  onSelectElder: (elderId: string) => void;
  reports: DailyReportItem[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  onOpenReport: (date: string) => void;
}

export function ReportListPage({
  elderIds,
  elderNames,
  activeElderId,
  onSelectElder,
  reports,
  loading,
  error,
  onRetry,
  onOpenReport,
}: ReportListPageProps) {
  return (
    <div className="page">
      <ElderSwitcher
        elderIds={elderIds}
        names={elderNames}
        activeId={activeElderId}
        onChange={onSelectElder}
      />

      <div className="section-head">
        <h2 className="section-title">近 30 天日报</h2>
        {!loading && !error && reports.length > 0 && (
          <span className="section-count">{reports.length} 份</span>
        )}
      </div>

      {loading ? (
        <ListSkeleton rows={4} />
      ) : error ? (
        <ErrorBlock error={error} onRetry={onRetry} />
      ) : reports.length === 0 ? (
        <InlineEmpty
          title="这份日报还没生成"
          hint="日报通常在每天晚间整理完成后生成，您可以稍后再来看看。"
        />
      ) : (
        <ul className="report-list">
          {reports.map((item) => {
            const date = reportDateOf(item);
            const grade = gradeOf(item);
            return (
              <li key={item.report_id || date}>
                <button type="button" className="report-card" onClick={() => onOpenReport(date)}>
                  <span className="report-card-top">
                    <span className="report-date">{formatDateLabel(date)}</span>
                    {isToday(date) && <span className="report-badge">今天</span>}
                    <SeverityDot tone={grade} label={GRADE_LABEL[grade]} />
                  </span>
                  <span className="report-summary">{summaryOf(item)}</span>
                  <span className="report-link">
                    查看详情
                    <ChevronRightIcon />
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
