"""Client for FARA, the vision action model (an OpenAI-compatible vLLM endpoint)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

from vilagent.actions import Action, ActionKind, Target
from vilagent.agents.common import message_text
from vilagent.runs.budget import charge, count_tokens

logger = logging.getLogger(__name__)

# The action vocabulary: what the tool schema offers FARA, and what the parser accepts back.
BROWSER_ACTIONS = ("visit_url", "web_search", "history_back", "go_forward", "refresh")
DESKTOP_ACTIONS = ("left_click", "right_click", "double_click", "type", "key", "scroll", "mouse_move", "wait", "finish_step")

_FARA_BASE_PROMPT = """You are FARA, a visual computer-use executor for exactly ONE isolated plan step.
Perform only the current step. Never perform a later plan step, a double-check, or extra verification unless it is explicitly part of the current step command.
As soon as the current step's completion criterion is satisfied, immediately return finish_step with status success and perform no more actions — do NOT keep clicking or repeating once the goal is visibly done.
Be persistent: only return finish_step with status failure when the step is GENUINELY impossible after you have actually tried at least one or two different approaches. Never fail just because you are unsure, the first attempt did nothing, or you cannot see the result yet — re-look at the screenshot and try a clearly different action instead.
You have at most {max_actions} actions for this isolated step. On the final allowed action, return an ultimate finish_step success or failure decision. Never exceed the stated action limit.
ALWAYS begin your reply with ONE short, plain sentence (max ~15 words) saying what you SEE and what you will DO next, then on the next line return exactly one <tool_call> JSON object. The sentence is shown to the operator, so make it natural and informative (e.g. mention values you read).

Acting precisely:
* Reason from the CURRENT screenshot, not from an idealized flow. Determine an element's coordinates by consulting the screenshot before you move the cursor.
* Click buttons, links, and icons with the cursor tip in the CENTER of the element; do not click box edges.
* If a click did not take effect (the screen did not change after waiting), adjust the coordinate slightly so the cursor tip visually lands on the element, then click again.
* Use screenshot coordinates only for pointer actions (left_click, right_click, double_click, scroll, mouse_move). For type and key actions, act on the currently focused control and omit coordinates.
* The UI may differ from the ideal: popups, ads, cookie banners, dialogs, loading states, focus mismatch, localized labels, disabled or covered controls. If a recoverable obstruction directly blocks this step, use the smallest safe action to dismiss, wait for, or bypass it (e.g. close an overlay with its X, or key(['Escape'])), then continue the SAME step. Never interact with unrelated content or pursue a different goal.
* If an application or page is still loading, prefer wait then re-check rather than clicking blindly.
{environment_guidance}
You are currently operating in the '{environment}' environment.

<tools>
{tools_json}
</tools>
<tool_call>
{{"name":"computer_use","arguments":{{"action":"left_click","coordinate":[100,200]}}}}
</tool_call>"""


_NATIVE_GUIDANCE = """Native Windows desktop:
* You interact with the visible desktop and app windows. To start an app you usually click its taskbar/desktop icon or use the Start menu; but app launching is normally handled deterministically by the planner, so focus on operating the app that is already open.
* To OPEN an item (a desktop/file-explorer icon, a list row, a file) use the double_click action on it. Use a single left_click to select or to press buttons. If you need a double-click, emit one double_click action rather than two separate clicks.
* To scroll inside a panel or list, mouse_move() over it first, then scroll().
* For menus and dialogs, click the exact control; if a dropdown is open, click the desired item rather than typing."""


_BROWSER_GUIDANCE = """Web browser:
* If the page looks blank, white, or is still loading (spinner, partial content), emit a wait action and look again on the next screenshot — do NOT conclude the page is empty or return finish_step failure; pages often need a moment to render.
* Navigate with browser_action visit_url for a known URL, and web_search for a query — prefer these over manually clicking the address bar.
* When a separate scrollable container overlays the page, mouse_move() over it first, then scroll() to scroll within it.
* If a popup/cookie/consent dialog appears and clicking its X or "Accept"/"Reject" button does not close it, try key(['Escape']).
* On search bars with an auto-suggest popup (locations, recipients, products), after typing you may need to either press Enter to accept the highlighted suggestion or left_click() the suggestion/search button — do not assume typing alone submits.
* For calendar/date widgets, click the arrows to change month and click the day cell; do not type dates into them.
* history_back returns to the previous page."""


# A step ran out of actions without FARA saying it finished: did it actually get done?
# The example deliberately shows "failure" so it does not nudge FARA toward passing its own work.
_VERIFY_PROMPT = """You are FARA, checking ONE condition on the screen. Do NOT act: no clicks, no typing.
Look at the screenshot and decide whether the condition is visibly true right now.
First write ONE short sentence saying what you see that decides it, then on the next line return exactly one <tool_call>:
finish_step with status "success" only if the condition is clearly true on screen, otherwise status "failure" (also when you cannot tell).

<tools>
{tools_json}
</tools>
<tool_call>
{{"name":"computer_use","arguments":{{"action":"finish_step","status":"failure"}}}}
</tool_call>"""


def _fara_tools_json(environment: str) -> str:
    pointer_actions = list(DESKTOP_ACTIONS)
    tools = [
        {
            "name": "computer_use",
            "description": "Mouse and keyboard control.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": pointer_actions},
                    "keys": {"type": "array", "description": "Key names for action=key, e.g. ['Enter'], ['Control','a'], ['Escape']."},
                    "text": {"type": "string", "description": "Text to type for action=type."},
                    "coordinate": {"type": "array", "description": "[x, y] screenshot pixel for pointer actions."},
                    "pixels": {"type": "number", "description": "Scroll amount for action=scroll; positive scrolls up, negative down."},
                    "time": {"type": "number", "description": "Seconds to wait for action=wait."},
                    "status": {"type": "string", "enum": ["success", "failure"], "description": "Outcome for action=finish_step."}
                },
                "required": ["action"]
            }
        }
    ]
    if environment == "browser":
        tools.append({
            "name": "browser_action",
            "description": "Browser navigation actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(BROWSER_ACTIONS)},
                    "url": {"type": "string", "description": "Full URL for action=visit_url."},
                    "query": {"type": "string", "description": "Search query for action=web_search."}
                },
                "required": ["action"]
            }
        })
    return "\\n".join(json.dumps(t) for t in tools)


def _build_fara_system_prompt(environment: str, max_actions: int) -> str:
    environment_guidance = _BROWSER_GUIDANCE if environment == "browser" else _NATIVE_GUIDANCE
    return _FARA_BASE_PROMPT.format(
        max_actions=max_actions,
        environment=environment,
        environment_guidance=environment_guidance,
        tools_json=_fara_tools_json(environment),
    )


_FARA_AUTONOMOUS_PROMPT = """You are FARA, an autonomous computer-use agent. You own the ENTIRE task from start to finish.
Work step by step on your own: observe the current screenshot, decide the single best next action, perform it, observe the result, and continue until the whole task is genuinely done.
ALWAYS begin each turn with ONE short, plain sentence (max ~15 words) saying what you SEE and what you will DO next (mention any values you read), then on the next line return exactly one <tool_call> JSON object per turn. That sentence is shown to the operator.
You have a budget of about {max_actions} actions for the entire task. Use them efficiently; do not waste actions.
Only return finish_step with status success once the COMPLETE task is accomplished and you can see the proof on screen. Do not stop early after a single sub-action — keep going until the overall goal is reached.
Be persistent: only return finish_step with status failure when the task is GENUINELY impossible after you have actually tried at least two different approaches. Never fail just because you are unsure, an attempt did nothing, or a page is still loading — re-look at the screenshot and try a clearly different action instead.

Acting precisely:
* Reason from the CURRENT screenshot, not from an idealized flow. Determine an element's coordinates by consulting the screenshot before you move the cursor.
* Click buttons, links, and icons with the cursor tip in the CENTER of the element; do not click box edges.
* If a click did not take effect (the screen did not change after waiting), adjust the coordinate slightly so the cursor tip visually lands on the element, then click again.
* Use screenshot coordinates only for pointer actions (left_click, right_click, double_click, scroll, mouse_move). For type and key actions, act on the currently focused control and omit coordinates.
* After typing into a field, move focus deliberately (Tab, or click the next field) before typing the next value — never assume focus moved on its own.
* The UI may differ from the ideal: popups, ads, cookie banners, dialogs, loading states, focus mismatch, localized labels, disabled or covered controls. Dismiss or wait out recoverable obstructions (close an overlay with its X, or key(['Escape'])) and continue toward the goal.
* If something is still loading, prefer wait then re-check rather than clicking blindly.
{environment_guidance}
You are currently operating in the '{environment}' environment.

<tools>
{tools_json}
</tools>
<tool_call>
{{"name":"computer_use","arguments":{{"action":"left_click","coordinate":[100,200]}}}}
</tool_call>"""


def _build_fara_autonomous_system_prompt(environment: str, max_actions: int) -> str:
    environment_guidance = _BROWSER_GUIDANCE if environment == "browser" else _NATIVE_GUIDANCE
    return _FARA_AUTONOMOUS_PROMPT.format(
        max_actions=max_actions,
        environment=environment,
        environment_guidance=environment_guidance,
        tools_json=_fara_tools_json(environment),
    )


class FaraVisionActionProvider:
    """The computer-use model: a LangChain chat model that sees the screen and answers an action."""

    def __init__(self, model: BaseChatModel | None, model_name: str = ""):
        self._model = model
        self.model_name = model_name
        # Usage counters, read from the same replies (no extra calls).
        self.request_count = 0
        self.total_tokens = 0

    def is_enabled(self) -> bool:
        return self._model is not None

    async def get_next_action(
        self,
        instruction: str,
        image_base64: str,
        chat_history: list[dict[str, Any]],
        environment: str = "native",
        max_actions: int = 10,
        image_media_type: str = "image/png",
        autonomous: bool = False,
    ) -> tuple[Action | None, list[dict[str, Any]] | None]:
        """Query FARA 7B for the next action to execute in the loop.

        ``autonomous=True`` frames the system prompt around completing the WHOLE
        task end-to-end (FARA owns the full task), instead of one isolated plan step.
        """
        if not self.is_enabled():
            return None, None
        charge("vision")

        if autonomous:
            sys_prompt = _build_fara_autonomous_system_prompt(environment, max_actions)
            continuation = "Next screenshot. Choose the next action, or terminate when the whole task is complete."
        else:
            sys_prompt = _build_fara_system_prompt(environment, max_actions)
            continuation = "Next screenshot. Choose the next action or terminate."
        compact_history = _compact_history(chat_history)
        image_url = f"data:{image_media_type};base64,{image_base64}"
        if not chat_history:
            messages = [{"role": "system", "content": sys_prompt}]
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Task: {instruction}"},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            })
        else:
            messages = [{"role": "system", "content": sys_prompt}, *compact_history]
            label = "Task" if autonomous else "Task step"
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{label}: {instruction}\n{continuation}"},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            })

        content = await self._complete(messages)

        new_history = list(compact_history)
        new_history.append({"role": "assistant", "content": content})
        new_history.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "<tool_response>\n{\"status\": \"success\"}\n</tool_response>"}
            ]
        })

        try:
            return parse_reply(content), new_history
        except ValueError as exc:
            # The same screenshot would get the same unusable reply back; tell FARA what to fix,
            # and log what it really sent (typed text masked) so a new spelling can be seen.
            logger.warning("FARA reply could not be used (%s): %s", exc, masked_call(content))
            raise UnusableReply(str(exc), _with_correction(new_history, str(exc), environment)) from exc

    async def verify(self, criterion: str, image_base64: str, image_media_type: str = "image/png") -> Verification:
        """Ask FARA, in a fresh conversation, whether ``criterion`` is visibly true right now."""
        if not self.is_enabled():
            return Verification(False, "The vision model is disabled.")
        charge("vision")
        messages = [
            {"role": "system", "content": _VERIFY_PROMPT.format(tools_json=_fara_tools_json("native"))},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Condition: {criterion}"},
                    {"type": "image_url", "image_url": {"url": f"data:{image_media_type};base64,{image_base64}"}},
                ],
            },
        ]
        try:
            action = parse_reply(await self._complete(messages))
        except ValueError:
            return Verification(False, "The vision model's verdict could not be read.")
        passed = action.kind == ActionKind.finish and action.args.get("status") != "failure"
        return Verification(passed, action.thought or ("The condition looks satisfied." if passed else "The condition is not visible."))

    async def _complete(self, messages: list[dict[str, Any]]) -> str:
        """One reply from the model, with its usage counted from the same answer."""
        # Strict vLLM chat templates reject consecutive same-role turns (a tool_response user
        # message followed by the new screenshot, or a supervisor nudge); merge them so the
        # conversation alternates and the endpoint does not 400.
        reply = await self._model.ainvoke(_collapse_consecutive_roles(messages))
        self.request_count += 1
        usage = getattr(reply, "usage_metadata", None) or {}
        if total := int(usage.get("total_tokens") or 0):
            self.total_tokens += total
            count_tokens("vision", total)
        return message_text(reply)


@dataclass(frozen=True)
class Verification:
    passed: bool
    reason: str


class UnusableReply(ValueError):
    """FARA's reply could not be turned into an action; ``history`` holds the correction to send."""

    def __init__(self, message: str, history: list[dict[str, Any]]):
        super().__init__(message)
        self.history = history


def _with_correction(history: list[dict[str, Any]], reason: str, environment: str) -> list[dict[str, Any]]:
    allowed = ", ".join(DESKTOP_ACTIONS + (BROWSER_ACTIONS if environment == "browser" else ()))
    corrected = [message for message in history if message.get("role") != "user" or "<tool_response>" not in str(message.get("content"))]
    corrected.append({
        "role": "user",
        "content": (
            f"<supervisor>\nThat reply could not be used: {reason}.\n"
            f"Return exactly one <tool_call> whose action is one of: {allowed}.\n"
            'A click names the screenshot pixel it aims at, like this: '
            '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[640,380]}}</tool_call>\n'
            "</supervisor>"
        ),
    })
    return corrected


def masked_call(content: str) -> str:
    """The tool call from a reply, as a log may keep it: anything typed is replaced."""
    call = re.search(r"<tool_call>\s*({.*?})\s*</tool_call>", content, re.DOTALL)
    text = (call.group(1) if call else content).strip()[:400]
    return re.sub(r'("text"\s*:\s*)"(?:[^"\\]|\\.)*"', r'\1"«typed text»"', text)


def parse_reply(content: str) -> Action:
    """FARA's reply: one narration sentence, then a <tool_call>. Raises ValueError for unusable calls."""
    thought_match = re.search(r"^(.*?)(?:<tool_call>|$)", content, re.DOTALL)
    thought = thought_match.group(1).strip() if thought_match else ""
    match = re.search(r"<tool_call>\s*({.*?})\s*</tool_call>", content, re.DOTALL)
    if not match:
        return Action(kind=ActionKind.finish, args={"status": "failure", "reason": "no tool_call in the reply"}, thought=thought)
    try:
        tool_call = json.loads(match.group(1))
    except json.JSONDecodeError:
        return Action(kind=ActionKind.finish, args={"status": "failure", "reason": "invalid tool_call JSON"}, thought=thought)
    return to_action(tool_call.get("name", "computer_use"), tool_call.get("arguments") or {}, thought)


_POINTER = {"left_click": ActionKind.click, "right_click": ActionKind.right_click, "double_click": ActionKind.double_click}

# A 7B model does not always use the exact names from the tool schema ("type_text" for "type",
# "visit" for "visit_url"); the obvious variants mean the same thing, so accept them instead of
# throwing the turn away.
_ALIASES = {
    "click": "left_click",
    "leftclick": "left_click",
    "left_single_click": "left_click",
    "tap": "left_click",
    "rightclick": "right_click",
    "context_click": "right_click",
    "doubleclick": "double_click",
    "left_double_click": "double_click",
    "type_text": "type",
    "input_text": "type",
    "insert_text": "type",
    "enter_text": "type",
    "write": "type",
    "write_text": "type",
    "keypress": "key",
    "key_press": "key",
    "press": "key",
    "press_key": "key",
    "hotkey": "key",
    "move_mouse": "mouse_move",
    "hover": "mouse_move",
    "sleep": "wait",
    "pause": "wait",
    "finish": "finish_step",
    "terminate": "finish_step",
    "done": "finish_step",
    "complete": "finish_step",
    "stop": "finish_step",
    "visit": "visit_url",
    "goto": "visit_url",
    "go_to": "visit_url",
    "navigate": "visit_url",
    "open_url": "visit_url",
    "browse": "visit_url",
    "search": "web_search",
    "search_web": "web_search",
    "google": "web_search",
    "back": "history_back",
    "go_back": "history_back",
    "forward": "go_forward",
    "reload": "refresh",
}
# Scrolling by name instead of by a signed amount ("positive scrolls up" in the schema).
_SCROLL_BY_NAME = {"scroll_up": 1, "page_up": 1, "scroll_down": -1, "page_down": -1}
_SCROLL_DEFAULT = 400


def canonical_action(name: Any) -> str:
    """The schema's name for what FARA asked for ("" when it is nothing we can run)."""
    key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in _SCROLL_BY_NAME:
        return "scroll"
    known = set(_POINTER) | set(DESKTOP_ACTIONS) | set(BROWSER_ACTIONS)
    return key if key in known else _ALIASES.get(key, "")


def _first(arguments: dict[str, Any], *keys: str) -> Any:
    return next((arguments[key] for key in keys if arguments.get(key) not in (None, "")), None)


# Where a click can hide its coordinate: FARA does not spell this the same way every turn.
_POINT_KEYS = ("coordinate", "coordinates", "point", "position", "location", "at", "xy", "center", "centre", "target")
_NUMBERS = re.compile(r"-?\d+(?:\.\d+)?")


def _target(arguments: dict[str, Any]) -> Target | None:
    """The click point however the model wrote it: [x, y], {"x": …, "y": …}, "x,y", or x and y."""
    point = _first(arguments, *_POINT_KEYS)
    if isinstance(point, dict):
        point = _first(point, *_POINT_KEYS) or [point.get("x"), point.get("y")]
    if isinstance(point, (list, tuple)) and len(point) == 1 and isinstance(point[0], (list, tuple, str)):
        point = point[0]  # [[x, y]]
    if isinstance(point, str):
        point = _NUMBERS.findall(point)
    if point is None and arguments.get("x") is not None and arguments.get("y") is not None:
        point = [arguments["x"], arguments["y"]]
    if isinstance(point, (list, tuple)) and len(point) == 2:
        try:
            return Target.at(round(float(point[0])), round(float(point[1])))
        except (TypeError, ValueError):
            return None
    return None


def _scroll_amount(raw_name: str, arguments: dict[str, Any]) -> int:
    pixels = _first(arguments, "pixels", "amount", "distance", "clicks")
    direction = _SCROLL_BY_NAME.get(str(raw_name or "").strip().lower().replace(" ", "_"))
    if direction is None:
        down = str(_first(arguments, "direction") or "").lower().startswith("d")
        direction = -1 if down else 1 if _first(arguments, "direction") else 0
    try:
        size = abs(int(float(pixels))) if pixels is not None else _SCROLL_DEFAULT
    except (TypeError, ValueError):
        size = _SCROLL_DEFAULT
    if direction:  # the name (or a direction argument) decides the sign
        return direction * size
    try:
        return int(float(pixels)) if pixels is not None else 0
    except (TypeError, ValueError):
        return 0


def to_action(tool_name: str, arguments: dict[str, Any], thought: str | None = None) -> Action:
    """Translate one FARA tool call into an :class:`Action`.

    Raises ValueError for calls that cannot be executed (an unknown action, a click with no
    coordinate); the caller turns that into a correction FARA sees on its next turn.
    """
    raw = arguments.get("action")
    name = canonical_action(raw) or (canonical_action(tool_name) if tool_name != "computer_use" else "")
    if name in BROWSER_ACTIONS or (tool_name == "browser_action" and name):
        args: dict[str, Any] = {"action": name}
        if url := _first(arguments, "url", "link", "address"):
            args["url"] = url
        if query := _first(arguments, "query", "search_query", "text"):
            args["query"] = query
        return Action(kind=ActionKind.browser_action, args=args, thought=thought)

    target = _target(arguments)

    if name in _POINTER:
        if target is None:
            raise ValueError(f"FARA sent {name} without a coordinate")
        return Action(kind=_POINTER[name], target=target, thought=thought)
    if name == "scroll":
        return Action(kind=ActionKind.scroll, target=target, args={"amount": _scroll_amount(raw, arguments)}, thought=thought)
    if name == "mouse_move":
        return Action(kind=ActionKind.mouse_move, target=target, thought=thought)
    if name == "type":
        return Action(kind=ActionKind.type_text, args={"text": _first(arguments, "text", "content", "value", "input") or ""}, thought=thought)
    if name == "key":
        return Action(kind=ActionKind.hotkey, args={"keys": _first(arguments, "keys", "key", "keystroke", "text") or []}, thought=thought)
    if name == "wait":
        return Action(kind=ActionKind.wait, args={"time": _first(arguments, "time", "seconds", "duration") or 1}, thought=thought)
    if name == "finish_step":
        return Action(kind=ActionKind.finish, args={"status": arguments.get("status", "success")}, thought=thought)
    raise ValueError(f"FARA sent an unknown action: {raw!r}")


def _content_to_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": str(content or "")}]


def _collapse_consecutive_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent same-role messages (except system) into one multi-part message.

    Guarantees the conversation alternates user/assistant, which strict vLLM chat
    templates require — otherwise consecutive user turns trigger a 400 Bad Request.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if out and out[-1].get("role") == role and role != "system":
            merged = _content_to_parts(out[-1]["content"]) + _content_to_parts(content)
            out[-1]["content"] = merged
        else:
            out.append({"role": role, "content": content})
    return out


def _compact_history(history: list[dict[str, Any]], *, max_messages: int = 4) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for message in history[-max_messages:]:
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
            compact.append({"role": message.get("role", "user"), "content": "\n".join(text_parts)})
        else:
            compact.append({"role": message.get("role", "assistant"), "content": str(content or "")})
    return compact
