/**
 * 迭代 4 取证脚本（私有 Chromium，不依赖仓库依赖）
 *
 * 为什么不用 Playwright MCP：共享 MCP 浏览器的 context 会被其它 agent 关掉/顶号
 * （backend 日志出现 ws_superseded same_device_reconnect 风暴），无法稳定取证。
 * 这里复用本机已下载的 ms-playwright chromium 与本地 node_modules 里的 playwright-core。
 *
 * 用法（node 20+）：
 *   node spikes/verify/browser_probe.cjs first-frame
 *   node spikes/verify/browser_probe.cjs identity
 *   node spikes/verify/browser_probe.cjs fallback
 * 输出：stdout 一行 JSON（含毫秒数、console/ws 摘要、截图路径）。
 */
const fs = require("fs");
const path = require("path");

// 依赖与浏览器路径都可覆盖：默认从本机 node_modules / 用户缓存里找
const PW_CORE =
  process.env.PW_CORE ||
  (() => {
    try {
      return path.dirname(require.resolve("playwright-core/package.json"));
    } catch {
      return "playwright-core";
    }
  })();
const CHROME =
  process.env.CHROME_PATH ||
  path.join(
    process.env.LOCALAPPDATA || path.join(process.env.HOME || "", ".cache"),
    "ms-playwright",
    "chromium-1248",
    "chrome-win64",
    "chrome.exe",
  );
const BASE = "http://127.0.0.1:5173/index.html";
const DEVICE_ID = "11111111-1111-4111-8111-111111111106";
const DEVICE_SECRET = "demo-device-secret-0001";  // pragma: allowlist secret
const OUT_DIR = path.resolve(__dirname);

const { chromium } = require(PW_CORE);

const step = process.argv[2] || "first-frame";
const shot = (name) => path.join(OUT_DIR, name);

function attachRecorders(page, rec) {
  page.on("console", (m) => rec.console.push({ t: Date.now(), type: m.type(), text: m.text() }));
  page.on("pageerror", (e) => rec.pageErrors.push({ t: Date.now(), text: String(e).slice(0, 300) }));
  page.on("websocket", (ws) => {
    rec.ws.push({ t: Date.now(), kind: "open", url: ws.url() });
    ws.on("framesent", (f) => rec.ws.push({ t: Date.now(), kind: "sent", payload: String(f.payload).slice(0, 400) }));
    ws.on("framereceived", (f) => rec.ws.push({ t: Date.now(), kind: "recv", payload: String(f.payload).slice(0, 400) }));
    ws.on("close", () => rec.ws.push({ t: Date.now(), kind: "close", url: ws.url() }));
  });
}

async function login(page) {
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  const needLogin = await page.getByRole("button", { name: "登录" }).count();
  if (needLogin > 0) {
    await page.getByRole("textbox", { name: "设备编号" }).fill(DEVICE_ID);
    await page.getByRole("textbox", { name: "设备密钥" }).fill(DEVICE_SECRET);
  }
  return needLogin > 0;
}

async function dismissAskIfPresent(page) {
  // 身份确认层是 absolute inset-0 覆盖层，不收起的话截图只能拍到它，
  // 拍不到底下的数字人/降级图，所以取证截图前要先点「是我」把层收起。
  const ask = page.getByText("请问是哪位在说话？");
  if ((await ask.count()) === 0) return { dismissed: false, ms: 0 };
  const t = Date.now();
  await page.getByRole("button", { name: "是我" }).click();
  try {
    await page.waitForFunction(() => !document.body.innerText.includes("请问是哪位在说话？"), null, { timeout: 12000, polling: 50 });
  } catch (e) {
    /* 收不起来也照样截图，结果里 dismissed=false */
  }
  return { dismissed: true, ms: Date.now() - t };
}

async function main() {
  if (!fs.existsSync(CHROME)) throw new Error("chromium not found: " + CHROME);
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ["--enable-unsafe-swiftshader", "--use-angle=swiftshader", "--disable-dev-shm-usage"],
  });
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1, locale: "zh-CN" });
  const page = await ctx.newPage();
  const rec = { console: [], ws: [], pageErrors: [] };
  attachRecorders(page, rec);

  const result = { step, ok: false };

  if (step === "first-frame") {
    const t0 = Date.now();
    const didLogin = await login(page);
    const tFillDone = Date.now();
    if (didLogin) {
      await page.getByRole("button", { name: "登录" }).click();
    }
    const tClick = Date.now();
    const canvas = page.locator('canvas[aria-label="数字人"]');
    await canvas.waitFor({ state: "attached", timeout: 20000 });
    const tAttached = Date.now();
    await page.waitForFunction(
      () => {
        const c = document.querySelector('canvas[aria-label="数字人"]');
        return !!c && getComputedStyle(c).opacity === "1";
      },
      null,
      { timeout: 20000, polling: 20 }
    );
    const tVisible = Date.now();
    await page.waitForTimeout(300);
    await page.screenshot({ path: shot("01-first-frame-overlay.png") });
    const askDismiss = await dismissAskIfPresent(page);
    await page.waitForTimeout(300);
    await page.screenshot({ path: shot("01b-first-frame.png") });
    const firstFrameLog = rec.console.map((c) => c.text).filter((t) => t.includes("first frame"));
    const vrmLoadLog = rec.console.map((c) => c.text).filter((t) => t.includes("[vrm_load]"));
    const hud = await page.evaluate(() => {
      const c = document.querySelector('canvas[aria-label="数字人"]');
      const r = c.getBoundingClientRect();
      return {
        canvas: { w: c.width, h: c.height, css: [Math.round(r.width), Math.round(r.height)], opacity: getComputedStyle(c).opacity },
        fallbackImg: (() => {
          const i = document.querySelector('img[alt="数字人头像"]');
          return i ? { src: i.getAttribute("src"), naturalWidth: i.naturalWidth } : null;
        })(),
        webgl: !!document.createElement("canvas").getContext("webgl2"),
        overlayAsk: document.body.innerText.includes("请问是哪位在说话？"),
        text: document.body.innerText.replace(/\s+/g, " ").slice(0, 120),
      };
    });
    Object.assign(result, {
      ok: hud.canvas.opacity === "1",
      loginFlowMs: tClick - t0,
      fillMs: tFillDone - t0,
      canvasAttachedMs: tAttached - tClick,
      canvasVisibleMs: tVisible - tClick,
      firstFrameLog,
      vrmLoadLog,
      hud,
      consoleErrors: rec.console.filter((c) => c.type === "error").map((c) => c.text.slice(0, 200)),
      wsRecv: rec.ws.filter((w) => w.kind === "recv").map((w) => ({ dt: w.t - tClick, p: w.payload.slice(0, 120) })),
      screenshot: shot("01-first-frame-overlay.png"),
    });
  }

  if (step === "identity") {
    const t0 = Date.now();
    const didLogin = await login(page);
    if (didLogin) await page.getByRole("button", { name: "登录" }).click();
    const tClick = Date.now();
    await page.getByText("请问是哪位在说话？").waitFor({ state: "visible", timeout: 25000 });
    const tAsk = Date.now();
    await page.screenshot({ path: shot("02-identity-ask.png") });
    const askFrames = rec.ws.filter((w) => w.kind === "recv" && w.payload.includes("identity_ask"));
    const tConfirmClick = Date.now();
    await page.getByRole("button", { name: "是我" }).click();
    let confirmOk = false;
    try {
      await page.waitForFunction(() => !document.body.innerText.includes("请问是哪位在说话？"), null, { timeout: 12000, polling: 50 });
      confirmOk = true;
    } catch (e) {
      confirmOk = false;
    }
    const tOverlayClosed = Date.now();
    await page.waitForTimeout(600);
    await page.screenshot({ path: shot("03-after-confirm.png") });
    const identityResult = rec.ws.filter((w) => w.kind === "recv" && w.payload.includes("identity_result"));
    const confirmSent = rec.ws.filter((w) => w.kind === "sent" && w.payload.includes("identity_confirm"));
    Object.assign(result, {
      ok: confirmOk && identityResult.length > 0,
      loginFlowMs: tClick - t0,
      askVisibleMs: tAsk - tClick,
      askFrame: askFrames.length ? askFrames[0].payload : null,
      confirmClickToOverlayClosedMs: tOverlayClosed - tConfirmClick,
      identityResultFrame: identityResult.length ? identityResult[0].payload : null,
      identityConfirmSent: confirmSent.length ? confirmSent[0].payload : null,
      sentTypes: rec.ws.filter((w) => w.kind === "sent").map((w) => {
        try { return { dt: w.t - tClick, type: JSON.parse(w.payload).type }; } catch (e) { return { dt: w.t - tClick, type: "raw" }; }
      }),
      recvTypes: rec.ws.filter((w) => w.kind === "recv").map((w) => {
        try { return { dt: w.t - tClick, type: JSON.parse(w.payload).type }; } catch (e) { return { dt: w.t - tClick, type: "raw" }; }
      }),
      hud: await page.evaluate(() => ({
        canvasOpacity: (() => { const c = document.querySelector('canvas[aria-label="数字人"]'); return c ? getComputedStyle(c).opacity : "none"; })(),
        fallbackImg: (() => { const i = document.querySelector('img[alt="数字人头像"]'); return i ? { src: i.getAttribute("src"), naturalWidth: i.naturalWidth } : null; })(),
        text: document.body.innerText.replace(/\s+/g, " ").slice(0, 160),
      })),
      consoleErrors: rec.console.filter((c) => c.type === "error").map((c) => c.text.slice(0, 200)),
      pageErrors: rec.pageErrors,
      screenshots: [shot("02-identity-ask.png"), shot("03-after-confirm.png")],
    });
  }

  if (step === "fallback") {
    const didLogin = await login(page);
    const tClick = Date.now();
    if (didLogin) await page.getByRole("button", { name: "登录" }).click();
    const img = page.locator('img[alt="数字人头像"]');
    await img.waitFor({ state: "visible", timeout: 30000 });
    let loaded = false;
    try {
      await page.waitForFunction(
        () => {
          const i = document.querySelector('img[alt="数字人头像"]');
          return !!i && i.naturalWidth > 0;
        },
        null,
        { timeout: 12000, polling: 50 }
      );
      loaded = true;
    } catch (e) {
      loaded = false;
    }
    const tFallback = Date.now();
    await page.screenshot({ path: shot("04-fallback.png") });
    const askDismiss = await dismissAskIfPresent(page);
    // 注意：fallback.png 这张占位图自带的文案就是「安语 / Anyu · 数字人形象加载中」，
    // 所以别用「找不到加载中字样」判断它已经画出来，只看 <img> 真的解码完成（naturalWidth>0）。
    await img.waitFor({ state: "visible", timeout: 20000 }).catch(() => {});
    await page
      .waitForFunction(
        () => {
          const i = document.querySelector('img[alt="数字人头像"]');
          return !!i && i.naturalWidth > 0;
        },
        null,
        { timeout: 20000, polling: 50 }
      )
      .catch(() => {});
    await page.waitForTimeout(300);
    await page.screenshot({ path: shot("04b-fallback-visible.png") });
    const vrmLoadLog = rec.console.map((c) => c.text).filter((t) => t.includes("[vrm_load]"));
    const hud = await page.evaluate(() => {
      const i = document.querySelector('img[alt="数字人头像"]');
      const c = document.querySelector('canvas[aria-label="数字人"]');
      return {
        fallback: i ? { src: i.getAttribute("src"), naturalWidth: i.naturalWidth, naturalHeight: i.naturalHeight } : null,
        canvasOpacity: c ? getComputedStyle(c).opacity : "none",
        bodyText: document.body.innerText.replace(/\s+/g, " ").slice(0, 200),
        blank: document.body.innerText.trim().length === 0,
      };
    });
    Object.assign(result, {
      ok: hud.fallback !== null && hud.fallback.naturalWidth > 0,
      fallbackVisibleMs: tFallback - tClick,
      fallbackImageLoaded: loaded,
      vrmLoadLog,
      hud,
      consoleErrors: rec.console.filter((c) => c.type === "error").map((c) => c.text.slice(0, 200)),
      pageErrors: rec.pageErrors,
      uncaught: rec.pageErrors.length,
      askDismiss,
      screenshot: shot("04-fallback.png"),
    });
  }

  if (step === "hang") {
    // 验证「资源挂住不返回 → 5s 超时降级」这条路径：vrm 请求永不响应。
    await page.route("**/avatars/*.vrm", () => {
      /* 故意不 fulfill / 不 continue：请求挂在 pending，只能在客户端超时 */
    });
    const didLogin = await login(page);
    const tClick = Date.now();
    if (didLogin) await page.getByRole("button", { name: "登录" }).click();
    const img = page.locator('img[alt="数字人头像"]');
    await img.waitFor({ state: "visible", timeout: 30000 });
    let loaded = false;
    try {
      await page.waitForFunction(
        () => {
          const i = document.querySelector('img[alt="数字人头像"]');
          return !!i && i.naturalWidth > 0;
        },
        null,
        { timeout: 20000, polling: 50 }
      );
      loaded = true;
    } catch (e) {
      loaded = false;
    }
    const tFallback = Date.now();
    await page.screenshot({ path: shot("05-fallback-timeout.png") });
    const askDismiss = await dismissAskIfPresent(page);
    await img.waitFor({ state: "visible", timeout: 20000 }).catch(() => {});
    await page
      .waitForFunction(
        () => {
          const i = document.querySelector('img[alt="数字人头像"]');
          return !!i && i.naturalWidth > 0;
        },
        null,
        { timeout: 20000, polling: 50 }
      )
      .catch(() => {});
    await page.waitForTimeout(300);
    await page.screenshot({ path: shot("05b-fallback-timeout-visible.png") });
    const hud = await page.evaluate(() => {
      const i = document.querySelector('img[alt="数字人头像"]');
      const c = document.querySelector('canvas[aria-label="数字人"]');
      return {
        fallback: i ? { src: i.getAttribute("src"), naturalWidth: i.naturalWidth, naturalHeight: i.naturalHeight } : null,
        canvasOpacity: c ? getComputedStyle(c).opacity : "none",
        blank: document.body.innerText.trim().length === 0,
        text: document.body.innerText.replace(/\s+/g, " ").slice(0, 160),
      };
    });
    Object.assign(result, {
      ok: hud.fallback !== null && hud.fallback.naturalWidth > 0,
      fallbackVisibleMs: tFallback - tClick,
      fallbackImageLoaded: loaded,
      vrmLoadLog: rec.console.map((c) => c.text).filter((t) => t.includes("[vrm_load]")),
      hud,
      pageErrors: rec.pageErrors,
      uncaught: rec.pageErrors.length,
      askDismiss,
      screenshot: shot("05-fallback-timeout.png"),
    });
  }

  if (step === "restored") {
    const didLogin = await login(page);
    const tClick = Date.now();
    if (didLogin) await page.getByRole("button", { name: "登录" }).click();
    const canvas = page.locator('canvas[aria-label="数字人"]');
    await canvas.waitFor({ state: "attached", timeout: 20000 });
    await page.waitForFunction(
      () => {
        const c = document.querySelector('canvas[aria-label="数字人"]');
        return !!c && getComputedStyle(c).opacity === "1";
      },
      null,
      { timeout: 20000, polling: 20 }
    );
    const tVisible = Date.now();
    await page.waitForTimeout(400);
    await page.screenshot({ path: shot("06-restored-overlay.png") });
    const askDismiss = await dismissAskIfPresent(page);
    await page.waitForTimeout(400);
    await page.screenshot({ path: shot("06-restored.png") });
    const hud = await page.evaluate(() => {
      const c = document.querySelector('canvas[aria-label="数字人"]');
      const i = document.querySelector('img[alt="数字人头像"]');
      return {
        canvasOpacity: c ? getComputedStyle(c).opacity : "none",
        canvasSize: c ? [c.width, c.height] : null,
        fallbackImg: i ? { src: i.getAttribute("src"), naturalWidth: i.naturalWidth } : null,
      };
    });
    Object.assign(result, {
      ok: hud.canvasOpacity === "1" && hud.fallbackImg === null,
      canvasVisibleMs: tVisible - tClick,
      vrmLoadLog: rec.console.map((c) => c.text).filter((t) => t.includes("[vrm_load]")),
      hud,
      pageErrors: rec.pageErrors,
      askDismiss,
      screenshot: shot("06-restored.png"),
    });
  }

  await ctx.close();
  await browser.close();
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ step, ok: false, fatal: String(e && e.stack ? e.stack : e).slice(0, 1500) }, null, 2) + "\n");
  process.exit(1);
});
