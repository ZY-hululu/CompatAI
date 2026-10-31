"""FastAPI 应用装配。

路由清单与状态：
    GET  /healthz          ✅ liveness
    GET  /api/meta         ✅ 前端初始化：默认目标集、浏览器版本候选、BCD 版本
    POST /api/detect       ✅ L1 确定性静态检测
    POST /api/explain-fix  ⏳ L2（LLM 解释与修复）
    POST /api/verify       ⏳ L3（回灌检测 + jsdom）
    GET  /                 ✅ 静态托管 frontend/index.html（同源，省掉 CORS 调试）
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import detect as detect_api
from app.api import explain as explain_api
from app.api import verify as verify_api
from app.core.config import PROJECT_ROOT, get_settings
from app.l1.bcd import IndexMissingError, default_index
from app.l1.targets import CORE_BROWSERS, DEFAULT_TARGETS, MAX_TARGETS

settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("compat")

app = FastAPI(
    title="AI 浏览器兼容性助手",
    version="0.3.0",
    description="L1 确定性静态检测 / L2 LLM 解释与修复 / L3 轻量验证",
)

# 前端也可能被独立静态服务器（:5500 / file://）打开，保留 CORS 便于调试。
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    """liveness：不触碰 BCD 索引，避免健康检查把 IO 拖垮。"""
    return {"status": "ok"}


@app.middleware("http")
async def no_cache_for_app_shell(request, call_next):
    """让页面外壳（HTML / 自家 JS / CSS）每次都向服务器确认，避免"改了前端却仍在跑旧代码"。

    踩过的坑：`fix_code_*` 改成 `fix_edits` 后，浏览器仍用缓存里的旧 `app.js`，
    于是后端明明返回了 11 条补丁，页面却提示"模型没有给出可直接应用的修复"。
    vendor（Tailwind / ECharts）体积大且极少变，保留默认缓存策略。
    """
    response = await call_next(request)
    path = request.url.path
    if "/assets/vendor/" in path:
        return response
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# 注意顺序：先注册 /api 路由，再挂静态目录 —— 挂载点 "/" 会兜底剩余路径，
# 但 Starlette 按注册顺序匹配，所以 /api/* 不会被静态目录吞掉。
app.include_router(detect_api.router)
app.include_router(explain_api.router)
app.include_router(verify_api.router)


@app.get("/api/meta", tags=["meta"])
def meta() -> dict[str, object]:
    """前端初始化所需的一切：默认目标、可选浏览器、每个浏览器的版本候选。

    ``releases`` 只作为输入框候选（datalist），用户仍可填任意合法版本号；
    它来自本地 BCD 索引，不引入任何网络依赖。
    """
    payload: dict[str, object] = {
        "browsers": list(CORE_BROWSERS),
        "default_targets": list(DEFAULT_TARGETS),
        "max_targets": MAX_TARGETS,
        "max_source_chars": settings.max_source_chars,
        "bcd_index_ready": settings.bcd_index_path.is_file(),
        "bcd_version": None,
        "releases": {},
    }

    try:
        index = default_index()
    except IndexMissingError:
        # 索引没建好也要能打开页面：前端会据此给出 503 提示
        return payload

    payload["bcd_version"] = index.bcd_version
    payload["releases"] = {
        browser: index.releases(browser)
        for browser in (*CORE_BROWSERS, "safari_ios", "opera")
        if index.releases(browser)
    }
    return payload


# 同源托管前端：uvicorn 起来后直接开 http://127.0.0.1:8000/ 即可联调。
FRONTEND_DIR = PROJECT_ROOT.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    logger.info("静态托管前端目录：%s", FRONTEND_DIR)
