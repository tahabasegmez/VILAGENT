"""Tests for local desktop-session lifecycle management."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.computer_use.models import MonitorRef, Observation, ProviderHealthStatus, Rect, Size
from vilagent.computer_use.session import (
    DesktopSessionNotFoundError,
    DesktopSessionService,
    DesktopSessionStoppedError,
    LatestObservationUnavailableError,
    ObservationSequenceError,
)


def _observation(session_id: str, observation_id: str, previous=None) -> Observation:
    return Observation(
        observation_id=observation_id,
        previous_observation_id=previous.observation_id if previous else None,
        session_id=session_id,
        monitor=MonitorRef(monitor_id="primary", primary=True, bounds=Rect(x=0, y=0, width=100, height=100)),
        screen_size=Size(width=100, height=100),
    )


class FakeObservationProvider:
    name = "fake-screen"

    def __init__(self, store, *, error: Exception | None = None):
        self.store = store
        self.error = error
        self.calls = 0

    async def observe(self, session_id, *, previous=None):
        if self.error:
            raise self.error
        self.calls += 1
        observation = _observation(session_id, f"obs-{self.calls}", previous)
        await self.store.save(observation)
        return observation


def test_session_lifecycle_tracks_latest_observation_and_releases_lease():
    async def run():
        service = DesktopSessionService(observation_provider_factory=FakeObservationProvider)
        created = await service.create(session_id="session-1")
        lease = await service.get_desktop_lease("session-1")
        await lease.acquire("run-1")

        first = await service.observe("session-1")
        second = await service.observe("session-1")
        stopped = await service.stop("session-1")

        assert created.provider_name == "fake-screen"
        assert second.previous_observation_id == first.observation_id
        assert stopped.latest_observation_id == second.observation_id
        assert stopped.provider_health == ProviderHealthStatus.stopped
        assert (await lease.snapshot()).owner_id is None
        with pytest.raises(DesktopSessionStoppedError):
            await service.observe("session-1")

    asyncio.run(run())


def test_provider_failure_marks_session_degraded_and_success_recovers_it():
    async def run():
        provider = None

        def factory(store):
            nonlocal provider
            provider = FakeObservationProvider(store, error=RuntimeError("capture unavailable"))
            return provider

        service = DesktopSessionService(observation_provider_factory=factory)
        await service.create(session_id="session-1")

        with pytest.raises(RuntimeError, match="capture unavailable"):
            await service.observe("session-1")
        degraded = await service.get("session-1")
        assert degraded.provider_health == ProviderHealthStatus.degraded
        assert degraded.last_error is not None

        provider.error = None
        await service.observe("session-1")
        healthy = await service.get("session-1")
        assert healthy.provider_health == ProviderHealthStatus.healthy
        assert healthy.last_error is None

    asyncio.run(run())


def test_delete_removes_session():
    async def run():
        service = DesktopSessionService(observation_provider_factory=FakeObservationProvider)
        await service.create(session_id="session-1")
        await service.delete("session-1")

        with pytest.raises(DesktopSessionNotFoundError):
            await service.get("session-1")

    asyncio.run(run())


def test_concurrent_observations_are_serialized_per_session():
    class SlowObservationProvider(FakeObservationProvider):
        async def observe(self, session_id, *, previous=None):
            await asyncio.sleep(0.01)
            return await super().observe(session_id, previous=previous)

    async def run():
        service = DesktopSessionService(observation_provider_factory=SlowObservationProvider)
        await service.create(session_id="session-1")

        first, second = await asyncio.gather(service.observe("session-1"), service.observe("session-1"))

        assert second.previous_observation_id == first.observation_id

    asyncio.run(run())


def test_observation_sequence_rejects_stale_explicit_previous():
    async def run():
        service = DesktopSessionService(observation_provider_factory=FakeObservationProvider)
        await service.create(session_id="session-1")
        first = await service.observe("session-1")
        await service.observe("session-1", previous=first)

        with pytest.raises(ObservationSequenceError):
            await service.observe("session-1", previous=first)

    asyncio.run(run())


def test_latest_observation_requires_capture_and_returns_snapshot():
    async def run():
        service = DesktopSessionService(observation_provider_factory=FakeObservationProvider)
        await service.create(session_id="session-1")

        with pytest.raises(LatestObservationUnavailableError):
            await service.get_latest_observation("session-1")

        observed = await service.observe("session-1")
        latest = await service.get_latest_observation("session-1")
        latest.summary = "changed outside service"

        assert latest.observation_id == observed.observation_id
        assert (await service.get_latest_observation("session-1")).summary is None

    asyncio.run(run())


def test_session_restores_latest_observation_from_store_factory(tmp_path):
    from vilagent.computer_use.observation_store import JsonFileObservationStore

    async def run():
        factory = lambda session_id: JsonFileObservationStore(tmp_path / session_id)
        first = DesktopSessionService(observation_provider_factory=FakeObservationProvider, observation_store_factory=factory)
        await first.create(session_id="session-1")
        observed = await first.observe("session-1")

        restarted = DesktopSessionService(observation_provider_factory=FakeObservationProvider, observation_store_factory=factory)
        snapshot = await restarted.create(session_id="session-1")

        assert snapshot.latest_observation_id == observed.observation_id
        assert (await restarted.get_latest_observation("session-1")).observation_id == observed.observation_id

    asyncio.run(run())
