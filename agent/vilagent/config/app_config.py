"""The app's settings. They live in the state file, not in a config file.

There is no ``config.yaml``: the models are connections in the encrypted database
(``vilagent/connections.py``) and everything else — budgets, approval rules, the browser, the
log level — is the ``settings`` block of ``<data dir>/.vilagent_state.json``, beside the
operator's other choices. ``.env`` is only read for how the process starts (the interpreter the
launcher uses, the gateway's host, port and data dir).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from vilagent.config.computer_use_config import ComputerUseConfig

logger = logging.getLogger(__name__)

# agent/vilagent/config/app_config.py -> agent/ and the repo root.
_AGENT_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _AGENT_DIR.parent
#: Where the settings sit inside the state file.
SETTINGS_KEY = "settings"


def _load_dotenv_files() -> None:
    """Load ``.env`` from the cwd and its parents (nearest wins), then the repo root."""
    candidates = [directory / ".env" for directory in (Path.cwd(), *Path.cwd().parents)]
    if not getattr(sys, "frozen", False):
        candidates.append(_REPO_ROOT / ".env")
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)


_load_dotenv_files()


def apply_logging_level(name: str | None) -> None:
    """Apply ``log_level`` to the ``vilagent``/``app`` loggers (third-party loggers untouched)."""
    level = logging.getLevelNamesMapping().get((name or "info").strip().upper(), logging.INFO)
    for logger_name in ("vilagent", "app"):
        logging.getLogger(logger_name).setLevel(level)
    for handler in logging.root.handlers:
        if level < handler.level:
            handler.setLevel(level)


class AppConfig(BaseModel):
    """Everything about a run that is not a model: budgets, approvals, the browser, logging."""

    log_level: str = "info"
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)
    model_config = ConfigDict(extra="ignore")


_app_config: AppConfig | None = None


def get_app_config() -> AppConfig:
    """The settings as stored, built fresh when the state file changed under us."""
    global _app_config
    if _app_config is None:
        _app_config = load_app_config()
    return _app_config


def load_app_config() -> AppConfig:
    """Read the ``settings`` block; anything missing falls back to the defaults above."""
    from vilagent.server.state import get_state_value

    stored = get_state_value(SETTINGS_KEY, None)
    if not isinstance(stored, dict):
        return AppConfig()
    try:
        return AppConfig.model_validate(stored)
    except Exception:  # a value a later version no longer accepts must not stop the app
        logger.warning("The stored settings could not be read; using the defaults.")
        return AppConfig()


def save_app_config(config: AppConfig) -> AppConfig:
    """Store the settings and use them from now on."""
    from vilagent.server.state import set_state_value

    set_state_value(SETTINGS_KEY, config.model_dump(mode="json"))
    return set_app_config(config)


def reset_app_config() -> None:
    global _app_config
    _app_config = None


def set_app_config(config: AppConfig) -> AppConfig:
    """Use this instance from now on (the settings API, and tests)."""
    global _app_config
    _app_config = config
    return config
