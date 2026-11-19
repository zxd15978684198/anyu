/*
 * Anyu 长者端 Service Worker —— PRD:338 FR-MED-08 第①层。
 *
 * 只做两件事：
 *   ① 让长者端页面本身在断网时还能打开（应用壳 + 导航兜底）；
 *   ② 把访问过的静态资源放进 runtime cache，断网重开不白屏。
 *
 * 明确不做：
 *   - **不缓存任何 `/api/**` 响应**。用昨天的药单糊弄今天，是这套兜底里最危险的
 *     错误 —— 页面会显示一条「已确认」的假象；当日药单由页面写 IndexedDB
 *     （`frontend/src/pad/offline/db.ts`），断网播报也由页面用 `speechSynthesis`
 *     完成，跟缓存命中与否无关。
 *   - **不碰 `/ws` 与 `/metrics`**（实时语音与指标是纯在线链路）。
 *   - **不发任何跨域请求**，更没有任何云端 TTS：断网前提下的语音由浏览器原生
 *     `speechSynthesis` 提供（计划件 §5 R4 红线）。
 */
const VERSION = "sm-pad-v1";
const SHELL_CACHE = VERSION + "-shell";
const RUNTIME_CACHE = VERSION + "-runtime";

/* 应用壳：长者端入口 + 图标。安装时尽力缓存，任何一项失败都不阻断安装
 * （离线能不能打开页面是「最好有」，断网能不能播报才是「必须有」）。 */
const SHELL_URLS = ["/", "/index.html", "/favicon.ico"];
/* 业务链路前缀：一律直连网络，进不了任何缓存。 */
const NEVER_CACHE_PREFIXES = ["/api/", "/ws", "/metrics"];
const STATIC_EXTENSIONS = [
  ".js",
  ".css",
  ".mjs",
  ".json",
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ".svg",
  ".woff",
  ".woff2",
  ".ttf",
  ".wasm",
  ".mp3",
  ".wav",
  ".onnx",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      await Promise.all(
        SHELL_URLS.map(async (url) => {
          try {
            const response = await fetch(url, { cache: "reload" });
            if (response.ok) await cache.put(url, response.clone());
          } catch (error) {
            /* 壳缓存失败只影响「断网重开页面」，不影响本地播报 */
            console.warn("[sw] 预缓存失败", url, error);
          }
        }),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((key) => !key.startsWith(VERSION)).map((key) => caches.delete(key)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (NEVER_CACHE_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))) return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
    return;
  }
  if (isStaticAsset(url.pathname)) {
    event.respondWith(cacheFirst(request));
  }
});

function isStaticAsset(pathname) {
  const lower = pathname.toLowerCase();
  return STATIC_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/* 导航：网络优先（页面要拿到最新版本），断网时回落到最近一次成功的页面副本。
 * 回落只给 HTML 壳，不给任何带数据的响应。 */
async function networkFirstNavigation(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, response.clone()).catch(() => undefined);
    }
    return response;
  } catch {
    const cache = await caches.open(SHELL_CACHE);
    const cached = (await cache.match(request)) || (await cache.match("/index.html"));
    if (cached) return cached;
    return new Response(
      "<!doctype html><meta charset=\"utf-8\"><title>已断网</title>" +
        "<body style=\"font:32px/1.5 sans-serif;padding:48px\">当前网络已断开，" +
        "正在使用本地用药提醒。请保持平板电量，恢复联网后会自动上报。</body>",
      { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } },
    );
  }
}

/* 静态资源：缓存优先（Vite 产物带内容哈希，命中即最新）。 */
async function cacheFirst(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone()).catch(() => undefined);
    return response;
  } catch (error) {
    const shell = await (await caches.open(SHELL_CACHE)).match(request);
    if (shell) return shell;
    throw error;
  }
}
