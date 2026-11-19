/**
 * console「长者记忆」页（task-40 / FR-MEM-02）门禁用例。
 *
 * 为什么放在 `frontend/tests/`：`frontend/tsconfig.json` 的 `include` 只有 `src`，且 `types` 只有
 * `vite/client`；把用例放进 `src` 会让 `tsc --noEmit` 找不到 `node:test` 的类型。
 *
 * 为什么不引 jsdom / vitest：AGENTS §4-12 不许新增依赖。这里用 node 内置 test runner +
 * `frontend/node_modules` 里**已有**的 `react-dom/server`（`scripts/node-ts-hook.mjs` 只认 `.ts`，
 * 不认识 `.tsx`，所以页面在运行时用 `typescript` 转成 ESM，`../ui/*` 用最小桩件替换，
 * 断言的是本页自己产出的 DOM 字符串——不是桩件的措辞）。
 *
 * 删掉什么会变红（反向验收口径）：
 * 1. `mayHaveNextPage` 的 `count === limit` 改成 `total > limit` 或 `total / limit` → 翻页用例红；
 * 2. `deleteOutcomeText` 把 0 与 N 合成一句话 → 删除口径用例红；
 * 3. 页面里把 total 写成「共 N 条」或 `total / limit` → 页面渲染用例红；
 * 4. 把 5xx 画成「暂无记忆」（`listStateOf` 里先判 count 再判 error）→ 失败态用例红。
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  MEMORY_CONTENT_MAX,
  MEMORY_PAGE_SIZE,
  PAGE_TOTAL_HINT,
  SOURCE_HINT,
  categoryLabel,
  contentPreview,
  deleteOutcomeText,
  formatTimestamp,
  importanceText,
  listStateOf,
  mayHaveNextPage,
  memoryErrorText,
  pageNote,
  pageRangeText,
  previewImportance,
  sourceLabel,
  statusLabel,
  statusTone,
  validateChunkDraft,
} from "../src/console/memory";
import { HttpError } from "../src/console/http";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(HERE, "..");
const REPO_ROOT = resolve(FRONTEND_ROOT, "..");
const PAGE_PATH = resolve(FRONTEND_ROOT, "src/console/pages/MemoryPage.tsx");

const frontendRequire = createRequire(resolve(FRONTEND_ROOT, "package.json"));
const moduleUrl = (specifier: string): string => pathToFileURL(frontendRequire.resolve(specifier)).href;
const fileUrl = (relative: string): string => pathToFileURL(resolve(FRONTEND_ROOT, relative)).href;
const encode = (source: string): string =>
  `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;

// ---------------------------------------------------------------------------
// total 口径：total 是「当前页条数」，不许拿它算页数
// ---------------------------------------------------------------------------

test("翻页只看「本页条数 == limit」，不看 total", () => {
  assert.equal(mayHaveNextPage(20, 20), true);
  assert.equal(mayHaveNextPage(19, 20), false);
  assert.equal(mayHaveNextPage(0, 20), false);
  assert.equal(mayHaveNextPage(MEMORY_PAGE_SIZE, MEMORY_PAGE_SIZE), true);

  assert.match(pageNote(20, 20), /可能还有下一页/);
  assert.equal(pageNote(3, 20), "已经是最后一页。");
  assert.equal(pageNote(0, 20), "没有符合条件的记忆。");

  assert.equal(pageRangeText(0, 20), "第 1–20 条");
  assert.equal(pageRangeText(20, 3), "第 21–23 条");
  assert.equal(pageRangeText(0, 0), "本页没有记录");
});

test("total 的解释只有一句，且与契约 M1 一致", () => {
  assert.match(PAGE_TOTAL_HINT, /当前页/);
  assert.match(PAGE_TOTAL_HINT, /不是全部/);
});

// ---------------------------------------------------------------------------
// 失败 / 空态：5xx 不许画成「暂无记忆」
// ---------------------------------------------------------------------------

test("500 走错误分支，不当成空列表", () => {
  const error = memoryErrorText(new HttpError(500, "INTERNAL_ERROR", "boom"));
  assert.equal(listStateOf({ loading: false, error, count: 0 }), "error");
  assert.match(error, /不是「暂无记忆」/);
  assert.equal(listStateOf({ loading: true, error: null, count: 0 }), "loading");
  assert.equal(listStateOf({ loading: false, error: null, count: 0 }), "empty");
  assert.equal(listStateOf({ loading: false, error: null, count: 2 }), "ready");
});

test("403 / 404 / 422 各有自己的可见文案", () => {
  const forbidden = memoryErrorText(new HttpError(403, "ROLE_FORBIDDEN", "nope"));
  const missing = memoryErrorText(new HttpError(404, "NOT_FOUND", "nope"));
  const invalid = memoryErrorText(new HttpError(422, "VALIDATION_ERROR", "nope"));

  assert.match(forbidden, /仅对管理员开放/);
  assert.match(missing, /档案/);
  assert.match(invalid, new RegExp(`1–${MEMORY_CONTENT_MAX}`));

  const texts = new Set([forbidden, missing, invalid]);
  assert.equal(texts.size, 3, "403 / 404 / 422 不能共用一句话");
  assert.equal(listStateOf({ loading: false, error: forbidden, count: 0 }), "error");
});

// ---------------------------------------------------------------------------
// 删除口径：deleted_chunks=0 不是失败，但也不是「删掉了」
// ---------------------------------------------------------------------------

test("删除 0 条与删除 N 条必须是两句不同的话", () => {
  const removed = deleteOutcomeText(3);
  const nothing = deleteOutcomeText(0);

  assert.match(removed, /已软删除 3 条/);
  assert.doesNotMatch(removed, /本来就没有/);
  assert.match(nothing, /本来就没有/);
  assert.match(nothing, /deleted_chunks=0/);
  assert.match(nothing, /审计/);
  assert.doesNotMatch(nothing, /已软删除/);
  assert.notEqual(removed, nothing);
});

// ---------------------------------------------------------------------------
// 写入表单：超长拒收不截断；来源系数要能提前看见
// ---------------------------------------------------------------------------

test("正文超过 200 字被拦下并报出字数，不替用户截断", () => {
  assert.equal(validateChunkDraft({ content: "", importance: "1" }), "请填写记忆正文。");
  assert.equal(validateChunkDraft({ content: "   ", importance: "1" }), "请填写记忆正文。");

  const long = "长".repeat(MEMORY_CONTENT_MAX + 1);
  const message = validateChunkDraft({ content: long, importance: "1" });
  assert.ok(message);
  assert.match(message, new RegExp(`最多 ${MEMORY_CONTENT_MAX} 字（当前 ${MEMORY_CONTENT_MAX + 1} 字）`));

  assert.equal(validateChunkDraft({ content: "长".repeat(MEMORY_CONTENT_MAX), importance: "1" }), null);
  assert.equal(validateChunkDraft({ content: " 喜欢越剧 ", importance: "0.5" }), null);
});

test("重要度只在 0–1 之间接受", () => {
  assert.match(String(validateChunkDraft({ content: "abc", importance: "abc" })), /0 到 1/);
  assert.match(String(validateChunkDraft({ content: "abc", importance: "-0.1" })), /0 到 1/);
  assert.match(String(validateChunkDraft({ content: "abc", importance: "1.5" })), /0 到 1/);
  assert.equal(validateChunkDraft({ content: "abc", importance: "0" }), null);
  assert.equal(validateChunkDraft({ content: "abc", importance: "1" }), null);
});

test("inferred 的折算系数 0.6 在提交前就能看见", () => {
  assert.equal(previewImportance(1, "explicit"), "1.00");
  assert.equal(previewImportance(1, "inferred"), "0.60");
  assert.equal(previewImportance(0.9, "inferred"), "0.54");
  assert.equal(previewImportance(5, "inferred"), "1.00");
  assert.equal(previewImportance(Number.NaN, "explicit"), "—");
  assert.match(SOURCE_HINT, /0\.6/);
});

test("列表里的标签与预览按契约口径显示", () => {
  assert.equal(statusLabel("active"), "生效中");
  assert.equal(statusLabel("superseded"), "已被覆盖");
  assert.equal(statusLabel("deleted"), "已软删除");
  assert.equal(statusTone("superseded"), "warning");
  assert.equal(categoryLabel("family"), "家庭");
  assert.equal(categoryLabel("private"), "私人");
  assert.equal(sourceLabel("inferred"), "行为推断");
  assert.equal(importanceText(0.6), "0.60");
  assert.equal(contentPreview("张奶奶\n喜欢越剧", 20), "张奶奶 喜欢越剧");
  assert.match(contentPreview("长".repeat(80), 10), /^长{10}…$/);
  assert.equal(formatTimestamp(null), "—");
  assert.notEqual(formatTimestamp("2026-11-10T04:00:00+00:00"), "—");
});

// ---------------------------------------------------------------------------
// 页面渲染（假数据）：本页在 console 入口真正长出来的 DOM
// ---------------------------------------------------------------------------

interface RenderedPage {
  html: string;
}

/**
 * 把 `MemoryPage.tsx` 转成可 import 的 ESM：
 * 1. `typescript.transpileModule` 去掉类型、按 React 19 的自动运行时产出 `jsx()` 调用；
 * 2. 把每个 import 说明符换成绝对 file:// URL（`.ts` 由 node 的 TS 钩子转译）；
 * 3. `../ui/*` 换成最小桩件——它们只负责「把收到的 props 画出来」，本用例断言的是页面自己的文案与结构。
 */
async function renderMemoryPage(principal: unknown): Promise<RenderedPage> {
  const ts = frontendRequire("typescript") as {
    transpileModule: (source: string, options: Record<string, unknown>) => { outputText: string };
    ScriptTarget: { ES2022: number };
    ModuleKind: { ESNext: number };
    JsxEmit: { ReactJSX: number };
  };
  const reactUrl = moduleUrl("react");
  const jsxRuntimeUrl = moduleUrl("react/jsx-runtime");

  const stub = (body: string): string => encode(`import { createElement as h } from "${reactUrl}";\n${body}`);

  const feedbackStub = stub(`
export function Badge({ tone, children }) {
  return h("span", { "data-ui": "badge", "data-tone": tone ?? "neutral" }, children);
}
export function EmptyState({ title, description, action }) {
  return h("div", { "data-ui": "empty-state" }, [h("h2", { key: "t" }, title), description ? h("p", { key: "d" }, description) : null, action ? h("div", { key: "a" }, action) : null]);
}
export function LoadingState({ text }) {
  return h("div", { "data-ui": "loading-state" }, text ?? "加载中");
}
export function ErrorBanner({ message }) {
  return h("div", { "data-ui": "error-banner", role: "alert" }, message);
}
`);

  const formStub = stub(`
export function Field({ label, htmlFor, required, hint, error, children }) {
  return h("div", { "data-ui": "field", "data-for": htmlFor ?? "" }, [
    h("label", { key: "l", htmlFor }, [label, required ? h("span", { key: "r" }, "*") : null]),
    h("div", { key: "c" }, children),
    hint ? h("p", { key: "h", "data-ui": "hint" }, hint) : null,
    error ? h("p", { key: "e", "data-ui": "field-error" }, error) : null,
  ]);
}
export function FormGrid({ children }) {
  return h("div", { "data-ui": "form-grid" }, children);
}
export function FormActions({ children }) {
  return h("div", { "data-ui": "form-actions" }, children);
}
export function Select({ id, value, onChange, options, disabled }) {
  return h("select", {
    id,
    value,
    disabled: Boolean(disabled),
    "data-ui": "select",
    onChange: () => undefined,
  }, (options ?? []).map((option) => h("option", { key: option.value, value: option.value }, option.label)));
}
`);

  const dialogStub = stub(`
export function Dialog({ open, title, onClose, children, footer }) {
  if (!open) return null;
  return h("div", { "data-ui": "dialog", role: "dialog" }, [
    h("h2", { key: "t" }, title),
    h("div", { key: "b" }, children),
    footer ? h("div", { key: "f" }, footer) : null,
  ]);
}
`);

  const tableStub = stub(`
export function DataTable({ columns, rows, rowKey, loading, error, emptyTitle, emptyDescription, caption }) {
  if (error) return h("div", { "data-ui": "table-error", role: "alert" }, error);
  if (loading) return h("div", { "data-ui": "table-loading" }, caption ?? "加载中");
  if (rows.length === 0) {
    return h("div", { "data-ui": "table-empty" }, [
      h("h2", { key: "t" }, emptyTitle ?? "暂无数据"),
      emptyDescription ? h("p", { key: "d" }, emptyDescription) : null,
    ]);
  }
  return h("table", { "data-ui": "table" }, [
    h("caption", { key: "c" }, caption ?? ""),
    h("tbody", { key: "b" }, rows.map((row) => h("tr", { key: rowKey(row) }, columns.map((column) =>
      h("td", { key: column.key }, column.render ? column.render(row) : String(row[column.key] ?? "")))))),
  ]);
}
`);

  const specifiers: Record<string, string> = {
    react: reactUrl,
    "react/jsx-runtime": jsxRuntimeUrl,
    "../memory": fileUrl("src/console/memory.ts"),
    "../elders": fileUrl("src/console/elders.ts"),
    "../permissions": fileUrl("src/console/permissions.ts"),
    "../ui/Feedback": feedbackStub,
    "../ui/Form": formStub,
    "../ui/Dialog": dialogStub,
    "../ui/Table": tableStub,
  };

  // `import type` 由 transpileModule 直接删掉，剩下的都是运行期依赖
  const transpiled = ts.transpileModule(readFileSync(PAGE_PATH, "utf8"), {
    fileName: "MemoryPage.tsx",
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
      jsx: ts.JsxEmit.ReactJSX,
      verbatimModuleSyntax: false,
    },
  }).outputText;

  const rewritten = transpiled.replace(/from\s+"([^"]+)"/g, (whole, specifier: string) => {
    const target = specifiers[specifier];
    if (target) return `from "${target}"`;
    // 通用兜底：白名单外的相对 import 按页面真实目录解析（.ts / .tsx 都试）。
    // 否则页面新增 import（如 shared/ui/Notice）会以 data: URL 为 base 解析 → Invalid URL。
    if (specifier.startsWith("./") || specifier.startsWith("../")) {
      const base = resolve(PAGE_PATH, "..");
      for (const ext of [".ts", ".tsx", ""]) {
        const candidate = resolve(base, `${specifier}${ext}`);
        if (existsSync(candidate)) return `from "${pathToFileURL(candidate).href}"`;
      }
    }
    return whole;
  });

  const page = (await import(encode(rewritten))) as {
    MemoryPage: (props: { principal: unknown }) => unknown;
  };
  const react = (await import(reactUrl)) as {
    createElement: (type: unknown, props: Record<string, unknown>) => unknown;
  };
  const server = (await import(moduleUrl("react-dom/server"))) as {
    renderToStaticMarkup: (element: unknown) => string;
  };

  const html = server.renderToStaticMarkup(react.createElement(page.MemoryPage, { principal }));
  return { html };
}

const ADMIN = { kind: "staff", id: "s-admin", role: "admin", name: "王管理" };
const DOCTOR = { kind: "staff", id: "s-doctor", role: "doctor", name: "李医生" };

test("管理员看到的是「本页条数」口径，不是「共 N 条」", async () => {
  const { html } = await renderMemoryPage(ADMIN);

  assert.match(html, /长者记忆/);
  assert.ok(html.includes(PAGE_TOTAL_HINT), "页面上必须原样出现 total 的解释");
  assert.match(html, /清空该长者的生效记忆/);
  assert.match(html, /写入记忆/);
  assert.match(html, /新增 \/ 纠正一条记忆/);
  assert.match(html, /已经是最后一页|可能还有下一页|没有符合条件的记忆/);
  assert.doesNotMatch(html, /共\s*\d+\s*条/, "total 是当前页条数，不许写成「共 N 条」");
  assert.match(html, /还没有可管理的长者/, "空档案时给出空态，而不是一张假表");
  assert.match(html, /推断出来的记忆权重更低|折算/);
});

test("非管理员整页只有 403 文案，没有写入入口", async () => {
  const { html } = await renderMemoryPage(DOCTOR);

  assert.match(html, /当前角色无权查看长者记忆/);
  assert.match(html, /仅对管理员开放/);
  assert.doesNotMatch(html, /写入记忆/);
  assert.doesNotMatch(html, /清空该长者的生效记忆/);
  assert.doesNotMatch(html, /data-ui="table"/);
});

test("页面只用 ../memory 的封装与 ../http，自己不造 fetch", () => {
  const source = readFileSync(PAGE_PATH, "utf8");

  assert.match(source, /from "\.\.\/memory"/);
  assert.doesNotMatch(source, /\bfetch\(/, "端点封装只能在 ../memory，页面里不许再写一套");
  assert.doesNotMatch(source, /from "\.\.\/ui\/Pagination"/, "Pagination 按 total/pageSize 算页数，会与 M1 口径冲突");
  assert.match(source, /error=\{listError\}/, "失败必须进 error 分支，不能落到空态");
  assert.match(source, /role="status"/, "删除与写入的结果要能被告知读屏");
  assert.match(source, /确认软删除/, "删除必须二次确认");
});

// 断言仓库里确实有这条输出路径（而非在用例里自造）：页面文件本身可被读到
test("被测页面文件存在", () => {
  assert.ok(readFileSync(PAGE_PATH, "utf8").length > 0);
  assert.ok(resolve(REPO_ROOT, "scripts/run-frontend-tests.mjs").length > 0);
});
