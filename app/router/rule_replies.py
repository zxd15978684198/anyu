"""L0 固定意图的**固定话术表**（迭代 8 N01；短路点在 ``dialogue_service.respond()``）。

一句话说清本模块的存在理由
==========================
规则层只负责「匹配」，从来不生成话术；于是命中 L0 之后，编排层手里只有一个
`RouteDecision` 标签，**没有可播的文本**，唯一的文本来源就是 `llm.chat`——
这就是「命中急症还在跟长者闲聊」「说『停』还要过一遍大模型」的根因
（TD §8.3 迭代 6 验收：「命中规则层零模型」「敏感不进大模型、不等确认」）。

本模块补的就是那个「出口」：把「这句话本身已经说完」的意图映射成一句固定话术。

**什么进表、什么不进表**
========================
进表的判据只有一条：**这一句话本身就是完整答复**，不需要外部数据、不需要动作、
不需要上下文。据此收三个意图：

- ``alert``：安全告警三类（`safety_emergency` / `safety_harm` /
  `safety_request_human`，见 `app/router/safety.py`），按 ``rule_id`` 分别给话术；
- ``stop``：停止播放指令，回一句「停下来了」就结束；
- ``farewell``：道别，回一句告别语就结束。

明确**不进表**的意图与理由（改这张表之前先读这里，别顺手加）：

===================  ==========================================================
intent               不短路的原因
===================  ==========================================================
``weather_query``    必须经 MCP 取真实天气（迭代 6 验收第 3 条），短路等于编天气
``ask_time``         要用真实时钟；固定话术会报一个错的时间，比不答更糟
``repeat``           要复述上一轮**真实**回复，固定话术会变成假复述
``play_next`` \
``play_yueju`` \
``play_jingju`` \
``volume_up`` \
``volume_down``      隐含**动作**。``respond()`` 不执行动作，短路会让数字人
                     「说放了但其实没放」——宁可多花一次模型，也不说假话
``medication_take``  要落用药确认链路（子图 + 回执），不是一句话能结束的
===================  ==========================================================

第二张表：`INSTANT_REPLIES` / `instant_reply()`（2026-11-12 · T6 · FR-DLG-02 补全）
================================================================================
上面那张「不进表」的理由**没有被推翻**，被推翻的只是推论「所以它们只能问模型」：

- ``ask_time`` —— 进 ``instant_reply``，且**读真实时钟**（``Asia/Shanghai``，AGENTS §8）。
  原理由针对的是「固定话术会报错时间」；用真时钟算出来的话术不是固定话术，问题不存在。
- ``volume_up`` / ``volume_down`` / ``play_next`` / ``play_yueju`` / ``play_jingju`` ——
  动作**已经由编排层投递**：``DialogueRuntime._push_media_control`` 在
  ``respond()`` **之前**（``app/dialogue_runtime.py:202`` → ``:241``）就按
  ``decision.intent`` 翻成 ``media_control`` 下行，``media_service.CONTROL_INTENTS``
  是那张映射表的唯一一处。所以这里补的只是「动作已发出」的**回话**，不是假装执行。
  这也正是 ``respond()`` 不执行动作、但仍然可以短路的原因：执行权不在它手里。
- ``repeat`` —— 不进这张表：复述要用**上一轮真实回复**，而那是 Redis 里的事实。
  ``dialogue_service._repeat_reply`` 单独读最近一轮来拼，本模块保持纯值表。

于是本模块有两张表，分工是**依赖什么**而不是「重不重要」：

===================  ==================================  =========================
表                    依赖                                调用方
===================  ==================================  =========================
``reply_for``         什么都不依赖（纯值查找）              ``respond()`` 第一道短路
``instant_reply``     可选的 ``now``（报时用真时钟）        ``respond()`` 第二道短路
===================  ==================================  =========================

``reply_for`` / ``SHORT_CIRCUIT_INTENTS`` 的取值与语义**一个字没改**——表外意图守卫
（``tests/test_l0_short_circuit.py:200-213``）继续成立：它们确实不在 ``reply_for``
那张表里，只是现在多了一条不需要模型的出口。

``pause`` / ``resume``（FR-CON-03）在这张表里，**回话必须是实话**：
pad 的播放器从 2026-11-12（W6）起支持 ``pause`` / ``resume``——暂停时记下
``currentTime``，继续说「继续」就 seek 回原位接着放（``MediaPlayer.tsx`` 的
``pausedAtRef``；``media_service.CONTROL_INTENTS`` 已有 ``pause``/``resume`` 两条映射，
``realtime.ts`` 的 ``media_control`` 白名单也已放行）。所以这两句现在可以如实说
「停在这儿 / 从原位接着放」；**仍然不许**说成「关掉了」——pause 不是收尾，
``media_history`` 不会写 ``finished_at``，说了「关了」就是假话。

（历史：2026-11-12 之前 pad 只认 ``next`` / ``stop``，未知 action 整条丢弃
（``realtime.ts:613-621`` 旧版），那时 pause 的回话只能是「这台只能整个关掉」。
接通后该话术已改成真话，勿再回退。）

安全话术的措辞红线
==================
AGENTS §4.4：**不说医疗诊断口径**，不猜病因、不给处置建议。三类话术只做三件事：
「我已经通知人了」「您现在别动/离远点」「人马上到」。具体措辞留在下面的表里，
改文案时守住这三件事即可。

与 ``app/core/degrade_speech.py`` 的分工
=======================================
那边是「模型坏了/超时了怎么兜」（失败路径的降级话术，键是 ``llm_timeout`` 这类
故障名）；这边是「本来就不该问模型」的正常固定应答。两件事混在一张表里，
以后想统计「降级率」时会把正常短路也算进去。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

#: 安全规则命中 → 固定话术。键是 `safety.py` 里的 `SafetyRuleSpec.rule_id`。
#: **只允许出现 `safety.py` 真实存在的 rule_id**；新增规则必须同批加条目，
#: 否则会落到下面的 `SAFETY_FALLBACK`（能播，但措辞不贴类别）。
SAFETY_REPLIES: Final[Mapping[str, str]] = {
    "safety_emergency": "我马上帮您叫人，您先别动，找个稳当的地方坐好或者躺好。",
    "safety_harm": "我马上叫人过来，您先离那个东西远一点，保护好自己。",
    "safety_request_human": "好，我这就帮您叫人，马上有人过来。",
}

#: 未知安全 `rule_id` 的兜底话术：**宁可喊人，不可沉默**——安全路径上沉默比啰嗦危险。
SAFETY_FALLBACK: Final[str] = "我马上帮您叫人，您先别急，人马上到。"

#: 非安全类固定意图 → 话术。键是 `rules.py` 里的动作级 `intent`。
DIRECT_REPLIES: Final[Mapping[str, str]] = {
    "stop": "好，停下了。",
    "farewell": "好，您慢点，有事随时叫我。",
}

#: 允许走短路的 `intent` 闭集（含 `alert`）。表外的意图一律返回 None 走原路径。
SHORT_CIRCUIT_INTENTS: Final[frozenset[str]] = frozenset({"alert", *DIRECT_REPLIES})

ALERT_INTENT: Final[str] = "alert"

#: 报时意图（`rules.py` 的 `info_query` 分支）。单独拎出来是因为它要读时钟。
ASK_TIME_INTENT: Final[str] = "ask_time"

#: 第二张表（FR-DLG-02）：**动作已由编排层投递**或**回话就是全部内容**的固定意图。
#:
#: 与 ``DIRECT_REPLIES`` 的区别只在「依赖什么」：这张表的话术描述的是编排层
#: **已经或正在**做的事（``_push_media_control`` 按同一个 ``decision.intent`` 下发），
#: 所以短路不会变成「说放了但其实没放」。
#:
#: ``pause`` / ``resume`` 的措辞红线见模块 docstring：这两句可以说「停在这儿 / 从原位
#: 接着放」（pad 已支持），但**不许**说「关掉了」——pause 不写 `media_history.finished_at`。
INSTANT_REPLIES: Final[Mapping[str, str]] = {
    "play_next": "好，给您换一段。",
    "volume_up": "好，我大声点儿。",
    "volume_down": "好，小声点儿。",
    "play_yueju": "好，给您放越剧。",
    "play_jingju": "好，给您放京剧。",
    "pause": "好，先给您停在这儿；您说「继续」，我就从刚才那儿接着放。",
    "resume": "好，咱接着听。",
}

#: 报时用的时区：存储一律 UTC，展示层转 Asia/Shanghai（AGENTS §8）。
TZ_SHANGHAI: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")

_CN_DIGITS: Final[tuple[str, ...]] = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")


def _cn_number(value: int) -> str:
    """0–59 → 中文读法。报时要口语（「三点二十」），不念「15 点 20 分」。"""
    if value < 10:
        return _CN_DIGITS[value]
    tens, ones = divmod(value, 10)
    head = "十" if tens == 1 else f"{_CN_DIGITS[tens]}十"
    return head if ones == 0 else f"{head}{_CN_DIGITS[ones]}"


def _period_of(hour: int) -> str:
    """24 小时制 → 「上午/下午/晚上/凌晨」（长者听得懂的时段词）。"""
    if 5 <= hour < 12:
        return "上午"
    if 12 <= hour < 18:
        return "下午"
    if 18 <= hour < 24:
        return "晚上"
    return "凌晨"


def _clock_reply(now: datetime | None = None) -> str:
    """用**真实时钟**报时；naive 时间按东八区解释（与 ``build_dialogue_messages`` 同口径）。"""
    moment = now if now is not None else datetime.now(TZ_SHANGHAI)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ_SHANGHAI)
    local = moment.astimezone(TZ_SHANGHAI)
    hour12 = local.hour % 12 or 12
    clock = f"{_period_of(local.hour)}{_cn_number(hour12)}点"
    if local.minute == 0:
        return f"现在是{clock}整。"
    return f"现在是{clock}{_cn_number(local.minute)}分。"


def instant_reply(decision: object, *, now: datetime | None = None) -> str | None:
    """第二道短路：给出「不需要模型」的固定回话；给不出就返回 None（照原路径走）。

    与 :func:`reply_for` 一样只做 ``getattr``、不 import ``RouteDecision``，因此
    **永不抛异常**。``repeat`` 不在这里（要读上一轮真实回复，见
    ``dialogue_service._repeat_reply``）。
    """
    intent = getattr(decision, "intent", None)
    if not isinstance(intent, str):
        return None
    if intent == ASK_TIME_INTENT:
        return _clock_reply(now)
    return INSTANT_REPLIES.get(intent)


def reply_for(decision: object) -> str | None:
    """给出这一轮 L0 的固定话术；不该短路时返回 None。

    参数故意标 ``object``：调用方（编排层）传的是 `RouteDecision`，但这里只做
    ``getattr`` 读取，不 import 类型——与 `dialogue_service` 现有的「只读标签、
    不依赖路由层类型」口径一致，路由层改字段也不会把编排层拖进来跟着改。

    未知 `intent`、缺字段、非字符串一律返回 None（等价「照原样走」），
    因此本函数**永不抛异常**：短路是优化路径，不是新的失败点。
    """
    intent = getattr(decision, "intent", None)
    if not isinstance(intent, str) or intent not in SHORT_CIRCUIT_INTENTS:
        return None

    if intent == ALERT_INTENT:
        rule_id = getattr(decision, "rule_id", None)
        if isinstance(rule_id, str):
            return SAFETY_REPLIES.get(rule_id, SAFETY_FALLBACK)
        return SAFETY_FALLBACK

    return DIRECT_REPLIES.get(intent)


__all__ = [
    "ALERT_INTENT",
    "ASK_TIME_INTENT",
    "DIRECT_REPLIES",
    "INSTANT_REPLIES",
    "SAFETY_FALLBACK",
    "SAFETY_REPLIES",
    "SHORT_CIRCUIT_INTENTS",
    "TZ_SHANGHAI",
    "instant_reply",
    "reply_for",
]
