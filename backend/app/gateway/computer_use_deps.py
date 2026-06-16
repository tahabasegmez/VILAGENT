"""Lightweight dependencies for trusted VILAGENT Gateway routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, Request

from app.gateway.internal_auth import INTERNAL_AUTH_HEADER_NAME, is_valid_internal_auth_token

if TYPE_CHECKING:
    from vilagent.computer_use.remote_host import RemoteWindowsHostControl
    from vilagent.computer_use.windows import WindowsAgentHost


def get_computer_use_host(request: Request) -> WindowsAgentHost:
    host = getattr(request.app.state, "computer_use_host", None)
    if host is None:
        raise HTTPException(status_code=503, detail="Computer use host not available")
    return cast("WindowsAgentHost", host)


def require_internal_request(request: Request) -> None:
    """Allow a route only to trusted local/internal Gateway callers."""
    if not is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
        raise HTTPException(status_code=403, detail="Computer-use host APIs require trusted internal authentication")


def get_computer_use_remote_control(request: Request) -> RemoteWindowsHostControl:
    remote = getattr(request.app.state, "computer_use_remote_control", None)
    if remote is None:
        raise HTTPException(status_code=503, detail="Computer use remote host control not available")
    return cast("RemoteWindowsHostControl", remote)
