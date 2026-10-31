"""把 L2 的「行区间替换」应用到源码上。

这是"多条修复能否合并"的关键：每个 issue 只改动它自己涉及的那几行，
因此不同 issue 的修改可以按行号倒序依次应用，互不干扰。

如果模型仍返回旧形态（整份文件 ``fix_code_*``），由调用方决定优先级；
本模块只负责 ``FixEdit``。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.core.schemas import FixEdit, SourceCode

LANGUAGES: tuple[str, ...] = ("css", "js", "html")


def apply_edits(code: SourceCode, edits: Iterable[FixEdit]) -> SourceCode:
    """返回应用了全部编辑的新 ``SourceCode``（不修改入参）。"""
    buckets: dict[str, list[FixEdit]] = {language: [] for language in LANGUAGES}
    for edit in edits:
        buckets[edit.language].append(edit)

    merged: dict[str, str] = {}
    for language in LANGUAGES:
        source = getattr(code, language)
        bucket = buckets[language]
        merged[language] = _apply_one(source, bucket) if bucket else source

    return SourceCode(**merged)


def _apply_one(source: str, edits: Sequence[FixEdit]) -> str:
    lines = source.split("\n")
    # 从大行号往小行号应用：否则前面的删改会让后面的行号整体位移
    for edit in sorted(edits, key=lambda item: item.start_line, reverse=True):
        replacement = [] if edit.replacement == "" else edit.replacement.split("\n")
        lines[edit.start_line - 1 : edit.end_line] = replacement
    return "\n".join(lines)


def count_lines(source: str) -> int:
    """与 L1 的 ``_line_count`` 保持同一口径：尾随换行不额外算一行。"""
    if not source.strip():
        return 0
    return source.rstrip("\n").count("\n") + 1
