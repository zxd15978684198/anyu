"""降级话术唯一源（迭代 5 §4.3 收口）。

背景：迭代 3 遗留两套硬编码话术——REST 降级链 `app/api/audio.py::DEGRADE_TEMPLATES`
（书面语）与 WS 下行 `app/audio/signaling.py::_ERROR_TALKBACK`（口语）。两者**键相同、
受众不同**，属于同一能力的两种措辞，不是两套实现。本模块只做一件事：把键与措辞
收到一处，调用方按受众取用；**禁止再开第三套**（AGENTS §4.8 一能力一形态）。

口径：
1. 键闭集 = `asr_unavailable / tts_unavailable / llm_timeout / tool_budget /
   tool_breaker / internal`；
2. 书面（REST）与口语（WS）**同键不同措辞**，但触发条件必须一致；
3. 三档超时（迭代 5 §4.2）各自映射到已有键，不新增键：
   ASR 首包超时 → `asr_unavailable`；LLM 首 token 超时 → `llm_timeout`；
   TTS 首包超时 → `tts_unavailable`；
4. 话术内容**不得含内部细节**（provider 名、堆栈、trace_id），只对长者说人话；
5. `tool_budget` 与 `tool_breaker` 是两个机制、两句话术（PRD FR-DLG-10）：
   前者 = 本轮预算耗尽（TD line 832「超限强制收尾」），后者 = 同一 Tool 在 1 分钟
   内第 3 次调用触发熔断。**不得把两者合并成一个键**，否则长者听到的原因就是错的。
"""

from __future__ import annotations

from typing import Final

# REST / 书面风格：页面提示、需要长者读的文字。
REST_DEGRADE: Final[dict[str, str]] = {
    "asr_unavailable": "抱歉，语音识别暂时不可用。您可以直接输入文字，或稍后再试。",
    "tts_unavailable": "抱歉，语音播报暂时不可用，但我会用文字回复您。",
    "llm_timeout": "抱歉，我正在思考，您可以稍后再试或联系工作人员。",
    "tool_budget": "抱歉，这个请求我暂时处理不了，已帮您记下，稍后再试。",
    "tool_breaker": "抱歉，当前请求处理超时，我会尽快通知工作人员。",
    "internal": "出了点小状况，请稍后再试。",
}

# WS / 口语风格：由数字人「说」出来的短句，必须能听懂且互不相同。
SPOKEN_DEGRADE: Final[dict[str, str]] = {
    "asr_unavailable": "我这会儿听不太清，请稍后再说一遍。",
    "tts_unavailable": "我这会儿说话不太利索，先用文字回您。",
    "llm_timeout": "我稍微想一想，请等我一下下。",
    "tool_budget": "这个我处理不了，帮您记下来稍后再试。",
    "tool_breaker": "这个功能暂时不可用，稍后再试试。",
    "internal": "出了点小状况，请再说一遍。",
}

# 三档超时 → 已有键（不新增键，迭代 5 §4.2 / §4.3）。
TIMEOUT_STAGE_TO_KEY: Final[dict[str, str]] = {
    "asr": "asr_unavailable",
    "llm": "llm_timeout",
    "tts": "tts_unavailable",
}

_KEYS: Final[frozenset[str]] = frozenset(REST_DEGRADE)


def rest_speech(key: str) -> str:
    """取书面降级话术；未知键回落到 `internal`，调用方不必自己兜底。"""
    return REST_DEGRADE.get(key, REST_DEGRADE["internal"])


def spoken_speech(key: str) -> str:
    """取口语降级话术；未知键回落到 `internal`。"""
    return SPOKEN_DEGRADE.get(key, SPOKEN_DEGRADE["internal"])


def key_for_timeout(stage: str) -> str:
    """把超时环节（asr/llm/tts）映射到已有降级键；未知环节按 `internal` 处理。"""
    return TIMEOUT_STAGE_TO_KEY.get(stage, "internal")


__all__ = [
    "REST_DEGRADE",
    "SPOKEN_DEGRADE",
    "TIMEOUT_STAGE_TO_KEY",
    "key_for_timeout",
    "rest_speech",
    "spoken_speech",
]
