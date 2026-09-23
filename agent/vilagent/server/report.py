"""Turn a run result into the message the operator reads in the chat."""

from __future__ import annotations

from vilagent.agents.common import SINGLE_STEP_IDS, RunResult, StepResult

FRIENDLY_STEP_ERRORS = {
    "no_progress_repeated_action": "I kept retrying the same spot without it taking effect.",
    "fara_terminate_failure": "I decided this part couldn't be completed as asked.",
    "step_uncertain_after_action_limit": "I couldn't confirm this finished within the action budget.",
    "step_failed_after_action_limit": "I ran out of attempts before this part succeeded.",
    "vision_model_unreachable": "I couldn't reach the vision model (FARA) — check its Colab/ngrok endpoint.",
    "vision_action_failed": "The vision model didn't return a usable next action.",
    "fara_disabled": "The vision model is disabled or unreachable.",
    "playwright_unavailable": "The browser couldn't start (is Playwright/Edge installed?).",
    "browser_session_failed": "The browser session couldn't start.",
    "browser_step_failed": "I couldn't complete this on the page.",
    "target_not_found": "I couldn't find that element on screen.",
    "unverified_completion": "I couldn't confirm on screen that this part was actually done.",
    "action_budget_exhausted": "I ran out of actions before this part was done.",
}


def friendly_error(code: str | None, summary: str | None) -> str:
    if code and code in FRIENDLY_STEP_ERRORS:
        return FRIENDLY_STEP_ERRORS[code]
    if summary and summary.strip():
        return summary.strip()
    if code:
        return code.replace("_", " ")
    return "an unexpected error"


def narration_block(narration: list[dict[str, str]] | None) -> str:
    """The models' own natural-language account while working (holds the answer)."""
    if not narration:
        return ""
    seen: set[str] = set()
    lines: list[str] = []
    for item in narration:
        text = (item.get("text") or "").strip()
        if len(text) < 4:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(text)
    if not lines:
        return ""
    # Keep the most recent thoughts — that is where the read result / answer lands.
    lines = lines[-10:]
    return "**What the agent did and found:**\n" + "\n".join(lines)


def format_run_result(result: RunResult, narration: list[dict[str, str]] | None = None) -> str:
    account = narration_block(narration)
    status = result.status.value
    plan_steps = list(result.plan.steps)
    outcomes = list(result.steps)
    goal = (result.plan.goal or "").strip()
    is_autonomous = len(plan_steps) == 1 and plan_steps[0].step_id in SINGLE_STEP_IDS

    # Match each executed step to its plan instruction (by step_id) for readable labels.
    instructions = {s.step_id: (s.instruction or "").strip() for s in plan_steps}

    def _label(outcome: StepResult) -> str:
        return instructions.get(outcome.step_id) or (outcome.summary or outcome.step_id)

    tail = f"\n\n{account}" if account else ""

    if status == "completed":
        if is_autonomous:
            brief = instructions.get(plan_steps[0].step_id, goal) or goal
            body = f"\n\n{brief}" if brief else ""
            return f"✅ **Done.**{body}{tail}"
        lines = ["✅ **Task complete.**"]
        if goal:
            lines += ["", f"*{goal}*"]
        done = [o for o in outcomes if o.status.value == "completed"]
        if done:
            lines += ["", "**What I did:**"]
            lines += [f"{i + 1}. {_label(o)}" for i, o in enumerate(done)]
        return "\n".join(lines) + tail

    if status == "denied":
        declined = next((o for o in outcomes if o.status.value == "denied"), None)
        what = f": *{_label(declined)}*" if declined else "."
        return f"⏹️ **Stopped: you declined{what}** Nothing after it was done.{tail}"

    # failed / blocked
    lines = ["⚠️ **I couldn't fully finish this task.**"]
    if goal:
        lines += ["", f"*{goal}*"]
    if outcomes:
        lines += ["", "**Progress:**"]
        failed_reason = None
        for o in outcomes:
            ok = o.status.value == "completed"
            mark = "✓" if ok else "✗"
            lines.append(f"{mark} {_label(o)}")
            if not ok and failed_reason is None:
                failed_reason = friendly_error(getattr(o, "error_code", None), getattr(o, "summary", None))
        if failed_reason:
            lines += ["", f"**Why it stopped:** {failed_reason}"]
    else:
        lines += ["", f"**Why it stopped:** {friendly_error(None, getattr(result, 'summary', None))}"]
    lines += ["", "_Tip: try rephrasing the task or splitting it into smaller, clearer steps._"]
    return "\n".join(lines) + tail
