"""Windows-specific computer-use providers."""

from vilagent.computer_use.windows.action import WindowsUIAActionProvider, create_stable_uia_control_resolver
from vilagent.computer_use.windows.redaction import RedactionUnavailableError, WindowsUIAPasswordRedactor
from vilagent.computer_use.windows.input import WindowsPhysicalInputProvider, WindowsRoutedActionProvider
from vilagent.computer_use.windows.bootstrap import create_windows_session_service, create_windows_uia_provider
from vilagent.computer_use.windows.child_process import DedicatedWindowsHostProcess, create_dedicated_windows_host_supervisor
from vilagent.computer_use.windows.desktop_safety import WindowsDesktopSafetyProvider
from vilagent.computer_use.windows.host import WindowsAgentHost
from vilagent.computer_use.windows.hotkey import WindowsGlobalHotkeyListener
from vilagent.computer_use.windows.screen import WindowsScreenProvider
from vilagent.computer_use.windows.target import WindowsUIATargetProvider
from vilagent.computer_use.windows.uia import WindowsUIAProvider
from vilagent.computer_use.windows.verification import WindowsUIAVerificationProvider

__all__ = [
    "WindowsScreenProvider",
    "WindowsAgentHost",
    "DedicatedWindowsHostProcess",
    "WindowsDesktopSafetyProvider",
    "WindowsGlobalHotkeyListener",
    "WindowsUIAActionProvider",
    "WindowsUIAPasswordRedactor",
    "WindowsPhysicalInputProvider",
    "WindowsRoutedActionProvider",
    "RedactionUnavailableError",
    "WindowsUIAProvider",
    "WindowsUIATargetProvider",
    "WindowsUIAVerificationProvider",
    "create_windows_session_service",
    "create_dedicated_windows_host_supervisor",
    "create_stable_uia_control_resolver",
    "create_windows_uia_provider",
]
