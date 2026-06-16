"""Tests for cost-aware, fail-closed target resolution."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.models import (
    MonitorRef,
    Observation,
    Rect,
    Size,
    TargetQuery,
    TargetRef,
    TargetStrategy,
)
from vilagent.computer_use.target_resolver import TargetResolver


def _observation():
    return Observation(
        observation_id="obs-1",
        session_id="session-1",
        monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100), primary=True),
        screen_size=Size(width=100, height=100),
    )


class FakeProvider:
    def __init__(self, name, strategy, result=None, *, error=None):
        self.name = name
        self.strategy = strategy
        self.result = result
        self.error = error
        self.calls = 0

    async def resolve(self, query, *, observation):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _target(strategy, *, confidence=1, observation_id="obs-1"):
    return TargetRef(strategy=strategy, selector={"name": "Save"}, confidence=confidence, observation_id=observation_id)


def test_resolver_prefers_cheaper_semantic_provider_and_skips_vision():
    async def run():
        app = FakeProvider("app", TargetStrategy.app)
        uia = FakeProvider("uia", TargetStrategy.uia, _target(TargetStrategy.uia))
        vision = FakeProvider("vision", TargetStrategy.vision, _target(TargetStrategy.vision))

        result = await TargetResolver([vision, uia, app]).resolve(TargetQuery(description="Save button"), observation=_observation())

        assert result.target is not None and result.target.strategy == TargetStrategy.uia
        assert [attempt.provider_name for attempt in result.attempts] == ["app", "uia"]
        assert vision.calls == 0

    asyncio.run(run())


def test_resolver_rejects_stale_low_confidence_and_strategy_mismatch_candidates():
    async def run():
        providers = [
            FakeProvider("wrong-strategy", TargetStrategy.app, _target(TargetStrategy.uia)),
            FakeProvider("stale", TargetStrategy.browser, _target(TargetStrategy.browser, observation_id="obs-old")),
            FakeProvider("low-confidence", TargetStrategy.uia, _target(TargetStrategy.uia, confidence=0.5)),
            FakeProvider("vision", TargetStrategy.vision, _target(TargetStrategy.vision, confidence=0.9)),
        ]

        result = await TargetResolver(providers).resolve(TargetQuery(description="Save button", minimum_confidence=0.8), observation=_observation())

        assert result.target is not None and result.target.strategy == TargetStrategy.vision
        assert [attempt.error_code for attempt in result.attempts[:-1]] == [
            "target_strategy_mismatch",
            "stale_target",
            "target_confidence_below_threshold",
        ]

    asyncio.run(run())


def test_coordinate_is_disabled_by_default_and_provider_errors_are_sanitized():
    async def run():
        app = FakeProvider("app", TargetStrategy.app, error=RuntimeError("secret provider details"))
        coordinate = FakeProvider("coordinate", TargetStrategy.coordinate, _target(TargetStrategy.coordinate))

        result = await TargetResolver([app, coordinate]).resolve(TargetQuery(description="target"), observation=_observation())

        assert result.target is None
        assert coordinate.calls == 0
        assert result.attempts[0].error_code == "target_provider_error"
        assert "secret" not in str(result.model_dump())

    asyncio.run(run())


def test_coordinate_can_be_explicitly_enabled():
    async def run():
        coordinate = FakeProvider("coordinate", TargetStrategy.coordinate, _target(TargetStrategy.coordinate))
        query = TargetQuery(description="target", allowed_strategies=[TargetStrategy.coordinate])

        result = await TargetResolver([coordinate]).resolve(query, observation=_observation())

        assert result.target is not None and result.target.strategy == TargetStrategy.coordinate

    asyncio.run(run())
