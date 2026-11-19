# owner=W4 · 编排接线 · privacy 谓词上收（唯一实现，两份镜像删除）

"""W4 验收：`privacy` 真伪判定只剩**一份**实现，且落在路由层。

唯一依据：`设计记录（编排接线）` §4-W4 / §8 S-W4。

覆盖口径：

1. **语义逐字等于原 `app/agents/companion.py::is_privacy_converged`**（表驱动）：
   bool 原样；`None` 假；字符串去空白 + 小写后查 `PRIVACY_FALSE_TAGS`；数字等其余
   类型走 Python 真值（`0` / `0.0` 假、非 0 真）。断言用 `is`，顺带钉住「返回的是
   真 bool」而不是 `0`/`1`。
2. **同一个函数对象**：从 `app.agents.companion` 与 `app.audio.reply_stub` 两个调用点
   import 到的 `is_privacy_converged`，与 `app.router.decision.is_privacy_converged`
   是 `is` 同一对象——「语义相同」不算达标，两份实现迟早分叉。
3. **旧镜像已删**：`reply_stub._privacy_is_true` 与两处 `_PRIVACY_FALSE_TAGS` 都必须
   不存在，防止有人顺手把它们加回来（加回来就等于又有第二套判定）。

本文件不碰数据库、不构造 LLM：只 import 三个模块做纯函数断言。
"""

from __future__ import annotations

import pytest

from app.agents import companion as companion_module
from app.agents.companion import is_privacy_converged as companion_predicate
from app.audio import reply_stub as reply_stub_module
from app.audio.reply_stub import is_privacy_converged as reply_stub_predicate
from app.router import decision

#: 「明确为假」的取值集合——W4 上收前后逐字相同（`companion` / `reply_stub` 曾各存一份）。
_EXPECTED_FALSE_TAGS = frozenset({"", "0", "false", "no", "none", "null", "off"})


def test_false_tags_contract_is_frozen() -> None:
    """排除集本身就是契约：动它等于改「什么算隐私收敛」，必须让本用例红。"""
    assert decision.PRIVACY_FALSE_TAGS == _EXPECTED_FALSE_TAGS


@pytest.mark.parametrize(
    ("privacy", "expected"),
    [
        # ---- 布尔：Laya 五信号透传的形态，必须原样返回 ----
        pytest.param(None, False, id="none"),
        pytest.param(False, False, id="bool-false"),
        pytest.param(True, True, id="bool-true"),
        # ---- 数字：契约外的漂移形态，走 Python 真值兜底 ----
        pytest.param(0, False, id="int-zero"),
        pytest.param(1, True, id="int-one"),
        pytest.param(0.0, False, id="float-zero"),
        pytest.param(2.5, True, id="float-nonzero"),
        # ---- 字符串：落库/评测夹具形态，去空白 + 忽略大小写后查排除集 ----
        pytest.param("", False, id="empty-string"),
        pytest.param("  ", False, id="blank-string"),
        pytest.param("false", False, id="str-false"),
        pytest.param("FALSE", False, id="str-false-upper"),
        pytest.param(" false ", False, id="str-false-padded"),
        pytest.param("no", False, id="str-no"),
        pytest.param("none", False, id="str-none"),
        pytest.param("null", False, id="str-null"),
        pytest.param("off", False, id="str-off"),
        pytest.param("0", False, id="str-zero"),
        # ---- 真值：一切「非明确为假」的取值，含历史标签值 ----
        pytest.param("true", True, id="str-true"),
        pytest.param("  TRUE  ", True, id="str-true-padded-upper"),
        pytest.param("couple", True, id="str-couple"),
        pytest.param("双人间", True, id="str-chinese-double-room"),
        # ---- 其它类型：真值兜底，宁可多说一句 ----
        pytest.param(object(), True, id="opaque-object"),
    ],
)
def test_predicate_truth_table(privacy: object, expected: bool) -> None:
    """表驱动钉住语义：只判「是否明确为假」，其余一律按真处理。"""
    assert decision.is_privacy_converged(privacy) is expected


def test_both_call_sites_are_the_same_function_object() -> None:
    """两处调用点必须是**同一对象**；只是语义相同的话，改一处忘另一处的老坑还在。"""
    assert companion_predicate is reply_stub_predicate is decision.is_privacy_converged


def test_call_sites_expose_router_as_their_home() -> None:
    """从任一调用点 import 拿到的都是路由层实现（`__module__` 钉住来源）。"""
    assert companion_predicate.__module__ == "app.router.decision"
    assert reply_stub_predicate.__module__ == "app.router.decision"


def test_local_mirrors_are_gone() -> None:
    """旧镜像名必须已删除：`_privacy_is_true` / `_PRIVACY_FALSE_TAGS` 不许再出现。"""
    assert not hasattr(reply_stub_module, "_privacy_is_true")
    assert not hasattr(reply_stub_module, "_PRIVACY_FALSE_TAGS")
    assert not hasattr(companion_module, "_PRIVACY_FALSE_TAGS")
