"""VILAGENT gateway: the local FastAPI app that serves the operator UI and its API."""

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from vilagent.config.app_config import apply_logging_level, get_app_config
from vilagent.server import api as computer_use
from vilagent.server import browser_api, memory_api, models_api
from vilagent.server.config import get_gateway_config
from vilagent.server.runtime import agent_runtime

_LOGS_DIR = get_gateway_config().data_dir / "logs"
_LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOGS_DIR / "agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    try:
        startup_config = get_app_config()
    except Exception as exc:
        logger.exception("Failed to load configuration during gateway startup")
        raise RuntimeError(f"Failed to load configuration during gateway startup: {exc}") from exc
    apply_logging_level(startup_config.log_level)
    gateway_config = get_gateway_config()
    logger.info("Starting VILAGENT gateway on %s:%s (data dir: %s)", gateway_config.host, gateway_config.port, gateway_config.data_dir)

    async with agent_runtime(app, startup_config):
        yield
    logger.info("VILAGENT gateway stopped")


def create_app() -> FastAPI:
    config = get_gateway_config()
    app = FastAPI(
        title="VILAGENT Gateway",
        description="Local gateway for VILAGENT, a Windows-first computer-use agent.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if config.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.enable_docs else None,
    )
    if config.dev_origin:
        # Only in development, where the UI is served by `next dev` on another port.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[config.dev_origin],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(computer_use.router)
    app.include_router(memory_api.router)
    app.include_router(models_api.router)
    app.include_router(browser_api.router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        return {"status": "healthy", "service": "vilagent-gateway"}

    if config.static_dir is not None:
        app.mount("/", StaticFiles(directory=config.static_dir, html=True), name="ui")
    return app


app = create_app()
