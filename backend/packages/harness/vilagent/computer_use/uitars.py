"""UI-TARS vision provider.

UI-TARS does not emit FARA's ``<tool_call>`` JSON; it uses a ``Thought: ... ``
``Action: name(args)`` action space. This module parses that format and maps it
onto the same typed ``ActionCommand`` surface the FARA provider produces, so the
plan-execute vision loop can drive either model unchanged.

Coordinates are treated as absolute screen pixels (the UI-TARS-1.5 convention,
since we send the full-resolution screenshot). ``coordinate_scale`` is exposed for
older 0-1000-normalized checkpoints.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    Condition,
    ConditionOperator,
    Rect,
    TargetRef,
    TargetStrategy,
)
from vilagent.config.computer_use_config import ComputerUseVisionModelConfig

# Keep this static, ASCII, and short: UI-TARS is a small model and every token
# of system prompt is paid on every action.
_UITARS_PROMPT = """You are UI-TARS, a GUI operator. Do ONLY the current step.
Output exactly:
Thought: <one short line>
Action: <one action>

Action space ({environment} environment):
click(start_box='(x,y)')
left_double(start_box='(x,y)')
right_single(start_box='(x,y)')
drag(start_box='(x,y)', end_box='(x,y)')
hotkey(key='ctrl c')           # space-separated keys
type(content='text')           # types into the focused field
scroll(start_box='(x,y)', direction='down')   # up|down|left|right
wait()
finished(content='done')       # the step's completion criterion is satisfied

Coordinates are absolute screen pixels. Use finished() the moment the criterion
holds; never do a later step, a re-check, or extra verification.
Reason from the current screenshot. If a popup, dialog, banner, loading state,
focus mismatch, localized label, disabled control, or covered target directly
blocks this step, use the smallest safe action to dismiss, wait for, or bypass
it, then continue this same step. Do not pursue a different goal. You have at most
{max_actions} actions for this step."""


def build_uitars_prompt(environment: str, max_actions: int) -> str:
    return _UITARS_PROMPT.format(environment=environment, max_actions=max_actions)


class UiTarsVisionActionProvider:
    """Query a UI-TARS endpoint and map its action-space output to ActionCommands."""

    def __init__(self, config: ComputerUseVisionModelConfig, *, base_url: str, api_key: str | None, model_name: str, coordinate_scale: float = 1.0):
        self._config = config
        self._base_url = base_url
        self._api_key = api_key
        self._model_name = model_name
        self._coordinate_scale = coordinate_scale

    def is_enabled(self) -> bool:
        return bool(self._base_url)

    async def get_next_action(
        self,
        instruction: str,
        image_base64: str,
        chat_history: list[dict[str, Any]],
        environment: str = "native",
        max_actions: int = 4,
        image_media_type: str = "image/png",
    ) -> tuple[ActionCommand | None, list[dict[str, Any]] | None]:
        if not self.is_enabled():
            return None, None

        sys_prompt = build_uitars_prompt(environment, max_actions)
        compact_history = _compact_history(chat_history)
        user_text = f"Step: {instruction}" if not chat_history else f"Step: {instruction}\nNew screenshot. Choose the next action or finished()."
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}, *compact_history]
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:{image_media_type};base64,{image_base64}"}},
                ],
            }
        )

        payload = {"model": self._model_name, "messages": messages, "temperature": 0.0, "max_tokens": 256}
        headers = {"Accept": "application/json", "ngrok-skip-browser-warning": "true"}
        if self._api_key and self._api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url.rstrip('/')}/chat/completions"

        async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"].get("content", "") or ""
        new_history = list(compact_history)
        new_history.append({"role": "assistant", "content": content})

        thought, action_text = _split_thought_action(content)
        command = self._parse_action(action_text, thought)
        return command, new_history

    def _parse_action(self, action_text: str, thought: str) -> ActionCommand:
        parsed = _parse_uitars_action(action_text)
        if parsed is None:
            return _terminate("failure", reason="unparseable_action", thought=thought)
        name, args = parsed

        if name == "finished":
            return _terminate("success", reason=args.get("content", "finished"), thought=thought)
        if name in ("wait",):
            return ActionCommand(action_id="uitars-wait", session_id="", kind=ActionKind.browser_action, args={"action": "wait", "thought": thought})
        if name in ("call_user", "fail", "error"):
            return _terminate("failure", reason=name, thought=thought)

        if name == "type":
            return ActionCommand(action_id="uitars-type", session_id="", kind=ActionKind.type_text, args={"text": args.get("content", ""), "thought": thought})
        if name == "hotkey":
            return ActionCommand(action_id="uitars-hotkey", session_id="", kind=ActionKind.hotkey, args={"keys": args.get("key", ""), "thought": thought})

        if name in ("click", "left_single", "left_double", "right_single", "right_click", "double_click"):
            kind = ActionKind.click
            if name in ("left_double", "double_click"):
                kind = ActionKind.double_click
            elif name in ("right_single", "right_click"):
                kind = ActionKind.right_click
            target = self._coordinate_target(args.get("start_box"))
            return ActionCommand(
                action_id="uitars-click",
                session_id="",
                kind=kind,
                target=target,
                args={"thought": thought},
                postconditions=_screen_change_postcondition() if target is not None else [],
            )
        if name == "scroll":
            target = self._coordinate_target(args.get("start_box"))
            return ActionCommand(
                action_id="uitars-scroll",
                session_id="",
                kind=ActionKind.scroll,
                target=target,
                args={"direction": str(args.get("direction", "down")).lower(), "thought": thought},
            )
        if name == "drag":
            start = self._coordinate_target(args.get("start_box"))
            end = _parse_point(args.get("end_box"))
            drag_args: dict[str, Any] = {"thought": thought}
            if end is not None:
                drag_args["end"] = [int(end[0] * self._coordinate_scale), int(end[1] * self._coordinate_scale)]
            return ActionCommand(action_id="uitars-drag", session_id="", kind=ActionKind.drag, target=start, args=drag_args)

        # Unknown verb: fail closed instead of guessing.
        return _terminate("failure", reason=f"unknown_action:{name}", thought=thought)

    def _coordinate_target(self, raw_box: Any) -> TargetRef | None:
        point = _parse_point(raw_box)
        if point is None:
            return None
        x = int(point[0] * self._coordinate_scale)
        y = int(point[1] * self._coordinate_scale)
        return TargetRef(
            strategy=TargetStrategy.coordinate,
            confidence=1.0,
            observation_id="",
            bounds=Rect(x=x, y=y, width=1, height=1),
            selector={"point": [x, y]},
        )


def _terminate(status: str, *, reason: str, thought: str) -> ActionCommand:
    return ActionCommand(
        action_id="uitars-finish",
        session_id="",
        kind=ActionKind.browser_action,
        args={"action": "terminate", "status": status, "reason": reason, "thought": thought},
    )


def _screen_change_postcondition() -> list[Condition]:
    return [
        Condition(
            kind="screen_changed",
            operator=ConditionOperator.changed,
            description="The visible desktop should change after the UI-TARS coordinate action.",
        )
    ]


def _split_thought_action(content: str) -> tuple[str, str]:
    thought = ""
    action_text = content
    thought_match = re.search(r"Thought:\s*(.*?)(?:\n\s*Action:|$)", content, re.DOTALL | re.IGNORECASE)
    if thought_match:
        thought = thought_match.group(1).strip()
    action_match = re.search(r"Action:\s*(.*)", content, re.DOTALL | re.IGNORECASE)
    if action_match:
        action_text = action_match.group(1).strip()
    return thought, action_text


def _parse_uitars_action(action_text: str) -> tuple[str, dict[str, Any]] | None:
    """Parse ``name(arg='...', arg2='...')`` -> (name, {arg: value})."""
    text = action_text.strip()
    # take the first call only
    call = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", text, re.DOTALL)
    if not call:
        # bare verb like "wait" or "finished"
        bare = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", text)
        if bare:
            return bare.group(1).lower(), {}
        return None
    name = call.group(1).lower()
    arg_blob = call.group(2)
    args: dict[str, Any] = {}
    for key, _q, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"])(.*?)\2", arg_blob, re.DOTALL):
        args[key] = value
    # positional single arg fallback, e.g. finished('done')
    if not args:
        positional = re.match(r"\s*(['\"])(.*)\1\s*$", arg_blob, re.DOTALL)
        if positional:
            args["content"] = positional.group(2)
    return name, args


def _parse_point(raw_box: Any) -> tuple[float, float] | None:
    if raw_box is None:
        return None
    numbers = re.findall(r"-?\d+(?:\.\d+)?", str(raw_box))
    if len(numbers) >= 4:
        # bounding box -> center
        x1, y1, x2, y2 = (float(numbers[0]), float(numbers[1]), float(numbers[2]), float(numbers[3]))
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0
    if len(numbers) >= 2:
        return float(numbers[0]), float(numbers[1])
    return None


def _compact_history(history: list[dict[str, Any]], *, max_messages: int = 6) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for message in history[-max_messages:]:
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
            compact.append({"role": message.get("role", "user"), "content": "\n".join(text_parts)})
        else:
            compact.append({"role": message.get("role", "assistant"), "content": str(content or "")})
    return compact
