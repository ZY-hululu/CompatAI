"""特性查询（``/api/lookup``）的测试。

查询是"用户直接问某个特性"，与检测共用同一份 BCD 与同一套版本比较，
所以这里重点验证：候选路径的生成顺序、未命中时的纠错、以及结论与 L1 一致。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TARGETS = ["chrome 90", "safari 14.1", "firefox 88"]


def lookup(feature: str, targets: list[str] | None = None, kind: str = "auto") -> dict:
    client = TestClient(app)
    response = client.post(
        "/api/lookup",
        json={"feature": feature, "targets": targets or TARGETS, "kind": kind},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 三类特性都能查到
# --------------------------------------------------------------------------- #

def test_css_property_is_resolved() -> None:
    body = lookup("accent-color")

    assert body["found"] is True
    assert body["kind"] == "css"
    assert body["bcd_path"] == "css.properties.accent-color"
    assert body["unsupported_browsers"] == ["chrome 90", "safari 14.1", "firefox 88"]
    assert "requires Chrome 93" in body["summary"]
    assert len(body["verdicts"]) == 3


def test_supported_feature_gets_positive_summary() -> None:
    body = lookup("gap", ["chrome 90"])

    assert body["found"] is True
    assert body["unsupported_browsers"] == []
    assert "supported in all targets" in body["summary"]


def test_js_api_is_resolved_case_insensitively() -> None:
    for feature in ("Array.at", "array.at"):
        body = lookup(feature)
        assert body["kind"] == "js", feature
        assert body["bcd_path"] == "javascript.builtins.Array.at", feature
        assert body["verdicts"][0]["requirement"] == "92"


def test_html_element_is_resolved() -> None:
    body = lookup("dialog")
    assert body["kind"] == "html"
    assert body["bcd_path"] == "html.elements.dialog"


def test_html_attribute_with_space_is_resolved() -> None:
    body = lookup("img loading")
    assert body["bcd_path"] == "html.elements.img.loading"
    assert body["unsupported_browsers"] == ["safari 14.1"]


def test_global_attribute_falls_back() -> None:
    body = lookup("inert")
    assert body["bcd_path"] == "html.global_attributes.inert"


# --------------------------------------------------------------------------- #
# 未命中与纠错
# --------------------------------------------------------------------------- #

def test_unknown_feature_returns_suggestions() -> None:
    body = lookup("accent-colour")       # 英式拼写

    assert body["found"] is False
    assert body["bcd_path"] is None
    assert "css.properties.accent-color" in body["alternatives"]


def test_totally_unknown_feature_is_not_found_without_noise() -> None:
    body = lookup("zzzzzz-not-a-feature")
    assert body["found"] is False
    assert body["alternatives"] == []


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #

def test_blank_feature_is_rejected() -> None:
    client = TestClient(app)
    response = client.post("/api/lookup", json={"feature": "   ", "targets": TARGETS})
    assert response.status_code == 422


def test_invalid_target_is_rejected() -> None:
    client = TestClient(app)
    response = client.post("/api/lookup", json={"feature": "gap", "targets": ["netscape 4"]})
    assert response.status_code == 422


def test_kind_restricts_candidates() -> None:
    """显式指定 kind=css 时，不该命中 JS 的 ``api.Element.closest`` 之类。"""
    body = lookup("closest", kind="css")
    assert body["found"] is False
