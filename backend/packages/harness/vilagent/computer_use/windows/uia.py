"""Read-only pywinauto UI Automation provider."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vilagent.computer_use.models import Rect, UIAElementRef, UIAQuery, WindowRef


class UIAUnavailableError(RuntimeError):
    """Raised when Windows UI Automation cannot be initialized."""


def _default_desktop_factory() -> Any:
    try:
        from pywinauto import Desktop

        return Desktop(backend="uia")
    except Exception as exc:
        raise UIAUnavailableError("pywinauto UIA backend is unavailable") from exc


def prepare_comtypes_cache(cache_dir: str | Path | None = None) -> Path:
    """Force generated COM wrappers into a writable VILAGENT runtime directory."""
    resolved = Path(cache_dir or Path(tempfile.gettempdir()) / "vilagent" / "comtypes_cache").resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    try:
        import comtypes.gen

        comtypes.gen.__path__ = [str(resolved)]
    except Exception as exc:
        raise UIAUnavailableError("Unable to prepare the writable comtypes cache") from exc
    return resolved


class WindowsUIAProvider:
    """Query Windows accessibility data without mutating the desktop."""

    name = "windows-uia"

    def __init__(
        self,
        *,
        desktop_factory: Callable[[], Any] | None = None,
        comtypes_cache_dir: str | Path | None = None,
        cache_preparer: Callable[[str | Path | None], Path] = prepare_comtypes_cache,
    ):
        self._desktop_factory = desktop_factory or _default_desktop_factory
        self._comtypes_cache_dir = comtypes_cache_dir
        self._cache_preparer = cache_preparer

    @property
    def comtypes_cache_dir(self) -> str | Path | None:
        return self._comtypes_cache_dir

    async def list_windows(self) -> list[WindowRef]:
        return await asyncio.to_thread(self._list_windows_sync)

    async def find(self, query: UIAQuery) -> list[UIAElementRef]:
        return await asyncio.to_thread(self._find_sync, query)

    def _list_windows_sync(self) -> list[WindowRef]:
        desktop = self._create_desktop()
        return [self._window_ref(control) for control in desktop.windows()]

    def _find_sync(self, query: UIAQuery) -> list[UIAElementRef]:
        desktop = self._create_desktop()
        results: list[UIAElementRef] = []
        for window in desktop.windows():
            if query.window_title and query.window_title.casefold() not in self._text(window).casefold():
                continue
            candidates = [window, *window.descendants()]
            for control in candidates:
                if self._matches(control, query):
                    results.append(self._element_ref(control))
                    if len(results) >= query.max_results:
                        return results
        return results

    def _create_desktop(self) -> Any:
        self._cache_preparer(self._comtypes_cache_dir)
        return self._desktop_factory()

    def _matches(self, control: Any, query: UIAQuery) -> bool:
        info = getattr(control, "element_info", None)
        if query.name and query.name.casefold() not in self._text(control).casefold():
            return False
        if query.automation_id and query.automation_id != str(getattr(info, "automation_id", "") or ""):
            return False
        if query.control_type and query.control_type.casefold() != str(getattr(info, "control_type", "") or "").casefold():
            return False
        if query.process_id is not None and query.process_id != self._process_id(control):
            return False
        return True

    def _window_ref(self, control: Any) -> WindowRef:
        return WindowRef(
            window_id=self._element_id(control),
            title=self._text(control),
            process_name=None,
            process_id=self._process_id(control),
            bounds=self._bounds(control),
        )

    def _element_ref(self, control: Any) -> UIAElementRef:
        info = getattr(control, "element_info", None)
        return UIAElementRef(
            element_id=self._element_id(control),
            name=self._text(control),
            automation_id=str(getattr(info, "automation_id", "") or ""),
            control_type=str(getattr(info, "control_type", "") or ""),
            process_id=self._process_id(control),
            bounds=self._bounds(control),
            enabled=self._optional_bool(control, "is_enabled"),
            visible=self._optional_bool(control, "is_visible"),
        )

    @staticmethod
    def _text(control: Any) -> str:
        try:
            return str(control.window_text() or "")
        except Exception:
            return ""

    @staticmethod
    def _process_id(control: Any) -> int | None:
        try:
            return int(control.process_id())
        except Exception:
            return None

    @staticmethod
    def _bounds(control: Any) -> Rect | None:
        try:
            rectangle = control.rectangle()
            width = int(rectangle.right - rectangle.left)
            height = int(rectangle.bottom - rectangle.top)
            if width < 1 or height < 1:
                return None
            return Rect(x=int(rectangle.left), y=int(rectangle.top), width=width, height=height)
        except Exception:
            return None

    @staticmethod
    def _optional_bool(control: Any, method_name: str) -> bool | None:
        try:
            return bool(getattr(control, method_name)())
        except Exception:
            return None

    def _element_id(self, control: Any) -> str:
        info = getattr(control, "element_info", None)
        runtime_id = getattr(info, "runtime_id", None)
        if runtime_id:
            return ".".join(str(part) for part in runtime_id)
        raw = "|".join(
            (
                str(getattr(info, "automation_id", "") or ""),
                str(getattr(info, "control_type", "") or ""),
                self._text(control),
                str(self._process_id(control) or ""),
                str(self._bounds(control) or ""),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
