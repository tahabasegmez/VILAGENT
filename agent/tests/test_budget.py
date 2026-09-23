"""Per-run budgets: the meter, charging at the choke points, and the duration limit."""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeEnv, FakeFara, click, config

from vilagent.agents.vision_loop import LoopLimits, run_vision_loop
from vilagent.config.computer_use_config import ComputerUseBudgetConfig
from vilagent.runs.budget import BudgetExhausted, BudgetMeter, enforce_duration
from vilagent.runs.manager import RunManager
from vilagent.runs.records import RunRecords
from vilagent.runs.session import RunSession, current_session
from vilagent.server.activity import new_activity
from vilagent.vision.fara import FaraVisionActionProvider


def run_session(**limits: int) -> RunSession:
    activity = new_activity("t", "r1", "task", "model", config())
    meter = BudgetMeter(limits, duration_seconds=60)
    return RunSession(run_id="r1", thread_id="t", prompt="task", approach="plan_execute", execution_mode="hybrid", activity=activity, budget=meter)


def test_meter_raises_once_the_limit_is_used():
    meter = BudgetMeter({"vision": 2}, duration_seconds=60)

    meter.charge("vision")
    meter.charge("vision")
    meter.charge("planner")  # not limited

    with pytest.raises(BudgetExhausted) as spent:
        meter.charge("vision")
    assert (spent.value.code, str(spent.value)) == ("budget_exhausted:vision", "The run used all 2 vision-model calls.")
    assert meter.used == {"vision": 2}


def test_paused_time_is_not_counted():
    now = [100.0]
    meter = BudgetMeter({}, duration_seconds=60, clock=lambda: now[0])

    now[0] = 110.0
    meter.pause()
    now[0] = 500.0
    assert meter.active_seconds() == 10.0
    meter.resume()
    now[0] = 505.0

    assert meter.active_seconds() == 15.0


def test_default_budgets_fit_a_full_plan():
    budgets = ComputerUseBudgetConfig()

    # plan + 2 replans + brief headroom; one 16-action browser step alone exceeded the old vision default.
    assert budgets.planner_calls >= 3 and budgets.vision_calls >= 16 * 3
    assert BudgetMeter.from_config(budgets).limits == {"planner": 6, "vision": 200, "supervisor": 10, "action": 150}


def test_actions_are_charged_in_control_perform(tmp_path):
    env = FakeEnv()
    session = run_session(action=1)

    async def act_twice():
        await env.act(click())
        await env.act(click())

    with RunManager(RunRecords(tmp_path)).open(session):
        with pytest.raises(BudgetExhausted):
            asyncio.run(act_twice())

    assert len(env.actions) == 1
    assert current_session() is None


def test_nothing_is_charged_outside_a_run():
    env = FakeEnv()

    asyncio.run(env.act(click()))

    assert len(env.actions) == 1


def _never_answers():
    class _Model:
        async def ainvoke(self, messages):
            raise AssertionError("the budget is charged before the model is asked")

    return _Model()


def test_vision_calls_are_charged_before_the_request(tmp_path):
    fara = FaraVisionActionProvider(_never_answers(), "microsoft/Fara-7B")

    with RunManager(RunRecords(tmp_path)).open(run_session(vision=0)):
        with pytest.raises(BudgetExhausted):
            asyncio.run(fara.get_next_action(instruction="x", image_base64="", chat_history=[]))


def test_the_vision_loop_does_not_retry_a_spent_budget():
    fara = FakeFara([BudgetExhausted("vision", 5)])

    with pytest.raises(BudgetExhausted):
        asyncio.run(
            run_vision_loop(env=FakeEnv(), fara=fara, instruction=lambda: "x", limits=LoopLimits(max_actions=4), config=config(), goal="g", done_when="d", role="vision")
        )

    assert len(fara.calls) == 1


def test_duration_limit_cancels_the_run():
    meter = BudgetMeter({}, duration_seconds=0.05)  # the run gets to start, then is cut off
    cancelled = []

    async def forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    with pytest.raises(BudgetExhausted) as spent:
        asyncio.run(enforce_duration(forever(), meter))

    assert spent.value.code == "budget_exhausted:duration" and cancelled == [True]


def test_duration_limit_returns_the_result_in_time():
    async def quick():
        return "done"

    assert asyncio.run(enforce_duration(quick(), BudgetMeter({}, duration_seconds=60))) == "done"

