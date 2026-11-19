/**
 * `node --test` 的 TS 加载钩子（T1 补证据用；不新增任何依赖）。
 *
 * 为什么需要它：
 * 1. **解析**：前端源码是 Vite 风格的无扩展名 import（`import { MicCapture } from "./capture"`），
 *    Node 的 ESM 解析要求显式扩展名 —— 把 `./x` 解析成同目录 `./x.ts`，只做解析，不改语义。
 * 2. **转译**：kws.ts / capture.ts 用了 TS 参数属性（`constructor(public reason: ...)`），
 *    属于「不可擦除语法」。Node 26 的默认 strip-only 模式对这种写法直接抛
 *    `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX`，且 Node 26 已移除 `--experimental-transform-types`，
 *    内置 `stripTypeScriptTypes` 也只接受 mode:"strip"。
 *    所以这里复用项目**已有**的 devDependency `typescript`（`pnpm --dir frontend install` 后必然存在，
 *    不是新增依赖）做 transpileModule —— 产物只用于测试进程，仓库里的源码一个字节都不动。
 *
 * 用法（与本轮报告里的复跑命令一致）：
 *   node --import ./scripts/node-ts-hook.mjs --test frontend/tests/wake-word.test.ts
 */

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";

const frontendRequire = createRequire(new URL("../frontend/package.json", import.meta.url));
const typescript = frontendRequire("typescript");

registerHooks({
  resolve(specifier, context, nextResolve) {
    const isRelative = specifier.startsWith("./") || specifier.startsWith("../");
    const hasExtension = /\.[cm]?[jt]sx?$/.test(specifier);
    if (isRelative && !hasExtension && context.parentURL) {
      // 先 .ts 再 .tsx：组件（如 shared/ui/TrendChart.tsx）也要能在测试里真实渲染。
      for (const ext of [".ts", ".tsx"]) {
        const candidate = new URL(`${specifier}${ext}`, context.parentURL);
        try {
          readFileSync(fileURLToPath(candidate));
          return { url: candidate.href, shortCircuit: true };
        } catch {
          /* 试下一个扩展名 */
        }
      }
      return nextResolve(specifier, context);
    }
    return nextResolve(specifier, context);
  },

  load(url, context, nextLoad) {
    const isTs = url.endsWith(".ts") || url.endsWith(".tsx");
    if (isTs && !url.includes("node_modules")) {
      const fileName = fileURLToPath(url);
      const source = readFileSync(fileName, "utf8");
      const output = typescript.transpileModule(source, {
        fileName,
        compilerOptions: {
          target: typescript.ScriptTarget.ES2022,
          module: typescript.ModuleKind.ESNext,
          verbatimModuleSyntax: false,
          sourceMap: false,
          // .tsx 用自动 JSX 运行时（react/jsx-runtime），与 Vite 的 @vitejs/plugin-react 一致，
          // 不需要在每个组件里 import React。
          jsx: typescript.JsxEmit.ReactJSX,
        },
      }).outputText;
      return { format: "module", source: output, shortCircuit: true };
    }
    return nextLoad(url, context);
  },
});
