"""Tests for the narrow typed remote Windows host facade."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vilagent.computer_use.browser import BrowserHealth
from vilagent.computer_use.models import ActionCommand, ActionKind, ActionLifecycleRecord, ActionOwner, BrowserStateSummary, ComputerUseHostHealth, DesktopSafetySnapshot, DesktopSafetyStatus, MonitorRef, Observation, Rect, Size, TargetQuery, UIAQuery, action_fingerprint
from vilagent.computer_use.remote_host import RemoteHostOperationError, RemoteHostUnavailableError, RemoteLifecycleRecordNotFoundError, RemoteSessionNotFoundError, RemoteWindowsHostControl


def _health():
    return ComputerUseHostHealth(
        desktop_safety=DesktopSafetySnapshot(status=DesktopSafetyStatus.ready),
        emergency_stop_engaged=False,
        emergency_stop_hotkey_registered=True,
        local_ipc_listening=True,
        ipc_heartbeat_status="healthy",
        mutation_allowed=True,
    )


def test_remote_facade_returns_typed_health_and_heartbeat():
    async def run():
        class Client:
            async def typed_health(self):
                return _health()

            async def heartbeat(self):
                return SimpleNamespace(succeeded=True)

        remote = RemoteWindowsHostControl(Client())

        assert (await remote.health()).mutation_allowed is True
        assert await remote.heartbeat() is True

    asyncio.run(run())


def test_remote_facade_fails_closed_on_invalid_or_unavailable_health():
    async def run():
        class Client:
            async def typed_health(self):
                return None

        remote = RemoteWindowsHostControl(Client())

        with pytest.raises(RemoteHostUnavailableError, match="health is unavailable"):
            await remote.health()

    asyncio.run(run())


def test_remote_facade_controls_owner_scoped_browser_sessions():
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

        class Client:
            async def browser_health(self):
                return SimpleNamespace(succeeded=True, error_code=None, result=BrowserHealth(enabled=True, healthy=True).model_dump(mode="json"))

            async def create_browser_session(self, url, requested_owner):
                assert url == "https://example.com"
                assert requested_owner == owner
                return SimpleNamespace(
                    succeeded=True,
                    error_code=None,
                    result=BrowserStateSummary(url=url, tab_id="tab-1", allowed_domain=True).model_dump(mode="json"),
                )

            async def list_browser_sessions(self, requested_owner):
                assert requested_owner == owner
                return SimpleNamespace(succeeded=True, error_code=None, result={"sessions": ["tab-1"]})

            async def close_browser_session(self, browser_session_id, requested_owner):
                assert browser_session_id == "tab-1"
                assert requested_owner == owner
                return SimpleNamespace(succeeded=True, error_code=None, result={})

        remote = RemoteWindowsHostControl(Client())

        assert (await remote.browser_health()).healthy is True
        assert (await remote.create_browser_session("https://example.com", owner)).tab_id == "tab-1"
        assert await remote.list_browser_sessions(owner) == ["tab-1"]
        assert await remote.close_browser_session("tab-1", owner) is None

    asyncio.run(run())


def test_remote_facade_maps_browser_operation_errors():
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

        class Client:
            async def create_browser_session(self, url, requested_owner):
                return SimpleNamespace(succeeded=False, error_code="browser_policy_denied", result=None)

        with pytest.raises(RemoteHostOperationError, match="browser_policy_denied"):
            await RemoteWindowsHostControl(Client()).create_browser_session("https://blocked.example", owner)

    asyncio.run(run())


def test_remote_facade_lists_typed_sessions_and_fails_closed_when_unavailable():
    async def run():
        class Client:
            async def typed_sessions(self):
                return []

        class BrokenClient:
            async def typed_sessions(self):
                return None

        assert await RemoteWindowsHostControl(Client()).list_sessions() == []
        with pytest.raises(RemoteHostUnavailableError, match="sessions are unavailable"):
            await RemoteWindowsHostControl(BrokenClient()).list_sessions()

    asyncio.run(run())


def test_remote_facade_gets_session_and_preserves_not_found():
    async def run():
        class Client:
            async def session(self, session_id):
                assert session_id == "session-1"
                return SimpleNamespace(succeeded=False, result=None, error_code="session_not_found")

        with pytest.raises(RemoteSessionNotFoundError):
            await RemoteWindowsHostControl(Client()).get_session("session-1")

    asyncio.run(run())


def test_remote_facade_validates_uia_windows_and_elements():
    async def run():
        class Client:
            async def uia_windows(self):
                return SimpleNamespace(succeeded=True, error_code=None, result={"windows": [{"window_id": "window-1", "title": "Editor"}]})

            async def uia_find(self, query):
                assert query == UIAQuery(name="Save")
                return SimpleNamespace(
                    succeeded=True,
                    error_code=None,
                    result={"elements": [{"element_id": "element-1", "name": "Save", "automation_id": "save"}]},
                )

        remote = RemoteWindowsHostControl(Client())

        assert (await remote.list_uia_windows())[0].title == "Editor"
        assert (await remote.find_uia_elements(UIAQuery(name="Save")))[0].automation_id == "save"

    asyncio.run(run())


def test_remote_facade_observes_session_with_owned_browser_state():
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        observation = Observation(
            observation_id="obs-1",
            session_id="session-1",
            browser_state=BrowserStateSummary(url="https://example.com", tab_id="tab-1", allowed_domain=True),
            monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=10, height=10)),
            screen_size=Size(width=10, height=10),
        )

        class Client:
            async def observe_session(self, session_id, *, owner=None, browser_session_id=None):
                assert session_id == "session-1"
                assert owner == owner_ref
                assert browser_session_id == "tab-1"
                return SimpleNamespace(succeeded=True, error_code=None, result=observation.model_dump(mode="json"))

        owner_ref = owner
        result = await RemoteWindowsHostControl(Client()).observe_session("session-1", owner=owner, browser_session_id="tab-1")

        assert result.browser_state.tab_id == "tab-1"
        assert result.browser_state.allowed_domain is True

    asyncio.run(run())


def test_remote_facade_resolves_target_with_owned_browser_state():
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

        class Client:
            async def resolve_target(self, session_id, query, *, owner=None, browser_session_id=None):
                assert session_id == "session-1"
                assert query.description == "Save"
                assert owner == owner_ref
                assert browser_session_id == "tab-1"
                return SimpleNamespace(
                    succeeded=True,
                    error_code=None,
                    result={
                        "target": {
                            "strategy": "browser",
                            "selector": {"css": "#save"},
                            "confidence": 1,
                            "observation_id": "obs-1",
                        },
                        "attempts": [],
                    },
                )

        owner_ref = owner
        result = await RemoteWindowsHostControl(Client()).resolve_target(
            "session-1",
            TargetQuery(description="Save"),
            owner=owner,
            browser_session_id="tab-1",
        )

        assert result.target.strategy == "browser"
        assert result.target.selector == {"css": "#save"}

    asyncio.run(run())


def test_remote_facade_validates_sanitized_audit_events():
    async def run():
        class Client:
            async def audit(self, session_id):
                assert session_id == "session-1"
                return SimpleNamespace(
                    succeeded=True,
                    error_code=None,
                    result={"events": [{"event_id": "event-1", "event_type": "action_blocked", "session_id": "session-1"}]},
                )

        events = await RemoteWindowsHostControl(Client()).list_audit_events("session-1")

        assert events[0].event_id == "event-1"

    asyncio.run(run())


def test_remote_facade_validates_owner_scoped_events_and_action_lookup():
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        action = ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey)
        record = ActionLifecycleRecord(action=action, owner=owner, action_fingerprint=action_fingerprint(action))

        class Client:
            async def lifecycle_events(self, requested_owner, *, session_id=None, after_sequence=0, limit=100):
                assert requested_owner == owner
                return SimpleNamespace(succeeded=True, error_code=None, result={"events": []})

            async def action(self, action_id, requested_owner):
                assert action_id == "action-1"
                assert requested_owner == owner
                return SimpleNamespace(succeeded=True, error_code=None, result=record.model_dump(mode="json"))

        remote = RemoteWindowsHostControl(Client())

        assert await remote.list_lifecycle_events(owner) == []
        assert (await remote.get_action("action-1", owner)).action.action_id == "action-1"

    asyncio.run(run())


def test_remote_action_lookup_hides_missing_or_different_owner():
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

        class Client:
            async def action(self, action_id, requested_owner):
                return SimpleNamespace(succeeded=False, error_code="action_not_found", result=None)

        with pytest.raises(RemoteLifecycleRecordNotFoundError):
            await RemoteWindowsHostControl(Client()).get_action("action-1", owner)

    asyncio.run(run())


def test_remote_facade_waits_for_sanitized_lifecycle_events():
    async def run():
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

        class Client:
            async def wait_lifecycle_events(self, requested_owner, *, session_id=None, after_sequence=0, limit=100, timeout_seconds=20):
                assert requested_owner == owner
                assert timeout_seconds == 0.01
                return SimpleNamespace(succeeded=True, error_code=None, result={"events": []})

        assert await RemoteWindowsHostControl(Client()).wait_lifecycle_events(owner, timeout_seconds=0.01) == []

    asyncio.run(run())
