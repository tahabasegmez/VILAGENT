"""Fail-closed lifecycle supervision for the future dedicated host process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from vilagent.computer_use.ipc import LocalHostIpcClient


class HostChildProcess(Protocol):
    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def last_error_code(self) -> str | None: ...


class HostSupervisorStatus(StrEnum):
    stopped = "stopped"
    starting = "starting"
    healthy = "healthy"
    unhealthy = "unhealthy"


class HostSupervisorSnapshot(BaseModel):
    status: HostSupervisorStatus
    child_alive: bool
    last_error_code: str | None = None
    automatic_restart_allowed: bool = False


class HostProcessSupervisor:
    """Supervise one child and stop it on process or heartbeat failure."""

    def __init__(
        self,
        *,
        process_factory: Callable[[], HostChildProcess],
        client_factory: Callable[[], LocalHostIpcClient],
        readiness_probe: Callable[[HostChildProcess], Awaitable[bool]] | None = None,
        monitor_interval_seconds: float = 2,
        join_timeout_seconds: float = 5,
    ):
        if monitor_interval_seconds <= 0 or join_timeout_seconds <= 0:
            raise ValueError("Supervisor intervals must be positive")
        self._process_factory = process_factory
        self._client_factory = client_factory
        self._readiness_probe = readiness_probe
        self._monitor_interval_seconds = monitor_interval_seconds
        self._join_timeout_seconds = join_timeout_seconds
        self._process: HostChildProcess | None = None
        self._monitor_task: asyncio.Task | None = None
        self._status = HostSupervisorStatus.stopped
        self._last_error_code: str | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> HostSupervisorSnapshot:
        async with self._lock:
            if self._status != HostSupervisorStatus.stopped:
                raise RuntimeError("Host process supervisor is already active")
            self._status = HostSupervisorStatus.starting
            self._last_error_code = None
            self._process = self._process_factory()
            try:
                await asyncio.to_thread(self._process.start)
            except Exception:
                self._status = HostSupervisorStatus.unhealthy
                self._last_error_code = "host_process_start_failed"
                return self._snapshot()
            if self._readiness_probe is not None:
                try:
                    ready = await self._readiness_probe(self._process)
                except Exception:
                    ready = False
                if not ready:
                    self._status = HostSupervisorStatus.unhealthy
                    process_error_code = None
                    try:
                        process_error_code = self._process.last_error_code() if self._process is not None else None
                    except Exception:
                        process_error_code = None
                    self._last_error_code = process_error_code or "host_process_handshake_failed"
                    await self._terminate_child()
                    return self._snapshot()
            self._monitor_task = asyncio.create_task(self._monitor(), name="vilagent-host-process-supervisor")
            return self._snapshot()

    async def stop(self) -> HostSupervisorSnapshot:
        async with self._lock:
            if self._monitor_task is not None:
                self._monitor_task.cancel()
                await asyncio.gather(self._monitor_task, return_exceptions=True)
                self._monitor_task = None
            await self._terminate_child()
            self._status = HostSupervisorStatus.stopped
            self._last_error_code = None
            return self._snapshot()

    async def snapshot(self) -> HostSupervisorSnapshot:
        async with self._lock:
            return self._snapshot()

    async def create_client(self) -> LocalHostIpcClient:
        """Return a client only while a ready child remains supervisor-owned."""
        async with self._lock:
            if self._process is None or not self._process.is_alive() or self._status not in {
                HostSupervisorStatus.starting,
                HostSupervisorStatus.healthy,
            }:
                raise RuntimeError("Host process supervisor has no ready child")
            return self._client_factory()

    async def _monitor(self) -> None:
        client = self._client_factory()
        while True:
            if self._process is None or not self._process.is_alive():
                await self._mark_unhealthy("host_process_exited")
                return
            response = await client.heartbeat()
            if not response.succeeded:
                await self._mark_unhealthy("host_process_heartbeat_failed")
                return
            async with self._lock:
                self._status = HostSupervisorStatus.healthy
            await asyncio.sleep(self._monitor_interval_seconds)

    async def _mark_unhealthy(self, error_code: str) -> None:
        async with self._lock:
            self._status = HostSupervisorStatus.unhealthy
            self._last_error_code = error_code
            await self._terminate_child()

    async def _terminate_child(self) -> None:
        process = self._process
        if process is not None and process.is_alive():
            request_stop = getattr(process, "request_stop", None)
            if request_stop is not None:
                try:
                    await asyncio.to_thread(request_stop)
                    await asyncio.to_thread(process.join, self._join_timeout_seconds)
                except Exception:
                    pass
            if process.is_alive():
                await asyncio.to_thread(process.terminate)
                await asyncio.to_thread(process.join, self._join_timeout_seconds)
        self._process = None

    def _snapshot(self) -> HostSupervisorSnapshot:
        return HostSupervisorSnapshot(
            status=self._status,
            child_alive=self._process is not None and self._process.is_alive(),
            last_error_code=self._last_error_code,
        )
