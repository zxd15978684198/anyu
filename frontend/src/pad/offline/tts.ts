/**
 * 本地播报：只用浏览器原生 `speechSynthesis`（PRD:338 FR-MED-08 第①层）。
 *
 * **红线（计划件 §5 R4）**：断网时云端 TTS 一定不可用，所以这里不许 `fetch`、
 * 不许 import 任何 TTS 客户端、不许退回「先试云端再降级」——那条路上断网时的
 * 表现是静默失败，而长者端最不能接受的就是「以为播了其实没播」。只用系统已装的
 * 中文语音，播不出来就交给大字弹窗兜底（`overlay.ts`）。
 *
 * 语音可用性因设备而异（CCRC 平板常见 MIUI / HarmonyOS 精简语音包），所以
 * `speakLocally()` 返回布尔值让调用方决定要不要额外提示「请家属检查语音设置」。
 */

/** 系统里是否至少有一个可用语音（`getVoices()` 在部分浏览器是异步填充的）。 */
export function canSpeakLocally(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

/** 优先挑中文语音：没有就交给浏览器默认（`utterance.lang` 仍写 zh-CN）。 */
function pickVoice(): SpeechSynthesisVoice | null {
  if (!canSpeakLocally()) return null;
  const voices = window.speechSynthesis.getVoices();
  if (voices.length === 0) return null;
  const zh = voices.filter((voice) => voice.lang.toLowerCase().startsWith("zh"));
  const preferred = zh.find((voice) => /zh[-_]CN/i.test(voice.lang));
  return preferred ?? zh[0] ?? null;
}

/**
 * 本地播报一段中文。返回 false 表示这台设备播不出来（调用方应保证大字弹窗仍显示）。
 *
 * 三条工程细节：
 * - **先 `cancel()`**：断网重连、连续两条提醒时，排队播报会变成「两段话叠着说」；
 * - **`rate` 0.85**：长者端默认语速偏快，PRD §5.3 的 1.5s 预算是接口预算，不约束本地语音；
 * - **不 `await`**：`speechSynthesis` 没有 Promise 接口，且播报失败不该阻塞弹窗渲染。
 */
export function speakLocally(text: string): boolean {
  if (!canSpeakLocally()) return false;
  if (typeof SpeechSynthesisUtterance === "undefined") return false;
  try {
    const synth = window.speechSynthesis;
    synth.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "zh-CN";
    utterance.rate = 0.85;
    utterance.pitch = 1;
    utterance.volume = 1;
    const voice = pickVoice();
    if (voice !== null) utterance.voice = voice;
    synth.speak(utterance);
    return true;
  } catch (error) {
    console.warn("[offline] 本地播报失败", error);
    return false;
  }
}

/** 停掉正在播的那一段（关弹窗时调用，避免人走了声音还在）。 */
export function stopSpeaking(): void {
  if (!canSpeakLocally()) return;
  try {
    window.speechSynthesis.cancel();
  } catch (error) {
    console.warn("[offline] 停止播报失败", error);
  }
}
