"""Built-in tools used by the VILAGENT computer-use agent.

The legacy VILAGENT built-ins (task/subagent, present_file, setup_agent,
update_agent, view_image) were removed during the computer-use purge. Only the
clarification tool remains in the default surface.
"""

from .clarification_tool import ask_clarification_tool

__all__ = [
    "ask_clarification_tool",
]
