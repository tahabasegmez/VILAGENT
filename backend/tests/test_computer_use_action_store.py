"""Tests for action lifecycle and one-time approval storage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from vilagent.computer_use.action_store import (
    ActionOwnershipError,
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    IdempotencyConflictError,
    InMemoryActionStore,
    InvalidActionTransitionError,
    SessionOwnershipError,
)
from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleStatus,
    ActionOwner,
    ApprovalLifecycleStatus,
)


def _action(action_id="action-1"):
    return ActionCommand(action_id=action_id, session_id="session-1", kind=ActionKind.hotkey, args={"keys": ["CTRL", "L"]})


def _owner(agent_id="agent-1"):
    return ActionOwner(thread_id="thread-1", run_id="run-1", agent_id=agent_id)


def test_submit_stores_immutable_snapshot_and_filters_by_owner():
    async def run():
        store = InMemoryActionStore()
        action = _action()
        submitted = await store.submit(action, owner=_owner())
        action.args["keys"] = ["CTRL", "R"]

        stored = await store.get_action("action-1", owner=_owner())
        hidden = await store.list_actions(owner=_owner("other-agent"))

        assert submitted.action.args["keys"] == ["CTRL", "L"]
        assert stored.action.args["keys"] == ["CTRL", "L"]
        assert hidden == []
        with pytest.raises(ActionOwnershipError):
            await store.get_action("action-1", owner=_owner("other-agent"))

    asyncio.run(run())


def test_approval_is_one_time_and_updates_action_status():
    async def run():
        store = InMemoryActionStore()
        await store.submit(_action(), owner=_owner())
        approval = await store.request_approval("action-1", owner=_owner(), reasons=["dangerous"])

        decided = await store.decide_approval(approval.approval_id, approved=True, decided_by="operator-1", owner=_owner())
        action = await store.get_action("action-1", owner=_owner())

        assert decided.status == ApprovalLifecycleStatus.approved
        assert action.status == ActionLifecycleStatus.approved
        with pytest.raises(ApprovalAlreadyDecidedError):
            await store.decide_approval(approval.approval_id, approved=False, decided_by="operator-2", owner=_owner())

    asyncio.run(run())


def test_expired_approval_fail_closed_denies_action():
    async def run():
        current = datetime(2026, 1, 1, tzinfo=UTC)
        store = InMemoryActionStore(now=lambda: current)
        await store.submit(_action(), owner=_owner())
        approval = await store.request_approval("action-1", owner=_owner(), expires_at=current + timedelta(seconds=1))
        current += timedelta(seconds=2)

        with pytest.raises(ApprovalExpiredError):
            await store.decide_approval(approval.approval_id, approved=True, decided_by="operator-1", owner=_owner())

        expired = await store.get_approval(approval.approval_id, owner=_owner())
        action = await store.get_action("action-1", owner=_owner())
        assert expired.status == ApprovalLifecycleStatus.expired
        assert action.status == ActionLifecycleStatus.denied
        assert action.error is not None and action.error.code == "approval_expired"

    asyncio.run(run())


def test_cancel_cancels_pending_approval_and_action():
    async def run():
        store = InMemoryActionStore()
        await store.submit(_action(), owner=_owner())
        approval = await store.request_approval("action-1", owner=_owner())

        action = await store.cancel("action-1", owner=_owner(), reason="operator cancelled")
        cancelled_approval = await store.get_approval(approval.approval_id, owner=_owner())

        assert action.status == ActionLifecycleStatus.cancelled
        assert cancelled_approval.status == ApprovalLifecycleStatus.cancelled

    asyncio.run(run())


def test_invalid_lifecycle_transition_is_rejected():
    async def run():
        store = InMemoryActionStore()
        await store.submit(_action(), owner=_owner())

        with pytest.raises(InvalidActionTransitionError):
            await store.transition("action-1", ActionLifecycleStatus.succeeded, owner=_owner())

    asyncio.run(run())


def test_action_and_approval_changes_emit_ordered_sanitized_events():
    async def run():
        store = InMemoryActionStore()
        await store.submit(_action(), owner=_owner())
        approval = await store.request_approval("action-1", owner=_owner(), reasons=["secret reason"])
        await store.decide_approval(approval.approval_id, approved=True, decided_by="operator-1", owner=_owner())

        events = await store.events.list(owner=_owner())
        payload = [event.model_dump(mode="json") for event in events]

        assert [event.sequence for event in events] == list(range(1, 6))
        assert [event.event_type.value for event in events] == [
            "action_submitted",
            "action_status_changed",
            "approval_requested",
            "action_status_changed",
            "approval_decided",
        ]
        assert events[-1].approval_status == ApprovalLifecycleStatus.approved
        assert "secret reason" not in str(payload)
        assert "args" not in str(payload)

    asyncio.run(run())


def test_owner_scoped_idempotency_returns_existing_record_without_duplicate_event():
    async def run():
        store = InMemoryActionStore()
        first = ActionCommand(
            action_id="action-1",
            session_id="session-1",
            kind=ActionKind.hotkey,
            args={"keys": ["CTRL", "L"]},
            idempotency_key="retry-1",
        )
        retry = first.model_copy(update={"action_id": "action-2"})

        submitted = await store.submit(first, owner=_owner())
        repeated = await store.submit(retry, owner=_owner())

        assert repeated.action.action_id == submitted.action.action_id
        assert [event.event_type.value for event in await store.events.list(owner=_owner())] == ["action_submitted"]

    asyncio.run(run())


def test_owner_scoped_idempotency_rejects_payload_conflict_but_is_independent_per_owner():
    async def run():
        store = InMemoryActionStore()
        first = ActionCommand(action_id="action-1", session_id="session-1", kind=ActionKind.hotkey, idempotency_key="retry-1")
        conflict = ActionCommand(action_id="action-2", session_id="session-1", kind=ActionKind.launch_app, idempotency_key="retry-1")

        await store.submit(first, owner=_owner())
        with pytest.raises(IdempotencyConflictError):
            await store.submit(conflict, owner=_owner())

        other = await store.submit(conflict, owner=_owner("other-agent"))
        assert other.action.action_id == "action-2"

    asyncio.run(run())


def test_session_owner_check_rejects_different_owner_after_first_action():
    async def run():
        store = InMemoryActionStore()
        await store.submit(_action(), owner=_owner())

        await store.assert_session_owner("session-1", owner=_owner())
        with pytest.raises(SessionOwnershipError):
            await store.assert_session_owner("session-1", owner=_owner("other-agent"))
        await store.assert_session_owner("unbound-session", owner=_owner("other-agent"))

    asyncio.run(run())
