/**
 * 兜底形象 = 小满（2026-11-17 产品负责人：「数字人兜底形象就设置成小满」）。
 *
 * 兜底曾有三处分叉：没分配人设时 3D 先挑 `catalog.json` 的第一个形象（丽莎），
 * 3D 失败时静态层只画剪影，登录页预热又按目录挑。长者看到的是「怎么换了个人」。
 * 这里用源码契约把三处钉成同一个答案：小满的 VRM（`vrm1_sample.vrm`）+ 小满的静态图
 * （`static-portrait.png`），预热也必须是同一个 URL，否则登录后命不中解析缓存。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(`../src/${relative}`, import.meta.url)), "utf8");

const flat = (source: string): string => source.replace(/\s+/g, " ");

const STAGE_SRC = flat(read("pad/avatar/AvatarStage.tsx"));
const PRELOAD_SRC = flat(read("pad/avatar/preloadAvatar.ts"));
const ELDERS_SRC = read("console/pages/EldersPage.tsx");

test("3D 兜底排在目录默认之前：没分配人设时先看到小满", () => {
  assert.ok(
    STAGE_SRC.includes(
      "dedupeUrls([config.avatar_url, DEFAULT_AVATAR_URL, catalogDefault])",
    ),
    "候选链必须是 服务端配置 → 小满 → 目录默认；目录默认（丽莎）不能插在小满前面",
  );
});

test("静态兜底用的是小满的一帧，不再退回剪影", () => {
  assert.ok(
    STAGE_SRC.includes('const DEFAULT_FALLBACK_IMAGE_URL = "/avatars/static-portrait.png";'),
    "静态兜底必须是小满的 static-portrait.png",
  );
  assert.ok(
    STAGE_SRC.includes(
      "const fallbackSrc = config.fallback_image_url ?? DEFAULT_FALLBACK_IMAGE_URL;",
    ),
    "avatar-config 没配 fallback_image_url 时要落到小满静态图",
  );
  assert.ok(
    STAGE_SRC.includes('setStage({ kind: "fallback", src: DEFAULT_FALLBACK_IMAGE_URL })'),
    "连配置都取不到时也落到小满静态图，而不是剪影",
  );
});

test("登录页预热与兜底是同一个文件：不再按目录挑形象", () => {
  assert.ok(
    !PRELOAD_SRC.includes("pickAvatarUrl") && !PRELOAD_SRC.includes("loadAvatarCatalog"),
    "预热必须固定热小满（DEFAULT_AVATAR_PRELOAD_URL），否则登录后兜底命不中缓存",
  );
  assert.ok(PRELOAD_SRC.includes("const url = explicitUrl ?? DEFAULT_AVATAR_PRELOAD_URL;"));
});

test("照护台文案说清兜底是谁", () => {
  assert.ok(ELDERS_SRC.includes('const NO_PERSONA_LABEL = "未指定（小满兜底）";'));
  assert.ok(ELDERS_SRC.includes("小满兜底形象"));
});
