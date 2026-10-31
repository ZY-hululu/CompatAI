"""L2 后置校验的单元测试 —— 证明"模型编造会被拦住"。

每条用例对应一条闸门：
白名单 id / 版本封闭 / 解释口径 / 修复代码语法门禁 / JSON 抽取。
"""

from __future__ import annotations

import json

import pytest

from app.core.schemas import DetectRequest, SourceCode
from app.l1.detector import detect
from app.l2.validation import allowed_versions, extract_json, validate

CSS = ".a { accent-color: red; }"
JS = "const x = user?.name;"

# 校验 fix_edits 的行号需要原始源码
CODE = SourceCode(css=CSS, js=JS)


def make_issues(targets=("safari 12",)):
    body = detect(DetectRequest(css=CSS, js=JS, targets=list(targets)))
    labels = [target.label for target in body.targets_resolved]
    return body.issues, labels


def item(issue_id: str, text: str, **overrides) -> dict:
    base = {
        "issue_id": issue_id,
        "explain_text": text,
        "fix_diff": "",
        "fix_code_css": None,
        "fix_code_js": None,
        "fix_code_html": None,
    }
    base.update(overrides)
    return base


def payload(*items: dict) -> str:
    return json.dumps({"explanations": list(items)}, ensure_ascii=False)


CSS_TEXT = "Safari 12 会忽略该 CSS 声明，样式回退到默认表现；需要 Safari 15.4 才支持。"
JS_TEXT = "Safari 12 在解析阶段直接抛 SyntaxError，整个脚本都不会执行；需要 Safari 13.1。"


# --------------------------------------------------------------------------- #
# 基线
# --------------------------------------------------------------------------- #

def test_issue_order_is_css_then_js() -> None:
    issues, _ = make_issues()
    assert [issue.type for issue in issues] == ["css", "js"]
    assert [issue.id for issue in issues] == ["issue_1", "issue_2"]


def test_valid_output_passes() -> None:
    issues, targets = make_issues()
    result = validate(payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT)), issues, targets, CODE)

    assert result.ok, result.problems
    assert [explanation.issue_id for explanation in result.explanations] == ["issue_1", "issue_2"]


def test_output_is_reordered_to_l1_order() -> None:
    issues, targets = make_issues()
    result = validate(payload(item("issue_2", JS_TEXT), item("issue_1", CSS_TEXT)), issues, targets, CODE)
    assert result.ok, result.problems
    assert [explanation.issue_id for explanation in result.explanations] == ["issue_1", "issue_2"]


def test_markdown_fence_is_tolerated() -> None:
    issues, targets = make_issues()
    wrapped = "```json\n" + payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT)) + "\n```"
    assert validate(wrapped, issues, targets, CODE).ok


# --------------------------------------------------------------------------- #
# 闸门 1：白名单 id
# --------------------------------------------------------------------------- #

def test_unknown_issue_id_is_rejected() -> None:
    issues, targets = make_issues()
    result = validate(payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT), item("issue_99", JS_TEXT)), issues, targets, CODE)

    assert not result.ok
    assert any("不在白名单" in problem for problem in result.problems), result.problems


def test_duplicate_issue_id_is_rejected() -> None:
    issues, targets = make_issues()
    result = validate(payload(item("issue_1", CSS_TEXT), item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT)), issues, targets, CODE)

    assert not result.ok
    assert any("重复" in problem for problem in result.problems), result.problems


def test_missing_issue_is_rejected() -> None:
    issues, targets = make_issues()
    result = validate(payload(item("issue_1", CSS_TEXT)), issues, targets, CODE)

    assert not result.ok
    assert any("缺少" in problem for problem in result.problems), result.problems


# --------------------------------------------------------------------------- #
# 闸门 2：版本封闭
# --------------------------------------------------------------------------- #

def test_invented_version_is_rejected() -> None:
    """依据写 15.4，模型写成 15 → 必须拦下。"""
    issues, targets = make_issues()
    bad = "Safari 12 会忽略该 CSS 声明，需要 Safari 15 才支持。"
    result = validate(payload(item("issue_1", bad), item("issue_2", JS_TEXT)), issues, targets, CODE)

    assert not result.ok
    combined = "；".join(result.problems)
    assert "版本号" in combined and "15" in combined, result.problems


def test_bare_invented_version_is_rejected() -> None:
    issues, targets = make_issues()
    bad = "Safari 12 不支持该声明，从 16.2 起才有，样式重新回退。"
    result = validate(payload(item("issue_1", bad), item("issue_2", JS_TEXT)), issues, targets, CODE)

    assert not result.ok
    assert any("16.2" in problem for problem in result.problems), result.problems


def test_version_from_target_list_is_allowed() -> None:
    """解释里提"目标浏览器 12"是合法的（来自 targets）。"""
    issues, targets = make_issues()
    assert "12" in allowed_versions(issues[0], targets)


def test_versions_are_scoped_per_issue() -> None:
    """CSS issue 里出现 JS issue 的版本号（13.1）应被拦下。"""
    issues, targets = make_issues()
    mixed = "Safari 12 会忽略该样式，需要 Safari 13.1 才支持。"
    result = validate(payload(item("issue_1", mixed), item("issue_2", JS_TEXT)), issues, targets, CODE)

    assert not result.ok
    assert any("13.1" in problem for problem in result.problems), result.problems


# --------------------------------------------------------------------------- #
# 闸门 3：解释口径
# --------------------------------------------------------------------------- #

def test_api_style_wording_on_syntax_issue_is_rejected() -> None:
    """语法级问题被写成"运行时 undefined" → 口径错误，必须拦。"""
    issues, targets = make_issues()
    wrong = "Safari 12 没有这个成员，运行时得到 undefined 并抛 TypeError。"
    result = validate(payload(item("issue_1", CSS_TEXT), item("issue_2", wrong)), issues, targets, CODE)

    assert not result.ok
    assert any("issue_2" in problem and "口径" in problem for problem in result.problems), result.problems


def test_syntax_wording_on_api_issue_is_rejected() -> None:
    """反向也要拦：API 级问题被写成"解析失败"。"""
    body = detect(DetectRequest(js="const v = items.at(-1);", targets=["safari 12"]))
    issues = body.issues
    targets = [target.label for target in body.targets_resolved]
    assert issues[0].bcd_path == "javascript.builtins.Array.at"

    wrong = "Safari 12 在解析阶段抛 SyntaxError，整个脚本都不会执行；需要 Safari 15.4。"
    result = validate(payload(item("issue_1", wrong)), issues, targets, CODE)

    assert not result.ok
    assert any("口径" in problem for problem in result.problems), result.problems


# --------------------------------------------------------------------------- #
# 闸门 4：修复代码语法门禁
# --------------------------------------------------------------------------- #

def test_broken_css_fix_is_rejected() -> None:
    issues, targets = make_issues()
    result = validate(
        payload(item("issue_1", CSS_TEXT, fix_code_css=".a { accent-color red; }"), item("issue_2", JS_TEXT)),
        issues,
        targets,
        CODE,
    )

    assert not result.ok
    assert any("语法错误" in problem for problem in result.problems), result.problems


def test_unbalanced_js_fix_is_rejected() -> None:
    issues, targets = make_issues()
    result = validate(
        payload(
            item("issue_1", CSS_TEXT),
            item("issue_2", JS_TEXT, fix_code_js="function f() {", fix_diff="包一层函数"),
        ),
        issues,
        targets,
        CODE,
    )

    assert not result.ok
    assert any("不配平" in problem for problem in result.problems), result.problems


def test_unclosed_html_fix_is_rejected() -> None:
    issues, targets = make_issues()
    result = validate(
        payload(item("issue_1", CSS_TEXT, fix_code_html="<div>"), item("issue_2", JS_TEXT)),
        issues,
        targets,
        CODE,
    )

    assert not result.ok
    assert any("未闭合" in problem for problem in result.problems), result.problems


def test_valid_css_fix_passes() -> None:
    issues, targets = make_issues()
    fix = ".a { -webkit-accent-color: red; }"
    result = validate(payload(item("issue_1", CSS_TEXT, fix_code_css=fix), item("issue_2", JS_TEXT)), issues, targets, CODE)
    assert result.ok, result.problems


# --------------------------------------------------------------------------- #
# JSON 抽取
# --------------------------------------------------------------------------- #

def test_extract_json_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        extract_json("抱歉，我无法完成")


def test_extract_json_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        extract_json("[1, 2, 3]")


def test_schema_enum_is_built_from_l1_ids() -> None:
    from app.l2.prompts import build_schema

    schema = build_schema(["issue_1", "issue_2"])
    enum = schema["properties"]["explanations"]["items"]["properties"]["issue_id"]["enum"]
    assert enum == ["issue_1", "issue_2"]
    assert schema["additionalProperties"] is False
