import { useEffect, useState, type ReactElement } from "react";
import {
  ApiError,
  cachedProfile,
  fetchCurrent,
  hasSession,
  NETWORK_ERROR_TEXT,
  setUnauthorizedHandler,
  type Principal,
} from "./api";
import { Shell } from "./layout/Shell";
import { LoginPage } from "./pages/LoginPage";
import { EldersPage } from "./pages/EldersPage";
import { PersonasPage } from "./pages/PersonasPage";
import { StaffPage } from "./pages/StaffPage";
import { GuardiansPage } from "./pages/GuardiansPage";
import { DevicesPage } from "./pages/DevicesPage";
import { HealthPage } from "./pages/HealthPage";
import { ReportsPage } from "./pages/ReportsPage";
import { NotificationsPage } from "./pages/NotificationsPage";
import { MedicationsPage } from "./pages/MedicationsPage";
import { MemoryPage } from "./pages/MemoryPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { WorkOrdersPage } from "./pages/WorkOrdersPage";
import { AuditPage } from "./pages/AuditPage";
import { HomePage } from "./pages/HomePage";
import { RoomsPage } from "./pages/RoomsPage";
import { MediaLibraryPage } from "./pages/MediaLibraryPage";
import { DEFAULT_ROUTE, NAV_ITEMS, navigate, useHashRoute } from "./router";
import { EmptyState } from "./ui/Feedback";
import { can, isAdmin, type Capability } from "./permissions";
import { ErrorBoundary } from "../shared/ui/ErrorBoundary";

/**
 * 照护台入口（W1-1）。
 * 无本地登录态 → 登录页；有 → fetchCurrent 恢复身份（过期自动 refresh），失败回登录页。
 * 登录后进入 Shell（左侧导航按能力过滤）+ hash 路由页面。
 */
type View = "loading" | "login" | "app";

interface PageDef {
  path: string;
  capability: Capability;
  render: (principal: Principal) => ReactElement;
}

const PAGES: PageDef[] = [
  { path: "/home", capability: "elder:read", render: (p) => <HomePage principal={p} /> },
  { path: "/elders", capability: "elder:read", render: (p) => <EldersPage principal={p} /> },
  // 数字人角色（FR-3D-03）：只读资产列表，能力与导航同源（persona:read）。
  {
    path: "/personas",
    capability: "persona:read",
    render: (p) => <PersonasPage principal={p} />,
  },
  // 迭代 8：三新页 + 药单占位。capability 与 router.tsx 的 NAV_ITEMS 一一对应。
  { path: "/health", capability: "health_write", render: (p) => <HealthPage principal={p} /> },
  // 工单看板（FR-ALT-02）：capability 与 router.tsx 的 NAV_ITEMS 一一对应
  { path: "/work-orders", capability: "workorder:read", render: (p) => <WorkOrdersPage principal={p} /> },
  { path: "/notifications", capability: "notifications_ack", render: () => <NotificationsPage /> },
  { path: "/reports", capability: "reports_view", render: (p) => <ReportsPage principal={p} /> },
  { path: "/medications", capability: "medication:read", render: (p) => <MedicationsPage principal={p} /> },
  { path: "/staff", capability: "staff:write", render: (p) => <StaffPage principal={p} /> },
  { path: "/guardians", capability: "guardian:write", render: (p) => <GuardiansPage principal={p} /> },
  { path: "/devices", capability: "device:write", render: (p) => <DevicesPage principal={p} /> },
  { path: "/rooms", capability: "room:write", render: (p) => <RoomsPage principal={p} /> },
  // 运行指标页已于 2026-11-11 下架（产品负责人裁决：管理员的运行指标不该出现在前端，
  // 运维侧直接抓 `GET /metrics`）。capability `metrics:read` 同步删除。
  { path: "/memory", capability: "memory:read", render: (p) => <MemoryPage principal={p} /> },
  // 机构知识库（FR-KNW-02）：录入/换代/停用都只认 admin，故挂 knowledge:write（只在 ADMIN_CAPS）
  {
    path: "/knowledge",
    capability: "knowledge:write",
    render: (p) => <KnowledgePage principal={p} />,
  },
  {
    path: "/media-library",
    capability: "media:write",
    render: (p) => <MediaLibraryPage principal={p} />,
  },
  { path: "/audit", capability: "audit:read", render: (p) => <AuditPage principal={p} /> },
];

export default function App() {
  const [view, setView] = useState<View>("loading");
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const route = useHashRoute();

  // 401 收口：api.ts 在 access + refresh 都失效时回调这里（只跳一次，重复调用无副作用）。
  // 没有它时 refresh 失败只清本地态，视图仍停在 app，后续请求裸奔 401 只显示横幅。
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setPrincipal(null);
      setView("login");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    if (!hasSession()) {
      setView("login");
      return;
    }
    // 先用本地缓存的角色渲染，再向服务端核实（fetchCurrent，E3）
    setPrincipal(cachedProfile());
    fetchCurrent()
      .then((identity) => {
        setPrincipal(identity);
        setView("app");
      })
      .catch((err: unknown) => {
        void (err instanceof ApiError ? err.message : NETWORK_ERROR_TEXT);
        setPrincipal(null);
        setView("login");
      });
  }, []);

  // 无 hash、或落在一个当前角色无权访问的页面时，跳到第一个可用页面。
  // 导航与路由同源（NAV_ITEMS），所以这里不会把护工误送到管理员页。
  useEffect(() => {
    if (view !== "app" || !principal) return;
    const page = PAGES.find((item) => item.path === route);
    if (page && can(principal, page.capability)) return;
    const fallback = NAV_ITEMS.find((item) => can(principal, item.capability));
    navigate(fallback ? fallback.path : DEFAULT_ROUTE);
  }, [view, principal, route]);

  if (view === "loading") {
    return (
      <div className="auth-page">
        <div className="spinner" role="status" aria-label="正在恢复登录态" />
      </div>
    );
  }

  if (view === "app" && principal) {
    const page = PAGES.find((item) => item.path === route);
    const allowed = page ? can(principal, page.capability) : false;

    return (
      <Shell
        principal={principal}
        route={route}
        onNavigate={navigate}
        onLogout={() => {
          setPrincipal(null);
          setView("login");
        }}
      >
        {page && allowed ? (
          /* 页面级错误边界（A6）：一个页面读数据抛错时，导航与顶栏必须存活。
             实测无边界时 HomePage 读 data.elders.active 抛 TypeError 会让整棵树卸载、
             #root 清空、用户看到纯白页无提示。resetKey 用路由，换页即重置边界。 */
          <ErrorBoundary
            key={route}
            resetKey={route}
            label={NAV_ITEMS.find((item) => item.path === route)?.label ?? "这一页"}
            onFallback={() => {
              const fallback = NAV_ITEMS.find(
                (item) => item.path !== route && can(principal, item.capability),
              );
              navigate(fallback ? fallback.path : DEFAULT_ROUTE);
            }}
          >
            {page.render(principal)}
          </ErrorBoundary>
        ) : (
          <EmptyState
            title={page ? "当前角色无权访问" : "页面不存在"}
            description={
              page
                ? isAdmin(principal)
                  ? "该页面暂不可用，请从左侧导航进入其他页面。"
                  : "该页面不在您的角色范围内，请从左侧导航进入可用页面。"
                : "请从左侧导航选择一个页面。"
            }
          />
        )}
      </Shell>
    );
  }

  return (
    <LoginPage
      onSuccess={(p) => {
        setPrincipal(p);
        setView("app");
        if (!window.location.hash) navigate(isAdmin(p) ? "/home" : DEFAULT_ROUTE);
      }}
    />
  );
}
