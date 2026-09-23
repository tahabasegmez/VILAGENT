"""Mask personal data before anything reaches the memory (DEV principle 6).

Memory keeps *how* a task was done, never *what* was typed: quoted literals, e-mail addresses,
long numbers (phone, card, account) and URL query strings are replaced by placeholders, and
plan arguments are never stored at all (see ``step_outline``).
"""

from __future__ import annotations

import re
from typing import Any

_URL_TAIL = re.compile(r"(https?://[^\s?#]+)[?#]\S*", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NUMBER = re.compile(r"\d(?:[ -]?\d){5,}")  # six or more digits, spaces and dashes allowed between
_DOUBLE_QUOTED = re.compile(r"[\"“”]([^\"“”]+)[\"“”]")
_SINGLE_QUOTED = re.compile(r"(?<!\w)['‘]([^'’\n]+)['’](?!\w)")

TEXT = "«text»"
EMAIL = "«email»"
NUMBER = "«number»"


def text(value: str | None) -> str:
    """Free text (a task, a lesson, a model's note): quotes, e-mails, numbers and URL tails masked."""
    if not value:
        return ""
    value = _URL_TAIL.sub(r"\1", value)
    value = _EMAIL.sub(EMAIL, value)
    value = _DOUBLE_QUOTED.sub(TEXT, value)
    value = _SINGLE_QUOTED.sub(TEXT, value)
    return _NUMBER.sub(NUMBER, value)


def step_outline(step: dict[str, Any]) -> dict[str, Any]:
    """What memory keeps of a plan step: the masked instruction, its kind and environment (no args)."""
    return {"instruction": text(step.get("instruction")), "kind": step.get("action_kind"), "environment": step.get("environment")}
