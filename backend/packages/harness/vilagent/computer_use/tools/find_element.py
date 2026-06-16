"""find_element — resolve a UI target through UIA, DOM, or vision.

Returns a compact target reference the agent can pass to ``perform_action``.
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool("find_element")
async def find_element_tool(
    element_description: str,
    session_id: str | None = None,
    name: str | None = None,
    control_type: str | None = None,
    automation_id: str | None = None,
) -> str:
    """Find a UI element on the current screen by name, control type, or automation ID.

    Uses UIA first, then browser DOM, then vision.
    Returns a target reference for use with perform_action.

    Args:
        element_description: A natural-language description of the element.
        session_id: Optional desktop session. Omit it for the active/default
            Windows session; VILAGENT will create one when needed.
        name: Optional exact UIA element name.
        control_type: Optional UIA control type filter (e.g. Button, Edit).
        automation_id: Optional UIA automation ID.
    """
    from vilagent.computer_use.models import ActionOwner, TargetQuery
    from vilagent.computer_use.tools._context import ensure_desktop_session, get_action_owner, get_host_control

    remote = get_host_control()
    owner = ActionOwner.model_validate(get_action_owner())
    try:
        resolved_session_id = await ensure_desktop_session(session_id)
    except Exception as exc:
        return f"Target not found: session_unavailable ({getattr(exc, 'code', exc.__class__.__name__)})"

    # Build a target query from the provided filters.
    selector_hints = {}
    if name:
        selector_hints["name"] = name
    if control_type:
        selector_hints["control_type"] = control_type
    if automation_id:
        selector_hints["automation_id"] = automation_id

    try:
        result = await remote.resolve_target(
            resolved_session_id,
            TargetQuery(description=element_description, selector_hints=selector_hints),
            owner=owner,
        )
    except Exception as exc:
        return f"Target not found: {getattr(exc, 'code', exc.__class__.__name__)}"

    if isinstance(result, dict):
        if not result.get("succeeded", False):
            error = result.get("error_code", "target_not_found")
            detail = result.get("error_message", "")
            return f"Target not found: {error}. {detail}".strip()
        data = result.get("data", {})
        target = data.get("target", data)
    else:
        if result.target is None:
            attempts = ", ".join(f"{a.provider_name}:{a.outcome}" for a in result.attempts)
            return f"Target not found. Attempts: {attempts}".strip()
        target = result.target.model_dump(mode="json")

    strategy = target.get("strategy", "unknown")
    selector = target.get("selector", {})
    element_name = selector.get("name", selector.get("text", ""))
    ctrl = selector.get("control_type", "")
    aid = selector.get("automation_id", "")
    bounds = target.get("bounds") or {}
    confidence = target.get("confidence")

    parts = [
        f"Found via {strategy}: {ctrl} {element_name!r}".strip(),
    ]
    if confidence is not None:
        parts.append(f"  confidence: {confidence}")
    if aid:
        parts.append(f"  automation_id: {aid}")
    if selector:
        parts.append(f"  selector: {selector}")
    if bounds:
        parts.append(f"  bounds: ({bounds.get('x')}, {bounds.get('y')}, {bounds.get('width')}x{bounds.get('height')})")

    return "\n".join(parts)
