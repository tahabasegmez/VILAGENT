"""Gateway lifecycle integration for the VILAGENT Windows Agent Host."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from vilagent.config.app_config import AppConfig

logger = logging.getLogger(__name__)
_HOST_HEARTBEAT_INTERVAL_SECONDS = 2.0


async def _monitor_host_heartbeat(client) -> None:
    while True:
        response = await client.heartbeat()
        if not response.succeeded:
            logger.error("VILAGENT host IPC heartbeat failed: %s", response.error_code)
        await asyncio.sleep(_HOST_HEARTBEAT_INTERVAL_SECONDS)


@asynccontextmanager
async def computer_use_runtime(app: FastAPI, startup_config: AppConfig) -> AsyncGenerator[None, None]:
    """Create the startup-bound Windows Agent Host when explicitly enabled."""
    if not startup_config.computer_use.enabled:
        app.state.computer_use_host = None
        app.state.computer_use_remote_control = None
        app.state.computer_use_host_supervisor = None
        yield
        return
    if startup_config.computer_use.platform.casefold() != "windows":
        raise RuntimeError("VILAGENT computer use currently supports only the Windows platform")

    from vilagent.computer_use.remote_host import RemoteWindowsHostControl

    runtime_mode = getattr(startup_config.computer_use, "runtime_mode", "in_process")
    if runtime_mode == "dedicated_process":
        from vilagent.computer_use.process_supervisor import HostSupervisorStatus
        from vilagent.computer_use.windows import create_dedicated_windows_host_supervisor

        supervisor = create_dedicated_windows_host_supervisor(startup_config.computer_use)
        try:
            started = await supervisor.start()
            if started.status == HostSupervisorStatus.unhealthy or not started.child_alive:
                reason = started.last_error_code or "unknown"
                raise RuntimeError(f"VILAGENT dedicated host process failed to start: {reason}")
            heartbeat_client = await supervisor.create_client()
            initial_heartbeat = await heartbeat_client.heartbeat()
            if not initial_heartbeat.succeeded:
                raise RuntimeError("VILAGENT dedicated host initial heartbeat failed")
        except Exception:
            await supervisor.stop()
            app.state.computer_use_host = None
            app.state.computer_use_remote_control = None
            app.state.computer_use_host_supervisor = None
            raise
        app.state.computer_use_host = None
        app.state.computer_use_remote_control = RemoteWindowsHostControl(heartbeat_client)
        app.state.computer_use_host_supervisor = supervisor
        logger.info("VILAGENT dedicated Windows Agent Host initialized")
        try:
            yield
        finally:
            await supervisor.stop()
            app.state.computer_use_host = None
            app.state.computer_use_remote_control = None
            app.state.computer_use_host_supervisor = None
            logger.info("VILAGENT dedicated Windows Agent Host stopped")
        return

    from vilagent.computer_use.windows import WindowsAgentHost

    host = WindowsAgentHost(startup_config.computer_use)
    try:
        await host.initialize()
        heartbeat_client = host.create_ipc_client()
        initial_heartbeat = await heartbeat_client.heartbeat()
        if not initial_heartbeat.succeeded:
            raise RuntimeError("VILAGENT host IPC initial heartbeat failed")
    except Exception:
        await host.shutdown()
        app.state.computer_use_host = None
        app.state.computer_use_remote_control = None
        app.state.computer_use_host_supervisor = None
        raise
    app.state.computer_use_host = host
    app.state.computer_use_remote_control = RemoteWindowsHostControl(heartbeat_client)
    app.state.computer_use_host_supervisor = None
    heartbeat_task = asyncio.create_task(_monitor_host_heartbeat(heartbeat_client), name="vilagent-host-heartbeat")
    logger.info("VILAGENT Windows Agent Host initialized")
    try:
        yield
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await host.shutdown()
        app.state.computer_use_host = None
        app.state.computer_use_remote_control = None
        app.state.computer_use_host_supervisor = None
        logger.info("VILAGENT Windows Agent Host stopped")
