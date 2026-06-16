from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.gateway.routers.computer_use import ComputerUseTaskRunRequest
from vilagent.computer_use.models import PolicyDecision, RiskLevel
from vilagent.computer_use.plan_execute import ComputerUsePlan, ComputerUsePlanStep, _PLANNER_SYSTEM_PROMPT, _plan_approval_action, _planner_context
from vilagent.computer_use.policy import DefaultActionPolicy


def test_task_run_requires_ui_auto_approval_threshold():
    with pytest.raises(ValidationError):
        ComputerUseTaskRunRequest(thread_id="thread", prompt="Open Calculator")


def test_planned_step_requires_explicit_risk_level():
    with pytest.raises(ValidationError):
        ComputerUsePlanStep(step_id="s1", instruction="Open Calculator")
    with pytest.raises(ValidationError):
        ComputerUsePlanStep(step_id="s1", instruction="Open Calculator", risk={})

    step = ComputerUsePlanStep(
        step_id="s1",
        instruction="Open Calculator",
        risk={"level": RiskLevel.low},
    )
    assert step.risk.level == RiskLevel.low


def test_plan_approval_always_requires_explicit_approval():
    plan = ComputerUsePlan(
        goal="Open Downloads",
        steps=[ComputerUsePlanStep(step_id="s1", instruction="Open Downloads", risk={"level": RiskLevel.low})],
    )

    action = _plan_approval_action(plan, "session-1")

    assert action.auto_approve_risk_threshold is None
    assert DefaultActionPolicy().evaluate(action).decision == PolicyDecision.require_approval


def test_planner_prompt_requires_isolated_commands_and_separate_checks():
    # Wording tightened for token efficiency; the intent (one command per step,
    # checks in their own step, an explicit completion criterion) must remain.
    assert "exactly one user-visible command" in _PLANNER_SYSTEM_PROMPT
    assert "every check" in _PLANNER_SYSTEM_PROMPT
    assert "completion_criteria" in _PLANNER_SYSTEM_PROMPT


def test_planner_prompt_requires_localized_executor_ready_steps():
    assert "context.windows_ui_language" in _PLANNER_SYSTEM_PROMPT
    assert "Executor-ready steps" in _PLANNER_SYSTEM_PROMPT
    assert "its own step" in _PLANNER_SYSTEM_PROMPT
    assert "canonical keys" in _PLANNER_SYSTEM_PROMPT
    assert "max_actions: bounded executor budget" in _PLANNER_SYSTEM_PROMPT
    assert "never >6" in _PLANNER_SYSTEM_PROMPT
    assert "popups, overlays, loading states" in _PLANNER_SYSTEM_PROMPT
    assert "16 concise steps" in _PLANNER_SYSTEM_PROMPT


def test_planner_context_includes_windows_ui_language(monkeypatch):
    monkeypatch.setattr("vilagent.computer_use.plan_execute._windows_ui_language", lambda: "tr-TR")

    assert _planner_context({"architecture": "plan_execute"}) == {
        "architecture": "plan_execute",
        "windows_ui_language": "tr-TR",
    }
