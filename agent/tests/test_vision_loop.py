"""The shared observe → FARA → act loop."""

from __future__ import annotations

import asyncio

from fakes import FakeEnv, FakeFara, click, config, finish

from vilagent.actions import Action, ActionKind
from vilagent.agents.common import StepStatus
from vilagent.agents.vision_loop import NUDGE, PUSH_BACK, LoopLimits, run_vision_loop
from vilagent.control import EMERGENCY_STOP, Control
from vilagent.vision.fara import UnusableReply


class FakeSupervisor:
    max_calls = 2

    def __init__(self, advice="Close the popup first."):
        self.advice = advice
        self.calls = 0

    async def advise(self, **kwargs):
        self.calls += 1
        return self.advice


def _run(env, fara, limits=None, supervisor=None, events=None):
    return asyncio.run(
        run_vision_loop(
            env=env,
            fara=fara,
            instruction=lambda: "do it",
            limits=limits or LoopLimits(max_actions=6),
            config=config(),
            goal="do it",
            done_when="done",
            role="vision",
            supervisor=supervisor,
            on_activity=(lambda role, event, thought: events.append((role, event, thought))) if events is not None else None,
        )
    )


def _texts(fara):
    return [str(message.get("content")) for message in fara.calls[-1]["chat_history"]]


def test_multi_action_step_ends_on_finish():
    env, fara = FakeEnv(), FakeFara([click(1, 1), click(2, 2), finish()])

    result = _run(env, fara)

    assert result.status == StepStatus.completed
    assert result.actions == 2
    assert len(env.actions) == 2


def test_thoughts_are_reported_live():
    events = []
    _run(FakeEnv(), FakeFara([click(thought="Clicking Save"), finish()]), events=events)

    assert ("vision", "Working (0 actions done)…", "Clicking Save") in events


def test_first_give_up_is_pushed_back():
    env, fara = FakeEnv(), FakeFara([finish("failure"), click(), finish()])

    result = _run(env, fara)

    assert result.status == StepStatus.completed
    assert any(PUSH_BACK in text for text in _texts(fara))


def test_second_give_up_fails():
    result = _run(FakeEnv(), FakeFara([finish("failure"), finish("failure")]))

    assert result.status == StepStatus.failed
    assert result.error_code == "fara_terminate_failure"


def test_repeated_action_gets_nudged_then_ends_undecided():
    fara = FakeFara([click(thought=f"note {i}") for i in range(12)])

    result = _run(FakeEnv(), fara, LoopLimits(max_actions=12, max_nudges=1))

    assert any(NUDGE in text for text in _texts(fara))
    # Earlier clicks succeeded, so the step may be done: the loop reports it, the executor decides.
    assert (result.status, result.ended_by, result.succeeded) == (StepStatus.failed, "repeat", True)
    assert result.thoughts == ("note 3", "note 4", "note 5")


def test_repeated_action_without_success_fails():
    result = _run(FakeEnv(failures=["missed"] * 20), FakeFara([click()] * 12), LoopLimits(max_actions=12, max_nudges=0))

    assert result.status == StepStatus.failed
    assert result.error_code == "no_progress_repeated_action"


def test_supervisor_is_consulted_before_the_generic_nudge():
    supervisor = FakeSupervisor()
    fara = FakeFara([click()] * 3 + [finish()])

    result = _run(FakeEnv(), fara, supervisor=supervisor)

    assert result.status == StepStatus.completed
    assert supervisor.calls == 1
    assert any("Close the popup first." in text for text in _texts(fara))
    assert not any(NUDGE in text for text in _texts(fara))


def test_wait_and_mouse_move_are_not_executed():
    env = FakeEnv()
    wait = Action(kind=ActionKind.wait, args={"time": 0})
    move = Action(kind=ActionKind.mouse_move)

    result = _run(env, FakeFara([wait, move, finish()]))

    assert result.status == StepStatus.completed
    assert env.actions == []


def test_failed_action_is_reported_back_for_a_retry():
    env, fara = FakeEnv(failures=["missed"]), FakeFara([click(1, 1), click(2, 2), finish()])

    result = _run(env, fara)

    assert result.status == StepStatus.completed
    assert '"retry"' in _texts(fara)[-1] or any('"retry"' in text for text in _texts(fara))


def test_click_without_visible_change_is_reported():
    fara = FakeFara([click(1, 1), click(2, 2), finish()])

    _run(FakeEnv(static_screen=True), fara)

    assert any("no_visible_change" in text for text in _texts(fara))


def test_spent_budget_is_reported_not_decided():
    result = _run(FakeEnv(), FakeFara([click(i, i) for i in range(5)]), LoopLimits(max_actions=3))

    assert (result.status, result.ended_by, result.succeeded) == (StepStatus.failed, "budget", True)
    assert result.error_code == "action_budget_exhausted"


def test_only_fara_finishing_completes_the_loop():
    assert _run(FakeEnv(), FakeFara([click(), finish()])).ended_by == "finish_success"
    assert _run(FakeEnv(), FakeFara([finish("failure"), finish("failure")])).ended_by == "finish_failure"


def test_model_errors_are_retried_then_fail():
    recovered = _run(FakeEnv(), FakeFara([RuntimeError("503"), finish()]))
    broken = _run(FakeEnv(), FakeFara([RuntimeError("503")] * 3), LoopLimits(max_actions=6, max_model_errors=2))

    assert recovered.status == StepStatus.completed
    assert broken.status == StepStatus.failed
    assert broken.error_code == "vision_action_failed"


def test_disabled_model_fails_fast():
    class Disabled(FakeFara):
        async def get_next_action(self, **kwargs):
            return None, None

    result = _run(FakeEnv(), Disabled([]))

    assert result.error_code == "fara_disabled"


def test_emergency_stop_ends_the_loop():
    control = Control()
    asyncio.run(control.engage("test"))

    result = _run(FakeEnv(control=control), FakeFara([click()]))

    assert result.status == StepStatus.failed
    assert result.error_code == EMERGENCY_STOP


def test_downscaled_coordinates_are_mapped_back(monkeypatch):
    env = FakeEnv()
    monkeypatch.setattr("vilagent.agents.vision_loop.encode_image_for_vision", lambda png, **kw: ("", "image/jpeg", 2.0))

    _run(env, FakeFara([click(10, 20), finish()]))

    assert env.actions[0].point == (20, 40)


def test_an_unusable_reply_is_retried_with_the_correction_in_the_history():
    correction = {"role": "user", "content": "<supervisor>\nThat reply could not be used: unknown action.\n</supervisor>"}
    fara = FakeFara([UnusableReply("FARA sent an unknown action: 'type_text'", [correction]), finish()])

    result = asyncio.run(
        run_vision_loop(
            env=FakeEnv(), fara=fara, instruction=lambda: "do it", limits=LoopLimits(max_actions=4), config=config(), goal="g", done_when="d", role="vision"
        )
    )

    assert result.status == StepStatus.completed
    # The retry carries the correction, so FARA is not asked the same question again.
    assert fara.calls[-1]["chat_history"] == [correction]
