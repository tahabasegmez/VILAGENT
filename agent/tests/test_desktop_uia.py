"""UI Automation lookup and invocation against a fake pywinauto desktop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vilagent.actions import TargetStrategy
from vilagent.env.desktop import uia


class FakeRect:
    left, top, right, bottom = 10, 20, 110, 70


class FakeControl:
    def __init__(self, text, *, automation_id="", control_type="Button", process_id=42, children=(), enabled=True):
        self._text = text
        self._process_id = process_id
        self._children = list(children)
        self._enabled = enabled
        self.element_info = SimpleNamespace(automation_id=automation_id, control_type=control_type, runtime_id=[process_id, len(text), automation_id])
        self.invoked = self.focused = 0

    def window_text(self):
        return self._text

    def process_id(self):
        return self._process_id

    def rectangle(self):
        return FakeRect()

    def descendants(self):
        return self._children

    def is_enabled(self):
        return self._enabled

    def is_visible(self):
        return True

    def invoke(self):
        self.invoked += 1

    def set_focus(self):
        self.focused += 1


class FakeDesktop:
    def __init__(self, *windows):
        self._windows = windows

    def windows(self):
        return list(self._windows)


def _desktop():
    save = FakeControl("Save", automation_id="save")
    cancel = FakeControl("Cancel", automation_id="cancel")
    window = FakeControl("Editor", control_type="Window", children=[save, cancel])
    return FakeDesktop(window), save


def test_find_matches_name_substring_and_limits_results():
    desktop, _ = _desktop()

    assert [e.automation_id for e in uia.find({"name": "sav"}, desktop=desktop)] == ["save"]
    assert len(uia.find({"control_type": "button"}, desktop=desktop, max_results=1)) == 1
    assert uia.find({"unknown": "x"}, desktop=desktop) == []


def test_resolve_target_returns_a_stable_selector():
    desktop, _ = _desktop()

    target = uia.resolve_target("Save", {}, desktop=desktop)

    assert target.strategy == TargetStrategy.uia
    assert target.selector["automation_id"] == "save"
    assert target.selector["process_id"] == 42
    assert target.bounds.center == (60, 45)


def test_resolve_target_rejects_ambiguous_or_disabled_elements():
    desktop, _ = _desktop()
    assert uia.resolve_target("Button", {"control_type": "Button"}, desktop=desktop) is None

    disabled = FakeDesktop(FakeControl("Win", children=[FakeControl("Go", automation_id="go", enabled=False)]))
    assert uia.resolve_target("Go", {}, desktop=disabled) is None


def test_invoke_and_focus_the_resolved_element():
    desktop, save = _desktop()
    selector = uia.resolve_target("Save", {}, desktop=desktop).selector

    uia.invoke(selector, desktop=desktop)
    uia.invoke(selector, focus_only=True, desktop=desktop)

    assert (save.invoked, save.focused) == (1, 1)


def test_invoke_requires_a_stable_selector():
    desktop, _ = _desktop()

    with pytest.raises(ValueError):
        uia.invoke({"name": "Save"}, desktop=desktop)
    with pytest.raises(LookupError):
        uia.invoke({"automation_id": "missing"}, desktop=desktop)


def test_comtypes_cache_is_prepared_in_a_writable_dir(tmp_path):
    assert uia.prepare_comtypes_cache(tmp_path / "cache") == (tmp_path / "cache").resolve()
    assert (tmp_path / "cache").is_dir()


class SizedControl(FakeControl):
    """A control with its own rectangle, so "the smallest match" can be told apart."""

    def __init__(self, text, *, width=100, **kwargs):
        super().__init__(text, **kwargs)
        self._rect = SimpleNamespace(left=0, top=0, right=width, bottom=20)

    def rectangle(self):
        return self._rect


def test_the_whole_name_beats_a_fragment_and_the_smallest_match_wins():
    # "Documents" answers in several places at once; the lookup used to give up on all of them.
    exact = SizedControl("Documents", automation_id="tree-item", width=100)
    inside = SizedControl("Documents", automation_id="breadcrumb", width=400)
    longer = SizedControl("My Documents Backup", automation_id="other", width=100)
    desktop = FakeDesktop(FakeControl("Explorer", control_type="Window", children=[longer, inside, exact]))

    target = uia.resolve_target("Documents", {}, desktop=desktop)

    assert target.selector["automation_id"] == "tree-item"


def test_two_equally_good_candidates_leave_the_choice_to_the_screen():
    first = SizedControl("Documents", automation_id="a", width=100)
    second = SizedControl("Documents", automation_id="b", width=100)
    desktop = FakeDesktop(FakeControl("Explorer", control_type="Window", children=[first, second]))

    assert uia.resolve_target("Documents", {}, desktop=desktop) is None  # the caller then looks


def test_the_control_type_the_planner_asked_for_breaks_a_tie():
    row = SizedControl("Documents", automation_id="row", control_type="ListItem")
    item = SizedControl("Documents", automation_id="item", control_type="TreeItem")
    desktop = FakeDesktop(FakeControl("Explorer", control_type="Window", children=[row, item]))

    target = uia.resolve_target("Documents", {"control_type": "TreeItem"}, desktop=desktop)

    assert target.selector["automation_id"] == "item"
