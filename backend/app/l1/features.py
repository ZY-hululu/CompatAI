"""解析器与 BCD 之间的中间数据结构 —— **不进线上契约**。

``Feature`` 只回答"代码里出现了什么、在哪"，不含任何兼容性结论；
结论一律由 ``bcd.py`` 查表得出。这个边界是"确定性检测"能成立的前提：
一旦解析器开始自己判断支不支持，架构就退化回旧方案了。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.schemas import IssueKind, ParseWarning


@dataclass(frozen=True, slots=True)
class Feature:
    """一处待查表的特性用法。"""

    kind: IssueKind
    name: str
    """人类可读名：CSS 属性名 / JS 成员路径 / HTML 标签名。"""

    candidates: tuple[str, ...]
    """BCD 候选路径，**按优先级从具体到宽泛**。

    CSS 的 ``gap`` 在 Flex 容器里应查 ``css.properties.gap.flex_context``，
    查不到再退回 ``css.properties.gap``；这个顺序就是"上下文优先"的落地方式。
    """

    line: int
    column: int
    context: str = ""
    """上下文说明（如 ``display: flex``），用于 issue 展示与 confidence 判定。"""

    inferred: bool = False
    """是否由启发式推断得出（如"看到 ``.at(`` 就当 Array.prototype.at"）。

    这类结论可能不准（同一方法名可能属于别的对象），detector 会把它们标成
    ``confidence: context_unknown``，让界面如实显示"这是推断，不是确证"。
    """

    @property
    def primary_path(self) -> str:
        return self.candidates[0]


@dataclass(slots=True)
class ScanResult:
    """单个解析器的产出。"""

    features: list[Feature] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    statements_scanned: int = 0
    """扫过的语句数（CSS 声明 / JS 语句 / HTML 标签），用于 stats。"""

    skipped: int = 0
    """有意跳过的数量（自定义属性、厂商前缀等），方便核实"不是漏扫而是不扫"。"""
