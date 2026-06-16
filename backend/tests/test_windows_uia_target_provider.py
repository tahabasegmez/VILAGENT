"""Tests for safe semantic UIA target resolution."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.models import MonitorRef, Observation, Rect, Size, TargetQuery, TargetStrategy, UIAElementRef
from vilagent.computer_use.windows.target import WindowsUIATargetProvider


def _observation():
    return Observation(
        observation_id="obs-1",
        session_id="session-1",
        monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100), primary=True),
        screen_size=Size(width=100, height=100),
    )


class FakeUIAProvider:
    def __init__(self, results):
        self.results = results
        self.queries = []

    async def find(self, query):
        self.queries.append(query)
        return self.results


def _element(**updates):
    values = {
        "element_id": "42.7",
        "name": "Save",
        "automation_id": "save-button",
        "control_type": "Button",
        "process_id": 42,
        "bounds": Rect(x=10, y=20, width=30, height=15),
        "enabled": True,
        "visible": True,
    }
    values.update(updates)
    return UIAElementRef(**values)


def test_uia_target_provider_returns_stable_selector_for_unique_match():
    async def run():
        uia = FakeUIAProvider([_element()])
        provider = WindowsUIATargetProvider(uia)

        target = await provider.resolve(
            TargetQuery(description="Save button", selector_hints={"automation_id": "save-button"}),
            observation=_observation(),
        )

        assert target is not None
        assert target.strategy == TargetStrategy.uia
        assert target.observation_id == "obs-1"
        assert target.selector == {
            "element_id": "42.7",
            "process_id": 42,
            "automation_id": "save-button",
            "control_type": "Button",
        }
        assert target.confidence == 0.99
        assert uia.queries[0].max_results == 2

    asyncio.run(run())


def test_uia_target_provider_fails_closed_for_ambiguous_or_unusable_match():
    async def run():
        ambiguous = WindowsUIATargetProvider(FakeUIAProvider([_element(), _element(element_id="42.8")]))
        hidden = WindowsUIATargetProvider(FakeUIAProvider([_element(visible=False)]))

        assert await ambiguous.resolve(TargetQuery(description="Save"), observation=_observation()) is None
        assert await hidden.resolve(TargetQuery(description="Save"), observation=_observation()) is None

    asyncio.run(run())


def test_uia_target_provider_ignores_unknown_hints_and_falls_back_to_description():
    async def run():
        uia = FakeUIAProvider([])
        provider = WindowsUIATargetProvider(uia)

        await provider.resolve(TargetQuery(description="Save", selector_hints={"raw_xpath": "secret"}), observation=_observation())

        assert uia.queries[0].name == "Save"
        assert not hasattr(uia.queries[0], "raw_xpath")

    asyncio.run(run())
