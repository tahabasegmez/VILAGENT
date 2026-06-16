"""Tests for the optional browser-use runtime adapter."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.computer_use.browser import BrowserUnavailableError
from vilagent.computer_use.browser_use_runtime import BrowserUseRuntime


class FakeLocator:
    def __init__(self):
        self.clicked = False
        self.filled = []

    async def count(self):
        return 1

    async def click(self):
        self.clicked = True

    async def fill(self, text):
        self.filled.append(text)


class FakePage:
    def __init__(self, url):
        self.url = url
        self.closed = False
        self.locators = {}

    async def title(self):
        return "Inbox"

    def locator(self, selector):
        locator = self.locators.setdefault(selector, FakeLocator())
        return locator

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.started = False
        self.pages = []

    async def start(self):
        self.started = True

    async def create_new_tab(self, url):
        page = FakePage(url)
        page.tab_id = f"tab-{len(self.pages) + 1}"
        self.pages.append(page)
        return page


def test_browser_use_runtime_reports_missing_optional_dependency():
    async def run():
        runtime = BrowserUseRuntime()

        health = await runtime.health()

        assert health.healthy is False
        assert health.error_code == "browser_use_not_installed"
        with pytest.raises(BrowserUnavailableError):
            await runtime.create_session("https://example.com")

    asyncio.run(run())


def test_browser_use_runtime_wraps_playwright_like_session():
    async def run():
        session = FakeSession()
        runtime = BrowserUseRuntime(session_factory=lambda: session)

        health = await runtime.health()
        created = await runtime.create_session("https://example.com/inbox")
        observed = await runtime.observe(created.tab_id)
        target = await runtime.resolve_dom(created.tab_id, {"css": "#save"})
        exists = await runtime.query_state(created.tab_id, target)
        clicked = await runtime.execute(created.tab_id, target, {"browser_action": "click"})
        typed = await runtime.execute(created.tab_id, {"css": "#message"}, {"browser_action": "type", "text": "hello"})
        await runtime.close_session(created.tab_id)

        assert health.healthy is True
        assert session.started is True
        assert created.url == "https://example.com/inbox"
        assert created.title == "Inbox"
        assert created.tab_id == "tab-1"
        assert observed.url == created.url
        assert target == {"css": "#save"}
        assert exists == {"exists": True}
        assert clicked is True
        assert typed is True
        assert session.pages[0].locators["#save"].clicked is True
        assert session.pages[0].locators["#message"].filled == ["hello"]
        assert session.pages[0].closed is True

    asyncio.run(run())
