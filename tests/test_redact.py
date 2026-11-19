"""脱敏底座用例（迭代 9 · W1）：四个 mask 函数、递归脱敏、文本脱敏、扫描脚本与日志接入。

覆盖口径：每个 mask 函数都有「正常 / 边界（空串、长度不符、非法日期）/ 幂等 / 非命中」四类；
`redact_mapping` 覆盖嵌套 dict / list / tuple、大小写与子串键名、非字符串值、不改入参；
`scan_text` 覆盖四类命中 + 两条反例（干净行、已脱敏行）；`scan_logs.main` 对 `tmp_path`
真跑一遍，验证命中退出码 1、干净退出码 0、`--glob` 生效。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from scripts.scan_logs import main, scan_paths, scan_text

from app.core.logging import JsonFormatter
from app.core.redact import (
    REDACTION_RULES,
    detect_sensitive,
    mask_address,
    mask_id_card,
    mask_phone,
    mask_secret,
    redact_mapping,
    redact_text,
)

pytestmark = pytest.mark.injection


# ======================================================================
# 一、mask_phone
# ======================================================================


def test_mask_phone_keeps_first_three_and_last_four() -> None:
    """标准 11 位手机号：前 3 后 4，中间 4 个星号。"""
    assert mask_phone("13700000000") == "137****0000"


def test_mask_phone_masks_short_value_entirely() -> None:
    """长度不足：整体打码，不保留任何字符。"""
    assert mask_phone("1370000") == "*******"


def test_mask_phone_masks_eleven_digits_that_are_not_mobile() -> None:
    """11 位但不是手机号（第二位不在 3-9）：整体打码，不做「前 3 后 4」。"""
    assert mask_phone("12345678901") == "***********"


def test_mask_phone_keeps_empty_value_empty() -> None:
    """空值不是隐私，原样返回，避免在日志里制造无意义的星号。"""
    assert mask_phone("") == ""


def test_mask_phone_masks_malformed_shape_entirely() -> None:
    """带空格或国际区号的写法识别不了：整体打码（fail closed）。"""
    assert mask_phone("137 0000 0000") == "*" * 13
    assert mask_phone("+8613700000000") == "*" * 14


def test_mask_phone_is_idempotent() -> None:
    """已打码的值再套一次不变 —— 日志与审计导出可能对同一份数据重复脱敏。"""
    once = mask_phone("13700000000")
    assert mask_phone(once) == once


# ======================================================================
# 二、mask_id_card
# ======================================================================


def test_mask_id_card_eighteen_digits() -> None:
    """18 位新证：前 6 后 4，中间 8 个星号。"""
    assert mask_id_card("110101199003071234") == "110101********1234"


def test_mask_id_card_eighteen_digits_with_x_check_bit() -> None:
    """末位校验位可以是 X（大小写都认）。"""
    assert mask_id_card("11010119900307123X") == "110101********123X"
    assert mask_id_card("11010119900307123x") == "110101********123x"


def test_mask_id_card_fifteen_digits_legacy() -> None:
    """15 位老证：前 6 后 4，中间 5 个星号。"""
    assert mask_id_card("110101900307123") == "110101*****7123"


def test_mask_id_card_masks_illegal_birth_date_entirely() -> None:
    """18 位但月份非法：不是身份证，整体打码（不按形状硬套掩码）。"""
    masked = mask_id_card("110101199013071234")
    assert masked == "*" * 18


def test_mask_id_card_masks_short_value_entirely() -> None:
    assert mask_id_card("12345") == "*****"


# ======================================================================
# 三、mask_address
# ======================================================================


def test_mask_address_keeps_up_to_district() -> None:
    """保留到「区」，门牌号 / 栋 / 单元 / 室全部换成 ***。"""
    assert mask_address("北京市朝阳区建国路88号院3栋2单元501室") == "北京市朝阳区***"


def test_mask_address_keeps_province_city_district() -> None:
    assert mask_address("广东省深圳市南山区科技园科苑路1栋2单元501室") == ("广东省深圳市南山区***")


def test_mask_address_keeps_value_that_has_no_house_number() -> None:
    """只写到区：本来就没有门牌号可打，原样返回。"""
    assert mask_address("北京市朝阳区") == "北京市朝阳区"


def test_mask_address_masks_whole_value_without_district() -> None:
    """识别不到区 / 县 / 旗层级：整体打码（连街道一起丢）。"""
    value = "建国路88号3栋501室"
    assert mask_address(value) == "*" * len(value)


def test_mask_address_is_idempotent() -> None:
    once = mask_address("北京市朝阳区建国路88号")
    assert mask_address(once) == once


# ======================================================================
# 四、mask_secret
# ======================================================================


def test_mask_secret_sk_scheme() -> None:
    """`sk-` 前缀只留 scheme，后半段全丢。"""
    assert mask_secret("sk-proj-abcdef1234567890") == "sk-***"


def test_mask_secret_bearer_scheme() -> None:
    """`Bearer <token>` 归一化成 `Bearer ***`（不保留原大小写，免得被当线索）。"""
    assert mask_secret("Bearer eyJhbGciOiJIUzI1NiJ9") == "Bearer ***"
    assert mask_secret("bearer eyJhbGciOiJIUzI1NiJ9") == "Bearer ***"


def test_mask_secret_github_scheme() -> None:
    assert mask_secret("ghp_abcdefgh12345678") == "ghp_***"
    assert mask_secret("github_pat_abcdefghij1234567890") == "github_pat_***"


def test_mask_secret_long_base64_keeps_short_prefix() -> None:
    """≥32 字符、大小写数字混合：保留前 4 字符，够定位是哪一条，不够还原。"""
    value = "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Ng"
    assert len(value) >= 32
    assert mask_secret(value) == f"{value[:4]}***"


def test_mask_secret_plain_password_is_fully_masked() -> None:
    """口令不符合任何已知密钥格式也必须整体打码 —— fail closed。"""
    assert mask_secret("123456") == "***"


def test_mask_secret_keeps_empty_value_empty() -> None:
    assert mask_secret("") == ""
    assert mask_secret("   ") == "   "


# ======================================================================
# 五、redact_mapping
# ======================================================================


def test_redact_mapping_masks_sensitive_keys_nested() -> None:
    """嵌套 dict + list + tuple 全部下钻，键名命中即套对应 mask。"""
    data: dict[str, Any] = {
        "elder": {"phone": "13800138000", "id_card": "110101199003071234", "name": "张三"},
        "contacts": [{"mobile": "13900139000"}, {"MOBILE": "13700000000"}],
    }
    result = redact_mapping(data)

    assert result["elder"]["phone"] == "138****8000"
    assert result["elder"]["id_card"] == "110101********1234"
    assert result["elder"]["name"] == "张三"
    assert [
        item["mobile"] if "mobile" in item else item["MOBILE"] for item in result["contacts"]
    ] == [
        "139****9000",
        "137****0000",
    ]


def test_redact_mapping_masks_tuple_items_under_sensitive_key() -> None:
    """敏感键下面是元组：逐项打码并保持元组类型（不是悄悄变成 list）。"""
    result = redact_mapping({"phones": ("13800138000", "13700000000")})
    assert result["phones"] == ("138****8000", "137****0000")


def test_redact_mapping_matches_key_case_insensitively() -> None:
    assert redact_mapping({"PHONE": "13800138000"})["PHONE"] == "138****8000"
    assert redact_mapping({"User_Mobile": "13800138000"})["User_Mobile"] == "138****8000"


def test_redact_mapping_matches_key_by_substring() -> None:
    result = redact_mapping(
        {"user_phone_number": "13800138000", "home_address": "北京市朝阳区建国路88号"}
    )
    assert result["user_phone_number"] == "138****8000"
    assert result["home_address"] == "北京市朝阳区***"


def test_redact_mapping_masks_password_and_secret_keys() -> None:
    result = redact_mapping(
        {
            "password": "hunter2",
            "api_key": "sk-abcdefgh123456",
            "authorization": "Bearer abcdefgh1234",
            "sdp": "v=0\r\no=-",
        }
    )
    assert result["password"] == "***"
    assert result["api_key"] == "sk-***"
    assert result["authorization"] == "Bearer ***"
    assert result["sdp"] == "***"


def test_redact_mapping_keeps_non_string_values() -> None:
    """非字符串值原样保留：本函数只脱敏，不改类型。"""
    data = {"phone": 13800138000, "count": 3, "flag": None}
    assert redact_mapping(data) == data


def test_redact_mapping_leaves_plain_fields_untouched() -> None:
    """普通字段原样通过；`identity_source` 这类非隐私枚举不能被 `identity` 带下车。"""
    data = {"elder_name": "张三", "level": "INFO", "identity_source": "device"}
    assert redact_mapping(data) == data


def test_redact_mapping_masks_bare_identity_key_only() -> None:
    """单独的 `identity` 键按身份证处理，但 `identity_card` 这类更长键名同样命中。"""
    result = redact_mapping(
        {"identity": "110101199003071234", "identity_card": "110101199003071234"}
    )
    assert result["identity"] == "110101********1234"
    assert result["identity_card"] == "110101********1234"


def test_redact_mapping_does_not_mutate_input() -> None:
    data = {"phone": "13800138000", "nested": {"password": "hunter2"}}
    redact_mapping(data)
    assert data["phone"] == "13800138000"
    assert data["nested"]["password"] == "hunter2"


def test_redact_mapping_returns_new_dict() -> None:
    data = {"msg": "ok"}
    assert redact_mapping(data) is not data


# ======================================================================
# 六、redact_text
# ======================================================================


def test_redact_text_masks_phone_in_sentence() -> None:
    assert redact_text("他的号码是 13800138000，请回电") == "他的号码是 138****8000，请回电"


def test_redact_text_masks_id_card_and_bearer() -> None:
    text = "证件 110101199003071234，头 Bearer abcdefgh123456"
    assert redact_text(text) == "证件 110101********1234，头 Bearer ***"


def test_redact_text_masks_sk_key() -> None:
    assert redact_text("key=sk-abcdefgh12345678") == "key=sk-***"


def test_redact_text_leaves_clean_text_unchanged() -> None:
    """干净文本逐字节不动（含 32 位纯小写 hex 的 trace_id，不能被当成 base64 密钥）。"""
    text = "2026-11-08 20:04:28 INFO 用药提醒已发送 trace=a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert redact_text(text) == text


def test_redact_text_is_idempotent() -> None:
    once = redact_text("号码 13800138000 证件 110101199003071234")
    assert redact_text(once) == once


def test_redact_text_handles_empty_string() -> None:
    assert redact_text("") == ""


# ======================================================================
# 七、scan_text / scan_paths / main
# ======================================================================


def test_scan_text_finds_phone() -> None:
    findings = scan_text("用户手机 13800138000")
    assert [(item.kind, item.fragment) for item in findings] == [("phone", "138****8000")]


def test_scan_text_finds_id_card() -> None:
    findings = scan_text("证件号 110101199003071234")
    assert [item.kind for item in findings] == ["id_card"]
    assert findings[0].fragment == "110101********1234"


def test_scan_text_finds_secret() -> None:
    assert [item.kind for item in scan_text("Authorization: Bearer abcdefgh123456")] == ["secret"]


def test_scan_text_finds_full_address() -> None:
    findings = scan_text("住址：北京市朝阳区建国路88号院3栋2单元501室")
    assert [item.kind for item in findings] == ["address"]
    assert findings[0].fragment == "北京市朝阳区***"


def test_scan_text_returns_empty_for_clean_line() -> None:
    assert scan_text('{"ts":"2026-11-08T20:04:28Z","level":"INFO","msg":"ok"}') == []


def test_scan_text_ignores_already_masked_line() -> None:
    """脱敏后的日志行不能再被报成命中，否则验收脚本永远红。"""
    assert scan_text("手机 138****8000 证件 110101********1234") == []


def test_scan_text_reports_position() -> None:
    text = "号码 13800138000"
    findings = scan_text(text)
    assert findings[0].position == text.index("13800138000")


def test_scan_text_flags_address_only_with_house_number() -> None:
    """只有区、没有门牌号：不算「完整地址」，避免把正常中文误报。"""
    assert [item for item in scan_text("朝阳区天气晴") if item.kind == "address"] == []


def test_scan_paths_reports_file_and_line(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text('{"msg":"started"}\n{"msg":"phone 13800138000"}\n', encoding="utf-8")

    findings, scanned = scan_paths(tmp_path)

    assert scanned == 1
    assert [(item.line, item.kind) for item in findings] == [(2, "phone")]
    assert findings[0].format().endswith(":2: phone: 138****8000")


def test_main_exits_1_when_plaintext_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """正例验收：日志里写一条 13800138000 必须被抓到并以退出码 1 结束。"""
    (tmp_path / "dirty.log").write_text("elder phone 13800138000\n", encoding="utf-8")

    assert main(["--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "phone: 138****8000" in captured.out
    assert "dirty.log:1:" in captured.out


def test_main_exits_0_on_clean_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "clean.log").write_text('{"msg":"ok","trace_id":"abc"}\n', encoding="utf-8")

    assert main(["--root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "扫描 1 个文件" in captured.out


def test_main_exits_0_and_warns_when_root_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """目录不存在：不伪造「零命中」，也不让退出码撒谎 —— 打出「未扫到任何文件」。"""
    missing = tmp_path / "no-such-dir"

    assert main(["--root", str(missing)]) == 0
    captured = capsys.readouterr()
    assert "未扫到任何文件" in captured.err


def test_main_honours_glob_option(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("ok\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("13800138000\n", encoding="utf-8")

    assert main(["--root", str(tmp_path), "--glob", "*.log"]) == 0
    assert main(["--root", str(tmp_path), "--glob", "*.txt"]) == 1


def test_redaction_rules_table_is_single_source() -> None:
    """规则表里四类敏感信息都在，且文本替换用的规则不含地址（地址只扫描）。"""
    kinds = {rule.kind for rule in REDACTION_RULES}
    assert kinds == {"secret", "id_card", "phone", "address"}
    assert {rule.kind for rule in REDACTION_RULES if rule.in_text} == {"secret", "id_card", "phone"}


def test_detect_sensitive_masks_fragment() -> None:
    """扫描结果里的片段本身已打码：验收报告不该再泄露一次。"""
    hits = dict(detect_sensitive("号码 13800138000"))
    assert hits == {"phone": "138****8000"}


# ======================================================================
# 八、日志接入（app/core/logging.py）
# ======================================================================


def _make_record(msg: str, **extra: object) -> logging.LogRecord:
    """造一条不经过 logger 的 LogRecord，避免测试依赖全局 logging 配置。"""
    record = logging.LogRecord(
        name="tests.redact",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _format(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


def test_json_formatter_redacts_message() -> None:
    payload = _format(_make_record("联系 13800138000 与 110101199003071234"))
    assert payload["msg"] == "联系 138****8000 与 110101********1234"


def test_json_formatter_redacts_extra_by_key_name() -> None:
    """extra 里值不像手机号（这里是正常号码）也按键名脱敏；口令整体打码。"""
    payload = _format(_make_record("ok", user_phone="13800138000", password="hunter2"))
    assert payload["user_phone"] == "138****8000"
    assert payload["password"] == "***"


def test_json_formatter_keeps_existing_field_shape() -> None:
    """接入脱敏不能改字段集合与取值口径（既有日志断言仍要绿）。"""
    payload = _format(_make_record("ok", service="anyu"))
    assert set(payload) >= {"ts", "level", "logger", "msg", "trace_id", "service"}
    assert payload["trace_id"] == "-"
    assert payload["logger"] == "tests.redact"
    assert payload["service"] == "anyu"
