"""Emergency stop, action gate and action log."""

from __future__ import annotations

import asyncio
import json

import pytest
from fakes import FakeEnv, click

from vilagent.control import EMERGENCY_STOP, ActionLog, Control, RunStopped


def test_actions_are_logged(tmp_path):
    log_path = tmp_path / "actions.jsonl"
    env = FakeEnv(control=Control(log=ActionLog(log_path)), failures=[None, "missed"])

    assert asyncio.run(env.act(click())).ok
    assert not asyncio.run(env.act(click())).ok

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [(r["environment"], r["kind"], r["ok"], r["error"]) for r in records] == [("native", "click", True, None), ("native", "click", False, "missed")]


def test_emergency_stop_blocks_actions_until_reset():
    control = Control()
    env = FakeEnv(control=control)

    asyncio.run(control.engage("operator"))
    blocked = asyncio.run(env.act(click()))
    control.reset()
    allowed = asyncio.run(env.act(click()))

    assert blocked.error == EMERGENCY_STOP
    assert allowed.ok
    assert len(env.actions) == 1


def test_gate_can_block_an_action():
    class DenyClicks:
        async def check(self, action, environment):
            return "clicks need approval" if action.kind == "click" else None

    env = FakeEnv(control=Control(gate=DenyClicks()))

    outcome = asyncio.run(env.act(click()))

    assert outcome.error == "action_blocked: clicks need approval"
    assert env.actions == []


def test_emergency_stop_cancels_running_tasks_and_runs_cleanup():
    control = Control()
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    control.on_stop(cleanup)

    async def scenario():
        async def forever():
            await asyncio.sleep(3600)

        run = asyncio.create_task(control.run(forever()))
        await asyncio.sleep(0)
        await control.engage("operator")
        with pytest.raises(RunStopped):
            await run

    asyncio.run(scenario())

    assert control.stopped and control.stop_reason == "operator"
    assert cleaned == [True]


def test_cancelling_the_caller_is_not_reported_as_a_stop():
    control = Control()

    async def scenario():
        caller = asyncio.create_task(control.run(asyncio.sleep(3600)))
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

    asyncio.run(scenario())
