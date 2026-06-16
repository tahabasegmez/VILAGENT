"""Robustness tests for hotkey token normalization.

The vision executors (FARA emits ``keys`` as an array, UI-TARS emits
space-separated ``key='ctrl c'`` combos) and the planner all funnel hotkeys
through ``_pywinauto_hotkey``. A parsing gap here used to fail the whole step
with ``hotkey_failed``, so these cover the shapes a model can realistically
produce.
"""

from __future__ import annotations

import pytest

from vilagent.computer_use.windows.action import _flatten_hotkey_tokens, _pywinauto_hotkey


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # combined string forms
        ("ctrl+c", "^c"),
        ("ctrl c", "^c"),
        ("CTRL+C", "^c"),
        ("ctrl+shift+esc", "^+{ESC}"),
        ("alt+f4", "%{F4}"),
        ("enter", "{ENTER}"),
        ("ctrl+l", "^l"),
        # FARA array forms
        (["ctrl", "c"], "^c"),
        (["ctrl", "shift", "t"], "^+t"),
        (["alt", "f4"], "%{F4}"),
        (["enter"], "{ENTER}"),
        # array-with-combined-string (mixed shapes)
        (["ctrl+c"], "^c"),
        (["ctrl c"], "^c"),
        # windows / super key as modifier and standalone
        (["win", "r"], "{VK_LWIN down}r{VK_LWIN up}"),
        ("win", "{VK_LWIN}"),
        ("super+d", "{VK_LWIN down}d{VK_LWIN up}"),
        # named keys
        ("printscreen", "{PRTSC}"),
        ("pagedown", "{PGDN}"),
        # literal plus key
        ("ctrl++", "^{+}"),
        (["ctrl", "+"], "^{+}"),
        # unknown vk-style name falls back instead of raising
        (["volume_up"], "{VK_VOLUME_UP}"),
    ],
)
def test_hotkey_normalizes_to_pywinauto(raw, expected):
    assert _pywinauto_hotkey(raw) == expected


def test_empty_hotkey_raises():
    with pytest.raises(ValueError):
        _pywinauto_hotkey([])
    with pytest.raises(ValueError):
        _pywinauto_hotkey("")


def test_flatten_handles_whitespace_and_plus():
    assert _flatten_hotkey_tokens("ctrl  shift   t") == ["ctrl", "shift", "t"]
    assert _flatten_hotkey_tokens(["ctrl+a"]) == ["ctrl", "a"]
    assert _flatten_hotkey_tokens("ctrl++") == ["ctrl", "+"]
