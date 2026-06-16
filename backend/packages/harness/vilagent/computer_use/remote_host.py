"""Narrow typed facade for the dedicated host's remote control plane."""

from __future__ import annotations

from vilagent.computer_use.browser import BrowserHealth
from vilagent.computer_use.ipc import LocalHostIpcClient
from vilagent.computer_use.models import (
    ActionLifecycleRecord,
    ActionOwner,
    ApprovalRecord,
    BlobRef,
    BrowserStateSummary,
    ComputerUseAuditEvent,
    ComputerUseHostHealth,
    ComputerUseLifecycleEvent,
    DesktopSessionSnapshot,
    EmergencyStopSnapshot,
    Observation,
    TargetQuery,
    TargetResolutionResult,
    UIAElementRef,
    UIAQuery,
    WindowRef,
)


class RemoteHostUnavailableError(RuntimeError):
    """Raised when the dedicated host cannot provide a valid typed response."""


class RemoteSessionNotFoundError(RuntimeError):
    """Raised when the dedicated host confirms a session does not exist."""


class RemoteLifecycleRecordNotFoundError(RuntimeError):
    """Raised when an owner-scoped lifecycle record is not visible."""


class RemoteHostOperationError(RuntimeError):
    """Raised for a sanitized typed remote operation failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RemoteWindowsHostControl:
    """Read-only remote facade used during the dedicated-host migration."""

    def __init__(self, client: LocalHostIpcClient):
        self._client = client

    async def health(self) -> ComputerUseHostHealth:
        health = await self._client.typed_health()
        if health is None:
            raise RemoteHostUnavailableError("Dedicated Windows host health is unavailable")
        return health

    async def heartbeat(self) -> bool:
        return (await self._client.heartbeat()).succeeded

    async def list_sessions(self) -> list[DesktopSessionSnapshot]:
        sessions = await self._client.typed_sessions()
        if sessions is None:
            raise RemoteHostUnavailableError("Dedicated Windows host sessions are unavailable")
        return sessions

    async def get_session(self, session_id: str) -> DesktopSessionSnapshot:
        response = await self._client.session(session_id)
        if response.error_code == "session_not_found":
            raise RemoteSessionNotFoundError("Desktop session not found")
        if not response.succeeded or response.result is None:
            raise RemoteHostUnavailableError("Dedicated Windows host session is unavailable")
        try:
            return DesktopSessionSnapshot.model_validate(response.result)
        except Exception as exc:
            raise RemoteHostUnavailableError("Dedicated Windows host session is unavailable") from exc

    async def list_uia_windows(self) -> list[WindowRef]:
        response = await self._client.uia_windows()
        return self._validated_list(response, "windows", WindowRef, "Windows UI Automation windows are unavailable")

    async def find_uia_elements(self, query: UIAQuery) -> list[UIAElementRef]:
        response = await self._client.uia_find(query)
        return self._validated_list(response, "elements", UIAElementRef, "Windows UI Automation elements are unavailable")

    async def list_audit_events(self, session_id: str) -> list[ComputerUseAuditEvent]:
        response = await self._client.audit(session_id)
        return self._validated_list(response, "events", ComputerUseAuditEvent, "Computer-use audit is unavailable")

    async def list_lifecycle_events(
        self,
        owner: ActionOwner,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ComputerUseLifecycleEvent]:
        response = await self._client.lifecycle_events(
            owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return self._validated_list(response, "events", ComputerUseLifecycleEvent, "Action lifecycle storage is unavailable")

    async def get_action(self, action_id: str, owner: ActionOwner) -> ActionLifecycleRecord:
        response = await self._client.action(action_id, owner)
        return self._validated_record(response, ActionLifecycleRecord, "action_not_found")

    async def wait_lifecycle_events(
        self,
        owner: ActionOwner,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        timeout_seconds: float = 20,
    ) -> list[ComputerUseLifecycleEvent]:
        response = await self._client.wait_lifecycle_events(
            owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
        return self._validated_list(response, "events", ComputerUseLifecycleEvent, "Action lifecycle storage is unavailable")

    async def list_pending_approvals(self, owner: ActionOwner) -> list[ApprovalRecord]:
        return self._validated_list(await self._client.approvals(owner), "approvals", ApprovalRecord, "Action lifecycle storage is unavailable")

    async def get_approval(self, approval_id: str, owner: ActionOwner) -> ApprovalRecord:
        return self._operation_record(await self._client.approval(approval_id, owner), ApprovalRecord)

    async def decide_approval(self, approval_id: str, owner: ActionOwner, *, approved: bool, decided_by: str, reason: str | None = None) -> ApprovalRecord:
        response = await self._client.decide_approval(approval_id, owner, approved=approved, decided_by=decided_by, reason=reason)
        return self._operation_record(response, ApprovalRecord)

    async def submit_action(self, action, owner: ActionOwner) -> ActionLifecycleRecord:
        return self._operation_record(await self._client.submit_action(action, owner), ActionLifecycleRecord)

    async def cancel_action(self, action_id: str, owner: ActionOwner, *, reason: str | None = None) -> ActionLifecycleRecord:
        return self._operation_record(await self._client.cancel_action(action_id, owner, reason=reason), ActionLifecycleRecord)

    async def execute_action(self, action_id: str, owner: ActionOwner) -> ActionLifecycleRecord:
        return self._operation_record(await self._client.execute_action(action_id, owner), ActionLifecycleRecord)

    async def create_session(self, session_id: str | None = None) -> DesktopSessionSnapshot:
        return self._operation_record(await self._client.create_session(session_id), DesktopSessionSnapshot)

    async def stop_session(self, session_id: str) -> DesktopSessionSnapshot:
        return self._operation_record(await self._client.stop_session(session_id), DesktopSessionSnapshot)

    async def delete_session(self, session_id: str) -> None:
        self._operation_record(await self._client.delete_session(session_id), None)

    async def observe_session(
        self,
        session_id: str,
        *,
        owner: ActionOwner | None = None,
        browser_session_id: str | None = None,
    ) -> Observation:
        return self._operation_record(
            await self._client.observe_session(session_id, owner=owner, browser_session_id=browser_session_id),
            Observation,
        )

    async def resolve_target(
        self,
        session_id: str,
        query: TargetQuery,
        *,
        owner: ActionOwner | None = None,
        browser_session_id: str | None = None,
    ) -> TargetResolutionResult:
        return self._operation_record(
            await self._client.resolve_target(session_id, query, owner=owner, browser_session_id=browser_session_id),
            TargetResolutionResult,
        )

    async def observation_blob_export_info(self, session_id: str, observation_id: str, blob_id: str, owner: ActionOwner) -> BlobRef:
        return self._operation_record(await self._client.observation_blob_export_info(session_id, observation_id, blob_id, owner), BlobRef)

    async def export_observation_blob(self, session_id: str, observation_id: str, blob_id: str, owner: ActionOwner) -> tuple[BlobRef, bytes]:
        try:
            return await self._client.export_observation_blob(session_id, observation_id, blob_id, owner)
        except RuntimeError as exc:
            code = str(exc)
            if code == "ipc_unavailable":
                raise RemoteHostUnavailableError("Dedicated Windows host is unavailable") from exc
            raise RemoteHostOperationError(code) from exc

    async def browser_health(self) -> BrowserHealth:
        return self._operation_record(await self._client.browser_health(), BrowserHealth)

    async def create_browser_session(self, url: str, owner: ActionOwner) -> BrowserStateSummary:
        return self._operation_record(await self._client.create_browser_session(url, owner), BrowserStateSummary)

    async def list_browser_sessions(self, owner: ActionOwner) -> list[str]:
        response = await self._client.list_browser_sessions(owner)
        if not response.succeeded or response.result is None:
            if response.error_code == "ipc_unavailable":
                raise RemoteHostUnavailableError("Dedicated Windows host is unavailable")
            raise RemoteHostOperationError(response.error_code or "remote_operation_failed")
        try:
            sessions = response.result["sessions"]
            if not isinstance(sessions, list) or not all(isinstance(item, str) for item in sessions):
                raise TypeError("invalid browser sessions payload")
            return sessions
        except Exception as exc:
            raise RemoteHostUnavailableError("Dedicated Windows host returned an invalid response") from exc

    async def close_browser_session(self, browser_session_id: str, owner: ActionOwner) -> None:
        self._operation_record(await self._client.close_browser_session(browser_session_id, owner), None)

    async def emergency_stop(self) -> EmergencyStopSnapshot:
        return self._operation_record(await self._client.emergency_stop(), EmergencyStopSnapshot)

    async def engage_emergency_stop(self, reason: str) -> EmergencyStopSnapshot:
        return self._operation_record(await self._client.engage_emergency_stop(reason), EmergencyStopSnapshot)

    async def reset_emergency_stop(self, reason: str) -> EmergencyStopSnapshot:
        return self._operation_record(await self._client.reset_emergency_stop(reason), EmergencyStopSnapshot)

    @staticmethod
    def _validated_list(response, field: str, model_type, message: str):
        if not response.succeeded or response.result is None:
            raise RemoteHostUnavailableError(message)
        try:
            return [model_type.model_validate(item) for item in response.result[field]]
        except Exception as exc:
            raise RemoteHostUnavailableError(message) from exc

    @staticmethod
    def _validated_record(response, model_type, not_found_code: str):
        if response.error_code == not_found_code:
            raise RemoteLifecycleRecordNotFoundError("Lifecycle record not found")
        if not response.succeeded or response.result is None:
            raise RemoteHostUnavailableError("Action lifecycle storage is unavailable")
        try:
            return model_type.model_validate(response.result)
        except Exception as exc:
            raise RemoteHostUnavailableError("Action lifecycle storage is unavailable") from exc

    @staticmethod
    def _operation_record(response, model_type):
        if not response.succeeded or response.result is None:
            if response.error_code == "ipc_unavailable":
                raise RemoteHostUnavailableError("Dedicated Windows host is unavailable")
            raise RemoteHostOperationError(response.error_code or "remote_operation_failed")
        if model_type is None:
            return None
        try:
            return model_type.model_validate(response.result)
        except Exception as exc:
            raise RemoteHostUnavailableError("Dedicated Windows host returned an invalid response") from exc
