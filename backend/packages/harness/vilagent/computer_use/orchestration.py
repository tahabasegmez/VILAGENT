"""Policy-controlled action submission and execution lifecycle."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from vilagent.computer_use.action_store import InMemoryActionStore
from vilagent.computer_use.engine import ComputerUseEngine
from vilagent.computer_use.models import (
    ActionCommand,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    ActionStatus,
    PolicyDecision,
    PolicyVerdict,
    StructuredError,
)
from vilagent.computer_use.policy import ActionPolicy

EngineFactory = Callable[[str, ActionPolicy], Awaitable[ComputerUseEngine]]
SessionValidator = Callable[[str], Awaitable[object]]


class _StoredApprovalPolicy:
    """Allow execution only after the orchestration store approved the snapshot."""

    def evaluate(self, action: ActionCommand) -> PolicyVerdict:
        return PolicyVerdict(decision=PolicyDecision.allow, reasons=["Action lifecycle approval already validated."], policy_id="vilagent.stored-approval.v1")


class ComputerUseActionService:
    """Submit typed actions, route approval, and execute approved snapshots."""

    def __init__(
        self,
        *,
        action_store: InMemoryActionStore,
        policy: ActionPolicy,
        engine_factory: EngineFactory,
        session_validator: SessionValidator,
        max_actions_per_owner: int | None = None,
    ):
        if max_actions_per_owner is not None and max_actions_per_owner < 1:
            raise ValueError("max_actions_per_owner must be positive")
        self._action_store = action_store
        self._policy = policy
        self._engine_factory = engine_factory
        self._session_validator = session_validator
        self._max_actions_per_owner = max_actions_per_owner
        self._submission_locks: dict[tuple[str, str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._execution_tasks: dict[str, asyncio.Task] = {}
        self._execution_lock = asyncio.Lock()

    async def submit(self, action: ActionCommand, *, owner: ActionOwner) -> ActionLifecycleRecord:
        await self._session_validator(action.session_id)
        owner_key = (owner.thread_id, owner.run_id, owner.agent_id)
        async with self._session_locks[action.session_id]:
            await self._action_store.assert_session_owner(action.session_id, owner=owner)
            async with self._submission_locks[owner_key]:
                return await self._submit_locked(action, owner=owner)

    async def _submit_locked(self, action: ActionCommand, *, owner: ActionOwner) -> ActionLifecycleRecord:
        record, created = await self._action_store.submit_or_get(action, owner=owner)
        if not created:
            return record
        if await self._owner_budget_exhausted(owner):
            return await self._action_store.transition(
                action.action_id,
                ActionLifecycleStatus.denied,
                owner=owner,
                error=StructuredError(code="action_budget_exhausted", message="Action budget is exhausted for this owner."),
            )
        verdict = self._policy.evaluate(record.action)
        if verdict.decision == PolicyDecision.deny:
            return await self._action_store.transition(
                action.action_id,
                ActionLifecycleStatus.denied,
                owner=owner,
                error=StructuredError(code="policy_denied", message="; ".join(verdict.reasons) or "Action denied by policy."),
            )
        if verdict.decision == PolicyDecision.require_approval:
            await self._action_store.request_approval(
                action.action_id,
                owner=owner,
                reasons=verdict.reasons,
                consequences=action.risk.consequences,
            )
            return await self._action_store.get_action(action.action_id, owner=owner)
        return await self._action_store.transition(action.action_id, ActionLifecycleStatus.approved, owner=owner)

    async def execute(self, action_id: str, *, owner: ActionOwner) -> ActionLifecycleRecord:
        async with self._execution_lock:
            if action_id in self._execution_tasks:
                raise RuntimeError("Action execution is already running")
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("Action execution requires an asyncio task")
            self._execution_tasks[action_id] = task
        try:
            record = await self._action_store.transition(action_id, ActionLifecycleStatus.executing, owner=owner)
            engine = await self._engine_factory(record.action.session_id, _StoredApprovalPolicy())
            result = await engine.execute(record.action, owner_id=f"{owner.run_id}:{owner.agent_id}")
        except asyncio.CancelledError:
            await self._action_store.cancel(action_id, owner=owner, reason="Action execution was cancelled.")
            raise
        except Exception as exc:
            return await self._action_store.transition(
                action_id,
                ActionLifecycleStatus.failed,
                owner=owner,
                error=StructuredError(code="action_orchestration_error", message=str(exc) or exc.__class__.__name__, retryable=True),
            )

        finally:
            async with self._execution_lock:
                self._execution_tasks.pop(action_id, None)

        lifecycle_status = {
            ActionStatus.succeeded: ActionLifecycleStatus.succeeded,
            ActionStatus.failed: ActionLifecycleStatus.failed,
            ActionStatus.blocked: ActionLifecycleStatus.failed,
            ActionStatus.cancelled: ActionLifecycleStatus.cancelled,
            ActionStatus.uncertain: ActionLifecycleStatus.uncertain,
        }[result.status]
        return await self._action_store.transition(action_id, lifecycle_status, owner=owner, result=result, error=result.error)

    async def cancel(self, action_id: str, *, owner: ActionOwner, reason: str | None = None) -> ActionLifecycleRecord:
        async with self._execution_lock:
            task = self._execution_tasks.get(action_id)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return await self._action_store.get_action(action_id, owner=owner)
        return await self._action_store.cancel(action_id, owner=owner, reason=reason)

    async def _owner_budget_exhausted(self, owner: ActionOwner) -> bool:
        if self._max_actions_per_owner is None:
            return False
        records = await self._action_store.list_actions(owner=owner)
        admitted = sum(
            record.status not in {ActionLifecycleStatus.denied, ActionLifecycleStatus.cancelled}
            for record in records
        )
        return admitted > self._max_actions_per_owner
