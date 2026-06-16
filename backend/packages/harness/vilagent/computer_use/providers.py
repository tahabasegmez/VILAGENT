"""Provider protocols for the VILAGENT execution plane."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from vilagent.computer_use.models import (
    ActionCommand,
    ApprovalDecision,
    ApprovalRequest,
    ComputerUseAuditEvent,
    Condition,
    DesktopSafetySnapshot,
    NativeActionResult,
    Observation,
    TargetQuery,
    TargetRef,
    TargetStrategy,
    VerificationResult,
)


@runtime_checkable
class ObservationProvider(Protocol):
    name: str

    async def observe(self, session_id: str, *, previous: Observation | None = None) -> Observation:
        """Capture a fresh desktop observation."""
        ...


@runtime_checkable
class ActionProvider(Protocol):
    name: str

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        """Execute a policy-approved typed action."""
        ...


@runtime_checkable
class DesktopSafetyProvider(Protocol):
    name: str

    async def check(self) -> DesktopSafetySnapshot:
        """Return whether host desktop mutation is currently safe."""
        ...


@runtime_checkable
class TargetProvider(Protocol):
    name: str
    strategy: TargetStrategy

    async def resolve(self, query: TargetQuery, *, observation: Observation) -> TargetRef | None:
        """Resolve a target candidate without mutating the desktop."""
        ...


@runtime_checkable
class VerificationProvider(Protocol):
    name: str

    async def verify(
        self,
        conditions: list[Condition],
        *,
        before: Observation,
        after: Observation,
    ) -> VerificationResult:
        """Verify action postconditions against before/after observations."""
        ...


@runtime_checkable
class ApprovalProvider(Protocol):
    name: str

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Request a user decision for an action that policy gates."""
        ...


@runtime_checkable
class AuditEventStore(Protocol):
    async def append(self, event: ComputerUseAuditEvent) -> None:
        """Persist an immutable host-action audit event."""
        ...

    async def list_session(self, session_id: str) -> list[ComputerUseAuditEvent]:
        """List sanitized audit events for one desktop session."""
        ...
