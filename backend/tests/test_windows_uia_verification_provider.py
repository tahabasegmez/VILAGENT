"""Tests for read-only semantic UIA verification."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.models import Condition, ConditionOperator, MonitorRef, Observation, Rect, Size, UIAElementRef
from vilagent.computer_use.windows.verification import WindowsUIAVerificationProvider


def _observation():
    return Observation(
        observation_id="obs-1",
        session_id="session-1",
        monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100), primary=True),
        screen_size=Size(width=100, height=100),
    )


class FakeUIAProvider:
    def __init__(self, results=None, *, error=None):
        self.results = results or []
        self.error = error
        self.queries = []

    async def find(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.results


def test_uia_verification_supports_exists_and_not_exists():
    async def run():
        present = WindowsUIAVerificationProvider(FakeUIAProvider([UIAElementRef(element_id="42.7")]))
        missing = WindowsUIAVerificationProvider(FakeUIAProvider())
        exists = Condition(kind="uia_element", operator=ConditionOperator.exists, selector={"automation_id": "save"})
        not_exists = Condition(kind="uia_element", operator=ConditionOperator.not_exists, selector={"automation_id": "save"})

        present_result = await present.verify([exists], before=_observation(), after=_observation())
        missing_result = await missing.verify([not_exists], before=_observation(), after=_observation())

        assert present_result.succeeded is True
        assert missing_result.succeeded is True

    asyncio.run(run())


def test_uia_verification_fails_closed_for_invalid_operator_selector_and_provider_error():
    async def run():
        provider = WindowsUIAVerificationProvider(FakeUIAProvider(error=RuntimeError("uia unavailable")))
        conditions = [
            Condition(kind="uia_element", operator=ConditionOperator.equals, selector={"automation_id": "save"}),
            Condition(kind="uia_element", operator=ConditionOperator.exists),
            Condition(kind="uia_element", operator=ConditionOperator.exists, selector={"automation_id": "save"}),
        ]

        result = await provider.verify(conditions, before=_observation(), after=_observation())

        assert result.succeeded is False
        assert result.failed_conditions == conditions

    asyncio.run(run())
