"""Keyboard input on the Windows desktop (pywinauto): launch apps, type, press hotkeys.

All functions are blocking; the desktop environment runs them in a worker thread.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any


def launch_app(app_name: str) -> str:
    """Open an app the way a person would; returns how it was launched."""
    app_name = app_name.strip()
    if not app_name:
        raise ValueError("launch_app needs an app name")
    # Start search matches display names and Store apps that CreateProcess cannot resolve.
    try:
        _launch_from_start_search(app_name)
        return "start_search"
    except Exception:
        pass
    # An executable on PATH (calc, notepad, explorer, ...).
    try:
        subprocess.Popen([app_name], shell=False)
        return "direct"
    except OSError:
        pass
    # `start` resolves App Paths the way the Run dialog does.
    result = subprocess.run(["cmd", "/c", "start", "", app_name], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"Could not launch '{app_name}'")
    return "shell_start"


def type_text(text: str) -> None:
    from pywinauto import keyboard

    keyboard.send_keys(_escape_send_keys(text), with_spaces=True)


def press_hotkey(keys: Any) -> None:
    from pywinauto import keyboard

    keyboard.send_keys(_pywinauto_hotkey(keys))


def _launch_from_start_search(app_name: str) -> None:
    from pywinauto import keyboard

    search_text = " ".join(app_name.split())
    if not search_text:
        raise ValueError("Windows Start search requires an app name")
    # Open Start, let it focus its search box, type the app name, wait for results to
    # rank, then open the top hit with Enter.
    keyboard.send_keys("{VK_LWIN}")
    time.sleep(0.7)
    keyboard.send_keys(search_text, with_spaces=True, vk_packet=True)
    time.sleep(1.0)
    keyboard.send_keys("{ENTER}")
    time.sleep(0.5)


def navigate_focused_browser(url: str) -> None:
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
