/**
 * W1 声纹录入（pad）：C4 三端点的前端封装 + 浏览器端 16k/16bit/单声道 WAV 采集。
 *
 * 契约（契约记录 C4，后端已实现，前端一行不改后端）：
 *   GET    /api/voiceprints/status   → { elder_id, status, registered, version }
 *   POST   /api/voiceprints/samples  → multipart: audio(file, WAV≤10s) + sample_index(1|2|3)
 *   DELETE /api/voiceprints
 * 鉴权一律设备令牌（`Authorization: Bearer <access_token>`），令牌从 session.ts 取，
 * 不自建第二套 fetch —— 错误分类口径与 rest-client.ts 保持一致。
 *
 * WAV 编码（S-D4-1）：`MediaRecorder` 在浏览器里只给 webm/opus，后端只收 WAV，
 * 所以这里走 `AudioContext` 取 PCM → `OfflineAudioContext(1, len*16000/sr, 16000)`
 * 重采样 → 44 字节 PCM16 小端头。头与 float→int16 转换复用 `audio/wav.ts::assembleWav`，
 * 不再复制一份 RIFF 头（两处各写一份迟早会漂）。
 */
import { MicCaptureError, MIC_ERROR_TEXT, type MicErrorCode } from "./audio/capture";
import { assembleWav } from "./audio/wav";
import { deviceToken } from "./http";

/** 后端硬要求：16kHz / 16bit / 单声道 WAV，单段 ≤10s（C4） */
export const SAMPLE_RATE = 16000;
/** 单段上限：到点自动停（UI 计时 + 采集侧硬闸双保险） */
export const MAX_SAMPLE_SECONDS = 10;
/** 太短的段没有声纹价值，本地就拒，不必打后端（省一次 422） */
const MIN_SAMPLE_SECONDS = 0.6;
const UPLOAD_TIMEOUT_MS = 20_000;

/** 三段入学样本的序号（后端 sample_index 只收 1/2/3） */
export type SampleIndex = 1 | 2 | 3;

/**
 * 前端归一后的状态。
 * 注意：后端 `voiceprint_service.VoiceprintStatus.status` 的实际取值是
 * `"none" | "partial" | "active"`（app/services/voiceprint_service.py:191），
 * 与计划/任务书的 `not_registered` 字面不同；本模块把 `none` 归一成
 * `not_registered`，调用方只需判断「是否已 active」。
 */
export type VoiceprintState = "not_registered" | "partial" | "active";

export interface VoiceprintStatus {
  status: VoiceprintState;
  /** 已入库的样本序号（1/2/3 的子集） */
  registered: number[];
}

export interface SubmitResult {
  ok: boolean;
  /** 失败时的后端错误码（duplicate_sample / vp_extract_unavailable / device_unbound …）或 network */
  code?: string;
  /** 后端 detail 原文，仅供页面映射话术，不直接透出内部信息 */
  detail?: string;
}

/* ------------------------------------------------------------------ REST */

/**
 * 查一次声纹注册状态。
 * 失败（未登录、409 设备未绑长者、503 服务未起、网络错、body 非法）一律返回 null，
 * **不抛**：这是首屏旁路的锦上添花，不能因为声纹服务没起就把主界面弄崩。
 */
export async function getVoiceprintStatus(): Promise<VoiceprintStatus | null> {
  const token = deviceToken();
  if (!token) return null;
  try {
    const res = await fetch("/api/voiceprints/status", {
      headers: {
        Authorization: `Bearer ${token}`,
        accept: "application/json",
      },
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { status?: unknown; registered?: unknown };
    const status: VoiceprintState =
      body.status === "active" ? "active" : body.status === "partial" ? "partial" : "not_registered";
    const registered = Array.isArray(body.registered)
      ? body.registered.filter((n): n is number => typeof n === "number")
      : [];
    return { status, registered };
  } catch {
    return null;
  }
}

/**
 * 上传一段样本。成功返 `{ok:true}`；失败把后端 `code`/`detail` 原样带回，
 * 由页面决定话术（不在这里拼长者可见文案，避免 UI 文案分散到两处）。
 */
export async function submitSample(blob: Blob, sampleIndex: SampleIndex): Promise<SubmitResult> {
  const token = deviceToken();
  if (!token) {
    return { ok: false, code: "no_session", detail: "登录已过期，请重新登录" };
  }

  const form = new FormData();
  // 文件名带 .wav：后端只校验 RIFF/WAVE 头，但文件名是排查日志时的一手线索
  form.append("audio", blob, `voiceprint-${sampleIndex}.wav`);
  form.append("sample_index", String(sampleIndex));

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), UPLOAD_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch("/api/voiceprints/samples", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
      signal: ctrl.signal,
    });
  } catch {
    return { ok: false, code: "network", detail: "网络连接失败，请检查网络后重试" };
  } finally {
    clearTimeout(timer);
  }

  if (res.ok) return { ok: true };

  // 错误体统一 {detail, code, trace_id}（app/api/voiceprints.py）；body 非 JSON 时只留 HTTP 码
  let code = `http_${res.status}`;
  let detail: string | undefined;
  try {
    const body = (await res.json()) as { code?: unknown; detail?: unknown };
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    /* 非 JSON：保留 http_xxx */
  }
  return { ok: false, code, detail };
}

/**
 * 清除已注册的声纹（C4 的 DELETE /api/voiceprints，204 无响应体）。
 *
 * 为什么要有它：三段录满后 `status=active`，入口按钮按设计隐藏，而此前
 * 前端**没有任何重录出口**——长者录歪了（环境吵、念错段）只能登库删，
 * 2026-11-11 产品负责人实测就是问「为什么录完按钮就消失了」。
 *
 * 失败口径与 submitSample 一致：不抛，把后端 code 带回页面映射话术。
 * 未登录直接给 no_session，不打后端。
 */
export async function deleteVoiceprints(): Promise<SubmitResult> {
  const token = deviceToken();
  if (!token) {
    return { ok: false, code: "no_session", detail: "登录已过期，请重新登录" };
  }
  let res: Response;
  try {
    res = await fetch("/api/voiceprints", {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    return { ok: false, code: "network", detail: "网络连接失败，请检查网络后重试" };
  }
  if (res.ok) return { ok: true };

  let code = `http_${res.status}`;
  let detail: string | undefined;
  try {
    const body = (await res.json()) as { code?: unknown; detail?: unknown };
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    /* 非 JSON：保留 http_xxx */
  }
  return { ok: false, code, detail };
}

/* ----------------------------------------------------- 采集 + 重采样 + 编码 */

function toMicError(err: unknown): MicCaptureError {
  // 与 audio/capture.ts::mapGetUserMediaError 同口径；那边没导出该函数，
  // 而本波只允许新增 voiceprint.ts / VoiceprintEnroll.tsx 两个文件，
  // 故在此复用其**错误类型与话术表**（不复制文案）。
  const name = err instanceof DOMException ? err.name : "";
  const code: MicErrorCode =
    name === "NotFoundError" || name === "OverconstrainedError"
      ? "not_found"
      : name === "NotReadableError" || name === "AbortError"
        ? "not_readable"
        : "not_allowed";
  return new MicCaptureError(code, MIC_ERROR_TEXT[code]);
}

/** float32（[-1,1]）→ PCM16 小端（Int16Array，交给 assembleWav 写头） */
function floatToPcm16(data: Float32Array): Int16Array {
  const out = new Int16Array(data.length);
  for (let i = 0; i < data.length; i++) {
    const s = data[i] < -1 ? -1 : data[i] > 1 ? 1 : data[i];
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

/** 任意采样率单声道 Float32 → 16k PCM16（OfflineAudioContext 一步完成重采样） */
async function resampleToPcm16(samples: Float32Array, sourceRate: number): Promise<Int16Array> {
  const length = Math.max(1, Math.ceil((samples.length * SAMPLE_RATE) / sourceRate));
  const offline = new OfflineAudioContext(1, length, SAMPLE_RATE);
  const buffer = offline.createBuffer(1, samples.length, sourceRate);
  // 用 getChannelData().set() 而非 copyToChannel()：TS 5.7+ 把 TypedArray 泛型化为
  // Float32Array<ArrayBufferLike>，而 copyToChannel 形参要求 Float32Array<ArrayBuffer>，
  // 直接传会报 TS2345（SharedArrayBuffer 不可赋值给 ArrayBuffer）。set() 接受 ArrayLike<number>。
  buffer.getChannelData(0).set(samples);
  const src = offline.createBufferSource();
  src.buffer = buffer;
  src.connect(offline.destination);
  src.start();
  const rendered = await offline.startRendering();
  return floatToPcm16(rendered.getChannelData(0));
}

/**
 * 一段声纹样本的采集器：start() 开麦，stop() 返回 16k 单声道 WAV Blob。
 * 到点自动停由页面计时驱动（10s），采集侧另有样本数硬闸，双保险防超长被后端 413。
 */
export class VoiceSampler {
  private ctx: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private node: ScriptProcessorNode | null = null;
  private stream: MediaStream | null = null;
  private chunks: Float32Array[] = [];
  private samples = 0;
  private sampleRate = SAMPLE_RATE;

  get recording(): boolean {
    return this.ctx !== null;
  }

  async start(): Promise<void> {
    if (this.ctx) return;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          // 与主链路同口径（audio/capture.ts §2.3）：外放场景先压掉回声与底噪
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
    } catch (err) {
      throw toMicError(err);
    }

    const ctx = new AudioContext();
    if (ctx.state === "suspended") await ctx.resume().catch(() => undefined);
    const source = ctx.createMediaStreamSource(this.stream);
    // ScriptProcessorNode 已 deprecated，但它是唯一不需要额外 AudioWorklet 文件就能
    // 拿 PCM 的手段（本波写作用域不允许再加第三个文件）。仅在录入这 10 秒内用，
    // 不做任何 DSP，4096 帧/块的开销可忽略。
    const node = ctx.createScriptProcessor(4096, 1, 1);
    const limit = Math.round(MAX_SAMPLE_SECONDS * ctx.sampleRate);
    node.onaudioprocess = (e: AudioProcessingEvent) => {
      if (this.samples >= limit) return; // 硬闸：即便页面计时卡住也不会超长
      const chunk = new Float32Array(e.inputBuffer.getChannelData(0));
      this.samples += chunk.length;
      this.chunks.push(chunk);
    };
    source.connect(node);
    // ScriptProcessor 必须接 destination 才会被调度；中间串 0 增益，
    // 否则麦克风会回灌到扬声器（长者端外放，会啸叫）。
    const mute = ctx.createGain();
    mute.gain.value = 0;
    node.connect(mute);
    mute.connect(ctx.destination);

    this.ctx = ctx;
    this.source = source;
    this.node = node;
    this.sampleRate = ctx.sampleRate;
    this.chunks = [];
    this.samples = 0;
  }

  /** 结束采集并产出 WAV；太短/没采到返回 null（页面提示重录，不打后端） */
  async stop(): Promise<Blob | null> {
    const chunks = this.chunks;
    const sampleRate = this.sampleRate;
    this.chunks = [];
    this.samples = 0;
    await this.teardown();

    let total = 0;
    for (const c of chunks) total += c.length;
    if (total === 0) return null;

    const merged = new Float32Array(total);
    let offset = 0;
    for (const c of chunks) {
      merged.set(c, offset);
      offset += c.length;
    }
    const pcm = await resampleToPcm16(merged, sampleRate);
    if (pcm.length < SAMPLE_RATE * MIN_SAMPLE_SECONDS) return null;
    // 44 字节 RIFF 头 + PCM16 小端数据（audio/wav.ts 唯一实现）
    return assembleWav([pcm]);
  }

  /** 中断（退出页面/切段）：不产出 Blob，直接释放麦克风 */
  async abort(): Promise<void> {
    this.chunks = [];
    this.samples = 0;
    await this.teardown();
  }

  private async teardown(): Promise<void> {
    const node = this.node;
    const source = this.source;
    const stream = this.stream;
    const ctx = this.ctx;
    this.node = null;
    this.source = null;
    this.stream = null;
    this.ctx = null;

    if (node) {
      node.onaudioprocess = null;
      node.disconnect();
    }
    source?.disconnect();
    stream?.getTracks().forEach((t) => t.stop());
    if (ctx && ctx.state !== "closed") {
      await ctx.close().catch(() => undefined);
    }
  }
}
