"""The autonomous approach's brief: the task as one paragraph for FARA, and where it runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from vilagent.agents.common import EnvironmentContext, PlannedRisk, RiskLevel, extract_json_object, message_text, response_tokens
from vilagent.runs.budget import charge, count_tokens

logger = logging.getLogger(__name__)

BRIEF_PROMPT = """\
You are the briefing layer for FARA, an autonomous computer-use agent that operates a
Windows machine (desktop apps and a web browser) by looking at the screen and using the
mouse and keyboard. The user gives a task. Turn it into ONE clear, complete instruction
for FARA and decide where it runs.

Output ONLY one JSON object: {"environment":"browser|native","directive":"...","risk":"low|medium|high|critical","risk_reasons":["..."]}.
- environment: "browser" if the task is primarily done on the web (websites, web apps,
  search, webmail), "native" if it is primarily a Windows desktop application.
- directive: a single self-contained paragraph, in clear plain language, that tells FARA
  exactly what to accomplish from start to finish and what the finished result looks like.
  Include the concrete specifics from the user (names, text to type, URLs, values, order
  of operations) so FARA never has to guess. Do NOT write numbered steps or pseudo-code;
  write it as natural prose FARA can follow. Do NOT add commentary outside the JSON.
- experience, when given, holds similar runs that worked and lessons from earlier runs: use them
  as advice (fold useful lessons into the directive), but the task wins.
- risk: how much harm a mistake could do (critical = money, deleting data, messages to others,
  account changes); risk_reasons: one short reason per concern."""


async def write_brief(prompt: str, build: Callable[[bool], Any], experience: dict | None = None) -> tuple[EnvironmentContext, str, PlannedRisk]:
    """(environment, directive, risk); if the model call fails, the raw prompt runs on the desktop.

    ``experience`` (similar runs, lessons) is advisory context from memory.
    """
    try:
        model = build(False)
        charge("planner")
        response = await model.ainvoke(
            [SystemMessage(content=BRIEF_PROMPT), HumanMessage(content=json.dumps({"task": prompt, **({"experience": experience} if experience else {})}, ensure_ascii=False))]
        )
        count_tokens("planner", response_tokens(response))
        payload = extract_json_object(message_text(response))
        directive = str(payload.get("directive") or "").strip() or prompt
        browser = str(payload.get("environment") or "").strip().lower() == "browser"
        return (EnvironmentContext.browser if browser else EnvironmentContext.native), directive, _risk(payload)
    except Exception:
        logger.warning("Brief generation failed; running the raw prompt on the desktop.", exc_info=True)
        return EnvironmentContext.native, prompt, _risk({})


def _risk(payload: dict) -> PlannedRisk:
    level = str(payload.get("risk") or "").strip().lower()
    reasons = [str(reason) for reason in payload.get("risk_reasons") or [] if str(reason).strip()]
    return PlannedRisk(level=RiskLevel(level) if level in RiskLevel.__members__ else RiskLevel.medium, reasons=reasons or ["Autonomous FARA task"])
