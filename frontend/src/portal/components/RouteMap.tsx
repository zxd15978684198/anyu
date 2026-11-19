/**
 * 三级路由交互图：点节点出详情。左列 sticky，右列详情随选择切换。
 * 数字与降级路径取 AGENTS §6 与 eval/reports/routing.md，未测/恒降级如实写。
 */
import { useState } from "react";
import { ROUTE_TIERS } from "../baselines";
import { useReveal } from "../hooks";
import { IconArrowDown, IconLayers } from "./Icons";

export function RouteMap() {
  const [active, setActive] = useState(ROUTE_TIERS[0].id);
  const ref = useReveal<HTMLDivElement>();
  const tier = ROUTE_TIERS.find((t) => t.id === active) ?? ROUTE_TIERS[0];

  return (
    <div className="p-route p-reveal" ref={ref}>
      <div className="p-route-stack" role="tablist" aria-label="三级路由">
        {ROUTE_TIERS.map((t, i) => (
          <div key={t.id}>
            <button
              type="button"
              role="tab"
              id={`p-tier-${t.id}`}
              aria-selected={t.id === active}
              aria-controls="p-tier-detail"
              className={t.id === active ? "p-tier is-active" : "p-tier"}
              data-tone={t.tone}
              onClick={() => setActive(t.id)}
            >
              <span className="p-tier-id">{t.id}</span>
              <span>
                <span className="p-tier-name">{t.name}</span>
                <span className="p-tier-carrier">{t.carrier}</span>
              </span>
              <span className="p-tier-budget">{t.budget}</span>
            </button>
            {i < ROUTE_TIERS.length - 1 && (
              <span className="p-route-arrow" aria-hidden="true">
                <IconArrowDown size={15} />
              </span>
            )}
          </div>
        ))}
        <p className="p-note">
          请求固定按 L0 → L1 → L2 升序，<strong>低层不得被高层覆盖</strong>。优先级：合法性校验 &gt;
          急症/敏感安全规则 &gt; 进行中用药/健康任务 &gt; 固定意图 &gt; 低置信澄清或 System Two &gt; 记忆个性化。
        </p>
      </div>

      <div
        className="p-card p-detail"
        id="p-tier-detail"
        role="tabpanel"
        aria-labelledby={`p-tier-${tier.id}`}
        tabIndex={0}
      >
        <span className="p-badge" data-tone={tier.tone}>
          <IconLayers size={14} />
          {tier.id} · {tier.name}
        </span>
        <h3 className="p-detail-title" style={{ marginTop: 12 }}>{tier.carrier}</h3>

        <div className="p-detail-block">
          <div className="p-detail-label">负责什么</div>
          <p className="p-detail-text">{tier.duty}</p>
        </div>

        <div className="p-detail-block">
          <div className="p-detail-label">不可用 / 低置信时</div>
          <p className="p-detail-text">{tier.degrade}</p>
        </div>

        <div className="p-detail-block">
          <div className="p-detail-label">事实与实测</div>
          <ul className="p-facts">
            {tier.facts.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        </div>

        <div className="p-callout" data-tone="info">
          <span>
            <strong>延迟预算口径：</strong>逐层毫秒数是按 1.5s 端到端目标拆分的工程预算，
            非 PRD 明文数值；调整必须同步 <code>docs/HARNESS_DESIGN.md</code>。
          </span>
        </div>
      </div>
    </div>
  );
}