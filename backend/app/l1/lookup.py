"""特性查询：把"某特性在目标浏览器支不支持"变成一次确定性查表。

与检测（detector）的区别
------------------------
检测是"从源码里发现用了什么"，查询是"用户直接问某个特性"。
两者共用同一份 BCD 索引与同一套版本比较，所以**结论口径完全一致**。
不经过任何 LLM。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from app.l1.bcd import BcdIndex, QueryResult
from app.l1.severity import build_source_data
from app.l1.targets import Target

AUTO_ORDER: tuple[str, ...] = ("css", "js", "html")


@dataclass(frozen=True, slots=True)
class ResolvedFeature:
    path: str
    kind: str            # css / js / html
    description: str


def _kind_of(path: str) -> str:
    if path.startswith("css."):
        return "css"
    if path.startswith(("javascript.", "api.")):
        return "js"
    return "html"


def _js_variants(query: str) -> tuple[str, ...]:
    """``array.at`` 也要能命中 ``javascript.builtins.Array.at``（首段首字母大写）。"""
    variants = [query]
    head, _, tail = query.partition(".")
    if head:
        capitalized = head[0].upper() + head[1:]
        variants.append(f"{capitalized}.{tail}" if tail else capitalized)
    return tuple(dict.fromkeys(variants))


def candidates_for(query: str, kind: str = "auto") -> tuple[str, ...]:
    """按 kind 生成候选 BCD 路径（顺序即优先级）。"""
    query = query.strip()
    if not query:
        return ()

    out: list[str] = []
    lowered = query.lower()
    html_form = lowered.replace(" ", ".").replace("_", "-")

    if kind in ("auto", "css"):
        out.append(f"css.properties.{lowered}")

    if kind in ("auto", "js"):
        for variant in _js_variants(query):
            out.append(f"javascript.builtins.{variant}")
            out.append(f"api.{variant}")

    if kind in ("auto", "html"):
        out.append(f"html.elements.{html_form}")
        # 纯属性名（inert / popover）→ 全局属性
        if "." not in html_form:
            out.append(f"html.global_attributes.{html_form}")
        else:
            out.append(f"html.global_attributes.{html_form.split('.')[-1]}")

    return tuple(dict.fromkeys(out))


def resolve(index: BcdIndex, query: str, kind: str = "auto") -> ResolvedFeature | None:
    resolved = index.resolve(candidates_for(query, kind))
    if resolved is None:
        return None
    path, entry = resolved
    return ResolvedFeature(path=path, kind=_kind_of(path), description=entry.desc)


def summary_for(index: BcdIndex, result: QueryResult, entry) -> str:
    """生成人可读依据；全目标支持时给正向结论（不能复用"不支持"的模板）。"""
    if result.unsupported or result.partial_only:
        return build_source_data(result, entry, index.browser_name)
    return f"MDN BCD: supported in all targets ({result.path})"


def evaluate_for(index: BcdIndex, resolved: ResolvedFeature, targets: list[Target]) -> QueryResult:
    entry = index.get(resolved.path)
    if entry is None:  # 理论上不会发生：resolve 已经确认存在
        raise LookupError(f"索引里找不到 {resolved.path}")
    return index.query(entry, targets)


def alternatives(index: BcdIndex, query: str, limit: int = 5) -> list[str]:
    """未命中时给相近路径：先做子串匹配，再用 difflib 纠拼写。

    例：``accent-colour``（英式拼写）→ ``css.properties.accent-color``。
    """
    needle = query.strip().lower()
    if len(needle) < 2:
        return []

    hits = [path for path in index.features if needle in path.lower()]
    hits.sort(key=len)          # 越短越接近
    if len(hits) >= limit:
        return hits[:limit]

    leaf = needle.rsplit(".", 1)[-1]
    for name in difflib.get_close_matches(leaf, index.leaf_names(), n=limit, cutoff=0.72):
        match = next(
            (path for path in index.features if path == name or path.endswith("." + name)),
            None,
        )
        if match:
            hits.append(match)

    return list(dict.fromkeys(hits))[:limit]
