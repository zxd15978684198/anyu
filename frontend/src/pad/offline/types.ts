/**
 * 断网兜底的数据口径（PRD:338 FR-MED-08 第①层：长者端 IndexedDB 缓存当日药单）。
 *
 * 字段与 `frontend/src/pad/tasks.ts` 的 `MedicationSnapshot` 同源，只有一处不同：
 * `scheduledAt` 从展示用的「08:00」换成**带时区的 ISO 时刻**。断网时没有服务端可问
 * 「现在算不算到点」，平板只能拿自己的钟跟一个绝对时刻比 —— 字符串「08:00」既没有
 * 日期也没有时区，跨日与跨时区都会算错，所以缓存里一律存 ISO（UTC 存储，AGENTS §8）。
 */

/** 一条被缓存到本地的用药提醒（当日药单的一行）。 */
export interface OfflineReminder {
  /** `medication_events.reminder_id`：服务端补呼/建单的幂等定位键 */
  reminderId: string;
  /** 来源计划；补呼时服务端按 (event_id, attempt_no) 去重 */
  planId: string;
  /** 药品名（来自 `drug_snapshot`，不随药单改版变化） */
  drugName: string;
  /** 单次剂量，如「5mg」「1 片」 */
  dose: string;
  /** 服用方式，如「口服，温水送服」 */
  usage: string;
  /** 计划到点时刻（ISO8601，带时区） */
  scheduledAt: string;
  /** 本次触达的确认窗口秒数（PRD §4.5：每次触达等 60s） */
  windowSeconds: number;
  /** 已触达次数（服务端告知；离线期间+1 由恢复后补呼决定，本端不自增） */
  attemptsSent: number;
  /** 快照生成时刻（ISO8601），用于列表展示与「旧快照」提示 */
  snapshotAt: string;
  /**
   * 本地播报过的时刻（ISO8601）。非空 = 这台平板已经在该窗口里播过一次，
   * 重载页面不再重播（PRD:326「重连不集中补播」的端侧对应实现）。
   */
  announcedAt?: string;
}

/** 上报给服务端的一条事件摘要（不含任何诊断/健康推断，只有定位键与时刻）。 */
export interface OfflineEvent {
  reminderId: string;
  /** 到点时刻（ISO，带时区） */
  scheduledAt: string;
  /** 窗口关闭时刻（ISO）：`scheduledAt + (attempts-1)*5min + windowSeconds` */
  windowEnd: string;
  /** 已触达次数，供服务端复核（服务端以库内为准，这里只是提示） */
  attemptsSent: number;
  /** 是否已由平板本地播报过 */
  announced: boolean;
}

/** 一次联网状态变化的上报载荷（第②③层由 pad 根组件接后端接口）。 */
export interface OfflineReport {
  /** offline = 掉线（第③层：与用药窗口重叠要立即通知护工）；online = 恢复（第②层） */
  kind: "offline" | "online";
  /** 事件发生时刻（ISO） */
  at: string;
  /** 与本次状态变化相关的提醒 */
  events: OfflineEvent[];
}

/** `initOfflineCache()` 的接线参数。**全部可选**：不传真机也照常兜底。 */
export interface OfflineCacheOptions {
  /**
   * 联网时拉「当日药单」并写入 IndexedDB。断网时不会调用。
   * 由 pad 根组件接到后端（长者端设备凭证目前没有药单读接口，
   * `GET /api/medication/events` 只对员工/家属开放），所以这里不内置请求地址。
   */
  loadReminders?: () => Promise<OfflineReminder[]>;
  /** 掉线上报（第③层：掉线与用药窗口重叠 → 立即通知护工） */
  reportOffline?: (report: OfflineReport) => void | Promise<void>;
  /** 恢复上报（第②层：服务端按窗口规则补呼或建单） */
  reportReconnect?: (report: OfflineReport) => void | Promise<void>;
  /** 大字弹窗挂载点，默认 `document.body` */
  mountRoot?: HTMLElement;
  /** 轮询间隔（毫秒），默认 5000；演示/联调可调小 */
  tickMs?: number;
  /** 是否显示大字弹窗，默认 true（自动化点测时可关掉只看播报） */
  overlay?: boolean;
}

/** `initOfflineCache()` 返回的句柄：只有一个入口，收尾与手动刷新都走它。 */
export interface OfflineCacheHandle {
  /** 立刻按 `loadReminders` 刷新一次缓存（联网时才真正拉数据） */
  refresh: () => Promise<number>;
  /** 直接把一批提醒写进缓存（根组件手里已有数据时用，避免再发一次请求） */
  cache: (reminders: OfflineReminder[]) => Promise<void>;
  /** 立刻跑一次「到点检查」（演示/点测用，平时由内部定时器驱动） */
  check: () => Promise<void>;
  /** 拆掉定时器与事件监听，并收起弹窗（不注销 Service Worker） */
  dispose: () => void;
}
