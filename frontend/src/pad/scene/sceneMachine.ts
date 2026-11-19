/**
 * 长者端场景状态机（场景化改造 · 批一）——纯函数，无 React 依赖。
 *
 * 为什么要有这一层：`App.tsx` 用 22 个 `useState` 同时管顶栏、舞台、右栏、任务抽屉、
 * 戏曲、记忆、外呼、来电、声纹、引导、设置。`overlayPriority` 只解决了「谁压谁」，
 * 缺的是「现在到底处在哪一幕」这个上位概念。这里补的就是那一幕。
 *
 * 三条边界（写死，别在本文件里越界）：
 * 1. **`activeTask` 仍是任务提示的唯一真源**（`App.tsx` 里由派发逻辑写入）。本机
 *    只接收 `task.open` 事件，不自己判断「该不该弹」——reducer 不做业务裁决。
 * 2. **`pickVisible` 仍是唯一可见性出口**。本机只回答「该有哪些层在场」，
 *    「在场但冲突时谁可见」交给 `overlayPriority`，因此场景机不得直接决定渲染。
 * 3. **`sos` 不可被任何自动事件覆盖**：只有 `sos.dismiss`（用户/系统显式撤销）
 *    能把它关掉（FR-UI-08）；`interruption`（来电/外呼/身份/声纹）是打断层，
 *    不切场景，结束时回到进入前的场景（深度 1）。
 *
 * `loggedIn`（登录）与 `guideOpen`（首启引导）是 `MainScreen` 之外/边界的外层 gate，
 * 不进本机——它们的语义是「还不是这个页面」，不是「这个层被压住了」。
 */
export type SceneName = "standby" | "dialog" | "task" | "leisure";
export type TaskName = "medication" | "health" | "sos";
export type LeisureName = "media" | "memory";
export type InterruptionName = "incoming" | "outbound" | "identity" | "enroll";

export interface SceneState {
  scene: SceneName;
  /** 仅 `scene === "task"` 时非空；`sos` 只能由 `sos.dismiss` 撤销 */
  task: TaskName | null;
  /** 仅 `scene === "leisure"` 时非空 */
  leisure: LeisureName | null;
  /** 打断层独立于场景：进出期间 `scene` 不变 */
  interruption: InterruptionName | null;
  /** 深度 1：关闭后回到进入前的场景，不统一回待机 */
  previousScene: SceneName | null;
}

export const initialScene: SceneState = {
  scene: "standby",
  task: null,
  leisure: null,
  interruption: null,
  previousScene: null,
};

export type SceneEvent =
  | { type: "dialog.ask" }
  | { type: "dialog.end" }
  | { type: "task.open"; task: TaskName }
  | { type: "task.close" }
  | { type: "leisure.open"; leisure: LeisureName }
  | { type: "leisure.close" }
  | { type: "standby.enter" }
  | { type: "interruption.begin"; interruption: InterruptionName }
  | { type: "interruption.end" }
  | { type: "sos.dismiss" };

/** 进入新场景：记下上一幕（深度 1），清掉与上一幕绑定的子状态。 */
function enter(state: SceneState, scene: SceneName, patch: Partial<SceneState> = {}): SceneState {
  return {
    ...state,
    ...patch,
    scene,
    previousScene: state.scene === scene ? state.previousScene : state.scene,
  };
}

/** 退出当前场景：回到上一幕；没有上一幕就回待机。 */
function back(state: SceneState): SceneState {
  return {
    ...state,
    scene: state.previousScene ?? "standby",
    task: state.scene === "task" ? null : state.task,
    leisure: state.scene === "leisure" ? null : state.leisure,
    previousScene: null,
  };
}

export function reduceScene(state: SceneState, event: SceneEvent): SceneState {
  switch (event.type) {
    // ---------------- 对话 ----------------
    case "dialog.ask": {
      // 任务卡在场时不抢场景（长者正看着药单/采集/结果）；戏曲在响时也不抢（戏别停）。
      if (state.scene === "task" || state.scene === "leisure" || state.scene === "dialog") {
        return state;
      }
      return enter(state, "dialog");
    }
    case "dialog.end":
      return state.scene === "dialog" ? back(state) : state;

    // ---------------- 任务（照护） ----------------
    case "task.open": {
      // 不变量 2：自动任务可压 leisure，绝不可压 sos。
      if (state.task === "sos" && event.task !== "sos") return state;
      if (state.scene === "task" && state.task === event.task) return state;
      return enter(state, "task", { task: event.task, leisure: null });
    }
    case "task.close": {
      // 不变量 1：sos 不能被 task.close 关掉，只能 sos.dismiss。
      if (state.scene !== "task" || state.task === "sos") return state;
      return back(state);
    }
    case "sos.dismiss":
      return state.task === "sos" ? back(state) : state;

    // ---------------- 消遣 ----------------
    case "leisure.open": {
      // 不变量 4：leisure 只能由显式 leisure.open 打开，永不因自动事件进入。
      if (state.task === "sos") return state;
      if (state.scene === "leisure" && state.leisure === event.leisure) return state;
      // 不变量 5：从 dialog 进 leisure 时 dialog 不结束——previousScene 记住它，
      // 关掉播放器能回到对话，而不是被当成「对话已结束」。
      return enter(state, "leisure", { leisure: event.leisure, task: null });
    }
    case "leisure.close":
      return state.scene === "leisure" ? back(state) : state;

    // ---------------- 待机 ----------------
    case "standby.enter": {
      // 有打断层或有任务卡时不回待机（任务还没收口，不能把长者的等待页撤掉）。
      if (state.interruption !== null || state.scene === "task") return state;
      return { ...state, scene: "standby", task: null, leisure: null, previousScene: null };
    }

    // ---------------- 打断层 ----------------
    case "interruption.begin": {
      if (state.interruption !== null) return state; // 深度 1：不叠打断层
      // 不变量 3：打断层期间 scene 不变，结束回 previousScene。
      return { ...state, interruption: event.interruption, previousScene: state.scene };
    }
    case "interruption.end": {
      if (state.interruption === null) return state;
      return {
        ...state,
        interruption: null,
        scene: state.previousScene ?? "standby",
        previousScene: null,
      };
    }
    default:
      return state;
  }
}

/** 该场景下是否允许「自动事件」打开某层：只有照护任务与对话可以，消遣不行。 */
export function isAutoScene(scene: SceneName): boolean {
  return scene !== "leisure";
}
