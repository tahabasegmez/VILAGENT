"""Restricted semantic Windows UI Automation action provider."""

from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vilagent.computer_use.models import ActionCommand, ActionKind, NativeActionResult, TargetStrategy
from vilagent.computer_use.windows.uia import UIAUnavailableError, prepare_comtypes_cache


class WindowsUIAActionProvider:
    """Execute only semantic UIA invoke/focus operations, never raw input."""

    name = "windows-uia-action"
    supported_actions = frozenset({ActionKind.click, ActionKind.focus_window, ActionKind.launch_app, ActionKind.hotkey, ActionKind.type_text, ActionKind.browser_action, ActionKind.integration_action})

    def __init__(
        self,
        *,
        control_resolver: Callable[[dict[str, Any]], Any],
        app_search_launcher: Callable[[str], None] | None = None,
        url_navigator: Callable[[str], None] | None = None,
    ):
        self._control_resolver = control_resolver
        self._app_search_launcher = app_search_launcher or _launch_from_windows_start_search
        self._url_navigator = url_navigator or _navigate_focused_browser

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        if action.kind not in self.supported_actions:
            return NativeActionResult(succeeded=False, error_code="uia_action_unsupported", error_message=f"UIA action '{action.kind.value}' is unsupported.")
        if action.kind == ActionKind.launch_app:
            return await self._launch_app(action)
        if action.kind == ActionKind.type_text:
            return await self._type_text(action)
        if action.kind == ActionKind.hotkey:
            return await self._hotkey(action)
        if action.kind == ActionKind.browser_action:
            return await self._browser_action(action)
        if action.kind == ActionKind.integration_action:
            return NativeActionResult(succeeded=True, details={"mode": "integration_action"})
            
        if action.target is None or action.target.strategy != TargetStrategy.uia:
            return NativeActionResult(succeeded=False, error_code="uia_target_required", error_message="A semantic UIA target is required.")
        if not _is_stable_selector(action.target.selector):
            return NativeActionResult(succeeded=False, error_code="uia_stable_selector_required", error_message="A stable semantic selector is required.")
        return await self._semantic_action(action)

    async def _semantic_action(self, action: ActionCommand) -> NativeActionResult:
        control = self._control_resolver(action.target.selector)
        if not control:
            return NativeActionResult(succeeded=False, error_code="uia_element_not_found", error_message="Target element not found")
        
        if action.kind == ActionKind.click:
            try:
                control.invoke()
                return NativeActionResult(succeeded=True, details={"mode": "semantic_uia"})
            except Exception as e:
                return NativeActionResult(succeeded=False, error_code="uia_invoke_failed", error_message=str(e))
        
        if action.kind == ActionKind.focus_window:
            try:
                control.set_focus()
                return NativeActionResult(succeeded=True)
            except Exception as e:
                return NativeActionResult(succeeded=False, error_code="uia_focus_failed", error_message=str(e))
                
        return NativeActionResult(succeeded=False, error_code="uia_action_unsupported", error_message=f"UIA action '{action.kind.value}' is unsupported.")

    async def _launch_app(self, action: ActionCommand) -> NativeActionResult:
        app_name = str(action.args.get("app_name") or action.args.get("command") or "").strip()
        if not app_name:
            return NativeActionResult(succeeded=False, error_code="app_name_required", error_message="Launch action requires app_name.")
        # 1. Direct executable on PATH (calc, notepad, cmd, explorer, mspaint).
        try:
            subprocess.Popen([app_name], shell=False)
            return NativeActionResult(succeeded=True, details={"mode": "direct_app_launch", "app": app_name})
        except (FileNotFoundError, OSError):
            pass
        except Exception as direct_error:
            return NativeActionResult(succeeded=False, error_code="launch_app_failed", error_message=str(direct_error), details={"requested_app": app_name})
        # 2. Shell `start` resolves App Paths (msedge, chrome, winword, excel) the way
        #    the Run dialog does, which plain CreateProcess/Popen does not.
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["cmd", "/c", "start", "", app_name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return NativeActionResult(succeeded=True, details={"mode": "shell_start", "app": app_name})
        except Exception:
            pass
        # 3. Windows Start menu search fallback (best effort).
        try:
            await asyncio.to_thread(self._app_search_launcher, app_name)
            return NativeActionResult(succeeded=True, details={"mode": "windows_start_search", "app": app_name})
        except Exception as search_error:
            return NativeActionResult(
                succeeded=False,
                error_code="launch_app_failed",
                error_message=f"Could not launch '{app_name}': {search_error}",
                details={"requested_app": app_name},
            )

    async def _type_text(self, action: ActionCommand) -> NativeActionResult:
        try:
            from pywinauto import keyboard
            keyboard.send_keys(_escape_send_keys(str(action.args.get("text", ""))), with_spaces=True)
            return NativeActionResult(succeeded=True)
        except Exception as e:
            return NativeActionResult(succeeded=False, error_code="type_text_failed", error_message=str(e))

    async def _hotkey(self, action: ActionCommand) -> NativeActionResult:
        try:
            from pywinauto import keyboard
            keyboard.send_keys(_pywinauto_hotkey(action.args.get("keys")))
            return NativeActionResult(succeeded=True)
        except Exception as e:
            return NativeActionResult(succeeded=False, error_code="hotkey_failed", error_message=str(e))

    async def _browser_action(self, action: ActionCommand) -> NativeActionResult:
        operation = str(action.args.get("action") or "")
        url = str(action.args.get("url") or "").strip()
        if operation != "visit_url" or not url:
            return NativeActionResult(succeeded=False, error_code="browser_action_unsupported", error_message="Targetless browser action requires visit_url and url.")
        try:
            await asyncio.to_thread(self._url_navigator, url)
            return NativeActionResult(succeeded=True, details={"mode": "focused_browser_navigation", "url": url})
        except Exception as exc:
            return NativeActionResult(succeeded=False, error_code="browser_navigation_failed", error_message=str(exc) or exc.__class__.__name__)


def create_stable_uia_control_resolver(cache_dir: str | Path | None = None) -> Callable[[dict[str, Any]], Any]:
    def resolve(selector: dict[str, Any]) -> Any:
        prepare_comtypes_cache(cache_dir)
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        matches = []
        for window in desktop.windows():
            for control in (window, *window.descendants()):
                if _matches_stable_selector(control, selector):
                    matches.append(control)
                    if len(matches) > 1:
                        raise RuntimeError("Stable UIA selector resolved to multiple controls")
        return matches[0] if matches else None

    return resolve


def _launch_from_windows_start_search(app_name: str) -> None:
    from pywinauto import keyboard

    search_text = " ".join(app_name.split())
    if not search_text:
        raise ValueError("Windows Start search requires an app name")
    keyboard.send_keys("{VK_LWIN}")
    time.sleep(0.4)
    keyboard.send_keys(search_text, with_spaces=True, vk_packet=True)
    time.sleep(0.8)
    keyboard.send_keys("{ENTER}")


def _navigate_focused_browser(url: str) -> None:
    from pywinauto import keyboard

    keyboard.send_keys("^l")
    keyboard.send_keys(url, with_spaces=True, vk_packet=True)
    keyboard.send_keys("{ENTER}")


_MODIFIER_TOKENS = {
    "ctrl": "^",
    "control": "^",
    "ctl": "^",
    "alt": "%",
    "option": "%",
    "opt": "%",
    "shift": "+",
}
# Tokens that mean "the Windows / Super / Meta key" used as a modifier.
_WIN_TOKENS = {"win", "windows", "super", "meta", "cmd", "command", "lwin", "rwin", "winleft"}


def _flatten_hotkey_tokens(raw_keys: Any) -> list[str]:
    """Normalize the many shapes a model can emit for a hotkey into atomic key tokens.

    Accepts a single combined string (``"ctrl+c"``, ``"ctrl c"``, ``"ENTER"``),
    a list of tokens (``["ctrl", "c"]``), or a list containing combined strings
    (``["ctrl+c"]``, ``["ctrl c"]``). FARA emits ``keys`` as an array and
    UI-TARS emits space-separated ``key='ctrl c'`` style combos, so both must
    flatten to the same atomic token list.
    """
    if isinstance(raw_keys, str):
        items: list[str] = [raw_keys]
    elif isinstance(raw_keys, (list, tuple)):
        items = [str(part) for part in raw_keys]
    else:
        items = []

    tokens: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if text == "+":
            tokens.append("+")
            continue
        trailing_plus = False
        if text.endswith("++"):
            # e.g. "ctrl++" means Ctrl plus the literal '+' key.
            text = text[:-2]
            trailing_plus = True
        for part in re.split(r"[+\s]+", text):
            part = part.strip()
            if part:
                tokens.append(part)
        if trailing_plus:
            tokens.append("+")
    return tokens


def _escape_send_keys(text: str) -> str:
    """Escape pywinauto send_keys control characters so text is typed literally.

    pywinauto treats ``^ + % ~ ( ) { } [ ]`` as syntax (modifiers, grouping), so
    ``12+23=`` would otherwise read ``+`` as Shift and mistype. Wrapping each
    special character in braces types it verbatim.
    """
    out: list[str] = []
    for ch in text:
        if ch == "{":
            out.append("{{}")
        elif ch == "}":
            out.append("{}}")
        elif ch in "^+%~()[]":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def _pywinauto_hotkey(raw_keys: Any) -> str:
    keys = _flatten_hotkey_tokens(raw_keys)
    if not keys:
        raise ValueError("Hotkey action requires keys")

    normalized = [key.casefold().strip("{}") for key in keys]
    modifiers = normalized[:-1]
    prefix = "".join(_MODIFIER_TOKENS[key] for key in modifiers if key in _MODIFIER_TOKENS)

    final = _pywinauto_key_token(keys[-1])

    if any(key in _WIN_TOKENS for key in modifiers):
        return f"{{VK_LWIN down}}{prefix}{final}{{VK_LWIN up}}"
    return f"{prefix}{final}"


def _pywinauto_key_token(raw_key: str) -> str:
    key = raw_key.strip().strip("{}")
    normalized = key.casefold()
    aliases = {
        "return": "ENTER",
        "enter": "ENTER",
        "escape": "ESC",
        "esc": "ESC",
        "backspace": "BACKSPACE",
        "back": "BACKSPACE",
        "delete": "DELETE",
        "del": "DELETE",
        "insert": "INSERT",
        "ins": "INSERT",
        "pageup": "PGUP",
        "pgup": "PGUP",
        "pagedown": "PGDN",
        "pgdn": "PGDN",
        "left": "LEFT",
        "arrowleft": "LEFT",
        "right": "RIGHT",
        "arrowright": "RIGHT",
        "up": "UP",
        "arrowup": "UP",
        "down": "DOWN",
        "arrowdown": "DOWN",
        "home": "HOME",
        "end": "END",
        "tab": "TAB",
        "space": "SPACE",
        "spacebar": "SPACE",
        "capslock": "CAPSLOCK",
        "numlock": "NUMLOCK",
        "scrolllock": "SCROLLLOCK",
        "printscreen": "PRTSC",
        "prtsc": "PRTSC",
        "prtscr": "PRTSC",
        "pause": "PAUSE",
        "break": "BREAK",
        "apps": "APPS",
        "menu": "APPS",
        "contextmenu": "APPS",
    }
    # Windows / Super / Meta key used as a standalone key (not just a modifier).
    if normalized in _WIN_TOKENS:
        return "{VK_LWIN}"
    canonical = aliases.get(normalized)
    if canonical is None and normalized.startswith("f") and normalized[1:].isdigit():
        canonical = normalized.upper()
    if canonical is not None:
        return f"{{{canonical}}}"
    if key in ("+", "^", "%", "~", "(", ")", "{", "}"):
        return f"{{{key}}}"
    if len(key) == 1:
        return normalized
    if normalized.startswith("vk_"):
        return f"{{{key.upper()}}}"
    # Last-resort: a multi-char alphanumeric name we don't recognise is treated
    # as a Windows virtual-key name (e.g. "VOLUME_UP") rather than hard-failing
    # the whole step, which is what produced the recurring hotkey errors.
    if re.fullmatch(r"[A-Za-z0-9_]+", key):
        return f"{{VK_{key.upper()}}}" if not key.upper().startswith("VK_") else f"{{{key.upper()}}}"
    raise ValueError(f"Unsupported hotkey code: {raw_key}")


def _matches_stable_selector(control: Any, selector: dict[str, Any]) -> bool:
    info = getattr(control, "element_info", None)
    values = {
        "automation_id": str(getattr(info, "automation_id", "") or ""),
        "control_type": str(getattr(info, "control_type", "") or ""),
        "element_id": _element_id(control),
    }
    try:
        values["process_id"] = int(control.process_id())
    except Exception:
        values["process_id"] = None
    try:
        values["name"] = str(control.window_text() or "")
    except Exception:
        values["name"] = ""
    supported = {"automation_id", "control_type", "element_id", "process_id", "name"}
    return all(key in supported and values[key] == value for key, value in selector.items())


def _is_stable_selector(selector: dict[str, Any]) -> bool:
    return bool(selector) and set(selector).issubset({"automation_id", "control_type", "element_id", "process_id"})


def _element_id(control: Any) -> str:
    info = getattr(control, "element_info", None)
    runtime_id = getattr(info, "runtime_id", None)
    if runtime_id:
        return ".".join(str(part) for part in runtime_id)
    raw = "|".join(
        (
            str(getattr(info, "automation_id", "") or ""),
            str(getattr(info, "control_type", "") or ""),
            str(getattr(control, "window_text", lambda: "")() or ""),
            str(getattr(control, "process_id", lambda: "")() or ""),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
