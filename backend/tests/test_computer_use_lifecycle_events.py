"""Tests for bounded, sanitized computer-use lifecycle events."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.lifecycle_events import InMemoryLifecycleEventStore
from vilagent.computer_use.models import (
    ActionKind,
    ActionLifecycleStatus,
    ActionOwner,
    ComputerUseLifecycleEvent,
    LifecycleEventType,
)


def _owner(agent_id="agent-1"):
    return ActionOwner(thread_id="thread-1", run_id="run-1", agent_id=agent_id)


def _event(action_id: str, *, owner=None):
    return ComputerUseLifecycleEvent(
        sequence=1,
        event_type=LifecycleEventType.action_submitted,
        owner=owner or _owner(),
        session_id="session-1",
        action_id=action_id,
        action_kind=ActionKind.type_text,
        action_status=ActionLifecycleStatus.pending,
    )


def test_event_store_assigns_sequence_filters_and_bounds_retention():
    async def run():
        store = InMemoryLifecycleEventStore(max_events=2)
        first = await store.append(_event("action-1"))
        second = await store.append(_event("action-2", owner=_owner("other-agent")))
        third = await store.append(_event("action-3"))

        owned = await store.list(owner=_owner())
        after = await store.list(after_sequence=second.sequence)

        assert first.sequence == 1
        assert [event.action_id for event in owned] == ["action-3"]
        assert [event.action_id for event in after] == ["action-3"]
        assert third.sequence == 3

    asyncio.run(run())


def test_lifecycle_event_contract_has_no_action_payload_fields():
    payload = _event("action-1").model_dump(mode="json")

    assert "args" not in payload
    assert "target" not in payload
    assert "selector" not in payload
    assert "reasons" not in payload
    assert "consequences" not in payload


def test_event_store_snapshot_restore_preserves_sequence_continuity():
    async def run():
        first = InMemoryLifecycleEventStore()
        await first.append(_event("action-1"))
        await first.append(_event("action-2"))

        restored = InMemoryLifecycleEventStore()
        await restored.restore(await first.snapshot())
        appended = await restored.append(_event("action-3"))

        assert appended.sequence == 3
        assert [event.action_id for event in await restored.list()] == ["action-1", "action-2", "action-3"]

    asyncio.run(run())


def test_event_store_wait_unblocks_for_matching_owner_and_times_out_safely():
    async def run():
        store = InMemoryLifecycleEventStore()
        waiter = asyncio.create_task(store.wait(owner=_owner(), timeout_seconds=1))
        await asyncio.sleep(0)
        await store.append(_event("hidden", owner=_owner("other-agent")))
        await store.append(_event("action-1"))

        events = await waiter
        timed_out = await store.wait(owner=_owner(), after_sequence=events[-1].sequence, timeout_seconds=0.01)

        assert [event.action_id for event in events] == ["action-1"]
        assert timed_out == []

    asyncio.run(run())
