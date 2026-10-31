"""L2 接口：``POST /api/explain-fix``。

约定：**永远返回 200**。模型不可用、输出不合法、网络失败都收敛成
``degraded=True`` + 模板化解释，而不是 5xx —— 前端不需要为 L2 写第二套错误 UI。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.schemas import ExplainFixRequest, ExplainFixResponse
from app.l2.llm import explain_and_fix

router = APIRouter(prefix="/api", tags=["l2"])


@router.post(
    "/explain-fix",
    response_model=ExplainFixResponse,
    summary="解释与修复（L2）",
    description=(
        "把 L1 的 issues 交给大模型解释并给出修复建议。"
        "带四道闸门：白名单提示词 → 结构化 JSON 输出 → 后置校验（版本/口径/语法）→ 确定性降级。"
        "任一闸门失败都会重试，重试耗尽则返回 degraded=True 的模板化结果，不会 5xx。"
    ),
)
def explain_endpoint(payload: ExplainFixRequest) -> ExplainFixResponse:
    return explain_and_fix(payload)
