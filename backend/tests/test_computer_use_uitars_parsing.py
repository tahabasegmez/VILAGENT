"""Pure-logic tests for the UI-TARS action-space parser.

UI-TARS emits ``Thought: ... Action: name(args)`` rather than FARA's
``<tool_call>`` JSON. These lock the parsing + mapping onto typed ActionCommands
without touching the live endpoint.
"""

from __future__ import annotations

import pytest

from vilagent.computer_use.models import ActionKind
from vilagent.computer_use.uitars import (
    UiTarsVisionActionProvider,
    _parse_point,
    _parse_uitars_action,
    _split_thought_action,
)
from vilagent.config.computer_use_config import ComputerUseVisionModelConfig


def _provider() -> UiTarsVisionActionProvider:
    return UiTarsVisionActionProvider(
        ComputerUseVisionModelConfig(),
        base_url="http://x/v1",
        api_key=None,
        model_name="UI-TARS-1.5-7B",
    )


def test_split_thought_action():
    thought, action = _split_thought_action("Thought: focus the bar\nAction: click(start_box='(10,20)')")
    assert thought == "focus the bar"
    assert action == "click(start_box='(10,20)')"


@pytest.mark.parametrize(
    ("text", "name", "args"),
    [
        ("click(start_box='(10,20)')", "click", {"start_box": "(10,20)"}),
        ("hotkey(key='ctrl c')", "hotkey", {"key": "ctrl c"}),
        ("type(content='hello world')", "type", {"content": "hello world"}),
        ("scroll(start_box='(5,5)', direction='down')", "scroll", {"start_box": "(5,5)", "direction": "down"}),
        ("finished(content='done')", "finished", {"content": "done"}),
        ("wait()", "wait", {}),
        ("finished('done')", "finished", {"content": "done"}),
    ],
)
def test_parse_uitars_action(text, name, args):
    parsed = _parse_uitars_action(text)
    assert parsed is not None
    assert parsed[0] == name
    for key, value in args.items():
        assert parsed[1][key] == value


def test_parse_point_handles_point_and_box():
    assert _parse_point("(100,200)") == (100.0, 200.0)
    assert _parse_point("(10,20,30,40)") == (20.0, 30.0)  # box center
    assert _parse_point(None) is None


def test_map_click_produces_coordinate_target():
    cmd = _provider()._parse_action("click(start_box='(100,200)')", "go")
    assert cmd.kind == ActionKind.click
    assert cmd.target is not None
    assert cmd.target.selector["point"] == [100, 200]
    assert cmd.postconditions  # screen-change postcondition attached


def test_map_double_and_right_click():
    assert _provider()._parse_action("left_double(start_box='(1,2)')", "").kind == ActionKind.double_click
    assert _provider()._parse_action("right_single(start_box='(1,2)')", "").kind == ActionKind.right_click


def test_map_hotkey_and_type():
    hk = _provider()._parse_action("hotkey(key='ctrl c')", "")
    assert hk.kind == ActionKind.hotkey and hk.args["keys"] == "ctrl c"
    ty = _provider()._parse_action("type(content='abc')", "")
    assert ty.kind == ActionKind.type_text and ty.args["text"] == "abc"


def test_map_finished_terminates_success():
    cmd = _provider()._parse_action("finished(content='ok')", "")
    assert cmd.args["action"] == "terminate"
    assert cmd.args["status"] == "success"


def test_map_drag_has_end_point():
    cmd = _provider()._parse_action("drag(start_box='(1,2)', end_box='(3,4)')", "")
    assert cmd.kind == ActionKind.drag
    assert cmd.target.selector["point"] == [1, 2]
    assert cmd.args["end"] == [3, 4]


def test_unparseable_action_fails_closed():
    cmd = _provider()._parse_action("garbage!!!", "")
    assert cmd.args["action"] == "terminate"
    assert cmd.args["status"] == "failure"
