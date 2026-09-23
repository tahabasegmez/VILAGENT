"""The run graph's nodes and routing: thin wrappers around the agents' plain functions.

Plan and execute:  recall → plan → select_step → gate → execute_step → (select_step | replan → select_step | finalize)
Autonomous FARA:   recall → brief → gate → execute_task → finalize
Direct (pure VLM): recall → direct → gate → execute_task → finalize (no planner call at all)
A declined gate ends the run (finalize) with the step marked ``denied``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from vilagent.agents.brief import write_brief
from vilagent.agents.common import (
    BRIEF_STEP_ID,
    DIRECT_STEP_ID,
    SINGLE_STEP_APPROACHES,
    EnvironmentContext,
    Plan,
    PlannedRisk,
    PlanStep,
    RiskLevel,
    StepResult,
    StepStatus,
    approach_name,
    exception_summary,
)
from vilagent.agents.plan_execute import prepare_plan
from vilagent.agents.vision_loop import LoopLimits, run_vision_loop
from vilagent.approvals.policy import needs_approval, risky_words
from vilagent.env.browser import PlaywrightUnavailableError
from vilagent.graph.state import RunContext, RunState, plan_of, results_of
from vilagent.memory.retrieval import Experience, step_keys
from vilagent.runs.trace import note

# Let the previous step's UI finish rendering before the next step observes the screen.
STEP_SETTLE_SECONDS = 0.5


def _prepared(state: RunState, ctx: RunContext, plan: Plan) -> dict[str, Any]:
    plan = prepare_plan(plan, max_steps=ctx.max_steps, vision_only=state.get("execution_mode") == "vision_only")
    return plan.model_dump(mode="json")


async def recall(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """What memory offers this task; once per run (a resumed run keeps what it recalled)."""
    ctx = runtime.context
    if ctx.recall is None:
        note(output="Memory is off.")
        return {"experience": {}}
    ctx.notify("lead", "Recalling similar tasks…")
    experience = await ctx.recall(state["prompt"])
    recalled = Experience.from_dict(experience)
    note(
        output=f"{len(recalled.similar_runs)} similar run(s), {len(recalled.lessons)} lesson(s)." if experience else "Nothing relevant remembered.",
        memory=[f"Run: {run['task']}" for run in recalled.similar_runs] + [f"Lesson: {lesson['text']}" for lesson in recalled.lessons],
    )
    return {"experience": experience}


def _planner_context(state: RunState, ctx: RunContext) -> dict[str, Any]:
    advice = Experience.from_dict(state.get("experience")).for_planner()
    return {**ctx.planner_context, "experience": advice} if advice else ctx.planner_context


def _lessons(state: RunState, step: PlanStep) -> list[str]:
    apps, domains = step_keys(step.model_dump(mode="json"), state.get("plan") or {}, state["prompt"])
    return Experience.from_dict(state.get("experience")).for_step(apps, domains)


async def plan(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    ctx.notify("lead", "Planning…")
    prepared = _prepared(state, ctx, await ctx.planner.plan(state["prompt"], context=_planner_context(state, ctx)))
    ctx.publish(Plan.model_validate(prepared), [], None)
    note(output=_outline(prepared))
    return {"plan": prepared, "replans": 0}


def select_step(state: RunState) -> dict[str, Any]:
    # A revised plan may reuse the blocked step's id; only completed steps are skipped.
    done = {result.step_id for result in results_of(state) if result.status == StepStatus.completed}
    step = next((step for step in plan_of(state).steps if step.step_id not in done), None)
    note(output=f"Next: {step.instruction}" if step else "Every step is done.", meta={"step_id": step.step_id} if step else None)
    return {"current_step_id": step.step_id if step else None}


async def execute_step(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    current, results = plan_of(state), results_of(state)
    step = next(step for step in current.steps if step.step_id == state["current_step_id"])
    ctx.publish(current, results, step.step_id)
    lessons = _lessons(state, step)
    note(label=step.instruction, memory=lessons, meta={"step_id": step.step_id})
    if results:
        await asyncio.sleep(STEP_SETTLE_SECONDS)
    result = await ctx.executor.execute(step, on_activity=ctx.on_activity, lessons=lessons)
    ctx.publish(current, [*results, result], None)
    note(output=f"{result.status.value}: {result.summary}", ok=result.status == StepStatus.completed)
    return {"results": [result.model_dump(mode="json")]}


async def replan(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    current, results = plan_of(state), results_of(state)
    blocked = results[-1]
    ctx.notify("lead", "Revising the plan…")
    screenshot = await ctx.executor.screenshot_url(blocked.environment) if getattr(ctx.planner, "sees_images", False) else None
    revised = await ctx.planner.replan(state["prompt"], plan=current, completed_steps=results, blocked_step=blocked, context=_planner_context(state, ctx), screenshot=screenshot)
    prepared = _prepared(state, ctx, revised)
    ctx.publish(Plan.model_validate(prepared), results, None)
    note(output=_outline(prepared))
    return {"plan": prepared, "replans": state.get("replans", 0) + 1}


async def brief(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    ctx.notify("lead", "Writing the task brief for FARA…")
    environment, directive, risk = await write_brief(state["prompt"], ctx.brief_model, Experience.from_dict(state.get("experience")).for_planner())
    step = PlanStep(
        step_id=BRIEF_STEP_ID,
        instruction=directive,
        completion_criteria="FARA reports the whole task is complete.",
        environment=environment,
        requires_vision=True,
        risk=risk,
    )
    task_plan = Plan(goal=state["prompt"], steps=[step])
    ctx.publish(task_plan, [], step.step_id)
    note(output=f"[{environment.value}] {directive}")
    return {"plan": task_plan.model_dump(mode="json"), "current_step_id": step.step_id}


def direct(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Pure vision: the operator's words go to FARA as they are; no model plans or rates them."""
    ctx = runtime.context
    prompt = state["prompt"]
    # Nobody rated this task, so the approval keywords rate it: a task that says "send" or
    # "delete" reaches the operator's threshold, anything else stays medium.
    words = risky_words(prompt, ctx.config.approvals.keywords)
    risk = PlannedRisk(
        level=RiskLevel.high if words else RiskLevel.medium,
        reasons=[f'The task says "{word}".' for word in words] or ["Nobody planned or rated this task; FARA runs it as written."],
    )
    step = PlanStep(
        step_id=DIRECT_STEP_ID,
        instruction=prompt,
        completion_criteria="FARA reports the whole task is complete.",
        environment=EnvironmentContext.native,
        requires_vision=True,
        risk=risk,
    )
    task_plan = Plan(goal=prompt, steps=[step])
    ctx.notify("lead", "Handing the task straight to FARA…")
    ctx.publish(task_plan, [], step.step_id)
    note(output=prompt)
    return {"plan": task_plan.model_dump(mode="json"), "current_step_id": step.step_id}


async def execute_task(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Autonomous: FARA runs the whole task in one loop; only its own finish counts as success."""
    ctx = runtime.context
    task_plan = plan_of(state)
    step = task_plan.steps[0]
    browser_task = step.environment == EnvironmentContext.browser
    role = "browser" if browser_task else "vision"
    try:
        fara = await ctx.executor.vision_model()
    except Exception as exc:
        result = StepResult.of(step, StepStatus.failed, f"Vision model unreachable: {exception_summary(exc)}", error_code="vision_model_unreachable")
    else:
        ctx.notify(role, f"FARA is running the task on its own ({step.environment.value}).", step.instruction[:200])
        lessons = _lessons(state, step)
        note(memory=lessons, meta={"step_id": step.step_id})
        instruction = step.instruction + (f"\nLESSONS FROM EARLIER RUNS HERE: {' '.join(lessons)}" if lessons else "")
        try:
            loop = await run_vision_loop(
                env=ctx.browser if browser_task else ctx.desktop,
                fara=fara,
                instruction=lambda: instruction,
                limits=autonomous_limits(ctx.autonomous_max_actions),
                config=ctx.config,
                goal=step.instruction,
                done_when=step.completion_criteria,
                role=role,
                supervisor=ctx.supervisor,
                on_activity=ctx.on_activity,
                lessons=lessons,
            )
            verified_by = "fara_finish" if loop.status == StepStatus.completed else None
            result = StepResult.of(step, loop.status, loop.summary, error_code=loop.error_code, actions=loop.actions, verified_by=verified_by, notes=list(loop.notes), struggles=list(loop.struggles))
        except PlaywrightUnavailableError as exc:
            result = StepResult.of(step, StepStatus.failed, str(exc), error_code="playwright_unavailable")
    ctx.publish(task_plan, [result], None)
    note(output=f"{result.status.value}: {result.summary}", ok=result.status == StepStatus.completed)
    return {"results": [result.model_dump(mode="json")]}


def gate(state: RunState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Ask the operator before a step whose planned risk reaches the threshold.

    LangGraph re-runs this node when the answer arrives, so nothing here may have a side
    effect before ``interrupt()``.
    """
    ctx = runtime.context
    current = plan_of(state)
    step = next(step for step in current.steps if step.step_id == state["current_step_id"])
    if not needs_approval(step.risk.level.value, ctx.approval_threshold):
        note(output=f"No approval needed ({step.risk.level.value} risk).", meta={"step_id": step.step_id})
        return {}
    note(output=f"Asking the operator ({step.risk.level.value} risk): {step.instruction}", meta={"step_id": step.step_id})
    answer = interrupt(
        {"kind": "step", "step_id": step.step_id, "title": step.instruction, "level": step.risk.level.value, "reasons": [*step.risk.reasons, *step.risk.consequences]}
    )
    if answer.get("approve"):
        note(output="Approved by the operator.")
        return {}
    note(output=f"Declined: {answer.get('reason') or 'the operator said no.'}", ok=False)
    declined = StepResult.of(step, StepStatus.denied, answer.get("reason") or "The operator declined this step.", error_code="denied")
    ctx.publish(current, [*results_of(state), declined], None)
    return {"results": [declined.model_dump(mode="json")]}


def finalize(state: RunState) -> dict[str, Any]:
    results = results_of(state)
    if state.get("approach") not in SINGLE_STEP_APPROACHES and state.get("current_step_id") is None:  # every step completed
        ending = {"status": StepStatus.completed.value, "summary": "Plan completed."}
    else:
        last = results[-1]
        ending = {"status": last.status.value, "summary": last.summary}
    note(output=f"{ending['status']}: {ending['summary']}", ok=ending["status"] == StepStatus.completed.value)
    return ending


def _outline(plan: dict[str, Any]) -> str:
    return "\n".join(f"{number}. {step['instruction']}" for number, step in enumerate(plan.get("steps") or [], 1))


def autonomous_limits(max_actions: int) -> LoopLimits:
    return LoopLimits(max_actions=max(4, max_actions), max_noops=12, max_nudges=3, max_model_errors=4, history=8, autonomous=True)


# --- routing -------------------------------------------------------------------


def route_start(state: RunState) -> Literal["plan", "brief", "direct"]:
    approach = state.get("approach")
    return approach_name(approach)


def route_step(state: RunState) -> Literal["gate", "finalize"]:
    return "finalize" if state.get("current_step_id") is None else "gate"


def route_gate(state: RunState) -> Literal["execute_step", "execute_task", "finalize"]:
    results = state.get("results") or []
    if results and results[-1]["step_id"] == state.get("current_step_id") and results[-1]["status"] == StepStatus.denied.value:
        return "finalize"
    return "execute_task" if state.get("approach") in SINGLE_STEP_APPROACHES else "execute_step"


def route_after_step(state: RunState) -> Literal["select_step", "replan", "finalize"]:
    last = StepResult.model_validate(state["results"][-1])
    if last.status == StepStatus.completed:
        return "select_step"
    if last.status == StepStatus.blocked and state.get("replans", 0) < state.get("max_replans", 2):
        return "replan"
    return "finalize"
