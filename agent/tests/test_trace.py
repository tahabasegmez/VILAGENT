"""The live run trace: spans, their statuses, and what the graph and the FARA loop report."""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeEnv, FakeFara, click, config, finish, run_graph

from vilagent.actions import Action, ActionKind
from vilagent.agents.common import Plan, PlanStep, RiskLevel, StepStatus
from vilagent.agents.vision_loop import LoopLimits, run_vision_loop
from vilagent.graph import nodes
from vilagent.runs import trace
from vilagent.runs.budget import BudgetMeter, count_tokens
from vilagent.runs.records import TYPED_TEXT
from vilagent.runs.session import RunSession, _current
from vilagent.server.activity import new_activity


@pytest.fixture
def session():
    run = RunSession(
        run_id="r1", thread_id="t", prompt="task", approach="plan_execute", execution_mode="hybrid", activity=new_activity("t", "r1", "task", "m", config()), budget=BudgetMeter({}, 60)
    )
    token = _current.set(run)
    yield run
    _current.reset(token)


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    monkeypatch.setattr(nodes, "STEP_SETTLE_SECONDS", 0)


def spans(session: RunSession) -> list[dict]:
    """Each span's latest state, in the order the spans started."""
    latest: dict[str, dict] = {}
    for event in session.events._events:
        if event.type == "trace":
            latest[event.data["id"]] = event.data
    return list(latest.values())


def test_spans_nest_and_end_with_their_status(session):
    with trace.span("outer") as outer:
        with trace.span("inner", kind="model"):
            trace.note(output="x" * 5000, thinking="hmm", memory=["a lesson"], meta={"step_id": "s1"})
        with trace.span("failed", kind="action"):
            trace.note(ok=False)
    with pytest.raises(RuntimeError), trace.span("broken"):
        raise RuntimeError("boom")
    with pytest.raises(KeyError), trace.span("paused", pause_on=(KeyError,)):
        raise KeyError("wait")

    by_name = {item["name"]: item for item in spans(session)}
    assert by_name["inner"]["parent"] == outer.id and by_name["outer"]["parent"] is None
    assert (by_name["inner"]["thinking"], by_name["inner"]["memory"], by_name["inner"]["meta"]) == ("hmm", ["a lesson"], {"step_id": "s1"})
    assert len(by_name["inner"]["output"]) == trace.MAX_TEXT
    assert [by_name[name]["status"] for name in ("outer", "inner", "failed", "broken", "paused")] == ["done", "done", "error", "error", "waiting"]
    assert by_name["broken"]["output"] == "boom" and by_name["paused"]["ended_at"] is None


def test_nothing_is_traced_outside_a_run():
    with trace.span("free") as free:
        trace.note(output="ignored")
    assert free is None


def test_the_fara_loop_traces_each_turn_and_masks_typed_text(session):
    typing = Action(kind=ActionKind.type_text, args={"text": "my password"}, thought="Typing it in.")
    fara = FakeFara([typing, click(), finish()])

    result = asyncio.run(
        run_vision_loop(
            env=FakeEnv(failures=[None, "not clickable"]),
            fara=fara,
            instruction=lambda: "fill the form",
            limits=LoopLimits(max_actions=4),
            config=config(),
            goal="g",
            done_when="d",
            role="vision",
        )
    )

    turns = [item for item in spans(session) if item["name"] == "fara"]
    assert result.status == StepStatus.completed
    assert [item["label"] for item in turns] == ["FARA #1", "FARA #2", "FARA #3"]
    assert turns[0]["output"] == f'type_text {{"text": "{TYPED_TEXT}"}}' and turns[0]["thinking"] == "Typing it in."
    assert "my password" not in str(session.events._events)
    assert [item["status"] for item in turns] == ["done", "error", "done"]
    assert turns[1]["output"].endswith("→ not clickable")


def test_the_graph_traces_every_node_and_a_waiting_gate(session):
    risky = PlanStep(step_id="s1", instruction="send it", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "enter"}, risk={"level": RiskLevel.high})

    class Planner:
        async def plan(self, prompt, *, context):
            return Plan(goal="g", steps=[risky])

    async def approve(request):
        return {"approve": True}

    async def recall(prompt):
        return {"similar_runs": [{"task": "sent it before", "plan": []}], "lessons": [{"key_type": "general", "key": "", "text": "Check the address.", "source": "auto"}]}

    result = run_graph(desktop=FakeEnv(), browser=FakeEnv("browser"), fara=FakeFara([]), planner=Planner(), ask=approve, recall=recall)

    traced = spans(session)
    assert result.status == StepStatus.completed
    assert [item["name"] for item in traced] == ["recall", "plan", "select_step", "gate", "gate", "execute_step", "select_step", "finalize"]
    assert [item["status"] for item in traced if item["name"] == "gate"] == ["waiting", "done"]
    assert traced[0]["memory"] == ["Run: sent it before", "Lesson: Check the address."]
    assert traced[1]["output"] == "1. send it"
    step = traced[5]
    assert (step["label"], step["meta"], step["status"]) == ("send it", {"step_id": "s1"}, "done")
    assert traced[-1]["output"] == "completed: Plan completed."


def test_token_counts_reach_the_budget_snapshot(session):
    count_tokens("planner", 120)
    count_tokens("vision", 30)
    count_tokens("vision", 12)

    assert session.budget.snapshot()["tokens"] == {"planner": 120, "vision": 42}
    assert BudgetMeter.from_config(config().budgets, spent=session.budget.snapshot()).tokens == {"planner": 120, "vision": 42}
