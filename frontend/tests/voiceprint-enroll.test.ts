/**
 * 声纹录入弹框「会说人话」的守卫（task-72）。
 *
 * 背景（产品负责人实测原文）：「声纹录入的弹出框没有提示语」——老人打开只看到录音按钮，
 * 不知道为什么录、录几段、每段说什么、失败了怎么办、录完还剩几段。
 * 修复是把六类文案铺进去；本用例把它们逐个钉住，改回去就红。
 *
 * 为什么读源码：Node 的 `--test` 只擦类型、不认 TSX，仓库不许引 jsdom/vitest（AGENTS §4-12）。
 * 这里能钉住的是「文案与尺寸口径会不会被下一次改动悄悄删掉」，而不是渲染结果。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SRC = readFileSync(
  fileURLToPath(new URL("../src/pad/pages/VoiceprintEnroll.tsx", import.meta.url)),
  "utf8",
);

/** 去掉块注释与行注释：契约查的是**代码**，注释里提到旧尺寸不算回退。 */
const CODE = SRC.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

/** 取某个标识符之后的一段源码（拿不到就返回空串），用于只在单个函数体内做断言。 */
const after = (source: string, needle: string, length = 2000): string => {
  const at = source.indexOf(needle);
  return at < 0 ? "" : source.slice(at, at + length);
};

test("① 顶部有一句「为什么录」", () => {
  assert.ok(
    CODE.includes("录 3 段声音，以后您一开口我就认得您"),
    "缺「为什么录」这句：老人打开弹框只看到按钮，不知道自己在做什么",
  );
  assert.ok(CODE.includes('data-testid="enroll-purpose"'), "这句话要有可测锚点，否则下次重构会被当成装饰删掉");
});

test("② 流程提示：第 N 段 / 共 3 段，且每段要念的话在屏幕上", () => {
  assert.ok(CODE.includes("现在是第 {segment.index} 段"), "没写「现在是第几段」");
  assert.ok(CODE.includes("共 {SEGMENTS.length} 段"), "没写「一共几段」");
  assert.ok(CODE.includes("{segment.guide}"), "没把这一段的引导语显示出来");
  assert.ok(CODE.includes("{segment.demo}"), "没把这一段要念的话显示出来");
  assert.ok(CODE.includes("{segment.hint}"), "没说这一段该怎么念（快慢/内容提示）");

  // 三段引导语是产品负责人 2026-11-16 选定的话术：改文案要连带改这里，避免悄悄漂走
  for (const guide of ["请跟我说：", "请跟我数：", "请随便说一句您平时常说的话"]) {
    assert.ok(CODE.includes(guide), `缺引导语「${guide}」`);
  }
  // 本轮口径是「只加强屏幕文字」：不自动播放语音，避免麦克风把引导声录进去
  assert.equal(
    /speechSynthesis|new Audio\(/.test(CODE),
    false,
    "声纹录入页不应自动播放引导语音（本轮只做屏幕文字）",
  );
});

test("③ 状态行覆盖录音中（含已录秒数）/ 识别中 / 已保存 / 失败分档", () => {
  assert.ok(CODE.includes("正在录音，已经录了"), "录音中没有时长反馈：长者不知道还要说多久");
  assert.ok(CODE.includes("正在识别这一段的声纹"), "上传/识别阶段没有提示");
  assert.ok(CODE.includes("已保存，还剩"), "存好一段后没有回执，也不知道还剩几段");

  // 失败分三档说人话：声音太小 / 环境太吵 / 服务不可用
  assert.ok(CODE.includes("声音有点小"), "缺「声音太小」这一档");
  assert.ok(CODE.includes("周围有点吵"), "缺「环境太吵」这一档");
  assert.ok(CODE.includes("声纹服务暂时不可用"), "缺「服务不可用」这一档");

  // 声纹比对分数对长者没有意义，也不该出现
  assert.doesNotMatch(CODE, /相似度|打分|得分/, "不许把声纹分数透给长者");
});

test("③+ 录音中的大号秒数仍在（配音/节奏参照）", () => {
  assert.ok(CODE.includes("tabular-nums"), "秒数要用等宽数字，否则每秒跳动会抖宽度");
});

test("④ 有一句隐私说明：原文不留", () => {
  assert.ok(
    CODE.includes("录音只在你家里的设备上转成声纹，原文不留"),
    "缺隐私说明：长者会担心「我的声音被传到哪儿去了」",
  );
});

test("⑤ 「稍后再录」常驻，并说明顶栏可再次打开", () => {
  assert.ok(CODE.includes('exitLabel="稍后再录"'), "退出按钮不叫「稍后再录」：会被读成「放弃」");
  assert.ok(CODE.includes("顶栏的「录入声纹」可以再次打开"), "没说还能从哪里进来，老人怕退出就没了");
});

test("⑥ 已录段数用大号数字（text-6xl），不是一句话", () => {
  assert.ok(CODE.includes('data-testid="recorded-count"'), "已录段数缺可测锚点");
  assert.ok(CODE.includes("text-6xl"), "已录段数不是大号数字：长者要能一眼看出还剩几段");
});

test("正文一律 ≥28px，且不再有 20/24px 的说明文字", () => {
  const sizes = [...CODE.matchAll(/text-\[(\d+)px\]/g)].map((m) => Number(m[1]));
  assert.ok(sizes.length > 0, "读不到像素字号，无法确认长者端口径");
  const tooSmall = sizes.filter((px) => px < 28).sort((a, b) => a - b);
  assert.deepEqual(tooSmall, [], `这些字号小于 28px：${tooSmall.join(", ")}`);
  assert.doesNotMatch(CODE, /\btext-xl\b/, "还有 20px 正文：长者端正文口径是 ≥28px");
  assert.doesNotMatch(CODE, /\btext-2xl\b/, "还有 24px 正文：长者端正文口径是 ≥28px");
});

test("按钮触控 ≥48×48px（主按钮一律 80px 高）", () => {
  // 只数主按钮：状态行也有 `min-h-9`（36px 的文本占位），它不是可点元素。
  //
  // 2026-11-11 加了重录出口，主按钮从 2 个变 6 个：
  //   未完成分支 —— 「开始录音/录下一段」「说完了，保存这一段」
  //   done 分支   —— 「完成」「重新录一遍」，以及二次确认里的「先留着」「清空，重新录」
  // 所以这里钉的是**每个**主按钮都够大（逐个查），而不是数个数：
  // 数个数会在下次多加一个按钮时红，但漏掉「新按钮没写 min-h-20」这种真问题。
  const buttons = [...CODE.matchAll(/<button[\s\S]*?<\/button>/g)].map((m) => m[0]);
  assert.ok(buttons.length >= 6, `主按钮数量不对（至少 6 个），实际 ${buttons.length}`);

  for (const button of buttons) {
    // TaskShell 的退出按钮在组件外部，这里只管本文件自己写的 button
    assert.ok(
      button.includes("min-h-20"),
      `这个按钮没有 80px 最小高度，低于 48px 触控下限：${button.slice(0, 80)}`,
    );
  }
  assert.ok(CODE.includes("min-h-20"), "主按钮没有 80px 最小高度，低于 48px 触控下限");
});

test("⑦ 录满三段后仍能重录：入口不得再按 active 隐掉，且重录要二次确认", () => {
  const APP = readFileSync(
    fileURLToPath(new URL("../src/pad/App.tsx", import.meta.url)),
    "utf8",
  );
  const APP_CODE = APP.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

  // 背景：2026-11-11 产品负责人问「为啥声纹录入按钮录制完声音就消失了」。
  // 原因是入口条件写着 `voiceprint.status !== "active"` —— 录满即隐藏，而前端
  // 又没有任何删除出口，长者录歪了只能找管理员清库。
  assert.equal(
    /showEnrollEntry[\s\S]{0,160}status !== "active"/.test(APP_CODE),
    false,
    "入口又被 active 门住了：录满三段按钮消失，长者没有重录出口",
  );
  assert.ok(
    APP_CODE.includes("声纹设置"),
    "active 时入口要改名「声纹设置」，否则长者以为点进去还是去录",
  );
  assert.ok(APP_CODE.includes("onReset="), "App 没接 onReset：删掉后端样本后本地状态还是 active");

  // 删除是不可逆动作，必须二次确认，不能一点就清
  assert.ok(CODE.includes("重新录一遍"), "done 态没有「重新录一遍」");
  assert.ok(CODE.includes("要重新录一遍吗？"), "没有二次确认话术：一点就把三段删了");
  assert.ok(CODE.includes("清空，重新录"), "确认后没有明确的破坏性按钮文案");
  assert.ok(CODE.includes("先留着"), "确认层没有「先留着」出口：长者只能被迫删");
});

test("⑧ 重录必须真调 DELETE（本地清空会在后端 409 duplicate_sample 死循环）", async () => {
  const MODULE = readFileSync(
    fileURLToPath(new URL("../src/pad/voiceprint.ts", import.meta.url)),
    "utf8",
  );
  assert.ok(MODULE.includes("export async function deleteVoiceprints"), "voiceprint.ts 没有删除出口");
  assert.ok(MODULE.includes('method: "DELETE"'), "删除没走 DELETE");
  assert.ok(MODULE.includes('fetch("/api/voiceprints"'), "删除端点写错（应是不带 query 的 /api/voiceprints）");
  assert.ok(CODE.includes("deleteVoiceprints("), "弹框没有调用 deleteVoiceprints");

  // 删除成功后必须把本地状态也清掉（否则下一段会算成「第 4 段」，后端只有 1/2/3）
  const resetBody = after(CODE, "async function handleReset", 1400);
  assert.ok(resetBody.includes("await deleteVoiceprints()"), "handleReset 没真调 DELETE");
  assert.ok(resetBody.includes("setRegistered([])"), "删除成功后没清空已录段");
  assert.ok(resetBody.includes('setPhaseSafe("idle")'), "删除成功后没回到可录状态");
  // 失败分支不能顺流下去：否则长者以为能重录，实际每段都被 409 挡住
  const failAt = resetBody.indexOf("if (!result.ok)");
  assert.ok(failAt >= 0, "handleReset 没有处理删除失败");
  assert.ok(
    resetBody.slice(failAt).includes("return;"),
    "删除失败后没有提前返回",
  );
  assert.ok(
    resetBody.indexOf("setRegistered([])") > failAt,
    "必须在失败分支之后才清空本地状态",
  );

  // 真函数不在这里求值：`voiceprint.ts` 顶层 import 的是 `./audio/capture`（无扩展名），
  // Node 的裸 ESM 解析器认不了无扩展名的相对导入，硬 import 会 ERR_MODULE_NOT_FOUND。
  // 所以「未登录不打后端」用源码顺序钉：会话判空必须早于 fetch。
  // 断言随结构更新（W5：鉴权收敛到 pad/http 的 deviceToken()，判空变量从 session 改为 token），
  // 并**加强**为顺序检查，不是放宽。
  const deleteBody = after(MODULE, "export async function deleteVoiceprints", 1600);
  const guardAt = deleteBody.indexOf("if (!token)");
  const fetchAt = deleteBody.indexOf("fetch(");
  assert.ok(guardAt >= 0, "没有会话就先返回，别打后端（deviceToken 判空缺失）");
  assert.ok(fetchAt > guardAt, "会话判空必须早于 fetch，否则未登录也会打后端");
  assert.ok(
    deleteBody.indexOf("if (!session)") < deleteBody.indexOf("fetch("),
    "会话判空必须在 fetch 之前：登录过期时不该再发一次匿名请求",
  );
  assert.ok(
    deleteBody.includes('code: "no_session"'),
    "未登录要回 no_session，页面才能映射成「登录已过期」",
  );
});

test("⑨ 打开一个已 active 的弹框必须直接落到 done（不许停在能点「开始录音」的 idle）", () => {
  assert.ok(
    CODE.includes('phase === "done"') || CODE.includes('"done"'),
    "读不到 done 分支",
  );
  assert.ok(
    /useState<Phase>\(\(\) =>[\s\S]{0,200}length >= SEGMENTS\.length \? "done" : "idle"/.test(CODE),
    "首帧 phase 没按已录段数初始化：active 时打开会停在 idle，点「开始录音」只会收到 duplicate_sample",
  );
  assert.ok(
    CODE.includes("useRef<Phase>(phase)"),
    "phaseRef 初值没跟随 phase：录满直接进 done 时重入判断与 UI 不同步",
  );
});

test("不新增网络请求、不新增依赖：提交仍走 voiceprint.ts 的封装", () => {
  assert.equal(/\bfetch\s*\(/.test(CODE), false, "弹框里出现了 fetch：网络出口应当只在 voiceprint.ts");
  assert.ok(CODE.includes("submitSample("), "提交必须复用 submitSample");
  assert.ok(CODE.includes("getVoiceprintStatus("), "状态回查必须复用 getVoiceprintStatus");
  assert.equal(/from\s+"[^"]*(?:axios|ky|swr|react-query)/.test(CODE), false, "不许引第三方请求库");
});
