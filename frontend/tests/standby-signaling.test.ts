/**
 * 待机态常驻信令（N62b · FR-UI-10 护工呼入）的可打红用例。
 *
 * ## 为什么需要它
 *
 * N62b 之前，pad 的信令 WS 只在 `awake` 的 `useEffect` 里建立（`App.tsx:128-129`），
 * 长者睡着时服务端 `signaling.deliver()` 直接返回 False —— 「护工呼入 → 长者端振铃」
 * 没有任何可达路径。改法是把**信令**变成常驻、把**媒体**跟着 awake 起落。
 *
 * 这条改动有两个必须被钉住的契约，且都只有本文件能钉：
 *
 * 1. **常驻**：`setAwake(false)` 之后 `start()` 仍然要建出 WS 并完成 auth。
 *    回归成「醒着才建连」时，下面的 `wsInstances.length === 1` / `ringCalls` 立刻红。
 * 2. **隐私边界**：未接听之前**一次 `getUserMedia` 都不能调**、一个 `RTCPeerConnection`
 *    都不能建（FR-UI-11：未命中唤醒词不上传原始音频）。把媒体建链挪进响铃路径时，
 *    `micCalls.length === 0` 立刻红。
 *
 * ## 为什么用假 WebSocket / 假媒体
 *
 * 契约是「什么时候建、什么时候不建」的时序，不是浏览器实现细节。真 WebSocket 在 Node 里
 * 连不到服务端，真 getUserMedia 也不存在；用记录调用的假实现，断言的是**次数与时序**本身。
 *
 * 复跑（无需额外依赖，Node ≥ 22）：
 *   node --import ./scripts/node-ts-hook.mjs --test frontend/tests/standby-signaling.test.ts
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { parseServerSignal } from "../src/pad/protocol";
import { RealtimeClient } from "../src/pad/realtime";

const OFFER_SDP = "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n";
const ANSWER_SDP = "v=0\r\no=- 7612345678901234567 2 IN IP4 127.0.0.1\r\n";

/* ---------- 观测面：全测试共用的计数与调用序 ---------- */

/** `getUserMedia` 的每次调用（元素为 constraints）—— 隐私契约只看它的长度 */
const micCalls: unknown[] = [];
/** 建过的 RTCPeerConnection（每建一个压一个） */
const pcInstances: FakeRTCPeerConnection[] = [];
/** pc 上的调用序，用来断言「先 setRemoteDescription(offer) 再 createAnswer」 */
const pcOps: string[] = [];
/** 建过的 WebSocket（常驻信令的直接证据） */
const wsInstances: FakeWebSocket[] = [];
/** 每个 ws 发出的原始帧 */
const sentFrames: string[] = [];

/* ---------- 假 WebSocket：SignalSocket 只用 onopen/onmessage/onclose/send/close ---------- */

class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly CLOSED = 3;

  readyState = 0; // CONNECTING
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    wsInstances.push(this);
  }

  send(frame: string): void {
    sentFrames.push(frame);
  }

  close(): void {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code: 1000 });
  }

  /** 测试手动驱动：握手完成 */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  /** 测试手动驱动：服务端下行一帧 */
  receive(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

/* ---------- 假媒体：只实现 pad 侧用到的面 ---------- */

class FakeMediaStreamTrack {
  stopped = false;
  kind = "audio";

  stop(): void {
    this.stopped = true;
  }

  addEventListener(): void {
    /* 无设备插拔模拟 */
  }
}

class FakeMediaStream {
  readonly track = new FakeMediaStreamTrack();

  getAudioTracks(): FakeMediaStreamTrack[] {
    return [this.track];
  }

  getTracks(): FakeMediaStreamTrack[] {
    return [this.track];
  }
}

class FakeRTCPeerConnection {
  signalingState = "stable";
  localDescription: { type: string; sdp: string } | null = null;
  remoteDescription: { type: string; sdp: string } | null = null;
  ontrack: ((e: unknown) => void) | null = null;
  onicecandidate: ((e: unknown) => void) | null = null;
  readonly addedTracks: unknown[] = [];
  /** A1：只下行会话靠转辙器换远端音轨，方向必须记下来（隐私契约就看它） */
  readonly transceivers: Array<{ kind: string; direction?: string }> = [];
  closed = false;

  constructor() {
    pcInstances.push(this);
  }

  addTrack(track: unknown): void {
    this.addedTracks.push(track);
    pcOps.push("addTrack");
  }

  addTransceiver(kind: string, init?: { direction?: string }): unknown {
    this.transceivers.push({ kind, direction: init?.direction });
    pcOps.push(`addTransceiver:${kind}:${init?.direction ?? "sendrecv"}`);
    return {};
  }

  async createOffer(): Promise<{ type: string; sdp: string }> {
    pcOps.push("createOffer");
    return { type: "offer", sdp: OFFER_SDP };
  }

  async setRemoteDescription(d: { type: string; sdp: string }): Promise<void> {
    pcOps.push(`setRemoteDescription:${d.type}`);
    this.remoteDescription = d;
    this.signalingState = "have-remote-offer";
  }

  async createAnswer(): Promise<{ type: string; sdp: string }> {
    pcOps.push("createAnswer");
    return { type: "answer", sdp: ANSWER_SDP };
  }

  async setLocalDescription(d: { type: string; sdp: string }): Promise<void> {
    pcOps.push("setLocalDescription");
    this.localDescription = d;
    this.signalingState = "stable";
  }

  close(): void {
    this.closed = true;
  }
}

/* ---------- 浏览器全局安装 ---------- */

/**
 * `sessionStorage` 桩：Node 24 没有这个全局（Node 26 起才内置内存版 Storage），而
 * `realtime.ts → ice.ts → session.ts` 的 `loadSession()` 第一行就读它。不装桩时
 * `loadIceServers()` 会在待机/接听路径上抛 `ReferenceError`，用例假红在「没建 pc」。
 * 浏览器里它恒存在，所以桩的语义就是「空存储」：`getItem` 一律 null。
 */
function installSessionStorage(g: Record<string, unknown>): void {
  const store = new Map<string, string>();
  Object.defineProperty(g, "sessionStorage", {
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, String(value)),
      removeItem: (key: string) => void store.delete(key),
      clear: () => void store.clear(),
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
    configurable: true,
    writable: true,
  });
}

function installBrowserGlobals(): void {
  const g = globalThis as unknown as Record<string, unknown>;

  installSessionStorage(g);
  g.WebSocket = FakeWebSocket;
  g.RTCPeerConnection = FakeRTCPeerConnection;
  g.MediaStream = FakeMediaStream;
  // 说话窗（口型 rAF）与逐字字幕都靠 rAF 驱动；Node 里没有，给一个最小实现，
  // 否则 tts_text 一进来就 ReferenceError，字幕逻辑根本没被跑到。
  g.requestAnimationFrame = (cb: (t: number) => void) => setTimeout(() => cb(performance.now()), 16);
  g.cancelAnimationFrame = (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>);
  Object.defineProperty(g, "location", {
    value: { protocol: "http:", host: "127.0.0.1:5173" },
    configurable: true,
    writable: true,
  });

  const micStream = new FakeMediaStream();
  Object.defineProperty(g, "navigator", {
    value: {
      mediaDevices: {
        getUserMedia: async (constraints: unknown) => {
          micCalls.push(constraints);
          return micStream;
        },
        addEventListener: () => {},
        removeEventListener: () => {},
      },
    },
    configurable: true,
    writable: true,
  });

  // MicCapture.start() 的三件套：AudioContext + worklet 模块 + 重采样节点
  g.AudioContext = class FakeAudioContext {
    readonly audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() {
      return { connect: () => {} };
    }
    async close() {}
  } as unknown as typeof AudioContext;
  g.AudioWorkletNode = class FakeAudioWorkletNode {
    readonly port = { onmessage: null as unknown, postMessage: () => {}, close: () => {} };
    disconnect(): void {}
  } as unknown as typeof AudioWorkletNode;

  // ICE 配置接口：Node 里没有服务端，直接失败 → loadIceServers 契约上回落 []
  g.fetch = async () => {
    throw new Error("offline");
  };
}

function resetObservations(): void {
  micCalls.length = 0;
  pcInstances.length = 0;
  pcOps.length = 0;
  wsInstances.length = 0;
  sentFrames.length = 0;
}

/** 让挂在微任务/宏任务上的 handleSignal 跑完 */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

interface Harness {
  client: RealtimeClient;
  statuses: string[];
  subtitles: Array<[string, string]>;
  ringCalls: string[];
  ringEndings: string[];
}

function makeClient(ringTimeoutMs?: number): Harness {
  const statuses: string[] = [];
  const subtitles: Array<[string, string]> = [];
  const ringCalls: string[] = [];
  const ringEndings: string[] = [];

  const client = new RealtimeClient(
    {
      onStatus: (status) => statuses.push(status),
      onSubtitle: (text, source) => subtitles.push([text, source]),
      onAuthFail: () => {},
      onNeedManualRetry: () => {},
      onIncomingCall: (call) => ringCalls.push(call.sdp),
      onIncomingCallEnded: (reason) => ringEndings.push(reason),
      audioEl: {
      // tts_text 会 play() 并挂 AnalyserNode：给最小可用壳，避免用例打不到字幕逻辑
      play: () => Promise.resolve(),
      pause: () => {},
      paused: true,
      muted: false,
      currentTime: 0,
      volume: 1,
      addEventListener: () => {},
      removeEventListener: () => {},
    } as unknown as HTMLAudioElement,
    },
    ringTimeoutMs === undefined ? {} : { ringTimeoutMs },
  );

  return { client, statuses, subtitles, ringCalls, ringEndings };
}

/** 睡着启动：先 setAwake(false)，再 start()，然后走完 WS 握手与 auth_ok */
async function bootAsleep(harness: Harness, token = "device-token"): Promise<FakeWebSocket> {
  await harness.client.setAwake(false);
  await harness.client.start(token);
  const ws = wsInstances[0];
  assert.ok(ws, "start() 必须建出信令 WS（常驻信令：与 awake 无关）");
  ws.open();
  ws.receive({ type: "auth_ok" });
  await flush();
  return ws;
}

/** 只取信令类帧：reportDiag 也会经同一 socket 发 client_diag，不是本用例的断言对象 */
function signalingFrames(): Array<Record<string, unknown>> {
  return sentFrames
    .map((raw) => JSON.parse(raw) as Record<string, unknown>)
    .filter((msg) => msg.type !== "client_diag");
}

/* ---------- 用例 ---------- */

test("睡着启动：信令常驻 + 一条只下行会话（不开麦），getUserMedia 0 次", async () => {
  installBrowserGlobals();
  resetObservations();
  const harness = makeClient();

  const ws = await bootAsleep(harness);

  assert.equal(wsInstances.length, 1, "常驻信令：睡着也必须建出一条 WS");
  const frames = signalingFrames();
  assert.deepEqual(frames[0], { type: "auth", token: "device-token" });
  const offers = frames.filter((m) => m.type === "offer");
  assert.equal(offers.length, 1, "A1：待机态必须发一条只下行 offer（否则播报/开场白没有播放轨）");
  assert.equal(offers[0].sdp, OFFER_SDP);
  assert.equal(micCalls.length, 0, "未唤醒/未接听时不许调 getUserMedia（FR-UI-11 隐私边界）");
  assert.equal(pcInstances.length, 1, "A1：待机态建且只建一条 pc");
  const pc = pcInstances[0];
  assert.deepEqual(
    pc.transceivers,
    [{ kind: "audio", direction: "recvonly" }],
    "待机 pc 必须用 recvonly 转辙器换下行音轨",
  );
  assert.equal(pc.addedTracks.length, 0, "待机 pc 不得挂本地音轨（挂上就等于在录音）");

  // 待机态收到护工呼入 → 只响铃，仍然不许碰麦克风
  ws.receive({ type: "offer", sdp: OFFER_SDP });
  await flush();

  assert.deepEqual(harness.ringCalls, [OFFER_SDP], "offer 到达必须触发振铃回调");
  assert.equal(micCalls.length, 0, "响铃期间不许开麦：麦克风只能在长者确认后启动");
  assert.equal(pcInstances.length, 1, "响铃期间不得再建 pc（沿用待机那条只下行会话）");
  assert.deepEqual(
    signalingFrames().filter((m) => m.type === "answer"),
    [],
    "未接听就不该回 answer",
  );

  await harness.client.stop();
});

test("tts_text 先挂起不整段上屏；tts_segments 到了才按句逐字（不许闪回重打）", async () => {
  installBrowserGlobals();
  resetObservations();
  const harness = makeClient();

  const ws = await bootAsleep(harness);
  const full = "您好，我在呢。有什么想聊的，随时跟我说一声。";

  ws.receive({ type: "tts_text", text: full, degraded: false });
  await new Promise((r) => setTimeout(r, 250));

  assert.equal(
    harness.subtitles.filter(([, src]) => src === "reply").length,
    0,
    "文本比音频早到：此时整段上屏会让长者先看到整句、再缩回去重打（实测 21→22→3→9→10）",
  );

  ws.receive({
    type: "tts_segments",
    segments: [
      { text: "您好，我在呢。", start_ms: 0, duration_ms: 900 },
      { text: "有什么想聊的，随时跟我说一声。", start_ms: 900, duration_ms: 1200 },
    ],
  });
  await new Promise((r) => setTimeout(r, 1000)); // 超过兜底 900ms：计划在场就不许整段上屏

  const replies = harness.subtitles.filter(([, src]) => src === "reply");
  assert.ok(
    !replies.some(([text]) => text === full),
    "计划已接手后，兜底定时器不得把整段文本盖回逐字结果上",
  );

  await harness.client.stop();
});

test("4002 同设备接管：停止自动重连并给出提示，避免两端互踢", async () => {
  installBrowserGlobals();
  resetObservations();
  const harness = makeClient();

  const ws = await bootAsleep(harness);
  const before = wsInstances.length;

  // 服务端若让「另一条连接」接管这台设备，就会以 4002 关掉这条
  ws.readyState = FakeWebSocket.CLOSED;
  ws.onclose?.({ code: 4002 });
  await flush();

  assert.equal(
    wsInstances.length,
    before,
    "4002 不得自动重连：两条合法连接会按 1.5s 节奏互相顶掉，开场白跟着反复重播/丢失",
  );
  assert.ok(harness.statuses.includes("disconnected"), "接管后状态必须落到断线");
  assert.ok(
    harness.subtitles.some(([text]) => /已在另一处接管/.test(text)),
    "必须把「这台设备已在别处接管」告诉长者，而不是静默无声",
  );

  await harness.client.stop();
});

test("接听后才建媒体：getUserMedia 1 次 → pc 1 个 → offer 先于 answer，并把 answer 发回信令", async () => {
  installBrowserGlobals();
  resetObservations();
  const harness = makeClient();

  const ws = await bootAsleep(harness);
  ws.receive({ type: "offer", sdp: OFFER_SDP });
  await flush();
  assert.equal(micCalls.length, 0, "接听前必须 0 次采集（本用例的对照基线）");
  assert.equal(pcInstances.length, 1, "接听前只有待机那条只下行会话");
  pcOps.length = 0; // 只看接听那条 pc 的调用序

  await harness.client.acceptIncomingCall();

  assert.equal(micCalls.length, 1, "接听必须且只需开一次麦克风");
  assert.equal(pcInstances.length, 2, "接听必须新开一条带麦 PeerConnection（旧只下行会话被收掉）");
  assert.equal(pcInstances[0].closed, true, "旧只下行会话必须关掉，不能两条并存");

  const pc = pcInstances[1];
  assert.equal(pc.addedTracks.length, 1, "本地音轨必须挂进 pc（否则长者说话护工听不到）");
  assert.deepEqual(
    pcOps,
    ["addTrack", "setRemoteDescription:offer", "createAnswer", "setLocalDescription"],
    "接听顺序：挂本地轨 → 设远端 offer → createAnswer → 设本地 answer",
  );

  const answers = signalingFrames().filter((m) => m.type === "answer");
  assert.deepEqual(answers, [{ type: "answer", sdp: ANSWER_SDP }], "answer 必须带本地 SDP 回发");
  assert.equal(
    harness.statuses[harness.statuses.length - 1],
    "connecting",
    "接听后状态必须推进到 connecting（不是停在 listening/disconnected）",
  );

  await harness.client.stop();
});

test("响铃超时：收铃且不建媒体，超时后再接听也是空操作", async () => {
  installBrowserGlobals();
  resetObservations();
  const harness = makeClient(20);

  const ws = await bootAsleep(harness);
  ws.receive({ type: "offer", sdp: OFFER_SDP });
  await flush();
  assert.deepEqual(harness.ringCalls, [OFFER_SDP]);

  await new Promise((resolve) => setTimeout(resolve, 80));

  assert.deepEqual(harness.ringEndings, ["timeout"], "响铃超时必须收铃并告知上层");
  assert.equal(micCalls.length, 0, "超时不得开麦");
  assert.equal(pcInstances.length, 1, "超时不得再建 pc（只剩待机那条只下行会话）");

  await harness.client.acceptIncomingCall();
  assert.equal(micCalls.length, 0, "已作废的一通电话，接听必须是空操作");
  assert.equal(pcInstances.length, 1);

  await harness.client.stop();
});

test("长者拒接：收铃不建媒体，也不回 answer", async () => {
  installBrowserGlobals();
  resetObservations();
  const harness = makeClient();

  const ws = await bootAsleep(harness);
  ws.receive({ type: "offer", sdp: OFFER_SDP });
  await flush();

  harness.client.declineIncomingCall();
  await flush();

  assert.deepEqual(harness.ringEndings, ["declined"]);
  assert.equal(micCalls.length, 0);
  assert.equal(pcInstances.length, 1, "拒接不建带麦会话（待机那条只下行会话仍在）");
  assert.deepEqual(
    signalingFrames().filter((m) => m.type === "answer"),
    [],
    "拒接后服务端等不到 answer 会自行降级文字播报，pad 不该补发",
  );

  await harness.client.stop();
});

test("协议层放行：待机态 offer 能过 parseServerSignal 白名单（否则根本到不了响铃）", () => {
  const signal = parseServerSignal(JSON.stringify({ type: "offer", sdp: OFFER_SDP }));
  assert.ok(signal !== null, "服务端下发的 offer 必须被白名单放行");
  assert.equal(signal.type, "offer");

  const unknown = parseServerSignal(JSON.stringify({ type: "outbound_offer", sdp: OFFER_SDP }));
  assert.equal(unknown, null, "闭集未被放宽：白名单外的类型仍返回 null");
});

/* ---------- 跨语言口径：端侧铃响 vs 服务端外呼等待 ---------- */

/** 仓库根（本文件在 frontend/tests/ 下） */
const REPO_ROOT = new URL("../../", import.meta.url);

function readRepoFile(relative: string): string {
  return readFileSync(new URL(relative, REPO_ROOT), "utf8");
}

test("口径一致：App 传入的 ringTimeoutMs 与服务端 OUTBOUND_TIMEOUT_SECONDS 对齐（改一边就红）", () => {
  const serverSource = readRepoFile("app/audio/outbound.py");
  const serverMatch = /^OUTBOUND_TIMEOUT_SECONDS[^=\n]*=\s*([\d.]+)/m.exec(serverSource);
  assert.ok(serverMatch, "app/audio/outbound.py 里找不到 OUTBOUND_TIMEOUT_SECONDS（服务端外呼预算）");
  const serverMs = Math.round(Number(serverMatch[1]) * 1000);

  const appSource = readRepoFile("frontend/src/pad/App.tsx");
  const appValues = [...appSource.matchAll(/ringTimeoutMs:\s*([\d_]+)/g)];
  assert.equal(
    appValues.length,
    1,
    "App.tsx 必须显式传且只传一处 ringTimeoutMs（默认 30s 是兜底，不参与外呼口径）",
  );
  const appMs = Number(appValues[0][1].replaceAll("_", ""));

  assert.equal(
    appMs,
    serverMs,
    `铃响超时必须等于服务端外呼等待（服务端 ${serverMs}ms / App ${appMs}ms）：` +
      "服务端先放弃、长者端还在响，就是幽灵振铃",
  );
});
