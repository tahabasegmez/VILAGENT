"""The managed Playwright browser: action mapping and the shared session."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.actions import Action, ActionKind, Target
from vilagent.config.computer_use_config import ComputerUseBrowserConfig
from vilagent.control import Control
from vilagent.env import browser as bp
from vilagent.env.browser import BrowserEnvironment, PlaywrightBrowserSession


def _coord_action(kind: ActionKind, x: int, y: int, **args) -> Action:
    return Action(kind=kind, target=Target.at(x, y), args=args)


class FakeMouse:
    def __init__(self):
        self.events = []

    async def click(self, x, y, **kwargs):
        self.events.append(("click", x, y, kwargs.get("button", "left"), kwargs.get("click_count", 1)))

    async def move(self, x, y, **kwargs):
        self.events.append(("move", x, y))

    async def wheel(self, dx, dy):
        self.events.append(("wheel", dx, dy))


class FakeKeyboard:
    def __init__(self):
        self.events = []

    async def type(self, text, **kwargs):
        self.events.append(("type", text))

    async def press(self, key):
        self.events.append(("press", key))

    async def down(self, key):
        self.events.append(("down", key))

    async def up(self, key):
        self.events.append(("up", key))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.url = "about:blank"
        self.goto_calls = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    async def go_back(self, **kwargs):
        self.url = "back"

    async def reload(self, **kwargs):
        pass

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    def expect_event(self, name, **kwargs):

        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                # Pretend no popup happened: raise the timeout-like path is handled by caller.
                return False

            @property
            def value(self):
                raise TimeoutError("no popup")

        return _Ctx()


@pytest.mark.asyncio
async def test_shared_browser_session_persists_and_recreates_when_dead(monkeypatch):
    instances: list[_FakeShared] = []

    class _FakeShared:
        def __init__(self, **kwargs):
            self.alive = True
            self.started = False
            self.closed = False
            instances.append(self)

        async def start(self):
            self.started = True

        def on_close(self, callback):
            pass

        def is_alive(self):
            return self.alive

        async def close(self):
            self.closed = True

    monkeypatch.setattr(bp, "PlaywrightBrowserSession", _FakeShared)
    monkeypatch.setattr(bp, "_shared_session", None)

    first = await bp.get_shared_browser_session(headless=True)
    second = await bp.get_shared_browser_session(headless=True)
    assert first is second  # reused, not recreated
    assert len(instances) == 1 and instances[0].started

    # Operator closed the window -> next call rebuilds a fresh session.
    first.alive = False
    third = await bp.get_shared_browser_session(headless=True)
    assert third is not first
    assert first.closed is True
    assert len(instances) == 2

    await bp.close_shared_browser_session()
    assert third.closed is True
    assert bp._shared_session is None


def _session_with_fake_page() -> tuple[PlaywrightBrowserSession, FakePage]:
    s = PlaywrightBrowserSession(viewport_width=1000, viewport_height=700)
    page = FakePage()
    s._page = page
    s._started = True
    return s, page


@pytest.mark.asyncio
async def test_browser_session_maps_click_type_key_scroll_navigate():
    s, page = _session_with_fake_page()

    assert (await s.run_action(_coord_action(ActionKind.click, 120, 240))).ok
    assert ("click", 120.0, 240.0, "left", 1) in page.mouse.events

    assert (await s.run_action(Action(kind=ActionKind.type_text, args={"text": "hello"}))).ok
    assert ("type", "hello") in page.keyboard.events

    assert (await s.run_action(Action(kind=ActionKind.hotkey, args={"keys": ["Enter"]}))).ok
    assert ("down", "Enter") in page.keyboard.events and ("up", "Enter") in page.keyboard.events

    assert (await s.run_action(_coord_action(ActionKind.scroll, 500, 350, amount=-300))).ok
    assert any(e[0] == "wheel" for e in page.mouse.events)

    assert (await s.run_action(Action(kind=ActionKind.browser_action, args={"action": "visit_url", "url": "https://example.com"}))).ok
    assert "https://example.com" in page.goto_calls
    assert (await s.run_action(Action(kind=ActionKind.browser_action, args={"action": "teleport"}))).error == "browser_unsupported_operation:teleport"


@pytest.mark.asyncio
async def test_browser_keypress_chord_orders_down_then_up_reversed():
    s, page = _session_with_fake_page()
    await s.run_action(Action(kind=ActionKind.hotkey, args={"keys": ["ctrl", "a"]}))
    assert page.keyboard.events == [("down", "Control"), ("down", "a"), ("up", "a"), ("up", "Control")]


def test_browser_environment_uses_the_shared_session_and_reports_the_page(monkeypatch):
    s, page = _session_with_fake_page()
    requested = []

    async def shared(**kwargs):
        requested.append(kwargs)
        return s

    monkeypatch.setattr(bp, "get_shared_browser_session", shared)
    env = BrowserEnvironment(ComputerUseBrowserConfig(channel="chrome"), Control())

    assert "blank page" in env.context_hint()
    outcome = asyncio.run(env.act(Action(kind=ActionKind.browser_action, args={"action": "visit_url", "url": "example.com"})))

    assert outcome.ok
    assert page.goto_calls == ["https://example.com"]
    assert requested[0]["channel"] == "chrome"
    assert env.context_hint() == "CURRENT PAGE: https://example.com"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:/Users/me/AppData/Local/Google/Chrome/User Data", "chrome"),
        ("C:/Users/me/AppData/Local/Microsoft/Edge/User Data", "msedge"),
        (None, "msedge"),
    ],
)
def test_channel_follows_the_profile_path(path, expected):
    assert bp._infer_channel("msedge", path) == expected
