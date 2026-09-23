"""The supervisor model: recovery advice when FARA is stuck, and (optionally) the step check."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from vilagent.agents.common import message_text, response_tokens
from vilagent.runs.budget import charge, count_tokens
from vilagent.vision.fara import Verification

logger = logging.getLogger(__name__)

_PROMPT = """\
You supervise a fast GUI action model that got stuck on ONE step. Look at the
screenshot and reason about what is actually blocking progress (a popup, ad,
cookie banner, modal, wrong/unfocused window, a disabled or covered control, a
loading state, or a wrong assumption).
Reply with ONE short imperative recovery instruction (max 2 sentences) telling the
action model exactly what to do NEXT to unblock and continue the step — e.g.
"Close the ad by clicking the X at its top-right corner, then click the search box."
If nothing is actually blocking and it should simply retry the original step, reply
exactly: PROCEED. Do not explain. Output only the instruction or PROCEED."""


class RecoverySupervisor:
    """Gives at most ``max_calls`` recovery instructions per loop."""

    def __init__(self, model_factory: Callable[[], Any], *, max_calls: int = 2):
        self._model_factory = model_factory
        self.max_calls = max_calls

    async def advise(self, *, goal: str, done_when: str, thought: str | None, image_base64: str, media_type: str, lessons: Sequence[str] = ()) -> str | None:
        """A concrete next instruction, or None ("just retry", or the call failed)."""
        text = (
            f"Step goal: {goal}\n"
            f"Done when: {done_when}\n"
            f"The action model is stuck. Its last note: {thought or '(none)'}\n"
            + (f"Lessons from earlier runs here: {' '.join(lessons)}\n" if lessons else "")
            + "Look at the screen and give the single best recovery instruction, or PROCEED."
        )
        charge("supervisor")
        try:
            response = await self._model_factory().ainvoke(
                [
                    SystemMessage(content=_PROMPT),
                    HumanMessage(
                        content=[
                            {"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_base64}"}},
                        ]
                    ),
                ]
            )
            count_tokens("supervisor", response_tokens(response))
            advice = message_text(response).strip()
        except Exception:
            # Text-only models reject screenshots; rate limits (e.g. GLM-V 429) also land here.
            logger.warning("Recovery supervisor call failed; continuing without advice", exc_info=True)
            return None
        if not advice or advice.upper().startswith("PROCEED"):
            return None
        return advice[:500]


_VERIFY_PROMPT = """\
You check whether ONE condition is visibly true on a computer screen right now.
Look at the screenshot. Reply with YES or NO alone on the first line, then one short sentence
saying what you see that decides it. Answer NO when the screen does not clearly show it."""


class ModelVerifier:
    """Checks a step's completion criterion with the supervisor's (vision-capable) chat model."""

    def __init__(self, model_factory: Callable[[], Any]):
        self._model_factory = model_factory

    async def verify(self, criterion: str, image_base64: str, image_media_type: str = "image/png") -> Verification:
        charge("supervisor")
        response = await self._model_factory().ainvoke(
            [
                SystemMessage(content=_VERIFY_PROMPT),
                HumanMessage(
                    content=[
                        {"type": "text", "text": f"Condition: {criterion}"},
                        {"type": "image_url", "image_url": {"url": f"data:{image_media_type};base64,{image_base64}"}},
                    ]
                ),
            ]
        )
        count_tokens("supervisor", response_tokens(response))
        verdict, _, reason = message_text(response).strip().partition("\n")
        return Verification(verdict.strip().upper().startswith("YES"), reason.strip() or verdict.strip())
