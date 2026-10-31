"""L2 确定性降级：模型不可用或输出不合法时，仍然给用户一条**有依据**的解释。

原则：宁可给"没有自动修复"，也不给编造的修复。
因此降级结果的 ``fix_*`` 一律为 ``None``、``needs_human=True``，
前端必须显式提示"这条没有自动修复"，而不是让人误以为修好了。

文本全部由 L1 的 ``source_data`` 与 ``unsupported_browsers`` 拼出，零推测。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.schemas import Issue, IssueExplanation
from app.l2.prompts import classify

_TEMPLATES = {
    "syntax": (
        "{browsers} 不支持该语法（{feature}）：解析阶段直接抛 SyntaxError，"
        "整个脚本都不会执行。依据：{source_data}"
    ),
    "api": (
        "{browsers} 没有 {feature} 这个成员：运行时读取为 undefined，"
        "调用会抛 TypeError。依据：{source_data}"
    ),
    "css": (
        "{browsers} 不支持该 CSS 声明（{feature}）：样式会被忽略，"
        "元素回退到默认表现。依据：{source_data}"
    ),
    "html": (
        "{browsers} 无法识别 {feature}：会被当作未知元素或属性忽略，"
        "功能或样式不生效。依据：{source_data}"
    ),
}


def explain(issue: Issue) -> IssueExplanation:
    kind = classify(issue)
    template = _TEMPLATES[kind]
    text = template.format(
        browsers="、".join(issue.unsupported_browsers) or "目标浏览器",
        feature=issue.feature,
        source_data=issue.source_data,
    )
    return IssueExplanation(
        issue_id=issue.id,
        explain_text=text,
        fix_diff="未自动修复，需人工确认替代方案",
        fix_code_css=None,
        fix_code_js=None,
        fix_code_html=None,
        needs_human=True,
    )


def explain_all(issues: Sequence[Issue]) -> list[IssueExplanation]:
    return [explain(issue) for issue in issues]
