/**
 * 照护台 6 个 API-only 端点的前端契约（2026-11-16）。
 *
 * 钉的是请求本身：方法、路径、请求体与错误映射。页面是否显隐由能力矩阵覆盖；
 * 这里同时钉死「医生能撤销、护工不能」「房间/媒体只给管理员」。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { ackRiskEvent } from "../src/console/workOrders";
import { revokeHealthRecord } from "../src/console/health";
import { listRooms, updateRoom } from "../src/console/rooms";
import {
  deleteLibraryItem,
  listLibraryItems,
  updateLibraryItem,
} from "../src/console/mediaLibrary";
import { HttpError } from "../src/console/http";
import { can, capabilitiesOf } from "../src/console/permissions";
import type { Principal } from "../src/console/api";

const here = dirname(fileURLToPath(import.meta.url));

function installStorage(): void {
  const store = new Map<string, string>();
  store.set("kb_console_access_token", "test-access-token");
  const stub = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", { value: stub, configurable: true });
}

interface Captured {
  url: string;
  init: RequestInit;
}

function installFetch(status: number, body?: unknown): Captured[] {
  const calls: Captured[] = [];
  const stub = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    if (body === undefined) return new Response(null, { status });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  };
  Object.defineProperty(globalThis, "fetch", { value: stub, configurable: true });
  return calls;
}

function staff(role: string): Principal {
  return { kind: "staff", id: "staff-1", role, name: role };
}

test("风险确认：POST /api/risk-events/{id}/ack，无请求体，保留原确认时间", async () => {
  installStorage();
  const calls = installFetch(200, {
    event_id: "evt-1",
    elder_id: "elder-1",
    type: "sos",
    severity: "red",
    status: "open",
    acknowledged_at: "2026-11-16T04:00:00Z",
  });
  const ack = await ackRiskEvent("evt/1");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/risk-events/evt%2F1/ack");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, undefined);
  assert.equal(ack.acknowledged_at, "2026-11-16T04:00:00Z");
  // status 仍是 open 是预期：确认不改变事件状态。
  assert.equal(ack.status, "open");
});

test("风险确认失败保留 403 语义，不伪造成功", async () => {
  installStorage();
  installFetch(403, { detail: "role", code: "ROLE_FORBIDDEN" });
  await assert.rejects(
    () => ackRiskEvent("evt-1"),
    (err: unknown) => {
      assert.ok(err instanceof HttpError);
      assert.equal(err.status, 403);
      assert.equal(err.code, "ROLE_FORBIDDEN");
      return true;
    },
  );
});

test("健康撤销：POST /api/health/records/{id}/revoke", async () => {
  installStorage();
  const calls = installFetch(200, {
    record_id: "rec-1",
    elder_id: "elder-1",
    record_date: "2026-11-16",
    source: "staff_active",
    grade: "green",
    revoked_at: "2026-11-16T04:00:00Z",
    revoked_by: "staff-1",
  });
  const record = await revokeHealthRecord("rec/1");
  assert.equal(calls[0].url, "/api/health/records/rec%2F1/revoke");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(record.revoked_at, "2026-11-16T04:00:00Z");
  assert.equal(record.revoked_by, "staff-1");
});

test("房间更新：PATCH /api/rooms/{id}，只发送变化的字段", async () => {
  installStorage();
  const calls = installFetch(200, {
    room_id: "room-1",
    room_no: "A-302",
    status: "maintenance",
    bed_count: 2,
  });
  const room = await updateRoom("room/1", { status: "maintenance" });
  assert.equal(calls[0].url, "/api/rooms/room%2F1");
  assert.equal(calls[0].init.method, "PATCH");
  assert.equal(calls[0].init.body, JSON.stringify({ status: "maintenance" }));
  assert.equal(room.status, "maintenance");
  assert.equal(room.bed_count, 2);
});

test("房间列表仍走 GET /api/rooms，解析床位数", async () => {
  installStorage();
  const calls = installFetch(200, {
    items: [{ room_id: "room-1", room_no: "A-301", status: "available", bed_count: 2 }],
    total: 1,
  });
  const rooms = await listRooms();
  assert.equal(calls[0].url, "/api/rooms");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(rooms[0].bed_count, 2);
});

test("媒体列表：GET /api/media/library/items，带过滤与分页", async () => {
  installStorage();
  const calls = installFetch(200, {
    items: [
      {
        media_id: "media-1",
        kind: "opera",
        title: "越剧选段",
        genre: "越剧",
        media_url: "/media/a.mp3",
        url: "/media/a.mp3",
        duration_seconds: 180,
        copyright_status: "public_domain",
        playable: true,
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  });
  const data = await listLibraryItems({ kind: "opera", limit: 20, offset: 0 });
  assert.equal(calls[0].url, "/api/media/library/items?kind=opera&limit=20&offset=0");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(data.items[0].duration_seconds, 180);
  assert.equal(data.items[0].playable, true);
});

test("媒体更新：PATCH /api/media/library/{id}，duration 与响应 duration_seconds 解耦", async () => {
  installStorage();
  const calls = installFetch(200, {
    media_id: "media-1",
    kind: "movie",
    title: "新标题",
    genre: null,
    era: null,
    artist: null,
    media_url: "/media/b.mp4",
    url: "/media/b.mp4",
    duration_seconds: 90,
    copyright_status: "licensed_sample",
    playable: true,
  });
  const item = await updateLibraryItem("media/1", {
    title: "新标题",
    genre: null,
    duration: 90,
  });
  assert.equal(calls[0].url, "/api/media/library/media%2F1");
  assert.equal(calls[0].init.method, "PATCH");
  assert.equal(
    calls[0].init.body,
    JSON.stringify({ title: "新标题", genre: null, duration: 90 }),
  );
  assert.equal(item.duration_seconds, 90);
});

test("媒体删除：DELETE /api/media/library/{id}，204 视为成功", async () => {
  installStorage();
  const calls = installFetch(204);
  await deleteLibraryItem("media-1");
  assert.equal(calls[0].url, "/api/media/library/media-1");
  assert.equal(calls[0].init.method, "DELETE");
});

test("媒体删除遇播放历史：409 映射为 HttpError，不静默吞掉", async () => {
  installStorage();
  installFetch(409, { detail: "has history", code: "CONFLICT" });
  await assert.rejects(
    () => deleteLibraryItem("media-1"),
    (err: unknown) => {
      assert.ok(err instanceof HttpError);
      assert.equal(err.status, 409);
      assert.equal(err.code, "CONFLICT");
      return true;
    },
  );
});

test("角色矩阵：医生可撤销、护工不可；房间/媒体只给管理员", () => {
  const admin = staff("admin");
  const doctor = staff("doctor");
  const care = staff("care_worker");

  assert.equal(can(admin, "health:revoke"), true);
  assert.equal(can(doctor, "health:revoke"), true);
  assert.equal(can(care, "health:revoke"), false);

  assert.equal(can(admin, "room:write"), true);
  assert.equal(can(doctor, "room:write"), false);
  assert.equal(can(care, "room:write"), false);

  assert.equal(can(admin, "media:write"), true);
  assert.equal(can(doctor, "media:write"), false);
  assert.equal(can(care, "media:write"), false);

  assert.deepEqual(capabilitiesOf(null), []);
});

test("导航与页面表同时登记两个新路径", () => {
  const read = (path: string) => readFileSync(resolve(here, "../src/console", path), "utf8");
  const router = read("router.tsx");
  const app = read("App.tsx");
  for (const path of ["/rooms", "/media-library"]) {
    assert.match(router, new RegExp(`path:\\s*"${path}"`), `router 缺少 ${path}`);
    assert.match(app, new RegExp(`path:\\s*"${path}"`), `App 缺少 ${path}`);
  }
});
