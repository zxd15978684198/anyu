/**
 * W0 family 测试底：真实渲染 + 真实 hooks 请求生命周期 + family 认证实例。
 *
 * 页面用 react-dom/server 执行组件；hooks 用当前 React 19 的 dispatcher 驱动
 * useState/useEffect/useCallback（不引入 DOM、测试框架或依赖）。因此这不是端到端
 * 测试：SSR 不运行 effect，也不验证真实点击、失焦、音频播放或浏览器导航。
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { createElement, useEffect, useState, type ComponentProps, type ReactNode } from "react";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import FamilyApp from "../src/family/App.tsx";
import {
  authedFetch,
  clearSession,
  fetchCurrent,
  hasSession,
  login,
  logout,
  setUnauthorizedHandler,
} from "../src/family/api.ts";
import { useElderNames, useReports, type ReportsState } from "../src/family/hooks.ts";
import { HttpError } from "../src/family/http.ts";
import { listNotifications, type NotificationItem } from "../src/family/notifications.ts";
import { LoginPage } from "../src/family/pages/LoginPage.tsx";
import { NotificationsPage } from "../src/family/pages/NotificationsPage.tsx";
import { ReportDetailPage } from "../src/family/pages/ReportDetailPage.tsx";
import { ReportListPage } from "../src/family/pages/ReportListPage.tsx";
import { getReport, listReports, type DailyReportItem } from "../src/family/reports.ts";

// ---------------------------------------------------------------------------
// 可控浏览器边界：不启动浏览器，只替换 storage/fetch。
// ---------------------------------------------------------------------------

class MemoryStorage {
  private readonly map = new Map<string, string>();

  getItem(key: string): string | null {
    return this.map.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.map.set(key, String(value));
  }

  removeItem(key: string): void {
    this.map.delete(key);
  }

  clear(): void {
    this.map.clear();
  }
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

type FetchHandler = (url: string, init: RequestInit, index: number) => Response | Promise<Response>;

const storage = new MemoryStorage();
const originalFetch = globalThis.fetch;
const originalStorage = (globalThis as { localStorage?: unknown }).localStorage;
let fetchHandler: FetchHandler = () => {
  throw new Error("未配置 fetch 替身");
};
let fetchCalls: FetchCall[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function useFetch(handler: FetchHandler): void {
  fetchCalls = [];
  fetchHandler = handler;
  globalThis.fetch = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const index = fetchCalls.length;
    fetchCalls.push({ url, init });
    return fetchHandler(url, init, index);
  }) as typeof fetch;
}

beforeEach(() => {
  // 必须先装 storage 替身再 clearSession：Node 无 localStorage，
  // 顺序反了会在 shared/auth.ts 的 removeItem 上抛 TypeError，20 条用例全挂。
  (globalThis as { localStorage: unknown }).localStorage = storage;
  storage.clear();
  clearSession();
  setUnauthorizedHandler(null);
  useFetch(() => json({ items: [], total: 0 }));
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  setUnauthorizedHandler(null);
  clearSession();
  if (originalStorage === undefined) {
    delete (globalThis as { localStorage?: unknown }).localStorage;
  } else {
    (globalThis as { localStorage: unknown }).localStorage = originalStorage;
  }
});

// ---------------------------------------------------------------------------
// React 19 轻量 hooks 驱动器：只实现 family 实际使用的 hooks。
// effect 按真实依赖比较执行，重渲染不再重复请求；清理函数在卸载/依赖变化时运行。
// React 19.1.0 的开发构建把当前 dispatcher 放在 internals.H。
// ---------------------------------------------------------------------------

interface Dispatcher {
  useState: typeof React.useState;
  useEffect: typeof React.useEffect;
  useCallback: typeof React.useCallback;
  useRef: typeof React.useRef;
}

interface ReactWithInternals {
  __CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE: {
    H: Dispatcher | null;
  };
}

const internals = (React as unknown as ReactWithInternals)
  .__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;

interface EffectSlot {
  deps: readonly unknown[];
  cleanup: void | (() => void);
}

class HookHarness<T> {
  private readonly states: unknown[] = [];
  private readonly memos: unknown[] = [];
  private readonly refs: Array<{ current: unknown }> = [];
  private readonly effects: Array<EffectSlot | undefined> = [];
  private readonly queuedEffects: Array<() => void> = [];
  private cursor = 0;
  private rendering = false;
  private dirty = false;
  private unmounted = false;
  private previousDispatcher: Dispatcher | null = null;

  constructor(private readonly useValue: () => T) {}

  render(): T {
    if (this.unmounted) throw new Error("不能在卸载后重渲染");
    this.cursor = 0;
    this.rendering = true;
    this.previousDispatcher = internals.H;
    internals.H = {
      useState: this.useState,
      useEffect: this.useEffect,
      useCallback: this.useCallback,
      useRef: this.useRef,
    } as Dispatcher;
    try {
      return this.useValue();
    } finally {
      internals.H = this.previousDispatcher;
      this.rendering = false;
      this.flushEffects();
    }
  }

  async settle(): Promise<void> {
    // 一个 tick 消化 Promise.all/then/finally 与随后的 setState。
    for (let round = 0; round < 20; round += 1) {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
      if (this.dirty && !this.rendering) {
        this.dirty = false;
        this.render();
      }
    }
  }

  unmount(): void {
    for (const effect of this.effects) effect?.cleanup?.();
    this.unmounted = true;
  }

  private readonly useState = <S,>(initial: S | (() => S)): [S, React.Dispatch<React.SetStateAction<S>>] => {
    const index = this.cursor++;
    if (!(index in this.states)) {
      this.states[index] = typeof initial === "function" ? (initial as () => S)() : initial;
    }
    const setState: React.Dispatch<React.SetStateAction<S>> = (action) => {
      if (this.unmounted) return;
      const previous = this.states[index] as S;
      const next = typeof action === "function" ? (action as (old: S) => S)(previous) : action;
      if (Object.is(previous, next)) return;
      this.states[index] = next;
      this.dirty = true;
    };
    return [this.states[index] as S, setState];
  };

  private readonly useEffect = (effect: React.EffectCallback, deps?: readonly unknown[]): void => {
    const index = this.cursor++;
    const previous = this.effects[index];
    const changed = !previous || !deps || !previous.deps || deps.length !== previous.deps.length
      || deps.some((value, position) => !Object.is(value, previous.deps[position]));
    this.effects[index] = { deps: deps ?? [], cleanup: previous?.cleanup };
    if (changed) {
      this.queuedEffects.push(() => {
        previous?.cleanup?.();
        const cleanup = effect();
        this.effects[index] = { deps: deps ?? [], cleanup };
      });
    }
  };

  private readonly useCallback = <F extends (...args: never[]) => unknown>(
    callback: F,
    deps: readonly unknown[],
  ): F => {
    const index = this.cursor++;
    const previous = this.memos[index] as { callback: F; deps: readonly unknown[] } | undefined;
    const same = previous && previous.deps.length === deps.length
      && deps.every((value, position) => Object.is(value, previous.deps[position]));
    if (same) return previous.callback;
    const next = { callback, deps };
    this.memos[index] = next;
    return callback;
  };

  private readonly useRef = <S,>(initial: S): { current: S } => {
    const index = this.cursor++;
    if (!this.refs[index]) this.refs[index] = { current: initial };
    return this.refs[index] as { current: S };
  };

  private flushEffects(): void {
    const pending = this.queuedEffects.splice(0);
    for (const effect of pending) effect();
  }
}

/** 用 React 自己的 useState/useEffect 跑 harness，先证明驱动器不是“假执行”。 */
function harnessSmoke(): { count: number; effects: number } {
  let effects = 0;
  const harness = new HookHarness(() => {
    const [count] = useState(0);
    useEffect(() => {
      effects += 1;
    }, []);
    return { count, effects };
  });
  const first = harness.render();
  harness.render();
  harness.unmount();
  return { count: first.count, effects };
}

// ---------------------------------------------------------------------------
// 测试夹具
// ---------------------------------------------------------------------------

const report: DailyReportItem = {
  report_id: "r-1",
  elder_id: "elder-1",
  elder_name: "王奶奶",
  report_date: "2026-11-09",
  payload: {
    health: {
      latest: {
        grade: "yellow",
        systolic_mmhg: 138,
        diastolic_mmhg: 86,
        heart_rate_bpm: 76,
        sleep_hours: 6.5,
        mood_score: 3,
        abnormal_reasons: ["systolic_high", "unknown_reason"],
      },
      sources: { voice_scheduled: 2 },
    },
    medication: {
      counts: { taken_reported: 2, refused: 0, missed_unconfirmed: 1 },
    },
    sources: { health: "health_records", medication: "medication_events", note: "仅作照护参考" },
  },
};

const notification: NotificationItem = {
  notification_id: "n-1",
  elder_id: "elder-1",
  kind: "health_review",
  severity: "red",
  payload: { summary: "血压读数需要复核，建议咨询医生。" },
  created_at: "2026-11-09T06:00:00Z",
};

const listProps = (
  overrides: Partial<ComponentProps<typeof ReportListPage>> = {},
): ComponentProps<typeof ReportListPage> => ({
  elderIds: ["elder-1", "elder-2"],
  elderNames: { "elder-1": "王奶奶", "elder-2": "李爷爷" },
  activeElderId: "elder-1",
  onSelectElder: () => {},
  reports: [],
  loading: false,
  error: null,
  onRetry: () => {},
  onOpenReport: () => {},
  ...overrides,
});

const detailProps = (
  overrides: Partial<ComponentProps<typeof ReportDetailPage>> = {},
): ComponentProps<typeof ReportDetailPage> => ({
  elderId: "elder-1",
  elderName: "王奶奶",
  date: "2026-11-09",
  reports: [report],
  reportsLoading: false,
  reportsError: null,
  onRetryReports: () => {},
  ...overrides,
});

function render(node: ReactNode): string {
  return renderToStaticMarkup(node);
}

function occurrences(html: string, needle: string): number {
  return html.split(needle).length - 1;
}

// ---------------------------------------------------------------------------
// 五个入口组件的真实 SSR 输出
// ---------------------------------------------------------------------------

describe("W0 family · App 与登录页（真实 SSR，effect 限制显式保留）", () => {
  it("App 冷启动先渲染可读的 loading，而不是空白或越权页面", () => {
    const html = render(createElement(FamilyApp));
    assert.match(html, /state-center/);
    assert.match(html, /正在加载/);
    assert.doesNotMatch(html, /日报列表|登录已失效/);
    assert.equal(fetchCalls.length, 0, "SSR 不运行 effect，冷启动身份请求不能在这里冒充覆盖");
  });

  it("登录页真实渲染手机号/密码表单，空值时禁用提交", () => {
    const html = render(createElement(LoginPage, { onSuccess: () => {} }));
    assert.match(html, /安语 · 家属端/);
    assert.match(html, /id="family-phone"/);
    assert.match(html, /id="family-password"/);
    assert.match(html, /type="password"/);
    assert.match(html, /aria-label="显示密码"/);
    assert.match(html, /type="submit"[^>]*disabled/);
    // 输入、失焦、密码切换与 submit 属于客户端交互，SSR 不宣称覆盖。
  });
});

describe("W0 family · 日报列表四态（真实渲染）", () => {
  it("loading：骨架屏带读屏状态", () => {
    const html = render(createElement(ReportListPage, listProps({ loading: true })));
    assert.match(html, /aria-busy="true"/);
    assert.equal(occurrences(html, "skeleton-card"), 4);
    assert.doesNotMatch(html, /这份日报还没生成/);
  });

  it("error：网络错误变成可重试整块，不泄露内部错误", () => {
    const html = render(createElement(ReportListPage, listProps({
      error: new HttpError(0, "NETWORK", "网络不太好，请稍后重试"),
    })));
    assert.match(html, /class="retry-block"/);
    assert.match(html, /网络不太好，点这里重试/);
    assert.doesNotMatch(html, /NETWORK/);
  });

  it("empty：明确说明未生成；单长者时不渲染无意义切换器", () => {
    const html = render(createElement(ReportListPage, listProps({
      elderIds: ["elder-1"],
      elderNames: {},
    })));
    assert.match(html, /这份日报还没生成/);
    assert.match(html, /每天晚间整理完成后生成/);
    assert.doesNotMatch(html, /elder-switcher/);
  });

  it("success：报告卡显示日期、摘要、等级与详情入口，缺名字不显示 UUID", () => {
    const html = render(createElement(ReportListPage, listProps({
      reports: [report],
      elderNames: {},
    })));
    assert.match(html, /11 月 9 日/);
    assert.match(html, /当日血压 138\/86，睡眠 6.5 小时/);
    assert.match(html, /黄 · 待关注/);
    assert.match(html, /查看详情/);
    assert.match(html, /家人 1/);
    assert.doesNotMatch(html, /elder-1|elder-2/);
  });
});

describe("W0 family · 详情与通知（真实执行组件与请求函数）", () => {
  it("详情 SSR 明确只见首帧骨架与趋势，不冒称已执行详情 effect", () => {
    const html = render(createElement(ReportDetailPage, detailProps()));
    assert.match(html, /11 月 9 日/);
    assert.match(html, /王奶奶/);
    assert.match(html, /近 14 天血压趋势/);
    assert.match(html, /aria-busy="true"/);
    assert.doesNotMatch(html, /当日健康|用药记录暂未生成/);
    assert.equal(fetchCalls.length, 0);
  });

  it("详情趋势 error 分支有重试出口，loading 分支不显示假数据", () => {
    const errorHtml = render(createElement(ReportDetailPage, detailProps({
      reportsError: new HttpError(0, "NETWORK", "网络不太好，请稍后重试"),
    })));
    assert.match(errorHtml, /网络不太好，点这里重试/);
    const loadingHtml = render(createElement(ReportDetailPage, detailProps({ reportsLoading: true })));
    assert.match(loadingHtml, /正在加载趋势/);
    assert.doesNotMatch(loadingHtml, /ui-chart/);
  });

  it("单条详情与通知列表真的发出鉴权 GET，查询参数与服务端筛选下推正确", async () => {
    storage.setItem("kb_family_access_token", "family-access");
    storage.setItem("kb_family_refresh_token", "family-refresh");
    useFetch((url) => {
      if (url.startsWith("/api/reports/")) return json(report);
      return json({ items: [notification], total: 1, page: 1 });
    });
    const detail = await getReport("elder / 1", "2026-11-09");
    const page = await listNotifications({ page: 2, pageSize: 20, severity: "red" });
    assert.equal(detail.report_id, "r-1");
    assert.equal(page.items[0]?.notification_id, "n-1");
    assert.deepEqual(fetchCalls.map((call) => call.url), [
      "/api/reports/elder%20%2F%201?date=2026-11-09",
      "/api/notifications?page=2&page_size=20&severity=red",
    ]);
    for (const call of fetchCalls) {
      assert.equal((call.init.headers as Record<string, string>).Authorization, "Bearer family-access");
    }
    assert.equal(fetchCalls.some((call) => call.init.method === "POST"), false, "家属通知只读");
  });

  it("通知首帧 SSR 有全部筛选 chip 与骨架，空结果经真实请求后为空数组", async () => {
    storage.setItem("kb_family_access_token", "family-access");
    const html = render(createElement(NotificationsPage));
    assert.match(html, /aria-label="按严重程度筛选"/);
    assert.match(html, /aria-pressed="true">全部/);
    assert.equal(occurrences(html, "class=\"chip"), 4);
    assert.equal(occurrences(html, "skeleton-card"), 4);
    const empty = await listNotifications({ page: 1, pageSize: 20 });
    assert.deepEqual(empty.items, []);
    assert.equal(empty.total, 0);
  });

  it("通知排序权留在服务端：响应原序保留，红档不被前端重排", async () => {
    const green = { ...notification, notification_id: "n-green", severity: "green" };
    useFetch(() => json({ items: [notification, green], total: 2, page: 1 }));
    const result = await listNotifications();
    assert.deepEqual(result.items.map((item) => item.notification_id), ["n-1", "n-green"]);
    assert.equal(fetchCalls[0]?.url, "/api/notifications?page=1&page_size=20");
  });
});

// ---------------------------------------------------------------------------
// 真实 hooks：同 key 重渲染去重、依赖变化、reload、陈旧响应隔离
// ---------------------------------------------------------------------------

describe("W0 family · hooks 请求生命周期（真实 useState/useEffect）", () => {
  it("轻量 hooks 驱动器执行 React 原语且空依赖 effect 只跑一次", () => {
    assert.deepEqual(harnessSmoke(), { count: 0, effects: 1 });
  });

  it("useReports 同 elder 重渲染不重复请求，reload 才重新拉取", async () => {
    useFetch(() => json({ items: [report], total: 1, days: 30 }));
    let current: ReportsState | null = null;
    const harness = new HookHarness(() => {
      current = useReports("elder-1");
      return current;
    });
    harness.render();
    harness.render();
    assert.equal(fetchCalls.length, 1, "相同依赖重渲染不应产生 N+1");
    await harness.settle();
    assert.equal(current?.reports[0]?.report_id, "r-1");
    assert.equal(current?.loading, false);
    current?.reload();
    harness.render();
    await harness.settle();
    assert.equal(fetchCalls.length, 2, "reload 必须真的再请求一次");
    assert.deepEqual(fetchCalls.map((call) => call.url), [
      "/api/reports/elder-1/list?days=30",
      "/api/reports/elder-1/list?days=30",
    ]);
    harness.unmount();
  });

  it("useReports 无长者不发请求；失败进入 error 而非伪造空成功", async () => {
    useFetch(() => json({ detail: "offline" }, 503));
    let activeId: string | null = null;
    let current: ReportsState | null = null;
    const harness = new HookHarness(() => {
      current = useReports(activeId);
      return current;
    });
    harness.render();
    await harness.settle();
    assert.equal(fetchCalls.length, 0);
    assert.equal(current?.loading, false);
    activeId = "elder-1";
    harness.render();
    await harness.settle();
    assert.equal(fetchCalls.length, 1);
    assert.equal(current?.error instanceof HttpError, true);
    assert.equal((current?.error as HttpError).status, 503);
    assert.deepEqual(current?.reports, []);
    harness.unmount();
  });

  it("useReports 切换长者时丢弃旧响应，不让慢请求覆盖新长者", async () => {
    let resolveOld: ((response: Response) => void) | undefined;
    useFetch((url) => {
      if (url.includes("/elder-1/")) {
        return new Promise<Response>((resolve) => {
          resolveOld = resolve;
        });
      }
      return json({ items: [{ ...report, report_id: "new", elder_id: "elder-2" }] });
    });
    let activeId = "elder-1";
    let current: ReportsState | null = null;
    const harness = new HookHarness(() => {
      current = useReports(activeId);
      return current;
    });
    harness.render();
    activeId = "elder-2";
    harness.render();
    await harness.settle();
    assert.equal(current?.reports[0]?.report_id, "new");
    resolveOld?.(json({ items: [{ ...report, report_id: "stale", elder_id: "elder-1" }] }));
    await harness.settle();
    assert.equal(current?.reports[0]?.report_id, "new", "旧长者慢响应不得覆盖新结果");
    assert.equal(fetchCalls.length, 2);
    harness.unmount();
  });

  it("useElderNames 每位长者一次请求；新数组同 key 不去重错误，换 key 才新增请求", async () => {
    useFetch((url) => {
      if (url.includes("/elder-2/")) return json({ detail: "denied" }, 403);
      return json({ items: [report] });
    });
    let ids = ["elder-1", "elder-2"];
    let names: Record<string, string> = {};
    const harness = new HookHarness(() => {
      names = useElderNames(ids);
      return names;
    });
    harness.render();
    await harness.settle();
    assert.equal(fetchCalls.length, 2, "两位长者应是两条有界请求，不是每次渲染 N 条");
    assert.deepEqual(names, { "elder-1": "王奶奶" }, "一位失败只回退姓名，不阻断另一位");
    ids = [...ids];
    harness.render();
    await harness.settle();
    assert.equal(fetchCalls.length, 2, "同 key 的新数组引用不得触发重复 N+1");
    ids = ["elder-1"];
    harness.render();
    await harness.settle();
    assert.equal(fetchCalls.length, 3);
    assert.equal(fetchCalls.every((call) => call.url.includes("days=1")), true);
    harness.unmount();
  });
});

// ---------------------------------------------------------------------------
// family/api 真实单例：登录、refresh 单飞、双端 storage 隔离
// ---------------------------------------------------------------------------

describe("W0 family · 登录与刷新隔离（真实 family/api 实例）", () => {
  it("family 登录只写 kb_family 三键，logout 不清照护台会话", async () => {
    storage.setItem("kb_console_access_token", "console-access");
    storage.setItem("kb_console_refresh_token", "console-refresh");
    storage.setItem("kb_console_profile", JSON.stringify({ name: "护工" }));
    useFetch(() => json({
      access_token: "family-access",
      refresh_token: "family-refresh",
      principal: { kind: "guardian", id: "g-1", role: null, name: "家属" },
    }));
    const principal = await login("13900000000", "secret1");
    assert.equal(principal.kind, "guardian");
    assert.equal(hasSession(), true);
    assert.equal(storage.getItem("kb_family_access_token"), "family-access");
    assert.equal(storage.getItem("kb_console_access_token"), "console-access");
    logout();
    assert.equal(hasSession(), false);
    assert.equal(storage.getItem("kb_console_access_token"), "console-access");
    assert.equal(storage.getItem("kb_console_refresh_token"), "console-refresh");
  });

  it("family 并发 401 共用一次 refresh，重放只用 family 新 token，不碰 console", async () => {
    storage.setItem("kb_family_access_token", "old-family");
    storage.setItem("kb_family_refresh_token", "refresh-family");
    storage.setItem("kb_console_access_token", "console-access");
    storage.setItem("kb_console_refresh_token", "console-refresh");
    let resolveRefresh: ((response: Response) => void) | undefined;
    useFetch((url, init) => {
      if (url === "/api/sessions/refresh") {
        assert.equal(JSON.parse(String(init.body)).refresh_token, "refresh-family");
        return new Promise<Response>((resolve) => {
          resolveRefresh = resolve;
        });
      }
      const token = (init.headers as Record<string, string>).Authorization;
      return token === "Bearer old-family"
        ? new Response("", { status: 401 })
        : json({ ok: true, token });
    });
    const first = authedFetch("/api/reports/elder-1/list?days=30");
    // fetchCurrent 返回的是**身份对象**（内部已判 res.ok），不是 Response，
    // 不能与 authedFetch 的 Response 一起按 .status 断言。
    const second = fetchCurrent();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    const refreshCalls = fetchCalls.filter((call) => call.url === "/api/sessions/refresh");
    assert.equal(refreshCalls.length, 1, "同端并发 refresh 必须单飞");
    resolveRefresh?.(json({ access_token: "new-family" }));
    const [listResponse, identity] = await Promise.all([first, second]);
    assert.equal(listResponse.status, 200);
    // fetchCurrent 返回的是**响应体解析结果**；本用例的替身对非 401 一律回 {ok,token}，
    // 所以这里断言「重放确实带着新 token 到达了服务端」，而不是断言 principal 形状。
    assert.equal((identity as { token?: string }).token, "Bearer new-family");
    assert.equal(storage.getItem("kb_family_access_token"), "new-family");
    assert.equal(storage.getItem("kb_console_access_token"), "console-access");
    assert.equal(storage.getItem("kb_console_refresh_token"), "console-refresh");
    const replays = fetchCalls.filter((call) =>
      (call.init.headers as Record<string, string>).Authorization === "Bearer new-family");
    assert.equal(replays.length, 2);
  });

  it("refresh 被拒清 family 且只回调一次 family handler，console 键原样保留", async () => {
    storage.setItem("kb_family_access_token", "old-family");
    storage.setItem("kb_family_refresh_token", "expired-family");
    storage.setItem("kb_console_access_token", "console-access");
    storage.setItem("kb_console_refresh_token", "console-refresh");
    useFetch(() => new Response("", { status: 401 }));
    let unauthorized = 0;
    setUnauthorizedHandler(() => {
      unauthorized += 1;
    });
    const response = await authedFetch("/api/notifications?page=1&page_size=20");
    assert.equal(response.status, 401);
    assert.equal(unauthorized, 1);
    assert.equal(hasSession(), false);
    assert.equal(storage.getItem("kb_family_refresh_token"), null);
    assert.equal(storage.getItem("kb_console_access_token"), "console-access");
    assert.equal(storage.getItem("kb_console_refresh_token"), "console-refresh");
  });

  it("listReports 使用真实 family 鉴权层，失败 401 不会悄悄变空成功", async () => {
    storage.setItem("kb_family_access_token", "old-family");
    storage.setItem("kb_family_refresh_token", "expired-family");
    useFetch(() => new Response("", { status: 401 }));
    await assert.rejects(() => listReports("elder-1"), (error: unknown) => {
      assert.equal(error instanceof HttpError, true);
      assert.equal((error as HttpError).status, 401);
      assert.equal((error as HttpError).code, "TOKEN_INVALID");
      return true;
    });
    assert.equal(hasSession(), false);
  });
});
