/**
 * 角色权限矩阵 + 演示数据与重建。
 *
 * **口令刻意不进产物**（AGENTS §4-3、计划 S-8 / R-14）：只展示角色、入口、账号与可见范围，
 * 口令走 docs/demo-accounts.md 链接。pad 一键填入复用既有 GET /api/pad/demo-login
 * （APP_ENV=production 恒 404），门户不复制那份凭据。
 */
import { DEMO_DATA, DEMO_DEVICE, ROLES } from "../baselines";
import { copyText, useReveal } from "../hooks";
import { IconCopy, IconInfo, IconShield } from "./Icons";

export function RoleMatrix({ onToast }: { onToast: (msg: string) => void }) {
  const ref = useReveal<HTMLDivElement>();

  const copy = async (text: string, label: string) => {
    const ok = await copyText(text);
    onToast(ok ? `已复制${label}：${text}` : "复制失败，请手动选中");
  };

  return (
    <div className="p-reveal" ref={ref}>
      <div className="p-table-wrap">
        <table className="p-table">
          <caption className="sr-only">四端演示角色、入口、账号与可见范围</caption>
          <thead>
            <tr>
              <th scope="col">角色</th>
              <th scope="col">入口</th>
              <th scope="col">账号</th>
              <th scope="col">能看到什么</th>
              <th scope="col">边界</th>
            </tr>
          </thead>
          <tbody>
            {ROLES.map((r) => (
              <tr key={r.role}>
                <td>
                  <span className="p-badge" data-tone={r.tone}>{r.role}</span>
                </td>
                <td>
                  <a className="p-path" href={r.entry} style={{ color: "var(--color-brand-strong)" }}>
                    {r.entry}
                  </a>
                </td>
                <td>
                  <span className="p-copy-row">
                    <span className="p-mono p-num">{r.account}</span>
                    <button
                      type="button"
                      className="p-btn p-btn-ghost p-btn-sm"
                      onClick={() => void copy(r.account, "账号")}
                      aria-label={`复制${r.role}账号`}
                    >
                      <IconCopy />
                    </button>
                  </span>
                </td>
                <td>{r.sees}</td>
                <td className="p-table-strong">{r.cannot}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="p-callout">
        <IconShield />
        <span>
          <strong>口令不在本页。</strong>四份演示口令与设备密钥只存在于{" "}
          <code>docs/demo-accounts.md</code> 与 <code>scripts/seed_demo.py</code>，
          按 AGENTS §4-3 不编进任何前端产物。它们都是弱口令、仅限本机；
          <code>seed_demo.py</code> 有硬闸（DATABASE_URL 主机不在 localhost/127.0.0.1/postgres 直接退出），
          <code>GET /api/pad/demo-login</code> 在 <code>APP_ENV=production</code> 恒 404。
          真部署请删掉演示数据，改用 <code>scripts/create_account.py</code> 建强口令账号。
        </span>
      </div>
    </div>
  );
}

export function DemoData({ onToast }: { onToast: (msg: string) => void }) {
  const ref = useReveal<HTMLDivElement>();

  const copy = async (text: string, label: string) => {
    const ok = await copyText(text);
    onToast(ok ? `已复制${label}` : "复制失败，请手动选中");
  };

  return (
    <div className="p-split p-reveal" ref={ref}>
      <div className="p-card p-panel">
        <h3 className="p-panel-title">演示数据里有什么</h3>
        <div className="p-kv">
          {DEMO_DATA.map((d) => (
            <div className="p-kv-row" key={d.label}>
              <span className="p-kv-k">
                {d.label} <span className="p-badge" data-tone="brand">{d.value}</span>
              </span>
              <span className="p-kv-v">{d.detail}</span>
            </div>
          ))}
        </div>
        <div className="p-callout" data-tone="info">
          <IconInfo />
          <span>
            健康记录<strong>故意缺第 4 天与第 9 天</strong>，家属端趋势图断线是刻意的——
            用来验证「缺采如实呈现」而不是补值造平滑曲线。
          </span>
        </div>
      </div>

      <div className="p-card p-panel">
        <h3 className="p-panel-title">长者端设备凭证</h3>
        <div className="p-kv">
          <div className="p-kv-row">
            <span className="p-kv-k">设备编号</span>
            <span className="p-kv-v">
              <span className="p-copy-row">
                <span className="p-mono">{DEMO_DEVICE.code}</span>
                <button
                  type="button"
                  className="p-btn p-btn-ghost p-btn-sm"
                  onClick={() => void copy(DEMO_DEVICE.code, "设备编号")}
                  aria-label="复制设备编号"
                >
                  <IconCopy />
                </button>
              </span>
            </span>
          </div>
          <div className="p-kv-row">
            <span className="p-kv-k">绑定长者</span>
            <span className="p-kv-v">{DEMO_DEVICE.elder}</span>
          </div>
          <div className="p-kv-row">
            <span className="p-kv-k">数字人形象</span>
            <span className="p-kv-v">{DEMO_DEVICE.persona}</span>
          </div>
          <div className="p-kv-row">
            <span className="p-kv-k">设备密钥</span>
            <span className="p-kv-v">
              不在本页。库里只有 <code>devices.token_hash</code>（sha256），无法反查明文。
            </span>
          </div>
        </div>
        <p className="p-note">{DEMO_DEVICE.note}</p>

        <h3 className="p-panel-title" style={{ marginTop: 28 }}>重建 / 重置</h3>
        <span className="p-copy-row">
          <span className="p-mono">uv run python scripts/seed_demo.py</span>
          <button
            type="button"
            className="p-btn p-btn-secondary p-btn-sm"
            onClick={() => void copy("uv run python scripts/seed_demo.py", "重建命令")}
          >
            <IconCopy />
            复制命令
          </button>
        </span>
        <p className="p-note">
          脚本幂等：固定 UUID、按业务唯一键认领既有行、口令未变不重算哈希、
          <code>token_version</code> 不动（不会把已登录会话踢下线）。第二次跑输出应全为{" "}
          <code>unchanged</code>。
        </p>
        <div className="p-callout" data-tone="danger">
          <span>
            <strong>跑测试前先设独立库。</strong>仓内部分夹具会对 <code>DATABASE_URL</code> 执行
            TRUNCATE，裸跑全量会清掉演示数据（AGENTS §9 第三条，一天内踩过三次）。
            先设 <code>ANYU_TEST_DATABASE_URL</code> 指向 <code>anyu_test</code>。
          </span>
        </div>
      </div>
    </div>
  );
}