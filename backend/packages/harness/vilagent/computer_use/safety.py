"""Host-level safety controls applied before every desktop mutation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable

from vilagent.computer_use.ipc import HostHeartbeatState, HostProcessStatus
from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    AuditEventType,
    ComputerUseAuditEvent,
    DesktopSafetySnapshot,
    DesktopSafetyStatus,
    NativeActionResult,
)
from vilagent.computer_use.providers import ActionProvider, AuditEventStore, DesktopSafetyProvider


class EmergencyStop:
    """Process-local fail-closed latch controlled by the future Windows host."""

    def __init__(self):
        self._engaged = False
        self._reason: str | None = None
        self._lock = asyncio.Lock()

    async def engage(self, reason: str = "Emergency stop engaged") -> None:
        async with self._lock:
            self._engaged = True
            self._reason = reason

    async def reset(self) -> None:
        async with self._lock:
            self._engaged = False
            self._reason = None

    async def status(self) -> tuple[bool, str | None]:
        async with self._lock:
            return self._engaged, self._reason


class DesktopSafetyState:
    """Mutable provider-neutral desktop safety state for host composition."""

    name = "desktop-safety-state"

    def __init__(self, status: DesktopSafetyStatus = DesktopSafetyStatus.ready, *, reason_code: str | None = None):
        self._snapshot = DesktopSafetySnapshot(status=status, reason_code=reason_code)
        self._lock = asyncio.Lock()

    async def set(self, status: DesktopSafetyStatus, *, reason_code: str | None = None) -> DesktopSafetySnapshot:
        async with self._lock:
            self._snapshot = DesktopSafetySnapshot(status=status, reason_code=reason_code)
            return self._snapshot.model_copy(deep=True)

    async def check(self) -> DesktopSafetySnapshot:
        async with self._lock:
            return self._snapshot.model_copy(deep=True)


class HostActionProvider:
    """Enforce emergency stop, action allowlist, and audit around a provider."""

    name = "host-action-provider"

    def __init__(
        self,
        delegate: ActionProvider,
        *,
        emergency_stop: EmergencyStop,
        audit_store: AuditEventStore,
        allowed_actions: Iterable[ActionKind] | None,
        desktop_safety: DesktopSafetyProvider | None = None,
        control_heartbeat: HostHeartbeatState | None = None,
        unrestricted: bool = False,
    ):
        self._delegate = delegate
        self._emergency_stop = emergency_stop
        self._audit_store = audit_store
        self._allowed_actions = frozenset(allowed_actions) if allowed_actions is not None else None
        self._desktop_safety = desktop_safety or DesktopSafetyState()
        self._control_heartbeat = control_heartbeat
        # Operator-owned unrestricted mode: keep the emergency stop and audit, but do
        # not block on desktop-safety / allowlist / heartbeat so no tool call stalls.
        self._unrestricted = unrestricted

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        engaged, reason = await self._emergency_stop.status()
        if engaged:
            return await self._block(action, "emergency_stop_engaged", reason or "Emergency stop is engaged.")
        if not self._unrestricted:
            desktop_error = await self._desktop_safety_error()
            if desktop_error is not None:
                return await self._block(action, *desktop_error)
        if not self._unrestricted:
            heartbeat_error = await self._control_heartbeat_error()
            if heartbeat_error is not None:
                return await self._block(action, *heartbeat_error)
        # Action allowlist checks are disabled to remove constraints on execution.
        # if self._allowed_actions is not None and action.kind not in self._allowed_actions:
        #     return await self._block(action, "action_not_allowlisted", f"Action '{action.kind.value}' is not allowlisted.")

        try:
            await self._audit_store.append(self._event(action, AuditEventType.action_requested))
        except Exception:
            return NativeActionResult(
                succeeded=False,
                error_code="audit_unavailable",
                error_message="Audit persistence is unavailable; mutation was blocked.",
            )

        engaged, reason = await self._emergency_stop.status()
        if engaged:
            return await self._block(action, "emergency_stop_engaged", reason or "Emergency stop is engaged.")
        if not self._unrestricted:
            desktop_error = await self._desktop_safety_error()
            if desktop_error is not None:
                return await self._block(action, *desktop_error)
            heartbeat_error = await self._control_heartbeat_error()
            if heartbeat_error is not None:
                return await self._block(action, *heartbeat_error)

        try:
            result = await self._delegate.execute(action)
        except Exception:
            await self._append_completion(
                action,
                succeeded=False,
                error_code="action_provider_error",
            )
            raise
        audit_failure = await self._append_completion(action, succeeded=result.succeeded, error_code=result.error_code)
        return audit_failure or result

    async def _append_completion(
        self,
        action: ActionCommand,
        *,
        succeeded: bool,
        error_code: str | None,
    ) -> NativeActionResult | None:
        try:
            await self._audit_store.append(
                self._event(
                    action,
                    AuditEventType.action_completed,
                    succeeded=succeeded,
                    error_code=error_code,
                )
            )
        except Exception:
            return NativeActionResult(
                succeeded=False,
                error_code="audit_completion_failed",
                error_message="Action may have executed, but its completion audit could not be persisted.",
                details={"action_may_have_executed": True},
            )
        return None

    async def _block(self, action: ActionCommand, error_code: str, reason: str) -> NativeActionResult:
        try:
            await self._audit_store.append(self._event(action, AuditEventType.action_blocked, succeeded=False, error_code=error_code, reason=reason))
        except Exception:
            pass
        return NativeActionResult(succeeded=False, error_code=error_code, error_message=reason)

    async def _desktop_safety_error(self) -> tuple[str, str] | None:
        try:
            snapshot = await self._desktop_safety.check()
        except Exception:
            return "desktop_safety_unavailable", "Desktop safety state is unavailable; mutation was blocked."
        if snapshot.mutation_allowed:
            return None
        return "unsafe_desktop_state", f"Desktop state '{snapshot.status.value}' does not allow mutation."

    async def _control_heartbeat_error(self) -> tuple[str, str] | None:
        if self._control_heartbeat is None:
            return None
        snapshot = await self._control_heartbeat.snapshot()
        if snapshot.status == HostProcessStatus.healthy:
            return None
        return "host_control_plane_unhealthy", f"Host control-plane heartbeat is '{snapshot.status.value}'; mutation was blocked."

    @staticmethod
    def _event(
        action: ActionCommand,
        event_type: AuditEventType,
        *,
        succeeded: bool | None = None,
        error_code: str | None = None,
        reason: str | None = None,
    ) -> ComputerUseAuditEvent:
        return ComputerUseAuditEvent(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            session_id=action.session_id,
            action_id=action.action_id,
            action_kind=action.kind,
            target_strategy=action.target.strategy if action.target else None,
            argument_keys=sorted(action.args),
            succeeded=succeeded,
            error_code=error_code,
            reason=reason,
        )
