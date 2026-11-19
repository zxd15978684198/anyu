/**
 * 共享时间格式化契约（前端计划 W2，Q-1 已裁决放开共享）。
 *
 * 核心断言：**把进程时区改成非东八区，输出仍必须是上海时间**。
 * 本机通常就是 +08，不切时区的话「用了浏览器本地时区」这个缺陷根本测不出来
 * ——收敛前 console 12 份实现里多数正是这个毛病（AGENTS §8 违规）。
 *
 * 另钉住两端刻意不同的契约（不是冲突）：
 * console 解析不了原样返回、family 解析不了返回 —。
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { after, before, describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  formatDateLabel,
  formatDateOnly,
  formatDateTimeFull,
  formatDateTimeShort,
  formatInstant,
  formatShortDate,
  isToday,
  parseDateOnly,
  parseInstant,
  shiftDate,
  shortId,
  todayInShanghai,
} from "../src/shared/format.ts";

const originalTz = process.env.TZ;

before(() => {
  // 纽约（冬令时 UTC-5）：与上海差 13 小时，任何「走本地时区」的实现都会露馅。
  process.env.TZ = "America/New_York";
});

after(() => {
  if (originalTz === undefined) delete process.env.TZ;
  else process.env.TZ = originalTz;
});

describe("W2 · 展示时区固定 Asia/Shanghai，不随运行环境漂移", () => {
  it("UTC 00:30 在东八区是 08:30（纽约时区下仍须如此）", () => {
    const text = formatDateTimeFull("2026-11-10T00:30:00Z");
    assert.equal(text, "2026-11-10 08:30");
  });

  it("跨日边界：UTC 16:00 是上海次日 00:00，不是当日", () => {
    assert.equal(formatDateTimeFull("2026-11-10T16:00:00Z"), "2026-11-11 00:00");
  });

  it("带 +00:00 偏移的串同样正确", () => {
    assert.equal(formatDateTimeFull("2026-11-10T04:00:00+00:00"), "2026-11-10 12:00");
  });

  it("不带时区后缀的串按 UTC 解释（补 Z），不被当本地时间", () => {
    // 若不补 Z，纽约时区会把 04:00 当本地 → 上海 17:00，整整差 5 小时。
    assert.equal(formatDateTimeFull("2026-11-10T04:00:00"), "2026-11-10 12:00");
  });

  it("短形状同样固定上海（家属端 375px 窄列）", () => {
    assert.equal(formatDateTimeShort("2026-11-10T00:30:00Z"), "11-10 08:30");
  });

  it("只要日期时也按上海自然日，不掉天", () => {
    assert.equal(formatDateOnly("2026-11-10T16:00:00Z"), "2026-11-11");
  });

  it("formatInstant 可配年份与非法值策略", () => {
    assert.equal(formatInstant("2026-11-10T00:30:00Z", { withYear: false }), "11-10 08:30");
    assert.equal(formatInstant("坏数据", { invalid: "raw" }), "坏数据");
    assert.equal(formatInstant("坏数据", { invalid: "dash" }), "—");
  });
});

describe("W2 · 两端契约刻意不同，各自钉住", () => {
  it("console：空 → —，解析不了 → 原样返回（不吞掉）", () => {
    assert.equal(formatDateTimeFull(null), "—");
    assert.equal(formatDateTimeFull(undefined), "—");
    assert.equal(formatDateTimeFull(""), "—");
    assert.equal(formatDateTimeFull("坏数据"), "坏数据");
  });

  it("family：空与解析不了都 → —", () => {
    assert.equal(formatDateTimeShort(null), "—");
    assert.equal(formatDateTimeShort(""), "—");
    assert.equal(formatDateTimeShort("坏数据"), "—");
  });

  it("parseInstant 对空串与非法值返回 null", () => {
    assert.equal(parseInstant(""), null);
    assert.equal(parseInstant("坏数据"), null);
    assert.equal(parseInstant("2026-11-10T00:30:00Z") instanceof Date, true);
  });
});

describe("W2 · 自然日串只做纯 UTC 算术，不经本地时区", () => {
  it("parseDateOnly 容忍带时间的串，只取前 10 位", () => {
    assert.deepEqual(parseDateOnly("2026-11-09T00:00:00"), { y: 2026, m: 11, d: 9 });
    assert.deepEqual(parseDateOnly("2026-11-09"), { y: 2026, m: 11, d: 9 });
  });

  it("formatShortDate 补零", () => {
    assert.equal(formatShortDate("2026-01-05"), "01-05");
  });

  it("formatDateLabel 带星期，且星期按 UTC 算不受时区影响", () => {
    // 2026-11-09 是周一。
    assert.equal(formatDateLabel("2026-11-09"), "11 月 9 日 周一");
  });

  it("shiftDate 跨月跨年正确", () => {
    assert.equal(shiftDate("2026-11-09", 1), "2026-11-10");
    assert.equal(shiftDate("2026-11-30", 1), "2026-12-01");
    assert.equal(shiftDate("2026-01-01", -1), "2025-12-31");
    assert.equal(shiftDate("2026-11-09", -9), "2026-10-31");
  });

  it("todayInShanghai 返回 YYYY-MM-DD 且与 isToday 自洽", () => {
    const today = todayInShanghai();
    assert.match(today, /^\d{4}-\d{2}-\d{2}$/);
    assert.equal(isToday(today), true);
    assert.equal(isToday(shiftDate(today, -1)), false);
    assert.equal(isToday(`${today}T00:00:00`), true);
  });

  it("shortId 只截前 8 位，短值原样", () => {
    assert.equal(shortId("11111111-1111-4111"), "11111111");
    assert.equal(shortId("abc"), "abc");
  });
});

describe("W2 · shared 是叶子模块", () => {
  const text = readFileSync(new URL("../src/shared/format.ts", import.meta.url), "utf8");

  it("不 import pad / console / family（否则共享层反向依赖业务端）", () => {
    assert.equal(/from\s+"\.\.\/(pad|console|family)\//.test(text), false);
  });

  it("不 import three / three-vrm（隔离硬约束）", () => {
    assert.equal(/from\s+"(three|@pixiv)/.test(text), false);
  });

  it("时区常量只有一处，且是 Asia/Shanghai", () => {
    const hits = text.match(/Asia\/Shanghai/g) ?? [];
    assert.equal(hits.length >= 1, true);
    assert.equal(/const SHANGHAI = "Asia\/Shanghai";/.test(text), true);
  });
});

describe("W2 · console 12 份实现已全部收敛", () => {
  const files = [
    "audit.ts",
    "memory.ts",
    "workOrders.ts",
    "knowledge.ts",
    "devices.ts",
    "medications.ts",
    "pages/EldersPage.tsx",
    "pages/GuardiansPage.tsx",
    "pages/HealthPage.tsx",
    "pages/HomePage.tsx",
    "pages/NotificationsPage.tsx",
    "pages/ReportsPage.tsx",
  ];

  it("每个文件都改为引用 shared/format", () => {
    for (const f of files) {
      const text = readFileSync(new URL(`../src/console/${f}`, import.meta.url), "utf8");
      assert.equal(
        /from "\.\.\/(\.\.\/)?shared\/format"/.test(text),
        true,
        `${f} 未引用 shared/format`,
      );
    }
  });

  it("console 与 family 源码里不再有走本地时区的格式化调用", () => {
    for (const end of ["console", "family"]) {
      const dir = new URL(`../src/${end}/`, import.meta.url);
      const walk = (url: URL): string[] => {
        const out: string[] = [];
        for (const name of readdirSync(url)) {
          const child = new URL(name, url);
          if (statSync(child).isDirectory()) out.push(...walk(new URL(`${name}/`, url)));
          else if (/\.(ts|tsx)$/.test(name)) out.push(fileURLToPath(child));
        }
        return out;
      };
      for (const p of walk(dir)) {
        const text = readFileSync(p, "utf8");
        const code = text
          // 先剥块注释（含 /** … */ 首行），再剥行注释：
          // 注释里会正当地提到 getTimezoneOffset() 说明旧实现的毛病，不能当成残留。
          .replace(/\/\*[\s\S]*?\*\//g, "")
          .split("\n")
          .filter((line) => !line.trim().startsWith("//"))
          .join("\n");
        // 豁免 `datetime-local` 往返助手：浏览器把 <input type="datetime-local"> 按
        // **本地时区**解释，所以 toLocalInput/toApiIso 这一对必须用本地时区才能无损往返
        // （与 audit.ts / AuditPage 的同类字段一致）。它们不是「展示」，是表单编解码。
        const withoutLocalInput = code.replace(
          /export function (toLocalInput|toApiIso)\([\s\S]*?\n\}/g,
          "",
        );
        assert.equal(
          /toLocaleString\(|toLocaleDateString\(|toLocaleTimeString\(|\.getHours\(\)|getTimezoneOffset\(\)/.test(withoutLocalInput),
          false,
          `${p} 仍有走本地时区的**展示**格式化或「今天」计算（AGENTS §8：展示一律 Asia/Shanghai；datetime-local 表单往返已豁免）`,
        );
      }
    }
  });

  it("family/format.ts 是薄再导出，不再自己实现", () => {
    const text = readFileSync(new URL("../src/family/format.ts", import.meta.url), "utf8");
    assert.equal(text.includes('from "../shared/format"'), true);
    assert.equal(/new Intl\.DateTimeFormat/.test(text), false, "不应再自己实现格式化");
  });
});