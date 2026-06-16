"""Local desktop-session lifecycle owned by the computer-use harness."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from vilagent.computer_use.lease import DesktopLease
from vilagent.computer_use.models import (
    DesktopSessionRef,
    DesktopSessionSnapshot,
    DesktopSessionStatus,
    Observation,
    ProviderHealthStatus,
    StructuredError,
)
from vilagent.computer_use.observation_store import InMemoryObservationStore, JsonFileObservationStore
from vilagent.computer_use.providers import ObservationProvider


class DesktopSessionNotFoundError(KeyError):
    pass


class DesktopSessionStoppedError(RuntimeError):
    pass


class ObservationSequenceError(RuntimeError):
    pass


class LatestObservationUnavailableError(RuntimeError):
    pass


ObservationStore = InMemoryObservationStore | JsonFileObservationStore


@dataclass(slots=True)
class _DesktopSessionRuntime:
    ref: DesktopSessionRef
    observation_provider: ObservationProvider
    observation_store: ObservationStore
    desktop_lease: DesktopLease
    status: DesktopSessionStatus = DesktopSessionStatus.ready
    provider_health: ProviderHealthStatus = ProviderHealthStatus.healthy
    latest_observation: Observation | None = None
    last_error: StructuredError | None = None
    observation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DesktopSessionService:
    """Manage isolated logical sessions over the local Windows desktop."""

    name = "desktop-session-service"

    def __init__(
        self,
        *,
        observation_provider_factory: Callable[[ObservationStore], ObservationProvider],
        observation_store_factory: Callable[[str], ObservationStore] | None = None,
        max_observations_per_session: int = 100,
        lease_stale_after_seconds: float = 30,
    ):
        self._observation_provider_factory = observation_provider_factory
        self._observation_store_factory = observation_store_factory
        self._max_observations_per_session = max_observations_per_session
        self._lease_stale_after_seconds = lease_stale_after_seconds
        self._sessions: dict[str, _DesktopSessionRuntime] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, session_id: str | None = None) -> DesktopSessionSnapshot:
        resolved_id = session_id or uuid.uuid4().hex
        async with self._lock:
            if resolved_id in self._sessions:
                raise ValueError(f"Desktop session '{resolved_id}' already exists")
            store = (
                self._observation_store_factory(resolved_id)
                if self._observation_store_factory is not None
                else InMemoryObservationStore(max_observations_per_session=self._max_observations_per_session)
            )
            history = await store.list_session(resolved_id)
            runtime = _DesktopSessionRuntime(
                ref=DesktopSessionRef(session_id=resolved_id),
                observation_provider=self._observation_provider_factory(store),
                observation_store=store,
                desktop_lease=DesktopLease(stale_after_seconds=self._lease_stale_after_seconds),
                latest_observation=history[-1] if history else None,
            )
            self._sessions[resolved_id] = runtime
            return self._snapshot(runtime)

    async def get(self, session_id: str) -> DesktopSessionSnapshot:
        async with self._lock:
            return self._snapshot(self._get_runtime_locked(session_id))

    async def list(self) -> list[DesktopSessionSnapshot]:
        async with self._lock:
            return [self._snapshot(runtime) for runtime in self._sessions.values()]

    async def observe(self, session_id: str, *, previous: Observation | None = None) -> Observation:
        async with self._lock:
            runtime = self._get_runtime_locked(session_id)
        async with runtime.observation_lock:
            async with self._lock:
                if runtime.status == DesktopSessionStatus.stopped:
                    raise DesktopSessionStoppedError(session_id)
                latest = runtime.latest_observation
                if previous is not None and latest is not None and previous.observation_id != latest.observation_id:
                    raise ObservationSequenceError("Requested previous observation is no longer the session's latest observation")

            try:
                observation = await runtime.observation_provider.observe(session_id, previous=latest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lock:
                    runtime.provider_health = ProviderHealthStatus.degraded
                    runtime.last_error = StructuredError(
                        code="observation_provider_error",
                        message=str(exc) or exc.__class__.__name__,
                        retryable=True,
                    )
                raise

            async with self._lock:
                runtime.latest_observation = observation
                runtime.provider_health = ProviderHealthStatus.healthy
                runtime.last_error = None
            return observation

    async def stop(self, session_id: str) -> DesktopSessionSnapshot:
        async with self._lock:
            runtime = self._get_runtime_locked(session_id)
            runtime.status = DesktopSessionStatus.stopped
            runtime.provider_health = ProviderHealthStatus.stopped
        await runtime.desktop_lease.force_release()
        return self._snapshot(runtime)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            runtime = self._get_runtime_locked(session_id)
            self._sessions.pop(session_id)
        await runtime.desktop_lease.force_release()
        await runtime.observation_store.delete_session(session_id)

    async def get_observation_store(self, session_id: str) -> ObservationStore:
        async with self._lock:
            return self._get_runtime_locked(session_id).observation_store

    async def get_latest_observation(self, session_id: str) -> Observation:
        async with self._lock:
            runtime = self._get_runtime_locked(session_id)
            if runtime.latest_observation is None:
                raise LatestObservationUnavailableError(f"Desktop session '{session_id}' has no observation")
            return runtime.latest_observation.model_copy(deep=True)

    async def get_desktop_lease(self, session_id: str) -> DesktopLease:
        async with self._lock:
            return self._get_runtime_locked(session_id).desktop_lease

    def _get_runtime_locked(self, session_id: str) -> _DesktopSessionRuntime:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise DesktopSessionNotFoundError(session_id) from exc

    @staticmethod
    def _snapshot(runtime: _DesktopSessionRuntime) -> DesktopSessionSnapshot:
        return DesktopSessionSnapshot(
            session=runtime.ref,
            status=runtime.status,
            provider_name=runtime.observation_provider.name,
            provider_health=runtime.provider_health,
            latest_observation_id=runtime.latest_observation.observation_id if runtime.latest_observation else None,
            last_error=runtime.last_error,
        )
