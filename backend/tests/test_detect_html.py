"""HTML 解析（BS4 + html.parser）的测试。

要点：
1. 标签与属性都能落到 BCD 路径，属性查不到时回落 ``html.global_attributes``；
2. 内嵌 ``<style>`` / ``<script>`` / ``style=""`` 的内容会被交给 CSS/JS 解析器，
   且行号按内嵌位置平移（否则 issue 指错行，等于白报）；
3. 自定义元素与 data-/aria- 属性不制造噪音。
"""

from __future__ import annotations

from app.core.schemas import DetectRequest
from app.l1.detector import detect

DEFAULT_TARGETS = ["chrome 90", "safari 14.1", "firefox 88", "edge 90"]


def run(html: str, targets: list[str] | None = None):
    return detect(DetectRequest(html=html, targets=targets or DEFAULT_TARGETS))


# --------------------------------------------------------------------------- #
# 标签与属性
# --------------------------------------------------------------------------- #

def test_element_is_detected() -> None:
    body = run("<dialog open>hi</dialog>")
    paths = {issue.bcd_path for issue in body.issues}
    assert "html.elements.dialog" in paths


def test_element_scoped_attribute_is_detected() -> None:
    body = run('<img src="a.png" loading="lazy">')
    issue = next(i for i in body.issues if i.bcd_path == "html.elements.img.loading")
    assert issue.type == "html"
    assert issue.severity == "medium"          # 1/4 目标不支持
    assert "requires Safari 15.4" in issue.source_data
    assert issue.line == 1


def test_global_attribute_falls_back() -> None:
    """div 没有专属 inert 属性 → 必须回落到 html.global_attributes.inert。"""
    body = run("<div inert>内容</div>")
    paths = {issue.bcd_path for issue in body.issues}
    assert "html.global_attributes.inert" in paths


def test_custom_element_and_data_attributes_are_skipped() -> None:
    body = run('<my-widget data-x="1" aria-label="x" class="a"></my-widget>')
    assert body.unknown_features == []
    assert body.issues == []


# --------------------------------------------------------------------------- #
# 内嵌内容与行号平移
# --------------------------------------------------------------------------- #

def test_embedded_style_is_scanned_with_line_offset() -> None:
    html = "<style>\n.a { accent-color: red; }\n</style>\n<style>\n.b { accent-color: blue; }\n</style>"
    body = run(html, ["safari 12"])
    css_issues = [i for i in body.issues if i.type == "css"]
    assert [i.line for i in css_issues] == [2, 5]      # 两段 <style> 各自的行号都要对
    assert all(i.bcd_path == "css.properties.accent-color" for i in css_issues)


def test_embedded_script_is_scanned_with_line_offset() -> None:
    html = "<div>x</div>\n<script>\nconst a = b?.c;\n</script>"
    body = run(html, ["safari 12"])
    js_issues = [i for i in body.issues if i.type == "js"]
    assert len(js_issues) == 1
    assert js_issues[0].line == 3
    assert js_issues[0].bcd_path == "javascript.operators.optional_chaining"


def test_script_with_src_is_not_inlined() -> None:
    """带 src 的 <script> 内容不该被当成内联 JS 扫描（里面通常没有代码）。"""
    body = run('<script src="app.js">const a = b?.c;</script>', ["safari 12"])
    assert [i for i in body.issues if i.type == "js"] == []
    assert body.unknown_features == []


def test_inline_style_attribute_is_scanned_as_declarations() -> None:
    body = run('<div style="display: flex; accent-color: red;"></div>', ["safari 12"])
    css_issues = [i for i in body.issues if i.type == "css"]
    assert [i.bcd_path for i in css_issues] == ["css.properties.accent-color"]
    assert css_issues[0].line == 1


# --------------------------------------------------------------------------- #
# 与 CSS/JS 组合
# --------------------------------------------------------------------------- #

def test_multiple_issues_have_stable_ids_and_type_order() -> None:
    body = run('<dialog>\n<style>\n.a { accent-color: red; }\n</style>\n</dialog>')
    ids = [issue.id for issue in body.issues]
    assert ids == [f"issue_{n}" for n in range(1, len(ids) + 1)]
    types = [issue.type for issue in body.issues]
    assert types == sorted(types, key=lambda t: {"css": 0, "html": 1, "js": 2}[t])


def test_stats_count_elements() -> None:
    body = run("<dialog><img></dialog>")
    assert body.stats.html_elements == 2
    assert body.stats.html_lines == 1
