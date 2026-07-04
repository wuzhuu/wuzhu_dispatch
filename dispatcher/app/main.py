"""wuzhu-dispatch Dispatcher — central control plane."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import os
from pathlib import Path

from .config import settings, check_production_settings
from .database import init_db
from .middleware.ratelimit import RateLimitMiddleware
from .middleware.security import CSRFMiddleware, SecurityHeadersMiddleware
from .routes import admin, auth, client, compute, dashboard
from .scheduler import scheduler_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RATE_LIMIT_RULES: dict[str, tuple[int, int]] = {
    "login": (settings.rate_limit_login, settings.rate_limit_login_window),
    "heartbeat": (settings.rate_limit_heartbeat, settings.rate_limit_heartbeat_window),
    "pull": (settings.rate_limit_pull, settings.rate_limit_pull_window),
    "log": (settings.rate_limit_log, settings.rate_limit_log_window),
    "renew": (settings.rate_limit_renew, settings.rate_limit_renew_window),
    "task_create": (settings.rate_limit_task_create, settings.rate_limit_task_create_window),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Production-mode secret check
    try:
        check_production_settings()
    except RuntimeError as e:
        logger.critical(str(e))
        raise

    logger.info("Initializing database …")
    await init_db()
    logger.info("Database ready.")

    scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    logger.info("Dispatcher shutdown.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="wuzhu-dispatch (Dispatcher)",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    # ── Middleware (order: last added = first executed) ──────────
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CSRFMiddleware, csrf_secret=settings.session_secret)

    RateLimitMiddleware.RULES = RATE_LIMIT_RULES
    app.add_middleware(RateLimitMiddleware)

    # CORS — origins from settings.cors_origins_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ─────────────────────────────────────────────────
    app.include_router(auth.router)      # /api/v1/auth/*
    app.include_router(client.router)    # /api/v1/client/*
    app.include_router(admin.router)     # /api/v1/admin/*
    app.include_router(compute.router)   # /api/v1/compute/*
    app.include_router(dashboard.router) # /api/v1/admin/dashboard/*

    # ── Web Dashboard (Jinja2) ────────────────────────────────────
    _templates_dir = Path(__file__).resolve().parent / "templates"
    _static_dir = Path(__file__).resolve().parent / "static"
    _templates_dir.mkdir(exist_ok=True)
    _static_dir.mkdir(exist_ok=True)
    templates = Jinja2Templates(directory=str(_templates_dir))

    # Serve static files
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/admin", include_in_schema=False)
    async def admin_dashboard(request: Request):
        """Web Dashboard entry point."""
        return templates.TemplateResponse(request, "dashboard.html", {"request": request})

    @app.get("/admin/{path:path}", include_in_schema=False)
    async def admin_dashboard_spa(request: Request, path: str):
        """Catch-all SPA fallback."""
        return templates.TemplateResponse(request, "dashboard.html", {"request": request})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
