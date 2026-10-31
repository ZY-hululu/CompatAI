"""三解析器合流：一次请求同时拿到 CSS / JS / HTML 三个维度的报错。

这是用户明确提的验收条件："我希望这步做完后，我能在一个请求里同时看到
CSS、JS、HTML 三个维度的报错。"

这里用的就是前端默认预填的那份示例，所以测试同时钉住了"页面演示数据"与
"接口真实输出"的一致性。
"""

from __future__ import annotations

import json
from collections import Counter

from app.core.schemas import DetectRequest
from app.l1.detector import detect

DEFAULT_TARGETS = ["chrome 90", "safari 14.1", "firefox 88", "edge 90"]

CSS = """.card {
  display: flex;
  gap: 12px;
  aspect-ratio: 16 / 9;
  accent-color: #8E95F9;
  container-type: inline-size;
  backdrop-filter: blur(6px);
  color-interpolation: sRGB;
  scroll-behavior: smooth;
}
"""

JS = """const name = user?.profile?.name;
const total = count ?? 0;
const last = items.at(-1);
"""

HTML = """<dialog class="modal" open>
  <img src="cover.png" loading="lazy" decoding="async">
  <div inert>内容</div>
</dialog>
"""


def test_one_request_yields_all_three_dimensions() -> None:
    body = detect(DetectRequest(css=CSS, js=JS, html=HTML, targets=DEFAULT_TARGETS))

    assert body.status == "success"
    assert body.parse_warnings == []

    per_type = Counter(issue.type for issue in body.issues)
    assert per_type == {"css": 6, "html": 4, "js": 1}, per_type
    assert len(body.issues) == 11

    # id 连续且按 (type, line, column, bcd_path) 稳定排序
    assert [issue.id for issue in body.issues] == [f"issue_{n}" for n in range(1, 12)]
    types = [issue.type for issue in body.issues]
    assert types == ["css"] * 6 + ["html"] * 4 + ["js"]

    # 每个维度都要有可读的证据链
    assert all(issue.bcd_path for issue in body.issues)
    assert all(issue.source_data.startswith("MDN BCD:") for issue in body.issues)


def test_severity_distribution_matches_the_ui_cards() -> None:
    body = detect(DetectRequest(css=CSS, js=JS, html=HTML, targets=DEFAULT_TARGETS))
    severity = Counter(issue.severity for issue in body.issues)
    assert severity == {"high": 8, "medium": 2, "low": 1}, severity


def test_browser_distribution_matches_the_bar_chart() -> None:
    body = detect(DetectRequest(css=CSS, js=JS, html=HTML, targets=DEFAULT_TARGETS))
    per_browser = Counter(
        label.split(" ")[0]
        for issue in body.issues
        for label in issue.unsupported_browsers
    )
    assert per_browser == {"safari": 11, "firefox": 8, "chrome": 5, "edge": 5}, per_browser


def test_stats_cover_three_sources() -> None:
    body = detect(DetectRequest(css=CSS, js=JS, html=HTML, targets=DEFAULT_TARGETS))
    stats = body.stats
    assert stats.css_lines == 10
    assert stats.js_lines == 3
    assert stats.html_lines == 4
    assert stats.total_lines == 17
    assert stats.declarations_scanned == 8
    assert stats.js_statements > 0
    assert stats.html_elements == 3


def test_js_issue_points_at_the_real_usage() -> None:
    body = detect(DetectRequest(css=CSS, js=JS, html=HTML, targets=DEFAULT_TARGETS))
    js_issue = next(issue for issue in body.issues if issue.type == "js")
    assert js_issue.feature == "at()"
    assert js_issue.bcd_path == "javascript.builtins.Array.at"
    assert js_issue.line == 3
    assert js_issue.confidence == "context_unknown"   # 方法名推断，如实标注


def test_html_issues_include_element_and_attribute() -> None:
    body = detect(DetectRequest(css=CSS, js=JS, html=HTML, targets=DEFAULT_TARGETS))
    paths = {issue.bcd_path for issue in body.issues if issue.type == "html"}
    assert paths == {
        "html.elements.dialog",
        "html.elements.dialog.open",
        "html.elements.img.loading",
        "html.global_attributes.inert",
    }


def test_full_request_is_deterministic() -> None:
    payload = DetectRequest(css=CSS, js=JS, html=HTML, targets=DEFAULT_TARGETS)
    first = json.dumps(detect(payload).model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    second = json.dumps(detect(payload).model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    assert first == second
