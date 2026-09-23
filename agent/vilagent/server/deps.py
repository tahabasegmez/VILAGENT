"""FastAPI dependencies for the VILAGENT gateway."""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request

from vilagent.config.app_config import AppConfig, get_app_config
from vilagent.server.runtime import Runtime

INTERNAL_AUTH_HEADER_NAME = "X-VILAGENT-Internal-Token"
INTERNAL_AUTH_ENV_VAR = "VILAGENT_INTERNAL_AUTH_TOKEN"

# The launcher shares this token with the UI; without it every request is rejected.
_INTERNAL_AUTH_TOKEN = os.environ.get(INTERNAL_AUTH_ENV_VAR) or secrets.token_urlsafe(32)


def internal_auth_headers() -> dict[str, str]:
    return {INTERNAL_AUTH_HEADER_NAME: _INTERNAL_AUTH_TOKEN}


def is_valid_internal_auth_token(token: str | None) -> bool:
    return bool(token) and secrets.compare_digest(token, _INTERNAL_AUTH_TOKEN)


def require_internal_request(request: Request) -> None:
    """Allow a route only to trusted local callers holding the launch token."""
    if not is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
        raise HTTPException(status_code=403, detail="Computer-use APIs require trusted internal authentication")


def get_config() -> AppConfig:
    """The settings as stored in the state file."""
    return get_app_config()


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="The agent runtime is not running (computer_use.enabled is off in the settings, or it failed to start — see the agent log).")
    return runtime
