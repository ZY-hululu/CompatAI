"""L1 接口：``POST /api/detect``。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.schemas import DetectRequest, DetectResponse
from app.l1.bcd import IndexMissingError
from app.l1.detector import detect

router = APIRouter(prefix="/api", tags=["l1"])


@router.post(
    "/detect",
    response_model=DetectResponse,
    summary="确定性静态检测（L1）",
    description=(
        "解析 HTML/CSS/JS 源码，逐个特性查询本地 MDN BCD 索引，返回结构化 issues。"
        "该接口不调用任何大模型，同一输入必然得到同一输出。"
    ),
)
def detect_endpoint(payload: DetectRequest) -> DetectResponse:
    try:
        return detect(payload)
    except IndexMissingError as exc:
        # 部署没做完 → 503，并在 detail 里给出可执行的修复命令
        raise HTTPException(status_code=503, detail=str(exc)) from exc
