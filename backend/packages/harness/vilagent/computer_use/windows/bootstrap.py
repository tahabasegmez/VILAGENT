"""Configuration-driven construction of read-only Windows providers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from vilagent.computer_use.observation_store import JsonFileObservationStore
from vilagent.computer_use.session import DesktopSessionService
from vilagent.computer_use.windows.screen import WindowsScreenProvider
from vilagent.computer_use.windows.redaction import WindowsUIAPasswordRedactor
from vilagent.computer_use.windows.uia import WindowsUIAProvider
from vilagent.config.computer_use_config import ComputerUseConfig


def create_windows_session_service(
    config: ComputerUseConfig,
    *,
    grabber: Callable[[], Any] | None = None,
    redact: Callable[[Any], Any] | None = None,
    active_window_reader: Callable[[], Any] | None = None,
) -> DesktopSessionService:
    """Build the local read-only screen observation session service."""

    resolved_redactor = redact
    if resolved_redactor is None and config.observation.redact_sensitive_regions:
        resolved_redactor = WindowsUIAPasswordRedactor(comtypes_cache_dir=config.uia_comtypes_cache_dir)

    def observation_provider_factory(store):
        return WindowsScreenProvider(store, grabber=grabber, redact=resolved_redactor, active_window_reader=active_window_reader)

    store_factory = None
    if config.observation.storage_path is not None:
        store_factory = lambda session_id: JsonFileObservationStore(
            Path(config.observation.storage_path) / session_id,
            max_observations_per_session=config.observation.max_history_per_session,
            retention_hours=config.observation.screenshot_retention_hours,
            max_storage_bytes=config.observation.max_storage_bytes_per_session,
        )

    return DesktopSessionService(
        observation_provider_factory=observation_provider_factory,
        observation_store_factory=store_factory,
        max_observations_per_session=config.observation.max_history_per_session,
        lease_stale_after_seconds=config.desktop_lease_stale_after_seconds,
    )


def create_windows_uia_provider(config: ComputerUseConfig) -> WindowsUIAProvider:
    """Build the read-only Windows UI Automation provider."""
    return WindowsUIAProvider(comtypes_cache_dir=config.uia_comtypes_cache_dir)


def create_browser_runtime(config: ComputerUseConfig) -> Any | None:
    """Build an optional browser-use runtime when browser automation is enabled."""
    if not config.browser.enabled:
        return None
    from vilagent.computer_use.browser_use_runtime import create_browser_use_runtime

    return create_browser_use_runtime()
