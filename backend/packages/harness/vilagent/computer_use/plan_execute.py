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
from vilagent.computer_use.browser_playwright import (
    PlaywrightBrowserSession,
    PlaywrightUnavailableError,
    get_shared_browser_session,
)
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
    max_actions: int = Field(default=8, ge=1, le=16)
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
{"goal":"...","steps":[{"step_id":"s1","instruction":"a clear sub-goal","completion_criteria":"one observable proof this sub-goal is done","max_actions":8,"environment":"browser|native","requires_vision":true,"action_kind":"click|type_text|hotkey|launch_app|browser_action","target_description":"...","selector_hints":{},"args":{},"risk":{"level":"low|medium|high|critical","reasons":["..."],"consequences":["..."]},"requires_verification":true}]}

How execution works (plan for THIS, do not micro-manage):
- A vision step is handed to FARA, a capable GUI agent that SEES the screen and performs AS MANY actions as the sub-goal needs (look, click, type, press keys, scroll, dismiss a popup, retry) until the step's completion_criteria is met. So a vision step should be a MEANINGFUL SUB-GOAL, not a single click. FARA handles the small motor actions, focus moves, autocomplete confirmations, and obstructions on its own — you do NOT need a separate step for every click or key press.
- A deterministic step is executed exactly once by the runtime (no vision): launching an app, typing a known literal string, or pressing a known hotkey.

Rules:
- Plan, never execute. Do not ask for screenshots or session ids.
- environment: 'native' Windows desktop UI, or 'browser' for anything on the web.
- requires_vision: false ONLY when the command has an unambiguous keyboard, text-entry, UIA, or DOM equivalent (a fixed app launch, a known literal to type, or a fixed hotkey); true when the screen must be looked at. Prefer deterministic input steps over visual actions when the command is fixed and unambiguous; otherwise use a vision step.
- To open a desktop app, use ONE launch_app step (action_kind="launch_app", requires_vision=false) with args.app_name set to the app's Windows EXECUTABLE name (the short command you would type in the Run dialog). Give a single launchable name, NEVER a sentence, URL, or goal.
- To type a fixed literal (a known number, code, or exact text) into an already-focused field, you may use a deterministic type_text step (action_kind="type_text", requires_vision=false, exact string with its symbols in args.text). When the field must first be found/focused on screen, fold the whole "click the field and type X" into a single vision step instead.
- WEB / BROWSER tasks run in a dedicated browser the runtime manages for you. Do NOT plan a launch_app step to open a browser, and do NOT switch to the desktop for web work. Mark every web step environment="browser". Make the FIRST browser step a navigation: action_kind="browser_action", args.action="visit_url", args.url the full "https://..." URL. After that, each browser step is a page sub-goal (e.g. "search for X and open the first result", "fill the login form with user U and password P and submit") that FARA carries out by acting on the real page.
- Keep steps at the SUB-GOAL altitude: one coherent outcome per step (e.g. "compose and send an email to alice@x.com with subject S and body B", "log in with these credentials", "add item I to the cart"). Do not split a single coherent interaction into one-click steps, and do not bundle unrelated goals into one step. Put a genuine verification/decision (e.g. "confirm the order total is correct before paying") in its own step.
- Write each step's instruction in 1-3 plain sentences: WHAT outcome to reach, the concrete specifics (names, exact text, URLs, values, order), and how to recognise success. Include autocomplete/confirmation hints when relevant (e.g. "after typing the recipient, press Enter to pick the highlighted suggestion before moving on"). Be specific; never vague ("handle the page") and never padded.
- Put known values in args (app_name, text, keys, url) and canonical hotkeys (ENTER, ESC, CTRL+L) in args.keys. Set completion_criteria to the exact observable end state.
- max_actions: how many actions FARA may take for that sub-goal (8 typical; up to 12-16 for multi-field forms or pages with overlays/loading). It is a budget, not a command count.
- Use context.windows_ui_language for localized controls. Assess risk per step (critical = the UI's Very High).
- Up to 12 steps; prefer fewer, well-scoped sub-goals.
"""


# The recovery supervisor is a stronger vision+reasoning model (the selected
# planner model, e.g. a cloud Qwen3-VL). It is only called when the fast action
# model (FARA) is stuck, so keep this prompt short and decisive.
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
        # FARA is the only vision provider; this field is retained for signature
        # compatibility with the orchestrator/router but is always FARA.
        self._vision_provider = vision_provider
        # Optional recovery supervisor: when on, a stronger reasoning model is
        # consulted only when the fast vision model is stuck. The factory builds the
        # model (the selected planner model OR a dedicated env-configured GLM-V).
        # Fully removable — with vision_recovery=False there is zero behaviour change.
        self._vision_recovery = vision_recovery and supervisor_model_factory is not None
        self._supervisor_model_factory = supervisor_model_factory
        self._detected_vision_models: dict[tuple[str | None, str | None, str], str] = {}
        # Lazily-created dedicated Playwright browser, reused across all browser steps
        # of a run and closed by the orchestrator at the end.
        self._browser_session: PlaywrightBrowserSession | None = None

    async def _build_fara_provider(self, config) -> FaraVisionActionProvider:
        """Construct the FARA action provider bound to the detected served model.

        Raises on an unreachable endpoint so callers can fail the step cleanly.
        """
        base_url = config.computer_use.vision_fara_model.base_url
        api_key = config.computer_use.vision_fara_model.api_key or "not-needed"
        default_model = config.computer_use.vision_fara_model.model_name
        detected_model = await _detect_served_model_name_once(
            self._detected_vision_models,
            base_url,
            api_key,
            default_model,
        )
        return FaraVisionActionProvider(
            ComputerUseFaraModelConfig(
                enabled=True,
                model_name=detected_model,
                base_url=base_url,
                api_key=api_key,
                timeout_seconds=config.computer_use.vision_fara_model.timeout_seconds,
            )
        )

    async def _ensure_browser_session(self, config) -> PlaywrightBrowserSession:
        # Reuse a persistent, shared browser so it stays open after the task finishes
        # (the operator can inspect the result) and is reused by the next run.
        self._browser_session = await get_shared_browser_session(
            headless=config.computer_use.browser.playwright_headless,
            viewport_width=config.computer_use.browser.viewport_width,
            viewport_height=config.computer_use.browser.viewport_height,
        )
        return self._browser_session

    async def close_browser(self) -> None:
        # Intentionally a no-op: the shared browser persists across runs and is only
        # torn down on emergency stop or app shutdown (close_shared_browser_session).
        self._browser_session = None

    def _selected_vision_provider(self, config) -> str:
        return "fara"

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
            # Browser steps are driven through a dedicated Playwright browser (real
            # DOM mouse/keyboard/navigation), never by pixel-clicking the desktop.
            if step.environment == EnvironmentContext.browser:
                return await self._execute_browser_step(
                    step,
                    action_kind=action_kind,
                    session_id=resolved_session_id,
                    config=config,
                    on_activity_update=on_activity_update,
                    cancel_check=cancel_check,
                )
            # Typing, launching, and hotkeys are deterministic keyboard operations
            # and must never be done by visually clicking on-screen keys (which is
            # unreliable and lets the vision model falsely 'finish' after one click).
            if step.requires_vision and action_kind not in {ActionKind.launch_app, ActionKind.hotkey, ActionKind.type_text}:
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

        try:
            provider: Any = await self._build_fara_provider(config)
        except Exception as e:
            return session_id, StepExecutionResult(
                step_id=step.step_id,
                environment=step.environment,
                requires_vision=step.requires_vision,
                status=StepStatus.failed,
                error_code="vision_model_unreachable",
                summary=f"Vision model unreachable: {_exception_summary(e)}",
            )

        chat_history: list[dict[str, Any]] = []
        repeated_signature: str | None = None
        repeated_count = 0
        supervisor_calls = 0
        nudge_count = 0
        real_actions = 0
        # Multi-action budget: the step may need several actions before FARA declares
        # finish_step (e.g. focus a field, then type, then confirm). Give a generous cap.
        max_attempts = max(1, min(max_actions, 12))
        if self._vision_recovery:
            # Give the supervisor-guided retries room beyond the base action budget.
            max_attempts = min(max_attempts + 2 * _MAX_SUPERVISOR_CALLS, 14)
        last_action_id = ""
        last_status = ActionLifecycleStatus.failed
        last_error_code: str | None = None
        model_calls = 0
        succeeded_any = False

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
                    max_dim=getattr(config.computer_use, "vision_max_image_dimension", 0),
                    jpeg_quality=getattr(config.computer_use, "vision_jpeg_quality", 85),
                )

                try:
                    model_calls += 1
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
                    if model_calls < max_attempts:
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
                    # Multi-action step: a successful action does NOT end the step. FARA
                    # keeps going (observe -> act) until it emits finish_step, its own
                    # completion signal. The provider already fed a success tool_response
                    # into chat_history, so just continue. Only a spent budget ends it.
                    succeeded_any = True
                    repeated_signature = None
                    repeated_count = 0
                    if on_activity_update:
                        on_activity_update("vision_executor", f"Action {real_actions} done; continuing the step.", thought)
                    if real_actions >= max_attempts:
                        return session_id, StepExecutionResult(
                            step_id=step.step_id,
                            environment=step.environment,
                            requires_vision=step.requires_vision,
                            status=StepStatus.completed,
                            action_id=last_action_id,
                            action_status=last_status,
                            summary=f"Vision step reached its {max_attempts}-action budget after successful actions.",
                        )
                    continue

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
        
        # Budget/noop headroom exhausted without an explicit finish_step. If real
        # actions succeeded along the way, treat the step as completed (the work
        # happened, FARA just never declared done); otherwise report the last error.
        if succeeded_any:
            return session_id, StepExecutionResult(
                step_id=step.step_id,
                environment=step.environment,
                requires_vision=step.requires_vision,
                status=StepStatus.completed,
                action_id=last_action_id,
                action_status=ActionLifecycleStatus.succeeded,
                summary="Vision step ended after its successful actions (no explicit finish_step).",
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

    async def _execute_browser_step(
        self,
        step: ComputerUsePlanStep,
        *,
        action_kind: ActionKind,
        session_id: str,
        config: Any,
        on_activity_update: Callable[[str, str, str | None], None] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[str, StepExecutionResult]:
        """Run a ``browser`` environment step against the dedicated Playwright browser."""
        try:
            session = await self._ensure_browser_session(config)
        except PlaywrightUnavailableError as exc:
            return session_id, StepExecutionResult(
                step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                status=StepStatus.failed, error_code="playwright_unavailable", summary=str(exc),
            )
        except Exception as exc:
            return session_id, StepExecutionResult(
                step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                status=StepStatus.failed, error_code="browser_session_failed",
                summary=f"Could not start the browser: {_exception_summary(exc)}",
            )

        # The Playwright browser IS the browser, so a "launch the browser" step is not a
        # desktop app launch. If the step (or its instruction) carries a URL, navigate to
        # it now so the browser does not sit on about:blank; otherwise it is a no-op.
        if action_kind == ActionKind.launch_app:
            url = _first_url(step.args.get("url"), step.target_description, step.instruction)
            if url:
                action = ActionCommand(
                    action_id=f"browser-step-{step.step_id}-{uuid.uuid4().hex}",
                    session_id=session_id, kind=ActionKind.browser_action,
                    args={"action": "visit_url", "url": url}, risk=_action_risk(step),
                )
                if on_activity_update:
                    on_activity_update("browser_executor", f"Opening {url}", None)
                ok, err = await session.run_action(action)
                return session_id, StepExecutionResult(
                    step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                    status=StepStatus.completed if ok else StepStatus.failed,
                    action_id=action.action_id,
                    action_status=ActionLifecycleStatus.succeeded if ok else ActionLifecycleStatus.failed,
                    error_code=None if ok else err, summary=f"visit_url {url} -> {'ok' if ok else err}",
                )
            return session_id, StepExecutionResult(
                step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                status=StepStatus.completed, summary="Browser is managed by Playwright; no app launch needed.",
            )

        # Deterministic browser commands (navigate / type / hotkey) run once directly.
        if not step.requires_vision and action_kind in {ActionKind.browser_action, ActionKind.type_text, ActionKind.hotkey}:
            action = self._build_browser_deterministic_action(step, action_kind, session_id)
            if on_activity_update:
                on_activity_update("browser_executor", step.instruction, None)
            ok, err = await session.run_action(action)
            return session_id, StepExecutionResult(
                step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                status=StepStatus.completed if ok else StepStatus.failed,
                action_id=action.action_id,
                action_status=ActionLifecycleStatus.succeeded if ok else ActionLifecycleStatus.failed,
                error_code=None if ok else err,
                summary=f"{action_kind.value} -> {'ok' if ok else err}",
            )

        return await self._execute_browser_vision_loop(
            step,
            session=session,
            session_id=session_id,
            config=config,
            max_actions=_vision_action_limit(step, config.computer_use.budgets.vision_calls),
            on_activity_update=on_activity_update,
            cancel_check=cancel_check,
        )

    def _build_browser_deterministic_action(
        self, step: ComputerUsePlanStep, action_kind: ActionKind, session_id: str
    ) -> ActionCommand:
        if action_kind == ActionKind.browser_action:
            args = dict(step.args)
            if not args.get("action"):
                args["action"] = "visit_url" if args.get("url") else "refresh"
        else:
            args = _args_for_step(step, action_kind)
        return ActionCommand(
            action_id=f"browser-step-{step.step_id}-{uuid.uuid4().hex}",
            session_id=session_id,
            kind=action_kind,
            args=args,
            risk=_action_risk(step),
        )

    async def _execute_browser_vision_loop(
        self,
        step: ComputerUsePlanStep,
        *,
        session: PlaywrightBrowserSession,
        session_id: str,
        config: Any,
        max_actions: int,
        on_activity_update: Callable[[str, str, str | None], None] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[str, StepExecutionResult]:
        """FARA drives the Playwright browser: screenshot -> action -> execute, looped."""
        if on_activity_update:
            on_activity_update("browser_executor", f"Running browser step: {step.instruction}", None)
        try:
            provider = await self._build_fara_provider(config)
        except Exception as e:
            return session_id, StepExecutionResult(
                step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                status=StepStatus.failed, error_code="vision_model_unreachable",
                summary=f"Vision model unreachable: {_exception_summary(e)}",
            )

        chat_history: list[dict[str, Any]] = []
        repeated_signature: str | None = None
        repeated_count = 0
        supervisor_calls = 0
        nudge_count = 0
        real_actions = 0
        model_calls = 0
        succeeded_any = False
        last_error_code: str | None = None
        # Browser sub-goals are coarser (navigate, then act on the page), so allow more
        # actions before FARA must declare finish_step.
        max_attempts = max(1, min(max_actions, 16))
        if self._vision_recovery:
            max_attempts = min(max_attempts + 2 * _MAX_SUPERVISOR_CALLS, 18)

        def _failed(error_code: str, summary: str) -> tuple[str, StepExecutionResult]:
            return session_id, StepExecutionResult(
                step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                status=StepStatus.failed, error_code=error_code, summary=summary,
            )

        for _index in range(max_attempts + _MAX_VISION_NOOPS):
            if cancel_check and await cancel_check():
                return _failed("client_disconnected", "Client disconnected during execution")
            try:
                image_bytes = await session.screenshot()
                image_base64, image_media_type, coord_scale = encode_image_for_vision(
                    image_bytes,
                    max_dim=getattr(config.computer_use, "vision_max_image_dimension", 0),
                    jpeg_quality=getattr(config.computer_use, "vision_jpeg_quality", 85),
                )
                try:
                    model_calls += 1
                    action, new_history = await provider.get_next_action(
                        instruction=_vision_step_command(step, max_actions=max_attempts, current_url=session.current_url),
                        image_base64=image_base64,
                        chat_history=chat_history,
                        environment="browser",
                        max_actions=max_attempts,
                        image_media_type=image_media_type,
                    )
                except Exception as e:
                    last_error_code = _exception_summary(e)
                    if model_calls < max_attempts:
                        await asyncio.sleep(0.5)
                        continue
                    return _failed("vision_action_failed", f"Failed to get next browser action: {last_error_code}")

                chat_history = list(new_history or [])[-6:]
                thought = action.args.get("thought") if action else None
                if on_activity_update:
                    on_activity_update("browser_executor", "Evaluating browser state...", thought)
                if not action:
                    return _failed("fara_disabled", "Fara model is disabled or unreachable")
                if coord_scale != 1.0:
                    action = _rescale_action_for_screen(action, coord_scale)

                op = action.args.get("action")
                if op == "terminate":
                    if action.args.get("status") == "failure":
                        return _failed("fara_terminate_failure", "Fara decided to terminate with failure")
                    return session_id, StepExecutionResult(
                        step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                        status=StepStatus.completed, action_status=ActionLifecycleStatus.succeeded,
                        summary="Fara completed the browser step successfully",
                    )
                if action.kind == ActionKind.browser_action and op in {"wait", "mouse_move"}:
                    if op == "wait":
                        try:
                            wait_seconds = float(action.args.get("time") or 1.0)
                        except (TypeError, ValueError):
                            wait_seconds = 1.0
                        await asyncio.sleep(max(0.0, min(wait_seconds, 3.0)))
                    if on_activity_update:
                        on_activity_update("browser_executor", f"Vision model requested '{op}'; re-observing.", thought)
                    continue

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
                            step=step, recent_thought=thought, image_base64=image_base64,
                            image_media_type=image_media_type, on_activity_update=on_activity_update,
                        )
                        supervisor_calls += 1
                    if advice is None and nudge_count < _MAX_VISION_NUDGES:
                        nudge_count += 1
                        advice = _GENERIC_STUCK_NUDGE
                    if advice:
                        repeated_signature = None
                        repeated_count = 0
                        chat_history.append({
                            "role": "user",
                            "content": f"<supervisor>\n{advice}\n</supervisor>\nDo exactly this now, then continue the original step.",
                        })
                        continue
                    return _failed("no_progress_repeated_action", "Vision model repeated the same action without making progress.")

                real_actions += 1
                ok, err = await session.run_action(action)
                if ok:
                    # Multi-action step: keep going until FARA emits finish_step.
                    succeeded_any = True
                    repeated_signature = None
                    repeated_count = 0
                    if on_activity_update:
                        on_activity_update("browser_executor", f"Action {real_actions} done; continuing the step.", thought)
                    if real_actions >= max_attempts:
                        return session_id, StepExecutionResult(
                            step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                            status=StepStatus.completed, action_status=ActionLifecycleStatus.succeeded,
                            summary=f"Browser step reached its {max_attempts}-action budget after successful actions.",
                        )
                    continue
                last_error_code = err
                if real_actions >= max_attempts:
                    return _failed("browser_step_failed", f"Browser step failed after {max_attempts} action(s) ({err}).")
                chat_history.append({
                    "role": "user",
                    "content": f'<tool_response>\n{{"status": "retry", "error": "{err}"}}\n</tool_response>',
                })
            except Exception as exc:
                return _failed(exc.__class__.__name__, f"browser loop failed: {exc}")

        if succeeded_any:
            return session_id, StepExecutionResult(
                step_id=step.step_id, environment=step.environment, requires_vision=step.requires_vision,
                status=StepStatus.completed, action_status=ActionLifecycleStatus.succeeded,
                summary="Browser step ended after its successful actions (no explicit finish_step).",
            )
        return _failed(last_error_code or "browser_step_incomplete", f"Browser vision loop ended ({last_error_code}).")

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
        try:
            return await self._run(
                prompt,
                owner=owner,
                session_id=session_id,
                browser_session_id=browser_session_id,
                context=context,
                on_activity_update=on_activity_update,
                on_plan_update=on_plan_update,
                cancel_check=cancel_check,
            )
        finally:
            # Always tear down the dedicated Playwright browser at the end of a run.
            await self._executor.close_browser()

    async def _run(
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


def _vision_step_command(step: ComputerUsePlanStep, *, max_actions: int | None = None, current_url: str | None = None, goal: str | None = None) -> str:
    resolved_max_actions = max_actions or step.max_actions
    context_lines = ""
    if goal:
        context_lines += f"OVERALL GOAL (for context only): {goal}\n"
    if current_url is not None:
        where = current_url or "about:blank (a blank page)"
        context_lines += f"CURRENT PAGE: {where}\n"
        if not current_url or current_url == "about:blank":
            context_lines += "The browser is on a blank page; if this step needs a website, FIRST navigate there with browser_action visit_url before anything else.\n"
    return (
        f"CURRENT STEP: {step.instruction}\n"
        f"{context_lines}"
        f"MAXIMUM ACTIONS FOR THIS STEP: {resolved_max_actions}\n"
        f"ACTION KIND: {step.action_kind.value if step.action_kind else 'infer from instruction'}\n"
        f"TARGET: {step.target_description or 'described by the instruction'}\n"
        f"SELECTOR HINTS: {json.dumps(step.selector_hints, ensure_ascii=False)}\n"
        f"ACTION ARGUMENTS: {json.dumps(step.args, ensure_ascii=False)}\n"
        f"COMPLETION CRITERION: {step.completion_criteria}\n"
        "Reason from the current screenshot and perform as many actions as needed to accomplish THIS step (e.g. focus a field, type, then confirm). "
        "The UI may differ from the ideal path: popups, dialogs, cookie banners, ads, loading states, focus mismatch, localized labels, or disabled/covered controls may appear. "
        "If such a recoverable obstruction directly blocks this step, use the smallest safe action to dismiss, wait for, or bypass it, then continue this same step. "
        "Do not interact with unrelated content or pursue a later step / different user goal. "
        "Return finish_step success as soon as THIS step's completion criterion is satisfied; return finish_step failure if it cannot be done. "
        f"You have at most {resolved_max_actions} action(s) for this step."
    )


def _vision_action_limit(step: ComputerUsePlanStep, configured_vision_calls: int) -> int:
    # The planner's per-step max_actions is the budget for that sub-goal, clamped to a
    # sane multi-action range. Plan-execute runs a bit more generously than the typical
    # budget; browser sub-goals get extra headroom over native ones.
    cap = 16 if step.environment == EnvironmentContext.browser else 12
    return max(4, min(step.max_actions, cap))


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _first_url(*candidates: Any) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        match = _URL_RE.search(str(candidate))
        if match:
            return match.group(0).rstrip(".,;)")
    return None


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
