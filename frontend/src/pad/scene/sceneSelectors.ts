/**
 * 场景选择器（场景化改造 · 批一）—— 把 `SceneState` 翻译成「界面上该出现哪些层」。
 *
 * 为什么要在状态机之上再有一层选择器：`sceneMachine.ts` 只回答「现在处在哪一幕」，
 * 而 `App.tsx` 真正需要的是三个派生量——舞台该渲染哪个面板（`visibleLayers`）、
 * 顶栏该出现哪些按钮（`topBar`）、底部兜底条该不该出现（`footerMode`）。把这些规则
 * 写进组件里，规则就散落在 21 个 `useState` 的分支中；写成纯函数后它们可以被用例逐条钉住，
 * 批二收敛渲染时也只需要替换调用点，不改规则本身。
 *
 * 三条边界（与 `sceneMachine.ts` 对齐，别在这里越界）：
 * 1. **`activeTask` 仍是任务提示的唯一真源**：本层只把 `scene === "task"` 翻译成
 *    `task: true`，不判断「该不该弹」。
 * 2. **本层只回答「该有哪些层在场」**：在场但互相冲突时谁可见，仍由
 *    `overlayPriority.ts` 的 `pickVisible` 决定，选择器不排序、不裁决。
 * 3. **纯函数、无 React**：不 import `react`、不 import `App.tsx`，入参出参都是普通对象。
 *
 * `settings` 与 `wakeWord` 两条**恒为 `false`**：它们不进场景机（见 `sceneMachine.ts`
 * 顶部注释——设置面板是独立路由式浮层，唤醒词配置在设置里），因此不会由任何场景或
 * 打断层派生出来。保留这两个键是为了让返回结构与 `overlayPriority` 的层名对齐，
 * 批二把渲染收敛到本层之后，App 侧就不用再各自拼一份键集合。
 */
import type { SceneState } from "./sceneMachine";

/** 舞台上「在场」的层；键名与 `overlayPriority.ts` 的层概念一一对应。 */
export interface VisibleLayers {
  /** 照护任务卡（用药 / 采集 / SOS 结果）：`scene === "task"`。 */
  task: boolean;
  /** 戏曲/影视播放器：只有显式 `leisure.open("media")` 才有。 */
  media: boolean;
  /** 记忆回顾：只有显式 `leisure.open("memory")` 才有。 */
  memory: boolean;
  /** 声纹确认层：`interruption === "identity"`。 */
  identity: boolean;
  /** 声纹录入向导：`interruption === "enroll"`。 */
  enroll: boolean;
  /** 唤醒词配置：不进场景机，恒 `false`。 */
  wakeWord: boolean;
  /** 设置面板：不进场景机，恒 `false`。 */
  settings: boolean;
}

/** 顶栏三项：批二收敛后只剩「状态条 + SOS + 设置」，均为静态入口。 */
export interface TopBarState {
  sos: boolean;
  settings: boolean;
  status: boolean;
}

/**
 * 底部兜底区形态：
 * - `hidden`：有打断层（来电/外呼/身份/声纹），兜底条让位给打断层；
 * - `dialog`：对话场景，底部是输入/语音兜底；
 * - `fallback`：其余场景（待机、任务、消遣）保留最小兜底入口。
 */
export type FooterMode = "hidden" | "fallback" | "dialog";

/** 当前这一幕该有哪些层在场。 */
export function visibleLayers(state: SceneState): VisibleLayers {
  const isTask = state.scene === "task";
  const isLeisure = state.scene === "leisure";

  return {
    // `sos` 也是 `TaskName`，`scene` 同样是 `task`：SOS 结果页因此天然落在任务卡上，
    // 不需要为它单开一层（FR-UI-08 要求它不可被覆盖，这条由状态机保证）。
    task: isTask,
    media: isLeisure && state.leisure === "media",
    memory: isLeisure && state.leisure === "memory",
    // 打断层独立于场景：身份确认与声纹录入期间，当前场景的层仍「在场」但不该抢焦点。
    identity: state.interruption === "identity",
    enroll: state.interruption === "enroll",
    wakeWord: false,
    settings: false,
  };
}

/**
 * 顶栏该出现哪些入口：三项恒 `true`（批二目标）。
 *
 * 参数保留是为了签名稳定——批二真正收敛渲染时，若「外呼期间隐藏设置」这类规则要回来，
 * 只改这个函数的实现，调用点不用动。
 */
export function topBar(_state: SceneState): TopBarState {
  return { sos: true, settings: true, status: true };
}

/** 底部兜底区的形态；打断层优先于对话形态（打断期间兜底条必须让位）。 */
export function footerMode(state: SceneState): FooterMode {
  if (state.interruption !== null) return "hidden";
  if (state.scene === "dialog") return "dialog";
  return "fallback";
}
