"""Tests for the spawn-safe dedicated Windows host process adapter."""

from __future__ import annotations

import asyncio
import os

import pytest

from vilagent.computer_use.action_store import JsonFileActionStore
from vilagent.computer_use.lifecycle_ownership import LifecycleOwnershipClaim
from vilagent.computer_use.models import ActionCommand, ActionKind, ActionOwner, RiskAssessment, RiskLevel, TargetQuery, TargetStrategy
from vilagent.computer_use.process_supervisor import HostSupervisorStatus
from vilagent.computer_use.remote_host import RemoteWindowsHostControl
from vilagent.computer_use.windows.child_process import DedicatedWindowsHostProcess, HostProcessHandshake, create_dedicated_windows_host_supervisor
from vilagent.config.computer_use_config import ComputerUseConfig


class FakeConnection:
    def __init__(self, response=None, *, available=True):
        self.response = response
        self.available = available
        self.sent = []

    def poll(self, timeout):
        return self.available

    def recv(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def send(self, payload):
        self.sent.append(payload)


class FakeProcess:
    def __init__(self, target, args, name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.alive = False
        self.events = []

    def start(self):
        self.events.append("start")
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.events.append("terminate")
        self.alive = False

    def join(self, timeout=None):
        self.events.append(("join", timeout))


class FakeContext:
    def __init__(self, response):
        self.parent = FakeConnection(response)
        self.child = FakeConnection()
        self.process = None

    def Pipe(self, duplex=True):
        assert duplex is True
        return self.parent, self.child

    def Process(self, target, args, name, daemon):
        self.process = FakeProcess(target, args, name, daemon)
        return self.process


def test_dedicated_process_uses_spawn_safe_payload_and_authenticated_handshake():
    context = FakeContext({"ready": True, "port": 43123, "error_code": None})
    process = DedicatedWindowsHostProcess(ComputerUseConfig(enabled=True), context=context, ipc_token="secret-token")

    process.start()
    handshake = process.wait_handshake()
    client = process.create_client()
    process.request_stop()

    assert handshake == HostProcessHandshake(ready=True, port=43123)
    assert context.process.name == "vilagent-windows-host"
    assert context.process.daemon is False
    assert isinstance(context.process.args[0], dict)
    assert context.process.args[1] == "secret-token"
    assert client._port == 43123
    assert context.parent.sent == [{"operation": "stop"}]


def test_dedicated_process_handshake_timeout_and_invalid_payload_fail_closed():
    timeout_context = FakeContext(None)
    timeout_context.parent.available = False
    timeout_process = DedicatedWindowsHostProcess(ComputerUseConfig(enabled=True), context=timeout_context)

    invalid_context = FakeContext({"ready": True, "port": 0, "raw_error": "sensitive"})
    invalid_process = DedicatedWindowsHostProcess(ComputerUseConfig(enabled=True), context=invalid_context)

    assert timeout_process.wait_handshake(0.01).error_code == "host_process_handshake_timeout"
    invalid = invalid_process.wait_handshake()
    assert invalid.error_code == "host_process_handshake_invalid"
    assert "sensitive" not in invalid.model_dump_json()


def test_dedicated_supervisor_waits_for_handshake_before_monitoring():
    async def run():
        context = FakeContext({"ready": True, "port": 43123, "error_code": None})
        supervisor = create_dedicated_windows_host_supervisor(
            ComputerUseConfig(enabled=True),
            context=context,
            monitor_interval_seconds=0.01,
        )

        starting = await supervisor.start()
        await supervisor.stop()

        assert starting.status == HostSupervisorStatus.starting
        assert context.process.events[0] == "start"

    import asyncio

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Dedicated VILAGENT host currently targets Windows")
def test_real_spawn_child_handshake_heartbeat_and_graceful_shutdown(tmp_path):
    async def run():
        config = ComputerUseConfig(
            enabled=True,
            lifecycle_path=str(tmp_path / "lifecycle.json"),
            host_safety={"audit_dir": str(tmp_path / "audit")},
        )
        process = DedicatedWindowsHostProcess(config)
        process.start()
        try:
            handshake = await asyncio.to_thread(process.wait_handshake, 15)
            assert handshake.ready is True
            response = await process.create_client().heartbeat()
            assert response.succeeded is True
            sessions = await process.create_client().typed_sessions()
            assert sessions == []
        finally:
            process.request_stop()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.terminate()
                process.join(5)

        assert process.is_alive() is False

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Dedicated VILAGENT host currently targets Windows")
def test_real_spawn_child_runs_under_fail_closed_supervisor(tmp_path):
    async def run():
        supervisor = create_dedicated_windows_host_supervisor(
            ComputerUseConfig(
                enabled=True,
                lifecycle_path=str(tmp_path / "lifecycle.json"),
                host_safety={"audit_dir": str(tmp_path / "audit")},
            ),
            handshake_timeout_seconds=15,
            monitor_interval_seconds=0.05,
        )

        starting = await supervisor.start()
        await asyncio.sleep(0.15)
        healthy = await supervisor.snapshot()
        stopped = await supervisor.stop()

        assert starting.status == HostSupervisorStatus.starting
        assert healthy.status == HostSupervisorStatus.healthy
        assert healthy.child_alive is True
        assert stopped.status == HostSupervisorStatus.stopped
        assert stopped.child_alive is False

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Dedicated VILAGENT host currently targets Windows")
def test_real_spawn_child_fails_closed_when_lifecycle_is_already_owned(tmp_path):
    async def run():
        lifecycle_path = tmp_path / "lifecycle.json"
        existing_owner = LifecycleOwnershipClaim(lifecycle_path, owner_name="gateway-in-process-host")
        await existing_owner.acquire()
        process = DedicatedWindowsHostProcess(
            ComputerUseConfig(
                enabled=True,
                lifecycle_path=str(lifecycle_path),
                host_safety={"audit_dir": str(tmp_path / "audit")},
            )
        )
        process.start()
        try:
            handshake = await asyncio.to_thread(process.wait_handshake, 15)
            assert handshake.ready is False
            assert handshake.error_code == "host_process_initialization_failed:LifecycleOwnershipError"
            assert str(lifecycle_path) not in handshake.model_dump_json()
        finally:
            process.request_stop()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.terminate()
                process.join(5)
            await existing_owner.release()

        assert process.is_alive() is False

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Dedicated VILAGENT host currently targets Windows")
def test_real_spawn_child_reads_persisted_owner_scoped_action_and_events(tmp_path):
    async def run():
        lifecycle_path = tmp_path / "lifecycle.json"
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        store = JsonFileActionStore(lifecycle_path)
        await store.initialize()
        await store.submit(
            ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey, args={"typed_text": "secret"}),
            owner=owner,
        )
        process = DedicatedWindowsHostProcess(
            ComputerUseConfig(
                enabled=True,
                lifecycle_path=str(lifecycle_path),
                host_safety={"audit_dir": str(tmp_path / "audit")},
            )
        )
        process.start()
        try:
            handshake = await asyncio.to_thread(process.wait_handshake, 15)
            assert handshake.ready is True
            remote = RemoteWindowsHostControl(process.create_client())
            action = await remote.get_action("action-1", owner)
            events = await remote.list_lifecycle_events(owner)

            assert action.action.action_id == "action-1"
            assert events[0].action_id == "action-1"
            assert "secret" not in events[0].model_dump_json()
        finally:
            process.request_stop()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.terminate()
                process.join(5)

        assert process.is_alive() is False

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Dedicated VILAGENT host currently targets Windows")
def test_real_spawn_child_owns_session_observation_target_and_approval_mutations(tmp_path):
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        process = DedicatedWindowsHostProcess(
            ComputerUseConfig(
                enabled=True,
                lifecycle_path=str(tmp_path / "lifecycle.json"),
                observation={"redact_sensitive_regions": False},
                host_safety={"audit_dir": str(tmp_path / "audit")},
            )
        )
        process.start()
        try:
            handshake = await asyncio.to_thread(process.wait_handshake, 15)
            assert handshake.ready is True
            remote = RemoteWindowsHostControl(process.create_client())
            assert await remote.heartbeat() is True

            created = await remote.create_session("session-1")
            observation = await remote.observe_session("session-1")
            resolution = await remote.resolve_target(
                "session-1",
                TargetQuery(description="unlikely-vilagent-test-target", allowed_strategies=[TargetStrategy.uia]),
            )
            action = await remote.submit_action(
                ActionCommand(
                    action_id="action-1",
                    session_id="session-1",
                    kind=ActionKind.hotkey,
                    risk=RiskAssessment(level=RiskLevel.high),
                ),
                owner,
            )
            approvals = await remote.list_pending_approvals(owner)
            approval = await remote.get_approval(approvals[0].approval_id, owner)
            decided = await remote.decide_approval(approval.approval_id, owner, approved=False, decided_by="operator-1")
            stopped = await remote.stop_session("session-1")
            await remote.delete_session("session-1")
            engaged = await remote.engage_emergency_stop("operator stop")
            reset = await remote.reset_emergency_stop("operator reset")

            assert created.session.session_id == "session-1"
            assert observation.session_id == "session-1"
            assert resolution.target is None
            assert resolution.attempts[0].outcome.value == "not_found"
            assert action.status.value == "awaiting_approval"
            assert decided.status.value == "denied"
            assert stopped.status.value == "stopped"
            assert engaged.engaged is True
            assert reset.engaged is False
        finally:
            process.request_stop()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.terminate()
                process.join(5)

        assert process.is_alive() is False

    asyncio.run(run())
