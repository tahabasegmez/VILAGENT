"""Internal registry for CU tools to access the host control and action owner.

The host control is set once at agent initialization time by
``make_computer_use_agent`` and read by tools at invocation time.

This avoids injecting config through tool parameters, which is not
supported by the ``@tool`` decorator in VILAGENT's LangChain version.
"""

from __future__ import annotations

import threading
from typing import Any

# Module-level thread-safe singleton.
_lock = threading.Lock()
_host_control: Any = None
_action_owner: dict[str, str] | None = None


def set_host_control(ctrl: Any) -> None:
    """Set the host control instance (called once at agent init)."""
    global _host_control
    with _lock:
        _host_control = ctrl


def get_host_control() -> Any:
    """Retrieve the ``RemoteWindowsHostControl``."""
    with _lock:
        ctrl = _host_control
    if ctrl is None:
        raise RuntimeError(
            "Computer-use host control not initialized.  "
            "Ensure make_computer_use_agent was called with a valid host."
        )
    return ctrl


def set_action_owner(owner: dict[str, str]) -> None:
    """Set the default action owner (called once at agent init)."""
    global _action_owner
    with _lock:
        _action_owner = owner


def get_action_owner(thread_id: str = "vilagent", run_id: str = "default") -> dict[str, str]:
    """Retrieve or build an ``ActionOwner`` dict."""
    with _lock:
        owner = _action_owner
    if owner is not None:
        return owner
    return {"thread_id": thread_id, "run_id": run_id, "agent_id": "cu-lead"}


async def ensure_desktop_session(session_id: str | None = None) -> str:
    """Return an existing desktop session id, creating one when needed.

    Small planner models often guess ``default`` or omit ``session_id``.  The
    host runtime should absorb that friction instead of asking the user for an
    internal id.
    """
    ctrl = get_host_control()
    requested = (session_id or "").strip()
    if requested in {"", "default", "default-session"}:
        snapshot = await ctrl.create_session(None)
        return snapshot.session.session_id

    try:
        await ctrl.get_session(requested)
        return requested
    except Exception as exc:
        if getattr(exc, "__class__", type(exc)).__name__ != "RemoteSessionNotFoundError":
            raise
        snapshot = await ctrl.create_session(requested)
        return snapshot.session.session_id
