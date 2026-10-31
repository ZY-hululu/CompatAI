"""目标浏览器解析与版本比较。

设计约束：
1. 纯函数、零 I/O、零第三方依赖 —— 这样它能在任何层被复用，也最容易被单元测试钉死。
2. 支持任意合法版本号（1~4 段数字），不预设"只用 15.0"这类窄集合。
3. 一次性报告全部非法项，不做"报第一个就返回"的挤牙膏式校验。
4. 这里出现的 ``re`` 只用于**定长词法校验**（版本号是否形如 ``12.1``），不参与代码特征提取，
   与"确定性检测、禁止正则猜代码"的原则不冲突。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# --------------------------------------------------------------------------- #
# 浏览器标识
# --------------------------------------------------------------------------- #

# 前端常见别名 → MDN BCD browser id。BCD 的 id 是唯一权威，别名只做入口便利。
BROWSER_ALIASES: dict[str, str] = {
    "chrome": "chrome",
    "chr": "chrome",
    "safari": "safari",
    "firefox": "firefox",
    "ff": "firefox",
    "edge": "edge",
    "opera": "opera",
    "ie": "ie",
    # 移动端：BCD 用下划线全称，Can I Use 用缩写，两套都收
    "ios": "safari_ios",
    "ios_saf": "safari_ios",
    "safari_ios": "safari_ios",
    "and_chr": "chrome_android",
    "chrome_android": "chrome_android",
    "and_ff": "firefox_android",
    "firefox_android": "firefox_android",
    "samsung": "samsunginternet_android",
    "samsunginternet_android": "samsunginternet_android",
    "webview_android": "webview_android",
    "webview_ios": "webview_ios",
}

# 前端下拉框的主推浏览器（其余走高级输入）
CORE_BROWSERS: tuple[str, ...] = ("chrome", "safari", "firefox", "edge")

# 默认目标集：企业项目常见的"历史兼容基准线"。
# 注意 safari 14.1 是 Flex gap 的支持起点（BCD: gap.flex_context.safari = 14.1），
# 因此这一组目标下 gap 属于"边界通过"；想演示 gap 问题用 safari 14.0。
DEFAULT_TARGETS: tuple[str, ...] = (
    "chrome 90",
    "safari 14.1",
    "firefox 88",
    "edge 90",
)

MAX_TARGETS = 16
MAX_VERSION_SEGMENTS = 4

# 版本号词法：1~4 段纯数字，不接受 "≥90"、"9x"、"latest" 这类模糊写法
_VERSION_RE = re.compile(r"\d+(?:\.\d+){0,3}")


class TargetFormatError(ValueError):
    """目标浏览器配置非法。message 里带全部出错项。"""


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #

VersionKey = tuple[int, ...]


def parse_version(text: str) -> VersionKey:
    """``"12.1"`` → ``(12, 1)``；非法则抛 ``TargetFormatError``。"""
    raw = (text or "").strip()
    if not raw or _VERSION_RE.fullmatch(raw) is None:
        raise TargetFormatError(
            f"版本号 {text!r} 非法：需为 1~{MAX_VERSION_SEGMENTS} 段纯数字，如 15 / 12.1 / 12.1.2"
        )
    key = tuple(int(part) for part in raw.split("."))
    if len(key) > MAX_VERSION_SEGMENTS:
        raise TargetFormatError(f"版本号 {text!r} 超过 {MAX_VERSION_SEGMENTS} 段")
    return key


def compare_versions(left: VersionKey, right: VersionKey) -> int:
    """按数值逐段比较，短的一方补 0：``(15, 0) == (15,)``，``(10, 10) > (10, 9)``。"""
    width = max(len(left), len(right))
    a = left + (0,) * (width - len(left))
    b = right + (0,) * (width - len(right))
    return (a > b) - (a < b)


def version_at_least(current: VersionKey, required: VersionKey) -> bool:
    """``current >= required``。注意 15.0 < 15.1 —— 这正是"用户选 15.0、特性要 15.1"的判定点。"""
    return compare_versions(current, required) >= 0


def dedupe_key(key: VersionKey) -> VersionKey:
    """去掉尾部的 0 段：``(15, 0)`` 与 ``(15,)`` 是同一个目标。

    不去重会让 ``["chrome 90", "chrome 90.0"]`` 把 severity 的 ratio 分母虚增，
    从而把 medium 抬成 high —— 分母错了，等级就错了。
    """
    trimmed = list(key)
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
    return tuple(trimmed)


# --------------------------------------------------------------------------- #
# 目标浏览器
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Target:
    """一个已规范化的检测目标。``browser`` 永远是 BCD id，不是用户输入的原串。"""

    browser: str          # 例：safari / safari_ios / chrome
    version: str          # 保留用户的段层级（"15.0" 不会塌成 "15"），用于回显与可复现
    key: VersionKey       # 数值比较用
    raw: str              # 用户原始输入，便于报错定位

    @property
    def label(self) -> str:
        """写进 ``unsupported_browsers`` / ``targets_resolved`` 的规范名。"""
        return f"{self.browser} {self.version}"

    @property
    def sort_key(self) -> tuple[str, int, ...]:
        return (self.browser, 0, *self.key)


def parse_targets(specs: Sequence[str]) -> list[Target]:
    """把 ``["chrome 90", "safari 12.1"]`` 解析成 ``list[Target]``。

    规则：
    * 每个元素形如 ``"<浏览器> <版本>"``，浏览器大小写不敏感；
    * 重复项按 ``(browser, key)`` 静默去重（不报错，保持幂等），保留首次出现的顺序；
    * 有任何一个非法项就抛 ``TargetFormatError``，且 message 里列出**全部**非法项。
    """
    if not specs:
        raise TargetFormatError("targets 不能为空，至少指定一个目标浏览器")
    if len(specs) > MAX_TARGETS:
        raise TargetFormatError(f"targets 最多 {MAX_TARGETS} 个，收到 {len(specs)} 个")

    errors: list[str] = []
    parsed: list[Target] = []
    seen: set[tuple[str, VersionKey]] = set()

    for index, spec in enumerate(specs):
        raw = (spec or "").strip()
        if not raw:
            errors.append(f"targets[{index}] 为空")
            continue

        parts = raw.split()
        if len(parts) != 2:
            errors.append(
                f"targets[{index}] {raw!r} 格式错误：应为 '<浏览器> <版本>'，例如 'safari 15.0'"
            )
            continue

        browser_token, version_token = parts
        browser = BROWSER_ALIASES.get(browser_token.lower())
        if browser is None:
            errors.append(
                f"targets[{index}] 未知浏览器 {browser_token!r}；"
                f"可用：{', '.join(sorted(BROWSER_ALIASES))}"
            )
            continue

        try:
            key = parse_version(version_token)
        except TargetFormatError as exc:
            errors.append(f"targets[{index}] {exc}")
            continue

        identity = (browser, dedupe_key(key))
        if identity in seen:
            continue
        seen.add(identity)
        parsed.append(Target(browser=browser, version=version_token, key=key, raw=raw))

    if errors:
        raise TargetFormatError("；".join(errors))
    return parsed
