"""Tests for fail-closed dedicated host process supervision."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from vilagent.computer_use.process_supervisor import HostProcessSupervisor, HostSupervisorStatus


class FakeProcess:
    def __init__(self, *, fail_start=False):
        self.alive = False
        self.fail_start = fail_start
        self.events = []

    def start(self):
        self.events.append("start")
        if self.fail_start:
            raise OSError("sensitive process detail")
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.events.append("terminate")
        self.alive = False

    def join(self, timeout=None):
        self.events.append(("join", timeout))

    def request_stop(self):
        self.events.append("request_stop")
        self.alive = False


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def heartbeat(self):
        if self.responses:
            return self.responses.pop(0)
        return SimpleNamespace(succeeded=True)


def test_supervisor_becomes_healthy_and_stops_child_without_auto_restart():
    async def run():
        process = FakeProcess()
        supervisor = HostProcessSupervisor(
            process_factory=lambda: process,
            client_factory=lambda: FakeClient([SimpleNamespace(succeeded=True)]),
            monitor_interval_seconds=0.01,
        )

        starting = await supervisor.start()
        await asyncio.sleep(0.03)
        healthy = await supervisor.snapshot()
        stopped = await supervisor.stop()

        assert starting.status == HostSupervisorStatus.starting
        assert healthy.status == HostSupervisorStatus.healthy
        assert stopped.status == HostSupervisorStatus.stopped
        assert stopped.automatic_restart_allowed is False
        assert process.events[0] == "start"
        assert "request_stop" in process.events

    asyncio.run(run())


def test_supervisor_exposes_client_only_while_ready_child_is_owned():
    async def run():
        process = FakeProcess()
        client = FakeClient([])
        supervisor = HostProcessSupervisor(
            process_factory=lambda: process,
            client_factory=lambda: client,
        )

        try:
            await supervisor.create_client()
        except RuntimeError as exc:
            assert "no ready child" in str(exc)
        else:
            raise AssertionError("Expected stopped supervisor to reject client creation")

        await supervisor.start()
        assert await supervisor.create_client() is client
        await supervisor.stop()

        try:
            await supervisor.create_client()
        except RuntimeError as exc:
            assert "no ready child" in str(exc)
        else:
            raise AssertionError("Expected stopped supervisor to reject client creation")

    asyncio.run(run())


def test_supervisor_requires_successful_readiness_probe_before_monitoring():
    async def run():
        process = FakeProcess()

        async def not_ready(child):
            assert child is process
            return False

        supervisor = HostProcessSupervisor(
            process_factory=lambda: process,
            client_factory=lambda: FakeClient([]),
            readiness_probe=not_ready,
        )

        snapshot = await supervisor.start()

        assert snapshot.status == HostSupervisorStatus.unhealthy
        assert snapshot.last_error_code == "host_process_handshake_failed"
        assert snapshot.child_alive is False
        assert "request_stop" in process.events

    asyncio.run(run())


def test_supervisor_terminates_child_on_heartbeat_failure():
    async def run():
        process = FakeProcess()
        supervisor = HostProcessSupervisor(
            process_factory=lambda: process,
            client_factory=lambda: FakeClient([SimpleNamespace(succeeded=False)]),
            monitor_interval_seconds=0.01,
        )

        await supervisor.start()
        await asyncio.sleep(0.03)
        snapshot = await supervisor.snapshot()

        assert snapshot.status == HostSupervisorStatus.unhealthy
        assert snapshot.child_alive is False
        assert snapshot.last_error_code == "host_process_heartbeat_failed"
        assert "request_stop" in process.events

    asyncio.run(run())


def test_supervisor_marks_process_exit_and_start_failure_without_leaking_details():
    async def run():
        exited = FakeProcess()
        exit_supervisor = HostProcessSupervisor(
            process_factory=lambda: exited,
            client_factory=lambda: FakeClient([]),
            monitor_interval_seconds=0.01,
        )
        await exit_supervisor.start()
        exited.alive = False
        await asyncio.sleep(0.03)
        exit_snapshot = await exit_supervisor.snapshot()

        failed = FakeProcess(fail_start=True)
        failed_supervisor = HostProcessSupervisor(process_factory=lambda: failed, client_factory=lambda: FakeClient([]))
        failed_snapshot = await failed_supervisor.start()

        assert exit_snapshot.last_error_code == "host_process_exited"
        assert failed_snapshot.status == HostSupervisorStatus.unhealthy
        assert failed_snapshot.last_error_code == "host_process_start_failed"
        assert "sensitive" not in failed_snapshot.model_dump_json()

    asyncio.run(run())
