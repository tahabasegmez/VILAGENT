"""Explicitly gated Windows physical input provider."""

from __future__ import annotations

import asyncio
import ctypes
from collections.abc import Awaitable, Callable
from ctypes import wintypes

from vilagent.computer_use.models import ActionCommand, ActionKind, NativeActionResult, TargetStrategy

_INPUT_MOUSE = 0
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouse_data", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra_info", ctypes.POINTER(ctypes.c_ulong))]


class _InputUnion(ctypes.Union):
    _fields_ = [("mouse", _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


def send_input_click(x: int, y: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.SetCursorPos(x, y):
        raise OSError(ctypes.get_last_error(), "SetCursorPos failed")
    inputs = (_Input * 2)(
        _Input(type=_INPUT_MOUSE, union=_InputUnion(mouse=_MouseInput(flags=_MOUSEEVENTF_LEFTDOWN))),
        _Input(type=_INPUT_MOUSE, union=_InputUnion(mouse=_MouseInput(flags=_MOUSEEVENTF_LEFTUP))),
    )
    if user32.SendInput(2, inputs, ctypes.sizeof(_Input)) != 2:
        raise OSError(ctypes.get_last_error(), "SendInput failed")


class WindowsRoutedActionProvider:
    """Route semantic targets first and coordinate targets only to physical input."""

    name = "windows-routed-action"

    def __init__(self, semantic, physical, browser=None):
        self._semantic = semantic
        self._physical = physical
        self._browser = browser

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        if action.kind == ActionKind.browser_action:
            if action.target is None and action.args.get("action") == "visit_url":
                return await self._semantic.execute(action)
            if self._browser is None:
                return NativeActionResult(succeeded=False, error_code="browser_action_disabled", error_message="Browser actions are disabled.")
            return await self._browser.execute(action)
        if (
            action.target is not None
            and action.target.strategy == TargetStrategy.coordinate
            and action.kind in getattr(self._physical, "supported_actions", {ActionKind.click})
        ):
            return await self._physical.execute(action)
        return await self._semantic.execute(action)


class WindowsPhysicalInputProvider:
    """Perform narrowly supported physical input only when explicitly enabled."""

    name = "windows-physical-input"
    supported_actions = frozenset({ActionKind.click})

    def __init__(
        self,
        *,
        enabled: bool = False,
        click_injector: Callable[[int, int], None] | None = send_input_click,
        injection_guard: Callable[[], Awaitable[bool]] | None = None,
    ):
        self._enabled = enabled
        self._click_injector = click_injector
        self._injection_guard = injection_guard

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        if not self._enabled:
            return NativeActionResult(succeeded=False, error_code="physical_input_disabled", error_message="Physical input is disabled.")
        if action.kind not in self.supported_actions:
            return NativeActionResult(succeeded=False, error_code="physical_input_unsupported", error_message="Physical input action is unsupported.")
        if action.target is None or action.target.strategy != TargetStrategy.coordinate or action.target.bounds is None:
            return NativeActionResult(succeeded=False, error_code="coordinate_target_required", error_message="Physical click requires a bounded coordinate target.")
        if self._click_injector is None:
            return NativeActionResult(succeeded=False, error_code="physical_input_backend_unavailable", error_message="Physical input backend is unavailable.")
        x = action.target.bounds.x + action.target.bounds.width // 2
        y = action.target.bounds.y + action.target.bounds.height // 2
        try:
            await asyncio.sleep(0)
            if self._injection_guard is not None and not await self._injection_guard():
                return NativeActionResult(succeeded=False, error_code="physical_input_guard_blocked", error_message="Physical input safety guard blocked injection.")
            await asyncio.to_thread(self._click_injector, x, y)
        except asyncio.CancelledError:
            raise
        except Exception:
            return NativeActionResult(succeeded=False, error_code="physical_input_failed", error_message="Physical input failed.")
        return NativeActionResult(succeeded=True, details={"mode": "physical_coordinate_click"})
