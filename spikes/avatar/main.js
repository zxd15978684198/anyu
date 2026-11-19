/**
 * T5a spike —— 只回答三个问题（plan day04 §5 W4 / T5a）：
 *   ① vrm1_sample.vrm 能否加载成功（gltf.userData.vrm 有值）
 *   ② 首帧耗时 ms（loadAsync 开始 → 第一次 renderer.render 返回）
 *   ③ rotateVRM0 后朝向是否正确（不转的话人物会背对镜头）
 *
 * 第 ③ 条的判据不靠肉眼：VRM 人形骨架里 leftHand 与 rightHand 的世界 x 之差就是「脸的朝向」。
 * 人物面向 +Z（镜头在 +Z）时，自己的左手在 +X，故 dx = left.x - right.x > 0。
 * 同一份数据在 rotateVRM0 前后各取一次，正好把「不转就背对」也测出来。
 */
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";
import { VRMAnimationLoaderPlugin, createVRMAnimationClip } from "@pixiv/three-vrm-animation";

const VRM_URL = "/avatars/vrm1_sample.vrm";
const IDLE_URL = "/anims/idle.vrma";
const QUERY = new URLSearchParams(location.search);
const USE_IDLE = QUERY.get("idle") === "1";
/** frame=fixed（默认，旧常量）｜frame=auto（按头高自适应）—— 用于取景改前/改后的对照 */
const FRAME_MODE = QUERY.get("frame") === "auto" ? "auto" : "fixed";

/** 头高基准：bust 常量 (0,1.55,2.2)/(0,1.45,0) 正是 headY=1.55 这一特例 */
const REFERENCE_HEAD_Y = 1.55;
const MIN_FRAME_SCALE = 0.7;
const MAX_FRAME_SCALE = 1.4;

/**
 * 取景推导（与 frontend/src/pad/avatar/VrmAvatar.ts 的 frameBust 同口径）：
 *   scale   = clamp(headY / 1.55, 0.7, 1.4)
 *   机位    = (0, headY, 2.2 × scale)      // 与头骨同高
 *   看向    = (0, headY − 0.10 × scale, 0) // 略低于头骨，把肩/胸带进画面
 * headY=1.55 时原样退回旧常量，故「改前」= scale 1 的那一行。
 */
function computeFraming(headY, mode) {
  const fixed = {
    source: "fixed",
    scale: 1,
    position: [0, 1.55, 2.2],
    target: [0, 1.45, 0],
  };
  if (mode !== "auto" || !headY) return fixed;
  const scale = Math.min(Math.max(headY / REFERENCE_HEAD_Y, MIN_FRAME_SCALE), MAX_FRAME_SCALE);
  return {
    source: "auto",
    headY: Number(headY.toFixed(3)),
    scale: Number(scale.toFixed(4)),
    position: [0, Number(headY.toFixed(3)), Number((2.2 * scale).toFixed(3))],
    target: [0, Number((headY - 0.1 * scale).toFixed(3)), 0],
  };
}

const spike = {
  done: false,
  url: VRM_URL,
  loaded: false,
  metaVersion: null,
  elapsedToVrmMs: null,
  firstFrameMs: null,
  warmFrameMs: null,
  webglRenderer: null,
  bytes: null,
  transferMs: null,
  dxBefore: null,
  facingBefore: null,
  dxAfter: null,
  facingAfter: null,
  rotationYAfter: null,
  error: null,
};
window.__SPIKE__ = spike;

const out = document.getElementById("out");
const render = () => {
  out.textContent = JSON.stringify(spike, null, 2);
};
const fail = (error) => {
  spike.error = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  spike.done = true;
  render();
};

/** left.x - right.x：> 0 面向 +Z（镜头），< 0 背对 */
function measureFacing(vrm) {
  const left = vrm.humanoid?.getNormalizedBoneNode("leftHand");
  const right = vrm.humanoid?.getNormalizedBoneNode("rightHand");
  if (!left || !right) return { dx: null, facing: "no_humanoid" };
  vrm.scene.updateMatrixWorld(true);
  const pl = new THREE.Vector3();
  const pr = new THREE.Vector3();
  left.getWorldPosition(pl);
  right.getWorldPosition(pr);
  const dx = pl.x - pr.x;
  return { dx: Number(dx.toFixed(4)), facing: dx > 0 ? "toward_camera(+Z)" : "away_from_camera(-Z)" };
}

/**
 * 资产里没有 .vrma 待机动画 → 不摆姿势就是 T-pose，双臂横伸到框外。
 * 这里把上臂往下收回 70°，得到一个自然的 A-pose，用来验证「静息姿势」够不够看。
 */
function relaxArms(vrm) {
  const drop = THREE.MathUtils.degToRad(Number(new URLSearchParams(location.search).get("drop") ?? 70));
  const left = vrm.humanoid?.getNormalizedBoneNode("leftUpperArm");
  const right = vrm.humanoid?.getNormalizedBoneNode("rightUpperArm");
  if (left) left.rotation.z = -drop;
  if (right) right.rotation.z = drop;
  return { drop: Number(drop.toFixed(3)), applied: Boolean(left && right) };
}

async function main() {
  const t0 = performance.now();

  const loader = new GLTFLoader();
  loader.register((parser) => new VRMLoaderPlugin(parser));

  const gltf = await loader.loadAsync(VRM_URL);
  spike.elapsedToVrmMs = Number((performance.now() - t0).toFixed(1));

  const vrm = gltf.userData.vrm;
  spike.loaded = Boolean(vrm);
  if (!vrm) throw new Error("gltf.userData.vrm 为空：文件不是 VRM");
  spike.metaVersion = vrm.meta?.metaVersion ?? null;

  // 与正式代码一致的三连；先测「旋转前」的朝向，再 rotateVRM0，再测一次
  VRMUtils.removeUnnecessaryVertices(gltf.scene);
  VRMUtils.removeUnnecessaryJoints(gltf.scene);

  const before = measureFacing(vrm);
  spike.dxBefore = before.dx;
  spike.facingBefore = before.facing;

  VRMUtils.rotateVRM0(vrm);

  const after = measureFacing(vrm);
  spike.dxAfter = after.dx;
  spike.facingAfter = after.facing;
  spike.rotationYAfter = Number(vrm.scene.rotation.y.toFixed(4));

  // 朝向已测完，这时才摆静息姿势（不影响上面的朝向结论）
  spike.relaxPose = relaxArms(vrm);

  // —— T5 收尾：自适应取景的唯一输入 —— 头骨（head joint）的世界高度 ——
  // 正式代码 VrmAvatar.ts 用同一个口径：bust 常量 (0,1.55,2.2)/(0,1.45,0) 恰好是
  // 「头高 1.55m」这个特例，其余身材按 headY/1.55 等比缩放。
  const headNode = vrm.humanoid?.getNormalizedBoneNode("head");
  vrm.scene.updateMatrixWorld(true);
  const headPos = new THREE.Vector3();
  if (headNode) headNode.getWorldPosition(headPos);
  spike.headY = headNode ? Number(headPos.y.toFixed(3)) : null;

  // —— 与正式代码相同的 bust 取景 ——
  const canvas = document.getElementById("stage");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setClearColor(0xeeeaf1, 1);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);
  const framing = computeFraming(spike.headY, FRAME_MODE);
  spike.framing = framing;
  camera.position.set(...framing.position);
  camera.lookAt(...framing.target);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x8d8dc0, 1.6));
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
  keyLight.position.set(1.4, 2.5, 2.8);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0xbad2ff, 0.8);
  fillLight.position.set(-2.2, 1.2, -1.8);
  scene.add(fillLight);

  scene.add(vrm.scene);

  // 待机动画（task-12 交付的 Apache-2.0 .vrma）：有就用动画，没有就停在静息姿势
  const mixer = new THREE.AnimationMixer(vrm.scene);
  if (USE_IDLE) {
    const animLoader = new GLTFLoader();
    animLoader.register((parser) => new VRMAnimationLoaderPlugin(parser));
    const animGltf = await animLoader.loadAsync(IDLE_URL);
    const vrma = animGltf.userData.vrmAnimations?.[0];
    if (!vrma) throw new Error("vrmAnimations 为空：文件不是 .vrma");
    const clip = createVRMAnimationClip(vrma, vrm);
    mixer.clipAction(clip).play();
    spike.idleClip = {
      url: IDLE_URL,
      duration: Number(clip.duration.toFixed(3)),
      tracks: clip.tracks.length,
    };
  }

  vrm.update(0);
  renderer.render(scene, camera); // ← 首帧（含 MToon 着色器编译与贴图上传）
  spike.firstFrameMs = Number((performance.now() - t0).toFixed(1));

  // 稳态帧耗时：着色器已编译后的平均，比首帧更能说明「转起来卡不卡」
  const warm = performance.now();
  for (let i = 0; i < 10; i += 1) {
    mixer.update(1 / 60);
    vrm.update(1 / 60);
    renderer.render(scene, camera);
  }
  spike.warmFrameMs = Number(((performance.now() - warm) / 10).toFixed(1));

  // 截图前把待机动画推进到 1.5s 处，落在一个有代表性的姿态（不是第 0 帧）
  if (USE_IDLE) {
    for (let i = 0; i < 90; i += 1) {
      mixer.update(1 / 60);
      vrm.update(1 / 60);
    }
    renderer.render(scene, camera);
  }

  // 软件光栅（无头浏览器）与真机 GPU 差一个数量级，必须把实际渲染器记下来
  const gl = renderer.getContext();
  const debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
  spike.webglRenderer = debugInfo ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);

  // 传输体积 / 网络耗时（按完整 URL 找条目，GLTFLoader 用 fetch）
  const entry = performance
    .getEntriesByType("resource")
    .filter((item) => item.name.includes("vrm1_sample.vrm"))
    .at(-1);
  if (entry) {
    spike.bytes = entry.transferSize || entry.encodedBodySize || null;
    spike.transferMs = Number(entry.duration.toFixed(1));
  }

  spike.done = true;
  render();
  // 便于 Playwright 判断「画面已经真的出来了」
  document.body.dataset.ready = "1";
}

main().catch(fail);
