"""Fail-closed Windows input-desktop safety detection."""

from __future__ import annotations

import asyncio
import ctypes
import os
from collections.abc import Callable

from vilagent.computer_use.models import DesktopSafetySnapshot, DesktopSafetyStatus

_DESKTOP_READOBJECTS = 0x0001
_UOI_NAME = 2
_LOCKED_DESKTOP_NAMES = frozenset({"winlogon", "screen-saver", "screensaver"})


class WindowsDesktopSafetyProvider:
    """Classify the active Windows input desktop without mutating it."""

    name = "windows-desktop-safety"

    def __init__(self, *, probe: Callable[[], DesktopSafetySnapshot] | None = None):
        self._probe = probe or probe_windows_input_desktop

    async def check(self) -> DesktopSafetySnapshot:
        try:
            return await asyncio.to_thread(self._probe)
        except Exception:
            return DesktopSafetySnapshot(
                status=DesktopSafetyStatus.unavailable,
                reason_code="desktop_safety_probe_failed",
            )


def probe_windows_input_desktop() -> DesktopSafetySnapshot:
    """Read the input desktop name and conservatively classify its safety."""
    if os.name != "nt":
        return DesktopSafetySnapshot(
            status=DesktopSafetyStatus.unavailable,
            reason_code="unsupported_platform",
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.GetUserObjectInformationW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    user32.GetUserObjectInformationW.restype = ctypes.c_bool
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    user32.CloseDesktop.restype = ctypes.c_bool

    desktop = user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not desktop:
        return DesktopSafetySnapshot(
            status=DesktopSafetyStatus.unavailable,
            reason_code="input_desktop_unavailable",
        )

    try:
        required_bytes = ctypes.c_uint32()
        user32.GetUserObjectInformationW(desktop, _UOI_NAME, None, 0, ctypes.byref(required_bytes))
        if required_bytes.value == 0:
            return DesktopSafetySnapshot(
                status=DesktopSafetyStatus.unknown,
                reason_code="input_desktop_name_unavailable",
            )
        buffer = ctypes.create_unicode_buffer(required_bytes.value // ctypes.sizeof(ctypes.c_wchar))
        if not user32.GetUserObjectInformationW(desktop, _UOI_NAME, buffer, required_bytes.value, ctypes.byref(required_bytes)):
            return DesktopSafetySnapshot(
                status=DesktopSafetyStatus.unknown,
                reason_code="input_desktop_name_unavailable",
            )
        return classify_input_desktop(buffer.value)
    finally:
        user32.CloseDesktop(desktop)


def classify_input_desktop(name: str) -> DesktopSafetySnapshot:
    """Map a Windows input desktop name to a conservative mutation policy."""
    normalized = name.strip().casefold()
    if normalized == "default":
        return DesktopSafetySnapshot(status=DesktopSafetyStatus.ready)
    if normalized in _LOCKED_DESKTOP_NAMES:
        return DesktopSafetySnapshot(
            status=DesktopSafetyStatus.locked,
            reason_code="locked_input_desktop",
        )
    if not normalized:
        return DesktopSafetySnapshot(
            status=DesktopSafetyStatus.unknown,
            reason_code="input_desktop_name_empty",
        )
    return DesktopSafetySnapshot(
        status=DesktopSafetyStatus.secure_desktop,
        reason_code="non_default_input_desktop",
    )
