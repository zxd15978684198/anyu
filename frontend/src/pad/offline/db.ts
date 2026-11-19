/**
 * 当日药单的 IndexedDB 缓存（PRD:338 FR-MED-08 第①层）。
 *
 * 三个约束：
 *
 * 1. **零依赖**：不引 idb/dexie —— 长者端为了一个「缓存一行 JSON」的库再装一个
 *    包不划算（计划件 §5 也不许新增前端依赖）。原生 IndexedDB 的 promise 包装
 *    只有几十行，坏处是一旦写错很难查，所以这里所有请求都走同一个 `request<T>()`。
 * 2. **隐私模式兜底**：Safari 隐私模式 / 存储配额被拒时 `open()` 会直接抛；这时
 *    退回内存镜像，本次会话仍能播报，只是重载后丢失。**宁可丢缓存，不能白屏**。
 * 3. **只存当日**：写入即整表替换，不按天累积 —— 长者端不需要历史，留着反而会
 *    在恢复后让人误以为有「昨天没吃的药」。
 */
import type { OfflineReminder } from "./types";

const DB_NAME = "anyu-offline";
const DB_VERSION = 1;
const STORE = "reminders";

/** IndexedDB 不可用时的内存镜像（`null` = 还没退化，正常走 IDB）。 */
let memory: OfflineReminder[] | null = null;

function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("indexeddb request failed"));
  });
}

function openDb(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("indexedDB is not available"));
  }
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(DB_NAME, DB_VERSION);
    open.onupgradeneeded = () => {
      const db = open.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const store = db.createObjectStore(STORE, { keyPath: "reminderId" });
        store.createIndex("scheduledAt", "scheduledAt");
      }
    };
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error ?? new Error("indexeddb open failed"));
    open.onblocked = () => reject(new Error("indexeddb open blocked by another tab"));
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => Promise<T>,
): Promise<T> {
  const db = await openDb();
  try {
    const tx = db.transaction(STORE, mode);
    const store = tx.objectStore(STORE);
    const result = await run(store);
    // 等事务真正提交：否则页面立刻刷新会丢刚写进去的一行
    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new Error("indexeddb tx failed"));
      tx.onabort = () => reject(tx.error ?? new Error("indexeddb tx aborted"));
    });
    return result;
  } finally {
    db.close();
  }
}

/** 读回当日药单（按到点时刻升序）。 */
export async function readReminders(): Promise<OfflineReminder[]> {
  if (memory !== null) return sortByScheduledAt(memory);
  try {
    const rows = await withStore("readonly", async (store) => {
      const req = store.getAll();
      return (await request(req)) as OfflineReminder[];
    });
    return sortByScheduledAt(rows);
  } catch (error) {
    console.warn("[offline] IndexedDB 读失败，退回内存镜像", error);
    memory = memory ?? [];
    return sortByScheduledAt(memory);
  }
}

/** 整表替换：当日药单变了就以新的为准（不做增量合并，避免留下旧剂量）。 */
export async function writeReminders(reminders: OfflineReminder[]): Promise<void> {
  const rows = sortByScheduledAt(reminders);
  if (memory !== null) {
    memory = rows;
    return;
  }
  try {
    await withStore("readwrite", async (store) => {
      await request(store.clear());
      for (const row of rows) await request(store.put(row));
      return true;
    });
  } catch (error) {
    console.warn("[offline] IndexedDB 写失败，退回内存镜像", error);
    memory = rows;
  }
}

/** 记下「这条已经在本地播过」：重载后不再重播（PRD:326 不集中补播）。 */
export async function markAnnounced(reminderId: string, at: string): Promise<void> {
  const row = (await readReminders()).find((item) => item.reminderId === reminderId);
  if (row === undefined || row.announcedAt !== undefined) return;
  const next: OfflineReminder = { ...row, announcedAt: at };
  if (memory !== null) {
    memory = memory.map((item) => (item.reminderId === reminderId ? next : item));
    return;
  }
  try {
    await withStore("readwrite", async (store) => {
      await request(store.put(next));
      return true;
    });
  } catch (error) {
    console.warn("[offline] 播报标记没写进 IndexedDB（不影响本次播报）", error);
  }
}

function sortByScheduledAt(rows: OfflineReminder[]): OfflineReminder[] {
  return [...rows].sort((a, b) => Date.parse(a.scheduledAt) - Date.parse(b.scheduledAt));
}
