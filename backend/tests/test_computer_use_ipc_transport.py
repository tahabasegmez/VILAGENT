"""Tests for the authenticated loopback host IPC transport."""

from __future__ import annotations

import asyncio
import hashlib

from vilagent.computer_use.ipc import AuthenticatedHostControlDispatcher, HostHeartbeatState, LocalHostIpcClient, LocalHostIpcServer
from vilagent.computer_use.models import (
    AuditEventType,
    ComputerUseAuditEvent,
    ComputerUseHostHealth,
    DesktopSafetySnapshot,
    DesktopSafetyStatus,
    DesktopSessionRef,
    DesktopSessionSnapshot,
    DesktopSessionStatus,
    ProviderHealthStatus,
    UIAElementRef,
    UIAQuery,
    WindowRef,
    ActionOwner,
    BlobRef,
)


async def _health():
    return ComputerUseHostHealth(
        desktop_safety=DesktopSafetySnapshot(status=DesktopSafetyStatus.ready),
        emergency_stop_engaged=False,
        emergency_stop_hotkey_registered=True,
        mutation_allowed=True,
    )


async def _sessions():
    return [
        DesktopSessionSnapshot(
            session=DesktopSessionRef(session_id="session-1"),
            status=DesktopSessionStatus.ready,
            provider_name="fake-screen",
            provider_health=ProviderHealthStatus.healthy,
        )
    ]


def test_loopback_transport_serves_authenticated_control_operations():
    async def run():
        heartbeat = HostHeartbeatState()
        dispatcher = AuthenticatedHostControlDispatcher(token="secret-token", heartbeat=heartbeat, health_provider=_health)
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            client = LocalHostIpcClient(port=port, token="secret-token")

            beat = await client.heartbeat()
            health = await client.health()

            assert beat.succeeded is True
            assert beat.result["status"] == "healthy"
            assert health.succeeded is True
            assert health.result["mutation_allowed"] is True
        finally:
            await server.stop()

    asyncio.run(run())


def test_loopback_transport_rejects_wrong_token_without_leaking_it():
    async def run():
        dispatcher = AuthenticatedHostControlDispatcher(token="secret-token", heartbeat=HostHeartbeatState(), health_provider=_health)
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            response = await LocalHostIpcClient(port=port, token="wrong-token").heartbeat()

            assert response.succeeded is False
            assert response.error_code == "ipc_auth_failed"
            assert "wrong-token" not in response.model_dump_json()
            assert "secret-token" not in response.model_dump_json()
        finally:
            await server.stop()

    asyncio.run(run())


def test_loopback_transport_streams_raw_blob_and_client_validates_integrity():
    async def run():
        payload = b"raw-png-bytes"
        ref = BlobRef(blob_id="a" * 32, media_type="image/png", size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

        async def export(session_id, observation_id, blob_id, requested_owner):
            assert (session_id, observation_id, blob_id, requested_owner) == ("session-1", "obs-1", ref.blob_id, owner)
            return ref, payload

        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token", heartbeat=HostHeartbeatState(), health_provider=_health, blob_export_provider=export
        )
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            streamed_ref, streamed = await LocalHostIpcClient(port=port, token="secret-token").export_observation_blob(
                "session-1", "obs-1", ref.blob_id, owner
            )
            assert streamed_ref == ref
            assert streamed == payload
        finally:
            await server.stop()

    asyncio.run(run())


def test_loopback_client_returns_sanitized_unavailable_error_after_server_stops():
    async def run():
        dispatcher = AuthenticatedHostControlDispatcher(token="secret-token", heartbeat=HostHeartbeatState(), health_provider=_health)
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        client = LocalHostIpcClient(port=port, token="secret-token", timeout_seconds=0.2)
        await server.stop()

        response = await client.heartbeat()

        assert response.succeeded is False
        assert response.error_code == "ipc_unavailable"

    asyncio.run(run())


def test_loopback_client_returns_typed_health():
    async def run():
        dispatcher = AuthenticatedHostControlDispatcher(token="secret-token", heartbeat=HostHeartbeatState(), health_provider=_health)
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            health = await LocalHostIpcClient(port=port, token="secret-token").typed_health()
            assert health is not None
            assert health.mutation_allowed is True
        finally:
            await server.stop()

    asyncio.run(run())


def test_loopback_client_returns_typed_session_snapshots():
    async def run():
        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=HostHeartbeatState(),
            health_provider=_health,
            sessions_provider=_sessions,
            session_provider=lambda session_id: _sessions_get(session_id),
        )
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            sessions = await LocalHostIpcClient(port=port, token="secret-token").typed_sessions()
            assert sessions is not None
            assert sessions[0].session.session_id == "session-1"
        finally:
            await server.stop()

    asyncio.run(run())


async def _sessions_get(session_id):
    if session_id != "session-1":
        raise KeyError(session_id)
    return (await _sessions())[0]


async def _uia_windows():
    return [WindowRef(window_id="window-1", title="Editor")]


async def _uia_find(query):
    return [UIAElementRef(element_id="element-1", name=query.name or "Save", automation_id="save")]


async def _audit(session_id):
    return [ComputerUseAuditEvent(event_id="event-1", event_type=AuditEventType.action_blocked, session_id=session_id)]


def test_loopback_client_gets_typed_single_session_and_not_found():
    async def run():
        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=HostHeartbeatState(),
            health_provider=_health,
            session_provider=_sessions_get,
        )
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            client = LocalHostIpcClient(port=port, token="secret-token")
            found = await client.session("session-1")
            missing = await client.session("missing")
            assert found.succeeded is True
            assert found.result["session"]["session_id"] == "session-1"
            assert missing.error_code == "session_not_found"
        finally:
            await server.stop()

    asyncio.run(run())


def test_loopback_client_queries_typed_uia_windows_and_elements():
    async def run():
        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=HostHeartbeatState(),
            health_provider=_health,
            uia_windows_provider=_uia_windows,
            uia_find_provider=_uia_find,
        )
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            client = LocalHostIpcClient(port=port, token="secret-token")
            windows = await client.uia_windows()
            elements = await client.uia_find(UIAQuery(name="Save"))

            assert windows.succeeded is True
            assert windows.result["windows"][0]["title"] == "Editor"
            assert elements.succeeded is True
            assert elements.result["elements"][0]["automation_id"] == "save"
        finally:
            await server.stop()

    asyncio.run(run())


def test_loopback_client_reads_sanitized_audit_events():
    async def run():
        dispatcher = AuthenticatedHostControlDispatcher(
            token="secret-token",
            heartbeat=HostHeartbeatState(),
            health_provider=_health,
            audit_provider=_audit,
        )
        server = LocalHostIpcServer(dispatcher)
        port = await server.start()
        try:
            response = await LocalHostIpcClient(port=port, token="secret-token").audit("session-1")
            assert response.succeeded is True
            assert response.result["events"][0]["session_id"] == "session-1"
        finally:
            await server.stop()

    asyncio.run(run())
