/** 三端门卡：职责、角色、技术栈、能力清单与真的「进入」链接。 */
import { ENDS } from "../baselines";
import { useReveal } from "../hooks";
import { IconArrowRight, IconConsole, IconPhone, IconTablet } from "./Icons";

const GLYPH = {
  pad: IconTablet,
  console: IconConsole,
  family: IconPhone,
} as const;

export function EndCards() {
  const ref = useReveal<HTMLDivElement>();
  return (
    <div className="p-grid-3 p-reveal" ref={ref}>
      {ENDS.map((end) => {
        const Glyph = GLYPH[end.key];
        return (
          <article className="p-card p-end" data-accent={end.accent} key={end.key}>
            <div className="p-end-top">
              <div>
                <h3 className="p-end-name">{end.name}</h3>
                <p className="p-end-audience">{end.audience}</p>
              </div>
              <span className="p-end-glyph">
                <Glyph />
              </span>
            </div>

            <p className="p-end-duty">{end.duty}</p>

            <div className="p-meta">
              <span className="p-tag">{end.viewport}</span>
              <span className="p-tag">{end.stack}</span>
            </div>

            <ul className="p-caps">
              {end.capabilities.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>

            <div className="p-end-foot">
              <a className="p-btn p-btn-primary" href={end.href}>
                {end.hrefLabel}
                <IconArrowRight />
              </a>
              <span className="p-path">{end.href}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}