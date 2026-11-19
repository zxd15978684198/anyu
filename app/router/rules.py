"""L0 · 高频固定意图直路由（迭代 6 计划 §4 W1；AGENTS §6 L0 行）。

``app/router/rules.py`` 规则条数：**7**；加上 ``safety.py`` 一票否决 **3** 条，
两文件合计 **10** 条 ≤ 10 条硬上限（AGENTS §7、迭代 6 计划 §4 W1；只做减法）。
7 条对应 迭代 6 计划 W1 固定意图清单（换一段/大声点/小声点/停/越剧/京剧/几点了/
今天天气/我要吃药/再见/重复一遍），其中「大声/小声」合 1 条、「换一段/停/暂停/继续」
合 1 条（播放控制）、「越剧/京剧」合 1 条、「几点了/今天天气」合 1 条（信息问答）——
同一条目内分支各自直出**动作级** intent。**条数口径是 `DirectRule` 的条数**（7），
分支数不是条数：2026-11-12 的 FR-CON-03 就是往 `play_control` 里加两个分支（pause /
resume），条数不变、仍 ≤10。

对外 intent 契约（调用方按 intent 执行直路由动作；W2/W4 消费）：

===========================  =============
intent                       动作
===========================  =============
play_next                    换下一段节目
stop                         停止当前播放/播报
pause                        暂停（FR-CON-03；pad 播放器只认 next/stop，见 rule_replies）
resume                       继续播放（同上）
volume_up / volume_down      调大 / 调小音量
play_yueju / play_jingju     点播越剧 / 京剧
ask_time                     报时
weather_query                查今天天气
medication_take              进用药提醒流程
farewell                     道别收尾
repeat                       重复上一句/重放一遍
===========================  =============

> `health`（身体不适主诉）自 2026-11-13 task-43 起**不再由本层直出**：「头晕/眩晕/
> 头昏/眼前发黑/站不稳/心慌/胸口闷」已并入 `safety.py` 的急症词族，命中即由安全
> 一票否决短路为 `alert`（落 risk_events + 红/黄复核单）。`health` intent 仍在
> `INTENT_WHITELIST` 里，由 L2 主诉识别与健康采集子图消费。

工程口径
- 每条=关键词或可编译正则，**写死在代码**（不进 prompt、不进配置；AGENTS §5）。
  不确定的词不在本层扩义——未覆盖的说法留给 L1（System One）分类。
- 固定意图采用**全句锚定**（fullmatch）：只认「整句就是这条指令 + 常见语气词/
  应词」的说法，防止「停下来聊聊」「十一点了」这类半句误直路由；整句未锚定命中
  时，再按中英文标点（，,。！？?；;）**分句**逐句 fullmatch——每个分句仍整段锚定，
  复合句「换一段，刚才那出不好听」里的指令子句可直路由，半句修饰语依旧漏到
  L1/L2，属于可接受降级（AGENTS §6 分层职责；迭代 6 评测回流 21 条已并入）。
- 扫描顺序由 ``match_l0`` 保证：**safety 一票否决(3) 先于本文件(7)**。
- 本模块纯文本、纯函数、无 I/O；`route_ms` 归编排层观测（route 计时不在此层）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from app.router.decision import LAYER_L0, RouteDecision
from app.router.safety import match_safety

# 本文件规则条数：7（全 router 合计 10，≤10 上限 AGENTS §7 / 迭代 6 计划 §4 W1）
RULE_COUNT = 7

# 前置应词/语气词（ASR 常见起头：嗯、哦……），后接可选请求前缀（请/再/给我……）。
_LEAD = r"(?:(?:嗯|哦|哎|呃|诶)+[，,、]?\s*)?"
_REQ = r"(?:(?:请|麻烦你?|劳驾)?(?:再)?(?:给我|帮?我)?\s*)?"
# 后置语气词与标点（吗 问句语气词也在其中）。
_TAIL = r"(?:[吧呀啦呗哦噢哈啊呢嘛吗]|好不?|好不好|可以吗?)?\s*[!！。~？?]*$"

# 复合句分句符：整句 fullmatch 未命中时按它切分，逐分句再 fullmatch（口径见 docstring）。
_CLAUSE_SPLIT = re.compile(r"[，,。！？?；;]")


def _compile(core: str) -> re.Pattern[str]:
    """把直路由核心词包成全句锚定的可编译正则：lead + req + core + tail。"""
    return re.compile(f"{_LEAD}{_REQ}{core}{_TAIL}")


@dataclass(frozen=True, slots=True)
class DirectBranch:
    """一条目内的一个分支：整句命中该正则即直出该 ``intent``。"""

    intent: str
    label: str  # 中文一句话：给人看的示例词
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class DirectRule:
    """一条固定意图直路由规则（≥1 个分支）；整份 ``DIRECT_RULES`` 即 7 条。"""

    rule_id: str
    description: str
    branches: tuple[DirectBranch, ...]


DIRECT_RULES: tuple[DirectRule, ...] = (
    DirectRule(
        rule_id="play_control",
        description="播放控制：换一段 / 停 / 暂停 / 继续",
        branches=(
            DirectBranch(intent="play_next", label="换一段", pattern=_compile(r"换一?段")),
            DirectBranch(
                intent="stop",
                label="停/停下/停下来/停止/戏先停了",
                pattern=_compile(
                    r"(?:戏\s*)?(?:(?:先|就|等|快(?:点)?|马上)\s*)?停(?:下来?|下|止|一?下|一停|住|了)?"
                ),
            ),
            # FR-CON-03（2026-11-12）：暂停 / 继续。**只认「暂停」这个说法**——
            # 「先停一下」仍是 stop（见上一条分支与 test_router_l0 的正例），不许把
            # 长者说的「停」理解成可恢复的暂停，两者的收尾记账不同。
            DirectBranch(
                intent="pause",
                label="暂停/先暂停一下/暂停播放",
                # 后缀放宽到「播放/放/听/吧」，是为了让「暂停播放」这类自然说法也能
                # 落到这里、拿到「这台不能暂停」的实话，而不是丢给模型去编一句「停了」。
                pattern=_compile(r"(?:先|暂时)?暂停(?:一?下|一?会儿|播放|放|听|吧)*"),
            ),
            DirectBranch(
                intent="resume",
                label="继续/接着听/接着放",
                pattern=_compile(r"(?:继续|接着)(?:听|放|播|来)?"),
            ),
        ),
    ),
    DirectRule(
        rule_id="volume_control",
        description="音量控制：大声点 / 小声点",
        branches=(
            DirectBranch(
                intent="volume_up",
                label="大声点/大点声",
                pattern=_compile(r"(?:声音)?大(?:声)?一?点[声儿]?"),
            ),
            DirectBranch(
                intent="volume_down",
                label="小声点/小点声",
                pattern=_compile(r"(?:声音)?小(?:声)?一?点[声儿]?"),
            ),
        ),
    ),
    DirectRule(
        rule_id="opera",
        description="戏曲点播：越剧 / 京剧（分支直出剧种）",
        branches=(
            DirectBranch(
                intent="play_yueju",
                label="越剧",
                pattern=_compile(
                    r"(?:我想?要?|我要?|给我|请|想)?(?:来|放|唱|播(?:放)?|听|看)?(?:一)?(?:段|出|个|折)?越剧(?:解解闷|散散心)?[了啦]?"
                ),
            ),
            DirectBranch(
                intent="play_jingju",
                label="京剧",
                pattern=_compile(
                    r"(?:我想?要?|我要?|给我|请|想)?(?:来|放|唱|播(?:放)?|听|看)?(?:一)?(?:段|出|个|折)?京剧(?:解解闷|散散心)?[了啦]?"
                ),
            ),
        ),
    ),
    DirectRule(
        rule_id="info_query",
        description="信息问答：几点了 / 今天天气",
        branches=(
            DirectBranch(
                intent="ask_time",
                label="几点了",
                pattern=_compile(
                    r"(?:(?:现在|请问|帮?我?(?:看|瞧)(?:一)?(?:看|瞧)?)\s*)*几\s?点(?:钟)?(?:[了钟吗啊呀呢啦]{0,2})"
                ),
            ),
            DirectBranch(
                intent="weather_query",
                label="今天天气(怎么样/冷不冷)",
                pattern=_compile(
                    r"今天(?:的)?天气(?:怎么样?|什么样?|怎样?|如何|好不好?|(?:冷|热)不(?:冷|热)|冷吗|热吗)?"
                ),
            ),
            # N33：身体不适主诉（头晕类）自 2026-11-13 task-43 起**不再由本层直出**：
            # 这类主诉已并入 `safety.py` 的急症词族（⑩ 头晕/晕厥前兆族），命中即由
            # safety 一票否决短路到 `alert`，比这里多走一条「落 risk_events + 红/黄
            # 复核单」的链路。同一句话留在两层里只会让规则层先到先得、把告警吃掉，
            # 故此处整条分支删除；`health` intent 本身仍在（见 INTENT_WHITELIST），
            # 由 L2 主诉识别与 `health_checkup_graph.start_complaint_checkup` 使用。
        ),
    ),
    DirectRule(
        rule_id="medication",
        description="用药：我要吃药/该吃药了/现在吃药",
        branches=(
            DirectBranch(
                intent="medication_take",
                label="我要吃药",
                pattern=_compile(
                    r"(?:(?:我(?:要|想|得|得要)|该我?|现在(?:该)?)?吃(?:药|药片|药丸)"
                    r"(?:[了啦吧嘛]|好不好|可以吗?)?|吃药时间到?了?|把药拿?来?)"
                ),
            ),
        ),
    ),
    DirectRule(
        rule_id="farewell",
        description="道别：再见/拜拜",
        branches=(
            DirectBranch(
                intent="farewell",
                label="再见",
                pattern=_compile(r"(?:下次|回头)?(?:再见|回见|拜拜)"),
            ),
        ),
    ),
    DirectRule(
        rule_id="repeat",
        description="重听：再说一遍/重复一遍/再放一遍",
        branches=(
            DirectBranch(
                intent="repeat",
                label="再说一遍/再放一遍",
                pattern=_compile(
                    r"(?:你|我|请|帮?我)?再(?:说|讲|放|来)(?:一[遍次回下]|[遍次回下])(?:好吗|吧|哈|哦|呀|好不好)?"
                ),
            ),
            DirectBranch(
                intent="repeat",
                label="重复(一遍)",
                pattern=_compile(
                    r"(?:你|请|帮?我|再)?(?:刚才说的)?重复(?:一遍|一?遍|一下?|那?一遍?)?"
                ),
            ),
        ),
    ),
)

assert len(DIRECT_RULES) == RULE_COUNT, "rules.py 规则条数与顶部注释必须一致"


def _clause_candidates(text: str) -> Iterator[str]:
    """候选序列：整句在前、分句在后（口径见模块 docstring「全句锚定 + 分句」）。"""
    stripped = text.strip()
    yield stripped
    if _CLAUSE_SPLIT.search(stripped):
        for part in _CLAUSE_SPLIT.split(stripped):
            part = part.strip()
            if part:
                yield part


def match_rules(text: str) -> RouteDecision | None:
    """固定意图直路由扫描（`match_l0` 的第二步，safety 未命中时才进入）。

    整句 fullmatch 优先；未命中再逐分句 fullmatch（分句仍整段锚定，防半句
    误触发——见模块 docstring 工程口径）。
    """
    if not text:
        return None
    for rule in DIRECT_RULES:
        for branch in rule.branches:
            for candidate in _clause_candidates(text):
                if branch.pattern.fullmatch(candidate):
                    return RouteDecision(
                        elder_id=None,
                        thread_id=None,
                        layer=LAYER_L0,
                        rule_id=rule.rule_id,
                        intent=branch.intent,
                    )
    return None


def match_l0(text: str) -> RouteDecision | None:
    """L0 对外主入口：`safety 一票否决 → rules 固定意图 → None`（扫描顺序即契约）。

    命中返回 ``layer='L0'`` 的 :class:`~app.router.decision.RouteDecision`：
    safety 命中时 ``intent='alert'``、``sensitive=True``，调用方**短路**不进 L1/L2；
    rules 命中时 ``intent`` 为动作级直路由信号。两条都没命中返回 None（进 L1）。
    纯文本匹配不注入身份/计时/trace—— elder_id / thread_id 保持 None，由外层
    编排从 `runtime_context` 补齐；`route_ms` 归编排层观测。
    """
    if not text:
        return None
    safety_hit = match_safety(text)
    if safety_hit is not None:
        return safety_hit
    return match_rules(text)


__all__ = [
    "DIRECT_RULES",
    "RULE_COUNT",
    "DirectBranch",
    "DirectRule",
    "match_l0",
    "match_rules",
]
