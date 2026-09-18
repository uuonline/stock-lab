"""StockLab 应用入口。"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api.routes import router
from .config import settings
from .tasks import scheduler as sched

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("stocklab")

APP_VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("=" * 62)
    log.info("  %s v%s 启动", settings.app_name, APP_VERSION)
    log.info("  数据目录 : %s", settings.data_dir)
    log.info("  数据库   : %s", settings.db_path)
    log.info("  鉴权     : %s", "已开启" if settings.access_token else "关闭（仅建议内网使用）")
    log.info("  调度器   : %s", "启用" if settings.scheduler_enabled else "禁用")
    log.info("  AI 简报  : %s", f"已启用 ({settings.ai_model})" if (settings.ai_enabled and settings.ai_api_key) else "本地规则引擎")
    log.info("=" * 62)
    sched.start()
    try:
        yield
    finally:
        sched.stop()
        log.info("已停止")


app = FastAPI(
    title=settings.app_name,
    version=APP_VERSION,
    description="NAS 私有化股票研究系统 —— 行情 / 基本面 / 选股 / 回测 / AI 简报 / 提醒",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """可选的访问令牌保护。局域网可留空。"""
    token = settings.access_token
    if not token:
        return await call_next(request)

    path = request.url.path
    # 放行静态资源与文档，避免首页打不开
    if path.startswith("/static") or path in ("/favicon.ico",):
        return await call_next(request)

    provided = (
        request.headers.get("X-Access-Token")
        or request.query_params.get("token")
        or request.cookies.get("sl_token")
        or ""
    )
    if provided == token:
        response = await call_next(request)
        if request.query_params.get("token"):
            response.set_cookie("sl_token", token, max_age=86400 * 30, httponly=True, samesite="lax")
        return response

    if path.startswith("/api"):
        return JSONResponse(
            {"detail": "未授权：请提供有效的访问令牌", "code": "unauthorized"},
            status_code=401,
        )
    return JSONResponse(
        {
            "detail": "未授权",
            "hint": "请在网址后追加 ?token=你的令牌，例如 http://主机:8787/?token=xxx",
        },
        status_code=401,
    )


app.include_router(router)


@app.get("/favicon.ico")
def favicon():
    p = settings.web_dir / "static" / "favicon.svg"
    if p.exists():
        return FileResponse(str(p), media_type="image/svg+xml")
    return JSONResponse({}, status_code=404)


@app.get("/")
def index():
    p = settings.web_dir / "templates" / "index.html"
    if not p.exists():
        return JSONResponse({"detail": "前端未构建", "path": str(p)}, status_code=500)
    return FileResponse(str(p), media_type="text/html; charset=utf-8")


_web_static = settings.web_dir / "static"
if _web_static.exists():
    app.mount("/static", StaticFiles(directory=str(_web_static)), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="debug" if settings.debug else "info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
