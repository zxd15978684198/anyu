/**
 * 一键求助（PRD FR-ALT-06）。前端唯一出口，组件里不写裸 `fetch`。
 *
 * 口径：
 * - 长者按下的这一刻就要有结果页，不能等网络回来才变界面（急事上「点了没反应」最坏）；
 *   所以调用方先切到结果面板，再用返回值补「是否真的建单成功」的细字。
 * - 不传 `elder_id`：服务端只认设备令牌绑定的那位（AGENTS §4.9）。
 * - 不提供「撤销」（FR-UI-08 红色事件不允许长者一键撤销）。
 */

import { deviceToken } from "./http";

export type SosOutcome = {
  ok: boolean;
  /** 服务端是否真的新生成了一条求助（重复点按会 `created=false`） */
  created: boolean;
  /** 工单号，便于长者念给护工听；后端没给就是 null */
  workOrderNo: string | null;
  /** 给长者看的一句话（不出现内部错误码） */
  message: string;
};

const NETWORK_TEXT = "已经把求助记下来了，正在通知护工…";
const UNAUTHORIZED_TEXT = "登录已失效，请重新登录后再按一次求助";
const UNBOUND_TEXT = "这台设备还没绑定住户，请呼叫工作人员";

/**
 * 上报一次求助。
 *
 * 网络/服务异常时返回 `ok:false` 但仍 `created:false`：结果页照常显示「已通知」，
 * 因为本地并不掌握护工是否收到——宁可让长者安心等待也不要让人反复按。
 */
export async function reportSos(note?: string): Promise<SosOutcome> {
  const token = deviceToken();
  if (!token) {
    return { ok: false, created: false, workOrderNo: null, message: UNAUTHORIZED_TEXT };
  }
  let res: Response;
  try {
    res = await fetch("/api/sos", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(note ? { note } : {}),
    });
  } catch {
    return { ok: false, created: false, workOrderNo: null, message: NETWORK_TEXT };
  }

  if (res.status === 409) {
    return { ok: false, created: false, workOrderNo: null, message: UNBOUND_TEXT };
  }
  if (res.status === 401) {
    return { ok: false, created: false, workOrderNo: null, message: UNAUTHORIZED_TEXT };
  }
  if (!res.ok) {
    return { ok: false, created: false, workOrderNo: null, message: NETWORK_TEXT };
  }

  let body: { created?: boolean; work_order_no?: string | null } = {};
  try {
    body = (await res.json()) as typeof body;
  } catch {
    // 结果体解析失败不影响「求助已上报」这一事实
  }
  return {
    ok: true,
    created: body.created ?? true,
    workOrderNo: body.work_order_no ?? null,
    message: "已通知护工和家属，请坐着等一会儿",
  };
}
