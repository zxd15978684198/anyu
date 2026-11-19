/**
 * 唤醒词的设备级持久化（task-70）。
 *
 * 单独一个 `.ts` 而不是塞进 `WakeWordSettings.tsx`：存储逻辑要能被 node:test 直接 import
 * （测试跑的是 `.ts`，`.tsx` 要过 JSX 转译，没必要为了三个函数引入这套复杂度）。
 *
 * 键为什么带 device_id：`localStorage` 按**浏览器**隔离，不按账号；一台演示机先后登录
 * 多个设备号时不带维度会互相覆盖。读失败/存坏了（旧版本遗留）一律回退出厂词——
 * 设置是锦上添花，不该因为一条脏数据让长者叫不醒数字人。
 */

import { DEFAULT_WAKE_WORD, splitKeyword } from "./pinyin";

const STORAGE_PREFIX = "anyu.wakeWord.";

/** 存储键名（导出给测试与排查用；改前缀等于让所有人的设置失效，别随手改） */
export function wakeWordStorageKey(deviceId: string): string {
  return `${STORAGE_PREFIX}${deviceId}`;
}

/** 读取该设备保存的唤醒词；没存过、存坏了、localStorage 不可用都回退出厂词 */
export function loadWakeWord(deviceId: string): string {
  try {
    const stored = globalThis.localStorage?.getItem(wakeWordStorageKey(deviceId));
    if (stored && splitKeyword(stored)) return stored;
  } catch (err) {
    // 隐私模式/配额满时 localStorage 会抛 SecurityError；不影响唤醒，记一条就够
    console.warn("[pad] 读取唤醒词设置失败，使用出厂唤醒词", err);
  }
  return DEFAULT_WAKE_WORD;
}

/** 写入该设备的唤醒词；写不进去只告警（本次会话内仍然生效） */
export function saveWakeWord(deviceId: string, word: string): void {
  try {
    globalThis.localStorage?.setItem(wakeWordStorageKey(deviceId), word);
  } catch (err) {
    console.warn("[pad] 保存唤醒词失败（本次会话内仍生效，下次登录会恢复默认）", err);
  }
}
