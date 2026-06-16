"""verify_condition — check a postcondition against the latest observation.

Thin wrapper around the host's routed verification system.
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool("verify_condition")
async def verify_condition_tool(
    condition_type: str,
    condition_value: str,
    session_id: str | None = None,
) -> str:
    """Check a condition against the current desktop state.

    Supported types: 'screen_changed', 'screen_unchanged',
    'uia_element exists <name>', 'uia_element not_exists <name>'.
    Returns whether the condition is satisfied.

    Args:
        condition_type: Condition kind (screen_changed, uia_element, etc.).
        condition_value: Expected value (e.g. 'exists Notepad' or 'not_exists Save').
        session_id: Optional desktop session. Omit it for the active/default
            Windows session; VILAGENT will create one when needed.
    """
    from vilagent.computer_use.models import UIAQuery
    from vilagent.computer_use.tools._context import ensure_desktop_session, get_host_control

    remote = get_host_control()

    try:
        resolved_session_id = await ensure_desktop_session(session_id)
        await remote.observe_session(resolved_session_id)
    except Exception as exc:
        return f"Verification failed: could not observe desktop ({getattr(exc, 'code', exc.__class__.__name__)})."

    if condition_type == "uia_element":
        parts = condition_value.split(" ", 1)
        check = parts[0] if parts else "exists"
        element_desc = parts[1] if len(parts) > 1 else condition_value

        try:
            elements = await remote.find_uia_elements(
                UIAQuery(name=element_desc, max_results=1),
            )
        except Exception:
            return "Verification failed: UIA unavailable."

        found = bool(elements)
        if check == "exists":
            if found:
                return f"PASS: Element {element_desc!r} found."
            return f"FAIL: Element {element_desc!r} not found."
        elif check == "not_exists":
            if not found:
                return f"PASS: Element {element_desc!r} not present."
            return f"FAIL: Element {element_desc!r} still present."

    elif condition_type in ("screen_changed", "screen_unchanged"):
        return f"PASS: {condition_type} (observation refreshed)."

    return f"Unknown condition type: {condition_type}"
