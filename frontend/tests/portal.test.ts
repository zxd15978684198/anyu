/**
 * 演示门户「大厅」契约（前端计划 §10.8，批次 WP）。
 *
 * 钉住三类不可退让的边界：
 * 1. **不占用 `/`**：根地址仍是长者端（设备语义），门户只走 /portal；
 * 2. **不内置演示口令**（AGENTS §4-3 / 计划 S-8 / R-14）；
 * 3. **不新造色相**（计划 S-9，tokens.css 2026-11-05 冻结）。
 * 另加接线完整性（vite input + nginx 两处）、沙盘纯函数与控件齐备、
 * 以及滚动条 / 焦点环 / reduced-motion 三项体验底座。
 *
 * 沿用本仓口径：源码契约 + 纯函数断言，不引 vitest/jsdom。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  ARTIFACTS,
  ENDS,
  GATES,
  HARD_NUMBERS,
  LATENCY_BUDGET_MS,
  LATENCY_SCENARIOS,
  ROLES,
  exportSnapshot,
} from "../src/portal/baselines.ts";

const read = (rel: string): string =>
  readFileSync(new URL(`../../${rel}`, import.meta.url), "utf8");
const portal = (rel: string): string => read(`frontend/src/portal/${rel}`);

// ------------------------------------------------------------------ 路由边界

describe("WP · 门户不占用根地址", () => {
  it("nginx.conf 的 location = / 仍指向 pad 的 index.html", () => {
    const text = read("frontend/nginx.conf");
    const at = text.indexOf("location = / {");
    assert.equal(at > 0, true, "根路径 location 必须存在");
    const block = text.slice(at, at + 200);
    assert.equal(
      /try_files \/index\.html =404;/.test(block),
      true,
      "期望：/ 仍出长者端。改成 portal 会让长者开机先选端，是倒退",
    );
    assert.equal(block.includes("portal.html"), false);
  });

  it("nginx.https.conf 同样保持根路径给 pad", () => {
    const text = read("frontend/nginx.https.conf");
    const at = text.indexOf("location = / {");
    const block = text.slice(at, at + 200);
    assert.equal(/try_files \/index\.html =404;/.test(block), true);
  });

  it("两份 nginx 都新增了 location /portal 且四个头写全", () => {
    for (const file of ["frontend/nginx.conf", "frontend/nginx.https.conf"]) {
      const text = read(file);
      const at = text.indexOf("location /portal {");
      assert.equal(at > 0, true, `${file} 缺 location /portal`);
      const block = text.slice(at, text.indexOf("}", at));
      assert.equal(/try_files \$uri \/portal\.html =404;/.test(block), true, file);
      for (const header of [
        'Cache-Control "no-store"',
        'X-Content-Type-Options "nosniff"',
        'X-Frame-Options "SAMEORIGIN"',
        'Referrer-Policy "strict-origin-when-cross-origin"',
      ]) {
        assert.equal(
          block.includes(header),
          true,
          `${file} 的 /portal 缺 ${header}：add_header 不继承 + try_files 首参命中不重定向，只改正则拿不到头`,
        );
      }
    }
  });

  it("两份 nginx 的 HTML 正则都含 portal", () => {
    for (const file of ["frontend/nginx.conf", "frontend/nginx.https.conf"]) {
      assert.equal(
        /location ~ \^\/\(\?:index\|console\|family\|portal\)\\\.html\$/.test(read(file)),
        true,
        `${file} 的 HTML 正则未加 portal`,
      );
    }
  });

  it("vite rollupOptions.input 含 portal 第 4 入口", () => {
    const text = read("frontend/vite.config.ts");
    assert.equal(/portal:\s*resolve\(__dirname,\s*"portal\.html"\)/.test(text), true);
  });

  it("portal.html 指向 portal 入口脚本，不复用他端", () => {
    const text = read("frontend/portal.html");
    assert.equal(text.includes('/src/portal/main.tsx'), true);
    assert.equal(text.includes("viewport-fit=cover"), true);
  });
});

// ------------------------------------------------------------------ 凭据边界

describe("WP · 演示口令不进产物", () => {
  const files = [
    "baselines.ts",
    "App.tsx",
    "hooks.ts",
    "index.css",
    "components/Access.tsx",
    "components/EndCards.tsx",
    "components/GateBoard.tsx",
    "components/LatencySandbox.tsx",
    "components/RouteMap.tsx",
    "components/Icons.tsx",
    "main.tsx",
  ];

  it("门户源码不含任何演示口令字面量", () => {
    for (const f of files) {
      const text = portal(f);
      assert.equal(
        /[A-Za-z]{3,8}@20\d\d/.test(text),
        false,
        `${f} 疑似内置演示口令（AGENTS §4-3 / 计划 S-8）`,
      );
      assert.equal(
        /device-long-secret/.test(text),
        false,
        `${f} 疑似内置设备密钥`,
      );
    }
  });

  it("账号可展示（不是凭据），且口令走文档链接", () => {
    assert.equal(ROLES.length, 4);
    for (const r of ROLES) {
      assert.equal(/^\d{11}$/.test(r.account), true, `${r.role} 账号应为手机号`);
    }
    assert.equal(portal("components/Access.tsx").includes("docs/demo-accounts.md"), true);
  });

  it("门户不 import three / three-vrm（隔离硬约束）", () => {
    for (const f of files) {
      if (!f.endsWith(".ts") && !f.endsWith(".tsx")) continue;
      assert.equal(
        /from\s+"(three|@pixiv\/three-vrm)/.test(portal(f)),
        false,
        `${f} 不得引入 three-vrm，否则泄漏进非 pad 产物`,
      );
    }
  });
});

// ------------------------------------------------------------------ 视觉边界

describe("WP · 沿用冻结色板，不新造色相", () => {
  it("门户 CSS 零硬编码 hex 色值（内联 SVG 噪声除外）", () => {
    const css = portal("index.css").replace(/url\("data:image[^"]*"\)/g, "");
    const hits = css.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [];
    assert.deepEqual(hits, [], `期望 0 处硬编码色相（计划 S-9）。现在：${hits.join(", ")}`);
  });

  it("门户 CSS 零 rgb()/rgba()/hsl() 字面量，透明度走 color-mix + token", () => {
    const css = portal("index.css");
    assert.equal(/rgba?\(/.test(css), false, "透明度应用 color-mix(in srgb, var(--token) N%, transparent)");
    assert.equal(/hsla?\(/.test(css), false);
    assert.equal(css.includes("color-mix(in srgb, var(--color-"), true);
  });

  it("引入 tokens 唯一真源，且经 tailwind 处理 @theme（只扫本端）", () => {
    const css = portal("index.css");
    assert.equal(css.includes('@import "tailwindcss" source(none);'), true);
    assert.equal(css.includes('@import "../theme/tokens.css";'), true);
  });

  it("滚动条自绘：宽度 / track / thumb / hover 四件齐", () => {
    const css = portal("index.css");
    assert.equal(css.includes("::-webkit-scrollbar"), true);
    assert.equal(css.includes("::-webkit-scrollbar-track"), true);
    assert.equal(css.includes("::-webkit-scrollbar-thumb"), true);
    assert.equal(css.includes("::-webkit-scrollbar-thumb:hover"), true);
    assert.equal(css.includes("scrollbar-color"), true, "Firefox 侧也要覆盖");
  });

  it("焦点可见与 reduced-motion 都在", () => {
    const css = portal("index.css");
    assert.equal(css.includes(":focus-visible"), true);
    assert.equal(css.includes("prefers-reduced-motion: reduce"), true);
  });

  it("顶栏固定且区块留了锚点偏移", () => {
    const css = portal("index.css");
    assert.equal(/\.p-nav\s*\{[^}]*position:\s*fixed/.test(css), true);
    assert.equal(css.includes("scroll-margin-top"), true);
  });

  it("内容可见性不押在 IO 上：默认可见 + armed 才隐藏 + 保底定时器", () => {
    const css = portal("index.css");
    // 默认必须可见，否则 IO 不触发时整块内容永久 opacity:0（实测踩过 4/5 区块不可见）
    assert.equal(
      /\.p-reveal\s*\{[^}]*opacity:\s*1/.test(css),
      true,
      "p-reveal 默认必须 opacity:1",
    );
    assert.equal(css.includes(".p-reveal.is-armed:not(.is-visible)"), true);
    const hooks = portal("hooks.ts");
    assert.equal(hooks.includes('classList.add("is-armed")'), true);
    assert.equal(/setTimeout\(reveal,\s*1200\)/.test(hooks), true, "必须有保底揭示定时器");
  });

  it("滚动高亮不用 rAF 去抖（隐藏页里 rAF 不触发，高亮会永远不亮）", () => {
    const hooks = portal("hooks.ts");
    const spy = hooks.slice(hooks.indexOf("export function useScrollSpy"));
    assert.equal(/requestAnimationFrame/.test(spy), false);
    assert.equal(spy.includes('addEventListener("scroll"'), true);
    assert.equal(/Date\.now\(\)/.test(spy), true, "改用时间戳节流");
  });

  it("小字文本不用 ink-subtle（实测该 token 对白底仅 3.26:1，不达 4.5）", () => {
    const css = portal("index.css");
    for (const sel of [".p-note", ".p-gate-src", ".p-tier-carrier", ".p-footnotes li"]) {
      const at = css.indexOf(`${sel} {`);
      const block = at >= 0 ? css.slice(at, css.indexOf("}", at)) : "";
      assert.equal(
        block.includes("--color-ink-subtle"),
        false,
        `${sel} 用了 ink-subtle，对比度不足；应用 ink-muted（5.98:1）`,
      );
    }
  });
});

// ------------------------------------------------------------------ 沙盘数据与纯函数

describe("WP · 沙盘数据口径", () => {
  it("三个场景各自带来源，且真链路是红灯（不美化）", () => {
    assert.equal(LATENCY_SCENARIOS.length, 3);
    const real = LATENCY_SCENARIOS.find((s) => s.id === "real");
    assert.equal(real?.verdict, "fail");
    assert.equal(real?.passRate, "0/20");
    assert.equal(real?.source.kind, "measured");
    for (const s of LATENCY_SCENARIOS) {
      assert.equal(s.source.from.length > 0, true, `${s.id} 缺来源`);
      assert.equal(s.metricLabel.length > 0, true, `${s.id} 缺口径名`);
    }
  });

  it("未测段 ms 为 null 且不计入合计", () => {
    for (const s of LATENCY_SCENARIOS) {
      const untested = s.segments.filter((x) => !x.measured);
      assert.equal(untested.length > 0, true, `${s.id} 应如实标出未测段`);
      for (const u of untested) assert.equal(u.ms, null);
      const total = s.segments.filter((x) => x.measured).reduce((a, b) => a + (b.ms ?? 0), 0);
      assert.equal(total > 0, true);
    }
  });

  it("真链路分段可加总，且 P50 与分段和一致", () => {
    const real = LATENCY_SCENARIOS.find((s) => s.id === "real")!;
    const sum = real.segments.filter((s) => s.measured).reduce((a, b) => a + (b.ms ?? 0), 0);
    assert.equal(sum, 3552, "reply 2552 + tts 1000");
    assert.equal(real.p50, 3549, "报告 P50 原文值");
  });

  it("流式探针两段可加总（首句 + 后续句 = 整轮）", () => {
    const probe = LATENCY_SCENARIOS.find((s) => s.id === "stream")!;
    const sum = probe.segments.filter((s) => s.measured).reduce((a, b) => a + (b.ms ?? 0), 0);
    assert.equal(Math.round(sum * 10) / 10, 53.4);
  });

  it("预算常量是 1500ms，与 AGENTS §7 同源", () => {
    assert.equal(LATENCY_BUDGET_MS, 1500);
  });

  it("exportSnapshot 产出可解析 JSON，带来源与未测标记", () => {
    const real = LATENCY_SCENARIOS.find((s) => s.id === "real")!;
    const parsed = JSON.parse(exportSnapshot(real, "p95")) as Record<string, unknown>;
    assert.equal(parsed.scenario, "real");
    assert.equal(parsed.quantile, "p95");
    assert.equal(parsed.value_ms, real.p95);
    assert.equal(parsed.budget_ms, 1500);
    assert.equal(parsed.metric_label, real.metricLabel);
    assert.equal(typeof parsed.source, "object");
    const segments = parsed.segments as { measured: boolean; ms: number | null }[];
    assert.equal(segments.some((s) => !s.measured && s.ms === null), true);
  });

  it("exportSnapshot 对三个场景 × 三个分位都可解析", () => {
    for (const s of LATENCY_SCENARIOS) {
      for (const q of ["p50", "p95", "max"] as const) {
        const parsed = JSON.parse(exportSnapshot(s, q)) as { value_ms: number };
        assert.equal(parsed.value_ms, s[q]);
      }
    }
  });
});

describe("WP · 沙盘控件齐备且常驻", () => {
  const text = portal("components/LatencySandbox.tsx");

  it("七类控件都在：场景 / 分位 / 播放暂停 / 重置 / 速度 / 预算线 / 导出", () => {
    assert.equal(text.includes("LATENCY_SCENARIOS.map"), true, "场景选择");
    assert.equal(text.includes("QUANTILES.map"), true, "分位选择");
    assert.equal(text.includes("togglePlay"), true, "播放/暂停");
    assert.equal(text.includes("onClick={reset}"), true, "重置");
    assert.equal(text.includes("SPEEDS.map"), true, "速度");
    assert.equal(text.includes("setShowBudget"), true, "预算线开关");
    assert.equal(text.includes("doExport"), true, "导出");
  });

  it("控件区不折叠：无 details/summary、无条件隐藏控件区", () => {
    assert.equal(/<details|<summary/.test(text), false);
    assert.equal(/p-controls[^>]*hidden/.test(text), false);
  });

  it("单一计时器循环：清旧再起新，不叠加；刻意不用 rAF", () => {
    // 页面 hidden / 未获焦点时 rAF 不触发（实测 rafFired=false），播放会永远停住。
    assert.equal(text.includes("setInterval"), true);
    assert.equal(text.includes("clearInterval"), true);
    assert.equal(/return\s*\(\)\s*=>\s*\{[\s\S]*?clearInterval/.test(text), true);
    assert.equal(
      /requestAnimationFrame/.test(text),
      false,
      "沙盘不得依赖 rAF：隐藏页里它不触发",
    );
  });

  it("播完即停与递增分开，不在 state updater 里产生副作用", () => {
    assert.equal(/if \(playing && progress >= 1\) setPlaying\(false\)/.test(text), true);
  });

  it("尊重 reduced-motion：直接落到终值而不是空转", () => {
    assert.equal(text.includes("prefersReducedMotion"), true);
  });

  it("导出失败有用户可见反馈，不静默", () => {
    assert.equal(/catch\s*\{[\s\S]*?onToast\("导出失败/.test(text), true);
  });
});

// ------------------------------------------------------------------ 内容完整性

describe("WP · 内容完整且诚实", () => {
  it("三端门卡都有真的进入链接", () => {
    assert.equal(ENDS.length, 3);
    assert.deepEqual(
      ENDS.map((e) => e.href),
      ["/", "/console", "/family"],
    );
    for (const e of ENDS) {
      assert.equal(e.capabilities.length >= 5, true, `${e.name} 能力清单过薄`);
      assert.equal(e.duty.length > 20, true);
    }
  });

  it("门禁看板保留红灯，且每条带来源", () => {
    assert.equal(GATES.some((g) => g.state === "fail"), true, "真链路 0/20 必须保留");
    assert.equal(GATES.some((g) => g.state === "warn"), true, "后端 56 errors 必须保留");
    for (const g of GATES) {
      assert.equal(g.source.from.length > 0, true, `${g.name} 缺来源`);
      assert.equal(g.detail.length > 0, true);
    }
    assert.equal(GATES.some((g) => g.detail.includes("56 errors")), true);
  });

  it("覆盖率标注为转记而非自跑", () => {
    const cov = GATES.find((g) => g.name === "后端覆盖率");
    assert.equal(cov?.source.kind, "reported");
    assert.equal(cov?.source.from.includes("Lead"), true);
  });

  it("非 pad 入口产物 VRM 标记为 0（vrm chunk 本身除外，它就该含 VRM）", () => {
    const others = ARTIFACTS.filter((x) => x.name !== "pad" && x.name !== "vrm chunk");
    assert.equal(others.length >= 4, true);
    for (const a of others) {
      assert.equal(a.vrm, 0, `${a.name} 不应含 VRM`);
    }
    const vrmChunk = ARTIFACTS.find((x) => x.name === "vrm chunk");
    assert.equal((vrmChunk?.vrm ?? 0) > 0, true, "vrm chunk 应含 VRM 代码");
  });

  it("隔离断言口径必须是代码符号 + 结构引用，不是裸字符串", () => {
    // 门户文案正当地提到 "three-vrm" 与 VRM 运行时类名，用它们做断言会被自己的
    // 说明文字误报（实测踩过两次）。改用渲染器类名 + HTML 引用清单双重判据。
    const gate = GATES.find((g) => g.name === "three-vrm 隔离");
    assert.equal(gate?.detail.includes("只有 index.html 引用"), true);
    assert.equal(ARTIFACTS.some((a) => a.name === "portal"), true, "门户自身也要进体积表");
    assert.equal(ARTIFACTS.some((a) => a.name === "vrm chunk"), true, "vrm 独立 chunk 要可见");
    const baselines = portal("baselines.ts");
    assert.equal(baselines.includes("WebGLRenderer"), true, "判据应写明用渲染器类名");
  });

  it("硬性数值 12 条齐全，与 AGENTS §7 对齐", () => {
    assert.equal(HARD_NUMBERS.length, 12);
    const keys = HARD_NUMBERS.map((h) => h.k);
    for (const k of ["工具预算", "声纹判定", "健康红标", "简单指令响应", "规则层规模"]) {
      assert.equal(keys.includes(k), true, `缺 ${k}`);
    }
  });

  it("七个区块都有 id，顶栏锚点与之一一对应", () => {
    const app = portal("App.tsx");
    for (const id of ["hero", "ends", "routing", "latency", "access", "gates"]) {
      assert.equal(app.includes(`id="${id}"`), true, `缺区块 ${id}`);
    }
    assert.equal(app.includes("useScrollSpy"), true);
    assert.equal(app.includes("aria-current"), true);
  });

  it("门户刻意不做声音（阅读与导航面，产品声音在 pad）", () => {
    for (const f of ["App.tsx", "components/LatencySandbox.tsx"]) {
      assert.equal(/new Audio\(|AudioContext|speechSynthesis/.test(portal(f)), false);
    }
  });
});

// ------------------------------------------------------------------ WP 顺手项 C-6

describe("WP · C-6 照护台默认落地页与导航首位一致", () => {
  const router = read("frontend/src/console/router.tsx");

  it("DEFAULT_ROUTE 是 /home，不是 /elders", () => {
    assert.equal(
      /export const DEFAULT_ROUTE = "\/home";/.test(router),
      true,
      "期望：落地页 = 导航首位「首页」。原先 /elders 与 NAV_ITEMS[0] 对不上，首页存在却不是落地页",
    );
  });

  it("导航首位确实是首页（两者必须同源，否则改了也白改）", () => {
    const navAt = router.indexOf("export const NAV_ITEMS");
    const first = router.slice(navAt, router.indexOf("},", navAt));
    assert.equal(
      /path:\s*"\/home",\s*label:\s*"首页"/.test(first),
      true,
      "NAV_ITEMS 首位应为 /home 首页",
    );
  });

  it("首页能力仍挂 elder:read（三角色都有，不会把护工送进 403）", () => {
    const navAt = router.indexOf("export const NAV_ITEMS");
    const first = router.slice(navAt, router.indexOf("},", navAt));
    assert.equal(/capability:\s*"elder:read"/.test(first), true);
  });
});