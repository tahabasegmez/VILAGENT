"""Tests for fail-closed Windows input-desktop safety detection."""

from __future__ import annotations

import asyncio
import threading

from vilagent.computer_use.models import DesktopSafetySnapshot, DesktopSafetyStatus
from vilagent.computer_use.windows.desktop_safety import WindowsDesktopSafetyProvider, classify_input_desktop


def test_classifies_default_locked_and_non_default_desktops():
    assert classify_input_desktop("Default").status == DesktopSafetyStatus.ready
    assert classify_input_desktop("Winlogon").status == DesktopSafetyStatus.locked
    assert classify_input_desktop("Screen-saver").status == DesktopSafetyStatus.locked
    assert classify_input_desktop("CredentialUI").status == DesktopSafetyStatus.secure_desktop
    assert classify_input_desktop("").status == DesktopSafetyStatus.unknown


def test_provider_offloads_probe_from_event_loop_thread():
    async def run():
        event_loop_thread = threading.get_ident()
        probe_thread = None

        def probe():
            nonlocal probe_thread
            probe_thread = threading.get_ident()
            return DesktopSafetySnapshot(status=DesktopSafetyStatus.ready)

        snapshot = await WindowsDesktopSafetyProvider(probe=probe).check()

        assert snapshot.status == DesktopSafetyStatus.ready
        assert probe_thread != event_loop_thread

    asyncio.run(run())


def test_provider_converts_probe_failure_to_unavailable_without_leaking_details():
    async def run():
        def probe():
            raise OSError("sensitive desktop detail")

        snapshot = await WindowsDesktopSafetyProvider(probe=probe).check()

        assert snapshot.status == DesktopSafetyStatus.unavailable
        assert snapshot.reason_code == "desktop_safety_probe_failed"
        assert "sensitive" not in snapshot.model_dump_json()

    asyncio.run(run())
