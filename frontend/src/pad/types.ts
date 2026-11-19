/** pad 全局共享类型 */

/** 状态条状态：三态（正在听/正在想/正在说）+ 连接态（连接中/已断开），口径见 adhoc §3.3 */
export type PadStatus =
  | "listening" // 正在听
  | "thinking" // 正在想
  | "speaking" // 正在说
  | "connecting" // 连接中
  | "disconnected"; // 已断开

/**
 * 麦克风可用性（A6 / R-语音-01/02）：与连接态分开记。
 *
 * 信令通着但麦被拒/被拔，`PadStatus` 仍是 `listening`——把两者混成一个状态机会让
 * 「重建媒体会话」和「重连信令」两个恢复动作互相误触发。这里只表达一件事：
 * 这台机器现在能不能把长者的声音送出去。
 */
export type MicState = "ok" | "unavailable";
