"""FastAPI 入口：生命周期（建表、连 Redis/Neo4j、初始化数据）、中间件、路由。"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import cases, dashboard, events, rules
from app.core import config
from app.core.logging import setup_logging
from app.db.session import init_db, session_scope
from app.services import bootstrap
from app.services.runtime import get_runtime, init_runtime

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    if not getattr(app.state, "runtime_ready", False):     # 测试会提前注入 fakeredis + 内存图
        init_runtime()
    with session_scope() as s:
        bootstrap.bootstrap(s, get_runtime(), with_demo=getattr(app.state, "with_demo", True))
    rt = get_runtime()
    log.info("启动完成：规则版本 %s，图谱后端 %s", rt.rule_version, rt.graph.backend)
    from app.services.reliability import projection_worker
    stop = threading.Event()
    worker = threading.Thread(target=projection_worker, args=(rt, stop), daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join()
    close = getattr(rt.graph, "close", None)
    if close:
        close()


def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_NAME, version="1.0.0", lifespan=lifespan,
                  description="实时特征 + 规则 DSL 决策 + 关联图谱 + 案件闭环 + RAG 审核辅助")
    app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})

    @app.get("/health", tags=["运维"])
    def health():
        rt = get_runtime()
        try:
            redis_ok = bool(rt.redis.ping())
        except Exception:
            redis_ok = False
        return {"status": "ok" if redis_ok else "degraded", "redis": redis_ok,
                "graph": rt.graph.backend, "rule_version": rt.rule_version}

    for r in (dashboard.router, events.router, rules.router, cases.router):
        app.include_router(r, prefix=config.API_PREFIX)
    return app


app = create_app()