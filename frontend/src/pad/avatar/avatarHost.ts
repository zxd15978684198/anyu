/**
 * 数字人控制器与实时客户端之间的**中转站**（2026-11-11）。
 *
 * 为什么需要它：产品负责人要的是「一进页面数字人就站在那儿」，而 3D 的代价（10.7MB VRM 解析、
 * 纹理上传、MToon 着色器编译）必须在**登录之前**就付掉。于是把 `AvatarStage` 从「登录后
 * 的 MainScreen」提到 App 根层（见 `AvatarLayer`），它在登录页就已经挂载并渲染。
 *
 * 但控制器要交给 `RealtimeClient`（口型/说话态驱动），而客户端只在登录后才创建 ——
 * 两者生命周期不再重合，所以用一个模块级中转：
 *   - 渲染器先就绪 → 存下 controller，等客户端来了再补投；
 *   - 客户端先就绪 → 直接绑定已存下的 controller。
 *
 * 只存一个实例（长者端是单页单舞台），不做多实例管理。
 */
import type { AvatarController } from "./VrmAvatar";
import type { ClientDiagStage } from "../protocol";

/** RealtimeClient 里本模块用到的最小面（避免为一行类型去 import 整个客户端） */
interface AvatarClient {
  setAvatarController(controller: AvatarController | null): void;
  sendClientDiag?(stage: ClientDiagStage, ok: boolean, error?: string): void;
}

let controller: AvatarController | null = null;
let client: AvatarClient | null = null;

/** AvatarStage 就绪：存下控制器，客户端若已在则立刻补投 */
export function attachAvatarController(next: AvatarController): void {
  controller = next;
  client?.setAvatarController(next);
}

/** 客户端创建：登记并补投已就绪的控制器（控制器还没来也没关系，等 attachAvatarController） */
export function attachAvatarClient(next: AvatarClient): void {
  client = next;
  if (controller) next.setAvatarController(controller);
}

/** 客户端销毁（退出登录/卸载）：只解绑，不销毁控制器——3D 要在登录页继续跑 */
export function detachAvatarClient(next: AvatarClient): void {
  if (client === next) client = null;
}

/** 端侧诊断转发：登录前客户端还没建，直接丢弃（服务端本来也只认已鉴权连接） */
export function forwardAvatarDiag(stage: ClientDiagStage, ok: boolean, error?: string): void {
  client?.sendClientDiag?.(stage, ok, error);
}

/** 只读取当前控制器（测试与排障用） */
export function currentAvatarController(): AvatarController | null {
  return controller;
}

/** 单测用：清空中转状态 */
export function resetAvatarHostForTest(): void {
  controller = null;
  client = null;
}
