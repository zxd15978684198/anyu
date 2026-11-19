/**
 * 前端用例门禁的**零用例守卫**（task-22 / N61b）。
 *
 * ## 为什么不是直接 `node --test "frontend/{src,tests}/**\/*.test.*"`
 *
 * 实测（node v26.5.1）：`node --test <匹配不到任何文件的 glob>` → **EXIT 0 且 `ℹ tests 0`**。
 * 也就是说用例文件没提交、或 glob 写错一个字，CI 那一步会「绿着跑 0 个用例」—— 门禁看起来
 * 通过，实际什么都没验。这是比「没有门禁」更坏的状态：它给出的是信任，而不是漏洞。
 * 所以必须有人数守卫：文件数为 0 → 硬失败；用例数为 0 但文件非空 → 同样硬失败。
 *
 * ## 为什么自己扫目录，不把 glob 交给 node
 *
 * 1. **pattern 语法要跨版本**：`{src,tests}` 这种 brace 展开在 node 26 上实测可用，但
 *    `--test` 的 glob 由 node 内部的匹配器实现，24 / 26 / 未来的行为不保证一致；CI 钉 24、
 *    本地是 26，两边门禁必须同义。这里只依赖标准库的 `readdirSync`，没有任何版本假设。
 * 2. **需要先拿到文件数**：守卫要在「跑之前」就知道匹配到了几个文件，才能区分
 *    「glob 写错 / 文件没提交」与「文件在、但里面一条用例都没有」。
 * 3. **argv 要一眼可懂**：三处调用（`frontend/package.json` / `scripts/ci.py` /
 *    `.github/workflows/anyu.yml`）只写 `node scripts/run-frontend-tests.mjs`，
 *    没有一个带引号的 glob 需要逐字符对齐。
 *
 * 收录范围 = 两个根目录下的 `*.test.{js,mjs,cjs,ts,mts,cts,tsx,jsx}`：
 * `frontend/src`（构建产物里也在，所以只放不参与 `tsc --noEmit` 的 `.mjs`）
 * `frontend/tests`（`.ts` 用例只能放这里：`frontend/tsconfig.json` 的 `include` 只有
 * `["src"]` 且 `types` 只有 `["vite/client"]`，`.ts` 用例一旦进 `src/`，
 * `pnpm build` 前置的 `tsc --noEmit` 会报 `TS2307 Cannot find module 'node:test'`）。
 *
 * ## 用法
 *
 *   node scripts/run-frontend-tests.mjs                       # 默认两个根目录
 *   pnpm --dir frontend test                                  # 同上（cwd=frontend 也可以）
 *   node scripts/run-frontend-tests.mjs frontend/nonexistent   # 反证：0 文件 → EXIT 1
 *
 * 退出码：0 = 有用例且全绿；1 = 子进程失败 / 零文件 / 零用例 / 环境缺依赖。
 */

import { spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK = path.join(REPO_ROOT, "scripts", "node-ts-hook.mjs");
/** `--import` 只认 URL 或裸包名：Windows 反斜杠路径会被判成非法包名（ERR_INVALID_MODULE_SPECIFIER）。 */
const HOOK_URL = pathToFileURL(HOOK).href;
const TS_DEV_DEP = path.join(REPO_ROOT, "frontend", "node_modules", "typescript");

/** 默认收录根目录；`.mjs` 可以留在 src（不参与 tsc），`.ts` 只能进 tests。 */
const DEFAULT_ROOTS = ["frontend/src", "frontend/tests"];
/** node:test 用例文件的命名约定。 */
const TEST_FILE = /\.test\.[cm]?[jt]sx?$/;

/** 递归收集 `dir` 下所有用例文件（不跟随符号链接，跳过 node_modules / dist）。 */
function collect(dir) {
  if (!existsSync(dir)) return [];
  const found = [];
  const pending = [dir];
  while (pending.length > 0) {
    const current = pending.pop();
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (entry.name === "node_modules" || entry.name === "dist") continue;
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        pending.push(full);
      } else if (entry.isFile() && TEST_FILE.test(entry.name)) {
        found.push(full);
      }
    }
  }
  return found;
}

function fail(message) {
  console.error(`[frontend-tests] ${message}`);
  process.exit(1);
}

/** 从子进程输出里取用例总数（node 的 spec reporter 打 `ℹ tests N`，TAP 打 `# tests N`）。 */
function countTests(output) {
  const spec = /(?:ℹ|#)\s*tests\s+(\d+)/.exec(output);
  return spec ? Number(spec[1]) : null;
}

const argvRoots = process.argv.slice(2).filter((item) => !item.startsWith("-"));
const roots = argvRoots.length > 0 ? argvRoots : DEFAULT_ROOTS;

const files = roots
  .flatMap((root) => collect(path.resolve(REPO_ROOT, root)))
  .sort((a, b) => a.localeCompare(b));

if (files.length === 0) {
  fail(
    `零用例：在 ${roots.join(" / ")} 下扫 *.test.{js,mjs,cjs,ts,mts,cts,tsx,jsx} 得到 0 个文件。\n` +
      `[frontend-tests] 这是硬失败，不是跳过：注意 \`node --test\` 对匹配不到任何文件的 glob 会以 0 退出，` +
      `如果不在这里拦住，门禁就会「绿着跑 0 个用例」。\n` +
      `[frontend-tests] 检查：用例文件是否已提交、是否放在 frontend/src 或 frontend/tests 下。`,
  );
}

if (!existsSync(HOOK)) {
  fail(`缺少加载钩子 ${path.relative(REPO_ROOT, HOOK)}（用于把无扩展名 import 解析到 .ts）`);
}
if (!existsSync(TS_DEV_DEP)) {
  fail(
    `找不到 frontend/node_modules/typescript —— 先装前端依赖：pnpm --dir frontend install\n` +
      `[frontend-tests] 该钩子复用项目已有的 typescript devDependency 转译 .ts 用例，不引入新依赖。`,
  );
}

const relative = files.map((file) => path.relative(REPO_ROOT, file).split(path.sep).join("/"));
console.error(`[frontend-tests] 收录 ${files.length} 个用例文件：`);
for (const file of relative) console.error(`[frontend-tests]   - ${file}`);

const child = spawnSync(process.execPath, ["--import", HOOK_URL, "--test", ...files], {
  cwd: REPO_ROOT,
  encoding: "utf8",
  maxBuffer: 64 * 1024 * 1024,
});

// 透传子进程输出：先 stdout 后 stderr，保持「看起来就是 node --test 自己在跑」。
if (child.stdout) process.stdout.write(child.stdout);
if (child.stderr) process.stderr.write(child.stderr);

if (child.error) {
  fail(`无法启动 node --test：${child.error.message}`);
}

const combined = `${child.stdout ?? ""}\n${child.stderr ?? ""}`;
const total = countTests(combined);
const pass = /(?:ℹ|#)\s*pass\s+(\d+)/.exec(combined)?.[1];
const failCount = /(?:ℹ|#)\s*fail\s+(\d+)/.exec(combined)?.[1];
const tally = `tests ${total ?? "?"} / pass ${pass ?? "?"} / fail ${failCount ?? "?"}`;

if (total === null) {
  fail(`无法从输出解析用例数（` + tally + `）：宁可红，也不让「没跑明白」当成通过。`);
}

const status = child.status ?? 1;

if (status !== 0) {
  fail(`用例失败：${tally}（退出码 ${status}）`);
}
if (total === 0) {
  fail(
    `零用例：匹配到 ${files.length} 个文件，但一条用例都没跑（${tally}）。` +
      `文件在、用例空，同样是假绿。`,
  );
}

console.error(`[frontend-tests] OK：${files.length} 个文件 / ${tally}`);
process.exit(0);
