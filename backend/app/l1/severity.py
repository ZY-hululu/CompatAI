"""严重等级与依据文本 —— 纯函数、模板固定，不含任何 LLM 话术。

为什么要独立成模块：severity 和 source_data 是 L2 的输入白名单。
如果这两样由模型生成，"确定性检测"就名存实亡了。
"""

from __future__ import annotations

from app.core.schemas import Severity
from app.l1.bcd import FeatureEntry, QueryResult
from app.l1.bcd import MAJORITY_RATIO


def grade(result: QueryResult) -> Severity:
    """按"不支持的目标占比"定级，阈值固定且可解释。

    * ``>= 50%`` 的目标不支持 → ``high``（或全部不支持）
    * ``< 50%``  → ``medium``
    * 仅"部分实现"而无人完全不支持 → ``low``

    契约示例：3 个目标里 1 个不支持 → 1/3 < 0.5 → ``medium`` ✅
    """
    unsupported = result.unsupported
    if not unsupported:
        return "low"
    ratio = len(unsupported) / len(result.verdicts)
    return "high" if ratio >= MAJORITY_RATIO else "medium"


def _requirement_text(verdict, browser_name) -> str:
    """单个不支持目标的"需要什么版本"描述。"""
    display = browser_name(verdict.target.browser)
    if verdict.requirement is None:
        return f"not supported in {display}"
    if verdict.requirement == "preview":
        return f"{display} requires preview/nightly build"
    if verdict.requirement.startswith("≤"):
        return f"{display} supported since ≤{verdict.requirement[1:].strip()}"
    return f"requires {display} {verdict.requirement}"


def build_source_data(
    result: QueryResult,
    entry: FeatureEntry,
    browser_name,
) -> str:
    """生成人可读的依据串，例如：

    ``MDN BCD: requires Safari 14.1 (css.properties.gap.flex_context)``

    ``browser_name`` 是可调用对象（``index.browser_name``），避免这里再依赖索引对象。
    正文用 BCD 的展示名（Safari），与契约示例一致；``unsupported_browsers`` 里则用规范 id
    （``safari 14.1``），因为那是给机器和后续比对的。

    不使用 BCD 的 description（如 "Supported in Flex Layout"），因为路径已经表达了上下文，
    拼进正文反而让这句"证据"变得像解释，而它必须是纯事实。
    """
    if result.unsupported:
        parts = [_requirement_text(verdict, browser_name) for verdict in result.unsupported]
        head = "; ".join(parts)
    else:
        # 只有部分实现，没有完全不支持的浏览器
        partial = [v for v in result.verdicts if v.partial]
        head = "; ".join(
            f"{browser_name(v.target.browser)} {v.target.version} partial implementation"
            for v in partial
        )

    text = f"MDN BCD: {head} ({entry.path})"
    if entry.overridden:
        text += " [local override]"
    return text


def confidence_for(result: QueryResult, *, context_unknown: bool) -> str:
    """三个置信度来源的优先级：BCD 版本未知 > 上下文未知 > 确定。"""
    if any(v.confidence == "version_unknown" for v in result.verdicts):
        return "version_unknown"
    if context_unknown:
        return "context_unknown"
    return "certain"
