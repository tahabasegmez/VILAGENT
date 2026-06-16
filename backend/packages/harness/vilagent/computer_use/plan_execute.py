"""Plan-and-execute computer-use architecture for VILAGENT.

This module implements the user's target architecture without replacing the
current ReAct-style computer_use_agent yet:

1. A text planner creates a compact plan.
2. Each step is delegated to a specialized executor role.
3. Executors use the Windows host target/action plane, preferring vision when
   configured, while UIA/browser providers remain deterministic fallbacks.
4. A short replan is requested only when a step is blocked or unresolved.

The concrete LLM calls are injected behind PlannerProtocol so the gateway can
later switch between this architecture and the existing graph without changing
the host/action implementation.
"""

from __future__ import annotations

import asyncio
import base64
import locale
import logging
import os
import re
import uuid
import json
import httpx
from enum import StrEnum
from typing import Any, Protocol, Callable, Awaitable

from pydantic import BaseModel, ConfigDict, Field
from langchain_core.messages import HumanMessage, SystemMessage

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    Condition,
    RiskAssessment,
    RiskLevel,
    TargetQuery,
    TargetRef,
    TargetStrategy,
)
from vilagent.computer_use.remote_host import RemoteHostOperationError, RemoteSessionNotFoundError, RemoteWindowsHostControl
from vilagent.models import create_chat_model
from vilagent.config.app_config import get_app_config
from vilagent.computer_use.fara import FaraVisionActionProvider
from vilagent.computer_use.image_ops import encode_image_for_vision, scale_point
from vilagent.computer_use.uitars import UiTarsVisionActionProvider
from vilagent.config.computer_use_config import ComputerUseFaraModelConfig


class EnvironmentContext(StrEnum):
    browser = "browser"
    native = "native"


class StepStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    blocked = "blocked"
    failed = "failed"
    skipped = "skipped"


class PlannedRiskAssessment(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)


class ComputerUsePlanStep(BaseModel):
    step_id: str
    instruction: str = Field(min_length=1)
    completion_criteria: str = Field(default="The instructed command has completed.", min_length=1)
    max_actions: int = Field(default=4, ge=1, le=6)
    environment: EnvironmentContext = EnvironmentContext.native
    requires_vision: bool = True
    action_kind: ActionKind | None = None
    target_description: str | None = None
    selector_hints: dict[str, Any] = Field(default_factory=dict)
    args: dict[str, Any] = Field(default_factory=dict)
    postconditions: list[Condition] = Field(default_factory=list)
    risk: PlannedRiskAssessment
    requires_verification: bool = True
    model_config = ConfigDict(extra="forbid")


class ComputerUsePlan(BaseModel):
    goal: str
    steps: list[ComputerUsePlanStep] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class StepExecutionResult(BaseModel):
    step_id: str
    environment: EnvironmentContext
    requires_vision: bool
    status: StepStatus
    action_id: str | None = None
    action_status: ActionLifecycleStatus | None = None
    target: TargetRef | None = None
    error_code: str | None = None
    summary: str = ""


class PlanExecuteRunResult(BaseModel):
    status: StepStatus
    plan: ComputerUsePlan
    steps: list[StepExecutionResult]
    replan_count: int = 0
    request_count_estimate: int = 0
    summary: str = ""


class PlannerProtocol(Protocol):
    async def plan(self, prompt: str, *, context: dict[str, Any]) -> ComputerUsePlan:
        """Create the initial compact plan."""

    async def replan(
        self,
        prompt: str,
        *,
        plan: ComputerUsePlan,
        completed_steps: list[StepExecutionResult],
        blocked_step: StepExecutionResult,
        context: dict[str, Any],
    ) -> ComputerUsePlan:
        """Return a revised plan after a blocked/failed step."""


class JsonLLMPlanner:
    """Small JSON planner for the plan-execute architecture."""

    def __init__(self, model_name: str):
        self._model_name = model_name
        self._model = create_chat_model(model_name, thinking_enabled=False, attach_tracing=False)

    async def plan(self, prompt: str, *, context: dict[str, Any]) -> ComputerUsePlan:
        return await self._invoke_plan(
            "Create a compact computer-use plan for this user task.",
            prompt=prompt,
            context=context,
        )

    async def replan(
        self,
        prompt: str,
        *,
        plan: ComputerUsePlan,
        completed_steps: list[StepExecutionResult],
        blocked_step: StepExecutionResult,
        context: dict[str, Any],
    ) -> ComputerUsePlan:
        return await self._invoke_plan(
            "Revise the remaining computer-use plan after a blocked step.",
            prompt=prompt,
            context={
                **context,
                "previous_plan": plan.model_dump(mode="json"),
                "completed_steps": [step.model_dump(mode="json") for step in completed_steps],
                "blocked_step": blocked_step.model_dump(mode="json"),
            },
        )

    async def _invoke_plan(self, instruction: str, *, prompt: str, context: dict[str, Any]) -> ComputerUsePlan:
        response = await self._model.ainvoke(
            [
                SystemMessage(content=_PLANNER_SYSTEM_PROMPT),
                HumanMessage(
                    content=json.dumps(
                        {"instruction": instruction, "user_task": prompt, "context": context},
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        text = _message_text(response)
        try:
            payload = _extract_json_object(text)
            return ComputerUsePlan.model_validate(payload)
        except Exception:
            return _fallback_plan(prompt)



_PLANNER_SYSTEM_PROMPT = """\
You are the VILAGENT computer-use planner. Output ONLY one JSON object:
{"goal":"...","steps":[{"step_id":"s1","instruction":"one exact command","completion_criteria":"one observable proof this command finished","max_actions":4,"environment":"browser|native","requires_vision":true,"action_kind":"click|type_text|hotkey|launch_app|browser_action","target_description":"...","selector_hints":{},"args":{},"risk":{"level":"low|medium|high|critical","reasons":["..."],"consequences":["..."]},"requires_verification":true}]}
Rules:
- Plan, never execute. Do not ask for screenshots or session ids.
- One step = exactly one user-visible command. Put every check / verify / retry / sort-direction decision in its OWN step; never bundle execution and double-checking.
- environment: 'native' Windows UI or 'browser'.
- requires_vision: false only when the command has an unambiguous keyboard / UIA / DOM equivalent (keyboard, text-entry, UIA, or DOM equivalent); true when visual understanding of the screen is needed. Prefer deterministic input steps over visual actions. Prefer deterministic over visual.
- To enter text, numbers, or a calculation, use ONE type_text step: action_kind="type_text", requires_vision=false, and put the exact literal string (with symbols like + - * / =) in args.text. The keyboard types it directly into the focused field; NEVER spell it out by visually clicking on-screen keys or buttons. Add a separate step to focus the field first only if it is not already focused.
- To open an app, use ONE launch_app step (action_kind="launch_app", requires_vision=false) and set args.app_name to the app's Windows EXECUTABLE name (you know these: Edge=msedge, Chrome=chrome, Word=winword, Excel=excel, Notepad=notepad, Calculator=calc, ...). Give a single launchable name, NEVER a sentence, URL, or goal. The runtime launches the executable (also via App Paths / Start search), so a correct exe name is the most reliable. Launching and navigating are ALWAYS separate steps.
- To open a web page, use a separate browser_action step AFTER the browser is open: action_kind="browser_action", args.action="visit_url", args.url the full "https://..." URL. Do not bundle "launch browser and go to X" into one step.
- Plan the TRANSITIONS between steps, not only the actions. Spell it out simply: after one step finishes, the cursor/focus is somewhere; to do the next thing you must FIRST move focus there. Never assume the cursor is already in the right place, and never rely on the vision model to find and click each field. So whenever two consecutive steps touch different fields, controls, or regions, add an explicit deterministic step that moves focus first — a hotkey step (action_kind="hotkey", requires_vision=false, e.g. args.keys="TAB" for the next field, "ENTER" to confirm, or arrow keys) or a focus/click step — BEFORE the next action. Short examples (not one specific app):
    Email: type To -> hotkey TAB -> type Subject -> hotkey TAB -> type Body -> click Send.
    Login form: type username -> hotkey TAB -> type password -> hotkey ENTER.
    Save As dialog: type the file name -> hotkey ENTER.
    Spreadsheet: type a cell value -> hotkey TAB (next cell right) or ENTER (cell below).
  Pick the correct key(s) for that app's focus order.
- max_actions: bounded executor budget for that one command (4 typical for visual steps, 5-6 when popups, overlays, loading states, focus mismatch, localization, or other recoverable UI variance may appear, never >6); it does not permit merging commands.
- Make MANY small, separate steps rather than a few broad ones: one click, one type, one hotkey, one navigation, one wait per step. If a step could be split into two, split it. More short steps is better than one big step.
- Write each step's instruction like explaining to a beginner who can only see the screen: in 1-2 short, plain sentences say WHAT to do, WHERE (the exact target and how to recognise it on screen — its label, color, position, or nearby text), and HOW (click / type / which key). Be explicit and unambiguous; do not be vague ("handle the page") and do not pad with filler.
- Executor-ready steps: name the target and its expected current state, the exact interaction mechanism, known args/selector_hints, and the observable end state. No vague "navigate to the website" when a URL/browser action is known. Put app_name/text/keys/url in args when known; for hotkeys put canonical keys (ENTER, ESC, CTRL+L) in args.keys.
- Separate any known preparation (focus a field, clear content, dismiss an obstruction) into its own step. If an obstruction is only possible but not guaranteed, give the affected visual step enough max_actions for the executor to dismiss or bypass it before completing the same command.
- Use context.windows_ui_language for localized controls. Assess risk per step (critical = the UI's Very High).
- Up to 16 concise steps.
"""


# The recovery supervisor is a stronger vision+reasoning model (the selected
# planner model, e.g. a cloud Qwen3-VL). It is only called when the fast action
# model (FARA/UI-TARS) is stuck, so keep this prompt short and decisive.
_SUPERVISOR_PROMPT = """\
You supervise a fast GUI action model that got stuck on ONE step. Look at the
screenshot and reason about what is actually blocking progress (a popup, ad,
cookie banner, modal, wrong/unfocused window, a disabled or covered control, a
loading state, or a wrong assumption).
Reply with ONE short imperative recovery instruction (max 2 sentences) telling the
action model exactly what to do NEXT to unblock and continue the step — e.g.
"Close the ad by clicking the X at its top-right corner, then click the search box."
If nothing is actually blocking and it should simply retry the original step, reply
exactly: PROCEED. Do not explain. Output only the instruction or PROCEED."""

_MAX_SUPERVISOR_CALLS = 2

# Extra loop iterations allowed for non-mutating 'wait'/'mouse_move' actions so they
# do not consume the real-action budget for a step.
_MAX_VISION_NOOPS = 3

# Generic self-correction nudges given when the model is stuck and the recovery
# supervisor is unavailable/disabled/rate-limited, before failing the step.
_MAX_VISION_NUDGES = 2

_GENERIC_STUCK_NUDGE = (
    "You repeated the same action with no visible effect. The target may be in a "
    "different place, the page may have changed, the element may be off-screen, or "
    "the click missed. Re-examine the screenshot carefully and try a clearly DIFFERENT "
    "location or a different action (scroll the target into view, or click a different "
    "element); do not repeat the previous coordinates."
)


async def _detect_served_model_name(base_url: str | None, api_key: str | None, default_model: str) -> str:
    if not base_url:
        return default_model
    url = f"{base_url.rstrip('/')}/models"
    headers = {
        "Accept": "application/json",
        "ngrok-skip-browser-warning": "true",
    }
    if api_key and api_key != "not-needed":
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                    model_id = data["data"][0].get("id")
                    if model_id:
                        return model_id
    except Exception:
        pass
    return default_model


async def _detect_served_model_name_once(
    cache: dict[tuple[str | None, str | None, str], str],
    base_url: str | None,
    api_key: str | None,
    default_model: str,
    *,
    detector: Callable[[str | None, str | None, str], Awaitable[str]] | None = None,
) -> str:
    cache_key = (base_url, api_key, default_model)
    detected_model = cache.get(cache_key)
    if detected_model is None:
        detected_model = await (detector or _detect_served_model_name)(base_url, api_key, default_model)
        cache[cache_key] = detected_model
    return detected_model


class ComputerUseStepExecutor:
    """Execute one planned step through host observation, target resolution, and action lifecycle."""

    def __init__(
        self,
        remote: RemoteWindowsHostControl,
        *,
        vision_provider: str | None = None,
        vision_recovery: bool = False,
        supervisor_model_factory: Callable[[], Any] | None = None,
    ):
        self._remote = remote
        # The vision provider selection ("fara" | "ui_tars") is supplied by the
        # operator UI (persisted state) and is authoritative; config is only a
        # bootstrap default when the UI has not selected one yet.
        self._vision_provider = vision_provider
        # Optional recovery supervisor: when on, a stronger reasoning model is
        # consulted only when the fast vision model is stuck. The factory builds the
        # model (the selected planner model OR a dedicated env-configured GLM-V).
        # Fully removable — with vision_recovery=False there is zero behaviour change.
        self._vision_recovery = vision_recovery and supervisor_model_factory is not None
        self._supervisor_model_factory = supervisor_model_factory
        self._detected_vision_models: dict[tuple[str | None, str | None, str], str] = {}

    def _selected_vision_provider(self, config) -> str:
        return self._vision_provider or config.computer_use.vision_provider

    async def _get_recovery_advice(
        self,
        *,
        step: ComputerUsePlanStep,
        recent_thought: str | None,
        image_base64: str,
        image_media_type: str,
        on_activity_update: Callable[[str, str, str | None], None] | None,
    ) -> str | None:
        """Consult the supervisor model once for a concrete recovery instruction.

        Returns the instruction text, or None to mean "just proceed / no advice".
        """
        if self._supervisor_model_factory is None:
            return None
        if on_activity_update:
            on_activity_update("vision_executor", "Consulting recovery supervisor...", recent_thought)
        try:
            model = self._supervisor_model_factory()
            user_text = (
                f"Step goal: {step.instruction}\n"
                f"Done when: {step.completion_criteria}\n"
                f"The action model is stuck. Its last note: {recent_thought or '(none)'}\n"
                "Look at the screen and give the single best recovery instruction, or PROCEED."
            )
            response = await model.ainvoke(
                [
                    SystemMessage(content=_SUPERVISOR_PROMPT),
                    HumanMessage(
                        content=[
                            {"type": "text", "text": user_text},
                            {"type": "image_url", "image_url": {"url": f"data:{image_media_type};base64,{image_base64}"}},
                        ]
                    ),
                ]
            )
            advice = _message_text(response).strip()
        except Exception:
            logging.getLogger(__name__).warning(
                "Recovery supervisor call failed; check the supervisor model supports vision "
                "(text-only models reject screenshots). Skipping advice this step.",
                exc_info=True,
            )
            return None
        if not advice or advice.strip().upper().startswith("PROCEED"):
            return None
        return advice[:500]

    async def execute(
        self,
        step: ComputerUsePlanStep,
        *,
        owner: ActionOwner,
        session_id: str | None,
        auto_approve_risk_threshold: RiskLevel,
        on_activity_update: Callable[[str, str, str | None], None] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[str, StepExecutionResult]:
        resolved_session_id = await self._ensure_session(session_id)
        action_kind = step.action_kind or _infer_action_kind(step)
        try:
            config = get_app_config()
            # Typing, launching, and hotkeys are deterministic keyboard operations
            # and must never be done by visually clicking on-screen keys (which is
            # unreliable and lets the vision model falsely 'finish' after one click).
            if step.requires_vision and action_kind not in {ActionKind.launch_app, ActionKind.hotkey, ActionKind.type_text}:
                if self._selected_vision_provider(config) in ("fara", "ui_tars"):
                    return await self._execute_fara_vision_loop(
                        step,
                        owner=owner,
                        session_id=resolved_session_id,
                        auto_approve_risk_threshold=auto_approve_risk_threshold,
                        max_actions=_vision_action_limit(step, config.computer_use.budgets.vision_calls),
                        on_activity_update=on_activity_update,
                        cancel_check=cancel_check,
                    )

            if on_activity_update:
                on_activity_update("uia_executor", f"Resolving target for: {step.instruction}", None)
                
            target = await self._resolve_target(
                step,
                owner=owner,
                session_id=resolved_session_id,
                action_kind=action_kind,
            )
            args = _args_for_step(step, action_kind)
            action = ActionCommand(
                action_id=f"plan-step-{uuid.uuid4().hex}",
                session_id=resolved_session_id,
                kind=action_kind,
                target=target,
                args=args,
                postconditions=list(step.postconditions),
                risk=_action_risk(step),
                auto_approve_risk_threshold=auto_approve_risk_threshold,
            )
            stored = await self._remote.submit_action(action, owner)
            while stored.status == ActionLifecycleStatus.awaiting_approval:
                if cancel_check and await cancel_check():
                    return resolved_session_id, StepExecutionResult(
                        step_id=step.step_id,
                        environment=step.environment,
                        requires_vision=step.requires_vision,
                        status=StepStatus.failed,
                        action_id=action.action_id,
                        action_status=ActionLifecycleStatus.cancelled,
                        error_code="client_disconnected",
                        summary="Client disconnected during action approval wait",
                    )
                await asyncio.sleep(0.5)
                stored = await self._remote.get_action(action.action_id, owner)
            final = stored
            if stored.status in {ActionLifecycleStatus.pending, ActionLifecycleStatus.approved}:
                final = await self._remote.execute_action(action.action_id, owner)
            status = _step_status_from_action(final.status)
            final_error_code = final.error.code if getattr(final, "error", None) is not None else None
            summary = f"{action.kind.value} -> {final.status.value}"
            if final_error_code:
                summary = f"{summary} ({final_error_code})"
            return resolved_session_id, StepExecutionResult(
                step_id=step.step_id,
                environment=step.environment,
                requires_vision=step.requires_vision,
                status=status,
                action_id=action.action_id,
                action_status=final.status,
                target=target,
                error_code=final_error_code,
                summary=summary,
            )
        except (RemoteHostOperationError, RemoteSessionNotFoundError) as exc:
            return resolved_session_id, StepExecutionResult(
                step_id=step.step_id,
                environment=step.environment,
                requires_vision=step.requires_vision,
                status=StepStatus.blocked,
                error_code=getattr(exc, "code", exc.__class__.__name__),
                summary=f"blocked: {getattr(exc, 'code', exc.__class__.__name__)}",
            )
        except Exception as exc:
            return resolved_session_id, StepExecutionResult(
                step_id=step.step_id,
                environment=step.environment,
                requires_vision=step.requires_vision,
                status=StepStatus.failed,
                error_code=exc.__class__.__name__,
                summary=f"failed: {exc.__class__.__name__}",
            )

    async def _execute_fara_vision_loop(
        self,
        step: ComputerUsePlanStep,
        *,
        owner: ActionOwner,
        session_id: str,
        auto_approve_risk_threshold: RiskLevel,
        max_actions: int,
        on_activity_update: Callable[[str, str, str | None], None] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[str, StepExecutionResult]:
        config = get_app_config()
        
        if on_activity_update:
            on_activity_update("vision_executor", f"Running vision step: {step.instruction}", None)
        
        # 1. Resolve tunnel URL to bind both providers to the active Ngrok tunnel
        tunnel_url = None
        if config.computer_use.vision_uitars_model.pyngrok_url:
            tunnel_url = config.computer_use.vision_uitars_model.pyngrok_url.rstrip("/")
        elif config.computer_use.vision_fara_model.base_url:
            fara_base = config.computer_use.vision_fara_model.base_url.rstrip("/")
            if "localhost" not in fara_base and "127.0.0.1" not in fara_base:
                tunnel_url = fara_base
                if tunnel_url.endswith("/v1"):
                    tunnel_url = tunnel_url[:-3]

        if tunnel_url:
            base_url = f"{tunnel_url}/v1"
        else:
            base_url = config.computer_use.vision_fara_model.base_url

        # 2. Resolve API key
        if self._selected_vision_provider(config) == "ui_tars":
            api_key = config.computer_use.vision_uitars_model.api_key or "not-needed"
        else:
            api_key = config.computer_use.vision_fara_model.api_key or "not-needed"

        # 3. Resolve default model name
        if self._selected_vision_provider(config) == "ui_tars":
            default_model = config.computer_use.vision_uitars_model.model_name
        else:
            default_model = config.computer_use.vision_fara_model.model_name

        try:
            detected_model = await _detect_served_model_name_once(
                self._detected_vision_models,
                base_url,
                api_key,
                default_model,
            )
        except Exception as e:
            return session_id, StepExecutionResult(
                step_id=step.step_id,
                environment=step.environment,
                requires_vision=step.requires_vision,
                status=StepStatus.failed,
                error_code="vision_model_unreachable",
                summary=f"Vision model unreachable: {_exception_summary(e)}",
            )

        if self._selected_vision_provider(config) == "ui_tars":
            provider: Any = UiTarsVisionActionProvider(
                config.computer_use.vision_uitars_model,
                base_url=base_url,
                api_key=api_key,
                model_name=detected_model,
            )
        else:
            fara_config = ComputerUseFaraModelConfig(
                enabled=True,
                model_name=detected_model,
                base_url=base_url,
                api_key=api_key,
                timeout_seconds=config.computer_use.vision_fara_model.timeout_seconds,
            )
            provider = FaraVisionActionProvider(fara_config)
        chat_history: list[dict[str, Any]] = []
        repeated_signature: str | None = None
        repeated_count = 0
        supervisor_calls = 0
        nudge_count = 0
        real_actions = 0
        max_attempts = max(1, min(max_actions, 6))
        if self._vision_recovery:
            # Give the supervisor-guided retries room beyond the base action budget.
            max_attempts = min(max_attempts + 2 * _MAX_SUPERVISOR_CALLS, 8)
        last_action_id = ""
        last_status = ActionLifecycleStatus.failed
        last_error_code: str | None = None

        for index in range(max_attempts + _MAX_VISION_NOOPS):
            if cancel_check and await cancel_check():
                return session_id, StepExecutionResult(
                    step_id=step.step_id,
                    environment=step.environment,
                    requires_vision=step.requires_vision,
                    status=StepStatus.failed,
                    error_code="client_disconnected",
                    summary="Client disconnected during execution",
                )
            try:
                obs = await self._remote.observe_session(session_id, owner=owner)
                if obs.screenshot_ref is None:
                    # Retry instead of failing immediately if observation isn't ready
                    continue
                _, image_bytes = await self._remote.export_observation_blob(
                    session_id,
                    obs.observation_id,
                    obs.screenshot_ref.blob_id,
                    owner,
                )
                # Downscale + JPEG for the remote model send (the dominant latency
                # cost); coordinates are mapped back to screen pixels via coord_scale.
                image_base64, image_media_type, coord_scale = encode_image_for_vision(
                    image_bytes,
                    max_dim=config.computer_use.vision_max_image_dimension,
                    jpeg_quality=config.computer_use.vision_jpeg_quality,
                )

                try:
                    action, new_history = await provider.get_next_action(
                        instruction=_vision_step_command(step, max_actions=max_attempts),
                        image_base64=image_base64,
                        chat_history=chat_history,
                        environment=step.environment,
                        max_actions=max_attempts,
                        image_media_type=image_media_type,
                    )
                except Exception as e:
                    last_error_code = _exception_summary(e)
                    if real_actions < max_attempts:
                        if on_activity_update:
                            on_activity_update(
                                "vision_executor",
                                f"Vision model call failed; retrying once ({last_error_code})",
                                None,
                            )
                        await asyncio.sleep(0.5)
                        continue
                    return session_id, StepExecutionResult(
                        step_id=step.step_id,
                        environment=step.environment,
                        requires_vision=step.requires_vision,
                        status=StepStatus.failed,
                        error_code="vision_action_failed",
                        summary=f"Failed to get next vision action after {max_attempts} action(s): {last_error_code}",
                    )
                chat_history = list(new_history or [])[-6:]
                
                thought = action.args.get("thought") if action else None
                if on_activity_update:
                    on_activity_update("vision_executor", f"Evaluating vision state...", thought)

                if not action:
                    return session_id, StepExecutionResult(
                        step_id=step.step_id,
                        environment=step.environment,
                        requires_vision=step.requires_vision,
                        status=StepStatus.failed,
                        error_code="fara_disabled",
                        summary="Fara model is disabled or unreachable",
                    )
                if coord_scale != 1.0:
                    action = _rescale_action_for_screen(action, coord_scale)
                action = action.model_copy(
                    update={
                        "action_id": f"fara-step-{step.step_id}-{index + 1}-{uuid.uuid4().hex}",
                        "session_id": session_id,
                        "risk": _action_risk(step),
                        "auto_approve_risk_threshold": auto_approve_risk_threshold,
                    }
                )

                if action.args.get("action") == "terminate":
                    if action.args.get("status") == "failure":
                        return session_id, StepExecutionResult(
                            step_id=step.step_id,
                            environment=step.environment,
                            requires_vision=step.requires_vision,
                            status=StepStatus.failed,
                            error_code="fara_terminate_failure",
                            summary="Fara decided to terminate with failure",
                        )
                    return session_id, StepExecutionResult(
                        step_id=step.step_id,
                        environment=step.environment,
                        requires_vision=step.requires_vision,
                        status=StepStatus.completed,
                        action_id=action.action_id,
                        action_status=ActionLifecycleStatus.succeeded,
                        summary="Fara completed the step successfully",
                    )

                # 'wait' and 'mouse_move' are not desktop mutations and have no
                # native action kind, so the vision mapper routes them to
                # browser_action — which is disabled outside a browser session and
                # would otherwise fail the whole step with 'browser_action_disabled'
                # (e.g. FARA waiting for an app to open). Handle them locally: sleep
                # for wait, no-op for a standalone move, then re-observe and continue.
                op = action.args.get("action")
                if action.kind == ActionKind.browser_action and op in {"wait", "mouse_move"}:
                    if op == "wait":
                        try:
                            wait_seconds = float(action.args.get("time") or 1.0)
                        except (TypeError, ValueError):
                            wait_seconds = 1.0
                        await asyncio.sleep(max(0.0, min(wait_seconds, 3.0)))
                    if on_activity_update:
                        on_activity_update("vision_executor", f"Vision model requested '{op}'; re-observing.", thought)
                    continue

                # Loop guard: a vision model that keeps emitting the identical
                # action without progressing is broken out of early (on top of the
                # hard max_actions cap) so it cannot burn the whole step budget.
                signature = _vision_action_signature(action)
                if signature is not None and signature == repeated_signature:
                    repeated_count += 1
                else:
                    repeated_signature = signature
                    repeated_count = 0
                if repeated_count >= 2:
                    advice = None
                    if self._vision_recovery and supervisor_calls < _MAX_SUPERVISOR_CALLS:
                        advice = await self._get_recovery_advice(
                            step=step,
                            recent_thought=thought,
                            image_base64=image_base64,
                            image_media_type=image_media_type,
                            on_activity_update=on_activity_update,
                        )
                        supervisor_calls += 1
                    if advice is None and nudge_count < _MAX_VISION_NUDGES:
                        # Supervisor disabled, rate-limited (e.g. GLM-V 429), or
                        # unhelpful: still self-correct once before giving up.
                        nudge_count += 1
                        advice = _GENERIC_STUCK_NUDGE
                        if on_activity_update:
                            on_activity_update("vision_executor", "Stuck; nudging the model to try a different approach.", thought)
                    if advice:
                        repeated_signature = None
                        repeated_count = 0
                        chat_history.append({
                            "role": "user",
                            "content": (
                                "<supervisor>\n"
                                f"{advice}\n"
                                "</supervisor>\n"
                                "Your previous approach was not progressing. Do exactly this now, "
                                "then continue the original step."
                            ),
                        })
                        continue
                    return session_id, StepExecutionResult(
                        step_id=step.step_id,
                        environment=step.environment,
                        requires_vision=step.requires_vision,
                        status=StepStatus.failed,
                        action_id=last_action_id,
                        action_status=ActionLifecycleStatus.failed,
                        error_code="no_progress_repeated_action",
                        summary="Vision model repeated the same action without making progress.",
                    )

                if action.target:
                    action = action.model_copy(
                        update={"target": action.target.model_copy(update={"observation_id": obs.observation_id})}
                    )

                # This is a real (mutating) action; only these consume the budget.
                real_actions += 1
                stored = await self._remote.submit_action(action, owner)
                if stored.status == ActionLifecycleStatus.awaiting_approval:
                    if on_activity_update:
                        on_activity_update("vision_executor", "Waiting for action approval...", thought)
                    while stored.status == ActionLifecycleStatus.awaiting_approval:
                        if cancel_check and await cancel_check():
                            return session_id, StepExecutionResult(
                                step_id=step.step_id,
                                environment=step.environment,
                                requires_vision=step.requires_vision,
                                status=StepStatus.failed,
                                action_id=action.action_id,
                                action_status=ActionLifecycleStatus.cancelled,
                                error_code="client_disconnected",
                                summary="Client disconnected during action approval wait",
                            )
                        await asyncio.sleep(0.5)
                        stored = await self._remote.get_action(action.action_id, owner)

                outcome = stored
                if stored.status in {ActionLifecycleStatus.pending, ActionLifecycleStatus.approved}:
                    outcome = await self._remote.execute_action(action.action_id, owner)
                    last_status = outcome.status
                else:
                    last_status = stored.status

                last_action_id = action.action_id

                if last_status == ActionLifecycleStatus.succeeded:
                    return session_id, StepExecutionResult(
                        step_id=step.step_id,
                        environment=step.environment,
                        requires_vision=step.requires_vision,
                        status=StepStatus.completed,
                        action_id=last_action_id,
                        action_status=last_status,
                        summary="Vision step completed after its single command succeeded.",
                    )

                if last_status == ActionLifecycleStatus.uncertain:
                    last_error_code = outcome.error.code if outcome.error is not None else "postcondition_failed"
                    if real_actions >= max_attempts:
                        return session_id, StepExecutionResult(
                            step_id=step.step_id,
                            environment=step.environment,
                            requires_vision=step.requires_vision,
                            status=StepStatus.failed,
                            action_id=last_action_id,
                            action_status=last_status,
                            error_code="step_uncertain_after_action_limit",
                            summary=(
                                f"Step failed after {max_attempts} action(s) because the model could not verify "
                                f"completion ({last_error_code})."
                            ),
                        )
                    if self._vision_recovery and supervisor_calls < _MAX_SUPERVISOR_CALLS:
                        advice = await self._get_recovery_advice(
                            step=step,
                            recent_thought=thought,
                            image_base64=image_base64,
                            image_media_type=image_media_type,
                            on_activity_update=on_activity_update,
                        )
                        supervisor_calls += 1
                        if advice:
                            chat_history.append({
                                "role": "user",
                                "content": (
                                    "<supervisor>\n"
                                    f"{advice}\n"
                                    "</supervisor>\n"
                                    "The step is not verified complete yet. Do exactly this recovery instruction, "
                                    "then continue the original step."
                                ),
                            })
                            continue
                    chat_history[-1] = {
                        "role": "user",
                        "content": (
                            "<tool_response>\n"
                            f'{{"status": "uncertain", "error": "{last_error_code}", '
                            '"instruction": "Make one final attempt, then return an ultimate finish_step decision."}\n'
                            "</tool_response>"
                        ),
                    }
                    continue

                if (
                    last_status == ActionLifecycleStatus.failed
                    and outcome.error is not None
                    and outcome.error.code in {"desktop_changed_before_mutation", "stale_target"}
                ):
                    last_error_code = outcome.error.code
                    if real_actions >= max_attempts:
                        return session_id, StepExecutionResult(
                            step_id=step.step_id,
                            environment=step.environment,
                            requires_vision=step.requires_vision,
                            status=StepStatus.failed,
                            action_id=last_action_id,
                            action_status=last_status,
                            error_code="step_failed_after_action_limit",
                            summary=f"Step failed after {max_attempts} action(s) ({last_error_code}).",
                        )
                    chat_history[-1] = {
                        "role": "user",
                        "content": (
                            "<tool_response>\n"
                            f'{{"status": "retry", "error": "{outcome.error.code}"}}\n'
                            "</tool_response>"
                        ),
                    }
                    continue

                if last_status in {ActionLifecycleStatus.denied, ActionLifecycleStatus.failed, ActionLifecycleStatus.cancelled}:
                    error_code = outcome.error.code if outcome.error is not None else last_status.value
                    last_error_code = error_code
                    return session_id, StepExecutionResult(
                        step_id=step.step_id,
                        environment=step.environment,
                        requires_vision=step.requires_vision,
                        status=_step_status_from_action(last_status),
                        action_id=last_action_id,
                        action_status=last_status,
                        error_code=error_code,
                        summary=f"Fara action {last_status.value}: {error_code}",
                    )

            except Exception as exc:
                return session_id, StepExecutionResult(
                    step_id=step.step_id,
                    environment=step.environment,
                    requires_vision=step.requires_vision,
                    status=StepStatus.failed,
                    error_code=exc.__class__.__name__,
                    summary=f"fara loop failed: {exc}",
                )
        
        status = _step_status_from_action(last_status)
        return session_id, StepExecutionResult(
            step_id=step.step_id,
            environment=step.environment,
            requires_vision=step.requires_vision,
            status=status,
            action_id=last_action_id,
            action_status=last_status,
            error_code=last_error_code,
            summary=f"Fara vision loop ended with {last_error_code or last_status.value}",
        )

    async def _ensure_session(self, session_id: str | None) -> str:
        if session_id:
            try:
                await self._remote.get_session(session_id)
                return session_id
            except RemoteSessionNotFoundError:
                created = await self._remote.create_session(session_id)
                return created.session.session_id
        created = await self._remote.create_session(None)
        return created.session.session_id

    async def _resolve_target(
        self,
        step: ComputerUsePlanStep,
        *,
        owner: ActionOwner,
        session_id: str,
        action_kind: ActionKind,
    ) -> TargetRef | None:
        if action_kind in {ActionKind.launch_app, ActionKind.hotkey, ActionKind.type_text, ActionKind.integration_action}:
            return None
        description = step.target_description or step.instruction
        result = await self._remote.resolve_target(
            session_id,
            TargetQuery(
                description=description,
                selector_hints=dict(step.selector_hints),
                allowed_strategies=_strategies_for_environment(step.environment),
                minimum_confidence=0.5,
            ),
            owner=owner,
            browser_session_id=None,
        )
        if result.target is None:
            raise RemoteHostOperationError("target_not_found")
        return result.target


class PlanExecuteComputerUseOrchestrator:
    """Coordinator for the plan-first, subagent-style computer-use architecture."""

    def __init__(
        self,
        *,
        planner: PlannerProtocol,
        remote: RemoteWindowsHostControl,
        auto_approve_risk_threshold: RiskLevel,
        execution_mode: str = "hybrid",
        vision_provider: str | None = None,
        vision_recovery: bool = False,
        supervisor_model_factory: Callable[[], Any] | None = None,
        max_replans: int = 2,
        max_steps: int = 20,
    ):
        self._planner = planner
        self._executor = ComputerUseStepExecutor(
            remote,
            vision_provider=vision_provider,
            vision_recovery=vision_recovery,
            supervisor_model_factory=supervisor_model_factory,
        )
        self._auto_approve_risk_threshold = auto_approve_risk_threshold
        self._execution_mode = execution_mode
        self._max_replans = max_replans
        self._max_steps = max_steps

    async def run(
        self,
        prompt: str,
        *,
        owner: ActionOwner,
        session_id: str | None = None,
        browser_session_id: str | None = None,
        context: dict[str, Any] | None = None,
        on_activity_update: Callable[[str, str, str | None], None] | None = None,
        on_plan_update: Callable[[ComputerUsePlan, list[StepExecutionResult], str | None], None] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> PlanExecuteRunResult:
        request_count = 1
        context_payload = _planner_context(context)
        
        if on_activity_update:
            on_activity_update("lead", "Generating plan...", None)
            
        plan = await self._planner.plan(prompt, context=context_payload)
        if self._execution_mode == "vision_only":
            for step in plan.steps:
                step.requires_vision = True
        plan = _trim_plan(plan, self._max_steps)
        results: list[StepExecutionResult] = []
        if on_plan_update:
            on_plan_update(plan, results, None)
        replan_count = 0
        current_session_id = session_id

        # --- Pause for Plan Approval ---
        if on_activity_update:
            on_activity_update("lead", "Waiting for plan approval...", None)
            
        # Ensure session exists to attach the action to it
        if current_session_id is None:
            current_session_id = await self._executor._ensure_session(None)

        approval_action = _plan_approval_action(plan, current_session_id)
        stored_approval = await self._executor._remote.submit_action(approval_action, owner)
        if stored_approval.status == ActionLifecycleStatus.pending:
            stored_approval = await self._executor._remote.execute_action(stored_approval.action.action_id, owner)
        while stored_approval.status == ActionLifecycleStatus.awaiting_approval:
            await asyncio.sleep(1.0)
            if cancel_check and await cancel_check():
                return PlanExecuteRunResult(
                    status=StepStatus.failed,
                    plan=plan,
                    steps=[],
                    replan_count=0,
                    request_count_estimate=request_count,
                    summary="Cancelled during plan approval wait."
                )
            stored_approval = await self._executor._remote.get_action(stored_approval.action.action_id, owner)

        if stored_approval.status in {ActionLifecycleStatus.denied, ActionLifecycleStatus.cancelled, ActionLifecycleStatus.failed}:
            return PlanExecuteRunResult(
                status=StepStatus.failed,
                plan=plan,
                steps=[],
                replan_count=0,
                request_count_estimate=request_count,
                summary="Plan was rejected by user."
            )

        if stored_approval.status == ActionLifecycleStatus.approved:
            final_approval = await self._executor._remote.execute_action(stored_approval.action.action_id, owner)
            if final_approval.status in {ActionLifecycleStatus.failed, ActionLifecycleStatus.denied, ActionLifecycleStatus.cancelled}:
                return PlanExecuteRunResult(
                    status=StepStatus.failed,
                    plan=plan,
                    steps=[],
                    replan_count=0,
                    request_count_estimate=request_count,
                    summary="Plan was rejected by user."
                )
        # --- End Pause ---

        while True:
            if cancel_check and await cancel_check():
                break
            blocked = None
            for step in _remaining_steps(plan, results):
                if cancel_check and await cancel_check():
                    break
                if on_plan_update:
                    on_plan_update(plan, results, step.step_id)
                # Brief settle between steps so the UI from the previous step has
                # finished rendering before the next step observes/acts.
                if results:
                    await asyncio.sleep(0.5)
                current_session_id, result = await self._executor.execute(
                    step,
                    owner=owner,
                    session_id=current_session_id,
                    auto_approve_risk_threshold=self._auto_approve_risk_threshold,
                    on_activity_update=on_activity_update,
                    cancel_check=cancel_check,
                )
                results.append(result)
                if on_plan_update:
                    on_plan_update(plan, results, None)
                if result.status == StepStatus.failed:
                    return PlanExecuteRunResult(
                        status=StepStatus.failed,
                        plan=plan,
                        steps=results,
                        replan_count=replan_count,
                        request_count_estimate=request_count,
                        summary=result.summary,
                    )
                if result.status == StepStatus.blocked:
                    blocked = result
                    break

            if blocked is None:
                return PlanExecuteRunResult(
                    status=StepStatus.completed,
                    plan=plan,
                    steps=results,
                    replan_count=replan_count,
                    request_count_estimate=request_count,
                    summary="Plan completed.",
                )
            if replan_count >= self._max_replans:
                return PlanExecuteRunResult(
                    status=blocked.status,
                    plan=plan,
                    steps=results,
                    replan_count=replan_count,
                    request_count_estimate=request_count,
                    summary=blocked.summary,
                )
            request_count += 1
            replan_count += 1
            
            plan = await self._planner.replan(
                prompt,
                plan=plan,
                completed_steps=results,
                blocked_step=blocked,
                context=context_payload,
            )
            if self._execution_mode == "vision_only":
                for step in plan.steps:
                    step.requires_vision = True
            plan = _trim_plan(plan, self._max_steps)
            if on_plan_update:
                on_plan_update(plan, results, None)

            # --- Pause for Replanned Plan Approval ---
            if on_activity_update:
                on_activity_update("lead", "Waiting for replan approval...", None)
            
            replan_approval_action = _plan_approval_action(plan, current_session_id, revised=True)
            stored_approval = await self._executor._remote.submit_action(replan_approval_action, owner)
            if stored_approval.status == ActionLifecycleStatus.pending:
                stored_approval = await self._executor._remote.execute_action(stored_approval.action.action_id, owner)
            while stored_approval.status == ActionLifecycleStatus.awaiting_approval:
                await asyncio.sleep(1.0)
                if cancel_check and await cancel_check():
                    return PlanExecuteRunResult(
                        status=StepStatus.failed,
                        plan=plan,
                        steps=results,
                        replan_count=replan_count,
                        request_count_estimate=request_count,
                        summary="Cancelled during plan approval wait."
                    )
                stored_approval = await self._executor._remote.get_action(stored_approval.action.action_id, owner)

            if stored_approval.status in {ActionLifecycleStatus.denied, ActionLifecycleStatus.cancelled, ActionLifecycleStatus.failed}:
                return PlanExecuteRunResult(
                    status=StepStatus.failed,
                    plan=plan,
                    steps=results,
                    replan_count=replan_count,
                    request_count_estimate=request_count,
                    summary="Plan was rejected by user."
                )

            if stored_approval.status == ActionLifecycleStatus.approved:
                final_approval = await self._executor._remote.execute_action(stored_approval.action.action_id, owner)
                if final_approval.status in {ActionLifecycleStatus.failed, ActionLifecycleStatus.denied, ActionLifecycleStatus.cancelled}:
                    return PlanExecuteRunResult(
                        status=StepStatus.failed,
                        plan=plan,
                        steps=results,
                        replan_count=replan_count,
                        request_count_estimate=request_count,
                        summary="Plan was rejected by user."
                    )
            # --- End Pause ---


def _remaining_steps(plan: ComputerUsePlan, results: list[StepExecutionResult]) -> list[ComputerUsePlanStep]:
    completed_or_failed = {result.step_id for result in results}
    return [step for step in plan.steps if step.step_id not in completed_or_failed]


def _plan_approval_action(plan: ComputerUsePlan, session_id: str, *, revised: bool = False) -> ActionCommand:
    label = "replan" if revised else "plan"
    reason = "User must review the revised computer-use plan before execution" if revised else "User must review the proposed computer-use plan before execution"
    return ActionCommand(
        action_id=f"{label}-approval-{uuid.uuid4().hex}",
        session_id=session_id,
        kind=ActionKind.integration_action,
        risk=RiskAssessment(level=RiskLevel.high, reasons=[reason]),
        args={"type": "plan_approval", "plan_json": plan.model_dump_json()},
    )


def _trim_plan(plan: ComputerUsePlan, max_steps: int) -> ComputerUsePlan:
    return plan.model_copy(update={"steps": plan.steps[:max_steps]})


def _infer_action_kind(step: ComputerUsePlanStep) -> ActionKind:
    instruction = step.instruction.lower()
    if "open " in instruction or "launch " in instruction or step.args.get("app_name"):
        return ActionKind.launch_app
    if "type" in instruction or "write" in instruction or step.args.get("text"):
        return ActionKind.type_text
    if "double" in instruction:
        return ActionKind.double_click
    return ActionKind.click


def _args_for_step(step: ComputerUsePlanStep, action_kind: ActionKind) -> dict[str, Any]:
    args = dict(step.args)
    if action_kind == ActionKind.launch_app:
        # Normalize whatever the planner provided (or the instruction) down to a real
        # executable/app name. A planner that sets app_name to a whole sentence like
        # "Microsoft Edge browser and navigate to Gmail" must not be typed into Start
        # search verbatim — extract just the app and map known browsers/apps.
        raw = str(args.get("app_name") or args.get("command") or "").strip() or step.instruction
        normalized = _normalize_app_name(raw)
        if normalized:
            args["app_name"] = normalized
    if action_kind == ActionKind.type_text and not str(args.get("text") or "").strip():
        inferred_text = _infer_type_text(step.instruction)
        if inferred_text:
            args["text"] = inferred_text
    return args


def _infer_type_text(instruction: str) -> str | None:
    """Best-effort extraction of the literal text to type from a step instruction.

    Used only when the planner set a type_text step without an explicit args.text.
    """
    quoted = re.search(r"['\"‘’“”]([^'\"‘’“”]+)['\"‘’“”]", instruction)
    if quoted:
        return quoted.group(1).strip()
    verb = re.search(r"\b(?:type|write|enter|input)\b\s*:?\s*(.+)", instruction, re.IGNORECASE)
    if verb:
        return verb.group(1).strip().strip(".")
    return None


def _action_risk(step: ComputerUsePlanStep) -> RiskAssessment:
    return RiskAssessment.model_validate(step.risk.model_dump(mode="python"))


def _vision_step_command(step: ComputerUsePlanStep, *, max_actions: int | None = None) -> str:
    resolved_max_actions = max_actions or step.max_actions
    return (
        f"CURRENT STEP ONLY: {step.instruction}\n"
        f"MAXIMUM ACTIONS FOR THIS STEP: {resolved_max_actions}\n"
        f"ACTION KIND: {step.action_kind.value if step.action_kind else 'infer from instruction'}\n"
        f"TARGET: {step.target_description or 'described by the instruction'}\n"
        f"SELECTOR HINTS: {json.dumps(step.selector_hints, ensure_ascii=False)}\n"
        f"ACTION ARGUMENTS: {json.dumps(step.args, ensure_ascii=False)}\n"
        f"COMPLETION CRITERION: {step.completion_criteria}\n"
        "Reason from the current screenshot. The UI may differ from the ideal path: popups, dialogs, cookie banners, ads, loading states, focus mismatch, localized labels, or disabled/covered controls may appear. "
        "If such a recoverable obstruction directly blocks this step, use the smallest safe action to dismiss, wait for, or bypass it, then continue this same step. "
        "Do not interact with unrelated content or choose a different user goal. "
        "Stop immediately with finish_step success when the completion criterion is satisfied. "
        "Do not perform any later step or double-check. "
        f"After at most {resolved_max_actions} action(s), return an ultimate finish_step success or failure decision."
    )


def _vision_action_limit(step: ComputerUsePlanStep, configured_vision_calls: int) -> int:
    configured_limit = configured_vision_calls or 1
    return max(1, min(max(step.max_actions, 4), configured_limit, 6))


def _rescale_action_for_screen(action: ActionCommand, scale: float) -> ActionCommand:
    """Map a vision action's coordinates from the downscaled sent image back to screen pixels."""
    updates: dict[str, Any] = {}
    target = action.target
    if target is not None and target.strategy == TargetStrategy.coordinate and isinstance(target.selector, dict):
        point = target.selector.get("point")
        if isinstance(point, (list, tuple)) and len(point) == 2:
            sx, sy = scale_point(point, scale)
            new_selector = {**target.selector, "point": [sx, sy]}
            target_updates: dict[str, Any] = {"selector": new_selector}
            if target.bounds is not None:
                target_updates["bounds"] = target.bounds.model_copy(update={"x": sx, "y": sy})
            updates["target"] = target.model_copy(update=target_updates)
    end = action.args.get("end")
    if isinstance(end, (list, tuple)) and len(end) == 2:
        ex, ey = scale_point(end, scale)
        updates["args"] = {**action.args, "end": [ex, ey]}
    return action.model_copy(update=updates) if updates else action


def _vision_action_signature(action: ActionCommand) -> str | None:
    """Stable signature of a vision action for no-progress detection.

    Terminate/finish signals are excluded (they are not repeatable work).
    """
    if action.args.get("action") == "terminate":
        return None
    point = None
    if action.target is not None and isinstance(action.target.selector, dict):
        point = action.target.selector.get("point")
    parts = [
        action.kind.value,
        json.dumps(point, sort_keys=True) if point is not None else "",
        str(action.args.get("text", "")),
        str(action.args.get("keys", "")),
        str(action.args.get("direction", "")),
    ]
    return "|".join(parts)


def _exception_summary(exc: Exception) -> str:
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__


def _planner_context(context: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(context or {})
    payload.setdefault("windows_ui_language", _windows_ui_language())
    return payload


def _windows_ui_language() -> str:
    if os.name == "nt":
        try:
            import ctypes

            language_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            language = locale.windows_locale.get(language_id)
            if language:
                return language.replace("_", "-")
        except Exception:
            pass
    language = locale.getlocale()[0]
    return language.replace("_", "-") if language else "unknown"


def _normalize_app_name(raw: str) -> str | None:
    """Reduce a launch instruction/app_name to a single launchable app name.

    This is a generic safety net only — it does NOT hard-code any app→executable
    mapping. The planner is responsible for putting a real executable/app name in
    args.app_name (e.g. "msedge"); this just strips a leading verb, any trailing
    clause, and filler words so a stray sentence like
    "Launch Microsoft Edge browser and navigate to Gmail" becomes "Microsoft Edge"
    (which the launcher resolves via the executable, App Paths, or Start search).
    """
    text = raw.strip()
    verb = re.search(r"\b(?:open|launch|start|run)\b\s+(.+)", text, re.IGNORECASE)
    candidate = verb.group(1) if verb else text
    # Keep only the app, dropping any trailing clause ("... and navigate to Gmail").
    candidate = re.split(r"\b(?:and|then|to|in|with)\b|[,;]", candidate, maxsplit=1, flags=re.IGNORECASE)[0]
    # Drop filler words that are never part of an app/executable name.
    candidate = re.sub(r"\b(?:the|a|an|browser|application|app|window|program)\b", " ", candidate, flags=re.IGNORECASE)
    candidate = " ".join(candidate.split()).strip(" .,:;\"'")
    return candidate or None


# Backwards-compatible alias.
def _infer_app_name(instruction: str) -> str | None:
    return _normalize_app_name(instruction)


def _strategies_for_environment(environment: EnvironmentContext) -> list[TargetStrategy]:
    if environment == EnvironmentContext.browser:
        return [TargetStrategy.browser, TargetStrategy.vision, TargetStrategy.uia]
    return [TargetStrategy.uia, TargetStrategy.app, TargetStrategy.vision, TargetStrategy.coordinate]


def _step_status_from_action(status: ActionLifecycleStatus) -> StepStatus:
    if status == ActionLifecycleStatus.succeeded:
        return StepStatus.completed
    if status in {ActionLifecycleStatus.awaiting_approval, ActionLifecycleStatus.pending, ActionLifecycleStatus.approved, ActionLifecycleStatus.executing}:
        return StepStatus.running
    if status in {ActionLifecycleStatus.denied, ActionLifecycleStatus.cancelled}:
        return StepStatus.blocked
    return StepStatus.failed


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        stripped = stripped[start : end + 1]
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("planner response must be a JSON object")
    return data


def _fallback_plan(prompt: str) -> ComputerUsePlan:
    return ComputerUsePlan(
        goal=prompt,
        steps=[
            ComputerUsePlanStep(
                step_id="s1",
                instruction=prompt,
                environment=EnvironmentContext.native,
                requires_vision=True,
                target_description=prompt,
                risk=PlannedRiskAssessment(
                    level=RiskLevel.medium,
                    reasons=["Fallback plan could not be assessed by the planner."],
                    consequences=["The requested desktop action may change application state."],
                ),
            )
        ],
    )
