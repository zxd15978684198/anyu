/**
 * T1 端侧唤醒词「小伴」（FR-UI-11 / AC-42，P0）。
 *
 * 隐私红线（PRD:257）：关键词检测**全程在端侧**完成，未命中时原始音频不出端——
 * 本模块不 import 任何网络/信令/上传模块，麦克风帧只有一个去处：喂给 KWS 解码器。
 * 命中后才回调 onWake，由上层去建连（本文件不做建连，接线由 pad 根组件统一做）。
 *
 * 实现路线与 vad.ts 同源：sherpa-onnx WASM **静态注入**（运行期从 /kws-assets 动态
 * import emscripten 胶水 + fetch 模型包），不打进 JS bundle，加载失败超时回落。
 * 资产来源/许可证/SHA256 见 frontend/public/kws-assets/NOTICE.md。
 *
 * 回落（FR-UI-09）：wasm 或模型加载失败、麦克风不可用 → 抛 KwsUnavailableError，
 * 上层据此把唤醒入口降级为「呼叫按钮」；降级前必定先打日志（可观测，不静默）。
 */

import { MicCapture, MicCaptureError, TARGET_RATE } from "./capture";
import {
  DEFAULT_WAKE_TOKENS,
  DEFAULT_WAKE_WORD,
  describeKeywordProblem,
  splitKeyword,
} from "./pinyin";

/* ---------------- 运行时资产与唤醒词口径 ---------------- */

/** 静态资产前缀（与 /vad-assets 同思路：public 下原样托管，构建期不打包） */
const KWS_ASSETS = "/kws-assets";
const MODEL_ASSETS = `${KWS_ASSETS}/model`;
/** wasm 12MB + 模型包 12.5MB；本地托管正常 <2s，给足余量后按不可用降级 */
const LOAD_TIMEOUT_MS = 30_000;
/** 出厂唤醒词与它的 token 编码；自定义词由 pinyin.ts 现切（tokens.txt 为拼音声母 + 带调韵母混合词表） */
export { DEFAULT_WAKE_WORD };
/** 出厂 token 序列（= splitKeyword("小伴")）。保留这个名字是因为既有用例按它做词表对齐 */
export const WAKE_TOKENS: readonly string[] = DEFAULT_WAKE_TOKENS;

/**
 * 唤醒词打分参数——数值来自 20 正 + 28 负（×3 音高变体）的实测扫描：
 * score=1/threshold=0.25：正样本 20/20 命中、负样本仅「小半」同音触发（20 条日常句 0 误触）。
 * 反直觉但实测如此：score=2/3 反而掉到 16-12/20（sherpa-onnx 的 score 不是「越大越灵敏」，
 * 加大后解码路径被压制）；threshold 在 0.05~0.25 之间对 score=1 无差别，取 0.25 留精度余量。
 */
export const KEYWORD_SCORE = 1;
export const KEYWORD_THRESHOLD = 0.25;
/** 判定「开口」的帧峰值（PCM16），只用来算「开口→命中」时延，不参与检测决策 */
const VOICE_PEAK = 1200;
/** 连续静音帧数超过此值即认为上一轮说话结束（60ms/帧 → 约 0.5s） */
const SILENCE_RESET_FRAMES = 8;

/* ---------------- 运行时接口（/kws-assets/kws.js 的形状） ---------------- */

interface ModelPackMetadata {
  readonly files: readonly { filename: string; start: number; end: number }[];
}

/** emscripten 模块：只需要写虚拟文件系统这一个入口 */
interface SherpaKwsModule {
  FS_createDataFile(
    parent: string,
    name: string,
    data: Uint8Array,
    canRead: boolean,
    canWrite: boolean,
    canOwn: boolean,
  ): void;
}

interface KwsDetection {
  readonly label: string;
  readonly startTime: number;
  readonly timestamps: readonly number[];
  readonly tokens: readonly string[];
}

interface NativeKwsSpotter {
  processAudio(samples: Float32Array, sampleRate: number): KwsDetection[];
  reset(): void;
  dispose(): void;
}

interface NativeKwsConfig {
  readonly model: {
    encoder: string;
    decoder: string;
    joiner: string;
    tokens: string;
  };
  readonly keywords: readonly {
    label: string;
    matches: readonly { tokens: readonly string[]; score: number; threshold: number }[];
  }[];
  readonly maxActivePaths?: number;
}

interface KwsRuntimeModule {
  initKWSModule(): Promise<SherpaKwsModule>;
  loadModelPack(
    module: SherpaKwsModule,
    data: ArrayBuffer,
    metadata: ModelPackMetadata,
  ): string[];
  createKeywordSpotter(module: SherpaKwsModule, config: NativeKwsConfig): NativeKwsSpotter;
}

/* ---------------- 对外错误与状态 ---------------- */

/** 归一化后的不可用原因：上层按它决定提示话术，但降级动作统一是「呼叫按钮」 */
export type KwsFailureReason =
  | "unsupported"
  | "mic_unavailable"
  | "assets_unavailable"
  | "init_failed";

export class KwsUnavailableError extends Error {
  constructor(
    public reason: KwsFailureReason,
    message: string,
  ) {
    super(message);
    this.name = "KwsUnavailableError";
  }
}

export type WakeWordState =
  | "loading"
  | "listening"
  | "paused"
  | "woken"
  | "stopped"
  | "unavailable";

export interface WakeWordHandle {
  /** 当前状态：上层可据此渲染「正在听」指示或呼叫按钮 */
  readonly state: WakeWordState;
  /** 当前唤醒词（默认「小伴」；设置页保存后它立即变成新词） */
  readonly keyword: string;
  /** TTS 播报期间挂起（防数字人自己说「小伴」触发），播报完 resume */
  pause(): void;
  resume(): void;
  /**
   * 运行时换词：只重建解码器，麦克风采集与已加载的 wasm/模型都不动。
   *
   * 失败语义：词切不出来（pinyin.ts 返回 null）时抛 `UnsupportedKeywordError`，
   * 旧词继续生效——绝不允许「切词失败 → 唤醒功能悄悄消失」。
   */
  setKeyword(keyword: string): Promise<void>;
  /** 释放麦克风与解码器；已唤醒/已卸载后调用是幂等的 */
  stop(): Promise<void>;
}

/** 唤醒词不可用（词表外汉字/超长/空串）；与 KwsUnavailableError 分开，上层据此只提示不降级 */
export class UnsupportedKeywordError extends Error {
  constructor(
    public readonly keyword: string,
    message: string,
  ) {
    super(message);
    this.name = "UnsupportedKeywordError";
  }
}

/** 构造唤醒器时的可选参数：不传就是出厂「小伴」 */
export interface WakeWordOptions {
  /** 自定义唤醒词（汉字）；非法词会让 `initWakeWord` 抛 UnsupportedKeywordError */
  keyword?: string;
  /** 直接给 token（测试/迁移用）；给了就不再查拼音表 */
  tokens?: readonly string[];
}

/* ---------------- 加载 ---------------- */

function fetchWithTimeout(url: string): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LOAD_TIMEOUT_MS);
  return fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
}

/** 拉模型包：data 与 metadata 必须来自同一个 pack，故一起取、任一失败都算不可用 */
async function fetchModelPack(): Promise<{ data: ArrayBuffer; metadata: ModelPackMetadata }> {
  const dataUrl = `${MODEL_ASSETS}/preload.data`;
  const metaUrl = `${MODEL_ASSETS}/preload.js.metadata`;

  const metaResp = await fetchWithTimeout(metaUrl);
  if (!metaResp.ok) {
    throw new KwsUnavailableError(
      "assets_unavailable",
      `模型元数据加载失败: HTTP ${metaResp.status} ${metaUrl}`,
    );
  }
  const metadata = (await metaResp.json()) as ModelPackMetadata;

  const dataResp = await fetchWithTimeout(dataUrl);
  if (!dataResp.ok) {
    throw new KwsUnavailableError(
      "assets_unavailable",
      `模型权重加载失败: HTTP ${dataResp.status} ${dataUrl}`,
    );
  }
  return { data: await dataResp.arrayBuffer(), metadata };
}

/** 动态 import 胶水；用变量 URL 是为了让 Vite 不把它打进 bundle（public 静态资产） */
async function loadRuntime(): Promise<KwsRuntimeModule> {
  const url = `${KWS_ASSETS}/kws.js`;
  try {
    return (await import(/* @vite-ignore */ url)) as KwsRuntimeModule;
  } catch (err) {
    throw new KwsUnavailableError(
      "assets_unavailable",
      `KWS 运行时加载失败: ${url}（${err instanceof Error ? err.message : String(err)}）`,
    );
  }
}

/* ---------------- 唤醒器 ---------------- */

/**
 * 对外只导出 `initWakeWord`；类本身导出仅为单测覆盖状态机迁移与命中守卫
 * （`kws.test.ts` 直接构造它、不碰 wasm），运行时行为与不导出时完全一致。
 */
export class WakeWord implements WakeWordHandle {
  private state_: WakeWordState = "loading";
  private capture: MicCapture | null = null;
  private spotter: NativeKwsSpotter | null = null;
  private frames = 0;
  private inferenceMsTotal = 0;
  private voiceOnsetAt = 0;
  private silentFrames = 0;
  /** 换词时只重建这部分：wasm 模块与模型包留在内存里，实测换词 ~130ms */
  private runtime: KwsRuntimeModule | null = null;
  private module: SherpaKwsModule | null = null;
  private keyword_: string;
  private tokens_: readonly string[];

  constructor(
    private readonly onWake: () => void,
    options: WakeWordOptions = {},
  ) {
    const keyword = (options.keyword ?? DEFAULT_WAKE_WORD).trim();
    const tokens = options.tokens ?? splitKeyword(keyword);
    if (!tokens || tokens.length === 0) {
      // 构造期就炸：宁可让上层立刻降级到呼叫按钮，也不要起一个「永远叫不醒」的监听
      throw new UnsupportedKeywordError(
        keyword,
        describeKeywordProblem(keyword) ?? `唤醒词「${keyword}」无法切分成词表 token`,
      );
    }
    this.keyword_ = keyword;
    this.tokens_ = [...tokens];
  }

  get state(): WakeWordState {
    return this.state_;
  }

  get keyword(): string {
    return this.keyword_;
  }

  get tokens(): readonly string[] {
    return this.tokens_;
  }

  /** 按当前词造一个解码器；失败直接把原因抛给调用方（换词时要能保留旧的） */
  private createSpotterFor(keyword: string, tokens: readonly string[]): NativeKwsSpotter {
    if (!this.runtime || !this.module) {
      throw new KwsUnavailableError("init_failed", "KWS 运行时尚未就绪，无法重建解码器");
    }
    return this.runtime.createKeywordSpotter(this.module, {
      model: {
        encoder: "/encoder.onnx",
        decoder: "/decoder.onnx",
        joiner: "/joiner.onnx",
        tokens: "/tokens.txt",
      },
      keywords: [
        {
          label: keyword,
          matches: [{ tokens: [...tokens], score: KEYWORD_SCORE, threshold: KEYWORD_THRESHOLD }],
        },
      ],
      maxActivePaths: 4,
    });
  }

  private setState(next: WakeWordState): void {
    if (this.state_ === next) return;
    // 状态位 + 日志双份：降级路径必须可观测，不能被静默吞掉
    console.info(`[kws] 状态 ${this.state_} → ${next}`);
    this.state_ = next;
  }

  async start(): Promise<void> {
    const t0 = performance.now();

    const runtime = await loadRuntime();
    console.info(`[kws] 运行时脚本就绪 ${(performance.now() - t0).toFixed(0)}ms`);

    const { data, metadata } = await fetchModelPack();
    console.info(
      `[kws] 模型包就绪 ${(data.byteLength / 1024 / 1024).toFixed(1)}MB / ${metadata.files.length} 个文件` +
        ` ${(performance.now() - t0).toFixed(0)}ms`,
    );

    let module: SherpaKwsModule;
    let spotter: NativeKwsSpotter;
    try {
      module = await runtime.initKWSModule();
      const written = runtime.loadModelPack(module, data, metadata);
      console.info(`[kws] wasm 初始化完成，挂载 ${written.join(" ")}`);
      // 运行时装进实例：换词时复用同一个 wasm 实例，不重新下载 24MB 资产
      this.runtime = runtime;
      this.module = module;
      spotter = this.createSpotterFor(this.keyword_, this.tokens_);
    } catch (err) {
      this.runtime = null;
      this.module = null;
      throw new KwsUnavailableError(
        "init_failed",
        `KWS 初始化失败: ${err instanceof Error ? err.message : String(err)}`,
      );
    }

    // 预热：首帧要跑 wasm 实例化与图分配，先喂一帧静音把它吃掉，免得算进首句时延
    const warmStart = performance.now();
    spotter.processAudio(new Float32Array(320), TARGET_RATE);
    spotter.reset();
    console.info(`[kws] 预热 ${(performance.now() - warmStart).toFixed(0)}ms`);

    this.spotter = spotter;

    const capture = new MicCapture();
    try {
      await capture.start();
    } catch (err) {
      spotter.dispose();
      this.spotter = null;
      if (err instanceof MicCaptureError) {
        throw new KwsUnavailableError("mic_unavailable", `麦克风不可用: ${err.message}`);
      }
      throw new KwsUnavailableError(
        "mic_unavailable",
        `麦克风启动失败: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    this.capture = capture;
    capture.onFrame = (pcm) => this.handleFrame(pcm);

    this.setState("listening");
    console.info(`[kws] 开始监听「${this.keyword_}」，总耗时 ${(performance.now() - t0).toFixed(0)}ms`);
  }

  private handleFrame(pcm: Int16Array): void {
    if (this.state_ !== "listening") return;
    const spotter = this.spotter;
    if (!spotter) return;

    const now = performance.now();
    let peak = 0;
    const samples = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i += 1) {
      const s = pcm[i];
      const abs = s < 0 ? -s : s;
      if (abs > peak) peak = abs;
      samples[i] = s / 32768;
    }

    // 仅用于报「开口→命中」时延；检测本身由 KWS 内部 VAD/端点决定
    if (peak > VOICE_PEAK) {
      if (this.voiceOnsetAt === 0) this.voiceOnsetAt = now - (pcm.length / TARGET_RATE) * 1000;
      this.silentFrames = 0;
    } else if (this.voiceOnsetAt !== 0 && ++this.silentFrames > SILENCE_RESET_FRAMES) {
      this.voiceOnsetAt = 0;
    }

    const t = performance.now();
    const hits = spotter.processAudio(samples, TARGET_RATE);
    this.frames += 1;
    this.inferenceMsTotal += performance.now() - t;

    if (hits.length === 0) return;
    const hit = hits[0];
    const latency = this.voiceOnsetAt > 0 ? now - this.voiceOnsetAt : Number.NaN;
    console.info(
      `[kws] 命中「${this.keyword_}」label=${hit.label} tokens=${hit.tokens.join(" ")}` +
        ` 开口→命中 ${Number.isNaN(latency) ? "n/a" : `${Math.round(latency)}ms`}` +
        ` 推理 ${(this.inferenceMsTotal / this.frames).toFixed(1)}ms/帧（${this.frames} 帧）`,
    );
    this.wake();
  }

  private wake(): void {
    if (this.state_ === "woken" || this.state_ === "stopped") return;
    this.setState("woken");
    // 先释放麦克风再回调：建连方要重新 getUserMedia，避免两路采集抢同一设备
    void this.disposeResources().finally(() => {
      try {
        this.onWake();
      } catch (err) {
        console.error("[kws] onWake 回调抛错（已忽略，不影响降级判定）", err);
      }
    });
  }

  pause(): void {
    if (this.state_ === "listening") this.setState("paused");
  }

  resume(): void {
    if (this.state_ !== "paused") return;
    this.spotter?.reset();
    this.voiceOnsetAt = 0;
    this.silentFrames = 0;
    this.setState("listening");
  }

  /**
   * 运行时换词：只重建解码器（wasm 与模型包留在内存里）。
   *
   * 顺序上**先造新的、再造好之后才换掉旧的**：新词建不出来时旧词继续工作，
   * 不会出现「设置页保存失败 → 唤醒功能静默消失」这种最难排查的状态。
   */
  async setKeyword(keyword: string): Promise<void> {
    const word = keyword.trim();
    if (word === this.keyword_) return;

    const tokens = splitKeyword(word);
    if (!tokens) {
      throw new UnsupportedKeywordError(
        word,
        describeKeywordProblem(word) ?? `唤醒词「${word}」无法切分成词表 token`,
      );
    }

    // 已结束（命中过/已卸载）或还没起解码器：只换配置，下次 start 用它
    if (this.state_ === "stopped" || this.state_ === "woken" || this.spotter === null) {
      this.keyword_ = word;
      this.tokens_ = [...tokens];
      console.info(`[kws] 唤醒词改为「${word}」（下次监听生效） tokens=${tokens.join(" ")}`);
      return;
    }

    const spotter = this.createSpotterFor(word, tokens);
    const previous = this.spotter;
    this.spotter = spotter;
    this.keyword_ = word;
    this.tokens_ = [...tokens];
    previous.dispose();
    // 换词后旧的「开口时刻/帧统计」都失去意义，清零免得日志里出现负时延
    this.voiceOnsetAt = 0;
    this.silentFrames = 0;
    this.frames = 0;
    this.inferenceMsTotal = 0;
    console.info(`[kws] 唤醒词已切换为「${word}」 tokens=${tokens.join(" ")}`);
  }

  private async disposeResources(): Promise<void> {
    const capture = this.capture;
    this.capture = null;
    this.spotter?.dispose();
    this.spotter = null;
    // 解码器已释放，运行时的引用也一起放掉：这个 handle 不会再被换词（wake/stop 之后语义上已死）
    this.runtime = null;
    this.module = null;
    if (capture) {
      capture.onFrame = null;
      await capture.stop().catch(() => undefined);
    }
  }

  async stop(): Promise<void> {
    if (this.state_ === "stopped") return;
    this.setState("stopped");
    await this.disposeResources();
  }
}

/**
 * 唯一入口：启动端侧唤醒词监听，命中时回调一次。
 *
 * 唤醒词默认「小伴」；传 `options.keyword` 可用用户在设置页保存的自定义词（汉字），
 * 词不可切分时**回退出厂词并打日志**——一个存坏的词不该把唤醒功能整个废掉。
 *
 * 失败一律抛 KwsUnavailableError（reason 区分原因），调用方据此降级为呼叫按钮（FR-UI-09）；
 * 本函数自己会先打降级日志，不会静默什么都不做。
 *
 * 生命周期：命中后本轮即结束（麦克风已释放、解码器已 dispose），回调里不要去 stop 它；
 * 需要再次监听就重新调用 initWakeWord（wasm/模型走 HTTP 缓存，实测第二次初始化 ~130ms）。
 */
export async function initWakeWord(
  onWake: () => void,
  options: WakeWordOptions = {},
): Promise<WakeWordHandle> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices) {
    console.info("[kws] 当前环境无 mediaDevices → 降级呼叫按钮（FR-UI-09）");
    throw new KwsUnavailableError("unsupported", "当前环境不支持麦克风采集");
  }

  // 存坏的词（旧版本遗留 / 后来的词表变更）回退出厂词：唤醒必须始终可用
  let keyword = (options.keyword ?? DEFAULT_WAKE_WORD).trim() || DEFAULT_WAKE_WORD;
  let tokens = options.tokens ?? splitKeyword(keyword);
  if (!tokens) {
    const problem = describeKeywordProblem(keyword) ?? "无法切分成词表 token";
    console.warn(`[kws] 唤醒词「${keyword}」暂不支持（${problem}）→ 回退出厂「${DEFAULT_WAKE_WORD}」`);
    keyword = DEFAULT_WAKE_WORD;
    tokens = DEFAULT_WAKE_TOKENS;
  }

  const handle = new WakeWord(onWake, { keyword, tokens });
  try {
    await handle.start();
    return handle;
  } catch (err) {
    const normalized =
      err instanceof KwsUnavailableError
        ? err
        : new KwsUnavailableError(
            "init_failed",
            `唤醒词初始化异常: ${err instanceof Error ? err.message : String(err)}`,
          );
    console.warn(
      `[kws] 不可用 reason=${normalized.reason} → 降级呼叫按钮（FR-UI-09）：${normalized.message}`,
    );
    await handle.stop();
    throw normalized;
  }
}
