/**
 * 长者切换器（W4 §5）。
 *
 * 规则：**只有一位长者时不渲染**（避免无意义的单选项），多于一位时用胶囊 tab。
 * 姓名只能从日报的 `elder_name` 推出来（`/api/elders` 对家属 403），
 * 因此可能还没解析到 —— 此时显示「家人 1／家人 2」，绝不显示 UUID。
 *
 * 375px：容器自身横向滚动（`overflow-x: auto`），不撑宽 document。
 * 每个 tab 用 `aria-pressed` 表达选中态，文字常显，不只靠颜色。
 */

interface ElderSwitcherProps {
  elderIds: string[];
  names: Record<string, string>;
  activeId: string | null;
  onChange: (elderId: string) => void;
}

export function ElderSwitcher({ elderIds, names, activeId, onChange }: ElderSwitcherProps) {
  if (elderIds.length <= 1) return null;

  return (
    <div className="elder-switcher" role="group" aria-label="切换查看的长者">
      {elderIds.map((elderId, index) => {
        const selected = elderId === activeId;
        const name = names[elderId] ?? `家人 ${String(index + 1)}`;
        return (
          <button
            key={elderId}
            type="button"
            className={selected ? "elder-tab is-active" : "elder-tab"}
            aria-pressed={selected}
            onClick={() => onChange(elderId)}
          >
            {name}
          </button>
        );
      })}
    </div>
  );
}
