/**
 * T-G 降级 REST 客户端（C3 契约）：WebRTC 主链路不可用时，按住说话整段上传。
 * 错误四类均不白屏：401 → 回登录；413 → 超长提示；503 → body 降级话术；网络/其他 → 通用话术。
 */
import { assembleWav } from "./audio/wav";

// 单次整段上传的等待上限：必须覆盖「最长 120 秒音频（约 3.84MB）上传 + 服务端 ASR」。
// 旧值 15 秒是按已被推翻的 30 秒录音上限估的——录满 2 分钟时它会先 abort 成
// 「语音服务暂时不可用」，把一次合法请求说成故障。60 秒是同一语义链上的兄弟常量。
const UTTERANCE_TIMEOUT_MS = 60_000;
const GENERIC_UNAVAILABLE = "语音服务暂时不可用，请稍后再试";
// 上限与服务端 `MAX_AUDIO_SECONDS = 120`（PRD FR-UI-01）同义：改一处必须改另一处，
// 否则长者录满 2 分钟后会拿到一句自相矛盾的「请说 30 秒以内」。
const TOO_LONG = "说话有点长，请说 2 分钟以内的内容";

export interface UtteranceSuccess {
  text: string;
  replyText: string;
  replyAudioUrl: string | null;
}

export type UtteranceError =
  | { kind: "auth"; message?: undefined } // 401：上层回登录
  | { kind: "too_long"; message: string } // 413
  | { kind: "unavailable"; message: string } // 503 / 网络 / 其他
  | { kind: "empty"; message?: undefined }; // 按住时间过短无音频

/**
 * 从 FastAPI 错误体里抠出「能给长者看的那句话」。
 *
 * 后端 `app/main.py:664-683` 的 HTTPException handler 已把 `detail` 包成
 * `ErrorResponse{detail, code, trace_id}`——**响应体里根本没有 message 字段**，
 * 业务原因（503 的 ASR 降级话术、422 的入参说明）全在 `detail` 里。
 * detail 的历史口径是「内部错误描述」，故这里只做形态归一，不做语义判断：
 * 字符串直接用；FastAPI 校验错误数组（每项 `{msg, loc, type}`）取 msg 拼起来。
 */
function detailText(detail: unknown): string | null {
  if (typeof detail === "string") return detail.trim() || null;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item: unknown) => {
        if (typeof item === "string") return item.trim();
        if (item !== null && typeof item === "object") {
          const msg = (item as { msg?: unknown }).msg;
          if (typeof msg === "string") return msg.trim();
        }
        return "";
      })
      .filter((text) => text.length > 0);
    return parts.length > 0 ? parts.join("；") : null;
  }
  return null;
}

/** frames：按时间序的 16k PCM16 帧（调用方先放 preRoll 再放按住期间帧） */
export async function sendUtterance(
  token: string,
  frames: Int16Array[],
  durationMs: number,
): Promise<UtteranceSuccess | UtteranceError> {
  if (frames.length === 0) return { kind: "empty" };
  const wav = assembleWav(frames, durationMs);
  const form = new FormData();
  form.append("audio", wav, "utterance.wav");
  form.append("duration_ms", String(Math.round(durationMs)));

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), UTTERANCE_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch("/api/audio/utterance", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
      signal: ctrl.signal,
    });
  } catch {
    return { kind: "unavailable", message: GENERIC_UNAVAILABLE };
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401) return { kind: "auth" };
  if (res.status === 413) return { kind: "too_long", message: TOO_LONG };
  if (res.status === 200) {
    const body = (await res.json()) as {
      text?: string;
      reply?: { text?: string; audio_url?: string | null };
    };
    return {
      text: body.text ?? "",
      replyText: body.reply?.text ?? "",
      replyAudioUrl: body.reply?.audio_url ?? null,
    };
  }
  // 503 及其他：后端把具体原因写在 `detail`（503 = 降级话术模板，422 = 入参说明），
  // 其次才看契约里的 message，最后才是通用文案——只读 message 会让长者永远看到
  // 「语音服务暂时不可用」，分不清「麦克风没声」和「服务在重启」。
  let message = GENERIC_UNAVAILABLE;
  try {
    const body = (await res.json()) as { detail?: unknown; message?: string };
    message = detailText(body.detail) ?? body.message ?? GENERIC_UNAVAILABLE;
  } catch {
    /* body 非 JSON，用通用话术 */
  }
  return { kind: "unavailable", message };
}
