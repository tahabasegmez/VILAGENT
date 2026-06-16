"""Tests for config-driven Windows provider construction."""

from __future__ import annotations

import asyncio

from PIL import Image

from vilagent.computer_use.windows import create_windows_session_service, create_windows_uia_provider
from vilagent.config.computer_use_config import ComputerUseConfig


def test_windows_bootstrap_builds_observation_session_service():
    async def run():
        config = ComputerUseConfig(observation={"storage_path": None, "redact_sensitive_regions": False})
        service = create_windows_session_service(config, grabber=lambda: Image.new("RGB", (40, 30)))
        await service.create(session_id="session-1")

        observation = await service.observe("session-1")

        assert observation.screen_size.width == 40
        assert (await service.get("session-1")).provider_name == "windows-screen"

    asyncio.run(run())


def test_windows_bootstrap_passes_configured_comtypes_cache_path():
    config = ComputerUseConfig(uia_comtypes_cache_dir=".runtime/uia-cache")

    provider = create_windows_uia_provider(config)

    assert provider.comtypes_cache_dir == ".runtime/uia-cache"
