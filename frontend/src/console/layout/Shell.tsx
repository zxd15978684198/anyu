/** 照护台外壳（W1-2）：左侧导航 + 顶栏（姓名/角色/退出）+ 内容区。导航项按能力过滤。 */
import type { ReactNode } from "react";
import { logout, roleLabel, type Principal } from "../api";
import { can } from "../permissions";
import { NAV_ITEMS } from "../router";

export function Shell({
  principal,
  route,
  onNavigate,
  onLogout,
  children,
}: {
  principal: Principal;
  route: string;
  onNavigate: (path: string) => void;
  onLogout: () => void;
  children: ReactNode;
}) {
  const items = NAV_ITEMS.filter((item) => can(principal, item.capability));

  return (
    <div className="app-shell">
      <aside className="side-nav" aria-label="主导航">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">康</span>
          <span className="brand-text">
            安语 · 照护台
            <small>Anyu Console</small>
          </span>
        </div>
        <nav>
          <ul>
            {items.map((item) => {
              const active = route === item.path;
              return (
                <li key={item.path}>
                  <button
                    type="button"
                    className={`nav-item${active ? " is-active" : ""}`}
                    aria-current={active ? "page" : undefined}
                    onClick={() => onNavigate(item.path)}
                  >
                    {item.label}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>
        <p className="side-foot">管理员可写，护工 / 医生只读</p>
      </aside>

      <div className="main-col">
        <header className="topbar">
          <span className="topbar-role">{roleLabel(principal)}</span>
          <span className="topbar-name">{principal.name}</span>
          <button
            type="button"
            className="btn-link"
            onClick={() => {
              logout();
              onLogout();
            }}
          >
            退出登录
          </button>
        </header>
        <main className="page">{children}</main>
      </div>
    </div>
  );
}
