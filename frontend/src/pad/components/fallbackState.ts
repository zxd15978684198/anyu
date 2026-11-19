/**
 * 长者端两级降级入口的判定（AGENTS §6：WebRTC 主链路 → 按住说话 → 页面输入框）。
 *
 * 为什么拆成纯 `.ts`：`scripts/node-ts-hook.mjs` 只认 `.ts`，`.tsx` 进不了 node:test；
 * 而「什么时候能用、提示哪句话」恰好是最容易被改回去的一层——把它做成真函数，
 * 门禁里就能断言行为本身，而不是拿正则去猜 JSX 渲染结果。
 *
 * 两个入口的**可见性口径**（2026-11-10 task-69）：
 *  - 按住说话**常驻**：主链路正常时也能按（走 C3 REST 整段上传），断线时更要能按。
 *    此前它被 `status === "disconnected"` 门住，主链路一好老人就再也找不到这条路。
 *  - 打字输入只在后端真有纯文本入口时才启用；否则按钮留在原地但禁用，并明说原因，
 *    绝不做一个按下去没反应（或假装成功）的假入口。
 */

import type { PadStatus } from "../types";

/** 入口名：用于无障碍标签与用例断言，避免两处各写一套中文。 */
export const TALK_ENTRY_LABEL = "按住说话";
export const TEXT_ENTRY_LABEL = "打字";

/** 按住说话的「什么时候用它」：连着与断开各一句，都不是「请稍后再试」。 */
export const TALK_HINT_LIVE = "如果它没听清，按住这里说一遍，会整段上传";
export const TALK_HINT_OFFLINE = "现在连不上了，按住这里说话也能让数字人听懂";

/** 打字输入的「什么时候用它」：可用 / 后端未接通各一句。 */
export const TEXT_HINT_READY = "不方便出声时，在这里打字或手写，再点发送";
export const TEXT_HINT_BLOCKED = "打字发送还没接通，先用上面的「按住说话」";

/** 一次最多打多少字：与后端 `MAX_CONTENT_LENGTH` 的记忆口径无关，只防老人误触粘贴长文。 */
export const MAX_TEXT_LENGTH = 200;

export interface TalkEntryState {
  /** 是否可点。按住说话**恒为 true**（唯一退路不许因上层状态消失）。 */
  readonly enabled: boolean;
  /** 显示在按钮上方的「什么时候用它」。 */
  readonly hint: string;
  /** 语音服务可用性未知/不可用：按钮不消失，但要预期它可能回降级话术。 */
  readonly degraded: boolean;
}

export interface TextEntryState {
  /** 是否可用（后端有纯文本入口 + 当前不在忙）。 */
  readonly enabled: boolean;
  readonly hint: string;
  readonly ready: boolean;
}

export interface FallbackState {
  readonly talk: TalkEntryState;
  readonly text: TextEntryState;
}

export interface FallbackInput {
  /** 主链路连接态（`PadStatus` 五态）。 */
  readonly status: PadStatus;
  /** ASR 是否可用；未知按可用处理，不能因为「查不到」就把按钮藏掉。 */
  readonly asrAvailable?: boolean;
  /** 后端是否已有纯文本对话入口（今天没有：`POST /api/audio/utterance` 只收音频）。 */
  readonly textEntryAvailable?: boolean;
}

/**
 * 判定两级降级入口的可用性与提示语。
 *
 * 注意这里**没有** `visible` 字段：两个入口都常驻渲染，「显示与否」不再是产品决策，
 * 只剩「能不能按」——这正是 task-69 要修的那处回退（旧实现把整块入口藏在断线分支里）。
 */
export function resolveFallbackState(input: FallbackInput): FallbackState {
  const { status, asrAvailable = true, textEntryAvailable = false } = input;
  const offline = status === "disconnected";
  const textReady = textEntryAvailable === true;

  return {
    talk: {
      // 恒可用：它是主链路之外唯一的活路。禁用会让断网时的老人彻底没有输入手段。
      enabled: true,
      hint: offline ? TALK_HINT_OFFLINE : TALK_HINT_LIVE,
      // ASR 挂了也不藏按钮：按下去会拿到 503 的降级话术，比「按钮消失」可解释。
      degraded: asrAvailable === false,
    },
    text: {
      enabled: textReady,
      hint: textReady ? TEXT_HINT_READY : TEXT_HINT_BLOCKED,
      ready: textReady,
    },
  };
}
