/**
 * 照护台「改档」弹窗的 D-01 警告文案源码契约（批B-D-01 / task-46）。
 *
 * 为什么守源码而不是渲染：`frontend/tests` 里 Node `--test` 只做类型擦除、不认 TSX，
 * 仓库又不许引 jsdom/vitest（AGENTS §4-12）。这里要守的是**三处会悄悄回退的地方**：
 *
 * 1. 文案 —— 后端在 `PATCH role=admin` 时会清掉该员工全部 `elder_staff_assignments`，
 *    弹窗若不说，管理员会以为只是改了个头衔，回到列表才发现负责长者没了。
 * 2. 条件 —— 只在**目标角色是 admin 且原本不是 admin**时提示；写宽了护工/医生互改
 *    也会弹「将清除其负责长者」，变成假警告，管理员会学会无视它。
 * 3. 位置 —— 提示必须在 `StaffEditDialog` 的 `FormGrid` 里、保存按钮之前，
 *    不能挂到列表页或新建弹窗上（那里根本没有「改档」这回事）。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SRC = readFileSync(
  fileURLToPath(new URL("../src/console/pages/StaffPage.tsx", import.meta.url)),
  "utf8",
);

/** 去掉注释：契约要查的是**代码**，不是注释里写的文案。 */
const stripComments = (source: string): string =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const CODE = stripComments(SRC);

test("改档弹窗写明「改为管理员将清除其负责长者，之后需重新分配」", () => {
  assert.ok(
    SRC.includes("改为管理员将清除其负责长者，之后需重新分配"),
    "文案是验收口径，必须逐字可搜到",
  );
});

test("只在目标角色是 admin 且原角色不是 admin 时提示", () => {
  assert.match(
    CODE,
    /role === "admin"\s*&&\s*staff\.role !== "admin"/,
    "两个条件缺一不可：只判 role 会让「本来就是管理员」的改档也弹警告",
  );
});

test("提示放在改档弹窗的表单里，用既有 note-banner 样式", () => {
  const dialogStart = CODE.indexOf("function StaffEditDialog");
  assert.ok(dialogStart > 0, "StaffEditDialog 必须仍然存在");
  const dialogBody = CODE.slice(dialogStart);

  const gridAt = dialogBody.indexOf("<FormGrid>");
  const warningAt = dialogBody.indexOf("改为管理员将清除其负责长者");
  const readonlyAt = dialogBody.indexOf("姓名 / 手机号 / 负责长者");
  assert.ok(gridAt > 0 && warningAt > gridAt, "提示必须在表单网格内（不是页头或页脚）");
  assert.ok(
    readonlyAt > warningAt,
    "提示要排在「姓名/手机号/负责长者」只读块之前，紧贴角色选择器",
  );
  // 断言随结构更新（W4）：手写 <p className="note-banner"> 已收敛到 <Notice> 原语，
  // 样式真源在 shared/ui/primitives.css 的 .ui-notice（与原 note-banner 同色同形）。
  // 判据改为「用了 Notice 原语」，并额外钉住它仍在表单网格内、位置不变，不是放宽。
  assert.match(dialogBody, /<Notice/, "提示应复用 Notice 原语（样式真源在 shared/ui）");
  assert.match(dialogBody, /role="status"/, "读屏软件要能念出来（不是纯装饰）");
});

test("提示不许挂到新建弹窗（那里没有「改档」语义）", () => {
  const createStart = CODE.indexOf("function StaffCreateDialog");
  const editStart = CODE.indexOf("function StaffEditDialog");
  assert.ok(createStart > 0 && editStart > createStart, "两个弹窗的先后顺序变了，请同步本用例");
  assert.ok(
    !CODE.slice(createStart, editStart).includes("改为管理员将清除其负责长者"),
    "新建管理员本来就不分配长者，弹这句只会制造噪音",
  );
});
