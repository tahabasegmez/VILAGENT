"""What a finished FARA loop means for a plan step, and the check that settles uncertain ones.

The loop reports how it ended (``LoopResult.ended_by``); ``classify`` is the single table that
turns that into a step outcome, so every orchestrator routes with the same rules:

- ``completed``: FARA said the step is finished.
- ``verify``: FARA acted successfully but never said so (budget spent, repeating itself, turns
  used up); whether the step is done must be checked on screen.
- ``blocked``: the plan is wrong or the screen differs from what it expects; replan.
- ``failed``: the model is unreachable or the operator stopped the run; replanning can't help.
- ``denied``: the operator declined an action; the run ends (never replanned around).
"""

from __future__ import annotations

from typing import Literal, Protocol

from vilagent.agents.common import PlanStep
from vilagent.agents.vision_loop import LoopResult
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.env.base import Environment
from vilagent.vision.fara import Verification
from vilagent.vision.image_ops import encode_image_for_vision

Verdict = Literal["completed", "verify", "blocked", "failed", "denied"]

_UNCERTAIN = frozenset({"budget", "repeat", "exhausted"})
_REPLAN = _UNCERTAIN | {"finish_failure", "action_error"}


def classify(result: LoopResult) -> Verdict:
    if result.ended_by == "finish_success":
        return "completed"
    if result.ended_by == "denied":
        return "denied"
    if result.ended_by in _UNCERTAIN and result.succeeded:
        return "verify"
    if result.ended_by in _REPLAN:
        return "blocked"
    return "failed"


class Checker(Protocol):
    async def verify(self, criterion: str, image_base64: str, image_media_type: str = "image/png") -> Verification: ...


async def verify_step(step: PlanStep, env: Environment, checker: Checker, config: ComputerUseConfig) -> Verification:
    """Ask FARA or the supervisor model whether the step's criterion is visible now; errors count as not verified."""
    try:
        image, media_type, _ = encode_image_for_vision(await env.screenshot(), max_dim=config.vision_max_image_dimension, jpeg_quality=config.vision_jpeg_quality)
        return await checker.verify(step.completion_criteria, image, media_type)
    except Exception as exc:
        return Verification(False, f"The check could not run ({exc.__class__.__name__}).")
