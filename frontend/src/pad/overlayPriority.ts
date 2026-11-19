/**
 * 浮层互斥仲裁（纯函数，无 React 依赖）
 *
 * 为什么需要这一层：长者端同一时刻最多该看到**一个**浮层。2026-11-11 用户反馈
 * 「弹窗互相遮蔽而且内容显示不全」——身份确认、声纹录入、用药抽屉、健康采集、
 * 唤醒词设置、顶栏「设置」弹层各自用 absolute/fixed + z-index 渲染，z 序是散在
 * 六个地方写死的（z-10 / z-20 / z-30 / z-40），谁也说不清叠加时会怎样。
 *
 * 这里的做法是**先仲裁、再渲染**：被压制的浮层根本不进 DOM（不是靠 z-index 盖住），
 * 但调用方的 state 一律保留，所以关掉上层后下层会自然回到屏幕上——不需要任何
 * 「记住谁被谁盖住了」的簿记。
 *
 * 优先级只在下面这一个数组里定义。要调整顺序就改这里，并在
 * `frontend/tests/popup-arbitration.test.ts` 里同步改断言（那组用例会钉死顺序，
 * 避免有人为了某次演示把 SOS 挪到第二位）。
 */
export type OverlayId =
  | "sos"
  | "incoming"
  | "identity"
  | "medication"
  | "health"
  | "media"
  | "memory"
  | "enroll"
  | "wakeWord"
  | "settings"
  | "privacy"
  | "guide";

/**
 * 数组下标即优先级：越靠前越优先，**同下标不会出现两次**。
 *
 * 顺序的三条依据：
 * 1. `sos` 最高——按铃之后老人只剩「等待」一件事，任何卡片都不该盖住它；而且
 *    FR-UI-08 明确红色事件不可由长者撤销，结果页必须一直可读。
 * 2. `identity` 高于用药/健康——身份没确认时，用药回执与健康数据都记不到人头上
 *    （FR-VPR-03 的 generic 模式会禁用这两件事），先问清「是谁」比先发药更基本。
 * 3. `settings` 最低——顶栏那个 `<details>` 只是入口收纳，任何正式浮层出现都该把它收起。
 */
export const OVERLAY_PRIORITY: readonly OverlayId[] = [
  "sos",
  // 2026-11-16 纳入仲裁：来电是全屏遮罩，原实现靠 z-30 硬压且注释明写
  // 「有人打电话进来压过其它流程」——那是有意设计，但绕过仲裁就违背了本文件
  // 「被压制的层根本不进 DOM」的铁律。收进来并放在 sos 之后，语义与原注释一致：
  // 只有求助结果页能压过「护工打进来」。
  "incoming",
  "identity",
  "medication",
  "health",
  // 2026-11-12 新增两项（戏曲播放器 media / 长者记忆面板 memory）：排在 health 之后、
  // enroll 之前。理由两条：
  //   1. 它们是长者自己主动打开的「消遣/查看」，吃药回执与健康采集是有人在等结果的
  //      照护动作——两者同时来，让长者先看见前者；
  //   2. 它们又高于声纹录入、唤醒词设置与本机设置：那三项是调试/初始化流程，
  //      长者不会在听戏的同时去录声纹，反而常出现「设置面板忘了关，一按戏就叠住」。
  "media",
  "memory",
  "enroll",
  "wakeWord",
  "settings",
  // 2026-11-16 纳入仲裁并置**最低**：隐私说明与首启引导都是「读一段文字」的信息层，
  // 任何照护动作、来电、甚至设置面板都该压过它们。
  // 修的是真缺陷：隐私说明原为全屏 z-40 且不参与仲裁，长者开着它时 15s 轮询弹出的
  // 用药卡虽按仲裁渲染了却在 z-20，被整个盖住 —— 卡片状态是「已打开」，
  // 长者看不见也点不到，一条用药提醒静默错过。纳入后隐私层直接不进 DOM。
  "privacy",
  "guide",
];

/**
 * 从「当前打开着的浮层」里挑出唯一该被渲染的那个。
 *
 * - 传进来的顺序无关：结果只由 `OVERLAY_PRIORITY` 决定（调用方不必先排序）。
 * - 重复项（同一 id 出现两次）只算一个——「同级只留一个」。
 * - 表外 id 直接忽略（TypeScript 挡住了，但运行时数据可能来自 JSON/日志回放）。
 */
export function pickVisible(open: readonly OverlayId[]): OverlayId | null {
  let best: OverlayId | null = null;
  let bestRank = Number.POSITIVE_INFINITY;

  for (const id of open) {
    const rank = OVERLAY_PRIORITY.indexOf(id);
    if (rank === -1) continue;
    // 用严格小于：同 id 重复时保留先遇到的那个，结果与输入顺序无关（因为同 rank）
    if (rank < bestRank) {
      best = id;
      bestRank = rank;
    }
  }

  return best;
}

/**
 * 这个浮层是否「开着但被压制」——用于日志/诊断与单测断言。
 *
 * 未打开的 id 一律返回 false（不是「被压制」，而是根本没开），
 * 否则调用方会把「没打开」误报成「被更高的层挡住了」。
 */
export function overlaySuppressed(open: readonly OverlayId[], id: OverlayId): boolean {
  if (!open.includes(id)) return false;
  return pickVisible(open) !== id;
}
