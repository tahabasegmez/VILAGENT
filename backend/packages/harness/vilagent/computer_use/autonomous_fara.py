"""Autonomous FARA orchestrator — an alternative to plan-and-execute.

Instead of a planner decomposing the task into steps that are fed to FARA one at a
time, this approach:

1. The selected planner/instruction model rewrites the user's request into ONE clear,
   complete paragraph brief for FARA (and classifies the environment: browser/native).
2. FARA then carries out the WHOLE task autonomously in a single continuous agent loop
   (observe -> act -> observe ...), until it decides the task is finished.

It reuses the same building blocks as plan-execute (FARA provider, the Playwright
browser session, the Windows host) but bypasses the per-step plan structure. It returns
a ``PlanExecuteRunResult`` (with a one-item synthetic plan) so the gateway and operator
UI render it exactly like a plan-execute run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleStatus,
    ActionOwner,
    RiskAssessment,
    RiskLevel,
)
from vilagent.computer_use.fara import FaraVisionActionProvider
from vilagent.computer_use.browser_playwright import (
    PlaywrightBrowserSession,
    PlaywrightUnavailableError,
    get_shared_browser_session,
)
from vilagent.computer_use.image_ops import encode_image_for_vision
from vilagent.computer_use.remote_host import RemoteSessionNotFoundError, RemoteWindowsHostControl
from vilagent.computer_use.plan_execute import (
    ComputerUsePlan,
    ComputerUsePlanStep,
    EnvironmentContext,
    PlanExecuteRunResult,
    PlannedRiskAssessment,
    StepExecutionResult,
    StepStatus,
    _detect_served_model_name_once,
    _exception_summary,
    _extract_json_object,
    _message_text,
    _rescale_action_for_screen,
    _vision_action_signature,
)
from vilagent.config.computer_use_config import ComputerUseFaraModelConfig
from vilagent.config.app_config import get_app_config
from vilagent.models import create_chat_model

logger = logging.getLogger(__name__)


_DIRECTIVE_SYSTEM_PROMPT = """\
You are the briefing layer for FARA, an autonomous computer-use agent that operates a
Windows machine (desktop apps and a web browser) by looking at the screen and using the
mouse and keyboard. The user gives a task. Turn it into ONE clear, complete instruction
for FARA and decide where it runs.

Output ONLY one JSON object: {"environment":"browser|native","directive":"..."}.
- environment: "browser" if the task is primarily done on the web (websites, web apps,
  search, webmail), "native" if it is primarily a Windows desktop application.
- directive: a single self-contained paragraph, in clear plain language, that tells FARA
  exactly what to accomplish from start to finish and what the finished result looks like.
  Include the concrete specifics from the user (names, text to type, URLs, values, order
  of operations) so FARA never has to guess. Do NOT write numbered steps or pseudo-code;
  write it as natural prose FARA can follow. Do NOT add commentary outside the JSON."""


# In autonomous mode a successful action does NOT end the run; only FARA's terminate or
# the action budget does. Keep a generous-but-bounded budget and noop headroom.
_DEFAULT_MAX_ACTIONS = 40
_MAX_NOOPS = 12
_MAX_NUDGES = 3
_MAX_MODEL_ERRORS = 4

_GENERIC_STUCK_NUDGE = (
    "You repeated the same action with no visible effect. Re-examine the current "
    "screenshot carefully and try a clearly DIFFERENT location or a different action "
    "(scroll the target into view, dismiss an overlay, or click a different element). "
    "Do not repeat the previous coordinates."
)


class AutonomousFaraOrchestrator:
    """Run an entire task with a single autonomous FARA loop."""

    def __init__(
        self,
        *,
        instruction_model_name: str,
        remote: RemoteWindowsHostControl,
        auto_approve_risk_threshold: RiskLevel,
        max_actions: int = _DEFAULT_MAX_ACTIONS,
    ):
        self._instruction_model_name = instruction_model_name
        self._remote = remote
        self._threshold = auto_approve_risk_threshold
        self._max_actions = max(4, max_actions)
        self._browser_session: PlaywrightBrowserSession | None = None
        self._fara_provider = None

    async def run(
        self,
        prompt: str,
        *,
        owner: ActionOwner,
        session_id: str | None = None,
        browser_session_id: str | None = None,
        context: dict[str, Any] | None = None,
        on_activity_update: Callable[[str, str, str | None], None] | None = None,
        on_plan_update: Callable[[Any, list[Any], str | None], None] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> PlanExecuteRunResult:
        config = get_app_config()
        if on_activity_update:
            on_activity_update("lead", "Writing the task brief for FARA...", None)

        environment, directive = await self._make_directive(prompt)
        plan = _single_step_plan(prompt, directive, environment)
        if on_plan_update:
            on_plan_update(plan, [], plan.steps[0].step_id)

        try:
            provider = await self._build_provider(config)
        except Exception as exc:
            return _result(plan, StepStatus.failed, f"Vision model unreachable: {_exception_summary(exc)}", error_code="vision_model_unreachable")

        if on_activity_update:
            role = "browser" if environment == EnvironmentContext.browser else "vision"
            on_activity_update(role, f"FARA is running the task autonomously ({environment.value}).", directive[:200])

        try:
            if environment == EnvironmentContext.browser:
                status, summary, error_code = await self._run_browser(directive, config, provider, on_activity_update, cancel_check)
            else:
                status, summary, error_code = await self._run_native(directive, config, provider, session_id, owner, on_activity_update, cancel_check)
        finally:
            await self._close_browser()

        result = _result(plan, status, summary, error_code=error_code)
        fara = self._fara_provider
        result = result.model_copy(update={
            "vision_request_count": getattr(fara, "request_count", 0) or 0,
            "vision_total_tokens": getattr(fara, "total_tokens", 0) or 0,
        })
        if on_plan_update:
            on_plan_update(plan, result.steps, None)
        return result

    # --- directive generation ------------------------------------------------

    async def _make_directive(self, prompt: str) -> tuple[EnvironmentContext, str]:
        try:
            model = create_chat_model(self._instruction_model_name, thinking_enabled=False, attach_tracing=False)
            try:
                model = model.bind(temperature=0)
            except Exception:
                pass
            response = await model.ainvoke(
                [
                    SystemMessage(content=_DIRECTIVE_SYSTEM_PROMPT),
                    HumanMessage(content=json.dumps({"task": prompt}, ensure_ascii=False)),
                ]
            )
            payload = _extract_json_object(_message_text(response))
            directive = str(payload.get("directive") or "").strip() or prompt
            env_raw = str(payload.get("environment") or "native").strip().lower()
            environment = EnvironmentContext.browser if env_raw == "browser" else EnvironmentContext.native
            return environment, directive
        except Exception:
            logger.warning("Directive generation failed; using the raw prompt as a native task.", exc_info=True)
            return EnvironmentContext.native, prompt

    async def _build_provider(self, config) -> FaraVisionActionProvider:
        base_url = config.computer_use.vision_fara_model.base_url
        api_key = config.computer_use.vision_fara_model.api_key or "not-needed"
        default_model = config.computer_use.vision_fara_model.model_name
        detected = await _detect_served_model_name_once({}, base_url, api_key, default_model)
        self._fara_provider = FaraVisionActionProvider(
            ComputerUseFaraModelConfig(
                enabled=True, model_name=detected, base_url=base_url, api_key=api_key,
                timeout_seconds=config.computer_use.vision_fara_model.timeout_seconds,
            )
        )
        return self._fara_provider

    # --- browser run ---------------------------------------------------------

    async def _run_browser(self, directive, config, provider, on_activity_update, cancel_check):
        try:
            session = await self._ensure_browser_session(config)
        except PlaywrightUnavailableError as exc:
            return StepStatus.failed, str(exc), "playwright_unavailable"
        except Exception as exc:
            return StepStatus.failed, f"Could not start the browser: {_exception_summary(exc)}", "browser_session_failed"

        async def observe():
            return await session.screenshot(), None

        async def act(action, _ctx):
            return await session.run_action(action)

        return await self._loop("browser", directive, config, provider, observe, act, on_activity_update, cancel_check)

    # --- native run ----------------------------------------------------------

    async def _run_native(self, directive, config, provider, session_id, owner, on_activity_update, cancel_check):
        resolved = await self._ensure_native_session(session_id)

        async def observe():
            obs = await self._remote.observe_session(resolved, owner=owner)
            if obs.screenshot_ref is None:
                return None, None
            _, image_bytes = await self._remote.export_observation_blob(
                resolved, obs.observation_id, obs.screenshot_ref.blob_id, owner
            )
            return image_bytes, obs.observation_id

        async def act(action, observation_id):
            action = action.model_copy(update={
                "action_id": f"fara-auto-{uuid.uuid4().hex}",
                "session_id": resolved,
                "risk": RiskAssessment(level=RiskLevel.medium, reasons=["autonomous FARA action"]),
                "auto_approve_risk_threshold": self._threshold,
            })
            if action.target is not None and observation_id:
                action = action.model_copy(update={"target": action.target.model_copy(update={"observation_id": observation_id})})
            stored = await self._remote.submit_action(action, owner)
            while stored.status == ActionLifecycleStatus.awaiting_approval:
                if cancel_check and await cancel_check():
                    return False, "client_disconnected"
                await asyncio.sleep(0.3)
                stored = await self._remote.get_action(action.action_id, owner)
            outcome = stored
            if stored.status in {ActionLifecycleStatus.pending, ActionLifecycleStatus.approved}:
                outcome = await self._remote.execute_action(action.action_id, owner)
            if outcome.status == ActionLifecycleStatus.succeeded:
                return True, None
            return False, (outcome.error.code if getattr(outcome, "error", None) is not None else outcome.status.value)

        return await self._loop("native", directive, config, provider, observe, act, on_activity_update, cancel_check)

    async def _ensure_native_session(self, session_id: str | None) -> str:
        if session_id:
            try:
                await self._remote.get_session(session_id)
                return session_id
            except RemoteSessionNotFoundError:
                created = await self._remote.create_session(session_id)
                return created.session.session_id
        created = await self._remote.create_session(None)
        return created.session.session_id

    # --- shared autonomous loop ---------------------------------------------

    async def _loop(self, environment, directive, config, provider, observe, act, on_activity_update, cancel_check):
        role = "browser" if environment == "browser" else "vision"
        chat_history: list[dict[str, Any]] = []
        repeated_signature: str | None = None
        repeated_count = 0
        nudges = 0
        model_errors = 0
        actions_done = 0
        failure_retries = 0
        last_error: str | None = None
        budget = self._max_actions

        for _i in range(budget + _MAX_NOOPS):
            if cancel_check and await cancel_check():
                return StepStatus.failed, "Client disconnected during execution.", "client_disconnected"

            image_bytes, ctx = await observe()
            if image_bytes is None:
                await asyncio.sleep(0.3)
                continue
            image_b64, media, scale = encode_image_for_vision(
                image_bytes,
                max_dim=getattr(config.computer_use, "vision_max_image_dimension", 0),
                jpeg_quality=getattr(config.computer_use, "vision_jpeg_quality", 85),
            )
            try:
                action, new_history = await provider.get_next_action(
                    instruction=directive,
                    image_base64=image_b64,
                    chat_history=chat_history,
                    environment=environment,
                    max_actions=budget,
                    image_media_type=media,
                    autonomous=True,
                )
            except Exception as exc:
                last_error = _exception_summary(exc)
                model_errors += 1
                if model_errors <= _MAX_MODEL_ERRORS:
                    await asyncio.sleep(0.5)
                    continue
                return StepStatus.failed, f"FARA call failed repeatedly: {last_error}", "vision_action_failed"

            chat_history = list(new_history or [])[-8:]
            thought = action.args.get("thought") if action else None
            if on_activity_update:
                on_activity_update(role, f"FARA working ({actions_done} actions done)...", thought)
            if not action:
                return StepStatus.failed, "FARA is disabled or unreachable.", "fara_disabled"
            if scale != 1.0:
                action = _rescale_action_for_screen(action, scale)

            op = action.args.get("action")
            if op == "terminate":
                if action.args.get("status") == "failure":
                    # Don't accept the first give-up: push back once so FARA tries a
                    # different approach. Only honour a repeated failure decision.
                    if failure_retries < 1:
                        failure_retries += 1
                        chat_history.append({
                            "role": "user",
                            "content": (
                                "<supervisor>\nDo not give up yet. Re-examine the current screenshot and try a "
                                "clearly DIFFERENT approach to make progress on the task. Only return finish_step "
                                "failure again if it is truly impossible.\n</supervisor>"
                            ),
                        })
                        if on_activity_update:
                            on_activity_update(role, "FARA tried to give up; nudging it to try a different approach.", thought)
                        continue
                    return StepStatus.failed, "FARA terminated the task with failure.", "fara_terminate_failure"
                return StepStatus.completed, f"FARA completed the task after {actions_done} action(s).", None
            if action.kind == ActionKind.browser_action and op in {"wait", "mouse_move"}:
                if op == "wait":
                    try:
                        secs = float(action.args.get("time") or 1.0)
                    except (TypeError, ValueError):
                        secs = 1.0
                    await asyncio.sleep(max(0.0, min(secs, 3.0)))
                continue

            signature = _vision_action_signature(action)
            if signature is not None and signature == repeated_signature:
                repeated_count += 1
            else:
                repeated_signature = signature
                repeated_count = 0
            if repeated_count >= 2:
                if nudges < _MAX_NUDGES:
                    nudges += 1
                    repeated_signature = None
                    repeated_count = 0
                    chat_history.append({"role": "user", "content": f"<supervisor>\n{_GENERIC_STUCK_NUDGE}\n</supervisor>"})
                    continue
                return StepStatus.failed, "FARA repeated the same action without progress.", "no_progress_repeated_action"

            ok, err = await act(action, ctx)
            actions_done += 1
            if not ok:
                last_error = err
                if err == "client_disconnected":
                    return StepStatus.failed, "Client disconnected during execution.", err
                if actions_done >= budget:
                    return StepStatus.failed, f"Task failed after {budget} actions ({err}).", err
                chat_history.append({"role": "user", "content": f'<tool_response>\n{{"status": "retry", "error": "{err}"}}\n</tool_response>'})
                continue
            # Success of one action does not end the task; keep going until FARA terminates.

        return StepStatus.failed, f"FARA did not finish within the {budget}-action budget.", last_error or "action_budget_exhausted"

    # --- browser lifecycle ---------------------------------------------------

    async def _ensure_browser_session(self, config) -> PlaywrightBrowserSession:
        # Persistent shared browser: stays open after the task so the operator can see
        # the result; reused by the next run.
        browser = config.computer_use.browser
        self._browser_session = await get_shared_browser_session(
            headless=browser.playwright_headless,
            viewport_width=browser.viewport_width,
            viewport_height=browser.viewport_height,
            channel=browser.channel,
            use_user_profile=browser.use_user_profile,
            user_data_dir=browser.user_data_dir,
            profile_directory=browser.profile_directory,
        )
        return self._browser_session

    async def _close_browser(self) -> None:
        # No-op: the shared browser persists across runs (see close_shared_browser_session).
        self._browser_session = None


def _single_step_plan(prompt: str, directive: str, environment: EnvironmentContext) -> ComputerUsePlan:
    return ComputerUsePlan(
        goal=prompt,
        steps=[
            ComputerUsePlanStep(
                step_id="autonomous",
                instruction=directive,
                completion_criteria="FARA reports the whole task is complete.",
                environment=environment,
                requires_vision=True,
                risk=PlannedRiskAssessment(level=RiskLevel.medium, reasons=["Autonomous FARA task"], consequences=["Desktop/browser state may change."]),
            )
        ],
    )


def _result(plan: ComputerUsePlan, status: StepStatus, summary: str, *, error_code: str | None = None) -> PlanExecuteRunResult:
    step = plan.steps[0]
    return PlanExecuteRunResult(
        status=status,
        plan=plan,
        steps=[
            StepExecutionResult(
                step_id=step.step_id,
                environment=step.environment,
                requires_vision=True,
                status=status,
                error_code=error_code,
                summary=summary,
            )
        ],
        summary=summary,
    )
