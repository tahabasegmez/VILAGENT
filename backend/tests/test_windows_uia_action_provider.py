"""Tests for restricted semantic Windows UIA mutations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from vilagent.computer_use.models import ActionCommand, ActionKind, TargetRef, TargetStrategy
from vilagent.computer_use.windows.action import WindowsUIAActionProvider, _matches_stable_selector, _pywinauto_hotkey


class FakeControl:
    def __init__(self):
        self.invoked = 0
        self.focused = 0

    def invoke(self):
        self.invoked += 1

    def set_focus(self):
        self.focused += 1


def _action(kind, *, strategy=TargetStrategy.uia, selector=None):
    return ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=kind,
        target=TargetRef(
            strategy=strategy,
            selector=selector or {"automation_id": "save-button"},
            confidence=1,
            observation_id="obs-1",
        ),
    )


def test_uia_click_uses_semantic_invoke():
    control = FakeControl()
    provider = WindowsUIAActionProvider(control_resolver=lambda selector: control)

    result = asyncio.run(provider.execute(_action(ActionKind.click)))

    assert result.succeeded is True
    assert result.details == {"mode": "semantic_uia"}
    assert control.invoked == 1


def test_uia_focus_uses_set_focus():
    control = FakeControl()
    provider = WindowsUIAActionProvider(control_resolver=lambda selector: control)

    result = asyncio.run(provider.execute(_action(ActionKind.focus_window)))

    assert result.succeeded is True
    assert control.focused == 1


def test_uia_provider_executes_text_and_rejects_unstable_targets():
    provider = WindowsUIAActionProvider(control_resolver=lambda selector: FakeControl())

    text_input = asyncio.run(provider.execute(_action(ActionKind.type_text)))
    unstable = asyncio.run(provider.execute(_action(ActionKind.click, selector={"name": "Save"})))
    wrong_strategy = asyncio.run(provider.execute(_action(ActionKind.click, strategy=TargetStrategy.vision)))

    assert text_input.succeeded is True
    assert unstable.error_code == "uia_stable_selector_required"
    assert wrong_strategy.error_code == "uia_target_required"


def test_launch_app_uses_requested_app_name(monkeypatch):
    launched = []

    def fake_popen(args, **kwargs):
        launched.append((args, kwargs))

    monkeypatch.setattr("vilagent.computer_use.windows.action.subprocess.Popen", fake_popen)
    provider = WindowsUIAActionProvider(control_resolver=lambda selector: FakeControl())
    action = ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=ActionKind.launch_app,
        args={"app_name": "calc.exe"},
    )

    result = asyncio.run(provider.execute(action))

    assert result.succeeded is True
    assert launched == [(["calc.exe"], {"shell": False})]
    assert result.details == {"mode": "direct_app_launch", "app": "calc.exe"}


def test_launch_app_falls_back_to_windows_start_search(monkeypatch):
    searched = []

    def missing_executable(args, **kwargs):
        raise FileNotFoundError("not on PATH")

    monkeypatch.setattr("vilagent.computer_use.windows.action.subprocess.Popen", missing_executable)
    provider = WindowsUIAActionProvider(
        control_resolver=lambda selector: FakeControl(),
        app_search_launcher=searched.append,
    )
    action = ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=ActionKind.launch_app,
        args={"app_name": "Calculator"},
    )

    result = asyncio.run(provider.execute(action))

    assert result.succeeded is True
    assert searched == ["Calculator"]
    assert result.details["mode"] == "windows_start_search"
    assert result.details["app"] == "Calculator"


def test_launch_app_reports_both_direct_and_start_search_failures(monkeypatch):
    def missing_executable(args, **kwargs):
        raise FileNotFoundError("not on PATH")

    def failed_search(app_name):
        raise RuntimeError("Start search unavailable")

    monkeypatch.setattr("vilagent.computer_use.windows.action.subprocess.Popen", missing_executable)
    provider = WindowsUIAActionProvider(
        control_resolver=lambda selector: FakeControl(),
        app_search_launcher=failed_search,
    )
    action = ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=ActionKind.launch_app,
        args={"app_name": "Unknown App"},
    )

    result = asyncio.run(provider.execute(action))

    assert result.succeeded is False
    assert result.error_code == "launch_app_failed"
    assert "Direct launch failed" in result.error_message
    assert "Windows Start search failed" in result.error_message


def test_launch_app_requires_app_name():
    provider = WindowsUIAActionProvider(control_resolver=lambda selector: FakeControl())
    action = ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=ActionKind.launch_app,
        args={},
    )

    result = asyncio.run(provider.execute(action))

    assert result.succeeded is False
    assert result.error_code == "app_name_required"


def test_targetless_visit_url_navigates_focused_browser():
    navigated = []
    provider = WindowsUIAActionProvider(
        control_resolver=lambda selector: FakeControl(),
        url_navigator=navigated.append,
    )
    action = ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=ActionKind.browser_action,
        args={"action": "visit_url", "url": "https://example.com"},
        postconditions=[{"kind": "screen_changed"}],
    )

    result = asyncio.run(provider.execute(action))

    assert result.succeeded is True
    assert navigated == ["https://example.com"]
    assert result.details["mode"] == "focused_browser_navigation"


def test_hotkey_normalizes_fara_key_arrays():
    assert _pywinauto_hotkey(["WIN", "E"]) == "{VK_LWIN down}e{VK_LWIN up}"
    assert _pywinauto_hotkey(["CTRL", "L"]) == "^l"
    assert _pywinauto_hotkey("Enter") == "{ENTER}"
    assert _pywinauto_hotkey(["CTRL", "SHIFT", "enter"]) == "^+{ENTER}"
    assert _pywinauto_hotkey("PageDown") == "{PGDN}"


def test_stable_selector_uses_exact_semantic_identity():
    control = FakeControl()
    control.element_info = SimpleNamespace(automation_id="save-button", control_type="Button", runtime_id=[1, 2])
    control.process_id = lambda: 42
    control.window_text = lambda: "Save"

    assert _matches_stable_selector(control, {"automation_id": "save-button", "process_id": 42})
    assert not _matches_stable_selector(control, {"automation_id": "save"})
    assert not _matches_stable_selector(control, {"unknown": "value"})
