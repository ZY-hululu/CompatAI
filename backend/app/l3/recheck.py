"""L3 轻量验证：修复码回灌 L1 + jsdom 前后 DOM 对比。

两件事分开做、分开报告
--------------------
1. **静态回灌**：把 ``fixed`` 重新丢给 L1，按 ``(type, bcd_path, feature)`` 比对前后集合，
   得出 ``resolved`` / ``remaining`` / ``new_issues``。用 key 而不是 id/行号比对，
   因为修复会让行号整体位移。
2. **行为对比**：jsdom 跑原始与修复后的代码，比较执行期异常与 DOM 结构变化。

刻意不做的
----------
* 不用 Playwright（项目约束）：jsdom **没有布局引擎**，所以"Flex gap 缺失导致的视觉间距问题"
  它验证不了。``dom_diff.ran=True`` 只代表"JS 行为与 DOM 结构已比对"，不代表像素正确。
* 不把 CSS 修复交给 jsdom：CSS 由 L1 回灌判定，jsdom 只负责 JS/HTML。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.core.schemas import (
    DetectRequest,
    DetectResponse,
    DomDiff,
    Issue,
    SourceCode,
    VerifyRequest,
    VerifyResponse,
)
from app.l1.bcd import BcdIndex, default_index
from app.l1.detector import detect

logger = logging.getLogger("compat.l3")

MAX_ERRORS = 20


@dataclass(slots=True)
class Snapshot:
    """jsdom 一次执行的快照。"""

    ok: bool
    errors: list[str] = field(default_factory=list)
    html: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    timed_out: bool = False


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def verify(
    payload: VerifyRequest,
    *,
    index: BcdIndex | None = None,
    settings: Settings | None = None,
) -> VerifyResponse:
    settings = settings or get_settings()
    index = index if index is not None else default_index()
    targets = payload.targets

    before = _detect(payload.original, targets, index)
    after = _detect(payload.fixed, targets, index)

    before_keys = {_key(issue): issue for issue in before.issues}
    after_keys = {_key(issue): issue for issue in after.issues}

    resolved = [issue.id for issue in before.issues if _key(issue) not in after_keys]
    remaining = [issue.id for issue in before.issues if _key(issue) in after_keys]
    new_issues = [issue for issue in after.issues if _key(issue) not in before_keys]

    # "resolved 不等于真修好"：修复可能只是把特性改写成无法识别的形态
    unknown_after = list(after.unknown_features)
    needs_review = _needs_review(before.issues, resolved, unknown_after)

    dom_diff = _dom_diff(payload.original, payload.fixed, settings)
    timed_out = any("超时" in message for message in dom_diff.errors)

    return VerifyResponse(
        status="partial" if timed_out else "success",
        before_count=len(before.issues),
        after_count=len(after.issues),
        resolved=resolved,
        remaining=remaining,
        new_issues=new_issues,
        unknown_features=unknown_after,
        needs_review=needs_review,
        dom_diff=dom_diff,
    )


def _tokens(text: str) -> set[str]:
    """把 ``<div open>`` / ``foo.bar`` 这类特性名拆成可比对的词元。"""
    return {part for part in re.split(r"[^0-9A-Za-z_$\-]+", text) if len(part) >= 2}


def _issue_tokens(issue: Issue) -> set[str]:
    """提取"这条 issue 的身份词元"。

    HTML 条目要特别小心：``<div inert>`` 与 ``<div open>`` 共享通用标签名 ``div``，
    若把标签名也算进身份，会产生大量假阳性。所以带属性时**只用属性名**，
    只有纯元素问题（``<dialog>``）才用标签名。
    """
    if issue.type == "html":
        parts = issue.feature.strip("<>").split()
        identity = parts[1] if len(parts) >= 2 else (parts[0] if parts else "")
        return {identity} if len(identity) >= 2 else set()
    if issue.type == "js":
        return {segment for segment in issue.feature.replace("()", "").split(".") if len(segment) >= 2}
    return {issue.feature} if len(issue.feature) >= 2 else set()


def _needs_review(
    issues: list[Issue], resolved_ids: list[str], unknown_features: list[str]
) -> list[str]:
    """在 resolved 里挑出"很可能只是查不到"的条目。

    判定方式：把修复后的未识别特性拆成词元，与已解决 issue 的特性词元求交集。
    例：``<dialog open>`` 被改成 ``<div open>`` → 未知项 ``<div open>`` 含 ``open``，
    与 ``<dialog open>`` 的词元相交 → 标记待复核。

    这是启发式而非证明（所以叫"待复核"而不是"未修复"），但它把
    "查不到" 与 "真解决" 明确分开了 —— 界面上不再混为一谈。
    """
    if not unknown_features:
        return []

    unknown_tokens: set[str] = set()
    for feature in unknown_features:
        unknown_tokens |= _tokens(feature)
    if not unknown_tokens:
        return []

    resolved = set(resolved_ids)
    return [
        issue.id
        for issue in issues
        if issue.id in resolved and _issue_tokens(issue) & unknown_tokens
    ]


# --------------------------------------------------------------------------- #
# 静态回灌
# --------------------------------------------------------------------------- #

def _detect(code: SourceCode, targets: list[str], index: BcdIndex) -> DetectResponse:
    return detect(
        DetectRequest(html=code.html, css=code.css, js=code.js, targets=targets),
        index=index,
    )


def _key(issue: Issue) -> tuple[str, str, str]:
    """问题身份 = 类型 + BCD 路径 + 特性名。

    不含行号与 id：修复会让行号位移、id 也会重排，用它们比对会得出"全都变了"的假结论。
    """
    return (issue.type, issue.bcd_path, issue.feature)


# --------------------------------------------------------------------------- #
# jsdom 执行
# --------------------------------------------------------------------------- #

def _invoke(
    codes: list[SourceCode],
    settings: Settings,
    timeout: float,
) -> list[Snapshot | None]:
    """一次 node 启动跑完所有快照（批量可以省掉每次 ~2s 的启动开销）。

    超时分两层：
    * **node 内部**：worker 线程 + ``worker.terminate()``，能打断同步死循环（主手段）；
    * **Python 进程级**：任务卡在 node 主进程本身时，taskkill 整棵树（兜底）。
    """
    node = shutil.which(settings.node_binary)
    if not node or not settings.jsdom_runner_path.is_file():
        logger.warning("jsdom 不可用：node=%s runner=%s", node, settings.jsdom_runner_path)
        return [None] * len(codes)

    payload = json.dumps(
        {
            "timeoutMs": int(timeout * 1000),
            "runs": [{"html": code.html, "js": code.js} for code in codes],
        },
        ensure_ascii=False,
    )
    process = subprocess.Popen(
        [node, str(settings.jsdom_runner_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
    )

    # node 自己每次最多跑 timeout 秒，这里留足启动与退出的余量
    backstop = timeout * len(codes) + 15
    try:
        stdout, stderr = process.communicate(input=payload, timeout=backstop)
    except subprocess.TimeoutExpired:
        logger.warning("jsdom 主进程超时（%ss），终止进程树", backstop)
        _kill_tree(process)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _close_pipes(process)
        return [
            Snapshot(
                ok=False,
                timed_out=True,
                errors=[f"jsdom 主进程超时（>{backstop:g}s），已强制终止"],
            )
            for _ in codes
        ]
    except OSError as exc:
        _close_pipes(process)
        logger.warning("jsdom 启动失败：%s", exc)
        return [None] * len(codes)

    if process.returncode != 0:
        message = f"node 退出码 {process.returncode}：{(stderr or '')[:300]}"
        return [Snapshot(ok=False, errors=[message]) for _ in codes]

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        message = f"jsdom 输出不是 JSON：{(stdout or '')[:300]}"
        return [Snapshot(ok=False, errors=[message]) for _ in codes]

    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        raw_results = [data] if isinstance(data, dict) else []

    snapshots: list[Snapshot | None] = []
    for index in range(len(codes)):
        item = raw_results[index] if index < len(raw_results) else {}
        snapshots.append(
            Snapshot(
                ok=bool(item.get("ok")),
                timed_out=bool(item.get("timeout")),
                errors=[str(error) for error in item.get("errors") or []],
                html=str(item.get("html") or ""),
                counts={str(k): int(v) for k, v in (item.get("counts") or {}).items()},
            )
        )
    return snapshots


def run_snapshot(
    code: SourceCode,
    settings: Settings | None = None,
    timeout: float | None = None,
) -> Snapshot | None:
    """执行一次 jsdom。返回 ``None`` 表示环境不可用（没装 node / jsdom）。"""
    settings = settings or get_settings()
    timeout = timeout if timeout is not None else settings.jsdom_timeout_seconds
    return _invoke([code], settings, timeout)[0]


def _kill_tree(process: subprocess.Popen) -> None:
    """杀整个进程树。Windows 上 node 可能被 .CMD 包装，kill 单进程会留下孤儿占着管道。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            process.kill()
    except Exception:  # noqa: BLE001 - 清理路径不允许再抛
        try:
            process.kill()
        except Exception:
            pass


def _close_pipes(process: subprocess.Popen) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            if stream:
                stream.close()
        except Exception:
            pass


def _dom_diff(original: SourceCode, fixed: SourceCode, settings: Settings) -> DomDiff:
    if not (original.js.strip() or original.html.strip()):
        return DomDiff(ran=False, errors=["没有 JS/HTML 代码，跳过行为对比"])

    base, after = _invoke([original, fixed], settings, settings.jsdom_timeout_seconds)
    if base is None or after is None:
        return DomDiff(ran=False, errors=["jsdom 不可用（未安装 node 或 jsdom）"])

    errors = [f"原始代码: {message}" for message in base.errors]
    errors += [f"修复后: {message}" for message in after.errors]

    if base.timed_out or after.timed_out:
        return DomDiff(ran=False, errors=errors[:MAX_ERRORS])

    if base.errors and not base.html:
        # 原始代码就没跑起来，比较没有意义
        return DomDiff(
            ran=True,
            changed=False,
            added_nodes=0,
            removed_nodes=0,
            errors=errors[:MAX_ERRORS],
        )

    tags = set(base.counts) | set(after.counts)
    added = sum(max(0, after.counts.get(tag, 0) - base.counts.get(tag, 0)) for tag in tags)
    removed = sum(max(0, base.counts.get(tag, 0) - after.counts.get(tag, 0)) for tag in tags)

    return DomDiff(
        ran=True,
        changed=_normalize(base.html) != _normalize(after.html),
        added_nodes=added,
        removed_nodes=removed,
        errors=errors[:MAX_ERRORS],
    )


def _normalize(html: str) -> str:
    return " ".join(html.split())
