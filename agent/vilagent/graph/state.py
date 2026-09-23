"""The run graph's state (checkpointed, JSON-safe) and context (per run, never checkpointed)."""

from __future__ import annotations

import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from vilagent.agents.common import ActivityCallback, Plan, StepResult
from vilagent.agents.plan_execute import PlanCallback, StepExecutor
from vilagent.agents.planner import Planner
from vilagent.agents.supervisor import RecoverySupervisor
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.env.base import Environment

SCHEMA_VERSION = 1


class RunState(TypedDict, total=False):
    """Plain JSON only, so checkpoints stay readable across code changes (bump the version if the shape changes)."""

    schema_version: int
    prompt: str
    approach: str  # plan | brief | direct
    execution_mode: str  # hybrid | vision_only
    max_replans: int
    experience: dict[str, Any]  # memory.retrieval.Experience.to_dict(); {} when memory is off
    plan: dict[str, Any] | None  # Plan.model_dump(mode="json")
    results: Annotated[list[dict[str, Any]], operator.add]  # StepResult dumps, append-only
    current_step_id: str | None
    replans: int
    status: str
    summary: str


@dataclass
class RunContext:
    """The live objects a run needs; rebuilt for every run (and for a resumed one)."""

    config: ComputerUseConfig
    desktop: Environment
    browser: Environment
    executor: StepExecutor
    planner: Planner
    #: Builds the chat model that writes the autonomous brief (``thinking_enabled`` in, model out).
    brief_model: Callable[[bool], Any]
    supervisor: RecoverySupervisor | None = None
    planner_context: dict[str, Any] = field(default_factory=dict)
    max_steps: int = 20
    autonomous_max_actions: int = 40
    on_activity: ActivityCallback | None = None
    on_plan_update: PlanCallback | None = None
    # Steps at or above this planned risk wait for the operator (approvals.policy.THRESHOLDS).
    approval_threshold: str = "high"
    # Asks the operator about a step: request dict in, {"approve": bool, "reason": str} out.
    ask: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # Recalls experience for the task (prompt in, Experience dict out); None = no memory.
    recall: Callable[[str], Awaitable[dict[str, Any]]] | None = None

    def notify(self, role: str, event: str, thought: str | None = None) -> None:
        if self.on_activity:
            self.on_activity(role, event, thought)

    def publish(self, plan: Plan, results: list[StepResult], current_step_id: str | None) -> None:
        if self.on_plan_update:
            self.on_plan_update(plan, results, current_step_id)


def plan_of(state: RunState) -> Plan:
    return Plan.model_validate(state["plan"]) if state.get("plan") else Plan(goal=state.get("prompt", ""))


def results_of(state: RunState) -> list[StepResult]:
    return [StepResult.model_validate(result) for result in state.get("results") or []]
