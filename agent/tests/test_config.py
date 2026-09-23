"""Tests for VILAGENT computer-use configuration."""

from __future__ import annotations

from vilagent.config.app_config import AppConfig
from vilagent.config.computer_use_config import ComputerUseConfig


def test_computer_use_is_on_and_owes_nothing_to_the_environment(monkeypatch):
    # Nothing about a model comes from the environment any more: every model is a connection.
    for name in ("VILAGENT_FARA_ENABLED", "VILAGENT_FARA_MODEL_NAME", "VILAGENT_FARA_BASE_URL", "VILAGENT_FARA_API_KEY"):
        monkeypatch.setenv(name, "whatever")

    config = ComputerUseConfig()

    assert config.enabled is True  # operating the computer is what the app is for
    assert config.vision_model_name == ""
    assert config.platform == "windows"
    assert config.physical_input_enabled is True
    assert config.redact_passwords is True
    assert config.uia_comtypes_cache_dir == ".vilagent/comtypes-cache"
    assert config.action_log_path == ".vilagent/actions.jsonl"
    assert config.budgets.total_actions == 150


def test_older_config_layouts_still_load():
    config = ComputerUseConfig.model_validate(
        {
            "enabled": True,
            "runtime_mode": "dedicated_process",
            "unrestricted": True,
            "vision_model": {"provider": "uitars"},
            "host_safety": {"physical_input_enabled": False, "audit_dir": "x"},
            "observation": {"redact_sensitive_regions": False, "storage_path": "y"},
            "browser": {"enabled": True, "allowed_domains": ["example.com"], "channel": "chrome"},
        }
    )

    assert config.enabled is True
    assert config.physical_input_enabled is False
    assert config.redact_passwords is False
    assert config.browser.channel == "chrome"


def test_settings_are_stored_in_the_state_file(tmp_path, monkeypatch):
    """There is no config.yaml: the settings live beside the operator's other choices."""
    from vilagent.config import app_config
    from vilagent.server import state

    monkeypatch.setattr(state, "STATE_FILE_PATH", tmp_path / "state.json")
    app_config.reset_app_config()

    assert app_config.get_app_config().computer_use.budgets.total_actions == 150  # the defaults

    app_config.save_app_config(AppConfig(log_level="debug", computer_use=ComputerUseConfig(enabled=True)))
    app_config.reset_app_config()

    assert state.read_state()["settings"]["log_level"] == "debug"
    assert app_config.get_app_config().computer_use.enabled is True

    state.set_state_value("settings", {"computer_use": {"budgets": {"total_actions": "not a number"}}})
    app_config.reset_app_config()

    assert app_config.get_app_config().computer_use.budgets.total_actions == 150  # unreadable: the defaults
