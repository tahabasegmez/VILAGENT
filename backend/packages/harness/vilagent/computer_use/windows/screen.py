"""Read-only Windows screen observation provider."""

from __future__ import annotations

import asyncio
import ctypes
import io
import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from typing import Any

from vilagent.computer_use.models import MonitorRef, Observation, Rect, Size, WindowRef
from vilagent.computer_use.observation_store import InMemoryObservationStore


class ScreenCaptureError(RuntimeError):
    """Raised when the current Windows session cannot provide a screenshot."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _default_grabber() -> Any:
    if os.name == "nt":
        return _grab_primary_screen_win32()

    from PIL import ImageGrab
    try:
        return ImageGrab.grab(all_screens=False)
    except Exception as exc:
        raise ScreenCaptureError("screen_capture_unavailable") from exc


def _call_in_fresh_thread(callback: Callable[[], Any]) -> Any:
    """Run thread-affine Windows capture code on a disposable thread."""
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="vilagent-screen-capture") as executor:
        return executor.submit(callback).result()


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("width", wintypes.LONG),
        ("height", wintypes.LONG),
        ("planes", wintypes.WORD),
        ("bit_count", wintypes.WORD),
        ("compression", wintypes.DWORD),
        ("size_image", wintypes.DWORD),
        ("x_pixels_per_meter", wintypes.LONG),
        ("y_pixels_per_meter", wintypes.LONG),
        ("colors_used", wintypes.DWORD),
        ("colors_important", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapInfoHeader), ("colors", wintypes.DWORD * 3)]


def _grab_primary_screen_win32() -> Any:
    """Capture the interactive primary display through Win32 GDI."""
    from PIL import Image

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    handle = ctypes.c_void_p
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = handle
    user32.ReleaseDC.argtypes = [wintypes.HWND, handle]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = handle
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = handle
    user32.SetThreadDesktop.argtypes = [handle]
    user32.SetThreadDesktop.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = [handle]
    user32.CloseDesktop.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    gdi32.CreateCompatibleDC.argtypes = [handle]
    gdi32.CreateCompatibleDC.restype = handle
    gdi32.CreateCompatibleBitmap.argtypes = [handle, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = handle
    gdi32.SelectObject.argtypes = [handle, handle]
    gdi32.SelectObject.restype = handle
    gdi32.BitBlt.argtypes = [handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [handle, handle, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(_BitmapInfo), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [handle]
    gdi32.DeleteDC.restype = wintypes.BOOL
    thread_id = kernel32.GetCurrentThreadId()
    previous_desktop = user32.GetThreadDesktop(thread_id)
    input_desktop = user32.OpenInputDesktop(0, False, 0x0001)
    if not input_desktop or not user32.SetThreadDesktop(input_desktop):
        if input_desktop:
            user32.CloseDesktop(input_desktop)
        raise ScreenCaptureError("screen_capture_input_desktop_unavailable")

    screen_dc = memory_dc = bitmap = previous = None
    try:
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        if width <= 0 or height <= 0:
            raise ScreenCaptureError("screen_capture_invalid_dimensions")
        screen_dc = user32.GetDC(None)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc) if screen_dc else None
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height) if memory_dc else None
        previous = gdi32.SelectObject(memory_dc, bitmap) if bitmap else None
        if not screen_dc or not memory_dc or not bitmap or not previous:
            raise ScreenCaptureError("screen_capture_dc_unavailable")
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, 0, 0, 0x00CC0020 | 0x40000000):
            raise ScreenCaptureError("screen_capture_bitblt_failed")

        info = _BitmapInfo()
        info.header = _BitmapInfoHeader(
            size=ctypes.sizeof(_BitmapInfoHeader),
            width=width,
            height=-height,
            planes=1,
            bit_count=32,
            compression=0,
            size_image=width * height * 4,
        )
        buffer = ctypes.create_string_buffer(info.header.size_image)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0):
            raise ScreenCaptureError("screen_capture_dib_failed")
        return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).copy()
    except ScreenCaptureError:
        raise
    except Exception as exc:
        raise ScreenCaptureError("screen_capture_unavailable") from exc
    finally:
        if previous and memory_dc:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        if previous_desktop:
            user32.SetThreadDesktop(previous_desktop)
        user32.CloseDesktop(input_desktop)


def _default_active_window_reader() -> WindowRef | None:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        title_length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        rectangle = wintypes.RECT()
        bounds = None
        if user32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
            width = rectangle.right - rectangle.left
            height = rectangle.bottom - rectangle.top
            if width > 0 and height > 0:
                bounds = Rect(x=rectangle.left, y=rectangle.top, width=width, height=height)
        return WindowRef(
            window_id=f"hwnd:{int(hwnd)}",
            title=title_buffer.value,
            process_id=int(process_id.value),
            bounds=bounds,
        )
    except Exception:
        return None


class WindowsScreenProvider:
    """Capture the primary Windows display without granting input privileges."""

    name = "windows-screen"

    def __init__(
        self,
        store: InMemoryObservationStore,
        *,
        grabber: Callable[[], Any] | None = None,
        redact: Callable[[Any], Any] | None = None,
        active_window_reader: Callable[[], WindowRef | None] | None = None,
    ):
        self._store = store
        self._grabber = grabber or _default_grabber
        self._redact = redact
        self._active_window_reader = active_window_reader or _default_active_window_reader

    async def observe(self, session_id: str, *, previous: Observation | None = None) -> Observation:
        # UI Automation initializes COM and can make a reused worker thread
        # ineligible for SetThreadDesktop.
        image = await asyncio.to_thread(_call_in_fresh_thread, self._grabber)
        redaction_applied = self._redact is not None
        if self._redact is not None:
            image = await asyncio.to_thread(_call_in_fresh_thread, lambda: self._redact(image))

        payload = await asyncio.to_thread(self._encode_png, image)
        screenshot_ref = await self._store.put_blob(payload, media_type="image/png")
        width, height = image.size
        diff = await self._calculate_diff(previous, image)
        active_window = await asyncio.to_thread(self._active_window_reader)
        observation = Observation(
            observation_id=uuid.uuid4().hex,
            previous_observation_id=previous.observation_id if previous else None,
            session_id=session_id,
            active_window=active_window,
            screenshot_ref=screenshot_ref,
            monitor=MonitorRef(monitor_id="primary", primary=True, bounds=Rect(x=0, y=0, width=width, height=height)),
            screen_size=Size(width=width, height=height),
            diff_from_previous=diff,
            redaction_applied=redaction_applied,
        )
        await self._store.save(observation)
        return observation

    async def _calculate_diff(self, previous: Observation | None, image: Any) -> float | None:
        if previous is None or previous.screenshot_ref is None:
            return None
        try:
            previous_payload = await self._store.get_blob(previous.screenshot_ref.blob_id)
        except KeyError:
            return None
        return await asyncio.to_thread(self._image_diff, previous_payload, image)

    @staticmethod
    def _encode_png(image: Any) -> bytes:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    @staticmethod
    def _image_diff(previous_payload: bytes, current_image: Any) -> float:
        from PIL import Image, ImageChops, ImageStat

        with Image.open(io.BytesIO(previous_payload)) as previous_image:
            previous_rgb = previous_image.convert("RGB")
        current_rgb = current_image.convert("RGB")
        if previous_rgb.size != current_rgb.size:
            return 1.0
        difference = ImageChops.difference(previous_rgb, current_rgb)
        mean = ImageStat.Stat(difference).mean
        return min(1.0, sum(mean) / (len(mean) * 255))
