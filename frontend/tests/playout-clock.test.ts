/**
 * 播放侧唯一时钟（`audio/playout-clock-worklet.js` + `realtime.ts` 接线）的守卫用例。
 *
 * 分两层：
 * 1. **真跑 worklet**：把 processor 类在 Node 里实例化（注入 AudioWorkletProcessor /
 *    registerProcessor / sampleRate 三个 worklet 全局），逐 quantum 喂 Float32 样本，
 *    验证采样计数、峰值定位、旁路直通。纯计时逻辑不需要浏览器就能证伪。
 * 2. **源码契约**：接线最容易的退化方式是「时钟悬空不接 destination」（process 根本不跑，
 *    采样数恒 0，字幕永远不动）或「时钟直接出声」（与 <audio> 元素双声）。两者都在类型层
 *    合法，只能靠字面量兜住。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const WORKLET_URL = new URL("../src/pad/audio/playout-clock-worklet.js", import.meta.url);
const read = (name: string): string => readFileSync(new URL(`../src/pad/${name}`, import.meta.url), "utf8");

const SAMPLE_RATE = 48_000;
const QUANTUM = 128;

interface Report {
  samples: number;
  rms: number;
  peakSamples: number;
  sampleRate: number;
}

/** 在 Node 里加载 worklet：注入三个 worklet 全局，返回 processor 构造器。 */
function loadProcessor(): {
  make: () => { process: (i: Float32Array[][] , o: Float32Array[][]) => boolean; port: { messages: Report[] } };
} {
  const source = readFileSync(WORKLET_URL, "utf8");
  let captured: unknown = null;
  class AudioWorkletProcessorStub {
    port = {
      messages: [] as Report[],
      postMessage(data: Report) {
        this.messages.push(data);
      },
    };
  }
  const register = (_name: string, ctor: unknown) => {
    captured = ctor;
  };
  // worklet 是浏览器全局脚本（没有 export），Node 里只能把三个全局当参数注入
  new Function("AudioWorkletProcessor", "registerProcessor", "sampleRate", source)(
    AudioWorkletProcessorStub,
    register,
    SAMPLE_RATE,
  );
  assert.ok(captured, "worklet 没有调用 registerProcessor：主线程 new AudioWorkletNode 会抛");
  const Ctor = captured as new () => {
    process: (i: Float32Array[][], o: Float32Array[][]) => boolean;
    port: { messages: Report[] };
  };
  return { make: () => new Ctor() };
}

/** 喂 n 个 quantum，每个 quantum 恒定幅度；返回输出缓冲供直通断言。 */
function feed(
  node: { process: (i: Float32Array[][], o: Float32Array[][]) => boolean },
  amplitudes: number[],
): Float32Array[] {
  const outputs: Float32Array[] = [];
  for (const amp of amplitudes) {
    const input = new Float32Array(QUANTUM).fill(amp);
    const output = new Float32Array(QUANTUM);
    node.process([[input]], [[output]]);
    outputs.push(output);
  }
  return outputs;
}

test("worklet：每个 quantum 推进 128 个采样，空输入也推进", () => {
  const { make } = loadProcessor();
  const node = make();
  feed(node, new Array(40).fill(0.2));
  const last = node.port.messages.at(-1);
  assert.ok(last, "40 个 quantum（106ms）之后一条上报都没有：主线程时间轴永远不动");
  assert.equal(last.samples, 40 * QUANTUM);

  // 空输入（轨还没到 / 元素还没播）：图在跑就说明播放时钟在走，停计数会让时间轴凭空卡住
  for (let i = 0; i < 40; i += 1) node.process([[]], [[]]);
  assert.equal(node.port.messages.at(-1)?.samples, 80 * QUANTUM);
});

test("worklet：process 恒返回 true（返回 false 会让节点被回收，时钟一次性死掉）", () => {
  const { make } = loadProcessor();
  const node = make();
  assert.equal(node.process([[new Float32Array(QUANTUM)]], [[new Float32Array(QUANTUM)]]), true);
  assert.equal(node.process([[]], [[]]), true);
});

test("worklet：上报周期约 20ms，不是每 quantum 一条（主线程会被消息淹掉）", () => {
  const { make } = loadProcessor();
  const node = make();
  feed(node, new Array(375).fill(0.2)); // 1 秒
  const count = node.port.messages.length;
  assert.ok(count >= 40 && count <= 60, `1 秒上报了 ${count} 条，应在 50 条量级`);
});

test("worklet：峰值采样序号指向峰值那个 quantum，不是上报窗口末尾", () => {
  const { make } = loadProcessor();
  const node = make();
  // 第 2 个 quantum 是峰值，其后同窗口内都是小幅度
  feed(node, [0.05, 0.9, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05]);
  const report = node.port.messages[0];
  assert.ok(report, "第一个上报窗口没出消息");
  assert.equal(report.peakSamples, 2 * QUANTUM, "零点被推后到窗口末尾：首帧出声会系统性晚 ~20ms");
  assert.ok(Math.abs(report.rms - 0.9) < 1e-6, "上报的应是窗口内 RMS 峰值");
});

test("worklet：采样数跨轮单调，静音段不清零（清零会让上一轮 anchor 减出负数）", () => {
  const { make } = loadProcessor();
  const node = make();
  feed(node, new Array(40).fill(0.5));
  const mid = node.port.messages.at(-1)?.samples ?? 0;
  feed(node, new Array(200).fill(0)); // 长静音
  const after = node.port.messages.at(-1)?.samples ?? 0;
  assert.ok(after > mid, "静音段把计数清掉了：轮次零点会失效");
  assert.equal(after, 240 * QUANTUM);
});

test("worklet：旁路直通 —— 输入原样拷到输出，不改声", () => {
  const { make } = loadProcessor();
  const node = make();
  const input = new Float32Array(QUANTUM);
  for (let i = 0; i < QUANTUM; i += 1) input[i] = Math.sin(i / 7) * 0.4;
  const output = new Float32Array(QUANTUM);
  node.process([[input]], [[output]]);
  assert.deepEqual(Array.from(output), Array.from(input));
});

test("源码契约：时钟接 gain=0 汇点 —— 既被 pull 又不与 <audio> 双声", () => {
  const realtime = read("realtime.ts");
  assert.ok(
    realtime.includes("sink.gain.value = 0;"),
    "汇点不是 0 增益：时钟会与 <audio> 元素同时出声（双声）",
  );
  assert.ok(
    realtime.includes("node.connect(sink);") && realtime.includes("sink.connect(ctx.destination);"),
    "时钟没有到 destination 的通路：AudioWorkletNode 不被 pull，process 不跑、采样数恒 0",
  );
  assert.ok(
    !/this\.clockNode\.connect\(\s*ctx\.destination/.test(realtime),
    "时钟直连 destination：绕过 0 增益汇点就是双声",
  );
});

test("源码契约：两条播放源都接到时钟，时钟晚到时补接", () => {
  const realtime = read("realtime.ts");
  assert.ok(
    realtime.includes("if (this.streamSource) this.streamSource.connect(node);"),
    "轨路径没补接：模块加载完成前到达的远端轨永远不进时钟",
  );
  assert.ok(
    realtime.includes("if (this.elementSource) this.elementSource.connect(node);"),
    "元素路径没补接：打字回复的音频不进时钟",
  );
  assert.equal(
    (realtime.match(/this\.tapPlayoutClock\(source\);/g) ?? []).length,
    2,
    "两条播放路径都必须 tap 时钟，少一条就有一类回复走回墙钟",
  );
});

test("源码契约：时钟建不起来时降级到 analyser + 墙钟，并上报一次诊断", () => {
  const realtime = read("realtime.ts");
  assert.ok(
    realtime.includes('this.reportDiag("playback", false,'),
    "时钟失败必须留一条 client_diag：否则线上分不清「精度下降」与「功能坏了」",
  );
  assert.ok(
    realtime.includes("if (this.clockNode) return this.clockRms;"),
    "readRms 必须优先用时钟的 quantum RMS，否则口型仍走 Analyser 的平滑滞后",
  );
  /* 2026-11-12：这里的断言原先钉的是一行三元式
   * `return this.subtitleAnchorAt !== null ? now - this.subtitleAnchorAt : 0;`，
   * 后来 playbackElapsedMs 长出了「元素路径等首帧能量」的分支（见 realtime.ts 的同名方法），
   * 三元式被拆成两段早退。契约没变——**没有采样锚点时仍要退回墙钟**——所以下面按
   * 语义拆成三条断言，而不是再去追一个已经不存在的字面量。 */
  assert.ok(
    realtime.includes("if (this.subtitleAnchorAt === null) return 0;"),
    "没有采样锚点时必须先停在 0：老内核上时钟起不来，不能凭空算出已播时长",
  );
  assert.ok(
    realtime.includes("return now - this.subtitleAnchorAt;"),
    "没有采样锚点时必须退回墙钟：老内核上字幕不能整条不动",
  );
});