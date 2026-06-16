"""Tests for the Windows Agent Host safety boundary."""

from __future__ import annotations

import asyncio
import base64

import pytest
from PIL import Image

from vilagent.computer_use.action_store import InMemoryActionStore, JsonFileActionStore
from vilagent.computer_use.lifecycle_ownership import LifecycleOwnershipError
from vilagent.computer_use.browser import BrowserHealth
from vilagent.computer_use.models import ActionCommand, ActionKind, ActionOwner, ActionStatus, BlobRef, BrowserStateSummary, Condition, ConditionOperator, DesktopSafetyStatus, NativeActionResult, TargetQuery, TargetRef, TargetStrategy, UIAElementRef, VerificationResult
from vilagent.computer_use.safety import DesktopSafetyState
from vilagent.computer_use.windows.host import WindowsAgentHost
from vilagent.config.computer_use_config import ComputerUseConfig


class MemoryAuditStore:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    async def append(self, event):
        if self.fail:
            raise OSError("audit unavailable")
        self.events.append(event)


class FakeSemanticProvider:
    name = "fake-semantic"

    def __init__(self):
        self.actions = []

    async def execute(self, action):
        self.actions.append(action)
        return NativeActionResult(succeeded=True)


class AllowVerificationProvider:
    name = "allow-verification"

    async def verify(self, conditions, *, before, after):
        return VerificationResult(succeeded=True, checked_conditions=len(conditions))


class FakeUIAProvider:
    async def find(self, query):
        return [UIAElementRef(element_id="42.7", automation_id="editor", control_type="Window", enabled=True, visible=True)]

    async def list_windows(self):
        return []


class FakeHotkeyListener:
    def __init__(self):
        self.running = False
        self.events = []

    async def start(self):
        self.events.append("start")
        self.running = True

    async def stop(self):
        self.events.append("stop")
        self.running = False


class FakeBrowserRuntime:
    def __init__(self):
        self.closed = []

    async def health(self):
        return BrowserHealth(enabled=True, healthy=True)

    async def create_session(self, url):
        return BrowserStateSummary(url=url, title="Inbox", tab_id="tab-1")

    async def close_session(self, browser_session_id):
        self.closed.append(browser_session_id)

    async def observe(self, browser_session_id):
        return BrowserStateSummary(url="https://example.com/inbox", title="Inbox", tab_id=browser_session_id)

    async def resolve_dom(self, browser_session_id, hints):
        return {"css": "#save"}

    async def execute(self, browser_session_id, selector, args):
        return True

    async def query_state(self, browser_session_id, selector):
        return {"exists": True}


class CapturingTargetResolver:
    def __init__(self):
        self.queries = []

    async def resolve(self, query, *, observation):
        self.queries.append(query)
        from vilagent.computer_use.models import TargetResolutionResult

        return TargetResolutionResult()


def _focus_action(observation_id="obs-1"):
    return ActionCommand(
        action_id="focus-1",
        session_id="session-1",
        kind=ActionKind.focus_window,
        target=TargetRef(
            strategy=TargetStrategy.uia,
            selector={"automation_id": "editor"},
            confidence=1,
            observation_id=observation_id,
        ),
    )


def test_host_combines_sessions_actions_audit_and_emergency_stop():
    async def run():
        audit = MemoryAuditStore()
        semantic = FakeSemanticProvider()
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=audit,
            desktop_safety=DesktopSafetyState(),
            semantic_action_provider=semantic,
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.sessions.create(session_id="session-1")
        observation = await host.sessions.observe("session-1")
        allowed = await host.actions.execute(_focus_action())
        await host.engage_emergency_stop("operator stop")
        blocked = await host.actions.execute(_focus_action())

        assert observation.screen_size.width == 20
        assert allowed.succeeded is True
        assert blocked.error_code == "emergency_stop_engaged"
        assert len(semantic.actions) == 1
        assert any(event.session_id == "host" and event.emergency_stop_engaged is True for event in audit.events)

    asyncio.run(run())


def test_host_adds_latest_screenshot_hint_for_vision_resolution():
    async def run():
        resolver = CapturingTargetResolver()
        host = WindowsAgentHost(
            ComputerUseConfig(vision_model={"enabled": True, "pyngrok_url": "https://example.ngrok-free.app"}),
            audit_store=MemoryAuditStore(),
            desktop_safety=DesktopSafetyState(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
            target_resolver=resolver,
        )
        await host.sessions.create(session_id="session-1")
        observation = await host.sessions.observe("session-1")

        await host.resolve_target("session-1", TargetQuery(description="Save", allowed_strategies=[TargetStrategy.vision]))

        assert resolver.queries
        hints = resolver.queries[0].selector_hints
        assert hints["screenshot_media_type"] == "image/png"
        assert base64.b64decode(hints["screenshot_base64"])
        assert observation.screenshot_ref is not None

    asyncio.run(run())


def test_host_captures_screenshot_hint_when_no_latest_observation_exists():
    async def run():
        resolver = CapturingTargetResolver()
        host = WindowsAgentHost(
            ComputerUseConfig(vision_model={"enabled": True, "pyngrok_url": "https://example.ngrok-free.app"}),
            audit_store=MemoryAuditStore(),
            desktop_safety=DesktopSafetyState(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (24, 12)),
            target_resolver=resolver,
        )
        await host.sessions.create(session_id="session-1")

        await host.resolve_target("session-1", TargetQuery(description="Save", allowed_strategies=[TargetStrategy.vision]))

        assert resolver.queries
        hints = resolver.queries[0].selector_hints
        assert hints["screenshot_media_type"] == "image/png"
        assert base64.b64decode(hints["screenshot_base64"])

    asyncio.run(run())


def test_host_health_combines_desktop_safety_and_emergency_stop():
    async def run():
        desktop_safety = DesktopSafetyState(DesktopSafetyStatus.locked, reason_code="locked_input_desktop")
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=MemoryAuditStore(),
            desktop_safety=desktop_safety,
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )

        locked = await host.health()
        await desktop_safety.set(DesktopSafetyStatus.ready)
        await host.engage_emergency_stop("operator stop")
        stopped = await host.health()

        assert locked.desktop_safety.status == DesktopSafetyStatus.locked
        assert locked.mutation_allowed is False
        assert locked.emergency_stop_hotkey_registered is False
        assert stopped.desktop_safety.status == DesktopSafetyStatus.ready
        assert stopped.emergency_stop_engaged is True
        assert stopped.emergency_stop_reason == "operator stop"
        assert stopped.mutation_allowed is False

    asyncio.run(run())


def test_enabled_host_owns_hotkey_listener_lifecycle_and_reports_health(tmp_path):
    async def run():
        listener = FakeHotkeyListener()
        host = WindowsAgentHost(
            ComputerUseConfig(enabled=True, lifecycle_path=str(tmp_path / "lifecycle.json")),
            audit_store=MemoryAuditStore(),
            hotkey_listener=listener,
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )

        await host.initialize()
        health = await host.health()
        await host.shutdown()

        assert health.emergency_stop_hotkey_registered is True
        assert health.local_ipc_listening is True
        assert listener.events == ["start", "stop"]

    asyncio.run(run())


def test_host_reset_is_fail_closed_when_audit_is_unavailable():
    async def run():
        audit = MemoryAuditStore()
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=audit,
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.engage_emergency_stop()
        audit.fail = True

        with pytest.raises(OSError, match="audit unavailable"):
            await host.reset_emergency_stop()

        assert (await host.emergency_stop.status())[0] is True

    asyncio.run(run())


def test_blob_export_rate_and_size_limits_are_audited():
    async def run():
        audit = MemoryAuditStore()
        host = WindowsAgentHost(
            ComputerUseConfig(observation={"max_exports_per_minute_per_owner": 1, "max_export_bytes": 3}),
            audit_store=audit,
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        ref = BlobRef(blob_id="a" * 32, media_type="image/png", size_bytes=4, sha256="b" * 64)

        async def info(*args):
            return ref

        host._blob_export_info = info
        with pytest.raises(RuntimeError, match="blob_export_size_limit"):
            await host._blob_export("session-1", "obs-1", ref.blob_id, owner)
        with pytest.raises(RuntimeError, match="blob_export_rate_limit"):
            await host._blob_export("session-1", "obs-1", ref.blob_id, owner)

        assert [event.error_code for event in audit.events] == ["blob_export_size_limit", "blob_export_rate_limit"]

    asyncio.run(run())


def test_blob_export_fails_closed_when_audit_is_unavailable():
    async def run():
        audit = MemoryAuditStore(fail=True)
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=audit,
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        ref = BlobRef(blob_id="a" * 32, media_type="image/png", size_bytes=2, sha256="b" * 64)

        async def info(*args):
            return ref

        class Store:
            async def get_exportable_blob(self, observation_id, blob_id):
                return b"ok"

        host._blob_export_info = info
        host.sessions.get_observation_store = lambda session_id: asyncio.sleep(0, result=Store())
        with pytest.raises(OSError, match="audit unavailable"):
            await host._blob_export("session-1", "obs-1", ref.blob_id, owner)

    asyncio.run(run())


def test_host_created_engine_uses_host_emergency_stop_and_session_lease():
    async def run():
        audit = MemoryAuditStore()
        semantic = FakeSemanticProvider()
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=audit,
            semantic_action_provider=semantic,
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.sessions.create(session_id="session-1")
        observed = await host.sessions.observe("session-1")
        engine = await host.create_engine("session-1", verification_provider=AllowVerificationProvider())
        await host.engage_emergency_stop()

        result = await engine.execute(_focus_action(observed.observation_id), owner_id="run-1")

        assert result.status == ActionStatus.failed
        assert result.error is not None and result.error.code == "emergency_stop_engaged"
        assert semantic.actions == []
        lease = await host.sessions.get_desktop_lease("session-1")
        assert (await lease.snapshot()).owner_id is None

    asyncio.run(run())


def test_host_created_engine_rejects_action_for_another_session():
    async def run():
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=MemoryAuditStore(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.sessions.create(session_id="session-1")
        engine = await host.create_engine("session-1", verification_provider=AllowVerificationProvider())
        action = _focus_action()
        action.session_id = "session-2"

        with pytest.raises(ValueError, match="bound to desktop session"):
            await engine.execute(action, owner_id="run-1")

    asyncio.run(run())


def test_opt_in_physical_click_profile_still_requires_allowlist_approval_and_postcondition():
    async def run():
        clicks = []
        host = WindowsAgentHost(
            ComputerUseConfig(host_safety={"physical_input_enabled": True, "allowed_actions": [ActionKind.focus_window, ActionKind.click]}),
            audit_store=MemoryAuditStore(),
            desktop_safety=DesktopSafetyState(),
            semantic_action_provider=FakeSemanticProvider(),
            physical_click_injector=lambda x, y: clicks.append((x, y)),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.sessions.create(session_id="session-1")
        observed = await host.sessions.observe("session-1")
        from vilagent.computer_use.models import Rect, TargetRef

        action = ActionCommand(
            action_id="physical-1",
            session_id="session-1",
            kind=ActionKind.click,
            target=TargetRef(strategy=TargetStrategy.coordinate, bounds=Rect(x=2, y=2, width=4, height=4), confidence=1, observation_id=observed.observation_id),
            postconditions=[Condition(kind="screen_changed", operator=ConditionOperator.changed)],
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        submitted = await host.action_service.submit(action, owner=owner)

        assert submitted.status.value == "awaiting_approval"
        assert clicks == []

    asyncio.run(run())


def test_opt_in_browser_provider_precedes_uia_and_remains_outside_default_allowlist():
    async def run():
        host = WindowsAgentHost(
            ComputerUseConfig(browser={"enabled": True, "allowed_domains": ["example.com"]}),
            audit_store=MemoryAuditStore(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            browser_dom_resolver=lambda tab, hints: asyncio.sleep(0, result={"css": "#save"}),
            browser_action_executor=lambda tab, selector, args: asyncio.sleep(0, result=True),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        from vilagent.computer_use.models import BrowserStateSummary
        observation = (await host.sessions.create(session_id="session-1"))
        captured = await host.sessions.observe("session-1")
        captured.browser_state = BrowserStateSummary(url="https://example.com", tab_id="tab-1")
        result = await host.target_resolver.resolve(TargetQuery(description="Save", allowed_strategies=[TargetStrategy.browser, TargetStrategy.uia]), observation=captured)
        assert result.target is not None and result.target.strategy == TargetStrategy.browser

    asyncio.run(run())


def test_host_shutdown_stops_sessions_and_releases_leases():
    async def run():
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=MemoryAuditStore(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.sessions.create(session_id="session-1")
        lease = await host.sessions.get_desktop_lease("session-1")
        await lease.acquire("run-1")

        await host.shutdown()

        assert (await host.sessions.get("session-1")).status.value == "stopped"
        assert (await lease.snapshot()).owner_id is None

    asyncio.run(run())


def test_host_resolves_uia_target_against_latest_observation():
    async def run():
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=MemoryAuditStore(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.sessions.create(session_id="session-1")
        observation = await host.sessions.observe("session-1")

        result = await host.resolve_target("session-1", TargetQuery(description="Editor", allowed_strategies=[TargetStrategy.uia]))

        assert result.target is not None
        assert result.target.observation_id == observation.observation_id
        assert result.target.selector["automation_id"] == "editor"

    asyncio.run(run())


def test_host_default_verification_routes_semantic_uia_conditions():
    async def run():
        host = WindowsAgentHost(
            ComputerUseConfig(),
            audit_store=MemoryAuditStore(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        await host.sessions.create(session_id="session-1")
        observation = await host.sessions.observe("session-1")
        condition = Condition(kind="uia_element", operator=ConditionOperator.exists, selector={"automation_id": "editor"})

        result = await host.verification.verify([condition], before=observation, after=observation)

        assert result.succeeded is True

    asyncio.run(run())


def test_enabled_host_uses_persistent_action_store_and_initializes_it(tmp_path):
    async def run():
        host = WindowsAgentHost(
            ComputerUseConfig(enabled=True, lifecycle_path=str(tmp_path / "lifecycle.json")),
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )

        assert isinstance(host.action_store, JsonFileActionStore)
        await host.initialize()
        await host.action_store.submit(ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey), owner=ActionOwner(thread_id="t", run_id="r", agent_id="a"))
        assert (tmp_path / "lifecycle.json").exists()
        assert (await host.create_ipc_client().heartbeat()).succeeded is True
        await host.shutdown()

    from vilagent.computer_use.models import ActionOwner

    asyncio.run(run())


def test_disabled_host_keeps_in_memory_action_store():
    host = WindowsAgentHost(
        ComputerUseConfig(),
        audit_store=MemoryAuditStore(),
        semantic_action_provider=FakeSemanticProvider(),
        screen_grabber=lambda: Image.new("RGB", (20, 10)),
    )

    assert type(host.action_store) is InMemoryActionStore


def test_enabled_hosts_cannot_share_durable_lifecycle_ownership(tmp_path):
    async def run():
        config = ComputerUseConfig(enabled=True, lifecycle_path=str(tmp_path / "lifecycle.json"))
        first = WindowsAgentHost(
            config,
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        second = WindowsAgentHost(
            config,
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )

        await first.initialize()
        with pytest.raises(LifecycleOwnershipError, match="already owned"):
            await second.initialize()

        await first.shutdown()
        await second.initialize()
        await second.shutdown()

    asyncio.run(run())


def test_enabled_host_ipc_exposes_owner_scoped_action_and_sanitized_events(tmp_path):
    async def run():
        from vilagent.computer_use.models import ActionOwner
        from vilagent.computer_use.remote_host import RemoteLifecycleRecordNotFoundError, RemoteWindowsHostControl

        host = WindowsAgentHost(
            ComputerUseConfig(enabled=True, lifecycle_path=str(tmp_path / "lifecycle.json")),
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        other = ActionOwner(thread_id="thread-2", run_id="run-2", agent_id="agent-2")
        await host.initialize()
        try:
            await host.action_store.submit(
                ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey, args={"typed_text": "secret"}),
                owner=owner,
            )
            remote = RemoteWindowsHostControl(host.create_ipc_client())

            action = await remote.get_action("action-1", owner)
            events = await remote.list_lifecycle_events(owner)
            waited = await remote.wait_lifecycle_events(owner, after_sequence=events[-1].sequence, timeout_seconds=0.01)

            assert action.action.action_id == "action-1"
            assert events[0].action_id == "action-1"
            assert waited == []
            assert "secret" not in events[0].model_dump_json()
            with pytest.raises(RemoteLifecycleRecordNotFoundError):
                await remote.get_action("action-1", other)
        finally:
            await host.shutdown()

    asyncio.run(run())


def test_enabled_host_remote_control_owns_approval_action_and_session_mutations(tmp_path):
    async def run():
        from vilagent.computer_use.models import ActionOwner, RiskAssessment, RiskLevel
        from vilagent.computer_use.remote_host import RemoteWindowsHostControl

        host = WindowsAgentHost(
            ComputerUseConfig(enabled=True, lifecycle_path=str(tmp_path / "lifecycle.json")),
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        await host.initialize()
        try:
            remote = RemoteWindowsHostControl(host.create_ipc_client())
            assert await remote.heartbeat() is True
            created = await remote.create_session("session-1")
            observation = await remote.observe_session("session-1")
            target = await remote.resolve_target(
                "session-1",
                TargetQuery(description="Editor", allowed_strategies=[TargetStrategy.uia]),
            )
            submitted = await remote.submit_action(
                ActionCommand(
                    action_id="focus-1",
                    session_id="session-1",
                    kind=ActionKind.focus_window,
                    target=target.target,
                ),
                owner,
            )
            executed = await remote.execute_action("focus-1", owner)

            awaiting = await remote.submit_action(
                ActionCommand(
                    action_id="hotkey-1",
                    session_id="session-1",
                    kind=ActionKind.hotkey,
                    risk=RiskAssessment(level=RiskLevel.high),
                ),
                owner,
            )
            approvals = await remote.list_pending_approvals(owner)
            reconciled = await remote.get_approval(approvals[0].approval_id, owner)
            approved = await remote.decide_approval(
                reconciled.approval_id,
                owner,
                approved=True,
                decided_by="operator-1",
            )
            cancelled = await remote.cancel_action("hotkey-1", owner, reason="operator cancelled")
            stopped = await remote.stop_session("session-1")
            await remote.delete_session("session-1")
            engaged = await remote.engage_emergency_stop("operator stop")
            reset = await remote.reset_emergency_stop("operator reset")

            assert created.session.session_id == "session-1"
            assert observation.observation_id == target.target.observation_id
            assert submitted.status.value == "approved"
            assert executed.status.value == "succeeded"
            assert awaiting.status.value == "awaiting_approval"
            assert approved.status.value == "approved"
            assert cancelled.status.value == "cancelled"
            assert stopped.status.value == "stopped"
            assert engaged.engaged is True
            assert reset.engaged is False
        finally:
            await host.shutdown()

    asyncio.run(run())


def test_enabled_host_remote_control_manages_owner_scoped_browser_sessions(tmp_path):
    async def run():
        from vilagent.computer_use.remote_host import RemoteHostOperationError, RemoteWindowsHostControl

        runtime = FakeBrowserRuntime()
        host = WindowsAgentHost(
            ComputerUseConfig(
                enabled=True,
                lifecycle_path=str(tmp_path / "lifecycle.json"),
                browser={"enabled": True, "allowed_domains": ["example.com"]},
            ),
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            browser_runtime=runtime,
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        other = ActionOwner(thread_id="thread-2", run_id="run-2", agent_id="agent-2")
        await host.initialize()
        try:
            remote = RemoteWindowsHostControl(host.create_ipc_client())

            health = await remote.browser_health()
            created = await remote.create_browser_session("https://example.com/inbox", owner)
            listed = await remote.list_browser_sessions(owner)
            hidden = await remote.list_browser_sessions(other)

            assert health.healthy is True
            assert created.tab_id == "tab-1"
            assert listed == ["tab-1"]
            assert hidden == []
            with pytest.raises(RemoteHostOperationError, match="browser_session_not_found"):
                await remote.close_browser_session("tab-1", other)
            await remote.close_browser_session("tab-1", owner)
            assert runtime.closed == ["tab-1"]
            assert await remote.list_browser_sessions(owner) == []
        finally:
            await host.shutdown()

    asyncio.run(run())


def test_enabled_host_remote_observation_attaches_owned_browser_state(tmp_path):
    async def run():
        from vilagent.computer_use.remote_host import RemoteHostOperationError, RemoteWindowsHostControl

        runtime = FakeBrowserRuntime()
        host = WindowsAgentHost(
            ComputerUseConfig(
                enabled=True,
                lifecycle_path=str(tmp_path / "lifecycle.json"),
                browser={"enabled": True, "allowed_domains": ["example.com"]},
            ),
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            browser_runtime=runtime,
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        other = ActionOwner(thread_id="thread-2", run_id="run-2", agent_id="agent-2")
        await host.initialize()
        try:
            remote = RemoteWindowsHostControl(host.create_ipc_client())
            await remote.create_session("session-1")
            browser = await remote.create_browser_session("https://example.com/inbox", owner)

            plain = await remote.observe_session("session-1")
            enriched = await remote.observe_session("session-1", owner=owner, browser_session_id=browser.tab_id)

            assert plain.browser_state is None
            assert enriched.browser_state is not None
            assert enriched.browser_state.tab_id == browser.tab_id
            assert enriched.browser_state.url == "https://example.com/inbox"
            assert enriched.browser_state.allowed_domain is True
            with pytest.raises(RemoteHostOperationError, match="browser_session_not_found"):
                await remote.observe_session("session-1", owner=other, browser_session_id=browser.tab_id)
        finally:
            await host.shutdown()

    asyncio.run(run())


def test_enabled_host_remote_target_resolution_uses_owned_browser_state(tmp_path):
    async def run():
        from vilagent.computer_use.remote_host import RemoteHostOperationError, RemoteWindowsHostControl

        runtime = FakeBrowserRuntime()
        host = WindowsAgentHost(
            ComputerUseConfig(
                enabled=True,
                lifecycle_path=str(tmp_path / "lifecycle.json"),
                browser={"enabled": True, "allowed_domains": ["example.com"]},
            ),
            audit_store=MemoryAuditStore(),
            hotkey_listener=FakeHotkeyListener(),
            uia_provider=FakeUIAProvider(),
            semantic_action_provider=FakeSemanticProvider(),
            browser_runtime=runtime,
            screen_grabber=lambda: Image.new("RGB", (20, 10)),
        )
        owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
        other = ActionOwner(thread_id="thread-2", run_id="run-2", agent_id="agent-2")
        await host.initialize()
        try:
            remote = RemoteWindowsHostControl(host.create_ipc_client())
            await remote.create_session("session-1")
            browser = await remote.create_browser_session("https://example.com/inbox", owner)

            result = await remote.resolve_target(
                "session-1",
                TargetQuery(description="Save", selector_hints={"text": "Save"}, allowed_strategies=[TargetStrategy.browser, TargetStrategy.uia]),
                owner=owner,
                browser_session_id=browser.tab_id,
            )

            assert result.target is not None
            assert result.target.strategy == TargetStrategy.browser
            assert result.target.selector["css"] == "#save"
            with pytest.raises(RemoteHostOperationError, match="browser_session_not_found"):
                await remote.resolve_target(
                    "session-1",
                    TargetQuery(description="Save", allowed_strategies=[TargetStrategy.browser]),
                    owner=other,
                    browser_session_id=browser.tab_id,
                )
        finally:
            await host.shutdown()

    asyncio.run(run())


def test_host_creates_optional_browser_runtime_when_enabled(monkeypatch):
    runtime = FakeBrowserRuntime()

    import vilagent.computer_use.windows.host as host_module

    monkeypatch.setattr(host_module, "create_browser_runtime", lambda config: runtime)

    host = WindowsAgentHost(
        ComputerUseConfig(browser={"enabled": True, "allowed_domains": ["example.com"]}),
        audit_store=MemoryAuditStore(),
        uia_provider=FakeUIAProvider(),
        semantic_action_provider=FakeSemanticProvider(),
        screen_grabber=lambda: Image.new("RGB", (20, 10)),
    )

    assert host._browser_runtime is runtime
    assert host.browser_lifecycle is not None
