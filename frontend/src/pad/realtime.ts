/**
 * T-E 实时链路编排器：信令 WS + 麦克风采集 + 端侧 TEN-VAD + WebRTC 收发。
 * 主链路口径（adhoc §3/§4 + A2 裁决 2026-11-05）：麦克风 16k 帧喂端侧 VAD；
 * speechstart → 发 asr_start → worklet 立即把 preRoll 环形缓冲经轨回放补发
 * （服务端不回溯轨道，asr_start 后服务端才开始消费识别流，句首 600ms 靠补发）；
 * speechend → 发 asr_end；WebRTC 音频轨连续上行。主线程 preRoll 环形缓冲
 * 仅用于 C3 降级 WAV，与轨上回放相互独立。
 * 打断（A2 裁决）：前端本地 speechstart 同一刻立即停播，不等服务端；
 * 服务端 A3 收到 asr_start 停写轨清缓冲（A3 媒体层未落码前联调不会中断）。
 * TEN-VAD 加载失败 → vadFallback：不发 asr_start/end，由服务端 webrtcvad 判定（字幕连续）。
 */
import type { MicState, PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import type {
  ClientDiagMsg,
  ClientDiagStage,
  IdentityAskReason,
  IdentityResultSource,
  MediaControlMsg,
  ServerSignal,
  TtsSegment,
} from "./protocol";
import { MicCapture } from "./audio/capture";
import { TenVad } from "./audio/vad";
import { loadIceServers } from "./ice";
import {
  pickSegmentIndex,
  publishSubtitlePlanPresence,
  publishSubtitleProgress,
  segmentProgressAt,
} from "./subtitlePlan";
import { SignalSocket } from "./signal-socket";

/**
 * 迭代 5 口型：驱动所需的最小控制器面（只取两个 setter）。
 * 故意**不从** `avatar/VrmAvatar` import 类型——`realtime.ts` 是 App 的静态依赖，
 * 静态 import 那个模块会把 three + three-vrm（约 750KB）拖进 pad 主包，
 * 而 AvatarStage 走 `lazy()` 正是为了避开这件事。`AvatarController` 结构上满足本接口。
 * 表情细节不透出本接口：aa×0.85 / ih×0.12 的分配与降级收在 VrmAvatar 内部（§5.2 双表情）。
 */
export interface LipSyncTarget {
  setMouthOpen(value: number): void;
  setSpeaking(on: boolean): void;
}

/* ---------- 迭代 5 · 口型（W3；v1.3 §5.2：移植参考实现 engine.js，取代 D1 自研口径）----------
 * PRD FR-3D-01 的验证口径是「播放任一 TTS 可看到口型开合」，技术设计文档 line 147/191
 * 明确口型 = TTS 音频能量近似，不做 viseme。v1.3 权威口径 = 移植运行中系统
 * 本人既有工程 asr 的同款口型引擎（engine.js，约 1300 行、已在运行中系统验证）。常数全部**沿用其取值**（R8，
 * 来源行号逐条标注下方）；观察类调整只写报告给建议值，不进代码。
 * v1.1→v1.3 差异（将被取代）：lerp 0.35 / target=rms*4 / 静音 rms<0.02 持续 1200ms /
 * analyser 下游接 gain0→destination 汇点 / 只写 aa 表情。 */
/** 静噪门限：归一化 RMS 低于它视为底噪 → target 为 0（engine.js:1167 内联字面值 0.012） */
const MOUTH_NOISE_GATE = 0.012;
/** 能量增益：target = clamp((rms − NOISE_GATE) × GAIN, 0, 1)（engine.js:1167 内联字面值 6.5） */
const MOUTH_GAIN = 6.5;
/** 起音跟随速率：mouth += (target − mouth) × min(1, dt×18)（engine.js:1170 内联字面值 18；
 *  engine.js:28 声明的 const MOUTH_ATTACK = 1.0 从未被引用属死常量，不落地） */
const MOUTH_ATTACK_RATE = 18;
/** 收音释放速率：mouth = max(target, mouth − dt×RELEASE)（engine.js:29 的 MOUTH_RELEASE） */
const MOUTH_RELEASE_RATE = 3.6;
/** 说话判定：mouthLevel 高于此值视为有声 → silenceTime 归零（engine.js:1176 内联字面值 0.06） */
const MOUTH_SPEAK_THRESHOLD = 0.06;
/** 静音保持：无声持续 ≥0.45s 才判「这一句说完了」——口型归零 + 播放胶囊转待机（不是关窗） */
const MOUTH_SILENCE_HOLD = 0.45;

/**
 * 静音兜底：连续无声 ≥3s 才**真正关窗**（停 rAF）。
 *
 * 为什么不在 0.45s 就关：一句回复是**逐句**入队播的（`app/audio/tts_sentences.py` 一句合成完就播），
 * 句间停顿与「整段说完」在能量上不可区分。0.45s 就关窗会让后半段语音全程不动嘴，
 * 且状态永远停在「正在说」——2026-11-10 产品负责人实测指出这两点。
 * 3s 远大于句间停顿（实测 <1s），又能在整段播完后停下 rAF，避免空转耗电。
 */
const PLAYBACK_IDLE_STOP = 3;
/** 计划时长到点后的宽限（ms）：留一点尾巴给自己播放器排空，但不再等「静音 3s」。 */
const PLAYBACK_END_GRACE_MS = 500;
/**
 * 播放态卡死的硬上限（秒）：连续这么久**一次能量都没读到**，强制收窗并回「待机」。
 *
 * 为什么在「计划时长到点」之外还要这一层：胶囊复位此前完全依赖
 * ①能量判定（要求平滑能量能掉到阈值以下）②`tts_segments` 计划时长（要求服务端发了计划）。
 * 这两条都断的时候——远端轨空转噪声把能量吊在阈值上、或这一轮压根没计划——胶囊就会永远停在
 * 「播放·正在说」，数字人也被 `setSpeaking(true)` 按在说话动作上（2026-11-11 产品负责人复述）。
 * 这一层是兜底：正常轮次句间停顿 <1s、整段播完 <15s，走不到这里。
 */
const PLAYBACK_STUCK_TIMEOUT = 12;
/**
 * 心跳判据要求「连续多少帧有能量」才刷新 `lipLastProgressAt`（60fps 下 3 帧≈50ms）。
 *
 * 为什么不是单帧：复现实测（`scripts/audit-watchdog-stale.mjs`，R2/R7）单帧 rms≥0.43 的
 * 孤立尖峰就能把平滑能量顶过说话门限，同时刷新心跳——于是静音攒不满 3s（尖峰间隔 <3s）、
 * 心跳又一直有——三条收口出口全堵死。要求连续 3 帧，孤立尖峰就只是噪声。
 */
const LIP_PROGRESS_STREAK_REQUIRED = 3;
/**
 * 「文本已到、音频还没到」的等待上限（秒）。
 *
 * 开场白/回复的 `tts_text` 在服务端 push 的**同一毫秒**就到浏览器，音频却要等 TTS 合成
 * （edge-tts 首句实测 3–5s）+ 建轨 + 首包，两者差好几秒。这段空档里 readRms 恒 0，
 * 若照常按静音计时判停，口型和「正在说」都会在音频真正出声之前就收掉 —— 表现是
 * 「有声音但不动嘴、胶囊一直待机」（2026-11-10 产品负责人实测）。
 * 所以等待态单独计时，不参与静音判停；超过这个上限说明 TTS/轨道真出问题了，才收窗。
 *
 * 2026-11-11 从 8s 抬到 20s：espeak 兜底一次合成实测 6.3s，加上建轨/首包，8s 会在
 * 音频到达**前一瞬间**关窗；窗一关程序计划被清掉，字幕组件立刻退回「整段上屏」——
 * 用户看到的就是「打字机效果没了」。
 */
const PLAYBACK_WAIT_AUDIO_TIMEOUT = 20;

/**
 * 「这一段播完了」的显示阈值（秒）：静音超过它才把播放胶囊从「正在说」翻回「待机」。
 *
 * 为什么不复用 MOUTH_SILENCE_HOLD(0.45s)：回复是逐句合成逐句播的，句间停顿实测 0.4–0.9s，
 * 0.45s 翻状态会让胶囊在整段播报中途闪一下「待机」（实测抓到过一次）。嘴照旧按 0.45s
 * 收（口型要跟声音走），只是「正在说」这枚标签多等一会儿，避免闪烁。
 */
const PLAYBACK_STATUS_HOLD = 1.0;
/**
 * `tts_text` 到 `tts_segments` 之间等多久才放弃「按句逐字」、改回整段上屏（毫秒）。
 *
 * 为什么需要兜底：正常回复一定会跟一条计划；但 TTS 全挂的降级路径（`reply_stub` 里
 * 那条只有 `tts_text` 的分支）、以及 mock TTS 的单句场景，可能永远没有计划。
 * 没有兜底就会「只有声音、字幕一直空着」——比不逐字更糟。
 *
 * 为什么不是 0：等待期间字幕是空的，长者会觉得「点了没反应」。
 * 为什么从 900ms 调到 8000ms（2026-11-10 实测）：`tts_segments` 要等服务端**合成完第一句**
 * 才发（Edge TTS 冷启 1~4s），900ms 会在计划到达前先把整段文字打上去，随后计划一到又从
 * 第 0 字重打——长者看到「整句闪一下、缩回去、再逐字出来」。兜底只需要保证「有声音时不会
 * 一直没有字」，8s 足够覆盖冷启，且真正没有计划的路径在收窗时也会补上（见 closeSpeakingWindow）。
 */
const REPLY_TEXT_FALLBACK_MS = 20000;
/** AnalyserNode 时域窗 1024 点（engine.js:1123；≈21ms@48k，够跟口型、不把音节平均掉） */
const MOUTH_FFT_SIZE = 1024;
/** Analyser 自体平滑 0.5（engine.js:1124；v1.1 的「0 + rAF lerp」口径一并废弃） */
const MOUTH_SMOOTHING = 0.5;
/**
 * 只下行会话建链失败后的重试间隔（毫秒）。
 *
 * 2026-11-10 实测：约每 10 次登录有 1 次服务端**连 offer 都没收到**（那条 trace 里
 * 只有 `ws_authed`，没有任何 `media signal received`），开场白随即被判
 * `track_not_ready` 丢掉。失败点多是 ICE 配置拉取 / setLocalDescription 的瞬时错误，
 * 退避一次再建即可覆盖。
 */
const DOWNLINK_RETRY_DELAY_MS = 800;

/** `audio/playout-clock-worklet.js` 每 ~20ms 上报一次的播放进度与能量。 */
interface PlayoutClockReport {
  /** 已处理采样数（单调，跨轮不复位；轮次零点由主线程做差） */
  samples: number;
  /** 本上报窗口内的 RMS 峰值 */
  rms: number;
  /** 峰值所在的采样序号：定「首帧出声」零点用，精度 2.67ms */
  peakSamples: number;
  sampleRate: number;
}

/** C5 identity_ask 上抛 UI 的载荷（候选恒 1 位，故平铺为单值） */
export interface IdentityAskPayload {
  /** 回带用：confirm 必须带同一个 ask_id */
  askId: string;
  reason: IdentityAskReason;
  /** = candidates[0].elder_id */
  elderId: string;
  /** = candidates[0].display_name */
  displayName: string;
  /** 恒 30；计时由服务端负责，前端不自行兜底 */
  timeoutS: number;
}

/** C5 identity_result 上抛 UI 的载荷 */
export interface IdentityResultPayload {
  source: IdentityResultSource;
  /** generic 时为 null */
  elderId: string | null;
}

/** 护工呼入（FR-UI-10）：服务端当 offerer，pad 在待机态收到后先响铃，确认后才接 */
export interface IncomingCall {
  /** 服务端下发的 offer SDP（接听时原样喂给 setRemoteDescription） */
  sdp: string;
}

/** 响铃收尾原因：超时 / 长者拒接 / 被新的一通替换 */
export type IncomingCallEndedReason = "timeout" | "declined" | "replaced";

/** 外呼通话收尾原因：长者挂断 / 被新的一通替换 / 建连失败 */
export type OutboundCallEndedReason = "hung_up" | "replaced" | "failed";

/** 响铃超时：与身份询问同量级（服务端 30s 计时口径） */
const RING_TIMEOUT_MS = 30_000;

/** 构造期可调项（只为用例能把响铃超时压到毫秒级，生产不传） */
export interface RealtimeOptions {
  ringTimeoutMs?: number;
  /**
   * FR-UI-10（P0）：服务端主动外呼时**自动接听**，不等长者点接听。
   *
   * 只建下行：不调 getUserMedia、不加本地音轨，answer 天然 recvonly —— 这样既满足
   * 「数字人主动开口播报」，又不违反 FR-UI-11「未命中唤醒词时原始音频不出端」。
   * 唤醒词命中后由 App 调 `acceptIncomingCall()` 升级为带麦全双工。
   *
   * 默认 false：待机外呼仍是「响铃 + 长者确认」，既有行为与用例语义不变。
   */
  autoAnswerOutbound?: boolean;
}

export interface RealtimeCallbacks {
  onStatus: (status: PadStatus) => void;
  /**
   * A6 / R-语音-01/02：麦克风可用性变化。`unavailable` = 首发开麦失败 / 拔麦重开失败；
   * `ok` = 开麦成功或退回待机（待机本来就不开麦，不算故障）。
   *
   * 只报**可用性**，不报原因：三种失败（权限拒绝 / 无设备 / 被占用）的恢复动作都是
   * 「看一眼插头或权限，再按重试」，屏幕文案不需要按原因分叉；原始错误仍走 `onSubtitle`。
   */
  onMicState?: (state: MicState) => void;
  /**
   * `local=true` 表示这条字幕来自**本机正在播的那段回复**（打字轮的 `<audio>`），
   * 不是服务端推来的。上层拦「迟到开场白」时只能拦 `local=false` 的那些——
   * 打字轮自己的逐句时间轴也走这里，一起拦掉会让长者盯着自己那句问题看整整数秒
   * （2026-11-11 CDP 实测：声音已经在答第三句，字幕还停在「今天天气怎么样」）。
   */
  onSubtitle: (text: string, source: SubtitleSource, local?: boolean) => void;
  onAuthFail: () => void;
  /** 连续 5 次自动重连失败：上层出「重新连接」按钮，用户点击后调 retry() */
  onNeedManualRetry: () => void;
  /** C5：服务端下发 identity_ask → 上层展示大按钮确认层 */
  onIdentityAsk?: (ask: IdentityAskPayload) => void;
  /** C5：服务端下发 identity_result → 上层显示/收起身份横幅；generic 显示警示教育条 */
  onIdentityResult?: (result: IdentityResultPayload) => void;
  /**
   * C5：确认层作废（WS 断开 / 重连 / 连续重连失败）。
   * 服务端在连接收尾的 finally 里会取消 30s 计时器且**不再下发** identity_result，
   * 前端若不清理，确认层会永久盖住页面 → 上层须收起 identityAsk。
   */
  onIdentityCancelled?: () => void;
  /**
   * FR-UI-10：待机态收到护工呼入（服务端 offer）。
   * 此时 pad **没有** PeerConnection、**没有**麦克风采集 —— 上层据此出振铃提示，
   * 长者确认后调 `acceptIncomingCall()` 才建媒体。
   */
  onIncomingCall?: (call: IncomingCall) => void;
  /** 响铃收尾（超时/拒接/被替换）：上层收起振铃，恢复待机态 */
  onIncomingCallEnded?: (reason: IncomingCallEndedReason) => void;
  /**
   * FR-UI-10：外呼**自动接听**成功、下行通道已建（`autoAnswerOutbound` 为 true 时才回调）。
   * 上层据此出「数字人在跟您说话」横幅 + 挂断入口；此时**没有**麦克风采集，
   * 唤醒词命中后再调 `acceptIncomingCall()` 才升级为全双工。
   */
  onOutboundCall?: () => void;
  /** 外呼通话收尾：挂断 / 被替换 / 建连失败，上层收起「通话中」横幅 */
  onOutboundCallEnded?: (reason: OutboundCallEndedReason) => void;
  /**
   * FR-CON-03：规则层判定的点播动作（换一段 / 停 / 大声点 / 小声点 / 放某剧种）。
   *
   * 上层**必须自己按 `seq` 去重**：这里只做协议解析与形状校验，不替上层记状态
   * （一个客户端可能同时挂着媒体面板与待机屏，谁负责哪些动作是 App 的决定）。
   */
  onMediaControl?: (command: MediaControlMsg) => void;
  /** TTS 下行播放元素（登录点击后已有用户手势，autoplay 可用） */
  audioEl: HTMLAudioElement;
}

export class RealtimeClient {
  private socket: SignalSocket | null = null;
  private mic: MicCapture | null = null;
  private vad: TenVad | null = null;
  private pc: RTCPeerConnection | null = null;
  private unbindDeviceChange: (() => void) | null = null;
  private starting = false;
  /** TTS 是否在播（打断判定用；tts_text 置位，打断/新一轮 partial 复位） */
  private ttsPlaying = false;
  /**
   * FR-UI-07「再说一遍」：最近一次**可重放**的 TTS（只有元素路径的 REST 音频有 URL）。
   *
   * 远端 WebRTC 那一路声音是实时流，没有 URL、停掉就没了，重放不了；所以只记
   * `playReplyAudio` 拿到的那一段。新一轮远端播报（`tts_text`）会把它标记作废——
   * 否则「再说一遍」会把一段**更早**的话再念一遍，长者听到的和他刚听过的不一样。
   */
  private lastSpeech: { url: string; durationMs?: number; segments: TtsSegment[] } | null =
    null;
  /** 待确认的 ask_id：断线时据此判定「确认层需要作废」，identity_result 到达后清空 */
  private pendingAskId: string | null = null;
  /**
   * 会话代次（P1-5）：`auth_ok` 与 `stop()` 各 +1；`setupSession` 每个 await 后校验。
   * getUserMedia 可能慢于 1s 重连退避，两次 setupSession 交错时只有最新一代能落成，
   * 旧代必须自行释放自己拿到的 stream/AudioContext（否则麦克风常亮、worklet 双喂 VAD）。
   */
  private generation = 0;
  /** WS 未 OPEN 时最后一条未送达的诊断（P2-6）：ready/auth_ok 后补发一次即清空 */
  private pendingDiag: ClientDiagMsg | null = null;
  /**
   * N62b：媒体开关（`awake` 的媒体面）。**默认 true**，保持「连上即建媒体」的既有行为；
   * App 在唤醒词命中/失焦时显式 `setAwake()` 同步。false = 待机态：
   * 只保留信令 WS（能收 offer、能响铃），不建 PeerConnection、不碰麦克风。
   */
  private mediaEnabled = true;
  /**
   * A6：当前麦克风可用性。与 `mediaEnabled` 分开：后者是「该不该开麦」（待机/唤醒），
   * 前者是「开没开成」。只有后者为 true 时前者才可能因故障变成 unavailable。
   */
  private micState: MicState = "ok";
  /** 待机态收到的 offer（S-D7-3 反向外呼）：响铃期间先存着，长者接听时才交给 setRemoteDescription */
  private pendingOffer: string | null = null;
  /** 响铃计时器：超时收铃；服务端 offerer 等不到 answer 会自行降级文字播报 */
  private ringTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly ringTimeoutMs: number;
  /**
   * FR-UI-10：待机态收到外呼**自动接听**（下行 only）。默认 false = 响铃等长者确认。
   * App 侧传 true；自动接听期间麦克风一次都不开（FR-UI-11）。
   */
  private readonly autoAnswerOutbound: boolean;
  /** 自动接听建立的下行会话是否仍在通话中（长者挂断/被替换/失败时收尾并上抛 onOutboundCallEnded） */
  private outboundCallActive = false;

  /* ---------- 迭代 5 口型（v1.3：移植 engine.js 口径）---------- */
  /** AvatarStage 的 onReady 交上来的控制器；未就绪 / 降级 / 卸载时恒 null */
  private avatar: LipSyncTarget | null = null;
  /**
   * 共享音频图，整个 client 生命周期只建一次：`<audio>` 元素一旦绑过
   * MediaElementAudioSourceNode 就不能再绑第二次（会抛 InvalidStateError），
   * 所以重连（auth_ok → teardown → setup）**不重建**它，只在 stop() 里关。
   */
  private audioCtx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  /** `getByteTimeDomainData` 只接受非共享缓冲的 Uint8Array（TS 5.7+ 泛型化后的口径） */
  private lipBuf: Uint8Array<ArrayBuffer> | null = null;
  /** WebRTC 远端轨能量源（ontrack 建，teardown 断） */
  private streamSource: MediaStreamAudioSourceNode | null = null;
  /**
   * P1-7：最近一次下行轨流。既做 `ensureTrackAnalyser` 的幂等依据（已是这一段流就不再
   * 重建 MediaStreamAudioSourceNode），也做兜底来源（`<audio>.srcObject` 被清空时仍能
   * 把分析器接上）。detachTrackAnalyser 一并清空——换会话就是换流。
   */
  private trackStream: MediaStream | null = null;
  /** `<audio>` 播 URL/Blob 音源时的能量源（tts_text 按需建，只建一次） */
  private elementSource: MediaElementAudioSourceNode | null = null;
  /**
   * 播放侧唯一时钟（`audio/playout-clock-worklet.js`）。旁路观测，不参与出声：
   * `source → clockNode → clockSink(gain 0) → destination`，声音仍由 `<audio>` 元素自己出。
   * 建不出来（老内核 / 模块加载失败）就整条退回 analyser + 墙钟，功能不减、精度下降。
   */
  private clockNode: AudioWorkletNode | null = null;
  private clockSink: GainNode | null = null;
  /** addModule 只许跑一次；失败也记住，不每帧重试 */
  private clockLoading: Promise<void> | null = null;
  /** worklet 报来的已处理采样数（单调，跨轮不复位） */
  private clockSamples = 0;
  /** worklet 报来的本窗口 RMS 峰值：口型直接用它，不再走 AnalyserNode */
  private clockRms = 0;
  private clockSampleRate = 48_000;
  /** 本轮首次越过静噪门的采样序号（2.67ms 精度，由 worklet 记录而非 rAF 现场取） */
  private clockEnergySamples: number | null = null;
  /** 本轮时间轴零点对应的采样序号；null = 还没定锚，退回墙钟 */
  private clockAnchorSamples: number | null = null;
  private lipRaf = 0;
  /** 口型开合度 0~1（engine.js 的 mouthLevel；aa/ih 权重分配收在 VrmAvatar 内部） */
  private lipMouth = 0;
  /** 上一帧时间戳（performance.now）→ rAF 真实 dt（秒），engine.js:1170/1172 的 dt 语义 */
  private lipLastAt = 0;
  /** 无声累计秒（engine.js:1183 的 silenceTime）：开窗清零，有声帧归零，≥SILENCE_HOLD 判停 */
  private lipSilenceTime = 0;
  /** rAF 判定的说话态（engine.js:1176）：只在阈值边沿跨越时 setSpeaking，同值连调不重复下发 */
  private lipSpeaking = false;
  /**
   * 平滑能量（约 0.55s 窗口，60fps 下系数 0.86/0.14）。
   *
   * 为什么不用逐帧 rms 直接判「在说话」：2026-11-11 实测开场白里每 0.5–3s 出现一次
   * 0.06–0.32 的孤立尖峰（远端轨残留/重采样抖动的短噪声），逐帧判会让「正在说」
   * 被无限续命（静音累计最长只到 0.27s，够不到 1.0s 的收口阈值），胶囊一直卡在
   * 「播放·正在说」，嘴也跟着一跳一跳。用平滑能量判说话态：孤立的 1–3 帧尖峰拉不动
   * 均值，真正的语音（持续数十帧）不受影响。口型仍按逐帧 rms 跟随，起音不迟钝。
   */
  private lipEnergyAvg = 0;
  /** 本段播放是否已判过「静音→待机」：开窗与重新出声时复位，避免每帧重复发 listening */
  private playbackIdle = false;
  /** 等待首帧音频的累计秒（见 PLAYBACK_WAIT_AUDIO_TIMEOUT）：开窗清零，读到能量即不再累加 */
  private lipWaitAudioTime = 0;
  /**
   * 最近一次**真的读到能量**的时刻（ms）。见 PLAYBACK_STUCK_TIMEOUT：
   * 连续 12s 没刷新过就强制收窗，让胶囊不会永久停在「播放·正在说」。
   */
  private lipLastProgressAt = 0;
  /** 连续「有能量」帧数（见 LIP_PROGRESS_STREAK_REQUIRED）：孤立尖峰不刷新心跳 */
  private lipProgressStreak = 0;
  /**
   * P1-7「首句口型竞态」的等待态：本句已开窗、但还没从分析器读到过有效能量
   * （rms 越过 NOISE_GATE）。开场白「先文本后媒体轨」时 analyser 尚未接通，readRms 恒 0，
   * 0.45s 静音判停会把窗关掉；轨到达后据此判断「要不要把静音计时从轨到达时刻重起」。
   * 读到能量即转 false；每次 openSpeakingWindow 重新置 true。
   */
  private lipAwaitingEnergy = false;
  /** P1-6 ⑥：本句 `tts_text` 到达时刻（performance.now），用来算「首段音频出声」耗时 */
  private playbackTtsTextAt: number | null = null;
  /** P1-6 ⑥：本句是否已上报过 playback 诊断（一句话只报一次，rAF 每帧都会经过判定点） */
  private playbackReported = false;
  /** 说话窗：tts_text 开启；ended / 静音判停 / 打断 / 断链关闭 */
  private speakingWindow = false;

  /**
   * 数字人这条声道是否被上层压住（戏曲/电影在响时为 true）。
   *
   * 为什么必须由这里管而不是上层直接改 `audioEl.muted`：远端轨在 `ontrack`、
   * 每段 `tts_text`、以及打字回复 `playReplyAudio` 三处都会把 `muted` 放回 false，
   * 上层改了立刻被覆盖 —— 表现就是「听着戏，数字人突然插一句」。
   */
  private speechSuppressed = false;
  /**
   * 本轮字幕是不是本机 `<audio>` 播出来的（打字轮）。服务端推来的 `tts_text` 置回 false。
   * 只用来给 `onSubtitle` 打标，让上层能分清「打字回复自己的时间轴」与「迟到的开场白」。
   */
  private localReply = false;
  /**
   * 本轮计划背后有没有真实音频（`tts_segments.estimated` 取反）。
   *
   * 为什么需要：问候语走系统播报 + WebRTC 下行轨，服务端**入队成功后才发计划**，
   * 而浏览器建轨（offer/ICE/DTLS）可能还要数秒（服务端等轨上限 10s）。计划先到、
   * 声音后到时，若时间轴从「计划到达」就用墙钟起跑，字幕会领先声音整整一个建轨空档。
   * 有音频的计划：时间轴停在 0，等首帧能量定锚（`playbackElapsedMs`）；
   * 估算计划（TTS 全挂、永远没有声音）：立刻用墙钟逐字，不等能量。
   */
  private planHasAudio = true;
  /**
   * 逐句字幕计划（方案 B）：服务端按句下发，`start_ms` 相对「本段音频第 0 帧」。
   * `null` = 没有计划（旧服务端 / 降级轮次），此时沿用 `tts_text` 的整段渲染。
   */
  private subtitlePlan: TtsSegment[] | null = null;
  /**
   * 本段音频的总时长（ms，服务端 	ts_segments 各句 start_ms + duration_ms 的最大值）。
   *
   * 为什么需要：远端轨在 PCM 排空后仍会残留 1–3s 的低幅噪声尖峰（2026-11-11 实测
   * rms 0.05–0.13、间隔 <0.3s），只靠「静音累计 1s」判收口会把「播放·正在说」
   * 拖到最后一个尖峰之后。服务端已经告诉我这段音频多长，到点就该收口。
   */
  private subtitlePlanEndMs = 0;
  /**
   * 只有 `tts_text`、没有 `tts_segments` 时的**计划时长兜底**（ms，取 `REPLY_TEXT_FALLBACK_MS`）。
   *
   * 为什么需要（复审 P1-6；复现脚本 `scripts/audit-watchdog-stale.mjs` 的 R2）：收口的第三条
   * 出口是「计划时长到点」，它依赖 `tts_segments`。服务端只发 `tts_text`（旧服务端 / 分段送达
   * 失败 / 丢帧）时 `subtitlePlanEndMs` 恒 0，只剩「静音连续 3s」与「12s 心跳」两条；而远端轨
   * 残留尖峰间隔 <3s、单帧就能把平滑能量顶过说话门限时，两条同时失效——胶囊永远停在
   * 「播放·正在说」。这条兜底让「没有计划」也有一个时长上限。
   */
  private planFallbackMs = 0;
  /** 播放锚点（performance.now）：第一段音频真的出声的那一帧，全条时间轴的零点 */
  private subtitleAnchorAt: number | null = null;
  /** 当前已上屏的句序号（-1 = 还没推过），用于去重，避免每帧都回调 */
  private subtitleIndex = -1;
  /** 已收到 `tts_text`、但还没决定「按句逐字」还是「整段上屏」的文本 */
  private pendingReplyText: string | null = null;
  /**
   * 屏幕上此刻有字幕吗（有字才允许动嘴，见 `lipFrame` 的门）。
   *
   * 2026-11-11 用户报「文字还没出来数字人先张口」：口型跟的是音频能量，而文字可能还在
   * `pendingReplyText` 里等 `tts_segments`。两者不同步的根子就是「有声音没有字」这个状态，
   * 所以这里显式记一个「字已在屏上」的标记，口型每帧先看它。
   */
  private subtitleVisible = false;
  /** 等 `tts_segments` 的兜底定时器；计划一到就撤掉 */
  private replyTextTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private cb: RealtimeCallbacks, options: RealtimeOptions = {}) {
    this.ringTimeoutMs = options.ringTimeoutMs ?? RING_TIMEOUT_MS;
    this.autoAnswerOutbound = options.autoAnswerOutbound ?? false;
  }

  async start(token: string): Promise<void> {
    if (this.starting) return;
    this.starting = true;

    this.socket = new SignalSocket(token);
    this.socket.onStateChange = (state) => {
      if (state === "connecting" || state === "closed") {
        this.cb.onStatus("disconnected");
        // C5：断线/重连即作废确认层——服务端 finally 会取消计时器且不发 identity_result，
        // 前端不清就会永久挂住（ask_id 也不可再生效，重连后服务端会重新下发 ask）
        this.cancelPendingAsk();
      }
      // P2-6：冷启窗口里 client_diag 会因 WS 未 OPEN 被 send 丢弃 → ready 后补发最后一条
      if (state === "ready") this.flushPendingDiag();
    };
    this.socket.onAuthFail = this.cb.onAuthFail;
    this.socket.onGiveUp = this.cb.onNeedManualRetry;
    // 4002：同一设备已被另一条连接接管。不能再重连（否则两端互相踢），
    // 只把状态置为断线并让上层给出「本设备已在别处接管」的提示。
    this.socket.onSuperseded = () => {
      this.cb.onStatus("disconnected");
      this.cb.onSubtitle("这台设备已在另一处接管，这里已停止连接", "notice");
      // 审计 P1（2026-11-11）：接管之后自动重连已被主动关掉，之前**没有任何手动恢复入口**，
      // 长者只能刷新页面。这里借「连续重连失败」同一个出口把「重新连接」按钮亮出来。
      this.cb.onNeedManualRetry();
    };
    this.socket.onServerSignal = (msg) => void this.handleSignal(msg);
    this.socket.connect();
  }

  /** 「重新连接」按钮：复位信令状态机并重连（会话组件不重建） */
  retry(): void {
    this.socket?.retry();
  }

  /**
   * A6：麦克风不可用提示条上的「重试」。
   *
   * - 已有会话（`mic + pc`）：走拔麦重连的既有路径 `reopenMic()`；
   * - 没有会话（首发开麦就失败了，`pc` 未建）：等价于补一次 `setAwake(true)` 的建链——
   *   `generation += 1` 作废在飞旧代，收干净再 `setupSession()`。这时服务端还没收到过
   *   本设备的 offer，重建不会打断任何已建立的媒体会话。
   * - WS 未 ready：不能建媒体，提示稍后再按；**不复位信令**（信令断开由「重新连接」负责，
   *   两条恢复动作必须各管一段，否则一次点按会把两个状态机同时打乱）。
   */
  async retryMic(): Promise<void> {
    if (!this.mediaEnabled) return;
    if (this.mic && this.pc) {
      await this.reopenMic();
      return;
    }
    if (!this.socket?.ready) {
      this.cb.onSubtitle("还没连上服务器，请稍后再按「重试」", "notice");
      return;
    }
    const gen = ++this.generation;
    await this.teardownSession();
    if (!this.isCurrent(gen)) return;
    try {
      await this.setupSession(gen);
    } catch (err) {
      if (!this.isCurrent(gen)) return;
      this.reportDiag("webrtc", false, this.diagError(err));
      this.cb.onSubtitle("语音连接建立失败，请稍后再按「重试」", "notice");
    }
  }

  /** 麦克风可用性去重上报：同一状态不重复刷父组件（每轮开麦都会走到 ok） */
  private setMicState(next: MicState): void {
    if (this.micState === next) return;
    this.micState = next;
    this.cb.onMicState?.(next);
  }

  /**
   * C5：大按钮点选回带。elder_id 取自 identity_ask 的 candidates[0].elder_id
   * （服务端校验：不匹配 / 已定身份后重复 confirm → 忽略 + warn，不回 error）。
   * 返回 false = WS 未 OPEN，本条未发出；上层不得置「已提交」，应提示重试。
   */
  sendIdentityConfirm(askId: string, elderId: string): boolean {
    return (
      this.socket?.send({ type: "identity_confirm", ask_id: askId, elder_id: elderId }) ?? false
    );
  }

  async stop(): Promise<void> {
    this.starting = false;
    this.micState = "ok"; // 组件卸载/退出登录：告警随页面一起消失，不回调已卸载的父组件
    this.generation += 1; // P1-5：作废在飞的 setupSession（getUserMedia 可能还在 await）
    this.pendingAskId = null; // 会话结束：确认层随组件卸载，无需再回调
    this.pendingDiag = null;
    this.clearRing(); // N62b：待机响铃随会话结束一并收起
    this.endOutboundCall("hung_up"); // N62d：外呼下行随会话结束
    this.pendingOffer = null;
    this.unbindDeviceChange?.();
    this.unbindDeviceChange = null;
    this.socket?.stop();
    this.socket = null;
    await this.teardownSession();
    this.disposeLipSync(); // 卸载/退出登录：取消 rAF、断开 Analyser、关音频图
  }

  private async handleSignal(msg: ServerSignal): Promise<void> {
    switch (msg.type) {
      case "auth_ok": {
        // P1-5：每次 auth_ok 开启新代次；旧的（含还在 await getUserMedia 的）自行作废
        const gen = ++this.generation;
        this.flushPendingDiag(); // P2-6：WS 已 OPEN，补发冷启期丢掉的那条诊断
        await this.teardownSession(); // 重连场景先清旧会话
        if (gen !== this.generation) break; // 期间又来了 auth_ok / stop()：本次不建链
        // N62b：待机态（睡着/未唤醒）不建带麦会话。
        // 信令常驻 ≠ 开麦：这里返回时 getUserMedia 一次都没调过（用例断言该计数为 0）。
        //
        // A1（2026-11-10）：待机态仍要一条**只下行**会话 —— 没有它就没有播放轨，
        // 开场白/提醒/告警的音频会被服务端等 5s 后丢弃（只有字幕没声音）。
        if (!this.mediaEnabled) {
          // 失败重试一次：见 DOWNLINK_RETRY_DELAY_MS 的注释（约 1/10 的登录拿不到播放轨）。
          for (let attempt = 0; attempt < 2; attempt += 1) {
            try {
              await this.setupDownlinkSession(gen);
              break;
            } catch (err) {
              if (gen !== this.generation) break;
              this.reportDiag("webrtc", false, this.diagError(err));
              if (attempt > 0) break;
              await new Promise((resolve) => setTimeout(resolve, DOWNLINK_RETRY_DELAY_MS));
              if (gen !== this.generation) break;
              // 上一轮的 pc / 远端流要收掉再建，否则两个 pc 抢同一个 audioEl
              await this.teardownSession();
              if (gen !== this.generation) break;
            }
          }
          break;
        }
        try {
          await this.setupSession(gen);
        } catch (err) {
          if (gen !== this.generation) break; // 已换代：失败属于旧代，不再提示/上报
          // C5：建链失败上报一次诊断（服务端不回执，只入 trace）
          this.reportDiag("webrtc", false, this.diagError(err));
          this.cb.onSubtitle("语音连接建立失败，请稍后重试", "notice");
        }
        break;
      }
      case "answer":
        if (this.pc?.signalingState !== "stable") {
          await this.pc?.setRemoteDescription({ type: "answer", sdp: msg.sdp });
        }
        break;
      case "offer":
        // 迭代 7 · S-D7-3 反向外呼：这次服务端当 offerer，pad 只当 answerer（角色反转）。
        // N62b：待机态先响铃、**不**建媒体；长者确认后 acceptIncomingCall() 才开麦并回 answer。
        // N62d：待机态 + autoAnswerOutbound 时改为立刻自动接听，且只建下行（不开麦）。
        if (this.mediaEnabled) await this.acceptServerOffer(msg.sdp);
        else if (this.autoAnswerOutbound) await this.autoAnswerOutboundOffer(msg.sdp);
        else this.ringIncoming(msg.sdp);
        break;
      case "ice":
        try {
          await this.pc?.addIceCandidate(JSON.parse(msg.candidate));
        } catch {
          /* 迟到/无效候选丢弃，不中断 */
        }
        break;
      case "asr_partial":
        this.ttsPlaying = false; // 新一轮识别，打断态复位
        this.closeSpeakingWindow(); // 迭代 5：上一句的口型/说话动画随新一轮识别收尾
        this.cb.onSubtitle(msg.text, "partial");
        this.cb.onStatus("listening");
        break;
      case "asr_final":
        this.cb.onSubtitle(msg.text, "final");
        this.cb.onStatus("thinking");
        break;
      case "tts_text":
        this.ttsPlaying = true;
        // FR-UI-07：这一轮走远端流，上一段可重放的 URL 已不是「刚说过的话」，作废。
        this.lastSpeech = null;
        this.localReply = false; // 服务端接管这一轮：字幕重新按「远端推送」判定
        this.playbackTtsTextAt = performance.now(); // P1-6 ⑥：播放耗时的起点（服务端 push 时刻）
        this.playbackReported = false;
        this.resetSubtitlePlan(); // 新一轮回复：旧计划作废（新的 tts_segments 随后到）
        // 真 TTS 到了才出声；但媒体正在播时仍保持压住（见 `setSpeechSuppressed`）
        this.cb.audioEl.muted = this.speechSuppressed;
        // 迭代 5：<audio> 若在播 URL/Blob 音源（REST 降级链）→ 并入同一 Analyser；srcObject 时内部直接返回
        this.ensureElementAnalyser();
        void this.cb.audioEl.play().catch(() => {});
        this.openSpeakingWindow(); // 说话窗开启：口型 rAF 运转，setSpeaking 由 rAF 能量判定（§5.2 分工）
        // 2026-11-10（实机取证后修正）：这里**不直接**把整段文本推上屏。
        // 服务端先发 `tts_text`、几百毫秒到两秒后才合成完音频、再补一条 `tts_segments`；
        // 如果先整段上屏、收到计划再按句重打，长者会看到「整句闪一下 → 缩回去 → 重新逐字」
        // （实测字幕长度序列 21→22→3→9→10→21→22）。
        // 现在的口径：先挂起文本，等计划到了按句逐字；计划迟迟不来（降级/mock TTS）
        // 才在 REPLY_TEXT_FALLBACK_MS 后整段上屏。
        this.armReplyText(msg.text);
        // 2026-11-10：**不**在这里发 speaking —— 文本比音频早数秒到，先报「正在说」就是谎报
        // （产品负责人实测：字幕在说、声音还没来，胶囊已经说「正在说」）。说话态统一由首帧
        // 能量触发（stepLipSync 的 lipSpeaking 边沿），有声音才说「正在说」。
        break;
      case "tts_segments":
        // 方案 B：逐句字幕计划。文本按句推进，时间轴锚在「首帧出声」那一刻。
        // estimated=true（TTS 全挂的估算计划）→ 没有音频，立刻用墙钟逐字。
        this.applySubtitlePlan(msg.segments, msg.estimated !== true);
        break;
      case "media_control": {
        // FR-CON-03：服务端判定好的点播动作。形状不对就整条丢弃——一条坏消息
        // 不该让暂停中的播放器收到 `action: undefined` 而静默停掉。
        // 白名单必须与服务端 `media_service.CONTROL_INTENTS` 的取值一一对应；
        // `pause` / `resume` 是 2026-11-12 补的（暂停续播，播放器本地 seek）。
        const action = msg.action;
        if (
          action !== "next" &&
          action !== "stop" &&
          action !== "pause" &&
          action !== "resume" &&
          action !== "volume_up" &&
          action !== "volume_down" &&
          action !== "play"
        ) {
          break;
        }
        const seq = typeof msg.seq === "number" ? msg.seq : 0;
        const command: MediaControlMsg = { type: "media_control", action, seq };
        if (msg.kind === "opera" || msg.kind === "movie") command.kind = msg.kind;
        if (typeof msg.genre === "string" && msg.genre !== "") command.genre = msg.genre;
        this.cb.onMediaControl?.(command);
        break;
      }
      case "error":
        this.cb.onSubtitle(msg.message, "notice");
        break;
      case "identity_pending":
        // D 方案（2026-11-10）：服务端登录时不再索取确认，等老人开口后拿首句
        // 音频跑声纹。这里不需要弹层 —— 身份胶囊本来就按 `identitySource === null`
        // 显示「待确认」，收到本帧只确认「确认层不该出现」。
        this.pendingAskId = null;
        break;
      case "identity_ask": {
        // C5：候选恒 1 位（设备绑定长者 1v1）→ 平铺上抛；空候选属服务端违约，不下发确认层
        const candidate = msg.candidates[0];
        if (!candidate) break;
        this.pendingAskId = msg.ask_id; // 记下待确认 ask：断线时据此作废确认层
        this.cb.onIdentityAsk?.({
          askId: msg.ask_id,
          reason: msg.reason,
          elderId: candidate.elder_id,
          displayName: candidate.display_name,
          timeoutS: msg.timeout_s,
        });
        break;
      }
      case "identity_result":
        // C5：身份已定 → 确认层作废；source=generic 时上层显示通用模式警示教育条
        this.pendingAskId = null;
        this.cb.onIdentityResult?.({ source: msg.source, elderId: msg.elder_id });
        break;
      case "pong":
      case "auth_fail":
        break; // auth_fail 已由 SignalSocket 处理
    }
  }

  /**
   * N62b：唤醒态切换（`awake` 的媒体面）。信令 WS **常驻**，只有媒体跟着 awake 起落：
   * - false：收掉当前媒体会话（关 pc / 停麦），WS 不动 —— 睡着时仍能收到护工呼入的 offer；
   * - true：若 WS 已 ready，补建一轮媒体会话（等价 auth_ok 时的路径）。
   * 幂等：值没变直接返回。
   */
  async setAwake(awake: boolean): Promise<void> {
    if (this.mediaEnabled === awake) return;

    this.mediaEnabled = awake;
    // 状态切换即作废在飞的 setup（2026-11-11 审计 P1）：
    // 待机「只下行会话」与唤醒「带麦双向会话」是两条互斥的建连路径，之前只有 auth_ok
    // 会 `generation += 1`，于是 setAwake 期间在飞的 setupSession / setupDownlinkSession
    // 都还是同一个代次，后完成的那条会覆盖 `this.pc` —— 泄漏一个仍在收远端音频的
    // PeerConnection（也是「同一句话听到两遍 / 胶囊卡正在说」的来源之一）。
    this.generation += 1;
    if (!awake) {
      this.clearRing();
      this.pendingOffer = null;
      await this.teardownSession();
      // A6：待机是「故意关麦」，不是故障；不清掉会让长者睡下还看见「麦克风没声音」
      this.setMicState("ok");
      // A1：退回待机也要留一条只下行会话（播报通道），否则休眠即失聪
      if (!this.socket?.ready) return;
      const sleepGen = this.generation;
      try {
        await this.setupDownlinkSession(sleepGen);
      } catch (err) {
        if (sleepGen !== this.generation) return;
        this.reportDiag("webrtc", false, this.diagError(err));
      }
      return;
    }

    if (!this.socket?.ready) return; // 还没鉴权：auth_ok 到达时会自己建
    // N62d：从「待机自动接听的下行通话」切回唤醒态 —— 下行让位给带麦的双向会话
    this.endOutboundCall("replaced");
    await this.teardownSession();
    const gen = this.generation;
    if (!this.isCurrent(gen)) return;
    try {
      await this.setupSession(gen);
    } catch (err) {
      if (gen !== this.generation) return;
      this.reportDiag("webrtc", false, this.diagError(err));
      this.cb.onSubtitle("语音连接建立失败，请稍后重试", "notice");
    }
  }

  /**
   * 待机态收到服务端 offer → 响铃。**此刻不建任何媒体**：不调 getUserMedia、不建
   * RTCPeerConnection —— 长者没接之前，麦克风一次都不能开（FR-UI-11 隐私口径）。
   */
  private ringIncoming(sdp: string): void {
    this.clearRing(); // 一通未完又来一通：以最新 offer 为准
    this.pendingOffer = sdp;
    this.cb.onIncomingCall?.({ sdp });
    this.ringTimer = setTimeout(() => {
      this.ringTimer = null;
      if (this.pendingOffer === null) return;
      this.pendingOffer = null;
      this.cb.onIncomingCallEnded?.("timeout");
    }, this.ringTimeoutMs);
  }

  /**
   * FR-UI-10：待机态外呼自动接听（P0 触达）。只建下行：
   * - **不调 getUserMedia、不加本地音轨** → answer 天然 recvonly，满足 FR-UI-11；
   * - 保留 `pendingOffer`：唤醒词命中后 App 调 `acceptIncomingCall()` 即可升级为带麦全双工；
   * - 不排 ring 计时器（自动接听即已接听，不会再「响到超时」）。
   */
  private async autoAnswerOutboundOffer(sdp: string): Promise<void> {
    // 一通未完又来一通：旧的按「被替换」收尾，长者端只看到最新一通
    if (this.outboundCallActive) this.endOutboundCall("replaced");
    this.clearRing();
    this.pendingOffer = sdp;
    const gen = this.generation;
    if (!(await this.acceptServerOffer(sdp))) {
      if (this.generation === gen) this.pendingOffer = null;
      return;
    }
    if (this.generation !== gen) return;
    this.outboundCallActive = true;
    this.cb.onOutboundCall?.();
  }

  /**
   * 长者挂断外呼通话：只关下行会话，不碰唤醒词、不碰麦克风（本来就没开）。
   * 非自动接听的通话（响铃期）走 `declineIncomingCall()`。
   */
  hangUpOutboundCall(): void {
    if (!this.outboundCallActive) return;
    this.pendingOffer = null;
    this.clearRing();
    this.endOutboundCall("hung_up");
    void this.teardownSession();
  }

  /** 外呼通话收尾：标志位先清，回调只发一次 */
  private endOutboundCall(reason: OutboundCallEndedReason): void {
    if (!this.outboundCallActive) return;
    this.outboundCallActive = false;
    this.cb.onOutboundCallEnded?.(reason);
  }

  private clearRing(): void {
    if (this.ringTimer !== null) {
      clearTimeout(this.ringTimer);
      this.ringTimer = null;
    }
  }

  /**
   * 长者确认接听：**到这一步才**开麦、建 PeerConnection，然后回 answer（S-D7-3 反向角色）。
   * 响铃期间 pendingOffer 为空说明已超时收铃，直接返回（不接一通已作废的通话）。
   */
  async acceptIncomingCall(): Promise<void> {
    const sdp = this.pendingOffer;
    if (sdp === null) return;

    this.pendingOffer = null;
    this.clearRing();

    const gen = this.generation;
    await this.teardownSession();
    if (gen !== this.generation) return;

    let mic: MicCapture | null = null;
    try {
      mic = new MicCapture();
      await mic.start(); // 隐私边界：接听之后才有 getUserMedia
      if (gen !== this.generation) {
        await mic.stop();
        return;
      }
      this.mic = mic;

      const iceServers = await loadIceServers();
      if (gen !== this.generation) return;
      const pcConn = new RTCPeerConnection({ iceServers });
      this.pc = pcConn;
      this.attachRemoteStream(pcConn);
      pcConn.onicecandidate = (e) => {
        if (e.candidate) {
          this.socket?.send({ type: "ice", candidate: JSON.stringify(e.candidate) });
        }
      };
      const stream = mic.stream;
      if (stream) for (const track of stream.getAudioTracks()) pcConn.addTrack(track, stream);

      await pcConn.setRemoteDescription({ type: "offer", sdp });
      const answer = await pcConn.createAnswer();
      await pcConn.setLocalDescription(answer);
      if (gen !== this.generation) {
        pcConn.close();
        if (this.pc === pcConn) this.pc = null;
        return;
      }
      const localSdp = pcConn.localDescription?.sdp;
      if (localSdp) this.socket?.send({ type: "answer", sdp: localSdp });
      this.reportDiag("webrtc", true);
      this.cb.onStatus("connecting");
    } catch (err) {
      if (mic) {
        await mic.stop().catch(() => {}); // 半途失败不能把麦克风留在常亮
        if (this.mic === mic) this.mic = null;
      }
      this.reportDiag("webrtc", false, this.diagError(err));
      this.cb.onSubtitle("接听失败，将改用文字播报", "notice");
      // N62d：若这是「自动接听 → 唤醒升级」的路径，下行已被 teardown，通话按失败收尾
      this.endOutboundCall("failed");
    }
  }

  /** 长者拒接：只收铃、不建媒体；服务端等不到 answer 会自行降级文字播报 */
  declineIncomingCall(): void {
    if (this.pendingOffer === null) return;
    this.pendingOffer = null;
    this.clearRing();
    this.cb.onIncomingCallEnded?.("declined");
  }

  /**
   * auth_ok 后建立一轮会话：麦克风 + VAD + PeerConnection + offer。
   * P1-5：`gen` 是本代次号，每个 await 之后校验 `isCurrent(gen)`；不一致说明期间又来了
   * auth_ok 或 stop()，本代必须 `rollback()` 自己拿到的 stream/AudioContext/pc 再返回，
   * 否则 getUserMedia 慢于重连退避时两代会交错：mic 引用被覆盖、旧 stream 无人释放
   * （麦克风常亮）、两个 worklet 同时喂 VAD（重复 asr_start/asr_end）。
   */
  private async setupSession(gen: number): Promise<void> {
    if (!this.isCurrent(gen)) return;

    // W2：ICE 配置（STUN/TURN）与麦克风、VAD **并行**预取，到第 3 步才 await。
    // 放在最前面只是为了让 3s 超时与 mic 启动重叠，不改变下面的代次闸门顺序；
    // loadIceServers 契约上永不 reject（失败即 `[]`），故此处不会产生未捕获拒绝。
    const iceServersPromise = loadIceServers();

    // 1. 麦克风
    const mic = new MicCapture();
    let vad: TenVad | null = null;
    let pc: RTCPeerConnection | null = null;
    this.mic = mic;

    /** 代次过期/中途失败：只释放本代自己创建的对象，不动可能已被新一代挂上的引用 */
    const rollback = async (): Promise<void> => {
      vad?.free();
      if (this.vad === vad) this.vad = null;
      if (pc) {
        pc.onicecandidate = null;
        pc.ontrack = null;
        pc.close();
        if (this.pc === pc) this.pc = null;
      }
      if (this.mic === mic) this.mic = null;
      await mic.stop(); // 关键：已拿到 stream 就必须关，否则麦克风常亮、采集轨泄漏
    };

    try {
      await mic.start();
    } catch (err) {
      if (this.mic === mic) this.mic = null;
      if (!this.isCurrent(gen)) return; // 已换代：本次错误属于旧代，不打扰当前会话
      this.setMicState("unavailable"); // A6：首发开麦失败必须让顶栏与提示条看见
      this.cb.onSubtitle(err instanceof Error ? err.message : "麦克风启动失败", "notice");
      this.reportDiag("webrtc", false, this.diagError(err)); // 会话建不起来：同一轮不重复上报 ok
      return;
    }
    // 最先到达的最慢 await：交错的那一代在这里被闸门拦下（见报告里的复现脚本）
    if (!this.isCurrent(gen)) {
      await rollback();
      return;
    }
    this.setMicState("ok"); // 开麦成功即复位：上一轮的「麦克风没声音」提示必须自己收掉
    mic.onFrame = (pcm) => {
      if (gen === this.generation) this.vad?.feed(pcm); // 旧代帧不得喂新一代 VAD
    };

    // 2. 端侧 VAD（失败回落服务端 webrtcvad）
    try {
      vad = await TenVad.load({
        onSpeechStart: () => {
          if (gen !== this.generation) return; // 旧代 VAD 不得发 asr_start
          // T-F 打断（A2 裁决定案）：TTS 在播时本地开口 → 立即停播 + 清回复字幕，
          // 不等服务端；服务端 A3 收到 asr_start 停写轨清缓冲
          if (this.ttsPlaying) {
            this.ttsPlaying = false;
            this.cb.audioEl.muted = true; // 打断即静音，输出通路立即关闭
            this.cb.audioEl.pause();
            this.closeSpeakingWindow(); // 迭代 5：打断同时收口型/说话动画
            this.cb.onSubtitle("", "final");
          }
          this.cb.onStatus("listening");
          this.socket?.send({ type: "asr_start" });
          this.mic?.flushPreRollToTrack(); // asr_start 后立即轨上回放 600ms preRoll（句首保完整）
        },
        onSpeechEnd: () => {
          if (gen !== this.generation) return; // 旧代 VAD 不得发 asr_end
          this.cb.onStatus("thinking");
          this.socket?.send({ type: "asr_end" });
        },
      });
      this.vad = vad;
    } catch (err) {
      if (!this.isCurrent(gen)) {
        await rollback();
        return;
      }
      console.warn("[vad] 端侧 TEN-VAD 不可用，回落服务端 webrtcvad:", err);
      this.cb.onSubtitle("语音检测已切换为服务端模式", "notice");
    }
    if (!this.isCurrent(gen)) {
      await rollback();
      return;
    }

    // 3. WebRTC：上行麦克风轨 + offer
    const stream = this.mic.stream;
    if (!stream) return;
    // W2：带上服务端下发的 STUN/TURN。空数组 = 未登录/未配置/接口失败，
    // `{ iceServers: [] }` 与旧的无参建连行为等价，不会更差。
    // 注意这里用的是「不过滤 loopback」的浏览器视角配置（服务端自连才滤），
    // 本机 https://localhost 联调时 turn:localhost:3478 必须保留，否则同机也只剩 STUN。
    const iceServers = await iceServersPromise;
    if (!this.isCurrent(gen)) {
      await rollback();
      return;
    }
    const pcConn = new RTCPeerConnection({ iceServers });
    pc = pcConn;
    this.pc = pcConn;
    for (const track of stream.getAudioTracks()) pcConn.addTrack(track, stream);

    pcConn.onicecandidate = (e) => {
      if (e.candidate) {
        this.socket?.send({ type: "ice", candidate: JSON.stringify(e.candidate) });
      }
    };
    this.attachRemoteStream(pcConn);
    stream.getAudioTracks()[0]?.addEventListener("ended", () => {
      if (gen !== this.generation) return;
      this.cb.onSubtitle("麦克风已断开，正在重连…", "notice");
      void this.reopenMic();
    });

    const offer = await pcConn.createOffer();
    if (!this.isCurrent(gen)) {
      await rollback();
      return;
    }
    await pcConn.setLocalDescription(offer);
    if (!this.isCurrent(gen)) {
      await rollback();
      return;
    }
    this.socket?.send({ type: "offer", sdp: pcConn.localDescription!.sdp });
    // C5：会话建立成功（本地 SDP 就绪 + offer 已发）上报一次链路诊断，服务端只入 trace
    this.reportDiag("webrtc", true);

    // 麦克风插拔：回落默认设备重开（adhoc §2.3）
    this.unbindDeviceChange = this.watchDeviceChange();
    this.cb.onStatus("listening");
  }

  /**
   * 待机态「只下行」会话（A1 · 2026-11-10）：让系统在长者不开口时也能说话。
   *
   * ## 为什么要有它
   *
   * 开场白、用药提醒、告警都是**系统主动播报**，只需要下行轨；而待机态原本一条
   * `RTCPeerConnection` 都不建（N62b 隐私口径），于是服务端
   * `playback_track_ready_for_device()` 恒为 `False`，`_spawn_greeting_audio` 等满
   * `GREETING_TRACK_WAIT_SECONDS` 后把音频丢掉 —— 现象是「有字幕没声音、数字人也不
   * 动嘴」（后端日志 `identity_greeting_audio_dropped reason=track_not_ready`）。
   *
   * ## 与 `setupSession` 的差别只有一处
   *
   * **不调 `getUserMedia`、不加本地音轨**，用 `recvonly` 转辙器换一条远端音轨：
   * 麦克风一次都没开（FR-UI-11 不变），服务端 `ontrack` 只认上行轨、也不会被触发，
   * 所以既拿到了播放轨，又不会被当成「长者开始说话」。
   *
   * ## 唤醒后怎么升级
   *
   * 这里不做重协商：`setAwake(true)` 会先 `teardownSession()` 再走 `setupSession()`，
   * 带麦的会话整体替换掉这条只下行会话（服务端本就按「后到的 offer 换掉旧会话」处理）。
   * 少一条重协商路径，就少一个 glare 竞态。
   */
  private async setupDownlinkSession(gen: number): Promise<void> {
    const iceServers = await loadIceServers();
    if (!this.isCurrent(gen)) return;
    const pcConn = new RTCPeerConnection({ iceServers });
    this.pc = pcConn;
    pcConn.addTransceiver("audio", { direction: "recvonly" });
    pcConn.onicecandidate = (e) => {
      if (e.candidate) {
        this.socket?.send({ type: "ice", candidate: JSON.stringify(e.candidate) });
      }
    };
    this.attachRemoteStream(pcConn);
    const offer = await pcConn.createOffer();
    if (!this.isCurrent(gen)) return;
    await pcConn.setLocalDescription(offer);
    if (!this.isCurrent(gen)) return;
    this.socket?.send({ type: "offer", sdp: pcConn.localDescription!.sdp });
  }

  /**
   * 迭代 7 · S-D7-3 反向外呼的接听分支：这次服务端当 offerer，pad 只建接收侧。
   *
   * 与 `setupSession` 的差别是**不碰麦克风 / VAD**：外呼是服务端播报，pad 不回采，
   * 这条连接上没人说话，开麦只会白占设备（与 FR-UI-11「未命中不出端」的隐私口径同向）。
   * 同设备的旧会话已被服务端换掉（`_handle_offer` 的 `close_session` 语义），所以这里
   * 先收掉自己的旧连接，以服务端 offer 为准 —— glare 由服务端胜出，不做双 offer 协商。
   */
  private async acceptServerOffer(sdp: string): Promise<boolean> {
    const gen = this.generation;
    await this.teardownSession();
    if (this.generation !== gen) return false; // 期间又 auth_ok / stop()：本代作废

    try {
      // 与 setupSession 同一份 ICE 配置（loadIceServers 契约上永不 reject，失败即 []）
      const iceServers = await loadIceServers();
      if (this.generation !== gen) return false;

      const pcConn = new RTCPeerConnection({ iceServers });
      this.pc = pcConn;
      this.attachRemoteStream(pcConn);
      pcConn.onicecandidate = (e) => {
        if (e.candidate) {
          this.socket?.send({ type: "ice", candidate: JSON.stringify(e.candidate) });
        }
      };

      await pcConn.setRemoteDescription({ type: "offer", sdp });
      const answer = await pcConn.createAnswer();
      await pcConn.setLocalDescription(answer);
      if (this.generation !== gen) {
        pcConn.close();
        if (this.pc === pcConn) this.pc = null;
        return false;
      }
      this.socket?.send({ type: "answer", sdp: pcConn.localDescription!.sdp });
      // 与 setupSession 同口径：本地 SDP 就绪 + answer 已发 → 记一次成功诊断（服务端只入 trace）
      this.reportDiag("webrtc", true);
      this.cb.onStatus("connecting");
      return true;
    } catch (err) {
      this.reportDiag("webrtc", false, this.diagError(err));
      this.cb.onSubtitle("接听失败，将改用文字播报", "notice");
      return false;
    }
  }

  /**
   * 下行音频挂载：客户端发起（`setupSession`）与服务端外呼（`acceptServerOffer`）共用。
   * G-D12 的竞态口径必须两边一致，故收成一处 —— 复制一份迟早只改一边。
   */
  private attachRemoteStream(pcConn: RTCPeerConnection): void {
    pcConn.ontrack = (e) => {
      // 听写阶段不出声：静默挂载，收到 tts_text 才放开（用户裁决 2026-11-05）
      //
      // 但「收到 tts_text 才放开」与「ontrack 先到/后到」是竞态（迭代 4 收口 G-D12）：
      // 服务端在 `auth_ok` 之后、浏览器发 offer 之前就下发开场白，所以 tts_text 通常
      // **先到**（tts_text 分支已 `muted = false` 并开始播），而 ontrack 随后挂 srcObject
      // 时若无条件静音，第一次开口就被吃掉 —— 表现是「有字幕没声音，直到长者自己说
      // 第一句话才恢复」。开场白一旦配音频（W5），这就是必现 bug。
      // 2026-11-10：`speakingWindow` 开着时**绝不静音**。此前只看 `ttsPlaying`，而它会被
      // `asr_partial` 复位（长者在播报中出一点声、或端侧 VAD 误触发）；随后的 ontrack 就把
      // 已经在播的开场白静音 → 实测「音频在走、`<audio>.muted` 为 true、用户听不到、
      // 口型也不动」各出现一次。文本已到 = 这一段该出声，与麦克风那边的状态无关。
      // 2026-11-11 12:2x 实测（sm:lipDebug 探针）：ontrack 若早于 tts_text 到达，旧的
      // 「`!ttsPlaying && !speakingWindow` 就静音」会让开场白前 **4.21 秒**是哑的——
      // `muted` 在 7.05s 变 true、11.71s 才被 tts_text 放回 false，而那一刻 `currentTime`
      // 已经 4.21。长者听到的是「开头没声，后半句突然出声」，还会误判成口型不同步。
      // 远端轨只承载服务端 TTS，不存在需要遮挡的内容；打断静音（`interrupt()` 里的
      // `muted = true`）仍由下一段 `tts_text` 放开，行为不变。所以这里直接放开。
      this.cb.audioEl.muted = this.speechSuppressed;
      this.cb.audioEl.srcObject = e.streams[0] ?? null;
      // 迭代 5：远端轨 → 共享 Analyser（getByteTimeDomainData 算 RMS 的唯一 WebRTC 入口）
      this.attachTrackAnalyser(e.streams[0] ?? null);
      // P1-7：首句「tts_text 先到、轨后到」——开窗时的静音计时从文本到达起算，轨到达前
      // readRms 恒 0；此处把计时从「轨到达」重起（窗已被判停关掉则重开），首句才会张嘴。
      this.onTrackEnergyReady();
      this.cb.audioEl.onended = () => {
        this.ttsPlaying = false;
        // 迭代 5：明确结束信号 → 立即收口型 + 关说话动画（rAF 静音判停是它缺席时的退路）。
        // 2026-11-10：结束同时把播放胶囊推回「待机」，否则会一直停在「正在说」
        this.endPlayback();
      };
      void this.cb.audioEl.play().catch(() => {
        this.cb.onSubtitle("点击屏幕后开始播放语音", "notice");
      });
    };
  }

  /** 本代次是否仍有效（P1-5 闸门）：stop() 或新的 auth_ok 会改 generation/starting */
  private isCurrent(gen: number): boolean {
    return this.starting && gen === this.generation;
  }

  private async teardownSession(): Promise<void> {
    this.vad?.free();
    this.vad = null;
    // 迭代 5：连接收尾 → 断开能量源、取消 rAF、口型归零（音频图本身留着跨重连复用）
    this.detachTrackAnalyser();
    this.closeSpeakingWindow();
    if (this.pc) {
      this.pc.onicecandidate = null;
      this.pc.ontrack = null;
      this.pc.close();
      this.pc = null;
    }
    // 先把当前 mic 摘下来再 await：途中若有新一代会话建好麦克风，旧路径收尾时
    // 再把 `this.mic` 置空就会把**新**的 MediaStream 丢掉（谁也不会去 stop 它，
    // 麦克风指示灯不灭）。审计 P2（2026-11-11）：交换 null 与 await 的顺序即可。
    const mic = this.mic;
    this.mic = null;
    await mic?.stop();
  }

  /**
   * 打字回复的语音播报（2026-11-11）：用同一个 `<audio>` 元素播服务端合成的 WAV，
   * 由同一个 Analyser 驱动口型与「播放·正在说」。
   *
   * 为什么走元素路径而不是远端轨：打字回复没有 WebSocket 媒体会话（长者是坐着打字的，
   * 麦克风/PC 可能压根没建）。`ensureElementAnalyser()` 会把元素绑到共享 Analyser 并接回
   * destination——这是元素路径**唯一**的出声通路，详见该方法的注释。
   *
   * 收口：`onended` → `endPlayback()`（关窗 + 复位胶囊），与语音轮的 ended 同一条出口。
   */
  /**
   * 打字回复的文本由 App 侧逐字展示（`startTextReveal` 不经过 `RealtimeClient`），
   * 而「有字才动嘴」的闸门 `subtitleVisible` 只在 realtime 内部被置位，
   * 于是输入框那条路径会出现「有声音、没口型」。App 把字上屏后显式告知一次即可。
   */
  markReplyTextShown(): void {
    this.subtitleVisible = true;
  }

  /**
   * 压住 / 放开数字人这条声道（媒体互斥）。
   *
   * 只关声音，**不停会话**：WebRTC 连接、麦克风、字幕后端都照旧，
   * 放开时不用重连（重连一次要 1~2 秒，长者会在戏停之后听见一段空白）。
   *
   * 幂等：同一个值连调不重复 pause/play —— 上层 effect 每次渲染都可能调一次。
   */
  setSpeechSuppressed(on: boolean): void {
    if (this.speechSuppressed === on) return;
    this.speechSuppressed = on;
    const el = this.cb.audioEl;
    el.muted = on;
    if (on) {
      try {
        el.pause();
      } catch {
        /* 未在播：忽略 */
      }
      // 压住即收口型：嘴还在动但没声音，比不动更像故障
      this.closeSpeakingWindow();
      this.avatar?.setMouthOpen(0);
      return;
    }
    void el.play()?.catch(() => {
      // 被策略拦下就静默：下一段 tts_text 到来时还会再 play 一次
    });
  }

  /**
   * 长者按「打断播报」：把本页正在出声的一切真正停掉（FR-UI-04）。
   *
   * 与 VAD 打断（`onSpeechStart` 里的 `ttsPlaying` 分支）是同一套收口动作，区别只在
   * 触发者是**按钮**而不是麦克风。所以这里**不发 `asr_start`**：按钮只说「别念了」，
   * 没说「我要说话」；真发过去，后端 `app/audio/webrtc.py` 的 `_handle_asr_start`
   * 会 `barge_in()` + `begin_turn()`，而我们没有对应的 `asr_end`，等于挂一个永远
   * 不收口的 turn。长者真想说话，下一句开口时 VAD 会照旧走打断路径。
   *
   * 幂等：没在播时调也不报错（按钮 disabled 前可能被连点）。
   */
  interruptSpeech(): void {
    const el = this.cb.audioEl;
    this.ttsPlaying = false;
    this.localReply = false;
    // 只是压住这一路声音；下一段 `tts_text` 会按 `speechSuppressed` 重新赋值 muted，
    // 所以不会把数字人永久静音。
    el.muted = true;
    try {
      el.pause();
    } catch {
      /* 未在播：忽略 */
    }
    // 打字回复走元素 src（`playReplyAudio`）；远端流走 srcObject。srcObject 必须留着：
    // 清了就把 WebRTC 这一路的声音一起摘掉，只能重连才回来（那是「断线」不是「打断」）。
    if (!el.srcObject) {
      el.onended = null; // 免得清 src 触发的 ended 再走一遍 endPlayback
      el.removeAttribute("src");
      try {
        el.load();
      } catch {
        /* 部分内核 load() 会抛：忽略，pause 已经把声音停了 */
      }
    }
    this.closeSpeakingWindow(); // 口型归零 + 挂起的整段文本按「无计划」正常上屏
    this.resetSubtitlePlan(); // 逐句时间轴作废：字幕队列一起清空
    this.cb.onSubtitle("", "final");
    this.cb.onStatus("listening");
  }

  playReplyAudio(
    url: string,
    durationMs?: number,
    segments: TtsSegment[] = [],
    options: { textAlreadyShown?: boolean } = {},
  ): void {
    const el = this.cb.audioEl;
    if (!url) return;
    // FR-UI-07：记下这一段，供「再说一遍」原样重放（含逐句时间轴，字幕才不会与声音错位）。
    this.lastSpeech = { url, durationMs, segments };
    this.localReply = true; // 这一轮的字幕由本机播放驱动，上层不许按「迟到开场白」拦掉
    try {
      el.pause();
    } catch {
      /* 未在播：忽略 */
    }
    el.srcObject = null; // 清掉可能残留的远端流，否则 src 不生效
    el.src = url;
    // 必须早于 play()：静音状态下播开头会丢声（2026-11-11 实测 4.21s 哑）。
    // 媒体在播时不放开（`speechSuppressed`）—— 那一轮的字幕仍会照常上屏。
    this.cb.audioEl.muted = this.speechSuppressed;
    el.onended = () => {
      this.endPlayback();
    };
    this.openSpeakingWindow(); // 开窗（内部接轨分析器、起 rAF）
    this.ensureElementAnalyser(); // 元素源 → 共享 Analyser → destination
    if (segments.length > 0 && !options.textAlreadyShown) {
      // 逐句时间窗（2026-11-11）：打字回复此前只有总时长，前端只能按平均字速铺字，
      // 而语音在标点处会停 —— 字幕与口型必然越走越偏。有 segments 就走与语音轮同一条
      // 时间轴（applySubtitlePlan），逐字跟的是真实语音节奏。
      //
      // 2026-11-17：`textAlreadyShown` —— 打字轮的 2s 兜底已经把整段文上屏后，音频才
      // 合成完（实测 /tts 3.9s）。此时再套计划会把字幕句序重置为第 0 句，长者看到同一句
      // 从第一个字重打一遍。已上屏的这一轮只补声音与口型，不再重播字幕；`lastSpeech`
      // 仍保存 segments，「再说一遍」重放时照常带时间轴。
      this.applySubtitlePlan(segments);
    }
    if (typeof durationMs === "number" && durationMs > 0) {
      // 没有逐句时间窗时才用总时长当准绳；有 plan 时以 plan 的末端为准（applySubtitlePlan 已写）。
      // 不设它就会退回 planFallbackMs(20s)，24s 的回复会在 20.5s 被提前收口（实测）。
      if (this.subtitlePlanEndMs <= 0) this.subtitlePlanEndMs = durationMs;
      if (this.subtitleAnchorAt === null) this.subtitleAnchorAt = performance.now();
      this.planFallbackMs = 0;
    }
    void el.play().catch(() => {
      // 自动播放被拦（长者没交互过）或文件 404：不留一个永远「正在说」的胶囊
      this.endPlayback();
    });
  }

  /**
   * FR-UI-07「再说一遍」：把上一段能重放的 TTS 再念一遍。
   *
   * 返回 false = 没有可重放的东西（上一段是远端实时流，或还没说过话）。上层据此
   * 改上大字提示，**不要**假装播过了——长者会对着没声音的屏幕等。
   */
  replayLastSpeech(): boolean {
    const last = this.lastSpeech;
    if (!last) return false;
    // 走同一条元素播放路径：语气、口型、逐句字幕都与首播一致（不复用「只 play()」的捷径，
    // 那样 onended 不接、字幕计划不重建，胶囊会永远卡在「正在说」）。
    this.playReplyAudio(last.url, last.durationMs, last.segments);
    return true;
  }

  /**
   * `<audio>` 元素正在播打字回复的语音吗？
   *
   * 为什么单独判定：元素路径的音量包络里，句中停顿（换气、标点）能有 1–2 秒读不到能量。
   * 若照样按静音翻「待机」，长者会看到胶囊在「正在说/待机」之间来回闪（实测 24s 回复闪了 6 次）。
   * 元素播放状态是**确定的**，比能量判定可靠，所以这条路径以它为准。
   */
  private isElementPlaying(): boolean {
    const el = this.cb.audioEl;
    if (!el) return false;
    return Boolean(el.src) && !el.srcObject && !el.paused && !el.ended;
  }

  /** C5 client_diag 的对上层入口：3D 舞台等组件上报一次链路诊断（服务端只记日志、不回执） */
  sendClientDiag(stage: ClientDiagStage, ok: boolean, error = ""): void {
    this.reportDiag(stage, ok, error);
  }

  /**
   * 迭代 5：接管 3D 控制器（`AvatarStage` 的 `onReady` 出口）。
   * 传 `null` = 降级/卸载/StrictMode 双挂载，立即收口型与说话动画，避免握着已释放的句柄。
   * 3D 可能晚于 WS 就绪，也可能早于；两条时序都靠这里对齐（说话窗进行中到达也会补一次 setSpeaking）。
   */
  setAvatarController(controller: LipSyncTarget | null): void {
    // 「没有字就不许张口」的**最后一道闸**（2026-11-11 第三次复现）：
    // 口型有多条写入路径（rAF 能量、talking 动画、组件内部），`lipFrame` 里那道门只管住了
    // 能量这一条。实测仍出现「有声无字、嘴在动」，所以在这里把所有写入统一收口：
    // 屏幕上没有字时，`setMouthOpen` 一律归零、`setSpeaking(true)` 一律降为 false；
    // 字上屏后自动恢复。用 Proxy 而不是包装对象，避免丢掉控制器上的其它方法。
    this.avatar = controller
      ? (new Proxy(controller, {
          get: (target, prop) => {
            if (prop === "setMouthOpen") {
              return (value: number) => {
                target.setMouthOpen(this.subtitleVisible || this.isElementPlaying() ? value : 0);
              };
            }
            if (prop === "setSpeaking") {
              return (on: boolean) => {
                target.setSpeaking(on && (this.subtitleVisible || this.isElementPlaying()));
              };
            }
            // receiver 必须传 target：控制器的私有字段在 getter 里访问，传 proxy 会抛 TypeError。
            const value = Reflect.get(target, prop, target);
            return typeof value === "function" ? value.bind(target) : value;
          },
        }) as LipSyncTarget)
      : null;
    if (!controller) {
      this.closeSpeakingWindow();
      return;
    }
    if (!this.speakingWindow) return;
    // 说话窗已开而 3D 迟于 tts_text 就绪：接回口型循环。计时与状态计数从「现在」重起，
    // 否则开窗至今累计的巨大 dt 会一帧直接触发静音判停。
    // setSpeaking 不在此补置——说话态统一由 rAF 的能量阈值判定（§5.2 第 4 条「以 rAF 为准」），
    // 迟到期间的声音窗口错过了就让 rAF 用之后的能量自然判定，不强置 talking。
    this.lipLastAt = performance.now();
    this.lipSilenceTime = 0;
    this.lipMouth = 0;
    controller.setMouthOpen(0);
    this.startLipLoop();
  }

  /** C5：作废当前待确认 ask（断线/重连）；无待确认时为 no-op */
  private cancelPendingAsk(): void {
    if (!this.pendingAskId) return;
    this.pendingAskId = null;
    this.cb.onIdentityCancelled?.();
  }

  /**
   * C5 client_diag：仅诊断日志（服务端不回执）；error 截断 + 压平换行，避免带出无关上下文。
   * P2-6：WS 未 OPEN 时 send() 返回 false，直接丢弃会丢掉冷启/弱网窗口里最有用的那条
   * （恰是 vrm_load/webrtc 降级发生的时刻）→ 留在内存，ready/auth_ok 后补发一次。
   */
  private reportDiag(stage: ClientDiagStage, ok: boolean, error = "", elapsedMs?: number): void {
    const msg: ClientDiagMsg = {
      type: "client_diag",
      stage,
      ok,
      error: error.slice(0, 200).replace(/\s+/g, " ").trim(),
    };
    // 只有带真实耗时的阶段才加字段：别的 stage 多一个 0 会被读成「瞬间完成」
    if (elapsedMs !== undefined) msg.elapsed_ms = Math.max(0, Math.round(elapsedMs));
    if (this.socket?.send(msg)) {
      this.pendingDiag = null; // 已送达，旧缓存作废（只保留最近一条）
      return;
    }
    this.pendingDiag = msg;
  }

  /**
   * P1-6 ⑥ 前端播放：第一帧真有能量（rms 越过静噪门）就算「首段音频真的出声」。
   * 起点取本句 `tts_text` 到达时刻——服务端 push 那一刻，正是六节点链路里 ⑤ 的终点。
   * 没有起点（例如开场白未带 tts_text 的极端情形）就不报，不编一个耗时。
   */
  private reportPlaybackStart(now: number): void {
    this.playbackReported = true;
    const startedAt = this.playbackTtsTextAt;
    if (startedAt === null) return;
    this.reportDiag("playback", true, "", now - startedAt);
  }

  /** P2-6：ws 由 connecting 转 ready（或 auth_ok）后补发最后一条未送达的诊断 */
  private flushPendingDiag(): void {
    const msg = this.pendingDiag;
    if (!msg) return;
    if (this.socket?.send(msg)) this.pendingDiag = null;
  }

  private diagError(err: unknown): string {
    if (err instanceof Error) return err.message || err.name;
    return typeof err === "string" ? err : "unknown_error";
  }

  /** 麦克风拔出/失效 → 重新 getUserMedia 并 replaceTrack，PeerConnection 不重建 */
  private async reopenMic(): Promise<void> {
    const gen = this.generation;
    const mic = this.mic;
    const pc = this.pc;
    if (!mic || !pc) return;
    const sender = pc.getSenders().find((s) => s.track?.kind === "audio");
    await mic.stop();
    if (gen !== this.generation) return; // 会话已换代：不要把旧 mic 重新拉起来
    try {
      await mic.start();
    } catch (err) {
      if (gen !== this.generation) return;
      this.setMicState("unavailable"); // A6：拔麦后重开失败 → 顶栏改口，别再说「正在听」
      this.cb.onSubtitle(err instanceof Error ? err.message : "麦克风重连失败", "notice");
      return;
    }
    if (gen !== this.generation) {
      await mic.stop(); // 重开期间换了会话：本代自行释放，避免麦克风常亮
      return;
    }
    mic.onFrame = (pcm) => {
      if (gen === this.generation) this.vad?.feed(pcm);
    };
    const track = mic.stream?.getAudioTracks()[0];
    if (track) {
      track.addEventListener("ended", () => {
        if (gen !== this.generation) return;
        this.cb.onSubtitle("麦克风已断开，正在重连…", "notice");
        void this.reopenMic();
      });
      await sender?.replaceTrack(track);
    }
    this.setMicState("ok"); // 重开成功：提示条与顶栏跟着复位
    this.cb.onStatus("listening");
  }

  private watchDeviceChange(): () => void {
    const handler = () => {
      const track = this.mic?.stream?.getAudioTracks()[0];
      if (track && track.readyState === "ended") void this.reopenMic();
    };
    navigator.mediaDevices.addEventListener("devicechange", handler);
    return () =>
      navigator.mediaDevices.removeEventListener("devicechange", handler);
  }

  /* =====================================================================================
   * 迭代 5 · 口型开合（v1.3：移植 engine.js §5.2 权威口径，参照 attachStream/_setMouth/_updateMouth）
   * 链路：两条播放路径 → 同一个 AnalyserNode → 每帧真实 dt 起音/收音跟随 → controller.setMouthOpen
   *   ① WebRTC 远端轨：ontrack 建 MediaStreamAudioSourceNode → **只接 Analyser，不接 destination**
   *      （engine.js:1121-1127 原文口径；轨的声音由 `<audio>` srcObject 自己出，音频图上再接
   *      destination 才会造成「同一份声音两路出」的双声）。
   *   ② `<audio>` 的 URL/Blob 音源：tts_text 时建 MediaElementAudioSourceNode → Analyser，
   *      **且 source→destination 必须保留**——engine.js「不接 destination」的规则只适用轨路径：
   *      元素一旦被 WebAudio「接管」，它的直通输出就交了出来，不接回 destination 就没人出声。
   *   engine.js:1126 注释原文：「只做分析，不接 destination，避免和页面 <audio> 重复播放」。
   * ===================================================================================== */

  /**
   * 懒建共享音频图。对照 engine.js:1114-1127 的 attachStream：**不接 destination 原样照抄**——
   * v1.1 曾架一条 gain=0 的汇点到 destination 以防「部分内核取数值恒 0」，参考系统全程
   * 无此汇点且正常工作，按 §5.2 移植口径一并废弃；若真遇到不 pull 的内核，后果只是口型
   * 不动，语音与字幕照旧（不白屏、不出错），届时再按 R8 报告并给建议值，不悄悄加边。
   */
  private ensureAnalyser(): AnalyserNode | null {
    if (this.analyser) return this.analyser;
    try {
      const ctx = new AudioContext();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = MOUTH_FFT_SIZE; // engine.js:1123
      analyser.smoothingTimeConstant = MOUTH_SMOOTHING; // engine.js:1124
      // 只做分析，不接 destination —— engine.js:1126-1127 原文口径。该规则针对轨路径
      // （轨由 <audio> srcObject 自行出声）；元素路径唯一例外见 ensureElementAnalyser。
      this.audioCtx = ctx;
      this.analyser = analyser;
      this.lipBuf = new Uint8Array(analyser.fftSize); // engine.js:1125 的 timeData
      void this.ensurePlayoutClock(ctx);
      return analyser;
    } catch {
      // 老内核 / 策略禁用 WebAudio：只丢口型，语音与字幕照旧（不白屏、不出错）
      return null;
    }
  }

  /**
   * 建播放侧时钟（异步，只跑一次）。**旁路**：`source → clock → gain(0) → destination`。
   *
   * 为什么不像 analyser 那样直接悬空：AudioWorkletNode 必须有到 destination 的通路才会被
   * pull，否则 `process()` 根本不跑、采样数恒 0。gain=0 保证这条通路**不出声**——声音仍旧
   * 由 `<audio>` 元素自己出（轨走 srcObject、URL 音源走 MediaElementSource→destination），
   * 所以不存在双声，也不需要在时钟建好之前把元素静音（那样一旦 context 被自动播放策略
   * 挂起，长者就彻底听不见声音了）。
   */
  private ensurePlayoutClock(ctx: AudioContext): Promise<void> {
    if (this.clockLoading) return this.clockLoading;
    const loading = (async () => {
      try {
        await ctx.audioWorklet.addModule(new URL("./audio/playout-clock-worklet.js", import.meta.url));
        if (this.audioCtx !== ctx) return; // 加载期间会话已换代：这一份图作废
        const node = new AudioWorkletNode(ctx, "playout-clock");
        const sink = ctx.createGain();
        sink.gain.value = 0;
        node.connect(sink);
        sink.connect(ctx.destination);
        node.port.onmessage = (event: MessageEvent<PlayoutClockReport>) => {
          const report = event.data;
          this.clockSamples = report.samples;
          this.clockRms = report.rms;
          this.clockSampleRate = report.sampleRate;
          // 零点在这里定，不在 rAF 里定：worklet 记的是峰值所在的那个 quantum（2.67ms），
          // rAF 现场取只会拿到「最近一次上报」的采样数，零点系统性偏后最多 20ms。
          if (
            this.clockEnergySamples === null &&
            this.speakingWindow &&
            report.rms > MOUTH_NOISE_GATE
          ) {
            this.clockEnergySamples = report.peakSamples;
          }
        };
        this.clockNode = node;
        this.clockSink = sink;
        // 图可能先于时钟建好（轨已到、模块还在加载）：补接已有的两个源。
        if (this.streamSource) this.streamSource.connect(node);
        if (this.elementSource) this.elementSource.connect(node);
      } catch (err) {
        // 降级：analyser + 墙钟继续跑，功能不减、精度下降。只报一次诊断。
        this.clockNode = null;
        this.clockSink = null;
        this.reportDiag("playback", false, err instanceof Error ? err.message : String(err));
      }
    })();
    this.clockLoading = loading;
    return loading;
  }

  /** 把一路播放源接到时钟上（时钟还没建好时是 no-op，建好后会补接）。 */
  private tapPlayoutClock(source: AudioNode): void {
    if (!this.clockNode) return;
    try {
      source.connect(this.clockNode);
    } catch {
      /* 已接过 / 节点已关：口型退回 analyser，不影响出声 */
    }
  }

  /**
   * ① WebRTC 远端轨 → 共享 Analyser。对照 engine.js:1111-1131 attachStream：
   * 仅 MediaStreamSource → Analyser，无任何 destination 边。重连后旧轨作废，先断再建；
   * 断开时同步把口型归零（engine.js:1133-1143 detachStream 末尾 setMouth(0) 的语义），
   * 不把旧源的残留开合度带进新轨。
   */
  private attachTrackAnalyser(stream: MediaStream | null): void {
    this.detachTrackAnalyser();
    this.lipMouth = 0;
    if (this.speakingWindow) this.avatar?.setMouthOpen(0);
    if (!stream) return;
    this.trackStream = stream; // 记住这一段流：后面 ensureTrackAnalyser 据此判幂等/兜底
    const analyser = this.ensureAnalyser();
    const ctx = this.audioCtx;
    if (!analyser || !ctx) return;
    // P1-7：分析必须在 running 的 context 上才有数据。轨先于 tts_text 到时没人 resume 过
    // （开窗时的 resume 那时 audioCtx 还是 null），这里补一次，避免新建即 suspended → 恒 0。
    if (ctx.state !== "running") void ctx.resume().catch(() => {});
    try {
      const source = ctx.createMediaStreamSource(stream);
      source.connect(analyser);
      this.tapPlayoutClock(source);
      this.streamSource = source;
    } catch {
      this.streamSource = null; // 无音轨等异常：口型退化为不动，语音不受影响
    }
  }

  private detachTrackAnalyser(): void {
    try {
      this.streamSource?.disconnect();
    } catch {
      /* 已断开或未连接：忽略 */
    }
    this.streamSource = null;
    this.trackStream = null;
  }

  /**
   * P1-7：幂等地保证「下行媒体流 → 共享 Analyser」这一段已接好（`ensureAnalyser` 负责
   * AnalyserNode 本身，这里负责它上游的 MediaStreamAudioSourceNode）。
   * 轨来源按优先级取：`<audio>.srcObject`（ontrack 已挂上）→ trackStream（缓存的下行流）。
   * 已接好且 stream 未换 → no-op（正常轮次每帧调也无副作用）；换流（重连新轨）→ 先断再建，
   * 口径同 attachTrackAnalyser。返回 true = 此刻有可读能量的源。
   *
   * 为什么需要它：开窗（tts_text）时 ontrack 可能还没跑过，此前全文件没有任何「开窗时补接
   * 分析器」的路径，于是首句 analyser 为 null，readRms 恒 0。
   */
  private ensureTrackAnalyser(): boolean {
    const direct = this.cb.audioEl.srcObject;
    const stream =
      direct instanceof MediaStream && direct.getAudioTracks().length > 0
        ? direct
        : this.trackStream;
    if (!stream || stream.getAudioTracks().length === 0) return false;
    if (this.streamSource && this.trackStream === stream) return true;
    this.attachTrackAnalyser(stream);
    return this.streamSource !== null;
  }

  /**
   * ② `<audio>` 播放 URL/Blob 音源 → 同一 Analyser，**source→destination 必须保留**（§5.2
   * 第 2 条要求写清的两条路径区别）：engine.js:1126「只接 analyser 不接 destination」只适用
   * 轨路径——WebRTC 轨的声音由 `<audio>` srcObject 自己出，图上不接 destination 恰好避免
   * 双声；元素路径相反，元素一旦绑定 MediaElementAudioSourceNode 就被 WebAudio「接管」，
   * 接管后 source→destination 是**唯一出声通路**，不接回就「有口型没声音」。
   * 位置敏感：若对 `srcObject`（WebRTC 轨）也去绑元素源，就会把已经在出声的那一路接管掉，
   * 所以只在元素确实在播 `src`（且没有 srcObject）时绑定，且同一元素只绑一次。
   */
  private ensureElementAnalyser(): void {
    if (this.elementSource) return;
    const el = this.cb.audioEl;
    if (el.srcObject || !el.src) return;
    const analyser = this.ensureAnalyser();
    const ctx = this.audioCtx;
    if (!analyser || !ctx) return;
    try {
      const source = ctx.createMediaElementSource(el);
      source.connect(analyser);
      source.connect(ctx.destination); // 绑定后这是唯一出声路径（不是叠加）
      this.tapPlayoutClock(source);
      this.elementSource = source;
    } catch {
      /* 元素已被别处绑过：沿用现有图，不重复建 */
    }
  }

  /**
   * 打开说话窗：tts_text 时调用（首句轨晚到的补救重开见 onTrackEnergyReady）。**不在此置
   * setSpeaking(true)**——§5.2 第 4 条：说话态由 rAF 的能量阈值统一判定（mouthLevel>0.06），
   * 这里只负责把窗开起来让 rAF 运转；起音跟随会把首包能量几帧内抬过阈值，talking 动画随即接上。
   */
  private openSpeakingWindow(): void {
    if (this.speakingWindow) return;
    // P1-7：下行轨若已到（正常轮次 / 重连后），开窗这一刻就把分析器接上，不必等 ontrack。
    // 轨还没到则接不上，记「等待能量」，由 ontrack（onTrackEnergyReady）或每帧自愈补接。
    this.ensureTrackAnalyser();
    this.speakingWindow = true;
    this.lipMouth = 0;
    this.lipSilenceTime = 0;
    this.lipEnergyAvg = 0;
    this.lipAwaitingEnergy = true;
    this.lipSpeaking = false;
    this.playbackIdle = false;
    this.lipWaitAudioTime = 0;
    this.clockEnergySamples = null;
    this.clockAnchorSamples = null;
    this.lipLastAt = performance.now();
    this.lipLastProgressAt = this.lipLastAt;
    this.lipProgressStreak = 0;
    // 本轮开始时的计划时长兜底：等服务端 tts_segments 到了会被真实时长覆盖（见 applySubtitlePlan）
    this.planFallbackMs = REPLY_TEXT_FALLBACK_MS;
    // 自动播放策略下 context 可能刚被建出来还是 suspended：出声与取能量都要求 running
    if (this.audioCtx?.state !== "running") void this.audioCtx?.resume().catch(() => {});
    this.startLipLoop(); // 无控制器时不启 rAF：没有可驱动的对象，纯耗电
  }

  /**
   * P1-7：下行媒体轨到达时的口型补救（首句「先文本后媒体轨」竞态）。
   * 时序：tts_text 先到 → 开窗，但此时 `audioEl.srcObject` 还没有轨、analyser 也没建，
   * readRms 恒 0 → 0.45s 后先被判「静音待机」（窗仍开着，见 markPlaybackIdle）；
   * ontrack 随后到达时把静音计时起点挪到「轨到达」，首句才不会白等文本到达那几百毫秒。
   *
   * 只在「本句已开窗且还没读到过有效能量」且「TTS 仍在播」的等待态生效（即上述竞态）：
   *   - 窗已被静音判停关掉 → 整窗重开（等同于从轨到达时刻重新开窗）；
   *   - 窗还开着 → 只把静音计时起点挪到「轨到达」，不沿用文本到达时刻。
   * 已读到能量（正常轮次）或 TTS 已收（ended / 打断 / 新一轮 partial）→ no-op，不动手感。
   */
  private onTrackEnergyReady(): void {
    if (!this.lipAwaitingEnergy || !this.ttsPlaying) return;
    if (!this.speakingWindow) {
      this.openSpeakingWindow(); // 被静音判停关掉：重开（内部清零计时、重起 rAF）
      return;
    }
    this.lipLastAt = performance.now(); // 换成「轨到达」这一帧为 dt 起点
    this.lipSilenceTime = 0;
    this.lipMouth = 0;
    this.avatar?.setMouthOpen(0);
    this.startLipLoop();
  }

  /**
   * 关闭说话窗：口型与说话动画一起收（幂等，未开窗时也能调用）。
   * 两类驱动的分工（§5.2 第 4 条，以 rAF 为准）：
   *   rAF（能量量测）：说话态判定 + 静音 ≥SILENCE_HOLD(0.45s) 判停 ——「声音到底停了没」的真值；
   *   确定信号（协议/生命周期）：ended（元素播完）、打断（speechstart）、asr_partial（新一轮
   *   识别）、teardown——这些是「这路声音确定不再播」的事件，职责保留直接收窗；
   *   两处可能先后到达，close 幂等，后到的一次归为 no-op，不互相打架。
   */
  private closeSpeakingWindow(): void {
    this.speakingWindow = false;
    if (this.lipRaf) {
      cancelAnimationFrame(this.lipRaf);
      this.lipRaf = 0;
    }
    this.lipMouth = 0;
    this.lipSilenceTime = 0;
    this.lipSpeaking = false;
    this.playbackIdle = false;
    this.lipWaitAudioTime = 0;
    // 收窗时还没有计划 = 这一轮永远不会有逐句时间轴（TTS 全挂 / mock 单句）。
    // 挂起的整段文本必须在上屏后再清，否则长者的观感是「有声音、字幕从头到尾空着」。
    if (!this.subtitlePlan) this.flushPendingReplyText();
    // 音频从头到尾没到（`lipAwaitingEnergy` 仍为 true）时**保留计划**：这一轮的字幕
    // 靠计划时间轴自己逐字走完，清掉计划会让 BigSubtitle 退回整段上屏（打字机消失）。
    // 下一轮 `tts_text` 会 `resetSubtitlePlan()`，不会串台。
    if (!this.lipAwaitingEnergy) this.resetSubtitlePlan();
    this.avatar?.setMouthOpen(0);
    this.avatar?.setSpeaking(false);
  }

  /** 丢弃逐句字幕计划与锚点（新一轮回复 / 打断 / 收窗时）：下次从整段文本重新开始。 */
  private resetSubtitlePlan(): void {
    this.subtitlePlan = null;
    this.subtitlePlanEndMs = 0;
    this.subtitleAnchorAt = null;
    this.subtitleIndex = -1;
    this.planHasAudio = true; // 新一轮默认「计划带音频」，估算计划到达时再置 false
    // 新一轮书幕还没上屏：口型门（`lipFrame`）据此拒绝「无字的张口」。
    this.subtitleVisible = false;
    // 挂起的整段文本也一起作废：新一轮的文本会重新挂起（否则上一轮迟到的兜底
    // 定时器会把旧台词盖到新回复上）。
    this.clearReplyTextTimer();
    this.pendingReplyText = null;
    // 进度通道一起清空：否则待机时字幕组件还拿着上一轮的进度继续打字
    publishSubtitleProgress(null);
    publishSubtitlePlanPresence(false);
  }

  /**
   * 收到逐句字幕计划：存下来，并**立刻**按当前播放进度对齐一次。
   * 计划通常在音频出声之后才到（要等第一句合成完），所以「等锚点」不够，
   * 必须按已播放的毫秒数跳到该显示的那一句，否则前几秒会停在整段文本上。
   *
   * 同时把**挂起的整段文本作废**：文本已经由逐句推进接手，兜底定时器再触发就会
   * 把整段文本盖回逐字结果上（实机表现为「逐字到一半突然整段跳出」）。
   */
  private applySubtitlePlan(segments: TtsSegment[], hasAudio = true): void {
    if (!segments.length) {
      this.subtitlePlan = null;
      this.subtitlePlanEndMs = 0;
      publishSubtitleProgress(null);
      publishSubtitlePlanPresence(false);
      // 空计划说明这一轮不会有时间轴：立刻把挂起的整段文本放出来，
      // 不给长者留一段「有声无字」的等待。
      this.flushPendingReplyText();
      return;
    }
    this.settleReplyText();
    this.planHasAudio = hasAudio;
    this.subtitlePlan = segments;
    this.subtitlePlanEndMs = segments.reduce((max, seg) => Math.max(max, seg.start_ms + seg.duration_ms), 0);
    // 拿到真实时长就作废兜底（两者取「已知计划」优先，兜底只在没计划时生效）
    this.planFallbackMs = 0;
    this.subtitleIndex = -1;
    // 有计划的句子：字幕组件首帧必须从 0 个字开始打，否则会先整句闪一下再缩回去
    publishSubtitlePlanPresence(true);
    // 计划到达 ≈ 首段音频已经入队（服务端在入队成功后才发计划）：若此时说话窗已被
    // 12s 心跳兜底关掉（问候语合成慢的极端情形），必须重新开窗，否则音频真到的那一帧
    // 没有 rAF 在跑 —— 长者看到「字幕在走、嘴不动」（2026-11-11 用户报的就是这个）。
    if (!this.speakingWindow) this.openSpeakingWindow();
    // 记下「计划到达时刻」作为**墙钟兜底**的零点，并立刻对齐一次。
    // - 估算计划（没有音频）：`playbackElapsedMs` 直接用墙钟，从这一刻逐字；
    // - 带音频的计划：`playbackElapsedMs` 在首帧能量前停在 0，这里对齐到的是
    //   「第 0 句、0 个字」（BigSubtitle 有计划时首帧本就从 0 打起），先把句子容器
    //   放上屏，等真出声再逐字——不会领先声音，也不会整句闪一下。
    // 之前「计划一到就按墙钟起跑、出声再倒推采样锚点」会让问候语字幕领先声音
    // 一个建轨空档（数秒），2026-11-11 改为停在 0 等首帧能量。
    if (this.subtitleAnchorAt === null) {
      this.subtitleAnchorAt = performance.now();
      this.advanceSubtitle(this.subtitleAnchorAt);
    } else this.advanceSubtitle(performance.now());
  }

  /**
   * 挂起回复文本：等 `tts_segments` 到了按句逐字，等不到才整段上屏。
   *
   * 为什么不能立刻上屏（2026-11-10 实机取证）：服务端先发 `tts_text`、再合成音频、
   * 再补计划。先整段上屏的话，长者看到的是「整句亮一下 → 缩回去 → 重新逐字」，
   * 采样到的字符数序列是 21→22→3→9→10→21→22，像卡顿。
   */
  private armReplyText(text: string): void {
    this.clearReplyTextTimer();
    this.pendingReplyText = text;
    this.replyTextTimer = setTimeout(() => {
      this.replyTextTimer = null;
      this.flushPendingReplyText();
    }, REPLY_TEXT_FALLBACK_MS);
  }

  /** 计划已接手：撤掉兜底定时器并丢弃挂起文本 */
  private settleReplyText(): void {
    this.clearReplyTextTimer();
    this.pendingReplyText = null;
  }

  /** 兜底：把挂起的整段文本上屏（降级 TTS / 旧服务端 / 空计划） */
  private flushPendingReplyText(): void {
    this.clearReplyTextTimer();
    const pending = this.pendingReplyText;
    this.pendingReplyText = null;
    if (pending) {
      this.subtitleVisible = true;
      this.cb.onSubtitle(pending, "reply", this.localReply);
    }
  }

  private clearReplyTextTimer(): void {
    if (this.replyTextTimer !== null) {
      clearTimeout(this.replyTextTimer);
      this.replyTextTimer = null;
    }
  }

  /**
   * 音频始终没来时的收口兜底：把整份计划合成一段文本一次性上屏。
   *
   * 带音频的计划在首帧能量前停在 0 字（`playbackElapsedMs`），若声音永远不来，
   * 收窗后 rAF 停摆、进度冻结在 0 —— 长者看到的是「空字幕」。这里清掉计划与进度通道，
   * 让 BigSubtitle 退回整段显示（没有声音时，字至少要在）。只在「等音频超时」分支调，
   * 打断/断链路径有自己的收口（onSubtitle("") / disconnected），不走这里。
   */
  private flushPlanAsText(): void {
    const plan = this.subtitlePlan;
    if (!plan || plan.length === 0) return;
    const full = plan.map((seg) => seg.text).join("");
    this.resetSubtitlePlan();
    this.subtitleVisible = true;
    this.cb.onSubtitle(full, "reply", this.localReply);
  }

  /**
   * 本轮已播放毫秒 —— **三线共用的那一个数**。
   *
   * 优先用播放侧采样计数：它由音频设备时钟推进，与主线程掉帧无关，零点精度 2.67ms。
   * 采样锚点还没定（本轮还没出声）时：带音频的计划**停在 0** 等首帧能量，不从计划
   * 到达就跑墙钟——否则问候语「计划先到、建轨空档数秒」会让字幕一路领先声音。
   * 估算计划（没有音频）或 AudioContext 被挂起（声音不会来）才退回墙钟差，功能不减。
   */
  private playbackElapsedMs(now: number): number {
    if (this.clockAnchorSamples !== null) {
      return ((this.clockSamples - this.clockAnchorSamples) / this.clockSampleRate) * 1000;
    }
    if (this.subtitleAnchorAt === null) return 0;
    const ctxRunning = this.audioCtx === null || this.audioCtx.state === "running";
    if (this.planHasAudio && this.lipAwaitingEnergy && ctxRunning) return 0;
    return now - this.subtitleAnchorAt;
  }

  /**
   * 按播放锚点推进字幕：取 `start_ms ≤ 已播放毫秒` 的最后一句上屏。
   * 锚点是「第一段音频真的出声」的那一帧（与口型同一个能量判定点），
   * 服务端的时间轴按播放流落位换算（含句间静音），两端零点一致 → 不需要额外握手。
   *
   * 本函数由口型 rAF **每帧**调用，所以除了「换句」之外还负责发布**句内进度**
   * （`publishSubtitleProgress`）：逐字揭示要的是「这一句播到几毫秒了」，而换句只是
   * 每秒一次的事件；进度每帧写进模块变量、不触发 React，组件自己在 rAF 里读。
   */
  private advanceSubtitle(now: number): void {
    const plan = this.subtitlePlan;
    if (!plan || (this.subtitleAnchorAt === null && this.clockAnchorSamples === null)) return;
    const elapsed = this.playbackElapsedMs(now);
    const index = pickSegmentIndex(plan, elapsed);
    const progress = segmentProgressAt(plan, elapsed, index);
    if (!progress) {
      publishSubtitleProgress(null);
      return;
    }
    publishSubtitleProgress(progress);
    if (progress.index === this.subtitleIndex) return;
    this.subtitleIndex = progress.index;
    this.subtitleVisible = true;
    this.cb.onSubtitle(progress.text, "reply", this.localReply);
  }

  /**
   * 一段语音静音结束：口型归零、播放胶囊回「待机」，但**不关窗**。
   * 逐句播放时句间停顿会走到这里；下一句读到能量时 rAF 会把状态推回「正在说」（见 stepLipSync）。
   * 只发一次 listening：`playbackIdle` 守卫，否则每帧都会打一次状态。
   */
  private markPlaybackIdle(): void {
    if (this.playbackIdle) return;
    this.playbackIdle = true;
    this.lipMouth = 0;
    this.lipEnergyAvg = 0;
    this.lipSpeaking = false;
    this.avatar?.setMouthOpen(0);
    this.avatar?.setSpeaking(false);
    this.cb.onStatus("listening");
  }

  /**
   * 整段播放确定结束（`<audio>` 的 ended）：收窗并让胶囊回「待机」。
   * 与 `closeSpeakingWindow()` 分开，是因为关窗在 teardown/断链路径上也会被调，
   * 那些路径有自己的状态（disconnected），不能在这里覆盖成 listening。
   */
  private endPlayback(): void {
    this.closeSpeakingWindow();
    this.cb.onStatus("listening");
  }

  /** 本会话是否已经报过口型帧异常（只报一次，避免每帧刷屏） */
  private lipFaultReported = false;

  /**
   * 口型帧抛错时的收口（2026-11-11）。
   *
   * 以前这里没有兜底：`stepLipSync` 里任何一行抛错（analyser 取数据时上下文已关、
   * VRM 内部写表情失败……）都会让 `requestAnimationFrame` 链**永久断掉**，而
   * `speakingWindow` 还是 true、嘴还停在上一帧张开的量上 —— 用户看到的就是
   * 「数字人一直张口卡顿、保持一个姿势、胶囊一直是播放·正在说」。
   * 现在：嘴归零 + 胶囊回待机 + 关窗 + 记一次错，下一轮还能正常起。
   */
  private onLipFault(err: unknown): void {
    try {
      this.avatar?.setMouthOpen(0);
      this.avatar?.setSpeaking(false);
    } catch {
      // 收口阶段再抛就彻底放弃画面，只保证状态机不卡死
    }
    this.lipMouth = 0;
    this.lipSpeaking = false;
    this.markPlaybackIdle();
    this.closeSpeakingWindow();
    if (this.lipFaultReported) return;
    this.lipFaultReported = true;
    const message = err instanceof Error ? err.message : String(err);
    this.cb.onSubtitle(`声音播放出了点问题，已自动恢复（${message}）`, "notice");
  }

  /**
   * 口型 / 播放态的排障快照（只在 `localStorage["sm:lipDebug"] === "1"` 时写全局）。
   *
   * 为什么要有：2026-11-11 用户报「开场白有时一直卡在『播放·正在说』、口型紊乱」。
   * 光看胶囊文本分不清是「能量一直在」「说话窗没关」「分析器接错流」还是「窗口被反复重开」，
   * 这些都得看内部量。默认关闭：不写全局、不额外分配，行为与时序零变化。
   */
  private lipDebugFlag: boolean | null = null;
  private publishLipDebug(now: number, rms: number, target: number): void {
    if (this.lipDebugFlag === null) {
      try {
        this.lipDebugFlag = globalThis.localStorage?.getItem("sm:lipDebug") === "1";
      } catch {
        this.lipDebugFlag = false; // 隐私模式下 localStorage 会抛：只关调试，不影响播放
      }
    }
    if (!this.lipDebugFlag) return;
    (globalThis as unknown as Record<string, unknown>).__padLip = {
      t: Math.round(now),
      rms: Number(rms.toFixed(4)),
      target: Number(target.toFixed(3)),
      mouth: Number(this.lipMouth.toFixed(3)),
      speaking: this.lipSpeaking,
      silence: Number(this.lipSilenceTime.toFixed(2)),
      avg: Number(this.lipEnergyAvg.toFixed(4)),
      awaiting: this.lipAwaitingEnergy,
      window: this.speakingWindow,
      hasAnalyser: this.analyser !== null,
      hasTrackSrc: this.streamSource !== null,
      hasElSrc: this.elementSource !== null,
      muted: this.cb.audioEl?.muted ?? null,
      paused: this.cb.audioEl?.paused ?? null,
      // 三线同步取证（B0/B8）：光看 rms/mouth 分不清「字幕走快了」还是「时钟走慢了」，
      // 必须把**那一个数**（已播放毫秒）和它的来源一起暴露出来。
      elapsedMs: Math.round(this.playbackElapsedMs(now)),
      samples: this.clockSamples,
      anchorSamples: this.clockAnchorSamples,
      sampleRate: this.clockSampleRate,
      segIndex: this.subtitleIndex,
      segCount: this.subtitlePlan?.length ?? 0,
      planEndMs: this.subtitlePlanEndMs,
    };
  }

  private startLipLoop(): void {
    // 2026-11-10：**不再要求 avatar 就绪**。此前 `if (this.lipRaf || !this.avatar) return`
    // 让「口型循环」同时承担了「播放态判定」—— 3D 加载失败（降级静态头像，FR-3D-02 允许）
    // 或 WebGL 不可用时 avatar 为 null，rAF 压根不跑，于是「播放·正在说」永远不出现、
    // 逐句字幕的时间轴也没有推进的驱动。口型与播放态是两件事，驱动只与播放有关。
    // `setMouthOpen`/`setSpeaking` 都走可选链，没有 3D 时自然只更新状态。
    if (this.lipRaf) return;
    this.lipRaf = requestAnimationFrame(this.stepLipSync);
  }

  /**
   * 每帧：取 RMS → NOISE_GATE/GAIN 出 target → 真实 dt 起音/收音跟随 → setMouthOpen；
   * 说话态判定与静音判停也统一在这里（§5.2 第 4 条「以 rAF 为准」）。
   * 公式逐行对齐 engine.js:1157-1188。用箭头函数固定 this（rAF 回调里没有绑定）。
   * 注：后台标签页 rAF 会被暂停，回到前台的第一帧 dt 很大 → 走 release/静音累计一帧到位，
   * 立即判停收窗，不会留下一个永久讲话的数字人（engine.js 主循环同语义，不加 clamp 原样照抄）。
   */
  private stepLipSync = (): void => {
    this.lipRaf = 0;
    if (!this.speakingWindow) return;
    // 整帧包一层 try：此前任一帧抛错（analyser 在轨/上下文刚关掉时取数据、
    // controller 写入时 VRM 内部异常）都会把整条 rAF 链掐死 —— 于是嘴停在上一帧的
    // 开合度、胶囊永远停在「播放·正在说」，正是 2026-11-11 用户报的「一直张口卡顿」。
    // 现在出错就当场收口并记一次错，链不会断；下一轮 openSpeakingWindow 仍能重新起。
    try {
      this.lipFrame();
    } catch (err) {
      this.onLipFault(err);
    }
  };

  private lipFrame(): void {
    const now = performance.now();
    const dt = (now - this.lipLastAt) / 1000;
    this.lipLastAt = now;
    // P1-7：等待态里每帧自愈一次——轨可能在开窗之后才挂到 srcObject 上，或 ontrack 当时
    // createMediaStreamSource 抛过（还没音轨）。补接成功后本函数即空转，读到能量就不再调。
    if (this.lipAwaitingEnergy) this.ensureTrackAnalyser();
    const rms = this.readRms();
    this.lipEnergyAvg = this.lipEnergyAvg * 0.86 + rms * 0.14;
    // target = clamp((rms − NOISE_GATE) × GAIN, 0, 1)（engine.js:1167；v1.1 的 rms×4 废弃）
    let target = (rms - MOUTH_NOISE_GATE) * MOUTH_GAIN;
    target = target > 1 ? 1 : target > 0 ? target : 0;
    // 空闲期的残余噪声不许动嘴（2026-11-11 用户报「口型紊乱」）：平滑能量没到说话门限
    // 就不张嘴——孤立尖峰能把 target 顶到 0.5 以上，嘴会一跳一跳。真正说话时
    // 均值一两帧就过线，起音只慢约 30ms，肉眼看不出。
    if (this.lipEnergyAvg < MOUTH_SPEAK_THRESHOLD * 0.6) target = 0;
    // 「文字还没出来，人先张口」（2026-11-11 用户报）：口型读的是音量包络，而文字可能还挂在
    // `pendingReplyText` 里等 `tts_segments`。出声那一帧若屏幕上还没有字，就先把挂起的整段放出来，
    // 让字和声音**同一帧**到达；仍没有字（例如只有提示语的一轮）就把嘴闭上——
    // 宁可慢一帧，也不许出现「空气说话」。
    // 打字回复的音频走 <audio> 元素（`isElementPlaying()`），文本由服务端时间轴推进；两种来源都算「有字」。
    if (target > 0 && !this.subtitleVisible && !this.isElementPlaying()) {
      this.flushPendingReplyText();
      if (!this.subtitleVisible) target = 0;
    }
    this.publishLipDebug(now, rms, target);
    // 越过静噪门限 = 真的读到声音：首句等待态结束，后续 ontrack/自愈不再干预本句计时
    if (target > 0) {
      this.lipAwaitingEnergy = false;
      // 心跳判据（2026-11-11 复审 R7）：单帧尖峰（编码噪声 / 坏轨咔哒）曾把这里刷新一次，
      // 于是 watchdog 永远不触发——而静音要连续 3s 才收口，尖峰间隔 <3s 时三条出口全堵死，
      // 表现就是「一直张口 + 胶囊卡在正在说」。要求**连续 ≥3 帧**有能量（约 50ms）才算真在出声。
      this.lipProgressStreak += 1;
      if (this.lipProgressStreak >= LIP_PROGRESS_STREAK_REQUIRED) this.lipLastProgressAt = now;
      // 时间轴零点：第一段音频真的出声的那一刻。一条回复只定一次（重新出声不重置，
      // 否则逐句播放的句间停顿会把字幕推回第一句）。
      // 时间轴零点 = 第一段音频**真的出声**的那个采样（worklet 记的 peakSamples，2.67ms 精度）。
      // 远端轨（问候语/系统播报）与 <audio> 元素（打字回复）**共用这一个锚点**：能量判定点
      // 就是出声点，不再按墙钟或元素播放位置倒推。倒推会把「计划到达 → 真出声」之间的
      // 建轨/解码空档当成已播时长，字幕一路领先声音（问候语领先数秒、打字轮曾领先 68ms）。
      // 一条回复只定一次（重新出声不重置，否则句间停顿会把字幕推回第一句）。
      if (this.subtitleAnchorAt === null) this.subtitleAnchorAt = now;
      if (this.clockAnchorSamples === null) {
        if (this.clockEnergySamples !== null) {
          this.clockAnchorSamples = this.clockEnergySamples;
        } else {
          // 没有采样时钟（老内核 / worklet 加载失败）：墙钟零点也取「真出声」这一帧，
          // 而不是计划到达那一帧，同样不让建轨空档混进已播时长。
          this.subtitleAnchorAt = now;
        }
      }
      // P1-6 ⑥：第一帧真有能量 = 首段音频真的出声，这是「1.5s 开口」的服务端唯一证据来源
      if (!this.playbackReported) this.reportPlaybackStart(now);
    } else {
      this.lipProgressStreak = 0;
    }
    if (target > this.lipMouth) {
      // 起音：快跟（engine.js:1170；速率 18，MOUTH_ATTACK=1.0 在 engine.js:28 是死常量不落地）
      this.lipMouth += (target - this.lipMouth) * Math.min(1, dt * MOUTH_ATTACK_RATE);
    } else {
      // 收音：慢缓（engine.js:1172；MOUTH_RELEASE=3.6）
      this.lipMouth = Math.max(target, this.lipMouth - dt * MOUTH_RELEASE_RATE);
    }
    // VrmAvatar 内部分配 aa×0.85 + ih×0.12（缺 ih 降级 aa-only，写值 try/catch 在那边收口）
    this.avatar?.setMouthOpen(this.lipMouth);

    // 说话判定只认能量阈值（engine.js:1176）：有声 → silenceTime 归零，静音累计
    // ≥ SILENCE_HOLD(0.45s) → 判停收窗（取代 v1.1 的「静音 1.2s 兜底」）。
    // 说话判定用平滑能量（见 lipEnergyAvg 注释）：单帧尖峰不算「在说」，
    // 否则一次抖动就把静音累计清零，「正在说」永远收不了口。
    const speaking = this.lipEnergyAvg > MOUTH_SPEAK_THRESHOLD;
    let silenceEnded = false;
    if (speaking) {
      this.lipSilenceTime = 0;
    } else {
      this.lipSilenceTime += dt;
      silenceEnded = this.lipSilenceTime >= MOUTH_SILENCE_HOLD;
    }
    if (speaking !== this.lipSpeaking) {
      this.lipSpeaking = speaking;
      this.avatar?.setSpeaking(speaking);
      if (speaking) {
        // 静音后又出声（逐句播放的下一句）：胶囊从「待机」回到「正在说」
        this.playbackIdle = false;
        this.cb.onStatus("speaking");
      }
    }
    if (this.lipAwaitingEnergy) {
      // 文本已到、音频还在路上（TTS 合成 + 建轨，开场白实测 3–5s）：这段空档不是「播完」，
      // 不能按静音判停，否则口型与「正在说」会在出声之前就收掉。等满上限才认作出问题。
      this.lipWaitAudioTime += dt;
      if (this.lipWaitAudioTime >= PLAYBACK_WAIT_AUDIO_TIMEOUT) {
        this.markPlaybackIdle();
        this.closeSpeakingWindow();
        // 等满上限仍没有声音：带音频的计划此前一直停在 0 字，收窗后 rAF 不再推进，
        // 不兜底就会永远空屏。把计划整段上屏——没有声音时，字至少要在。
        this.flushPlanAsText();
        return;
      }
    } else if (silenceEnded) {
      // 只是**这一段**说完了：口型已经收住；胶囊等够 PLAYBACK_STATUS_HOLD 再翻「待机」，
      // 避开逐句播放的句间停顿（窗还留着——后面还可能有下一句）
      // 元素路径（打字回复）例外：音频明明在播，句间停顿不算播完，否则胶囊会来回闪。
      const elementPlaying = this.isElementPlaying();
      if (!elementPlaying && this.lipSilenceTime >= PLAYBACK_STATUS_HOLD) this.markPlaybackIdle();
      if (!elementPlaying && this.lipSilenceTime >= PLAYBACK_IDLE_STOP) {
        this.closeSpeakingWindow(); // 连续 3s 无声：整段确实播完了，停 rAF 省电
        return;
      }
    }
    // 字幕跟着同一条时间轴走：锚点已定就每帧对一次（内部按句序号去重，不会重复回调）
    this.advanceSubtitle(now);
    // 兜底：整整 PLAYBACK_STUCK_TIMEOUT 秒没有任何能量，说明这一轮的声音不会再来了。
    // 走 endPlayback 而不是 closeSpeakingWindow：前者额外发 listening，胶囊才会真的翻回「待机」；
    // 后者只关窗（teardown/断链路径复用），单用会让胶囊停在「正在说」——正是被报的那个 bug。
    if (now - this.lipLastProgressAt > PLAYBACK_STUCK_TIMEOUT * 1000) {
      this.endPlayback();
      // 声音始终没来（计划停在 0 字）：收窗后整段上屏兜底，别让字幕空着。
      // 正常播完时 closeSpeakingWindow 已清掉计划，flushPlanAsText 见空计划即返回。
      this.flushPlanAsText();
      return;
    }
    // 计划时长到点即收口：见 subtitlePlanEndMs 注释（远端轨的残留噪声会把静音判定拖长）。
    // 没有计划（只发 tts_text 的服务端 / 分段丢帧）时退回 planFallbackMs，否则这条出口整条失效，
    // 只剩「静音 3s」与「12s 心跳」——而残留尖峰恰好能同时废掉那两条（复审 P1-6）。
    const planEndMs = this.subtitlePlanEndMs > 0 ? this.subtitlePlanEndMs : this.planFallbackMs;
    if (
      (this.subtitleAnchorAt !== null || this.clockAnchorSamples !== null) &&
      planEndMs > 0 &&
      this.playbackElapsedMs(now) > planEndMs + PLAYBACK_END_GRACE_MS
    ) {
      this.endPlayback();
      return;
    }
    this.lipRaf = requestAnimationFrame(this.stepLipSync);
  };

  /** 字节时域 → 归一化 RMS（0~1）。无声/无图时返回 0。 */
  private readRms(): number {
    // 时钟建起来了就用它的 quantum RMS：与字幕读的是同一条消息，且没有 AnalyserNode
    // 的 smoothingTimeConstant 滞后与 FFT 窗口糊化。
    if (this.clockNode) return this.clockRms;
    const analyser = this.analyser;
    const buf = this.lipBuf;
    if (!analyser || !buf) return 0;
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i += 1) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / buf.length);
  }

  /** 会话结束/组件卸载：取消 rAF、断开全部节点、关音频图（下一会话重建）。 */
  private disposeLipSync(): void {
    this.closeSpeakingWindow(); // 先用还活着的控制器把口型/说话态收掉
    this.avatar = null;
    this.detachTrackAnalyser();
    try {
      this.elementSource?.disconnect();
    } catch {
      /* 已断开 */
    }
    this.elementSource = null;
    this.analyser?.disconnect();
    this.analyser = null;
    this.lipBuf = null;
    this.clockNode?.disconnect();
    this.clockNode = null;
    this.clockSink?.disconnect();
    this.clockSink = null;
    this.clockLoading = null;
    this.clockSamples = 0;
    this.clockRms = 0;
    this.clockEnergySamples = null;
    this.clockAnchorSamples = null;
    const ctx = this.audioCtx;
    this.audioCtx = null;
    if (ctx && ctx.state !== "closed") void ctx.close().catch(() => {});
  }
}
