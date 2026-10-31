"""前后端接口契约 —— 唯一契约定义处（Data Contract）。

三条铁律
--------
1. 这里只描述**跨网络传输的字节**。解析器的内部结构（如 ``Feature``）不进本文件，
   放 ``app/l1/features.py``，避免契约被实现细节污染。
2. 请求模型 ``extra="forbid"``：前端字段拼错/多传立刻 422，绝不静默丢弃
   —— 静默丢弃是"用户以为设了 target，其实没生效"这类事故的根源。
3. 响应模型的所有集合字段都有默认值：L2 需要把 L1 的响应整体回传，
   即便前端裁剪过字段也应能通过校验。

与 docs/00-plan.md §4 的对应关系：契约原有字段语义不变，下列字段为**纯加法**
（全部可选或带默认值），逐条标注了「新增」。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import get_settings
from app.l1.targets import MAX_TARGETS, Target, TargetFormatError, parse_targets

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #

IssueKind = Literal["css", "js", "html"]
"""检测来源，与三个解析器一一对应。"""

Severity = Literal["high", "medium", "low"]
"""严重等级，由 l1/severity.py 按确定性矩阵生成，绝不由 LLM 决定。"""

Confidence = Literal["certain", "context_unknown", "version_unknown"]
"""*新增* —— 该条 issue 的置信度来源：

* ``certain``：BCD 明确给出该目标版本不支持；
* ``context_unknown``：CSS 无法推断布局上下文（如脱离规则的 ``gap``），按基础路径判定；
* ``version_unknown``：BCD 中该浏览器版本为 ``true``/``preview`` 等非数字值，只能保守判定。
"""

Status = Literal["success", "partial", "error"]
"""``partial`` = 有解析器失败或结果被截断，但仍有可用 issue。"""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# 共享片段
# --------------------------------------------------------------------------- #

def _check_size(value: str, field_name: str) -> str:
    limit = get_settings().max_source_chars
    if len(value) > limit:
        raise ValueError(f"{field_name} 长度 {len(value)} 超过上限 {limit} 字符")
    return value


def _check_targets(specs: list[str]) -> list[str]:
    """只做校验，不做转换；解析结果由调用方用 ``parse_targets`` 取。"""
    try:
        parse_targets(specs)
    except TargetFormatError as exc:
        raise ValueError(str(exc)) from None
    return specs


class SourceCode(StrictModel):
    """待检测 / 已修复的源代码载体。

    ``css`` 只收样式表内容（不要带 ``<style>`` 标签），``js`` 只收脚本内容；
    ``html`` 里的 ``<style>``/``<script>`` 由 HTML 解析器单独提取，两者互不重复。
    """

    html: str = Field(default="", description="整段 HTML（可空）")
    css: str = Field(default="", description="样式表内容（可空）")
    js: str = Field(default="", description="脚本源码（可空）")

    @field_validator("html", "css", "js")
    @classmethod
    def _validate_size(cls, value: str, info) -> str:
        return _check_size(value, str(info.field_name))

    @field_validator("html", "css", "js", mode="after")
    @classmethod
    def _normalize_newlines(cls, value: str) -> str:
        # 统一 CRLF，保证行列号在不同提交来源下一致（可复现的前提）
        return value.replace("\r\n", "\n").replace("\r", "\n")

    def is_empty(self) -> bool:
        return not (self.html.strip() or self.css.strip() or self.js.strip())

    @model_validator(mode="after")
    def _require_payload(self) -> "SourceCode":
        # 三个字段全空等于"让我检测空气"：直接 422，而不是返回一个空 issues 的 success
        # —— 后者会让前端误以为"代码没问题"。
        if self.is_empty():
            raise ValueError("html / css / js 至少提供一个非空字段")
        return self


class ResolvedTarget(StrictModel):
    """*新增* —— 后端最终采用的目标，回显给前端。

    存在的意义：让"用户填了 safari 15.0"与"后端按 safari 15.0 比较"这两件事
    在响应里可被核对，避免联调时互相甩锅。
    """

    browser: str = Field(description="BCD browser id，如 safari / safari_ios")
    version: str = Field(description="规范化的版本串，保留用户书写的段层级")
    label: str = Field(description="规范名，如 'safari 15.0'，即 unsupported_browsers 的取值格式")


class ParseWarning(StrictModel):
    """*新增* —— 某段代码解析失败而非整体 500。"""

    kind: IssueKind
    message: str
    line: int | None = None


class DetectStats(StrictModel):
    """*新增* —— 前端三张统计卡的数据源，后端算好，避免前端重复数行数。"""

    css_lines: int = 0
    js_lines: int = 0
    html_lines: int = 0
    total_lines: int = 0
    declarations_scanned: int = 0
    js_statements: int = 0
    html_elements: int = 0
    features_scanned: int = 0
    issues_total: int = 0


# --------------------------------------------------------------------------- #
# L1 契约
# --------------------------------------------------------------------------- #

class DetectRequest(SourceCode):
    """``POST /api/detect`` 请求体（契约 3.1）。

    与契约的差异：无。``targets`` 接受任意合法版本号，
    如 ``["chrome 90", "safari 12.1", "ios_saf 16.4"]``。
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "html": "<div class='box'><span>a</span></div>",
                "css": ".box { display: flex; gap: 10px; }",
                "js": "const x = obj?.name;",
                "targets": ["chrome 120", "safari 15.0", "firefox 115"],
            }
        },
    )

    targets: list[str] = Field(
        min_length=1,
        max_length=MAX_TARGETS,
        description="形如 'chrome 90' / 'safari 12.1' 的目标列表",
    )

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, value: list[str]) -> list[str]:
        return _check_targets(value)

    def parsed_targets(self) -> list[Target]:
        return parse_targets(self.targets)


class Issue(StrictModel):
    """L1 输出的单条问题（契约 3.1 的 issues 元素）。

    与契约的差异（新增字段，语义不变）：
    * ``bcd_path``：命中的 BCD 路径，如 ``css.properties.gap.flex_context``。
      L2 用它定位、L3 用它做前后比对，也是写论文时唯一不需要"讲故事"的证据链。
    * ``confidence``：见 ``Confidence``。
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "id": "issue_1",
                "type": "css",
                "feature": "gap",
                "bcd_path": "css.properties.gap.flex_context",
                "line": 1,
                "column": 15,
                "unsupported_browsers": ["safari 15.0"],
                "severity": "medium",
                "source_data": "MDN BCD: requires Safari 14.1 (css.properties.gap.flex_context)",
                "confidence": "certain",
            }
        },
    )

    id: str = Field(min_length=1, description="issue_1..n，由稳定排序生成，L1 内部唯一")
    type: IssueKind
    feature: str = Field(min_length=1, description="人类可读特性名，如 'gap' / 'Array.prototype.at'")
    bcd_path: str = Field(default="", description="命中的 BCD 路径，查不到时为已归一化的候选路径")
    line: int = Field(ge=1, description="1-based 行号")
    column: int = Field(ge=1, description="1-based 列号")
    unsupported_browsers: list[str] = Field(
        min_length=1,
        description="targets 中不能完整支持的项（完全不支持，或仅部分实现），顺序与请求一致",
    )
    severity: Severity
    source_data: str = Field(min_length=1, description="BCD 依据的人可读摘要，模板固定生成")
    confidence: Confidence = "certain"


class DetectResponse(StrictModel):
    """``POST /api/detect`` 响应体（契约 3.1）。

    与契约的差异（新增字段）：``targets_resolved`` / ``parse_warnings`` / ``stats``，
    ``status`` 在契约的 ``success`` 之外增加了 ``partial``（部分解析失败或结果被截断）。
    """

    status: Status = "success"
    issues: list[Issue] = Field(default_factory=list)
    unknown_features: list[str] = Field(default_factory=list)
    targets_resolved: list[ResolvedTarget] = Field(default_factory=list)
    parse_warnings: list[ParseWarning] = Field(default_factory=list)
    stats: DetectStats = Field(default_factory=DetectStats)


# --------------------------------------------------------------------------- #
# L2 契约
# --------------------------------------------------------------------------- #

class ExplainFixRequest(StrictModel):
    """``POST /api/explain-fix`` 请求体（契约 3.2）。

    同时携带 L1 的完整响应**和**原始代码：issues 是白名单（模型只许解释这些），
    原始代码是上下文（模型只许从里面读被引用的片段）。
    """

    detect: DetectResponse
    code: SourceCode


class FixEdit(StrictModel):
    """一条**行区间替换**（补丁级修复）。

    为什么不用"模型返回整份文件"：多个 issue 各返回一份完整文件时无法安全合并——
    文本拼接会产生重复规则、后写覆盖前者。行区间替换可以按行号倒序依次应用，
    N 条修复就能真正叠加成一次修复。

    行号语义：``start_line`` / ``end_line`` 均为 **1-based 且含两端**；
    ``replacement`` 为空字符串表示删除该区间。
    """

    language: Literal["css", "js", "html"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    replacement: str = ""

    @model_validator(mode="after")
    def _check_range(self) -> "FixEdit":
        if self.start_line > self.end_line:
            raise ValueError(f"start_line({self.start_line}) 不能大于 end_line({self.end_line})")
        return self


class IssueExplanation(StrictModel):
    """单条解释 + 修复（契约 3.2 的 explanations 元素）。

    与契约的差异：``fix_code_css`` 之外补了 ``fix_code_js`` / ``fix_code_html``
    （L1 覆盖三类特性，只用 CSS 字段承载 JS 修复会张冠李戴），
    以及 ``needs_human``（降级或校验失败时置 true，前端要显式提示"未自动修复"）。

    **``fix_edits``（新增）是首选修复形态**：行区间替换，可以与其他 issue 的修复合并。
    ``fix_code_*`` 保留为兼容旧输出的整份文件替换，两者同时存在时以 ``fix_edits`` 为准。
    """

    issue_id: str = Field(min_length=1, description="必须命中 L1 issues 中的某个 id")
    explain_text: str = Field(min_length=1)
    fix_diff: str = ""
    fix_edits: list[FixEdit] = Field(default_factory=list, description="行区间替换，可合并")
    fix_code_css: str | None = None
    fix_code_js: str | None = None
    fix_code_html: str | None = None
    needs_human: bool = False


class ExplainFixResponse(StrictModel):
    status: Status = "success"
    explanations: list[IssueExplanation] = Field(default_factory=list)
    model: str | None = Field(default=None, description="实际使用的模型名，审计用")
    degraded: bool = Field(default=False, description="True = 未过 LLM，走确定性降级路径")


# --------------------------------------------------------------------------- #
# L3 契约
# --------------------------------------------------------------------------- #

class DomDiff(StrictModel):
    """jsdom 前后对比结果。

    ``ran=False`` 表示未执行（如没有 JS 代码、或 Node 不可用），
    此时**不要**把它当成"通过"。
    """

    ran: bool = False
    changed: bool = False
    added_nodes: int = 0
    removed_nodes: int = 0
    errors: list[str] = Field(default_factory=list)


class VerifyRequest(StrictModel):
    """``POST /api/verify`` 请求体。"""

    original: SourceCode
    fixed: SourceCode
    targets: list[str] = Field(min_length=1, max_length=MAX_TARGETS)

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, value: list[str]) -> list[str]:
        return _check_targets(value)

    def parsed_targets(self) -> list[Target]:
        return parse_targets(self.targets)


class VerifyResponse(StrictModel):
    """``POST /api/verify`` 响应体。

    ``resolved`` / ``remaining`` 是 issue_id 集合；``new_issues`` 是修复引入的新问题
    （修复把 gap 换成了别人不支持的属性时，这里必须出现东西，否则就是漏检）。

    *新增* ``unknown_features`` / ``needs_review``：解决"resolved 不等于真修好"的问题。
    修复如果把特性改写成了无法识别的形态（如把 ``<dialog open>`` 换成 ``<div open>``，
    而 ``open`` 不是 ``div`` 的属性），L1 只是**查不到**它，会误判为已解决。
    ``unknown_features`` 回传修复后代码里查不到的特性；``needs_review`` 列出
    "虽已 resolved、但特性名与未识别项重合"的 issue id，需人工复核。
    """

    status: Status = "success"
    before_count: int = 0
    after_count: int = 0
    resolved: list[str] = Field(default_factory=list)
    remaining: list[str] = Field(default_factory=list)
    new_issues: list[Issue] = Field(default_factory=list)
    unknown_features: list[str] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    dom_diff: DomDiff = Field(default_factory=DomDiff)


# --------------------------------------------------------------------------- #
# 特性查询（L1 的"查表"能力，独立于检测）
# --------------------------------------------------------------------------- #

LookupKind = Literal["auto", "css", "js", "html"]


class LookupRequest(StrictModel):
    """``POST /api/lookup`` 请求体。

    把"某属性/某 API 在你的目标浏览器里支不支持"变成一次确定性查表。
    不走 LLM，答案直接来自本地 BCD 索引。
    """

    feature: str = Field(
        min_length=1,
        max_length=120,
        description="特性名：CSS 属性（gap）、JS API（Array.at / fetch）、HTML 标签或属性（dialog / img.loading）",
        examples=["accent-color"],
    )
    targets: list[str] = Field(min_length=1, max_length=MAX_TARGETS)
    kind: LookupKind = Field(default="auto", description="auto 时按 css → js → html 的顺序尝试")

    @field_validator("feature")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("feature 不能为空白")
        return stripped

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, value: list[str]) -> list[str]:
        return _check_targets(value)

    def parsed_targets(self) -> list[Target]:
        return parse_targets(self.targets)


class LookupVerdict(StrictModel):
    """单个目标浏览器的判定。"""

    browser: str
    version: str
    label: str
    supported: bool
    partial: bool = False
    confidence: Confidence = "certain"
    requirement: str | None = Field(default=None, description="BCD 记录的最低版本")


class LookupResponse(StrictModel):
    status: Status = "success"
    query: str
    found: bool = False
    kind: IssueKind | None = None
    bcd_path: str | None = None
    description: str | None = None
    verdicts: list[LookupVerdict] = Field(default_factory=list)
    unsupported_browsers: list[str] = Field(default_factory=list)
    summary: str | None = Field(default=None, description="人可读依据，模板生成")
    alternatives: list[str] = Field(
        default_factory=list, description="未命中时给出的相近 BCD 路径（最多 5 条）"
    )


# --------------------------------------------------------------------------- #
# 错误载体（我们自己抛的错，非 FastAPI 默认 422）
# --------------------------------------------------------------------------- #

class ErrorPayload(StrictModel):
    status: Literal["error"] = "error"
    error: str
    detail: list[dict] | None = None


__all__ = [
    "Confidence",
    "DetectRequest",
    "DetectResponse",
    "DetectStats",
    "DomDiff",
    "ErrorPayload",
    "ExplainFixRequest",
    "ExplainFixResponse",
    "Issue",
    "IssueExplanation",
    "IssueKind",
    "ParseWarning",
    "ResolvedTarget",
    "Severity",
    "SourceCode",
    "Status",
    "StrictModel",
    "VerifyRequest",
    "VerifyResponse",
]
