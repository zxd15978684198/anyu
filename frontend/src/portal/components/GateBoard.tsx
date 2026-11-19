/**
 * 门禁看板 + 产物体积 + 服务拓扑 + 硬性数值。
 *
 * 纪律（计划 S-10）：**红灯不美化**。后端 56 errors、真链路 0/20 都如实呈现；
 * 覆盖率注明是转记 Lead 实测而非本页自跑；每条带来源与提交号。
 */
import { ARTIFACTS, GATES, HARD_NUMBERS, PORTAL_BUILD, SERVICES } from "../baselines";
import { useReveal } from "../hooks";
import { IconCheck, IconInfo, IconPulse } from "./Icons";

const KIND: Record<string, string> = {
  pad: "pad",
  console: "console",
  family: "family",
  portal: "shared",
  "共享 client": "shared",
  "vrm chunk": "pad",
};

export function GateBoard() {
  const ref = useReveal<HTMLDivElement>();
  const maxJs = Math.max(...ARTIFACTS.map((a) => a.js));

  return (
    <div className="p-reveal" ref={ref}>
      <div className="p-gates">
        {GATES.map((g) => (
          <div className="p-card p-gate" data-state={g.state} key={g.name}>
            <div className="p-gate-top">
              <span className="p-gate-name">{g.name}</span>
              <span className="p-gate-value">{g.value}</span>
            </div>
            <p className="p-gate-detail">{g.detail}</p>
            <p className="p-gate-src">
              <span className="p-badge" data-tone={g.source.kind === "measured" ? "success" : "neutral"}>
                {g.source.kind === "measured" ? "实测" : g.source.kind === "reported" ? "转记" : g.source.kind}
              </span>{" "}
              {g.source.from}
              {g.source.at ? ` @ ${g.source.at}` : ""}
            </p>
          </div>
        ))}
      </div>

      <div className="p-callout" data-tone="danger">
        <IconInfo />
        <span>
          <strong>两处红灯刻意保留。</strong>后端全量仍有 <strong>56 errors</strong>（沙箱 tmp_path 与
          事件循环残留，非本轮引入但也没清零）；真链路首包 <strong>0/20</strong> 达标，TTS 20/20 撞满
          1s 超时档。后者正是「流式化」立项的依据。把它们美化成全绿，评审一问就穿。
        </span>
      </div>

      <div className="p-split" style={{ marginTop: 32 }}>
        <div className="p-card p-panel">
          <h3 className="p-panel-title">
            <IconPulse size={17} /> 产物体积与依赖隔离
          </h3>
          <div className="p-bars">
            {ARTIFACTS.map((a) => (
              <div className="p-bar-row" key={a.name}>
                <span className="p-bar-name">{a.name}</span>
                <span className="p-bar-track">
                  <span
                    className="p-bar-fill"
                    data-kind={KIND[a.name] ?? "shared"}
                    style={{ width: `${(a.js / maxJs) * 100}%` }}
                  />
                </span>
                <span className="p-bar-value">{a.js.toFixed(2)} kB</span>
              </div>
            ))}
          </div>
          <div className="p-table-wrap" style={{ marginTop: 20 }}>
            <table className="p-table">
              <caption className="sr-only">各入口 JS/CSS 体积与 VRM 标记数</caption>
              <thead>
                <tr>
                  <th scope="col">入口</th>
                  <th scope="col">JS</th>
                  <th scope="col">CSS</th>
                  <th scope="col">VRM 标记</th>
                </tr>
              </thead>
              <tbody>
                {ARTIFACTS.map((a) => (
                  <tr key={a.name}>
                    <td className="p-table-strong">{a.name}</td>
                    <td className="p-num">{a.js.toFixed(2)} kB</td>
                    <td className="p-num">{a.css > 0 ? `${a.css.toFixed(2)} kB` : "—"}</td>
                    <td>
                      <span className="p-badge" data-tone={a.vrm === 0 ? "success" : "brand"}>
                        {a.vrm === 0 && <IconCheck size={13} />}
                        {a.vrm}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="p-note">
            PRD line 88 硬约束：pad 的 three-vrm 不得进 console/family 构建包。
            当前隔离靠 import 图成立，<strong>产物层断言仍是缺口</strong>（前端计划 W0 补）。
          </p>
        </div>

        <div className="p-card p-panel">
          <h3 className="p-panel-title">服务拓扑（compose 一键起）</h3>
          <div className="p-table-wrap">
            <table className="p-table">
              <caption className="sr-only">服务与端口</caption>
              <thead>
                <tr>
                  <th scope="col">服务</th>
                  <th scope="col">端口</th>
                  <th scope="col">说明</th>
                </tr>
              </thead>
              <tbody>
                {SERVICES.map((s) => (
                  <tr key={s.name}>
                    <td className="p-table-strong">{s.name}</td>
                    <td className="p-num">{s.port}</td>
                    <td>{s.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="p-note">
            业务表与 LangGraph checkpoint <strong>物理隔离</strong>（独立 schema{" "}
            <code>langgraph</code>、独立连接池）；除天气 API 外所有外部系统走 Mock，但 Mock 落库可查。
          </p>
        </div>
      </div>

      <div className="p-card p-panel" style={{ marginTop: 24 }}>
        <h3 className="p-panel-title">硬性数值（AGENTS §7 · 写死在常量里，不得私自调整）</h3>
        <div className="p-table-wrap">
          <table className="p-table">
            <caption className="sr-only">硬性数值与出处</caption>
            <thead>
              <tr>
                <th scope="col">参数</th>
                <th scope="col">值</th>
                <th scope="col">出处</th>
              </tr>
            </thead>
            <tbody>
              {HARD_NUMBERS.map((h) => (
                <tr key={h.k}>
                  <td className="p-table-strong">{h.k}</td>
                  <td className="p-num">{h.v}</td>
                  <td>{h.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="p-note">
          本表是<strong>实施口径</strong>：与 PRD 正文同一个数字不一致时以本表为准（AGENTS 前置·铁律 7）。
          要按 PRD 调整某个数值，先改 §7 并写明理由与影响面，再改代码。门户只展示，不提供修改入口。
        </p>
      </div>

      <p className="p-note" style={{ marginTop: 20 }}>
        基线提交 <code>{PORTAL_BUILD.baselineCommit}</code> · 门禁提交{" "}
        <code>{PORTAL_BUILD.gateCommit}</code> · 快照日期 {PORTAL_BUILD.date}
      </p>
    </div>
  );
}