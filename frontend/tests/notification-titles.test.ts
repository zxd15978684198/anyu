/**
 * 工单类通知的「人话摘要」契约（2026-11-13）。
 *
 * 背景：真机验证发现 `work_order` 通知在家属端只显示类型名「工单」，看不到单号与状态。
 * 这里锁住两端同一口径：`payload.order_no` + `to_status` → 「工单 WO-… 已接单」。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { NotificationItem } from "../src/family/notifications.ts";
import {
  kindLabel as familyKindLabel,
  payloadSummary as familySummary,
} from "../src/family/notifications.ts";
import { payloadSummary as consoleSummary } from "../src/console/notifications.ts";

function familyItem(overrides: Partial<NotificationItem>): NotificationItem {
  return {
    notification_id: "n-1",
    kind: "work_order",
    severity: "red",
    status: "generated",
    created_at: "2026-11-13T12:07:21.413087Z",
    payload: null,
    ...overrides,
  } as NotificationItem;
}

describe("工单通知：家属端摘要", () => {
  it("已接单：拼出单号与状态", () => {
    const item = familyItem({
      payload: { order_no: "WO-20261113-0001", to_status: "acknowledged" },
    });
    assert.equal(familySummary(item), "工单 WO-20261113-0001 已接单");
  });

  it("已关闭：状态词跟着变", () => {
    const item = familyItem({
      payload: { order_no: "WO-20261113-0001", to_status: "closed" },
    });
    assert.equal(familySummary(item), "工单 WO-20261113-0001 已关闭");
  });

  it("未知状态：只给单号，不瞎猜状态", () => {
    const item = familyItem({
      payload: { order_no: "WO-20261113-0001", to_status: "whatever" },
    });
    assert.equal(familySummary(item), "工单 WO-20261113-0001");
  });

  it("缺 order_no：退回原行为（不看工单口径，继续找 summary / 类型名）", () => {
    const item = familyItem({ payload: { to_status: "acknowledged" } });
    assert.equal(familySummary(item), familyKindLabel("work_order"));
    assert.equal(familySummary(item), "工单");
  });

  it("非工单类型：不套工单模板", () => {
    const item = familyItem({ kind: "health_review", payload: { summary: "血压偏高" } });
    assert.equal(familySummary(item), "血压偏高");
  });
});

describe("类型名：七类都必须有中文，不许把 kind 原文怼给家属", () => {
  const expected: Record<string, string> = {
    health_review: "健康复核",
    medication_escalation: "用药升级",
    medication_review: "到期复核",
    collection_missed: "漏采",
    family_report: "日报",
    safety_alert: "安全提醒",
    attention_signal: "建议关注",
    work_order: "工单",
  };
  for (const [kind, label] of Object.entries(expected)) {
    it(`${kind} → ${label}`, () => {
      assert.equal(familyKindLabel(kind), label);
      assert.notEqual(familyKindLabel(kind), kind, "不许回退成英文 kind");
    });
  }
});

describe("工单通知：照护台摘要（与家属端同口径）", () => {
  it("已接单", () => {
    assert.equal(
      consoleSummary({ order_no: "WO-20261113-0001", to_status: "acknowledged" }, "work_order"),
      "工单 WO-20261113-0001 已接单",
    );
  });

  it("不传 kind 时保持旧行为（不会把所有通知都当工单）", () => {
    const text = consoleSummary({ order_no: "WO-20261113-0001" });
    // 无 kind 时走通用摘要（对象只剩「N 个字段」提示），绝不能冒出工单模板。
    assert.ok(!text.includes("工单"), `unexpected work-order template: ${text}`);
  });

  it("非工单类型带 order_no 也不套模板", () => {
    const text = consoleSummary({ order_no: "WO-1", to_status: "closed" }, "health_review");
    assert.ok(!text.includes("已关闭"), `unexpected work-order template: ${text}`);
  });
});
