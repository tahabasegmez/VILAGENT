"""The gateway API the operator UI calls."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from vilagent.agents.common import APPROACHES, Plan, StepResult, approach_name
from vilagent.agents.plan_execute import StepExecutor
from vilagent.agents.planner import JsonLLMPlanner, planner_context
from vilagent.agents.supervisor import ModelVerifier, RecoverySupervisor
from vilagent.approvals.broker import Approvals
from vilagent.approvals.policy import THRESHOLDS
from vilagent.config.app_config import AppConfig
from vilagent.control import RunStopped
from vilagent.graph.build import run_graph
from vilagent.graph.state import RunContext
from vilagent.memory.retrieval import recall as memory_recall
from vilagent.runs.budget import BudgetExhausted, BudgetMeter, enforce_duration
from vilagent.runs.manager import RunActive
from vilagent.runs.session import RunSession, RunStatus
from vilagent.server import logs
from vilagent.server.activity import activity_from_result, new_activity, plan_step_activity, set_idle, update_activity
from vilagent.server.deps import get_config, get_runtime, require_internal_request
from vilagent.server.memory import current_embedder, memory_enabled
from vilagent.server.models import planner_factory, planner_model, planner_sees_images, supervisor_factory, supervisor_sees_images, vision_model
from vilagent.server.report import format_run_result
from vilagent.server.runtime import Runtime
from vilagent.server.state import get_state_value, set_state_value
from vilagent.vision.fara import FaraVisionActionProvider

logger = logging.getLogger(__name__)
# Run ids name files in <data dir>/runs, so they are restricted to a safe alphabet.
RUN_ID_PATTERN = r"^[A-Za-z0-9._-]{1,200}$"
router = APIRouter(prefix="/api/computer-use", tags=["computer-use"], dependencies=[Depends(require_internal_request)])


class ComputerUseTaskRunRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200)
    run_id: str | None = Field(default=None, pattern=RUN_ID_PATTERN)
    prompt: str = Field(min_length=1, max_length=12000)
    model_config = ConfigDict(extra="forbid")


class RunStarted(BaseModel):
    run_id: str
    thread_id: str


class ExecutionModeSelection(BaseModel):
    execution_mode: str
    options: list[str] = Field(default_factory=lambda: ["hybrid", "vision_only"])


class ExecutionModeUpdate(BaseModel):
    execution_mode: str = Field(pattern="^(hybrid|vision_only)$")
    model_config = ConfigDict(extra="forbid")


class AgentApproachSelection(BaseModel):
    approach: str
    options: list[str] = Field(default_factory=lambda: list(APPROACHES))


class AgentApproachUpdate(BaseModel):
    approach: str = Field(pattern=f"^({'|'.join(APPROACHES)})$")
    model_config = ConfigDict(extra="forbid")


class VisionRecoverySelection(BaseModel):
    enabled: bool


class VerifierSelection(BaseModel):
    verifier: Literal["fara", "supervisor", "none"]
    options: list[str] = Field(default_factory=lambda: ["fara", "supervisor", "none"])
    # The supervisor check needs a model that reads screenshots; the UI warns when it can't.
    supervisor_sees_images: bool = False


class VerifierUpdate(BaseModel):
    verifier: Literal["fara", "supervisor", "none"]
    model_config = ConfigDict(extra="forbid")


class ApprovalThresholdSelection(BaseModel):
    threshold: Literal["off", "critical", "high", "medium"]
    options: list[str] = Field(default_factory=lambda: list(THRESHOLDS))


class ApprovalThresholdUpdate(BaseModel):
    threshold: Literal["off", "critical", "high", "medium"]
    model_config = ConfigDict(extra="forbid")


class ApprovalAnswer(BaseModel):
    approve: bool
    scope: Literal["once", "step"] = "once"
    model_config = ConfigDict(extra="forbid")


class StatusResponse(BaseModel):
    enabled: bool
    approach: str
    execution_mode: str
    platform: str
    budgets: dict[str, int]


class EmergencyStopRequest(BaseModel):
    reason: str = Field(default="Operator emergency stop", min_length=1, max_length=500)


class EmergencyStopStatus(BaseModel):
    engaged: bool
    reason: str | None = None


# --- running a task ----------------------------------------------------------


@router.post("/runs", status_code=202, response_model=RunStarted, summary="Start one computer-use task")
async def start_run(
    body: ComputerUseTaskRunRequest,
    runtime: Runtime = Depends(get_runtime),
    config: AppConfig = Depends(get_config),
) -> RunStarted:
    """Start a task with the approach selected in the UI; follow it on ``/runs/{id}/events``.

    One run at a time: a second one is rejected with 409 while a run is active.
    """
    run_id = body.run_id or f"run-{os.urandom(4).hex()}"
    fields = {
        "thread_id": body.thread_id,
        "prompt": body.prompt,
        "approach": approach_name(get_state_value("agent_approach", None)),
        "execution_mode": get_state_value("execution_mode", "hybrid"),
    }
    return _launch(runtime, config, run_id, fields, resume=False)


@router.post("/runs/{run_id}/resume", status_code=202, response_model=RunStarted, summary="Continue an interrupted run")
async def resume_run(
    run_id: str = PathParam(pattern=RUN_ID_PATTERN),
    runtime: Runtime = Depends(get_runtime),
    config: AppConfig = Depends(get_config),
) -> RunStarted:
    """The step that was running starts over; completed steps are kept. Follow it on ``/runs/{id}/events``."""
    record = runtime.runs.records.read(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown run.")
    if record.get("status") != "interrupted":
        raise HTTPException(status_code=409, detail="Only an interrupted run can be resumed.")
    fields = {key: record[key] for key in ("thread_id", "prompt", "approach", "execution_mode", "started_at")}
    return _launch(runtime, config, run_id, fields, resume=True, spent=record.get("budget"))


@router.post("/runs/{run_id}/discard", summary="Give up an interrupted run")
async def discard_run(run_id: str = PathParam(pattern=RUN_ID_PATTERN), runtime: Runtime = Depends(get_runtime)) -> dict:
    if not await runtime.runs.discard(run_id):
        raise HTTPException(status_code=409, detail="Only an interrupted run can be discarded.")
    return {"run_id": run_id, "discarded": True}


def _launch(runtime: Runtime, config: AppConfig, run_id: str, fields: dict, *, resume: bool, spent: dict | None = None) -> RunStarted:
    # The computer-use model is the vision role's connection; its name is only for the UI.
    cu = config.computer_use.model_copy(update={"vision_model_name": vision_model(config)[1]})
    model_name = planner_model(config)
    session = RunSession(
        run_id=run_id,
        activity=new_activity(fields["thread_id"], run_id, fields["prompt"], model_name, cu),
        budget=BudgetMeter.from_config(cu.budgets, spent=spent),
        **fields,
    )

    def waiting(waiting: bool) -> None:
        if session.active:
            session.status = "awaiting_approval" if waiting else "running"

    session.approvals = Approvals(session.events, timeout_seconds=cu.approvals.timeout_seconds, budget=session.budget, on_waiting=waiting)
    try:
        runtime.runs.start(session, lambda: _execute(session, runtime, config, model_name, resume=resume))
    except RunActive as active:
        raise HTTPException(status_code=409, detail=f"A task is already running (run {active.run_id}).") from None
    return RunStarted(run_id=run_id, thread_id=session.thread_id)


async def _execute(session: RunSession, runtime: Runtime, config: AppConfig, model_name: str, *, resume: bool = False) -> None:
    cu = config.computer_use
    activity = session.activity

    # What the models said while working (planner reasoning, FARA's per-action notes).
    narration: list[dict[str, str]] = []

    def record(role: str, text: str | None) -> None:
        text = " ".join((text or "").split())
        if text and not (narration and narration[-1]["text"] == text):
            narration.append({"role": role, "text": text})

    def publish() -> None:
        budget = session.budget.snapshot() if session.budget is not None else None
        session.events.publish("activity", {"activity": activity.model_dump(mode="json"), "budget": budget})

    def on_activity(role: str, event: str, thought: str | None = None) -> None:
        update_activity(activity, role, event, thought)
        if thought and role in {"vision", "browser"}:
            record("vision", thought)
        publish()

    def on_plan_update(plan: Plan, results: list[StepResult], current_step_id: str | None) -> None:
        activity.plan_steps = plan_step_activity(plan, results, current_step_id)
        if current_step_id is not None and session.approvals is not None:
            session.approvals.current_step = current_step_id  # scope of "allow for this step"
        runtime.runs.records.write(session)  # a crash leaves a record that shows how far the run got
        publish()

    async def recall(prompt: str) -> dict:
        if runtime.memory is None or not memory_enabled():
            return {}
        try:
            experience = await memory_recall(runtime.memory, prompt, embedder=current_embedder(), run_id=session.run_id)
        except Exception:
            logger.warning("Recall failed; planning without memory", exc_info=True)
            return {}
        return experience.to_dict()

    async def ask(request: dict) -> dict:
        answer = await session.approvals.ask(kind="step", title=request["title"], reasons=request["reasons"], level=request["level"])
        return {"approve": answer.approve, "reason": answer.reason}

    def stopped(status: RunStatus, reason: str, event: str, **extra: str) -> None:
        set_idle(activity, event)
        output = {"status": "failed", "error": reason, **extra, "messages": [{"type": "ai", "content": f"⏹️ **Stopped.** {reason}"}]}
        runtime.runs.finish(session, status, output, error=reason)

    publish()
    build_planner = planner_factory(config)

    async def build_vision(_: Any) -> FaraVisionActionProvider:
        """The computer-use model, built once per run from its connection."""
        return FaraVisionActionProvider(*vision_model(config))

    supervisor = model_verifier = None
    if get_state_value("vision_recovery", False):
        supervisor = RecoverySupervisor(supervisor_factory(config))
    verification = get_state_value("verifier", "fara")
    if verification == "supervisor":
        model_verifier = ModelVerifier(supervisor_factory(config))

    try:
        context = RunContext(
            config=cu,
            desktop=runtime.desktop,
            browser=runtime.browser,
            executor=StepExecutor(
                cu,
                desktop=runtime.desktop,
                browser=runtime.browser,
                fara_factory=build_vision,
                supervisor=supervisor,
                verification=verification,
                model_verifier=model_verifier,
            ),
            planner=JsonLLMPlanner(build_planner, on_thinking=lambda text: record("planner", text), sees_images=planner_sees_images(config)),
            brief_model=build_planner,
            supervisor=supervisor,
            planner_context=planner_context({"text_model": model_name}),
            max_steps=min(cu.budgets.total_actions, 20),
            autonomous_max_actions=min(cu.budgets.total_actions, 40),
            on_activity=on_activity,
            on_plan_update=on_plan_update,
            approval_threshold=get_state_value("approval_threshold", cu.approvals.threshold),
            ask=ask,
            recall=recall,
        )
        run = run_graph(
            runtime.graph, context, run_id=session.run_id, prompt=session.prompt, approach=session.approach, execution_mode=session.execution_mode, resume=resume
        )
        result = await runtime.control.run(enforce_duration(run, session.budget))
    except BudgetExhausted as spent:
        return stopped("failed", str(spent), spent.code, error_code=spent.code)
    except RunStopped as stop:
        return stopped("stopped", str(stop), f"stopped: {stop}")
    except Exception as exc:
        logger.exception("Task run failed unexpectedly")
        set_idle(activity, "failed")
        runtime.runs.finish(session, "failed", {"status": "failed", "error": str(exc)}, error=str(exc))
        return

    session.activity = activity_from_result(session.thread_id, session.run_id, model_name, config, result)
    output = {
        "status": result.status.value,
        "plan": result.plan.model_dump(mode="json"),
        "steps": [step.model_dump(mode="json") for step in result.steps],
        "replan_count": result.replan_count,
        "request_count_estimate": result.request_count_estimate,
        "usage": {
            "planner_requests": result.planner_request_count,
            "planner_tokens": result.planner_total_tokens,
            "vision_requests": result.vision_request_count,
            "vision_tokens": result.vision_total_tokens,
        },
        "narration": narration,
        "messages": [{"type": "ai", "content": format_run_result(result, narration)}],
    }
    runtime.runs.finish(session, result.status.value, output, error=None if result.status.value == "completed" else result.summary)


@router.get("/runs/{run_id}/events", summary="Follow a run live (Server-Sent Events)")
async def run_events(
    request: Request,
    run_id: str = PathParam(pattern=RUN_ID_PATTERN),
    runtime: Runtime = Depends(get_runtime),
) -> EventSourceResponse:
    """``activity`` events while the run works, then one ``run.finished``; resumes after ``Last-Event-ID``."""
    session = runtime.runs.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That run is not active in this gateway; read it from /runs/{id}.")
    try:
        after = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        after = 0

    async def stream():
        async for event in session.events.follow(after):
            yield {"id": str(event.seq), "event": event.type, "data": json.dumps(event.data, ensure_ascii=False)}

    return EventSourceResponse(stream(), ping=15)


@router.post("/runs/{run_id}/approvals/{approval_id}", summary="Answer an approval the run is waiting for")
async def answer_approval(
    body: ApprovalAnswer,
    run_id: str = PathParam(pattern=RUN_ID_PATTERN),
    approval_id: str = PathParam(pattern=r"^[a-f0-9]{1,32}$"),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    session = runtime.runs.get(run_id)
    if session is None or session.approvals is None or not session.approvals.answer(approval_id, approve=body.approve, scope=body.scope):
        raise HTTPException(status_code=404, detail="Nothing is waiting for that answer (it may have timed out).")
    return {"id": approval_id, "approve": body.approve}


@router.post("/runs/{run_id}/cancel", summary="Cancel the active run (later runs are not blocked)")
async def cancel_run(run_id: str = PathParam(pattern=RUN_ID_PATTERN), runtime: Runtime = Depends(get_runtime)) -> dict:
    if not runtime.runs.cancel(run_id):
        raise HTTPException(status_code=404, detail="That run is not active.")
    return {"run_id": run_id, "cancelled": True}


@router.get("/runs", summary="Recent runs, newest first")
async def list_runs(limit: int = Query(default=20, ge=1, le=50), runtime: Runtime = Depends(get_runtime)) -> list[dict]:
    return runtime.runs.records.list(limit)


@router.get("/runs/{run_id}", summary="One run: live while active, else its record")
async def get_run(run_id: str = PathParam(pattern=RUN_ID_PATTERN), runtime: Runtime = Depends(get_runtime)) -> dict:
    if (session := runtime.runs.get(run_id)) is not None:
        return session.record()
    if (record := runtime.runs.records.read(run_id)) is None:
        raise HTTPException(status_code=404, detail="Unknown run.")
    return record


# --- operator selections (persisted between launches) -------------------------


@router.get("/execution-mode", response_model=ExecutionModeSelection, summary="Get the execution mode")
async def get_execution_mode() -> ExecutionModeSelection:
    return ExecutionModeSelection(execution_mode=get_state_value("execution_mode", "hybrid"))


@router.post("/execution-mode", response_model=ExecutionModeSelection, summary="Switch the execution mode")
async def set_execution_mode(body: ExecutionModeUpdate) -> ExecutionModeSelection:
    set_state_value("execution_mode", body.execution_mode)
    return ExecutionModeSelection(execution_mode=body.execution_mode)


@router.get("/approach", response_model=AgentApproachSelection, summary="Get the agent approach")
async def get_agent_approach() -> AgentApproachSelection:
    return AgentApproachSelection(approach=approach_name(get_state_value("agent_approach", None)))


@router.post("/approach", response_model=AgentApproachSelection, summary="Switch the agent approach")
async def set_agent_approach(body: AgentApproachUpdate) -> AgentApproachSelection:
    set_state_value("agent_approach", body.approach)
    return AgentApproachSelection(approach=body.approach)


@router.get("/vision/recovery", response_model=VisionRecoverySelection, summary="Is the recovery supervisor on?")
async def get_vision_recovery() -> VisionRecoverySelection:
    return VisionRecoverySelection(enabled=bool(get_state_value("vision_recovery", False)))


@router.post("/vision/recovery", response_model=VisionRecoverySelection, summary="Turn the recovery supervisor on or off")
async def set_vision_recovery(body: VisionRecoverySelection) -> VisionRecoverySelection:
    set_state_value("vision_recovery", body.enabled)
    return VisionRecoverySelection(enabled=body.enabled)


@router.get("/approvals/threshold", response_model=ApprovalThresholdSelection, summary="Ask before steps of this planned risk")
async def get_approval_threshold(config: AppConfig = Depends(get_config)) -> ApprovalThresholdSelection:
    return ApprovalThresholdSelection(threshold=get_state_value("approval_threshold", config.computer_use.approvals.threshold))


@router.post("/approvals/threshold", response_model=ApprovalThresholdSelection, summary="Switch the approval threshold")
async def set_approval_threshold(body: ApprovalThresholdUpdate) -> ApprovalThresholdSelection:
    set_state_value("approval_threshold", body.threshold)
    return ApprovalThresholdSelection(threshold=body.threshold)


def verifier_response(config: AppConfig) -> VerifierSelection:
    return VerifierSelection(verifier=get_state_value("verifier", "fara"), supervisor_sees_images=supervisor_sees_images(config))


@router.get("/vision/verifier", response_model=VerifierSelection, summary="Who checks a step FARA did not finish")
async def get_verifier(config: AppConfig = Depends(get_config)) -> VerifierSelection:
    return verifier_response(config)


@router.post("/vision/verifier", response_model=VerifierSelection, summary="Switch the step check: FARA, the supervisor model, or off")
async def set_verifier(body: VerifierUpdate, config: AppConfig = Depends(get_config)) -> VerifierSelection:
    set_state_value("verifier", body.verifier)
    return verifier_response(config)


# --- status, health, emergency stop, logs -------------------------------------


@router.get("/status", response_model=StatusResponse, summary="Is the agent enabled, and its budgets")
async def get_status(config: AppConfig = Depends(get_config)) -> StatusResponse:
    cu = config.computer_use
    budgets = cu.budgets
    return StatusResponse(
        enabled=cu.enabled,
        approach=approach_name(get_state_value("agent_approach", None)),
        execution_mode=get_state_value("execution_mode", "hybrid"),
        platform=cu.platform,
        budgets={"planner_calls": budgets.planner_calls, "vision_calls": budgets.vision_calls, "supervisor_calls": budgets.supervisor_calls, "total_actions": budgets.total_actions, "duration_seconds": budgets.duration_seconds},
    )


@router.get("/emergency-stop", response_model=EmergencyStopStatus, summary="Emergency-stop status")
async def get_emergency_stop(runtime: Runtime = Depends(get_runtime)) -> EmergencyStopStatus:
    return EmergencyStopStatus(engaged=runtime.control.stopped, reason=runtime.control.stop_reason)


@router.post("/emergency-stop/engage", response_model=EmergencyStopStatus, summary="Stop every running task and block further actions")
async def engage_emergency_stop(request: EmergencyStopRequest, runtime: Runtime = Depends(get_runtime)) -> EmergencyStopStatus:
    await runtime.control.engage(request.reason)
    return await get_emergency_stop(runtime)


@router.post("/emergency-stop/reset", response_model=EmergencyStopStatus, summary="Allow actions again")
async def reset_emergency_stop(request: EmergencyStopRequest, runtime: Runtime = Depends(get_runtime)) -> EmergencyStopStatus:
    runtime.control.reset()
    return await get_emergency_stop(runtime)


@router.get("/logs/{source}", response_class=PlainTextResponse, summary="Read a log (agent | gateway | ui)")
async def get_operator_logs(source: str) -> str:
    return logs.read_log(source)


@router.delete("/logs/{source}", response_class=PlainTextResponse, summary="Clear a log")
async def clear_operator_logs(source: str) -> str:
    return logs.clear_log(source)
