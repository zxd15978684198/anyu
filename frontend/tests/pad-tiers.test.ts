/**
 * 长者端多端适配的档位契约（B7）。
 *
 * ## 为什么读 CSS 而不是渲染
 *
 * 仓库不许引 jsdom/vitest（AGENTS §4-12），node --test 只做类型擦除。真正逐档的
 * rect 量取由 `.tmp/adhoc-20261111/measure.mjs`（CDP + 系统 Chrome）做，本文件钉的是
 * **那次量取的结论不许被下一次改动悄悄推翻**：
 *
 *  1. 两条长者下限（正文 ≥20px、触控 ≥48px）在**每一档**都成立 —— 逐块扫 `:root`，
 *     不是只看默认档。上一版就是「D 档合规、P 档把字号压到 18」漏过去的。
 *  2. 竖屏必须真的能用：`#rotate-hint` 那堵「请横屏」墙（`#root{display:none}`）
 *     不许回来。挡屏不是适配。
 *  3. 3D 只许用 opacity 隐去，不许 `display:none`、不许卸载 —— 卸载会断
 *     `avatar_ready` 上报，服务端开场白要等满 5s（PRD FR-3D-02 的降级口径）。
 *  4. 每档的顶栏/底栏默认值 = CDP 实测值。对不上就说明版面改了没重量，
 *     登录页 → 主界面那一瞬间数字人会跳。
 *
 * 复跑：node scripts/run-frontend-tests.mjs
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

const CSS = read("index.css").replace(/\/\*[\s\S]*?\*\//g, "");
const HTML = readFileSync(fileURLToPath(new URL("../index.html", import.meta.url)), "utf8");
const BOX_SRC = read("avatar/avatarBox.ts");
const LAYER_SRC = read("avatar/AvatarLayer.tsx");
const STANDBY_SRC = read("components/StandbyPanel.tsx");
const LOGIN_SRC = read("pages/LoginPage.tsx");
const RULES_SRC = readFileSync(
  fileURLToPath(new URL("../../app/router/rules.py", import.meta.url)),
  "utf8",
);

interface RootBlock {
  media: string;
  decls: Map<string, string>;
}

/** 极简 CSS 扫描：只认 `@media` 与 `:root`，够用来逐档取令牌（不引解析器）。 */
function collectRoots(source: string, media = "", out: RootBlock[] = []): RootBlock[] {
  let i = 0;
  while (i < source.length) {
    const open = source.indexOf("{", i);
    if (open < 0) break;
    const head = source.slice(i, open).trim();
    let depth = 1;
    let j = open + 1;
    while (j < source.length && depth > 0) {
      if (source[j] === "{") depth += 1;
      else if (source[j] === "}") depth -= 1;
      j += 1;
    }
    const body = source.slice(open + 1, j - 1);
    if (head.startsWith("@media")) {
      collectRoots(body, head.replace(/^@media\s*/, "").trim(), out);
    } else if (head === ":root") {
      const decls = new Map<string, string>();
      for (const line of body.split(";")) {
        const colon = line.indexOf(":");
        if (colon > 0) decls.set(line.slice(0, colon).trim(), line.slice(colon + 1).trim());
      }
      out.push({ media, decls });
    }
    i = j;
  }
  return out;
}

const ROOTS = collectRoots(CSS);

/** 令牌值转像素；不是长度（`100%` / `clamp()` / `var()`）就返回 null，交给别的用例。 */
function px(value: string | undefined): number | null {
  if (!value) return null;
  const match = /^([0-9.]+)(px|rem)$/.exec(value);
  if (!match) return null;
  return match[2] === "rem" ? Number(match[1]) * 16 : Number(match[1]);
}

const label = (block: RootBlock): string => block.media || "D 档（:root 默认）";

test("档位扫描器自检：八个设备档 + 两个登录页矮屏覆盖都在，默认档在最前", () => {
  // 8 个设备档（D / Dshort / T / D2 / P / TP / PV / UW）+ 2 个跨档的登录页覆盖
  // （max-height: 860px 收尺寸、矮屏横屏改两列）。登录页覆盖不是新设备档，
  // 放在所有设备档之后，MEASURED_BANDS 的 find 仍先命中设备档。
  assert.equal(ROOTS.length, 10, `扫到 ${ROOTS.length} 个 :root 块，档位增删后请同步本用例`);
  assert.equal(ROOTS[0].media, "", "第一个 :root 必须是无媒体条件的默认档（D）");
  assert.ok(ROOTS[0].decls.has("--pad-header-h"));
});

test("正文下限：每一档的字号令牌都 ≥20px", () => {
  const textTokens = [
    "--pad-status-text",
    "--pad-sos-text",
    "--pad-talk-text",
    "--pad-hint",
    "--pad-med-text",
    "--pad-med-title",
    "--pad-ask-text",
    "--pad-ask-title",
    "--pad-subtitle-prev",
    "--pad-subtitle-lg",
    "--pad-subtitle-md",
    "--pad-subtitle-sm",
    "--pad-login-h1",
    "--pad-login-text",
    "--pad-login-note",
    "--pad-drawer-title",
  ];
  for (const block of ROOTS) {
    for (const token of textTokens) {
      const value = px(block.decls.get(token));
      if (value === null) continue;
      assert.ok(value >= 20, `${label(block)} 的 ${token} = ${value}px，破了长者端 20px 正文下限`);
    }
  }
});

test("触控下限：每一档的可按控件高度都 ≥48px", () => {
  const touchTokens = [
    "--pad-status-h",
    "--pad-sos-h",
    "--pad-talk-h",
    "--pad-ask-h",
    "--pad-login-field-h",
    "--pad-login-btn-h",
    "--pad-login-sub-h",
  ];
  for (const block of ROOTS) {
    for (const token of touchTokens) {
      const value = px(block.decls.get(token));
      if (value === null) continue;
      assert.ok(value >= 48, `${label(block)} 的 ${token} = ${value}px，破了 48px 触控下限`);
    }
  }
});

test("竖屏真的能用：「请横屏」挡屏墙不许回来", () => {
  assert.ok(!HTML.includes("rotate-hint"), "index.html 又塞回了横屏引导块");
  assert.ok(!CSS.includes("rotate-hint"), "index.css 又写回了横屏引导样式");
  assert.ok(
    !/#root\s*\{[^}]*display:\s*none/.test(CSS),
    "#root 又被整块隐藏：竖屏打不开等于没适配",
  );
  // 竖屏两档必须真的存在（而不是靠默认档硬撑）
  assert.ok(
    ROOTS.some((b) => b.media.includes("orientation: portrait") && b.media.includes("601px")),
    "缺平板竖屏档（TP）",
  );
  assert.ok(
    ROOTS.some((b) => b.media.includes("orientation: portrait") && b.media.includes("600px")),
    "缺手机竖屏档（PV）",
  );
});

test("3D 只许用 opacity 隐去：display:none / 卸载都会断掉 avatar_ready", () => {
  assert.ok(
    !/\.aspect-square\s*\{[^}]*display:\s*none/.test(CSS),
    "3D 盒又被 display:none：rAF 停摆、avatar_ready 不再上报，开场白要等满 5s",
  );
  for (const block of ROOTS) {
    const opacity = block.decls.get("--pad-avatar-opacity");
    if (opacity === undefined) continue;
    assert.ok(opacity === "0" || opacity === "1", `${label(block)} 的 opacity 只应是 0 或 1`);
  }
  const phoneLandscape = ROOTS.find((b) => b.media.includes("max-width: 1023px"));
  assert.equal(phoneLandscape?.decls.get("--pad-avatar-opacity"), "0", "手机横屏没隐去 3D");
  const phonePortrait = ROOTS.find(
    (b) => b.media.includes("600px") && b.media.includes("orientation: portrait"),
  );
  assert.notEqual(
    phonePortrait?.decls.get("--pad-avatar-opacity"),
    "1",
    "手机竖屏又把 3D 打开了：375×667 减掉三带只剩 333px，放不下能看的正方形",
  );
  const tabletPortrait = ROOTS.find(
    (b) => b.media.includes("601px") && b.media.includes("orientation: portrait"),
  );
  assert.equal(
    tabletPortrait?.decls.get("--pad-avatar-opacity"),
    "1",
    "平板竖屏该把 3D 放回来：768×1024 上半有 446px，够站一个整身",
  );
  // 2026-11-13 登录页不显示数字人：opacity 改成「可见性 ? 变量 : 0」，仍然只动 opacity
  assert.match(
    LAYER_SRC,
    /opacity: visible \? "var\(--pad-avatar-opacity\)" : 0/,
    "隐去没走 opacity 变量",
  );
  assert.equal(LAYER_SRC.includes("display: none"), false, "不许用 display:none 隐去 3D");
});

test("几何令牌真的被消费：写了没人读的变量等于没写", () => {
  assert.match(BOX_SRC, /max-h-\[var\(--pad-avatar-max\)\]/, "3D 盒没消费 --pad-avatar-max");
  assert.match(BOX_SRC, /max-w-\[var\(--pad-avatar-max\)\]/, "3D 盒没消费 --pad-avatar-max");
  assert.match(BOX_SRC, /aspect-square/, "3D 盒不再是正方形");
  for (const edge of ["top", "bottom", "left", "right"]) {
    assert.match(
      LAYER_SRC,
      new RegExp(`${edge}: "(?:calc\\()?var\\(--pad-stage-${edge}\\)`),
      `数字人层的 ${edge} 没走 --pad-stage-${edge}`,
    );
  }
  // 超宽档整页居中，fixed 层必须补上居中量，否则人比舞台左偏
  assert.match(
    LAYER_SRC,
    /left: "calc\(var\(--pad-stage-left\) \+ var\(--pad-shell-inset\)\)"/,
    "超宽档居中量没补：2560 上数字人比舞台左偏 80px",
  );
  const uw = ROOTS.find((b) => b.media.includes("2200px"));
  assert.ok(uw?.decls.get("--pad-shell-inset"), "超宽档没给 --pad-shell-inset");
});

/**
 * 每档默认值 = CDP 实测值（2026-11-11，`.tmp/adhoc-20261111/measure.mjs`）。
 * 判红时先重量再改数，别反过来照着数调版面。
 */
const MEASURED_BANDS: [string, number, number][] = [
  ["", 84, 161], // D 1920×1080 / 2560×1440
  ["min-width: 1440px", 80, 161], // Dshort 1536×702
  ["max-width: 1439px", 64, 140], // T 1024×768（footer 实测 136，默认 140 是上限）
  ["min-width: 1200px", 64, 146], // D2 1280×800（header 继承 T）
  ["max-width: 1023px", 114, 132], // P 932×430（footer 实测 128）
  ["601px", 114, 148], // TP 768×1024（footer 实测 144）
  ["600px", 170, 168], // PV 375×667（footer 实测 164）
];

test("带宽默认值贴着实测：登录页 → 主界面那一帧不许跳", () => {
  for (const [mediaKey, header, footer] of MEASURED_BANDS) {
    const block = ROOTS.find((b) =>
      mediaKey === "" ? b.media === "" : b.media.includes(mediaKey),
    );
    assert.ok(block, `找不到档位 ${mediaKey || "D"}`);
    const inheritedHeader = px(block.decls.get("--pad-header-h"));
    if (inheritedHeader !== null) {
      assert.equal(inheritedHeader, header, `${label(block)} 的 --pad-header-h 与实测不符`);
    }
    const declaredFooter = px(block.decls.get("--pad-footer-h-real"));
    if (declaredFooter !== null) {
      assert.ok(
        declaredFooter >= footer - 8 && declaredFooter <= footer + 8,
        `${label(block)} 的 --pad-footer-h-real = ${declaredFooter}px，离实测 ${footer}px 太远`,
      );
    }
  }
});

test("堆叠档：中部改上下排，舞台让出字幕带的高度", () => {
  const stacked = ROOTS.filter((b) => b.decls.get("--pad-main-dir") === "column");
  assert.ok(stacked.length >= 1, "没有任何一档改成上下堆叠：窄屏字幕会被挤成竖线");
  for (const block of stacked) {
    assert.equal(block.decls.get("--pad-stage-right"), "0px", `${label(block)} 舞台还在给右栏让位`);
    assert.match(
      block.decls.get("--pad-stage-bottom") ?? "",
      /--pad-subtitle-band-h/,
      `${label(block)} 舞台底部没算上字幕带：3D 会压在字幕上`,
    );
  }
  assert.match(CSS, /#root main \{\s*flex-direction: var\(--pad-main-dir\);/, "中部没消费排布变量");
});

test("待机四个常问句：命中的是规则层原词，不是随手编的说法", () => {
  const asks = [...STANDBY_SRC.matchAll(/\{ text: "([^"]+)", hint: "([^"]+)" \}/g)].map((m) => ({
    text: m[1],
    hint: m[2],
  }));
  assert.equal(asks.length, 4, "常问句不是四条：两列两行的版面是按四条算的");

  // 逐条回 rules.py 找正则，用 Python 语义的近似判定：把分支 pattern 抠出来在 JS 里跑。
  // 这些正则只用到 (?:)、?、*、\s 与中文字面量，JS 与 Python 语义一致。
  const patterns = [...RULES_SRC.matchAll(/pattern=_compile\(\s*((?:r"[^"]*"\s*)+)\)/g)].map((m) =>
    [...m[1].matchAll(/r"([^"]*)"/g)].map((s) => s[1]).join(""),
  );
  assert.ok(patterns.length >= 7, `只抠到 ${patterns.length} 条规则正则，rules.py 写法变了`);
  for (const ask of asks) {
    const hit = patterns.some((p) => new RegExp(p).test(ask.text));
    assert.ok(hit, `「${ask.text}」没命中任何 L0 固定意图：会白掉到 L2，慢且费预算`);
  }
});

test("待机面板：按下去就锁住，不会连点成两三轮", () => {
  assert.match(STANDBY_SRC, /disabled=\{asked !== null\}/, "没锁住：回复上屏前能连点");
  assert.match(STANDBY_SRC, /onAsk\(ask\.text\)/, "没把原句交给调用方");
  assert.ok(
    !/fetch\(|sendTextTurn\(/.test(STANDBY_SRC),
    "面板自己发请求了：必须走 App 的 submitTextTurn，与打字框同一条链路",
  );
  assert.match(STANDBY_SRC, /min-h-\[var\(--pad-ask-h\)\]/, "按钮高度没走令牌：窄档会破触控下限");
});

/**
 * 登录页矮屏（2026-11-11 B8 复测抓到的真缺陷）。
 *
 * 原来 form 自己是 `h-full flex-col justify-center`：内容比视口高时 flex 子项被压缩
 * ——1536×702 与 932×430 上「登录」80px→36px、「填入演示账号」64px→36px，双双破
 * 48px 触控下限；justify-center 还把溢出的一半顶到视口外（h1 y=-4、末行提示 y=650）。
 * 修法两件：①外层滚动 + 内层 min-h-full 居中（装得下居中、装不下长高，谁也不被压扁）；
 * ②尺寸走令牌，矮屏收一档，横屏矮窗口改左右两列。
 */
test("登录页：矮屏不压扁按钮、不把内容顶出屏幕", () => {
  assert.match(LOGIN_SRC, /overflow-y-auto/, "登录页没有滚动兜底：装不下时内容会被裁掉");
  assert.match(
    LOGIN_SRC,
    /min-h-full flex-col justify-center/,
    "居中排布没放进 min-h-full 的内层：h-full 直接挂 flex 列会压缩子项",
  );
  assert.match(LOGIN_SRC, /h-\[var\(--pad-login-btn-h\)\]/, "主按钮高度没走令牌");
  assert.match(LOGIN_SRC, /h-\[var\(--pad-login-sub-h\)\]/, "次按钮高度没走令牌");
  assert.equal(
    (LOGIN_SRC.match(/h-\[var\(--pad-login-field-h\)\]/g) ?? []).length,
    2,
    "两个输入框都要走 --pad-login-field-h",
  );
  assert.ok(!/text-2xl|text-xl\b/.test(LOGIN_SRC), "登录页还有写死的正文字号：矮屏收不下来");
  assert.equal(
    (LOGIN_SRC.match(/pad-login-col/g) ?? []).length,
    2,
    "登录页不是两列分组：矮屏横屏无法并排，单列 8 块在 430px 里必然溢出",
  );
  assert.match(
    CSS,
    /@media \(max-height: 560px\) and \(orientation: landscape\)/,
    "缺横屏矮窗口的两列覆盖",
  );
  assert.match(
    CSS,
    /#root > form > \.pad-login \{\s*flex-direction: row;/,
    "两列覆盖没真的改成横排",
  );
});