"""FARA client: prompt, request shape and tool-call → Action translation."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from vilagent.actions import ActionKind
from vilagent.vision import fara as fara_module
from vilagent.vision.fara import FaraVisionActionProvider, UnusableReply, _build_fara_system_prompt, _collapse_consecutive_roles, to_action


def test_collapse_consecutive_roles_merges_user_turns_for_strict_vllm():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "act1"},
        {"role": "user", "content": "<tool_response>ok</tool_response>"},
        {"role": "user", "content": [{"type": "text", "text": "next"}, {"type": "image_url", "image_url": {"url": "x"}}]},
    ]
    out = _collapse_consecutive_roles(messages)

    assert [m["role"] for m in out] == ["system", "assistant", "user"]
    assert isinstance(out[-1]["content"], list) and len(out[-1]["content"]) == 3


def test_pointer_calls_become_coordinate_actions():
    action = to_action("computer_use", {"action": "double_click", "coordinate": [10, 20]}, "open it")

    assert action.kind == ActionKind.double_click
    assert action.point == (10, 20)
    assert action.thought == "open it"


def test_pointer_call_without_coordinate_is_rejected():
    with pytest.raises(ValueError):
        to_action("computer_use", {"action": "left_click"})


@pytest.mark.parametrize(
    ("arguments", "kind", "args"),
    [
        ({"action": "type", "text": "value", "coordinate": [10, 20]}, ActionKind.type_text, {"text": "value"}),
        ({"action": "key", "keys": ["ENTER"], "coordinate": [10, 20]}, ActionKind.hotkey, {"keys": ["ENTER"]}),
        ({"action": "wait", "time": 2}, ActionKind.wait, {"time": 2}),
        ({"action": "finish_step", "status": "success"}, ActionKind.finish, {"status": "success"}),
        ({"action": "terminate", "status": "failure"}, ActionKind.finish, {"status": "failure"}),
    ],
)
def test_non_pointer_calls_drop_coordinates(arguments, kind, args):
    action = to_action("computer_use", arguments)

    assert action.kind == kind
    assert action.target is None
    assert action.args == args


def test_scroll_keeps_optional_coordinate_and_amount():
    assert to_action("computer_use", {"action": "scroll", "pixels": -300}).args == {"amount": -300}
    assert to_action("computer_use", {"action": "scroll", "pixels": 120, "coordinate": [5, 6]}).point == (5, 6)


def test_browser_navigation_calls():
    visit = to_action("browser_action", {"action": "visit_url", "url": "https://example.com"})
    search = to_action("computer_use", {"action": "web_search", "query": "cats"})

    assert (visit.kind, visit.args) == (ActionKind.browser_action, {"action": "visit_url", "url": "https://example.com"})
    assert search.args == {"action": "web_search", "query": "cats"}


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
        to_action("computer_use", {"action": "teleport"})


def test_prompt_reserves_coordinates_for_pointer_actions():
    prompt = _build_fara_system_prompt("native", 2)

    assert "screenshot coordinates only for pointer actions" in prompt
    assert "For type and key actions" in prompt
    assert "popups, ads, cookie banners" in prompt
    assert "smallest safe action" in prompt


class _FakeModel:
    """Stands in for the connection's chat model: it answers, and reports its usage."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    async def ainvoke(self, messages):
        self.seen.append(messages)
        content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return AIMessage(content=content, usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})


def _provider(*replies: str) -> tuple[FaraVisionActionProvider, _FakeModel]:
    model = _FakeModel(*replies)
    return FaraVisionActionProvider(model, "microsoft/Fara-7B"), model


def test_get_next_action_parses_thought_and_tool_call():
    provider, model = _provider('I see the Save button.\n<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[3,4]}}</tool_call>')

    action, history = asyncio.run(provider.get_next_action(instruction="save", image_base64="AAA", chat_history=[]))

    assert action.kind == ActionKind.click and action.point == (3, 4)
    assert action.thought == "I see the Save button."
    assert provider.request_count == 1 and provider.total_tokens == 7
    assert model.seen[0][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "tool_response" in history[-1]["content"][0]["text"]


def test_reply_without_tool_call_is_a_failed_finish():
    provider, _ = _provider("I am not sure.")

    action, _ = asyncio.run(provider.get_next_action(instruction="save", image_base64="AAA", chat_history=[]))

    assert action.kind == ActionKind.finish
    assert action.args["status"] == "failure"


def test_a_provider_without_a_connection_returns_nothing():
    provider = FaraVisionActionProvider(None)

    assert asyncio.run(provider.get_next_action(instruction="x", image_base64="", chat_history=[])) == (None, None)


@pytest.mark.parametrize(
    ("arguments", "kind", "args"),
    [
        # The names FARA really sends instead of the ones in the schema.
        ({"action": "type_text", "text": "hello"}, ActionKind.type_text, {"text": "hello"}),
        ({"action": "input_text", "content": "hello"}, ActionKind.type_text, {"text": "hello"}),
        ({"action": "click", "coordinate": [4, 5]}, ActionKind.click, {}),
        ({"action": "press", "key": "Enter"}, ActionKind.hotkey, {"keys": "Enter"}),
        ({"action": "done", "status": "success"}, ActionKind.finish, {"status": "success"}),
        ({"action": "visit", "url": "https://example.com"}, ActionKind.browser_action, {"action": "visit_url", "url": "https://example.com"}),
        ({"action": "search", "query": "cats"}, ActionKind.browser_action, {"action": "web_search", "query": "cats"}),
        ({"action": "back"}, ActionKind.browser_action, {"action": "history_back"}),
    ],
)
def test_the_names_fara_actually_uses_are_understood(arguments, kind, args):
    action = to_action("computer_use", arguments)

    assert action.kind == kind
    assert action.args == args


def test_scroll_by_name_or_direction_gets_a_signed_amount():
    assert to_action("computer_use", {"action": "scroll_down"}).args == {"amount": -400}
    assert to_action("computer_use", {"action": "scroll_up", "pixels": 250}).args == {"amount": 250}
    assert to_action("computer_use", {"action": "scroll", "direction": "down", "amount": 150}).args == {"amount": -150}


def test_a_reply_that_cannot_be_used_comes_back_with_a_correction(monkeypatch):
    fara, _ = _provider("")
    monkeypatch.setattr(fara, "_complete", _reply('Teleporting there.\n<tool_call>{"name":"computer_use","arguments":{"action":"teleport"}}</tool_call>'))

    with pytest.raises(UnusableReply) as unusable:
        asyncio.run(fara.get_next_action(instruction="go", image_base64="x", chat_history=[], environment="browser"))

    # The next turn tells FARA what went wrong and which actions exist, instead of repeating.
    correction = str(unusable.value.history[-1]["content"])
    assert "teleport" in str(unusable.value) and "could not be used" in correction
    assert "visit_url" in correction and "left_click" in correction


def _reply(content: str):
    async def complete(messages):
        return content

    return complete


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "left_click", "coordinate": [640, 380]},
        {"action": "left_click", "coordinate": "640,380"},
        {"action": "left_click", "coordinate": {"x": 640, "y": 380}},
        {"action": "left_click", "coordinate": [[640, 380]]},
        {"action": "left_click", "position": [640, 380]},
        {"action": "left_click", "point": "(640, 380)"},
        {"action": "left_click", "x": "640", "y": "380"},
        {"action": "left_click", "target": {"coordinate": [640, 380]}},
    ],
)
def test_a_click_is_understood_however_the_coordinate_is_written(arguments):
    # "left_click without a coordinate" cost a whole run: FARA does not spell this the same way twice.
    assert to_action("computer_use", arguments).point == (640, 380)


def test_a_click_with_nothing_to_aim_at_is_still_refused():
    with pytest.raises(ValueError, match="without a coordinate"):
        to_action("computer_use", {"action": "left_click", "coordinate": []})


def test_the_correction_shows_a_usable_click(monkeypatch):
    fara, _ = _provider("")
    monkeypatch.setattr(fara, "_complete", _reply('Closing it.\n<tool_call>{"name":"computer_use","arguments":{"action":"left_click"}}</tool_call>'))

    with pytest.raises(UnusableReply) as unusable:
        asyncio.run(fara.get_next_action(instruction="close it", image_base64="x", chat_history=[], environment="native"))

    correction = str(unusable.value.history[-1]["content"])
    assert "without a coordinate" in correction and '"coordinate":[640,380]' in correction


def test_an_unusable_reply_is_logged_without_what_was_typed():
    reply = 'Typing it.\n<tool_call>{"name":"computer_use","arguments":{"action":"type","text":"my password"}}</tool_call>'

    logged = fara_module.masked_call(reply)

    assert "my password" not in logged and "«typed text»" in logged and logged.startswith('{"name"')
