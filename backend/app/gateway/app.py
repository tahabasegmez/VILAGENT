import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.computer_use_runtime import computer_use_runtime
from app.gateway.config import get_gateway_config
from app.gateway.routers import (
    computer_use,
    models,
)
from vilagent.config import app_config as vilagent_app_config
from vilagent.config.app_config import apply_logging_level

AppConfig = vilagent_app_config.AppConfig
get_app_config = vilagent_app_config.get_app_config

import sys
from pathlib import Path

# Ensure logs directory exists at the project root
project_root = Path(__file__).parent.parent.parent.parent
logs_dir = project_root / "logs"
logs_dir.mkdir(exist_ok=True)
log_file = logs_dir / "vilagent.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8")
    ]
)

logger = logging.getLogger(__name__)
_LEGACY_VILAGENT_ENABLED = os.getenv("VILAGENT_ENABLE_LEGACY_VILAGENT", "").lower() in {"1", "true", "yes", "on"}

# Upper bound (seconds) each lifespan shutdown hook is allowed to run.
# Bounds worker exit time so uvicorn's reload supervisor does not keep
# firing signals into a worker that is stuck waiting for shutdown cleanup.
_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0


_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""

    # Load config and check necessary environment variables at startup.
    # `startup_config` is a local snapshot used only for one-shot bootstrap
    # work (logging level, langgraph_runtime engines, channels). Request-time
    # config resolution always routes through `get_app_config()` in
    # `app/gateway/deps.py::get_config()` so `config.yaml` edits become
    # visible without a process restart. We deliberately do NOT cache this
    # snapshot on `app.state` to keep that contract enforceable.
    try:
        startup_config = get_app_config()
        apply_logging_level(startup_config.log_level)
        logger.info("Configuration loaded successfully")
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")

    # Initialize computer-use runtime components
    async with computer_use_runtime(app, startup_config):
        logger.info("VILAGENT runtime initialised")
        yield

    # Tear down the persistent Playwright browser (kept open across runs) on shutdown.
    try:
        from vilagent.computer_use.browser_playwright import close_shared_browser_session
        await close_shared_browser_session()
    except Exception:
        logger.warning("Failed to close the shared browser on shutdown", exc_info=True)

    logger.info("Shutting down API Gateway")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    config = get_gateway_config()
    docs_url = "/docs" if config.enable_docs else None
    redoc_url = "/redoc" if config.enable_docs else None
    openapi_url = "/openapi.json" if config.enable_docs else None

    app = FastAPI(
        title="VILAGENT Gateway",
        description="""
## VILAGENT Gateway

Local Gateway for VILAGENT, a Windows-first computer-use agent operator.

### Features

- **Computer Use**: Desktop sessions, observations, target resolution, approvals, and safe action execution
- **Runs**: Minimal computer-use agent task execution
- **Health Monitoring**: Local runtime health checks

### Architecture

The Electron/Next operator talks to this local Gateway through trusted server-side proxy routes.
        """,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        openapi_tags=[
            {
                "name": "models",
                "description": "Operations for querying available AI models and their configurations",
            },
            {
                "name": "mcp",
                "description": "Manage Model Context Protocol (MCP) server configurations",
            },
            {
                "name": "memory",
                "description": "Access and manage global memory data for personalized conversations",
            },
            {
                "name": "skills",
                "description": "Manage skills and their configurations",
            },
            {
                "name": "artifacts",
                "description": "Access and download thread artifacts and generated files",
            },
            {
                "name": "uploads",
                "description": "Upload and manage user files for threads",
            },
            {
                "name": "threads",
                "description": "Legacy thread-local filesystem data",
            },
            {
                "name": "agents",
                "description": "Create and manage custom agents with per-agent config and prompts",
            },
            {
                "name": "suggestions",
                "description": "Generate follow-up question suggestions for conversations",
            },
            {
                "name": "channels",
                "description": "Manage IM channel integrations (Feishu, Slack, Telegram)",
            },
            {
                "name": "assistants-compat",
                "description": "LangGraph Platform-compatible assistants API (stub)",
            },
            {
                "name": "runs",
                "description": "LangGraph Platform-compatible runs lifecycle (create, stream, cancel)",
            },
            {
                "name": "health",
                "description": "Health check and system status endpoints",
            },
            {
                "name": "computer-use",
                "description": "Manage VILAGENT desktop sessions, observations, UIA queries, audit, and emergency stop",
            },
        ],
    )

    from fastapi.middleware.cors import CORSMiddleware
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(models.router)
    app.include_router(computer_use.router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """Health check endpoint.

        Returns:
            Service health status information.
        """
        return {"status": "healthy", "service": "vilagent-gateway"}

    return app


# Create app instance for uvicorn
app = create_app()
