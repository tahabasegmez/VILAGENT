"""Windows UI Automation (pywinauto): find an element by description and invoke it.

This is the deterministic "hybrid" path: when a plan step names a control, it is
resolved from the accessibility tree instead of guessed from pixels. All functions
are blocking.
"""

from __future__ import annotations

import ctypes
import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vilagent.actions import Rect, Target, TargetStrategy

_QUERY_KEYS = frozenset({"automation_id", "name", "control_type", "process_id", "window_title"})
# Keys that identify an element without depending on its (localized, changing) label.
_STABLE_KEYS = frozenset({"element_id", "automation_id", "control_type", "process_id"})


class UIAUnavailableError(RuntimeError):
    """Raised when Windows UI Automation cannot be initialized."""


def prepare_comtypes_cache(cache_dir: str | Path | None = None) -> Path:
    """Point comtypes' generated wrappers at a writable directory."""
    resolved = Path(cache_dir or Path(tempfile.gettempdir()) / "vilagent" / "comtypes_cache").resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    try:
        import comtypes.gen

        comtypes.gen.__path__ = [str(resolved)]
    except Exception as exc:
        raise UIAUnavailableError("Unable to prepare the writable comtypes cache") from exc
    return resolved


def _desktop(cache_dir: str | Path | None) -> Any:
    prepare_comtypes_cache(cache_dir)
    try:
        from pywinauto import Desktop

        return Desktop(backend="uia")
    except Exception as exc:
        raise UIAUnavailableError("pywinauto UIA backend is unavailable") from exc


@dataclass(frozen=True)
class Element:
    element_id: str
    name: str
    automation_id: str
    control_type: str
    process_id: int | None
    bounds: Rect | None
    enabled: bool | None
    visible: bool | None


def _text(control: Any) -> str:
    try:
        return str(control.window_text() or "")
    except Exception:
        return ""


def _process_id(control: Any) -> int | None:
    try:
        return int(control.process_id())
    except Exception:
        return None


def _bounds(control: Any) -> Rect | None:
    try:
        rect = control.rectangle()
        width, height = int(rect.right - rect.left), int(rect.bottom - rect.top)
        return Rect(x=int(rect.left), y=int(rect.top), width=width, height=height) if width > 0 and height > 0 else None
    except Exception:
        return None


def _flag(control: Any, method: str) -> bool | None:
    try:
        return bool(getattr(control, method)())
    except Exception:
        return None


def _element_id(control: Any) -> str:
    info = getattr(control, "element_info", None)
    runtime_id = getattr(info, "runtime_id", None)
    if runtime_id:
        return ".".join(str(part) for part in runtime_id)
    raw = "|".join((str(getattr(info, "automation_id", "") or ""), str(getattr(info, "control_type", "") or ""), _text(control), str(_process_id(control) or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _element(control: Any) -> Element:
    info = getattr(control, "element_info", None)
    return Element(
        element_id=_element_id(control),
        name=_text(control),
        automation_id=str(getattr(info, "automation_id", "") or ""),
        control_type=str(getattr(info, "control_type", "") or ""),
        process_id=_process_id(control),
        bounds=_bounds(control),
        enabled=_flag(control, "is_enabled"),
        visible=_flag(control, "is_visible"),
    )


def _matches_query(control: Any, query: dict[str, Any]) -> bool:
    info = getattr(control, "element_info", None)
    if (name := query.get("name")) and str(name).casefold() not in _text(control).casefold():
        return False
    if (automation_id := query.get("automation_id")) and automation_id != str(getattr(info, "automation_id", "") or ""):
        return False
    if (control_type := query.get("control_type")) and str(control_type).casefold() != str(getattr(info, "control_type", "") or "").casefold():
        return False
    if query.get("process_id") is not None and query["process_id"] != _process_id(control):
        return False
    return True


def find(query: dict[str, Any], *, max_results: int = 20, desktop: Any = None, cache_dir: str | Path | None = None) -> list[Element]:
    """Elements matching ``query`` (name substring, automation_id, control_type, process_id, window_title)."""
    query = {key: value for key, value in query.items() if key in _QUERY_KEYS and value not in (None, "")}
    if not query:
        return []
    desktop = desktop or _desktop(cache_dir)
    results: list[Element] = []
    for window in _windows_foreground_first(desktop):
        if (title := query.get("window_title")) and str(title).casefold() not in _text(window).casefold():
            continue
        for control in (window, *window.descendants()):
            if _matches_query(control, query):
                results.append(_element(control))
                if len(results) >= max_results:
                    return results
    return results


def _windows_foreground_first(desktop: Any) -> list[Any]:
    """The window the operator is looking at first; a step usually means a control inside it."""
    windows = list(desktop.windows())
    try:
        handle = ctypes.windll.user32.GetForegroundWindow()
    except Exception:  # pragma: no cover - not Windows
        return windows
    return sorted(windows, key=lambda window: getattr(getattr(window, "element_info", None), "handle", None) != handle)


def resolve_target(description: str, hints: dict[str, Any], *, desktop: Any = None, cache_dir: str | Path | None = None) -> Target | None:
    """The element a step refers to, or None when nothing fits or two candidates fit equally.

    A name like "Documents" matches in several places at once, so the candidates are ranked:
    the whole name beats a prefix beats a fragment, the control type the planner asked for
    beats another, and the smallest match beats its container. None means the caller should
    look at the screen instead — it is not a failure.
    """
    query = {key: value for key, value in hints.items() if key in _QUERY_KEYS}
    if not query:
        query = {"name": description}
    usable = [item for item in find(query, desktop=desktop, cache_dir=cache_dir) if item.visible is not False and item.enabled is not False]
    element = _best(usable, str(query.get("name") or description), str(hints.get("control_type") or ""))
    if element is None:
        return None
    selector: dict[str, Any] = {"element_id": element.element_id}
    if element.process_id is not None:
        selector["process_id"] = element.process_id
    if element.automation_id:
        selector["automation_id"] = element.automation_id
    if element.control_type:
        selector["control_type"] = element.control_type
    return Target(strategy=TargetStrategy.uia, selector=selector, bounds=element.bounds)


def _best(elements: list[Element], name: str, control_type: str) -> Element | None:
    """The one clearly best candidate, or None when two fit equally well."""
    if not elements:
        return None
    ranked = sorted(elements, key=lambda element: _score(element, name, control_type), reverse=True)
    if len(ranked) > 1 and _score(ranked[0], name, control_type) == _score(ranked[1], name, control_type):
        return None  # nothing to choose between them; the screen can decide
    return ranked[0]


def _score(element: Element, name: str, control_type: str) -> tuple[int, int, int]:
    """How well an element answers to a name: exact, then prefix, then fragment; smaller wins ties."""
    text, wanted = element.name.casefold().strip(), name.casefold().strip()
    fit = 3 if text == wanted else 2 if text.startswith(wanted) else 1 if wanted in text else 0
    typed = 1 if control_type and element.control_type.casefold() == control_type.casefold() else 0
    area = element.bounds.width * element.bounds.height if element.bounds else 0
    return (fit, typed, -area)


def _matches_selector(control: Any, selector: dict[str, Any]) -> bool:
    element = _element(control)
    values = {
        "element_id": element.element_id,
        "automation_id": element.automation_id,
        "control_type": element.control_type,
        "process_id": element.process_id,
    }
    return all(values[key] == value for key, value in selector.items())


def invoke(selector: dict[str, Any], *, focus_only: bool = False, desktop: Any = None, cache_dir: str | Path | None = None) -> None:
    """Invoke (or just focus) the one element matching a stable selector."""
    if not selector or not set(selector) <= _STABLE_KEYS:
        raise ValueError("A stable UIA selector is required")
    desktop = desktop or _desktop(cache_dir)
    matches = []
    for window in desktop.windows():
        for control in (window, *window.descendants()):
            if _matches_selector(control, selector):
                matches.append(control)
                if len(matches) > 1:
                    raise RuntimeError("uia_selector_ambiguous")
    if not matches:
        raise LookupError("uia_element_not_found")
    if focus_only:
        matches[0].set_focus()
    else:
        matches[0].invoke()
