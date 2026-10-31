"""JS 解析（esprima AST + tokenizer 兜底）的测试。

重点不是"能识别几个特性"，而是三件事：
1. `obj?.name` 这种 **esprima 解析不了的语法** 照样能被检出；
2. 解析失败时 API 检测不能陪葬（`arr.at(-1)` 仍要报）；
3. 字符串/注释/正则里的内容**不能**被误报。
"""

from __future__ import annotations

import pytest

from app.core.schemas import DetectRequest
from app.l1.detector import detect
from app.l1.parsers.js_parser import tokenize

DEFAULT_TARGETS = ["chrome 90", "safari 14.1", "firefox 88", "edge 90"]


def run(js: str, targets: list[str] | None = None):
    return detect(DetectRequest(js=js, targets=targets or DEFAULT_TARGETS))


# --------------------------------------------------------------------------- #
# 验收核心：可选链
# --------------------------------------------------------------------------- #

def test_optional_chaining_is_detected_on_old_safari() -> None:
    body = run("const x = obj?.name;", ["safari 12"])
    assert body.status == "success"
    assert len(body.issues) == 1

    issue = body.issues[0]
    assert issue.type == "js"
    assert issue.bcd_path == "javascript.operators.optional_chaining"
    assert issue.line == 1
    assert issue.column == 14          # `?.` 的列号（1-based）：`const x = obj` 占 13 列
    assert issue.severity == "high"    # 1/1 >= 0.5
    assert "requires Safari 13.1" in issue.source_data
    assert issue.confidence == "certain"


def test_optional_chaining_is_not_flagged_when_targets_support_it() -> None:
    """默认目标集全支持可选链（Safari 13.1 起）→ 0 条才是正确结果。"""
    body = run("const x = obj?.name;")
    assert body.issues == []
    assert body.unknown_features == []


@pytest.mark.parametrize(
    ("js", "path", "targets"),
    [
        ("const a = b ?? 1;", "javascript.operators.nullish_coalescing", ["safari 12"]),
        ("let a; a ??= 1;", "javascript.operators.nullish_coalescing_assignment", ["safari 13"]),
        ("let a; a &&= 1;", "javascript.operators.logical_and_assignment", ["safari 13"]),
        ("let a; a ||= 1;", "javascript.operators.logical_or_assignment", ["safari 13"]),
        ("const n = 2 ** 10;", "javascript.operators.exponentiation", ["safari 10"]),
        ("const big = 123n;", "javascript.builtins.BigInt", ["safari 13"]),
        ("const n = 1_000_000;", "javascript.grammar.numeric_separators", ["safari 12"]),
        ("const f = (a, ...rest) => rest;", "javascript.operators.spread", ["safari 7"]),
        ("const s = `${a?.b}`;", "javascript.operators.optional_chaining", ["safari 12"]),
    ],
)
def test_syntax_features_are_detected(js: str, path: str, targets: list[str]) -> None:
    body = run(js, targets)
    assert path in {issue.bcd_path for issue in body.issues}, [i.bcd_path for i in body.issues]


def test_private_field_and_static_block_survive_parse_failure() -> None:
    """类字段/静态块都在 ES2022，esprima 必失败 → 必须靠 tokenizer 兜住。"""
    js = "class A {\n  #secret = 1;\n  static { this.ready = true; }\n}\n"
    body = run(js, ["safari 14.0"])
    paths = {issue.bcd_path for issue in body.issues}
    assert "javascript.classes.private_class_fields" in paths
    assert "javascript.classes.static_initialization_blocks" in paths


# --------------------------------------------------------------------------- #
# 兜底：解析失败不影响 API 检测
# --------------------------------------------------------------------------- #

def test_api_detection_still_works_when_esprima_fails() -> None:
    js = "const n = user?.name;\nconst last = items.at(-1);\n"
    body = run(js, ["safari 12"])
    paths = {issue.bcd_path for issue in body.issues}
    assert "javascript.operators.optional_chaining" in paths
    assert "javascript.builtins.Array.at" in paths


def test_known_root_chain_uses_exact_path() -> None:
    body = run("const last = Array.prototype.at.call(items, -1);", ["safari 12"])
    paths = {issue.bcd_path for issue in body.issues}
    assert "javascript.builtins.Array.at" in paths


def test_scope_prefix_is_stripped() -> None:
    """window.fetch → api.fetch，而不是 api.window.fetch。"""
    body = run("window.fetch('/api');", ["safari 9"])
    assert [issue.bcd_path for issue in body.issues] == ["api.fetch"]


def test_document_alias_maps_to_interface() -> None:
    body = run("document.querySelector('.a');", ["safari 3"])
    paths = {issue.bcd_path for issue in body.issues}
    assert "api.Document.querySelector" in paths


def test_method_table_marks_inferred_confidence() -> None:
    """对象类型未知时按方法名推断，必须如实标 context_unknown。"""
    body = run("const last = items.at(-1);")
    issue = next(i for i in body.issues if i.bcd_path == "javascript.builtins.Array.at")
    assert issue.confidence == "context_unknown"
    assert issue.feature == "at()"


# --------------------------------------------------------------------------- #
# 不误报
# --------------------------------------------------------------------------- #

def test_local_variables_are_not_reported_as_unknown_features() -> None:
    body = run("const obj = {};\nobj.someMethod();\nfunction f(a) { return a.value; }")
    assert body.unknown_features == []
    assert body.issues == []


@pytest.mark.parametrize(
    "js",
    [
        'const s = "obj?.name ?? 1";',
        "// const x = obj?.name;\nconst y = 1;",
        "/* obj?.name ?? 1 */ const y = 2;",
        "const re = /a?.b\\/c/g;",
        "const t = `plain ${1} text`;",
    ],
)
def test_strings_comments_and_regex_are_not_misdetected(js: str) -> None:
    body = run(js, ["safari 10"])
    assert [i.bcd_path for i in body.issues] == [], [i.bcd_path for i in body.issues]


def test_tokenizer_tracks_positions_across_lines() -> None:
    tokens = tokenize("a\n  b?.c\n")
    by_value = {token.value: (token.line, token.column) for token in tokens}
    assert by_value["a"] == (1, 1)
    assert by_value["b"] == (2, 3)
    assert by_value["?."] == (2, 4)


def test_template_expression_is_scanned_as_code() -> None:
    body = run("const s = `x${a?.b}y`;", ["safari 10"])
    assert [i.bcd_path for i in body.issues] == ["javascript.operators.optional_chaining"]
