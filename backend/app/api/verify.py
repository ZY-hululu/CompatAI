"""L3 接口：``POST /api/verify``。

输入原始代码 + 修复后代码 + 目标浏览器，返回：
* ``resolved`` / ``remaining``：按 (type, bcd_path, feature) 比对出的前后差异；
* ``new_issues``：修复**引入**的新问题（这一项最容易漏，也最能说明修复有风险）；
* ``dom_diff``：jsdom 行为对比结果（``ran=False`` 时**不等于**通过）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.schemas import VerifyRequest, VerifyResponse
from app.l1.bcd import IndexMissingError
from app.l3.recheck import verify

router = APIRouter(prefix="/api", tags=["l3"])


@router.post(
    "/verify",
    response_model=VerifyResponse,
    summary="回灌验证（L3）",
    description=(
        "把修复后的代码重新送回 L1 检测，对比前后问题集合，并用 jsdom 对比执行期行为与 DOM 结构。"
        "JS 执行有 5 秒强制超时（同步死循环会导致进程被 kill），超时会在 dom_diff.errors 中说明。"
    ),
)
def verify_endpoint(payload: VerifyRequest) -> VerifyResponse:
    try:
        return verify(payload)
    except IndexMissingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
