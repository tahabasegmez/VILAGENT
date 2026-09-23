"""The observe → ask FARA → act loop, shared by plan steps and autonomous runs.

The loop ends when FARA says it is finished, when its action budget is spent, or
when it gets stuck. Stuck means repeating the same action: the recovery
supervisor (if enabled) or a generic nudge gets it moving again, a bounded
number of times.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from vilagent.actions import POINTER_KINDS, Action, ActionKind
from vilagent.agents.common import ActivityCallback, StepStatus, exception_summary
from vilagent.agents.supervisor import RecoverySupervisor
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.control import ACTION_BLOCKED, EMERGENCY_STOP
from vilagent.env.base import Environment
from vilagent.runs.records import TYPED_TEXT
from vilagent.runs.trace import note, span
from vilagent.vision.fara import FaraVisionActionProvider
from vilagent.vision.image_ops import encode_image_for_vision, scale_point, screen_changed

NUDGE = (
    "You repeated the same action several times. FIRST decide: is the goal ALREADY "
    "satisfied on screen? If yes, return finish_step with status success right now. If "
    "not, the target may be elsewhere, the page may have changed, the element may be "
    "off-screen, or the click missed — re-examine the screenshot and try a clearly "
    "DIFFERENT location or action (scroll the target into view, dismiss an overlay, or "
    "click a different element). Do not repeat the previous coordinates."
)
PUSH_BACK = (
    "Do not give up yet. Re-examine the current screenshot and try a clearly DIFFERENT "
    "approach (navigate, scroll, dismiss an overlay, or click a different element). Only "
    "return finish_step failure again if it is truly impossible."
)
_REPEATS_BEFORE_STUCK = 2
_MAX_WAIT_SECONDS = 3.0
_THOUGHTS_KEPT = 3
MAX_NOTE_CHARS = 300  # one of FARA's notes, as memory keeps it


# How a loop ended. The loop reports facts; StepExecutor decides what they mean for the step
# (agents/outcomes.py), and autonomous runs count only FARA's own finish as success.
EndedBy = Literal["finish_success", "finish_failure", "budget", "repeat", "exhausted", "action_error", "model_error", "stopped", "denied"]


@dataclass(frozen=True)
class LoopLimits:
    max_actions: int
    max_noops: int = 3  # extra turns for wait / mouse_move
    max_nudges: int = 2
    max_model_errors: int = 4
    history: int = 6
    autonomous: bool = False


@dataclass(frozen=True)
class LoopResult:
    status: StepStatus  # completed only when FARA said it finished
    summary: str
    error_code: str | None = None
    actions: int = 0
    ended_by: EndedBy = "finish_success"
    succeeded: bool = False  # at least one action succeeded
    thoughts: tuple[str, ...] = ()  # FARA's last few notes, evidence for a replan
    notes: tuple[str, ...] = ()  # everything FARA said while doing this step, for memory
    struggles: tuple[str, ...] = ()  # what went sideways on the way, even when the step ended well


async def run_vision_loop(
    *,
    env: Environment,
    fara: FaraVisionActionProvider,
    instruction: Callable[[], str],
    limits: LoopLimits,
    config: ComputerUseConfig,
    goal: str,
    done_when: str,
    role: str,
    supervisor: RecoverySupervisor | None = None,
    on_activity: ActivityCallback | None = None,
    lessons: Sequence[str] = (),
) -> LoopResult:
    def notify(event: str, thought: str | None = None) -> None:
        if on_activity:
            on_activity(role, event, thought)

    history: list[dict[str, Any]] = []
    thoughts: deque[str] = deque(maxlen=_THOUGHTS_KEPT)
    notes: list[str] = []
    struggles: list[str] = []
    last_signature: str | None = None
    repeats = nudges = model_errors = actions = supervisor_calls = 0
    succeeded = pushed_back = False
    last_error: str | None = None
    previous_png: bytes | None = None
    check_change = False

    def end(ended_by: EndedBy, summary: str, error_code: str | None = None) -> LoopResult:
        status = {"finish_success": StepStatus.completed, "denied": StepStatus.denied}.get(ended_by, StepStatus.failed)
        if ended_by in {"budget", "exhausted"}:
            struggles.append("ran out of its action budget")
        return LoopResult(status, summary, error_code, actions, ended_by, succeeded, tuple(thoughts), tuple(notes), tuple(dict.fromkeys(struggles)))

    for turn in range(1, limits.max_actions + limits.max_noops + 1):
        if env.control.stopped:
            return end("stopped", "Emergency stop engaged.", EMERGENCY_STOP)

        with span("fara", kind="model", label=f"FARA #{turn}"):
            png = await env.screenshot()
            if check_change and previous_png is not None and not screen_changed(previous_png, png):
                _set_tool_response(history, {"status": "no_visible_change", "hint": "The screen did not change; the click may have missed."})
            previous_png, check_change = png, False
            image_base64, media_type, scale = encode_image_for_vision(png, max_dim=config.vision_max_image_dimension, jpeg_quality=config.vision_jpeg_quality)

            try:
                action, new_history = await fara.get_next_action(
                    instruction=instruction(),
                    image_base64=image_base64,
                    chat_history=history,
                    environment=env.name,
                    max_actions=limits.max_actions,
                    image_media_type=media_type,
                    autonomous=limits.autonomous,
                )
            except Exception as exc:
                model_errors += 1
                last_error = exception_summary(exc)
                # A reply we could not read carries the correction FARA should see next turn.
                history = list(getattr(exc, "history", history))[-limits.history :]
                note(output=f"Model call failed: {last_error}", ok=False)
                struggles.append("a model call failed")
                if model_errors <= limits.max_model_errors:
                    notify(f"Vision model call failed; retrying ({last_error})")
                    await asyncio.sleep(0.5)
                    continue
                return end("model_error", f"The vision model kept failing: {last_error}", "vision_action_failed")
            if action is None:
                note(output="FARA is disabled or unreachable.", ok=False)
                return end("model_error", "The vision model (FARA) is disabled or unreachable.", "fara_disabled")
            history = list(new_history or [])[-limits.history :]
            if action.thought:
                thoughts.append(action.thought)
                notes.append(action.thought[:MAX_NOTE_CHARS])
            notify(f"Working ({actions} actions done)…", action.thought)
            note(output=describe_action(action), thinking=action.thought)
            if scale != 1.0:
                action = rescale_action(action, scale)

            if action.kind == ActionKind.finish:
                if action.args.get("status") != "failure":
                    return end("finish_success", f"FARA finished after {actions} action(s).")
                if not pushed_back:
                    pushed_back = True
                    struggles.append("tried to give up")
                    history.append({"role": "user", "content": f"<supervisor>\n{PUSH_BACK}\n</supervisor>"})
                    notify("FARA tried to give up; asking it to try another approach.", action.thought)
                    continue
                return end("finish_failure", "FARA decided this cannot be done.", "fara_terminate_failure")

            if action.kind in (ActionKind.wait, ActionKind.mouse_move):
                if action.kind == ActionKind.wait:
                    await asyncio.sleep(_wait_seconds(action))
                continue

            signature = action_signature(action)
            repeats = repeats + 1 if signature == last_signature else 0
            last_signature = signature
            if repeats >= _REPEATS_BEFORE_STUCK:
                advice = None
                if supervisor is not None and supervisor_calls < supervisor.max_calls:
                    supervisor_calls += 1
                    notify("Stuck; consulting the recovery supervisor…", action.thought)
                    struggles.append("needed the recovery supervisor")
                    with span("supervisor", kind="model", label="Recovery advice"):
                        advice = await supervisor.advise(goal=goal, done_when=done_when, thought=action.thought, image_base64=image_base64, media_type=media_type, lessons=lessons)
                        note(output=advice or "No advice; retrying.")
                if advice is None and nudges < limits.max_nudges:
                    nudges += 1
                    advice = NUDGE
                    struggles.append("repeated itself and had to be nudged")
                    notify("Stuck; nudging the model to try something different.", action.thought)
                if advice:
                    last_signature, repeats = None, 0
                    history.append({"role": "user", "content": f"<supervisor>\n{advice}\n</supervisor>\nDo exactly this now, then continue."})
                    continue
                return end("repeat", "The model repeated the same action without progress.", "no_progress_repeated_action")

            outcome = await env.act(action)
            actions += 1
            if outcome.ok:
                succeeded = True
                check_change = action.kind in POINTER_KINDS
                if actions >= limits.max_actions:
                    return end("budget", f"Did not finish within the {limits.max_actions}-action budget.", "action_budget_exhausted")
                continue

            last_error = outcome.error
            note(output=f"{describe_action(action)} → {outcome.error}", ok=False)
            if outcome.error == EMERGENCY_STOP:
                return end("stopped", "Emergency stop engaged.", EMERGENCY_STOP)
            if (outcome.error or "").startswith(ACTION_BLOCKED):
                # The operator said no: never retry around it (DEV principle 4).
                return end("denied", "The operator declined an action.", "denied")
            if actions >= limits.max_actions:
                return end("action_error", f"Failed after {actions} action(s) ({outcome.error}).", "step_failed_after_action_limit")
            _set_tool_response(history, {"status": "retry", "error": outcome.error})

    return end("exhausted", f"Did not finish ({last_error or 'budget spent'}).", last_error or "action_budget_exhausted")


def describe_action(action: Action) -> str:
    """One line for the live trace; typed text is masked."""
    args = {key: (TYPED_TEXT if key == "text" else value) for key, value in action.args.items()}
    return f"{action.kind.value} {json.dumps(args, ensure_ascii=False)}" if args else action.kind.value


def _wait_seconds(action: Action) -> float:
    try:
        seconds = float(action.args.get("time") or 1.0)
    except (TypeError, ValueError):
        seconds = 1.0
    return max(0.0, min(seconds, _MAX_WAIT_SECONDS))


def _set_tool_response(history: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Replace FARA's optimistic success tool_response (or add one) with the real result."""
    message = {"role": "user", "content": f"<tool_response>\n{json.dumps(payload)}\n</tool_response>"}
    last = history[-1] if history else None
    if last is not None and last.get("role") == "user" and "<tool_response>" in json.dumps(last.get("content"), ensure_ascii=False):
        history[-1] = message
    else:
        history.append(message)


def rescale_action(action: Action, scale: float) -> Action:
    """Map coordinates from the downscaled image the model saw back to screen pixels."""
    if action.target is None or action.target.point is None:
        return action
    point = scale_point(action.target.point, scale)
    return action.model_copy(update={"target": action.target.model_copy(update={"point": point})})


def action_signature(action: Action) -> str:
    """What makes two actions 'the same' for stuck detection."""
    return "|".join(
        (
            action.kind.value,
            json.dumps(action.point),
            str(action.args.get("text", "")),
            str(action.args.get("keys", "")),
            str(action.args.get("url", "")),
        )
    )
