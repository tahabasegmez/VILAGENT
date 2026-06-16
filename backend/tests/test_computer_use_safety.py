"""Tests for host-level computer-use mutation safety."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.ipc import HostHeartbeatState
from vilagent.computer_use.models import ActionCommand, ActionKind, DesktopSafetyStatus, NativeActionResult, TargetRef, TargetStrategy
from vilagent.computer_use.safety import DesktopSafetyState, EmergencyStop, HostActionProvider


def _action(kind=ActionKind.click):
    return ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=kind,
        target=TargetRef(
            strategy=TargetStrategy.uia,
            selector={"automation_id": "save"},
            confidence=1,
            observation_id="obs-1",
        ),
        args={"text": "secret", "button": "left"},
    )


class MemoryAuditStore:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    async def append(self, event):
        if self.fail:
            raise OSError("disk unavailable")
        self.events.append(event)


class FakeActionProvider:
    name = "fake-action"

    def __init__(self):
        self.actions = []

    async def execute(self, action):
        self.actions.append(action)
        return NativeActionResult(succeeded=True)


class ExplodingActionProvider(FakeActionProvider):
    async def execute(self, action):
        raise RuntimeError("provider failed")


def test_host_allows_non_allowlisted_action_since_allowlist_is_disabled():
    async def run():
        delegate = FakeActionProvider()
        audit = MemoryAuditStore()
        host = HostActionProvider(delegate, emergency_stop=EmergencyStop(), audit_store=audit, allowed_actions=[ActionKind.focus_window])

        result = await host.execute(_action())

        assert result.succeeded is True
        assert len(delegate.actions) == 1
        assert audit.events[0].argument_keys == ["button", "text"]
        assert "secret" not in audit.events[0].model_dump_json()

    asyncio.run(run())


def test_emergency_stop_blocks_allowlisted_action_until_reset():
    async def run():
        delegate = FakeActionProvider()
        stop = EmergencyStop()
        host = HostActionProvider(delegate, emergency_stop=stop, audit_store=MemoryAuditStore(), allowed_actions=[ActionKind.click])
        await stop.engage("operator stop")

        blocked = await host.execute(_action())
        await stop.reset()
        allowed = await host.execute(_action())

        assert blocked.error_code == "emergency_stop_engaged"
        assert allowed.succeeded is True
        assert len(delegate.actions) == 1

    asyncio.run(run())


def test_host_fails_closed_when_request_audit_is_unavailable():
    async def run():
        delegate = FakeActionProvider()
        host = HostActionProvider(
            delegate,
            emergency_stop=EmergencyStop(),
            audit_store=MemoryAuditStore(fail=True),
            allowed_actions=[ActionKind.click],
        )

        result = await host.execute(_action())

        assert result.error_code == "audit_unavailable"
        assert delegate.actions == []

    asyncio.run(run())


def test_host_rechecks_emergency_stop_after_request_audit():
    async def run():
        delegate = FakeActionProvider()
        stop = EmergencyStop()

        class EngagingAuditStore(MemoryAuditStore):
            async def append(self, event):
                await super().append(event)
                if event.event_type.value == "action_requested":
                    await stop.engage("late stop")

        host = HostActionProvider(delegate, emergency_stop=stop, audit_store=EngagingAuditStore(), allowed_actions=[ActionKind.click])

        result = await host.execute(_action())

        assert result.error_code == "emergency_stop_engaged"
        assert delegate.actions == []

    asyncio.run(run())


def test_host_audits_delegate_exception():
    async def run():
        audit = MemoryAuditStore()
        host = HostActionProvider(
            ExplodingActionProvider(),
            emergency_stop=EmergencyStop(),
            audit_store=audit,
            allowed_actions=[ActionKind.click],
        )

        try:
            await host.execute(_action())
        except RuntimeError:
            pass

        assert audit.events[-1].error_code == "action_provider_error"

    asyncio.run(run())


def test_host_blocks_unsafe_desktop_before_and_after_request_audit():
    async def run():
        delegate = FakeActionProvider()
        safety = DesktopSafetyState(DesktopSafetyStatus.locked)
        blocked_host = HostActionProvider(
            delegate,
            emergency_stop=EmergencyStop(),
            desktop_safety=safety,
            audit_store=MemoryAuditStore(),
            allowed_actions=[ActionKind.click],
        )
        blocked = await blocked_host.execute(_action())

        class LockingAuditStore(MemoryAuditStore):
            async def append(self, event):
                await super().append(event)
                if event.event_type.value == "action_requested":
                    await safety.set(DesktopSafetyStatus.secure_desktop)

        await safety.set(DesktopSafetyStatus.ready)
        late_host = HostActionProvider(
            delegate,
            emergency_stop=EmergencyStop(),
            desktop_safety=safety,
            audit_store=LockingAuditStore(),
            allowed_actions=[ActionKind.click],
        )
        late_blocked = await late_host.execute(_action())

        assert blocked.error_code == "unsafe_desktop_state"
        assert late_blocked.error_code == "unsafe_desktop_state"
        assert delegate.actions == []

    asyncio.run(run())


def test_host_blocks_when_desktop_safety_provider_fails():
    class BrokenSafety:
        name = "broken-safety"

        async def check(self):
            raise RuntimeError("sensitive provider detail")

    async def run():
        delegate = FakeActionProvider()
        host = HostActionProvider(
            delegate,
            emergency_stop=EmergencyStop(),
            desktop_safety=BrokenSafety(),
            audit_store=MemoryAuditStore(),
            allowed_actions=[ActionKind.click],
        )

        result = await host.execute(_action())

        assert result.error_code == "desktop_safety_unavailable"
        assert "sensitive" not in result.error_message
        assert delegate.actions == []

    asyncio.run(run())


def test_host_blocks_mutation_when_control_plane_heartbeat_is_not_healthy():
    async def run():
        delegate = FakeActionProvider()
        heartbeat = HostHeartbeatState()
        host = HostActionProvider(
            delegate,
            emergency_stop=EmergencyStop(),
            audit_store=MemoryAuditStore(),
            allowed_actions=[ActionKind.click],
            control_heartbeat=heartbeat,
        )

        starting = await host.execute(_action())
        await heartbeat.beat()
        allowed = await host.execute(_action())
        await heartbeat.stop()
        stopped = await host.execute(_action())

        assert starting.error_code == "host_control_plane_unhealthy"
        assert allowed.succeeded is True
        assert stopped.error_code == "host_control_plane_unhealthy"
        assert len(delegate.actions) == 1

    asyncio.run(run())
