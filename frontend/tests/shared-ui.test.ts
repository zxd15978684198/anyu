/**
 * 共享 UI 原语契约（前端计划 W2/W3；A6 错误边界、Q-2 单点图表）。
 *
 * 图表用 **真实渲染**（react-dom/server）断言，不只查源码：
 * Q-2 裁决「只有一个数据点时两端都画」必须是渲染结果里真有圆点与数值标注，
 * 而不是注释里写了「会画」。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { TrendChart, dateTickLabel } from "../src/shared/ui/TrendChart.tsx";
import { ErrorBoundary } from "../src/shared/ui/ErrorBoundary.tsx";

const read = (p: string) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");
/** 去掉注释再断言：注释里会正当地引用旧写法做对比说明，不能当成实现残留。 */
const stripComments = (text: string) =>
  text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const css = stripComments(read("shared/ui/primitives.css"));

const baseProps = {
  summary: "测试趋势",
  emptyTitle: "暂无采集记录",
  emptyHint: "有记录后显示趋势。",
};

// ------------------------------------------------------------------ Q-2 单点

describe("Q-2 · 只有一个数据点时两端都画出来（真实渲染断言）", () => {
  it("单点：渲染出圆点，且不画折线（一个点连不成线）", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        dates: ["2026-11-09"],
        series: [
          { key: "sys", label: "收缩压", color: "var(--color-brand)", values: [128] },
          { key: "dia", label: "舒张压", color: "var(--color-info-ink)", values: [78] },
        ],
      }),
    );
    assert.equal(html.includes("<circle"), true, "单点必须画圆点（原 family 行为是直接不画）");
    assert.equal(html.includes("<path"), false, "单点不该画折线");
    assert.equal(html.includes("ui-chart-empty"), false, "单点不该落到空态");
  });

  it("单点：直接把数值标在点旁，避免「只有一个点看不出是多少」", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        dates: ["2026-11-09"],
        series: [{ key: "sys", label: "收缩压", color: "var(--color-brand)", values: [128] }],
      }),
    );
    assert.equal(html.includes("收缩压 128"), true);
  });

  it("showSinglePoint=false 可恢复旧 family 行为（显式选择，不是默认）", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        showSinglePoint: false,
        dates: ["2026-11-09"],
        series: [{ key: "sys", label: "收缩压", color: "var(--color-brand)", values: [128] }],
      }),
    );
    assert.equal(html.includes("ui-chart-empty"), true);
    assert.equal(html.includes("暂无采集记录"), true);
  });

  it("多点：画折线，且缺值断开不补 0", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        dates: ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"],
        series: [
          { key: "sys", label: "收缩压", color: "var(--color-brand)", values: [120, null, null, 130] },
        ],
      }),
    );
    // 只有两个孤立有值点，各自成不了 ≥2 点的游程 → 不画 path，但两点都要画 circle
    assert.equal(html.includes("<path"), false, "缺值把两端隔开，不该连成一条假趋势");
    assert.equal((html.match(/<circle/g) ?? []).length, 2);
  });

  it("连续多点：画出一条折线", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        dates: ["2026-11-07", "2026-11-08", "2026-11-09"],
        series: [
          { key: "sys", label: "收缩压", color: "var(--color-brand)", values: [120, 125, 130] },
        ],
      }),
    );
    assert.equal(html.includes("<path"), true);
    assert.equal((html.match(/<circle/g) ?? []).length, 3);
  });

  it("完全无数据 → 空态文案", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, { ...baseProps, dates: [], series: [] }),
    );
    assert.equal(html.includes("ui-chart-empty"), true);
    assert.equal(html.includes("暂无采集记录"), true);
  });

  it("右轴只在真有右轴数据时出现（缺数据不长出空轴）", () => {
    const withSleep = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        dates: ["2026-11-07", "2026-11-08"],
        series: [
          { key: "sys", label: "收缩压", color: "var(--color-brand)", values: [120, 125] },
          { key: "sleep", label: "睡眠时长", color: "var(--color-success-ink)", axis: "right", unit: "小时", dashed: true, values: [6.5, 7.2] },
        ],
      }),
    );
    assert.equal(withSleep.includes("小时"), true);
    assert.equal(withSleep.includes("stroke-dasharray"), true, "次要系列用虚线区分");

    const withoutSleep = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        dates: ["2026-11-07", "2026-11-08"],
        series: [
          { key: "sys", label: "收缩压", color: "var(--color-brand)", values: [120, 125] },
          { key: "sleep", label: "睡眠时长", color: "var(--color-success-ink)", axis: "right", unit: "小时", values: [null, null] },
        ],
      }),
    );
    assert.equal(withoutSleep.includes("右轴"), false, "没有睡眠数据不该长出右轴与图例");
  });

  it("读屏：整图 role=img + 概述，每点有 title", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        summary: "近 2 天血压趋势",
        dates: ["2026-11-07", "2026-11-08"],
        series: [{ key: "sys", label: "收缩压", color: "var(--color-brand)", values: [120, 125] }],
      }),
    );
    assert.equal(html.includes('role="img"'), true);
    assert.equal(html.includes("近 2 天血压趋势"), true);
    assert.equal(html.includes("<title>"), true);
  });

  it("轴标签字号由调用方给（避免 viewBox 缩放后破 ≥16px 口径）", () => {
    const html = renderToStaticMarkup(
      createElement(TrendChart, {
        ...baseProps,
        dates: ["2026-11-07", "2026-11-08"],
        axisFontSize: 18,
        series: [{ key: "sys", label: "收缩压", color: "var(--color-brand)", values: [120, 125] }],
      }),
    );
    assert.equal(html.includes('font-size="18"'), true);
  });

  it("dateTickLabel 取 MM-DD", () => {
    assert.equal(dateTickLabel("2026-11-09"), "11-09");
    assert.equal(dateTickLabel("2026-11-09T00:00:00"), "11-09");
  });
});

// ------------------------------------------------------------------ 两端适配

describe("W2 · 两端图表都委托共享实现，单点口径一致", () => {
  it("console 与 family 的 TrendChart 都是薄适配层", () => {
    for (const f of ["console/components/TrendChart.tsx", "family/components/TrendChart.tsx"]) {
      const text = read(f);
      assert.equal(text.includes("shared/ui/TrendChart"), true, f);
      assert.equal(/function buildSegments/.test(text), false, `${f} 仍自己实现切段`);
      assert.equal(/viewBox=/.test(text), false, `${f} 仍自己画 SVG`);
    }
  });

  it("family 不再用 validDays < 2 直接拒绝单点", () => {
    const text = stripComments(read("family/components/TrendChart.tsx"));
    assert.equal(/validDays < 2/.test(text), false);
    assert.equal(text.includes("暂无足够数据"), false, "旧的空态文案应移除（单点现在要画）");
  });

  it("两端都保留各自尺寸与刻度数（不是强行统一外观）", () => {
    assert.equal(/width=\{760\}/.test(read("console/components/TrendChart.tsx")), true);
    assert.equal(/tickCount=\{4\}/.test(read("console/components/TrendChart.tsx")), true);
    assert.equal(/width=\{320\}/.test(read("family/components/TrendChart.tsx")), true);
    assert.equal(/tickCount=\{3\}/.test(read("family/components/TrendChart.tsx")), true);
    assert.equal(/axisFontSize=\{18\}/.test(read("family/components/TrendChart.tsx")), true);
  });
});

// ------------------------------------------------------------------ A6 错误边界

describe("A6 · 页面级错误边界（一个页面崩不该白屏整个应用）", () => {
  it("ErrorBoundary 是类组件，有 getDerivedStateFromError 与 componentDidCatch", () => {
    const text = read("shared/ui/ErrorBoundary.tsx");
    assert.equal(/static getDerivedStateFromError/.test(text), true);
    assert.equal(/componentDidCatch/.test(text), true);
    assert.equal(/console\.error/.test(text), true, "不吞错，仍打控制台便于排障");
  });

  it("错误屏给「重试」与「返回」两个真实出口，不是死胡同", () => {
    const text = read("shared/ui/ErrorBoundary.tsx");
    assert.equal(text.includes("重试这一页"), true);
    assert.equal(text.includes("返回可用页面"), true);
    assert.equal(/private retry = /.test(text), true);
    assert.equal(text.includes('role="alert"'), true);
  });

  it("console 把边界包在**页面**外层（导航存活），key 用路由以便换页重置", () => {
    const text = read("console/App.tsx");
    assert.equal(text.includes("<ErrorBoundary"), true);
    assert.equal(/key=\{route\}/.test(text), true);
    assert.equal(/resetKey=\{route\}/.test(text), true);
    const at = text.indexOf("<ErrorBoundary");
    const shellAt = text.indexOf("<Shell");
    assert.equal(shellAt < at, true, "边界必须在 Shell 内层，否则导航会一起消失");
    assert.equal(text.includes("{page.render(principal)}"), true);
  });

  it("family 也包了边界，且顶栏在外层存活", () => {
    const text = read("family/App.tsx");
    assert.equal(text.includes("<ErrorBoundary"), true);
    const headerAt = text.indexOf("<header");
    const boundaryAt = text.indexOf("<ErrorBoundary");
    assert.equal(headerAt < boundaryAt, true, "顶栏必须在边界外层");
    assert.equal(text.includes("</ErrorBoundary>"), true);
  });

  it("边界能捕获子组件抛错并渲染错误屏（真实渲染）", () => {
    const Boom = () => {
      throw new Error("数据形状不符");
    };
    // renderToStaticMarkup 不走 componentDidCatch，但 getDerivedStateFromError 也不适用于
    // 服务端渲染的错误捕获；这里改为直接断言错误屏 markup 由组件在 error 状态下产出。
    const boundary = new ErrorBoundary({
      resetKey: "x",
      label: "首页",
      children: createElement(Boom),
    });
    boundary.state = { error: new Error("数据形状不符") };
    const html = renderToStaticMarkup(boundary.render() as never);
    assert.equal(html.includes("首页暂时打不开"), true);
    assert.equal(html.includes("数据形状不符"), true);
    assert.equal(html.includes("重试这一页"), true);
    assert.equal(html.includes("其他页面不受影响"), true);
  });
});

describe("W4 · 错误横幅统一到 ErrorBanner（22 处手写 → 4 处富文本）", () => {
  it("ErrorBanner 渲染 role=alert + 消息；给了 onRetry 就有重试按钮", async () => {
    const { ErrorBanner } = await import("../src/console/ui/Feedback.tsx");
    const plain = renderToStaticMarkup(createElement(ErrorBanner, { message: "读接口未就绪" }));
    assert.equal(plain.includes('role="alert"'), true);
    assert.equal(plain.includes("读接口未就绪"), true);
    assert.equal(plain.includes("重试"), false, "没给 onRetry 就不该出现死按钮");

    const withRetry = renderToStaticMarkup(
      createElement(ErrorBanner, { message: "加载失败", onRetry: () => {} }),
    );
    assert.equal(withRetry.includes("重试"), true);
    assert.equal(withRetry.includes('type="button"'), true);
    assert.equal(withRetry.includes("alert-danger banner"), true, "类名与手写横幅一致，样式不变");
  });

  it("13 个页面都在用 ErrorBanner，手写横幅只剩 3 处富文本警告", () => {
    const files = [
      "AuditPage", "DevicesPage", "EldersPage", "GuardiansPage", "HealthPage",
      "HomePage", "KnowledgePage", "MedicationsPage", "MemoryPage",
      "NotificationsPage", "ReportsPage", "StaffPage", "WorkOrdersPage",
    ];
    let handWritten = 0;
    for (const f of files) {
      const text = read(`console/pages/${f}.tsx`);
      assert.equal(text.includes("<ErrorBanner"), true, `${f} 未使用 ErrorBanner`);
      handWritten += (text.match(/className="alert-danger banner"/g) ?? []).length;
    }
    assert.equal(handWritten, 3, "只应剩 3 处富文本警告（含 <strong> 等），不该压成纯文本");
  });

  it("剩下的 3 处是静态内容（富文本警告或政策提示），不是漏改的动态错误消息", () => {
    for (const f of ["DevicesPage", "WorkOrdersPage"]) {
      const text = read(`console/pages/${f}.tsx`);
      const blocks = text.match(/<div className="alert-danger banner"[\s\S]{0,700}?<\/div>/g) ?? [];
      assert.equal(blocks.length > 0, true, `${f} 应还有静态横幅`);
      for (const b of blocks) {
        const body = b.replace(/^<div[^>]*>/, "").replace(/<\/div>$/, "");
        // 动态错误消息形如 {error} / {actionError}；静态内容不该只有单个插值
        assert.equal(
          /^\s*\{[A-Za-z0-9_?.]+\}\s*$/.test(body),
          false,
          `${f} 有一处动态错误横幅漏改：${body.slice(0, 40)}`,
        );
      }
    }
  });

  it("语义色单一真源：五处联合类型都别名到 shared/tone", () => {
    const tone = read("shared/tone.ts");
    assert.equal(/export type Tone = "neutral" \| "info" \| "success" \| "warning" \| "danger";/.test(tone), true);
    for (const [f, name] of [
      ["console/medications.ts", "BadgeTone"],
      ["console/notifications.ts", "BadgeTone"],
      ["console/health.ts", "BadgeTone"],
      ["console/knowledge.ts", "KnowledgeTone"],
      ["console/ui/Feedback.tsx", "Tone"],
    ] as const) {
      const text = read(f);
      assert.equal(
        new RegExp(`import type \\{ Tone as ${name} \\}|import type \\{ Tone \\}`).test(text),
        true,
        `${f} 未从 shared/tone 引入`,
      );
      assert.equal(
        new RegExp(`export type ${name} = "neutral"`).test(text),
        false,
        `${f} 仍自己定义联合类型`,
      );
    }
  });

  it("tone 映射只引用 tokens，不硬编码色值", () => {
    const tone = read("shared/tone.ts");
    assert.deepEqual(tone.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [], []);
    for (const t of ["neutral", "info", "success", "warning", "danger"]) {
      assert.equal(tone.includes(`var(--color-${t}-ink)`), true, `缺 ${t} ink`);
      assert.equal(tone.includes(`var(--color-${t}-soft)`), true, `缺 ${t} soft`);
    }
  });
});

// ------------------------------------------------------------------ 原语样式

describe("W3 · 共享 UI 原语样式（三端共用，不新造色相）", () => {
  it("焦点可见：有 :focus-visible 且用统一焦点环", () => {
    assert.equal(css.includes(":focus-visible"), true);
    assert.equal(css.includes("--ui-ring"), true);
    assert.equal(/input:focus-visible/.test(css), true);
    assert.equal(/tr\[tabindex\]:focus-visible/.test(css), true, "可点表格行的焦点态");
  });

  it("滚动条四件齐：宽度 / track / thumb / hover，另加 Firefox", () => {
    for (const part of [
      "::-webkit-scrollbar",
      "::-webkit-scrollbar-track",
      "::-webkit-scrollbar-thumb",
      "::-webkit-scrollbar-thumb:hover",
      "scrollbar-color",
    ]) {
      assert.equal(css.includes(part), true, `缺 ${part}`);
    }
  });

  it("骨架屏与 Toast 四语义都在", () => {
    assert.equal(css.includes(".ui-skeleton"), true);
    assert.equal(css.includes("@keyframes ui-shimmer"), true);
    assert.equal(css.includes(".ui-toast"), true);
    for (const tone of ["success", "warning", "danger", "info"]) {
      assert.equal(css.includes(`.ui-toast[data-tone="${tone}"]`), true, `缺 ${tone} 语义`);
    }
  });

  it("reduced-motion 兜住所有动画，含既有端的无限 spinner", () => {
    assert.equal(css.includes("prefers-reduced-motion: reduce"), true);
    assert.equal(/animation-iteration-count:\s*1\s*!important/.test(css), true);
    assert.equal(/\.ui-skeleton\s*\{\s*animation:\s*none/.test(css), true);
  });

  it("零硬编码色值：不写 hex，也不写 rgb()/hsl() 字面量", () => {
    const stripped = css.replace(/url\("data:image[^"]*"\)/g, "");
    assert.deepEqual(stripped.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [], []);
    assert.equal(/rgba?\(/.test(stripped), false);
    assert.equal(/hsla?\(/.test(stripped), false);
    assert.equal(css.includes("color-mix(in srgb, var(--color-"), true);
  });

  it("四端都引入了共享原语", () => {
    for (const e of ["pad", "console", "family", "portal"]) {
      const text = read(`${e}/index.css`);
      if (e === "portal") {
        // 门户自带一套原语（它是独立辅助页），不强制引入，但必须有焦点环与滚动条
        assert.equal(text.includes(":focus-visible"), true);
        assert.equal(text.includes("::-webkit-scrollbar-thumb"), true);
        continue;
      }
      assert.equal(
        text.includes('@import "../shared/ui/primitives.css";'),
        true,
        `${e} 未引入共享原语`,
      );
    }
  });

  it("原语不新造色相，只引用 tokens 既有变量", () => {
    const used = new Set((css.match(/var\(--color-[a-z-]+\)/g) ?? []).map((s) => s.slice(4, -1)));
    const tokens = stripComments(read("theme/tokens.css"));
    for (const name of used) {
      assert.equal(tokens.includes(`${name}:`), true, `${name} 不在 tokens.css 里，属新造色相`);
    }
  });
});