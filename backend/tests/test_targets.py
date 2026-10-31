"""targets.py 单元测试。

版本解析与比较是 L1 正确性的地基：这里错一位，后面所有 issue 都是错的，
而且错得很"安静"（不会抛异常，只会得出错误结论）。所以先把它钉死。
"""

from __future__ import annotations

import pytest

from app.l1.targets import (
    MAX_TARGETS,
    TargetFormatError,
    compare_versions,
    dedupe_key,
    parse_targets,
    parse_version,
    version_at_least,
)


# --------------------------------------------------------------------------- #
# 版本号解析：必须支持多级版本号，且不接受模糊写法
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15", (15,)),
        ("15.0", (15, 0)),
        ("12.1", (12, 1)),
        ("12.1.2", (12, 1, 2)),
        ("120", (120,)),
        ("90.0.4430", (90, 0, 4430)),
    ],
)
def test_parse_version_multilevel(text: str, expected: tuple[int, ...]) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "  ", "latest", "≥90", ">=90", "9x", "12.", ".1", "1.2.3.4.5", "-1"])
def test_parse_version_rejects_fuzzy_input(text: str) -> None:
    with pytest.raises(TargetFormatError):
        parse_version(text)


# --------------------------------------------------------------------------- #
# 版本比较：短的一方补 0，按数值而非字典序
# --------------------------------------------------------------------------- #

def test_short_key_pads_with_zero() -> None:
    assert compare_versions((15, 0), (15,)) == 0
    assert compare_versions((15,), (15, 0)) == 0


def test_numeric_not_lexicographic() -> None:
    # 字符串比较会把 "10.10" 排在 "10.9" 前面，必须避免
    assert compare_versions((10, 10), (10, 9)) == 1
    assert compare_versions((90,), (100,)) == -1


def test_fifteen_zero_is_below_fifteen_one() -> None:
    """用户选 safari 15.0、特性要求 15.1 —— 必须判定为不支持。"""
    assert version_at_least((15, 0), (15, 1)) is False
    assert version_at_least((15, 1), (15, 0)) is True
    assert version_at_least((15, 0), (15, 0)) is True


def test_safari_12_1_vs_required_12() -> None:
    assert version_at_least((12, 1), (12,)) is True


def test_dedupe_key_trims_trailing_zeros() -> None:
    assert dedupe_key((15, 0)) == (15,)
    assert dedupe_key((15, 0, 0)) == (15,)
    assert dedupe_key((15, 0, 1)) == (15, 0, 1)
    assert dedupe_key((0,)) == (0,)


# --------------------------------------------------------------------------- #
# 目标解析
# --------------------------------------------------------------------------- #

def test_parse_targets_basic() -> None:
    parsed = parse_targets(["chrome 90", "safari 12.1"])
    assert [t.label for t in parsed] == ["chrome 90", "safari 12.1"]
    assert parsed[1].browser == "safari"
    assert parsed[1].key == (12, 1)


def test_parse_targets_normalizes_alias_to_bcd_id() -> None:
    (target,) = parse_targets(["ios_saf 15.0"])
    assert target.browser == "safari_ios"
    assert target.label == "safari_ios 15.0"
    assert target.raw == "ios_saf 15.0"


def test_parse_targets_case_insensitive_and_whitespace_tolerant() -> None:
    (target,) = parse_targets(["  Safari   15.0 "])
    assert target.browser == "safari"
    assert target.version == "15.0"


def test_parse_targets_dedupes_equivalent_versions() -> None:
    """90 与 90.0 是同一个目标：重复会让 severity 的分母虚增。"""
    parsed = parse_targets(["chrome 90", "chrome 90.0", "Chrome 90"])
    assert len(parsed) == 1
    assert parsed[0].raw == "chrome 90"


def test_parse_targets_keeps_order() -> None:
    parsed = parse_targets(["firefox 115", "chrome 120", "safari 15.0"])
    assert [t.browser for t in parsed] == ["firefox", "chrome", "safari"]


def test_parse_targets_reports_all_invalid_items() -> None:
    """不能报第一个就返回：前端要一次改完所有错误。"""
    with pytest.raises(TargetFormatError) as excinfo:
        parse_targets(["chrome", "safari abc", "netscape 1", "firefox 115"])
    message = str(excinfo.value)
    assert "targets[0]" in message
    assert "targets[1]" in message
    assert "targets[2]" in message
    assert "targets[3]" not in message


def test_parse_targets_empty_rejected() -> None:
    with pytest.raises(TargetFormatError, match="不能为空"):
        parse_targets([])


def test_parse_targets_too_many_rejected() -> None:
    with pytest.raises(TargetFormatError, match="最多"):
        parse_targets(["chrome 1"] * (MAX_TARGETS + 1))
