"""The planner: turns a task into a short plan of sub-goal steps (JSON from a chat model)."""

from __future__ import annotations

import json
import locale
import os
from collections.abc import Callable
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from vilagent.agents.common import (
    EnvironmentContext,
    Plan,
    PlannedRisk,
    PlanStep,
    RiskLevel,
    StepResult,
    extract_json_object,
    extract_reasoning,
    message_text,
    response_tokens,
)
from vilagent.runs.budget import charge, count_tokens
from vilagent.runs.trace import note

PLANNER_SYSTEM_PROMPT = """\
You are the VILAGENT computer-use planner. Output ONLY one JSON object:
{"goal":"...","steps":[{"step_id":"s1","instruction":"a clear sub-goal","completion_criteria":"one observable proof this sub-goal is done","max_actions":8,"environment":"browser|native","requires_vision":true,"action_kind":"click|type_text|hotkey|launch_app|browser_action","target_description":"...","selector_hints":{},"args":{},"risk":{"level":"low|medium|high|critical","reasons":["..."],"consequences":["..."]},"requires_verification":true}]}

How execution works (plan for THIS, do not micro-manage):
- A vision step is handed to FARA, a capable GUI agent that SEES the screen and performs AS MANY actions as the sub-goal needs (look, click, type, press keys, scroll, dismiss a popup, retry) until the step's completion_criteria is met. So a vision step should be a MEANINGFUL SUB-GOAL, not a single click. FARA handles the small motor actions, focus moves, autocomplete confirmations, and obstructions on its own — you do NOT need a separate step for every click or key press.
- A deterministic step is executed exactly once by the runtime (no vision): launching an app, typing a known literal string, or pressing a known hotkey.

Rules:
- LANGUAGE: write EVERY "instruction" and "completion_criteria" value in ENGLISH ONLY, no matter what language the user wrote in. The executor (FARA) only reliably understands English, so never emit Turkish or any non-English text in those fields. You may keep proper nouns, exact text-to-type, URLs, and search queries verbatim. The "goal" field may restate the user's request (English preferred).
- Plan, never execute. Do not ask for screenshots or session ids.
- environment: 'native' Windows desktop UI, or 'browser' for anything on the web.
- requires_vision: false ONLY when the command has an unambiguous keyboard, text-entry, UIA, or DOM equivalent (a fixed app launch, a known literal to type, or a fixed hotkey); true when the screen must be looked at. Prefer deterministic input steps over visual actions when the command is fixed and unambiguous; otherwise use a vision step.
- On the NATIVE desktop, a control with a visible NAME (a navigation-pane entry, a ribbon or toolbar button, a menu item, a named list row, a labelled checkbox) is found by that name without a screenshot: action_kind="click", requires_vision=false, target_description set to the name EXACTLY as it appears (e.g. "Documents", "Save", "New folder"), and selector_hints.control_type when you know it (Button, TreeItem, ListItem, MenuItem, Edit). The runtime falls back to looking at the screen by itself if the name turns out to be ambiguous, so prefer this over a vision step whenever the control has a name. Keep requires_vision=true when the target has no stable name: an icon, a thumbnail, a canvas, a chart, a video, or a position on screen ("the second result").
- To open a desktop app, use ONE launch_app step (action_kind="launch_app", requires_vision=false) with args.app_name set to the app's NAME AS YOU WOULD TYPE IT INTO THE WINDOWS START SEARCH — its common/display name (e.g. "Notepad", "Microsoft Edge", "Google Chrome", "Word", "Excel", "Paint"), not a raw exe path. The runtime presses the Windows key, types this name, and opens the top result. Give a single app name, NEVER a sentence, URL, or goal.
- To type a fixed literal (a known number, code, or exact text) into an already-focused field, you may use a deterministic type_text step (action_kind="type_text", requires_vision=false, exact string with its symbols in args.text). When the field must first be found/focused on screen, fold the whole "click the field and type X" into a single vision step instead.
- WEB / BROWSER tasks run in a dedicated browser the runtime manages for you. Do NOT plan a launch_app step to open a browser, and do NOT switch to the desktop for web work. Mark every web step environment="browser". Make the FIRST browser step a navigation: action_kind="browser_action", args.action="visit_url", args.url the full "https://..." URL. After that, each browser step is a page sub-goal (e.g. "search for X and open the first result", "fill the login form with user U and password P and submit") that FARA carries out by acting on the real page.
- Keep steps at the SUB-GOAL altitude: one coherent outcome per step (e.g. "compose and send an email to alice@x.com with subject S and body B", "log in with these credentials", "add item I to the cart"). Do not split a single coherent interaction into one-click steps, and do not bundle unrelated goals into one step. Put a genuine verification/decision (e.g. "confirm the order total is correct before paying") in its own step.
- Write each step's instruction in 1-3 plain sentences: WHAT outcome to reach, the concrete specifics (names, exact text, URLs, values, order), and how to recognise success. Include autocomplete/confirmation hints when relevant (e.g. "after typing the recipient, press Enter to pick the highlighted suggestion before moving on"). Be specific; never vague ("handle the page") and never padded.
- Put known values in args (app_name, text, keys, url) and canonical hotkeys (ENTER, ESC, CTRL+L) in args.keys. Set completion_criteria to the exact observable end state.
- max_actions: how many actions FARA may take for that sub-goal (8 typical; up to 12-16 for multi-field forms or pages with overlays/loading). It is a budget, not a command count.
- Use context.windows_ui_language for localized controls. Assess risk per step (critical = the UI's Very High).
- Up to 12 steps; prefer fewer, well-scoped sub-goals.
- context.experience, when present, holds similar runs that worked and lessons from earlier runs. It is advisory: reuse a plan that worked and apply the lessons, but the current task (and, when replanning, the current screen) wins.
"""


class Planner(Protocol):
    async def plan(self, prompt: str, *, context: dict[str, Any]) -> Plan: ...

    async def replan(
        self, prompt: str, *, plan: Plan, completed_steps: list[StepResult], blocked_step: StepResult, context: dict[str, Any], screenshot: str | None = None
    ) -> Plan: ...


class JsonLLMPlanner:
    """Planner backed by the selected chat model; unparseable replies fall back to a one-step plan."""

    def __init__(self, build: Callable[[bool], Any], on_thinking: Callable[[str], None] | None = None, *, sees_images: bool = False):
        self._on_thinking = on_thinking
        self.request_count = 0
        self.total_tokens = 0
        # Surface the model's reasoning when it supports thinking. How it answers otherwise
        # (temperature and the rest) belongs to its connection, so nothing is bound here.
        self._model = build(True)
        # Only a vision-capable model is shown the blocked step's screen when replanning.
        self.sees_images = sees_images

    async def plan(self, prompt: str, *, context: dict[str, Any]) -> Plan:
        return await self._invoke("Create a compact computer-use plan for this user task.", prompt, context)

    async def replan(
        self, prompt: str, *, plan: Plan, completed_steps: list[StepResult], blocked_step: StepResult, context: dict[str, Any], screenshot: str | None = None
    ) -> Plan:
        """``blocked_step.evidence`` carries FARA's last notes; ``screenshot`` (a data URL) shows the screen now."""
        return await self._invoke(
            "Revise the remaining computer-use plan after a blocked step.",
            prompt,
            {
                **context,
                "previous_plan": plan.model_dump(mode="json"),
                "completed_steps": [step.model_dump(mode="json") for step in completed_steps],
                "blocked_step": blocked_step.model_dump(mode="json"),
            },
            screenshot=screenshot,
        )

    async def _invoke(self, instruction: str, prompt: str, context: dict[str, Any], *, screenshot: str | None = None) -> Plan:
        charge("planner")
        text = json.dumps({"instruction": instruction, "user_task": prompt, "context": context}, ensure_ascii=False)
        content: str | list[dict[str, Any]] = text
        if screenshot:
            content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": screenshot}}]
        response = await self._model.ainvoke([SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=content)])
        self.request_count += 1
        tokens = response_tokens(response)
        self.total_tokens += tokens
        count_tokens("planner", tokens)
        if reasoning := extract_reasoning(response):
            note(thinking=reasoning)
            if self._on_thinking:
                self._on_thinking(reasoning)
        try:
            return Plan.model_validate(extract_json_object(message_text(response)))
        except Exception:
            return fallback_plan(prompt)


def fallback_plan(prompt: str) -> Plan:
    return Plan(
        goal=prompt,
        steps=[
            PlanStep(
                step_id="s1",
                instruction=prompt,
                environment=EnvironmentContext.native,
                requires_vision=True,
                target_description=prompt,
                risk=PlannedRisk(
                    level=RiskLevel.medium,
                    reasons=["Fallback plan could not be assessed by the planner."],
                    consequences=["The requested desktop action may change application state."],
                ),
            )
        ],
    )


def planner_context(context: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(context or {})
    payload.setdefault("windows_ui_language", windows_ui_language())
    return payload


def windows_ui_language() -> str:
    if os.name == "nt":
        try:
            import ctypes

            language = locale.windows_locale.get(int(ctypes.windll.kernel32.GetUserDefaultUILanguage()))
            if language:
                return language.replace("_", "-")
        except Exception:
            pass
    language = locale.getlocale()[0]
    return language.replace("_", "-") if language else "unknown"
