"""Tests for persistent computer-use JSONL audit storage."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest

from vilagent.computer_use.audit import JsonlComputerUseAuditStore
from vilagent.computer_use.models import AuditEventType, ComputerUseAuditEvent


def _event(index: int) -> ComputerUseAuditEvent:
    return ComputerUseAuditEvent(
        event_id=uuid.uuid4().hex,
        event_type=AuditEventType.action_requested,
        session_id="session-1",
        action_id=f"action-{index}",
    )


def test_jsonl_audit_store_persists_concurrent_events(tmp_path):
    async def run():
        store = JsonlComputerUseAuditStore(tmp_path)
        await asyncio.gather(*(store.append(_event(index)) for index in range(10)))

        events = await store.list_session("session-1")

        assert len(events) == 10
        assert {event.action_id for event in events} == {f"action-{index}" for index in range(10)}

    asyncio.run(run())


def test_jsonl_audit_store_rejects_unsafe_session_id(tmp_path):
    store = JsonlComputerUseAuditStore(tmp_path)

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(store.list_session("../escape"))


def test_jsonl_audit_store_offloads_file_io(tmp_path):
    async def run():
        calls = []
        original = asyncio.to_thread

        async def spy(function, *args, **kwargs):
            calls.append(function.__name__)
            return await original(function, *args, **kwargs)

        store = JsonlComputerUseAuditStore(tmp_path)
        with patch("asyncio.to_thread", new=spy):
            await store.append(_event(1))
            await store.list_session("session-1")

        assert calls == ["_append_sync", "_list_sync"]

    asyncio.run(run())
