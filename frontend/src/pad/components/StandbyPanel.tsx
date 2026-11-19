/**
 * 待机面板：字幕为空时右栏显示的内容（FR-UI-02 的「静默态」）。
 *
 * 为什么不是只放一句「我在听」：字幕改成整列之后，待机时那一列有 500px 高，
 * 一句话漂在正中看着像「页面没加载完」。这里把长者最常问的四件事做成可直接按的
 * 大按钮 —— 不用打字、不用记唤醒词，按一下就是一轮真实对话。
 *
 * 四句话**刻意选的是规则层固定意图的原词**（`app/router/rules.py`）：
 * 今天天气怎么样 → weather_query、现在几点了 → ask_time、
 * 我想听越剧 → play_yueju、我要吃药 → medication_take。
 * 走 L0 直路由（≤200ms，PRD §5.3），不消耗 L2 预算，也不会因为模型没起而按不动。
 * 换词前先回去核对正则，改成「放个戏」这类没命中的说法就白白掉到 L2 了。
 *
 * 发送走 App 的 `submitTextTurn`，与打字框同一条链路（同一份短期历史、同一套安全规则），
 * 这里不自己发请求、不编回复。
 */

import { useState } from "react";

/** 顺序即视觉顺序：先「问一句」，再「听一段」，最后才是任务类。 */
const QUICK_ASKS = [
  { text: "今天天气怎么样", hint: "天气" },
  { text: "现在几点了", hint: "报时" },
  { text: "我想听越剧", hint: "戏曲" },
  { text: "我要吃药", hint: "用药" },
] as const;

export interface StandbyPanelProps {
  onAsk: (text: string) => void;
  /**
   * 主入口：打开今日药单（S6，`onOpenMedications` 可选 —— 不传就不渲染）。
   *
   * 为什么它会排在四个常问句**之上**、而且是唯一的品牌色实心按钮：待机时长者最常做的
   * 决定不是「问一句」而是「看看今天吃什么药」。四个常问句走 L0 直路由、回一句话；
   * 药单是一块要看的清单（`TodayMedications`），形态不同，所以给主按钮的位置与配色，
   * 但**不混进那四格** —— 混进去会让人以为按了就「读一条」，实际是打开面板。
   *
   * 为什么是可选 prop：`App.tsx` 的接线单独进行；未接线时待机屏必须与今天**完全一样**，
   * 不能因为组件升级就先冒出一颗按不动的按钮。
   */
  onOpenMedications?: () => void;
  /**
   * 次级入口：打开戏曲面板（2026-11-12，FR-CON-03）。
   *
   * 为什么不当成第五个「常问句」按钮：那四个按钮走 L0 规则层直路由，回答是一条文字+
   * 一段 TTS；唱戏要走 `/api/media/*` 并开一条播放历史，是**另一种形态**，混进同一排
   * 会让人以为「按了就会立刻唱起来」，也把 L0 直路由的语义（≤200ms、不花模型预算）搅乱。
   * 所以它单独一个次要按钮，文案用「听一段戏」，按下去先挑一段再开面板。
   */
  onOpenMedia?: (kind: "opera" | "movie") => void;
  /**
   * 次级入口：打开「您让我记住的」面板（FR-MEM-02）。
   *
   * 和「听一段戏」同一排、同一个理由：这是长者主动查看的自助动作，不是一句话问答。
   * 文案刻意用长者的语言（「您让我记住的」）而不是「长期记忆」——后者是工程词。
   */
  onOpenMemory?: () => void;
}

export function StandbyPanel({
  onAsk,
  onOpenMedications,
  onOpenMedia,
  onOpenMemory,
}: StandbyPanelProps) {
  // 按下去到回复上屏之间有一小段空窗，不锁住会被连点成两三轮
  const [asked, setAsked] = useState<string | null>(null);

  return (
    <div
      data-testid="standby-panel"
      className="pad-transcript flex min-h-0 flex-1 flex-col justify-center gap-5 overflow-y-auto"
    >
      <div className="shrink-0">
        <p
          className="font-bold leading-snug text-ink"
          style={{ fontSize: "var(--pad-ask-title)" }}
        >
          我在听，您说一句就行
        </p>
        <p className="mt-1 text-2xl leading-snug text-ink-muted">
          不方便说话时，也可以直接按下面一句
        </p>
      </div>
      {/* 主按钮：待机时最常做的那件事。实心品牌色 + 64px 高，视觉上比下面四格更重；
          与四个常问句分成两块（不是塞成第五格）——它打开的是清单，不是一问一答。 */}
      {onOpenMedications && (
        <button
          type="button"
          data-testid="open-medications"
          onClick={onOpenMedications}
          className="flex min-h-[64px] w-full shrink-0 items-center justify-center rounded-2xl bg-brand px-4 font-bold leading-tight text-ink-inverse transition-colors duration-150 active:bg-brand-strong"
          style={{ fontSize: "var(--pad-ask-text)" }}
        >
          看看今天的药
        </button>
      )}
      <div className="grid shrink-0 grid-cols-2 gap-3">
        {QUICK_ASKS.map((ask) => {
          const pending = asked === ask.text;
          return (
            <button
              key={ask.text}
              type="button"
              data-testid="quick-ask"
              data-ask={ask.hint}
              disabled={asked !== null}
              onClick={() => {
                setAsked(ask.text);
                onAsk(ask.text);
              }}
              className="flex min-h-[var(--pad-ask-h)] items-center justify-center rounded-2xl border-2 border-brand-line bg-brand-soft px-3 text-center font-bold leading-tight text-ink transition-colors duration-150 active:bg-brand-line disabled:border-line disabled:bg-surface-sunken disabled:text-ink-subtle"
              style={{ fontSize: "var(--pad-ask-text)" }}
            >
              {pending ? "正在问…" : ask.text}
            </button>
          );
        })}
      </div>
      {onOpenMedia && (
        <>
          <button
            type="button"
            data-testid="open-media"
            onClick={() => onOpenMedia("opera")}
            className="flex min-h-[var(--pad-ask-h)] shrink-0 items-center justify-center rounded-2xl border-2 border-line bg-surface px-3 text-center font-bold leading-tight text-ink-muted transition-colors duration-150 active:bg-surface-sunken"
            style={{ fontSize: "var(--pad-ask-text)" }}
          >
            听一段戏
          </button>
          {/* 电影（FR-CON-04）与戏曲同一个入口排布：都是「挑一段 → 播 → 记账」，
              只有 kind 不同。分成两个按钮而不是二级菜单——长者端多点一次就多一次走错。 */}
          <button
            type="button"
            data-testid="open-movie"
            onClick={() => onOpenMedia("movie")}
            className="flex min-h-[var(--pad-ask-h)] shrink-0 items-center justify-center rounded-2xl border-2 border-line bg-surface px-3 text-center font-bold leading-tight text-ink-muted transition-colors duration-150 active:bg-surface-sunken"
            style={{ fontSize: "var(--pad-ask-text)" }}
          >
            看个电影
          </button>
        </>
      )}
      {onOpenMemory && (
        <button
          type="button"
          data-testid="open-memory"
          onClick={onOpenMemory}
          className="flex min-h-[var(--pad-ask-h)] shrink-0 items-center justify-center rounded-2xl border-2 border-line bg-surface px-3 text-center font-bold leading-tight text-ink-muted transition-colors duration-150 active:bg-surface-sunken"
          style={{ fontSize: "var(--pad-ask-text)" }}
        >
          您让我记住的
        </button>
      )}
    </div>
  );
}