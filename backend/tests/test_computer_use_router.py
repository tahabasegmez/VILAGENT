"""Gateway API tests for VILAGENT host management."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.gateway.internal_auth import create_internal_auth_headers
from app.gateway.routers import computer_use
from app.gateway.routers.computer_use import _lifecycle_sse, _resolve_event_cursor
from vilagent.computer_use.action_store import ActionStorePersistenceError
from vilagent.computer_use.audit import JsonlComputerUseAuditStore
from vilagent.computer_use.browser import BrowserHealth
from vilagent.computer_use.models import ActionCommand, ActionKind, ActionOwner, BrowserStateSummary, DesktopSafetyStatus, NativeActionResult, UIAElementRef, WindowRef
from vilagent.computer_use.safety import DesktopSafetyState
from vilagent.computer_use.windows import WindowsAgentHost
from vilagent.config.computer_use_config import ComputerUseConfig


class FakeUIAProvider:
    async def list_windows(self):
        return [WindowRef(window_id="window-1", title="Editor")]

    async def find(self, query):
        return [UIAElementRef(element_id="element-1", name=query.name or "Save", automation_id="save")]


class FakeSemanticProvider:
    name = "fake-semantic"

    async def execute(self, action):
        return NativeActionResult(succeeded=True)


def _client(tmp_path: Path, *, with_host=True) -> TestClient:
    app = FastAPI()
    app.include_router(computer_use.router)
    if with_host:
        host = WindowsAgentHost(
            ComputerUseConfig(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            audit_store=JsonlComputerUseAuditStore(tmp_path / "audit"),
            screen_grabber=lambda: Image.new("RGB", (80, 60)),
        )
        app.state.computer_use_host = host

        class Remote:
            async def health(self):
                return await host.health()

            async def list_sessions(self):
                return await host.sessions.list()

            async def get_session(self, session_id):
                from vilagent.computer_use.remote_host import RemoteSessionNotFoundError
                from vilagent.computer_use.session import DesktopSessionNotFoundError

                try:
                    return await host.sessions.get(session_id)
                except DesktopSessionNotFoundError as exc:
                    raise RemoteSessionNotFoundError("Desktop session not found") from exc

            async def list_uia_windows(self):
                return await host.uia.list_windows()

            async def find_uia_elements(self, query):
                return await host.uia.find(query)

            async def list_audit_events(self, session_id):
                return await host.audit_store.list_session(session_id)

            async def browser_health(self):
                return BrowserHealth(enabled=False, healthy=False, error_code="browser_disabled")

            async def create_browser_session(self, url, owner):
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                raise RemoteHostOperationError("browser_unavailable")

            async def list_browser_sessions(self, owner):
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                raise RemoteHostOperationError("browser_unavailable")

            async def close_browser_session(self, browser_session_id, owner):
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                raise RemoteHostOperationError("browser_unavailable")

            async def list_lifecycle_events(self, owner, *, session_id=None, after_sequence=0, limit=100):
                return await host.lifecycle_events.list(
                    owner=owner,
                    session_id=session_id,
                    after_sequence=after_sequence,
                    limit=limit,
                )

            async def get_action(self, action_id, owner):
                from vilagent.computer_use.action_store import ActionNotFoundError, ActionOwnershipError
                from vilagent.computer_use.remote_host import RemoteLifecycleRecordNotFoundError

                try:
                    return await host.action_store.get_action(action_id, owner=owner)
                except (ActionNotFoundError, ActionOwnershipError) as exc:
                    raise RemoteLifecycleRecordNotFoundError("Action not found") from exc

            async def wait_lifecycle_events(self, owner, *, session_id=None, after_sequence=0, limit=100, timeout_seconds=20):
                return await host.lifecycle_events.wait(
                    owner=owner,
                    session_id=session_id,
                    after_sequence=after_sequence,
                    limit=limit,
                    timeout_seconds=timeout_seconds,
                )

            async def create_session(self, session_id=None):
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                try:
                    return await host.sessions.create(session_id=session_id)
                except ValueError as exc:
                    raise RemoteHostOperationError("session_conflict") from exc

            async def stop_session(self, session_id):
                from vilagent.computer_use.remote_host import RemoteHostOperationError
                from vilagent.computer_use.session import DesktopSessionNotFoundError

                try:
                    return await host.sessions.stop(session_id)
                except DesktopSessionNotFoundError as exc:
                    raise RemoteHostOperationError("session_not_found") from exc

            async def delete_session(self, session_id):
                from vilagent.computer_use.remote_host import RemoteHostOperationError
                from vilagent.computer_use.session import DesktopSessionNotFoundError

                try:
                    return await host.sessions.delete(session_id)
                except DesktopSessionNotFoundError as exc:
                    raise RemoteHostOperationError("session_not_found") from exc

            async def observe_session(self, session_id, *, owner=None, browser_session_id=None):
                from vilagent.computer_use.remote_host import RemoteHostOperationError
                from vilagent.computer_use.session import DesktopSessionNotFoundError, DesktopSessionStoppedError

                try:
                    return await host._observe_session(session_id, owner=owner, browser_session_id=browser_session_id)
                except DesktopSessionNotFoundError as exc:
                    raise RemoteHostOperationError("session_not_found") from exc
                except DesktopSessionStoppedError as exc:
                    raise RemoteHostOperationError("session_stopped") from exc

            async def resolve_target(self, session_id, query, *, owner=None, browser_session_id=None):
                from vilagent.computer_use.remote_host import RemoteHostOperationError
                from vilagent.computer_use.session import DesktopSessionNotFoundError, LatestObservationUnavailableError

                try:
                    return await host.resolve_target(session_id, query, owner=owner, browser_session_id=browser_session_id)
                except DesktopSessionNotFoundError as exc:
                    raise RemoteHostOperationError("session_not_found") from exc
                except LatestObservationUnavailableError as exc:
                    raise RemoteHostOperationError("observation_missing") from exc

            async def list_pending_approvals(self, owner):
                return await host.action_store.list_pending_approvals(owner=owner)

            async def get_approval(self, approval_id, owner):
                from vilagent.computer_use.action_store import ActionOwnershipError, ApprovalNotFoundError
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                try:
                    return await host.action_store.get_approval(approval_id, owner=owner)
                except (ApprovalNotFoundError, ActionOwnershipError) as exc:
                    raise RemoteHostOperationError("approval_not_found") from exc

            async def decide_approval(self, approval_id, owner, *, approved, decided_by, reason=None):
                from vilagent.computer_use.action_store import ActionOwnershipError, ApprovalAlreadyDecidedError, ApprovalExpiredError, ApprovalNotFoundError
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                try:
                    return await host.action_store.decide_approval(
                        approval_id,
                        owner=owner,
                        approved=approved,
                        decided_by=decided_by,
                        reason=reason,
                    )
                except (ApprovalNotFoundError, ActionOwnershipError) as exc:
                    raise RemoteHostOperationError("approval_not_found") from exc
                except (ApprovalAlreadyDecidedError, ApprovalExpiredError) as exc:
                    raise RemoteHostOperationError("approval_conflict") from exc

            async def submit_action(self, action, owner):
                from vilagent.computer_use.action_store import ActionStoreError, ActionStorePersistenceError, SessionOwnershipError
                from vilagent.computer_use.remote_host import RemoteHostOperationError
                from vilagent.computer_use.session import DesktopSessionNotFoundError

                try:
                    return await host.action_service.submit(action, owner=owner)
                except DesktopSessionNotFoundError as exc:
                    raise RemoteHostOperationError("session_not_found") from exc
                except ActionStorePersistenceError as exc:
                    raise RemoteHostOperationError("lifecycle_unavailable") from exc
                except SessionOwnershipError as exc:
                    raise RemoteHostOperationError("session_owner_conflict") from exc
                except ActionStoreError as exc:
                    raise RemoteHostOperationError("action_conflict") from exc

            async def cancel_action(self, action_id, owner, *, reason=None):
                from vilagent.computer_use.action_store import ActionNotFoundError, ActionOwnershipError, InvalidActionTransitionError
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                try:
                    return await host.action_store.cancel(action_id, owner=owner, reason=reason)
                except (ActionNotFoundError, ActionOwnershipError) as exc:
                    raise RemoteHostOperationError("action_not_found") from exc
                except InvalidActionTransitionError as exc:
                    raise RemoteHostOperationError("invalid_transition") from exc

            async def execute_action(self, action_id, owner):
                from vilagent.computer_use.action_store import ActionNotFoundError, ActionOwnershipError, InvalidActionTransitionError
                from vilagent.computer_use.remote_host import RemoteHostOperationError

                try:
                    return await host.action_service.execute(action_id, owner=owner)
                except (ActionNotFoundError, ActionOwnershipError) as exc:
                    raise RemoteHostOperationError("action_not_found") from exc
                except InvalidActionTransitionError as exc:
                    raise RemoteHostOperationError("invalid_transition") from exc

            async def emergency_stop(self):
                from vilagent.computer_use.models import EmergencyStopSnapshot

                engaged, reason = await host.emergency_stop.status()
                return EmergencyStopSnapshot(engaged=engaged, reason=reason)

            async def engage_emergency_stop(self, reason):
                from vilagent.computer_use.models import EmergencyStopSnapshot

                await host.engage_emergency_stop(reason)
                return EmergencyStopSnapshot(engaged=True, reason=reason)

            async def reset_emergency_stop(self, reason):
                from vilagent.computer_use.models import EmergencyStopSnapshot

                await host.reset_emergency_stop(reason)
                return EmergencyStopSnapshot(engaged=False)

        app.state.computer_use_remote_control = Remote()
    return TestClient(app, headers=create_internal_auth_headers())


def test_router_returns_503_without_enabled_host(tmp_path):
    response = _client(tmp_path, with_host=False).get("/api/computer-use/sessions")

    assert response.status_code == 503


def test_router_rejects_non_internal_call(tmp_path):
    app = FastAPI()
    app.include_router(computer_use.router)
    client = TestClient(app)

    response = client.get("/api/computer-use/sessions")

    assert response.status_code == 403


def test_host_health_endpoint_is_internal_and_reports_combined_mutation_state(tmp_path):
    client = _client(tmp_path)
    client.app.state.computer_use_host.desktop_safety = DesktopSafetyState(
        DesktopSafetyStatus.locked,
        reason_code="locked_input_desktop",
    )

    response = client.get("/api/computer-use/health")

    assert response.status_code == 200
    assert response.json()["desktop_safety"]["status"] == "locked"
    assert response.json()["desktop_safety"]["reason_code"] == "locked_input_desktop"
    assert response.json()["emergency_stop_engaged"] is False
    assert response.json()["mutation_allowed"] is False


def test_session_lifecycle_and_observation_endpoints(tmp_path):
    client = _client(tmp_path)

    created = client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    observed = client.post("/api/computer-use/sessions/session-1/observe")
    listed = client.get("/api/computer-use/sessions")
    stopped = client.post("/api/computer-use/sessions/session-1/stop")
    deleted = client.delete("/api/computer-use/sessions/session-1")

    assert created.status_code == 201
    assert observed.status_code == 200
    assert observed.json()["screen_size"] == {"width": 80, "height": 60}
    assert listed.json()[0]["latest_observation_id"] == observed.json()["observation_id"]
    assert stopped.json()["status"] == "stopped"
    assert deleted.status_code == 204


def test_duplicate_and_missing_sessions_map_to_http_errors(tmp_path):
    client = _client(tmp_path)
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})

    duplicate = client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    missing = client.get("/api/computer-use/sessions/missing")

    assert duplicate.status_code == 409
    assert missing.status_code == 404


def test_uia_query_endpoints(tmp_path):
    client = _client(tmp_path)

    windows = client.get("/api/computer-use/uia/windows")
    elements = client.post("/api/computer-use/uia/find", json={"name": "Save"})

    assert windows.json()[0]["title"] == "Editor"
    assert elements.json()[0]["automation_id"] == "save"


def test_browser_lifecycle_endpoints_are_internal_and_owner_scoped():
    app = FastAPI()
    app.include_router(computer_use.router)
    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

    class Remote:
        async def browser_health(self):
            return BrowserHealth(enabled=True, healthy=True, active_sessions=1)

        async def create_browser_session(self, url, requested_owner):
            assert requested_owner == owner
            return BrowserStateSummary(url=url, tab_id="tab-1", allowed_domain=True)

        async def list_browser_sessions(self, requested_owner):
            assert requested_owner == owner
            return ["tab-1"]

        async def close_browser_session(self, browser_session_id, requested_owner):
            assert browser_session_id == "tab-1"
            assert requested_owner == owner

    app.state.computer_use_remote_control = Remote()
    client = TestClient(app, headers=create_internal_auth_headers())
    owner_payload = owner.model_dump()

    health = client.get("/api/computer-use/browser/health")
    created = client.post("/api/computer-use/browser/sessions", json={"owner": owner_payload, "url": "https://example.com"})
    listed = client.get("/api/computer-use/browser/sessions", params=owner_payload)
    closed = client.request("DELETE", "/api/computer-use/browser/sessions/tab-1", json=owner_payload)

    assert health.json()["healthy"] is True
    assert created.status_code == 201
    assert created.json()["tab_id"] == "tab-1"
    assert listed.json() == ["tab-1"]
    assert closed.status_code == 204


def test_browser_lifecycle_disabled_maps_to_http_errors(tmp_path):
    client = _client(tmp_path)
    owner_payload = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}

    health = client.get("/api/computer-use/browser/health")
    listed = client.get("/api/computer-use/browser/sessions", params=owner_payload)
    created = client.post("/api/computer-use/browser/sessions", json={"owner": owner_payload, "url": "https://example.com"})

    assert health.status_code == 200
    assert health.json()["error_code"] == "browser_disabled"
    assert listed.status_code == 503
    assert created.status_code == 503


def test_emergency_stop_and_audit_endpoints(tmp_path):
    client = _client(tmp_path)

    engaged = client.post("/api/computer-use/emergency-stop/engage", json={"reason": "operator stop"})
    status = client.get("/api/computer-use/emergency-stop")
    audit = client.get("/api/computer-use/audit/host")
    reset = client.post("/api/computer-use/emergency-stop/reset", json={"reason": "operator reset"})

    assert engaged.json() == {"engaged": True, "reason": "operator stop"}
    assert status.json()["engaged"] is True
    assert audit.json()[0]["emergency_stop_engaged"] is True
    assert reset.json() == {"engaged": False, "reason": None}


def test_action_submission_requires_typed_payload(tmp_path):
    client = _client(tmp_path)

    response = client.post("/api/computer-use/actions", json={})

    assert response.status_code == 422


def test_browser_action_submission_helper_builds_stored_action_without_executing(tmp_path):
    client = _client(tmp_path)
    owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})

    submitted = client.post(
        "/api/computer-use/browser/actions",
        json={
            "owner": owner,
            "session_id": "session-1",
            "action_id": "browser-1",
            "target": {
                "strategy": "browser",
                "selector": {"css": "#save"},
                "confidence": 1,
                "observation_id": "obs-1",
            },
            "browser_state": {
                "url": "https://example.com",
                "tab_id": "tab-1",
                "allowed_domain": True,
            },
            "browser_action": "click",
        },
    )
    invalid = client.post(
        "/api/computer-use/browser/actions",
        json={
            "owner": owner,
            "session_id": "session-1",
            "target": {
                "strategy": "uia",
                "selector": {"automation_id": "save"},
                "confidence": 1,
                "observation_id": "obs-1",
            },
            "browser_state": {
                "url": "https://example.com",
                "tab_id": "tab-1",
                "allowed_domain": True,
            },
        },
    )

    assert submitted.status_code == 201
    body = submitted.json()
    assert body["status"] == "approved"
    assert body["action"]["kind"] == "browser_action"
    assert body["action"]["args"]["tab_id"] == "tab-1"
    assert body["action"]["postconditions"][0]["kind"] == "browser_dom"
    assert invalid.status_code == 422


def test_approval_management_never_accepts_replacement_action_payload(tmp_path):
    client = _client(tmp_path)
    host = client.app.state.computer_use_host
    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

    async def seed():
        await host.action_store.submit(
            ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey),
            owner=owner,
        )
        return await host.action_store.request_approval("action-1", owner=owner)

    import asyncio

    approval = asyncio.run(seed())
    owner_payload = owner.model_dump()
    listed = client.get("/api/computer-use/approvals", params=owner_payload)
    rejected_replacement = client.post(
        f"/api/computer-use/approvals/{approval.approval_id}/approve",
        json={
            "owner": owner_payload,
            "decided_by": "operator-1",
            "reason": "approved",
            "action": {"action_id": "replacement"},
        },
    )
    approved = client.post(
        f"/api/computer-use/approvals/{approval.approval_id}/approve",
        json={"owner": owner_payload, "decided_by": "operator-1", "reason": "approved"},
    )
    duplicate = client.post(
        f"/api/computer-use/approvals/{approval.approval_id}/deny",
        json={"owner": owner_payload, "decided_by": "operator-2"},
    )

    assert listed.status_code == 200 and len(listed.json()) == 1
    assert rejected_replacement.status_code == 422
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert "action" not in approved.json()
    assert duplicate.status_code == 409


def test_approval_ownership_mismatch_is_hidden_and_action_can_be_cancelled(tmp_path):
    client = _client(tmp_path)
    host = client.app.state.computer_use_host
    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

    async def seed():
        await host.action_store.submit(
            ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey),
            owner=owner,
        )
        return await host.action_store.request_approval("action-1", owner=owner)

    import asyncio

    approval = asyncio.run(seed())
    wrong_owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "other-agent"}
    hidden = client.get(f"/api/computer-use/approvals/{approval.approval_id}", params=wrong_owner)
    cancelled = client.post(
        "/api/computer-use/actions/action-1/cancel",
        json={"owner": owner.model_dump(), "reason": "operator cancelled"},
    )

    assert hidden.status_code == 404
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_submit_and_execute_approved_stored_action_without_replacement_payload(tmp_path):
    client = _client(tmp_path)
    owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    observation = client.post("/api/computer-use/sessions/session-1/observe").json()
    action = {
        "action_id": "action-1",
        "session_id": "session-1",
        "kind": "focus_window",
        "target": {
            "strategy": "uia",
            "selector": {"automation_id": "editor"},
            "confidence": 1,
            "observation_id": observation["observation_id"],
        },
    }

    submitted = client.post("/api/computer-use/actions", json={"owner": owner, "action": action})
    rejected_replacement = client.post(
        "/api/computer-use/actions/action-1/execute",
        json={"owner": owner, "action": {"action_id": "replacement"}},
    )
    executed = client.post("/api/computer-use/actions/action-1/execute", json={"owner": owner})

    assert submitted.status_code == 201
    assert submitted.json()["status"] == "approved"
    assert rejected_replacement.status_code == 422
    assert executed.status_code == 200
    assert executed.json()["status"] == "succeeded"


def test_high_risk_action_cannot_execute_before_approval(tmp_path):
    client = _client(tmp_path)
    owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    action = {
        "action_id": "action-1",
        "session_id": "session-1",
        "kind": "hotkey",
        "risk": {"level": "high"},
    }

    submitted = client.post("/api/computer-use/actions", json={"owner": owner, "action": action})
    blocked = client.post("/api/computer-use/actions/action-1/execute", json={"owner": owner})
    approval_id = submitted.json()["approval_id"]
    client.post(
        f"/api/computer-use/approvals/{approval_id}/approve",
        json={"owner": owner, "decided_by": "operator-1"},
    )

    assert submitted.json()["status"] == "awaiting_approval"
    assert blocked.status_code == 409


def test_lifecycle_events_are_owner_filtered_sanitized_and_incremental(tmp_path):
    client = _client(tmp_path)
    owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    client.post(
        "/api/computer-use/actions",
        json={
            "owner": owner,
            "action": {
                "action_id": "action-1",
                "session_id": "session-1",
                "kind": "hotkey",
                "args": {"keys": ["CTRL", "L"], "typed_text": "secret"},
            },
        },
    )

    events = client.get("/api/computer-use/events", params=owner)
    after_first = client.get("/api/computer-use/events", params={**owner, "after_sequence": 1})
    hidden = client.get("/api/computer-use/events", params={**owner, "agent_id": "other-agent"})

    assert events.status_code == 200
    assert [event["event_type"] for event in events.json()] == ["action_submitted", "action_status_changed"]
    assert [event["sequence"] for event in after_first.json()] == [2]
    assert hidden.json() == []
    assert "secret" not in events.text
    assert "args" not in events.text


def test_action_lookup_is_remote_owner_scoped_and_hides_other_owner(tmp_path):
    client = _client(tmp_path)
    owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    other = {"thread_id": "thread-2", "run_id": "run-2", "agent_id": "agent-2"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    client.post(
        "/api/computer-use/actions",
        json={"owner": owner, "action": {"action_id": "action-1", "session_id": "session-1", "kind": "hotkey"}},
    )

    found = client.get("/api/computer-use/actions/action-1", params=owner)
    hidden = client.get("/api/computer-use/actions/action-1", params=other)

    assert found.status_code == 200
    assert found.json()["action"]["action_id"] == "action-1"
    assert hidden.status_code == 404


def test_lifecycle_event_wait_returns_existing_events_and_validates_timeout(tmp_path):
    client = _client(tmp_path)
    owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    client.post(
        "/api/computer-use/actions",
        json={"owner": owner, "action": {"action_id": "action-1", "session_id": "session-1", "kind": "hotkey"}},
    )

    events = client.get("/api/computer-use/events/wait", params={**owner, "after_sequence": 0, "timeout_seconds": 0.01})
    invalid = client.get("/api/computer-use/events/wait", params={**owner, "timeout_seconds": 31})

    assert events.status_code == 200
    assert events.json()[0]["action_id"] == "action-1"
    assert invalid.status_code == 422


def test_lifecycle_stream_requires_internal_auth_and_valid_cursor(tmp_path):
    app = FastAPI()
    app.include_router(computer_use.router)
    configured = _client(tmp_path).app.state
    app.state.computer_use_host = configured.computer_use_host
    app.state.computer_use_remote_control = configured.computer_use_remote_control
    unauthenticated = TestClient(app)
    authenticated = TestClient(app, headers=create_internal_auth_headers())
    params = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}

    rejected = unauthenticated.get("/api/computer-use/events/stream", params=params)
    invalid = authenticated.get("/api/computer-use/events/stream", params={**params, "heartbeat_seconds": 31})

    assert rejected.status_code == 403
    assert invalid.status_code == 422


def test_lifecycle_sse_emits_sanitized_resumable_event_and_stops_on_disconnect(tmp_path):
    async def run():
        client = _client(tmp_path)
        host = client.app.state.computer_use_host
        remote = client.app.state.computer_use_remote_control
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        await host.action_store.submit(
            ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey, args={"typed_text": "secret"}),
            owner=owner,
        )

        class FakeRequest:
            def __init__(self):
                self.calls = 0
                self.headers = {"Last-Event-ID": "0"}

            async def is_disconnected(self):
                self.calls += 1
                return self.calls > 2

        request = FakeRequest()
        frames = [frame async for frame in _lifecycle_sse(request, remote, owner=owner, session_id=None, after_sequence=0, heartbeat_seconds=0.01)]

        assert len(frames) == 1
        assert "event: computer-use.lifecycle" in frames[0]
        assert "id: 1" in frames[0]
        assert "secret" not in frames[0]
        assert "args" not in frames[0]
        assert _resolve_event_cursor(SimpleNamespace(headers={"Last-Event-ID": "4"}), 2) == 4

    import asyncio

    asyncio.run(run())


def test_target_resolution_auto_observes_and_returns_stable_uia_target(tmp_path):
    client = _client(tmp_path)
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})

    # Resolution on a fresh session auto-captures an observation (host
    # `_latest_or_observe` fallback) instead of failing, so plan-execute's UIA
    # path stays robust without a separate explicit observe.
    auto = client.post(
        "/api/computer-use/sessions/session-1/targets/resolve",
        json={"description": "Save", "selector_hints": {"automation_id": "save"}, "allowed_strategies": ["uia"]},
    )
    assert auto.status_code == 200

    observation = client.post("/api/computer-use/sessions/session-1/observe").json()
    resolved = client.post(
        "/api/computer-use/sessions/session-1/targets/resolve",
        json={"description": "Save", "selector_hints": {"automation_id": "save"}, "allowed_strategies": ["uia"]},
    )

    assert resolved.status_code == 200
    # Uses the latest stored observation and stamps it on the resolved target so
    # the engine's stale-target guard can validate it.
    assert resolved.json()["target"]["observation_id"] == observation["observation_id"]
    assert resolved.json()["target"]["selector"]["element_id"] == "element-1"
    assert resolved.json()["attempts"][0]["outcome"] == "resolved"


def test_action_submission_is_owner_scoped_idempotent_and_rejects_conflicts(tmp_path):
    client = _client(tmp_path)
    owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    first = {
        "action_id": "action-1",
        "session_id": "session-1",
        "kind": "hotkey",
        "args": {"keys": ["CTRL", "L"]},
        "idempotency_key": "retry-1",
    }
    retry = {**first, "action_id": "action-2"}
    conflict = {**retry, "kind": "launch_app"}

    submitted = client.post("/api/computer-use/actions", json={"owner": owner, "action": first})
    repeated = client.post("/api/computer-use/actions", json={"owner": owner, "action": retry})
    rejected = client.post("/api/computer-use/actions", json={"owner": owner, "action": conflict})
    events = client.get("/api/computer-use/events", params=owner)

    assert submitted.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["action"]["action_id"] == "action-1"
    assert rejected.status_code == 409
    assert [event["event_type"] for event in events.json()] == ["action_submitted", "action_status_changed"]


def test_action_lifecycle_storage_failure_maps_to_service_unavailable(tmp_path, monkeypatch):
    client = _client(tmp_path)

    async def fail_submit(action, *, owner):
        raise ActionStorePersistenceError("disk unavailable")

    monkeypatch.setattr(client.app.state.computer_use_host.action_service, "submit", fail_submit)
    response = client.post(
        "/api/computer-use/actions",
        json={
            "owner": {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"},
            "action": {"action_id": "action-1", "session_id": "session-1", "kind": "hotkey"},
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Action lifecycle storage unavailable"


def test_session_rejects_action_submission_from_different_owner(tmp_path):
    client = _client(tmp_path)
    first_owner = {"thread_id": "thread-1", "run_id": "run-1", "agent_id": "agent-1"}
    other_owner = {"thread_id": "thread-2", "run_id": "run-2", "agent_id": "agent-2"}
    client.post("/api/computer-use/sessions", json={"session_id": "session-1"})
    action = {"action_id": "action-1", "session_id": "session-1", "kind": "hotkey"}

    first = client.post("/api/computer-use/actions", json={"owner": first_owner, "action": action})
    rejected = client.post(
        "/api/computer-use/actions",
        json={"owner": other_owner, "action": {**action, "action_id": "action-2"}},
    )

    assert first.status_code == 201
    assert rejected.status_code == 409
    assert "another action owner" in rejected.json()["detail"]
