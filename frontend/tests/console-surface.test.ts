/**
 * 照护台前端面收敛回归（2026-11-16 产品负责人裁决：演示重置下架）。
 *
 * 钉两件事，方向相反、缺一不可：
 * 1. **前端真的没有这个入口**——导航、路由表、能力表、页面文件、数据层、API 调用点全清；
 *    只删导航项而留着路由，`#/reset` 仍能直达，等于没删。
 * 2. **后端能力没被顺手删掉**——`/api/admin/teaching-reset` 仍在路由表里。
 *    FR-ADM-02 是 P0，裁决只说「不该出现在前端」，清演示流水仍要能走命令行。
 *
 * 另附一条结构不变量：导航表与页面表的路径集合必须完全一致（同源，迭代 8 起的要求）。
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { describe, it } from "node:test";

const root = new URL("../../", import.meta.url);
const read = (p: string) => readFileSync(new URL(p, root), "utf8");
const stripComments = (text: string) =>
  text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

/** 递归收集 console 下所有源码（含子目录 medications/ 等）。 */
function consoleSources(): string[] {
  const out: string[] = [];
  const walk = (rel: string) => {
    for (const e of readdirSync(new URL(rel, root), { withFileTypes: true })) {
      const next = `${rel}/${e.name}`;
      if (e.isDirectory()) walk(next);
      else if (/\.(ts|tsx)$/.test(e.name)) out.push(next);
    }
  };
  walk("frontend/src/console");
  return out;
}

describe("演示重置已从照护台前端下架", () => {
  it("导航表里没有 /reset，也没有「演示重置」这个标签", () => {
    const router = stripComments(read("frontend/src/console/router.tsx"));
    assert.equal(router.includes('"/reset"'), false);
    assert.equal(router.includes("演示重置"), false);
  });

  it("页面表里没有 /reset，App 也不再 import ResetPage", () => {
    const app = stripComments(read("frontend/src/console/App.tsx"));
    assert.equal(app.includes('"/reset"'), false);
    assert.equal(app.includes("ResetPage"), false);
  });

  it("能力表里 admin:reset 已删（类型联合与 ADMIN_CAPS 两处都不留）", () => {
    const perms = stripComments(read("frontend/src/console/permissions.ts"));
    assert.equal(perms.includes("admin:reset"), false);
  });

  it("页面文件与数据层文件都已删除", () => {
    assert.equal(existsSync(new URL("frontend/src/console/pages/ResetPage.tsx", root)), false);
    assert.equal(existsSync(new URL("frontend/src/console/reset.ts", root)), false);
  });

  it("console 全量源码里没有任何一处调用 teaching-reset 端点", () => {
    for (const f of consoleSources()) {
      const code = stripComments(read(f));
      assert.equal(code.includes("teaching-reset"), false, `${f} 仍在调用重置端点`);
      assert.equal(code.includes("ResetPage"), false, `${f} 仍引用已删页面`);
    }
  });

  it("门户文案不再把演示重置当成照护台能力宣传", () => {
    const baselines = stripComments(read("frontend/src/portal/baselines.ts"));
    assert.equal(baselines.includes("演示重置"), false);
    assert.equal(baselines.includes("演示数据一键重置"), false);
  });
});

describe("下架只动前端：后端能力与导航同源不变量都还在", () => {
  it("后端端点仍注册（FR-ADM-02 走命令行仍可用）", () => {
    assert.equal(existsSync(new URL("app/api/admin_reset.py", root)), true);
    const api = read("app/api/__init__.py");
    assert.equal(api.includes("admin_reset.router"), true, "路由表里必须仍挂着 admin_reset");
    const endpoint = read("app/api/admin_reset.py");
    assert.equal(endpoint.includes("/api/admin/teaching-reset"), true);
  });

  it("导航表与页面表的路径集合完全一致（删一项必须两边同删）", () => {
    const paths = (text: string) =>
      [...stripComments(text).matchAll(/path:\s*"(\/[^"]*)"/g)].map((m) => m[1]).sort();
    const nav = paths(read("frontend/src/console/router.tsx"));
    const pages = paths(read("frontend/src/console/App.tsx"));
    assert.deepEqual(pages, nav);
    assert.equal(nav.length > 0, true);
  });
});