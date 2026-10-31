"""特性查询接口：``POST /api/lookup``。

不经过 LLM：给的答案直接来自本地 BCD 索引，与 L1 检测用的是同一套版本比较。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.schemas import LookupRequest, LookupResponse, LookupVerdict
from app.l1.bcd import IndexMissingError, default_index
from app.l1.lookup import alternatives, evaluate_for, resolve, summary_for

router = APIRouter(prefix="/api", tags=["l1"])


@router.post(
    "/lookup",
    response_model=LookupResponse,
    summary="查某个特性在目标浏览器里支不支持（L1 查表）",
    description=(
        "输入 CSS 属性 / JS API / HTML 标签或属性，直接返回每个目标浏览器的支持情况与 BCD 依据。"
        "未命中时会给出相近的 BCD 路径，便于纠正拼写。"
    ),
)
def lookup_endpoint(payload: LookupRequest) -> LookupResponse:
    try:
        index = default_index()
    except IndexMissingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    targets = payload.parsed_targets()
    resolved = resolve(index, payload.feature, payload.kind)

    if resolved is None:
        return LookupResponse(
            status="success",
            query=payload.feature,
            found=False,
            alternatives=alternatives(index, payload.feature),
            summary=f"BCD 中查不到 {payload.feature!r}；请检查拼写或换一个特性名",
        )

    result = evaluate_for(index, resolved, targets)
    entry = index.get(resolved.path)

    return LookupResponse(
        status="success",
        query=payload.feature,
        found=True,
        kind=resolved.kind,
        bcd_path=resolved.path,
        description=resolved.description or None,
        verdicts=[
            LookupVerdict(
                browser=verdict.target.browser,
                version=verdict.target.version,
                label=verdict.target.label,
                supported=verdict.supported,
                partial=verdict.partial,
                confidence=verdict.confidence,
                requirement=verdict.requirement,
            )
            for verdict in result.verdicts
        ],
        unsupported_browsers=[verdict.target.label for verdict in result.unsupported],
        summary=summary_for(index, result, entry),
    )
