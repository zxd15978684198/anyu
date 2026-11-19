/**
 * 外呼自动接听的契约用例：N62e（task-44）。只写测试，不改 `realtime.ts`。
 *
 * ## 为什么要这组用例
 *
 * PRD:256 FR-UI-10（P0）写的是「服务端主动发起外呼时 pad **自动接听**建连，数字人主动
 * 开口播报」，PRD:336 FR-MED-06（P0）也写「触达以 pad **接听建连** + 卡片回执为准」。
 * N62b 把待机外呼做成了「响铃 + 长者按接听」——用药提醒 T0 长者不按，这通就只是响一声
 * 到超时，P0 触达事实不成立。N62d 补自动接听，本文件把它的语义钉死：
 *
 * 1. `autoAnswerOutbound=true` → 收到 offer **立刻** answer，零用户点击；
 * 2. 自动接听期间 `getUserMedia` 调用数 == 0、answer 方向 recvonly（FR-UI-11 隐私红线）；
 * 3. 缺省 false → 仍是响铃语义（既有行为不许被悄悄改掉）；
 * 4. 自动接听后唤醒词命中 → `acceptIncomingCall()` 升级带麦，此时 getUM 恰好 1 次；
 * 5. `hangUpOutboundCall()` 后 pc 关闭、`pendingOffer`/计时器不残留。
 *
 * ## 为什么不迁就现状
 *
 * N62d 落地前这些用例**就是红的**——那是「实现还没写完」的信号，不是用例写错。改断言去
 * 迁就当前实现（比如把「自动接听」断言成「必须点接听」）等于把 PRD 的 P0 语义悄悄降级回
 * 来，正是本任务要防的事。
 *
 * ## 假件口径（与 audit-reverse 对齐过）
 *
 * - 自动接听的 recvonly **不靠 answer SDP 文本证明**：那串 SDP 是假 pc 自己写的，断言
 *   「里面有 a=recvonly」等于断言自己的常量，是伪证据。真正的观测面是：`pc.addedTracks`
 *   为 0、`getUserMedia` 调用 0 次——没有本地轨，浏览器协商出来的 answer 必然只能收。
 *   文件里仍保留一条**否定式**断言（不许出现 `a=sendrecv` / `a=sendonly`），用来抓
 *   「实现偷偷加轨却忘了改方向」这条破防；真 SDP 方向要真浏览器 e2e，不在本卡范围。
 * - `getUserMedia` 用计数器包住，不关心拿到的流长什么样；只数次数。
 * - 走真实分派入口 `handleSignal(parseServerSignal(...))`，不直接调私有方法——否则删掉
 *   `case "offer"` 的分支时用例仍然绿，就失去意义了。
 * - 待机态必须先 `await client.setAwake(false)`：`mediaEnabled` 默认 true，唤醒态收到
 *   offer 走的是「本来就能带麦」的 `acceptServerOffer`，到不了自动接听分支。
 *
 * 复跑：`node --import ./scripts/node-ts-hook.mjs --test frontend/tests/outbound-autoanswer.test.ts`
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { PadStatus } from "../src/pad/types";
import type { ServerSignal } from "../src/pad/protocol";
import { parseServerSignal } from "../src/pad/protocol";
import { RealtimeClient, type RealtimeOptions } from "../src/pad/realtime";

const OFFER_SDP = "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n";

/** 全局调用序（跨假实例共用）：断言「先关旧 pc、再建新的」这类顺序。 */
const ops: string[] = [];

/**
 * 假 `RTCPeerConnection`：只实现接听路径用到的面。
 *
 * `createAnswer()` 的方向推导刻意模仿真 WebRTC 的结果：没有本地音轨就没有可发的媒体，
 * answer 必然是 recvonly。这样「实现偷偷 addTrack」会同时弄红 recvonly 断言与 getUM 计数。
 */
class FakeRTCPeerConnection {
  static instances: FakeRTCPeerConnection[] = [];
  static nextId = 1;

  readonly id = FakeRTCPeerConnection.nextId++;
  readonly config: { iceServers?: unknown } | undefined;
  signalingState = "stable";
  localDescription: { type: string; sdp: string } | null = null;
  remoteDescription: { type: string; sdp: string } | null = null;
  ontrack: ((e: unknown) => void) | null = null;
  onicecandidate: ((e: unknown) => void) | null = null;
  closed = false;
  /** 本地音轨：自动接听阶段必须为空（这是隐私红线的直接观测面） */
  readonly addedTracks: unknown[] = [];
  readonly transceivers: Array<{ kind: string; direction?: string }> = [];

  constructor(config?: { iceServers?: unknown }) {
    this.config = config;
    ops.push(`new:${this.id}`);
    FakeRTCPeerConnection.instances.push(this);
  }

  addTrack(track: unknown): void {
    ops.push(`addTrack:${this.id}`);
    this.addedTracks.push(track);
  }

  addTransceiver(kind: string, init?: { direction?: string }): { kind: string } {
    ops.push(`addTransceiver:${this.id}:${kind}:${init?.direction ?? ""}`);
    const transceiver = { kind, direction: init?.direction };
    this.transceivers.push(transceiver);
    return transceiver;
  }

  async setRemoteDescription(d: { type: string; sdp: string }): Promise<void> {
    ops.push(`setRemoteDescription:${this.id}:${d.type}`);
    this.remoteDescription = d;
  }

  async createAnswer(): Promise<{ type: string; sdp: string }> {
    // 有本地轨才可能 sendrecv；没有就只能收（与浏览器协商结果同口径）
    const direction = this.addedTracks.length > 0 ? "sendrecv" : "recvonly";
    ops.push(`createAnswer:${this.id}:${direction}`);
    return {
      type: "answer",
      sdp:
        `v=0\r\no=- 0 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n` +
        `m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:0\r\na=${direction}\r\n`,
    };
  }

  async setLocalDescription(d: { type: string; sdp: string }): Promise<void> {
    ops.push(`setLocalDescription:${this.id}`);
    this.localDescription = d;
  }

  static reset(): void {
    // 注意：不清实例本身，只重置「下次从 1 开始编号」的计数与列表，避免上个用例的引用泄漏
    for (const pc of FakeRTCPeerConnection.instances) pc.closed = true;
    FakeRTCPeerConnection.instances = [];
    FakeRTCPeerConnection.nextId = 1;
  }

  close(): void {
    ops.push(`close:${this.id}`);
    this.closed = true;
  }
}

/** getUserMedia 计数：自动接听阶段必须为 0，唤醒升级后必须恰好为 1。 */
let getUserMediaCalls = 0;

function makeFakeAudioTrack(): { stop(): void; kind: string } {
  return { kind: "audio", stop: () => {} };
}

function makeFakeMediaStream(): {
  getTracks(): Array<{ stop(): void }>;
  getAudioTracks(): Array<{ kind: string; stop(): void }>;
} {
  const track = makeFakeAudioTrack();
  return {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  };
}

/** 浏览器全局：只塞接听路径真正会碰到的那些面。 */
function installBrowserGlobals(): void {
  const g = globalThis as unknown as Record<string, unknown>;
  g.RTCPeerConnection = FakeRTCPeerConnection;
  g.MediaStream = class FakeMediaStream {
    getAudioTracks(): unknown[] {
      return [];
    }
  };

  g.AudioContext = class FakeAudioContext {
    readonly audioWorklet = { addModule: async (): Promise<void> => {} };
    createMediaStreamSource(): { connect: () => void } {
      return { connect: () => {} };
    }
    async close(): Promise<void> {}
  };
  g.AudioWorkletNode = class FakeAudioWorkletNode {
    readonly port = {
      onmessage: null as ((e: MessageEvent<ArrayBuffer>) => void) | null,
      postMessage: (): void => {},
      close: (): void => {},
    };
    connect(): void {}
    disconnect(): void {}
  };

  const mediaDevices = {
    getUserMedia: async (): Promise<unknown> => {
      getUserMediaCalls += 1;
      return makeFakeMediaStream();
    },
  };
  // Node 自带全局 navigator（只读属性），往它上面挂 mediaDevices；没有时退回造一个
  const nav = (globalThis as unknown as { navigator?: object }).navigator;
  if (nav) {
    Object.defineProperty(nav, "mediaDevices", {
      value: mediaDevices,
      configurable: true,
      writable: true,
    });
  } else {
    (globalThis as unknown as Record<string, unknown>).navigator = { mediaDevices };
  }
}

interface ClientInternals {
  socket: { ready: boolean; send(message: unknown): boolean; stop(): void };
  pc: FakeRTCPeerConnection | null;
  pendingOffer: string | null;
  ringTimer: ReturnType<typeof setTimeout> | null;
  mediaEnabled: boolean;
  handleSignal(msg: ServerSignal): Promise<void>;
  acceptIncomingCall(): Promise<void>;
  hangUpOutboundCall(): void;
}

interface Harness {
  client: RealtimeClient;
  internals: ClientInternals;
  statuses: PadStatus[];
  subtitles: Array<[string, string]>;
  sent: unknown[];
  rings: string[];
  outbound: string[];
}

/**
 * 建一个 client：socket 换成纯记录器，`handleSignal` 仍是真实分派。
 * `onOutboundCall` / `onOutboundCallEnded` 由 N62d 新增；现在传进去只是挂回调，
 * 实现没调用它们时断言自然会失败——这正是本文件要的「红」。
 */
function makeClient(options: RealtimeOptions = {}): Harness {
  const statuses: PadStatus[] = [];
  const subtitles: Array<[string, string]> = [];
  const sent: unknown[] = [];
  const rings: string[] = [];
  const outbound: string[] = [];

  const client = new RealtimeClient(
    {
      onStatus: (status) => statuses.push(status),
      onSubtitle: (text, source) => subtitles.push([text, source]),
      onAuthFail: () => {},
      onNeedManualRetry: () => {},
      audioEl: {} as HTMLAudioElement,
      onIncomingCall: (call) => rings.push(call.sdp),
      onOutboundCall: () => outbound.push("started"),
      onOutboundCallEnded: (reason) => outbound.push(`ended:${reason}`),
    },
    options,
  );

  const internals = client as unknown as ClientInternals;
  // 假 socket 要够 `stop()` 用（真实现里 `stop()` 会 `socket.stop()` 再置空）
  internals.socket = {
    ready: true,
    send: (message: unknown): boolean => {
      sent.push(message);
      return true;
    },
    stop: (): void => {},
  };
  return { client, internals, statuses, subtitles, sent, rings, outbound };
}

function signalingMessages(sent: unknown[]): unknown[] {
  return sent.filter((m) => (m as { type?: unknown }).type !== "client_diag");
}

function sentAnswers(sent: unknown[]): Array<{ type: string; sdp: string }> {
  return signalingMessages(sent).filter(
    (m): m is { type: string; sdp: string } => (m as { type?: unknown }).type === "answer",
  );
}

/** 待机态（awake=false）：媒体关掉、信令留着——外呼 offer 是在这个态到达的。 */
async function standby(client: RealtimeClient): Promise<void> {
  await client.setAwake(false);
}

/** 走真实分派入口把一条服务端 offer 喂进去。 */
async function deliverOffer(internals: ClientInternals): Promise<void> {
  const signal = parseServerSignal(JSON.stringify({ type: "offer", sdp: OFFER_SDP }));
  assert.ok(signal !== null, "parseServerSignal 必须放行服务端下发的 offer（S-D7-3）");
  await internals.handleSignal(signal);
}

/** 每个用例开头统一复位：假件、计数器、调用序。 */
function resetGlobals(): void {
  installBrowserGlobals();
  FakeRTCPeerConnection.reset();
  ops.length = 0;
  getUserMediaCalls = 0;
}

test("用例 1+2：autoAnswerOutbound=true 收 offer 立刻 answer，零点击、零麦克风、recvonly", async () => {
  resetGlobals();
  const { client, internals, sent, rings } = makeClient({
    autoAnswerOutbound: true,
    ringTimeoutMs: 10,
  });

  await standby(client);
  await deliverOffer(internals);

  // 1) 零点击：没有响铃回调（不该再把 P0 触达挂到长者动手上）
  assert.deepEqual(rings, [], "自动接听不许再走响铃语义：onIncomingCall 不应被调用");

  // answer 必须已经发回服务端；方向口径见文件头：只证明「没声明发送」，recvonly 的真值
  // 由「没有本地轨」推出来，不靠断言假 pc 自己写的那串 SDP。
  const answers = sentAnswers(sent);
  assert.equal(answers.length, 1, `必须立刻回一条 answer，实际 ${answers.length} 条`);
  const answer = answers[0];
  assert.doesNotMatch(answer.sdp, /a=sendrecv|a=sendonly/, "自动接听不许声明任何发送方向");

  // 2) 隐私边界：一次 getUserMedia 都没调，pc 上也没有本地音轨
  assert.equal(getUserMediaCalls, 0, "自动接听期间不许开麦（FR-UI-11 隐私红线）");
  const pc = FakeRTCPeerConnection.instances.at(-1);
  assert.ok(pc, "自动接听必须建一条下行 PeerConnection");
  assert.equal(pc.addedTracks.length, 0, "自动接听不许加本地音轨（没有本地轨 → answer 只能 recvonly）");

  // 通话中信号给到上层（App 据此渲染横幅 + 挂断入口）
  assert.ok(
    sentAnswers(sent).length === 1,
    "同一通电话在升级前只发一条 answer",
  );

  await client.stop();
});

test("用例 3：autoAnswerOutbound 缺省 false 时仍是响铃语义（既有行为不许被悄悄改）", async () => {
  resetGlobals();
  const { client, internals, sent, rings } = makeClient(); // 不传 autoAnswerOutbound

  await standby(client);
  await deliverOffer(internals);

  assert.equal(rings.length, 1, "缺省 false 必须响铃：onIncomingCall 应被调用一次");
  assert.deepEqual(sentAnswers(sent), [], "响铃阶段不许回 answer（长者还没确认）");
  assert.equal(
    FakeRTCPeerConnection.instances.length,
    0,
    "响铃阶段不许建 PeerConnection（等着长者接听才建）",
  );
  assert.equal(getUserMediaCalls, 0, "响铃阶段不许开麦");

  await client.stop();
});

test("用例 2b：自动接听不吃响铃超时（不会在两个回调里各说一套）", async () => {
  resetGlobals();
  const { client, internals, outbound } = makeClient({
    autoAnswerOutbound: true,
    ringTimeoutMs: 5,
  });

  await standby(client);
  await deliverOffer(internals);
  await new Promise((resolve) => setTimeout(resolve, 30)); // 越过 ringTimeoutMs

  assert.ok(
    outbound.includes("started"),
    "自动接听必须给上层 onOutboundCall 信号（App 才有挂断入口）",
  );
  assert.ok(
    !outbound.includes("ended:timeout"),
    "自动接听已视为接听，不能再触发响铃超时收尾",
  );
});

test("用例 4：自动接听后唤醒命中 → acceptIncomingCall() 升级带麦，getUserMedia 恰好 1 次", async () => {
  resetGlobals();
  const { client, internals, sent } = makeClient({
    autoAnswerOutbound: true,
    ringTimeoutMs: 10,
  });

  await standby(client);
  await deliverOffer(internals);

  const downlinkPc = FakeRTCPeerConnection.instances.at(-1);
  assert.ok(downlinkPc);
  // 契约：自动接听必须把 offer 留着，否则唤醒后没法升级（S-D7-3 全双工走 acceptIncomingCall）
  assert.equal(internals.pendingOffer, OFFER_SDP, "自动接听后必须保留 pendingOffer 供唤醒升级");

  await internals.acceptIncomingCall();

  assert.equal(getUserMediaCalls, 1, "唤醒升级必须且只能开麦一次");
  assert.equal(downlinkPc.closed, true, "升级必须收掉只收不发的下行 pc");
  const uplinkPc = FakeRTCPeerConnection.instances.at(-1);
  assert.ok(uplinkPc && uplinkPc !== downlinkPc, "升级必须换一条新 pc（重协商）");
  assert.equal(uplinkPc.addedTracks.length, 1, "升级后 pc 必须带本地音轨（全双工）");
  assert.equal(sentAnswers(sent).length, 2, "升级后要向服务端再发一条 answer");

  await client.stop();
});

test("用例 5：hangUpOutboundCall() 后 pc closed、pendingOffer/计时器不残留、不碰麦克风", async () => {
  resetGlobals();
  const { client, internals, outbound } = makeClient({
    autoAnswerOutbound: true,
    ringTimeoutMs: 1000,
  });

  await standby(client);
  await deliverOffer(internals);

  const pc = FakeRTCPeerConnection.instances.at(-1);
  assert.ok(pc);

  internals.hangUpOutboundCall();

  assert.equal(pc.closed, true, "挂断必须关闭 pc，不能留在后台继续收流");
  assert.equal(internals.pc, null, "挂断后 client.pc 必须清空（否则下一通电话会往旧 pc 上挂）");
  assert.equal(internals.pendingOffer, null, "挂断后不许再留着 offer 被后续唤醒复活");
  assert.equal(internals.ringTimer, null, "挂断必须清掉响铃计时器");
  assert.ok(outbound.includes("ended:hung_up"), "挂断要把结束原因上报给上层（收起横幅）");
  assert.equal(getUserMediaCalls, 0, "挂断路径不许开麦（没说话就不该开）");

  await client.stop();
});
