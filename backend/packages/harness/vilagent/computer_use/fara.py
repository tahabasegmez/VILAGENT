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
As soon as the current step's completion criterion is satisfied, immediately return finish_step with status success and perform no more actions.
If the step cannot be completed, return finish_step with status failure.
You have at most {max_actions} actions for this isolated step. On the final allowed action, return an ultimate finish_step success or failure decision. Never exceed the stated action limit.
Return exactly one <tool_call> JSON object.
Use screenshot coordinates only for pointer actions. For type and key actions, operate on the currently focused control and omit coordinates.
Reason from the current screenshot, not from an idealized flow. The UI may contain popups, ads, cookie banners, dialogs, loading states, focus mismatch, localized labels, disabled controls, or covered targets.
If a recoverable obstruction directly blocks this step, use the smallest safe action to dismiss, wait for, or bypass it, then continue the same step. Do not interact with unrelated content or pursue a different goal.
You are running with a retry limit of {max_actions} actions per step. Use your actions wisely.
You are currently operating in the '{environment}' environment.

<tools>
{tools_json}
</tools>
<tool_call>
{{"name":"computer_use","arguments":{{"action":"left_click","coordinate":[100,200]}}}}
</tool_call>"""

def _build_fara_system_prompt(environment: str, max_actions: int) -> str:
    tools = [
        {
            "name": "computer_use",
            "description": "Mouse and keyboard control.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["key", "type", "mouse_move", "left_click", "right_click", "double_click", "scroll", "wait", "finish_step"]},
                    "keys": {"type": "array"},
                    "text": {"type": "string"},
                    "coordinate": {"type": "array"},
                    "pixels": {"type": "number"},
                    "time": {"type": "number"},
                    "status": {"type": "string", "enum": ["success", "failure"]}
                },
                "required": ["action"]
            }
        },
        {
            "name": "bash",
            "description": "Execute a terminal bash command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"]
            }
        },
        {
            "name": "python_script",
            "description": "Execute python code using the d:/code/envs/win/vilagent/python.exe interpreter.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"]
            }
        }
    ]
    if environment == "browser":
        tools.append({
            "name": "browser_action",
            "description": "Browser specific actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["visit_url", "go_back", "go_forward", "refresh"]},
                    "url": {"type": "string"}
                },
                "required": ["action"]
            }
        })
    tools_str = "\\n".join(json.dumps(t) for t in tools)
    return _FARA_BASE_PROMPT.format(max_actions=max_actions, environment=environment, tools_json=tools_str)


class FaraVisionActionProvider:
    """End-to-end vision action provider that queries FARA 7B."""

    def __init__(self, config: ComputerUseFaraModelConfig):
        self._config = config

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
    ) -> tuple[ActionCommand | None, dict[str, Any] | None]:
        """Query FARA 7B for the next action to execute in the loop."""
        if not self.is_enabled():
            return None, None

        sys_prompt = _build_fara_system_prompt(environment, max_actions)
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
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Task step: {instruction}\nNext screenshot. Choose the next action or terminate."},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            })

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
            response.raise_for_status()
            data = response.json()

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
                args={"action": action_name, "url": args.get("url")},
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
