"""Tests for fail-closed baseline verification."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.models import Condition, MonitorRef, Observation, Rect, Size, VerificationResult
from vilagent.computer_use.verification import ConservativeVerificationProvider, RoutedVerificationProvider


def _observation(observation_id, *, previous=None, diff=None):
    return Observation(
        observation_id=observation_id,
        previous_observation_id=previous,
        session_id="session-1",
        monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=10, height=10)),
        screen_size=Size(width=10, height=10),
        diff_from_previous=diff,
    )


def test_conservative_verification_supports_screen_change_and_fails_unknown():
    async def run():
        provider = ConservativeVerificationProvider()
        before = _observation("obs-1")
        after = _observation("obs-2", previous="obs-1", diff=0.5)

        changed = await provider.verify([Condition(kind="screen_changed")], before=before, after=after)
        unknown = await provider.verify([Condition(kind="window_active")], before=before, after=after)
        empty = await provider.verify([], before=before, after=after)

        assert changed.succeeded is True
        assert unknown.succeeded is False
        assert empty.succeeded is True

    asyncio.run(run())


class FakeVerificationProvider:
    name = "fake-verification"

    def __init__(self, *, fail=False, error=None):
        self.fail = fail
        self.error = error
        self.calls = []

    async def verify(self, conditions, *, before, after):
        self.calls.append(conditions)
        if self.error is not None:
            raise self.error
        return VerificationResult(
            succeeded=not self.fail,
            checked_conditions=len(conditions),
            failed_conditions=conditions if self.fail else [],
        )


def test_routed_verification_groups_known_kinds_and_fails_unknown_or_provider_error():
    async def run():
        shared = FakeVerificationProvider()
        broken = FakeVerificationProvider(error=RuntimeError("secret"))
        provider = RoutedVerificationProvider({"known-a": shared, "known-b": shared, "broken": broken})
        conditions = [Condition(kind="known-a"), Condition(kind="known-b"), Condition(kind="unknown"), Condition(kind="broken")]

        result = await provider.verify(conditions, before=_observation("obs-1"), after=_observation("obs-2"))

        assert result.succeeded is False
        assert shared.calls == [[conditions[0], conditions[1]]]
        assert result.failed_conditions == [conditions[2], conditions[3]]
        assert result.details == {"fake-verification": {"error_code": "verification_provider_error"}}
        assert "secret" not in str(result.model_dump())

    asyncio.run(run())
