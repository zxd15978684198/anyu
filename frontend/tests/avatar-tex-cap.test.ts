/**
 * 贴图降采样：把 VRM 的贴图在**上传 GPU 之前**裁到 1024 以内。
 *
 * 为什么要有这条用例：2026-11-12 C 案实测，数字人首帧 2347ms 里有 1673ms 花在
 * `renderer.render` 内的纹理上传——这个 VRM 带 19 张 PNG、合计 22.3MP（最大 2048×2048），
 * 而长者端屏幕上数字人最大 582px 见方，2048 是 3.5 倍过采样。降到 1024 后总上传像素
 * 只剩 1/4，首帧降到 877ms。
 *
 * 这里锁两件东西，都是「改坏了会静默变慢、但不是报错」的那种：
 *   1. 上限值本身（512 以下会糊，2048 等于没降）；
 *   2. 降采样必须发生在 `rotateVRM0` 之前——`rotateVRM0` 之后场景已经转过，虽然对
 *      贴图没影响，但两段顺序能证明我们改的是「刚解析完、还没上屏」的原始场景。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (name: string): string =>
  readFileSync(new URL(`../src/pad/avatar/${name}`, import.meta.url), "utf8");

const LOAD_AVATAR_SRC = read("loadAvatar.ts");

test("贴图上限是 1024（不是 512 也不是 2048）", () => {
  const match = /export const AVATAR_TEXTURE_MAX_EDGE = (\d+);/.exec(LOAD_AVATAR_SRC);
  assert.ok(match, "找不到 AVATAR_TEXTURE_MAX_EDGE 的导出");
  const value = Number(match[1]);
  assert.ok(
    value >= 512 && value <= 1024,
    `贴图上限 ${value}px 不在合理区间：小于 512 长者会觉得糊，大于 1024 等于没砍上传量（实测 1024 时首帧 877ms，2048 时 2347ms）`,
  );
  assert.equal(value, 1024, "改上限要同时改这条用例，并重跑 .tmp/pad-visible-at.mjs --warm 记下新数字");
});

test("降采样发生在 rotateVRM0 之前", () => {
  const cap = LOAD_AVATAR_SRC.indexOf("capTextureResolution(gltf.scene");
  const rotate = LOAD_AVATAR_SRC.indexOf("VRMUtils.rotateVRM0(vrm)");
  assert.ok(cap > 0, "loadAvatarUncached 里没有调用 capTextureResolution");
  assert.ok(rotate > 0, "loadAvatarUncached 里没有调用 rotateVRM0");
  assert.ok(
    cap < rotate,
    "降采样必须在 rotateVRM0 之前：要改的是刚解析完、还没进渲染循环的原始场景",
  );
});

test("贴图要沿原型链找：MToon 的 map 是 getter，Object.values 一张都读不到", () => {
  /* 2026-11-12 第一版用 `Object.values(material)` 找贴图，实测 `seen:0`——
     MToon 的 `map` / `shadeMultiplyTexture` / `emissiveMap` 都是 class 原型上的 getter，
     不在 own enumerable 属性里。这条契约钉住「必须走原型链 + Reflect.get」。 */
  assert.match(LOAD_AVATAR_SRC, /function collectMaterialTextures\(material: object\)/);
  assert.match(LOAD_AVATAR_SRC, /Object\.getOwnPropertyNames\(current\)/);
  assert.match(LOAD_AVATAR_SRC, /current = Object\.getPrototypeOf\(current\) as object \| null;/);
  assert.match(LOAD_AVATAR_SRC, /value = Reflect\.get\(material, key\);/);
  // 只反射名字里带 map/texture 的属性，别把整个对象图都读一遍
  assert.match(LOAD_AVATAR_SRC, /if \(!\/map\|texture\/i\.test\(key\)\) continue;/);
  // 反证：不许再出现按 own values 找贴图的写法
  assert.equal(
    /capTextureResolution[\s\S]{0,400}Object\.values\(material/.test(LOAD_AVATAR_SRC),
    false,
    "capTextureResolution 不许再用 Object.values(material) 找贴图：MToon 的贴图是原型 getter，这样一张都读不到",
  );
});

test("只降 image 有宽高的非立方体贴图，且同一张贴图只处理一次", () => {
  assert.match(LOAD_AVATAR_SRC, /if \(!image\?\.width \|\| !image\.height \|\| image\.isCubeTexture\) continue;/);
  // 同一 material 多处引用同一张图时不能重复画 canvas
  assert.match(LOAD_AVATAR_SRC, /const seen = new Set<unknown>\(\);/);
  assert.match(LOAD_AVATAR_SRC, /if \(seen\.has\(texture\)\) continue;/);
});

test("长边没超过上限时不动原图（不白画一张 canvas）", () => {
  assert.match(LOAD_AVATAR_SRC, /if \(longEdge <= maxEdge\) return null;/);
});

test("跨域画不进 canvas 的图要吞掉异常，不许把整个加载拖进降级", () => {
  assert.match(LOAD_AVATAR_SRC, /try \{\s*const shrunk = shrinkTextureImage/);
  assert.match(LOAD_AVATAR_SRC, /\} catch \{\s*\/\* 跨域图片画不进 canvas/);
});
