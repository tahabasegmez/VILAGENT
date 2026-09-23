"""When to ask the operator: pure decisions, no I/O.

- Steps: the planner's risk level against the operator's threshold.
- Actions: rules that don't depend on any model's judgement (keywords in FARA's note, sites
  outside an allow-list, destructive hotkeys). They matter most in autonomous runs, where one
  "step" is the whole task.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from vilagent.actions import POINTER_KINDS, Action, ActionKind
from vilagent.config.computer_use_config import ComputerUseApprovalConfig

THRESHOLDS = ("off", "critical", "high", "medium")
_LEVELS = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_KEY_ALIASES = {"control": "ctrl", "escape": "esc", "return": "enter", "del": "delete", "win": "super", "windows": "super"}


def needs_approval(level: str, threshold: str) -> bool:
    """``threshold`` "high" asks for high and critical steps; "off" never asks."""
    if threshold not in _LEVELS:
        return False
    return _LEVELS.get(level, _LEVELS["critical"]) >= _LEVELS[threshold]


def risky_words(text: str, keywords: list[str]) -> list[str]:
    """Approval keywords appearing in ``text``; rates a task nobody planned (the direct approach)."""
    return [word for word in keywords if re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE)]


def matched_rule(action: Action, rules: ComputerUseApprovalConfig) -> str | None:
    """Why this action needs the operator's approval, or None."""
    keys = hotkey_keys(action.args.get("keys")) if action.kind == ActionKind.hotkey else frozenset()
    if keys:
        for blocked in rules.hotkeys:
            if hotkey_keys(blocked) == keys:
                return f"Presses {'+'.join(sorted(keys))}."
    commits = action.kind in POINTER_KINDS or "enter" in keys
    if commits and action.thought:
        for word in rules.keywords:
            if re.search(rf"\b{re.escape(word)}\b", action.thought, re.IGNORECASE):
                return f'The next action looks like "{word}".'
    if action.kind == ActionKind.browser_action and action.args.get("action") == "visit_url" and rules.allowed_domains:
        host = (urlparse(_with_scheme(str(action.args.get("url") or ""))).hostname or "").lower()
        if not any(host == domain or host.endswith(f".{domain}") for domain in (d.lower().lstrip(".") for d in rules.allowed_domains)):
            return f"Opens {host or 'an unknown site'}, which is not on the allowed list."
    return None


def hotkey_keys(value: Any) -> frozenset[str]:
    tokens = value if isinstance(value, list | tuple) else re.split(r"[+\s]+", str(value or ""))
    return frozenset(_KEY_ALIASES.get(token.strip().lower(), token.strip().lower()) for token in tokens if str(token).strip())


def _with_scheme(url: str) -> str:
    return url if "://" in url else f"https://{url}"
