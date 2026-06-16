"""observe_desktop — capture a screen observation and return a compact summary.

The tool calls the host IPC to take an observation, but NEVER puts screenshot
bytes into the LLM context.  Only metadata (foreground window, observation_id,
timestamp) and a UIA element summary are returned.
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool("observe_desktop")
async def observe_desktop_tool(session_id: str | None = None) -> str:
    """Capture the current desktop screen state and return a compact summary.

    Returns the observation ID, foreground window info, and a short UIA
    element listing.  Screenshot bytes are stored server-side and never
    returned here.

    Args:
        session_id: Optional desktop session. Omit it for the active/default
            Windows session; VILAGENT will create one when needed.
    """
    from vilagent.computer_use.tools._context import ensure_desktop_session, get_host_control

    remote = get_host_control()
    try:
        resolved_session_id = await ensure_desktop_session(session_id)
        observation = await remote.observe_session(resolved_session_id)
    except Exception as exc:
        return f"Observation failed: {getattr(exc, 'code', exc.__class__.__name__)}"

    if isinstance(observation, dict):
        if not observation.get("succeeded", False):
            return f"Observation failed: {observation.get('error_code', 'observation_failed')}"
        data = observation.get("data", {})
    else:
        data = observation.model_dump(mode="json")

    obs_id = data.get("observation_id", "unknown")
    ts = data.get("created_at", data.get("captured_at", ""))
    fg = data.get("active_window") or data.get("foreground_window") or {}
    fg_title = fg.get("title", "(unknown)")
    fg_process = fg.get("process_name", "")
    uia_count = data.get("uia_element_count", 0)

    parts = [
        f"Session {data.get('session_id', resolved_session_id)}",
        f"Observation {obs_id} at {ts}",
        f"Foreground: [{fg_process}] {fg_title}",
    ]
    if uia_count:
        parts.append(f"UIA elements visible: {uia_count}")

    # Include a compact UIA element listing if available (max 20 lines).
    elements = data.get("uia_elements", [])
    if elements:
        lines = []
        for el in elements[:20]:
            ctrl = el.get("control_type", "")
            name = el.get("name", "")
            aid = el.get("automation_id", "")
            lines.append(f"  {ctrl}: {name!r} (id={aid})")
        parts.append("Key elements:\n" + "\n".join(lines))

    return "\n".join(parts)
