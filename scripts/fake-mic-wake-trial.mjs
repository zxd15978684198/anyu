/**
 * 唤醒词「走真采集路径」的 20 次自动化试验（T1 补证据用；不新增任何依赖）。
 *
 * 为什么需要它：之前的 20/20 是「WAV 直灌 KWS 引擎」得来的，没经过
 * getUserMedia → AudioContext → AudioWorklet 这条真实链路，AC-42 证据不成立。
 * 这里用 Chrome 的假音频设备把 `wake-xiaoban.wav` 灌进**真的麦克风设备**，
 * 页面代码一行不改（不加测试专属分支），量到的就是产品链路：
 *   fake device → getUserMedia → AudioWorklet 重采样 → kws.handleFrame → 命中回调。
 *
 * 用法：
 *   node scripts/fake-mic-wake-trial.mjs --trials 20
 *   node scripts/fake-mic-wake-trial.mjs --trials 20 --headed --keep-open
 *
 * 结果：控制台 20 行表 + `.tmp/kws-fake-mic/trials.json`（逐次明细）。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const DIST = join(ROOT, "frontend", "dist");
const DEFAULT_WAV = join(ROOT, ".tmp", "voice", "wake-xiaoban.wav");
// Chrome 可执行文件：默认取标准安装位置，换路径用 CHROME_PATH 覆盖
const CHROME =
  process.env.CHROME_PATH ??
  join(process.env.PROGRAMFILES ?? "C:\\Program Files", "Google", "Chrome", "Application", "chrome.exe");
const OUT_DIR = join(ROOT, ".tmp", "kws-fake-mic");

/* ---------------- 参数 ---------------- */

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1) return fallback;
  const next = process.argv[i + 1];
  return next && !next.startsWith("--") ? next : true;
}

const TRIALS = Number(arg("trials", 20));
const PER_TRIAL_TIMEOUT_MS = Number(arg("timeout", 25_000));
const HEADED = arg("headed", false) === true;
const KEEP_OPEN = arg("keep-open", false) === true;
const WAV = resolve(String(arg("wav", DEFAULT_WAV)));
const PAGE = String(arg("page", "/index.html"));

if (!existsSync(DIST)) {
  console.error(`[trial] 找不到 ${DIST}，先跑 pnpm --dir frontend build`);
  process.exit(1);
}
if (!existsSync(WAV)) {
  console.error(`[trial] 找不到假麦克风音频 ${WAV}，先跑 python scripts/make-wake-wav.py`);
  process.exit(1);
}
if (!existsSync(CHROME)) {
  console.error(`[trial] 找不到 Chrome：${CHROME}`);
  process.exit(1);
}

/* ---------------- 静态服务（dist） ---------------- */

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".data": "application/octet-stream",
  ".onnx": "application/octet-stream",
  ".txt": "text/plain; charset=utf-8",
  ".wav": "audio/wav",
  ".png": "image/png",
  ".svg": "image/svg+xml",
};

function startServer() {
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const rel = url.pathname === "/" ? "/index.html" : url.pathname;
    const file = normalize(join(DIST, decodeURIComponent(rel)));
    if (!file.startsWith(DIST)) {
      res.writeHead(403).end("forbidden");
      return;
    }
    try {
      const body = readFileSync(file);
      res.writeHead(200, {
        "content-type": MIME[extname(file)] ?? "application/octet-stream",
        // 12MB 模型包不要落缓存，避免第二次起测读到上一次的半成品
        "cache-control": "no-store",
      });
      res.end(body);
    } catch {
      res.writeHead(404).end("not found");
    }
  });
  return new Promise((ok) => server.listen(0, "127.0.0.1", () => ok(server)));
}

/* ---------------- 极简 CDP 客户端（Node 自带 WebSocket，无依赖） ---------------- */

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { ok, fail } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) fail(new Error(`${msg.error.message} (${JSON.stringify(msg.error.data ?? "")})`));
        else ok(msg.result ?? {});
        return;
      }
      for (const fn of this.listeners) fn(msg);
    });
  }

  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((ok, fail) => {
      ws.addEventListener("open", ok, { once: true });
      ws.addEventListener("error", () => fail(new Error(`WS 连接失败 ${url}`)), { once: true });
    });
    return new Cdp(ws);
  }

  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    return new Promise((ok, fail) => {
      this.pending.set(id, { ok, fail });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }

  on(fn) {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter((f) => f !== fn);
    };
  }

  close() {
    try {
      this.ws.close();
    } catch {
      /* 已关 */
    }
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForFile(path, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (existsSync(path)) return;
    await sleep(100);
  }
  throw new Error(`超时未生成 ${path}`);
}

/* ---------------- 每次试验 ---------------- */

/** 页面内埋点：只读时间戳，不改变产品行为（产品代码里一个测试分支都没有） */
const INSTRUMENT = `(() => {
  const marks = { gumAt: 0, gumWaitMs: 0, listeningAt: 0, hitAt: 0, wokenAt: 0, hitText: "", errors: [] };
  Object.defineProperty(window, "__wakeMarks", { value: marks, configurable: true });
  const md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    const orig = md.getUserMedia.bind(md);
    md.getUserMedia = async (...args) => {
      const t0 = performance.now();
      try {
        const stream = await orig(...args);
        marks.gumAt = performance.now();
        marks.gumWaitMs = Math.round(marks.gumAt - t0);
        return stream;
      } catch (err) {
        marks.errors.push("getUserMedia: " + (err && err.message ? err.message : String(err)));
        throw err;
      }
    };
  }
  const info = console.info.bind(console);
  console.info = (...args) => {
    const text = args.map(String).join(" ");
    if (text.includes("[kws] 状态") && text.includes("→ listening")) marks.listeningAt = performance.now();
    if (text.includes("[kws] 命中")) {
      marks.hitAt = performance.now();
      marks.hitText = text;
    }
    if (text.includes("[kws] 状态") && text.includes("→ woken")) marks.wokenAt = performance.now();
    info(...args);
  };
  const warn = console.warn.bind(console);
  console.warn = (...args) => {
    marks.errors.push(args.map(String).join(" "));
    warn(...args);
  };
})();`;

async function runTrial(cdp, index) {
  const target = await cdp.send("Target.createTarget", { url: "about:blank" });
  const attached = await cdp.send("Target.attachToTarget", { targetId: target.targetId, flatten: true });
  const sessionId = attached.sessionId;
  const netEvents = [];
  const off = cdp.on((msg) => {
    if (msg.sessionId !== sessionId || !msg.method) return;
    if (msg.method === "Network.webSocketCreated" || msg.method === "Network.requestWillBeSent") {
      netEvents.push({ method: msg.method, url: msg.params?.url ?? msg.params?.request?.url ?? "" });
    }
  });

  try {
    await cdp.send("Network.enable", {}, sessionId);
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Runtime.enable", {}, sessionId);
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: INSTRUMENT }, sessionId);

    const t0 = Date.now();
    await cdp.send("Page.navigate", { url: `${baseUrl}${PAGE}` }, sessionId);

    let marks = null;
    while (Date.now() - t0 < PER_TRIAL_TIMEOUT_MS) {
      const res = await cdp
        .send("Runtime.evaluate", { expression: "window.__wakeMarks || null", returnByValue: true }, sessionId)
        .catch(() => ({ result: { value: null } }));
      const value = res.result?.value ?? null;
      if (value && value.hitAt > 0) {
        marks = value;
        break;
      }
      await sleep(120);
    }

    if (!marks) {
      const res = await cdp
        .send("Runtime.evaluate", { expression: "window.__wakeMarks || null", returnByValue: true }, sessionId)
        .catch(() => ({ result: { value: null } }));
      marks = res.result?.value ?? { errors: ["超时且页面无埋点"] };
      const preHitUploads = netEvents.filter(
        (e) => e.method === "Network.webSocketCreated" || /\/audio|upload/i.test(e.url),
      );
      return {
        trial: index,
        hit: false,
        captureToHitMs: null,
        onsetToHitMs: null,
        errors: marks.errors ?? [],
        pageElapsedMs: Date.now() - t0,
        preHitUploads,
      };
    }

    const onset = /开口→命中 (\d+)ms/.exec(marks.hitText);
    // 只统计「命中之前」的网络事件：命中之后建连 WebRTC 是产品设计，不算泄漏
    const preHitUploads = netEvents.filter(
      (e) => e.method === "Network.webSocketCreated" || /\/audio|upload/i.test(e.url),
    );
    return {
      trial: index,
      hit: true,
      captureToHitMs:
        marks.listeningAt > 0 ? Math.round(marks.hitAt - marks.listeningAt) : null,
      gumToHitMs: marks.gumAt > 0 ? Math.round(marks.hitAt - marks.gumAt) : null,
      onsetToHitMs: onset ? Number(onset[1]) : null,
      hitText: marks.hitText,
      preHitUploads,
      errors: marks.errors ?? [],
    };
  } finally {
    off();
    await cdp.send("Target.closeTarget", { targetId: target.targetId }).catch(() => {});
  }
}

/* ---------------- 主流程 ---------------- */

const server = await startServer();
const port = server.address().port;
const baseUrl = `http://127.0.0.1:${port}`;
mkdirSync(OUT_DIR, { recursive: true });
const profileDir = mkdtempSync(join(tmpdir(), "anyu-kws-trial-"));

const args = [
  ...(HEADED ? [] : ["--headless=new"]),
  "--remote-debugging-port=0",
  `--user-data-dir=${profileDir}`,
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-gpu",
  "--disable-features=Translate,MediaRouter",
  "--use-fake-device-for-media-stream",
  "--use-fake-ui-for-media-stream",
  `--use-file-for-fake-audio-capture=${WAV}`,
  "--autoplay-policy=no-user-gesture-required",
  "--window-size=1280,720",
  "about:blank",
];

console.log(`[trial] 服务 ${baseUrl}  音频 ${WAV}`);
console.log(`[trial] Chrome ${HEADED ? "有头" : "headless=new"}  试验 ${TRIALS} 次`);
const chrome = spawn(CHROME, args, { stdio: "ignore" });

let cdp = null;
const results = [];
try {
  await waitForFile(join(profileDir, "DevToolsActivePort"), 15_000);
  const [devtoolsPort] = readFileSync(join(profileDir, "DevToolsActivePort"), "utf8")
    .split("\n")
    .map((s) => s.trim());
  const version = await (await fetch(`http://127.0.0.1:${devtoolsPort}/json/version`)).json();
  cdp = await Cdp.connect(version.webSocketDebuggerUrl);
  console.log(`[trial] Chrome ${version.Browser} 调试端口 ${devtoolsPort}`);

  for (let i = 1; i <= TRIALS; i += 1) {
    const r = await runTrial(cdp, i);
    results.push(r);
    const verdict = r.hit ? "命中" : "未命中";
    const cap = r.captureToHitMs == null ? "-" : `${r.captureToHitMs}ms`;
    const onset = r.onsetToHitMs == null ? "-" : `${r.onsetToHitMs}ms`;
    console.log(
      `[trial] #${String(i).padStart(2, "0")} ${verdict}  采集→命中 ${cap}  开口→命中 ${onset}` +
        `  命中前上行 ${r.preHitUploads.length}`,
    );
  }
} finally {
  if (!KEEP_OPEN) {
    cdp?.close();
    chrome.kill();
  }
  server.close();
}

const hits = results.filter((r) => r.hit);
const captureLats = hits.map((r) => r.captureToHitMs).filter((v) => v != null).sort((a, b) => a - b);
const onsetLats = hits.map((r) => r.onsetToHitMs).filter((v) => v != null).sort((a, b) => a - b);
const median = (arr) => (arr.length === 0 ? null : arr[Math.floor(arr.length / 2)]);
const uploadLeaks = results.filter((r) => (r.preHitUploads?.length ?? 0) > 0);

const summary = {
  trials: results.length,
  hits: hits.length,
  hitRate: `${hits.length}/${results.length}`,
  captureToHit: { medianMs: median(captureLats), maxMs: captureLats.at(-1) ?? null },
  onsetToHit: { medianMs: median(onsetLats), maxMs: onsetLats.at(-1) ?? null },
  preHitSignal: uploadLeaks.length === 0 ? "0 次试验命中前有 WS/音频上行" : `${uploadLeaks.length} 次有上行`,
  within1500ms: hits.filter((r) => (r.onsetToHitMs ?? Infinity) <= 1500).length,
};

writeFileSync(join(OUT_DIR, "trials.json"), JSON.stringify({ summary, results }, null, 2), "utf8");
console.log("\n[trial] 汇总");
console.log(JSON.stringify(summary, null, 2));
console.log(`[trial] 明细 ${join(OUT_DIR, "trials.json")}`);
