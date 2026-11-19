/**
 * console「机构知识库」页（K3，FR-KNW-02 / AC-44）用例。
 *
 * 为什么放在 `frontend/tests/`：`frontend/tsconfig.json` 的 `include` 只有 `src`、`types` 只有
 * `vite/client`；放进 `src` 会让 `tsc --noEmit` 找不到 `node:test` 的类型。
 *
 * 为什么不引 jsdom / vitest：AGENTS §4-12 不许新增依赖。这里用 node 内置 test runner，
 * 页面在运行时用仓库已有的 `typescript` 转成 ESM 再交给 `react-dom/server` 渲染
 * （`scripts/node-ts-hook.mjs` 只认 `.ts`，不认 `.tsx`，所以 `.tsx` 必须自己转）。
 *
 * 三层证据：
 * 1. **纯函数**：标签映射、时区折算、表单校验——这些错了页面会「看起来正常但存错数据」；
 * 2. **端点封装**：打桩 `fetch` 断言 URL / 方法 / body，钉的是与 `app/api/knowledge.py` 的字面契约；
 * 3. **页面渲染 + 源码断言**：管理员看到四件事的入口、非管理员只有 403 文案、页面不许自己 fetch。
 *
 * 删掉什么会变红（反向验收口径）：
 * 1. `STATUS_LABEL` 里加「审核中」「已发布」→ 枚举用例红（TD 只有 draft/active/retired）；
 * 2. `toApiIso` 改成忽略时区直传字符串 → 时区用例红（东八区会整体偏 8 小时）；
 * 3. 列表行改回读 `row.embedding_provider` → 源码断言红（列表响应里没有这个字段）；
 * 4. 非管理员的 403 分支被删 → 渲染用例红（整页会掉进管理员视图）；
 * 5. `retireDocument` 忽略 `already_retired` → 重复停用文案用例红（会说成「已停用」而不是「本来就是」）。
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  ADMIN_ONLY_DESCRIPTION,
  ADMIN_ONLY_TITLE,
  AUTHORITY_LABEL,
  BODY_HINT,
  CATEGORY_LABEL,
  CATEGORY_ORDER,
  STATUS_LABEL,
  STATUS_ORDER,
  SUPERSEDE_HINT,
  authorityLabel,
  canSubmitForm,
  categoryLabel,
  createDocument,
  effectiveWindowText,
  emptyIngestForm,
  formatDateTime,
  listDocuments,
  retireDocument,
  statusLabel,
  statusTone,
  supersedeDocument,
  toApiIso,
  toLocalInput,
  validateForm,
} from "../src/console/knowledge";
import { HttpError } from "../src/console/http";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(HERE, "..");
const PAGE_PATH = resolve(FRONTEND_ROOT, "src/console/pages/KnowledgePage.tsx");
const CLIENT_PATH = resolve(FRONTEND_ROOT, "src/console/knowledge.ts");

const frontendRequire = createRequire(resolve(FRONTEND_ROOT, "package.json"));
const moduleUrl = (specifier: string): string => pathToFileURL(frontendRequire.resolve(specifier)).href;
const fileUrl = (relative: string): string => pathToFileURL(resolve(FRONTEND_ROOT, relative)).href;
const encode = (source: string): string =>
  `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;

// ---------------------------------------------------------------------------
// 1. 取值与标签：只有三值，且没有发明词
// ---------------------------------------------------------------------------

test("状态只有 TD 的三值，标签里没有「审核中」「已发布」这类发明词", () => {
  assert.deepEqual([...STATUS_ORDER], ["draft", "active", "retired"]);
  assert.deepEqual(
    STATUS_ORDER.map((status) => STATUS_LABEL[status]),
    ["草稿", "生效", "已退役"],
  );
  const labels = Object.values(STATUS_LABEL).join("");
  assert.doesNotMatch(labels, /审核|发布|上线/);
  assert.equal(statusLabel("active"), "生效");
  assert.equal(statusTone("retired"), "warning");
  assert.equal(statusLabel("something-new"), "something-new", "未知值原样显示，不猜");
});

test("类别六类与 TD 白名单一一对应", () => {
  assert.deepEqual(
    [...CATEGORY_ORDER],
    ["schedule", "meal", "activity", "policy", "facility", "service"],
  );
  assert.equal(CATEGORY_ORDER.length, Object.keys(CATEGORY_LABEL).length);
  assert.equal(categoryLabel("meal"), "餐饮");
  assert.equal(categoryLabel("policy"), "制度");
});

test("权威级别：机构规定与长者自述是两种东西，标签不合并", () => {
  assert.deepEqual(Object.keys(AUTHORITY_LABEL).sort(), ["elder_memory", "institution"]);
  assert.equal(authorityLabel("institution"), "机构规定");
  assert.equal(authorityLabel("elder_memory"), "长者自述（转录）");
  assert.notEqual(authorityLabel("institution"), authorityLabel("elder_memory"));
});

// ---------------------------------------------------------------------------
// 2. 时间：服务层把 naive 当 UTC，所以前端必须先折算
// ---------------------------------------------------------------------------

test("datetime-local 先折算成 UTC 再发，不能让东八区偏 8 小时", () => {
  const iso = toApiIso("2026-11-10T08:00");
  assert.ok(iso);
  assert.match(iso, /Z$/);
  // 同一瞬间：本地 08:00 与 UTC 的差就是本机时区偏移
  assert.equal(new Date(iso).getTime(), new Date("2026-11-10T08:00").getTime());
  assert.equal(toApiIso(""), null);
  assert.equal(toApiIso("   "), null);
  assert.equal(toApiIso("not-a-date"), null);
});

test("展示时间固定按 Asia/Shanghai，不随运行环境时区漂移", () => {
  const text = formatDateTime("2026-11-10T00:30:00Z");
  assert.match(text, /2026/);
  assert.match(text, /11/);
  assert.match(text, /08:30/, "UTC 00:30 在东八区是 08:30");
  assert.equal(formatDateTime(null), "—");
  assert.equal(formatDateTime("坏数据"), "坏数据", "解析不了就原样给人看，不吞掉");
});

test("生效窗口说话像人话：终点空就是长期有效", () => {
  assert.match(effectiveWindowText("2026-11-10T00:00:00Z", null), /长期有效/);
  assert.match(effectiveWindowText("2026-11-10T00:00:00Z", "2026-12-10T00:00:00Z"), /至/);
});

test("ISO 回填 datetime-local 不丢分钟", () => {
  const local = toLocalInput("2026-11-10T00:30:00Z");
  assert.match(local, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
  assert.equal(toLocalInput(null), "");
});

// ---------------------------------------------------------------------------
// 3. 表单校验：能在提交前拦下的就别让用户白等一次 422
// ---------------------------------------------------------------------------

test("新建表单：标题、正文、版本号三处各自报错", () => {
  const empty = emptyIngestForm();
  assert.equal(canSubmitForm(empty), false);
  assert.ok(validateForm(empty).title);
  assert.ok(validateForm(empty).body);

  const good = { ...empty, title: "午餐时间", body: "11:30 开餐", version: "2" };
  assert.equal(canSubmitForm(good), true);
  assert.deepEqual(validateForm(good), {});

  assert.match(String(validateForm({ ...good, version: "0" }).version), /正整数/);
  assert.match(String(validateForm({ ...good, version: "1.5" }).version), /正整数/);
});

test("生效终点必须晚于起点，相等也拒", () => {
  const base = { ...emptyIngestForm(), title: "t", body: "b" };
  const same = { ...base, effective_from: "2026-11-10T08:00", effective_until: "2026-11-10T08:00" };
  const reversed = { ...base, effective_from: "2026-11-10T08:00", effective_until: "2026-11-09T08:00" };
  const ok = { ...base, effective_from: "2026-11-10T08:00", effective_until: "2026-11-11T08:00" };

  assert.match(String(validateForm(same).effective_until), /晚于/);
  assert.match(String(validateForm(reversed).effective_until), /晚于/);
  assert.equal(validateForm(ok).effective_until, undefined);
});

test("换代可以不带版本号（缺省由服务层 +1），但必须带正文", () => {
  const form = { ...emptyIngestForm(), title: "忽略", version: "", body: "新的开餐时间" };
  assert.equal(canSubmitForm(form, true), true, "换代不看标题与版本号");
  assert.equal(canSubmitForm({ ...form, body: "  " }, true), false, "换代必须有正文");
});

// ---------------------------------------------------------------------------
// 4. 端点封装：断言与 app/api/knowledge.py 的字面契约
// ---------------------------------------------------------------------------

interface Recorded {
  url: string;
  method: string;
  body: string | null;
  auth: string | null;
}

const STORAGE = new Map<string, string>();

/** 打桩三件套：localStorage（authedFetch 读 access）、fetch（记录并回包）、原值还原。 */
function stubBrowser(responder: (recorded: Recorded) => { status: number; body: unknown }) {
  const recorded: Recorded[] = [];
  const globalAny = globalThis as unknown as Record<string, unknown>;
  const savedFetch = globalAny.fetch;
  const savedStorage = globalAny.localStorage;

  globalAny.localStorage = {
    getItem: (key: string) => STORAGE.get(key) ?? null,
    setItem: (key: string, value: string) => void STORAGE.set(key, value),
    removeItem: (key: string) => void STORAGE.delete(key),
  };
  STORAGE.set("kb_console_access_token", "token-abc");

  globalAny.fetch = async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    const headers = new Headers((init?.headers as HeadersInit | undefined) ?? {});
    const call: Recorded = {
      url,
      method: (init?.method ?? "GET").toUpperCase(),
      body: typeof init?.body === "string" ? init.body : null,
      auth: headers.get("authorization"),
    };
    recorded.push(call);
    const { status, body } = responder(call);
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  };

  return {
    recorded,
    restore: () => {
      globalAny.fetch = savedFetch;
      if (savedStorage === undefined) delete globalAny.localStorage;
      else globalAny.localStorage = savedStorage;
      STORAGE.clear();
    },
  };
}

test("列表：GET /api/knowledge/documents 带全量过滤参数，解析成 {items,total,limit,offset}", async () => {
  const page = {
    items: [
      {
        document_id: "d-1",
        title: "午餐时间",
        category: "meal",
        version: 2,
        authority_level: "institution",
        status: "active",
        effective_from: "2026-11-10T00:00:00Z",
        effective_until: null,
        chunks: 3,
      },
    ],
    total: 7,
    limit: 50,
    offset: 0,
  };
  const stub = stubBrowser(() => ({ status: 200, body: page }));
  try {
    const result = await listDocuments({ status: "active", category: "meal", q: "午餐", limit: 50, offset: 0 });
    const call = stub.recorded[0];
    assert.equal(call.method, "GET");
    const url = new URL(call.url, "http://localhost");
    assert.equal(url.pathname, "/api/knowledge/documents");
    assert.equal(url.searchParams.get("status"), "active");
    assert.equal(url.searchParams.get("category"), "meal");
    assert.equal(url.searchParams.get("q"), "午餐");
    assert.equal(url.searchParams.get("limit"), "50");
    assert.equal(url.searchParams.get("offset"), "0");
    assert.equal(call.auth, "Bearer token-abc", "必须带员工令牌");
    assert.equal(result.items.length, 1);
    assert.equal(result.total, 7, "total 是后端单独 count 的，不是 items.length");
    assert.equal(result.limit, 50);
  } finally {
    stub.restore();
  }
});

test("列表：留空的过滤条件不拼进 URL（避免 status= 被后端当非法值）", async () => {
  const stub = stubBrowser(() => ({ status: 200, body: { items: [], total: 0, limit: 50, offset: 0 } }));
  try {
    await listDocuments({ status: "", category: "", q: "" });
    const url = new URL(stub.recorded[0].url, "http://localhost");
    assert.equal(url.search, "", `不该有任何查询参数，实际 ${url.search}`);
  } finally {
    stub.restore();
  }
});

test("新建：POST JSON，body 字段与 KnowledgeIngestIn 对齐", async () => {
  const stub = stubBrowser(() => ({
    status: 201,
    body: {
      document_id: "d-2",
      title: "午餐时间",
      category: "meal",
      version: 1,
      authority_level: "institution",
      status: "active",
      effective_from: "2026-11-10T00:00:00Z",
      effective_until: null,
      chunks: 2,
      embedding_provider: "HttpEmbeddingProvider",
      semantic_embedding: true,
    },
  }));
  try {
    const created = await createDocument({
      title: "午餐时间",
      body: "11:30 开餐",
      category: "meal",
      version: 1,
      authority_level: "institution",
      effective_from: "2026-11-10T00:00:00.000Z",
      effective_until: null,
    });
    const call = stub.recorded[0];
    assert.equal(call.method, "POST");
    assert.equal(new URL(call.url, "http://localhost").pathname, "/api/knowledge/documents");
    assert.deepEqual(JSON.parse(String(call.body)), {
      title: "午餐时间",
      body: "11:30 开餐",
      category: "meal",
      version: 1,
      authority_level: "institution",
      effective_from: "2026-11-10T00:00:00.000Z",
      effective_until: null,
    });
    assert.equal(created.chunks, 2);
    assert.equal(created.semantic_embedding, true);
  } finally {
    stub.restore();
  }
});

test("换代：路径带 document_id，版本号留空就是 null（服务层 +1）", async () => {
  const stub = stubBrowser(() => ({
    status: 201,
    body: {
      document_id: "d-1",
      title: "午餐时间",
      category: "meal",
      version: 2,
      authority_level: "institution",
      status: "active",
      effective_from: "2026-11-11T00:00:00Z",
      effective_until: null,
      chunks: 3,
      embedding_provider: "DeterministicEmbeddingProvider",
      semantic_embedding: false,
    },
  }));
  try {
    await supersedeDocument("d-1", { body: "新的开餐时间", version: null });
    const call = stub.recorded[0];
    assert.equal(new URL(call.url, "http://localhost").pathname, "/api/knowledge/documents/d-1/supersede");
    assert.deepEqual(JSON.parse(String(call.body)), { body: "新的开餐时间", version: null });
  } finally {
    stub.restore();
  }
});

test("停用：POST 并保留 already_retired，供页面区分「停用成功」与「本来就是停用」", async () => {
  const stub = stubBrowser(() => ({
    status: 200,
    body: {
      document_id: "d-1",
      title: "午餐时间",
      version: 2,
      status: "retired",
      effective_until: "2026-11-10T04:00:00Z",
      already_retired: true,
    },
  }));
  try {
    const result = await retireDocument("d-1");
    const call = stub.recorded[0];
    assert.equal(call.method, "POST");
    assert.equal(new URL(call.url, "http://localhost").pathname, "/api/knowledge/documents/d-1/retire");
    assert.equal(result.already_retired, true);
    assert.equal(result.status, "retired");
  } finally {
    stub.restore();
  }
});

test("409 与 503 各带自己的 code，页面据此分支而不是解析 detail", async () => {
  const stub = stubBrowser((call) =>
    call.method === "POST"
      ? { status: 409, body: { detail: "duplicate", code: "CONFLICT", trace_id: "t-1" } }
      : { status: 503, body: { detail: "embedding down", code: "SERVICE_NOT_READY", trace_id: "t-2" } },
  );
  try {
    const conflict = await createDocument({
      title: "t",
      body: "b",
      category: "meal",
      version: 1,
      authority_level: "institution",
    }).then(
      () => null,
      (err: unknown) => err,
    );
    assert.ok(conflict instanceof HttpError, "同名同版本必须以 HttpError 抛出，而不是字符串");
    assert.equal(conflict.code, "CONFLICT");

    const notReady = await listDocuments({}).then(
      () => null,
      (err: unknown) => err,
    );
    assert.ok(notReady instanceof HttpError);
    assert.equal(notReady.code, "SERVICE_NOT_READY");
  } finally {
    stub.restore();
  }
});

// ---------------------------------------------------------------------------
// 5. 页面渲染：管理员看到四件事，非管理员只有 403
// ---------------------------------------------------------------------------

/**
 * 把 `KnowledgePage.tsx` 转成可 import 的 ESM（同 `memory-page.test.ts` 的手法）：
 * 1. `transpileModule` 去类型、按 React 19 自动运行时产 `jsx()` 调用；
 * 2. 每个 import 说明符换成绝对 file:// URL（`.ts` 交给 node 的 TS 钩子）；
 * 3. `../ui/*` 换成只回显 props 的最小桩件——断言的是页面自己的文案与结构。
 */
async function renderKnowledgePage(principal: unknown): Promise<string> {
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
  return h("div", { "data-ui": "empty-state" }, [
    h("h2", { key: "t" }, title),
    description ? h("p", { key: "d" }, description) : null,
    action ? h("div", { key: "a" }, action) : null,
  ]);
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
export function Select({ id, value, onChange, options, disabled }) {
  return h(
    "select",
    { id, value, disabled: Boolean(disabled), "data-ui": "select", onChange: () => undefined },
    (options ?? []).map((option) => h("option", { key: option.value, value: option.value }, option.label)),
  );
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
    h("tbody", { key: "b" }, rows.map((row) =>
      h("tr", { key: rowKey(row) }, columns.map((column) =>
        h("td", { key: column.key }, column.render ? column.render(row) : String(row[column.key] ?? "")))))),
  ]);
}
`);

  const specifiers: Record<string, string> = {
    react: reactUrl,
    "react/jsx-runtime": jsxRuntimeUrl,
    "../knowledge": fileUrl("src/console/knowledge.ts"),
    "../http": fileUrl("src/console/http.ts"),
    "../permissions": fileUrl("src/console/permissions.ts"),
    "../ui/Feedback": feedbackStub,
    "../ui/Form": formStub,
    "../ui/Dialog": dialogStub,
    "../ui/Table": tableStub,
  };

  const transpiled = ts.transpileModule(readFileSync(PAGE_PATH, "utf8"), {
    fileName: "KnowledgePage.tsx",
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
    // 通用兜底：白名单外的**相对** import 按页面真实目录解析（.ts / .tsx 都试）。
    // 没有这条时，页面新增一个 import（如 shared/ui/Notice）就会以 data: URL 为 base
    // 解析相对路径 → TypeError: Invalid URL，整个用例挂掉。
    // 改成通用解析后，页面加依赖不必再回来改这份名单。
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
    KnowledgePage: (props: { principal: unknown }) => unknown;
  };
  const react = (await import(reactUrl)) as {
    createElement: (type: unknown, props: Record<string, unknown>) => unknown;
  };
  const server = (await import(moduleUrl("react-dom/server"))) as {
    renderToStaticMarkup: (element: unknown) => string;
  };
  return server.renderToStaticMarkup(react.createElement(page.KnowledgePage, { principal }));
}

const ADMIN = { kind: "staff", id: "s-admin", role: "admin", name: "王管理" };
const DOCTOR = { kind: "staff", id: "s-doctor", role: "doctor", name: "李医生" };

test("管理员：列表/新建/换代/停用四件事的入口都在，且说清机构知识的边界", async () => {
  const html = await renderKnowledgePage(ADMIN);

  assert.match(html, /机构知识库/);
  assert.match(html, /新建文档/);
  assert.match(html, /刷新/);
  assert.match(html, /全院共享/);
  assert.match(html, /不参与衰减/);
  assert.match(html, /data-ui="select"/);
  assert.match(html, /全部状态/);
  assert.match(html, /全部类别/);
  assert.match(html, /知识文档/);
  // 初次进入是加载态，不是空态，也不是一张假表
  assert.match(html, /data-ui="table-loading"/);
});

test("非管理员：整页只有 403 文案，没有新建入口也没有数据表", async () => {
  const html = await renderKnowledgePage(DOCTOR);

  assert.ok(html.includes(ADMIN_ONLY_TITLE), "非管理员必须看到明确的 403 文案");
  assert.ok(html.includes(ADMIN_ONLY_DESCRIPTION));
  assert.doesNotMatch(html, /新建文档/);
  assert.doesNotMatch(html, /data-ui="select"/, "没有长者/状态筛选");
  assert.doesNotMatch(html, /data-ui="table"/);
  assert.doesNotMatch(html, /data-ui="dialog"/);
});

// ---------------------------------------------------------------------------
// 6. 源码契约：页面不自己发请求、不读列表里没有的字段
// ---------------------------------------------------------------------------

test("页面只用 ../knowledge 的封装，自己不造 fetch", () => {
  const source = readFileSync(PAGE_PATH, "utf8");

  assert.match(source, /from "\.\.\/knowledge"/);
  assert.doesNotMatch(source, /\bfetch\(/, "端点封装只能在 ../knowledge，页面里不许再写一套");
  assert.match(source, /error=\{listError\}/, "失败必须进 error 分支，不能落到空态");
  assert.match(source, /role="status"/, "操作结果要能被告知读屏");
  assert.match(source, /确认停用/, "停用必须二次确认");
  assert.match(source, /提交新版/, "换代必须二次确认");
  assert.match(source, /hint=\{BODY_HINT\}/, "正文输入区必须挂机构知识的边界提示");
  assert.match(source, /\{SUPERSEDE_HINT\}/, "换代弹窗必须说清旧版会退役");
});

test("三种状态各有出口：加载、失败可重试、空态不是一张空白表", () => {
  const source = readFileSync(PAGE_PATH, "utf8");

  // 加载与失败都交给 DataTable：失败时它渲染 ErrorBanner，空态时渲染 EmptyState
  assert.match(source, /loading=\{loading\}/);
  assert.match(source, /onRetry=\{\(\) => setReloadKey/);
  assert.match(source, /emptyTitle="[^"]*还没有/);
  assert.match(source, /emptyDescription="[^"]*新建文档/);
  // 列表行必须有「换代」「停用」两个动作，否则四件事只做到两件
  assert.match(source, />\s*换代\s*</);
  assert.match(source, />\s*停用\s*</);
});

test("列表行不读 embedding_provider / semantic_embedding（列表响应里没有这两个字段）", () => {
  const source = readFileSync(PAGE_PATH, "utf8");

  assert.doesNotMatch(source, /row\.embedding_provider/);
  assert.doesNotMatch(source, /row\.semantic_embedding/);
  assert.match(source, /created\.semantic_embedding/, "降级提示只能来自新建/换代的完整响应");
});

test("客户端契约文件：五个端点路径与中文标签都在，且没有发明状态词", () => {
  const source = readFileSync(CLIENT_PATH, "utf8");

  assert.match(source, /\/api\/knowledge\/search/);
  assert.match(source, /\/api\/knowledge\/documents"/);
  assert.match(source, /\/supersede/);
  assert.match(source, /\/retire/);
  assert.match(source, /\/api\/knowledge\/documents\$\{query/);
  // 状态词只允许出现在「不得自己发明……」那句注释里，标签取值不许有
  const labels = [...Object.values(STATUS_LABEL), ...Object.values(CATEGORY_LABEL)].join("|");
  assert.doesNotMatch(labels, /审核|发布|上线/);
  assert.ok(SUPERSEDE_HINT.length > 0);
  assert.ok(ADMIN_ONLY_TITLE.length > 0);
});
