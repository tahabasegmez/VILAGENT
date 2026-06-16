"""In-memory action lifecycle and one-time approval storage."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import BaseModel, Field

from vilagent.computer_use.lifecycle_events import InMemoryLifecycleEventStore
from vilagent.computer_use.models import (
    ActionCommand,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    ActionResult,
    ApprovalLifecycleStatus,
    ApprovalRecord,
    ComputerUseLifecycleEvent,
    LifecycleEventType,
    StructuredError,
    action_fingerprint,
    action_intent_fingerprint,
    utc_now,
)


class ActionStoreError(RuntimeError):
    pass


class ActionNotFoundError(ActionStoreError):
    pass


class ApprovalNotFoundError(ActionStoreError):
    pass


class ActionOwnershipError(ActionStoreError):
    pass


class SessionOwnershipError(ActionStoreError):
    pass


class InvalidActionTransitionError(ActionStoreError):
    pass


class ApprovalAlreadyDecidedError(ActionStoreError):
    pass


class ApprovalExpiredError(ActionStoreError):
    pass


class IdempotencyConflictError(ActionStoreError):
    pass


class ActionStorePersistenceError(ActionStoreError):
    pass


class ActionStoreSnapshot(BaseModel):
    version: Literal[1] = 1
    actions: list[ActionLifecycleRecord] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    events: list[ComputerUseLifecycleEvent] = Field(default_factory=list)


_ALLOWED_TRANSITIONS: dict[ActionLifecycleStatus, frozenset[ActionLifecycleStatus]] = {
    ActionLifecycleStatus.pending: frozenset(
        {
            ActionLifecycleStatus.awaiting_approval,
            ActionLifecycleStatus.approved,
            ActionLifecycleStatus.denied,
            ActionLifecycleStatus.cancelled,
        }
    ),
    ActionLifecycleStatus.awaiting_approval: frozenset(
        {
            ActionLifecycleStatus.approved,
            ActionLifecycleStatus.denied,
            ActionLifecycleStatus.cancelled,
        }
    ),
    ActionLifecycleStatus.approved: frozenset({ActionLifecycleStatus.executing, ActionLifecycleStatus.cancelled}),
    ActionLifecycleStatus.executing: frozenset(
        {
            ActionLifecycleStatus.succeeded,
            ActionLifecycleStatus.failed,
            ActionLifecycleStatus.uncertain,
            ActionLifecycleStatus.cancelled,
        }
    ),
}

_TERMINAL_STATUSES = frozenset(
    {
        ActionLifecycleStatus.denied,
        ActionLifecycleStatus.succeeded,
        ActionLifecycleStatus.failed,
        ActionLifecycleStatus.uncertain,
        ActionLifecycleStatus.cancelled,
    }
)


class InMemoryActionStore:
    """Single-process action queue suitable for initial Gateway orchestration."""

    def __init__(
        self,
        *,
        approval_ttl_seconds: float = 300,
        now: Callable[[], datetime] = utc_now,
        event_store: InMemoryLifecycleEventStore | None = None,
    ):
        if approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive")
        self._approval_ttl_seconds = approval_ttl_seconds
        self._now = now
        self.events = event_store or InMemoryLifecycleEventStore()
        self._actions: dict[str, ActionLifecycleRecord] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        self._idempotency_keys: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def submit(self, action: ActionCommand, *, owner: ActionOwner) -> ActionLifecycleRecord:
        record, _ = await self.submit_or_get(action, owner=owner)
        return record

    async def submit_or_get(self, action: ActionCommand, *, owner: ActionOwner) -> tuple[ActionLifecycleRecord, bool]:
        await self._ensure_ready()
        async with self._lock:
            idempotency_scope = self._idempotency_scope(action, owner)
            if idempotency_scope is not None and idempotency_scope in self._idempotency_keys:
                existing_action_id, existing_intent_fingerprint = self._idempotency_keys[idempotency_scope]
                if existing_intent_fingerprint != action_intent_fingerprint(action):
                    raise IdempotencyConflictError("Idempotency key was already used for a different action payload")
                await self._state_changed_locked()
                return self._get_action_locked(existing_action_id).model_copy(deep=True), False
            if action.action_id in self._actions:
                raise ActionStoreError(f"Action '{action.action_id}' already exists")
            snapshot = action.model_copy(deep=True)
            record = ActionLifecycleRecord(
                action=snapshot,
                owner=owner,
                action_fingerprint=action_fingerprint(snapshot),
                created_at=self._now(),
                updated_at=self._now(),
            )
            self._actions[action.action_id] = record
            if idempotency_scope is not None:
                self._idempotency_keys[idempotency_scope] = (action.action_id, action_intent_fingerprint(action))
            await self._emit_action_event(record, LifecycleEventType.action_submitted)
            return record.model_copy(deep=True), True

    async def get_action(self, action_id: str, *, owner: ActionOwner | None = None) -> ActionLifecycleRecord:
        await self._ensure_ready()
        async with self._lock:
            record = self._get_action_locked(action_id)
            self._assert_owner(record.owner, owner)
            return record.model_copy(deep=True)

    async def list_actions(
        self,
        *,
        owner: ActionOwner | None = None,
        session_id: str | None = None,
    ) -> list[ActionLifecycleRecord]:
        await self._ensure_ready()
        async with self._lock:
            records = self._actions.values()
            if owner is not None:
                records = (record for record in records if record.owner == owner)
            if session_id is not None:
                records = (record for record in records if record.action.session_id == session_id)
            return [record.model_copy(deep=True) for record in records]

    async def assert_session_owner(self, session_id: str, *, owner: ActionOwner) -> None:
        await self._ensure_ready()
        async with self._lock:
            if any(record.action.session_id == session_id and record.owner != owner for record in self._actions.values()):
                raise SessionOwnershipError("Desktop session is already bound to another action owner")

    async def transition(
        self,
        action_id: str,
        status: ActionLifecycleStatus,
        *,
        owner: ActionOwner | None = None,
        result: ActionResult | None = None,
        error: StructuredError | None = None,
        approval_id: str | None = None,
    ) -> ActionLifecycleRecord:
        await self._ensure_ready()
        async with self._lock:
            record = self._get_action_locked(action_id)
            self._assert_owner(record.owner, owner)
            transitioned = await self._transition_locked(record, status, result=result, error=error, approval_id=approval_id)
            return transitioned.model_copy(deep=True)

    async def request_approval(
        self,
        action_id: str,
        *,
        owner: ActionOwner | None = None,
        reasons: list[str] | None = None,
        consequences: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> ApprovalRecord:
        await self._ensure_ready()
        async with self._lock:
            action_record = self._get_action_locked(action_id)
            self._assert_owner(action_record.owner, owner)
            if action_record.approval_id is not None:
                raise ActionStoreError(f"Action '{action_id}' already has an approval request")
            now = self._now()
            approval = ApprovalRecord(
                approval_id=uuid.uuid4().hex,
                action_id=action_id,
                session_id=action_record.action.session_id,
                owner=action_record.owner,
                action_fingerprint=action_record.action_fingerprint,
                reasons=list(reasons or ()),
                consequences=list(consequences or ()),
                created_at=now,
                expires_at=expires_at or now + timedelta(seconds=self._approval_ttl_seconds),
                args=getattr(action_record.action, 'args', None),
            )
            self._approvals[approval.approval_id] = approval
            await self._transition_locked(action_record, ActionLifecycleStatus.awaiting_approval, approval_id=approval.approval_id)
            await self._emit_approval_event(approval, LifecycleEventType.approval_requested)
            return approval.model_copy(deep=True)

    async def get_approval(self, approval_id: str, *, owner: ActionOwner | None = None) -> ApprovalRecord:
        await self._ensure_ready()
        async with self._lock:
            approval = self._get_approval_locked(approval_id)
            self._assert_owner(approval.owner, owner)
            await self._expire_approval_locked(approval)
            return self._approvals[approval_id].model_copy(deep=True)

    async def list_pending_approvals(self, *, owner: ActionOwner | None = None) -> list[ApprovalRecord]:
        await self._ensure_ready()
        async with self._lock:
            for approval in list(self._approvals.values()):
                await self._expire_approval_locked(approval)
            return [
                approval.model_copy(deep=True)
                for approval in self._approvals.values()
                if approval.status == ApprovalLifecycleStatus.pending and (owner is None or approval.owner == owner)
            ]

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        reason: str | None = None,
        owner: ActionOwner | None = None,
    ) -> ApprovalRecord:
        await self._ensure_ready()
        async with self._lock:
            approval = self._get_approval_locked(approval_id)
            self._assert_owner(approval.owner, owner)
            if approval.status != ApprovalLifecycleStatus.pending:
                raise ApprovalAlreadyDecidedError(f"Approval '{approval_id}' is already {approval.status.value}")
            if self._now() >= approval.expires_at:
                await self._expire_approval_locked(approval)
                raise ApprovalExpiredError(f"Approval '{approval_id}' has expired")

            status = ApprovalLifecycleStatus.approved if approved else ApprovalLifecycleStatus.denied
            decided = approval.model_copy(
                deep=True,
                update={
                    "status": status,
                    "decided_at": self._now(),
                    "decided_by": decided_by,
                    "decision_reason": reason,
                },
            )
            self._approvals[approval_id] = ApprovalRecord.model_validate(decided.model_dump())
            action_status = ActionLifecycleStatus.approved if approved else ActionLifecycleStatus.denied
            await self._transition_locked(self._get_action_locked(approval.action_id), action_status)
            await self._emit_approval_event(self._approvals[approval_id], LifecycleEventType.approval_decided)
            return self._approvals[approval_id].model_copy(deep=True)

    async def cancel(self, action_id: str, *, owner: ActionOwner | None = None, reason: str | None = None) -> ActionLifecycleRecord:
        await self._ensure_ready()
        async with self._lock:
            record = self._get_action_locked(action_id)
            self._assert_owner(record.owner, owner)
            if record.approval_id is not None:
                approval = self._get_approval_locked(record.approval_id)
                if approval.status == ApprovalLifecycleStatus.pending:
                    cancelled = approval.model_copy(
                        deep=True,
                        update={
                            "status": ApprovalLifecycleStatus.cancelled,
                            "decided_at": self._now(),
                            "decision_reason": reason,
                        },
                    )
                    self._approvals[approval.approval_id] = ApprovalRecord.model_validate(cancelled.model_dump())
                    await self._emit_approval_event(self._approvals[approval.approval_id], LifecycleEventType.approval_decided)
            error = StructuredError(code="action_cancelled", message=reason or "Action was cancelled.")
            cancelled_action = await self._transition_locked(record, ActionLifecycleStatus.cancelled, error=error)
            return cancelled_action.model_copy(deep=True)

    async def _transition_locked(
        self,
        record: ActionLifecycleRecord,
        status: ActionLifecycleStatus,
        *,
        result: ActionResult | None = None,
        error: StructuredError | None = None,
        approval_id: str | None = None,
    ) -> ActionLifecycleRecord:
        if status not in _ALLOWED_TRANSITIONS.get(record.status, frozenset()):
            raise InvalidActionTransitionError(f"Cannot transition action '{record.action.action_id}' from {record.status.value} to {status.value}")
        now = self._now()
        updated = record.model_copy(
            deep=True,
            update={
                "status": status,
                "result": result,
                "error": error,
                "approval_id": approval_id if approval_id is not None else record.approval_id,
                "updated_at": now,
                "completed_at": now if status in _TERMINAL_STATUSES else None,
            },
        )
        validated = ActionLifecycleRecord.model_validate(updated.model_dump())
        self._actions[record.action.action_id] = validated
        await self._emit_action_event(validated, LifecycleEventType.action_status_changed)
        return validated

    async def _expire_approval_locked(self, approval: ApprovalRecord) -> None:
        if approval.status != ApprovalLifecycleStatus.pending or self._now() < approval.expires_at:
            return
        expired = approval.model_copy(
            deep=True,
            update={
                "status": ApprovalLifecycleStatus.expired,
                "decided_at": self._now(),
                "decision_reason": "Approval expired.",
            },
        )
        self._approvals[approval.approval_id] = ApprovalRecord.model_validate(expired.model_dump())
        await self._emit_approval_event(self._approvals[approval.approval_id], LifecycleEventType.approval_decided)
        action = self._get_action_locked(approval.action_id)
        if action.status == ActionLifecycleStatus.awaiting_approval:
            await self._transition_locked(
                action,
                ActionLifecycleStatus.denied,
                error=StructuredError(code="approval_expired", message="Approval request expired."),
            )

    async def _emit_action_event(self, record: ActionLifecycleRecord, event_type: LifecycleEventType) -> None:
        await self.events.append(
            ComputerUseLifecycleEvent(
                sequence=1,
                event_type=event_type,
                owner=record.owner,
                session_id=record.action.session_id,
                action_id=record.action.action_id,
                action_kind=record.action.kind,
                action_status=record.status,
                approval_id=record.approval_id,
                error_code=record.error.code if record.error is not None else None,
                created_at=self._now(),
            )
        )
        await self._state_changed_locked()

    async def _emit_approval_event(self, approval: ApprovalRecord, event_type: LifecycleEventType) -> None:
        action = self._get_action_locked(approval.action_id)
        await self.events.append(
            ComputerUseLifecycleEvent(
                sequence=1,
                event_type=event_type,
                owner=approval.owner,
                session_id=approval.session_id,
                action_id=approval.action_id,
                action_kind=action.action.kind,
                action_status=action.status,
                approval_id=approval.approval_id,
                approval_status=approval.status,
                created_at=self._now(),
            )
        )
        await self._state_changed_locked()

    async def _ensure_ready(self) -> None:
        return None

    async def _state_changed_locked(self) -> None:
        return None

    async def _snapshot_locked(self) -> ActionStoreSnapshot:
        return ActionStoreSnapshot(
            actions=[record.model_copy(deep=True) for record in self._actions.values()],
            approvals=[record.model_copy(deep=True) for record in self._approvals.values()],
            events=await self.events.snapshot(),
        )

    def _restore_locked(self, snapshot: ActionStoreSnapshot) -> None:
        self._actions = {record.action.action_id: record.model_copy(deep=True) for record in snapshot.actions}
        self._approvals = {record.approval_id: record.model_copy(deep=True) for record in snapshot.approvals}
        if len(self._actions) != len(snapshot.actions):
            raise ActionStorePersistenceError("Lifecycle snapshot contains duplicate action IDs")
        if len(self._approvals) != len(snapshot.approvals):
            raise ActionStorePersistenceError("Lifecycle snapshot contains duplicate approval IDs")
        self._idempotency_keys = {}
        for record in self._actions.values():
            scope = self._idempotency_scope(record.action, record.owner)
            if scope is not None:
                entry = (record.action.action_id, action_intent_fingerprint(record.action))
                if scope in self._idempotency_keys and self._idempotency_keys[scope] != entry:
                    raise ActionStorePersistenceError("Lifecycle snapshot contains conflicting idempotency keys")
                self._idempotency_keys[scope] = entry
            if record.approval_id is not None:
                approval = self._approvals.get(record.approval_id)
                if approval is None or approval.action_id != record.action.action_id:
                    raise ActionStorePersistenceError("Lifecycle snapshot contains an invalid action approval reference")
        for approval in self._approvals.values():
            action = self._actions.get(approval.action_id)
            if (
                action is None
                or approval.owner != action.owner
                or approval.session_id != action.action.session_id
                or approval.action_fingerprint != action.action_fingerprint
            ):
                raise ActionStorePersistenceError("Lifecycle snapshot contains an invalid approval action reference")

    def _get_action_locked(self, action_id: str) -> ActionLifecycleRecord:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise ActionNotFoundError(action_id) from exc

    def _get_approval_locked(self, approval_id: str) -> ApprovalRecord:
        try:
            return self._approvals[approval_id]
        except KeyError as exc:
            raise ApprovalNotFoundError(approval_id) from exc

    @staticmethod
    def _assert_owner(actual: ActionOwner, expected: ActionOwner | None) -> None:
        if expected is not None and actual != expected:
            raise ActionOwnershipError("Action lifecycle record does not belong to the requested owner")

    @staticmethod
    def _idempotency_scope(action: ActionCommand, owner: ActionOwner) -> tuple[str, str, str, str] | None:
        if action.idempotency_key is None:
            return None
        return owner.thread_id, owner.run_id, owner.agent_id, action.idempotency_key


class JsonFileActionStore(InMemoryActionStore):
    """Restart-safe action lifecycle store backed by one atomic JSON snapshot."""

    def __init__(self, path: str | Path, **kwargs):
        super().__init__(**kwargs)
        self._path = Path(path)
        self._load_lock = asyncio.Lock()
        self._loaded = False

    async def initialize(self) -> None:
        await self._ensure_ready()

    async def _ensure_ready(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            snapshot = await asyncio.to_thread(self._read_snapshot_sync)
            async with self._lock:
                self._restore_locked(snapshot)
                await self.events.restore(snapshot.events)
                executing = [record.action.action_id for record in self._actions.values() if record.status == ActionLifecycleStatus.executing]
                self._loaded = True
            for action_id in executing:
                await super().transition(
                    action_id,
                    ActionLifecycleStatus.uncertain,
                    error=StructuredError(
                        code="host_restart_during_execution",
                        message="Host restarted while action execution was in progress.",
                    ),
                )

    async def _state_changed_locked(self) -> None:
        if not self._loaded:
            return
        snapshot = await self._snapshot_locked()
        try:
            await asyncio.to_thread(self._write_snapshot_sync, snapshot)
        except Exception as exc:
            raise ActionStorePersistenceError("Unable to persist action lifecycle state") from exc

    def _read_snapshot_sync(self) -> ActionStoreSnapshot:
        if not self._path.exists():
            return ActionStoreSnapshot()
        try:
            return ActionStoreSnapshot.model_validate_json(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ActionStorePersistenceError("Unable to load action lifecycle state") from exc

    def _write_snapshot_sync(self, snapshot: ActionStoreSnapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self._path.parent, delete=False) as temporary:
                json.dump(snapshot.model_dump(mode="json"), temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(self._path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
