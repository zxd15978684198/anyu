/**
 * 构建护栏门禁（前端计划 W0，2026-11-13）。
 *
 * 现状缺口：前端无体积预算、three-vrm 隔离只靠 import 图碰巧成立、
 * Tailwind 扫全仓导致跨端泄漏（实测 family CSS 被撑到 2.7×）。
 * 本文件把这三条变成**会红的断言**：配置层永远断言，产物层在 dist 存在时断言。
 *
 * 沿用本仓口径：node:test + 源码/产物字符串断言，不引 vitest/jsdom。
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const root = new URL("../../", import.meta.url);
const read = (rel: string): string => readFileSync(new URL(rel, root), "utf8");
const distDir = new URL("frontend/dist/", root);
const assetsDir = new URL("assets/", distDir);
const hasDist = existsSync(assetsDir);

const ENTRIES = ["pad", "console", "family", "portal"] as const;
const HTML: Record<string, string> = {
  pad: "index.html",
  console: "console.html",
  family: "family.html",
  portal: "portal.html",
};

/** 体积上限（kB，未压缩）。取本轮实测值 + 合理余量；泄漏或误引大依赖会立刻撞线。
 *  CSS 预算在 W2/W3 加入共享 UI 原语（焦点环/滚动条/骨架屏/Toast/错误边界/图表）后
 *  从 24 提到 30：那是**合法新增功能**（每端约 +6.7 kB），不是跨端泄漏。
 *  泄漏的判据是「family/console 里出现 pad 专属 utility」，由独立断言守，不靠体积。 */
const JS_BUDGET: Record<string, number> = {
  pad: 320,
  console: 260,
  family: 60,
  portal: 80,
  client: 280,
  shared: 40,
  vrm: 820,
};
const CSS_BUDGET: Record<string, number> = {
  pad: 56,
  console: 30,
  family: 30,
  portal: 45,
};

function assets(ext: string): string[] {
  if (!hasDist) return [];
  return readdirSync(assetsDir).filter((f) => f.endsWith(ext));
}
function pick(prefix: string, ext: string): string | null {
  return assets(ext).find((f) => f.startsWith(`${prefix}-`)) ?? null;
}
function kb(file: string): number {
  return readFileSync(new URL(file, assetsDir)).length / 1000;
}

// ------------------------------------------------------------------ 配置层

describe("W0 · vite 四入口与显式分块", () => {
  const cfg = read("frontend/vite.config.ts");

  it("四个入口都在 rollupOptions.input", () => {
    for (const e of ENTRIES) {
      const file = e === "pad" ? "index.html" : `${e}.html`;
      assert.equal(
        new RegExp(`${e}:\\s*resolve\\(__dirname,\\s*"${file}"\\)`).test(cfg),
        true,
        `缺入口 ${e}`,
      );
    }
  });

  it("three/vrm 显式成独立 chunk（隔离从碰巧成立变结构成立）", () => {
    assert.equal(/manualChunks/.test(cfg), true);
    assert.equal(/@pixiv/.test(cfg), true);
    assert.equal(/return "vrm"/.test(cfg), true);
  });

  it("设了 chunkSizeWarningLimit，大依赖不再淹没在默认警告里", () => {
    assert.equal(/chunkSizeWarningLimit:\s*\d+/.test(cfg), true);
  });

  it("注释不再声称 console/family 用 MUI（2026-11-06 已裁决弃用）", () => {
    assert.equal(
      /console（照护台，MUI）|family（家属端，MUI）/.test(cfg),
      false,
      "vite.config.ts 头部注释仍写 MUI，与裁决相反",
    );
  });
});

describe("W0 · Tailwind 只扫本端，杜绝跨端泄漏", () => {
  for (const e of ENTRIES) {
    it(`${e} 用 source(none) + @source 限定本端`, () => {
      const css = read(`frontend/src/${e}/index.css`);
      assert.equal(
        /@import "tailwindcss" source\(none\);/.test(css),
        true,
        `${e} 未限定扫描范围，pad 专属 utility 会漏进来`,
      );
      assert.equal(/@source "\.\/\*\*\/\*\.tsx";/.test(css), true, `${e} 缺 @source tsx`);
      assert.equal(/@source "\.\/\*\*\/\*\.ts";/.test(css), true, `${e} 缺 @source ts`);
      assert.equal(
        css.includes('@import "../theme/tokens.css";'),
        true,
        `${e} 必须仍引入 tokens 唯一真源`,
      );
    });
  }
});

describe("W0 · 包管理器唯一（CI 只用 pnpm）", () => {
  it("CI 用 pnpm --frozen-lockfile，不用 npm", () => {
    const ci = read(".github/workflows/anyu.yml");
    assert.equal(/pnpm --dir frontend install --frozen-lockfile/.test(ci), true);
  });

  it("双锁文件仍在册时如实登记（删除需产品负责人裁决 Q-4，本断言只记录现状）", () => {
    const npmLock = existsSync(new URL("frontend/package-lock.json", root));
    const pnpmLock = existsSync(new URL("frontend/pnpm-lock.yaml", root));
    assert.equal(pnpmLock, true, "pnpm 锁必须在册");
    // 不 assert npmLock === false：删除已跟踪文件属 Q-4 待裁决，不擅自做。
    assert.equal(typeof npmLock, "boolean");
  });
});

// ------------------------------------------------------------------ 产物层

describe("W0 · 产物隔离（dist 存在时断言）", () => {
  it("只有 pad 的 index.html 引用 vrm chunk", () => {
    if (!hasDist) return;
    const vrm = pick("vrm", ".js");
    assert.equal(vrm !== null, true, "应产出独立 vrm chunk");
    for (const e of ENTRIES) {
      const html = read(`frontend/dist/${HTML[e]}`);
      const refs = html.includes(vrm as string);
      assert.equal(
        refs,
        e === "pad",
        e === "pad"
          ? "pad 必须引用 vrm chunk"
          : `${e} 不得引用 vrm chunk（PRD line 88 硬约束）`,
      );
    }
  });

  it("非 pad 入口与共享包的渲染器类名命中为 0", () => {
    if (!hasDist) return;
    // 用 WebGLRenderer 而不是裸串 three-vrm / VRM 运行时类名：
    // 门户文案会正当地提到那些词，用它们会被自己的说明文字误报（实测踩过两次）。
    for (const prefix of ["console", "family", "portal", "client"]) {
      const f = pick(prefix, ".js");
      if (!f) continue;
      const text = readFileSync(new URL(f, assetsDir), "utf8");
      assert.equal(
        text.includes("WebGLRenderer"),
        false,
        `${prefix} 产物含 three.js 渲染器，说明大依赖泄漏`,
      );
    }
  });

  it("family / console 产物不含 pad 专属 utility", () => {
    if (!hasDist) return;
    for (const prefix of ["family", "console"]) {
      const f = pick(prefix, ".css");
      if (!f) continue;
      const text = readFileSync(new URL(f, assetsDir), "utf8");
      for (const cls of [".grid-cols-2", ".grid-cols-3", ".animate-pulse"]) {
        assert.equal(
          text.includes(cls),
          false,
          `${prefix} CSS 含 pad 专属 ${cls}，Tailwind 扫描范围又放开了`,
        );
      }
    }
  });

  it("pad 自身 utility 未被误伤", () => {
    if (!hasDist) return;
    const f = pick("pad", ".css");
    if (!f) return;
    const text = readFileSync(new URL(f, assetsDir), "utf8");
    for (const cls of [".grid-cols-2", ".rounded-xl", ".text-xl"]) {
      assert.equal(text.includes(cls), true, `pad 少了 ${cls}，source() 限定误伤了本端`);
    }
  });

  it("四端 token 仍完整输出（@theme static 未被 source(none) 影响）", () => {
    if (!hasDist) return;
    for (const e of ENTRIES) {
      const f = pick(e, ".css");
      if (!f) continue;
      const text = readFileSync(new URL(f, assetsDir), "utf8");
      assert.equal(text.includes("--color-brand:"), true, `${e} 丢了品牌 token`);
      assert.equal(text.includes("--color-danger-ink:"), true, `${e} 丢了危险色 token`);
    }
  });
});

describe("W0 · 体积预算（dist 存在时断言）", () => {
  it("各入口 JS 在预算内", () => {
    if (!hasDist) return;
    for (const [prefix, limit] of Object.entries(JS_BUDGET)) {
      const f = pick(prefix, ".js");
      if (!f) continue;
      const size = kb(f);
      assert.equal(
        size <= limit,
        true,
        `${prefix} JS ${size.toFixed(2)} kB 超预算 ${limit} kB`,
      );
    }
  });

  it("各入口 CSS 在预算内（泄漏回归会立刻撞线）", () => {
    if (!hasDist) return;
    for (const [prefix, limit] of Object.entries(CSS_BUDGET)) {
      const f = pick(prefix, ".css");
      if (!f) continue;
      const size = kb(f);
      assert.equal(
        size <= limit,
        true,
        `${prefix} CSS ${size.toFixed(2)} kB 超预算 ${limit} kB`,
      );
    }
  });

  it("四个入口 HTML 都产出了", () => {
    if (!hasDist) return;
    for (const e of ENTRIES) {
      assert.equal(
        existsSync(new URL(`frontend/dist/${HTML[e]}`, root)),
        true,
        `缺产物 ${HTML[e]}`,
      );
    }
  });
});
// ------------------------------------------------------------------ 代码质量零计数
//
// 实测现状：explicit any 0 处、@ts-ignore/@ts-expect-error 0 处、非空断言 0 处，
// 且仓库未装任何 linter。tsc 已开 strict / noUnusedLocals / noUnusedParameters /
// noFallthroughCasesInSwitch，覆盖大部分规范面。
// 因此不引 eslint（新依赖、收益小），改为把这三个零计数**锁成回归断言**：
// 谁引入第一个 any 或类型抑制，门禁立刻变红。

function walkSource(dir: URL): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const child = new URL(name, dir);
    if (statSync(child).isDirectory()) out.push(...walkSource(new URL(`${name}/`, dir)));
    else if (/\.(ts|tsx)$/.test(name)) out.push(fileURLToPath(child));
  }
  return out;
}

describe("W0 · 代码质量零计数（锁住现状，防止悄悄变差）", () => {
  const files = walkSource(new URL("../src/", import.meta.url));

  it("源码文件数非零（防止 walk 写错导致门禁绿着跑 0 项）", () => {
    assert.equal(files.length > 50, true, `只扫到 ${files.length} 个文件，walk 可能坏了`);
  });

  it("零 explicit any", () => {
    const hits: string[] = [];
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      if (/:\s*any\b|\bas\s+any\b|<any>/.test(text)) hits.push(f);
    }
    assert.deepEqual(hits, [], `这些文件引入了 any：${hits.join(", ")}`);
  });

  it("零类型抑制（@ts-ignore / @ts-expect-error）", () => {
    const hits: string[] = [];
    for (const f of files) {
      if (/@ts-ignore|@ts-expect-error/.test(readFileSync(f, "utf8"))) hits.push(f);
    }
    assert.deepEqual(hits, [], `这些文件抑制了类型检查：${hits.join(", ")}`);
  });

  it("非空断言精确棘轮：只允许 realtime.ts 里既有的 3 处，新增即红", () => {
    // 实测既有 3 处全是 `pcConn.localDescription!.sdp`（offer/answer 握手）：
    // `setLocalDescription` 刚 resolve 后该字段必非空，是 WebRTC 的时序不变量。
    // realtime.ts 在本计划禁触清单里（三线同步核心），故不改它，改为锁住现状：
    // 数量与位置都钉死，任何**新增**非空断言（任何文件）立刻变红。
    const found: { file: string; count: number }[] = [];
    for (const f of files) {
      const code = readFileSync(f, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
      const n = (code.match(/[a-zA-Z_)\]]![.\[]/g) ?? []).length;
      if (n > 0) found.push({ file: f.replace(/\\/g, "/"), count: n });
    }
    assert.equal(found.length, 1, `非空断言只应出现在一个文件里，实际：${JSON.stringify(found)}`);
    assert.equal(
      found[0].file.endsWith("src/pad/realtime.ts"),
      true,
      `非空断言跑到了别的文件：${found[0].file}`,
    );
    assert.equal(found[0].count, 3, `realtime.ts 非空断言数量变了（既有 3 处 localDescription!.sdp）`);
    const text = readFileSync(files.find((f) => f.endsWith("realtime.ts")) as string, "utf8");
    assert.equal(
      (text.match(/localDescription!\.sdp/g) ?? []).length,
      3,
      "既有的 3 处必须都是 localDescription!.sdp，换成别的说明有人新增了断言",
    );
  });
});

describe("W0 · 包管理器唯一", () => {
  it("pnpm 锁在册，npm 锁不在册且已被忽略", () => {
    assert.equal(existsSync(new URL("frontend/pnpm-lock.yaml", root)), true);
    assert.equal(
      existsSync(new URL("frontend/package-lock.json", root)),
      false,
      "npm 锁应已移除：CI 只用 pnpm --frozen-lockfile，两份锁会装出不同依赖树",
    );
    assert.equal(
      read("frontend/.gitignore").includes("package-lock.json"),
      true,
      ".gitignore 应忽略 package-lock.json，防止再被误提交",
    );
  });
});

describe("W0 · 文档漂移门禁", () => {
  it("README 标注的 PRD 版本号 = PRD 修订记录末行版本号", () => {
    const readme = read("README.md");
    const prd = read("docs/安语3D数字人_PRD.md");
    // PRD 修订记录是 `| vX.Y | 日期 | 说明 |` 表；取最大版本号那行。
    const versions = [...prd.matchAll(/^\|\s*(v\d+\.\d+)\s*\|/gm)].map((m) => m[1]);
    assert.equal(versions.length > 0, true, "PRD 修订记录表没找到");
    const latest = versions.sort((a, b) => {
      const [am, an] = a.slice(1).split(".").map(Number);
      const [bm, bn] = b.slice(1).split(".").map(Number);
      return am - bm || an - bn;
    })[versions.length - 1];
    const readmeMatch = readme.match(/安语3D数字人_PRD\.md\)\s*\|\s*需求与验收基线（\*\*(v\d+\.\d+)\*\*/);
    assert.equal(readmeMatch !== null, true, "README 文档索引里找不到 PRD 版本标注");
    assert.equal(
      readmeMatch?.[1],
      latest,
      `README 标 ${readmeMatch?.[1]} 但 PRD 已到 ${latest} —— 文档漂移（曾长期标 v2.7 而实际 v3.2）`,
    );
  });
});
