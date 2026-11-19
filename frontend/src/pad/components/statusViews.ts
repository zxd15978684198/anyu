/**
 * 长者端状态胶囊的**取值表**（纯数据 + 纯函数，不含 JSX）。
 *
 * 为什么单独一个 `.ts`：`StatusBar.tsx` 是组件，Node 的 `node --test` 只做类型擦除、不认 TSX，
 * 于是把「三枚胶囊各显示什么字」搬到纯 TS 模块里，测试才能真的跑起来断言，
 * 而不是去 grep 源码字符串（那种断言改个变量名就失效）。
 *
 * 口径（PRD line 571「状态条」，契约见 `设计记录（身份闭环缺口补齐）` §3-W4）：
 *  - 三枚胶囊分别回答**三个互不相关**的问题：连接通不通 / 这句话算谁的 / 小满在不在说话；
 *  - 每枚必须**自报家门**（`name`）+ **当前取值**（`label`）。只给取值时，长者看到
 *    「已断开 已确认 待机」三串字，无法判断哪枚管什么——2026-11-10 产品负责人实测后判定为可读性缺陷。
 *  - 屏幕文案一律按长者视角（2026-11-13）：**准备中 / 识别中 / 待确认** 都不用系统词，
 *    也不写成「要你去做点什么」。措辞必须落在本表里（而不是组件里覆盖），否则表格断言
 *    描述的是底座、不是界面。
 */

/** 胶囊底色/字色档位，与 `StatusBadge.tsx` 的信号色同源 */
export type BadgeTone = "danger" | "warning" | "success" | "info" | "neutral";

/**
 * 连接态（来自 `App.tsx` 的实际情况）：正在听 / 已断开 / 麦克风没声音。
 *
 * 「准备中」**不是**第三个连接态，而是「还没连成过」时对 `disconnected` 的说法
 * （2026-11-13 产品负责人实测：刚进页面就说「已断开」，像在报故障）。它由 `connectionView()`
 * 结合「曾经连上过没有」派生，所以「准备中」不占 `ConnectionState` 取值。
 *
 * `mic_unavailable`（2026-11-17 · A6 / R-语音-01/02）是**真连接态**：信令通着、但麦克风
 * 开不起来（权限被拒 / 被拔 / 被占用）。此时说「正在听」是假话——长者喊半天没反应，
 * 顶栏却告诉他系统在听。它与 `disconnected` 的区别是恢复动作不同：前者重建媒体会话，
 * 后者重连信令，所以必须是独立取值而不是「已断开」的另一种措辞。
 */
export type ConnectionState = "listening" | "disconnected" | "mic_unavailable";
/**
 * 身份态：
 *  - `unknown`：还没拿到服务端的身份判定（刚登录、刚连上）——**不能显示「已确认」**，
 *    那是把「不知道」当成「确认了」（2026-11-10 产品负责人实测：登录设备后立刻显示「身份·已确认」）；
 *  - `confirmed`：服务端下发 identity_result 且 source ≠ generic；
 *  - `generic`：通用模式三不（PRD FR-VPR-03）。
 */
export type IdentityState = "unknown" | "confirmed" | "generic";
/** 播放态：待机 / 正在说 */
export type PlaybackState = "idle" | "speaking";

export interface PillView {
  /** 这枚胶囊**是什么**（固定不变的标签，如「连接」） */
  name: string;
  /** 当前取值（长者端只用短词，不长句） */
  label: string;
  tone: BadgeTone;
  /** 读屏全称：说明这枚胶囊代表什么状态、当前值是什么 */
  ariaLabel: string;
}

export const CONNECTION_VIEW: Record<ConnectionState, PillView> = {
  listening: { name: "连接", label: "正在听", tone: "info", ariaLabel: "连接状态：正在听" },
  disconnected: { name: "连接", label: "已断开", tone: "neutral", ariaLabel: "连接状态：已断开" },
  /**
   * 麦克风开不起来（A6）：信令通着，但系统**没有在听**。
   * 用 warning 而非 danger：不是急症、不是服务故障，是要长者看一眼插头/权限；
   * 屏幕上不能出现「麦克风」以外的系统词（「音频输入设备」之类长者不认识）。
   */
  mic_unavailable: {
    name: "连接",
    label: "麦克风没声音",
    tone: "warning",
    ariaLabel: "连接状态：麦克风没有声音，请检查麦克风是否插好，或按“重试麦克风”",
  },
};

/**
 * 「还没连成过」时给 `disconnected` 的说法（2026-11-13 · 状态条措辞）。
 *
 * 开机 / 刚进页面那一瞬间物理上就是没连上，但对长者说「已断开」等于报故障；
 * 真正该说「已断开」的是**连过又掉**。这个区别只能靠调用方记一个「曾经 listening 过」的事实，
 * 所以 `ConnectionState` 不加第三档——加档要改 `App.tsx` 的状态机，而措辞不该污染状态机。
 */
export const CONNECTION_PREPARING: PillView = {
  name: "连接",
  label: "准备中",
  tone: "neutral",
  ariaLabel: "连接状态：准备中，正在建立连接",
};

/** 连接胶囊的取值：三档实际状态 + 一个「连成过没有」的事实 → 四句屏幕文案 */
export function connectionView(state: ConnectionState, everConnected: boolean): PillView {
  // 麦克风不可用与「连过没有」无关：这是确定的故障，不是「还没准备好」
  if (state === "mic_unavailable") return CONNECTION_VIEW.mic_unavailable;
  if (state === "listening") return CONNECTION_VIEW.listening;
  return everConnected ? CONNECTION_VIEW.disconnected : CONNECTION_PREPARING;
}

export const IDENTITY_VIEW: Record<IdentityState, PillView> = {
  unknown: {
    name: "身份",
    // 「待确认」像是要长者去做点什么；他其实什么都不用做，系统正在认人
    label: "识别中",
    tone: "neutral",
    ariaLabel: "身份状态：识别中",
  },
  confirmed: { name: "身份", label: "已确认", tone: "success", ariaLabel: "身份状态：已确认" },
  generic: {
    name: "身份",
    // 「通用模式」是给开发看的词，长者只需要知道「还没认出是谁」
    label: "待确认",
    tone: "warning",
    // 通用模式必须让长者知道「为什么不叫出名字」，与 FR-VPR-03 三不保持一致
    ariaLabel: "身份状态：待确认，通用模式不读取个人记忆、不记录健康数据、不确认用药",
  },
};

export const PLAYBACK_VIEW: Record<PlaybackState, PillView> = {
  idle: { name: "播放", label: "待机", tone: "neutral", ariaLabel: "播放状态：待机" },
  speaking: { name: "播放", label: "正在说", tone: "info", ariaLabel: "播放状态：正在说" },
};

/** 屏幕上的完整文案：`标签 · 取值`。分隔符只做视觉留白，读屏靠 `ariaLabel`，不重复播报 */
export function pillText(view: PillView): string {
  return `${view.name} · ${view.label}`;
}
