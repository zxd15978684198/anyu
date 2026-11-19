/**
 * 门户唯一数字真源（前端计划 §10.3 / §10.4，自裁 S-10）。
 *
 * 纪律：**每个数字都带来源与提交号，未测的写未测，红灯不美化**。
 * 门户不接后端实时指标——避免成为第二个 `/metrics` 消费口径（计划 §10.7）。
 * 同日在途件 `day10/adhoc-20261113-语音三线同步与流式化.md` 的 P1/P2 收口后，
 * 只改本文件的 LATENCY 段并更新 source 提交号，不要散改组件。
 */

/** 数字出处。kind 决定徽标：measured=实测 / reported=转记 / budget=预算 / untested=未测 */
export interface Provenance {
  kind: "measured" | "reported" | "budget" | "untested";
  from: string;
  at?: string;
}

export const PORTAL_BUILD = {
  baselineCommit: "801d44c",
  gateCommit: "4e6aee6",
  date: "2026-11-13",
} as const;

// ---------------------------------------------------------------- 三端

export interface EndInfo {
  key: "pad" | "console" | "family";
  name: string;
  audience: string;
  href: string;
  hrefLabel: string;
  viewport: string;
  stack: string;
  duty: string;
  capabilities: string[];
  accent: "brand" | "info" | "success";
}

/** 职责取技术设计文档 line 162-164；入口取 docs/demo-accounts.md:12-17。 */
export const ENDS: EndInfo[] = [
  {
    key: "pad",
    name: "长者端",
    audience: "长者 · 卧室设备",
    href: "/",
    hrefLabel: "进入长者端",
    viewport: "1920×1080 大字 · 八档断点",
    stack: "React + Tailwind + three-vrm",
    duty: "唤醒词端侧检测 → WebRTC 全双工对话；大字幕、SOS、用药与健康任务面板、断网本地兜底。",
    capabilities: [
      "唤醒词「小伴」端侧 KWS，未命中音频不出端",
      "WebRTC 全双工 + 可打断插话",
      "口型 / TTS / 打字机三线同步",
      "按住说话降级 + 页面输入兜底",
      "IndexedDB 断网兜底 + speechSynthesis",
      "VRM 加载失败或超 5s 降级静态头像",
    ],
    accent: "brand",
  },
  {
    key: "console",
    name: "照护台",
    audience: "护工 / 医生 / 管理员",
    href: "/console",
    hrefLabel: "进入照护台",
    viewport: "桌面优先 · 角色按能力分流",
    stack: "React + 原生 CSS + tokens",
    duty: "机构概览、长者档案、健康采集与代填、工单看板、消息中心、用药管理、每日日报、账号与绑定、知识录入、操作日志审计。",
    capabilities: [
      "能力驱动导航，前端隐藏不替代服务端校验",
      "工单全生命周期与状态变更历史",
      "健康代填 + 绿黄红分级复核",
      "通知重发 / 10 秒撤销 + 审计",
      "医生开药改药停；护工与管理员只读代确认",
      "设备绑定与凭证重发（二次确认 + 审计）",
    ],
    accent: "info",
  },
  {
    key: "family",
    name: "家属端",
    audience: "家属 · 手机优先",
    href: "/family",
    hrefLabel: "进入家属端",
    viewport: "375px 移动优先",
    stack: "React + 原生 CSS + tokens",
    duty: "绑定长者的每日日报与趋势、日报 TTS 语音条点播、站内通知中心。只读，不做处置动作。",
    capabilities: [
      "只看 active 绑定长者的日报",
      "趋势图断线如实呈现缺采日",
      "日报语音条点播",
      "通知按红→黄→绿优先级",
      "读不到长者档案（A-01 一律 403）",
      "通知只读：deliver / ack 对家属 403",
    ],
    accent: "success",
  },
];

// ---------------------------------------------------------------- 三级路由

export interface RouteTier {
  id: "L0" | "L1" | "L2";
  name: string;
  carrier: string;
  budget: string;
  duty: string;
  degrade: string;
  tone: "danger" | "warning" | "info";
  facts: string[];
}

/** 取 AGENTS §6 模型路由表；实测数字取 eval/reports/routing.md。 */
export const ROUTE_TIERS: RouteTier[] = [
  {
    id: "L0",
    name: "规则层",
    carrier: "app/router/safety.py + rules.py",
    budget: "路由 ≤200ms",
    duty: "急症/敏感一票否决前置；高频固定意图直路由（换一段、大声点、停、越剧京剧、几点了、今天天气、我要吃药、再见、重复一遍）。",
    degrade: "不存在降级：规则层失效即 P0 缺陷。",
    tone: "danger",
    facts: [
      "规则条数 ≤10（safety + rules 合计），只做减法",
      "阈值与分级写在代码而非 prompt",
      "异常值同时触发人工告警",
      "实测 route_ms p50 0.2ms / p95 0.6ms",
    ],
  },
  {
    id: "L1",
    name: "System One",
    carrier: "Laya 本地权重 · CPU 推理",
    budget: "单句 ≤200ms",
    duty: "一次前向出 intent / is_sensitive / confidence / privacy 四信号，七类意图分流。",
    degrade: "confidence <0.85 只澄清一次，仍模糊升级 L2；模型加载失败全部走 L2 并打降级标记。",
    tone: "warning",
    facts: [
      "置信度阈值 0.85（AGENTS §7 硬性数值）",
      "spike 实测 p95 822–1568ms，与 200ms 预算差一个数量级",
      "因此当前 L1 实际恒降级到 L2——如实呈现，不掩盖",
      "110 条固定集：109/110 命中，敏感召回 100%",
    ],
  },
  {
    id: "L2",
    name: "System Two",
    carrier: "DeepSeek / Qwen 兼容 API · 统一适配器",
    budget: "首 token ≤800ms",
    duty: "开放域闲聊、多工具 ReAct 编排、结构化输出、记忆摘要。",
    degrade: "超时/限流重试 2 次后退化为「L1 结果 + 模板话术」并提示人工；不得自动执行高危写操作。",
    tone: "info",
    facts: [
      "工具预算 8 次/轮；同一 Tool 1 分钟内第 3 次熔断",
      "失败四分类：参数错 / 目标不存在 / 临时错误 / 越权",
      "越权拒绝 + 写 audit_logs + trace_id",
      "分层命中占比 L0=52 / L1=31 / L2=27",
    ],
  },
];

// ---------------------------------------------------------------- 延迟沙盘

export interface LatencySegment {
  label: string;
  ms: number | null;
  note: string;
  /** null = 未测，渲染为斜纹占位且不计入合计 */
  measured: boolean;
}

export interface LatencyScenario {
  id: string;
  name: string;
  blurb: string;
  /** 服务端回复侧口径；① 唤醒 ② ASR ⑥ 前端播放不在内 */
  segments: LatencySegment[];
  p50: number;
  p95: number;
  max: number;
  /** 判据指标的口径名，避免把「首包」与「整轮」混读 */
  metricLabel: string;
  passRate: string;
  verdict: "pass" | "fail";
  source: Provenance;
  footnotes: string[];
}

export const LATENCY_BUDGET_MS = 1500;

/** 三份实测报告，口径各自标注，**不混算**。 */
export const LATENCY_SCENARIOS: LatencyScenario[] = [
  {
    id: "mock",
    name: "Mock 链路",
    blurb: "迭代 5 Iter2 基线：Mock 合成音 + 垫层话术，全链路本地无外部调用。",
    segments: [
      { label: "① 唤醒", ms: null, note: "端侧 KWS，未纳入本表口径", measured: false },
      { label: "② ASR", ms: null, note: "迭代 5 无 ASR 引擎接线", measured: false },
      { label: "③④⑤ 回复侧合计", ms: 8.8, note: "组装 + 垫层话术 + Mock TTS 首句入队（未分解）", measured: true },
      { label: "⑥ 前端播放", ms: null, note: "未测", measured: false },
    ],
    p50: 8.8,
    p95: 12.2,
    max: 13.0,
    metricLabel: "服务端回复侧首包",
    passRate: "20/20",
    verdict: "pass",
    source: { kind: "measured", from: "eval/reports/latency-baseline.md v1.1", at: "2026-11-07" },
    footnotes: [
      "tts_provider=mock、llm 由 reply_stub 垫层打点并标 provider=mock",
      "**不能外推生产**：Mock 合成音天然极低（该报告 §0 与 plan R3 已明记）",
      "首包口径 = 第一句完成时刻，不是整段合成完",
    ],
  },
  {
    id: "real",
    name: "真链路",
    blurb: "P1-6 复测：真 LLM（DeepSeek 兼容端点）+ 真 weather MCP + 真 Edge TTS。",
    segments: [
      { label: "① 唤醒", ms: null, note: "未纳入本表口径", measured: false },
      { label: "② ASR", ms: null, note: "未纳入本表口径", measured: false },
      { label: "③④ 组装 + LLM/Agent", ms: 2552, note: "reply_ms：出话术", measured: true },
      { label: "⑤ TTS 首块", ms: 1000, note: "tts_ms：20/20 撞满 1s 超时档", measured: true },
      { label: "⑥ 前端播放", ms: null, note: "未测", measured: false },
    ],
    p50: 3549,
    p95: 8236,
    max: 9339,
    metricLabel: "服务端回复侧合计",
    passRate: "0/20",
    verdict: "fail",
    source: { kind: "measured", from: "eval/reports/latency-real.md（P1-6 复测）", at: "2026-11-09" },
    footnotes: [
      "1.5s 内且未降级：**0/20**；TTS 档超时 20/20；LLM 档降级 3/20",
      "真调用工具 4 轮、合计 4 次；异常轮次 0",
      "阈值按 AGENTS §7 不得下调 → 需换流式 TTS / 更快 LLM 端点后复测",
      "这份红灯正是「流式化」立项的依据，门户如实呈现",
    ],
  },
  {
    id: "stream",
    name: "逐句流式探针",
    blurb: "88 字 / 3 句探针 ×5 次：证明逐句流式下「首包 ≪ 整轮」。非判据。",
    segments: [
      { label: "① 唤醒", ms: null, note: "未纳入本表口径", measured: false },
      { label: "② ASR", ms: null, note: "未纳入本表口径", measured: false },
      { label: "③④⑤ 首句入队", ms: 16.3, note: "首包 15.8–16.8ms 区间中值", measured: true },
      { label: "后续句入队", ms: 37.1, note: "整轮 53.4ms 减去首句；整轮/首包 ≈ 3.2–3.4×", measured: true },
      { label: "⑥ 前端播放", ms: null, note: "未测", measured: false },
    ],
    p50: 16.3,
    p95: 16.8,
    max: 55.3,
    metricLabel: "首句入队（首包）",
    passRate: "非判据",
    verdict: "pass",
    source: { kind: "measured", from: "day05-latency-baseline.md §2 补充探针", at: "2026-11-07" },
    footnotes: [
      "跑在 /metrics 20 次快照之后，不掺主测口径",
      "方向可外推：真 TTS 下整段合成的差距只会更大",
      "这是「首包口径改为第一句」的收益证据",
    ],
  },
];

// ---------------------------------------------------------------- 角色与演示数据

export interface RoleInfo {
  role: string;
  entry: string;
  account: string;
  tone: "danger" | "warning" | "info" | "success";
  sees: string;
  cannot: string;
}

/** 取 docs/demo-accounts.md:23-28。**口令刻意不进产物**（AGENTS §4-3、计划 S-8）。 */
export const ROLES: RoleInfo[] = [
  {
    role: "管理员",
    entry: "/console",
    account: "13700000000",
    tone: "danger",
    sees: "全部长者档案、用药管理（可读 + 代确认）、每日日报、健康采集、消息中心、运维统计",
    cannot: "不能开药（开药只给医生）",
  },
  {
    role: "护工",
    entry: "/console",
    account: "13800000002",
    tone: "warning",
    sees: "仅被分配长者的档案 / 健康 / 用药（可读 + 代确认）、消息中心",
    cannot: "看不到每日日报（reports_view 只给管理员与医生）",
  },
  {
    role: "医生",
    entry: "/console",
    account: "13600000001",
    tone: "info",
    sees: "被分配长者的档案 / 健康 / 用药、每日日报、消息中心",
    cannot: "可开药、改药、停用、代确认——药单写权限只在医生",
  },
  {
    role: "家属",
    entry: "/family",
    account: "13900000002",
    tone: "success",
    sees: "已绑定长者的日报列表与详情、通知中心",
    cannot: "读不到长者档案（A-01：/api/elders 一律 403）；通知只读",
  },
];

export const DEMO_DATA = [
  { label: "药单", value: "2 张", detail: "氨氯地平 5mg 每日 08:00、二甲双胍 0.5g 每日 12:30，均启用中" },
  { label: "健康记录", value: "11 条", detail: "覆盖最近 13 天，**故意缺第 4 天与第 9 天**——家属端趋势图断线是刻意的" },
  { label: "日报", value: "4 份", detail: "最近 4 个自然日" },
  { label: "通知", value: "2 条", detail: "健康复核、家属日报" },
] as const;

export const DEMO_DEVICE = {
  code: "11111111-1111-4111-8111-111111111106",
  elder: "张桂芬（A-301）",
  persona: "小满",
  note: "设备密钥库里只有 sha256，无法反查明文；pad 登录页「填入演示账号」走 GET /api/pad/demo-login，该端点在 APP_ENV=production 恒 404。",
} as const;

// ---------------------------------------------------------------- 门禁看板

export interface GateRow {
  name: string;
  value: string;
  state: "pass" | "warn" | "fail" | "info";
  detail: string;
  source: Provenance;
}

export const GATES: GateRow[] = [
  {
    name: "后端 pytest",
    value: "3273 passed",
    state: "warn",
    detail: "1 skipped · 387 warnings · **56 errors** · 451.91s。errors 未清零，如实呈现。",
    source: { kind: "reported", from: "day10/adhoc-20261113-收口验收报告.md", at: "4e6aee6" },
  },
  {
    name: "后端覆盖率",
    value: "83.11%",
    state: "pass",
    detail: "过 fail_under=70；PRD FR-TC-06 要求接口 ≥70%、核心 Tool 与权限 100%。",
    source: { kind: "reported", from: "Lead 在干净库 anyu_cov 实测（报告只转记）", at: "2026-11-13" },
  },
  {
    name: "ruff",
    value: "全绿",
    state: "pass",
    detail: "check All checks passed；format 390 files already formatted。",
    source: { kind: "reported", from: "收口验收报告 §2.1（格式漂移由 4e6aee6 补齐）", at: "4e6aee6" },
  },
  {
    name: "前端用例",
    value: "510 / 510",
    state: "pass",
    detail: "46 个文件 · node:test · 7.5s · 0 fail。含 W1 新增 14 例与门户新增 34 例。",
    source: { kind: "measured", from: "node scripts/run-frontend-tests.mjs（本门户构建时实跑）", at: "2026-11-13" },
  },
  {
    name: "前端类型与构建",
    value: "tsc 0 · build ✓",
    state: "pass",
    detail: "tsc --noEmit exit=0；vite build 四入口产出（含 portal）。",
    source: { kind: "measured", from: "本轮实跑", at: "2026-11-13" },
  },
  {
    name: "three-vrm 隔离",
    value: "结构性保证",
    state: "pass",
    detail:
      "three/vrm 已显式分成独立 chunk（741 kB），**只有 index.html 引用它**；console / family / portal 三个入口的 HTML 与 JS 均不引用，渲染器类名命中 0。PRD line 88 硬约束。隔离从「靠 import 图碰巧成立」升级为分块结构上成立。",
    source: { kind: "measured", from: "dist/*.html 引用清单 + 渲染器类名计数", at: "2026-11-13" },
  },
  {
    name: "Tailwind 跨端泄漏",
    value: "已消除",
    state: "pass",
    detail:
      "四端各自 @import \"tailwindcss\" source(none) + @source 只扫本端。CSS 实测：family 44.2→17.2 kB（-61%）、console 45.4→17.4 kB（-62%）、portal 61.9→33.6 kB（-46%）；pad 自身 utility 与四端 token 全部完好。",
    source: { kind: "measured", from: "vite build 前后产物对比", at: "2026-11-13" },
  },
  {
    name: "路由评测",
    value: "109 / 110",
    state: "pass",
    detail: "敏感召回 **100%**；route_ms p50 0.2ms / p95 0.6ms；分层 L0=52 / L1=31 / L2=27。",
    source: { kind: "measured", from: "eval/reports/routing.md", at: "2026-11-07" },
  },
  {
    name: "真链路首包",
    value: "0 / 20",
    state: "fail",
    detail: "1.5s 预算内 0 轮达标；TTS 20/20 撞 1s 档。流式化立项依据，不掩盖。",
    source: { kind: "measured", from: "eval/reports/latency-real.md", at: "2026-11-09" },
  },
];

export interface ArtifactRow {
  name: string;
  js: number;
  css: number;
  vrm: number;
}

/** 本轮 vite build 实测（kB，产物未压缩）。
 *  vrm 列统计的是**代码符号** `WebGLRenderer`（three.js 渲染器类名），
 *  不是裸字符串 `three-vrm`、也不是 VRM 运行时类名——门户文案会正当地提到那些词，
 *  用它们做断言会被自己的说明文字误报（实测踩过两次）。
 *  更硬的判据是结构性的：只有 index.html 引用 vrm chunk，见 tests/portal.test.ts。 */
export const ARTIFACTS: ArtifactRow[] = [
  { name: "pad", js: 231.13, css: 41.02, vrm: 1 },
  { name: "console", js: 195.47, css: 17.4, vrm: 0 },
  { name: "family", js: 32.14, css: 17.2, vrm: 0 },
  { name: "portal", js: 46.48, css: 33.6, vrm: 0 },
  { name: "共享 client", js: 222.44, css: 0, vrm: 0 },
  { name: "vrm chunk", js: 741.19, css: 0, vrm: 5 },
];

// ---------------------------------------------------------------- 硬性数值

/** AGENTS §7：写死在常量里，不得私自调整。门户只展示，不提供修改入口。 */
export const HARD_NUMBERS = [
  { k: "工具预算", v: "8 次/轮", why: "PRD FR-DLG-10" },
  { k: "同 Tool 熔断", v: "1 分钟第 3 次", why: "FR-DLG-10 · TD §6.2" },
  { k: "快决策置信度", v: "0.85", why: "PRD §2.3" },
  { k: "声纹判定", v: "cosine ≥0.55", why: "2026-11-09 裁决，由 0.85 下调" },
  { k: "用药触达", v: "T0/+5/+10/+15 各等 60s", why: "PRD §4.5" },
  { k: "健康采集窗口", v: "19:00–20:00", why: "PRD §4.6" },
  { k: "健康红标", v: "SBP ≥180 或 DBP ≥120", why: "单分量即判红" },
  { k: "原始音频保留", v: "72 小时", why: "TD §1.2 第 8 条" },
  { k: "3D 降级", v: "失败或 >5s", why: "FR-3D-02" },
  { k: "简单指令响应", v: "≤1.5s（20 次 ≥19）", why: "PRD §5.3" },
  { k: "固定意图路由", v: "≤200ms", why: "PRD §5.3 · FR-DLG-02" },
  { k: "规则层规模", v: "≤10 条", why: "TD §1.1 / §1.3" },
] as const;

export const SERVICES = [
  { name: "postgres + pgvector", port: "5432", note: "业务表与 langgraph schema 物理隔离" },
  { name: "redis", port: "6379", note: "短期会话 / 限流 / 重复调用检测" },
  { name: "backend", port: "8000", note: "FastAPI + Uvicorn" },
  { name: "frontend nginx", port: "80 / 443", note: "托管 dist，HTTPS 本地受控证书" },
  { name: "laya", port: "7001", note: "System One 四信号" },
  { name: "funasr", port: "7002", note: "2pass 流式 ASR" },
  { name: "voiceprint", port: "7003", note: "CAM++ 192 维，独立容器不装 torch 进主服务" },
  { name: "weather-mcp", port: "7101", note: "唯一真实外部接口" },
  { name: "mock-operations-mcp", port: "7102", note: "工单 / 通知，Mock 但落库可查" },
  { name: "coturn", port: "3478 / 5349", note: "TURN 中继，凭据从环境注入" },
] as const;

/** 沙盘导出：把当前场景与分位导出成 JSON，便于贴进评审材料。 */
export function exportSnapshot(scenario: LatencyScenario, quantile: "p50" | "p95" | "max"): string {
  return JSON.stringify(
    {
      generated_by: "安语演示门户",
      baseline_commit: PORTAL_BUILD.baselineCommit,
      scenario: scenario.id,
      scenario_name: scenario.name,
      quantile,
      metric_label: scenario.metricLabel,
      budget_ms: LATENCY_BUDGET_MS,
      value_ms: scenario[quantile],
      pass_rate: scenario.passRate,
      verdict: scenario.verdict,
      segments: scenario.segments.map((s) => ({
        label: s.label,
        ms: s.ms,
        measured: s.measured,
        note: s.note,
      })),
      source: scenario.source,
      footnotes: scenario.footnotes,
    },
    null,
    2,
  );
}