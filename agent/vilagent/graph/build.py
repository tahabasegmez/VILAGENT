"""Build the run graph and run one task through it."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command

from vilagent.agents.common import RunResult, StepStatus
from vilagent.graph import nodes
from vilagent.graph.state import SCHEMA_VERSION, RunContext, RunState, plan_of, results_of
from vilagent.runs.trace import span

logger = logging.getLogger(__name__)

MAX_REPLANS = 2


class NothingToResume(RuntimeError):
    """The run has no checkpoint (it ended, was discarded, or expired)."""


def traced(name: str, node: Callable[..., Any]) -> Callable[..., Any]:
    """The node as one span of the run's live trace; a gate waiting for the operator shows as ``waiting``.

    Always async, so sync nodes run on the event loop (their events are published there) rather
    than in LangGraph's thread pool.
    """
    takes_runtime = len(inspect.signature(node).parameters) > 1

    async def run(state: RunState, runtime: Runtime[RunContext]) -> Any:
        with span(name, pause_on=(GraphInterrupt,)):
            result = node(state, runtime) if takes_runtime else node(state)
            return await result if inspect.isawaitable(result) else result

    run.__name__ = name
    return run


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> Any:
    graph = StateGraph(RunState, context_schema=RunContext)
    for name in ("recall", "plan", "select_step", "gate", "execute_step", "replan", "brief", "direct", "execute_task", "finalize"):
        graph.add_node(name, traced(name, getattr(nodes, name)))

    graph.add_edge(START, "recall")
    graph.add_conditional_edges("recall", nodes.route_start, ["plan", "brief", "direct"])
    graph.add_edge("plan", "select_step")
    graph.add_conditional_edges("select_step", nodes.route_step, ["gate", "finalize"])
    graph.add_conditional_edges("gate", nodes.route_gate, ["execute_step", "execute_task", "finalize"])
    graph.add_conditional_edges("execute_step", nodes.route_after_step, ["select_step", "replan", "finalize"])
    graph.add_edge("replan", "select_step")
    graph.add_edge("brief", "gate")
    graph.add_edge("direct", "gate")
    graph.add_edge("execute_task", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


async def _ask(context: RunContext, request: dict[str, Any]) -> dict[str, Any]:
    if context.ask is None:
        return {"approve": False, "reason": "Nobody could be asked to approve this step."}
    return await context.ask(request)


def recursion_limit(max_steps: int, max_replans: int) -> int:
    """LangGraph's default (25 super-steps) is below what a 20-step plan with replans needs."""
    return 20 + max_steps * 5 * (max_replans + 1)


async def run_graph(
    graph: Any,
    context: RunContext,
    *,
    run_id: str,
    prompt: str,
    approach: str,
    execution_mode: str,
    max_replans: int = MAX_REPLANS,
    resume: bool = False,
) -> RunResult:
    """Run a task through the graph, or with ``resume`` continue an interrupted one from its checkpoint.

    Resuming re-runs the step that was in flight from its start (completed steps are kept), and
    asks again if the run was waiting for an approval. Checkpoints are deleted by the caller
    once the run has really ended (``RunManager``).
    """
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": recursion_limit(context.max_steps, max_replans)}
    initial: RunState = {
        "schema_version": SCHEMA_VERSION,
        "prompt": prompt,
        "approach": approach,
        "execution_mode": execution_mode,
        "max_replans": max_replans,
        "replans": 0,
    }
    # "sync": a step's checkpoint is on disk before the next step starts. (LangGraph fails
    # with a durability mode but no checkpointer, so it is only set when there is one.)
    durability = "sync" if graph.checkpointer is not None else None
    command: Any = initial
    if resume:
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            raise NothingToResume("Its saved progress is gone, so the run cannot be resumed.")
        context.publish(plan_of(snapshot.values), results_of(snapshot.values), None)
        command = Command(resume=await _ask(context, snapshot.interrupts[0].value)) if snapshot.interrupts else None
    while True:
        state = await graph.ainvoke(command, config, context=context, durability=durability)
        interrupts = state.get("__interrupt__") or []
        if not interrupts:
            break
        # A gate is waiting for the operator: ask, then resume the graph with the answer.
        command = Command(resume=await _ask(context, interrupts[0].value))
    replans = state.get("replans", 0)
    fara = context.executor.fara
    return RunResult(
        status=StepStatus(state["status"]),
        plan=plan_of(state),
        steps=results_of(state),
        replan_count=replans,
        request_count_estimate=1 + replans,
        summary=state["summary"],
        planner_request_count=getattr(context.planner, "request_count", 0),
        planner_total_tokens=getattr(context.planner, "total_tokens", 0),
        vision_request_count=getattr(fara, "request_count", 0),
        vision_total_tokens=getattr(fara, "total_tokens", 0),
    )
