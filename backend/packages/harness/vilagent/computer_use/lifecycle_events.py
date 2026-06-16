"""Bounded, sanitized lifecycle events for live computer-use status."""

from __future__ import annotations

import asyncio
from collections import deque

from vilagent.computer_use.models import ActionOwner, ComputerUseLifecycleEvent


class InMemoryLifecycleEventStore:
    """Process-local ordered event store with bounded retention."""

    def __init__(self, *, max_events: int = 10_000):
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: deque[ComputerUseLifecycleEvent] = deque(maxlen=max_events)
        self._next_sequence = 1
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)

    async def append(self, event: ComputerUseLifecycleEvent) -> ComputerUseLifecycleEvent:
        async with self._condition:
            stored = event.model_copy(deep=True, update={"sequence": self._next_sequence})
            self._next_sequence += 1
            self._events.append(stored)
            self._condition.notify_all()
            return stored.model_copy(deep=True)

    async def list(
        self,
        *,
        owner: ActionOwner | None = None,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ComputerUseLifecycleEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        async with self._lock:
            return self._list_locked(owner=owner, session_id=session_id, after_sequence=after_sequence, limit=limit)

    async def wait(
        self,
        *,
        owner: ActionOwner | None = None,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        timeout_seconds: float = 20,
    ) -> list[ComputerUseLifecycleEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        async with self._condition:
            def select() -> list[ComputerUseLifecycleEvent]:
                return self._list_locked(owner=owner, session_id=session_id, after_sequence=after_sequence, limit=limit)

            events = select()
            if events:
                return events
            try:
                await asyncio.wait_for(self._condition.wait_for(lambda: bool(select())), timeout=timeout_seconds)
            except TimeoutError:
                return []
            return select()

    async def snapshot(self) -> list[ComputerUseLifecycleEvent]:
        async with self._lock:
            return [event.model_copy(deep=True) for event in self._events]

    async def restore(self, events: list[ComputerUseLifecycleEvent]) -> None:
        async with self._condition:
            ordered = sorted(events, key=lambda event: event.sequence)
            self._events.clear()
            self._events.extend(event.model_copy(deep=True) for event in ordered)
            self._next_sequence = ordered[-1].sequence + 1 if ordered else 1
            self._condition.notify_all()

    def _list_locked(
        self,
        *,
        owner: ActionOwner | None,
        session_id: str | None,
        after_sequence: int,
        limit: int,
    ) -> list[ComputerUseLifecycleEvent]:
        return [
            event.model_copy(deep=True)
            for event in self._events
            if event.sequence > after_sequence
            and (owner is None or event.owner == owner)
            and (session_id is None or event.session_id == session_id)
        ][:limit]
