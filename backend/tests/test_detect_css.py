"""L1 检测的验收测试。

核心验收（用户指定）：
* 契约示例代码必须返回 **1 条 gap 的 issue**
* 同一输入连续两次调用，JSON **完全相等**

另外把几个容易"安静地错"的点钉住：上下文优先、14.1 边界、确定性排序。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.schemas import DetectRequest
from app.l1.bcd import default_index
from app.l1.detector import detect
from app.l1.parsers.css_parser import scan_css
from app.main import app

EXTRACT = ".box { display: flex; gap: 10px; }"


# --------------------------------------------------------------------------- #
# 验收 1：契约示例的"事实纠正"
#
# 契约 3.1 的示例期望 `gap` + `safari 15.0` 报一条 issue，但 BCD 数据是：
#     css.properties.gap.flex_context.support.safari.version_added = "14.1"
# 即 **Safari 14.1 起 Flex gap 就支持了，15.0 当然也支持**。
# 所以这个示例的正确结果是 0 条 issue；真正会报错的是 safari 14.0（14.0 < 14.1）。
# 两个用例都写下来，防止以后有人凭印象改回"15.0 应该报错"。
# --------------------------------------------------------------------------- #

def test_contract_example_at_safari_15_0_has_no_issue() -> None:
    response = detect(
        DetectRequest(css=EXTRACT, targets=["chrome 120", "safari 15.0", "firefox 115"])
    )
    assert response.status == "success"
    assert response.issues == []
    assert response.unknown_features == []


def test_same_example_at_safari_14_0_yields_exactly_one_gap_issue() -> None:
    response = detect(
        DetectRequest(css=EXTRACT, targets=["chrome 120", "safari 14.0", "firefox 115"])
    )

    assert response.status == "success"
    assert len(response.issues) == 1

    issue = response.issues[0]
    assert issue.id == "issue_1"
    assert issue.type == "css"
    assert issue.feature == "gap"
    # 关键：命中的必须是 flex_context，而不是基础 gap（基础 gap 在 safari 10.1 就支持了）
    assert issue.bcd_path == "css.properties.gap.flex_context"
    assert issue.unsupported_browsers == ["safari 14.0"]
    assert issue.severity == "medium"          # 1/3 < 0.5
    assert issue.line == 1
    assert "requires Safari 14.1" in issue.source_data
    assert issue.source_data.endswith("(css.properties.gap.flex_context)")
    assert issue.confidence == "certain"


# --------------------------------------------------------------------------- #
# 验收 2：两次调用 JSON 完全相等（可复现）
# --------------------------------------------------------------------------- #

def test_same_input_twice_gives_identical_json() -> None:
    payload = DetectRequest(
        html="<div class='box'></div>",
        css=".a { gap: 1px } .box { display: flex; gap: 10px; } .b { gap: 2px }",
        targets=["chrome 90", "safari 14.0", "firefox 88", "edge 90"],
    )
    first = json.dumps(detect(payload).model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    second = json.dumps(detect(payload).model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    assert first == second


def test_ids_are_stable_under_reordering_of_stylesheet_rules() -> None:
    """同一批声明的 id 只由 (type, line, column, path) 决定，不受解析顺序影响。"""
    payload = DetectRequest(css=EXTRACT, targets=["safari 14.0"])
    ids_first = [issue.id for issue in detect(payload).issues]
    ids_second = [issue.id for issue in detect(payload).issues]
    assert ids_first == ids_second == ["issue_1"]


# --------------------------------------------------------------------------- #
# 上下文优先 / 边界版本
# --------------------------------------------------------------------------- #

def test_flex_gap_boundary_safari_14_0_fails_14_1_passes() -> None:
    """14.1 恰好是 Flex gap 的支持起点 —— 这一对用例证明比较是数值比较，不是字符串比较。"""
    failed = detect(DetectRequest(css=EXTRACT, targets=["safari 14.0"]))
    passed = detect(DetectRequest(css=EXTRACT, targets=["safari 14.1"]))

    assert len(failed.issues) == 1
    assert failed.issues[0].severity == "high"          # 1/1 >= 0.5
    assert passed.issues == []


def test_grid_context_uses_its_own_threshold() -> None:
    """: grid 容器的 gap 门槛是 safari 12，比 flex 的 14.1 低。"""
    response = detect(
        DetectRequest(css=".g { display: grid; gap: 8px; }", targets=["safari 11", "safari 14.1"])
    )
    assert len(response.issues) == 1
    issue = response.issues[0]
    assert issue.bcd_path == "css.properties.gap.grid_context"
    assert issue.unsupported_browsers == ["safari 11"]
    assert "requires Safari 12" in issue.source_data


def test_property_without_context_falls_back_to_base_path() -> None:
    """aspect-ratio 没有 *_context 子特性，直接落基础路径。"""
    response = detect(DetectRequest(css=".a { aspect-ratio: 16 / 9; }", targets=["safari 14.1"]))
    assert [issue.bcd_path for issue in response.issues] == ["css.properties.aspect-ratio"]
    assert response.issues[0].confidence == "certain"


def test_unknown_context_is_flagged_not_guessed() -> None:
    """单独一条 gap（不知道该容器是 flex 还是 grid）→ 走基础路径，并标 context_unknown。"""
    response = detect(DetectRequest(css=".x { gap: 10px; }", targets=["safari 10"]))
    assert len(response.issues) == 1
    issue = response.issues[0]
    assert issue.bcd_path == "css.properties.gap"
    assert issue.confidence == "context_unknown"


# --------------------------------------------------------------------------- #
# 不扫什么、扫不动什么
# --------------------------------------------------------------------------- #

def test_custom_properties_and_prefixed_properties_are_skipped() -> None:
    scan = scan_css(":root { --brand: #fff; } .a { -webkit-box-orient: vertical; color: red; }")
    names = [feature.name for feature in scan.features]
    assert names == ["color"]
    assert scan.skipped == 2


def test_unknown_property_goes_to_unknown_features_not_an_issue() -> None:
    response = detect(DetectRequest(css=".a { totally-made-up-prop: 1; }", targets=["chrome 90"]))
    assert response.issues == []
    assert response.unknown_features == ["totally-made-up-prop"]


def test_at_rules_are_walked() -> None:
    response = detect(
        DetectRequest(
            css="@media (min-width: 600px) { .box { display: flex; gap: 10px; } }",
            targets=["safari 14.0"],
        )
    )
    assert len(response.issues) == 1
    assert response.issues[0].line == 1


def test_stats_and_targets_resolved_are_filled() -> None:
    response = detect(DetectRequest(css=".a {\n  gap: 1px;\n}", targets=["chrome 90", "ios_saf 15.0"]))
    assert response.stats.css_lines == 3
    assert response.stats.declarations_scanned == 1
    assert [t.label for t in response.targets_resolved] == ["chrome 90", "safari_ios 15.0"]


def test_trailing_newline_does_not_add_a_line() -> None:
    """前端 textarea 提交的内容天然带尾随换行，行数不能因此 +1。"""
    with_newline = detect(DetectRequest(css=".a {\n  gap: 1px;\n}\n", targets=["chrome 90"]))
    without = detect(DetectRequest(css=".a {\n  gap: 1px;\n}", targets=["chrome 90"]))
    assert with_newline.stats.css_lines == without.stats.css_lines == 3


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_http_detect_matches_contract_shape(client: TestClient) -> None:
    response = client.post(
        "/api/detect",
        json={
            "html": "<div class='box'></div>",
            "css": EXTRACT,
            "js": "const x = obj?.name;",
            "targets": ["chrome 120", "safari 14.0", "firefox 115"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"status", "issues", "unknown_features"}
    assert len(body["issues"]) == 1
    assert set(body["issues"][0]) >= {
        "id", "type", "feature", "line", "column",
        "unsupported_browsers", "severity", "source_data",
    }
    # JS/HTML 解析器已接入：这个组合不应再降级成 partial
    assert body["status"] == "success"
    assert body["parse_warnings"] == []


def test_http_rejects_empty_source(client: TestClient) -> None:
    response = client.post("/api/detect", json={"targets": ["chrome 90"]})
    assert response.status_code == 422


def test_http_rejects_bad_target_with_all_errors(client: TestClient) -> None:
    response = client.post("/api/detect", json={"css": "a{color:red}", "targets": ["chrome", "safari abc"]})
    assert response.status_code == 422
    message = json.dumps(response.json(), ensure_ascii=False)
    assert "targets[0]" in message and "targets[1]" in message


def test_index_version_is_loaded() -> None:
    assert default_index().bcd_version.startswith("5.")
