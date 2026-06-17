"""FARA Vision provider for multi-round end-to-end plan execution."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from vilagent.computer_use.models import ActionCommand, ActionKind, Condition, ConditionOperator, TargetRef, TargetStrategy, Rect
from vilagent.config.computer_use_config import ComputerUseFaraModelConfig


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
* Navigate with browser_action visit_url for a known URL, and web_search for a query — prefer these over manually clicking the address bar.
* When a separate scrollable container overlays the page, mouse_move() over it first, then scroll() to scroll within it.
* If a popup/cookie/consent dialog appears and clicking its X or "Accept"/"Reject" button does not close it, try key(['Escape']).
* On search bars with an auto-suggest popup (locations, recipients, products), after typing you may need to either press Enter to accept the highlighted suggestion or left_click() the suggestion/search button — do not assume typing alone submits.
* For calendar/date widgets, click the arrows to change month and click the day cell; do not type dates into them.
* history_back returns to the previous page."""


def _fara_tools_json(environment: str) -> str:
    pointer_actions = ["key", "type", "mouse_move", "left_click", "right_click", "double_click", "scroll", "wait", "finish_step"]
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
                    "action": {"type": "string", "enum": ["visit_url", "web_search", "history_back", "go_back", "go_forward", "refresh"]},
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
    """End-to-end vision action provider that queries FARA 7B."""

    def __init__(self, config: ComputerUseFaraModelConfig):
        self._config = config
        # Usage counters bound to FARA's existing HTTP responses (no extra calls).
        self.request_count = 0
        self.total_tokens = 0

    def is_enabled(self) -> bool:
        return self._config.enabled and bool(self._config.base_url)

    async def get_next_action(
        self,
        instruction: str,
        image_base64: str,
        chat_history: list[dict[str, Any]],
        environment: str = "native",
        max_actions: int = 10,
        image_media_type: str = "image/png",
        autonomous: bool = False,
    ) -> tuple[ActionCommand | None, dict[str, Any] | None]:
        """Query FARA 7B for the next action to execute in the loop.

        ``autonomous=True`` frames the system prompt around completing the WHOLE
        task end-to-end (FARA owns the full task), instead of one isolated plan step.
        """
        if not self.is_enabled():
            return None, None

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

        # Strict vLLM chat templates reject consecutive same-role turns (which arise when
        # a tool_response user message is followed by the new screenshot user message, or
        # after a supervisor/retry nudge). Merge consecutive same-role messages so the
        # conversation strictly alternates and the endpoint does not 400.
        messages = _collapse_consecutive_roles(messages)

        payload = {
            "model": self._config.model_name,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 512,
        }

        headers = {
            "Accept": "application/json",
            "ngrok-skip-browser-warning": "true",
        }
        if self._config.api_key and self._config.api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        url = f"{self._config.base_url.rstrip('/')}/chat/completions"

        async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                # Surface the endpoint's real reason (vLLM/ngrok) instead of a bare status.
                body = (response.text or "")[:400]
                raise RuntimeError(f"FARA endpoint {response.status_code}: {body}")
            data = response.json()

        # Bind usage to the response we already received (no extra request).
        self.request_count += 1
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            try:
                self.total_tokens += int(usage.get("total_tokens") or 0)
            except (TypeError, ValueError):
                pass

        choice = data["choices"][0]["message"]
        content = choice.get("content", "")

        new_history = list(compact_history)
        new_history.append({"role": "assistant", "content": content})
        new_history.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "<tool_response>\n{\"status\": \"success\"}\n</tool_response>"}
            ]
        })

        # Extract thought
        thought_match = re.search(r"^(.*?)(?:<tool_call>|$)", content, re.DOTALL)
        thought = thought_match.group(1).strip() if thought_match else ""

        # Parse <tool_call>
        match = re.search(r"<tool_call>\s*({.*?})\s*</tool_call>", content, re.DOTALL)
        if not match:
            return ActionCommand(
                action_id="fara-fallback",
                session_id="",
                kind=ActionKind.browser_action,
                args={"action": "terminate", "status": "failure", "reason": "No tool_call found", "thought": thought},
            ), new_history

        try:
            tool_call = json.loads(match.group(1))
            args = tool_call.get("arguments", {})
        except json.JSONDecodeError:
            return ActionCommand(
                action_id="fara-fallback",
                session_id="",
                kind=ActionKind.browser_action,
                args={"action": "terminate", "status": "failure", "reason": "Invalid JSON"},
            ), new_history

        action_name = args.get("action")
        tool_name = tool_call.get("name", "computer_use")
        
        args["thought"] = thought
        mapped_command = self._map_to_vilagent_action(tool_name, action_name, args)
        return mapped_command, new_history

    def _map_to_vilagent_action(self, tool_name: str, action_name: str | None, args: dict[str, Any]) -> ActionCommand:
        if tool_name == "bash":
            return ActionCommand(
                action_id="fara-bash",
                session_id="",
                kind=ActionKind.integration_action,
                args={"tool": "bash", "command": args.get("command", "")}
            )
        if tool_name == "python_script":
            return ActionCommand(
                action_id="fara-python",
                session_id="",
                kind=ActionKind.integration_action,
                args={"tool": "python_script", "code": args.get("code", ""), "executable": "d:/code/envs/win/vilagent/python.exe"}
            )
        if tool_name == "browser_action":
            postconditions = []
            if action_name == "visit_url":
                postconditions = [
                    Condition(
                        kind="screen_changed",
                        operator=ConditionOperator.changed,
                        description="The visible browser content should change after navigation.",
                    )
                ]
            return ActionCommand(
                action_id="fara-browser",
                session_id="",
                kind=ActionKind.browser_action,
                args={"action": action_name, "url": args.get("url"), "thought": args.get("thought")},
                postconditions=postconditions,
            )

        target = None
        if "coordinate" in args and isinstance(args["coordinate"], list) and len(args["coordinate"]) == 2:
            x, y = args["coordinate"]
            target = TargetRef(
                strategy=TargetStrategy.coordinate,
                confidence=1.0,
                observation_id="",
                bounds=Rect(x=int(x), y=int(y), width=1, height=1),
                selector={"point": [int(x), int(y)]}
            )

        kind = ActionKind.click
        v_args = {}

        if action_name in ("left_click", "right_click", "double_click"):
            kind = getattr(ActionKind, action_name, ActionKind.click)
        elif action_name == "type":
            kind = ActionKind.type_text
            v_args["text"] = args.get("text", "")
        elif action_name == "key":
            kind = ActionKind.hotkey
            v_args["keys"] = args.get("keys", [])
        elif action_name == "scroll":
            kind = ActionKind.scroll
            v_args["amount"] = args.get("pixels", 0)
        elif action_name in ("mouse_move", "wait", "terminate", "finish_step", "visit_url"):
            kind = ActionKind.browser_action
            v_args["action"] = "terminate" if action_name == "finish_step" else action_name
            for k, v in args.items():
                if k != "action":
                    v_args[k] = v
        else:
            kind = ActionKind.browser_action
            v_args["action"] = action_name or "unknown"

        if kind not in {ActionKind.click, ActionKind.double_click, ActionKind.right_click, ActionKind.scroll}:
            target = None

        # Always carry FARA's natural-language thought so the UI can show it live.
        v_args["thought"] = args.get("thought")

        postconditions = []
        if target is not None and kind in {ActionKind.click, ActionKind.double_click, ActionKind.right_click}:
            postconditions = [
                Condition(
                    kind="screen_changed",
                    operator=ConditionOperator.changed,
                    description="The visible desktop should change after the Fara coordinate action.",
                )
            ]

        return ActionCommand(
            action_id="fara-action",
            session_id="",
            kind=kind,
            target=target,
            args=v_args,
            postconditions=postconditions,
        )


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
