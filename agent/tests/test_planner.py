"""Planner prompt, plan models and parsing."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from vilagent.agents import planner as planner_module
from vilagent.agents.common import PlannedRisk, PlanStep, RiskLevel
from vilagent.agents.planner import PLANNER_SYSTEM_PROMPT, JsonLLMPlanner, fallback_plan, planner_context


def test_planned_step_requires_explicit_risk_level():
    with pytest.raises(ValidationError):
        PlanStep(step_id="s1", instruction="Open Calculator")
    with pytest.raises(ValidationError):
        PlanStep(step_id="s1", instruction="Open Calculator", risk={})

    step = PlanStep(step_id="s1", instruction="Open Calculator", risk={"level": RiskLevel.low})
    assert step.risk.level == RiskLevel.low
    assert step.requires_vision is True


def test_prompt_asks_for_sub_goal_steps_with_completion_criteria():
    assert "Output ONLY one JSON object" in PLANNER_SYSTEM_PROMPT
    assert "Plan, never execute" in PLANNER_SYSTEM_PROMPT
    assert "MEANINGFUL SUB-GOAL" in PLANNER_SYSTEM_PROMPT
    assert "completion_criteria" in PLANNER_SYSTEM_PROMPT
    assert "its own step" in PLANNER_SYSTEM_PROMPT


def test_prompt_prefers_deterministic_steps_and_localized_english_output():
    assert "Prefer deterministic input steps over visual actions" in PLANNER_SYSTEM_PROMPT
    assert "keyboard, text-entry, UIA, or DOM equivalent" in PLANNER_SYSTEM_PROMPT
    assert "context.windows_ui_language" in PLANNER_SYSTEM_PROMPT
    assert "ENGLISH ONLY" in PLANNER_SYSTEM_PROMPT
    assert "Up to 12 steps" in PLANNER_SYSTEM_PROMPT
    assert "calculator" not in PLANNER_SYSTEM_PROMPT.casefold()


def test_planner_context_includes_windows_ui_language(monkeypatch):
    monkeypatch.setattr(planner_module, "windows_ui_language", lambda: "tr-TR")

    assert planner_context({"text_model": "m"}) == {"text_model": "m", "windows_ui_language": "tr-TR"}


def test_fallback_plan_is_one_vision_step():
    plan = fallback_plan("Perform a desktop operation")

    assert len(plan.steps) == 1
    assert plan.steps[0].requires_vision is True
    assert isinstance(plan.steps[0].risk, PlannedRisk)
    assert plan.steps[0].risk.level == RiskLevel.medium


class _Model:
    def __init__(self, content, reasoning=None):
        self._content = content
        self._reasoning = reasoning

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        extra = {"reasoning_content": self._reasoning} if self._reasoning else {}
        return type("Msg", (), {"content": self._content, "additional_kwargs": extra, "usage_metadata": {"total_tokens": 11}})()


def test_json_planner_parses_plans_and_reports_reasoning(monkeypatch):
    reply = '```json\n{"goal":"g","steps":[{"step_id":"s1","instruction":"open notepad","risk":{"level":"low"},"extra":"ignored"}]}\n```'
    model = _Model(reply, reasoning="Notepad first.")
    thoughts = []
    planner = JsonLLMPlanner(lambda thinking=False: model, on_thinking=thoughts.append)

    plan = asyncio.run(planner.plan("open notepad", context={}))

    assert plan.steps[0].instruction == "open notepad"
    assert thoughts == ["Notepad first."]
    assert (planner.request_count, planner.total_tokens) == (1, 11)


def test_json_planner_falls_back_on_unparseable_reply(monkeypatch):
    model = _Model("I cannot help with that.")

    plan = asyncio.run(JsonLLMPlanner(lambda thinking=False: model).plan("do x", context={}))

    assert plan.steps[0].instruction == "do x"


def test_replan_sends_the_evidence_and_the_screenshot(monkeypatch):
    from vilagent.agents.common import EnvironmentContext, Plan, StepResult, StepStatus

    model = _Model('{"goal": "g", "steps": []}')
    sent = []
    original = model.ainvoke

    async def capture(messages):
        sent.extend(messages)
        return await original(messages)

    model.ainvoke = capture
    blocked = StepResult(step_id="s1", environment=EnvironmentContext.native, requires_vision=True, status=StepStatus.blocked, evidence={"last_thoughts": ["A dialog is covering the menu."]})

    asyncio.run(JsonLLMPlanner(lambda thinking=False: model).replan("task", plan=Plan(goal="g"), completed_steps=[], blocked_step=blocked, context={}, screenshot="data:image/jpeg;base64,AAAA"))

    text, image = sent[-1].content
    assert "A dialog is covering the menu." in text["text"]
    assert image == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
