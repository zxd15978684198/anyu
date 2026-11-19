/**
 * 唤醒词 → sherpa-onnx KWS token 序列（task-70，让「小伴」以外的词也能用）。
 *
 * 为什么不用 npm 拼音库：AGENTS §4 第 12 条不许擅自加依赖；而这里只需要「汉字 → 带调拼音」
 * 这一张静态查表，运行期不需要分词、多音词纠错之类的能力。表由 pypinyin 离线生成后固化
 * 在 `pinyin-data.ts`（纯字符串，运行时零依赖），覆盖 GB2312 全部 6763 个汉字（1220 个读音）。
 *
 * 切分口径（与 `frontend/public/kws-assets/model/tokens.txt` 一一对应）：
 *   - 词表是「声母 + 带调韵母」的混合表：`x iǎo b àn` 才是「小伴」；
 *   - 所以先把拼音切成 声母 + 韵母，两段都在词表里才算数；
 *   - 没有声母的音节（阿 ā、安 ān、儿 ér）整段就是一个 token；
 *   - 任何一步落空就返回 null——调用方据此提示「该词暂不支持」，**不要**用近音字凑合：
 *     凑出来的 token 序列模型没学过，唤醒率会从 100% 掉到随机。
 *
 * 已知限制（都已在设置页文案/注释里体现，并非 bug）：
 *   1. 多音字取首读音。`长` 会被读成 cháng——若用户想要 zhǎng，当前表给不出来；
 *   2. GB2312 之外的生僻字（如 `𠮷`、`囧` 之外的扩展区字）不在表内 → null；
 *   3. 一个字对应多个 token 的情况（hm/ń 这类叹词）不参与唤醒词；
 *   4. 唤醒词超过 8 个字直接拒绝：KWS 的 `maxActivePaths=4` 是按 2~4 字调的，
 *      长句唤醒率没有实测数据，宁可让用户换短词，也不假装支持。
 */

import { SYLLABLE_CHARS } from "./pinyin-data";

/** 出厂唤醒词：与 T1 的 AC-42 实测口径一致（20/20 命中，中位 800ms） */
export const DEFAULT_WAKE_WORD = "小伴";

/** 设置页预设：都是 1~2 个音节，且已用单测钉住「可切分」 */
export const PRESET_WAKE_WORDS: readonly string[] = ["小伴", "小银", "小安", "阿福", "康康", "小欢"];

/** 唤醒词最长音节数（约等于字数）——超过就不让存，理由见文件头第 4 条 */
export const MAX_WAKE_WORD_CHARS = 8;

/**
 * `tokens.txt` 里的拼音 token（191 个：27 个声母 + 164 个带调/不带调韵母）。
 *
 * 为什么要抄一份进源码：`splitKeyword` 必须是同步的（设置页要在按键时立刻给中文提示），
 * 而 tokens.txt 在 public 下只能 fetch 成异步。这份常量与文件的一致性由
 * `frontend/tests/wake-word.test.ts` 的「词表对齐」用例守着——词表变了那里会红。
 */
const PINYIN_TOKENS: ReadonlySet<string> = new Set(
  (
    "a ai an ang ao b c ch d e ei en eng er f g h hm i ia ian iang iao ie in ing iu " +
    "ià iàn iàng iào iá ián iáng iáo iè ié iòng ióng iù iú iā iān iāng iāo iē iě iōng iū " +
    "iǎ iǎn iǎng iǎo iǒng iǔ j k l m n o ong ou p q r s sh t u ua uai uan uang ue ui un uo " +
    "uà uài uàn uàng uá uái uán uáng uè ué uì uí uò uó uā uāi uān uāng uē uě uī uō " +
    "uǎ uǎi uǎn uǎng uǐ uǒ w x y z zh " +
    "à ài àn àng ào á ái án áng áo è èi èn èng èr é éi én éng ér ì ìn ìng í ín íng " +
    "ò òng òu ó óng óu ù ùn ú ún üè üě ā āi ān āng āo ē ēi ēn ēng ě ěi ěn ěng ěr " +
    "ī īn īng ń ň ō ōng ōu ū ūn ǎ ǎi ǎn ǎng ǎo ǐ ǐn ǐng ǒ ǒng ǒu ǔ ǔn ǘ ǚ ǜ ǹ ḿ"
  ).split(" "),
);

/** 声母按长度降序：`zh` 必须比 `z` 先试，否则 `zhōng` 会被切成 z + hōng（h 不是韵母） */
const INITIALS: readonly string[] = [
  "zh",
  "ch",
  "sh",
  "b",
  "p",
  "m",
  "f",
  "d",
  "t",
  "n",
  "l",
  "g",
  "k",
  "h",
  "j",
  "q",
  "x",
  "r",
  "z",
  "c",
  "s",
  "y",
  "w",
];

/** 汉字 → 首读音；首次调用时解析 28KB 的表，之后复用（模块级，不放进组件 state） */
let charTable: Map<string, string> | null = null;

function table(): Map<string, string> {
  if (charTable) return charTable;
  const map = new Map<string, string>();
  for (const group of SYLLABLE_CHARS.split("|")) {
    const space = group.indexOf(" ");
    if (space <= 0) continue;
    const syllable = group.slice(0, space);
    // 一个字只记首读音：`Array.from` 按码点遍历，避免代理对被逐个字符拆开
    for (const ch of Array.from(group.slice(space + 1))) {
      if (!map.has(ch)) map.set(ch, syllable);
    }
  }
  charTable = map;
  return map;
}

/** 切分失败的原因；`message` 直接给长者看（中文、点名到字） */
export interface KeywordProblem {
  readonly code: "empty" | "too_long" | "unknown_char" | "unknown_token";
  readonly message: string;
}

export interface KeywordSplit {
  readonly word: string;
  /** 每个字的带调拼音（与汉字一一对应，供日志/错词排查） */
  readonly syllables: readonly string[];
  /** 送进 `createKeywordSpotter` 的 token 序列 */
  readonly tokens: readonly string[];
}

function splitSyllable(syllable: string): string[] | null {
  for (const initial of INITIALS) {
    if (!syllable.startsWith(initial)) continue;
    const final = syllable.slice(initial.length);
    // 韵母必须在词表里：`zh` + `ōng` 成立，`z` + `hōng` 不成立（hōng 不是 token）
    if (final && PINYIN_TOKENS.has(final)) return [initial, final];
  }
  // 零声母音节（阿 ā、安 ān、儿 ér）整段就是一个 token
  return PINYIN_TOKENS.has(syllable) ? [syllable] : null;
}

function analyse(word: string): { split: KeywordSplit | null; problem: KeywordProblem | null } {
  const trimmed = word.trim().normalize("NFC");
  if (trimmed.length === 0) {
    return { split: null, problem: { code: "empty", message: "请先输入唤醒词" } };
  }
  const chars = Array.from(trimmed);
  if (chars.length > MAX_WAKE_WORD_CHARS) {
    return {
      split: null,
      problem: {
        code: "too_long",
        message: `唤醒词最多 ${MAX_WAKE_WORD_CHARS} 个字，短一点更容易唤醒`,
      },
    };
  }

  const syllables: string[] = [];
  const tokens: string[] = [];
  for (const ch of chars) {
    const syllable = table().get(ch);
    if (!syllable) {
      return {
        split: null,
        problem: {
          code: "unknown_char",
          message: `「${ch}」这个字还不在词表里，请换一个词`,
        },
      };
    }
    const pieces = splitSyllable(syllable);
    if (!pieces) {
      return {
        split: null,
        problem: {
          code: "unknown_token",
          message: `「${ch}」的读音「${syllable}」不在唤醒词表里，请换一个字`,
        },
      };
    }
    syllables.push(syllable);
    tokens.push(...pieces);
  }

  return { split: { word: trimmed, syllables, tokens }, problem: null };
}

/**
 * 把唤醒词切成 `tokens.txt` 口径的 token 序列；不可切分时返回 null。
 *
 * 同步实现：设置页要在点击「保存」的同一帧给出中文提示，异步 fetch 词表来不及。
 */
export function splitKeyword(word: string): string[] | null {
  const { split } = analyse(word);
  return split ? [...split.tokens] : null;
}

/** 同上，但保留逐字拼音（日志与「为什么切不出来」排查用） */
export function splitKeywordDetail(word: string): KeywordSplit | null {
  return analyse(word).split;
}

/** 不可切分时的中文原因；可切分返回 null。设置页直接显示这句，不自己拼话术。 */
export function describeKeywordProblem(word: string): string | null {
  return analyse(word).problem?.message ?? null;
}

/** 是否可作为唤醒词（等价于 `splitKeyword(word) !== null`，但读起来更像意图） */
export function isSupportedWakeWord(word: string): boolean {
  return analyse(word).split !== null;
}

/**
 * 出厂 token 序列：由 `splitKeyword(DEFAULT_WAKE_WORD)` 现算，不再手写一遍。
 *
 * kws.ts 里保留同名导出是为了既有用例（`tests/kws.test.ts`）与向后兼容；
 * 改 `DEFAULT_WAKE_WORD` 时这里自动跟着变，不会出现「常量与词表对不上」的陈旧数据。
 */
export const DEFAULT_WAKE_TOKENS: readonly string[] =
  splitKeyword(DEFAULT_WAKE_WORD) ?? /* istanbul ignore next */ [];
