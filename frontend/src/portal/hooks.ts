/** 门户共用 hooks：滚动揭示、顶栏高亮、轻提示。无外部依赖。 */
import { useCallback, useEffect, useRef, useState } from "react";

/** 元素进入视口时加 .is-visible。
 *
 *  **可见性绝不押在 IntersectionObserver 上**：页面处于 hidden / 未获焦点时
 *  IO 与 rAF 都可能不触发，若 CSS 默认 opacity:0 就会整块内容永久看不见
 *  （实测踩过：5 个区块 4 个 opacity=0）。所以这里给一个**保底定时器**，
 *  与 IO 谁先到谁生效；CSS 侧默认可见，只有 armed 后才隐藏。 */
export function useReveal<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const reveal = () => node.classList.add("is-visible");
    // 只有 JS 确实在跑时才 armed（CSS 侧默认不隐藏），双保险。
    node.classList.add("is-armed");
    if (typeof IntersectionObserver === "undefined") {
      reveal();
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            reveal();
            io.unobserve(entry.target);
          }
        }
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.08 },
    );
    io.observe(node);
    const fallback = window.setTimeout(reveal, 1200);
    return () => {
      io.disconnect();
      window.clearTimeout(fallback);
    };
  }, []);
  return ref;
}

/** 顶栏当前区块高亮：取「视口上部 1/3 处」落在哪个区块。
 *  **不用 rAF 去抖**——隐藏页里 rAF 不触发，高亮会永远停在初始值（实测踩过）。
 *  改用时间戳节流，scroll 事件本身在隐藏页仍会派发。 */
export function useScrollSpy(ids: readonly string[]): string {
  const [active, setActive] = useState(ids[0] ?? "");
  useEffect(() => {
    let last = 0;
    const compute = () => {
      const probe = window.innerHeight * 0.34;
      let current = ids[0] ?? "";
      for (const id of ids) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (el.getBoundingClientRect().top <= probe) current = id;
      }
      setActive((prev) => (prev === current ? prev : current));
    };
    const onScroll = () => {
      const now = Date.now();
      if (now - last < 32) return;
      last = now;
      compute();
    };
    // 点锚点导航时**立即**按 hash 高亮：hashchange 在平滑滚动落定前就触发，
    // 此时按位置重算会算到旧区块；而某些环境（隐藏页 / 内嵌浏览器）根本不派发
    // scroll 事件，就再也没机会校正。所以先认 hash，再延时按实际位置复核。
    const onHash = () => {
      const id = window.location.hash.replace("#", "");
      if (ids.includes(id)) setActive(id);
      window.setTimeout(compute, 650);
    };
    compute();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      window.removeEventListener("hashchange", onHash);
    };
  }, [ids]);
  return active;
}

/** 轻提示：同一条消息重复触发会重置计时，不会堆叠。 */
export function useToast(): [string | null, (msg: string) => void] {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const show = useCallback((next: string) => {
    setMsg(next);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMsg(null), 2200);
  }, []);
  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current);
    },
    [],
  );
  return [msg, show];
}

/** 复制到剪贴板。
 *  `navigator.clipboard` 在文档未获焦点 / 权限被拒时会 reject（内嵌浏览器常见），
 *  所以必须有 execCommand 兜底——否则「复制」按钮成了死按钮，只提示用户自己选中。 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 落到 execCommand 兜底 */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/** 是否偏好减少动效（沙盘播放据此改为直接落到终值）。 */
export function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}