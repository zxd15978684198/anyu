import { defineConfig, type Connect, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { isAbsolute, relative, resolve, extname } from "node:path";
import { createReadStream, realpathSync, statSync } from "node:fs";
import type { ServerResponse } from "node:http";

// MPA 多入口：pad（长者端，React + Tailwind）/ console（照护台）/ family（家属端）
// / portal（演示门户，工程辅助页，不占用 `/`）。
// console 与 family 用原生 CSS + tokens，**不引组件库**（2026-11-06 产品负责人裁决弃用 MUI）。
// 分层硬约束 4：pad 专属依赖（如 three-vrm）不得进其他入口构建产物。

/** 生产由 nginx 提供：`location /media/ { root /srv; try_files $uri =404; }`，compose 把仓库根 `media/` 挂到 `/srv/media`。 */
const MEDIA_PREFIX = "/media/";
/** 仓库根的 `media/`（本文件在 `frontend/` 下，所以是 `../media`）。dev 与生产共用同一份素材。 */
const MEDIA_ROOT = resolve(__dirname, "..", "media");

const MEDIA_CONTENT_TYPES: Record<string, string> = {
  ".mp3": "audio/mpeg",
  ".m4a": "audio/mp4",
  ".aac": "audio/aac",
  ".wav": "audio/wav",
  ".ogg": "audio/ogg",
  ".mp4": "video/mp4",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
};

/** 解析后的真实路径是否仍在 `root` 之内（`relative` 为空 = 就是 root 本身）。 */
function isInside(root: string, target: string): boolean {
  const rel = relative(root, target);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

/** 403/404 之类的最小响应；不回退 index.html —— 音频拿到 HTML 只会变成「能播但没声音」。 */
function reply(res: ServerResponse, status: number, body: string): void {
  res.statusCode = status;
  res.setHeader("Content-Type", "text/plain; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(body);
}

/**
 * dev server 的 `/media/**` 静态服务。
 *
 * 为什么不用 `server.proxy`：后端（8000）根本没有 `/media` 路由，代理过去只会拿到 404，
 * 而 vite 的 SPA fallback 也不会再兜底，症状从「回退 index.html」变成「代理 404」，一样不响。
 * 为什么不做 `frontend/public/media` 副本：生产读的是 `/srv/media`（宿主 `media/`），
 * dev 再复制一份就成了**两套源**，改一处忘一处，素材先对后错。所以 dev 直接读仓库根 `media/`。
 * 为什么必须挡路径穿越：middleware 拿到的是用户可控的 URL。`/media/../../.env` 若不挡，
 * `resolve()` 之后就是仓库根的 `.env`（JWT secret / 数据库口令），dev server 会把密钥原样吐出去；
 * 反斜杠、绝对路径、编码过的 `%2e%2e`、以及 `media/` 内的符号链接都要按同一个标准判定：
 * **解析后的真实路径必须仍在 media 根目录内**，否则 403。
 */
export function createMediaMiddleware(mediaRoot: string = MEDIA_ROOT) {
  return function mediaMiddleware(
    req: Connect.IncomingMessage,
    res: ServerResponse,
    next: Connect.NextFunction,
  ): void {
    const rawUrl = req.url ?? "";
    // 交给 vite 自己的静态/模块/HTML 流程处理别的路径
    if (!rawUrl.startsWith(MEDIA_PREFIX)) {
      next();
      return;
    }
    if (req.method !== "GET" && req.method !== "HEAD") {
      reply(res, 405, "Method Not Allowed");
      return;
    }

    // 只取 path 部分，再解码：`%2e%2e` 必须和 `..` 走同一套判定
    const pathname = rawUrl.split(/[?#]/, 1)[0];
    let decoded: string;
    try {
      decoded = decodeURIComponent(pathname);
    } catch {
      reply(res, 400, "Bad Request");
      return;
    }
    // 反斜杠在 Windows 上是分隔符，但在 URL 里不是：不拦就等于放行 `..\..\.env`
    if (!decoded.startsWith(MEDIA_PREFIX) || decoded.includes("\0") || decoded.includes("\\")) {
      reply(res, 403, "Forbidden");
      return;
    }

    const relativePath = decoded.slice(MEDIA_PREFIX.length);
    // 绝对路径（`/etc/...`、`C:\...`）与空段一律拒绝；`..` 段由下面的真实路径检查兜住
    if (relativePath === "" || isAbsolute(relativePath)) {
      reply(res, 403, "Forbidden");
      return;
    }
    const candidate = resolve(mediaRoot, relativePath);
    if (!isInside(mediaRoot, candidate)) {
      reply(res, 403, "Forbidden");
      return;
    }

    // 符号链接可以绕开上面的字面量检查，所以以 realpath 的结果再判一次
    let realRoot: string;
    let realFile: string;
    try {
      realRoot = realpathSync(mediaRoot);
      realFile = realpathSync(candidate);
    } catch {
      reply(res, 404, "Not Found");
      return;
    }
    if (!isInside(realRoot, realFile)) {
      reply(res, 403, "Forbidden");
      return;
    }

    let size: number;
    try {
      const stat = statSync(realFile);
      if (!stat.isFile()) {
        reply(res, 404, "Not Found");
        return;
      }
      size = stat.size;
    } catch {
      reply(res, 404, "Not Found");
      return;
    }

    const contentType = MEDIA_CONTENT_TYPES[extname(realFile).toLowerCase()] ?? "application/octet-stream";
    res.statusCode = 200;
    res.setHeader("Content-Type", contentType);
    res.setHeader("Content-Length", String(size));
    // dev 期间素材会被 Lead 重新抓取，别让浏览器拿旧缓存
    res.setHeader("Cache-Control", "no-store");
    if (req.method === "HEAD") {
      res.end();
      return;
    }
    createReadStream(realFile)
      .on("error", () => {
        res.destroy();
      })
      .pipe(res);
  };
}

/** 只挂在 dev：生产由 nginx 负责，构建产物里不该出现这段逻辑。 */
function mediaDevServer(): Plugin {
  return {
    name: "anyu:media-dev-server",
    apply: "serve",
    configureServer(server) {
      // 直接 use（非 configureServer 的返回值）：必须早于 vite 的 HTML fallback，
      // 否则 `/media/x.mp3` 会先被回退成 index.html，middleware 永远轮不到。
      server.middlewares.use(createMediaMiddleware());
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), mediaDevServer()],
  // 每次构建（含 dev 启动）注入唯一指纹：长驻标签页在发版后仍跑旧 bundle，
  // 表现是「明明已经修好的问题照旧复现」。入口用这个指纹做一次强制重载（见 pad/buildInfo.ts）。
  define: {
    __PAD_BUILD__: JSON.stringify(new Date().toISOString()),
  },
  // 本机 8000 被占用时可用 ANYU_API_TARGET=http://127.0.0.1:8001 指到别的后端实例（演示/调试用）。
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: process.env.ANYU_API_TARGET ?? "http://127.0.0.1:8000", changeOrigin: true },
      // /metrics 不在 /api 前缀下（app/api/metrics.py 注册为 (metrics.router, "")），
      // 不单独代理会被 vite 回退成 index.html，照护台指标页会拿到 HTML 而不是 JSON。
      "/metrics": { target: process.env.ANYU_API_TARGET ?? "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: (process.env.ANYU_API_TARGET ?? "http://127.0.0.1:8000").replace(/^http/, "ws"), ws: true },
    },
  },
  build: {
    // pad 的 three/vrm 是 973KB 量级的大依赖，显式成独立 chunk：
    // ① 只有 pad 的 HTML 会引用它，隔离从「靠 import 图碰巧成立」变成结构上成立；
    // ② 浏览器可单独长缓存这块（版本钉死，改业务代码不会让它失效）。
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      input: {
        pad: resolve(__dirname, "index.html"),
        console: resolve(__dirname, "console.html"),
        family: resolve(__dirname, "family.html"),
        portal: resolve(__dirname, "portal.html"),
      },
      output: {
        manualChunks(id: string) {
          // 三端共享层显式成块：名字自描述，且与业务端分离便于长缓存。
          if (id.includes("/src/shared/")) return "shared";
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@pixiv") || id.includes("/three/")) return "vrm";
          return undefined;
        },
      },
    },
  },
});
