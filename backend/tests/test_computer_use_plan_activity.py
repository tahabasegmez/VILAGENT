from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.gateway.routers.computer_use import (
    AgentActivityItem,
    AgentActivityResponse,
    PlanStepActivityItem,
    _PLAN_EXECUTE_ACTIVITY,
    _plan_step_activity,
    get_agent_activity,
)
from vilagent.computer_use.models import RiskLevel
from vilagent.computer_use.plan_execute import ComputerUsePlan, ComputerUsePlanStep, EnvironmentContext, StepExecutionResult, StepStatus


def _plan() -> ComputerUsePlan:
    return ComputerUsePlan(
        goal="Sort Downloads",
        steps=[
            ComputerUsePlanStep(step_id="s1", instruction="Open Explorer.", risk={"level": RiskLevel.low}),
            ComputerUsePlanStep(step_id="s2", instruction="Open Downloads.", risk={"level": RiskLevel.low}),
            ComputerUsePlanStep(step_id="s3", instruction="Click Date modified once.", risk={"level": RiskLevel.low}),
        ],
    )


def test_live_plan_activity_has_exactly_one_running_step():
    items = _plan_step_activity(_plan(), [], "s2")

    assert [item.status for item in items] == ["pending", "running", "pending"]
    assert [item.max_actions for item in items] == [4, 4, 4]


def test_completed_step_advances_and_error_is_explicit():
    results = [
        StepExecutionResult(
            step_id="s1",
            environment=EnvironmentContext.native,
            requires_vision=False,
            status=StepStatus.completed,
            summary="done",
        ),
        StepExecutionResult(
            step_id="s2",
            environment=EnvironmentContext.native,
            requires_vision=True,
            status=StepStatus.failed,
            error_code="step_failed",
            summary="failed",
        ),
    ]

    items = _plan_step_activity(_plan(), results, None)

    assert [item.status for item in items] == ["completed", "failed", "pending"]
    assert items[1].error_code == "step_failed"


def test_run_specific_activity_request_returns_live_plan():
    activity = AgentActivityResponse(
        thread_id="thread-1",
        run_id="run-1",
        agents=[AgentActivityItem(agent_id="lead", role="lead", status="running")],
        plan_steps=[
            PlanStepActivityItem(
                step_id="s1",
                instruction="Open a window.",
                completion_criteria="The window is open.",
                status="running",
                requires_vision=False,
            )
        ],
    )
    _PLAN_EXECUTE_ACTIVITY["thread-1"] = activity
    try:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        result = asyncio.run(get_agent_activity("thread-1", request, run_id="run-1"))
    finally:
        _PLAN_EXECUTE_ACTIVITY.pop("thread-1", None)

    assert result is activity
    assert result.plan_steps[0].max_actions == 4
