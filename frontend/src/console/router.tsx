/**
 * 照护台迷你路由（W1-1）+ 导航表（迭代 8 同源）。
 *
 * 只支持「一个 URL 一个页面」的平铺导航：`#/elders`、`#/health` ……
 * 详情不占路由（用页面内抽屉），因此不需要 path 参数与嵌套路由。
 * 选 hash 而非 history 的原因：nginx 只需托管静态文件，刷新不 404，不需要 try_files 改造。
 *
 * 导航表为什么放这里而不是 Shell 里：导航项与 `App.tsx` 的 `PAGES` 表必须一一对应，
 * 两张表放同一个文件旁边才不会漂（迭代 8 加四页时改一处即可，Shell 只负责渲染）。
 */
import { useEffect, useState } from "react";
import type { Capability } from "./permissions";

/** 默认落地页与导航首位保持一致：`NAV_ITEMS[0]` 是「首页」（机构概览，接
 *  `/api/stats/overview`），登录后第一眼该看到「今天这个机构什么状况」。
 *  原先默认 `/elders` 与导航首位对不上——首页存在却不是落地页（前端计划 C-6）。
 *  能力仍挂 elder:read（三角色都有），护工/医生进去看到的是角色说明而不是 403。 */
export const DEFAULT_ROUTE = "/home";

export interface NavItem {
  path: string;
  label: string;
  capability: Capability;
}

/** 顺序即左侧导航顺序；`capability` 决定谁看得到（照护台按角色过滤，不做角色硬编码）。 */
export const NAV_ITEMS: NavItem[] = [
  // 首页（ALT-05）：真实机构概览接 `/api/stats/overview`。放第一位——
  // 登录后第一眼该看到「今天这个机构什么状况」，再决定从哪个业务页入手。
  // 能力挂 elder:read（三角色都有）：护工/医生进去看到的是角色说明，不是 403 页。
  { path: "/home", label: "首页", capability: "elder:read" },
  { path: "/elders", label: "长者档案", capability: "elder:read" },
  // 数字人角色（FR-3D-03）：资产只读一览；分配入口在长者档案详情里，两处共用同一列表。
  // 服务端 `GET /api/elders/personas` 只放 admin / doctor，故 capability 只进这两个角色。
  { path: "/personas", label: "数字人角色", capability: "persona:read" },
  { path: "/health", label: "健康采集", capability: "health_write" },
  // 工单看板（FR-ALT-02）：放在健康采集之后——护工/医生接单是紧随健康告警的动作，
  // 而消息中心是「发出去的通知」，工单是「要人处理的活」，两者相邻但顺序不要混。
  // 能力单列 workorder:read（不是 notifications_ack）：服务端 `_WORK_ORDER_ROLES` 白名单
  // 只有 admin / care_worker / doctor，挂错能力会让医生看见一个点进去就 403 的入口。
  { path: "/work-orders", label: "工单看板", capability: "workorder:read" },
  { path: "/notifications", label: "消息中心", capability: "notifications_ack" },
  { path: "/reports", label: "每日日报", capability: "reports_view" },
  // 用药页：迭代 11 从「只读占位」升级成可用页（医生开药单 + 三角色代确认）。
  // 能力用 medication:read 而非 reports_view——护工本来就能读药单（服务端 READING_ROLES 含 care_worker），
  // 挂在 reports_view 上会把它挡在门外。
  { path: "/medications", label: "用药管理", capability: "medication:read" },
  { path: "/staff", label: "员工账号", capability: "staff:write" },
  { path: "/guardians", label: "家属与绑定", capability: "guardian:write" },
  { path: "/devices", label: "设备绑定", capability: "device:write" },
  // 房间管理（FR-ADM-01）：只做改名与状态维护；新建房间与床位维护不在本批。
  { path: "/rooms", label: "房间管理", capability: "room:write" },
  // 运行指标导航项已下架（2026-11-11 产品负责人裁决：运行指标属运维面，不进照护台前端）
  // 长者记忆（FR-MEM-02）：契约 M1–M3 全部 admin only，故挂 memory:read（只有 ADMIN_CAPS 有）
  { path: "/memory", label: "长者记忆", capability: "memory:read" },
  // 机构知识库（FR-KNW-02）：录入口与写端点都只认 admin（Prd:481「录入与更新仅限管理员」）
  { path: "/knowledge", label: "知识库", capability: "knowledge:write" },
  // 媒体库维护（FR-ADM-02）：管理端列表含未授权素材；pad 的只读点播端点不在这里。
  { path: "/media-library", label: "媒体库", capability: "media:write" },
  // 操作日志（FR-ADM-05）：`app/api/audit_export.py` 的 `ADMIN_ROLES = {admin}`，
  // 能力单列 `audit:read`（只进 ADMIN_CAPS）——不复用 staff:write，避免将来放宽员工写权限时
  // 顺手把审计日志一起放出去。
  { path: "/audit", label: "操作日志", capability: "audit:read" },
  // 演示重置导航项已下架（2026-11-16 产品负责人裁决：清库属演示运维面，不进照护台前端）。
  // 后端 `GET/POST /api/admin/teaching-reset` 保留（admin + 行数比对 + 确认短语三闸不变），
  // 需要清演示流水时走命令行；前端 capability `admin:reset` 同步删除。
];

export function navigate(to: string): void {
  const target = `#${to}`;
  if (window.location.hash !== target) {
    window.location.hash = to;
  }
}

function readHash(): string {
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return DEFAULT_ROUTE;
  return raw.startsWith("/") ? raw : `/${raw}`;
}

/** 当前路由（`hashchange` 驱动；首次渲染直接读一遍，避免闪一下默认页）。 */
export function useHashRoute(): string {
  const [route, setRoute] = useState<string>(readHash);

  useEffect(() => {
    const onChange = () => setRoute(readHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return route;
}
