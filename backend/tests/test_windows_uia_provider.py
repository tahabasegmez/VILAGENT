"""Tests for the read-only pywinauto UIA provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from vilagent.computer_use.models import UIAQuery
from vilagent.computer_use.windows.uia import WindowsUIAProvider


class FakeRect:
    left = 10
    top = 20
    right = 110
    bottom = 70


class FakeControl:
    def __init__(self, text, *, automation_id="", control_type="Button", process_id=42, children=None):
        self._text = text
        self._process_id = process_id
        self._children = children or []
        self.element_info = SimpleNamespace(automation_id=automation_id, control_type=control_type, runtime_id=[process_id, len(text)])

    def window_text(self):
        return self._text

    def process_id(self):
        return self._process_id

    def rectangle(self):
        return FakeRect()

    def descendants(self):
        return self._children

    def is_enabled(self):
        return True

    def is_visible(self):
        return True


class FakeDesktop:
    def __init__(self, windows):
        self._windows = windows

    def windows(self):
        return self._windows


def test_uia_query_requires_selector():
    with pytest.raises(ValidationError, match="at least one selector"):
        UIAQuery()


def test_provider_lists_windows_and_finds_semantic_element():
    save = FakeControl("Save", automation_id="save-button")
    window = FakeControl("Editor", control_type="Window", children=[save])
    provider = WindowsUIAProvider(desktop_factory=lambda: FakeDesktop([window]))

    async def run():
        windows = await provider.list_windows()
        results = await provider.find(UIAQuery(window_title="Editor", automation_id="save-button"))

        assert windows[0].title == "Editor"
        assert results[0].name == "Save"
        assert results[0].bounds is not None
        assert results[0].bounds.width == 100

    asyncio.run(run())


def test_provider_respects_max_results():
    children = [FakeControl(f"Item {index}", control_type="ListItem") for index in range(5)]
    provider = WindowsUIAProvider(desktop_factory=lambda: FakeDesktop([FakeControl("Window", children=children)]))

    async def run():
        results = await provider.find(UIAQuery(control_type="ListItem", max_results=2))
        assert len(results) == 2

    asyncio.run(run())


def test_provider_prepares_writable_comtypes_cache_before_desktop_creation(tmp_path):
    events = []

    def prepare(cache_dir):
        events.append(("prepare", cache_dir))
        return tmp_path

    def desktop_factory():
        events.append(("desktop", None))
        return FakeDesktop([])

    provider = WindowsUIAProvider(
        desktop_factory=desktop_factory,
        comtypes_cache_dir=tmp_path,
        cache_preparer=prepare,
    )

    asyncio.run(provider.list_windows())

    assert events == [("prepare", tmp_path), ("desktop", None)]
