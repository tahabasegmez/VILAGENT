"""Tests for the Windows global emergency-stop hotkey listener."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.computer_use.windows.hotkey import WindowsGlobalHotkeyListener, parse_hotkey


def test_parse_hotkey_supports_explicit_safe_key_set():
    spec = parse_hotkey("ctrl+alt+escape")
    function_key = parse_hotkey("ctrl+shift+F12")

    assert spec.modifiers != 0
    assert spec.virtual_key == 0x1B
    assert function_key.virtual_key == 0x7B


@pytest.mark.parametrize("value", ["escape", "ctrl+alt", "ctrl+ctrl+x", "ctrl+control+x", "ctrl+alt+space"])
def test_parse_hotkey_rejects_ambiguous_or_unsupported_values(value):
    with pytest.raises(ValueError):
        parse_hotkey(value)


def test_listener_schedules_trigger_and_stops_background_runner():
    async def run():
        triggered = asyncio.Event()

        async def on_trigger():
            triggered.set()

        def runner(spec, trigger, ready, stop):
            ready.set()
            trigger()
            stop.wait(2)

        listener = WindowsGlobalHotkeyListener("ctrl+alt+escape", on_trigger=on_trigger, runner=runner)
        await listener.start()
        await asyncio.wait_for(triggered.wait(), 1)
        await listener.stop()

        assert listener.running is False

    asyncio.run(run())


def test_listener_fails_closed_when_registration_runner_exits():
    async def run():
        async def on_trigger():
            return None

        def runner(spec, trigger, ready, stop):
            ready.set()

        listener = WindowsGlobalHotkeyListener("ctrl+alt+escape", on_trigger=on_trigger, runner=runner)
        with pytest.raises(RuntimeError, match="registration failed"):
            await listener.start()

        assert listener.running is False

    asyncio.run(run())


def test_listener_fails_closed_when_runner_raises():
    async def run():
        async def on_trigger():
            return None

        def runner(spec, trigger, ready, stop):
            raise OSError("sensitive registration detail")

        listener = WindowsGlobalHotkeyListener("ctrl+alt+escape", on_trigger=on_trigger, runner=runner)
        with pytest.raises(RuntimeError, match="registration failed") as exc_info:
            await listener.start()

        assert "sensitive" not in str(exc_info.value)

    asyncio.run(run())


def test_listener_health_becomes_false_if_runner_exits_after_start():
    async def run():
        async def on_trigger():
            return None

        def runner(spec, trigger, ready, stop):
            ready.set()
            stop.wait(2)

        listener = WindowsGlobalHotkeyListener("ctrl+alt+escape", on_trigger=on_trigger, runner=runner)
        await listener.start()
        assert listener.running is True

        listener._stop.set()
        await asyncio.to_thread(listener._thread.join, 1)
        assert listener.running is False

    asyncio.run(run())
