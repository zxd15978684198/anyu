"""降级话术唯一源 `app/core/degrade_speech.py` 的收口断言（迭代 5 §4.3）。

三条验收口径（PRD line 572「三档超时各有可读降级提示」+ AGENTS §4.8 一能力一形态）：

1. **键集合一致**：REST（书面）与 SPOKEN（口语）同键、各 5 个，一个不多一个不少；
2. **三档话术互不相同、非空**：ASR 首包 / LLM 首 token / TTS 首包各映射到一个已有键，
   三句必须能听出区别 —— 三句一样等于「没有分档」；
3. **两处调用点只剩别名**：`app/api/audio.py::DEGRADE_TEMPLATES` 与
   `app/audio/signaling.py::_ERROR_TALKBACK` 必须是本模块常量的**同一个对象**。
   用 `is` 断言而不是 `==`：值相等只能证明「现在内容一样」，`is` 才能证明
   「没有第二份副本」—— 副本一旦回来，改一处忘一处就是迟早的事。

未知键的回落也在本文件锁住：调用方（REST 300 行、WS `_send_error`）都把「取话术」
当纯函数用，不该各自再写一遍 `.get(..., 兜底)`。
"""

from __future__ import annotations

from app.api import audio as audio_api
from app.audio import signaling
from app.core.degrade_speech import (
    REST_DEGRADE,
    SPOKEN_DEGRADE,
    TIMEOUT_STAGE_TO_KEY,
    key_for_timeout,
    rest_speech,
    spoken_speech,
)

#: 键闭集：6 个（迭代 5 修复新增 `tool_budget`：预算耗尽与同 Tool 熔断两句话术）。
EXPECTED_KEYS = frozenset(
    {
        "asr_unavailable",
        "tts_unavailable",
        "llm_timeout",
        "tool_breaker",
        "tool_budget",
        "internal",
    }
)

#: 三档超时对应的档位名（与 `app/core/timeouts.py` 的 stage 取值一致）。
TIMEOUT_STAGES = ("asr", "llm", "tts")


def _texts(table: dict[str, str], keys: list[str]) -> list[str]:
    """按给定键顺序取话术，便于逐句断言。"""
    return [table[key] for key in keys]


# ---------------------------------------------------------------- 键集合


def test_rest_and_spoken_share_the_same_key_set() -> None:
    assert set(REST_DEGRADE) == EXPECTED_KEYS
    assert set(SPOKEN_DEGRADE) == EXPECTED_KEYS
    # 断言长度是为了防「键集合相等但多塞了空值」这种等价改写。
    assert len(REST_DEGRADE) == len(SPOKEN_DEGRADE) == 6


def test_all_speeches_are_non_empty_strings() -> None:
    for table_name, table in (("REST", REST_DEGRADE), ("SPOKEN", SPOKEN_DEGRADE)):
        for key, text in table.items():
            assert isinstance(text, str), (table_name, key)
            assert text.strip() != "", (table_name, key)


def test_no_speech_leaks_internal_details() -> None:
    """话术是给长者听/读的：不许出现 provider 名、异常类名、trace 之类内部词。"""
    forbidden = ("Traceback", "trace_id", "Exception", "asyncio", "provider", "Error")
    for table_name, table in (("REST", REST_DEGRADE), ("SPOKEN", SPOKEN_DEGRADE)):
        for key, text in table.items():
            for word in forbidden:
                assert word not in text, (table_name, key, word)


# ---------------------------------------------------------------- 三档超时话术


def test_timeout_stages_map_to_existing_keys() -> None:
    for stage in TIMEOUT_STAGES:
        key = key_for_timeout(stage)
        assert key == TIMEOUT_STAGE_TO_KEY[stage]
        assert key in REST_DEGRADE
        assert key in SPOKEN_DEGRADE


def test_three_timeout_speeches_differ_and_are_readable() -> None:
    """PRD line 572：三档各有可读提示 —— 互不相同且非空（两套措辞都要满足）。"""
    for table_name, table in (("REST", REST_DEGRADE), ("SPOKEN", SPOKEN_DEGRADE)):
        keys = [key_for_timeout(stage) for stage in TIMEOUT_STAGES]
        texts = _texts(table, keys)

        assert len(set(texts)) == 3, (table_name, texts)
        for text in texts:
            assert text.strip() != "", (table_name, text)

    # 书面与口语也要能听出区别：同一句在页面和数字人嘴里不该一字不差。
    rest_texts = _texts(REST_DEGRADE, [key_for_timeout(stage) for stage in TIMEOUT_STAGES])
    spoken_texts = _texts(SPOKEN_DEGRADE, [key_for_timeout(stage) for stage in TIMEOUT_STAGES])
    assert all(rest != spoken for rest, spoken in zip(rest_texts, spoken_texts, strict=True))


def test_spoken_timeout_speeches_match_direct_lookup() -> None:
    for stage in TIMEOUT_STAGES:
        key = key_for_timeout(stage)
        assert spoken_speech(key) == SPOKEN_DEGRADE[key]
        assert rest_speech(key) == REST_DEGRADE[key]


# ---------------------------------------------------------------- 未知键回落


def test_unknown_key_falls_back_to_internal() -> None:
    assert rest_speech("no_such_key") == REST_DEGRADE["internal"]
    assert spoken_speech("no_such_key") == SPOKEN_DEGRADE["internal"]
    assert rest_speech("") == REST_DEGRADE["internal"]
    assert spoken_speech("") == SPOKEN_DEGRADE["internal"]


def test_unknown_timeout_stage_falls_back_to_internal() -> None:
    assert key_for_timeout("wake") == "internal"
    assert key_for_timeout("") == "internal"


# ---------------------------------------------------------------- 两处调用点收口


def test_rest_chain_has_no_local_copy() -> None:
    """`app/api/audio.py::DEGRADE_TEMPLATES` 只能是 REST_DEGRADE 的别名。"""
    assert audio_api.DEGRADE_TEMPLATES is REST_DEGRADE
    assert audio_api.DEGRADE_TEMPLATES is not SPOKEN_DEGRADE


def test_ws_chain_has_no_local_copy() -> None:
    """`app/audio/signaling.py::_ERROR_TALKBACK` 只能是 SPOKEN_DEGRADE 的别名。

    测试与 A3/A1 的下标取用方式（`signaling._ERROR_TALKBACK[<code>]`）必须继续可用，
    所以除了身份断言，再把五个键逐个按老写法取一遍。
    """
    assert signaling._ERROR_TALKBACK is SPOKEN_DEGRADE
    for key in EXPECTED_KEYS:
        assert signaling._ERROR_TALKBACK[key] == SPOKEN_DEGRADE[key]
