"""L2 提示词层：把 L1 的确定性结论翻译成自然语言。

设计原则
--------
**提示词只负责"劝"，机器校验才负责"拦"**。这里的每条硬约束，在 ``validation.py``
里都有对应的确定性检查；模型不听话的结局必须是"被拦下 → 重试 → 降级"，
而不是"看着像真的就放行"。

三条硬约束
----------
1. ``issue_id`` 白名单：只能解释 L1 命中的那些 id；
2. 版本号封闭：只允许引用 ``source_data`` / ``targets`` 里出现过的版本；
3. 不得声称"已验证 / 测试通过"——验证是 L3 的职责，L2 只做解释与给出候选修复。

解释口径（用户明确要求区分）
--------------------------
* **语法级**（``javascript.operators.*`` / ``statements`` / ``classes`` / ``grammar``）：
  旧浏览器**解析阶段**就失败，整个脚本一行都不执行。
* **API 级**（``javascript.builtins.*`` / ``api.*``）：旧浏览器没有这个成员，
  运行时得到 ``undefined`` 或抛 ``TypeError``。
* **CSS**：声明被忽略，样式回退，不报错。
* **HTML**：未知标签/属性被忽略，功能或样式不生效。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.schemas import Issue, SourceCode

# --------------------------------------------------------------------------- #
# 特性分类与口径
# --------------------------------------------------------------------------- #

SYNTAX_PREFIXES = (
    "javascript.operators.",
    "javascript.statements.",
    "javascript.classes.",
    "javascript.grammar.",
)
API_PREFIXES = ("javascript.builtins.", "api.")

KIND_LABELS = {
    "syntax": "语法级（解析阶段失败）",
    "api": "API 级（运行时缺失）",
    "css": "CSS 属性（样式降级）",
    "html": "HTML 标签/属性（被忽略）",
}

KIND_GUIDANCE = {
    "syntax": (
        "这是**语法级**问题：旧浏览器的解析器直接抛 SyntaxError，"
        "整个 <script> 一行都不会执行。必须讲清“解析失败 → 整段脚本不执行”，"
        "禁止说成“某个函数返回 undefined”。"
    ),
    "api": (
        "这是**API 级**问题：旧浏览器没有这个成员，运行时读取得到 undefined、"
        "调用则抛 TypeError（is not a function）。必须讲清“运行时报错/undefined”，"
        "禁止说成解析失败或语法错误。"
    ),
    "css": (
        "这是**CSS 属性**问题：旧浏览器会忽略该声明，元素回退到默认表现"
        "（布局或视觉降级，不抛错）。"
    ),
    "html": (
        "这是**HTML 标签/属性**问题：旧浏览器把它当作未知元素或未知属性忽略，"
        "不抛错，但功能或样式不生效。"
    ),
}


def classify(issue: Issue) -> str:
    """判定解释口径。只依据 ``bcd_path`` 前缀，不做语义猜测。"""
    if issue.type == "css":
        return "css"
    if issue.type == "html":
        return "html"
    path = issue.bcd_path or ""
    if path.startswith(SYNTAX_PREFIXES):
        return "syntax"
    if path.startswith(API_PREFIXES):
        return "api"
    # 兜底：未知命名空间按 API 处理（"缺失"比"语法错误"更保守）
    return "api"


# --------------------------------------------------------------------------- #
# System Prompt
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """你是一个浏览器兼容性修复助手，服务对象是我国大学生创新项目"AI 浏览器兼容性助手"。

你的输入只有两样东西：
1. 一组**已由确定性检测器判定**的兼容性问题（issues），每条都带 MDN BCD 依据；
2. 每条问题附近的源代码上下文（带行号）。

你的全部职责是：**解释这些问题，并给出最小、等价、向下兼容的修复方案**。

## 绝对禁止（违反任何一条，你的整份输出都会被丢弃）

1. **不得新增问题**：只能解释输入里出现的 issue_id，一个不多、一个不少。
2. **不得引入新版本号**：你提到的每一个"某浏览器某版本"，都必须能在该条问题的
   `BCD 依据` 或"目标浏览器"列表里**原文找到**。禁止推测、禁止四舍五入
   （依据写 14.1，你就不能写 14 或 15）。
3. **不得声称验证过**：不要写"已验证""测试通过""确认修复生效"。验证由下游环节完成。
4. **不得解释未被命中的问题**：即使你在代码里看到别的不兼容写法，也一律不提。
5. 不确定时就少说：解释可以短，但不能编。

## 解释口径（必须严格区分，这是硬性要求）

- **语法级**问题（BCD 路径以 `javascript.operators.` / `javascript.statements.` /
  `javascript.classes.` / `javascript.grammar.` 开头）：旧浏览器**解析阶段**直接
  抛 SyntaxError，**整个脚本都不会执行**。必须讲清这一点。
- **API 级**问题（BCD 路径以 `javascript.builtins.` / `api.` 开头）：旧浏览器
  **没有这个成员**，运行时读取得到 `undefined`，调用抛 `TypeError`。
- **CSS 属性**问题：旧浏览器忽略该声明，样式回退，不抛错。
- **HTML 标签/属性**问题：旧浏览器当作未知元素/属性忽略，功能或样式不生效。

## 修复要求

- 只修这一条问题，不要顺手重构别的代码；
- 修复后的代码必须**只使用兼容性不低于原写法**的替代方案（例如用负 margin 方案替代
  flex 的 gap，用存在性判断替代直接调用新 API）；
- 宁可给"无法自动修复"，也不要给一个会引入新问题的修复。

## 修复格式：只给"行区间替换"，禁止整份文件

你要输出的不是完整文件，而是**对哪几行做替换**。字段 `fix_edits` 是一个数组：

```json
{"language": "css", "start_line": 4, "end_line": 4, "replacement": "  margin: 0;"}
```

规则（违反会被机器校验拦下并要求重做）：

1. `language` 只能是 `css` / `js` / `html`；
2. `start_line` 与 `end_line` 必须是你在"代码上下文"里**看到过的真实行号**（1-based、含两端），
   超出该语言源码总行数一律无效；
3. `replacement` 是这几行的**替换文本**，可以多行；**不要带行号前缀**（不要出现 ` 12 | ` 这种标记）；
4. 删除整行时 `replacement` 传空字符串 `""`；
5. 尽量只改 1~3 行；不同 issue 的区间**不得重叠**；
6. 无法可靠修复时，`fix_edits` 传空数组 `[]`，并让 `fix_diff` 说明原因。

## 输出格式

只输出一个 JSON 对象，不要任何解释性前后文、不要 Markdown 代码围栏：

{"explanations": [{"issue_id": "issue_1", "explain_text": "...", "fix_diff": "...",
  "fix_edits": []}]}

所有字段都必须出现；`explain_text` 用中文，控制在 120 字以内；
`fix_diff` 用一句话描述改法，不需要修复时填空字符串。
"""


# --------------------------------------------------------------------------- #
# 上下文与消息构造
# --------------------------------------------------------------------------- #

MAX_PROMPT_CHARS = 16_000


def extract_context(source: str, line: int, window: int) -> str:
    """取命中行上下 ``window`` 行，保留原始行号（模型与人都需要它）。"""
    if not source.strip():
        return ""
    lines = source.split("\n")
    start = max(1, line - window)
    end = min(len(lines), line + window)
    return "\n".join(f"{number:>4} | {lines[number - 1]}" for number in range(start, end + 1))


def _source_for(issue: Issue, code: SourceCode) -> str:
    if issue.type == "css":
        return code.css or code.html
    if issue.type == "js":
        return code.js or code.html
    return code.html or code.css


def _issue_block(issue: Issue, code: SourceCode, window: int) -> str:
    kind = classify(issue)
    context = extract_context(_source_for(issue, code), issue.line, window)
    return (
        f"### {issue.id}\n"
        f"- 类型：{issue.type} / {KIND_LABELS[kind]}\n"
        f"- 特性：{issue.feature}（BCD: {issue.bcd_path}）\n"
        f"- 位置：第 {issue.line} 行第 {issue.column} 列，严重度 {issue.severity}\n"
        f"- 不支持的目标：{', '.join(issue.unsupported_browsers)}\n"
        f"- BCD 依据：{issue.source_data}\n"
        f"- 解释口径要求：{KIND_GUIDANCE[kind]}\n"
        f"- 代码上下文：\n```\n{context or '（无可用上下文）'}\n```"
    )


def build_messages(
    issues: Sequence[Issue],
    code: SourceCode,
    targets: Sequence[str],
    context_lines: int = 20,
) -> list[dict[str, str]]:
    """构造发送给模型的消息。只给命中行附近，不给整份文件（省 token 也防越权）。"""
    if not issues:
        raise ValueError("没有 issue 时不应调用模型")

    window = context_lines
    user = _compose_user(issues, code, targets, window)
    while len(user) > MAX_PROMPT_CHARS and window > 2:
        window //= 2
        user = _compose_user(issues, code, targets, window)

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _compose_user(
    issues: Sequence[Issue], code: SourceCode, targets: Sequence[str], window: int
) -> str:
    target_line = "、".join(targets) if targets else "（未提供）"
    blocks = "\n\n".join(_issue_block(issue, code, window) for issue in issues)
    return (
        f"## 目标浏览器\n{target_line}\n\n"
        f"## 待解释的问题（共 {len(issues)} 条，这是白名单）\n\n{blocks}\n\n"
        f"## 输出\n严格按 system 里的 JSON 结构输出，issue_id 必须是 "
        f"{', '.join(issue.id for issue in issues)} 之一，且不重不漏。"
    )


# --------------------------------------------------------------------------- #
# 结构化输出 Schema
# --------------------------------------------------------------------------- #

def build_schema(issue_ids: Sequence[str]) -> dict:
    """严格 JSON Schema。``issue_id`` 用 enum 直接封死白名单（防幻觉第一道）。

    ``fix_edits`` 是唯一被 schema 允许的修复形态：行区间替换。
    整份文件的 ``fix_code_*`` 仍能通过解析（兼容旧输出），但不出现在 schema 里，
    以免模型又退回"重写整块"那种无法合并的形态。
    """
    edit = {
        "type": "object",
        "properties": {
            "language": {"type": "string", "enum": ["css", "js", "html"]},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "replacement": {"type": "string"},
        },
        "required": ["language", "start_line", "end_line", "replacement"],
        "additionalProperties": False,
    }
    item = {
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "enum": list(issue_ids)},
            "explain_text": {"type": "string"},
            "fix_diff": {"type": "string"},
            "fix_edits": {"type": "array", "items": edit},
        },
        "required": ["issue_id", "explain_text", "fix_diff", "fix_edits"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"explanations": {"type": "array", "items": item}},
        "required": ["explanations"],
        "additionalProperties": False,
    }
