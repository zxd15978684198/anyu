import { useEffect, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import type { MedicationAction, MedicationAnnouncement } from "../padRest";
import type { MedicationSnapshot } from "../tasks";

/**
 * 用药提醒卡片（PRD §4.5、FR-UI-03/04/08、FR-MED-01/02/06）。
 *
 * 布局要点：文字按钮与语音同时可用（麦克风被拒也能确认）；播报可一键打断；
 * 普通确认 10 秒内可撤销；挂载即触发 card_rendered 回执（此处只留回调，不发起请求）。
 * 拒服不铺红底——红色只留给急症 / 红色事件。
 */
type MedState = "pending" | "taken" | "refused";

interface MedicationCardProps {
  data: MedicationSnapshot;
  onExit: () => void;
  onRendered?: () => void;
  onSubtitle?: (text: string) => void;
  /** 本次触达的事件 ID；缺失时「已服用」只做本地提示、不谎报已上报。 */
  reminderId?: string;
  /** 上报「已服用」；抛错即视为没传上去，卡片停留在 pending。
   *
   * 返回 CON-04 的播报载荷（确认响应里挂着的那一句）；类型允许 `void`/`null`，
   * 老调用方与「只关心是否成功」的实现都不用改签名。
   */
  onConfirm?: (reminderId: string) => Promise<MedicationAnnouncement | null | void>;
  /**
   * 撤回刚刚那次自报（FR-UI-08）。
   *
   * 必须打到服务端：本地 `setState("pending")` 只是把界面改回去，库里那条记录仍是
   * `taken_reported`，而后续 +5/+10 的触达拿到的还是「已处理」，长者看到的是
   * 「撤销了但药提醒不再来」。抛错即视为撤销失败，卡片保持 taken。
   */
  onUndo?: (reminderId: string) => Promise<void>;
  /**
   * 打断播报（FR-UI-04）：真正停掉正在念的语音 + 清空字幕队列。
   *
   * 为什么不在这里自己做：`<audio>` 与逐句字幕计划都归 `RealtimeClient` 管
   * （App 传进来），卡片只负责「按了什么」；自己 setState 只是界面上的假打断。
   * 未传时退化为原行为（只上屏一句「已停止播报」），保证本组件单独渲染也自洽。
   */
  onInterrupt?: () => void;
  /**
   * **W5 预留回调位**：确认 / 拒服 / 稍后提醒这三个收口动作发生时通知上层，
   * 由上层播一句「好的，已记下」「那这顿先不吃，我会告诉护工」这类确认话术。
   *
   * 本卡**不实现 TTS**（那是 W5 的事）：这里只把「该说哪句」这个语义抛出去，
   * 声音、打断、字幕时间轴全在 `RealtimeClient`，卡片不碰播放器。
   * 不传就等于没有语音播报，卡片行为与现在完全一致。
   *
   * 第二个参数是 W5 的**回填位**：上层拿到 confirm/undo 响应里的
   * `MedicationAnnouncement`（`{action, text, subtitle, audio_url, degraded}`，
   * 即 `tts_audio_service.announce_medication(...)` 的返回）后再转进来播放；
   * 卡片自己不请求、不缓存，也没有任何副作用——W4 传参时忽略它即可。
   */
  onFeedback?: (
    action: MedicationAction,
    announcement?: MedicationAnnouncement,
  ) => void;
}

export function MedicationCard({
  data,
  onExit,
  onRendered,
  onSubtitle,
  reminderId,
  onConfirm,
  onUndo,
  onInterrupt,
  onFeedback,
}: MedicationCardProps) {
  const [state, setState] = useState<MedState>("pending");
  const [remaining, setRemaining] = useState(data.windowSeconds);
  const [undoLeft, setUndoLeft] = useState(10);
  const [interrupted, setInterrupted] = useState(false);
  const [busy, setBusy] = useState(false);

  // 页面成功渲染即回执：由调用方保证幂等（FR-MED-02 / FR-ALT-03「只有客户端回执算送达」）
  useEffect(() => {
    onRendered?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 每次触达独立 60 秒确认窗口
  useEffect(() => {
    if (state !== "pending") return;
    const timer = setInterval(
      () => setRemaining((v) => Math.max(0, v - 1)),
      1000,
    );
    return () => clearInterval(timer);
  }, [state]);

  // 普通确认 10 秒可撤销；红色事件不允许长者一键撤销，本卡片不提供该入口
  useEffect(() => {
    if (state !== "taken") return;
    const timer = setInterval(
      () => setUndoLeft((v) => Math.max(0, v - 1)),
      1000,
    );
    return () => clearInterval(timer);
  }, [state]);

  async function handleTaken() {
    if (busy) return;
    setBusy(true);
    try {
      // CON-04：确认响应里带着「已记下」这句播报的音频；拿不到（老响应 / TTS 降级）
      // 就传 undefined，由上层退回字幕——两条路都得让长者知道这声确认已经落地。
      const announcement =
        reminderId && onConfirm ? await onConfirm(reminderId) : null;
      setState("taken");
      setUndoLeft(10);
      onSubtitle?.("好的，已记下您说吃过药了");
      onFeedback?.("taken", announcement ?? undefined);
    } catch {
      // 不切终态：服务端没收到 = 这次触达还没闭环，本地先报「已服用」会让
      // 后续 +5/+10/+15 在端侧被 dismissed 永久吞掉（taskDispatch 按 reminderId 去重）。
      onSubtitle?.("刚才没传上去，您再点一下就好");
    } finally {
      setBusy(false);
    }
  }

  function handleRefused() {
    setState("refused");
    onSubtitle?.("好的，这次先不吃，我会告诉护工");
    onFeedback?.("refused"); // W5：播一句「那这顿先不吃，我会告诉护工」
  }

  /**
   * 「稍后再说」：本次不确认，等服务端 +5/+10/+15 的下一次触达（PRD §4.5）。
   *
   * 与「完成 / 返回主界面」分开：那两个是终态后的收尾，不该再播「稍后再提醒您」。
   */
  function handleSnooze() {
    onFeedback?.("snoozed"); // W5：播一句「好的，过一会儿再提醒您」
    onExit();
  }

  async function handleUndo() {
    if (busy) return;
    // 没有 reminderId / 没有回调时只做界面回退（单独渲染本组件仍然自洽）；
    // 一旦能打到服务端就必须等它真撤成功，否则「撤销」只是把界面骗回 pending。
    if (!reminderId || !onUndo) {
      setState("pending");
      return;
    }
    setBusy(true);
    try {
      await onUndo(reminderId);
      setState("pending");
      setUndoLeft(10);
      onSubtitle?.("好的，这次先不算吃了，等下再问您");
    } catch {
      // 服务端拒收（超过 10 秒 / 终态已固定 / 不是本机提醒）：保持 taken，
      // 界面不能显示「已撤销」而库里仍是「已服用」。
      onSubtitle?.("这会儿撤不回了，我帮您记着");
    } finally {
      setBusy(false);
    }
  }

  return (
    <TaskShell
      eyebrow="用药提醒"
      title="该吃药了"
      badge={
        <StatusBadge tone="warning" size="md">
          第 {data.attemptNo} 次提醒
        </StatusBadge>
      }
      onExit={handleSnooze}
      exitLabel="稍后再说"
      bodyFit
      footer={
        <div className="flex items-stretch gap-4">
          {state === "pending" && (
            <>
              <button
                type="button"
                onClick={handleTaken}
                className="h-24 flex-1 rounded-2xl bg-brand text-4xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                已服用
              </button>
              <button
                type="button"
                onClick={handleRefused}
                className="h-24 rounded-2xl border border-line bg-surface px-10 text-3xl font-semibold text-ink active:bg-surface-sunken"
              >
                这次不吃
              </button>
            </>
          )}
          {state === "taken" && (
            <>
              {undoLeft > 0 ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={handleUndo}
                  className="h-24 flex-1 rounded-2xl border border-line bg-surface text-3xl font-semibold text-ink active:bg-surface-sunken disabled:opacity-50"
                >
                  撤销（{undoLeft} 秒内）
                </button>
              ) : (
                <span className="flex flex-1 items-center text-2xl text-ink-muted">
                  已记录，撤销时间已过
                </span>
              )}
              <button
                type="button"
                onClick={onExit}
                className="h-24 rounded-2xl bg-brand px-12 text-3xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                完成
              </button>
            </>
          )}
          {state === "refused" && (
            <button
              type="button"
              onClick={onExit}
              className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
            >
              返回主界面
            </button>
          )}
        </div>
      }
    >
      {/* 药品快照：历史提醒保留旧值，不随药单修改变化。
          剂量 / 计划时间 / 服用方式从三列改成一行：三列各自带标签会让正文区多出约 60px，
          正是「已服用 / 这次不吃」被挤到滚动区外的原因（2026-11-11 实测）。 */}
      <div className="rounded-2xl border border-line bg-surface-sunken px-6 py-5">
        <p className="text-xl text-ink-muted">本次提醒的药品</p>
        <p className="mt-1 text-4xl font-bold leading-tight text-ink">
          {data.drugName}
        </p>
        <p className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[26px] text-ink">
          <span>
            <span className="text-ink-muted">剂量 </span>
            <span className="font-semibold">{data.dose}</span>
          </span>
          <span aria-hidden="true" className="text-ink-subtle">
            ·
          </span>
          <span>
            <span className="text-ink-muted">计划时间 </span>
            <span className="font-semibold tabular-nums">{data.scheduledAt}</span>
          </span>
          <span aria-hidden="true" className="text-ink-subtle">
            ·
          </span>
          <span>
            <span className="text-ink-muted">服用方式 </span>
            <span className="font-semibold">{data.usage}</span>
          </span>
        </p>
      </div>

      {/* 状态行：信号色只出现在徽标上；打断播报是播报控制，不与确认动作混排 */}
      <div className="mt-4 flex items-center justify-between gap-6">
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge tone="success" size="sm">
            卡片已送达
          </StatusBadge>
          {state === "pending" && (
            <StatusBadge tone={remaining <= 10 ? "warning" : "neutral"} size="sm">
              确认剩余 {remaining} 秒
            </StatusBadge>
          )}
          {state === "taken" && (
            <StatusBadge tone="success" size="sm">
              已记录 · 已服自报
            </StatusBadge>
          )}
          {state === "refused" && (
            <StatusBadge tone="warning" size="sm">
              已记录拒服 · 护工会来确认
            </StatusBadge>
          )}
        </div>
        <button
          type="button"
          disabled={interrupted}
          onClick={() => {
            // 先停声音（真实打断），再置位：反过来的话按钮立刻变灰，
            // 而语音还在念 —— 长者会以为「按了没停」，再去按「已服用」。
            onInterrupt?.();
            setInterrupted(true);
            onSubtitle?.("已停止播报，提醒仍可确认");
          }}
          className="shrink-0 rounded-2xl border border-line bg-surface px-5 py-3 text-lg text-ink-muted active:bg-surface-sunken disabled:opacity-50"
        >
          {interrupted ? "已打断播报" : "打断播报"}
        </button>
      </div>

      <p className="mt-3 text-xl text-ink-muted">
        {data.snapshotAt} · 第 {data.attemptNo} 次提醒 · 也可以直接说「吃了」
        {state === "taken" && undoLeft > 0 && " · 10 秒内可撤销"}
      </p>
    </TaskShell>
  );
}
