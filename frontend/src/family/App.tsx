/**
 * 家属端入口（W4）：登录态 + 页面路由。
 *
 * 路由刻意不用 react-router（工程约束：不加依赖）。三个页面就是三个 state：
 * 列表 / 详情(date) / 通知中心；返回 = 把 state 改回列表。
 *
 * 会话口径：
 * - 本地无 token → 登录页；
 * - 有 token → `fetchCurrent()` 取 `scope_elder_ids`（家属能看哪几位长者只有它知道）；
 * - 任何请求 401 → `api.ts` 清态并调用这里注册的回调 → 回登录页（幂等，并发 401 也只跳一次）。
 * - 刷新失败/网络失败一律回登录页，不给白屏。
 */
import { useEffect, useState } from "react";
import {
  fetchCurrent,
  hasSession,
  logout,
  setUnauthorizedHandler,
  type CurrentIdentity,
} from "./api";
import { useElderNames, useReports } from "./hooks";
import { FullScreenLoading, InlineEmpty } from "./components/States";
import { ArrowLeftIcon, BellIcon, SignOutIcon } from "./components/Icons";
import { LoginPage } from "./pages/LoginPage";
import { NotificationsPage } from "./pages/NotificationsPage";
import { ReportDetailPage } from "./pages/ReportDetailPage";
import { ReportListPage } from "./pages/ReportListPage";
import { ErrorBoundary } from "../shared/ui/ErrorBoundary";

type View = "loading" | "login" | "app";
type Route = { name: "list" } | { name: "detail"; date: string } | { name: "notifications" };

export default function App() {
  const [view, setView] = useState<View>("loading");
  const [identity, setIdentity] = useState<CurrentIdentity | null>(null);
  const [elderOverride, setElderOverride] = useState<string | null>(null);
  const [route, setRoute] = useState<Route>({ name: "list" });

  // 401 收口：api.ts 在 access + refresh 都失效时回调这里（只跳一次，重复调用无副作用）
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setIdentity(null);
      setElderOverride(null);
      setRoute({ name: "list" });
      setView("login");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  // 冷启动恢复登录态
  useEffect(() => {
    if (!hasSession()) {
      setView("login");
      return;
    }
    let alive = true;
    fetchCurrent()
      .then((data) => {
        if (!alive) return;
        setIdentity(data);
        setView("app");
      })
      .catch(() => {
        if (!alive) return;
        setIdentity(null);
        setView("login");
      });
    return () => {
      alive = false;
    };
  }, []);

  const scopeElderIds = identity?.scope_elder_ids ?? [];
  const activeElderId =
    elderOverride && scopeElderIds.includes(elderOverride)
      ? elderOverride
      : (scopeElderIds[0] ?? null);

  const elderNames = useElderNames(scopeElderIds);
  const { reports, loading, error, reload } = useReports(activeElderId);

  async function handleLoginSuccess() {
    try {
      const data = await fetchCurrent();
      setIdentity(data);
      setElderOverride(null);
      setRoute({ name: "list" });
      setView("app");
    } catch {
      setView("login");
    }
  }

  function handleLogout() {
    logout();
    setIdentity(null);
    setElderOverride(null);
    setRoute({ name: "list" });
    setView("login");
  }

  if (view === "loading") {
    return <FullScreenLoading label="正在加载…" />;
  }

  if (view === "login" || !identity) {
    return <LoginPage onSuccess={() => void handleLoginSuccess()} />;
  }

  const elderName = activeElderId ? (elderNames[activeElderId] ?? null) : null;
  const inDetail = route.name === "detail";
  const inNotifications = route.name === "notifications";

  return (
    <div className="app-shell">
      <header className={inDetail || inNotifications ? "app-bar app-bar-sub" : "app-bar"}>
        {inDetail || inNotifications ? (
          <>
            <button
              type="button"
              className="icon-action"
              aria-label="返回日报列表"
              onClick={() => setRoute({ name: "list" })}
            >
              <ArrowLeftIcon />
            </button>
            <h1 className="app-bar-title">{inDetail ? "日报详情" : "通知中心"}</h1>
          </>
        ) : (
          <>
            <div className="app-bar-main">
              <h1 className="app-greeting">您好，{identity.name}</h1>
              <p className="app-sub">
                {elderName ? `正在查看 ${elderName} 的日报` : "家人的健康日报与提醒"}
              </p>
            </div>
            <div className="app-bar-actions">
              <button
                type="button"
                className="icon-action"
                aria-label="通知中心"
                onClick={() => setRoute({ name: "notifications" })}
              >
                <BellIcon />
              </button>
              <button type="button" className="icon-action" aria-label="退出登录" onClick={handleLogout}>
                <SignOutIcon />
              </button>
            </div>
          </>
        )}
      </header>

      <main className="app-main">
        {/* 页面级错误边界（A6）：顶栏与长者切换存活，用户能返回而不是白屏。 */}
        <ErrorBoundary
          key={`${route.name}-${activeElderId ?? ""}-${route.name === "detail" ? route.date : ""}`}
          resetKey={route.name}
          label={inNotifications ? "通知中心" : inDetail ? "日报详情" : "日报列表"}
          onFallback={() => setRoute({ name: "list" })}
        >
        {scopeElderIds.length === 0 ? (
          <InlineEmpty
            title="还没有绑定长者"
            hint="请联系社区的照护人员，把您的账号绑定到家人之后，就能在这里查看日报了。"
          />
        ) : inNotifications ? (
          <NotificationsPage />
        ) : inDetail && activeElderId ? (
          <ReportDetailPage
            elderId={activeElderId}
            elderName={elderName}
            date={route.date}
            reports={reports}
            reportsLoading={loading}
            reportsError={error}
            onRetryReports={reload}
          />
        ) : (
          <ReportListPage
            elderIds={scopeElderIds}
            elderNames={elderNames}
            activeElderId={activeElderId}
            onSelectElder={(elderId) => setElderOverride(elderId)}
            reports={reports}
            loading={loading}
            error={error}
            onRetry={reload}
            onOpenReport={(date) => setRoute({ name: "detail", date })}
          />
        )}
        </ErrorBoundary>
      </main>
    </div>
  );
}
