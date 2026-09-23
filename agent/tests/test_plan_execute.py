"""Plan-and-execute: step routing, replanning and helpers."""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeEnv, FakeFara, click, config, finish, run_graph

from vilagent.actions import ActionKind, Target, TargetStrategy
from vilagent.agents import vision_loop
from vilagent.agents.common import Plan, PlanStep, RiskLevel, StepStatus
from vilagent.agents.plan_execute import (
    StepExecutor,
    args_for_step,
    normalize_app_name,
    vision_action_limit,
    vision_step_command,
)

_UIA_TARGET = Target(strategy=TargetStrategy.uia, selector={"automation_id": "save"})


def _step(step_id="s1", instruction="do it", **kwargs) -> PlanStep:
    return PlanStep(step_id=step_id, instruction=instruction, risk={"level": RiskLevel.low}, **kwargs)


class FakePlanner:
    def __init__(self, plan: Plan, replan: Plan | None = None):
        self._plan = plan
        self._replan = replan
        self.replan_calls = 0
        self.blocked = []
        self.screenshots = []
        self.sees_images = False
        self.request_count = 1
        self.total_tokens = 5

    async def plan(self, prompt, *, context):
        assert "windows_ui_language" in context
        return self._plan

    async def replan(self, prompt, *, plan, completed_steps, blocked_step, context, screenshot=None):
        self.replan_calls += 1
        self.blocked.append(blocked_step)
        self.screenshots.append(screenshot)
        return self._replan or self._plan


def _orchestrate(steps, *, desktop=None, browser=None, fara=None, replan=None, sees_images=False, **kwargs):
    desktop = desktop or FakeEnv(target=_UIA_TARGET)
    browser = browser or FakeEnv("browser")
    fara = fara or FakeFara([])
    planner = FakePlanner(Plan(goal="goal", steps=steps), replan)
    planner.sees_images = sees_images
    result = run_graph(desktop=desktop, browser=browser, fara=fara, planner=planner, **kwargs)
    return result, desktop, browser, fara, planner


def test_deterministic_hotkey_step_runs_once_without_vision():
    result, desktop, _, fara, _ = _orchestrate([_step(instruction="press ctrl+s", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "ctrl+s"})])

    assert result.status == StepStatus.completed
    assert [a.kind for a in desktop.actions] == [ActionKind.hotkey]
    assert fara.calls == []


def test_named_control_is_resolved_through_ui_automation():
    result, desktop, _, _, _ = _orchestrate([_step(instruction="click Save", requires_vision=False, action_kind=ActionKind.click, target_description="Save")])

    assert result.status == StepStatus.completed
    assert desktop.resolved == ["Save"]
    assert desktop.actions[0].target == _UIA_TARGET


def test_launch_app_name_is_inferred_and_cleaned():
    result, desktop, _, _, _ = _orchestrate([_step(instruction="Launch Microsoft Edge browser and navigate to Gmail", requires_vision=False)])

    assert result.status == StepStatus.completed
    assert desktop.actions[0].kind == ActionKind.launch_app
    assert desktop.actions[0].args["app_name"] == "Microsoft Edge"


def test_type_text_is_deterministic_even_when_marked_vision():
    result, desktop, _, fara, _ = _orchestrate([_step(instruction="type 'hello'", action_kind=ActionKind.type_text)])

    assert result.status == StepStatus.completed
    assert desktop.actions[0].args["text"] == "hello"
    assert fara.calls == []


def test_a_control_that_cannot_be_named_is_looked_at_instead_of_blocking():
    # Two things answer to "Documents", or nothing does: that is a reason to look, not to give up.
    result, desktop, _, fara, planner = _orchestrate(
        [_step(instruction="click Save", requires_vision=False, action_kind=ActionKind.click)],
        desktop=FakeEnv(target=None),
        fara=FakeFara([click(), finish()]),
    )

    assert result.status == StepStatus.completed and planner.replan_calls == 0
    assert desktop.resolved == ["click Save"]  # the lookup was tried first
    assert [a.kind for a in desktop.actions] == [ActionKind.click] and fara.calls  # then the screen


def test_a_step_blocks_only_once_the_screen_has_been_tried_too():
    recovered = Plan(goal="goal", steps=[_step("s2", "press enter", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "enter"})])

    result, _, _, _, planner = _orchestrate(
        [_step(instruction="click Save", requires_vision=False, action_kind=ActionKind.click)],
        desktop=FakeEnv(target=None),
        fara=FakeFara([finish("failure"), finish("failure")]),
        replan=recovered,
    )

    assert planner.replan_calls == 1
    assert result.replan_count == 1
    assert result.status == StepStatus.completed
    assert [s.status for s in result.steps] == [StepStatus.blocked, StepStatus.completed]


def test_replans_are_bounded():
    result, _, _, _, planner = _orchestrate(
        [_step(instruction="click Save", requires_vision=False, action_kind=ActionKind.click)],
        desktop=FakeEnv(target=None),
        fara=FakeFara([finish("failure")] * 6),
        max_replans=1,
    )

    assert planner.replan_calls == 1
    assert result.status == StepStatus.blocked


def test_failed_step_stops_the_run():
    steps = [_step("s1", "press a", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "a"}), _step("s2", "press b", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "b"})]

    result, desktop, _, _, _ = _orchestrate(steps, desktop=FakeEnv(failures=["hotkey_failed"]))

    assert result.status == StepStatus.failed
    assert len(result.steps) == 1
    assert len(desktop.actions) == 1


def test_vision_step_runs_the_fara_loop_on_the_desktop():
    fara = FakeFara([click(), finish()])

    result, desktop, _, _, _ = _orchestrate([_step(instruction="click the icon")], fara=fara)

    assert result.status == StepStatus.completed
    assert result.steps[0].actions == 1
    assert desktop.resolved == []
    assert fara.calls[0]["environment"] == "native"
    assert result.vision_request_count == 2 and result.planner_total_tokens == 5


def test_vision_only_mode_sends_every_step_to_fara():
    fara = FakeFara([finish()])

    result, desktop, _, _, _ = _orchestrate(
        [_step(instruction="click Save", requires_vision=False, action_kind=ActionKind.click)],
        fara=fara,
        execution_mode="vision_only",
    )

    assert result.plan.steps[0].requires_vision is True
    assert desktop.resolved == []
    assert len(fara.calls) == 1


def test_browser_steps_run_in_the_browser():
    fara = FakeFara([click(), finish()])
    steps = [
        _step("b1", "open the browser", environment="browser", action_kind=ActionKind.launch_app, args={"url": "https://example.com"}),
        _step("b2", "search cats", environment="browser"),
    ]

    result, desktop, browser, _, _ = _orchestrate(steps, fara=fara)

    assert result.status == StepStatus.completed
    assert desktop.actions == []
    assert browser.actions[0].args == {"action": "visit_url", "url": "https://example.com"}
    assert fara.calls[0]["environment"] == "browser"
    assert "about:blank" in fara.calls[0]["instruction"]


def test_launching_the_browser_without_url_is_a_no_op():
    result, _, browser, _, _ = _orchestrate([_step("b1", "open the browser", environment="browser", action_kind=ActionKind.launch_app)])

    assert result.status == StepStatus.completed
    assert browser.actions == []


def test_unreachable_vision_model_fails_the_step():
    async def broken(config):
        raise ConnectionError("ngrok down")

    executor = StepExecutor(config(), desktop=FakeEnv(), browser=FakeEnv("browser"), fara_factory=broken)
    result = asyncio.run(executor.execute(_step()))

    assert result.status == StepStatus.failed
    assert result.error_code == "vision_model_unreachable"


def test_plan_is_trimmed_to_max_steps():
    steps = [_step(f"s{i}", "press a", requires_vision=False, action_kind=ActionKind.hotkey) for i in range(5)]

    result, _, _, _, _ = _orchestrate(steps, max_steps=2)

    assert len(result.plan.steps) == 2


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("Launch Microsoft Edge browser and navigate to Gmail", "Microsoft Edge"),
        ("open notepad", "notepad"),
        ("Start Google Chrome and search for cats", "Google Chrome"),
        ("open the edge browser", "edge"),
        ("open Microsoft To Do", "Microsoft To Do"),
        ("msedge", "msedge"),
    ],
)
def test_normalize_app_name(instruction, expected):
    assert normalize_app_name(instruction) == expected


def test_args_for_step_strips_a_sentence_app_name():
    step = _step(action_kind=ActionKind.launch_app, args={"app_name": "Microsoft Edge browser and navigate to Gmail"})

    assert args_for_step(step, ActionKind.launch_app)["app_name"] == "Microsoft Edge"


def test_vision_step_command_keeps_the_model_on_this_step():
    command = vision_step_command(_step(instruction="Click Date modified once."), max_actions=4, context="CURRENT PAGE: x")

    assert "CURRENT STEP: Click Date modified once." in command
    assert "CURRENT PAGE: x" in command
    assert "finish_step success" in command
    assert "pursue a later step / different user goal" in command
    assert "You have at most 4 action(s)" in command


def test_vision_action_limit_treats_the_planner_budget_as_a_hint():
    assert vision_action_limit(_step(max_actions=2)) == 8
    assert vision_action_limit(_step(max_actions=10)) == 10
    assert vision_action_limit(_step(max_actions=16)) == 12
    assert vision_action_limit(_step(max_actions=2, environment="browser")) == 12
    assert vision_action_limit(_step(max_actions=16, environment="browser")) == 16


def _repeating(n: int = 20) -> FakeFara:
    return FakeFara([click(thought="Clicking Save again.")] * n)


def test_step_that_spent_its_budget_is_verified_before_counting_as_done():
    fara = FakeFara([click(i, i) for i in range(12)], verdicts=[True])

    result, _, _, _, planner = _orchestrate([_step(instruction="rename the file", completion_criteria="The file is named a.txt.")], fara=fara)

    assert result.status == StepStatus.completed
    assert (result.steps[0].verified_by, fara.verifications) == ("fara_verify", ["The file is named a.txt."])
    assert planner.replan_calls == 0


def test_failed_verification_blocks_the_step_and_replans_with_evidence():
    fara = FakeFara([click(i, i, thought=f"note {i}") for i in range(8)] + [finish()], verdicts=[False])
    recovered = Plan(goal="goal", steps=[_step("s2", "try again")])

    result, _, _, _, planner = _orchestrate([_step(instruction="rename the file")], fara=fara, replan=recovered)

    assert [s.status for s in result.steps] == [StepStatus.blocked, StepStatus.completed]
    blocked = planner.blocked[0]
    assert blocked.error_code == "unverified_completion"
    assert blocked.evidence["last_thoughts"] == ["note 5", "note 6", "note 7"]


def test_stuck_fara_blocks_and_replans_instead_of_failing_the_run():
    fara = FakeFara([click()] + [finish("failure")] * 2)
    recovered = Plan(goal="goal", steps=[_step("s2", "another way")])

    result, _, _, _, planner = _orchestrate([_step(instruction="click the menu")], fara=fara, replan=recovered, execution_mode="vision_only")

    assert planner.replan_calls == 1
    assert planner.blocked[0].error_code == "fara_terminate_failure"
    assert result.status == StepStatus.completed


def test_step_without_verification_is_accepted_on_budget():
    fara = FakeFara([click(i, i) for i in range(12)])

    result, *_ = _orchestrate([_step(instruction="scroll a bit", requires_verification=False)], fara=fara)

    assert (result.status, result.steps[0].verified_by, fara.verifications) == (StepStatus.completed, "none", [])


def test_unreachable_model_still_fails_without_replanning(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(vision_loop.asyncio, "sleep", no_wait)  # skip the retry pauses
    fara = FakeFara([RuntimeError("503")] * 20)

    result, _, _, _, planner = _orchestrate([_step(instruction="click it")], fara=fara)

    assert result.status == StepStatus.failed and planner.replan_calls == 0


def test_vision_capable_planner_sees_the_blocked_screen():
    def replan_screenshots(sees_images: bool) -> list:
        *_, planner = _orchestrate([_step(instruction="click the menu")], fara=FakeFara([finish("failure")] * 2), replan=Plan(goal="goal", steps=[]), sees_images=sees_images)
        return planner.screenshots

    assert replan_screenshots(True)[0].startswith("data:image/")
    assert replan_screenshots(False) == [None]


class FakeChecker:
    def __init__(self, passed: bool):
        self.passed = passed
        self.criteria: list[str] = []

    async def verify(self, criterion, image_base64, image_media_type="image/png"):
        from vilagent.vision.fara import Verification

        self.criteria.append(criterion)
        return Verification(self.passed, "The supervisor looked.")


def test_step_checks_can_be_turned_off():
    fara = FakeFara([click(i, i) for i in range(12)], verdicts=[False])

    result, *_ = _orchestrate([_step(instruction="rename the file")], fara=fara, verification="none")

    assert (result.status, result.steps[0].verified_by, fara.verifications) == (StepStatus.completed, "none", [])
    assert "step checks are off" in result.steps[0].summary


def test_the_supervisor_model_can_check_instead_of_fara():
    fara, checker = FakeFara([click(i, i) for i in range(12)]), FakeChecker(passed=True)

    result, *_ = _orchestrate([_step(instruction="rename the file", completion_criteria="Named a.txt.")], fara=fara, verification="supervisor", model_verifier=checker)

    assert (result.status, result.steps[0].verified_by) == (StepStatus.completed, "model_verify")
    assert (checker.criteria, fara.verifications) == (["Named a.txt."], [])


def test_a_failed_supervisor_check_blocks_the_step():
    recovered = Plan(goal="goal", steps=[_step("s2", "try again")])

    result, _, _, _, planner = _orchestrate([_step(instruction="rename the file")], fara=FakeFara([click(i, i) for i in range(8)] + [finish()]), replan=recovered, verification="supervisor", model_verifier=FakeChecker(passed=False))

    assert planner.blocked[0].error_code == "unverified_completion"
    assert result.status == StepStatus.completed


def test_supervisor_check_without_a_model_falls_back_to_fara():
    fara = FakeFara([click(i, i) for i in range(12)], verdicts=[True])

    result, *_ = _orchestrate([_step(instruction="rename the file")], fara=fara, verification="supervisor", model_verifier=None)

    assert (result.steps[0].verified_by, len(fara.verifications)) == ("fara_verify", 1)
