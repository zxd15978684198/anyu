/**
 * 照护任务自动弹窗轮询（从 App.tsx 抽出，前端计划 W5）。
 *
 * 为什么抽这一条而不是拆渲染：App.tsx 的顶栏/浮层与 `overlayPriority` 仲裁深度耦合
 * （例如设置弹层的 `<details onToggle>` 要先问仲裁「有没有别的正式浮层在场」），
 * 抽成组件需要传十几个 prop，那正是本计划要消除的 prop drilling。
 * 这条轮询是**自包含**的：只读本地提醒、判到期、开任务，输入输出都清楚，抽出来零风险。
 *
 * 口径不变（逐条保留）：
 * - 同一时刻只允许一件任务占面板（`activeTaskRef.current !== null` 就跳过），
 *   两件并排会把数字人挤掉，而 PRD 要求「任务来时不整屏切页、人还在」；
 * - 已忽略的提醒不再弹（`dismissedTaskRef`）；
 * - IndexedDB 不可用（隐私模式/额度耗尽）时**跳过自动弹窗，不阻断语音主链路**；
 * - 15s 轮询 + 提醒更新事件双触发；卸载时清定时器与监听。
 */
import { useEffect, type Dispatch, type MutableRefObject } from "react";
import { readReminders } from "./offline/db";
import { REMINDERS_UPDATED_EVENT } from "./medicationView";
import {
  healthAskedKey,
  resolveDueMedication,
  shouldOpenHealth,
  type DueMedication,
} from "./taskDispatch";
import type { SceneEvent } from "./scene/sceneMachine";

/** 当前占着任务面板的那件事（同一时刻只允许一件）。 */
export type ActiveTask =
  | { kind: "medication"; due: DueMedication }
  | { kind: "health" }
  | { kind: "sos" }
  | null;

export interface TaskPollingOptions {
  /** 当前任务的镜像 ref：轮询闭包只注册一次，读不到最新 state，必须用 ref。 */
  activeTaskRef: MutableRefObject<ActiveTask>;
  /** 已被长者忽略的提醒 id，不再自动弹。 */
  dismissedTaskRef: MutableRefObject<Set<string>>;
  setActiveTask: (task: ActiveTask) => void;
  dispatch: Dispatch<SceneEvent>;
  /** 轮询间隔，默认 15s（与原实现一致）。 */
  intervalMs?: number;
}

export function useTaskPolling({
  activeTaskRef,
  dismissedTaskRef,
  setActiveTask,
  dispatch,
  intervalMs = 15_000,
}: TaskPollingOptions): void {
  useEffect(() => {
    let disposed = false;
    const tick = () => {
      void (async () => {
        if (disposed || activeTaskRef.current !== null) return;
        try {
          const rows = await readReminders();
          const due = resolveDueMedication(rows, new Date(), dismissedTaskRef.current);
          if (due) {
            dismissedTaskRef.current.add(due.reminderId);
            setActiveTask({ kind: "medication", due });
            dispatch({ type: "task.open", task: "medication" });
            return;
          }
        } catch {
          // IndexedDB 不可用（隐私模式/额度耗尽）：跳过自动弹窗，不阻断语音主链路
        }
        const asked = window.localStorage.getItem(healthAskedKey(new Date()));
        if (shouldOpenHealth(new Date(), asked)) {
          setActiveTask({ kind: "health" });
          dispatch({ type: "task.open", task: "health" });
        }
      })();
    };
    tick();
    const timer = window.setInterval(tick, intervalMs);
    window.addEventListener(REMINDERS_UPDATED_EVENT, tick);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      window.removeEventListener(REMINDERS_UPDATED_EVENT, tick);
    };
    // 与原实现一致：只注册一次，靠 ref 读最新值，避免每次渲染重起定时器。
  }, []);
}