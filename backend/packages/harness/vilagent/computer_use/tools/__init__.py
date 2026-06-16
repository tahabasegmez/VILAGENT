"""Computer-use agent tools — lightweight LangChain wrappers.

Each tool delegates to the host execution plane through a session-bound
``RemoteWindowsHostControl``.  Tools never hold DesktopLease, never touch
Win32, and never import ``app.*``.
"""

from vilagent.computer_use.tools.observe import observe_desktop_tool
from vilagent.computer_use.tools.find_element import find_element_tool
from vilagent.computer_use.tools.perform_native_action import perform_native_action_tool
from vilagent.computer_use.tools.perform_browser_action import perform_browser_action_tool
from vilagent.computer_use.tools.verify_condition import verify_condition_tool

__all__ = [
    "observe_desktop_tool",
    "find_element_tool",
    "perform_native_action_tool",
    "perform_browser_action_tool",
    "verify_condition_tool",
]
