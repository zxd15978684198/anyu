/**
 * pad 端（设备凭证）新增的两条收口接口 —— 迭代 6–10 缺口补齐的接线层。
 *
 * 为什么单开一个文件、不塞进 `offline/`：`offline/**` 是断网兜底的**纯端侧**实现，
 * 它刻意不内置请求地址（见 `offline/types.ts` 里 `loadReminders` 的注释：长者端原本
 * 「没有药单读接口」）。本文件只做 HTTP 信封与错误归一，让 `App.tsx` 的接线保持三行。
 *
 * 鉴权范式与 `voiceprint.ts` 一致：`Authorization: Bearer <设备 access_token>`，
 * 令牌取自 `session.ts`，**不自建第二套 fetch**。
 *
 * 对应后端（`app/api/pad.py`）：
 *   GET  /api/pad/medications/today → { reminders: OfflineReminder[] }
 *   POST /api/pad/offline/report    ← { kind, at, events } → { results: [...] }
 *   POST /api/pad/medications/{reminder_id}/rendered → { reminder_id, receipt, attempt_no }
 *   GET  /api/health/checkup/{elder_id}              → CheckupSession（今天没有会话时 404）
 *   POST /api/health/checkup/{elder_id}/answer       ← { text } → { session, record }
 * 后端字段与 `offline/types.ts` 的驼峰契约同源，不做二次映射；健康采集走
 * `app/api/health.py` 的 snake_case 原样（那份契约是后端既有形状，前端不改写它）。
 */
import type { OfflineReminder, OfflineReport } from "./offline/types";
import { detailText, deviceAuthHeader, timeoutSignal } from "./http";

/** 平板在卧室弱网环境，8s 内拿不到就当作「这次没拉到」，交给下一轮轮询 */
const REQUEST_TIMEOUT_MS = 8_000;

/** 统一错误：`code` 对后端 C7 错误体里的 `code` 字段（如 `device_not_bound`）。 */
export class PadApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "PadApiError";
  }
}

interface RequestInitLite {
  method?: "GET" | "POST";
  body?: unknown;
  headers?: Record<string, string>;
}

async function request(path: string, init: RequestInitLite = {}): Promise<unknown> {
  // 鉴权头与超时信号收敛到 pad/http（W5）：原本 8 处各写一遍 loadSession + Bearer，
  // 且超时有 AbortSignal.timeout 与 AbortController+setTimeout 两种等价写法。
  // 本端点的 8s 与错误语义（PadApiError）保持不变。
  let auth: Record<string, string>;
  try {
    auth = deviceAuthHeader();
  } catch {
    throw new PadApiError(401, "no_session", "本次会话已失效，请重新登录");
  }

  const timeout = timeoutSignal(REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      method: init.method ?? "GET",
      signal: timeout.signal,
      headers: {
        accept: "application/json",
        ...(init.body === undefined ? {} : { "content-type": "application/json" }),
        ...init.headers,
        ...auth,
      },
      ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    });

    const text = await response.text();
    let parsed: unknown = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      // 404/502 之类由代理回的 HTML 也会走到这里：保留原文前 120 字便于定位
      parsed = null;
      if (response.ok) {
        throw new PadApiError(response.status, "bad_json", text.slice(0, 120));
      }
    }

    if (!response.ok) {
      const obj = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
      // detail 用形态归一而不是 String()：FastAPI 校验错误是数组，
      // String(数组) 会得到 "[object Object]"，长者看不懂也排障不了。
      const detail = detailText(obj?.detail) ?? `HTTP ${response.status}`;
      const code = obj && typeof obj.code === "string" && obj.code ? obj.code : "http_error";
      throw new PadApiError(response.status, code, detail);
    }
    return parsed;
  } finally {
    timeout.clear();
  }
}

/**
 * 拉当日药单，喂给 `initOfflineCache({ loadReminders })`。
 *
 * 只做「形状兜底」不做业务判断：`reminderId` / `scheduledAt` 缺失的行直接丢掉，
 * 否则一条脏数据会让整张当日药单在 IndexedDB 里写不进去（`offline/db.ts` 按主键 put）。
 */
export async function fetchTodayMedicationReminders(): Promise<OfflineReminder[]> {
  const body = (await request("/api/pad/medications/today")) as {
    reminders?: OfflineReminder[];
  } | null;
  const rows = Array.isArray(body?.reminders) ? body.reminders : [];
  return rows.filter(
    (row): row is OfflineReminder =>
      typeof row?.reminderId === "string" && typeof row?.scheduledAt === "string",
  );
}

/**
 * 长者点「已服用」（FR-UI-03）。
 *
 * 端侧**不做乐观终态**：请求失败就抛出去，由卡片保持 `pending` 让长者重试。
 * 本地先改成 `taken` 会带来两个后果——服务端不知道这次触达已被处理（+5/+10/+15
 * 还会继续打进来），而 `taskDispatch` 又按 `reminderId` 去重，下一次触达在端侧被
 * 永久吞掉，长者看到的是「药提醒再也不出现了」。
 */
export async function confirmMedicationTaken(
  reminderId: string,
): Promise<MedicationAnnouncement | null> {
  const body = (await request(`/api/pad/medications/${reminderId}/confirm`, {
    method: "POST",
  })) as { announcement?: unknown } | null;
  // CON-04：服务端把「已记下」这句话连同音频一起挂在确认响应上；老响应/降级时
  // 该字段可能缺失或为 null，交给调用方退回卡片自带的字幕，**不静默什么都不做**。
  return asMedicationAnnouncement(body?.announcement);
}

/**
 * 长者点「撤销」撤回刚刚那次自报（FR-UI-08）。
 *
 * **这是真撤销**：服务端把事件退回 `waiting_confirm` 并清空终态，后续 +5/+10/+15
 * 的触达会照常再来一次。端侧同样不做乐观回滚——服务端拒收（超过 10 秒窗口、
 * 终态已不是 `taken_reported`、或这条不是本机的提醒）时必须把错误抛给卡片，
 * 否则界面显示「已撤销」而库里仍是「已服用」，长者会以为漏服被记成了已服。
 */
export async function undoMedicationTaken(reminderId: string): Promise<void> {
  await request(`/api/pad/medications/${reminderId}/undo`, { method: "POST" });
}

/**
 * 掉线/恢复上报（FR-MED-08 第②③层）。
 *
 * 端侧**不因失败重试**：`offline/index.ts` 在恢复时会重报一次，这里再叠加一层重试
 * 只会在弱网里堆请求；服务端按 (event_id, attempt_no) 幂等，重报本身是安全的。
 */
export async function postOfflineReport(report: OfflineReport): Promise<void> {
  await request("/api/pad/offline/report", { method: "POST", body: report });
}

/**
 * 卡片已在平板渲染完成（FR-UI-10 / FR-MED-02 的 `card_rendered` 回执）。
 *
 * 为什么必须真发一次：PRD FR-ALT-03「只有客户端回执算送达」——服务端记录的
 * `tts_finished` 只证明音频播完，证明不了长者眼前真有这张卡（TTS 断流、页面白屏时
 * 音频照样能播完）。`MedicationCard` 挂载后调这里，服务端把这一枪的 attempt 补成
 * `card_rendered`；同一 `(event, attempt_no)` 重复上报幂等，重渲染不会多记一枪。
 */
export async function postMedicationRendered(reminderId: string): Promise<void> {
  await request(`/api/pad/medications/${reminderId}/rendered`, { method: "POST" });
}

/** 健康采集后端 `CheckupSessionResponse` 的字段（snake_case 原样，见 `app/api/health.py`）。 */
export interface CheckupSession {
  session_id: string;
  record_date: string;
  /** asking | awaiting_review | completed | ended */
  status: string;
  /** 当前问的字段；复核/终态为空 */
  current_key: string | null;
  /**
   * 当前问句：`{ type, key, label, prompt, attempt, max_attempts }`（`type=review` 是复核闸门）。
   * 服务端 push 的是 dict，本组件只读 `prompt` / `attempt` 两个键。
   */
  prompt: {
    type?: string;
    key?: string;
    label?: string;
    prompt?: string;
    attempt?: number;
    max_attempts?: number;
  } | null;
  /** 本题等待截止（ISO）；`current_key` 重置题时用 */
  expires_at: string | null;
  attempt_no: number;
  answers: Record<string, number>;
  missing: string[];
  active: boolean;
}

export interface CheckupTurn {
  session: CheckupSession;
  record: unknown | null;
}

function asCheckupSession(body: unknown): CheckupSession {
  if (!body || typeof body !== "object") {
    throw new PadApiError(200, "bad_shape", "健康采集返回了无法识别的数据");
  }
  const session = body as Partial<CheckupSession>;
  if (typeof session.status !== "string" || !session.answers) {
    throw new PadApiError(200, "bad_shape", "健康采集返回了无法识别的数据");
  }
  return {
    session_id: String(session.session_id ?? ""),
    record_date: String(session.record_date ?? ""),
    status: session.status,
    current_key: session.current_key ?? null,
    prompt: session.prompt ?? null,
    expires_at: session.expires_at ?? null,
    attempt_no: Number(session.attempt_no ?? 0),
    answers: session.answers,
    missing: Array.isArray(session.missing) ? session.missing : [],
    active: Boolean(session.active),
  };
}

/**
 * 拉当天采集会话（pad 侧只读；会话由服务端 19:00 定时或主动采集创建）。
 *
 * 今天还没有会话时后端回 404（`HealthNotFoundError`），这里**原样抛出**
 * `PadApiError`：调用方要能区分「今天没排采集」和「网络连不上」，
 * 前者是正常状态、后者才需要提示重试。
 */
export async function fetchHealthCheckup(elderId: string): Promise<CheckupSession> {
  return asCheckupSession(
    await request(`/api/health/checkup/${encodeURIComponent(elderId)}`),
  );
}

/**
 * 一次性结束当天采集（pad「结束采集」按钮）。
 *
 * 为什么不逐题「标缺失」再发结束：那是 N+1 次串行往返（最多 6 次，实测按钮要转 2–5 秒），
 * 中途断一次还会留下半个缺失集。缺失集合改由服务端按图的 state 一次算完
 * （`POST /api/health/checkup/{elder_id}/finish`），端侧只发这一个请求。
 */
export async function finishHealthCheckup(elderId: string): Promise<CheckupTurn> {
  const body = (await request(`/api/health/checkup/${encodeURIComponent(elderId)}/finish`, {
    method: "POST",
  })) as { session?: unknown; record?: unknown } | null;
  return {
    session: asCheckupSession(body?.session),
    record: body?.record ?? null,
  };
}

/**
 * 回答当前一问（FR-UI-07 真接线；FR-HLT-01）。
 *
 * `text` 是长者原话，**数值解析在服务端**（`coerce_measurement`）：前端不替服务端
 * 判「这个数算不算血压」，否则同一句话在两端会得到不同结论。返回的是推进后的
 * 会话（下一问 / 复核 / 终态），页面只按 `status` 与 `current_key` 重画。
 */
export async function submitHealthCheckupAnswer(
  elderId: string,
  text: string,
): Promise<CheckupTurn> {
  const body = (await request(`/api/health/checkup/${encodeURIComponent(elderId)}/answer`, {
    method: "POST",
    body: { text },
  })) as { session?: unknown; record?: unknown } | null;
  return {
    session: asCheckupSession(body?.session),
    record: body?.record ?? null,
  };
}

// ---------------------------------------------------------------------------
// 用药播报（W5 预留位）
// ---------------------------------------------------------------------------

/** 用药卡上的三种处置动作：W4 只在卡片侧触发，W5 用来挑播报话术。 */
export type MedicationAction = "taken" | "refused" | "snoozed";

/**
 * 服务端播报回执，形如 `tts_audio_service.announce_medication(...)` 的返回：
 * `{ action, text, subtitle, audio_url, degraded }`。
 *
 * W4 只留类型与回调位，**不实现 TTS**；W5 接线时把 confirm/undo 响应里的这一段
 * 交给 `RealtimeClient.playReplyAudio(audio_url, ...)` 播放，`degraded` 为真时
 * 只上屏 `subtitle`（降级不静默）。
 */
export interface MedicationAnnouncement {
  action: MedicationAction;
  text: string;
  subtitle: string;
  audio_url: string | null;
  degraded: boolean;
}

/**
 * 把服务端返回的 `announcement` 收窄成 `MedicationAnnouncement`；**形状不对就当没给**。
 *
 * 为什么宽松：这条链的语义是「确认已经成功了，能不能顺便说一句」。字段缺失只会让
 * 前端退回卡片自带字幕（`onSubtitle`），绝不因此抛错——真抛了，长者的「已服用」会
 * 在界面上变成「刚才没传上去」，把一件已经落库的事报成失败。
 */
export function asMedicationAnnouncement(value: unknown): MedicationAnnouncement | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  // 服务端枚举是 `taken` / `snooze`（`MEDICATION_ANNOUNCE_ACTIONS`），端侧语义是
  // `taken` / `refused` / `snoozed`；差一个字母就地归一，别让类型判断静默掉一句播报。
  const action = raw.action === "snooze" ? "snoozed" : raw.action;
  if (action !== "taken" && action !== "refused" && action !== "snoozed") return null;
  const subtitle = typeof raw.subtitle === "string" ? raw.subtitle : "";
  if (!subtitle) return null;
  return {
    action,
    text: typeof raw.text === "string" ? raw.text : subtitle,
    subtitle,
    audio_url: typeof raw.audio_url === "string" && raw.audio_url ? raw.audio_url : null,
    degraded: raw.degraded === true,
  };
}
