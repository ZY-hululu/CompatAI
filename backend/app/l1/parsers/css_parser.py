"""CSS 解析（tinycss2）—— 只提取"用了什么、在哪、什么上下文"，不做任何兼容性判断。

为什么 CSS 检测不能只抓属性名
------------------------------
BCD 里 ``gap`` 是**上下文相关**的：

    css.properties.gap                → safari 10.1   （朴素查表会得出"Safari 15 支持"，错）
    css.properties.gap.flex_context   → safari 14.1   （这才是 Flex 布局里的真实门槛）
    css.properties.gap.grid_context   → safari 12

所以这里必须连"所在规则块的 ``display`` 值"一起提取，交给 bcd.py 按上下文优先级查表。
"""

from __future__ import annotations

import tinycss2

from app.core.schemas import ParseWarning
from app.l1.features import Feature, ScanResult

# 一条声明所属布局上下文的 BCD 子特性后缀
_CONTEXT_SUFFIX: dict[str, str] = {
    "flex": "flex_context",
    "grid": "grid_context",
    "multicol": "multicol_context",
}

_CONTEXT_LABEL: dict[str, str] = {
    "flex": "display: flex",
    "grid": "display: grid",
    "multicol": "multi-column layout",
}

# 这些 at-rule 的内容是**声明列表**，不是规则列表。剩下的（@media/@supports/@keyframes…）按规则递归。
_DECLARATION_BODY_AT_RULES = frozenset(
    {"font-face", "page", "property", "counter-style", "viewport", "font-feature-values", "font-palette-values"}
)


def scan_stylesheet(css: str) -> ScanResult:
    """扫描一段完整样式表（含规则与 at-rule）。"""
    return scan_css(css)


def scan_declarations(source: str) -> ScanResult:
    """扫描一段**裸声明列表**（``style="display:flex; gap:10px"`` 就是这种形态）。

    不能复用 ``scan_css``：裸声明没有选择器包裹，tinycss2 会把它当成语法错误。
    """
    result = ScanResult()
    if not source.strip():
        return result

    try:
        declarations = tinycss2.parse_declaration_list(
            source, skip_comments=True, skip_whitespace=True
        )
    except Exception as exc:
        result.warnings.append(ParseWarning(kind="css", message=f"内联样式解析失败：{exc}"))
        return result

    context = _infer_context(declarations)
    for declaration in declarations:
        if declaration.type == "declaration":
            _emit(declaration, context, result)
        elif declaration.type == "error":
            result.warnings.append(
                ParseWarning(
                    kind="css",
                    message=f"声明解析失败：{getattr(declaration, 'message', 'unknown')}",
                    line=declaration.source_line,
                )
            )
    return result


def scan_css(css: str) -> ScanResult:
    """扫描一段 CSS，返回待查表的特性列表。"""
    result = ScanResult()
    if not css.strip():
        return result

    try:
        rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    except Exception as exc:  # tinycss2 极少抛异常，但解析器崩溃不能带崩整个请求
        result.warnings.append(ParseWarning(kind="css", message=f"样式表解析失败：{exc}"))
        return result

    _walk_rules(rules, result)
    return result


# --------------------------------------------------------------------------- #
# 遍历
# --------------------------------------------------------------------------- #

def _walk_rules(rules, result: ScanResult) -> None:
    for rule in rules:
        if rule.type == "qualified-rule":
            _scan_declarations(rule.content, result)
        elif rule.type == "at-rule":
            _scan_at_rule(rule, result)
        elif rule.type == "error":
            result.warnings.append(
                ParseWarning(kind="css", message=f"CSS 语法错误：{getattr(rule, 'message', 'unknown')}", line=rule.source_line)
            )


def _scan_at_rule(rule, result: ScanResult) -> None:
    content = getattr(rule, "content", None)
    if not content:
        return  # @import / @charset 之类没有内容体

    keyword = rule.lower_at_keyword
    if keyword in _DECLARATION_BODY_AT_RULES:
        _scan_declarations(content, result)
        return

    # @media / @supports / @layer / @keyframes … 内部仍是规则列表
    nested = tinycss2.parse_rule_list(content, skip_comments=True, skip_whitespace=True)
    if any(node.type != "error" for node in nested):
        _walk_rules(nested, result)
    else:
        # 形如 @media (...) { color: red } 这种非法但存在的内容，按声明兜底
        _scan_declarations(content, result)


def _scan_declarations(content, result: ScanResult) -> None:
    declarations = tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True)
    context = _infer_context(declarations)

    for declaration in declarations:
        if declaration.type == "declaration":
            _emit(declaration, context, result)
        elif declaration.type == "error":
            result.warnings.append(
                ParseWarning(
                    kind="css",
                    message=f"声明解析失败：{getattr(declaration, 'message', 'unknown')}",
                    line=declaration.source_line,
                )
            )


def _emit(declaration, context: str | None, result: ScanResult) -> None:
    result.statements_scanned += 1
    name = declaration.lower_name

    if name.startswith("--"):
        result.skipped += 1  # 自定义属性，BCD 里没有对应条目
        return
    if name.startswith("-"):
        # 厂商前缀属性（-webkit-box-orient 等）是作者有意写的兼容写法，
        # 用"去掉前缀再查表"会得出错误结论，因此直接跳过。
        result.skipped += 1
        return

    result.features.append(
        Feature(
            kind="css",
            name=name,
            candidates=_candidates(name, context),
            line=declaration.source_line,
            column=declaration.source_column,
            context=_CONTEXT_LABEL.get(context or "", ""),
        )
    )


# --------------------------------------------------------------------------- #
# 上下文与候选路径
# --------------------------------------------------------------------------- #

def _infer_context(declarations) -> str | None:
    """从同一规则块的声明里推断布局上下文。

    刻意只在本规则块内推断（不做跨选择器/继承分析）：跨规则推断会引入不确定性，
    而"确定性"是这个系统的第一原则。推不出来时 context 为 None，
    由 detector 标记 ``context_unknown`` 而不是硬猜。
    """
    display = ""
    has_multicol = False

    for declaration in declarations:
        if declaration.type != "declaration":
            continue
        if declaration.lower_name == "display":
            display = tinycss2.serialize(declaration.value).strip().lower()
        elif declaration.lower_name in {"column-count", "column-width", "columns"}:
            has_multicol = True

    if "flex" in display:
        return "flex"
    if "grid" in display:
        return "grid"
    if has_multicol:
        return "multicol"
    return None


def _candidates(prop: str, context: str | None) -> tuple[str, ...]:
    """上下文优先的候选路径：先查具体子特性，不存在再退回基础属性。"""
    base = f"css.properties.{prop}"
    suffix = _CONTEXT_SUFFIX.get(context or "")
    if suffix:
        return (f"{base}.{suffix}", base)
    return (base,)
