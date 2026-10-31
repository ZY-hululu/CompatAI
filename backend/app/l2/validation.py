"""L2 后置校验：**假设模型一定会编**，然后逐条把它编的地方揪出来。

为什么校验不能只靠提示词
------------------------
提示词是"劝"，模型可以不听；校验是"拦"，不听就重试、再不听就降级。
这里的每条检查都是确定性代码，不依赖任何模型。

五类检查
--------
1. 结构：必须是合法 JSON，符合契约字段；
2. 白名单：``issue_id`` 不重不漏，不得出现 L1 没命中的 id；
3. 版本封闭：解释里出现的版本号必须能在该条 ``source_data`` / 目标列表里找到；
4. 口径：语法级问题必须谈"解析失败"，API 级问题必须谈"undefined/不存在"；
5. 修复代码语法门禁：CSS 能被 tinycss2 解析、JS 括号配平、HTML 标签闭合。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.core.schemas import FixEdit, Issue, IssueExplanation, SourceCode
from app.l1.parsers.css_parser import scan_css
from app.l1.parsers.js_parser import tokenize
from app.l2.edits import LANGUAGES, apply_edits, count_lines
from app.l2.prompts import classify

MAX_TEXT_CHARS = 400
MAX_FIX_CHARS = 8_000

# "Chrome 93" / "Safari 15.4" / "iOS 15" 这类"浏览器 + 版本"配对
VERSION_PAIR = re.compile(
    r"(Chrome|Safari|Firefox|Edge|Opera|iOS|Android)\s*(\d+(?:\.\d+){0,3})", re.IGNORECASE
)
# 裸版本号（必须带点，避免把 "2 个目标" 当成版本号）
BARE_VERSION = re.compile(r"(?<![\w.])(\d+\.\d+(?:\.\d+){0,2})(?![\w.])")
# 模型常见的偷懒：把带行号的上下文原样抄进 replacement（“ 12 | const x = 1;”）
LINE_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*\|")
VERSION_TOKEN = re.compile(r"\d+(?:\.\d+){0,3}")

# 各口径必须出现的语义锚点（命中任一即可，防止模型把语法错误写成运行时缺失）
KIND_KEYWORDS = {
    "syntax": ("解析", "语法", "SyntaxError", "脚本"),
    "api": ("undefined", "不存在", "没有", "TypeError", "方法", "成员"),
    "css": ("忽略", "不支持", "回退", "样式", "布局"),
    "html": ("忽略", "未知", "不支持", "标签", "属性"),
}

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    explanations: list[IssueExplanation] = field(default_factory=list)

    def feedback(self) -> str:
        return "上一轮输出存在以下问题，请修正后重新输出**完整** JSON：\n" + "\n".join(
            f"- {problem}" for problem in self.problems
        )


# --------------------------------------------------------------------------- #
# JSON 抽取
# --------------------------------------------------------------------------- #

def extract_json(raw: str) -> dict:
    """宽容地取出 JSON 对象（模型偶尔会套 Markdown 围栏或加前后缀）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中找不到 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


# --------------------------------------------------------------------------- #
# 版本封闭
# --------------------------------------------------------------------------- #

def allowed_versions(issue: Issue, targets: Sequence[str]) -> set[str]:
    """该条 issue 允许引用的版本号集合：BCD 依据 + 不支持列表 + 目标列表。"""
    allowed: set[str] = set(VERSION_TOKEN.findall(issue.source_data or ""))
    for label in issue.unsupported_browsers:
        allowed.update(VERSION_TOKEN.findall(label))
    for label in targets:
        allowed.update(VERSION_TOKEN.findall(label))
    return allowed


def _version_problems(text: str, issue: Issue, targets: Sequence[str]) -> list[str]:
    allowed = allowed_versions(issue, targets)
    found: set[str] = set()

    for match in VERSION_PAIR.finditer(text):
        found.add(match.group(2))
    found.update(BARE_VERSION.findall(text))

    invented = sorted(version for version in found if version not in allowed)
    if not invented:
        return []
    return [
        f"{issue.id} 的解释里出现了依据中不存在的版本号：{', '.join(invented)}"
        f"（该条仅允许引用 {', '.join(sorted(allowed)) or '无'}）"
    ]


# --------------------------------------------------------------------------- #
# 修复代码门禁
# --------------------------------------------------------------------------- #

def _balanced_js(code: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = set(pairs.values())
    stack: list[str] = []
    for token in tokenize(code):
        if token.kind != "punct":
            continue
        if token.value in pairs:
            stack.append(token.value)
        elif token.value in closers:
            if not stack or pairs[stack.pop()] != token.value:
                return False
    return not stack


def _balanced_html(code: str) -> bool:
    """粗检标签闭合：只处理 `<name ...>` / `</name>` / `<name/>`，不含属性内的 `<`。"""
    depth = 0
    index = 0
    length = len(code)
    while index < length:
        char = code[index]
        if char != "<":
            index += 1
            continue
        end = code.find(">", index)
        if end < 0:
            return False
        inner = code[index + 1 : end].strip()
        index = end + 1
        if not inner or inner.startswith(("!", "?")):
            continue
        if inner.startswith("/"):
            depth -= 1
            if depth < 0:
                return False
            continue
        name = re.split(r"[\s/>]", inner, maxsplit=1)[0].lower()
        if inner.endswith("/") or name in _VOID_TAGS:
            continue
        depth += 1
    return depth == 0


def _fix_code_problems(explanation: IssueExplanation) -> list[str]:
    problems: list[str] = []

    if explanation.fix_code_css:
        if len(explanation.fix_code_css) > MAX_FIX_CHARS:
            problems.append(f"{explanation.issue_id} 的 fix_code_css 过长（> {MAX_FIX_CHARS} 字符）")
        else:
            scan = scan_css(explanation.fix_code_css)
            if scan.warnings:
                problems.append(
                    f"{explanation.issue_id} 的 fix_code_css 有语法错误：{scan.warnings[0].message}"
                )

    if explanation.fix_code_js:
        if len(explanation.fix_code_js) > MAX_FIX_CHARS:
            problems.append(f"{explanation.issue_id} 的 fix_code_js 过长（> {MAX_FIX_CHARS} 字符）")
        elif not _balanced_js(explanation.fix_code_js):
            problems.append(f"{explanation.issue_id} 的 fix_code_js 括号/花括号不配平")

    if explanation.fix_code_html:
        if len(explanation.fix_code_html) > MAX_FIX_CHARS:
            problems.append(f"{explanation.issue_id} 的 fix_code_html 过长（> {MAX_FIX_CHARS} 字符）")
        elif not _balanced_html(explanation.fix_code_html):
            problems.append(f"{explanation.issue_id} 的 fix_code_html 标签未闭合")

    return problems


# --------------------------------------------------------------------------- #
# 主校验
# --------------------------------------------------------------------------- #

def validate(
    raw: str,
    issues: Sequence[Issue],
    targets: Sequence[str],
    code: SourceCode,
) -> ValidationResult:
    """校验一次模型输出。``raw`` 是模型返回的原始文本，``code`` 是**原始源码**（用于校验行号）。"""
    problems: list[str] = []

    try:
        data = extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return ValidationResult(ok=False, problems=[f"输出不是合法 JSON：{exc}"])

    items = data.get("explanations")
    if not isinstance(items, list):
        return ValidationResult(ok=False, problems=["缺少 explanations 数组"])

    allowed_ids = [issue.id for issue in issues]
    by_id = {issue.id: issue for issue in issues}

    explanations: list[IssueExplanation] = []
    seen: list[str] = []

    for position, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"explanation[{position}] 不是对象")
            continue
        try:
            explanation = IssueExplanation(**item)
        except ValidationError as exc:
            first = exc.errors()[0]
            problems.append(f"explanation[{position}] 字段非法：{first['loc']} {first['msg']}")
            continue

        issue_id = explanation.issue_id
        if issue_id not in by_id:
            problems.append(
                f"issue_id {issue_id!r} 不在白名单里（可用：{', '.join(allowed_ids)}）"
            )
            continue
        if issue_id in seen:
            problems.append(f"issue_id {issue_id!r} 重复出现")
            continue
        seen.append(issue_id)

        issue = by_id[issue_id]

        if len(explanation.explain_text) > MAX_TEXT_CHARS:
            problems.append(f"{issue_id} 的 explain_text 过长（> {MAX_TEXT_CHARS} 字符）")

        kind = classify(issue)
        keywords = KIND_KEYWORDS[kind]
        if not any(word in explanation.explain_text for word in keywords):
            problems.append(
                f"{issue_id} 属于{kind}类问题，但解释没有提到 "
                f"{'/'.join(keywords)} 中的任何一点（口径错误或过于含糊）"
            )

        problems.extend(_version_problems(explanation.explain_text, issue, targets))
        problems.extend(_version_problems(explanation.fix_diff, issue, targets))
        problems.extend(_fix_code_problems(explanation))

        explanations.append(explanation)

    missing = [issue_id for issue_id in allowed_ids if issue_id not in seen]
    if missing:
        problems.append(f"缺少这些 issue 的解释：{', '.join(missing)}")

    # 行区间替换：逐条校验范围/前缀/重叠，再对"合成后的整份代码"做语法门禁
    problems.extend(_apply_and_check_edits(explanations, code))

    if problems:
        return ValidationResult(ok=False, problems=problems)

    # 顺序对齐 L1（保证响应可复现，而不是跟着模型的输出顺序走）
    explanations.sort(key=lambda explanation: allowed_ids.index(explanation.issue_id))
    return ValidationResult(ok=True, explanations=explanations)


# --------------------------------------------------------------------------- #
# 行区间替换（fix_edits）的校验
# --------------------------------------------------------------------------- #

def _apply_and_check_edits(
    explanations: Sequence[IssueExplanation], code: SourceCode
) -> list[str]:
    """校验 ``fix_edits``：范围、行号前缀、区间重叠，最后对合成结果做语法门禁。

    关键在于**逐条校验 + 合成后校验**：单条替换可能只是 `gap: 12px;` 这种片段，
    单独解析没有意义；但把全部编辑应用回源码后，整份文件必须是合法语法。
    """
    problems: list[str] = []
    caps = {language: count_lines(getattr(code, language)) for language in LANGUAGES}
    ranges: dict[str, list[tuple[int, int, str]]] = {language: [] for language in LANGUAGES}
    collected: list[FixEdit] = []

    for explanation in explanations:
        for index, edit in enumerate(explanation.fix_edits):
            label = f"{explanation.issue_id} 的 fix_edits[{index}]"
            total = caps[edit.language]

            if total == 0:
                problems.append(f"{label}：{edit.language} 源码为空，无法定位第 {edit.start_line} 行")
                continue
            if edit.end_line > total:
                problems.append(
                    f"{label} 行号越界：{edit.language} 只有 {total} 行，"
                    f"收到 {edit.start_line}–{edit.end_line}"
                )
                continue
            if any(LINE_NUMBER_PREFIX.match(line) for line in edit.replacement.split("\n")):
                problems.append(
                    f"{label} 的 replacement 带了行号前缀（如「 12 | 」），请只给替换文本"
                )
                continue

            ranges[edit.language].append((edit.start_line, edit.end_line, explanation.issue_id))
            collected.append(edit)

    for language, items in ranges.items():
        items.sort()
        for previous, current in zip(items, items[1:]):
            if current[0] <= previous[1]:
                problems.append(
                    f"{previous[2]} 与 {current[2]} 的编辑区间在 {language} 上重叠："
                    f"{previous[0]}–{previous[1]} 与 {current[0]}–{current[1]}"
                )

    if problems or not collected:
        return problems

    composed = apply_edits(code, collected)
    if composed.css.strip() and scan_css(composed.css).warnings:
        problems.append(f"应用全部修复后 CSS 出现语法错误：{scan_css(composed.css).warnings[0].message}")
    if composed.js.strip() and not _balanced_js(composed.js):
        problems.append("应用全部修复后 JS 括号/花括号不配平")
    if composed.html.strip() and not _balanced_html(composed.html):
        problems.append("应用全部修复后 HTML 标签未闭合")

    return problems
