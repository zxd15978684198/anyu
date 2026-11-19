/**
 * 迭代 3 模块间接口契约的 A4 侧类型定义（v1.0 冻结版）。
 * 使用规则：下游只准引用本文件的契约类型，不得自行定义接口结构；
 * 契约改动必须先改冻结版契约（群通告 + 冻结人留痕），再同步此处。
 */

/* ===================== C2 · 信令 WS 消息契约（A2 冻结，A4 消费） =====================
 * 传输：WS /ws/signal，文本帧 JSON，每消息一个对象。
 * 语义：连接后 3000ms 内必须首发 auth（v1.7 冻结版；原 300ms 在刷新/主线程长任务下会误杀，见
 * `app/audio/signaling.py:WS_AUTH_TIMEOUT_MS`）；auth 前的任何其他消息服务端直接 4401 关闭；
 * 45s 无任何帧 → 服务端判死清理。 */

/** WS 关闭码：认证失败/超时（服务端主动关闭时使用） */
export const WS_CLOSE_UNAUTHORIZED = 4401;

/**
 * WS 关闭码：同设备「踢旧留新」——服务端已确认这条连接被同一 device_id 的
 * 新连接接管（`app/audio/signaling.py:73 WS_CLOSE_SUPERSEDED`）。
 *
 * **不得按异常断开重连**：两端都是合法客户端时（老人平板 + 调试页、或同一台
 * 设备开了两个标签页），重连会反过来顶掉对方，形成 1.5 秒一次的互相踢人循环，
 * 表现为「开场白反复重播 / 频繁丢失、日志刷屏、约 1/10 的登录拿不到播放轨」。
 * 客户端收到本码必须停下、把「本设备已在别处接管」告诉上层，由人决定怎么办。
 */
export const WS_CLOSE_SUPERSEDED = 4002;

/** 信令 WS 路径（dev 经 Vite 代理 /ws → :8000） */
export const SIGNAL_WS_PATH = "/ws/signal";

/* ---- C→S 客户端发送 ---- */

/** 连接后 3000ms 内必须首发（v1.7 冻结版）；token 为设备会话 access_token（E4 签发），不进 URL */
export interface AuthMsg {
  type: "auth";
  token: string;
}

/** WebRTC offer（客户端发起） */
export interface OfferMsg {
  type: "offer";
  sdp: string;
}

/** ICE 候选，双向 */
export interface IceMsg {
  type: "ice";
  /** ICE candidate JSON 字符串 */
  candidate: string;
}

/** 心跳，20s 周期 */
export interface PingMsg {
  type: "ping";
}

/** TEN-VAD speechstart；preRoll 由 pad 经 WebRTC 轨回放补发（A2 裁决：服务端不回溯轨道） */
export interface AsrStartMsg {
  type: "asr_start";
}

/** TEN-VAD speechend（0.8s 静音） */
export interface AsrEndMsg {
  type: "asr_end";
}

export type ClientSignal =
  | AuthMsg
  | OfferMsg
  | AnswerMsg // 迭代 7 · 反向外呼：pad 答服务端的 offer（复用既有 answer 类型）
  | IceMsg
  | PingMsg
  | AsrStartMsg
  | AsrEndMsg
  | IdentityConfirmMsg // C5 追加
  | ClientDiagMsg; // C5 追加

/* ---- S→C 服务端下发 ---- */

export interface AuthOkMsg {
  type: "auth_ok";
  /** 展示用；业务归属一律服务端裁决 */
  elder_id: string | null;
  room_id: string;
}

export interface AuthFailMsg {
  type: "auth_fail";
  code: "timeout" | "invalid" | "expired";
}

export interface AnswerMsg {
  type: "answer";
  sdp: string;
}

/**
 * 服务端下发的 offer（迭代 7 · S-D7-3 反向外呼）：与 C→S 的 `OfferMsg` 同形，方向相反。
 * 复用既有 message type、不新增类型；谁当 offerer 由服务端决定（角色反转）。
 */
export interface ServerOfferMsg {
  type: "offer";
  sdp: string;
}

/** ICE 候选（C2 契约标注「双向」：服务端也会下发自己的候选） */
export interface ServerIceMsg {
  type: "ice";
  /** ICE candidate JSON 字符串 */
  candidate: string;
}

export interface PongMsg {
  type: "pong";
}

/** ASR 中间结果（字幕预显，业务不依赖） */
export interface AsrPartialMsg {
  type: "asr_partial";
  text: string;
}

/** ASR 终稿（路由层/业务处理的唯一触发事件） */
export interface AsrFinalMsg {
  type: "asr_final";
  text: string;
}

/** 数字人回复文本（大字幕渲染；打断分两层已定案：前端 speechstart 同一刻立即停播，
 *  服务端 A3 收到 asr_start 停写轨清缓冲——前端不等服务端） */
export interface TtsTextMsg {
  type: "tts_text";
  text: string;
}

/** 逐句字幕的一段：相对「本段音频第 0 帧」的偏移，不是墙钟时间。
 *  `start_ms`/`duration_ms` 由服务端按**播放流落位**换算（20ms/帧，含句间静音），
 *  所以只要知道「第一段真的出声」的那一刻，整条字幕时间轴就确定了。 */
export interface TtsSegment {
  text: string;
  start_ms: number;
  duration_ms: number;
  /**
   * 句内逐字坐标：每个字符的**结束偏移**（毫秒，相对句首），长度 = `text` 的码点数。
   * 由服务端按 PCM 能量包络算出（停顿不占字），标点跟随前一个可朗读字符，
   * 末位恒等于 `duration_ms`。打字机与口型共用这一份坐标，不再各自计时。
   *
   * 可选：旧服务端 / 估算时长的开场白 / 纯静音降级不带它，前端回退匀速比例法。
   */
  char_ms?: number[];
}

/** 逐句字幕计划（方案 B）：服务端每合成完一句就整份重发一次，
 *  前端收到后按播放锚点推进，替代「整段文本一次性上屏」。
 *  兜底：没有这帧（旧服务端 / 降级轮次）就沿用 `tts_text` 的整段渲染。 */
export interface TtsSegmentsMsg {
  type: "tts_segments";
  segments: TtsSegment[];
  /**
   * 这份计划**没有音频**（TTS 不可用，服务端按字数估算时长）。
   * 缺省 / false = 计划背后有真实音频：前端把时间轴停在 0，等首帧出声再逐字，
   * 避免「计划先到、建轨空档数秒」时字幕领先声音（问候语三线同步，2026-11-11）。
   * true = 永远不会有声音：前端立刻用墙钟按估算节奏逐字，不等能量。
   */
  estimated?: boolean;
}

/** 服务端降级/异常通知；message 为已脱敏的长者可读话术 */
export interface ErrorMsg {
  type: "error";
  code:
    | "asr_unavailable"
    | "llm_timeout"
    | "tool_breaker"
    // 迭代 5 P0-2：TTS 首包超时。正文另由 tts_segments / tts_text 承载，本帧只给一句降级话术
    // （C2 追加枚举，2026-11-11 补登记，见 契约记录）
    | "tts_unavailable"
    | "internal";
  message: string;
}

/**
 * 媒体控制（FR-CON-03）：规则层判定出的点播动作，经 WS 下发给长者端。
 *
 * 为什么由服务端下发而不是前端自己听关键词：动作已经由路由层判定过
 * （`decision.intent`），前端再判一遍就是第二套规则，两边必然漂移；而且「停」「大声点」
 * 这类指令要先于回复生效，必须和这一轮回复同一条时序通道。
 *
 * `seq` 是**去重键**：同一台设备上逐条自增。连说两句「换一段」时两条指令可能跨两轮
 * 到达，前端只接受比上次更大的 `seq`，否则重放会连着切两段。
 *
 * `action = "play"` 附带 `kind` / `genre`：服务端只给「放哪个剧种」，真正挑哪一段仍走
 * `POST /api/media/pick` —— 选段口径（去重最近 5 条、版权白名单）只有服务端一处。
 */
export interface MediaControlMsg {
  type: "media_control";
  /**
   * 服务端可下发的播放动作。`pause` / `resume` 是**播放器本地动作**
   * （FR-CON-03 后半，2026-11-12）：pad 暂停时记 `currentTime`，resume 时 seek 回原位，
   * 不新建历史、不调 HTTP。
   */
  action: "next" | "stop" | "pause" | "resume" | "volume_up" | "volume_down" | "play";
  seq: number;
  kind?: "opera" | "movie";
  /** 中文剧种名（「越剧」/「京剧」），直接传给 `pick`，前端不做翻译 */
  genre?: string;
}

export type ServerSignal =
  | AuthOkMsg
  | AuthFailMsg
  | AnswerMsg
  | ServerOfferMsg // 迭代 7 · 反向外呼（S-D7-3，复用 offer 类型、方向相反）
  | ServerIceMsg
  | PongMsg
  | AsrPartialMsg
  | AsrFinalMsg
  | TtsTextMsg
  | TtsSegmentsMsg
  | MediaControlMsg // FR-CON-03：规则层点播动作
  | ErrorMsg
  | IdentityAskMsg // C5 追加
  | IdentityPendingMsg // D 方案追加（2026-11-10）
  | IdentityResultMsg; // C5 追加

export type ServerSignalType = ServerSignal["type"];

const SERVER_SIGNAL_TYPES: readonly ServerSignalType[] = [
  "auth_ok",
  "auth_fail",
  "answer",
  "offer", // 迭代 7 · 反向外呼：服务端当 offerer 时下发
  "ice",
  "pong",
  "asr_partial",
  "asr_final",
  "tts_text",
  "tts_segments",
  "media_control", // FR-CON-03
  "error",
  "identity_ask", // C5 追加
  "identity_pending", // D 方案追加（2026-11-10）
  "identity_result", // C5 追加
];

/**
 * 解析服务端文本帧：非 JSON / type 未知 → 返回 null（调用方记 trace 后丢弃，不白屏）。
 * 仅校验判别字段，字段级完整性由 TS 类型在编译期约束、由对端契约保证。
 */
export function parseServerSignal(raw: string): ServerSignal | null {
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof obj !== "object" || obj === null) return null;
  const t = (obj as { type?: unknown }).type;
  if (typeof t !== "string" || !SERVER_SIGNAL_TYPES.includes(t as ServerSignalType)) {
    return null;
  }
  return obj as ServerSignal;
}

/* ===================== C5 · 身份级联 WS 消息契约 v2（A2 定稿，A4 消费） =====================
 * 来源：契约记录 C5 节，字段名逐字照抄，不得自创。
 * 传输：复用 WS /ws/signal（C2 信封不变，文本帧 JSON）；下列为 C2 消息表的追加类型，
 * 原类型一律不动。级联语义：首轮强制声纹校验（cosine ≥0.55 → identity_result voice），
 * 会话内静默复检仅身份变化时干预。 */

/** identity_ask 的触发原因：声纹低置信 / 声纹服务不可用 */
export type IdentityAskReason = "low_confidence" | "vp_unavailable";

/** 候选长者条目；恒 1 位（设备绑定长者 1v1），confirm 的 elder_id 必须等于 candidates[0].elder_id */
export interface IdentityAskCandidate {
  elder_id: string;
  display_name: string;
}

/** S→C，低置信/服务不可用时主动确认 */
export interface IdentityAskMsg {
  type: "identity_ask";
  /** 本次确认的唯一 id；confirm 必须回带 */
  ask_id: string;
  reason: IdentityAskReason;
  candidates: IdentityAskCandidate[];
  /** 恒 30；计时起点=服务端发出本消息时刻，超时由服务端置 generic 并下发 identity_result */
  timeout_s: number;
}

/** D 方案（2026-11-10）：登录时**不索取确认**，身份等老人开口后再判。
 *  收到本帧表示服务端已进入「等待首句音频」状态；前端只需把身份胶囊显示为
 *  「待确认」，不弹确认层、不打断开场白。`reason` 目前恒为 `await_wake`。 */
export interface IdentityPendingMsg {
  type: "identity_pending";
  reason: "await_wake";
}

/** C→S，pad 大按钮点选；ask_id 不匹配 / 已定身份后重复 confirm → 服务端忽略 + warn（不回 error） */export interface IdentityConfirmMsg {
  type: "identity_confirm";
  ask_id: string;
  /** 必须等于 candidates[0].elder_id；否则服务端按未确认处理 */
  elder_id: string;
}

/** 最终身份模式：voice=声纹通过 ｜ confirm=大按钮确认 ｜ generic=通用模式（不读记忆/不写健康/不确认用药，FR-VPR-03） */
export type IdentityResultSource = "voice" | "confirm" | "generic";

/** S→C，告知最终身份模式；前端据此显示/隐藏身份横幅（generic → 警示教育条） */
export interface IdentityResultMsg {
  type: "identity_result";
  source: IdentityResultSource;
  /** generic 时为 null */
  elder_id: string | null;
}

/** client_diag 的链路阶段（3D/WebRTC/VAD/ASR + 首句播放诊断位） */
export type ClientDiagStage =
  | "vrm_load"
  /**
   * 「屏幕上已经有形象了」——含 FR-3D-02 的静态头像降级（没 WebGL / VRM 加载失败 /
   * 后端没配形象）。服务端据此放行开场白（2026-11-10：先出人、再说话），
   * 所以它比 `vrm_load` 宽：`vrm_load ok=false` 之后仍可能来一条 `avatar_ready ok=true`。
   */
  | "avatar_ready"
  | "webrtc"
  | "vad"
  | "asr"
  | "playback";

/** C→S，仅日志（链路诊断），服务端不回执；error 已脱敏 */
export interface ClientDiagMsg {
  type: "client_diag";
  stage: ClientDiagStage;
  ok: boolean;
  /** 已脱敏 */
  error: string;
  /** 仅 playback：tts_text 到首段音频出声的毫秒数（服务端截到 30s 上限） */
  elapsed_ms?: number;
}

/* ===================== C3 · 降级 REST 契约（A5 冻结，A4 消费） =====================
 * POST /api/audio/utterance，multipart/form-data，Authorization: Bearer <device access_token>。
 * audio: 16kHz/16bit/单声道 WAV（按住说话整段，含 preRoll）；duration_ms 客户端计时、服务端仅入 trace。
 * 错误：401 token 失效 ｜ 413 超长音频（>30s 拒收）｜ 503 ASR 不可用（body 携带降级话术）。
 * 注意：reply 形状依赖 TTS 决议（audio_url vs 直返 PCM），契约会若改此处必须同步。 */

export interface UtteranceReply {
  text: string;
  /** 服务端合成音频的本地可取地址；无 TTS 时为 null */
  audio_url: string | null;
}

export interface UtteranceResponse {
  /** ASR 终稿 */
  text: string;
  reply: UtteranceReply;
}

export type UtteranceErrorCode = "unauthorized" | "payload_too_large" | "asr_unavailable";
