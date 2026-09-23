"""The operator UI's persisted selections (approach, execution mode, planner preset, ...)."""

from __future__ import annotations

import json
from typing import Any

from vilagent.server.config import get_gateway_config

STATE_FILE_PATH = get_gateway_config().data_dir / ".vilagent_state.json"

def read_state() -> dict[str, Any]:
    if STATE_FILE_PATH.exists():
        try:
            return json.loads(STATE_FILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def write_state(state: dict[str, Any]) -> None:
    STATE_FILE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

def get_state_value(key: str, default: Any) -> Any:
    return read_state().get(key, default)

def set_state_value(key: str, value: Any) -> None:
    state = read_state()
    state[key] = value
    write_state(state)
