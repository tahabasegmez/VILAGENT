"""Mouse input on the Windows desktop (pyautogui). All functions are blocking."""

from __future__ import annotations


def _pyautogui():
    """Import pyautogui without its corner fail-safe or implicit pause (they stall the agent)."""
    import pyautogui

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    return pyautogui


def click(x: int, y: int) -> None:
    _pyautogui().click(x=x, y=y)


def double_click(x: int, y: int) -> None:
    pg = _pyautogui()
    # Two explicit clicks at double-click speed: some Win32 controls miss pyautogui.doubleClick().
    pg.moveTo(x, y)
    pg.click(x=x, y=y, clicks=2, interval=0.08)


def right_click(x: int, y: int) -> None:
    _pyautogui().rightClick(x=x, y=y)


def scroll(x: int, y: int, pixels: int) -> None:
    """Scroll at (x, y). ``pixels`` follows the model convention: positive = up."""
    pg = _pyautogui()
    pg.moveTo(x, y)
    if not pixels:
        pixels = -360  # default: a few notches down
    # One wheel notch is ~120 px.
    notches = int(round(pixels / 120)) or (1 if pixels > 0 else -1)
    pg.scroll(notches, x=x, y=y)


def screen_center() -> tuple[int, int]:
    try:
        width, height = _pyautogui().size()
        return int(width) // 2, int(height) // 2
    except Exception:
        return 960, 540
