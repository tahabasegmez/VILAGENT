"""Plan steps: how one step runs on the desktop or in the browser.

A step either runs deterministically once (launch an app, type a known text, press
a hotkey, open a URL, invoke a named UI Automation control) or is handed to the
FARA vision loop. The order of steps, replanning and verification routing live in
the run graph (``vilagent.graph``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Literal

from vilagent.actions import Action, ActionKind, ActionOutcome
from vilagent.agents.common import ActivityCallback, EnvironmentContext, Plan, PlanStep, StepResult, StepStatus, exception_summary
from vilagent.agents.outcomes import classify, verify_step
from vilagent.agents.supervisor import ModelVerifier, RecoverySupervisor
from vilagent.agents.vision_loop import LoopLimits, run_vision_loop
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.control import ACTION_BLOCKED
from vilagent.env.base import Environment
from vilagent.env.browser import BrowserEnvironment, PlaywrightUnavailableError
from vilagent.env.desktop import DesktopEnvironment
from vilagent.runs.trace import note, span
from vilagent.vision.fara import FaraVisionActionProvider
from vilagent.vision.image_ops import encode_image_for_vision

PlanCallback = Callable[[Plan, list[StepResult], str | None], None]
# Who checks a FARA step that ended without saying it finished: FARA, the supervisor model, or nobody.
CheckMode = Literal["fara", "supervisor", "none"]

# Keyboard-only kinds are executed directly, never by clicking on-screen keys.
_KEYBOARD_KINDS = frozenset({ActionKind.launch_app, ActionKind.hotkey, ActionKind.type_text})
# Per environment: (default, cap) actions for a FARA step; browser sub-goals get more headroom.
_ACTION_LIMITS = {EnvironmentContext.native: (8, 12), EnvironmentContext.browser: (12, 16)}
_REPLAN_IMAGE_MAX_DIM = 1280


class StepExecutor:
    """Run one plan step in its environment."""

    def __init__(
        self,
        config: ComputerUseConfig,
        *,
        desktop: DesktopEnvironment,
        browser: BrowserEnvironment,
        supervisor: RecoverySupervisor | None = None,
        fara_factory: Callable[[ComputerUseConfig], Any],
        verification: CheckMode = "fara",
        model_verifier: ModelVerifier | None = None,
    ):
        self._config = config
        self._desktop = desktop
        self._browser = browser
        self._supervisor = supervisor
        self._fara_factory = fara_factory
        # Who checks a step that ended without FARA saying it finished (the operator's choice).
        self._verification = verification if verification != "supervisor" or model_verifier else "fara"
        self._model_verifier = model_verifier
        self.fara: FaraVisionActionProvider | None = None

    async def vision_model(self) -> FaraVisionActionProvider:
        """The FARA client, built on first use and shared by every step of the run."""
        if self.fara is None:
            self.fara = await self._fara_factory(self._config)
        return self.fara

    async def execute(self, step: PlanStep, *, on_activity: ActivityCallback | None = None, lessons: Sequence[str] = ()) -> StepResult:
        """``lessons``: what memory knows about this step's app or site (for FARA and the supervisor)."""
        kind = step.action_kind or infer_action_kind(step)
        try:
            if step.environment == EnvironmentContext.browser:
                return await self._browser_step(step, kind, on_activity, lessons)
            if step.requires_vision and kind not in _KEYBOARD_KINDS:
                return await self._vision_step(step, self._desktop, "vision", on_activity, lessons)
            return await self._native_step(step, kind, on_activity, lessons)
        except PlaywrightUnavailableError as exc:
            return StepResult.of(step, StepStatus.failed, str(exc), error_code="playwright_unavailable")
        except Exception as exc:
            return StepResult.of(step, StepStatus.failed, f"Step failed: {exception_summary(exc)}", error_code=exc.__class__.__name__)

    async def _native_step(self, step: PlanStep, kind: ActionKind, on_activity: ActivityCallback | None, lessons: Sequence[str] = ()) -> StepResult:
        target = None
        if kind not in _KEYBOARD_KINDS:
            if on_activity:
                on_activity("uia", f"Finding: {step.target_description or step.instruction}", None)
            target = await self._desktop.resolve_target(step.target_description or step.instruction, step.selector_hints)
            if target is None:
                # Nothing answers to that name, or two things do: look at the screen instead of
                # giving up. The step is only blocked once vision has tried and failed too.
                if on_activity:
                    on_activity("uia", "Not found by name; looking at the screen instead.", None)
                return await self._vision_step(step, self._desktop, "vision", on_activity, lessons)
        elif on_activity:
            on_activity("uia", step.instruction, None)
        outcome = await self._desktop.act(Action(kind=kind, target=target, args=args_for_step(step, kind)))
        return _single_action_result(step, kind, outcome)

    async def _browser_step(self, step: PlanStep, kind: ActionKind, on_activity: ActivityCallback | None, lessons: Sequence[str]) -> StepResult:
        browser = self._browser
        if kind == ActionKind.launch_app:
            # The managed browser is always there; "open the browser" only needs a URL, if any.
            url = first_url(step.args.get("url"), step.target_description, step.instruction)
            if not url:
                return StepResult.of(step, StepStatus.completed, "The browser is managed by VILAGENT; nothing to launch.")
            if on_activity:
                on_activity("browser", f"Opening {url}", None)
            outcome = await browser.act(Action(kind=ActionKind.browser_action, args={"action": "visit_url", "url": url}))
            return _single_action_result(step, ActionKind.browser_action, outcome)
        if not step.requires_vision and kind in {ActionKind.browser_action, ActionKind.type_text, ActionKind.hotkey}:
            if on_activity:
                on_activity("browser", step.instruction, None)
            args = dict(step.args) if kind == ActionKind.browser_action else args_for_step(step, kind)
            if kind == ActionKind.browser_action and not args.get("action"):
                args["action"] = "visit_url" if args.get("url") else "refresh"
            outcome = await browser.act(Action(kind=kind, args=args))
            return _single_action_result(step, kind, outcome)
        return await self._vision_step(step, browser, "browser", on_activity, lessons)

    async def _vision_step(self, step: PlanStep, env: Environment, role: str, on_activity: ActivityCallback | None, lessons: Sequence[str]) -> StepResult:
        if on_activity:
            on_activity(role, f"Working on: {step.instruction}", None)
        try:
            fara = await self.vision_model()
        except Exception as exc:
            return StepResult.of(step, StepStatus.failed, f"Vision model unreachable: {exception_summary(exc)}", error_code="vision_model_unreachable")
        max_actions = vision_action_limit(step)
        if self._supervisor is not None:
            max_actions += 2 * self._supervisor.max_calls  # room for supervised retries
        result = await run_vision_loop(
            env=env,
            fara=fara,
            instruction=lambda: vision_step_command(step, max_actions=max_actions, context=env.context_hint(), lessons=lessons),
            limits=LoopLimits(max_actions=max_actions, max_model_errors=max_actions),
            config=self._config,
            goal=step.instruction,
            done_when=step.completion_criteria,
            role=role,
            supervisor=self._supervisor,
            on_activity=on_activity,
            lessons=lessons,
        )
        verdict = classify(result)
        if verdict == "completed":
            return StepResult.of(step, StepStatus.completed, result.summary, actions=result.actions, verified_by="fara_finish", notes=list(result.notes), struggles=list(result.struggles))
        if verdict == "verify":
            if not step.requires_verification or self._verification == "none":
                why = "the plan asked for none" if not step.requires_verification else "step checks are off"
                return StepResult.of(step, StepStatus.completed, f"{result.summary} Accepted without a check ({why}).", actions=result.actions, verified_by="none", notes=list(result.notes), struggles=list(result.struggles))
            if on_activity:
                on_activity(role, f"Checking: {step.completion_criteria}", None)
            by_model = self._verification == "supervisor"
            with span("verify", kind="model", label="Step check (supervisor)" if by_model else "Step check (FARA)"):
                check = await verify_step(step, env, self._model_verifier if by_model else fara, self._config)
                note(output=check.reason, ok=check.passed)
            if check.passed:
                verified_by = "model_verify" if by_model else "fara_verify"
                return StepResult.of(step, StepStatus.completed, f"Verified after {result.actions} action(s): {check.reason}", actions=result.actions, verified_by=verified_by, notes=list(result.notes), struggles=list(result.struggles))
            result = replace(result, error_code="unverified_completion", summary=f"Not done after {result.actions} action(s): {check.reason}")
            verdict = "blocked"
        evidence = {"error_code": result.error_code, "last_thoughts": list(result.thoughts), "context": env.context_hint()} if verdict == "blocked" else None
        return StepResult.of(step, StepStatus(verdict), result.summary, error_code=result.error_code, actions=result.actions, evidence=evidence, notes=list(result.notes), struggles=list(result.struggles))

    async def screenshot_url(self, environment: EnvironmentContext) -> str | None:
        """A small JPEG data URL of the step's screen, for a vision-capable replanner (never stored)."""
        env = self._browser if environment == EnvironmentContext.browser else self._desktop
        try:
            image, media_type, _ = encode_image_for_vision(await env.screenshot(), max_dim=_REPLAN_IMAGE_MAX_DIM, jpeg_quality=80)
        except Exception:
            return None
        return f"data:{media_type};base64,{image}"


def prepare_plan(plan: Plan, *, max_steps: int, vision_only: bool) -> Plan:
    """Trim the plan to ``max_steps``; vision-only mode sends every step through FARA."""
    steps = plan.steps[:max_steps]
    if vision_only:
        steps = [step.model_copy(update={"requires_vision": True}) for step in steps]
    return plan.model_copy(update={"steps": steps})


def _single_action_result(step: PlanStep, kind: ActionKind, outcome: ActionOutcome) -> StepResult:
    if outcome.ok:
        return StepResult.of(step, StepStatus.completed, f"{kind.value} -> ok", actions=1)
    if (outcome.error or "").startswith(ACTION_BLOCKED):
        return StepResult.of(step, StepStatus.denied, "The operator declined this action.", error_code="denied", actions=1)
    return StepResult.of(step, StepStatus.failed, f"{kind.value} -> {outcome.error}", error_code=outcome.error, actions=1)


def infer_action_kind(step: PlanStep) -> ActionKind:
    instruction = step.instruction.lower()
    if "open " in instruction or "launch " in instruction or step.args.get("app_name"):
        return ActionKind.launch_app
    if "type" in instruction or "write" in instruction or step.args.get("text"):
        return ActionKind.type_text
    if "double" in instruction:
        return ActionKind.double_click
    return ActionKind.click


def args_for_step(step: PlanStep, kind: ActionKind) -> dict[str, Any]:
    args = dict(step.args)
    if kind == ActionKind.launch_app:
        # A planner may put a whole sentence in app_name; reduce it to the app itself.
        raw = str(args.get("app_name") or args.get("command") or "").strip() or step.instruction
        if normalized := normalize_app_name(raw):
            args["app_name"] = normalized
    if kind == ActionKind.type_text and not str(args.get("text") or "").strip():
        if inferred := infer_type_text(step.instruction):
            args["text"] = inferred
    return args


def infer_type_text(instruction: str) -> str | None:
    """Best-effort literal to type when the planner gave no args.text."""
    quoted = re.search(r"['\"‘’“”]([^'\"‘’“”]+)['\"‘’“”]", instruction)
    if quoted:
        return quoted.group(1).strip()
    verb = re.search(r"\b(?:type|write|enter|input)\b\s*:?\s*(.+)", instruction, re.IGNORECASE)
    return verb.group(1).strip().strip(".") if verb else None


def normalize_app_name(raw: str) -> str | None:
    """Reduce 'Launch Microsoft Edge browser and navigate to Gmail' to 'Microsoft Edge'.

    Only a trailing clause that starts with a conjunction *and a verb* is dropped, so
    multi-word names like 'Microsoft To Do' survive.
    """
    text = raw.strip()
    verb = re.search(r"\b(?:open|launch|start|run)\b\s+(.+)", text, re.IGNORECASE)
    candidate = verb.group(1) if verb else text
    candidate = re.split(
        r"\s+(?:and|then|&|,)\s+(?:open|launch|start|run|go|navigate|visit|search|type|click|find|select|enter|add|create|write|set|check|read|press)\b",
        candidate,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    candidate = re.split(r"[;]|,\s", candidate, maxsplit=1)[0]
    candidate = re.sub(r"\b(?:the|a|an|browser|application|window|program)\b", " ", candidate, flags=re.IGNORECASE)
    candidate = " ".join(candidate.split()).strip(" .,:;\"'")
    return candidate or None


def vision_step_command(step: PlanStep, *, max_actions: int, context: str | None = None, lessons: Sequence[str] = ()) -> str:
    return (
        f"CURRENT STEP: {step.instruction}\n"
        + (f"{context}\n" if context else "")
        + (f"LESSONS FROM EARLIER RUNS HERE: {' '.join(lessons)}\n" if lessons else "")
        + f"MAXIMUM ACTIONS FOR THIS STEP: {max_actions}\n"
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
        f"You have at most {max_actions} action(s) for this step."
    )


def vision_action_limit(step: PlanStep) -> int:
    """The planner's budget is a hint: never below the environment's default, never above its cap."""
    floor, cap = _ACTION_LIMITS[step.environment]
    return min(max(step.max_actions, floor), cap)


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def first_url(*candidates: Any) -> str | None:
    for candidate in candidates:
        if candidate and (match := _URL_RE.search(str(candidate))):
            return match.group(0).rstrip(".,;)")
    return None
