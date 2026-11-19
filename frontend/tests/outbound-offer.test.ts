/**
 * 端侧接听（迭代 7 · S-D7-3 反向外呼）的可打红用例：N62c。
 *
 * ## 为什么需要它
 *
 * N62 把「服务端当 offerer、pad 当 answerer」这条链路接通了，但端侧 `case "offer"` →
 * `acceptServerOffer` 这段只有 `tsc --noEmit` 过——把应答分支删成空 `break`，现有门禁
 * （pytest / tsc / vite build）**一条都不会红**。这条用例补的就是这个缺口：
 * 删掉应答分支 → 下面的调用序列断言立刻失败。
 *
 * ## 为什么用假 `RTCPeerConnection` 而不是 jsdom
 *
 * 行为契约只有一句：**收到 offer 后，pad 必须按「收旧连接 → 建新 pc → 设远端 offer →
 * createAnswer → 设本地 answer → 把 answer 发回信令」这个顺序做完**。用一个记录调用序的
 * 假实现，断言的是这条顺序本身；换成真 WebRTC 在 Node 里既跑不起来，也只会把断言变成
 * 「某个浏览器实现恰好这样」。
 *
 * ## 放 `frontend/tests/` 而不是 `src/`
 *
 * `frontend/tsconfig.json` 的 `include` 是 `["src"]`、`types` 只有 `["vite/client"]`，
 * 用到 `node:test` / `node:assert` 的 `.ts` 一旦进 `src/`，`pnpm build` 前置的
 * `tsc --noEmit` 会报 `TS2307`。放 tests/ 则完全不进构建产物，但被
 * `scripts/run-frontend-tests.mjs` 的 glob 收录（门禁的一部分）。
 *
 * 复跑（无需装任何依赖，Node ≥ 22）：
 *   node --import ./scripts/node-ts-hook.mjs --test frontend/tests/outbound-offer.test.ts
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { PadStatus } from "../src/pad/types";
import type { ServerSignal } from "../src/pad/protocol";
import { parseServerSignal } from "../src/pad/protocol";
import { RealtimeClient } from "../src/pad/realtime";

const OFFER_SDP = "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n";
const ANSWER_SDP = "v=0\r\no=- 7612345678901234567 2 IN IP4 127.0.0.1\r\n";

/** 全局调用序（跨所有假实例共用，才看得出「先关旧的、后建新的」）。 */
const ops: string[] = [];

/**
 * 假 `RTCPeerConnection`：只实现 `acceptServerOffer` 用到的面，并把每次调用记进 `ops`。
 * `id` 让断言能区分「旧 pc 被 close」与「新 pc 被建」。
 */
class FakeRTCPeerConnection {
  static instances: FakeRTCPeerConnection[] = [];
  static nextId = 1;

  readonly id = FakeRTCPeerConnection.nextId++;
  readonly config: { iceServers?: unknown } | undefined;
  /** 与真实现同名：teardown 依赖 `signalingState` / `ontrack` / `onicecandidate` 字段可置空 */
  signalingState = "stable";
  localDescription: { type: string; sdp: string } | null = null;
  remoteDescription: { type: string; sdp: string } | null = null;
  ontrack: ((e: unknown) => void) | null = null;
  onicecandidate: ((e: unknown) => void) | null = null;
  closed = false;

  constructor(config?: { iceServers?: unknown }) {
    this.config = config;
    ops.push(`new:${this.id}`);
    FakeRTCPeerConnection.instances.push(this);
  }

  async setRemoteDescription(d: { type: string; sdp: string }): Promise<void> {
    ops.push(`setRemoteDescription:${this.id}:${d.type}`);
    this.remoteDescription = d;
  }

  async createAnswer(): Promise<{ type: string; sdp: string }> {
    ops.push(`createAnswer:${this.id}`);
    return { type: "answer", sdp: ANSWER_SDP };
  }

  async setLocalDescription(d: { type: string; sdp: string }): Promise<void> {
    ops.push(`setLocalDescription:${this.id}`);
    this.localDescription = d;
  }

  close(): void {
    ops.push(`close:${this.id}`);
    this.closed = true;
  }

  static reset(): void {
    FakeRTCPeerConnection.instances = [];
    FakeRTCPeerConnection.nextId = 1;
  }
}

/** 假 WebSocket 位置：`acceptServerOffer` 只经 `socket.send` 回发 answer / ice。 */
interface SentMessages {
  readonly sent: unknown[];
  readonly socket: { send(message: unknown): boolean };
}

/** 把浏览器全局塞进 Node：模块已在 import 时求值，这里只影响运行时查找。 */
function installBrowserGlobals(): void {
  const g = globalThis as unknown as Record<string, unknown>;
  g.RTCPeerConnection = FakeRTCPeerConnection;
  // `attachRemoteStream` 只读 `e.streams[0]`，不会 trigger ontrack；给个占位即可
  g.MediaStream = class FakeMediaStream {
    getAudioTracks(): unknown[] {
      return [];
    }
  };
}

interface ClientInternals {
  socket: { send(message: unknown): boolean };
  pc: FakeRTCPeerConnection | null;
  handleSignal(msg: ServerSignal): Promise<void>;
}

function makeClient(): {
  internals: ClientInternals;
  statuses: PadStatus[];
  subtitles: Array<[string, string]>;
  sent: unknown[];
} {
  const statuses: PadStatus[] = [];
  const subtitles: Array<[string, string]> = [];
  const collected: SentMessages = { sent: [], socket: { send: () => true } };
  collected.socket.send = (message: unknown): boolean => {
    collected.sent.push(message);
    return true;
  };

  const client = new RealtimeClient({
    onStatus: (status) => statuses.push(status),
    onSubtitle: (text, source) => subtitles.push([text, source]),
    onAuthFail: () => {},
    onNeedManualRetry: () => {},
    audioEl: {} as HTMLAudioElement,
  });

  // 私有字段：用例走的是 handleSignal（真实分派入口），只把 socket 换成假的
  const internals = client as unknown as ClientInternals;
  internals.socket = collected.socket;
  return { internals, statuses, subtitles, sent: collected.sent };
}

/** 只取信令类消息：`reportDiag` 也会经同一个 socket 发 `client_diag`，不是本用例的断言对象。 */
function signalingMessages(sent: unknown[]): unknown[] {
  return sent.filter((m) => (m as { type?: unknown }).type !== "client_diag");
}

test("收到服务端 offer：关旧 pc → 建新 pc → setRemoteDescription → createAnswer → 回发 answer", async () => {
  installBrowserGlobals();
  FakeRTCPeerConnection.reset();
  ops.length = 0;

  const { internals, statuses, sent } = makeClient();

  // 前置：已有一条（客户端发起的）会话连接，ontrack 已被挂过
  const oldPc = new FakeRTCPeerConnection({ iceServers: [] });
  oldPc.ontrack = () => {};
  internals.pc = oldPc;
  ops.length = 0; // 只记录接听过程

  // 走真实解析层：白名单里没有 "offer" 时这里就会是 null（协议层反向证据）
  const signal = parseServerSignal(JSON.stringify({ type: "offer", sdp: OFFER_SDP }));
  assert.ok(signal !== null, "parseServerSignal 必须放行服务端下发的 offer（S-D7-3）");
  await internals.handleSignal(signal);

  const instances = FakeRTCPeerConnection.instances;
  const newPc = instances[instances.length - 1];
  assert.ok(newPc, "接听必须新建一条 RTCPeerConnection");

  // 顺序就是契约（先断言它：删掉应答分支时，这条会直接列出「该发生的没发生」）：
  // 先收旧连接 → 再建新连接 → 再按 offer→answer 推进
  assert.deepEqual(
    ops,
    [
      `close:${oldPc.id}`,
      `new:${newPc.id}`,
      `setRemoteDescription:${newPc.id}:offer`,
      `createAnswer:${newPc.id}`,
      `setLocalDescription:${newPc.id}`,
    ],
    "收到 offer 必须依次：关旧 pc → 建新 pc → setRemoteDescription(offer) → createAnswer → setLocalDescription",
  );
  assert.notEqual(newPc, oldPc, "必须换新连接，不能在旧连接上硬塞远端 offer");

  // 旧连接不许残留：teardown 必须解绑 ontrack（否则旧轨继续往 <audio> 里灌）
  assert.equal(oldPc.ontrack, null, "旧 pc 的 ontrack 必须在 teardown 里解绑");
  assert.equal(oldPc.closed, true, "旧 pc 必须被 close");
  assert.equal(newPc.closed, false, "新 pc 不许被误关");
  assert.notEqual(newPc.ontrack, null, "新 pc 必须挂上下行轨回调（否则外呼只有字幕没有声音）");

  // answer 必须带着本地 SDP 回发信令，且是 answer 类型
  assert.deepEqual(signalingMessages(sent), [{ type: "answer", sdp: ANSWER_SDP }]);

  // 状态机推进：接听后进入 connecting（由 ICE 完成再转 listening）
  assert.deepEqual(statuses, ["connecting"]);
});

test("协议闭集：offer 被放行，未知类型仍被丢弃（没有放宽成 else 忽略）", () => {
  const offer = parseServerSignal(JSON.stringify({ type: "offer", sdp: OFFER_SDP }));
  assert.equal(offer?.type, "offer");

  const unknown = parseServerSignal(JSON.stringify({ type: "outbound_offer", sdp: OFFER_SDP }));
  assert.equal(unknown, null, "白名单外的类型必须返回 null，不能静默当成合法消息");
});

test("answer 回发失败路径：createAnswer 抛错时提示降级，且不把半成品 answer 发出去", async () => {
  installBrowserGlobals();
  FakeRTCPeerConnection.reset();
  ops.length = 0;

  class BrokenPc extends FakeRTCPeerConnection {
    override async createAnswer(): Promise<{ type: string; sdp: string }> {
      ops.push(`createAnswer:${this.id}:failed`);
      throw new Error("createAnswer boom");
    }
  }
  (globalThis as unknown as Record<string, unknown>).RTCPeerConnection = BrokenPc;

  const { internals, subtitles, sent } = makeClient();
  const signal = parseServerSignal(JSON.stringify({ type: "offer", sdp: OFFER_SDP }));
  assert.ok(signal !== null);
  await internals.handleSignal(signal);

  assert.deepEqual(signalingMessages(sent), [], "answer 没建出来就不能发（半成品 SDP 会让服务端直接拒连）");
  assert.deepEqual(subtitles, [["接听失败，将改用文字播报", "notice"]]);
});
