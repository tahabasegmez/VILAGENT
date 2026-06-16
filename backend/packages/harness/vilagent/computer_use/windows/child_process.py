"""Spawn-safe dedicated Windows host child-process bootstrap."""

from __future__ import annotations

import asyncio
import multiprocessing
import secrets
from multiprocessing.connection import Connection
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vilagent.computer_use.ipc import LocalHostIpcClient
from vilagent.computer_use.process_supervisor import HostProcessSupervisor
from vilagent.computer_use.windows.host import WindowsAgentHost
from vilagent.config.computer_use_config import ComputerUseConfig


class HostProcessHandshake(BaseModel):
    ready: bool
    port: int | None = Field(default=None, ge=1, le=65535)
    error_code: str | None = None
    model_config = ConfigDict(extra="forbid")


class DedicatedWindowsHostProcess:
    """Parent-side spawn adapter with a one-time pipe startup handshake."""

    def __init__(
        self,
        config: ComputerUseConfig,
        *,
        context: Any | None = None,
        ipc_token: str | None = None,
    ):
        self._context = context or multiprocessing.get_context("spawn")
        self._config_payload = config.model_dump(mode="json")
        self._ipc_token = ipc_token or secrets.token_urlsafe(32)
        self._parent_connection, child_connection = self._context.Pipe(duplex=True)
        self._process = self._context.Process(
            target=run_windows_host_child,
            args=(self._config_payload, self._ipc_token, child_connection),
            name="vilagent-windows-host",
            daemon=False,
        )
        self._handshake: HostProcessHandshake | None = None

    def start(self) -> None:
        self._process.start()

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def last_error_code(self) -> str | None:
        return self._handshake.error_code if self._handshake is not None else None

    def terminate(self) -> None:
        self._process.terminate()

    def join(self, timeout: float | None = None) -> None:
        self._process.join(timeout)

    def wait_handshake(self, timeout_seconds: float = 10) -> HostProcessHandshake:
        if timeout_seconds <= 0:
            raise ValueError("Handshake timeout must be positive")
        if not self._parent_connection.poll(timeout_seconds):
            return HostProcessHandshake(ready=False, error_code="host_process_handshake_timeout")
        try:
            self._handshake = HostProcessHandshake.model_validate(self._parent_connection.recv())
        except Exception:
            return HostProcessHandshake(ready=False, error_code="host_process_handshake_invalid")
        return self._handshake

    def create_client(self) -> LocalHostIpcClient:
        if self._handshake is None or not self._handshake.ready or self._handshake.port is None:
            raise RuntimeError("Dedicated host process handshake is not ready")
        return LocalHostIpcClient(
            port=self._handshake.port,
            token=self._ipc_token,
            execution_timeout_seconds=self._config_payload["budgets"]["duration_seconds"] + 1,
        )

    def request_stop(self) -> None:
        if self.is_alive():
            self._parent_connection.send({"operation": "stop"})


def run_windows_host_child(config_payload: dict[str, Any], ipc_token: str, connection: Connection) -> None:
    """Multiprocessing target; never expose raw child exceptions to parent."""
    try:
        asyncio.run(_run_windows_host_child(config_payload, ipc_token, connection))
    except Exception as exc:
        _send_handshake(
            connection,
            HostProcessHandshake(
                ready=False,
                error_code=f"host_process_initialization_failed:{type(exc).__name__}",
            ),
        )


async def _run_windows_host_child(config_payload: dict[str, Any], ipc_token: str, connection: Connection) -> None:
    config = ComputerUseConfig.model_validate(config_payload)
    host = WindowsAgentHost(config, ipc_token=ipc_token)
    try:
        await host.initialize()
        if host.ipc_server is None or host.ipc_server.port is None:
            raise RuntimeError("Dedicated host IPC failed to start")
        _send_handshake(connection, HostProcessHandshake(ready=True, port=host.ipc_server.port))
        await asyncio.to_thread(connection.recv)
    finally:
        await host.shutdown()


def _send_handshake(connection: Connection, handshake: HostProcessHandshake) -> None:
    try:
        connection.send(handshake.model_dump(mode="json"))
    except Exception:
        pass


def create_dedicated_windows_host_supervisor(
    config: ComputerUseConfig,
    *,
    context: Any | None = None,
    handshake_timeout_seconds: float = 10,
    monitor_interval_seconds: float = 2,
) -> HostProcessSupervisor:
    """Build a supervisor whose monitor starts only after a valid handshake."""
    holder: dict[str, DedicatedWindowsHostProcess] = {}

    def process_factory() -> DedicatedWindowsHostProcess:
        process = DedicatedWindowsHostProcess(config, context=context)
        holder["process"] = process
        return process

    async def readiness_probe(process: DedicatedWindowsHostProcess) -> bool:
        handshake = await asyncio.to_thread(process.wait_handshake, handshake_timeout_seconds)
        return handshake.ready

    def client_factory() -> LocalHostIpcClient:
        return holder["process"].create_client()

    return HostProcessSupervisor(
        process_factory=process_factory,
        client_factory=client_factory,
        readiness_probe=readiness_probe,
        monitor_interval_seconds=monitor_interval_seconds,
    )
