/**
 * spike 用的最小静态服务器（不引 vite，见 index.html 顶部注释）。
 *
 * 关键一点：把 /avatars/* 指到仓库里真实的 deploy/assets/avatars/*，
 * 于是页面请求的 URL 与生产 nginx 完全一致，测出来的就是线上那条路。
 * 唯一例外是 /avatars/animations/*：生产里由 vite 从 frontend/public 复制进 dist，
 * 仓库里则只存在于前端静态目录，故单独重写到那里（URL 仍是生产路径）。
 *
 *   node serve.mjs   →   http://127.0.0.1:5273
 */
import { createServer } from "node:http";
import { createReadStream, statSync } from "node:fs";
import { extname, join, normalize, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname);
const ASSETS = resolve(ROOT, "../../deploy/assets");
/** .vrma 落在前端静态目录（task-12 交付）；生产构建后它就是 /avatars/animations/* */
const ANIMS = resolve(ROOT, "../../frontend/public/avatars/animations");
const ANIMS_URL_PREFIX = "/avatars/animations/";
const PORT = Number(process.env.PORT ?? 5273);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".vrm": "application/octet-stream",
  ".glb": "model/gltf-binary",
};

function toFile(pathname) {
  const clean = decodeURIComponent(pathname.split("?")[0]);
  // /avatars/animations/* 在生产里由 vite 从 frontend/public 复制进 dist；
  // 仓库里只有前端静态目录那一份，直接重写过去，URL 仍是线上口径。
  if (clean.startsWith(ANIMS_URL_PREFIX)) {
    const target = resolve(ANIMS, `.${clean.slice(ANIMS_URL_PREFIX.length - 1)}`);
    return target.startsWith(ANIMS) ? target : null;
  }
  // 兼容旧 spike 页面用的短路径 /anims/*（等价于上面那条）
  if (clean.startsWith("/anims/")) {
    const target = resolve(ANIMS, `.${clean.slice("/anims".length)}`);
    return target.startsWith(ANIMS) ? target : null;
  }
  if (clean.startsWith("/avatars/")) {
    const target = resolve(ASSETS, `.${clean}`);
    return target.startsWith(ASSETS) ? target : null;
  }
  const target = resolve(ROOT, `.${normalize(clean)}`);
  if (!target.startsWith(ROOT)) return null;
  if (statSync(target, { throwIfNoEntry: false })?.isDirectory()) return join(target, "index.html");
  return target;
}

createServer((request, response) => {
  const pathname = request.url ?? "/";
  let file = null;
  try {
    file = toFile(pathname === "/" ? "/index.html" : pathname);
  } catch {
    file = null;
  }
  const stats = file ? statSync(file, { throwIfNoEntry: false }) : null;
  if (!file || !stats?.isFile()) {
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end(`404 ${pathname}`);
    return;
  }
  response.writeHead(200, {
    "content-type": MIME[extname(file)] ?? "application/octet-stream",
    "content-length": stats.size,
    "cache-control": "no-store",
  });
  createReadStream(file).pipe(response);
}).listen(PORT, "127.0.0.1", () => {
  console.log(`spike server → http://127.0.0.1:${PORT}`);
  console.log(`assets root → ${ASSETS}`);
});
