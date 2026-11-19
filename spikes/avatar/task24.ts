/**
 * task-24 复查探针：给四条修复取证（跑完删除 bundle，脚本留在库里可复现）。
 *
 * 直接 import 生产模块（AvatarStage / AvatarBoundary / VrmAvatar），不另抄一份逻辑：
 *   ① ?case=bad      坏 avatar_url → 5s 内切静态头像（不白屏）
 *   ② ?case=timeout  资产迟于预算到达 → 迟到那棵树必须被释放（P1-1 的唯一可观测信号：
 *                    BufferGeometry/Texture 的 dispose 计数在超时后应继续增长）
 *   ③ ?case=lost     VRM 正常出画后模拟 webglcontextlost → 回静态头像；restored → 回 canvas
 *   ④ ?case=chunk    lazy chunk 抛错 → ErrorBoundary 接住并落静态头像（页面不白）
 *   ⑤ ?case=handle   直接驱动 createAvatar：renderer.info.memory 归零 / 无残留 rAF /
 *                    forceContextLoss 真的把上下文弄丢
 *
 * 结果写 `window.__T24__`，同时打到 #log（截图可见）。
 */
import { createElement, lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
// 必须指到 frontend 下那份 three：探针裸 import "three" 会解析到 spikes/avatar/node_modules，
// 与 AvatarStage/VrmAvatar 用的那份是两个实例，原型 patch 就抓不到它们的 renderer。
import * as THREE from "../../frontend/node_modules/three/build/three.module.js";
import { AvatarStage } from "../../frontend/src/pad/avatar/AvatarStage";
import { AvatarBoundary } from "../../frontend/src/pad/avatar/AvatarBoundary";
import { createAvatar } from "../../frontend/src/pad/avatar/VrmAvatar";

type Json = Record<string, unknown>;

const params = new URLSearchParams(location.search);
const CASE = params.get("case") ?? "bad";

// ———————————————— 埋点（必须在挂载之前装好） ————————————————

/** rAF 计数：用来区分「循环还在自己排下一帧」与「只是我们的 await 在驱动帧」 */
const raf = { calls: 0, pending: 0 };
const nativeRaf = window.requestAnimationFrame.bind(window);
window.requestAnimationFrame = (cb: FrameRequestCallback): number => {
  raf.calls += 1;
  raf.pending += 1;
  let id = 0;
  id = nativeRaf((t) => {
    raf.pending -= 1;
    cb(t);
  });
  return id;
};

/** GPU 资源释放计数：超时路径没有 handle 可读，只能看 dispose 事件 */
const disposed = { geometry: 0, texture: 0, material: 0 };
const patchDispose = <T extends { dispose(): void }>(proto: T, key: keyof typeof disposed): void => {
  const original = proto.dispose;
  proto.dispose = function patched(this: T): void {
    disposed[key] += 1;
    original.apply(this);
  };
};
patchDispose(THREE.BufferGeometry.prototype, "geometry");
patchDispose(THREE.Texture.prototype, "texture");
patchDispose(THREE.Material.prototype, "material");

/**
 * 绘制调用计数：「帧循环还在跑」最直接的证据。
 * 注：three 的 WebGLRenderer 方法挂在**实例**上（构造器里 this.render = …），patch 原型抓不到；
 * 但 WebGL 上下文的方法是**真原型方法**，patch 它就能数到每一帧的 draw 调用。
 * 判据：活着时 >0，dispose 之后必须恒为 0。
 */
const draws = { calls: 0 };
for (const Ctx of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
  if (!Ctx) continue;
  for (const fn of ["drawElements", "drawArrays", "drawElementsInstanced", "drawArraysInstanced"] as const) {
    const original = (Ctx.prototype as unknown as Record<string, unknown>)[fn] as
      | ((...args: unknown[]) => unknown)
      | undefined;
    if (typeof original !== "function") continue;
    Ctx.prototype[fn] = function patchedDraw(this: unknown, ...args: unknown[]): unknown {
      draws.calls += 1;
      return original.apply(this, args);
    } as never;
  }
}

// ———————————————— 通用工具 ————————————————

const out: Json = { case: CASE };
(window as unknown as { __T24__: Json }).__T24__ = out;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const nextFrame = () => new Promise<number>((resolve) => window.requestAnimationFrame(resolve));
const advance = async (count: number): Promise<void> => {
  for (let i = 0; i < count; i += 1) await nextFrame();
};
/** 驱动若干帧，但带超时上限（防止某一侧卡住时整轮探针挂死）；超时后后台循环立即停手，避免污染后续计数 */
const driveFrames = async (frames: number, timeoutMs: number): Promise<{ fired: number; timedOut: boolean }> => {
  let fired = 0;
  let cancelled = false;
  const done = (async (): Promise<boolean> => {
    for (let i = 0; i < frames; i += 1) {
      if (cancelled) return false;
      await nextFrame();
      if (cancelled) return false;
      fired += 1;
    }
    return true;
  })();
  const finished = await Promise.race([done, sleep(timeoutMs).then(() => false)]);
  cancelled = true;
  return { fired, timedOut: !finished };
};

/**
 * 单帧前后「排队中的 rAF 请求数」变化：
 * - 循环活着：我们这一帧的请求被消费（−1），循环在同一帧里又排了一帧（+1）→ 净变化 0
 * - 循环已停：只有我们的请求被消费 → 净变化 −1
 * 用差值而不是绝对值，避免 headless 下冻结的陈旧请求把读数带偏。
 */
const pendingDeltaAfterOneFrame = async (): Promise<{ before: number; after: number; delta: number }> => {
  const before = raf.pending;
  await nextFrame();
  await sleep(16);
  return { before, after: raf.pending, delta: raf.pending - before };
};

/** 空窗期自增的 rAF 请求数：循环活着时它自己会一直排帧，释放后应该彻底停手 */
const idleRafGrowth = async (ms: number): Promise<number> => {
  const before = raf.calls;
  await sleep(ms);
  return raf.calls - before;
};
const waitFor = async (
  predicate: () => boolean,
  maxFrames = 400,
  step = 5,
): Promise<boolean> => {
  for (let i = 0; i < maxFrames; i += step) {
    await advance(step);
    if (predicate()) return true;
  }
  return predicate();
};

function seedSession(): void {
  sessionStorage.setItem("kb_access_token", "probe-token");
  sessionStorage.setItem("kb_expires_at", String(Date.now() + 3_600_000));
  sessionStorage.setItem("kb_device", JSON.stringify({ id: "dev-1", room_id: "room-1", elder_id: "elder-1" }));
}

const nativeFetch = window.fetch.bind(window);
function stubConfig(config: Json): void {
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url.includes("/api/pad/avatar-config")) {
      return new Response(JSON.stringify(config), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return nativeFetch(input as RequestInfo, init);
  };
}

const CONFIG = (avatarUrl: string): Json => ({
  avatar_url: avatarUrl,
  fallback_image_url: "/avatars/fallback.png",
  rotation_y_offset: 0,
  version: 1,
});

function domState(): Json {
  const canvas = document.querySelector('canvas[aria-label="数字人"]') as HTMLCanvasElement | null;
  const img = document.querySelector('img[alt="数字人头像"]') as HTMLImageElement | null;
  return {
    canvasOpacity: canvas ? getComputedStyle(canvas).opacity : null,
    canvasBuffer: canvas ? `${canvas.width}x${canvas.height}` : null,
    imgSrc: img ? img.getAttribute("src") : null,
    imgComplete: img ? img.complete : null,
    imgNatural: img && img.naturalWidth ? `${img.naturalWidth}x${img.naturalHeight}` : null,
    rootText: (document.getElementById("root")?.textContent ?? "").trim().slice(0, 60),
  };
}

const root = createRoot(document.getElementById("root") as HTMLElement);
const diags: Json[] = [];

function log(message: string): void {
  const el = document.getElementById("log");
  if (el) el.textContent = `${el.textContent ?? ""}${message}\n`;
}

// ———————————————— 各场景 ————————————————

async function caseBad(): Promise<void> {
  stubConfig(CONFIG("/avatars/__missing__.vrm"));
  const startedAt = performance.now();
  root.render(createElement(AvatarStage, { onDiag: (d: Json) => diags.push(d as Json) }));
  const shown = await waitFor(() => {
    const img = document.querySelector('img[alt="数字人头像"]') as HTMLImageElement | null;
    return Boolean(img?.complete && img.naturalWidth > 0);
  });
  out.elapsedMs = Math.round(performance.now() - startedAt);
  out.fallbackShown = shown;
  out.dom = domState();
  out.diags = diags;
  out.within5s = (out.elapsedMs as number) <= 5000;
}

async function caseTimeout(): Promise<void> {
  stubConfig(CONFIG("/avatars/vrm1_sample.vrm"));
  const startedAt = performance.now();
  // headless 下 rAF 会饿（帧驱采样测出来的时长不可信），所以降级时刻一律用
  // MutationObserver + 元素事件来记，它们是微任务/事件驱动的，不受帧率影响。
  let imgAt = 0;
  const observer = new MutationObserver(() => {
    if (imgAt === 0 && document.querySelector('img[alt="数字人头像"]')) {
      imgAt = Math.round(performance.now() - startedAt);
    }
  });
  observer.observe(document.getElementById("root") as HTMLElement, { childList: true, subtree: true });
  root.render(
    createElement(AvatarStage, {
      onDiag: (d: Json) => {
        if (out.elapsedToDiagMs === undefined) out.elapsedToDiagMs = Math.round(performance.now() - startedAt);
        if (out.disposedAtFailure === undefined) out.disposedAtFailure = { ...disposed };
        diags.push(d as Json);
      },
    }),
  );
  // 资产在 ~6.5s 才到（Playwright 侧延迟）：这时降级早就该发生完了
  await sleep(8000);
  observer.disconnect();
  const img = document.querySelector('img[alt="数字人头像"]') as HTMLImageElement | null;
  out.elapsedToImgElementMs = imgAt;
  out.fallbackShown = Boolean(img);
  out.imgDecoded = Boolean(img?.complete && img.naturalWidth > 0);
  out.imgNatural = img ? `${img.naturalWidth}x${img.naturalHeight}` : null;
  out.diagsBeforeLate = diags.slice();
  // 迟到的那棵树若被 deepDispose，dispose 计数会从「失败时刻的快照」继续增长
  out.disposedAfterLate = { ...disposed };
  const before = out.disposedAtFailure as typeof disposed;
  out.lateReleaseObserved = disposed.geometry > before.geometry || disposed.texture > before.texture;
  out.memoryAfterLate = null; // 超时路径没有渲染器；释放证据看 dispose 计数
  out.dom = domState();
}

async function caseLost(): Promise<void> {
  stubConfig(CONFIG("/avatars/vrm1_sample.vrm"));
  root.render(createElement(AvatarStage, { onDiag: (d: Json) => diags.push(d as Json) }));
  // React 19 的 render 是异步提交：先推两帧让 DOM 落地，再抓 canvas
  await advance(2);
  const canvas = document.querySelector('canvas[aria-label="数字人"]') as HTMLCanvasElement | null;
  if (!canvas) {
    out.error = "canvas 未挂载";
    return;
  }
  const shown = await waitFor(() => getComputedStyle(canvas).opacity === "1", 900, 5);
  out.firstFrameShown = shown;
  out.diagAfterFirstFrame = diags.slice();
  out.domBeforeLoss = domState();

  const gl = (canvas.getContext("webgl2") ?? canvas.getContext("webgl")) as WebGLRenderingContext;
  const ext = gl.getExtension("WEBGL_lose_context");
  if (!ext) {
    out.error = "WEBGL_lose_context 不可用";
    return;
  }
  ext.loseContext();
  await advance(10);
  out.diagAfterLoss = diags.slice();
  out.domAfterLoss = domState();
  out.fellBackOnLoss = (out.domAfterLoss as Json).imgSrc != null;

  ext.restoreContext();
  await advance(20);
  out.domAfterRestore = domState();
  out.diagAfterRestore = diags.slice();
  out.backToCanvas = (out.domAfterRestore as Json).canvasOpacity === "1";
}

async function caseChunk(): Promise<void> {
  const Boom = lazy(() =>
    Promise.reject(new Error("Failed to fetch dynamically imported module: https://example.invalid/AvatarStage-xxx.js")),
  );
  root.render(
    createElement(
      AvatarBoundary,
      null,
      createElement(Suspense, { fallback: createElement("div", null, "chunk 下载中") }, createElement(Boom)),
    ),
  );
  // React 发现 lazy 失败后要再走一轮同步错误的提交，用定时器轮询即可（不必依赖 rAF）
  const startedAt = performance.now();
  let shown = false;
  for (let i = 0; i < 100 && !shown; i += 1) {
    await sleep(50);
    shown = Boolean(document.querySelector('img[alt="数字人头像"]'));
  }
  out.msToStaticFallback = Math.round(performance.now() - startedAt);
  out.dom = domState();
  out.staticFallbackShown = shown;
  // 「没白屏」的判据要看 DOM 有没有内容（img 没有文字，textContent 为空不代表白屏）
  out.pageNotBlank = (document.getElementById("root")?.innerHTML ?? "").trim().length > 0;
}

async function caseHandle(): Promise<void> {
  const holder = document.createElement("div");
  holder.style.cssText = "width:420px;height:420px";
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "width:100%;height:100%";
  holder.appendChild(canvas);
  document.getElementById("root")?.appendChild(holder);

  let firstFrames = 0;
  let lostCount = 0;
  let restoredCount = 0;
  const startedAt = performance.now();
  const handle = await createAvatar(canvas, {
    avatarUrl: "/avatars/vrm1_sample.vrm",
    timeoutMs: 15000,
    onFirstFrame: () => {
      firstFrames += 1;
    },
    onContextLost: () => {
      lostCount += 1;
    },
    onContextRestored: () => {
      restoredCount += 1;
    },
  });
  const loaded = await waitFor(() => firstFrames > 0, 900, 5);
  out.loaded = loaded;
  out.firstFrameMs = Math.round(performance.now() - startedAt);
  // 生产模块自带的诊断出口：renderer.info.memory 的两个计数 + 上下文状态
  out.statsAfterFirstFrame = handle.getStats();

  // ① 活着时：驱动若干帧，renderer.render 应被逐帧调用（帧循环真的在跑）
  const drawsBefore = draws.calls;
  const alive = await driveFrames(8, 4000);
  out.drawsWhileAlive = draws.calls - drawsBefore;
  out.framesDrivenAlive = alive.fired;

  handle.dispose();

  // ② 释放后：同一套驱动下 render 应一次都不再发生，且内存计数归零、上下文真的丢了
  out.statsAfterDispose = handle.getStats();
  out.stateAfterDispose = handle.getState();
  const drawsAfterDraw = draws.calls;
  const dead = await driveFrames(8, 4000);
  out.drawsAfterDispose = draws.calls - drawsAfterDraw;
  out.framesDrivenAfterDispose = dead.fired;
  out.rafIdleGrowthAfterDispose = await idleRafGrowth(300);
  out.noRafResidue = out.drawsAfterDispose === 0 && (out.drawsWhileAlive as number) > 0;
}

// ———————————————— main ————————————————

async function main(): Promise<void> {
  seedSession();
  const table: Record<string, () => Promise<void>> = {
    bad: caseBad,
    timeout: caseTimeout,
    lost: caseLost,
    chunk: caseChunk,
    handle: caseHandle,
  };
  try {
    await table[CASE]();
  } catch (error) {
    out.error = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  }
  out.finishedAt = Date.now();
  log(JSON.stringify(out, null, 2));
  document.body.dataset.ready = "1";
}

void main();
