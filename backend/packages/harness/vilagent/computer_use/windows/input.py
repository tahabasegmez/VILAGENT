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
    """Low-level Win32 SendInput left click (kept as a fallback backend)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.SetCursorPos(x, y):
        raise OSError(ctypes.get_last_error(), "SetCursorPos failed")
    inputs = (_Input * 2)(
        _Input(type=_INPUT_MOUSE, union=_InputUnion(mouse=_MouseInput(flags=_MOUSEEVENTF_LEFTDOWN))),
        _Input(type=_INPUT_MOUSE, union=_InputUnion(mouse=_MouseInput(flags=_MOUSEEVENTF_LEFTUP))),
    )
    if user32.SendInput(2, inputs, ctypes.sizeof(_Input)) != 2:
        raise OSError(ctypes.get_last_error(), "SendInput failed")


def _pyautogui():
    """Lazily import pyautogui with operator-friendly settings.

    FAILSAFE (abort when the cursor hits a screen corner) and the implicit PAUSE
    are disabled so the agent is never blocked mid-task on the operator's machine.
    """
    import pyautogui

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    return pyautogui


def pyautogui_click(x: int, y: int) -> None:
    _pyautogui().click(x=x, y=y)


def pyautogui_double_click(x: int, y: int) -> None:
    pg = _pyautogui()
    # Move first, then two explicit clicks with a short, double-click-speed interval.
    # This is more reliable across apps than pyautogui.doubleClick(), which some
    # Win32 controls fail to register as a real double-click.
    pg.moveTo(x, y)
    pg.click(x=x, y=y, clicks=2, interval=0.08)


def pyautogui_right_click(x: int, y: int) -> None:
    _pyautogui().rightClick(x=x, y=y)


def pyautogui_move(x: int, y: int) -> None:
    _pyautogui().moveTo(x, y)


def pyautogui_scroll(x: int, y: int, amount: int) -> None:
    pg = _pyautogui()
    pg.moveTo(x, y)
    # FARA emits the amount in "pixels" (positive = up); pyautogui.scroll uses wheel
    # "clicks" (~120px each, positive = up). Convert so we don't over/under-scroll.
    if not amount:
        amount = -360  # default: a few notches down
    clicks = int(round(amount / 120)) or (1 if amount > 0 else -1)
    pg.scroll(clicks, x=x, y=y)


def _screen_center() -> tuple[int, int]:
    try:
        width, height = _pyautogui().size()
        return int(width) // 2, int(height) // 2
    except Exception:
        return 960, 540


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
        supported = getattr(self._physical, "supported_actions", {ActionKind.click})
        is_coord = action.target is not None and action.target.strategy == TargetStrategy.coordinate
        # Coordinate pointer actions go to physical input; scroll also goes there even
        # without a coordinate (scroll the focused window at the current cursor).
        if action.kind in supported and (is_coord or action.kind == ActionKind.scroll):
            return await self._physical.execute(action)
        return await self._semantic.execute(action)


class WindowsPhysicalInputProvider:
    """Perform coordinate pointer input (click/double/right/scroll) via pyautogui.

    FARA's native-desktop pointer actions land here. Typing and hotkeys stay on the
    semantic pywinauto path; this provider handles the mouse.
    """

    name = "windows-physical-input"
    supported_actions = frozenset({ActionKind.click, ActionKind.double_click, ActionKind.right_click, ActionKind.scroll})

    def __init__(
        self,
        *,
        enabled: bool = False,
        click_injector: Callable[[int, int], None] | None = pyautogui_click,
        double_click_injector: Callable[[int, int], None] | None = pyautogui_double_click,
        right_click_injector: Callable[[int, int], None] | None = pyautogui_right_click,
        scroll_injector: Callable[[int, int, int], None] | None = pyautogui_scroll,
        injection_guard: Callable[[], Awaitable[bool]] | None = None,
    ):
        self._enabled = enabled
        self._click_injector = click_injector
        self._double_click_injector = double_click_injector
        self._right_click_injector = right_click_injector
        self._scroll_injector = scroll_injector
        self._injection_guard = injection_guard

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        if not self._enabled:
            return NativeActionResult(succeeded=False, error_code="physical_input_disabled", error_message="Physical input is disabled.")
        if action.kind not in self.supported_actions:
            return NativeActionResult(succeeded=False, error_code="physical_input_unsupported", error_message="Physical input action is unsupported.")
        has_target = action.target is not None and action.target.strategy == TargetStrategy.coordinate and action.target.bounds is not None
        if has_target:
            x = action.target.bounds.x + action.target.bounds.width // 2
            y = action.target.bounds.y + action.target.bounds.height // 2
        elif action.kind == ActionKind.scroll:
            # Targetless scroll: scroll at the screen centre (the focused window).
            x, y = _screen_center()
        else:
            return NativeActionResult(succeeded=False, error_code="coordinate_target_required", error_message="Physical pointer input requires a bounded coordinate target.")
        injector, call = self._injector_for(action, x, y)
        if injector is None:
            return NativeActionResult(succeeded=False, error_code="physical_input_backend_unavailable", error_message="Physical input backend is unavailable.")
        try:
            await asyncio.sleep(0)
            if self._injection_guard is not None and not await self._injection_guard():
                return NativeActionResult(succeeded=False, error_code="physical_input_guard_blocked", error_message="Physical input safety guard blocked injection.")
            await asyncio.to_thread(call)
        except asyncio.CancelledError:
            raise
        except Exception:
            return NativeActionResult(succeeded=False, error_code="physical_input_failed", error_message="Physical input failed.")
        return NativeActionResult(succeeded=True, details={"mode": f"physical_{action.kind.value}"})

    def _injector_for(self, action: ActionCommand, x: int, y: int):
        if action.kind == ActionKind.click:
            inj = self._click_injector
            return inj, (lambda: inj(x, y)) if inj else None
        if action.kind == ActionKind.double_click:
            inj = self._double_click_injector
            return inj, (lambda: inj(x, y)) if inj else None
        if action.kind == ActionKind.right_click:
            inj = self._right_click_injector
            return inj, (lambda: inj(x, y)) if inj else None
        if action.kind == ActionKind.scroll:
            inj = self._scroll_injector
            try:
                amount = int(action.args.get("amount") or action.args.get("pixels") or 0)
            except (TypeError, ValueError):
                amount = 0
            return inj, (lambda: inj(x, y, amount)) if inj else None
        return None, None
