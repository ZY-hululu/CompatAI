"""把 @mdn/browser-compat-data 的打包 ``data.json`` 转成本项目的精简索引。

为什么必须有这一步（而不是运行期直接读 BCD）
-------------------------------------------
1. BCD 的 support 形态有 8 种：``{version_added: "14.1"}`` / 数组（含 alternative_name）/
   ``"mirror"`` / ``null`` / ``true`` / ``false`` / ``"preview"`` / ``"≤37"``，还有
   ``partial_implementation`` / ``version_removed`` / ``flags``。
   运行期处理这些形态会把 detector 写成一堆 if；全部在构建期归一化，运行期只做纯比较。
2. 只保留检测需要的信息，加载更快、体积更小、diff 更可读。
3. 产物提交进仓库 → 运行期零网络、零 npm 依赖，结果可复现（同一索引 + 同一输入 = 同一输出）。

用法::

    python scripts/build_bcd_index.py
    python scripts/build_bcd_index.py --data node/node_modules/@mdn/browser-compat-data/data.json \
                                      --out data/bcd_index.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DATA = BACKEND_ROOT / "node" / "node_modules" / "@mdn" / "browser-compat-data" / "data.json"
DEFAULT_OUT = BACKEND_ROOT / "data" / "bcd_index.json"

# 只索引检测用得到的命名空间。svg / mathml / webdriver 暂不需要；
# api.* 是 DOM 接口（如 Element.closest），第 4 步做 JS 检测时用得上，先一起建好免得重建。
NAMESPACES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("css.properties", ("css", "properties")),
    ("html.elements", ("html", "elements")),
    # 全局属性（inert / popover / autocapitalize…）：元素专属属性查不到时回落到这里
    ("html.global_attributes", ("html", "global_attributes")),
    ("javascript", ("javascript",)),
    ("api", ("api",)),
)

# 允许在版本号里出现的非数字写法，归一化后原样保留，由 bcd.py 的比较器解释
SPECIAL_VERSIONS = {"preview", "≤"}


# --------------------------------------------------------------------------- #
# support 归一化
# --------------------------------------------------------------------------- #

def _split_added(value: Any) -> tuple[str | None, bool, bool]:
    """把 ``version_added`` 拆成 ``(版本串, never, unknown)``。

    * ``"14.1"`` → ``("14.1", False, False)``
    * ``null`` / ``False`` → 从未支持 → ``(None, True, False)``
    * ``True`` → 支持但版本未知 → ``(None, False, True)``
    * ``"preview"`` / ``"≤37"`` → 原样保留，比较器特殊处理
    """
    if value is None or value is False:
        return None, True, False
    if value is True:
        return None, False, True
    text = str(value).strip()
    if not text:
        return None, True, False
    return text, False, False


def _pick_primary(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """数组形态（多见于 alternative_name / 前缀别名）挑出主条目。"""
    if isinstance(raw, dict):
        return raw, []
    if isinstance(raw, list):
        dicts = [item for item in raw if isinstance(item, dict)]
        primary = next((item for item in dicts if "alternative_name" not in item), None)
        if primary is None and dicts:
            primary = dicts[0]
        alts = [str(item["alternative_name"]) for item in dicts if item.get("alternative_name")]
        return primary, alts
    return None, []


def normalize_support(
    raw: Any,
    browser: str,
    support: dict[str, Any],
    browser_meta: dict[str, dict[str, Any]],
    depth: int = 0,
) -> dict[str, Any]:
    """把单个浏览器的 support 节点归一化成固定形状。"""
    if raw == "mirror":
        # BCD 约定：该浏览器继承另一个浏览器的数据（如 safari_ios → safari）。
        # 映射关系直接从 browsers[].upstream 读，不硬编码映射表。
        upstream = browser_meta.get(browser, {}).get("upstream")
        if not upstream or depth >= 5:
            return {"unknown": True}
        return normalize_support(support.get(upstream), upstream, support, browser_meta, depth + 1)

    entry, alts = _pick_primary(raw)
    if entry is None:
        return {"unknown": True}

    added, never, unknown = _split_added(entry.get("version_added"))
    out: dict[str, Any] = {}
    if added is not None:
        out["added"] = added
    if entry.get("version_removed"):
        out["removed"] = str(entry["version_removed"])
    if never:
        out["never"] = True
    if unknown:
        out["unknown"] = True
    if entry.get("partial_implementation"):
        out["partial"] = True
    if entry.get("flags"):
        # 需要改 flag 才能用 → 默认配置下视为不可用
        out["flag"] = True
    if alts:
        out["alt"] = alts
    return out


def normalize_compat(
    compat: dict[str, Any], browser_meta: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    support = compat.get("support") or {}
    return {
        "desc": compat.get("description", ""),
        "support": {
            browser: normalize_support(raw, browser, support, browser_meta)
            for browser, raw in support.items()
            if browser in browser_meta
        },
    }


# --------------------------------------------------------------------------- #
# 遍历
# --------------------------------------------------------------------------- #

def walk(
    node: Any,
    prefix: str,
    out: dict[str, Any],
    browser_meta: dict[str, dict[str, Any]],
) -> None:
    """深度优先展开 BCD 树，``__compat`` 出现的路径即一条可查询的特性。"""
    if not isinstance(node, dict):
        return
    for key, child in node.items():
        if key.startswith("__") or not isinstance(child, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if "__compat" in child:
            out[path] = normalize_compat(child["__compat"], browser_meta)
        # 有 __compat 的节点同时可能有子特性（gap 的 flex_context 就是），继续往下走
        walk(child, path, out, browser_meta)


def get_path(root: Any, path: tuple[str, ...]) -> Any:
    node = root
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _release_sort_key(version: str) -> tuple[int, tuple[int, ...]]:
    """BCD 的 releases 顺序不是数值序（"10" 会排在 "9.1" 前面），这里显式排序。

    纯数字版本 → ``(0, (12, 1))``；``preview`` / ``TP`` 等非数字 → ``(1, ())`` 排在最后。
    """
    try:
        return (0, tuple(int(part) for part in version.split(".")))
    except ValueError:
        return (1, ())


def build(data: dict[str, Any]) -> dict[str, Any]:
    raw_browsers = data.get("browsers") or {}
    browser_meta: dict[str, dict[str, Any]] = {}
    for browser, info in raw_browsers.items():
        if not isinstance(info, dict):
            continue
        browser_meta[browser] = {
            "name": info.get("name", browser),
            "upstream": info.get("upstream"),
            # 版本列表供前端下拉框使用，倒序（新版本在前）；用户仍可自由填写任意版本号
            "releases": sorted((info.get("releases") or {}).keys(), key=_release_sort_key, reverse=True),
        }

    features: dict[str, Any] = {}
    per_namespace: dict[str, int] = {}
    for prefix, path in NAMESPACES:
        before = len(features)
        node = get_path(data, path)
        if node is None:
            print(f"  ! 命名空间缺失，跳过: {'.'.join(path)}", file=sys.stderr)
            continue
        walk(node, prefix, features, browser_meta)
        per_namespace[prefix] = len(features) - before
        print(f"  {prefix:<18} {per_namespace[prefix]:>6} 条")

    meta_version = (data.get("__meta") or {}).get("version", "unknown")
    return {
        "__meta": {
            "bcd_version": meta_version,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generator": "backend/scripts/build_bcd_index.py",
            "namespaces": [prefix for prefix, _ in NAMESPACES],
            "feature_count": len(features),
        },
        "browsers": browser_meta,
        "features": features,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 BCD 精简索引")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="BCD 打包 data.json 路径")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出索引路径")
    args = parser.parse_args(argv)

    if not args.data.is_file():
        print(f"找不到 BCD 数据: {args.data}", file=sys.stderr)
        print("请先安装：cd backend/node && npm i @mdn/browser-compat-data", file=sys.stderr)
        return 2

    print(f"读取 {args.data} ({args.data.stat().st_size / 1024 / 1024:.1f} MB) ...")
    with args.data.open(encoding="utf-8") as handle:
        data = json.load(handle)

    print("展平命名空间：")
    index = build(data)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=True：索引是可提交的构建产物，避免不同终端编码造成的字节差异
    with args.out.open("w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    size_mb = args.out.stat().st_size / 1024 / 1024
    print(f"\n写出 {args.out}")
    print(f"  BCD 版本 : {index['__meta']['bcd_version']}")
    print(f"  特性总数 : {index['__meta']['feature_count']}")
    print(f"  文件体积 : {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
