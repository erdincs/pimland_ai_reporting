"""FastAPI application entry point.

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.connectors.registry import registry
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.middleware.agent_guard import AgentGuardMiddleware
from app.services import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log = get_logger(__name__)
    log.info("app.startup", env=settings.app_env, bedrock_model=settings.bedrock_model_id)
    registry.load()
    scheduler.start()
    yield
    scheduler.stop()
    log.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.app_debug,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(AgentGuardMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()
