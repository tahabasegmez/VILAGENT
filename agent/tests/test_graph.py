"""The run graph itself: limits, checkpoints and the SQLite checkpointer."""

from __future__ import annotations

import asyncio

import fakes
import pytest
from fakes import FakeEnv, FakeFara, config, fara_factory, finish
from langgraph.checkpoint.memory import InMemorySaver

from vilagent.actions import ActionKind
from vilagent.agents.common import Plan, PlanStep, RiskLevel, StepStatus
from vilagent.agents.plan_execute import StepExecutor
from vilagent.agents.planner import planner_context
from vilagent.graph import build, nodes
from vilagent.graph.state import RunContext


class Planner:
    def __init__(self, plan: Plan):
        self._plan = plan

    async def plan(self, prompt, *, context):
        return self._plan

    async def replan(self, prompt, **kwargs):
        return self._plan


def _hotkey(index: int) -> PlanStep:
    return PlanStep(step_id=f"s{index}", instruction="press enter", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "enter"}, risk={"level": RiskLevel.low})


def _context(plan: Plan, desktop: FakeEnv | None = None) -> RunContext:
    desktop = desktop or FakeEnv()
    browser = FakeEnv("browser")
    executor = StepExecutor(config(), desktop=desktop, browser=browser, fara_factory=fara_factory(FakeFara([finish()])))
    return RunContext(config=config(), desktop=desktop, browser=browser, executor=executor, planner=Planner(plan), brief_model=lambda thinking=False: None, planner_context=planner_context(None))


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    monkeypatch.setattr(nodes, "STEP_SETTLE_SECONDS", 0)


def test_a_full_plan_fits_the_recursion_limit():
    context = _context(Plan(goal="g", steps=[_hotkey(i) for i in range(20)]))

    result = asyncio.run(build.run_graph(build.build_graph(), context, run_id="r", prompt="g", approach="plan_execute", execution_mode="hybrid"))

    assert result.status == StepStatus.completed and len(result.steps) == 20


class Crash(BaseException):
    """Stands in for the process dying mid-step: nothing in the graph handles it."""


def _crash_on(context: RunContext, step_id: str) -> None:
    execute = context.executor.execute

    async def crashing(step, **kwargs):
        if step.step_id == step_id:
            raise Crash()
        return await execute(step, **kwargs)

    context.executor.execute = crashing


def test_a_crashed_run_resumes_at_the_unfinished_step():
    saver = InMemorySaver()
    graph = build.build_graph(saver)
    plan = Plan(goal="g", steps=[_hotkey(1), _hotkey(2), _hotkey(3)])
    first = _context(plan)
    _crash_on(first, "s2")

    with pytest.raises(Crash):
        asyncio.run(build.run_graph(graph, first, run_id="r", prompt="g", approach="plan_execute", execution_mode="hybrid"))
    assert len(first.desktop.actions) == 1  # s1 ran, then the "crash" during s2

    resumed = _context(plan)
    result = asyncio.run(build.run_graph(graph, resumed, run_id="r", prompt="g", approach="plan_execute", execution_mode="hybrid", resume=True))

    assert result.status == StepStatus.completed
    assert [step.step_id for step in result.steps] == ["s1", "s2", "s3"]
    assert len(resumed.desktop.actions) == 2  # s1 was not repeated


def test_a_run_interrupted_while_waiting_for_approval_asks_again():
    graph = build.build_graph(InMemorySaver())
    risky = PlanStep(step_id="s1", instruction="send it", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "enter"}, risk={"level": "high"})
    first, resumed = _context(Plan(goal="g", steps=[risky])), _context(Plan(goal="g", steps=[risky]))
    asked = []

    async def crash_while_waiting(request):
        raise Crash()

    async def approve(request):
        asked.append(request["title"])
        return {"approve": True}

    first.ask, resumed.ask = crash_while_waiting, approve
    with pytest.raises(Crash):
        asyncio.run(build.run_graph(graph, first, run_id="r", prompt="g", approach="plan_execute", execution_mode="hybrid"))

    result = asyncio.run(build.run_graph(graph, resumed, run_id="r", prompt="g", approach="plan_execute", execution_mode="hybrid", resume=True))

    assert (result.status, asked, len(resumed.desktop.actions)) == (StepStatus.completed, ["send it"], 1)


def test_nothing_to_resume_without_a_checkpoint():
    with pytest.raises(build.NothingToResume):
        asyncio.run(build.run_graph(build.build_graph(InMemorySaver()), _context(Plan(goal="g")), run_id="gone", prompt="g", approach="plan_execute", execution_mode="hybrid", resume=True))


def test_state_round_trips_through_the_sqlite_checkpointer(tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def run():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
            context = _context(Plan(goal="g", steps=[_hotkey(1), _hotkey(2)]))
            return await build.run_graph(build.build_graph(saver), context, run_id="r", prompt="g", approach="plan_execute", execution_mode="hybrid")

    result = asyncio.run(run())

    assert result.status == StepStatus.completed and [step.step_id for step in result.steps] == ["s1", "s2"]


def test_direct_hands_the_prompt_to_fara_without_a_planner():
    fara = FakeFara([finish()])

    # planner=None: a planner call of any kind would raise, so this also proves there is none.
    result = fakes.run_graph(desktop=FakeEnv(), browser=FakeEnv("browser"), fara=fara, planner=None, prompt="scroll the page down", approach="direct")

    assert result.status == StepStatus.completed
    assert [(step.step_id, step.instruction) for step in result.plan.steps] == [("direct", "scroll the page down")]
    assert result.planner_request_count == 0


def test_a_direct_task_that_says_send_asks_the_operator_first():
    asked = []

    async def approve(request):
        asked.append((request["level"], request["title"]))
        return {"approve": True}

    result = fakes.run_graph(
        desktop=FakeEnv(), browser=FakeEnv("browser"), fara=FakeFara([finish()]), planner=None, prompt="send the report to Ada", approach="direct", approval_threshold="high", ask=approve
    )

    assert asked == [("high", "send the report to Ada")]
    assert result.status == StepStatus.completed
