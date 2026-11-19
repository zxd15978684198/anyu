import { useEffect, useRef, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";

/**
 * 身份主动确认层（C5 `identity_ask` 的前端消费方；PRD §3.2、FR-VPR-02/03）。
 *
 * 声纹低置信度或声纹服务不可用时服务端下发 `identity_ask`：候选集恒 1 位
 * （设备绑定长者 1v1），页面只出单个「是我」大按钮（C5 开放点裁决 1）。
 * 点选 → `onConfirm(askId, elderId)` → 上层经 `RealtimeClient.sendIdentityConfirm`
 * 回带 `identity_confirm`。30s 计时起点在服务端（C5 裁决 2），超时由服务端下发
 * `identity_result{source:"generic"}`；通用模式横幅（不读记忆/不写健康/不确认用药）
 * 由上层根据 `identity_result` 承载，本层不做本地计时兜底、不猜身份。
 *
 * 四条防「卡死」的护栏（task-25）：
 * 1. `onExit`：「稍后再说」= 本地收起、**不下发** confirm —— 断线后服务端已取消计时器
 *    且可能不再发结果，长者必须有路退出本层；
 * 2. `onConfirm` 回 boolean：WS 未 OPEN 时 send 被丢弃，不能置「已提交」，
 *    否则永久停在「已确认，正在进入…」；
 * 3. askId 变化即复位 submitted（外层已加 key={askId} 双保险）；
 * 4. **本地看门狗**（P1-3）：`send()` 返回 true 只代表 WS OPEN，不代表服务端收下
 *    （stale ask / ask_id 不匹配会被静默忽略、不回 error、不回结果）。故提交成功后
 *    起一个与服务端 ask 超时同窗口（`timeoutS`，缺省 30s）的本地定时器，到点复位
 *    `submitted` 并提示再按一次；收到结果或本层卸载/换 ask 时清掉。
 */
interface IdentityConfirmProps {
  /** `identity_ask.candidates[0].display_name` */
  displayName: string;
  /** `identity_ask.ask_id`：confirm 必须原样回带 */
  askId: string;
  /** `identity_ask.candidates[0].elder_id`：confirm 必须原样回带 */
  elderId: string;
  /** `identity_ask.timeout_s`（服务端计时，UI 只作提示；缺省按契约 30） */
  timeoutS?: number;
  /** 点「是我」→ 上层发送 `identity_confirm{ask_id, elder_id}`；返回是否真的发出 */
  onConfirm: (askId: string, elderId: string) => boolean;
  /**
   * WS 发不出去时的 REST 出口（`POST /api/identity/confirm`）。
   *
   * 没有它的时候，按住说话降级链路里点「是我」只会得到「网络还没连好」——
   * 而那条链路**本来就没有 WS**，等于把长者锁在一个永远点不动的按钮前。
   * 返回 `{ok, message?}`：`message` 是已经翻成人话的失败原因，本层原样显示。
   */
  onRestConfirm?: (askId: string, elderId: string) => Promise<{ ok: boolean; message?: string }>;
  /** 点「稍后再说」→ 本地收起确认层，不下发任何消息 */
  onExit?: () => void;
}

export function IdentityConfirm({
  displayName,
  askId,
  elderId,
  timeoutS = 30,
  onConfirm,
  onRestConfirm,
  onExit,
}: IdentityConfirmProps) {
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");
  // REST 出口在途：按钮转「正在确认…」，但不锁死（失败要能再点）
  const [restPending, setRestPending] = useState(false);
  // 同一个实例被复用于下一个 ask 时（外层漏加 key 的兜底）按 askId 复位
  const [currentAskId, setCurrentAskId] = useState(askId);
  if (currentAskId !== askId) {
    setCurrentAskId(askId);
    setSubmitted(false);
    setRestPending(false);
    setError("");
  }

  // P1-3 看门狗：提交成功后等服务端结果，超时不改变身份、只把按钮还给长者
  const watchdogRef = useRef<number | null>(null);

  function clearWatchdog() {
    if (watchdogRef.current !== null) {
      window.clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  }

  // 换 ask（同一实例复用）与卸载都必须清定时器，否则会对已作废的确认层复位状态
  useEffect(() => {
    clearWatchdog();
    return clearWatchdog;
  }, [askId]);

  function armWatchdog() {
    clearWatchdog();
    // 与服务端 ask 超时同一窗口：服务端超时后置 generic 并发 identity_result，
    // 前端在此之前不能把按钮锁死（stale ask 时服务端根本不会回结果）。
    watchdogRef.current = window.setTimeout(
      () => {
        watchdogRef.current = null;
        setSubmitted(false);
        setError("网络有点慢，没等到系统回话，请再按一次「是我」。");
      },
      Math.max(1, timeoutS) * 1000,
    );
  }

  /**
   * 「是我」的两级出口：先走 WS（正常链路），发不出去再走 REST（降级链路）。
   *
   * 顺序不能反：WS 那条路上服务端手上有 `ask_id`，能对「这一张卡」做超时与结果
   * 回推；REST 只是把「这台设备确认过」落库，它保证不了这一张卡的结果帧。
   * 所以首选 WS，只有在 WS 明确发不出去（没连上/已关闭）时才降级。
   */
  async function runRestConfirm(): Promise<void> {
    if (!onRestConfirm) {
      // 没给 REST 出口：保持原行为（提示重试），不要假装确认上了
      clearWatchdog();
      setSubmitted(false);
      setError("网络还没连好，请稍等一下再点一次。");
      return;
    }
    setRestPending(true);
    setError("");
    try {
      const outcome = await onRestConfirm(askId, elderId);
      if (outcome.ok) {
        setSubmitted(true);
        armWatchdog();
        return;
      }
      setSubmitted(false);
      setError(outcome.message ?? "这次没确认上，请再点一次「是我」。");
    } finally {
      setRestPending(false);
    }
  }

  function handleConfirm() {
    if (submitted || restPending) return; // 防二次点选：重复 confirm 服务端会忽略，但按钮不该再触发
    const sent = onConfirm(askId, elderId);
    if (sent) {
      setSubmitted(true);
      setError("");
      armWatchdog();
      return;
    }
    // WS 未 OPEN：本条已丢弃。有 REST 出口就走它——降级链路本来就没有 WS，
    // 只提示「网络还没连好」等于让长者对着一个永远点不动的按钮反复按。
    void runRestConfirm();
  }

  return (
    <TaskShell
      eyebrow="身份确认"
      title="请问是哪位在说话？"
      badge={
        <StatusBadge tone="info" size="md">
          需要您确认一下
        </StatusBadge>
      }
      footer={
        <div className="flex flex-col gap-4">
          {/* 主操作钉在页脚（滚动区之外）：矮屏上「是我 / 稍后再说」永远可见，
              不用滑屏去找按钮（2026-11-11 浮层内容显示不全）。 */}
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitted || restPending}
            className="min-h-20 w-full rounded-2xl bg-brand text-4xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-60"
          >
            {submitted ? "已确认，正在进入…" : restPending ? "正在确认…" : "是我"}
          </button>
          {error !== "" && (
            <p role="alert" className="text-center text-2xl text-warning-ink">
              {error}
            </p>
          )}
          {onExit && (
            <button
              type="button"
              onClick={onExit}
              className="min-h-14 w-full rounded-2xl border border-line bg-surface px-8 text-2xl text-ink-muted active:bg-surface-sunken"
            >
              稍后再说
            </button>
          )}
          <p className="text-xl text-ink-muted">
            听不太清声音时会显示本页，系统不会替您猜身份。
          </p>
        </div>
      }
    >
      <p className="text-2xl leading-relaxed text-ink-muted">
        请点下面的大按钮确认；如果不是您，{timeoutS} 秒后会自动按通用模式回答。
      </p>
      <div className="mt-6 flex items-center gap-8 rounded-2xl border border-line bg-surface-sunken p-6">
        <span className="flex h-24 w-24 shrink-0 items-center justify-center rounded-full border border-brand-line bg-brand-soft text-5xl font-bold text-brand">
          {displayName.slice(0, 1)}
        </span>
        <div className="flex flex-col gap-3">
          <span className="text-4xl font-bold text-ink">{displayName}</span>
          <span className="text-xl text-ink-muted">本房间绑定的长者</span>
        </div>
      </div>
    </TaskShell>
  );
}
