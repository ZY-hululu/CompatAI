"""L3 回灌验证的测试。

三条主线：
1. **静态回灌**：resolved / remaining / new_issues 必须按 (type, bcd_path, feature) 比对，
   行号位移不能把"同一个问题"算成"已解决"；
2. **行为对比**：jsdom 前后 DOM 快照的结构差异；
3. **强制超时**：`while (true) {}` 必须被 worker.terminate() 打断，
   而不是把请求线程拖死（这是用户明确点名的风险）。
"""

from __future__ import annotations

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.schemas import SourceCode, VerifyRequest
from app.l3.recheck import run_snapshot, verify
from app.main import app

requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能跑 jsdom")

CSS_ISSUE = ".card { accent-color: red; }"
CSS_CLEAN = ".card { color: red; }"
CSS_OTHER_ISSUE = ".card { container-type: inline-size; }"


def make(original: dict, fixed: dict, targets=("safari 12",)) -> VerifyRequest:
    return VerifyRequest(
        original=SourceCode(**original),
        fixed=SourceCode(**fixed),
        targets=list(targets),
    )


# --------------------------------------------------------------------------- #
# 静态回灌
# --------------------------------------------------------------------------- #

def test_removing_the_issue_marks_it_resolved() -> None:
    response = verify(make({"css": CSS_ISSUE}, {"css": CSS_CLEAN}))

    assert response.before_count == 1
    assert response.after_count == 0
    assert response.resolved == ["issue_1"]
    assert response.remaining == []
    assert response.new_issues == []


def test_untouched_code_stays_remaining() -> None:
    response = verify(make({"css": CSS_ISSUE}, {"css": CSS_ISSUE}))

    assert response.resolved == []
    assert response.remaining == ["issue_1"]
    assert response.new_issues == []


def test_fix_that_introduces_something_new_is_reported() -> None:
    """把 accent-color 换成 container-type：原来的问题解决了，但引入了新问题。"""
    response = verify(make({"css": CSS_ISSUE}, {"css": CSS_OTHER_ISSUE}))

    assert response.resolved == ["issue_1"]          # 旧问题没了
    assert response.after_count == 1
    assert len(response.new_issues) == 1
    assert response.new_issues[0].bcd_path == "css.properties.container-type"


def test_line_shift_does_not_count_as_resolved() -> None:
    """同一个问题被挪到别的行，仍然算"未解决"。"""
    original = ".a{}\n.b{}\n.c { accent-color: red; }"
    fixed = ".x{}\n.y{}\n.z{}\n.w{}\n.c { accent-color: red; }"
    response = verify(make({"css": original}, {"css": fixed}))

    assert response.resolved == []
    assert response.remaining == ["issue_1"]


def test_multiple_languages_are_compared_independently() -> None:
    original = {"css": CSS_ISSUE, "js": "const a = b?.c;", "html": "<div inert>x</div>"}
    fixed = {"css": CSS_CLEAN, "js": "const a = b ? b.c : undefined;", "html": "<div>x</div>"}
    response = verify(make(original, fixed))

    assert response.before_count == 3
    assert response.after_count == 0
    assert sorted(response.resolved) == ["issue_1", "issue_2", "issue_3"]


# --------------------------------------------------------------------------- #
# resolved 不等于真修好：needs_review
# --------------------------------------------------------------------------- #

def test_needs_review_flags_feature_that_only_became_unrecognizable() -> None:
    """`<dialog open>` 被改成 `<div open>`：元素问题真的没了，但 `open` 属性只是"查不到"。"""
    response = verify(make({"html": "<dialog open>x</dialog>"}, {"html": "<div open>x</div>"}))

    assert response.after_count == 0
    assert response.unknown_features == ["<div open>"]
    # 两条都被算作 resolved，但 open 那条要标待复核
    assert len(response.resolved) == 2
    assert response.needs_review == ["issue_2"], response.needs_review


def test_needs_review_empty_when_fix_leaves_nothing_unknown() -> None:
    response = verify(
        make({"html": '<dialog open>x</dialog>'}, {"html": '<div role="dialog">x</div>'})
    )

    assert response.after_count == 0
    assert response.unknown_features == []
    assert response.needs_review == []


def test_generic_tag_name_does_not_cause_false_positive() -> None:
    """`<div inert>` 与 `<div open>` 共享标签名 div，不能让前者被误标待复核。"""
    original = {"html": '<div inert>a</div>\n<dialog open>b</dialog>'}
    fixed = {"html": '<div aria-hidden="true">a</div>\n<div open>b</div>'}
    response = verify(make(original, fixed))

    # inert 被真正去掉了（aria-* 不参与检测），只有 dialog 的 open 变成了"查不到"
    assert response.needs_review == ["issue_3"], response.needs_review
    assert response.unknown_features == ["<div open>"]


def test_clean_fix_has_no_review_flags() -> None:
    response = verify(make({"css": CSS_ISSUE}, {"css": CSS_CLEAN}))

    assert response.resolved == ["issue_1"]
    assert response.unknown_features == []
    assert response.needs_review == []


# --------------------------------------------------------------------------- #
# jsdom 行为对比
# --------------------------------------------------------------------------- #

@requires_node
def test_dom_diff_detects_added_nodes() -> None:
    original = {
        "html": "<div id='root'>x</div>",
        "js": "document.getElementById('root').textContent = 'hi';",
    }
    fixed = {
        "html": "<div id='root'>x</div>",
        "js": "document.getElementById('root').textContent = 'hi';\n"
              "const span = document.createElement('span');\n"
              "document.body.appendChild(span);",
    }
    response = verify(make(original, fixed), )

    assert response.dom_diff.ran is True
    assert response.dom_diff.changed is True
    assert response.dom_diff.added_nodes >= 1
    assert response.dom_diff.errors == []


@requires_node
def test_dom_diff_reports_execution_errors() -> None:
    original = {"js": "const a = 1;"}
    fixed = {"js": "throw new Error('boom');"}
    response = verify(make(original, fixed))

    assert response.dom_diff.ran is True
    assert any("修复后" in message and "boom" in message for message in response.dom_diff.errors)


@requires_node
def test_dom_diff_skipped_without_js_or_html() -> None:
    response = verify(make({"css": CSS_ISSUE}, {"css": CSS_CLEAN}))

    assert response.dom_diff.ran is False
    assert "没有 JS/HTML" in response.dom_diff.errors[0]


@requires_node
def test_timeout_terminates_worker_instead_of_hanging() -> None:
    """死循环必须被打断，并且给出明确原因，而不是把请求挂死。"""
    settings = get_settings().model_copy(update={"jsdom_timeout_seconds": 2.0})
    started = time.time()
    snapshot = run_snapshot(SourceCode(js="while (true) {}"), settings, timeout=2.0)
    elapsed = time.time() - started

    assert snapshot is not None
    assert snapshot.timed_out is True
    assert "超时" in snapshot.errors[0]
    assert elapsed < 20, f"超时机制失效，耗时 {elapsed:.1f}s"


@requires_node
def test_verify_marks_partial_when_js_times_out() -> None:
    settings = get_settings().model_copy(update={"jsdom_timeout_seconds": 2.0})
    response = verify(
        make({"js": "while (true) {}"}, {"js": "const a = 1;"}),
        settings=settings,
    )

    assert response.status == "partial"
    assert any("超时" in message for message in response.dom_diff.errors)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

@requires_node
def test_http_verify_returns_contract_shape() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/verify",
        json={
            "original": {"css": CSS_ISSUE, "js": "", "html": ""},
            "fixed": {"css": CSS_CLEAN, "js": "", "html": ""},
            "targets": ["safari 12"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["before_count"] == 1
    assert body["after_count"] == 0
    assert body["resolved"] == ["issue_1"]
    assert set(body["dom_diff"]) >= {"ran", "changed", "added_nodes", "removed_nodes", "errors"}
    assert body["unknown_features"] == []
    assert body["needs_review"] == []


def test_http_verify_rejects_bad_targets() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/verify",
        json={
            "original": {"css": "a{}"},
            "fixed": {"css": "a{}"},
            "targets": ["netscape 4"],
        },
    )
    assert response.status_code == 422
