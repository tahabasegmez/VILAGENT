"""Live run activity: what each agent is doing, and the plan's step states."""

from __future__ import annotations

from pydantic import BaseModel, Field

from vilagent.agents.common import Plan, RunResult, StepResult
from vilagent.config.app_config import AppConfig
from vilagent.config.computer_use_config import ComputerUseConfig


class AgentActivityItem(BaseModel):
    agent_id: str
    role: str
    status: str
    task: str | None = None
    model_name: str | None = None
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: list[str] = Field(default_factory=list)
    last_event: str | None = None
    current_thought: str | None = None
    last_updated_at: str | None = None


class PlanStepActivityItem(BaseModel):
    step_id: str
    instruction: str
    completion_criteria: str
    max_actions: int = 4
    status: str
    requires_vision: bool
    error_code: str | None = None
    summary: str | None = None


class AgentActivityResponse(BaseModel):
    thread_id: str
    run_id: str | None = None
    agents: list[AgentActivityItem]
    plan_steps: list[PlanStepActivityItem] = Field(default_factory=list)
    total_request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0


ROLE_AGENT_IDS = {"lead": "computer_use_plan_execute", "vision": "vision_executor", "uia": "uia_executor", "browser": "browser_executor"}


def new_activity(thread_id: str, run_id: str, prompt: str, model_name: str, cu: ComputerUseConfig) -> AgentActivityResponse:
    return AgentActivityResponse(
        thread_id=thread_id,
        run_id=run_id,
        agents=[
            AgentActivityItem(agent_id="computer_use_plan_execute", role="lead", status="running", task=prompt, model_name=model_name, last_event="planning"),
            AgentActivityItem(
                agent_id="vision_executor",
                role="subagent",
                status="idle",
                task="Vision-driven actions.",
                model_name=cu.vision_model_name or None,
                last_event="idle",
            ),
            AgentActivityItem(agent_id="uia_executor", role="subagent", status="idle", task="Windows UI Automation.", last_event="idle"),
            AgentActivityItem(agent_id="browser_executor", role="subagent", status="idle", task="Managed browser.", last_event="idle"),
        ],
    )


def update_activity(activity: AgentActivityResponse, role: str, event: str, thought: str | None) -> None:
    """Mark the reporting agent as the one running and record its latest event/thought."""
    agent_id = ROLE_AGENT_IDS.get(role, role)
    for agent in activity.agents:
        if agent.agent_id == agent_id:
            agent.status = "running"
            agent.last_event = event
            if thought is not None:
                agent.current_thought = thought
        else:
            agent.status = "idle"


def set_idle(activity: AgentActivityResponse, event: str) -> None:
    for agent in activity.agents:
        agent.status = "idle"
    activity.agents[0].last_event = event


def activity_from_result(
    thread_id: str,
    run_id: str | None,
    model_name: str,
    config: AppConfig,
    result: RunResult,
) -> AgentActivityResponse:
    planner_requests = getattr(result, "planner_request_count", 0) or result.request_count_estimate
    planner_tokens = getattr(result, "planner_total_tokens", 0) or 0
    vision_requests = getattr(result, "vision_request_count", 0) or 0
    vision_tokens = getattr(result, "vision_total_tokens", 0) or 0
    agents = [
        AgentActivityItem(
            agent_id="computer_use_plan_execute",
            role="lead",
            status="idle",
            task=result.plan.goal,
            model_name=model_name,
            request_count=planner_requests,
            total_tokens=planner_tokens,
            last_event=f"{result.status.value}; replans={result.replan_count}",
        )
    ]
    for executor_id, label in (
        ("vision_executor", "vision"),
        ("uia_executor", "uia"),
        ("browser_executor", "browser"),
    ):
        if label == "vision":
            executor_steps = [step for step in result.steps if step.requires_vision]
        elif label == "browser":
            executor_steps = [step for step in result.steps if not step.requires_vision and step.environment == "browser"]
        else:
            executor_steps = [step for step in result.steps if not step.requires_vision and step.environment == "native"]
        total = len(executor_steps)
        failed = next((step for step in executor_steps if step.status.value in {"blocked", "failed"}), None)
        agents.append(
            AgentActivityItem(
                agent_id=executor_id,
                role="subagent",
                status="idle",
                task=f"{total} step(s) handled." if total else "No work in last plan.",
                model_name=config.computer_use.vision_model_name or None if label == "vision" else None,
                request_count=vision_requests if label == "vision" else 0,
                total_tokens=vision_tokens if label == "vision" else 0,
                tool_calls=[step.status.value for step in executor_steps],
                last_event=(failed.summary if failed else executor_steps[-1].summary if executor_steps else "idle"),
            )
        )
    return AgentActivityResponse(
        thread_id=thread_id,
        run_id=run_id,
        agents=agents,
        plan_steps=plan_step_activity(result.plan, result.steps, None),
        total_request_count=sum(agent.request_count for agent in agents),
        total_input_tokens=0,
        total_output_tokens=0,
        total_tokens=sum(agent.total_tokens for agent in agents),
    )


def plan_step_activity(plan: Plan, results: list[StepResult], current_step_id: str | None) -> list[PlanStepActivityItem]:
    result_by_step = {result.step_id: result for result in results}
    return [
        PlanStepActivityItem(
            step_id=step.step_id,
            instruction=step.instruction,
            completion_criteria=step.completion_criteria,
            max_actions=step.max_actions,
            status=(
                result_by_step[step.step_id].status.value
                if step.step_id in result_by_step
                else "running"
                if step.step_id == current_step_id
                else "pending"
            ),
            requires_vision=step.requires_vision,
            error_code=result_by_step[step.step_id].error_code if step.step_id in result_by_step else None,
            summary=result_by_step[step.step_id].summary if step.step_id in result_by_step else None,
        )
        for step in plan.steps
    ]
