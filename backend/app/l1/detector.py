"""L1 编排层：解析 → 查表 → 定级 → 排序定 id。

这一层只做"组装"，不做判断：
* 判断在 ``bcd.evaluate``（纯版本比较）
* 提取在 ``parsers/*``（纯语法）
* 定级与依据文本在 ``severity.py``（纯模板）

因此同一份输入 + 同一个索引，输出必须逐字节相同（见 tests/test_detect_css.py 的确定性用例）。
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.schemas import (
    DetectRequest,
    DetectResponse,
    DetectStats,
    Issue,
    ParseWarning,
    ResolvedTarget,
)
from app.l1.bcd import BcdIndex, default_index
from app.l1.features import Feature
from app.l1.parsers.css_parser import scan_css
from app.l1.parsers.html_parser import scan_html
from app.l1.parsers.js_parser import scan_js
from app.l1.severity import build_source_data, confidence_for, grade
from app.l1.targets import Target


def _line_count(text: str) -> int:
    if not text.strip():
        return 0
    # 尾随换行不额外算一行：编辑器里 ".a{..}\\n" 与 ".a{..}" 对用户都是 1 行，
    # 而 str.count("\\n")+1 会把它算成 2 行（前端 textarea 提交天然带尾随换行，必须对齐）。
    return text.rstrip("\n").count("\n") + 1


def detect(payload: DetectRequest, index: BcdIndex | None = None) -> DetectResponse:
    """执行一次确定性检测。``index`` 可注入，便于测试用临时索引。"""
    settings = get_settings()
    index = index if index is not None else default_index()
    targets: list[Target] = payload.parsed_targets()

    features: list[Feature] = []
    warnings: list[ParseWarning] = []
    stats = DetectStats(
        css_lines=_line_count(payload.css),
        js_lines=_line_count(payload.js),
        html_lines=_line_count(payload.html),
    )
    stats.total_lines = stats.css_lines + stats.js_lines + stats.html_lines

    if payload.css.strip():
        scan = scan_css(payload.css)
        features.extend(scan.features)
        warnings.extend(scan.warnings)
        stats.declarations_scanned += scan.statements_scanned

    if payload.js.strip():
        scan = scan_js(payload.js, index.js_roots())
        features.extend(scan.features)
        warnings.extend(scan.warnings)
        stats.js_statements += scan.statements_scanned

    if payload.html.strip():
        scan = scan_html(payload.html, index.js_roots())
        features.extend(scan.features)
        warnings.extend(scan.warnings)
        stats.html_elements += scan.statements_scanned

    issues, unknown_features = _collect_issues(features, targets, index)

    # 稳定排序 → 稳定 id：L3 的前后比对靠它
    issues.sort(key=lambda issue: (issue.type, issue.line, issue.column, issue.bcd_path, issue.feature))
    truncated = len(issues) > settings.max_issues
    if truncated:
        # 截断必须显性告知：否则用户会以为"就这 500 条"，而真相是"被截掉了"
        warnings.append(
            ParseWarning(
                kind=issues[0].type,
                message=(
                    f"问题数超过上限（{settings.max_issues} 条），结果已截断；"
                    "请缩小代码范围或减少目标浏览器后重新检测"
                ),
            )
        )
        issues = issues[: settings.max_issues]
    issues = [
        issue.model_copy(update={"id": f"issue_{position}"})
        for position, issue in enumerate(issues, start=1)
    ]

    stats.features_scanned = len(features)
    stats.issues_total = len(issues)

    return DetectResponse(
        status="partial" if (warnings or truncated) else "success",
        issues=issues,
        unknown_features=unknown_features,
        targets_resolved=[
            ResolvedTarget(browser=t.browser, version=t.version, label=t.label) for t in targets
        ],
        parse_warnings=warnings,
        stats=stats,
    )


def _collect_issues(
    features: list[Feature], targets: list[Target], index: BcdIndex
) -> tuple[list[Issue], list[str]]:
    issues: list[Issue] = []
    unknown_features: list[str] = []
    seen_unknown: set[str] = set()

    for feature in features:
        resolved = index.resolve(feature.candidates)
        if resolved is None:
            # 查不到 ≠ 不支持。宁可放进 unknown_features 让人复核，也不猜一个等级出来。
            if feature.name not in seen_unknown:
                seen_unknown.add(feature.name)
                unknown_features.append(feature.name)
            continue

        path, entry = resolved
        result = index.query(entry, targets)
        if not result.is_problem:
            continue

        # 该属性存在上下文子特性、但我们没推断出上下文 → 结论可能不准，如实降置信度；
        # JS 的"按方法名推断"同理（arr.at() 不一定是 Array.prototype.at）
        context_unknown = feature.inferred or (
            feature.kind == "css"
            and feature.context == ""
            and len(feature.candidates) == 1
            and index.has_context_subfeatures(path)
        )

        # 只有"部分实现"时，unsupported_browsers 放部分支持的目标：
        # 语义是"目标里不能完整支持的浏览器"，避免出现空数组（空数组会被误读成"没问题"）
        affected = [v.target.label for v in result.unsupported] or [
            v.target.label for v in result.verdicts if v.partial
        ]

        issues.append(
            Issue(
                id="pending",
                type=feature.kind,
                feature=feature.name,
                bcd_path=path,
                line=feature.line,
                column=feature.column,
                unsupported_browsers=affected,
                severity=grade(result),
                source_data=build_source_data(result, entry, index.browser_name),
                confidence=confidence_for(result, context_unknown=context_unknown),  # type: ignore[arg-type]
            )
        )

    return issues, unknown_features
