"""The desktop environment routes each action to the right Windows operation."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.actions import Action, ActionKind, Target, TargetStrategy
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.control import Control
from vilagent.env.desktop import DesktopEnvironment, keyboard


class FakeDriver:
    def __init__(self, *, fail_on: str | None = None):
        self.calls: list[tuple] = []
        self._fail_on = fail_on

    def init_thread(self):
        pass

    def __getattr__(self, name):
        def record(*args, **kwargs):
            if name == self._fail_on:
                raise RuntimeError(f"{name} failed")
            self.calls.append((name, *args, *sorted(kwargs.items())))
            if name == "screen_center":
                return (960, 540)
            if name == "screenshot":
                return b"png"
            if name == "resolve_target":
                return Target(strategy=TargetStrategy.uia, selector={"automation_id": "save"})
            return None

        return record


def _env(driver, **config):
    return DesktopEnvironment(ComputerUseConfig(**config), Control(), driver=driver)


def _run(env, action):
    return asyncio.run(env.act(action))


@pytest.mark.parametrize(
    ("kind", "call"),
    [(ActionKind.click, "click"), (ActionKind.double_click, "double_click"), (ActionKind.right_click, "right_click")],
)
def test_coordinate_pointer_actions_use_the_mouse(kind, call):
    driver = FakeDriver()

    outcome = _run(_env(driver), Action(kind=kind, target=Target.at(13, 24)))

    assert outcome.ok
    assert driver.calls == [(call, 13, 24)]


def test_scroll_without_target_uses_screen_center():
    driver = FakeDriver()

    assert _run(_env(driver), Action(kind=ActionKind.scroll, args={"amount": -300})).ok
    assert driver.calls[-1] == ("scroll", 960, 540, -300)


def test_physical_input_can_be_disabled():
    driver = FakeDriver()

    outcome = _run(_env(driver, physical_input_enabled=False), Action(kind=ActionKind.click, target=Target.at(1, 2)))

    assert outcome.error == "physical_input_disabled"
    assert driver.calls == []


def test_keyboard_actions():
    driver = FakeDriver()
    env = _env(driver)

    assert _run(env, Action(kind=ActionKind.launch_app, args={"app_name": "Notepad"})).ok
    assert _run(env, Action(kind=ActionKind.type_text, args={"text": "12+23="})).ok
    assert _run(env, Action(kind=ActionKind.hotkey, args={"keys": ["Control", "s"]})).ok
    assert _run(env, Action(kind=ActionKind.browser_action, args={"action": "visit_url", "url": "https://example.com"})).ok

    assert driver.calls == [
        ("launch_app", "Notepad"),
        ("type_text", "12+23="),
        ("press_hotkey", ["Control", "s"]),
        ("navigate", "https://example.com"),
    ]


def test_launch_requires_an_app_name():
    assert _run(_env(FakeDriver()), Action(kind=ActionKind.launch_app)).error == "app_name_required"


def test_uia_targets_invoke_or_focus_the_element():
    driver = FakeDriver()
    env = _env(driver)
    target = Target(strategy=TargetStrategy.uia, selector={"automation_id": "save"})

    assert _run(env, Action(kind=ActionKind.click, target=target)).ok
    assert _run(env, Action(kind=ActionKind.focus_window, target=target)).ok
    assert _run(env, Action(kind=ActionKind.double_click, target=target)).error == "uia_unsupported_action:double_click"

    assert driver.calls == [
        ("invoke", {"automation_id": "save"}, ("focus_only", False)),
        ("invoke", {"automation_id": "save"}, ("focus_only", True)),
    ]


def test_driver_errors_become_failed_outcomes():
    outcome = _run(_env(FakeDriver(fail_on="type_text")), Action(kind=ActionKind.type_text, args={"text": "x"}))

    assert not outcome.ok
    assert outcome.error == "action_error: RuntimeError"


def test_resolve_target_swallows_uia_errors():
    env = _env(FakeDriver(fail_on="resolve_target"))

    assert asyncio.run(env.resolve_target("Save")) is None


def test_launch_app_prefers_start_search_then_falls_back(monkeypatch):
    searched, launched = [], []
    monkeypatch.setattr(keyboard, "_launch_from_start_search", searched.append)
    monkeypatch.setattr(keyboard.subprocess, "Popen", lambda args, **kwargs: launched.append(args))

    assert keyboard.launch_app("Calculator") == "start_search"
    assert searched == ["Calculator"] and launched == []

    def broken_search(name):
        raise RuntimeError("no start menu")

    monkeypatch.setattr(keyboard, "_launch_from_start_search", broken_search)
    assert keyboard.launch_app("calc.exe") == "direct"
    assert launched == [["calc.exe"]]


def test_an_element_without_an_invoke_pattern_is_clicked_where_it_sits():
    # Tree items and list rows often implement no Invoke pattern; their place is known anyway.
    driver = FakeDriver(fail_on="invoke")
    env = _env(driver)
    target = Target(strategy=TargetStrategy.uia, selector={"automation_id": "save"}, bounds={"x": 10, "y": 20, "width": 100, "height": 50})

    assert _run(env, Action(kind=ActionKind.click, target=target)).ok
    assert driver.calls == [("click", 60, 45)]

    # Focusing a window has no such fallback, and neither has an element with no bounds.
    assert not _run(env, Action(kind=ActionKind.focus_window, target=target)).ok
    assert not _run(env, Action(kind=ActionKind.click, target=Target(strategy=TargetStrategy.uia, selector={"automation_id": "save"}))).ok
