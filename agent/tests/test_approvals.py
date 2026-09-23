"""Operator approvals: the policy, the broker, the graph gate and the action gate."""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeEnv, FakeFara, click, config, finish, run_graph

from vilagent.actions import Action, ActionKind
from vilagent.agents.common import Plan, PlanStep, StepStatus
from vilagent.approvals.broker import Answer, Approvals
from vilagent.approvals.gate import ApprovalGate
from vilagent.approvals.policy import matched_rule, needs_approval
from vilagent.config.computer_use_config import ComputerUseApprovalConfig
from vilagent.control import Control
from vilagent.runs.budget import BudgetMeter
from vilagent.runs.events import EventBus
from vilagent.runs.manager import RunManager
from vilagent.runs.records import RunRecords
from vilagent.runs.session import RunSession
from vilagent.server.activity import new_activity

RULES = ComputerUseApprovalConfig(allowed_domains=["example.com"])


# --- policy ------------------------------------------------------------------


@pytest.mark.parametrize(("level", "threshold", "asks"), [
    ("high", "high", True), ("critical", "high", True), ("medium", "high", False),
    ("medium", "medium", True), ("low", "medium", False), ("high", "critical", False), ("critical", "off", False),
])
def test_threshold(level, threshold, asks):
    assert needs_approval(level, threshold) is asks


def test_keywords_flag_committing_actions_only():
    assert matched_rule(click(thought="I will click Send to deliver the email."), RULES) == 'The next action looks like "send".'
    assert matched_rule(click(thought="Clicking the sender field."), RULES) is None  # whole words only
    typing = Action(kind=ActionKind.type_text, args={"text": "x"}, thought="Typing the text to send.")
    assert matched_rule(typing, RULES) is None
    enter = Action(kind=ActionKind.hotkey, args={"keys": ["Enter"]}, thought="Press Enter to place order.")
    assert matched_rule(enter, RULES) == 'The next action looks like "place order".'


def test_hotkey_and_site_rules():
    assert matched_rule(Action(kind=ActionKind.hotkey, args={"keys": ["Alt", "F4"]}), RULES) == "Presses alt+f4."
    assert matched_rule(Action(kind=ActionKind.hotkey, args={"keys": "CTRL+S"}), RULES) is None
    visit = lambda url: Action(kind=ActionKind.browser_action, args={"action": "visit_url", "url": url})  # noqa: E731
    assert matched_rule(visit("https://mail.example.com/inbox"), RULES) is None
    assert "evil.com" in matched_rule(visit("evil.com/login"), RULES)
    assert matched_rule(visit("https://anything.org"), ComputerUseApprovalConfig()) is None  # no allow-list, no rule


# --- broker ------------------------------------------------------------------


def test_answer_timeout_and_paused_budget():
    now = [0.0]
    meter = BudgetMeter({}, duration_seconds=60, clock=lambda: now[0])
    bus = EventBus()
    approvals = Approvals(bus, timeout_seconds=10, budget=meter)

    async def scenario():
        asking = asyncio.create_task(approvals.ask(kind="step", title="Send the email", reasons=["Messages others"], level="high"))
        await asyncio.sleep(0)
        (approval_id,) = approvals.pending
        now[0] = 50.0  # the operator takes a while: not counted
        assert meter.active_seconds() == 0.0
        assert approvals.answer(approval_id, approve=True, scope="step")
        answered = await asking
        timed_out = await Approvals(bus, timeout_seconds=0.01).ask(kind="action", title="x", reasons=[])
        return answered, timed_out

    answered, timed_out = asyncio.run(scenario())

    assert answered == Answer(True, "step", "")
    assert (approvals.asked, approvals.declined) == (1, 0)
    assert not timed_out.approve and "No answer within" in timed_out.reason
    types = [event.type for event in list(bus._events)]
    assert types == ["approval.requested", "approval.resolved", "approval.requested", "approval.resolved"]


# --- graph gate (risky steps) ------------------------------------------------


def _risky(step_id: str, level: str) -> PlanStep:
    return PlanStep(step_id=step_id, instruction=f"press enter ({level})", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "enter"}, risk={"level": level, "reasons": ["Sends it"]})


class Planner:
    def __init__(self, steps):
        self.plan_ = Plan(goal="g", steps=steps)
        self.replan_calls = 0

    async def plan(self, prompt, *, context):
        return self.plan_

    async def replan(self, prompt, **kwargs):
        self.replan_calls += 1
        return self.plan_


def _gated(answer: dict, threshold: str = "high"):
    asked = []

    async def ask(request):
        asked.append(request)
        return answer

    desktop = FakeEnv()
    planner = Planner([_risky("s1", "low"), _risky("s2", "high"), _risky("s3", "low")])
    result = run_graph(desktop=desktop, browser=FakeEnv("browser"), fara=FakeFara([]), planner=planner, approval_threshold=threshold, ask=ask)
    return result, asked, desktop, planner


def test_a_high_risk_step_waits_for_approval_and_then_runs():
    result, asked, desktop, _ = _gated({"approve": True})

    assert result.status == StepStatus.completed
    assert [(r["step_id"], r["level"], r["reasons"]) for r in asked] == [("s2", "high", ["Sends it"])]
    assert len(desktop.actions) == 3


def test_a_declined_step_ends_the_run_without_replanning():
    result, _, desktop, planner = _gated({"approve": False, "reason": "No answer within 120 s."})

    assert result.status == StepStatus.denied
    assert [(s.step_id, s.status) for s in result.steps] == [("s1", StepStatus.completed), ("s2", StepStatus.denied)]
    assert result.steps[-1].summary == "No answer within 120 s."
    assert len(desktop.actions) == 1 and planner.replan_calls == 0


def test_threshold_off_never_asks():
    result, asked, _, _ = _gated({"approve": False}, threshold="off")

    assert result.status == StepStatus.completed and asked == []


# --- action gate (rules inside the FARA loop) ---------------------------------


def _session(approvals: Approvals) -> RunSession:
    activity = new_activity("t", "r1", "task", "model", config())
    return RunSession(run_id="r1", thread_id="t", prompt="task", approach="plan_execute", execution_mode="hybrid", activity=activity, approvals=approvals)


def test_a_flagged_action_is_asked_and_a_step_grant_is_remembered_for_that_step(tmp_path):
    approvals = Approvals(EventBus(), timeout_seconds=10)
    asked = []

    async def ask(**request):
        asked.append((approvals.current_step, request["reasons"][0]))
        return Answer(True, "step")

    approvals.ask = ask
    control = Control(gate=ApprovalGate(RULES))
    env = FakeEnv(control=control)
    send = click(thought="Click Send.")

    async def act_in(step_id: str, times: int):
        approvals.current_step = step_id
        for _ in range(times):
            assert (await env.act(send)).ok

    with RunManager(RunRecords(tmp_path)).open(_session(approvals)):
        asyncio.run(act_in("s1", 2))
        asyncio.run(act_in("s2", 1))

    assert asked == [("s1", 'The next action looks like "send".'), ("s2", 'The next action looks like "send".')]
    assert len(env.actions) == 3


def test_a_declined_action_ends_the_step_as_denied(tmp_path):
    approvals = Approvals(EventBus(), timeout_seconds=10)

    async def decline(**request):
        return Answer(False, reason="The operator declined.")

    approvals.ask = decline
    control = Control(gate=ApprovalGate(RULES))
    desktop = FakeEnv(control=control)
    fara = FakeFara([click(thought="Open the menu."), click(3, 3, thought="Click Delete.")] + [finish()])
    step = PlanStep(step_id="s1", instruction="clean up the folder", risk={"level": "low"})

    with RunManager(RunRecords(tmp_path)).open(_session(approvals)):
        result = run_graph(desktop=desktop, browser=FakeEnv("browser"), fara=fara, planner=Planner([step]))

    assert result.status == StepStatus.denied
    assert (result.steps[0].status, result.steps[0].error_code) == (StepStatus.denied, "denied")
    assert len(desktop.actions) == 1  # the delete click never happened
