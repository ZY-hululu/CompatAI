"""BCD 索引的加载与查询 —— 纯比较，无网络、无 LLM、无副作用。

数据来源是 ``scripts/build_bcd_index.py`` 生成的 ``data/bcd_index.json``。
运行期**只做版本比较**，不解释 BCD 的 8 种 support 形态（那些在构建期已归一化）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.core.config import get_settings
from app.core.schemas import Confidence
from app.l1.targets import Target, TargetFormatError, parse_version, version_at_least

# 判断"多数目标不支持"的阈值：>= 50% 记为 high
MAJORITY_RATIO = 0.5


class IndexMissingError(RuntimeError):
    """索引不存在 —— 这不是 500，而是部署没做完，接口应回 503 并给出修复命令。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class SupportEntry:
    """某个浏览器对某特性的支持情况（已归一化）。"""

    added: str | None = None
    removed: str | None = None
    never: bool = False
    unknown: bool = False
    partial: bool = False
    flag: bool = False
    alt: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: dict) -> "SupportEntry":
        return cls(
            added=raw.get("added"),
            removed=raw.get("removed"),
            never=bool(raw.get("never", False)),
            unknown=bool(raw.get("unknown", False)),
            partial=bool(raw.get("partial", False)),
            flag=bool(raw.get("flag", False)),
            alt=tuple(raw.get("alt") or ()),
        )


@dataclass(frozen=True, slots=True)
class FeatureEntry:
    path: str
    desc: str
    support: dict[str, SupportEntry]
    overridden: bool = False
    """是否被 ``data/overrides.json`` 人工校正过（source_data 里会标注）。"""


@dataclass(frozen=True, slots=True)
class BrowserInfo:
    id: str
    name: str
    upstream: str | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """单个目标浏览器对该特性的判定。"""

    target: Target
    supported: bool
    partial: bool
    confidence: Confidence
    requirement: str | None
    """BCD 里的最低版本（``None`` 表示从未支持/需 flag）。"""


@dataclass(frozen=True, slots=True)
class QueryResult:
    path: str
    verdicts: tuple[Verdict, ...]

    @property
    def unsupported(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if not v.supported)

    @property
    def partial_only(self) -> bool:
        """有部分实现、但没有任何目标"完全不支持"。"""
        return not self.unsupported and any(v.partial for v in self.verdicts)

    @property
    def is_problem(self) -> bool:
        return bool(self.unsupported) or self.partial_only


# --------------------------------------------------------------------------- #
# 单目标判定
# --------------------------------------------------------------------------- #

def evaluate(entry: SupportEntry, target: Target) -> Verdict:
    """把一个支持条目与一个目标版本比较。**这是整个系统的判定核心。**"""
    if entry.never or entry.flag:
        # never: BCD 明确写 version_added: null/false；flag: 需开开关才可用
        return Verdict(target, False, False, "certain", None)

    if entry.unknown or entry.added is None:
        # BCD 只写 true：确实支持，但版本未知 —— 按支持处理，置信度降级
        return Verdict(target, True, entry.partial, "version_unknown", None)

    added = entry.added
    if added == "preview":
        return Verdict(target, False, entry.partial, "version_unknown", "preview")

    if added.startswith("≤"):
        # "≤37" 表示"至多 37 版本支持"：目标 >= 37 可判支持，低于则只能说"不确定"
        bound_text = added[1:].strip()
        try:
            bound = parse_version(bound_text)
        except TargetFormatError:
            return Verdict(target, True, entry.partial, "version_unknown", added)
        if version_at_least(target.key, bound):
            return Verdict(target, True, entry.partial, "certain", added)
        return Verdict(target, False, entry.partial, "version_unknown", added)

    try:
        required = parse_version(added)
    except TargetFormatError:
        return Verdict(target, True, entry.partial, "version_unknown", added)

    supported = version_at_least(target.key, required)

    if supported and entry.removed:
        try:
            removed = parse_version(entry.removed)
        except TargetFormatError:
            removed = None
        if removed is not None and version_at_least(target.key, removed):
            supported = False

    return Verdict(target, supported, entry.partial, "certain", added)


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #

class BcdIndex:
    def __init__(
        self,
        meta: dict,
        browsers: dict[str, BrowserInfo],
        features: dict[str, FeatureEntry],
        releases: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.meta = meta
        self.browsers = browsers
        self.features = features
        self._releases: dict[str, tuple[str, ...]] = releases or {}
        # 预先算好"哪些属性有 *_context 子特性"，避免每次请求扫 key
        self._context_sensitive: set[str] = {
            path.rsplit(".", 1)[0]
            for path in features
            if path.rsplit(".", 1)[-1] in {"flex_context", "grid_context", "multicol_context"}
        }
        # 惰性计算：JS 解析器需要"哪些根名在 BCD 里存在"来过滤局部变量
        self._js_roots: frozenset[str] | None = None
        # 惰性计算：特性查询的拼写纠正需要"所有叶子名"
        self._leaf_names: tuple[str, ...] | None = None

    # ---- 元信息 ----

    @property
    def bcd_version(self) -> str:
        return str(self.meta.get("bcd_version", "unknown"))

    def browser_name(self, browser_id: str) -> str:
        info = self.browsers.get(browser_id)
        return info.name if info else browser_id

    def releases(self, browser_id: str) -> list[str]:
        return list(self._releases.get(browser_id, ()))

    # ---- 查询 ----

    def get(self, path: str) -> FeatureEntry | None:
        return self.features.get(path)

    def resolve(self, candidates: Iterable[str]) -> tuple[str, FeatureEntry] | None:
        """按顺序取第一个在索引里存在的候选路径（上下文优先）。"""
        for path in candidates:
            entry = self.features.get(path)
            if entry is not None:
                return path, entry
        return None

    def js_roots(self) -> frozenset[str]:
        """所有可查的"根名"：``javascript.builtins.<X>`` 与 ``api.<X>`` 里的 X。

        JS 解析器用它区分"全局 API"（``Array``、``fetch``、``IntersectionObserver``）
        与"局部变量"（``arr``、``obj``）——后者不该产生任何特性查询。
        """
        if self._js_roots is None:
            roots: set[str] = set()
            for path in self.features:
                if path.startswith("javascript.builtins."):
                    parts = path.split(".")
                    if len(parts) >= 3:
                        roots.add(parts[2])
                elif path.startswith("api."):
                    parts = path.split(".")
                    if len(parts) >= 2:
                        roots.add(parts[1])
            self._js_roots = frozenset(roots)
        return self._js_roots

    def leaf_names(self) -> tuple[str, ...]:
        """所有路径的末段名（``accent-color`` / ``at`` / ``dialog``），用于拼写纠正。"""
        if self._leaf_names is None:
            self._leaf_names = tuple(sorted({path.rsplit(".", 1)[-1] for path in self.features}))
        return self._leaf_names

    def has_context_subfeatures(self, base_path: str) -> bool:
        """该属性是否存在 ``.flex_context`` 之类的上下文子特性。

        用来决定"上下文未知"要不要降置信度：像 ``color`` 这种属性没有上下文分支，
        落回基础路径是准确的，不该标 ``context_unknown``。
        """
        return base_path in self._context_sensitive

    def query(self, entry: FeatureEntry, targets: Iterable[Target]) -> QueryResult:
        return QueryResult(
            path=entry.path,
            verdicts=tuple(evaluate(entry.support.get(t.browser, SupportEntry(unknown=True)), t) for t in targets),
        )


def _load_overrides(path: Path) -> dict[str, dict[str, dict]]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return raw


def _apply_overrides(
    features: dict[str, FeatureEntry], overrides: dict[str, dict[str, dict]]
) -> int:
    """把人工校正表合并进索引，返回生效条数。

    只在 BCD 结构表达不出真实问题时使用（如 Safari 早期 ``position: sticky`` 必须带前缀），
    每一条都应在 overrides.json 里写清 reason，否则下次没人敢删。
    """
    applied = 0
    for path, per_browser in overrides.items():
        entry = features.get(path)
        if entry is None:
            continue
        support = dict(entry.support)
        for browser, patch in per_browser.items():
            if browser.startswith("_"):  # 允许 "_reason" 之类的注释键
                continue
            base = support.get(browser, SupportEntry())
            support[browser] = SupportEntry(
                added=patch.get("added", base.added),
                removed=patch.get("removed", base.removed),
                never=bool(patch.get("never", base.never)),
                unknown=bool(patch.get("unknown", base.unknown)),
                partial=bool(patch.get("partial", base.partial)),
                flag=bool(patch.get("flag", base.flag)),
                alt=base.alt,
            )
        features[path] = FeatureEntry(
            path=entry.path, desc=entry.desc, support=support, overridden=True
        )
        applied += 1
    return applied


@lru_cache(maxsize=2)
def load_index(index_path: str | None = None, overrides_path: str | None = None) -> BcdIndex:
    """加载索引（进程内缓存）。路径可显式指定，测试用临时索引时不必改环境变量。"""
    settings = get_settings()
    path = Path(index_path) if index_path else settings.bcd_index_path
    overrides = Path(overrides_path) if overrides_path else settings.bcd_overrides_path

    if not path.is_file():
        raise IndexMissingError(
            f"BCD 索引不存在：{path}。请先运行 "
            f"`python scripts/build_bcd_index.py`（需已安装 @mdn/browser-compat-data）"
        )

    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)

    browsers = {
        browser_id: BrowserInfo(
            id=browser_id,
            name=info.get("name", browser_id),
            upstream=info.get("upstream"),
        )
        for browser_id, info in (raw.get("browsers") or {}).items()
    }
    features = {
        feature_path: FeatureEntry(
            path=feature_path,
            desc=info.get("desc", ""),
            support={
                browser_id: SupportEntry.from_json(support)
                for browser_id, support in (info.get("support") or {}).items()
            },
        )
        for feature_path, info in (raw.get("features") or {}).items()
    }

    applied = _apply_overrides(features, _load_overrides(overrides))

    index = BcdIndex(
        meta=raw.get("__meta") or {},
        browsers=browsers,
        features=features,
        releases={
            browser_id: tuple(info.get("releases") or ())
            for browser_id, info in (raw.get("browsers") or {}).items()
        },
    )
    if applied:
        print(f"[bcd] 已应用 {applied} 条人工校正（{overrides}）")
    return index


def default_index() -> BcdIndex:
    return load_index()
