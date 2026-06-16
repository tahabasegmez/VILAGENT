"""Tests for the authenticated dedicated-host control-plane contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from vilagent.computer_use.ipc import AuthenticatedHostControlDispatcher, HostHeartbeatState, HostIpcRequest, HostIpcResponse, HostProcessStatus
from vilagent.computer_use.models import ActionOwner, BrowserStateSummary, ComputerUseHostHealth, DesktopSafetySnapshot, DesktopSafetyStatus, MonitorRef, Observation, Rect, Size


class MutableClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


def _health():
    return ComputerUseHostHealth(
        desktop_safety=DesktopSafetySnapshot(status=DesktopSafetyStatus.ready),
        emergency_stop_engaged=False,
        emergency_stop_hotkey_registered=True,
        mutation_allowed=True,
    )


def test_heartbeat_tracks_starting_healthy_stale_and_stopped():
    async def run():
        clock = MutableClock()
        heartbeat = HostHeartbeatState(stale_after_seconds=5, clock=clock)

        assert (await heartbeat.snapshot()).status == HostProcessStatus.starting
        assert (await heartbeat.beat()).status == HostProcessStatus.healthy
        clock.now += timedelta(seconds=6)
        assert (await heartbeat.snapshot()).status == HostProcessStatus.stale
        assert (await heartbeat.stop()).status == HostProcessStatus.stopped
        assert (await heartbeat.beat()).status == HostProcessStatus.healthy

    asyncio.run(run())


def test_dispatcher_requires_auth_and_never_echoes_token():
    async def run():
        heartbeat = HostHeartbeatState()

        async def health_provider():
            return _health()

        dispatcher = AuthenticatedHostControlDispatcher(token="secret-token", heartbeat=heartbeat, health_provider=health_provider)
        response = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(HostIpcRequest(request_id="request-1", operation="heartbeat", token="wrong-token").model_dump_json().encode())
        )

        assert response.succeeded is False
        assert response.error_code == "ipc_auth_failed"
        assert "secret-token" not in response.model_dump_json()
        assert "wrong-token" not in response.model_dump_json()

    asyncio.run(run())


def test_dispatcher_serves_authenticated_heartbeat_and_health():
    async def run():
        heartbeat = HostHeartbeatState()

        async def health_provider():
            return _health()

        dispatcher = AuthenticatedHostControlDispatcher(token="secret-token", heartbeat=heartbeat, health_provider=health_provider)
        beat = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(HostIpcRequest(request_id="beat-1", operation="heartbeat", token="secret-token").model_dump_json().encode())
        )
        health = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(HostIpcRequest(request_id="health-1", operation="health", token="secret-token").model_dump_json().encode())
        )

        assert beat.succeeded is True
        assert beat.result["status"] == "healthy"
        assert health.succeeded is True
        assert health.result["mutation_allowed"] is True

    asyncio.run(run())


def test_dispatcher_rejects_invalid_and_oversized_messages_with_sanitized_errors():
    async def run():
        async def health_provider():
            return _health()

        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=HostHeartbeatState(),
            health_provider=health_provider,
            max_message_bytes=32,
        )
        invalid = HostIpcResponse.model_validate_json(await dispatcher.dispatch(b"{sensitive-invalid-json"))
        oversized = HostIpcResponse.model_validate_json(await dispatcher.dispatch(b"x" * 33))

        assert invalid.error_code == "invalid_ipc_request"
        assert "sensitive" not in invalid.model_dump_json()
        assert oversized.error_code == "ipc_message_too_large"

    asyncio.run(run())


def test_ipc_request_requires_uia_query_only_for_uia_find():
    from pydantic import ValidationError

    from vilagent.computer_use.models import UIAQuery

    valid = HostIpcRequest(request_id="uia-1", operation="uia_find", token="token", uia_query=UIAQuery(name="Save"))

    assert valid.uia_query.name == "Save"
    for payload in (
        {"request_id": "uia-2", "operation": "uia_find", "token": "token"},
        {"request_id": "uia-3", "operation": "health", "token": "token", "uia_query": {"name": "Save"}},
    ):
        try:
            HostIpcRequest.model_validate(payload)
        except ValidationError:
            pass
        else:
            raise AssertionError("Expected operation-specific UIA query validation failure")


def test_ipc_request_requires_exact_owner_for_lifecycle_reads():
    from pydantic import ValidationError

    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
    events = HostIpcRequest(
        request_id="events-1",
        operation="lifecycle_events_list",
        token="token",
        owner=owner,
        after_sequence=2,
        limit=10,
    )
    action = HostIpcRequest(
        request_id="action-1",
        operation="action_get",
        token="token",
        owner=owner,
        action_id="action-1",
    )
    wait = HostIpcRequest(
        request_id="wait-1",
        operation="lifecycle_events_wait",
        token="token",
        owner=owner,
        timeout_seconds=20,
    )

    assert events.owner == owner
    assert action.action_id == "action-1"
    assert wait.timeout_seconds == 20
    for payload in (
        {"request_id": "events-2", "operation": "lifecycle_events_list", "token": "token"},
        {"request_id": "action-2", "operation": "action_get", "token": "token", "action_id": "action-1"},
        {"request_id": "health-2", "operation": "health", "token": "token", "owner": owner.model_dump()},
        {"request_id": "wait-2", "operation": "lifecycle_events_wait", "token": "token", "owner": owner.model_dump()},
        {
            "request_id": "wait-3",
            "operation": "lifecycle_events_wait",
            "token": "token",
            "owner": owner.model_dump(),
            "timeout_seconds": 31,
        },
    ):
        with pytest.raises(ValidationError):
            HostIpcRequest.model_validate(payload)
def test_ipc_blob_export_info_requires_exact_owner_session_observation_and_blob():
    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
    valid = HostIpcRequest(
        request_id="blob-1",
        operation="observation_blob_export_info",
        token="token",
        session_id="session-1",
        observation_id="observation-1",
        blob_id="a" * 32,
        owner=owner,
    )
    assert valid.owner == owner

    for payload in (
        {"request_id": "blob-2", "operation": "observation_blob_export_info", "token": "token", "session_id": "session-1"},
        {
            "request_id": "blob-3",
            "operation": "observation_blob_export_info",
            "token": "token",
            "session_id": "session-1",
            "observation_id": "observation-1",
            "blob_id": "a" * 32,
        },
        {"request_id": "health-blob", "operation": "health", "token": "token", "blob_id": "a" * 32},
    ):
        with pytest.raises(ValueError):
            HostIpcRequest.model_validate(payload)


def test_ipc_request_validates_browser_lifecycle_fields():
    from pydantic import ValidationError

    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
    create = HostIpcRequest(
        request_id="browser-1",
        operation="browser_session_create",
        token="token",
        owner=owner,
        url="https://example.com",
    )
    listed = HostIpcRequest(request_id="browser-2", operation="browser_sessions_list", token="token", owner=owner)
    closed = HostIpcRequest(
        request_id="browser-3",
        operation="browser_session_close",
        token="token",
        owner=owner,
        browser_session_id="tab-1",
    )
    observed = HostIpcRequest(
        request_id="browser-observe-1",
        operation="session_observe",
        token="token",
        session_id="session-1",
        owner=owner,
        browser_session_id="tab-1",
    )
    resolved = HostIpcRequest(
        request_id="browser-resolve-1",
        operation="target_resolve",
        token="token",
        session_id="session-1",
        target_query={"description": "Save"},
        owner=owner,
        browser_session_id="tab-1",
    )

    assert create.url == "https://example.com"
    assert listed.owner == owner
    assert closed.browser_session_id == "tab-1"
    assert observed.owner == owner
    assert resolved.browser_session_id == "tab-1"
    for payload in (
        {"request_id": "browser-4", "operation": "browser_session_create", "token": "token", "url": "https://example.com"},
        {"request_id": "browser-5", "operation": "browser_session_create", "token": "token", "owner": owner.model_dump()},
        {"request_id": "browser-6", "operation": "browser_sessions_list", "token": "token"},
        {"request_id": "browser-7", "operation": "browser_session_close", "token": "token", "owner": owner.model_dump()},
        {"request_id": "browser-8", "operation": "browser_health", "token": "token", "owner": owner.model_dump()},
        {"request_id": "browser-9", "operation": "health", "token": "token", "browser_session_id": "tab-1"},
        {"request_id": "browser-10", "operation": "health", "token": "token", "url": "https://example.com"},
        {"request_id": "browser-11", "operation": "session_observe", "token": "token", "session_id": "session-1", "browser_session_id": "tab-1"},
        {"request_id": "browser-12", "operation": "session_observe", "token": "token", "session_id": "session-1", "owner": owner.model_dump()},
        {"request_id": "browser-13", "operation": "target_resolve", "token": "token", "session_id": "session-1", "target_query": {"description": "Save"}, "browser_session_id": "tab-1"},
        {"request_id": "browser-14", "operation": "target_resolve", "token": "token", "session_id": "session-1", "target_query": {"description": "Save"}, "owner": owner.model_dump()},
    ):
        with pytest.raises(ValidationError):
            HostIpcRequest.model_validate(payload)


def test_dispatcher_attaches_browser_state_to_owned_observation():
    async def run():
        heartbeat = HostHeartbeatState()
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        calls = []

        async def health_provider():
            return _health()

        async def observation_provider(session_id, requested_owner, browser_session_id):
            calls.append((session_id, requested_owner, browser_session_id))
            return Observation(
                observation_id="obs-1",
                session_id=session_id,
                browser_state=BrowserStateSummary(url="https://example.com", tab_id=browser_session_id, allowed_domain=True),
                monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=10, height=10)),
                screen_size=Size(width=10, height=10),
            )

        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=heartbeat,
            health_provider=health_provider,
            observation_provider=observation_provider,
        )

        response = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(
                HostIpcRequest(
                    request_id="observe-1",
                    operation="session_observe",
                    token="secret-token",
                    session_id="session-1",
                    owner=owner,
                    browser_session_id="tab-1",
                ).model_dump_json().encode()
            )
        )

        assert response.succeeded is True
        assert response.result["browser_state"]["tab_id"] == "tab-1"
        assert calls == [("session-1", owner, "tab-1")]

    asyncio.run(run())


def test_dispatcher_preserves_sanitized_observation_reason_code():
    async def run():
        heartbeat = HostHeartbeatState()

        async def health_provider():
            return _health()

        async def observation_provider(session_id, requested_owner, browser_session_id):
            error = RuntimeError("raw details must not cross IPC")
            error.reason_code = "screen_capture_input_desktop_unavailable"
            raise error

        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=heartbeat,
            health_provider=health_provider,
            observation_provider=observation_provider,
        )

        response = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(
                HostIpcRequest(
                    request_id="observe-failed",
                    operation="session_observe",
                    token="secret-token",
                    session_id="session-1",
                ).model_dump_json().encode()
            )
        )

        assert response.succeeded is False
        assert response.error_code == "screen_capture_input_desktop_unavailable"
        assert response.error_message == "Desktop observation is unavailable."

    asyncio.run(run())


def test_dispatcher_does_not_expose_arbitrary_observation_reason_code():
    async def run():
        heartbeat = HostHeartbeatState()

        async def health_provider():
            return _health()

        async def observation_provider(session_id, requested_owner, browser_session_id):
            error = RuntimeError("raw details must not cross IPC")
            error.reason_code = "secret_internal_detail"
            raise error

        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=heartbeat,
            health_provider=health_provider,
            observation_provider=observation_provider,
        )

        response = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(
                HostIpcRequest(
                    request_id="observe-failed",
                    operation="session_observe",
                    token="secret-token",
                    session_id="session-1",
                ).model_dump_json().encode()
            )
        )

        assert response.error_code == "observation_unavailable"

    asyncio.run(run())


def test_dispatcher_serves_owner_scoped_browser_lifecycle():
    async def run():
        from vilagent.computer_use.browser import BrowserHealth

        heartbeat = HostHeartbeatState()
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        calls = []

        async def health_provider():
            return _health()

        async def browser_health_provider():
            return BrowserHealth(enabled=True, healthy=True, active_sessions=1)

        async def create_provider(url, requested_owner):
            calls.append(("create", url, requested_owner))
            return BrowserStateSummary(url=url, tab_id="tab-1", allowed_domain=True)

        async def list_provider(requested_owner):
            calls.append(("list", requested_owner))
            return ["tab-1"]

        async def close_provider(browser_session_id, requested_owner):
            calls.append(("close", browser_session_id, requested_owner))

        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=heartbeat,
            health_provider=health_provider,
            browser_health_provider=browser_health_provider,
            browser_session_create_provider=create_provider,
            browser_sessions_provider=list_provider,
            browser_session_close_provider=close_provider,
        )

        health = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(HostIpcRequest(request_id="bh", operation="browser_health", token="secret-token").model_dump_json().encode())
        )
        created = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(
                HostIpcRequest(
                    request_id="bc",
                    operation="browser_session_create",
                    token="secret-token",
                    owner=owner,
                    url="https://example.com",
                ).model_dump_json().encode()
            )
        )
        listed = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(
                HostIpcRequest(request_id="bl", operation="browser_sessions_list", token="secret-token", owner=owner).model_dump_json().encode()
            )
        )
        closed = HostIpcResponse.model_validate_json(
            await dispatcher.dispatch(
                HostIpcRequest(
                    request_id="bd",
                    operation="browser_session_close",
                    token="secret-token",
                    owner=owner,
                    browser_session_id="tab-1",
                ).model_dump_json().encode()
            )
        )

        assert health.result["healthy"] is True
        assert created.result["tab_id"] == "tab-1"
        assert listed.result == {"sessions": ["tab-1"]}
        assert closed.succeeded is True
        assert calls == [
            ("create", "https://example.com", owner),
            ("list", owner),
            ("close", "tab-1", owner),
        ]

    asyncio.run(run())
