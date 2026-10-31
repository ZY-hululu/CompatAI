"""JavaScript 解析：esprima AST 为主，自写 tokenizer 兜底。

为什么必须有兜底
----------------
esprima 4.0.1 只到 ES2018 左右：遇到 ``obj?.name``、``??``、``#priv``、``1_000``、
类字段、静态块都会直接抛语法错误。如果只用 AST，一份用了可选链的文件会**整份失去检测**
——而这恰恰是最常见的写法。

所以分工是：

* **tokenizer**（总是运行）：负责所有"语法级特性"（``?.`` ``??`` ``??=`` ``**`` 展开、
  async/generator、私有字段、静态块、数字分隔符、BigInt、动态 import、可选 catch 绑定）。
  它在字符层面工作，不受解析器版本限制。
* **esprima AST**（能解析时运行）：负责"API 级特性"，因为它能区分调用、构造、属性访问，
  位置也更准；解析失败则退回 token 链扫描，保证 ``arr.at(-1)`` 这类用法不会漏。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import esprima

from app.core.schemas import ParseWarning
from app.l1.features import Feature, ScanResult

# --------------------------------------------------------------------------- #
# 从 BCD 路径表里选出的语法特性（路径已逐个核对存在于本地索引）
# --------------------------------------------------------------------------- #

SYNTAX_PUNCTUATORS: tuple[tuple[str, str, str], ...] = (
    ("?.", "javascript.operators.optional_chaining", "可选链"),
    ("??=", "javascript.operators.nullish_coalescing_assignment", "空值合并赋值"),
    ("??", "javascript.operators.nullish_coalescing", "空值合并"),
    ("&&=", "javascript.operators.logical_and_assignment", "逻辑与赋值"),
    ("||=", "javascript.operators.logical_or_assignment", "逻辑或赋值"),
    ("**=", "javascript.operators.exponentiation_assignment", "幂赋值"),
    ("**", "javascript.operators.exponentiation", "幂运算符"),
    ("...", "javascript.operators.spread", "展开/剩余"),
)

# 方法名启发式表：只在"不知道对象类型"时兜底（如 arr.at(-1)）。
# 全部路径都已核对存在；命中时会标 inferred=True → 前端显示"上下文未知"。
METHOD_TABLE: dict[str, str] = {
    "at": "javascript.builtins.Array.at",
    "flatMap": "javascript.builtins.Array.flatMap",
    "fromEntries": "javascript.builtins.Object.fromEntries",
    "allSettled": "javascript.builtins.Promise.allSettled",
    "replaceAll": "javascript.builtins.String.replaceAll",
    "matchAll": "javascript.builtins.String.matchAll",
    "closest": "api.Element.closest",
    "replaceChildren": "api.Element.replaceChildren",
    "requestIdleCallback": "api.Window.requestIdleCallback",
}

# 这些标识符后面跟 ( 是语法结构，不是 API 调用
_NOT_CALLS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "typeof", "new", "function",
    "in", "of", "with", "do", "else", "case", "delete", "void", "yield", "await",
    "super", "import", "class", "extends", "instanceof",
})

# window.xxx 这类写法：window 是作用域而非特性
_SCOPE_ROOTS = frozenset({"window", "self", "globalThis", "global"})

# 小写 → BCD 接口名（document.querySelector → api.Document.querySelector）
_ROOT_ALIASES = {
    "document": "Document",
    "navigator": "Navigator",
    "location": "Location",
    "history": "History",
    "screen": "Screen",
    "performance": "Performance",
}

_REGEX_PREV_KEYWORDS = frozenset({
    "return", "typeof", "case", "in", "of", "new", "delete", "void",
    "instanceof", "do", "else", "yield", "await", "throw",
})


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Token:
    kind: str          # identifier / number / string / template / regex / punct
    value: str
    line: int
    column: int


# 长符号优先匹配：??= 必须在 ?? 之前，**= 必须在 ** 之前
_PUNCTUATORS: tuple[str, ...] = tuple(sorted(
    {
        "...", "??=", "**=", "&&=", "||=", "===", "!==", ">>>", "<<=", ">>=",
        "?.", "??", "=>", "==", "!=", "<=", ">=", "&&", "||", "++", "--",
        "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "**", "<<", ">>",
        "(", ")", "{", "}", "[", "]", ".", ",", ";", ":", "?", "=", "!", "&",
        "|", "+", "-", "*", "/", "%", "<", ">", "~", "^", "#", "@",
    },
    key=len,
    reverse=True,
))


def _regex_allowed(previous: Token | None) -> bool:
    """区分 `/` 是正则字面量还是除号（决定要不要把 `/.../` 当代码内容）。"""
    if previous is None:
        return True
    if previous.kind in {"number", "string", "template", "regex"}:
        return False
    if previous.kind == "identifier":
        return previous.value in _REGEX_PREV_KEYWORDS
    return previous.value not in {")", "]", "}"}


def tokenize(source: str) -> list[Token]:
    """最小但够用的 JS tokenizer：跳过字符串/注释/模板字面量文本，保留代码 token。

    模板里的 ``${...}`` 会按代码继续切词（里面有 ``?.`` 也要抓出来）。
    """
    tokens: list[Token] = []
    i = 0
    length = len(source)
    line = 1
    column = 1
    in_template = False
    template_stack: list[int] = []   # 记录进入 ${ 时的花括号深度
    brace_depth = 0
    previous: Token | None = None

    def advance(count: int = 1) -> None:
        nonlocal i, line, column
        for _ in range(count):
            if i < length and source[i] == "\n":
                line += 1
                column = 1
            else:
                column += 1
            i += 1

    while i < length:
        ch = source[i]

        if in_template:
            if ch == "\\":
                advance(2)
            elif ch == "`":
                advance()
                in_template = False
            elif ch == "$" and i + 1 < length and source[i + 1] == "{":
                advance(2)
                in_template = False
                template_stack.append(brace_depth)
            else:
                advance()
            continue

        if ch in " \t\r\n":
            advance()
            continue

        if ch == "/" and i + 1 < length and source[i + 1] == "/":
            while i < length and source[i] != "\n":
                advance()
            continue

        if ch == "/" and i + 1 < length and source[i + 1] == "*":
            advance(2)
            while i + 1 < length and not (source[i] == "*" and source[i + 1] == "/"):
                advance()
            advance(2)
            continue

        if ch in "'\"":
            quote = ch
            start_line, start_column = line, column
            advance()
            while i < length:
                if source[i] == "\\":
                    advance(2)
                    continue
                if source[i] == quote:
                    advance()
                    break
                advance()
            previous = Token("string", quote, start_line, start_column)
            tokens.append(previous)
            continue

        if ch == "`":
            previous = Token("template", "`", line, column)
            tokens.append(previous)
            advance()
            in_template = True
            continue

        if ch == "/" and _regex_allowed(previous):
            start_line, start_column = line, column
            advance()
            in_class = False
            while i < length:
                current = source[i]
                if current == "\\":
                    advance(2)
                    continue
                if current == "[":
                    in_class = True
                    advance()
                    continue
                if current == "]":
                    in_class = False
                    advance()
                    continue
                if current == "/" and not in_class:
                    advance()
                    break
                if current == "\n":
                    break
                advance()
            while i < length and (source[i].isalnum() or source[i] in "_$"):
                advance()
            previous = Token("regex", "/", start_line, start_column)
            tokens.append(previous)
            continue

        if ch.isdigit() or (ch == "." and i + 1 < length and source[i + 1].isdigit()):
            start_line, start_column = line, column
            start = i
            while i < length:
                current = source[i]
                if current.isalnum() or current in "._":
                    advance()
                    continue
                if current in "+-" and i > start and source[i - 1] in "eE":
                    advance()
                    continue
                break
            previous = Token("number", source[start:i], start_line, start_column)
            tokens.append(previous)
            continue

        if ch.isalpha() or ch in "_$":
            start_line, start_column = line, column
            start = i
            while i < length and (source[i].isalnum() or source[i] in "_$"):
                advance()
            previous = Token("identifier", source[start:i], start_line, start_column)
            tokens.append(previous)
            continue

        for punctuator in _PUNCTUATORS:
            if source.startswith(punctuator, i):
                token = Token("punct", punctuator, line, column)
                tokens.append(token)
                previous = token
                advance(len(punctuator))
                if punctuator == "{":
                    brace_depth += 1
                elif punctuator == "}":
                    if template_stack and brace_depth == template_stack[-1]:
                        template_stack.pop()
                        in_template = True
                    else:
                        brace_depth = max(0, brace_depth - 1)
                break
        else:
            advance()

    return tokens


# --------------------------------------------------------------------------- #
# 扫描入口
# --------------------------------------------------------------------------- #

def scan_js(source: str, known_roots: frozenset[str] = frozenset()) -> ScanResult:
    """扫描一段 JS。``known_roots`` 由 detector 从 BCD 索引里取（builtins/api 的根名）。"""
    result = ScanResult()
    if not source.strip():
        return result

    try:
        tokens = tokenize(source)
    except Exception as exc:  # 理论上不会发生，但不能让一个畸形文件带崩请求
        result.warnings.append(ParseWarning(kind="js", message=f"JS 分词失败：{exc}"))
        return result

    result.statements_scanned = sum(1 for token in tokens if token.kind == "identifier")
    result.features.extend(_syntax_features(tokens))
    result.features.extend(_api_features(source, tokens, known_roots))
    return result


# --------------------------------------------------------------------------- #
# 语法级特性（只看 token 流）
# --------------------------------------------------------------------------- #

def _syntax_features(tokens: list[Token]) -> list[Feature]:
    features: list[Feature] = []
    seen: set[tuple[str, int]] = set()

    def emit(path: str, label: str, token: Token) -> None:
        key = (path, token.line)
        if key in seen:      # 同一行同一个特性只报一次，避免 `a?.b?.c` 刷屏
            return
        seen.add(key)
        features.append(
            Feature(kind="js", name=label, candidates=(path,), line=token.line, column=token.column, context=label)
        )

    punct_map = {symbol: (path, label) for symbol, path, label in SYNTAX_PUNCTUATORS}

    for index, token in enumerate(tokens):
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None

        if token.kind == "punct":
            if token.value in punct_map:
                path, label = punct_map[token.value]
                emit(path, f"{label} {token.value}", token)
            elif token.value == "#" and nxt is not None and nxt.kind == "identifier":
                emit("javascript.classes.private_class_fields", f"私有字段 #{nxt.value}", token)
            continue

        if token.kind == "number":
            if "_" in token.value:
                emit("javascript.grammar.numeric_separators", "数字分隔符 1_000", token)
            if token.value.lower().endswith("n"):
                emit("javascript.builtins.BigInt", "BigInt 字面量 1n", token)
            continue

        if token.kind != "identifier":
            continue

        value = token.value
        if value == "await":
            emit("javascript.operators.await", "await", token)
        elif value == "async" and nxt is not None and nxt.value in {"function", "("}:
            emit("javascript.statements.async_function", "async 函数", token)
        elif value == "function" and nxt is not None and nxt.value == "*":
            emit("javascript.statements.generator_function", "生成器函数", token)
        elif value == "import" and nxt is not None and nxt.value == "(":
            emit("javascript.operators.import", "动态 import()", token)
        elif value == "catch" and nxt is not None and nxt.value == "{":
            emit("javascript.statements.try_catch.optional_catch_binding", "可选 catch 绑定", token)
        elif value == "static" and nxt is not None and nxt.value == "{":
            emit("javascript.classes.static_initialization_blocks", "静态初始化块 static {}", token)

    return features


# --------------------------------------------------------------------------- #
# API 级特性（AST 优先，token 兜底）
# --------------------------------------------------------------------------- #

def _api_features(source: str, tokens: list[Token], known_roots: frozenset[str]) -> list[Feature]:
    ast_chains = _ast_chains(source)
    chains = ast_chains if ast_chains is not None else _token_chains(tokens)
    source_kind = "ast" if ast_chains is not None else "token"

    features: list[Feature] = []
    seen: set[tuple[int, int, str]] = set()

    for parts, line, column, is_call in chains:
        for feature in _features_for_chain(parts, is_call, line, column, known_roots):
            key = (feature.line, feature.column, feature.candidates[0])
            if key in seen:
                continue
            seen.add(key)
            features.append(replace(feature, context=feature.context or source_kind))
    return features


def _normalize(parts: list[str]) -> list[str]:
    parts = [part for part in parts if part != "prototype"]
    if parts and parts[0] in _SCOPE_ROOTS:
        parts = parts[1:]
    if parts:
        parts[0] = _ROOT_ALIASES.get(parts[0], parts[0])
    return parts


def _candidates(parts: list[str]) -> tuple[str, ...]:
    """从具体到宽泛生成候选：``Array.prototype.at`` → Array.at → … → Array。

    逐级截断的好处：``console.log`` 这类 BCD 里不存在的深层路径会落到 ``api.console``，
    既不会误报，也不会把 ``console.log`` 塞进 unknown_features 制造噪音。
    """
    out: list[str] = []
    for size in range(len(parts), 0, -1):
        prefix = ".".join(parts[:size])
        out.append(f"javascript.builtins.{prefix}")
        out.append(f"api.{prefix}")
    return tuple(out)


def _features_for_chain(
    raw_parts: list[str],
    is_call: bool,
    line: int,
    column: int,
    known_roots: frozenset[str],
) -> list[Feature]:
    parts = _normalize(list(raw_parts))
    if not parts:
        return []

    root = parts[0]
    features: list[Feature] = []

    if root in known_roots:
        features.append(
            Feature(
                kind="js",
                name=".".join(parts),
                candidates=_candidates(parts),
                line=line,
                column=column,
            )
        )

    # 对象类型未知时的启发式兜底（只对调用生效，避免把普通属性名当 API）
    method = parts[-1]
    if is_call and method in METHOD_TABLE and method not in _NOT_CALLS:
        path = METHOD_TABLE[method]
        if not features:      # root 已知时优先用精确路径，不重复报
            features.append(
                Feature(
                    kind="js",
                    name=f"{method}()",
                    candidates=(path,),
                    line=line,
                    column=column,
                    inferred=True,
                    context="方法名推断",
                )
            )

    return features


def _ast_chains(source: str) -> list[tuple[list[str], int, int, bool]] | None:
    """用 esprima 解析；成功则返回调用/构造处的成员链，失败返回 ``None``。"""
    tree = None
    for parser in (esprima.parseScript, esprima.parseModule):
        try:
            tree = parser(source, {"loc": True})
            break
        except Exception:
            continue
    if tree is None:
        return None

    chains: list[tuple[list[str], int, int, bool]] = []
    for node in _walk_nodes(tree):
        kind = type(node).__name__
        if kind not in {"CallExpression", "NewExpression"}:
            continue
        parts = _node_chain(node.callee)
        if parts is None:
            continue
        location = getattr(node, "loc", None)
        line = location.start.line if location else 1
        column = (location.start.column + 1) if location else 1   # esprima 列号 0-based
        chains.append((parts, line, column, True))
    return chains


def _walk_nodes(root):
    stack = [root]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, esprima.nodes.Node):
            continue
        yield current
        for value in vars(current).values():
            if isinstance(value, (list, esprima.nodes.Node)):
                stack.append(value)


def _node_chain(node) -> list[str] | None:
    if isinstance(node, esprima.nodes.Identifier):
        return [node.name]
    kind = type(node).__name__
    if kind in {"StaticMemberExpression", "MemberExpression"} and not getattr(node, "computed", False):
        owner = _node_chain(node.object)
        prop = node.property
        if owner is None or not isinstance(prop, esprima.nodes.Identifier):
            return None
        return owner + [prop.name]
    return None


def _token_chains(tokens: list[Token]) -> list[tuple[list[str], int, int, bool]]:
    """esprima 解析失败时的兜底：从 token 流里还原 ``a.b.c(`` 形状。"""
    chains: list[tuple[list[str], int, int, bool]] = []
    index = 0
    total = len(tokens)

    while index < total:
        token = tokens[index]
        if token.kind != "identifier" or token.value in {"new", "function", "return"}:
            index += 1
            continue
        if index > 0 and tokens[index - 1].value in {".", "?."}:
            index += 1
            continue

        parts = [token.value]
        cursor = index + 1
        while cursor + 1 < total and tokens[cursor].value in {".", "?."} and tokens[cursor + 1].kind == "identifier":
            parts.append(tokens[cursor + 1].value)
            cursor += 2

        is_call = cursor < total and tokens[cursor].value == "("
        if is_call:
            chains.append((parts, token.line, token.column, True))
            index = cursor
        else:
            index = max(index + 1, cursor)

    return chains
