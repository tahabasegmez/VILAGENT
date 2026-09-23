"""Autonomous FARA (a branch of the run graph): brief, pick the environment, run until FARA finishes."""

from __future__ import annotations

from fakes import FakeEnv, FakeFara, click, finish, run_graph

from vilagent.agents.common import StepStatus


class BriefModel:
    def __init__(self, reply: str):
        self._reply = reply

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        return type("Msg", (), {"content": self._reply})()


def _run(monkeypatch, reply, script, max_actions=40):
    del monkeypatch  # the brief model is handed to the run graph, not patched into the module
    brief = BriefModel(reply)
    desktop, browser, fara = FakeEnv(), FakeEnv("browser"), FakeFara(script)
    plans = []
    result = run_graph(
        desktop=desktop,
        browser=browser,
        fara=fara,
        prompt="book a table",
        brief_model=lambda thinking=False: brief,
        approach="brief",
        autonomous_max_actions=max_actions,
        on_plan_update=lambda plan, results, current: plans.append(current),
    )
    return result, desktop, browser, fara, plans


def test_browser_task_runs_in_the_browser_until_finish(monkeypatch):
    result, desktop, browser, fara, plans = _run(monkeypatch, '{"environment":"browser","directive":"do the whole task"}', [click(), click(3, 3), finish()])

    assert result.status == StepStatus.completed
    assert len(browser.actions) == 2 and desktop.actions == []
    assert fara.calls[0]["instruction"] == "do the whole task"
    assert fara.calls[0]["autonomous"] is True
    assert result.plan.steps[0].instruction == "do the whole task"
    assert plans == ["brief", None]


def test_native_task_runs_on_the_desktop(monkeypatch):
    result, desktop, browser, _, _ = _run(monkeypatch, '{"environment":"native","directive":"use notepad"}', [click(), finish()])

    assert result.status == StepStatus.completed
    assert len(desktop.actions) == 1 and browser.actions == []


def test_unparseable_brief_falls_back_to_the_raw_prompt(monkeypatch):
    result, _, _, fara, _ = _run(monkeypatch, "sure, I can help", [finish()])

    assert result.plan.steps[0].environment == "native"
    assert fara.calls[0]["instruction"] == "book a table"


def test_repeated_give_up_fails_the_task(monkeypatch):
    result, _, _, _, _ = _run(monkeypatch, '{"environment":"native","directive":"x"}', [finish("failure"), finish("failure")])

    assert result.status == StepStatus.failed
    assert result.steps[0].error_code == "fara_terminate_failure"


def test_spent_budget_without_finish_is_a_failure(monkeypatch):
    result, _, _, _, _ = _run(monkeypatch, '{"environment":"native","directive":"x"}', [click(i, i) for i in range(10)], max_actions=4)

    assert result.status == StepStatus.failed
    assert result.steps[0].error_code == "action_budget_exhausted"
