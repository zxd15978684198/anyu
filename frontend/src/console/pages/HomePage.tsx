/**
 * 照护台首页（ALT-05，2026-11-12 重写）。
 *
 * ## 从占位卡改成真实数字
 * 原页面是登录后的占位提示（「业务页属后续批次」），那时还没有统计接口。现在
 * `GET /api/stats/overview` 已经在（`app/api/stats.py`），首页就把机构当天最该看的
 * 五个数字摆出来：长者、设备在线、未结工单（含红档）、用药、记忆切片。
 *
 * ## 为什么非管理员看到的是提示而不是空卡片
 * 统计端点是 **admin only**（`app/api/stats.py:284 _require_admin`）。护工/医生进来必然
 * 403，而 403 的响应体是契约错误体——不是「没有数据」。所以这里按角色分支：
 * 管理员请求并渲染；其他人直接展示一句说明，不发那个注定 403 的请求。
 * 这不是权限判定（服务端才是），只是别让页面先闪一次错误。
 *
 * ## 退出按钮不在这里
 * 退出登录属于 Shell 的顶部操作（`layout/Shell.tsx` 的 `onLogout`）。首页只收 `principal`，
 * 不重复实现一个退出按钮——两个入口只会让人怀疑哪个才真的退出了。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { roleLabel, type Principal } from "../api";
import { HttpError, apiGet, errorText } from "../http";
import { isAdmin } from "../permissions";
import { ErrorBanner, LoadingState } from "../ui/Feedback";
import { Notice } from "../../shared/ui/Notice";
import { formatDateTimeFull } from "../../shared/format";

/** 与 `app/api/stats.py` 的 `OverviewOut` 一一对应。 */
interface StatsOverview {
  elders: { total: number; active: number };
  devices: { total: number; online: number };
  work_orders: { open: number; red: number; yellow: number };
  health: { red_review_pending: number };
  medication: { plans_active: number; events_today: number; confirmed_today: number };
  memory: { chunks_active: number };
  generated_at: string;
}

/** 与 `app/api/stats.py` 的 `SecurityOut` 一一对应（累计值 + 今日窗口）。 */
interface StatsSecurity {
  audit_today: number;
  denied_requests: number;
  injection_blocked: number;
  exports_today: number;
  generated_at: string;
}

interface Metric {
  label: string;
  value: number;
  /** 一行补充说明，写清这个数字的口径（不是重复标题）。 */
  hint: string;
  /** 红档工单这类「看到就该动」的数字，用告警色。 */
  alert?: boolean;
}

function metricsOf(data: StatsOverview): Metric[] {
  return [
    { label: "在住长者", value: data.elders.active, hint: `档案共 ${data.elders.total} 位` },
    {
      label: "设备在线",
      value: data.devices.online,
      hint: `登记设备共 ${data.devices.total} 台`,
    },
    {
      label: "未结工单",
      value: data.work_orders.open,
      hint:
        `红档 ${data.work_orders.red} · 黄档 ${data.work_orders.yellow}` +
        // 红标不进红档（红档只数工单），单独报一个数，避免护理员以为"没红标"。
        (data.health.red_review_pending > 0
          ? ` · 红标待复核 ${data.health.red_review_pending}`
          : ""),
      alert: data.work_orders.red > 0,
    },
    {
      label: "今日用药",
      value: data.medication.events_today,
      hint: `已确认 ${data.medication.confirmed_today} 次 · 在用长嘱 ${data.medication.plans_active} 张`,
    },
    { label: "记忆切片", value: data.memory.chunks_active, hint: "status=active 的长期记忆" },
  ];
}

function securityMetricsOf(data: StatsSecurity): Metric[] {
  return [
    { label: "今日审计", value: data.audit_today, hint: "今日写入审计行数" },
    {
      label: "越权拒绝",
      value: data.denied_requests,
      hint: "累计（tool.denied + action=denied）",
      alert: data.denied_requests > 0,
    },
    { label: "注入拦截", value: data.injection_blocked, hint: "累计提示注入拦截次数" },
    { label: "今日导出", value: data.exports_today, hint: "audit.export + memory.export" },
  ];
}

function formatGeneratedAt(value: string): string {
  return formatDateTimeFull(value);
}

export function HomePage({ principal }: { principal: Principal }) {
  const admin = isAdmin(principal);
  const [data, setData] = useState<StatsOverview | null>(null);
  const [security, setSecurity] = useState<StatsSecurity | null>(null);
  const [loading, setLoading] = useState(admin);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setForbidden(false);
    try {
      const next = await apiGet<StatsOverview>("/api/stats/overview");
      if (!mounted.current) return;
      setData(next);
    } catch (err) {
      if (!mounted.current) return;
      setData(null);
      // 403 是「你本来就不该看」，当提示；其余才是错误。
      if (err instanceof HttpError && err.status === 403) setForbidden(true);
      else setError(errorText(err));
      return;
    } finally {
      if (mounted.current) setLoading(false);
    }
    // 安全统计是同一张页的第二块，取不到不影响主数字——单独一次请求，失败静默降级。
    try {
      const sec = await apiGet<StatsSecurity>("/api/stats/security");
      if (mounted.current) setSecurity(sec);
    } catch {
      if (mounted.current) setSecurity(null);
    }
  }, []);

  useEffect(() => {
    if (admin) void load();
  }, [admin, load]);

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1 className="page-title">首页</h1>
          <p className="page-sub">
            {principal.name || "—"} · {roleLabel(principal)}
            {admin ? "　机构当日概览" : "　按左侧导航进入您负责的页面"}
          </p>
        </div>
        {admin ? (
          <div className="page-actions">
            <button type="button" className="btn-secondary" onClick={() => void load()} disabled={loading}>
              {loading ? "正在刷新…" : "刷新数字"}
            </button>
          </div>
        ) : null}
      </header>

      {admin ? (
        <>
          {error ? <ErrorBanner message={error} onRetry={() => void load()} /> : null}
          {!error && forbidden ? (
            <Notice>统计概览只对管理员开放（服务端 403）；本页其余功能不受影响。</Notice>
          ) : null}
          {loading && !data ? <LoadingState text="正在统计…" /> : null}
          {data ? (
            <div className="card">
              <div className="metric-row">
                {metricsOf(data).map((item) => (
                  <div className="metric-item" key={item.label}>
                    <span className="page-sub">{item.label}</span>
                    <span
                      className="metric-value"
                      style={item.alert ? { color: "var(--color-danger-ink)" } : undefined}
                    >
                      {item.value}
                    </span>
                    <span className="cell-muted">{item.hint}</span>
                  </div>
                ))}
              </div>
              <p className="field-hint">
                统计于 {formatGeneratedAt(data.generated_at)}（上海时间）。数字来自数据库实时聚合，
                不含缓存。红档工单请到「工单看板」处理。
              </p>
            </div>
          ) : null}
          {security ? (
            <div className="card">
              <p className="report-subtitle">安全与审计</p>
              <div className="metric-row">
                {securityMetricsOf(security).map((item) => (
                  <div className="metric-item" key={item.label}>
                    <span className="page-sub">{item.label}</span>
                    <span
                      className="metric-value"
                      style={item.alert ? { color: "var(--color-danger-ink)" } : undefined}
                    >
                      {item.value}
                    </span>
                    <span className="cell-muted">{item.hint}</span>
                  </div>
                ))}
              </div>
              <p className="field-hint">
                越权拒绝与注入拦截是<strong>累计</strong>值，其余按今日窗口统计。
                逐条记录在「操作日志」页，可按主体、动作与时间段筛。
              </p>
            </div>
          ) : null}
        </>
      ) : (
        <div className="card">
          <Notice>
            机构概览（长者、设备、工单、用药、记忆）只对管理员开放。
          </Notice>
          <p className="field-hint">
            您可以在左侧导航进入长者档案、健康采集、工单看板、消息中心与用药管理；
            具体能看哪些长者由服务端按负责范围过滤。
          </p>
        </div>
      )}
    </section>
  );
}
