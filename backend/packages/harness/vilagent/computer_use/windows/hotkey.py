"""Windows global emergency-stop hotkey listener."""

from __future__ import annotations

import asyncio
import ctypes
import threading
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from dataclasses import dataclass

_HOTKEY_ID = 0x5649
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


@dataclass(frozen=True)
class HotkeySpec:
    modifiers: int
    virtual_key: int


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("w_param", wintypes.WPARAM),
        ("l_param", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("point", wintypes.POINT),
    ]


class WindowsGlobalHotkeyListener:
    """Own a background Windows message loop for one emergency-stop hotkey."""

    def __init__(
        self,
        hotkey: str,
        *,
        on_trigger: Callable[[], Awaitable[None]],
        runner: Callable[[HotkeySpec, Callable[[], None], threading.Event, threading.Event], None] | None = None,
    ):
        self._spec = parse_hotkey(hotkey)
        self._on_trigger = on_trigger
        self._runner = runner or self._run_windows_loop
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._error_code: str | None = None
        self._registered = False

    @property
    def running(self) -> bool:
        return self._registered and self._thread is not None and self._thread.is_alive()

    async def start(self) -> None:
        if self.running:
            return
        self._loop = asyncio.get_running_loop()
        self._ready.clear()
        self._stop.clear()
        self._error_code = None
        self._thread = threading.Thread(
            target=self._run_guarded,
            args=(self._spec, self._schedule_trigger, self._ready, self._stop),
            name="vilagent-emergency-hotkey",
            daemon=True,
        )
        self._thread.start()
        ready = await asyncio.to_thread(self._ready.wait, 5)
        if not ready or self._error_code is not None or not self._thread.is_alive():
            await self.stop()
            raise RuntimeError("Global emergency-stop hotkey registration failed")
        self._registered = True

    async def stop(self) -> None:
        self._stop.set()
        if self._thread_id is not None:
            try:
                ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 5)
        self._thread = None
        self._thread_id = None
        self._registered = False

    def _schedule_trigger(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._on_trigger(), self._loop)

    def _run_guarded(
        self,
        spec: HotkeySpec,
        trigger: Callable[[], None],
        ready: threading.Event,
        stop: threading.Event,
    ) -> None:
        try:
            self._runner(spec, trigger, ready, stop)
        except Exception:
            self._error_code = "hotkey_listener_failed"
            ready.set()
        finally:
            self._registered = False

    def _run_windows_loop(
        self,
        spec: HotkeySpec,
        trigger: Callable[[], None],
        ready: threading.Event,
        stop: threading.Event,
    ) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        registered = bool(user32.RegisterHotKey(None, _HOTKEY_ID, spec.modifiers | _MOD_NOREPEAT, spec.virtual_key))
        if not registered:
            self._error_code = "hotkey_registration_failed"
            ready.set()
            return
        ready.set()
        message = _Message()
        try:
            while not stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    return
                if message.message == _WM_HOTKEY and message.w_param == _HOTKEY_ID:
                    trigger()
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)


def parse_hotkey(value: str) -> HotkeySpec:
    """Parse a small explicit Windows global-hotkey configuration."""
    parts = [part.strip().casefold() for part in value.split("+") if part.strip()]
    if len(parts) < 2 or len(parts) != len(set(parts)):
        raise ValueError("Emergency-stop hotkey requires unique modifiers and one key")

    modifier_map = {
        "alt": _MOD_ALT,
        "ctrl": _MOD_CONTROL,
        "control": _MOD_CONTROL,
        "shift": _MOD_SHIFT,
        "win": _MOD_WIN,
        "windows": _MOD_WIN,
    }
    modifiers = 0
    key_parts: list[str] = []
    for part in parts:
        modifier = modifier_map.get(part)
        if modifier is None:
            key_parts.append(part)
        else:
            if modifiers & modifier:
                raise ValueError("Emergency-stop hotkey modifiers must be unique")
            modifiers |= modifier
    if modifiers == 0 or len(key_parts) != 1:
        raise ValueError("Emergency-stop hotkey requires modifiers and exactly one key")
    return HotkeySpec(modifiers=modifiers, virtual_key=_virtual_key(key_parts[0]))


def _virtual_key(key: str) -> int:
    if key in {"escape", "esc"}:
        return 0x1B
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    raise ValueError("Emergency-stop hotkey key must be escape, F1-F24, or one ASCII letter/digit")
