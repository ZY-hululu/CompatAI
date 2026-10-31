"""HTML 解析：BeautifulSoup4（``html.parser``）提取标签、属性，以及内嵌 CSS / JS。

为什么必须用 ``html.parser`` 而不是 lxml
-----------------------------------------
实测：``BeautifulSoup(html, "lxml")`` 拿不到 ``sourceline`` / ``sourcepos``（都是 None），
而 ``html.parser`` 两者都有。没有行号就无法把 issue 指回源码，UI 上等于白报。
"""

from __future__ import annotations

from dataclasses import replace

from bs4 import BeautifulSoup

from app.core.schemas import ParseWarning
from app.l1.features import Feature, ScanResult
from app.l1.parsers.css_parser import scan_css, scan_declarations
from app.l1.parsers.js_parser import scan_js

# 这些属性与兼容性无关（或属于未被索引的命名空间），扫了只会制造噪音
_SKIP_ATTRIBUTES = frozenset({"class", "id", "role", "xmlns", "lang", "dir"})


def scan_html(source: str, known_js_roots: frozenset[str] = frozenset()) -> ScanResult:
    """扫描一段 HTML。

    * 标签 → ``html.elements.<tag>``
    * 属性 → ``html.elements.<tag>.<attr>``，查不到回落 ``html.global_attributes.<attr>``
    * ``<style>`` / ``<script>`` 内容 → 交给 CSS / JS 解析器，行号按内嵌位置平移
    * ``style="..."`` → 按裸声明列表解析
    """
    result = ScanResult()
    if not source.strip():
        return result

    try:
        soup = BeautifulSoup(source, "html.parser")
    except Exception as exc:
        result.warnings.append(ParseWarning(kind="html", message=f"HTML 解析失败：{exc}"))
        return result

    lines = source.split("\n")
    cursor = [0]   # 内嵌内容在原文中的搜索游标，保证多段同内容不会错位

    def shift_of(content: str) -> int | None:
        index = source.find(content, cursor[0])
        if index < 0:
            return None
        cursor[0] = index + len(content)
        return source[:index].count("\n")

    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        line = getattr(tag, "sourceline", None) or 1
        tag_column = (getattr(tag, "sourcepos", 0) or 0) + 1
        result.statements_scanned += 1

        # 自定义元素（my-widget）没有 BCD 条目，直接跳过，避免 unknown_features 被刷屏
        if name and "-" not in name:
            result.features.append(
                Feature(
                    kind="html",
                    name=f"<{name}>",
                    candidates=(f"html.elements.{name}",),
                    line=line,
                    column=tag_column,
                )
            )

        for attribute, value in tag.attrs.items():
            attribute = attribute.lower()

            if attribute == "style" and isinstance(value, str):
                embedded = scan_declarations(value)
                column = _attribute_column(lines, line, tag_column, attribute)
                for feature in embedded.features:
                    result.features.append(
                        replace(feature, line=feature.line + line - 1, column=feature.column + column - 1)
                    )
                result.warnings.extend(embedded.warnings)
                continue

            if (
                attribute.startswith("data-")
                or attribute.startswith("aria-")
                or attribute in _SKIP_ATTRIBUTES
            ):
                continue

            result.features.append(
                Feature(
                    kind="html",
                    name=f"<{name} {attribute}>",
                    candidates=(
                        f"html.elements.{name}.{attribute}",
                        f"html.global_attributes.{attribute}",
                    ),
                    line=line,
                    column=_attribute_column(lines, line, tag_column, attribute),
                )
            )

        if name == "style":
            content = tag.string or ""
            if content.strip():
                offset = shift_of(content)
                for feature in scan_css(content).features:
                    result.features.append(replace(feature, line=feature.line + (offset or 0)))

        elif name == "script" and not tag.get("src"):
            content = tag.string or ""
            if content.strip():
                offset = shift_of(content)
                embedded = scan_js(content, known_js_roots)
                result.warnings.extend(embedded.warnings)
                for feature in embedded.features:
                    result.features.append(replace(feature, line=feature.line + (offset or 0)))

    return result


def _attribute_column(lines: list[str], line: int, tag_column: int, attribute: str) -> int:
    """属性在源码里的列号；跨行起始标签退化用标签列号。"""
    if 1 <= line <= len(lines):
        index = lines[line - 1].find(attribute, tag_column - 1)
        if index >= 0:
            return index + 1
    return tag_column
