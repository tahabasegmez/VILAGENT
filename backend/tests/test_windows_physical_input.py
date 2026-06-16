"""Tests for explicitly gated Windows physical input."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.models import ActionCommand, ActionKind, Rect, TargetRef, TargetStrategy
from vilagent.computer_use.windows.input import WindowsPhysicalInputProvider, WindowsRoutedActionProvider


def _click():
    return ActionCommand(
        action_id="click-1",
        session_id="session-1",
        kind=ActionKind.click,
        target=TargetRef(strategy=TargetStrategy.coordinate, bounds=Rect(x=10, y=20, width=6, height=8), confidence=1, observation_id="obs-1"),
    )


def test_physical_input_is_disabled_by_default():
    result = asyncio.run(WindowsPhysicalInputProvider().execute(_click()))
    assert result.succeeded is False
    assert result.error_code == "physical_input_disabled"


def test_enabled_physical_click_uses_bounded_target_center():
    calls = []
    result = asyncio.run(WindowsPhysicalInputProvider(enabled=True, click_injector=lambda x, y: calls.append((x, y))).execute(_click()))
    assert result.succeeded is True
    assert calls == [(13, 24)]


def test_routed_provider_sends_coordinate_only_to_physical_provider():
    class Provider:
        def __init__(self, name):
            self.name = name
            self.calls = []

        async def execute(self, action):
            self.calls.append(action)
            from vilagent.computer_use.models import NativeActionResult

            return NativeActionResult(succeeded=True, details={"provider": self.name})

    semantic = Provider("semantic")
    physical = Provider("physical")
    routed = WindowsRoutedActionProvider(semantic, physical)

    result = asyncio.run(routed.execute(_click()))

    assert result.details["provider"] == "physical"
    assert semantic.calls == []
    assert len(physical.calls) == 1


def test_routed_provider_sends_coordinate_targeted_non_physical_action_to_semantic_provider():
    class Provider:
        def __init__(self, name, supported_actions):
            self.name = name
            self.supported_actions = supported_actions
            self.calls = []

        async def execute(self, action):
            self.calls.append(action)
            from vilagent.computer_use.models import NativeActionResult

            return NativeActionResult(succeeded=True, details={"provider": self.name})

    semantic = Provider("semantic", {ActionKind.type_text})
    physical = Provider("physical", {ActionKind.click})
    routed = WindowsRoutedActionProvider(semantic, physical)
    action = _click().model_copy(update={"kind": ActionKind.type_text, "args": {"text": "value"}})

    result = asyncio.run(routed.execute(action))

    assert result.details["provider"] == "semantic"
    assert len(semantic.calls) == 1
    assert physical.calls == []


def test_routed_provider_does_not_send_browser_action_to_semantic_provider():
    class Provider:
        def __init__(self):
            self.calls = []

        async def execute(self, action):
            self.calls.append(action)
            from vilagent.computer_use.models import NativeActionResult
            return NativeActionResult(succeeded=True)

    semantic, physical, browser = Provider(), Provider(), Provider()
    routed = WindowsRoutedActionProvider(semantic, physical, browser)
    action = ActionCommand(action_id="browser-1", session_id="session-1", kind=ActionKind.browser_action)
    result = asyncio.run(routed.execute(action))
    assert result.succeeded is True
    assert len(browser.calls) == 1
    assert semantic.calls == physical.calls == []


def test_routed_provider_sends_targetless_visit_url_to_semantic_provider():
    class Provider:
        def __init__(self):
            self.calls = []

        async def execute(self, action):
            self.calls.append(action)
            from vilagent.computer_use.models import NativeActionResult
            return NativeActionResult(succeeded=True)

    semantic, physical, browser = Provider(), Provider(), Provider()
    routed = WindowsRoutedActionProvider(semantic, physical, browser)
    action = ActionCommand(
        action_id="navigate-1",
        session_id="session-1",
        kind=ActionKind.browser_action,
        args={"action": "visit_url", "url": "https://example.com"},
        postconditions=[{"kind": "screen_changed"}],
    )

    result = asyncio.run(routed.execute(action))

    assert result.succeeded is True
    assert len(semantic.calls) == 1
    assert physical.calls == browser.calls == []


def test_injection_guard_blocks_click_and_cancellation_propagates():
    calls = []

    async def blocked():
        return False

    result = asyncio.run(WindowsPhysicalInputProvider(enabled=True, click_injector=lambda x, y: calls.append((x, y)), injection_guard=blocked).execute(_click()))
    assert result.error_code == "physical_input_guard_blocked"
    assert calls == []

    async def cancelled():
        raise asyncio.CancelledError

    async def run_cancelled():
        await WindowsPhysicalInputProvider(enabled=True, click_injector=lambda x, y: calls.append((x, y)), injection_guard=cancelled).execute(_click())

    try:
        asyncio.run(run_cancelled())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("Cancellation must propagate before physical injection")
    assert calls == []
