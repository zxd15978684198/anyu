/**
 * 浮层「内容显示不全」的回归钉子（2026-11-11 用户实测：「弹窗互相遮蔽而且内容显示不全」）。
 *
 * 遮蔽（同一时刻多个浮层）由 App.tsx 的仲裁解决，归 kws；本文件钉的是**浮层自身**：
 *  1. 每个浮层都有 `max-height: min(92vh, calc(100dvh - 48px))`（矮屏不超出可视区）；
 *  2. 只有正文区滚动，**主操作按钮在滚动区之外**（footer / sticky），滚不出屏幕；
 *  3. `WakeWordSettings` 从 `absolute` 改 `fixed`，不再盖住宿主容器里的输入框；
 *  4. 数字问答不再带 `inputmode`（长者端键盘由系统决定，别在页面里强塞）。
 *
 * 为什么读源码：Node `--test` 不认 TSX、仓库不许引 jsdom（AGENTS §4-12）。
 * 能钉住的是「布局契约会不会被下一次改动悄悄回退」——类名/结构回退即红。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/pad/${relative}`, import.meta.url)), "utf8");

/** 去掉块注释与行注释：契约查代码，文档里引用旧结构不算回退。 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

/** 取出 `footer={...}` 的那个 JSX 表达式（花括号配对，字符串里的 `{` 也成对，够用）。 */
function jsxExpression(source: string, from: number): string {
  const open = source.indexOf("{", from);
  assert.notEqual(open, -1, "footer 后面没有表达式");
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open, i + 1);
    }
  }
  throw new Error("footer 表达式花括号不配对");
}

function footerBlocks(source: string): string[] {
  const blocks: string[] = [];
  let at = source.indexOf("footer={");
  while (at !== -1) {
    blocks.push(jsxExpression(source, at));
    at = source.indexOf("footer={", at + 8);
  }
  assert.ok(
    blocks.length > 0,
    "这个浮层没有 footer 区：主按钮又会被放进滚动区，矮屏上滚出屏幕",
  );
  return blocks;
}

const SHELL = stripComments(read("components/TaskShell.tsx"));

test("TaskShell：max-height 走 min(92vh, calc(100dvh - 48px)) 口径", () => {
  assert.match(SHELL, /92vh/, "外壳没有 92vh 封顶：矮屏会顶出可视区");
  assert.match(SHELL, /100dvh\s*-\s*48px/, "外壳没有 dvh - 48px：移动端地址栏收放时底部按钮会被切掉");
  assert.match(SHELL, /maxHeight/, "max-height 必须写在 style/类里，否则 min() 解析不了");
});

test("TaskShell：正文区滚动、footer 在滚动区之外（按钮永远可见）", () => {
  assert.match(
    SHELL,
    /bodyFit \? "overflow-hidden" : "overflow-y-auto"/,
    "让 bodyFit 决定正文滚动策略的表达式被改掉了：用药一屏放下的口径会失效",
  );
  assert.ok(SHELL.includes('data-testid="task-shell-body"'), "正文区缺可测锚点");
  assert.ok(SHELL.includes('data-testid="task-shell-footer"'), "页脚缺可测锚点");

  const body = SHELL.indexOf('data-testid="task-shell-body"');
  const footer = SHELL.indexOf('data-testid="task-shell-footer"');
  assert.ok(footer > body, "footer 必须在 body 之后渲染（数据结构上就在滚动区外）");
  const footerTag = SHELL.slice(Math.max(0, footer - 200), footer + 200);
  assert.match(footerTag, /shrink-0/, "footer 没被 shrink-0 钉住：正文长时会被压扁");
});

const LAYERS: Array<{ name: string; file: string; primary: string[] }> = [
  { name: "声纹录入", file: "pages/VoiceprintEnroll.tsx", primary: ["说完了，保存这一段", "开始录音"] },
  { name: "身份确认", file: "pages/IdentityConfirm.tsx", primary: ["是我", "稍后再说"] },
  { name: "用药卡片", file: "pages/MedicationCard.tsx", primary: ["已服用", "这次不吃"] },
  { name: "健康采集", file: "pages/HealthCheckup.tsx", primary: ["没听清，再问一次", "这题先跳过"] },
  { name: "SOS 结果", file: "pages/SosResult.tsx", primary: ["返回主界面"] },
];

for (const layer of LAYERS) {
  test(`${layer.name}：主按钮在 footer（滚动区之外），不靠滑屏才能点到`, () => {
    const src = stripComments(read(layer.file));
    assert.ok(
      src.includes("<TaskShell"),
      `${layer.name} 不用 TaskShell 了：max-h / 内层滚动的口径会各写各的`,
    );
    const footers = footerBlocks(src);
    for (const label of layer.primary) {
      assert.ok(
        footers.some((block) => block.includes(label)),
        `${layer.name} 的「${label}」不在 footer 里：正文一长就把按钮滚出屏幕`,
      );
    }
  });
}

test("WakeWordSettings：fixed 铺满 + 卡片封顶 + 正文滚动 + 按钮在菜单外层", () => {
  const src = stripComments(read("components/WakeWordSettings.tsx"));
  assert.ok(src.includes('role="dialog"'), "丢了 dialog 语义，读屏不知道这是弹层");
  assert.ok(src.includes("fixed inset-0"), "弹层不是 fixed：在带 transform 的容器里 absolute 会错位");
  assert.equal(
    src.includes("absolute inset-0"),
    false,
    "又回到 absolute inset-0：它会以宿主容器为参照，遮住输入框/canvas",
  );
  assert.match(src, /92vh/, "卡片没有 92vh 封顶");
  assert.match(src, /100dvh\s*-\s*48px/, "卡片没有 dvh - 48px：矮屏上底部按钮会被切掉");
  assert.ok(src.includes('data-testid="wake-word-body"'), "缺正文滚动区锚点");
  assert.ok(src.includes('data-testid="wake-word-footer"'), "缺页脚锚点");

  const body = src.indexOf('data-testid="wake-word-body"');
  const footer = src.indexOf('data-testid="wake-word-footer"');
  assert.ok(footer > body, "页脚必须在正文滚动区之后");
  const bodyToFooter = src.slice(body, footer);
  assert.equal(bodyToFooter.includes("保存"), false, "「保存」还在滚动区里：正文长时按钮被滚走");
  assert.ok(
    src.slice(footer).includes("保存"),
    "「保存」不在页脚：长者得先滚到底才看得到主按钮",
  );
});

test("健康采集：不再给输入框塞 inputmode（键盘交给系统）", () => {
  const src = stripComments(read("pages/HealthCheckup.tsx"));
  assert.equal(
    /input[Mm]ode/i.test(src),
    false,
    "又设了 inputmode：长者端键盘由系统决定，页面里强塞会挡住按钮",
  );
});

test("声纹录入：浮层挂在视口级（fixed），不回到 main 的中部高度里被压扁", () => {
  // 2026-11-17 实测：1536×703（1920 屏 125% 缩放）下，浮层原来挂在 main 的
  // `absolute inset-0` 里只拿到 429px，标题/进度/页脚把正文区压到 clientHeight 0，
  // 三段引导词一个都看不见。浮层必须相对视口定位，并且面板自己按整屏给高度。
  const APP = stripComments(
    readFileSync(fileURLToPath(new URL("../src/pad/App.tsx", import.meta.url)), "utf8"),
  );
  const at = APP.indexOf("{showEnroll && (");
  assert.ok(at > 0, "找不到声纹录入层的渲染块");
  const block = APP.slice(at, at + 900);
  assert.ok(block.includes("fixed inset-0"), "声纹录入层不是 fixed：又会以 main 为参照被压到 0 高");
  assert.equal(block.includes("absolute inset-0"), false, "声纹录入层又回到 absolute inset-0");

  const SRC = read("pages/VoiceprintEnroll.tsx");
  assert.match(SRC, /const SHELL_MAX_HEIGHT = "min\(calc\(100dvh - 32px\), 800px\)"/, "面板高度口径被改回抽屉的 600px：矮屏正文区会归零");
  assert.ok(
    (SRC.match(/maxHeight=\{SHELL_MAX_HEIGHT\}/g) ?? []).length === 2,
    "两个分支（录入中 / 已启用）都要用视口级高度，漏一个就会在另一态被截断",
  );
});

test("声纹录入：四类状态话术仍在（矮屏改造不许把提示删掉）", () => {
  const src = read("pages/VoiceprintEnroll.tsx");
  assert.ok(src.includes("正在录音，已经录了"), "录音中话术没了");
  assert.ok(src.includes("正在识别这一段的声纹"), "识别中话术没了");
  assert.ok(src.includes("段已保存，还剩"), "已保存话术没了");
  assert.ok(src.includes("没听清：可能是声音太小，或周围有点吵"), "失败原因话术没了");
});
