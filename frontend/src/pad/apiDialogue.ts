/**
 * 打字对话（降级入口第二级，AGENTS §6 / PRD FR-UI-07）。
 *
 * 后端 `POST /api/dialogue/text` 与话音走同一条对话链（L0→L1→DialogueService），
 * 所以这里的 `reply.text` 才是「数字人真说的话」，不要在前端再编一句。
 *
 * 口径：
 * - 只带设备令牌；`elder_id` 由服务端从会话读（请求体里塞也会被丢弃，AGENTS §4-9）。
 * - 失败不抛到界面：返回一句长者看得懂的话 + `ok:false`，输入框保留原文让人重发。
 */

import { deviceToken } from "./http";
import type { TtsSegment } from "./protocol";

export type TextTurnOutcome = {
  ok: boolean;
  /** 数字人的回复（失败时是兜底话术） */
  reply: string;
  /** 服务端是否降级（LLM 不可用/路由异常仍返回 200 + degraded） */
  degraded: boolean;
  /** 安全链是否命中（急症句会在这里被打标，界面据此改话术颜色） */
  safetyAlert: boolean;
};

/** 打字回复的语音（2026-11-11）：拿到就播，拿不到就纯字幕。 */
export type ReplyAudioOutcome = {
  ok: boolean;
  /** 可直接给 `<audio>` 用的地址；没有音频时为 null */
  audioUrl: string | null;
  /** 语音时长（ms）；前端用它对齐逐字节奏与口型收口 */
  durationMs: number | null;
  /** 逐句时间窗（服务端 split_sentences 切句）；降级时为空数组 */
  segments: TtsSegment[];
};

/**
 * 句内逐字坐标：整组一起收或一起丢。
 *
 * 只丢一个坏值会让后面的字全部错位（坐标是「第 i 个字」的绝对偏移，抽掉一个就整体前移），
 * 所以任何一个值不是有限非负数就整组作废，让揭示回落到匀速比例。
 */
function readCharMs(raw: unknown): number[] | undefined {
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const out: number[] = [];
  for (const value of raw) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return undefined;
    out.push(value);
  }
  return out;
}

/** 服务端下发的逐句时间窗只做形状校验：缺字段/类型不对的句子直接跳过。 */
function readSegments(raw: unknown): TtsSegment[] {
  if (!Array.isArray(raw)) return [];
  const out: TtsSegment[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const {
      text,
      start_ms: startMs,
      duration_ms: durationMs,
      char_ms: charMs,
    } = item as Record<string, unknown>;
    if (typeof text !== "string" || text === "") continue;
    if (typeof startMs !== "number" || startMs < 0) continue;
    if (typeof durationMs !== "number" || durationMs <= 0) continue;
    const segment: TtsSegment = { text, start_ms: startMs, duration_ms: durationMs };
    const offsets = readCharMs(charMs);
    if (offsets) segment.char_ms = offsets;
    out.push(segment);
  }
  return out;
}

const NETWORK_TEXT = "这句话没送出去，再说一次我就能听见";
const UNAUTHORIZED_TEXT = "登录已失效，请重新登录";

/**
 * 打字轮的服务端等待上限（task-42）。
 *
 * 此前这条请求没有超时：后端或网关把连接吊住时 `fetch` 可以永远不 settle，
 * `TextFallback` 的 `busy` 就一直是 true，底栏停在「发送中…」——长者看到的
 * 是「按钮坏了」，没有报错、没有重试入口，只能刷新页面。
 * 15s 的取法：正常打字轮回包实测在 1s 量级（LLM 首 token ≤800ms + TTS 不阻塞），
 * 给到 15s 已足够覆盖一次慢请求，又不至于让长者对着「发送中…」等太久。
 */
export const TEXT_TURN_TIMEOUT_MS = 15_000;

/** 打字轮超时（与网络失败区分开：文案要说「没等到回复」，而不是「没送出去」）。 */
export class TextTurnTimeoutError extends Error {
  constructor(readonly timeoutMs: number) {
    super(`text turn timed out after ${timeoutMs}ms`);
    this.name = "TextTurnTimeoutError";
  }
}

/**
 * 带超时的打字轮：到点 `abort()` 并抛 {@link TextTurnTimeoutError}。
 *
 * 为什么先 abort 再 reject：`sendTextTurn` 内部把 fetch 失败都吞成 `ok:false` 的
 * 结果对象，不 abort 的话那条「慢请求」会在超时后继续挂着占连接；abort 让它立刻收摊。
 * `finally` 清定时器，成功路径不留一条 15s 后才触发的僵尸定时器。
 */
export async function sendTextTurnWithin(
  text: string,
  timeoutMs: number = TEXT_TURN_TIMEOUT_MS,
): Promise<TextTurnOutcome> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timedOut = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new TextTurnTimeoutError(timeoutMs));
    }, timeoutMs);
  });
  try {
    return await Promise.race([sendTextTurn(text, controller.signal), timedOut]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export async function sendTextTurn(
  text: string,
  signal?: AbortSignal,
): Promise<TextTurnOutcome> {
  const token = deviceToken();
  if (!token) {
    return { ok: false, reply: UNAUTHORIZED_TEXT, degraded: false, safetyAlert: false };
  }

  let res: Response;
  try {
    res = await fetch("/api/dialogue/text", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ text }),
      // 可选：调用方（`submitTextTurn`）要能在超时时把这条请求掐掉，否则它就只是被抛下。
      ...(signal ? { signal } : {}),
    });
  } catch {
    return { ok: false, reply: NETWORK_TEXT, degraded: false, safetyAlert: false };
  }

  if (res.status === 401) {
    return { ok: false, reply: UNAUTHORIZED_TEXT, degraded: false, safetyAlert: false };
  }
  if (!res.ok) {
    return { ok: false, reply: NETWORK_TEXT, degraded: false, safetyAlert: false };
  }

  let body: {
    reply?: { text?: string };
    degraded?: boolean;
    safety_alert?: boolean;
  } = {};
  try {
    body = (await res.json()) as typeof body;
  } catch {
    return { ok: false, reply: NETWORK_TEXT, degraded: false, safetyAlert: false };
  }

  const replyText = (body.reply?.text ?? "").trim();
  if (!replyText) {
    return { ok: false, reply: NETWORK_TEXT, degraded: false, safetyAlert: false };
  }
  return {
    ok: true,
    reply: replyText,
    degraded: body.degraded === true,
    safetyAlert: body.safety_alert === true,
  };
}

/**
 * 给一句**已经上屏**的回复补语音（`POST /api/dialogue/tts`）。
 *
 * 为什么单独一次请求、而不是让 `/api/dialogue/text` 直接返回音频：
 * TTS 合成本机实测约 0.6× 实时（10 秒回复要等 6 秒），塞进打字响应会让每句话的回显
 * 都卡上数秒。先回文本、再补声音，长者「它理我了」的那一下来得快。
 *
 * 失败一律返回 `ok:false, audioUrl:null`——语音是增强项，不能因为它让回复消失。
 */
export async function requestReplyAudio(
  text: string,
  token: string,
): Promise<ReplyAudioOutcome> {
  if (!token.trim() || !text.trim()) {
    return { ok: false, audioUrl: null, durationMs: null, segments: [] };
  }

  let res: Response;
  try {
    res = await fetch("/api/dialogue/tts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ text }),
    });
  } catch {
    return { ok: false, audioUrl: null, durationMs: null, segments: [] };
  }
  if (!res.ok) {
    return { ok: false, audioUrl: null, durationMs: null, segments: [] };
  }

  let body: { audio_url?: string | null; audio_duration_ms?: number | null; audio_segments?: TtsSegment[] } = {};
  try {
    body = (await res.json()) as typeof body;
  } catch {
    return { ok: false, audioUrl: null, durationMs: null, segments: [] };
  }

  const audioUrl = typeof body.audio_url === "string" ? body.audio_url : null;
  if (!audioUrl) {
    return { ok: false, audioUrl: null, durationMs: null, segments: [] };
  }
  const durationMs =
    typeof body.audio_duration_ms === "number" && body.audio_duration_ms > 0
      ? body.audio_duration_ms
      : null;
  return { ok: true, audioUrl, durationMs, segments: readSegments(body.audio_segments) };
}
