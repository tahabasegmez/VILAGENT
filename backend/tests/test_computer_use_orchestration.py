"""Tests for policy-controlled action submission and execution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from vilagent.computer_use.action_store import InMemoryActionStore, InvalidActionTransitionError
from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    ActionResult,
    ActionStatus,
    RiskAssessment,
    RiskLevel,
)
from vilagent.computer_use.orchestration import ComputerUseActionService
from vilagent.computer_use.policy import DefaultActionPolicy


def _action(*, risk=RiskLevel.low, action_id="action-1", session_id="session-1"):
    return ActionCommand(
        action_id=action_id,
        session_id=session_id,
        kind=ActionKind.hotkey,
        risk=RiskAssessment(level=risk),
    )


def _owner():
    return ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")


class FakeEngine:
    def __init__(self):
        self.actions = []

    async def execute(self, action, *, owner_id):
        self.actions.append((action, owner_id))
        now = datetime.now(UTC)
        return ActionResult(
            action_id=action.action_id,
            status=ActionStatus.succeeded,
            started_at=now,
            completed_at=now,
            before_observation_id="obs-1",
            after_observation_id="obs-2",
        )


def _service(store, engine, *, max_actions_per_owner=None):
    async def engine_factory(session_id, policy):
        assert policy.evaluate(_action()).decision.value == "allow"
        return engine

    async def validate_session(session_id):
        if session_id not in {"session-1", "session-2"}:
            raise KeyError(session_id)

    return ComputerUseActionService(
        action_store=store,
        policy=DefaultActionPolicy(),
        engine_factory=engine_factory,
        session_validator=validate_session,
        max_actions_per_owner=max_actions_per_owner,
    )


def test_low_risk_submission_is_approved_and_executes_stored_snapshot():
    async def run():
        store = InMemoryActionStore()
        engine = FakeEngine()
        service = _service(store, engine)
        action = _action()

        submitted = await service.submit(action, owner=_owner())
        action.kind = ActionKind.launch_app
        completed = await service.execute("action-1", owner=_owner())

        assert submitted.status == ActionLifecycleStatus.approved
        assert completed.status == ActionLifecycleStatus.succeeded
        assert engine.actions[0][0].kind == ActionKind.hotkey

    asyncio.run(run())


def test_high_risk_submission_waits_for_approval_before_execution():
    async def run():
        store = InMemoryActionStore()
        service = _service(store, FakeEngine())

        submitted = await service.submit(_action(risk=RiskLevel.high), owner=_owner())

        assert submitted.status == ActionLifecycleStatus.awaiting_approval
        with pytest.raises(InvalidActionTransitionError):
            await service.execute("action-1", owner=_owner())

    asyncio.run(run())


def test_critical_submission_requires_explicit_approval():
    async def run():
        store = InMemoryActionStore()
        service = _service(store, FakeEngine())

        submitted = await service.submit(_action(risk=RiskLevel.critical), owner=_owner())

        assert submitted.status == ActionLifecycleStatus.awaiting_approval
        assert submitted.error is None

    asyncio.run(run())


def test_idempotent_retry_returns_existing_lifecycle_without_reapplying_policy():
    async def run():
        store = InMemoryActionStore()
        service = _service(store, FakeEngine())
        first = _action()
        first.idempotency_key = "retry-1"
        retry = first.model_copy(update={"action_id": "action-2"})

        submitted = await service.submit(first, owner=_owner())
        repeated = await service.submit(retry, owner=_owner())

        assert submitted.status == ActionLifecycleStatus.approved
        assert repeated.action.action_id == "action-1"
        assert repeated.status == ActionLifecycleStatus.approved
        assert [event.event_type.value for event in await store.events.list(owner=_owner())] == [
            "action_submitted",
            "action_status_changed",
        ]

    asyncio.run(run())


def test_owner_action_budget_denies_excess_without_affecting_other_owner():
    async def run():
        store = InMemoryActionStore()
        service = _service(store, FakeEngine(), max_actions_per_owner=1)
        first = await service.submit(_action(action_id="action-1"), owner=_owner())
        excess = await service.submit(_action(action_id="action-2"), owner=_owner())
        other_owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-2")
        other = await service.submit(_action(action_id="action-3", session_id="session-2"), owner=other_owner)

        assert first.status == ActionLifecycleStatus.approved
        assert excess.status == ActionLifecycleStatus.denied
        assert excess.error is not None and excess.error.code == "action_budget_exhausted"
        assert other.status == ActionLifecycleStatus.approved

    asyncio.run(run())


def test_idempotent_retry_does_not_consume_additional_action_budget():
    async def run():
        store = InMemoryActionStore()
        service = _service(store, FakeEngine(), max_actions_per_owner=1)
        first = _action(action_id="action-1")
        first.idempotency_key = "retry-1"
        retry = first.model_copy(update={"action_id": "action-2"})

        await service.submit(first, owner=_owner())
        repeated = await service.submit(retry, owner=_owner())

        assert repeated.status == ActionLifecycleStatus.approved
        assert repeated.action.action_id == "action-1"

    asyncio.run(run())


def test_concurrent_owner_submissions_admit_only_budgeted_action():
    async def run():
        store = InMemoryActionStore()
        service = _service(store, FakeEngine(), max_actions_per_owner=1)

        first, second = await asyncio.gather(
            service.submit(_action(action_id="action-1"), owner=_owner()),
            service.submit(_action(action_id="action-2"), owner=_owner()),
        )

        assert {first.status, second.status} == {ActionLifecycleStatus.approved, ActionLifecycleStatus.denied}

    asyncio.run(run())


def test_session_is_bound_to_first_action_owner_and_concurrent_other_owner_is_rejected():
    async def run():
        store = InMemoryActionStore()
        service = _service(store, FakeEngine())
        other_owner = ActionOwner(thread_id="thread-2", run_id="run-2", agent_id="agent-2")

        results = await asyncio.gather(
            service.submit(_action(action_id="action-1"), owner=_owner()),
            service.submit(_action(action_id="action-2"), owner=other_owner),
            return_exceptions=True,
        )

        admitted = [result for result in results if isinstance(result, ActionLifecycleRecord)]
        rejected = [result for result in results if isinstance(result, Exception)]
        assert len(admitted) == 1
        assert len(rejected) == 1
        assert rejected[0].__class__.__name__ == "SessionOwnershipError"

    asyncio.run(run())


def test_service_cancel_interrupts_running_execution_and_persists_cancelled():
    class SlowEngine:
        async def execute(self, action, *, owner_id):
            await asyncio.Event().wait()

    async def run():
        store = InMemoryActionStore()
        service = _service(store, SlowEngine())
        await service.submit(_action(), owner=_owner())

        execution = asyncio.create_task(service.execute("action-1", owner=_owner()))
        while (await store.get_action("action-1", owner=_owner())).status != ActionLifecycleStatus.executing:
            await asyncio.sleep(0)
        cancelled = await service.cancel("action-1", owner=_owner(), reason="operator cancelled")

        assert cancelled.status == ActionLifecycleStatus.cancelled
        assert execution.cancelled()

    asyncio.run(run())
