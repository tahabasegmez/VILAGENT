"""Checkpoint-friendly state schema for VILAGENT agent graphs."""

from typing import NotRequired, TypedDict

from langchain.agents import AgentState


class DesktopSessionState(TypedDict):
    session_id: str
    platform: str
    monitor_id: str


class CostBudgetState(TypedDict):
    planner_calls_used: int
    vision_calls_used: int
    actions_used: int


class FailureBudgetState(TypedDict):
    retries_used: int
    consecutive_failures: int


class ComputerUseState(AgentState):
    """Computer-use graph channels kept independent from VILAGENT file state.

    The lead-agent integration phase may compose these channels with selected
    VILAGENT reducers. Keeping the domain state independent now prevents
    sandbox/file fields from becoming an accidental execution dependency.
    """

    desktop_session: NotRequired[DesktopSessionState | None]
    plan: NotRequired[dict | None]
    current_step_id: NotRequired[str | None]
    latest_observation_id: NotRequired[str | None]
    latest_action_id: NotRequired[str | None]
    pending_approval_id: NotRequired[str | None]
    active_recipe_id: NotRequired[str | None]
    cost_budget: NotRequired[CostBudgetState | None]
    failure_budget: NotRequired[FailureBudgetState | None]
