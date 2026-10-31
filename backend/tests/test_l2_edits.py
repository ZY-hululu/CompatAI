"""``fix_edits``（行区间替换）的测试。

这是"多条修复能否合并"的核心：
* 单条替换只是片段，单独解析没有意义；
* N 条替换按行号倒序应用后，**必须**得到一份合法、且问题确实归零的代码。
"""

from __future__ import annotations

import json

import pytest

from app.core.schemas import DetectRequest, FixEdit, SourceCode
from app.l1.detector import detect
from app.l2.edits import apply_edits, count_lines
from app.l2.validation import validate

CSS = """.card {
  display: flex;
  aspect-ratio: 16 / 9;
  accent-color: red;
  container-type: inline-size;
}
"""
JS = "const x = user?.name;\nconst y = 1;\n"
TARGETS = ["safari 12"]


def code() -> SourceCode:
    return SourceCode(css=CSS, js=JS)


def issues_of(source: SourceCode):
    body = detect(DetectRequest(css=source.css, js=source.js, targets=TARGETS))
    return body.issues, [t.label for t in body.targets_resolved]


def expl(issue_id: str, text: str, edits: list[dict]) -> dict:
    return {"issue_id": issue_id, "explain_text": text, "fix_diff": "", "fix_edits": edits}


def payload(*items: dict) -> str:
    return json.dumps({"explanations": list(items)}, ensure_ascii=False)


# 版本校验是逐 issue 隔离的，所以三条 CSS 问题的措辞必须各自匹配其 source_data
ASPECT_TEXT = "Safari 12 会忽略该声明，样式回退；需要 Safari 15。"
ACCENT_TEXT = "Safari 12 会忽略该声明，样式回退；需要 Safari 15.4。"
CONTAINER_TEXT = "Safari 12 会忽略该声明，样式回退；需要 Safari 16。"
CSS_TEXT = ACCENT_TEXT
JS_TEXT = "Safari 12 解析阶段直接抛 SyntaxError，整个脚本不执行；需要 Safari 13.1。"


# --------------------------------------------------------------------------- #
# apply_edits 本身
# --------------------------------------------------------------------------- #

def test_apply_single_edit() -> None:
    result = apply_edits(SourceCode(css="a\nb\nc"), [FixEdit(language="css", start_line=2, end_line=2, replacement="B")])
    assert result.css == "a\nB\nc"


def test_apply_multiple_edits_independent_of_order() -> None:
    edits = [
        FixEdit(language="css", start_line=1, end_line=1, replacement="A"),
        FixEdit(language="css", start_line=3, end_line=3, replacement="C"),
    ]
    forward = apply_edits(SourceCode(css="1\n2\n3"), edits).css
    backward = apply_edits(SourceCode(css="1\n2\n3"), list(reversed(edits))).css
    assert forward == backward == "A\n2\nC"


def test_apply_delete_and_multiline_replacement() -> None:
    result = apply_edits(
        SourceCode(css="a\nb\nc\nd"),
        [
            FixEdit(language="css", start_line=2, end_line=2, replacement=""),          # 删除 b
            FixEdit(language="css", start_line=4, end_line=4, replacement="D1\nD2"),    # 一行变两行
        ],
    )
    assert result.css == "a\nc\nD1\nD2"


def test_apply_keeps_other_languages_untouched() -> None:
    result = apply_edits(
        SourceCode(css="a", js="x", html="<div></div>"),
        [FixEdit(language="js", start_line=1, end_line=1, replacement="X")],
    )
    assert (result.css, result.js, result.html) == ("a", "X", "<div></div>")


def test_count_lines_matches_l1_convention() -> None:
    assert count_lines("a\nb\n") == count_lines("a\nb") == 2
    assert count_lines("") == 0


# --------------------------------------------------------------------------- #
# 合并：三条修复叠加 → 问题归零（B 方案的核心价值）
# --------------------------------------------------------------------------- #

def test_three_edits_merge_and_clear_all_issues() -> None:
    source = code()
    issues, targets = issues_of(source)
    assert [issue.bcd_path for issue in issues[:3]] == [
        "css.properties.aspect-ratio",
        "css.properties.accent-color",
        "css.properties.container-type",
    ]

    raw = payload(
        expl("issue_1", ASPECT_TEXT, [{"language": "css", "start_line": 3, "end_line": 3, "replacement": "  margin: 0;"}]),
        expl("issue_2", ACCENT_TEXT, [{"language": "css", "start_line": 4, "end_line": 4, "replacement": ""}]),
        expl("issue_3", CONTAINER_TEXT, [{"language": "css", "start_line": 5, "end_line": 5, "replacement": "  width: 100%;"}]),
        expl("issue_4", JS_TEXT, [{"language": "js", "start_line": 1, "end_line": 1, "replacement": "const x = user && user.name;"}]),
    )

    result = validate(raw, issues, targets, source)
    assert result.ok, result.problems

    merged = apply_edits(source, [edit for explanation in result.explanations for edit in explanation.fix_edits])
    after = detect(DetectRequest(css=merged.css, js=merged.js, targets=TARGETS))

    assert after.issues == [], [(issue.feature, issue.bcd_path) for issue in after.issues]
    assert "margin: 0;" in merged.css and "accent-color" not in merged.css


# --------------------------------------------------------------------------- #
# 拦截：范围 / 行号前缀 / 区间重叠 / 合成后语法
# --------------------------------------------------------------------------- #

def _one_edit_result(edit: dict, source: SourceCode | None = None):
    source = source or code()
    issues, targets = issues_of(source)
    raw = payload(
        expl("issue_1", CSS_TEXT, [edit]),
        expl("issue_2", CSS_TEXT, []),
        expl("issue_3", CSS_TEXT, []),
        expl("issue_4", JS_TEXT, []),
    )
    return validate(raw, issues, targets, source)


def test_out_of_range_edit_is_rejected() -> None:
    result = _one_edit_result({"language": "css", "start_line": 12, "end_line": 12, "replacement": "x"})
    assert not result.ok
    assert any("行号越界" in problem for problem in result.problems), result.problems


def test_line_number_prefix_is_rejected() -> None:
    result = _one_edit_result(
        {"language": "css", "start_line": 3, "end_line": 3, "replacement": "   3 |   margin: 0;"}
    )
    assert not result.ok
    assert any("行号前缀" in problem for problem in result.problems), result.problems


def test_overlapping_edits_are_rejected() -> None:
    source = code()
    issues, targets = issues_of(source)
    raw = payload(
        expl("issue_1", CSS_TEXT, [{"language": "css", "start_line": 3, "end_line": 4, "replacement": "  margin: 0;"}]),
        expl("issue_2", CSS_TEXT, [{"language": "css", "start_line": 4, "end_line": 5, "replacement": ""}]),
        expl("issue_3", CSS_TEXT, []),
        expl("issue_4", JS_TEXT, []),
    )
    result = validate(raw, issues, targets, source)

    assert not result.ok
    assert any("重叠" in problem for problem in result.problems), result.problems


def test_broken_css_after_merge_is_rejected() -> None:
    """单条替换看着无害，但合成后整份 CSS 解析失败 → 必须拦。"""
    result = _one_edit_result(
        {"language": "css", "start_line": 3, "end_line": 3, "replacement": "  aspect-ratio 16 / 9;"}
    )
    assert not result.ok
    assert any("CSS 出现语法错误" in problem or "语法错误" in problem for problem in result.problems)


def test_unbalanced_js_after_merge_is_rejected() -> None:
    source = code()
    issues, targets = issues_of(source)
    raw = payload(
        expl("issue_1", CSS_TEXT, []),
        expl("issue_2", CSS_TEXT, []),
        expl("issue_3", CSS_TEXT, []),
        expl("issue_4", JS_TEXT, [{"language": "js", "start_line": 1, "end_line": 1, "replacement": "function f() {"}]),
    )
    result = validate(raw, issues, targets, source)

    assert not result.ok
    assert any("不配平" in problem for problem in result.problems), result.problems


def test_edit_on_empty_language_is_rejected() -> None:
    source = SourceCode(css=CSS)          # 没有 js
    issues, targets = issues_of(source)
    assert [issue.type for issue in issues] == ["css"] * 3

    raw = payload(
        expl("issue_1", CSS_TEXT, [{"language": "js", "start_line": 1, "end_line": 1, "replacement": "x"}]),
        expl("issue_2", CSS_TEXT, []),
        expl("issue_3", CSS_TEXT, []),
    )
    result = validate(raw, issues, targets, source)

    assert not result.ok
    assert any("源码为空" in problem for problem in result.problems), result.problems
