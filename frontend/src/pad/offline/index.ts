/**
 * 断网三层兜底的**唯一入口**（PRD:338 FR-MED-08；端侧第①层，服务端第②③层在
 * `app/services/notification_service.py`）。
 *
 * 职责边界：
 * - 本模块只做**端侧**：把当日药单写进 IndexedDB、注册 Service Worker、断网且到点
 *   时用 `speechSynthesis` 本地播报 + 大字弹窗、把「掉线/恢复」上报出去。
 * - 补呼与建单是**服务端**的事（窗口内补一枪 / 超窗建工单）。本模块不自己重试、
 *   不自己改药单、不做任何业务判定，只通过 `reportOffline` / `reportReconnect`
 *   把定位键（`reminder_id` + 窗口边界）交给接线层。
 * - 断网期间**不发任何网络请求**（Service Worker 也不缓存 `/api`）：断网时的
 *   静默重试只会制造「以为补了其实没补」的假象，PRD:326 还禁止集中补播。
 *
 * 接线方式（pad 根组件，一行）：
 * ```ts
 * const offline = initOfflineCache();
 * // 有药单数据源时再补：initOfflineCache({ loadReminders, reportOffline, reportReconnect })
 * return () => offline.dispose();
 * ```
 */
import { REMINDERS_UPDATED_EVENT } from "../medicationView";
import { markAnnounced, readReminders, writeReminders } from "./db";
import { dismissOverlay, overlayText, showMedicationOverlay } from "./overlay";
import { speakLocally } from "./tts";
import type {
  OfflineCacheHandle,
  OfflineCacheOptions,
  OfflineEvent,
  OfflineReminder,
  OfflineReport,
} from "./types";

/** Service Worker 固定放在 `frontend/public/sw.js`（Vite 原样拷到站点根）。 */
const SW_URL = "/sw.js";

/** 断网轮询间隔：5s 足够覆盖 60s 的确认窗口，又不至于把平板电池耗光。 */
const DEFAULT_TICK_MS = 5000;

/** 四次触达 T0/+5/+10/+15（PRD §4.5，写死在服务端 `RETRY_SPACING_MINUTES`）。 */
const MAX_ATTEMPTS = 4;
const SPACING_MINUTES = 5;

/** 窗口关闭时刻（毫秒）：与 `app/scheduler/medication.decide_event_action` 的
 *  `timeline_end` 同一算法 —— `T0 + 5min*(4-1) + windowSeconds`。 */
function windowEndOf(reminder: OfflineReminder): number {
  const start = Date.parse(reminder.scheduledAt);
  if (Number.isNaN(start)) return Number.NaN;
  return start + (MAX_ATTEMPTS - 1) * SPACING_MINUTES * 60_000 + reminder.windowSeconds * 1000;
}

/**
 * 初始化断网兜底。**唯一**导出入口，可重复调用（内部单例，第二次调用返回同一个句柄）。
 */
export function initOfflineCache(options: OfflineCacheOptions = {}): OfflineCacheHandle {
  const mountRoot = options.mountRoot;
  const tickMs = options.tickMs ?? DEFAULT_TICK_MS;
  const withOverlay = options.overlay !== false;

  let online = typeof navigator === "undefined" ? true : navigator.onLine;
  let reminders: OfflineReminder[] = [];
  let disposed = false;
  /** 已经在本端播过的提醒（内存镜像；权威副本是 IndexedDB 的 `announcedAt`）。 */
  const announced = new Set<string>();
  /** 掉线与窗口重叠的提醒（第③层：立刻通知护工，同一会话只报一次）。 */
  const overlapped = new Set<string>();
  /** 断网期间受影响的提醒（第②层：恢复后统一上报，由服务端按窗口规则处置）。 */
  const pendingReconnect = new Set<string>();
  let closeOverlay: (() => void) | null = null;
  let timer: number | undefined;

  function eventsOf(ids: Iterable<string>): OfflineEvent[] {
    const wanted = new Set(ids);
    const events: OfflineEvent[] = [];
    for (const reminder of reminders) {
      if (!wanted.has(reminder.reminderId)) continue;
      const end = windowEndOf(reminder);
      events.push({
        reminderId: reminder.reminderId,
        scheduledAt: reminder.scheduledAt,
        windowEnd: new Date(end).toISOString(),
        attemptsSent: reminder.attemptsSent,
        announced: announced.has(reminder.reminderId) || reminder.announcedAt !== undefined,
      });
    }
    return events;
  }

  async function send(kind: OfflineReport["kind"], events: OfflineEvent[]): Promise<void> {
    if (events.length === 0) return;
    const report: OfflineReport = { kind, at: new Date().toISOString(), events };
    const handler = kind === "offline" ? options.reportOffline : options.reportReconnect;
    if (handler === undefined) return;
    try {
      await handler(report);
    } catch (error) {
      // 上报失败不影响本地兜底：服务端还有自己的定时扫描（PRD:326 不依赖端侧上报）
      console.warn("[offline] 上报失败（不影响本地播报）", error);
    }
  }

  async function announce(reminder: OfflineReminder): Promise<void> {
    const at = new Date().toISOString();
    announced.add(reminder.reminderId);
    const spoken = speakLocally(overlayText(reminder));
    if (!spoken) console.warn("[offline] 本机没有可用语音，只用大字弹窗兜底");
    if (withOverlay) {
      closeOverlay?.();
      closeOverlay = showMedicationOverlay(reminder, { mountRoot });
    }
    await markAnnounced(reminder.reminderId, at);
  }

  async function loadFromDisk(): Promise<void> {
    reminders = await readReminders();
    // 以磁盘为准重建「已播过」集合：否则刷新药单后，旧 reminder_id 的播报标记会
    // 让新窗口被误判成「已播过」，断网时就静默不播了（这是最容易漏的一处兜底失效）。
    announced.clear();
    for (const reminder of reminders) {
      if (reminder.announcedAt !== undefined) announced.add(reminder.reminderId);
    }
  }

  async function check(): Promise<void> {
    if (disposed) return;
    const now = Date.now();
    const freshOverlap = new Set<string>();
    for (const reminder of reminders) {
      const start = Date.parse(reminder.scheduledAt);
      const end = windowEndOf(reminder);
      if (Number.isNaN(start) || Number.isNaN(end)) continue;
      if (online || now < start || now >= end) continue;

      // 第③层：掉线与窗口重叠 → 立刻通知护工（每次掉线会话每条约一次）
      if (!overlapped.has(reminder.reminderId)) {
        overlapped.add(reminder.reminderId);
        freshOverlap.add(reminder.reminderId);
      }
      // 第②层：恢复后要按窗口规则补呼/建单，先记下来
      pendingReconnect.add(reminder.reminderId);
      // 第①层：本地播报 + 大字弹窗，同一窗口只播一次（重载也不重播）
      if (!announced.has(reminder.reminderId) && reminder.announcedAt === undefined) {
        await announce(reminder);
      }
    }
    if (freshOverlap.size > 0) await send("offline", eventsOf(freshOverlap));
  }

  async function refresh(): Promise<number> {
    if (!online) return reminders.length;
    if (options.loadReminders === undefined) return reminders.length;
    try {
      const rows = await options.loadReminders();
      await cacheReminders(rows);
      return rows.length;
    } catch (error) {
      console.warn("[offline] 药单刷新失败，继续用缓存里的那份", error);
      return reminders.length;
    }
  }

  async function cacheReminders(rows: OfflineReminder[]): Promise<void> {
    await writeReminders(rows);
    await loadFromDisk();
    // 写完就通知面板重读。面板只读 IndexedDB，它无法感知「刚刚有人刷新过药单」；
    // 不通知的话，登录后当场看屏要等下一次 60s 轮询才出现，看着像「今天没有药」。
    window.dispatchEvent(new Event(REMINDERS_UPDATED_EVENT));
  }

  function handleOffline(): void {
    online = false;
    console.info("[offline] 网络已断开，进入本地兜底");
    void check();
  }

  function handleOnline(): void {
    online = true;
    console.info("[offline] 网络已恢复，准备按窗口规则上报");
    closeOverlay?.();
    closeOverlay = null;
    dismissOverlay();
    const events = eventsOf(pendingReconnect);
    pendingReconnect.clear();
    overlapped.clear();
    announced.clear();
    void (async () => {
      await send("online", events);
      await refresh();
      await check();
    })();
  }

  function handleVisibility(): void {
    // 平板熄屏/切后台时定时器会被节流，回到前台立刻补一次检查
    if (document.visibilityState === "visible") void check();
  }

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    if (timer !== undefined) window.clearInterval(timer);
    timer = undefined;
    window.removeEventListener("offline", handleOffline);
    window.removeEventListener("online", handleOnline);
    document.removeEventListener("visibilitychange", handleVisibility);
    closeOverlay?.();
    closeOverlay = null;
    dismissOverlay();
  }

  window.addEventListener("offline", handleOffline);
  window.addEventListener("online", handleOnline);
  document.addEventListener("visibilitychange", handleVisibility);
  timer = window.setInterval(() => {
    void check();
  }, tickMs);

  void (async () => {
    await loadFromDisk();
    await registerServiceWorker();
    await refresh();
    await check();
  })();

  return {
    refresh,
    cache: cacheReminders,
    check,
    dispose,
  };
}

/** 注册 Service Worker：失败只告警，绝不阻塞页面（无 SW 时本地播报仍然可用）。 */
async function registerServiceWorker(): Promise<void> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
  if (!window.isSecureContext) {
    console.info("[offline] 非安全上下文（需 https 或 localhost），跳过 Service Worker");
    return;
  }
  try {
    const registration = await navigator.serviceWorker.register(SW_URL, { scope: "/" });
    console.info("[offline] Service Worker 注册成功", registration.scope);
  } catch (error) {
    console.warn("[offline] Service Worker 注册失败（本地兜底仍可用）", error);
  }
}
