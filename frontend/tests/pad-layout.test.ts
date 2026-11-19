/**
 * 长者端布局/交互回归钉子（2026-11-11 产品负责人 CDP 截图实测后提的六条问题）。
 *
 * 为什么读源码而不是渲染：Node 的 `--test` 只做类型擦除、不认 TSX，仓库不许引
 * jsdom/vitest（AGENTS §4-12）。六条问题里有四条是**样式与状态机**问题（卡片压住
 * 数字人、双滚动条、没有按下反馈、空输入按钮不可辨），渲染层断言要真浏览器才有意义；
 * 在当前测试栈里能钉住的是「那条修复会不会被下一次改动悄悄回退」，所以逐条断言
 * 具体的类名与状态文案，并写明回退后果。
 *
 * 删掉什么会变红：
 *  1. `TodayMedications` 又被塞回中部左栏（`absolute` / `bottom-*`）→ 卡片重新压住数字人左腿；
 *  2. 用药卡片去掉 `bodyFit` → 面板内层重新出现滚动条，「已服用」又被挤到外面；
 *  3. `HoldToTalk` 删掉 `scale-[0.97]`/`ring-4` → 按下没有手感；
 *  4. 输入框改回 `<input>` 或删掉 Shift+Enter 分支 → 长者不会换行；
 *  5. 发送按钮回到 `disabled:opacity-50` → 空输入时按钮看不清。
 *
 * ---
 * 2026-11-13 追加「批二：版面收敛」一组（S7-批二，先红后绿）。上面九条管的是**单个组件
 * 自己的手感与排版**；批二动的是**版面归属**——顶栏该收几颗、药单该挂在右栏的哪个位置。
 * 这类改动最典型的退化是「改完页面又长回七个入口」「药单被塞回旧位置」，单测测不到、
 * 截图也只在当次有效，所以同样用源码契约钉住。四条判据见文末 describe 的用例标题。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, test } from "node:test";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

/** 去掉块注释与行注释：契约查的是**代码**，文档里引用「原来 gap-3」不算回退。 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const MEDS_SRC = stripComments(read("components/TodayMedications.tsx"));
const SHELL_SRC = read("components/TaskShell.tsx");
const CARD_SRC = read("pages/MedicationCard.tsx");
const TALK_SRC = stripComments(read("components/HoldToTalk.tsx"));
const TEXT_SRC = stripComments(read("components/TextFallback.tsx"));
const BAR_SRC = stripComments(read("components/StatusBar.tsx"));
const CSS_SRC = read("index.css").replace(/\/\*[\s\S]*?\*\//g, "");
const APP_SRC = stripComments(read("App.tsx"));
const LAYER_SRC = stripComments(read("avatar/AvatarLayer.tsx"));

/**
 * 2026-11-11 多端适配后，尺寸不再只写在组件里，而是「组件引用 `--pad-*` 令牌 +
 * `index.css` 的 `:root` 给默认值（D 档与改前逐像素一致）+ `@media` 覆盖」。
 * 所以契约要同时钉两件事：组件**确实用了**那个令牌、`:root` 的默认值**仍是**当年的像素。
 * 只钉前者，有人把默认值改成 520px 也不会有用例红；只钉后者，组件里写死一个值也能过。
 */
function tokenDefaultPx(name: string): number | null {
  const match = new RegExp(`--${name}:\\s*([0-9.]+)(rem|px);`).exec(CSS_SRC);
  if (!match) return null;
  const value = Number(match[1]);
  return match[2] === "rem" ? value * 16 : value;
}

/** 取源码里第一个匹配的像素宽度；没有就返回 null（用例自己给可读失败）。 */
function firstPxWidth(source: string): number | null {
  const match = /w-\[(\d+)px\]/.exec(source);
  return match ? Number(match[1]) : null;
}

test("数字人层必须在舞台聚光之上（否则淡紫渐变会变成脸前一层白雾）", () => {
  assert.match(
    LAYER_SRC,
    /className="pointer-events-none fixed z-10 /,
    "数字人层回到了 z-0：`.pad-stage` 是相对定位的普通文档元素，按绘制顺序会盖在 fixed z-0 的画布上，淡紫聚光又变成「白雾」",
  );
  assert.match(
    CSS_SRC,
    /\.pad-stage\s*\{[\s\S]*?radial-gradient\(ellipse 62% 58% at 50% 44%/,
    "舞台聚光被删了：数字人回到纯白底上，白衣服没有轮廓，看起来仍像浮在雾里",
  );
  // 聚光只能当背景：画布必须高于它，任务抽屉与浮层又必须高于画布
  const layerZ = /fixed z-(\d+)/.exec(LAYER_SRC)?.[1];
  assert.equal(layerZ, "10", `数字人层 z=${layerZ}，必须高于舞台（auto）且低于抽屉 z-20`);
});

test("底栏与中部同轴：按住说话对数字人、打字框对字幕卡", () => {
  assert.match(
    CSS_SRC,
    /\.pad-entry-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s*var\(--pad-subtitle-w\)/,
    "底栏没有按「舞台列 + 字幕栏」分栏：按住说话会偏离数字人垂线",
  );
  assert.match(
    CSS_SRC,
    /\.pad-entry-text\s*\{[^}]*padding-right:\s*var\(--pad-gutter-x\)/,
    "打字栏右槽与字幕卡不一致：两者右边缘对不齐",
  );
});

test("今日用药卡片：跟着字幕栏走，不再浮在数字人身上", () => {
  assert.match(
    MEDS_SRC,
    /--pad-med-w/,
    "宽度没走 --pad-med-w 令牌：多端适配后宽度必须由令牌给，组件里别再写死",
  );
  assert.ok(
    firstPxWidth(MEDS_SRC) === null,
    `卡片又写死了像素宽：右栏宽度由 --pad-subtitle-w 决定，写死会在窄档溢出`,
  );
  assert.doesNotMatch(MEDS_SRC, /\babsolute\b|\bfixed\b/, "卡片又脱离文档流：会重新压住数字人");
  assert.doesNotMatch(MEDS_SRC, /\bbottom-\d/, "卡片又贴底摆放：原来的 bottom-6 正压在左腿上");
  assert.match(MEDS_SRC, /shrink-0/, "卡片会被字幕挤扁：药单要定高，压缩的是对话区");

  // 位置在 App 里：药单必须落在字幕栏（aria-live 区）内，且舞台列是空的
  const column = APP_SRC.slice(APP_SRC.indexOf('aria-live="polite"'), APP_SRC.indexOf("</main>"));
  // 批二起右栏只放「一行摘要」，完整药单走抽屉；判据仍是「归属」：摘要必须在字幕栏里
  assert.ok(
    column.includes("<MedicationSummary"),
    "药单摘要不在字幕栏里：又被挪回中部左栏了",
  );
  assert.match(
    APP_SRC,
    /<div className="pad-stage[^"]*" \/>/,
    "舞台列里塞了内容：数字人是 fixed 层，列里放东西会盖在它身上",
  );
});

test("今日用药卡片：一行放下「时刻 + 药名」，剂量另起一行（窄卡片里不挤字号）", () => {
  assert.match(MEDS_SRC, /PANEL_ROWS/, "折叠行数必须来自常量（4 行以内），否则面板会向下长");
  assert.match(MEDS_SRC, /tabular-nums/, "时刻要用等宽数字，列才对得齐");
  assert.match(MEDS_SRC, /--pad-med-text/, "药名没走 --pad-med-text：P 档要单独给字号");
  const medText = tokenDefaultPx("pad-med-text");
  assert.equal(medText, 26, `药名默认字号 ${medText}px 不是 26px：长者端要能看清，不许缩成小字`);
});

test("用药面板：正文区改成「一屏放下」，只有一处滚动", () => {
  assert.match(SHELL_SRC, /bodyFit\?: boolean/, "TaskShell 缺 bodyFit 开关，无法关掉内层滚动");
  assert.match(
    SHELL_SRC,
    /bodyFit \? "overflow-hidden" : "overflow-y-auto"/,
    "正文区的滚动策略不是由 bodyFit 决定：要么全滚（其它任务）要么不滚（用药）",
  );
  assert.ok(CARD_SRC.includes("bodyFit"), "用药卡片没传 bodyFit：面板里还会出现第二条滚动条");

  // 三列 Info 是「已服用/这次不吃」被挤出可视区的主要原因
  assert.doesNotMatch(CARD_SRC, /grid-cols-3/, "剂量/计划/方式又变回三列：正文区会重新超高");
  assert.match(CARD_SRC, /剂量/, "单行写法里要保留「剂量」标签，不能只剩数值");
});

test("按住说话：按下有缩放 + 外环，且动画 ≤200ms", () => {
  assert.match(TALK_SRC, /scale-\[0\.97\]/, "按下没有缩放反馈：长者会以为没按上");
  assert.match(TALK_SRC, /ring-4/, "按下没有外环，大字场景里看不清");
  assert.match(TALK_SRC, /duration-150/, "过渡时长不是 150ms");
  assert.doesNotMatch(TALK_SRC, /duration-(2|3|4|5)\d\d/, "过渡超过 200ms，会被感知成卡顿");
});

test("按住说话：出结果前写「正在听您说…」，不可用时给可读替代而不是灰色半透明", () => {
  assert.match(TALK_SRC, /正在听您说…/, "松开到结果之间没有可读状态，仍只写「思考中」");
  assert.match(TALK_SRC, /暂时听不了，按这里打字/, "麦克风起不来时没有指路文案");
  assert.doesNotMatch(TALK_SRC, /disabled:opacity-50/, "禁用态又用半透明：大字幕下会被当成按钮坏了");
  assert.match(TALK_SRC, /data-state=\{state\}/, "状态没有暴露成可断言的锚点");
  assert.match(TALK_SRC, /onAskForText/, "没有把长者引到打字入口的回调");
});

test("打字输入：textarea + Enter 发送 + Shift+Enter 换行 + 输入法组合不算发送", () => {
  assert.match(TEXT_SRC, /<textarea/, "输入框不是 textarea，Shift+Enter 没地方换行");
  assert.match(TEXT_SRC, /event\.key === "Enter" && !event\.shiftKey/, "缺 Enter 发送 / Shift+Enter 换行分支");
  assert.match(TEXT_SRC, /isComposing/, "中文输入法选字时按 Enter 会把半句话发出去");
  assert.match(TEXT_SRC, /setDraft\(""\)/, "发送成功后没有清空");
  assert.match(TEXT_SRC, /fieldRef\.current\?\.focus\(\)/, "发送后没有把焦点还给输入框");
});

test("打字输入：空输入时发送按钮「看得见地」不可用（虚线空态，不是半透明）", () => {
  assert.match(TEXT_SRC, /data-sendable=/, "发送按钮没有可断言的可用态");
  assert.match(
    TEXT_SRC,
    /border-2 border-dashed border-line bg-surface text-ink-subtle/,
    "空输入态没有换成虚线描边：与可用态只差一个 disabled 属性等于看不见",
  );
  assert.doesNotMatch(TEXT_SRC, /disabled:opacity-50/, "又回到半透明空态");
  assert.match(TEXT_SRC, /min-h-14/, "发送按钮不到 56px，不满足触控下限");

  // 字号下限是 20px（长者端正文口径）；2026-11-11 底栏压高时从 28 收到 26，
  // 两行仍装得下。钉「≥20」而不是钉某个具体值：再压就破线，回升不该判红。
  const inputPx = /text-\[(\d+)px\]/.exec(TEXT_SRC);
  assert.ok(inputPx, "输入字号没写成显式像素：窄档覆盖后无法核对下限");
  assert.ok(Number(inputPx[1]) >= 20, `输入字号 ${inputPx[1]}px 破了长者端 20px 正文下限`);
});

test("打字输入：输入框定高 + 错误行常驻占位（报错瞬间底栏不许长高）", () => {
  assert.match(
    TEXT_SRC,
    /className="h-20 /,
    "输入框没定高：rows=2 会按字号自己撑，整条底栏被顶高、数字人层跟着跳",
  );
  assert.match(
    TEXT_SRC,
    /role="status" className="[^"]*\bh-6\b/,
    "错误行没有常驻高度：报错那一刻底栏长高一行，3D 舞台会整体位移",
  );
  assert.match(TEXT_SRC, /\{problem \?\? ""\}/, "错误行不是常驻渲染：不报错时不占位等于没留位");
});

test("状态胶囊：三枚 gap-4、与顶栏按钮同高同字号（≥20px）", () => {
  assert.match(BAR_SRC, /--pad-status-gap/, "三枚胶囊间距没走 --pad-status-gap");
  assert.equal(tokenDefaultPx("pad-status-gap"), 16, "胶囊间距默认不是 16px（= gap-4），三枚会挤成一团");
  assert.match(BAR_SRC, /--pad-status-h/, "胶囊高度没走 --pad-status-h");
  assert.equal(tokenDefaultPx("pad-status-h"), 56, "胶囊默认高度不是 56px（= min-h-14），与顶栏按钮不在同一基线");
  assert.match(BAR_SRC, /--pad-status-text/, "胶囊字号没走 --pad-status-text");
  assert.equal(tokenDefaultPx("pad-status-text"), 24, "胶囊默认字号不是 24px（= text-2xl），比顶栏按钮小一档");
  assert.doesNotMatch(BAR_SRC, /gap-3/, "旧的 gap-3 还在，三枚仍会挤在一起");
});

/* ------------------------------------------------------------------ *
 * 批二：版面收敛（顶栏收进设置弹层 / 右栏换成用药摘要 + 抽屉）
 *
 * 判据都落在**归属**上，不落在类名上：按钮长什么样随档位变，但「这颗按钮属于顶栏
 * 还是属于设置弹层」「药单由谁渲染」是结构决定，改版面时最容易被顺手改回去。
 * 每条失败信息写清「期望什么 / 现在是什么」，红的时候不用回头读用例。
 * ------------------------------------------------------------------ */

/** 顶栏 `<details>`「设置」弹层那一段源码（含标签本身）；找不到返回 null。 */
function detailsBlock(source: string): string | null {
  const start = source.indexOf("<details");
  if (start < 0) return null;
  const end = source.indexOf("</details>", start);
  if (end < 0) return null;
  return source.slice(start, end + "</details>".length);
}

/**
 * `<header>` 里、设置弹层**之外**的那一段——「顶栏收敛」只在这里核对。
 * 弹层内部本来就该有这些入口，把它们算进来等于自证。
 */
function headerOutsideSettings(source: string): string | null {
  const headerStart = source.indexOf("<header");
  const headerEnd = source.indexOf("</header>");
  if (headerStart < 0 || headerEnd < 0) return null;
  const header = source.slice(headerStart, headerEnd);
  const block = detailsBlock(header);
  return block === null ? header : header.replace(block, "");
}

/** 取 `<StandbyPanel … />` 这一次调用的源码；找不到返回 null。 */
function standbyPanelCall(source: string): string | null {
  const start = source.indexOf("<StandbyPanel");
  if (start < 0) return null;
  const end = source.indexOf("/>", start);
  if (end < 0) return null;
  return source.slice(start, end + 2);
}

describe("批二版面：顶栏收敛进设置弹层", () => {
  it("设置弹层里能同时找到唤醒词 / 使用引导 / 声纹 / 隐私四项入口", () => {
    const details = detailsBlock(APP_SRC);
    assert.notEqual(
      details,
      null,
      "期望：顶栏有一个 `<details>`「设置」弹层兜住次级入口。现在：App.tsx 里找不到 <details>，次级入口只能挤在顶栏上",
    );

    // 每项单独断言，红了直接看出缺的是哪一项，而不是笼统的「四项不全」。
    const entries: readonly [RegExp, string, string][] = [
      [/唤醒词/, "唤醒词设置", "长者叫不醒小伴时，这里是他唯一能自己改的词"],
      [/使用引导/, "使用引导（重看引导）", "FR-UI-06 要求可重看，收起来不能收没了"],
      [/声纹|enrollEntryLabel/, "声纹录入 / 声纹设置", "身份靠声纹，入口不能只在宽屏顶栏上出现"],
      [/隐私|政策|条款/, "隐私政策 / 服务条款", "长者端必须能自己看到隐私口径（不写数据的承诺）"],
    ];
    for (const [pattern, label, why] of entries) {
      assert.match(
        details!,
        pattern,
        `期望：设置弹层里有「${label}」入口。现在：弹层里搜不到——${why}`,
      );
    }
  });

  it("顶栏（设置弹层之外）不再有独立的声纹按钮", () => {
    const bar = headerOutsideSettings(APP_SRC);
    assert.notEqual(bar, null, "期望：App.tsx 有 <header>。现在：连顶栏都找不到，用例无法核对");

    assert.equal(
      bar!.includes("声纹"),
      false,
      "期望：声纹入口只出现在设置弹层里。现在：顶栏仍有独立的「声纹」按钮，七颗按钮挤在同一行（≤1439px 会换行、3D 只剩 256px）",
    );
    assert.equal(
      bar!.includes("enrollEntryLabel"),
      false,
      "期望：声纹入口文案只在弹层里用。现在：顶栏还在渲染 enrollEntryLabel（「录入声纹 / 声纹设置」）",
    );
  });

  it("顶栏（设置弹层之外）不再并列「使用引导」与「唤醒词」", () => {
    const bar = headerOutsideSettings(APP_SRC);
    assert.notEqual(bar, null, "期望：App.tsx 有 <header>。现在：连顶栏都找不到");
    for (const label of ["使用引导", "唤醒词"]) {
      assert.equal(
        bar!.includes(label),
        false,
        `期望：「${label}」只在设置弹层里。现在：顶栏仍挂着一颗独立按钮，与弹层里那颗重复，占了首屏宽度`,
      );
    }
  });
});

describe("批二版面：右栏药单改摘要 + 抽屉", () => {
  it("右栏用 MedicationSummary 渲染用药摘要", () => {
    assert.match(
      APP_SRC,
      /from\s+["']\.\/components\/MedicationSummary["']/,
      "期望：App.tsx 从 ./components/MedicationSummary 引入摘要组件。现在：没有这条 import，右栏还是那张占满高度的旧药单",
    );
    assert.match(
      APP_SRC,
      /<MedicationSummary\b/,
      "期望：右栏渲染 <MedicationSummary … />。现在：只有 import、没有使用（或用了别的组件名）",
    );
  });

  it("右栏不再直接渲染 <TodayMedications />", () => {
    assert.equal(
      APP_SRC.includes("<TodayMedications />"),
      false,
      "期望：药单正文只出现在抽屉里。现在：右栏仍在直接渲染 <TodayMedications />，两条链路同时存在等于没换",
    );
  });

  it("引入并挂载 MedicationDrawer（点摘要打开完整药单）", () => {
    assert.match(
      APP_SRC,
      /from\s+["'][^"']*MedicationDrawer["']/,
      "期望：App.tsx 引入 MedicationDrawer。现在：没有这条 import，摘要点开后无处可去",
    );
    assert.match(
      APP_SRC,
      /<MedicationDrawer\b/,
      "期望：App.tsx 里挂载 <MedicationDrawer … />。现在：只引入了组件却没渲染，抽屉永远不会出现",
    );
  });

  it("StandbyPanel 收到 onOpenMedications（首屏引导能直达药单）", () => {
    const call = standbyPanelCall(APP_SRC);
    assert.notEqual(
      call,
      null,
      "期望：App.tsx 调用 <StandbyPanel … />。现在：找不到这次调用",
    );
    assert.match(
      call!,
      /onOpenMedications=/,
      "期望：<StandbyPanel … onOpenMedications={…} />。现在：没传这个回调——空字幕页无法把长者引到药单，只能自己找顶栏",
    );
  });
});

/** 取 CSS 里查询串包含 `query` 的所有 `@media` 块正文（同一查询在文件里可能出现多次）。 */
function mediaBodies(query: string): string[] {
  const bodies: string[] = [];
  const opener = /@media[^{]*\{/g;
  let match: RegExpExecArray | null;
  while ((match = opener.exec(CSS_SRC)) !== null) {
    if (!match[0].includes(query)) continue;
    let depth = 1;
    let index = match.index + match[0].length;
    const start = index;
    while (index < CSS_SRC.length && depth > 0) {
      const char = CSS_SRC[index];
      if (char === "{") depth += 1;
      else if (char === "}") depth -= 1;
      index += 1;
    }
    bodies.push(CSS_SRC.slice(start, index - 1));
    opener.lastIndex = index;
  }
  return bodies;
}

const T_BODIES = (): string[] => mediaBodies("max-width: 1439px");
const P_BODIES = (): string[] => mediaBodies("max-width: 1023px");

describe("批三版式：T/P 档摘要、字幕卡、发送按钮、状态条", () => {
  it(".pad-med-summary：T 档 20px 两行截断、P 档 18px，基线不留规则", () => {
    const t = T_BODIES().join("\n");
    const p = P_BODIES().join("\n");
    assert.equal(
      t.includes(".pad-med-summary"),
      true,
      "期望：T 档（≤1439 或矮屏）有一条 .pad-med-summary 规则。现在：没有，窄栏里摘要按组件默认字号把整张卡铺满",
    );
    assert.equal(
      p.includes(".pad-med-summary"),
      true,
      "期望：P 档也收一档字号。现在：P 档与 T 档同字号，1024 以下更挤",
    );
    assert.match(
      t,
      /\.pad-med-summary\s*\{[^}]*font-size:\s*1\.25rem/,
      "期望：T 档 1.25rem（20px = 正文下限）。现在：字号没了或不是 20px",
    );
    assert.match(
      t,
      /\.pad-med-summary\s*\{[^}]*display:\s*-webkit-box/,
      "期望：摘要用 -webkit-line-clamp 两行截断。现在：没有 -webkit-box，摘要长起来会顶掉字幕",
    );
    assert.match(
      t,
      /\.pad-med-summary\s*\{[^}]*-webkit-line-clamp:\s*2/,
      "期望：只留两行。现在：行数没限住，三条以上用药就把右栏撑开",
    );
    assert.match(
      p,
      /\.pad-med-summary\s*\{[^}]*font-size:\s*1\.125rem/,
      "期望：P 档 1.125rem（18px）。现在：没这档覆盖",
    );
    const total = (CSS_SRC.match(/\.pad-med-summary/g) ?? []).length;
    const inMedia = (t.match(/\.pad-med-summary/g) ?? []).length + (p.match(/\.pad-med-summary/g) ?? []).length;
    assert.equal(
      total,
      inMedia,
      "期望：.pad-med-summary 只在 T/P 档出现。现在：基线也有规则 → D 档 1920 上的摘要跟着变了（基线必须不动）",
    );
    assert.match(
      read("components/MedicationSummary.tsx"),
      /pad-med-summary/,
      "期望：MedicationSummary 真的把 .pad-med-summary 挂在文本上。现在：条 CSS 写在样式表里，组件没这个类，窄档等于没改",
    );
  });

  it("--pad-subtitle-min：T 240px / P 150px，字幕卡按它给 min-height", () => {
    assert.equal(
      /:root\s*\{[^}]*--pad-subtitle-min:\s*0px/.test(CSS_SRC),
      true,
      "期望：基线 --pad-subtitle-min: 0px（D 档不设下限）。现在：基线缺这个令牌或默认值不是 0，1920 上字幕卡会凭空长高",
    );
    assert.equal(
      T_BODIES().join("\n").includes("--pad-subtitle-min: 240px"),
      true,
      "期望：T 档 240px。现在：T 档没抬下限，字幕顶部那句话仍可能被裁",
    );
    assert.equal(
      P_BODIES().join("\n").includes("--pad-subtitle-min: 150px"),
      true,
      "期望：P 档 150px。现在：P 档没给下限",
    );
    const tp = [...T_BODIES(), ...P_BODIES()].join("\n");
    assert.match(
      tp,
      /\.pad-subtitle-card\s*\{[^}]*min-height:\s*var\(--pad-subtitle-min\)/,
      "期望：字幕卡 .pad-subtitle-card 的 min-height 引用该令牌。现在：令牌发了没人用，字幕顶部仍被压掉",
    );
    assert.match(
      APP_SRC,
      /pad-subtitle-card/,
      "期望：App.tsx 给右栏字幕卡挂 .pad-subtitle-card。现在：CSS 里写了选择器，DOM 上没有这个类，规则是死的",
    );
    assert.equal(
      CSS_SRC.includes("overflow-y: auto"),
      true,
      "期望：.pad-transcript 自己声明 overflow-y: auto。现在：只剩组件的 Tailwind 类，样式表里查不到，改一处就漏",
    );
  });

  it("T/P 档 .pad-send-btn 不换行、不窄于 5.5rem", () => {
    const tp = [...T_BODIES(), ...P_BODIES()].join("\n");
    assert.match(
      tp,
      /\.pad-send-btn\s*\{[^}]*min-width:\s*5\.5rem/,
      "期望：窄栏发送按钮 min-width 5.5rem。现在：按钮被压到「发送中…」换行/变窄",
    );
    assert.match(
      tp,
      /\.pad-send-btn\s*\{[^}]*white-space:\s*nowrap/,
      "期望：white-space: nowrap。现在：「发送中…」折成两行，按钮高度跳变",
    );
    const total = (CSS_SRC.match(/\.pad-send-btn/g) ?? []).length;
    const inMedia = (tp.match(/\.pad-send-btn/g) ?? []).length;
    assert.equal(
      total,
      inMedia,
      "期望：.pad-send-btn 只在 T/P 档覆盖。现在：基线也有这条规则 → D 档按钮跟着变",
    );
    assert.match(
      TEXT_SRC,
      /pad-send-btn/,
      "期望：打字输入的发送按钮挂了 .pad-send-btn。现在：选择器在样式表里空转",
    );
  });

  it("P 档状态条只留第一枚胶囊（连接），按结构隐藏其余", () => {
    const p = P_BODIES().join("\n");
    const rule = /\[role="status"\]\s*>\s*\[role="group"\]:nth-child\(n\s*\+\s*2\)\s*\{[^}]*display:\s*none/.exec(p);
    assert.notEqual(
      rule,
      null,
      "期望：P 档用 [role=status] > [role=group]:nth-child(n + 2) { display: none } 隐掉后两枚。现在：三枚胶囊竖叠，吃掉整条 168px 字幕带",
    );
    assert.equal(
      /身份|播放|已连接/.test(rule![0]),
      false,
      "期望：只认结构、不认文案（文案会随状态变）。现在：选择器里写了胶囊文案，状态一变隐藏就失效",
    );
  });
});
