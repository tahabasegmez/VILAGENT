"""Authenticated control-plane contracts for a future dedicated host process."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vilagent.computer_use.action_store import (
    ActionNotFoundError,
    ActionOwnershipError,
    ActionStoreError,
    ActionStorePersistenceError,
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    InvalidActionTransitionError,
    SessionOwnershipError,
)
from vilagent.computer_use.browser import (
    BrowserHealth,
    BrowserPolicyError,
    BrowserSessionOwnershipError,
    BrowserUnavailableError,
)
from vilagent.computer_use.models import (
    ActionCommand,
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
from vilagent.computer_use.session import DesktopSessionNotFoundError, DesktopSessionStoppedError, LatestObservationUnavailableError
from vilagent.computer_use.observation_store import BlobExportDeniedError, BlobNotFoundError, ObservationNotFoundError

DEFAULT_MAX_IPC_MESSAGE_BYTES = 64 * 1024
DEFAULT_IPC_TIMEOUT_SECONDS = 5.0


def utc_now() -> datetime:
    return datetime.now(UTC)


class HostProcessStatus(StrEnum):
    starting = "starting"
    healthy = "healthy"
    stale = "stale"
    stopped = "stopped"


class HostHeartbeatSnapshot(BaseModel):
    status: HostProcessStatus
    last_heartbeat_at: datetime | None = None
    stale_after_seconds: float = Field(gt=0)
    checked_at: datetime = Field(default_factory=utc_now)


class HostHeartbeatState:
    """Track liveness independently from the IPC transport implementation."""

    def __init__(self, *, stale_after_seconds: float = 10, clock: Callable[[], datetime] = utc_now):
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._stale_after_seconds = stale_after_seconds
        self._clock = clock
        self._last_heartbeat_at: datetime | None = None
        self._stopped = False
        self._lock = asyncio.Lock()

    async def beat(self) -> HostHeartbeatSnapshot:
        async with self._lock:
            self._stopped = False
            self._last_heartbeat_at = self._clock()
            return self._snapshot()

    async def stop(self) -> HostHeartbeatSnapshot:
        async with self._lock:
            self._stopped = True
            return self._snapshot()

    async def snapshot(self) -> HostHeartbeatSnapshot:
        async with self._lock:
            return self._snapshot()

    def _snapshot(self) -> HostHeartbeatSnapshot:
        checked_at = self._clock()
        if self._stopped:
            status = HostProcessStatus.stopped
        elif self._last_heartbeat_at is None:
            status = HostProcessStatus.starting
        elif checked_at - self._last_heartbeat_at > timedelta(seconds=self._stale_after_seconds):
            status = HostProcessStatus.stale
        else:
            status = HostProcessStatus.healthy
        return HostHeartbeatSnapshot(
            status=status,
            last_heartbeat_at=self._last_heartbeat_at,
            stale_after_seconds=self._stale_after_seconds,
            checked_at=checked_at,
        )


class HostIpcOperation(StrEnum):
    heartbeat = "heartbeat"
    health = "health"
    sessions_list = "sessions_list"
    session_get = "session_get"
    uia_windows = "uia_windows"
    uia_find = "uia_find"
    audit_list = "audit_list"
    lifecycle_events_list = "lifecycle_events_list"
    lifecycle_events_wait = "lifecycle_events_wait"
    action_get = "action_get"
    approvals_reconcile_list = "approvals_reconcile_list"
    approval_reconcile_get = "approval_reconcile_get"
    approval_decide = "approval_decide"
    action_submit = "action_submit"
    action_cancel = "action_cancel"
    action_execute = "action_execute"
    session_create = "session_create"
    session_stop = "session_stop"
    session_delete = "session_delete"
    session_observe = "session_observe"
    target_resolve = "target_resolve"
    observation_blob_export_info = "observation_blob_export_info"
    browser_health = "browser_health"
    browser_session_create = "browser_session_create"
    browser_sessions_list = "browser_sessions_list"
    browser_session_close = "browser_session_close"
    emergency_stop_get = "emergency_stop_get"
    emergency_stop_engage = "emergency_stop_engage"
    emergency_stop_reset = "emergency_stop_reset"


class HostIpcRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    operation: HostIpcOperation
    token: str = Field(min_length=1, max_length=4096)
    session_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    uia_query: UIAQuery | None = None
    owner: ActionOwner | None = None
    action_id: str | None = Field(default=None, min_length=1, max_length=200)
    approval_id: str | None = Field(default=None, min_length=1, max_length=200)
    observation_id: str | None = Field(default=None, min_length=1, max_length=200)
    blob_id: str | None = Field(default=None, min_length=1, max_length=200)
    browser_session_id: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    action: ActionCommand | None = None
    target_query: TargetQuery | None = None
    approved: bool | None = None
    decided_by: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)
    after_sequence: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1, le=500)
    timeout_seconds: float | None = Field(default=None, gt=0, le=30)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_operation_fields(self) -> HostIpcRequest:
        required_session = {
            HostIpcOperation.session_get,
            HostIpcOperation.audit_list,
            HostIpcOperation.session_stop,
            HostIpcOperation.session_delete,
            HostIpcOperation.session_observe,
            HostIpcOperation.target_resolve,
            HostIpcOperation.observation_blob_export_info,
        }
        optional_session = {HostIpcOperation.lifecycle_events_list, HostIpcOperation.lifecycle_events_wait, HostIpcOperation.session_create}
        if self.operation in required_session and self.session_id is None:
            raise ValueError("operation requires session_id")
        if self.operation not in required_session | optional_session and self.session_id is not None:
            raise ValueError("operation forbids session_id")
        if (self.operation == HostIpcOperation.uia_find) != (self.uia_query is not None):
            raise ValueError("uia_find requires uia_query and other operations forbid it")
        owner_operations = {
            HostIpcOperation.lifecycle_events_list,
            HostIpcOperation.lifecycle_events_wait,
            HostIpcOperation.action_get,
            HostIpcOperation.approvals_reconcile_list,
            HostIpcOperation.approval_reconcile_get,
            HostIpcOperation.approval_decide,
            HostIpcOperation.action_submit,
            HostIpcOperation.action_cancel,
            HostIpcOperation.action_execute,
            HostIpcOperation.observation_blob_export_info,
            HostIpcOperation.browser_session_create,
            HostIpcOperation.browser_sessions_list,
            HostIpcOperation.browser_session_close,
        }
        owner_allowed_operations = owner_operations | {HostIpcOperation.session_observe, HostIpcOperation.target_resolve}
        if self.operation in owner_operations and self.owner is None:
            raise ValueError("operation requires owner")
        if self.owner is not None and self.operation not in owner_allowed_operations:
            raise ValueError("operation requires owner or forbids it")
        action_id_operations = {HostIpcOperation.action_get, HostIpcOperation.action_cancel, HostIpcOperation.action_execute}
        if (self.operation in action_id_operations) != (self.action_id is not None):
            raise ValueError("operation requires action_id or forbids it")
        approval_id_operations = {HostIpcOperation.approval_reconcile_get, HostIpcOperation.approval_decide}
        if (self.operation in approval_id_operations) != (self.approval_id is not None):
            raise ValueError("operation requires approval_id or forbids it")
        if (self.operation == HostIpcOperation.action_submit) != (self.action is not None):
            raise ValueError("action_submit requires action and other operations forbid it")
        if (self.operation == HostIpcOperation.target_resolve) != (self.target_query is not None):
            raise ValueError("target_resolve requires target_query and other operations forbid it")
        if self.operation == HostIpcOperation.observation_blob_export_info:
            if self.observation_id is None or self.blob_id is None:
                raise ValueError("observation_blob_export_info requires observation_id and blob_id")
        elif self.observation_id is not None or self.blob_id is not None:
            raise ValueError("operation forbids observation_id and blob_id")
        if (self.operation == HostIpcOperation.browser_session_create) != (self.url is not None):
            raise ValueError("browser_session_create requires url and other operations forbid it")
        if self.operation == HostIpcOperation.browser_session_close:
            if self.browser_session_id is None:
                raise ValueError("browser_session_close requires browser_session_id")
        elif self.operation in {HostIpcOperation.session_observe, HostIpcOperation.target_resolve}:
            if self.browser_session_id is not None and self.owner is None:
                raise ValueError("browser_session_id requires owner")
        elif self.browser_session_id is not None:
            raise ValueError("operation forbids browser_session_id")
        if self.operation == HostIpcOperation.approval_decide:
            if self.approved is None or self.decided_by is None:
                raise ValueError("approval_decide requires approved and decided_by")
        elif self.approved is not None or self.decided_by is not None:
            raise ValueError("operation forbids approval decision fields")
        reason_operations = {
            HostIpcOperation.approval_decide,
            HostIpcOperation.action_cancel,
            HostIpcOperation.emergency_stop_engage,
            HostIpcOperation.emergency_stop_reset,
        }
        if self.reason is not None and self.operation not in reason_operations:
            raise ValueError("operation forbids reason")
        if self.operation in {HostIpcOperation.lifecycle_events_list, HostIpcOperation.lifecycle_events_wait}:
            self.after_sequence = self.after_sequence if self.after_sequence is not None else 0
            self.limit = self.limit if self.limit is not None else 100
        elif self.after_sequence is not None or self.limit is not None:
            raise ValueError("operation forbids lifecycle event pagination")
        if (self.operation == HostIpcOperation.lifecycle_events_wait) != (self.timeout_seconds is not None):
            raise ValueError("lifecycle_events_wait requires timeout_seconds and other operations forbid it")
        return self


class HostIpcResponse(BaseModel):
    request_id: str | None = None
    succeeded: bool
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    model_config = ConfigDict(extra="forbid")


class AuthenticatedHostControlDispatcher:
    """Dispatch a minimal authenticated read-only host control protocol."""

    def __init__(
        self,
        *,
        token: str,
        heartbeat: HostHeartbeatState,
        health_provider: Callable[[], Awaitable[ComputerUseHostHealth]],
        sessions_provider: Callable[[], Awaitable[list[DesktopSessionSnapshot]]] | None = None,
        session_provider: Callable[[str], Awaitable[DesktopSessionSnapshot]] | None = None,
        uia_windows_provider: Callable[[], Awaitable[list[WindowRef]]] | None = None,
        uia_find_provider: Callable[[UIAQuery], Awaitable[list[UIAElementRef]]] | None = None,
        audit_provider: Callable[[str], Awaitable[list[ComputerUseAuditEvent]]] | None = None,
        lifecycle_events_provider: Callable[[ActionOwner, str | None, int, int], Awaitable[list[ComputerUseLifecycleEvent]]] | None = None,
        lifecycle_events_wait_provider: Callable[[ActionOwner, str | None, int, int, float], Awaitable[list[ComputerUseLifecycleEvent]]] | None = None,
        action_provider: Callable[[str, ActionOwner], Awaitable[ActionLifecycleRecord]] | None = None,
        approvals_provider: Callable[[ActionOwner], Awaitable[list[ApprovalRecord]]] | None = None,
        approval_provider: Callable[[str, ActionOwner], Awaitable[ApprovalRecord]] | None = None,
        approval_decision_provider: Callable[[str, ActionOwner, bool, str, str | None], Awaitable[ApprovalRecord]] | None = None,
        action_submit_provider: Callable[[ActionCommand, ActionOwner], Awaitable[ActionLifecycleRecord]] | None = None,
        action_cancel_provider: Callable[[str, ActionOwner, str | None], Awaitable[ActionLifecycleRecord]] | None = None,
        action_execute_provider: Callable[[str, ActionOwner], Awaitable[ActionLifecycleRecord]] | None = None,
        session_create_provider: Callable[[str | None], Awaitable[DesktopSessionSnapshot]] | None = None,
        session_stop_provider: Callable[[str], Awaitable[DesktopSessionSnapshot]] | None = None,
        session_delete_provider: Callable[[str], Awaitable[None]] | None = None,
        observation_provider: Callable[[str, ActionOwner | None, str | None], Awaitable[Observation]] | None = None,
        target_provider: Callable[[str, TargetQuery, ActionOwner | None, str | None], Awaitable[TargetResolutionResult]] | None = None,
        blob_export_info_provider: Callable[[str, str, str, ActionOwner], Awaitable[BlobRef]] | None = None,
        blob_export_provider: Callable[[str, str, str, ActionOwner], Awaitable[tuple[BlobRef, bytes]]] | None = None,
        browser_health_provider: Callable[[], Awaitable[BrowserHealth]] | None = None,
        browser_session_create_provider: Callable[[str, ActionOwner], Awaitable[BrowserStateSummary]] | None = None,
        browser_sessions_provider: Callable[[ActionOwner], Awaitable[list[str]]] | None = None,
        browser_session_close_provider: Callable[[str, ActionOwner], Awaitable[None]] | None = None,
        emergency_stop_provider: Callable[[], Awaitable[EmergencyStopSnapshot]] | None = None,
        emergency_stop_engage_provider: Callable[[str], Awaitable[None]] | None = None,
        emergency_stop_reset_provider: Callable[[str], Awaitable[None]] | None = None,
        max_message_bytes: int = DEFAULT_MAX_IPC_MESSAGE_BYTES,
    ):
        if not token:
            raise ValueError("IPC token must not be empty")
        if max_message_bytes < 1:
            raise ValueError("max_message_bytes must be positive")
        self._token = token
        self._heartbeat = heartbeat
        self._health_provider = health_provider
        self._sessions_provider = sessions_provider
        self._session_provider = session_provider
        self._uia_windows_provider = uia_windows_provider
        self._uia_find_provider = uia_find_provider
        self._audit_provider = audit_provider
        self._lifecycle_events_provider = lifecycle_events_provider
        self._lifecycle_events_wait_provider = lifecycle_events_wait_provider
        self._action_provider = action_provider
        self._approvals_provider = approvals_provider
        self._approval_provider = approval_provider
        self._approval_decision_provider = approval_decision_provider
        self._action_submit_provider = action_submit_provider
        self._action_cancel_provider = action_cancel_provider
        self._action_execute_provider = action_execute_provider
        self._session_create_provider = session_create_provider
        self._session_stop_provider = session_stop_provider
        self._session_delete_provider = session_delete_provider
        self._observation_provider = observation_provider
        self._target_provider = target_provider
        self._blob_export_info_provider = blob_export_info_provider
        self._blob_export_provider = blob_export_provider
        self._browser_health_provider = browser_health_provider
        self._browser_session_create_provider = browser_session_create_provider
        self._browser_sessions_provider = browser_sessions_provider
        self._browser_session_close_provider = browser_session_close_provider
        self._emergency_stop_provider = emergency_stop_provider
        self._emergency_stop_engage_provider = emergency_stop_engage_provider
        self._emergency_stop_reset_provider = emergency_stop_reset_provider
        self._max_message_bytes = max_message_bytes

    async def dispatch(self, payload: bytes) -> bytes:
        if len(payload) > self._max_message_bytes:
            return self._encode(self._error(None, "ipc_message_too_large", "IPC message exceeds the configured limit."))
        try:
            request = HostIpcRequest.model_validate_json(payload)
        except (ValidationError, ValueError, json.JSONDecodeError):
            return self._encode(self._error(None, "invalid_ipc_request", "IPC request is invalid."))
        if not secrets.compare_digest(request.token, self._token):
            return self._encode(self._error(request.request_id, "ipc_auth_failed", "IPC authentication failed."))

        if request.operation == HostIpcOperation.heartbeat:
            snapshot = await self._heartbeat.beat()
            return self._encode(self._success(request.request_id, snapshot.model_dump(mode="json")))
        if request.operation == HostIpcOperation.health:
            health = await self._health_provider()
            return self._encode(self._success(request.request_id, health.model_dump(mode="json")))
        if request.operation == HostIpcOperation.sessions_list and self._sessions_provider is not None:
            sessions = await self._sessions_provider()
            return self._encode(self._success(request.request_id, {"sessions": [session.model_dump(mode="json") for session in sessions]}))
        if request.operation == HostIpcOperation.session_get and self._session_provider is not None and request.session_id is not None:
            try:
                session = await self._session_provider(request.session_id)
            except Exception:
                return self._encode(self._error(request.request_id, "session_not_found", "Desktop session was not found."))
            return self._encode(self._success(request.request_id, session.model_dump(mode="json")))
        if request.operation == HostIpcOperation.uia_windows and self._uia_windows_provider is not None:
            try:
                windows = await self._uia_windows_provider()
            except Exception:
                return self._encode(self._error(request.request_id, "uia_unavailable", "Windows UI Automation is unavailable."))
            return self._encode(self._success(request.request_id, {"windows": [window.model_dump(mode="json") for window in windows]}))
        if request.operation == HostIpcOperation.uia_find and self._uia_find_provider is not None and request.uia_query is not None:
            try:
                elements = await self._uia_find_provider(request.uia_query)
            except Exception:
                return self._encode(self._error(request.request_id, "uia_unavailable", "Windows UI Automation is unavailable."))
            return self._encode(self._success(request.request_id, {"elements": [element.model_dump(mode="json") for element in elements]}))
        if request.operation == HostIpcOperation.audit_list and self._audit_provider is not None and request.session_id is not None:
            try:
                events = await self._audit_provider(request.session_id)
            except Exception:
                return self._encode(self._error(request.request_id, "audit_unavailable", "Computer-use audit is unavailable."))
            return self._encode(self._success(request.request_id, {"events": [event.model_dump(mode="json") for event in events]}))
        if request.operation == HostIpcOperation.lifecycle_events_list and self._lifecycle_events_provider is not None and request.owner is not None:
            try:
                events = await self._lifecycle_events_provider(
                    request.owner,
                    request.session_id,
                    request.after_sequence or 0,
                    request.limit or 100,
                )
            except Exception:
                return self._encode(self._error(request.request_id, "lifecycle_unavailable", "Action lifecycle storage is unavailable."))
            return self._encode(self._success(request.request_id, {"events": [event.model_dump(mode="json") for event in events]}))
        if (
            request.operation == HostIpcOperation.lifecycle_events_wait
            and self._lifecycle_events_wait_provider is not None
            and request.owner is not None
            and request.timeout_seconds is not None
        ):
            try:
                events = await self._lifecycle_events_wait_provider(
                    request.owner,
                    request.session_id,
                    request.after_sequence or 0,
                    request.limit or 100,
                    request.timeout_seconds,
                )
            except Exception:
                return self._encode(self._error(request.request_id, "lifecycle_unavailable", "Action lifecycle storage is unavailable."))
            return self._encode(self._success(request.request_id, {"events": [event.model_dump(mode="json") for event in events]}))
        if request.operation == HostIpcOperation.action_get and self._action_provider is not None and request.owner is not None and request.action_id is not None:
            try:
                action = await self._action_provider(request.action_id, request.owner)
            except (ActionNotFoundError, ActionOwnershipError):
                return self._encode(self._error(request.request_id, "action_not_found", "Action was not found."))
            except Exception:
                return self._encode(self._error(request.request_id, "lifecycle_unavailable", "Action lifecycle storage is unavailable."))
            return self._encode(self._success(request.request_id, action.model_dump(mode="json")))
        if request.operation == HostIpcOperation.approvals_reconcile_list and self._approvals_provider is not None and request.owner is not None:
            try:
                approvals = await self._approvals_provider(request.owner)
            except Exception:
                return self._lifecycle_error(request.request_id)
            return self._encode(self._success(request.request_id, {"approvals": [approval.model_dump(mode="json") for approval in approvals]}))
        if request.operation == HostIpcOperation.approval_reconcile_get and self._approval_provider is not None and request.owner is not None and request.approval_id is not None:
            try:
                approval = await self._approval_provider(request.approval_id, request.owner)
            except (ApprovalNotFoundError, ActionOwnershipError):
                return self._encode(self._error(request.request_id, "approval_not_found", "Approval request was not found."))
            except Exception:
                return self._lifecycle_error(request.request_id)
            return self._encode(self._success(request.request_id, approval.model_dump(mode="json")))
        if (
            request.operation == HostIpcOperation.approval_decide
            and self._approval_decision_provider is not None
            and request.owner is not None
            and request.approval_id is not None
            and request.approved is not None
            and request.decided_by is not None
        ):
            try:
                approval = await self._approval_decision_provider(
                    request.approval_id,
                    request.owner,
                    request.approved,
                    request.decided_by,
                    request.reason,
                )
            except (ApprovalNotFoundError, ActionOwnershipError):
                return self._encode(self._error(request.request_id, "approval_not_found", "Approval request was not found."))
            except (ApprovalAlreadyDecidedError, ApprovalExpiredError):
                return self._encode(self._error(request.request_id, "approval_conflict", "Approval request cannot be decided."))
            except Exception:
                return self._lifecycle_error(request.request_id)
            return self._encode(self._success(request.request_id, approval.model_dump(mode="json")))
        if request.operation == HostIpcOperation.action_submit and self._action_submit_provider is not None and request.owner is not None and request.action is not None:
            try:
                action = await self._action_submit_provider(request.action, request.owner)
            except DesktopSessionNotFoundError:
                return self._encode(self._error(request.request_id, "session_not_found", "Desktop session was not found."))
            except ActionStorePersistenceError:
                return self._lifecycle_error(request.request_id)
            except SessionOwnershipError:
                return self._encode(self._error(request.request_id, "session_owner_conflict", "Desktop session belongs to another action owner."))
            except ActionStoreError:
                return self._encode(self._error(request.request_id, "action_conflict", "Action submission conflicts with stored state."))
            return self._encode(self._success(request.request_id, action.model_dump(mode="json")))
        if request.operation == HostIpcOperation.action_cancel and self._action_cancel_provider is not None and request.owner is not None and request.action_id is not None:
            try:
                action = await self._action_cancel_provider(request.action_id, request.owner, request.reason)
            except (ActionNotFoundError, ActionOwnershipError):
                return self._encode(self._error(request.request_id, "action_not_found", "Action was not found."))
            except InvalidActionTransitionError:
                return self._encode(self._error(request.request_id, "invalid_transition", "Action transition is invalid."))
            except Exception:
                return self._lifecycle_error(request.request_id)
            return self._encode(self._success(request.request_id, action.model_dump(mode="json")))
        if request.operation == HostIpcOperation.action_execute and self._action_execute_provider is not None and request.owner is not None and request.action_id is not None:
            try:
                action = await self._action_execute_provider(request.action_id, request.owner)
            except (ActionNotFoundError, ActionOwnershipError):
                return self._encode(self._error(request.request_id, "action_not_found", "Action was not found."))
            except InvalidActionTransitionError:
                return self._encode(self._error(request.request_id, "invalid_transition", "Action transition is invalid."))
            except Exception:
                return self._lifecycle_error(request.request_id)
            return self._encode(self._success(request.request_id, action.model_dump(mode="json")))
        if request.operation == HostIpcOperation.session_create and self._session_create_provider is not None:
            try:
                session = await self._session_create_provider(request.session_id)
            except ValueError:
                return self._encode(self._error(request.request_id, "session_conflict", "Desktop session already exists."))
            return self._encode(self._success(request.request_id, session.model_dump(mode="json")))
        if request.operation == HostIpcOperation.session_stop and self._session_stop_provider is not None and request.session_id is not None:
            try:
                session = await self._session_stop_provider(request.session_id)
            except DesktopSessionNotFoundError:
                return self._encode(self._error(request.request_id, "session_not_found", "Desktop session was not found."))
            return self._encode(self._success(request.request_id, session.model_dump(mode="json")))
        if request.operation == HostIpcOperation.session_delete and self._session_delete_provider is not None and request.session_id is not None:
            try:
                await self._session_delete_provider(request.session_id)
            except DesktopSessionNotFoundError:
                return self._encode(self._error(request.request_id, "session_not_found", "Desktop session was not found."))
            return self._encode(self._success(request.request_id, {}))
        if request.operation == HostIpcOperation.session_observe and self._observation_provider is not None and request.session_id is not None:
            try:
                observation = await self._observation_provider(request.session_id, request.owner, request.browser_session_id)
            except DesktopSessionNotFoundError:
                return self._encode(self._error(request.request_id, "session_not_found", "Desktop session was not found."))
            except DesktopSessionStoppedError:
                return self._encode(self._error(request.request_id, "session_stopped", "Desktop session is stopped."))
            except BrowserSessionOwnershipError:
                return self._encode(self._error(request.request_id, "browser_session_not_found", "Browser session was not found."))
            except BrowserUnavailableError:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            except Exception as exc:
                reason_code = getattr(exc, "reason_code", None)
                error_code = (
                    reason_code
                    if isinstance(reason_code, str)
                    and (reason_code.startswith("screen_capture_") or reason_code == "redaction_unavailable")
                    else "observation_unavailable"
                )
                return self._encode(self._error(request.request_id, error_code, "Desktop observation is unavailable."))
            return self._encode(self._success(request.request_id, observation.model_dump(mode="json")))
        if request.operation == HostIpcOperation.target_resolve and self._target_provider is not None and request.session_id is not None and request.target_query is not None:
            try:
                target = await self._target_provider(request.session_id, request.target_query, request.owner, request.browser_session_id)
            except DesktopSessionNotFoundError:
                return self._encode(self._error(request.request_id, "session_not_found", "Desktop session was not found."))
            except LatestObservationUnavailableError:
                return self._encode(self._error(request.request_id, "observation_missing", "Desktop session has no observation."))
            except BrowserSessionOwnershipError:
                return self._encode(self._error(request.request_id, "browser_session_not_found", "Browser session was not found."))
            except BrowserUnavailableError:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            except Exception:
                return self._encode(self._error(request.request_id, "target_unavailable", "Target resolution is unavailable."))
            return self._encode(self._success(request.request_id, target.model_dump(mode="json")))
        if (
            request.operation == HostIpcOperation.observation_blob_export_info
            and self._blob_export_info_provider is not None
            and request.session_id is not None
            and request.observation_id is not None
            and request.blob_id is not None
            and request.owner is not None
        ):
            try:
                blob = await self._blob_export_info_provider(request.session_id, request.observation_id, request.blob_id, request.owner)
            except (DesktopSessionNotFoundError, ObservationNotFoundError, BlobNotFoundError, BlobExportDeniedError, ActionOwnershipError):
                return self._encode(self._error(request.request_id, "blob_not_found", "Observation blob was not found."))
            except Exception:
                return self._encode(self._error(request.request_id, "blob_unavailable", "Observation blob is unavailable."))
            return self._encode(self._success(request.request_id, blob.model_dump(mode="json")))
        if request.operation == HostIpcOperation.browser_health and self._browser_health_provider is not None:
            health = await self._browser_health_provider()
            return self._encode(self._success(request.request_id, health.model_dump(mode="json")))
        if (
            request.operation == HostIpcOperation.browser_session_create
            and self._browser_session_create_provider is not None
            and request.url is not None
            and request.owner is not None
        ):
            try:
                state = await self._browser_session_create_provider(request.url, request.owner)
            except BrowserPolicyError:
                return self._encode(self._error(request.request_id, "browser_policy_denied", "Browser URL is not allowed."))
            except BrowserUnavailableError:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            except Exception:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            return self._encode(self._success(request.request_id, state.model_dump(mode="json")))
        if request.operation == HostIpcOperation.browser_sessions_list and self._browser_sessions_provider is not None and request.owner is not None:
            try:
                sessions = await self._browser_sessions_provider(request.owner)
            except BrowserUnavailableError:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            except Exception:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            return self._encode(self._success(request.request_id, {"sessions": sessions}))
        if (
            request.operation == HostIpcOperation.browser_session_close
            and self._browser_session_close_provider is not None
            and request.browser_session_id is not None
            and request.owner is not None
        ):
            try:
                await self._browser_session_close_provider(request.browser_session_id, request.owner)
            except BrowserSessionOwnershipError:
                return self._encode(self._error(request.request_id, "browser_session_not_found", "Browser session was not found."))
            except BrowserUnavailableError:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            except Exception:
                return self._encode(self._error(request.request_id, "browser_unavailable", "Browser runtime is unavailable."))
            return self._encode(self._success(request.request_id, {}))
        if request.operation == HostIpcOperation.emergency_stop_get and self._emergency_stop_provider is not None:
            stop = await self._emergency_stop_provider()
            return self._encode(self._success(request.request_id, stop.model_dump(mode="json")))
        if request.operation == HostIpcOperation.emergency_stop_engage and self._emergency_stop_engage_provider is not None:
            try:
                await self._emergency_stop_engage_provider(request.reason or "Operator emergency stop")
            except Exception:
                return self._encode(self._error(request.request_id, "emergency_stop_unavailable", "Emergency stop is unavailable."))
            return self._encode(self._success(request.request_id, {"engaged": True, "reason": request.reason or "Operator emergency stop"}))
        if request.operation == HostIpcOperation.emergency_stop_reset and self._emergency_stop_reset_provider is not None:
            try:
                await self._emergency_stop_reset_provider(request.reason or "Operator reset")
            except Exception:
                return self._encode(self._error(request.request_id, "emergency_stop_reset_failed", "Emergency-stop reset failed; host remains stopped."))
            return self._encode(self._success(request.request_id, {"engaged": False, "reason": None}))
        return self._encode(self._error(request.request_id, "unsupported_ipc_operation", "IPC operation is unsupported."))

    async def dispatch_blob_stream(self, payload: bytes) -> tuple[HostIpcResponse, bytes | None]:
        try:
            request = HostIpcRequest.model_validate_json(payload)
            if not secrets.compare_digest(request.token, self._token):
                return self._error(request.request_id, "ipc_auth_failed", "IPC authentication failed."), None
            if request.operation != HostIpcOperation.observation_blob_export_info or self._blob_export_provider is None:
                return self._error(request.request_id, "unsupported_ipc_operation", "IPC operation is unsupported."), None
            blob, data = await self._blob_export_provider(request.session_id, request.observation_id, request.blob_id, request.owner)
            return self._success(request.request_id, blob.model_dump(mode="json")), data
        except (DesktopSessionNotFoundError, ObservationNotFoundError, BlobNotFoundError, BlobExportDeniedError, ActionOwnershipError):
            return self._error(None, "blob_not_found", "Observation blob was not found."), None
        except Exception:
            return self._error(None, "blob_unavailable", "Observation blob is unavailable."), None

    @staticmethod
    def _success(request_id: str, result: dict[str, Any]) -> HostIpcResponse:
        return HostIpcResponse(request_id=request_id, succeeded=True, result=result)

    @staticmethod
    def _error(request_id: str | None, code: str, message: str) -> HostIpcResponse:
        return HostIpcResponse(request_id=request_id, succeeded=False, error_code=code, error_message=message)

    def _lifecycle_error(self, request_id: str) -> bytes:
        return self._encode(self._error(request_id, "lifecycle_unavailable", "Action lifecycle storage is unavailable."))

    @staticmethod
    def _encode(response: HostIpcResponse) -> bytes:
        return response.model_dump_json().encode("utf-8")


class LocalHostIpcServer:
    """Serve the control dispatcher on an ephemeral IPv4 loopback port."""

    def __init__(
        self,
        dispatcher: AuthenticatedHostControlDispatcher,
        *,
        max_message_bytes: int = DEFAULT_MAX_IPC_MESSAGE_BYTES,
    ):
        self._dispatcher = dispatcher
        self._max_message_bytes = max_message_bytes
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> int:
        if self._server is None:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host="127.0.0.1",
                port=0,
                limit=self._max_message_bytes + 1,
            )
        if self.port is None:
            raise RuntimeError("Local host IPC server did not bind a loopback port")
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            if not isinstance(peer, tuple) or peer[0] != "127.0.0.1":
                response = HostIpcResponse(succeeded=False, error_code="ipc_peer_rejected", error_message="IPC peer is not allowed.")
            else:
                try:
                    payload = await reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    response = HostIpcResponse(succeeded=False, error_code="invalid_ipc_request", error_message="IPC request is invalid.")
                else:
                    request_payload = payload.rstrip(b"\r\n")
                    try:
                        request = HostIpcRequest.model_validate_json(request_payload)
                    except Exception:
                        request = None
                    if request is not None and request.operation == HostIpcOperation.observation_blob_export_info:
                        response, blob_data = await self._dispatcher.dispatch_blob_stream(request_payload)
                    else:
                        encoded = await self._dispatcher.dispatch(request_payload)
                        response = HostIpcResponse.model_validate_json(encoded)
                        blob_data = None
            writer.write(response.model_dump_json().encode("utf-8") + b"\n")
            if response.succeeded and blob_data is not None:
                writer.write(blob_data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


class LocalHostIpcClient:
    """Call the loopback-only host control protocol with bounded timeouts."""

    def __init__(
        self,
        *,
        port: int,
        token: str,
        timeout_seconds: float = DEFAULT_IPC_TIMEOUT_SECONDS,
        execution_timeout_seconds: float = 1801,
        max_message_bytes: int = DEFAULT_MAX_IPC_MESSAGE_BYTES,
    ):
        if not 1 <= port <= 65535:
            raise ValueError("IPC port must be between 1 and 65535")
        if not token:
            raise ValueError("IPC token must not be empty")
        if timeout_seconds <= 0 or execution_timeout_seconds <= 0:
            raise ValueError("IPC timeouts must be positive")
        self._port = port
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._execution_timeout_seconds = execution_timeout_seconds
        self._max_message_bytes = max_message_bytes

    async def heartbeat(self) -> HostIpcResponse:
        return await self._request(HostIpcOperation.heartbeat)

    async def health(self) -> HostIpcResponse:
        return await self._request(HostIpcOperation.health)

    async def typed_health(self) -> ComputerUseHostHealth | None:
        response = await self.health()
        if not response.succeeded or response.result is None:
            return None
        try:
            return ComputerUseHostHealth.model_validate(response.result)
        except ValidationError:
            return None

    async def typed_sessions(self) -> list[DesktopSessionSnapshot] | None:
        response = await self._request(HostIpcOperation.sessions_list)
        if not response.succeeded or response.result is None:
            return None
        try:
            return [DesktopSessionSnapshot.model_validate(item) for item in response.result["sessions"]]
        except (KeyError, TypeError, ValidationError):
            return None

    async def session(self, session_id: str) -> HostIpcResponse:
        return await self._request(HostIpcOperation.session_get, session_id=session_id)

    async def uia_windows(self) -> HostIpcResponse:
        return await self._request(HostIpcOperation.uia_windows)

    async def uia_find(self, query: UIAQuery) -> HostIpcResponse:
        return await self._request(HostIpcOperation.uia_find, uia_query=query)

    async def audit(self, session_id: str) -> HostIpcResponse:
        return await self._request(HostIpcOperation.audit_list, session_id=session_id)

    async def lifecycle_events(
        self,
        owner: ActionOwner,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> HostIpcResponse:
        return await self._request(
            HostIpcOperation.lifecycle_events_list,
            owner=owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def action(self, action_id: str, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(HostIpcOperation.action_get, owner=owner, action_id=action_id)

    async def wait_lifecycle_events(
        self,
        owner: ActionOwner,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        timeout_seconds: float = 20,
    ) -> HostIpcResponse:
        return await self._request(
            HostIpcOperation.lifecycle_events_wait,
            owner=owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
            timeout_seconds=timeout_seconds,
            request_timeout_seconds=timeout_seconds + 1,
        )

    async def approvals(self, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(HostIpcOperation.approvals_reconcile_list, owner=owner)

    async def approval(self, approval_id: str, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(HostIpcOperation.approval_reconcile_get, owner=owner, approval_id=approval_id)

    async def decide_approval(
        self,
        approval_id: str,
        owner: ActionOwner,
        *,
        approved: bool,
        decided_by: str,
        reason: str | None = None,
    ) -> HostIpcResponse:
        return await self._request(
            HostIpcOperation.approval_decide,
            owner=owner,
            approval_id=approval_id,
            approved=approved,
            decided_by=decided_by,
            reason=reason,
        )

    async def submit_action(self, action: ActionCommand, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(HostIpcOperation.action_submit, owner=owner, action=action)

    async def cancel_action(self, action_id: str, owner: ActionOwner, *, reason: str | None = None) -> HostIpcResponse:
        return await self._request(HostIpcOperation.action_cancel, owner=owner, action_id=action_id, reason=reason)

    async def execute_action(self, action_id: str, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(
            HostIpcOperation.action_execute,
            owner=owner,
            action_id=action_id,
            request_timeout_seconds=self._execution_timeout_seconds,
        )

    async def create_session(self, session_id: str | None = None) -> HostIpcResponse:
        return await self._request(HostIpcOperation.session_create, session_id=session_id)

    async def stop_session(self, session_id: str) -> HostIpcResponse:
        return await self._request(HostIpcOperation.session_stop, session_id=session_id)

    async def delete_session(self, session_id: str) -> HostIpcResponse:
        return await self._request(HostIpcOperation.session_delete, session_id=session_id)

    async def observe_session(self, session_id: str, *, owner: ActionOwner | None = None, browser_session_id: str | None = None) -> HostIpcResponse:
        return await self._request(
            HostIpcOperation.session_observe,
            session_id=session_id,
            owner=owner,
            browser_session_id=browser_session_id,
            request_timeout_seconds=30,
        )

    async def resolve_target(
        self,
        session_id: str,
        query: TargetQuery,
        *,
        owner: ActionOwner | None = None,
        browser_session_id: str | None = None,
    ) -> HostIpcResponse:
        return await self._request(
            HostIpcOperation.target_resolve,
            session_id=session_id,
            target_query=query,
            owner=owner,
            browser_session_id=browser_session_id,
            request_timeout_seconds=30,
        )

    async def observation_blob_export_info(self, session_id: str, observation_id: str, blob_id: str, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(
            HostIpcOperation.observation_blob_export_info,
            session_id=session_id,
            observation_id=observation_id,
            blob_id=blob_id,
            owner=owner,
        )

    async def export_observation_blob(self, session_id: str, observation_id: str, blob_id: str, owner: ActionOwner) -> tuple[BlobRef, bytes]:
        response, data = await self._request_with_blob(
            session_id=session_id, observation_id=observation_id, blob_id=blob_id, owner=owner
        )
        if not response.succeeded or response.result is None or data is None:
            raise RuntimeError(response.error_code or "blob_unavailable")
        ref = BlobRef.model_validate(response.result)
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise RuntimeError("blob_integrity_failed")
        return ref, data

    async def browser_health(self) -> HostIpcResponse:
        return await self._request(HostIpcOperation.browser_health)

    async def create_browser_session(self, url: str, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(HostIpcOperation.browser_session_create, url=url, owner=owner)

    async def list_browser_sessions(self, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(HostIpcOperation.browser_sessions_list, owner=owner)

    async def close_browser_session(self, browser_session_id: str, owner: ActionOwner) -> HostIpcResponse:
        return await self._request(HostIpcOperation.browser_session_close, browser_session_id=browser_session_id, owner=owner)

    async def _request_with_blob(self, *, session_id: str, observation_id: str, blob_id: str, owner: ActionOwner) -> tuple[HostIpcResponse, bytes | None]:
        request = HostIpcRequest(
            request_id=uuid.uuid4().hex, operation=HostIpcOperation.observation_blob_export_info, token=self._token,
            session_id=session_id, observation_id=observation_id, blob_id=blob_id, owner=owner,
        )
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self._port, limit=self._max_message_bytes + 1), self._timeout_seconds)
            writer.write(request.model_dump_json().encode() + b"\n")
            await writer.drain()
            response = HostIpcResponse.model_validate_json(await asyncio.wait_for(reader.readuntil(b"\n"), self._timeout_seconds))
            if not response.succeeded or response.result is None:
                return response, None
            ref = BlobRef.model_validate(response.result)
            return response, await asyncio.wait_for(reader.readexactly(ref.size_bytes), self._timeout_seconds)
        except Exception:
            return HostIpcResponse(request_id=request.request_id, succeeded=False, error_code="ipc_unavailable", error_message="Host IPC is unavailable."), None
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    async def emergency_stop(self) -> HostIpcResponse:
        return await self._request(HostIpcOperation.emergency_stop_get)

    async def engage_emergency_stop(self, reason: str) -> HostIpcResponse:
        return await self._request(HostIpcOperation.emergency_stop_engage, reason=reason)

    async def reset_emergency_stop(self, reason: str) -> HostIpcResponse:
        return await self._request(HostIpcOperation.emergency_stop_reset, reason=reason)

    async def _request(
        self,
        operation: HostIpcOperation,
        *,
        session_id: str | None = None,
        uia_query: UIAQuery | None = None,
        owner: ActionOwner | None = None,
        action_id: str | None = None,
        approval_id: str | None = None,
        observation_id: str | None = None,
        blob_id: str | None = None,
        browser_session_id: str | None = None,
        url: str | None = None,
        action: ActionCommand | None = None,
        target_query: TargetQuery | None = None,
        approved: bool | None = None,
        decided_by: str | None = None,
        reason: str | None = None,
        after_sequence: int | None = None,
        limit: int | None = None,
        timeout_seconds: float | None = None,
        request_timeout_seconds: float | None = None,
    ) -> HostIpcResponse:
        request = HostIpcRequest(
            request_id=uuid.uuid4().hex,
            operation=operation,
            token=self._token,
            session_id=session_id,
            uia_query=uia_query,
            owner=owner,
            action_id=action_id,
            approval_id=approval_id,
            observation_id=observation_id,
            blob_id=blob_id,
            browser_session_id=browser_session_id,
            url=url,
            action=action,
            target_query=target_query,
            approved=approved,
            decided_by=decided_by,
            reason=reason,
            after_sequence=after_sequence,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
        request_timeout = request_timeout_seconds or self._timeout_seconds
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._port, limit=self._max_message_bytes + 1),
                timeout=request_timeout,
            )
            writer.write(request.model_dump_json().encode("utf-8") + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=request_timeout)
            payload = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=request_timeout)
            return HostIpcResponse.model_validate_json(payload)
        except Exception:
            return HostIpcResponse(
                request_id=request.request_id,
                succeeded=False,
                error_code="ipc_unavailable",
                error_message="Host IPC is unavailable.",
            )
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
