/**
 * 演示门户「大厅」（前端计划 §10，批次 WP）。
 *
 * 定位：面向评审 / 面试官 / clone 仓库的同事的入口页。现状根地址直进长者端，
 * 来的人看不到另外两端与演示账号——本页把「三端 + 三级路由 + 全链路语音 + 门禁真实状态」
 * 一次讲清。**不占用 `/`**（长者端设备语义），走独立第 4 入口 /portal。
 *
 * 不是产品功能，PRD 无此需求，属范围外增强（计划 S-7 / README §12 D9）。
 */
import { useMemo } from "react";
import { ARTIFACTS, ENDS, GATES, LATENCY_SCENARIOS, PORTAL_BUILD } from "./baselines";
import { DemoData, RoleMatrix } from "./components/Access";
import { EndCards } from "./components/EndCards";
import { GateBoard } from "./components/GateBoard";
import {
  IconArrowRight,
  IconLayers,
  IconPulse,
  IconShield,
  IconSpark,
} from "./components/Icons";
import { LatencySandbox } from "./components/LatencySandbox";
import { RouteMap } from "./components/RouteMap";
import { useScrollSpy, useToast } from "./hooks";

const SECTIONS = [
  { id: "ends", label: "三端" },
  { id: "routing", label: "三级路由" },
  { id: "latency", label: "延迟沙盘" },
  { id: "access", label: "账号与数据" },
  { id: "gates", label: "门禁实况" },
] as const;

const SECTION_IDS = ["hero", ...SECTIONS.map((s) => s.id)] as const;

export default function App() {
  const ids = useMemo(() => SECTION_IDS, []);
  const active = useScrollSpy(ids);
  const [toast, showToast] = useToast();

  const passCount = GATES.filter((g) => g.state === "pass").length;
  const redCount = GATES.filter((g) => g.state === "fail" || g.state === "warn").length;
  const isolated = ARTIFACTS.filter((a) => a.name !== "pad").every((a) => a.vrm === 0);
  const realScenario = LATENCY_SCENARIOS.find((s) => s.id === "real");

  return (
    <>
      <div className="p-bg" aria-hidden="true">
        <div className="p-bg-mesh" />
        <div className="p-blob p-blob-a" />
        <div className="p-blob p-blob-b" />
        <div className="p-blob p-blob-c" />
        <div className="p-grain" />
      </div>

      <header className="p-nav">
        <a className="p-brand" href="#hero">
          <span className="p-brand-mark">
            <IconSpark size={17} />
          </span>
          <span>
            安语
            <span className="p-brand-sub"> ANYU · 演示门户</span>
          </span>
        </a>

        <nav className="p-nav-links" aria-label="页面区块">
          {SECTIONS.map((s) => (
            <a
              key={s.id}
              className={active === s.id ? "p-nav-link is-active" : "p-nav-link"}
              href={`#${s.id}`}
              aria-current={active === s.id ? "true" : undefined}
            >
              {s.label}
            </a>
          ))}
        </nav>

        <a className="p-nav-cta" href={ENDS[0].href}>
          进入长者端
          <IconArrowRight size={15} />
        </a>
      </header>

      <main className="p-main">
        {/* ---------------------------------------------------------- 开场 */}
        <section className="p-section p-hero" id="hero">
          <div className="p-hero-grid">
            <div className="p-staged">
              <span className="p-eyebrow">CCRC 康养社区 · 长者卧室</span>
              <h1 className="p-h1">
                一个数字人，
                <br />
                三端一条链路。
              </h1>
              <p className="p-lead">
                浏览器里的 VRM 数字人 + WebRTC 全双工语音，用「安全规则层 → Laya 端侧分类器 →
                主控 Agent」三级路由，承担情感陪伴、用药提醒、健康采集、戏曲与天气检索、人工告警五类职责。
                <strong> 不做医疗诊断，不替代 SOS 紧急呼叫。</strong>
              </p>
              <div className="p-hero-cta">
                <a className="p-btn p-btn-primary" href="#ends">
                  从三端开始
                  <IconArrowRight />
                </a>
                <a className="p-btn p-btn-secondary" href="#latency">
                  <IconPulse />
                  看延迟沙盘
                </a>
                <a className="p-btn p-btn-ghost" href="#gates">
                  <IconShield />
                  门禁实况（含红灯）
                </a>
              </div>
              <p className="p-note">
                本页是<strong>演示入口</strong>，不是产品功能：不占用根地址 <code>/</code>
                （长者端开机即数字人，不该先选端）。数字全部来自仓内实测报告，未测的写未测。
              </p>
            </div>

            <div className="p-hero-stats p-staged">
              <div className="p-stat">
                <div className="p-stat-v">3 端</div>
                <div className="p-stat-k">同一仓库 · Vite MPA</div>
                <p className="p-stat-note">
                  长者端 Tailwind + three-vrm；照护台与家属端原生 CSS + tokens，不引组件库。
                </p>
              </div>
              <div className="p-stat">
                <div className="p-stat-v">3 级</div>
                <div className="p-stat-k">L0 规则 → L1 Laya → L2 Agent</div>
                <p className="p-stat-note">
                  110 条固定集 109/110 命中，敏感召回 100%；route_ms p50 0.2ms。
                </p>
              </div>
              <div className="p-stat">
                <div className="p-stat-v">
                  {passCount}/{GATES.length}
                </div>
                <div className="p-stat-k">门禁通过项</div>
                <p className="p-stat-note">
                  另有 {redCount} 项带红灯，如实呈现不美化。
                </p>
              </div>
              <div className="p-stat">
                <div className="p-stat-v">{isolated ? "0" : "!"}</div>
                <div className="p-stat-k">非 pad 产物 VRM 标记</div>
                <p className="p-stat-note">
                  three-vrm 未泄漏进照护台 / 家属端 / 共享包（PRD line 88 硬约束）。
                </p>
              </div>
            </div>
          </div>
        </section>

        {/* ---------------------------------------------------------- 三端 */}
        <section className="p-section" id="ends">
          <div className="p-section-head">
            <span className="p-eyebrow">01 · 三端</span>
            <h2 className="p-h2">三个门，各走各的角色</h2>
            <p className="p-lead">
              单仓库单 React 工程、Vite MPA 三入口。职责边界取技术设计文档 line 162-164；
              每张卡片的「进入」都是真链接，直接跳到对应端。
            </p>
          </div>
          <EndCards />
          <div className="p-callout" data-tone="info">
            <IconLayers />
            <span>
              <strong>还有第四个地址：</strong>
              <code>/diagnose.html</code> 是 3D 自检页，判断当前浏览器能否跑 WebGL。
              它与本页同属「工程辅助页」，都不在 PRD 需求范围内（README §12 D9）。
            </span>
          </div>
        </section>

        {/* ---------------------------------------------------------- 路由 */}
        <section className="p-section" id="routing">
          <div className="p-section-head">
            <span className="p-eyebrow">02 · 三级路由</span>
            <h2 className="p-h2">低层不得被高层覆盖</h2>
            <p className="p-lead">
              点左侧任一层看它的职责、延迟预算、降级路径与实测事实。
              急症与敏感判定先于一切；规则层失效即 P0 缺陷，不存在降级。
            </p>
          </div>
          <RouteMap />
        </section>

        {/* ---------------------------------------------------------- 沙盘 */}
        <section className="p-section" id="latency">
          <div className="p-section-head">
            <span className="p-eyebrow">03 · 延迟沙盘</span>
            <h2 className="p-h2">1.5 秒预算，和它的真实差距</h2>
            <p className="p-lead">
              三个场景来自三份实测报告，口径各自标注、不混算。
              切到「真链路」就能看到问题：{realScenario ? `${realScenario.passRate} 达标` : ""}
              ，TTS 20/20 撞满 1s 超时档。这是流式化立项的依据，不是装饰。
            </p>
          </div>
          <LatencySandbox onToast={showToast} />
        </section>

        {/* ---------------------------------------------------------- 账号 */}
        <section className="p-section" id="access">
          <div className="p-section-head">
            <span className="p-eyebrow">04 · 账号与演示数据</span>
            <h2 className="p-h2">用什么身份进去，看到什么</h2>
            <p className="p-lead">
              四端账号与可见范围如下。<strong>口令刻意不在本页</strong>——
              按 AGENTS §4-3 不编进前端产物，需要时查 <code>docs/demo-accounts.md</code>。
            </p>
          </div>
          <RoleMatrix onToast={showToast} />
          <div style={{ marginTop: 32 }}>
            <DemoData onToast={showToast} />
          </div>
        </section>

        {/* ---------------------------------------------------------- 门禁 */}
        <section className="p-section" id="gates">
          <div className="p-section-head">
            <span className="p-eyebrow">05 · 门禁实况</span>
            <h2 className="p-h2">跑出来的数字，包括不好看的</h2>
            <p className="p-lead">
              每项标注「实测 / 转记」与来源提交号。覆盖率是转记 Lead 在干净库的实测，
              不是本页自跑；后端 56 errors 与真链路 0/20 都保留原样。
            </p>
          </div>
          <GateBoard />
        </section>
      </main>

      <footer className="p-footer">
        <div className="p-footer-inner">
          <p className="p-footer-text">
            安语 Anyu · 演示门户 · 基线 <code>{PORTAL_BUILD.baselineCommit}</code> ·{" "}
            {PORTAL_BUILD.date}
            <br />
            验收口径：能力是否存在、链路是否走通、边界是否守住——不是参数是否与 PRD 逐字一致。
          </p>
          <div className="p-chips">
            {ENDS.map((e) => (
              <a className="p-btn p-btn-ghost p-btn-sm" href={e.href} key={e.key}>
                {e.name}
                <IconArrowRight size={14} />
              </a>
            ))}
          </div>
        </div>
      </footer>

      {toast && (
        <div className="p-toast" role="status" aria-live="polite">
          {toast}
        </div>
      )}
    </>
  );
}