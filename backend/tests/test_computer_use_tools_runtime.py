"""Runtime-shape tests for VILAGENT computer-use tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from vilagent.computer_use.models import (
    ActionCommand,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    ActionResult,
    ActionStatus,
    DesktopSessionRef,
    DesktopSessionSnapshot,
    DesktopSessionStatus,
    ProviderHealthStatus,
    MonitorRef,
    NativeActionResult,
    Observation,
    Rect,
    Size,
    TargetRef,
    TargetResolutionResult,
    TargetStrategy,
    WindowRef,
    action_fingerprint,
)
from vilagent.computer_use.tools._context import set_action_owner, set_host_control
from vilagent.computer_use.tools.find_element import find_element_tool
from vilagent.computer_use.tools.observe import observe_desktop_tool
from vilagent.computer_use.tools.perform_native_action import perform_native_action_tool
from vilagent.computer_use.tools.perform_browser_action import perform_browser_action_tool


def _observation():
    return Observation(
        observation_id="obs-1",
        session_id="session-1",
        active_window=WindowRef(window_id="win-1", title="Editor", process_name="notepad.exe"),
        monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100), primary=True),
        screen_size=Size(width=100, height=100),
        redaction_applied=True,
    )


def _target():
    return TargetRef(
        strategy=TargetStrategy.uia,
        selector={"name": "Save", "control_type": "Button", "automation_id": "save"},
        bounds=Rect(x=1, y=2, width=30, height=10),
        confidence=0.95,
        observation_id="obs-1",
    )


def _owner():
    return ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")


class TypedToolRemote:
    def __init__(self):
        self.submitted_action: ActionCommand | None = None
        self.created_sessions: list[str | None] = []

    async def get_session(self, session_id):
        if session_id != "session-1":
            from vilagent.computer_use.remote_host import RemoteSessionNotFoundError

            raise RemoteSessionNotFoundError("Desktop session not found")
        return self._session_snapshot("session-1")

    async def create_session(self, session_id=None):
        self.created_sessions.append(session_id)
        return self._session_snapshot(session_id or "session-1")

    def _session_snapshot(self, session_id):
        return DesktopSessionSnapshot(
            session=DesktopSessionRef(
                session_id=session_id,
                platform="windows",
                monitor_id="primary",
            ),
            status=DesktopSessionStatus.ready,
            provider_name="test",
            provider_health=ProviderHealthStatus.healthy,
        )

    async def observe_session(self, session_id):
        assert session_id == "session-1"
        return _observation()

    async def resolve_target(self, session_id, query, *, owner=None, browser_session_id=None):
        assert session_id == "session-1"
        assert owner == _owner()
        return TargetResolutionResult(target=_target())

    async def submit_action(self, action, owner):
        assert owner == _owner()
        self.submitted_action = action
        return ActionLifecycleRecord(
            action=action,
            owner=owner,
            status=ActionLifecycleStatus.approved,
            action_fingerprint=action_fingerprint(action),
        )

    async def execute_action(self, action_id, owner):
        assert self.submitted_action is not None
        assert action_id == self.submitted_action.action_id
        now = datetime.now(UTC)
        result = ActionResult(
            action_id=action_id,
            status=ActionStatus.succeeded,
            started_at=now,
            completed_at=now,
            before_observation_id="obs-1",
            native_result=NativeActionResult(succeeded=True),
        )
        return ActionLifecycleRecord(
            action=self.submitted_action,
            owner=owner,
            status=ActionLifecycleStatus.succeeded,
            action_fingerprint=action_fingerprint(self.submitted_action),
            result=result,
            completed_at=now,
        )


def test_observe_desktop_tool_formats_typed_observation():
    async def run():
        set_host_control(TypedToolRemote())
        try:
            output = await observe_desktop_tool.coroutine("session-1")
        finally:
            set_host_control(None)  # type: ignore[arg-type]

        assert "Observation obs-1" in output
        assert "notepad.exe" in output
        assert "Editor" in output

    asyncio.run(run())


def test_find_element_tool_formats_typed_target_resolution():
    async def run():
        set_host_control(TypedToolRemote())
        set_action_owner(_owner().model_dump())
        try:
            output = await find_element_tool.coroutine("Save button", session_id="session-1", name="Save")
        finally:
            set_host_control(None)  # type: ignore[arg-type]
            set_action_owner(None)  # type: ignore[arg-type]

        assert "Found via uia" in output
        assert "confidence: 0.95" in output
        assert "automation_id: save" in output

    asyncio.run(run())


def test_perform_native_action_tool_resolves_target_and_executes_typed_lifecycle():
    async def run():
        remote = TypedToolRemote()
        set_host_control(remote)
        set_action_owner(_owner().model_dump())
        try:
            output = await perform_native_action_tool.coroutine(
                "focus_window",
                session_id="session-1",
                target_name="Save",
            )
        finally:
            set_host_control(None)  # type: ignore[arg-type]
            set_action_owner(None)  # type: ignore[arg-type]

        assert "Action focus_window succeeded" in output
        assert remote.submitted_action is not None
        assert remote.submitted_action.target == _target()

    asyncio.run(run())


def test_perform_browser_action_tool_resolves_target_and_executes_typed_lifecycle():
    async def run():
        remote = TypedToolRemote()
        set_host_control(remote)
        set_action_owner(_owner().model_dump())
        try:
            output = await perform_browser_action_tool.coroutine(
                "navigate",
                session_id="session-1",
                url="https://example.com",
            )
        finally:
            set_host_control(None)  # type: ignore[arg-type]
            set_action_owner(None)  # type: ignore[arg-type]

        assert "Action browser_action succeeded" in output
        assert remote.submitted_action is not None
        # target might be None since it's just navigate

    asyncio.run(run())


def test_tools_create_default_session_when_missing():
    async def run():
        remote = TypedToolRemote()
        set_host_control(remote)
        set_action_owner(_owner().model_dump())
        try:
            observed = await observe_desktop_tool.coroutine()
            acted = await perform_native_action_tool.coroutine(
                "focus_window",
                session_id="default",
                target_name="Save",
            )
        finally:
            set_host_control(None)  # type: ignore[arg-type]
            set_action_owner(None)  # type: ignore[arg-type]

        assert "Session session-1" in observed
        assert "Action focus_window succeeded" in acted
        assert remote.created_sessions == [None, None]

    asyncio.run(run())
