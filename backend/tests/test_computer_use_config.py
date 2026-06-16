"""Tests for VILAGENT computer-use configuration."""

from __future__ import annotations

import pytest

from vilagent.config.app_config import AppConfig
from vilagent.config.computer_use_config import ComputerUseConfig


def test_computer_use_is_disabled_by_default(monkeypatch):
    for name in (
        "VILAGENT_TEXT_MODEL_PROVIDER",
        "VILAGENT_TEXT_MODEL_CONFIG_NAME",
        "VILAGENT_TEXT_MODEL_NAME",
        "VILAGENT_TEXT_API_BASE_URL",
        "VILAGENT_TEXT_API_KEY",
        "VILAGENT_TEXT_PYNGROK_BASE_URL",

        "VILAGENT_FARA_ENABLED",
        "VILAGENT_FARA_MODEL_NAME",
        "VILAGENT_FARA_BASE_URL",
        "VILAGENT_FARA_API_KEY",
        "VILAGENT_UITARS_ENABLED",
        "VILAGENT_UITARS_MODEL_NAME",
        "VILAGENT_UITARS_PYNGROK_URL",
        "VILAGENT_UITARS_API_KEY",
        "VILAGENT_UITARS_ENDPOINT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    config = ComputerUseConfig()

    assert config.enabled is False
    assert config.agent_mode == "vilagent"
    assert config.planner_model is None
    assert config.text_model.provider == "api"
    assert config.text_model.model_config_name is None
    assert config.text_model.pyngrok_url is None
    assert config.vision_provider == "fara"
    assert config.vision_fara_model.enabled is False
    assert config.vision_fara_model.model_name == "microsoft/Fara-7B"
    assert config.vision_model.provider == "pyngrok"
    assert config.vision_model.enabled is False
    assert config.vision_model.model_name == "UI-TARS-1.5-7B"
    assert config.vision_model.endpoint_path == "/resolve"
    assert config.prompt_profile == "compact"
    assert config.platform == "windows"
    assert config.runtime_mode == "dedicated_process"
    assert config.uia_comtypes_cache_dir == ".vilagent/comtypes-cache"
    assert config.lifecycle_path == ".vilagent/computer-use/lifecycle.json"
    assert config.desktop_lease_stale_after_seconds == 30
    assert config.host_safety.allowed_actions is None
    assert config.host_safety.audit_dir == ".vilagent/computer-use/audit"
    assert config.host_safety.physical_input_enabled is False
    assert config.observation.max_history_per_session == 100
    assert config.observation.storage_path == ".vilagent/computer-use/observations"
    assert config.observation.max_storage_bytes_per_session == 536870912
    assert config.observation.max_export_bytes == 16777216
    assert config.observation.max_concurrent_exports == 2
    assert config.observation.max_exports_per_minute_per_owner == 30
    assert config.browser.enabled is False
    assert config.browser.allowed_domains == []
    assert config.budgets.vision_calls == 10


def test_vilagent_optional_env_refs_have_startup_safe_defaults(monkeypatch):
    monkeypatch.delenv("VILAGENT_UITARS_ENABLED", raising=False)
    monkeypatch.delenv("VILAGENT_UITARS_PYNGROK_URL", raising=False)
    monkeypatch.delenv("VILAGENT_FARA_ENABLED", raising=False)
    monkeypatch.delenv("REQUIRED_EXTERNAL_KEY", raising=False)

    resolved = AppConfig.resolve_env_variables(
        {
            "enabled": "$VILAGENT_UITARS_ENABLED",
            "pyngrok_url": "$VILAGENT_UITARS_PYNGROK_URL",
            "fara_enabled": "$VILAGENT_FARA_ENABLED",
        }
    )

    assert resolved == {"enabled": "false", "pyngrok_url": None, "fara_enabled": "false"}
    with pytest.raises(ValueError, match="REQUIRED_EXTERNAL_KEY"):
        AppConfig.resolve_env_variables("$REQUIRED_EXTERNAL_KEY")
